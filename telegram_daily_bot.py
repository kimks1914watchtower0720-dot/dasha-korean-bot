"""
다샤의 한국어 30일 — 텔레그램 매일 오전 11시 자동 발송 봇
Telegram bot: sends today's Korean lesson (grammar / vocab / sentences) at 11:00 daily.

배포가 필요한 이유 (왜 이 코드는 채팅 아티팩트 안에서 실행되지 않는가):
  실제로 텔레그램 서버에 메시지를 보내려면 (1) 봇 토큰을 안전하게 보관하는 서버,
  (2) 매일 정해진 시각에 깨어나는 스케줄러(cron/서버리스), (3) 구독자 chat_id 저장소가
  필요합니다. 이 파일은 그 세 가지를 갖춘 실제 서버(예: 저렴한 VPS, 또는 Render/Railway
  같은 서비스, 또는 AWS Lambda + EventBridge)에 배포해서 사용하세요.

필요 패키지:
    pip install python-telegram-bot==21.* apscheduler pytz

사용 순서:
  1. 텔레그램 @BotFather 에게 /newbot 으로 봇 생성 → 토큰 발급
  2. 아래 BOT_TOKEN 에 토큰 입력 (또는 환경변수 TG_BOT_TOKEN 사용)
  3. curriculum.json 을 이 파일과 같은 폴더에 둔다 (30일 콘텐츠, 계속 채워나가는 파일)
  4. subscribers.json 은 자동 생성됨 — 사용자가 봇에게 /start 하면 자동 등록
  5. 서버에서 `python telegram_daily_bot.py` 로 상시 실행 (systemd/pm2 등으로 관리 권장)
"""

import json
import os
import logging
import asyncio
import hmac
import html
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dasha-korean-bot")

BASE_DIR = Path(__file__).parent
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "여기에_봇토큰_입력")
CURRICULUM_PATH = BASE_DIR / "curriculum.json"
# subscribers.json 은 배포 때 사라지지 않도록 볼륨(DATA_DIR)에 저장한다.
# DATA_DIR 환경변수가 없으면 기존처럼 코드 폴더를 쓴다.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SUBSCRIBERS_PATH = DATA_DIR / "subscribers.json"
KST = pytz.timezone("Asia/Seoul")

# 무료 체험 범위: DAY 1 ~ FREE_DAYS 까지는 요금제와 무관하게 발송,
# 그 이후 DAY 부터는 plan == "premium" 인 구독자에게만 발송한다.
FREE_DAYS = 7

# 프리미엄 전환 요청 알림을 받을 관리자 chat_id (Railway 환경변수로 덮어쓸 수 있음)
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "6062717977")

# 결제 안내 값 — Railway 환경변수로 언제든 바꿀 수 있다.
PRICE_MONTH = os.environ.get("PRICE_MONTH", "500")
PRICE_YEAR = os.environ.get("PRICE_YEAR", "5000")
PAYMENT_ACCOUNT = os.environ.get("PAYMENT_ACCOUNT", "1234567890")

# ---------- 저장소 (subscribers.json: {"chat_id": {"day": 3, "plan": "premium"}}) ----------

def load_json(path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_subscribers():
    return load_json(SUBSCRIBERS_PATH, {})

def save_subscribers(subs):
    save_json(SUBSCRIBERS_PATH, subs)

def get_curriculum():
    # 사이트의 DAYS_FULL 데이터와 같은 구조로 계속 채워나가면 됩니다.
    return load_json(CURRICULUM_PATH, {})


def is_locked(info, day):
    """DAY 가 무료 범위를 넘고 프리미엄이 아니면 잠금."""
    return day > FREE_DAYS and info.get("plan") != "premium"


def payment_message():
    """/premium 을 누른 사람에게 보낼 결제 안내."""
    return (
        "💳 프리미엄 구독 안내\n"
        f"・1개월 구독: {PRICE_MONTH} 루블\n"
        f"・1년 구독: {PRICE_YEAR} 루블\n\n"
        f"입금 계좌: {PAYMENT_ACCOUNT}\n"
        "입금이 확인되면 프리미엄이 활성화됩니다.\n\n"
        "💳 Премиум-подписка\n"
        f"• 1 месяц — {PRICE_MONTH} руб.\n"
        f"• 1 год — {PRICE_YEAR} руб.\n\n"
        f"Счёт для оплаты: {PAYMENT_ACCOUNT}\n"
        "После подтверждения оплаты премиум будет активирован."
    )


def paywall_message():
    return (
        f"🔒 무료 체험은 DAY {FREE_DAYS}까지입니다.\n"
        f"DAY {FREE_DAYS + 1}부터는 프리미엄 이용권이 필요해요.\n\n"
        f"🔒 Бесплатный доступ — до DAY {FREE_DAYS}.\n"
        f"С DAY {FREE_DAYS + 1} нужна премиум-подписка.\n\n"
        "프리미엄 전환을 원하시면 /premium 을 눌러주세요.\n"
        "Чтобы оформить премиум, нажмите /premium."
    )


# ---------- 텔레그램 명령어 ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = get_subscribers()
    info = subs.setdefault(chat_id, {"day": 0, "plan": "free"})
    u = update.effective_user
    info["name"] = " ".join(x for x in [getattr(u, "first_name", None), getattr(u, "last_name", None)] if x)
    info["username"] = getattr(u, "username", "") or ""
    save_subscribers(subs)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🌱 첫날 시작하기 / Начать DAY 1", callback_data="start_day1")]]
    )
    await update.message.reply_text(
        "🎉 무료 체험 일주일, 오신 것을 환영합니다!\n"
        "다샤의 한국어 30일 봇입니다.\n"
        "매일 오전 11시(KST)에 그날의 문법·단어·예문과 숙제를 보내드려요.\n\n"
        "🎉 Добро пожаловать! Неделя бесплатного доступа.\n"
        "Это бот «Корейский за 30 дней с Дашей».\n"
        "Каждый день в 11:00 (по Сеулу) вы получите урок и домашнее задание.\n\n"
        "아래 버튼을 누르면 DAY 1이 바로 시작됩니다.\n"
        "Нажмите кнопку ниже, чтобы начать DAY 1.",
        reply_markup=keyboard,
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = get_subscribers()
    subs.setdefault(chat_id, {"day": 0, "plan": "free"})
    # /today 는 진도를 올리지 않고 "오늘의 DAY"를 다시 보여준다.
    # DAY 진행은 매일 11시 자동발송이만 담당한다.
    day = subs[chat_id].get("day", 0)
    if day < 1:
        day = 1
        subs[chat_id]["day"] = 1  # 가입 직후에는 DAY 1을 바로 보여준다
    if is_locked(subs[chat_id], day):
        save_subscribers(subs)
        await update.message.reply_text(paywall_message())
        return
    text = build_lesson_message(day)
    await update.message.reply_text(text, parse_mode="HTML")
    save_subscribers(subs)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = get_subscribers()
    subs.pop(chat_id, None)
    save_subscribers(subs)
    await update.message.reply_text("알림을 중단했습니다. 다시 시작하려면 /start 를 입력하세요.")


async def premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """사용자가 /premium 을 누르면 관리자에게 전환 요청 알림을 보낸다."""
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    subs = get_subscribers()
    info = subs.setdefault(chat_id, {"day": 0, "plan": "free"})
    save_subscribers(subs)

    await update.message.reply_text(payment_message())

    if not ADMIN_CHAT_ID:
        return
    name = " ".join(x for x in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if x)
    username = f"@{user.username}" if getattr(user, "username", None) else "(없음)"
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                "🔔 프리미엄 전환 요청이 들어왔습니다.\n"
                f"이름: {name or '(없음)'}\n"
                f"아이디: {username}\n"
                f"chat_id: {chat_id}\n"
                f"현재 DAY: {info.get('day', 0)} / plan: {info.get('plan', 'free')}"
            ),
        )
    except Exception as e:
        log.warning("관리자 알림 실패: %s", e)


async def start_day1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """첫날 시작하기 버튼 — DAY 1 레슨을 바로 보낸다."""
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    subs = get_subscribers()
    info = subs.setdefault(chat_id, {"day": 0, "plan": "free"})
    if info.get("day", 0) < 1:
        info["day"] = 1
    save_subscribers(subs)
    await query.message.reply_text(build_lesson_message(1), parse_mode="HTML")


ADMIN_PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>학생 관리</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e8eaed;--mut:#9aa2b1;--acc:#7c5cff;--ok:#2ea36b;--warn:#c9a227}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:22px;margin:0 0 6px}
.meta{color:var(--mut);font-size:13px;margin-bottom:20px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:11px 12px;border-bottom:1px solid var(--line);font-size:14px;text-align:left;vertical-align:middle}
th{color:var(--mut);font-weight:600;font-size:12px;letter-spacing:.03em;text-transform:uppercase}
tr:last-child td{border-bottom:0}
.badge{padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600}
.free{background:rgba(154,162,177,.18);color:#c3c9d4}
.premium{background:rgba(46,163,107,.18);color:#59d39a}
button{padding:7px 11px;border-radius:8px;border:1px solid var(--line);background:#0d0f14;color:var(--fg);font-size:13px;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
button:disabled{opacity:.5;cursor:default}
input[type=number],select{width:74px;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:#0d0f14;color:var(--fg);font-size:13px}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#0d0f14;border:1px solid var(--line);padding:10px 16px;border-radius:10px;font-size:14px;opacity:0;transition:opacity .2s}
.toast.on{opacity:1}
.sub{color:var(--mut);font-size:12px}
</style></head><body><div class="wrap">
<h1>학생 관리 <a href="/logout" style="float:right;font-size:13px;font-weight:400;color:#9aa2b1;text-decoration:none">로그아웃</a></h1>
<div class="meta" id="meta">불러오는 중...</div>
<table><thead><tr><th>chat_id</th><th>이름</th><th>요금제</th><th>진도(DAY)</th><th>마지막 발송</th><th>발송</th></tr></thead><tbody id="tb"></tbody></table>
<div class="toast" id="toast"></div>
</div>
<script>
var TOKEN = new URLSearchParams(location.search).get("token") || "";
var DAYS = [];
function q(p){ return TOKEN ? p + (p.indexOf("?")>-1?"&":"?") + "token=" + encodeURIComponent(TOKEN) : p; }
function toast(m){ var t=document.getElementById("toast"); t.textContent=m; t.classList.add("on"); setTimeout(function(){t.classList.remove("on");},2200); }
function post(path, body){ return fetch(q(path),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).then(function(r){return r.json();}); }
function load(){
  fetch(q("/api/list")).then(function(r){return r.json();}).then(function(d){
    DAYS = d.days || [];
    var subs = d.subs || {};
    var ids = Object.keys(subs);
    var free = 0, prem = 0;
    var tb = document.getElementById("tb"); tb.innerHTML = "";
    ids.forEach(function(id){
      var s = subs[id];
      if (s.plan === "premium") prem++; else free++;
      var tr = document.createElement("tr");
      var name = (s.name || "") + (s.username ? " (@" + s.username + ")" : "");
      var opts = DAYS.map(function(n){ return "<option value=" + n + (n === (s.day||0)+1 ? " selected" : "") + ">DAY " + n + "</option>"; }).join("");
      tr.innerHTML =
        "<td>" + id + "</td>" +
        "<td>" + (name || "<span class=sub>-</span>") + "</td>" +
        "<td><span class='badge " + (s.plan==="premium"?"premium":"free") + "'>" + (s.plan||"free") + "</span> <button data-act=plan data-id='" + id + "'>" + (s.plan==="premium"?"→ free":"→ premium") + "</button></td>" +
        "<td><div class=row><input type=number min=0 max=30 value='" + (s.day||0) + "' data-day='" + id + "'><button data-act=day data-id='" + id + "'>저장</button></div></td>" +
        "<td>" + (s.last_sent || "<span class=sub>-</span>") + "</td>" +
        "<td><div class=row><select data-send='" + id + "'>" + opts + "</select><button class=primary data-act=send data-id='" + id + "'>발송</button><label class=sub><input type=checkbox data-adv='" + id + "'> 진도 반영</label></div></td>";
      tb.appendChild(tr);
    });
    document.getElementById("meta").textContent = "전체 " + ids.length + "명 · 무료 " + free + "명 · 유료 " + prem + "명";
  });
}
document.addEventListener("click", function(e){
  var b = e.target.closest("button[data-act]"); if(!b) return;
  var id = b.getAttribute("data-id"); var act = b.getAttribute("data-act");
  b.disabled = true;
  if (act === "plan") {
    var cur = b.textContent.indexOf("premium") > -1 ? "premium" : "free";
    post("/api/plan", {chat_id:id, plan:cur}).then(function(r){ b.disabled=false; toast(r.ok?"요금제 변경됨":"실패: "+(r.error||"")); load(); });
  } else if (act === "day") {
    var v = document.querySelector("input[data-day='" + id + "']").value;
    post("/api/day", {chat_id:id, day:v}).then(function(r){ b.disabled=false; toast(r.ok?"진도 저장됨":"실패: "+(r.error||"")); load(); });
  } else if (act === "send") {
    var d = document.querySelector("select[data-send='" + id + "']").value;
    var adv = document.querySelector("input[data-adv='" + id + "']").checked;
    post("/api/send", {chat_id:id, day:d, advance:adv}).then(function(r){ b.disabled=false; toast(r.ok?("DAY "+d+" 발송 완료"):"실패: "+(r.error||"")); load(); });
  }
});
load();
setInterval(load, 30000);
</script></body></html>"""


# ---------- 관리자 대시보드 (IP 제한) ----------

# 비밀번호는 코드에 두지 않는다 (공개 저장소). Railway 환경변수 ADMIN_PASSWORD 를 읽는다.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_TTL = 12 * 60 * 60      # 로그인 유지 시간 (12시간)
MAX_TRIES = 8                   # 연속 실패 허용 횟수
LOCK_SECONDS = 10 * 60          # 초과 시 잠금 시간
_SESSIONS = {}                  # 토큰 -> 만료 시각
_TRIES = {}                     # IP -> [실패 횟수, 잠금 해제 시각]

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>학생 관리 · 로그인</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e8eaed;--mut:#9aa2b1;--acc:#7c5cff;--no:#c3453f}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif}
.box{width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px 24px}
h1{font-size:18px;margin:0 0 6px}
p.sub{color:var(--mut);font-size:13px;margin:0 0 20px}
input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);
background:#0d0f14;color:var(--fg);font-size:16px;letter-spacing:.15em;text-align:center}
button{width:100%;margin-top:12px;padding:12px;border-radius:10px;border:0;
background:var(--acc);color:#fff;font-size:15px;cursor:pointer}
.err{margin-top:14px;color:var(--no);font-size:13px;line-height:1.5}
</style></head><body>
<form class="box" method="post" action="/login" autocomplete="off">
  <h1>학생 관리</h1>
  <p class="sub">비밀번호를 입력하세요</p>
  <input type="password" name="password" inputmode="numeric" autofocus>
  <button type="submit">들어가기</button>
  <div class="err">__MSG__</div>
</form>
</body></html>"""



def tg_send_now(chat_id, text):
    """봇 이벤트 루프와 무관하게 텔레그램 API로 바로 보낸다."""
    data = urllib.parse.urlencode(
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML"}
    ).encode()
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/sendMessage"
    with urllib.request.urlopen(url, data, timeout=20) as r:
        return r.status


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "dasha-admin"

    def _ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _cookie_token(self):
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hr_admin":
                return v
        return ""

    def _auth(self):
        if not ADMIN_PASSWORD:
            return False, "ADMIN_PASSWORD 환경변수가 설정되지 않았습니다"
        tok = self._cookie_token()
        exp = _SESSIONS.get(tok)
        if not tok or not exp or exp < time.time():
            if tok:
                _SESSIONS.pop(tok, None)
            return False, ""
        return True, ""

    def _lock_left(self):
        rec = _TRIES.get(self._ip())
        if rec and rec[1] > time.time():
            return int(rec[1] - time.time())
        return 0

    def _login(self, msg="", code=200):
        page = LOGIN_PAGE.replace("__MSG__", html.escape(msg))
        return self._bytes(code, page.encode(), "text/html; charset=utf-8")

    def _do_login(self, raw):
        if not ADMIN_PASSWORD:
            return self._login("ADMIN_PASSWORD 환경변수가 설정되지 않았습니다", 503)
        left = self._lock_left()
        if left:
            return self._login("시도 횟수를 초과했습니다. " + str(left // 60 + 1) + "분 뒤에 다시 시도하세요.", 429)
        pw = parse_qs(raw.decode("utf-8", "ignore")).get("password", [""])[0]
        ip = self._ip()
        if hmac.compare_digest(pw, ADMIN_PASSWORD):
            _TRIES.pop(ip, None)
            now = time.time()
            for k, v in list(_SESSIONS.items()):
                if v < now:
                    _SESSIONS.pop(k, None)
            token = secrets.token_urlsafe(32)
            _SESSIONS[token] = now + SESSION_TTL
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                "hr_admin=" + token + "; Path=/; Max-Age=" + str(SESSION_TTL)
                + "; HttpOnly; SameSite=Lax; Secure",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rec = _TRIES.get(ip, [0, 0.0])
        rec[0] += 1
        if rec[0] >= MAX_TRIES:
            rec = [0, time.time() + LOCK_SECONDS]
            _TRIES[ip] = rec
            return self._login("시도 횟수를 초과했습니다. 10분 뒤에 다시 시도하세요.", 429)
        _TRIES[ip] = rec
        return self._login("비밀번호가 올바르지 않습니다. (남은 시도 " + str(MAX_TRIES - rec[0]) + "회)", 401)

    def _bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._bytes(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _deny(self, ip, why):
        page = (
            "<!DOCTYPE html><html lang=ko><head><meta charset=utf-8>"
            "<title>접근 차단</title><style>body{background:#0f1115;color:#e8eaed;"
            "font-family:system-ui,sans-serif;padding:40px;line-height:1.7}"
            "b{color:#7c5cff}</style></head><body>"
            "<h2>접근이 차단되었습니다</h2>"
            "<p>사유: " + html.escape(why) + "</p>"
            "<p>현재 접속 IP: <b>" + html.escape(ip) + "</b></p>"
            "<p>Railway 환경변수 <b>ADMIN_IPS</b> 에 이 IP를 넣으면 접속됩니다.</p>"
            "</body></html>"
        )
        self._bytes(403, page.encode(), "text/html; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._bytes(200, b"ok", "text/plain")
        if path == "/logout":
            _SESSIONS.pop(self._cookie_token(), None)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "hr_admin=; Path=/; Max-Age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ok, why = self._auth()
        if not ok:
            return self._login(why)
        if path == "/api/list":
            days = sorted(int(k) for k in get_curriculum().keys())
            return self._json(200, {"subs": get_subscribers(), "days": days})
        return self._bytes(200, ADMIN_PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/login":
            return self._do_login(raw)
        ok, why = self._auth()
        if not ok:
            return self._json(401, {"error": why or "로그인이 필요합니다"})
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        subs = get_subscribers()
        cid = str(body.get("chat_id", ""))

        if path == "/api/plan":
            if cid not in subs:
                return self._json(404, {"error": "등록되지 않은 chat_id"})
            subs[cid]["plan"] = "premium" if body.get("plan") == "premium" else "free"
            save_subscribers(subs)
            return self._json(200, {"ok": True, "sub": subs[cid]})

        if path == "/api/day":
            if cid not in subs:
                return self._json(404, {"error": "등록되지 않은 chat_id"})
            try:
                day = int(body.get("day", 0))
            except Exception:
                day = 0
            subs[cid]["day"] = max(0, min(30, day))
            save_subscribers(subs)
            return self._json(200, {"ok": True, "sub": subs[cid]})

        if path == "/api/send":
            try:
                day = int(body.get("day", 1))
            except Exception:
                day = 1
            try:
                status = tg_send_now(cid, build_lesson_message(day))
            except Exception as e:
                return self._json(500, {"error": str(e)})
            if body.get("advance") and cid in subs:
                subs[cid]["day"] = day
                subs[cid]["last_sent"] = datetime.now(KST).strftime("%Y-%m-%d")
                save_subscribers(subs)
            return self._json(200, {"ok": True, "status": status})

        return self._json(404, {"error": "알 수 없는 경로"})

    def log_message(self, *args):
        pass


def start_admin_server():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), AdminHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("관리자 대시보드 시작 — 포트 %s, 비밀번호 %s", port, "설정됨" if ADMIN_PASSWORD else "미설정(접속 불가)")



def build_lesson_message(day: int) -> str:
    curriculum = get_curriculum()
    lesson = curriculum.get(str(day))
    if not lesson:
        return f"DAY {day} 콘텐츠가 아직 준비되지 않았습니다. 조금만 기다려주세요! 🙏"
    lines = [f"📅 <b>DAY {day} · {lesson.get('title_kr','')}</b>", f"<i>{lesson.get('title_ru','')}</i>", ""]
    if lesson.get("homework"):
        lines.append(f"🎧 <b>Аудио + тест:</b> {lesson['homework']}")
        lines.append("")
    if lesson.get("grammar"):
        lines.append("📖 <b>Грамматика</b>")
        for g in lesson["grammar"]:
            lines.append(f"• {g['p']} — {g['ru']}")
        lines.append("")
    if lesson.get("vocab"):
        lines.append("🔤 <b>Слова</b>")
        for v in lesson["vocab"]:
            lines.append(f"• {v['kr']} — {v['ru']}")
        lines.append("")
    if lesson.get("sentences"):
        lines.append("💬 <b>Примеры</b>")
        for s in lesson["sentences"]:
            lines.append(f"• {s['kr']} — {s['ru']}")
    if lesson.get("video"):
        lines.append("")
        lines.append(f"🎬 영상 강의 / Видеоурок: {lesson['video']}")
    return "\n".join(lines)


# ---------- 매일 11시 자동 발송 ----------

async def send_daily_lessons(app):
    subs = get_subscribers()
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    log.info("자동 발송 시작: %s, 구독자 %d명", now, len(subs))
    for chat_id, info in subs.items():
        if info.get("last_sent") == today:
            continue  # 오늘 이미 보냈으면 건너뛴
        next_day = info.get("day", 0) + 1
        if is_locked(info, next_day):
            continue  # DAY 4 이상은 프리미엄 전용 (무료 사용자는 자동발송 제외)
        text = build_lesson_message(next_day)
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            info["day"] = next_day
            info["last_sent"] = today
        except Exception as e:
            log.warning("발송 실패 chat_id=%s: %s", chat_id, e)
    save_subscribers(subs)


async def _daily_loop(app):
    """매일 11:00(KST)까지 기다렸다가 발송하는 단순 루프.

    APScheduler 대신 직접 재우고 깨우는 방식이라 스레드/이벤트 루프 문제가 없다.
    """
    while True:
        now = datetime.now(KST)
        target = now.replace(hour=11, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        log.info("다음 자동 발송 예정: %s (%.0f초 뒤)", target, wait)
        await asyncio.sleep(wait)
        try:
            await send_daily_lessons(app)
        except Exception as e:
            log.warning("자동 발송 중 오류: %s", e)


async def _start_scheduler(app):
    # run_polling()이 이벤트 루프를 만든 "이후"에 시작해야 하므로 post_init 안에서 띄운다.
    if datetime.now(KST).hour >= 11:
        log.info("11시 이후 기동 — 오늘치 보충 발송을 시도합니다.")
        app.create_task(send_daily_lessons(app))
    app.create_task(_daily_loop(app))
    log.info("자동 발송 루프 시작됨. 매일 11:00(KST)에 발송됩니다.")


def main():
    start_admin_server()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_start_scheduler).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("premium", premium))
    app.add_handler(CallbackQueryHandler(start_day1, pattern="^start_day1$"))

    log.info("봇 시작됨.")
    app.run_polling()


if __name__ == "__main__":
    main()
