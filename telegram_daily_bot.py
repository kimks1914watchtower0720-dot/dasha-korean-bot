"""다샤의 한국어 30일 — 텔레그램 학습 봇 + 관리자 CMS

모든 레슨 콘텐츠는 SQLite DB 에서 온다. 봇 코드 안에 레슨 내용을 박지 않는다.

  관리자 웹  →  SQLite  →  텔레그램 봇  →  사용자

환경변수
  TG_BOT_TOKEN     텔레그램 봇 토큰 (필수)
  ADMIN_PASSWORD   관리자 페이지 비밀번호 (필수)
  DATA_DIR         데이터 저장 경로 (기본: 코드 폴더)
  ADMIN_CHAT_ID    알림 받을 관리자 chat_id
  FREE_DAYS        무료 공개 일수 (기본 7)
  PRICE_MONTH / PRICE_YEAR / PAYMENT_ACCOUNT
"""

import asyncio
import hmac
import html
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dasha-korean-bot")

BASE_DIR = Path(__file__).parent
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR = DATA_DIR / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "cms.db"
KST = pytz.timezone("Asia/Seoul")

FREE_DAYS = int(os.environ.get("FREE_DAYS", "7"))
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "6062717977")
PRICE_MONTH = os.environ.get("PRICE_MONTH", "500")
PRICE_YEAR = os.environ.get("PRICE_YEAR", "5000")
PAYMENT_ACCOUNT = os.environ.get("PAYMENT_ACCOUNT", "1234567890")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SESSION_TTL = 12 * 60 * 60
MAX_TRIES = 8
LOCK_SECONDS = 10 * 60
_SESSIONS = {}
_TRIES = {}

STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"


def now_kst():
    return datetime.now(KST)


def ts():
    return now_kst().strftime("%Y-%m-%d %H:%M:%S")

# ---------- DB ----------

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    day          INTEGER NOT NULL,
    title        TEXT DEFAULT '',
    title_ru     TEXT DEFAULT '',
    body         TEXT DEFAULT '',
    review       TEXT DEFAULT '',
    audio        TEXT DEFAULT '',
    files        TEXT DEFAULT '[]',
    link         TEXT DEFAULT '',
    scheduled_at TEXT DEFAULT '',
    status       TEXT DEFAULT 'draft',
    sort_order   INTEGER DEFAULT 0,
    sent_at      TEXT DEFAULT '',
    sent_body    TEXT DEFAULT '',
    updated_at   TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS users (
    chat_id    TEXT PRIMARY KEY,
    username   TEXT DEFAULT '',
    name       TEXT DEFAULT '',
    day        INTEGER DEFAULT 0,
    plan       TEXT DEFAULT 'free',
    active     INTEGER DEFAULT 1,
    created_at TEXT DEFAULT '',
    last_sent  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sends (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER,
    day       INTEGER,
    chat_id   TEXT,
    status    TEXT,
    error     TEXT DEFAULT '',
    kind      TEXT DEFAULT 'auto',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sends_lesson ON sends(lesson_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_day ON lessons(day);
"""


def init_db():
    with _db_lock:
        conn = db()
        conn.executescript(SCHEMA)
        conn.commit()
        migrate_legacy(conn)
        conn.close()


def migrate_legacy(conn):
    """기존 curriculum.json / subscribers.json 을 DB 로 한 번만 옮긴다."""
    n = conn.execute("SELECT COUNT(*) c FROM lessons").fetchone()["c"]
    if n == 0:
        path = BASE_DIR / "curriculum.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("curriculum.json 읽기 실패: %s", e)
                data = {}
            for key in sorted(data.keys(), key=lambda x: int(x)):
                d = data[key]
                conn.execute(
                    "INSERT OR IGNORE INTO lessons"
                    " (day,title,title_ru,body,review,link,status,sort_order,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        int(key),
                        d.get("title_kr", ""),
                        d.get("title_ru", ""),
                        legacy_body(d),
                        "",
                        d.get("homework", ""),
                        STATUS_DRAFT,
                        int(key),
                        ts(),
                    ),
                )
            conn.commit()
            log.info("커리큘럼 %d개를 DB로 옮겼습니다.", len(data))

    n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if n == 0:
        path = DATA_DIR / "subscribers.json"
        if path.exists():
            try:
                subs = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                log.warning("subscribers.json 읽기 실패: %s", e)
                subs = {}
            for cid, info in subs.items():
                conn.execute(
                    "INSERT OR IGNORE INTO users"
                    " (chat_id,username,name,day,plan,active,created_at,last_sent)"
                    " VALUES (?,?,?,?,?,1,?,?)",
                    (
                        str(cid),
                        info.get("username", ""),
                        info.get("name", ""),
                        int(info.get("day", 0) or 0),
                        info.get("plan", "free"),
                        ts(),
                        info.get("last_sent", ""),
                    ),
                )
            conn.commit()
            log.info("구독자 %d명을 DB로 옮겼습니다.", len(subs))


def legacy_body(d):
    """옛 curriculum.json 한 항목을 텔레그램 HTML 본문으로 바꾼다."""
    lines = []
    if d.get("grammar"):
        lines.append("\U0001F4D6 <b>\u0413\u0440\u0430\u043C\u043C\u0430\u0442\u0438\u043A\u0430</b>")
        for g in d["grammar"]:
            lines.append("\u2022 " + str(g.get("p", "")) + " \u2014 " + str(g.get("ru", "")))
        lines.append("")
    if d.get("vocab"):
        lines.append("\U0001F524 <b>\u0421\u043B\u043E\u0432\u0430</b>")
        for v in d["vocab"]:
            lines.append("\u2022 " + str(v.get("kr", "")) + " \u2014 " + str(v.get("ru", "")))
        lines.append("")
    if d.get("sentences"):
        lines.append("\U0001F4AC <b>\u041F\u0440\u0438\u043C\u0435\u0440\u044B</b>")
        for s in d["sentences"]:
            lines.append("\u2022 " + str(s.get("kr", "")) + " \u2014 " + str(s.get("ru", "")))
    return "\n".join(lines).strip()

# ---------- 레슨 / 사용자 조회 ----------


def row_to_lesson(r):
    d = dict(r)
    try:
        d["files"] = json.loads(d.get("files") or "[]")
    except Exception:
        d["files"] = []
    return d


def all_lessons():
    conn = db()
    rows = conn.execute(
        "SELECT * FROM lessons ORDER BY sort_order ASC, day ASC"
    ).fetchall()
    conn.close()
    return [row_to_lesson(r) for r in rows]


def get_lesson(lesson_id):
    conn = db()
    r = conn.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    conn.close()
    return row_to_lesson(r) if r else None


def get_lesson_by_day(day):
    conn = db()
    r = conn.execute("SELECT * FROM lessons WHERE day=?", (int(day),)).fetchone()
    conn.close()
    return row_to_lesson(r) if r else None


def all_users(active_only=False):
    conn = db()
    q = "SELECT * FROM users"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY created_at ASC"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user(chat_id):
    conn = db()
    r = conn.execute("SELECT * FROM users WHERE chat_id=?", (str(chat_id),)).fetchone()
    conn.close()
    return dict(r) if r else None


def upsert_user(chat_id, **fields):
    chat_id = str(chat_id)
    with _db_lock:
        conn = db()
        row = conn.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (chat_id,created_at) VALUES (?,?)", (chat_id, ts())
            )
        if fields:
            cols = ", ".join(k + "=?" for k in fields)
            conn.execute(
                "UPDATE users SET " + cols + " WHERE chat_id=?",
                tuple(fields.values()) + (chat_id,),
            )
        conn.commit()
        conn.close()


def log_send(lesson, chat_id, status, error="", kind="auto"):
    with _db_lock:
        conn = db()
        conn.execute(
            "INSERT INTO sends (lesson_id,day,chat_id,status,error,kind,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                lesson.get("id") if lesson else None,
                lesson.get("day") if lesson else None,
                str(chat_id),
                status,
                str(error)[:500],
                kind,
                ts(),
            ),
        )
        conn.commit()
        conn.close()


# ---------- 텔레그램 API ----------


class TelegramError(Exception):
    pass


def tg_api(method, payload):
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/" + method
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TelegramError(e.read().decode("utf-8", "ignore")[:300])
    except Exception as e:
        raise TelegramError(str(e))


def tg_api_file(method, fields, filename, filedata, field_name="audio"):
    """multipart/form-data 로 파일을 올린다 (외부 라이브러리 없이)."""
    boundary = "----dasha" + uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += ("--" + boundary + "\r\n").encode()
        body += ('Content-Disposition: form-data; name="' + k + '"\r\n\r\n').encode()
        body += (str(v) + "\r\n").encode()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body += ("--" + boundary + "\r\n").encode()
    body += (
        'Content-Disposition: form-data; name="' + field_name + '"; filename="'
        + filename + '"\r\n'
    ).encode()
    body += ("Content-Type: " + ctype + "\r\n\r\n").encode()
    body += filedata
    body += ("\r\n--" + boundary + "--\r\n").encode()
    url = "https://api.telegram.org/bot" + BOT_TOKEN + "/" + method
    req = urllib.request.Request(url, data=bytes(body))
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TelegramError(e.read().decode("utf-8", "ignore")[:300])
    except Exception as e:
        raise TelegramError(str(e))


def tg_text(chat_id, text):
    return tg_api(
        "sendMessage",
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"},
    )


def tg_audio(chat_id, path, caption=""):
    data = Path(path).read_bytes()
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1000]
    return tg_api_file("sendAudio", fields, Path(path).name, data, "audio")


def tg_document(chat_id, path):
    data = Path(path).read_bytes()
    return tg_api_file("sendDocument", {"chat_id": str(chat_id)}, Path(path).name, data, "document")

# ---------- 메시지 구성 ----------


def render_lesson(lesson):
    """레슨 하나를 텔레그램 본문 HTML 로 만든다. DB 내용만 사용한다."""
    parts = []
    head = "\U0001F4C5 <b>DAY " + str(lesson.get("day")) + ""
    if lesson.get("title"):
        head += " \u00b7 " + html.escape(lesson["title"])
    head += "</b>"
    parts.append(head)
    if lesson.get("title_ru"):
        parts.append("<i>" + html.escape(lesson["title_ru"]) + "</i>")
    if lesson.get("link"):
        parts.append("")
        parts.append("\U0001F3A7 <b>\u0410\u0443\u0434\u0438\u043E + \u0442\u0435\u0441\u0442:</b> " + lesson["link"])
    if lesson.get("body"):
        parts.append("")
        parts.append(lesson["body"])
    return "\n".join(parts).strip()


def is_locked(user, day):
    return int(day) > FREE_DAYS and (user or {}).get("plan") != "premium"


def paywall_text():
    return (
        "\U0001F512 \ubb34\ub8cc \uccb4\ud5d8\uc740 DAY " + str(FREE_DAYS) + "\uae4c\uc9c0\uc785\ub2c8\ub2e4.\n"
        "DAY " + str(FREE_DAYS + 1) + "\ubd80\ud130\ub294 \ud504\ub9ac\ubbf8\uc5c4 \uc774\uc6a9\uad8c\uc774 \ud544\uc694\ud574\uc694.\n\n"
        "\U0001F512 \u0411\u0435\u0441\u043F\u043B\u0430\u0442\u043D\u044B\u0439 \u0434\u043E\u0441\u0442\u0443\u043F \u2014 \u0434\u043E DAY " + str(FREE_DAYS) + ".\n"
        "\u0421 DAY " + str(FREE_DAYS + 1) + " \u043D\u0443\u0436\u043D\u0430 \u043F\u0440\u0435\u043C\u0438\u0443\u043C-\u043F\u043E\u0434\u043F\u0438\u0441\u043A\u0430.\n\n"
        "\ud504\ub9ac\ubbf8\uc5c4 \uc804\ud658\uc744 \uc6d0\ud558\uc2dc\uba74 /premium \uc744 \ub20c\ub7ec\uc8fc\uc138\uc694.\n"
        "\u0427\u0442\u043E\u0431\u044B \u043E\u0444\u043E\u0440\u043C\u0438\u0442\u044C \u043F\u0440\u0435\u043C\u0438\u0443\u043C, \u043D\u0430\u0436\u043C\u0438\u0442\u0435 /premium."
    )


def payment_text():
    return (
        "\U0001F4B3 \ud504\ub9ac\ubbf8\uc5c4 \uad6c\ub3c5 \uc548\ub0b4\n"
        "\u30fb1\uac1c\uc6d4 \uad6c\ub3c5: " + PRICE_MONTH + " \ub8e8\ube14\n"
        "\u30fb1\ub144 \uad6c\ub3c5: " + PRICE_YEAR + " \ub8e8\ube14\n\n"
        "\uc785\uae08 \uacc4\uc88c: " + PAYMENT_ACCOUNT + "\n"
        "\uc785\uae08\uc774 \ud655\uc778\ub418\uba74 \ud504\ub9ac\ubbf8\uc5c4\uc774 \ud65c\uc131\ud654\ub429\ub2c8\ub2e4.\n\n"
        "\U0001F4B3 \u041F\u0440\u0435\u043C\u0438\u0443\u043C-\u043F\u043E\u0434\u043F\u0438\u0441\u043A\u0430\n"
        "\u2022 1 \u043C\u0435\u0441\u044F\u0446 \u2014 " + PRICE_MONTH + " \u0440\u0443\u0431.\n"
        "\u2022 1 \u0433\u043E\u0434 \u2014 " + PRICE_YEAR + " \u0440\u0443\u0431.\n\n"
        "\u0421\u0447\u0451\u0442 \u0434\u043B\u044F \u043E\u043F\u043B\u0430\u0442\u044B: " + PAYMENT_ACCOUNT
    )


# ---------- 발송 ----------


def send_lesson_to(chat_id, lesson, kind="auto", include_media=True):
    """레슨 한 건을 한 사람에게 보낸다. 실패하면 sends 에 기록하고 예외를 올린다."""
    try:
        tg_text(chat_id, render_lesson(lesson))
        if lesson.get("review"):
            tg_text(chat_id, lesson["review"])
        if include_media and lesson.get("audio"):
            p = MEDIA_DIR / lesson["audio"]
            if p.exists():
                cap = "DAY " + str(lesson.get("day"))
                if lesson.get("title"):
                    cap += " \u00b7 " + lesson["title"]
                tg_audio(chat_id, p, cap)
        if include_media:
            for name in lesson.get("files") or []:
                p = MEDIA_DIR / name
                if p.exists():
                    tg_document(chat_id, p)
        log_send(lesson, chat_id, "ok", "", kind)
        return True, ""
    except Exception as e:
        log_send(lesson, chat_id, "error", str(e), kind)
        log.warning("발송 실패 chat_id=%s day=%s: %s", chat_id, lesson.get("day"), e)
        return False, str(e)


def broadcast_lesson(lesson, kind="auto", only_chat_ids=None):
    """레슨을 대상자 전원에게 보낸다. (ok, fail, skipped) 반환"""
    ok = fail = skipped = 0
    users = all_users(active_only=True)
    for u in users:
        if only_chat_ids and str(u["chat_id"]) not in only_chat_ids:
            continue
        if is_locked(u, lesson.get("day") or 0):
            skipped += 1
            continue
        good, _ = send_lesson_to(u["chat_id"], lesson, kind)
        if good:
            ok += 1
            upsert_user(
                u["chat_id"],
                day=int(lesson.get("day") or u.get("day") or 0),
                last_sent=now_kst().strftime("%Y-%m-%d"),
            )
        else:
            fail += 1
    return ok, fail, skipped


def mark_sent(lesson):
    with _db_lock:
        conn = db()
        conn.execute(
            "UPDATE lessons SET status=?, sent_at=?, sent_body=? WHERE id=?",
            (STATUS_SENT, ts(), render_lesson(lesson), lesson["id"]),
        )
        conn.commit()
        conn.close()


def due_lessons():
    """예약 시각이 지난 scheduled 레슨 목록."""
    now = now_kst().strftime("%Y-%m-%d %H:%M")
    conn = db()
    rows = conn.execute(
        "SELECT * FROM lessons WHERE status=? AND scheduled_at<>'' AND scheduled_at<=?"
        " ORDER BY scheduled_at ASC",
        (STATUS_SCHEDULED, now),
    ).fetchall()
    conn.close()
    return [row_to_lesson(r) for r in rows]


async def scheduler_loop():
    log.info("예약 발송 루프 시작 (60초 간격)")
    while True:
        try:
            for lesson in due_lessons():
                log.info("예약 발송: DAY %s (%s)", lesson["day"], lesson["scheduled_at"])
                ok, fail, skipped = broadcast_lesson(lesson, "scheduled")
                mark_sent(lesson)
                log.info("DAY %s 발송 완료 — 성공 %d, 실패 %d, 제외 %d",
                         lesson["day"], ok, fail, skipped)
                if ADMIN_CHAT_ID:
                    try:
                        tg_text(
                            ADMIN_CHAT_ID,
                            "\U0001F4E4 DAY " + str(lesson["day"]) + " \uc608\uc57d \ubc1c\uc1a1 \uc644\ub8cc\n"
                            "\uc131\uacf5 " + str(ok) + " / \uc2e4\ud328 " + str(fail)
                            + " / \uc81c\uc678 " + str(skipped),
                        )
                    except Exception:
                        pass
        except Exception as e:
            log.warning("스케줄러 오류: %s", e)
        await asyncio.sleep(60)

# ---------- 텔레그램 봇 명령어 ----------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    u = update.effective_user
    name = " ".join(x for x in [getattr(u, "first_name", None), getattr(u, "last_name", None)] if x)
    upsert_user(chat_id, name=name, username=getattr(u, "username", "") or "", active=1)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("\U0001F331 \uccab\ub0a0 \uc2dc\uc791\ud558\uae30 / \u041D\u0430\u0447\u0430\u0442\u044C DAY 1", callback_data="start_day1")]]
    )
    await update.message.reply_text(
        "\U0001F389 \ubb34\ub8cc \uccb4\ud5d8 \uc77c\uc8fc\uc77c, \uc624\uc2e0 \uac83\uc744 \ud658\uc601\ud569\ub2c8\ub2e4!\n"
        "\ub2e4\uc0e4\uc758 \ud55c\uad6d\uc5b4 30\uc77c \ubd07\uc785\ub2c8\ub2e4.\n\n"
        "\U0001F389 \u0414\u043E\u0431\u0440\u043E \u043F\u043E\u0436\u0430\u043B\u043E\u0432\u0430\u0442\u044C! \u041D\u0435\u0434\u0435\u043B\u044F \u0431\u0435\u0441\u043F\u043B\u0430\u0442\u043D\u043E\u0433\u043E \u0434\u043E\u0441\u0442\u0443\u043F\u0430.\n\n"
        "\uc544\ub798 \ubc84\ud2bc\uc744 \ub204\ub974\uba74 DAY 1\uc774 \ubc14\ub85c \uc2dc\uc791\ub429\ub2c8\ub2e4.\n"
        "\u041D\u0430\u0436\u043C\u0438\u0442\u0435 \u043A\u043D\u043E\u043F\u043A\u0443 \u043D\u0438\u0436\u0435, \u0447\u0442\u043E\u0431\u044B \u043D\u0430\u0447\u0430\u0442\u044C DAY 1.",
        reply_markup=keyboard,
    )


async def cb_start_day1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    lesson = get_lesson_by_day(1)
    if not lesson:
        await query.message.reply_text("DAY 1 \ub0b4\uc6a9\uc774 \uc544\uc9c1 \uc900\ube44\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4.")
        return
    upsert_user(chat_id, day=1)
    await asyncio.to_thread(send_lesson_to, chat_id, lesson, "manual")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    upsert_user(chat_id)
    user = get_user(chat_id) or {}
    day = int(user.get("day") or 0)
    if day < 1:
        day = 1
        upsert_user(chat_id, day=1)
    if is_locked(user, day):
        await update.message.reply_text(paywall_text())
        return
    lesson = get_lesson_by_day(day)
    if not lesson:
        await update.message.reply_text("DAY " + str(day) + " \ucf58\ud150\uce20\uac00 \uc544\uc9c1 \uc5c6\uc2b5\ub2c8\ub2e4.")
        return
    await asyncio.to_thread(send_lesson_to, chat_id, lesson, "manual")


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    u = update.effective_user
    upsert_user(chat_id)
    user = get_user(chat_id) or {}
    await update.message.reply_text(payment_text())
    if not ADMIN_CHAT_ID:
        return
    name = " ".join(x for x in [getattr(u, "first_name", None), getattr(u, "last_name", None)] if x)
    uname = "@" + u.username if getattr(u, "username", None) else "(\uc5c6\uc74c)"
    try:
        tg_text(
            ADMIN_CHAT_ID,
            "\U0001F514 \ud504\ub9ac\ubbf8\uc5c4 \uc804\ud658 \uc694\uccad\n"
            "\uc774\ub984: " + (name or "(\uc5c6\uc74c)") + "\n"
            "\uc544\uc774\ub514: " + uname + "\n"
            "chat_id: " + chat_id + "\n"
            "\ud604\uc7ac DAY: " + str(user.get("day", 0)) + " / plan: " + str(user.get("plan", "free")),
        )
    except Exception as e:
        log.warning("관리자 알림 실패: %s", e)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    upsert_user(chat_id, active=0)
    await update.message.reply_text("\uc54c\ub9bc\uc744 \uc911\ub2e8\ud588\uc2b5\ub2c8\ub2e4. \ub2e4\uc2dc \uc2dc\uc791\ud558\ub824\uba74 /start \ub97c \uc785\ub825\ud558\uc138\uc694.")

# ---------- 관리자 서버 ----------

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>관리자 로그인</title>
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
  <h1>한국어365 관리자</h1>
  <p class="sub">비밀번호를 입력하세요</p>
  <input type="password" name="password" autofocus>
  <button type="submit">들어가기</button>
  <div class="err">__MSG__</div>
</form>
</body></html>"""


def parse_multipart(body, content_type):
    """multipart/form-data 최소 파서. {name: (filename, bytes)} 와 {name: str} 을 돌려준다."""
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        return {}, {}
    boundary = ("--" + m.group(1).strip().strip('"')).encode()
    fields, files = {}, {}
    for part in body.split(boundary):
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        if not _:
            continue
        data = data.rstrip(b"\r\n")
        head_s = head.decode("utf-8", "ignore")
        nm = re.search(r'name="([^"]*)"', head_s)
        if not nm:
            continue
        name = nm.group(1)
        fm = re.search(r'filename="([^"]*)"', head_s)
        if fm:
            if fm.group(1):
                files[name] = (fm.group(1), data)
        else:
            fields[name] = data.decode("utf-8", "ignore")
    return fields, files


def safe_name(original):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(original or "file"))[-60:]
    return uuid.uuid4().hex[:8] + "_" + (base or "file")


class Admin(BaseHTTPRequestHandler):
    server_version = "dasha-cms"

    # --- 기본 응답 ---
    def _bytes(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._bytes(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    # --- 인증 ---
    def _ip(self):
        xff = self.headers.get("X-Forwarded-For", "")
        return xff.split(",")[0].strip() if xff else self.client_address[0]

    def _cookie_token(self):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
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

    def _login_page(self, msg="", code=200):
        page = LOGIN_PAGE.replace("__MSG__", html.escape(msg))
        self._bytes(code, page.encode(), "text/html; charset=utf-8")

    def _do_login(self, raw):
        if not ADMIN_PASSWORD:
            return self._login_page("ADMIN_PASSWORD 환경변수가 설정되지 않았습니다", 503)
        rec = _TRIES.get(self._ip())
        if rec and rec[1] > time.time():
            left = int(rec[1] - time.time()) // 60 + 1
            return self._login_page("시도 횟수를 초과했습니다. " + str(left) + "분 뒤에 다시 시도하세요.", 429)
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
            self.send_header("Set-Cookie", "hr_admin=" + token + "; Path=/; Max-Age="
                             + str(SESSION_TTL) + "; HttpOnly; SameSite=Lax; Secure")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        rec = _TRIES.get(ip, [0, 0.0])
        rec[0] += 1
        if rec[0] >= MAX_TRIES:
            _TRIES[ip] = [0, time.time() + LOCK_SECONDS]
            return self._login_page("시도 횟수를 초과했습니다. 10분 뒤에 다시 시도하세요.", 429)
        _TRIES[ip] = rec
        return self._login_page("비밀번호가 올바르지 않습니다. (남은 시도 "
                                + str(MAX_TRIES - rec[0]) + "회)", 401)

    def log_message(self, *args):
        pass

    # --- GET ---
    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
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
            return self._login_page(why)

        if path == "/api/lessons":
            return self._json(200, {"lessons": all_lessons(), "free_days": FREE_DAYS})
        if path == "/api/users":
            return self._json(200, {"users": all_users()})
        if path == "/api/sends":
            conn = db()
            rows = conn.execute(
                "SELECT * FROM sends ORDER BY id DESC LIMIT 300"
            ).fetchall()
            conn.close()
            return self._json(200, {"sends": [dict(r) for r in rows]})
        if path == "/api/preview":
            lesson = get_lesson(int(q.get("id", ["0"])[0] or 0))
            if not lesson:
                return self._json(404, {"error": "레슨을 찾을 수 없습니다"})
            return self._json(200, {"text": render_lesson(lesson),
                                    "review": lesson.get("review", ""),
                                    "audio": lesson.get("audio", ""),
                                    "files": lesson.get("files", [])})
        if path.startswith("/media/"):
            name = os.path.basename(path[len("/media/"):])
            p = MEDIA_DIR / name
            if not p.exists():
                return self._bytes(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self._bytes(200, p.read_bytes(), ctype)
        return self._bytes(200, ADMIN_PAGE.encode(), "text/html; charset=utf-8")

    # --- POST ---
    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/login":
            return self._do_login(raw)
        ok, why = self._auth()
        if not ok:
            return self._json(401, {"error": why or "로그인이 필요합니다"})

        ctype = self.headers.get("Content-Type", "")
        if path == "/api/upload":
            return self._upload(raw, ctype)

        try:
            b = json.loads(raw or b"{}")
        except Exception:
            b = {}

        if path == "/api/lesson/save":
            return self._save_lesson(b)
        if path == "/api/lesson/delete":
            return self._delete_lesson(b)
        if path == "/api/lesson/duplicate":
            return self._duplicate_lesson(b)
        if path == "/api/lesson/status":
            return self._set_status(b)
        if path == "/api/lesson/send":
            return self._send_lesson(b)
        if path == "/api/lesson/media-delete":
            return self._media_delete(b)
        if path == "/api/user/update":
            cid = str(b.get("chat_id", ""))
            if not get_user(cid):
                return self._json(404, {"error": "등록되지 않은 chat_id"})
            fields = {}
            if "plan" in b:
                fields["plan"] = "premium" if b["plan"] == "premium" else "free"
            if "day" in b:
                try:
                    fields["day"] = max(0, min(365, int(b["day"])))
                except Exception:
                    pass
            if "active" in b:
                fields["active"] = 1 if b["active"] else 0
            if fields:
                upsert_user(cid, **fields)
            return self._json(200, {"ok": True, "user": get_user(cid)})
        return self._json(404, {"error": "알 수 없는 경로"})

    # --- 레슨 처리 ---
    def _save_lesson(self, b):
        try:
            day = int(b.get("day") or 0)
        except Exception:
            return self._json(400, {"error": "DAY 번호가 올바르지 않습니다"})
        if day <= 0:
            return self._json(400, {"error": "DAY 번호는 1 이상이어야 합니다"})
        fields = {
            "day": day,
            "title": b.get("title", ""),
            "title_ru": b.get("title_ru", ""),
            "body": b.get("body", ""),
            "review": b.get("review", ""),
            "link": b.get("link", ""),
            "scheduled_at": (b.get("scheduled_at") or "").replace("T", " ")[:16],
            "status": b.get("status") if b.get("status") in
                      (STATUS_DRAFT, STATUS_SCHEDULED, STATUS_SENT) else STATUS_DRAFT,
            "sort_order": int(b.get("sort_order") or day),
            "updated_at": ts(),
        }
        lid = b.get("id")
        with _db_lock:
            conn = db()
            try:
                if lid:
                    cols = ", ".join(k + "=?" for k in fields)
                    conn.execute("UPDATE lessons SET " + cols + " WHERE id=?",
                                 tuple(fields.values()) + (int(lid),))
                else:
                    keys = ", ".join(fields)
                    marks = ", ".join("?" for _ in fields)
                    cur = conn.execute("INSERT INTO lessons (" + keys + ") VALUES (" + marks + ")",
                                       tuple(fields.values()))
                    lid = cur.lastrowid
                conn.commit()
            except sqlite3.IntegrityError:
                conn.close()
                return self._json(400, {"error": "이미 같은 DAY 번호의 레슨이 있습니다"})
            conn.close()
        return self._json(200, {"ok": True, "lesson": get_lesson(int(lid))})

    def _delete_lesson(self, b):
        lesson = get_lesson(int(b.get("id") or 0))
        if not lesson:
            return self._json(404, {"error": "레슨을 찾을 수 없습니다"})
        with _db_lock:
            conn = db()
            conn.execute("DELETE FROM lessons WHERE id=?", (lesson["id"],))
            conn.commit()
            conn.close()
        return self._json(200, {"ok": True})

    def _duplicate_lesson(self, b):
        src = get_lesson(int(b.get("id") or 0))
        if not src:
            return self._json(404, {"error": "레슨을 찾을 수 없습니다"})
        conn = db()
        mx = conn.execute("SELECT COALESCE(MAX(day),0) m FROM lessons").fetchone()["m"]
        conn.close()
        new_day = int(mx) + 1
        with _db_lock:
            conn = db()
            conn.execute(
                "INSERT INTO lessons (day,title,title_ru,body,review,link,files,status,sort_order,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (new_day, (src["title"] or "") + " (복사본)", src["title_ru"], src["body"],
                 src["review"], src["link"], json.dumps(src.get("files") or []),
                 STATUS_DRAFT, new_day, ts()),
            )
            conn.commit()
            conn.close()
        return self._json(200, {"ok": True, "day": new_day})

    def _set_status(self, b):
        lesson = get_lesson(int(b.get("id") or 0))
        if not lesson:
            return self._json(404, {"error": "레슨을 찾을 수 없습니다"})
        st = b.get("status")
        if st not in (STATUS_DRAFT, STATUS_SCHEDULED, STATUS_SENT):
            return self._json(400, {"error": "상태값이 올바르지 않습니다"})
        with _db_lock:
            conn = db()
            conn.execute("UPDATE lessons SET status=?, updated_at=? WHERE id=?",
                         (st, ts(), lesson["id"]))
            conn.commit()
            conn.close()
        return self._json(200, {"ok": True})

    def _send_lesson(self, b):
        lesson = get_lesson(int(b.get("id") or 0))
        if not lesson:
            return self._json(404, {"error": "레슨을 찾을 수 없습니다"})
        mode = b.get("mode", "test")
        if mode == "test":
            target = str(b.get("chat_id") or ADMIN_CHAT_ID)
            if not target:
                return self._json(400, {"error": "테스트 발송 대상이 없습니다"})
            good, err = send_lesson_to(target, lesson, "test")
            return self._json(200 if good else 500,
                              {"ok": good, "error": err, "target": target})
        if mode == "one":
            target = str(b.get("chat_id") or "")
            if not target:
                return self._json(400, {"error": "대상 chat_id 가 없습니다"})
            good, err = send_lesson_to(target, lesson, "manual")
            if good:
                upsert_user(target, day=int(lesson["day"]),
                            last_sent=now_kst().strftime("%Y-%m-%d"))
            return self._json(200 if good else 500, {"ok": good, "error": err})
        ok, fail, skipped = broadcast_lesson(lesson, "manual")
        mark_sent(lesson)
        return self._json(200, {"ok": True, "sent": ok, "failed": fail, "skipped": skipped})

    def _media_delete(self, b):
        lesson = get_lesson(int(b.get("id") or 0))
        if not lesson:
            return self._json(404, {"error": "레슨을 찾을 수 없습니다"})
        name = b.get("name", "")
        kind = b.get("kind", "audio")
        with _db_lock:
            conn = db()
            if kind == "audio":
                conn.execute("UPDATE lessons SET audio='' WHERE id=?", (lesson["id"],))
            else:
                files = [f for f in (lesson.get("files") or []) if f != name]
                conn.execute("UPDATE lessons SET files=? WHERE id=?",
                             (json.dumps(files), lesson["id"]))
            conn.commit()
            conn.close()
        try:
            p = MEDIA_DIR / os.path.basename(name or lesson.get("audio") or "")
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            pass
        return self._json(200, {"ok": True, "lesson": get_lesson(lesson["id"])})

    def _upload(self, raw, ctype):
        fields, files = parse_multipart(raw, ctype)
        try:
            lid = int(fields.get("id") or 0)
        except Exception:
            lid = 0
        lesson = get_lesson(lid)
        if not lesson:
            return self._json(404, {"error": "레슨을 먼저 저장해 주세요"})
        if not files:
            return self._json(400, {"error": "파일이 없습니다"})
        kind = fields.get("kind", "audio")
        saved = []
        for _, (fname, data) in files.items():
            if len(data) > 45 * 1024 * 1024:
                return self._json(400, {"error": "파일이 너무 큽니다 (45MB 이하)"})
            name = safe_name(fname)
            (MEDIA_DIR / name).write_bytes(data)
            saved.append(name)
        with _db_lock:
            conn = db()
            if kind == "audio":
                old = lesson.get("audio")
                conn.execute("UPDATE lessons SET audio=?, updated_at=? WHERE id=?",
                             (saved[0], ts(), lesson["id"]))
                if old and old != saved[0]:
                    try:
                        (MEDIA_DIR / old).unlink()
                    except Exception:
                        pass
            else:
                cur = (lesson.get("files") or []) + saved
                conn.execute("UPDATE lessons SET files=?, updated_at=? WHERE id=?",
                             (json.dumps(cur), ts(), lesson["id"]))
            conn.commit()
            conn.close()
        return self._json(200, {"ok": True, "lesson": get_lesson(lesson["id"])})

ADMIN_PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>한국어365 관리자</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e8eaed;--mut:#9aa2b1;--acc:#7c5cff;--ok:#2ea36b;--warn:#c9a227;--no:#c3453f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif;font-size:14px}
.wrap{max-width:1240px;margin:0 auto;padding:22px 18px 80px}
header{display:flex;align-items:center;gap:14px;margin-bottom:18px}
h1{font-size:19px;margin:0}
.tabs{display:flex;gap:6px;margin-left:auto}
.tabs button{padding:8px 14px;border-radius:9px;border:1px solid var(--line);background:#0d0f14;color:var(--mut);cursor:pointer}
.tabs button.on{background:var(--acc);border-color:var(--acc);color:#fff}
a.logout{color:var(--mut);text-decoration:none;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
tr:last-child td{border-bottom:0}
.badge{padding:3px 9px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.b-draft{background:rgba(154,162,177,.18);color:#c3c9d4}
.b-scheduled{background:rgba(201,162,39,.18);color:#e3c66a}
.b-sent{background:rgba(46,163,107,.18);color:#59d39a}
.b-free{background:rgba(154,162,177,.18);color:#c3c9d4}
.b-premium{background:rgba(46,163,107,.18);color:#59d39a}
button.b{padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:#0d0f14;color:var(--fg);cursor:pointer;font-size:13px}
button.b:hover{border-color:var(--acc)}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}
button.d{color:#e79a96}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.muted{color:var(--mut);font-size:12px}
.bar{display:flex;gap:8px;align-items:center;margin-bottom:12px}
input,select,textarea{padding:9px 11px;border-radius:9px;border:1px solid var(--line);background:#0d0f14;color:var(--fg);font-size:14px;font-family:inherit}
textarea{width:100%;min-height:150px;line-height:1.6;resize:vertical}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:flex-start;justify-content:center;padding:30px 16px;overflow:auto;z-index:20}
.modal.on{display:flex}
.sheet{width:100%;max-width:860px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px}
.sheet h2{margin:0 0 16px;font-size:17px}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
.grid label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:var(--mut)}
.grid.two{grid-template-columns:repeat(2,1fr)}
.full{grid-column:1/-1}
.foot{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
.foot .right{margin-left:auto;display:flex;gap:8px}
pre.prev{white-space:pre-wrap;word-break:break-word;background:#0d0f14;border:1px solid var(--line);border-radius:10px;padding:16px;line-height:1.7;max-height:60vh;overflow:auto}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#0d0f14;border:1px solid var(--line);padding:11px 18px;border-radius:10px;opacity:0;transition:opacity .2s;z-index:40}
.toast.on{opacity:1}
.hidden{display:none}
audio{width:260px;height:34px}
</style></head><body>
<div class="wrap">
  <header>
    <h1>한국어365 관리자</h1>
    <div class="tabs">
      <button id="t-lessons" class="on" onclick="tab('lessons')">레슨</button>
      <button id="t-users" onclick="tab('users')">학생</button>
      <button id="t-log" onclick="tab('log')">발송 기록</button>
    </div>
    <a class="logout" href="/logout">로그아웃</a>
  </header>

  <section id="p-lessons">
    <div class="bar">
      <button class="b p" onclick="openEditor(null)">+ 새 레슨</button>
      <span class="muted" id="lessonMeta"></span>
    </div>
    <div class="card"><table>
      <thead><tr><th>DAY</th><th>제목</th><th>예약 일시</th><th>상태</th><th>MP3</th><th>작업</th></tr></thead>
      <tbody id="lessonRows"></tbody>
    </table></div>
  </section>

  <section id="p-users" class="hidden">
    <div class="bar"><span class="muted" id="userMeta"></span></div>
    <div class="card"><table>
      <thead><tr><th>chat_id</th><th>이름</th><th>아이디</th><th>등록일</th><th>진도</th><th>요금제</th><th>상태</th><th>마지막 발송</th><th>작업</th></tr></thead>
      <tbody id="userRows"></tbody>
    </table></div>
  </section>

  <section id="p-log" class="hidden">
    <div class="bar"><span class="muted">최근 300건</span><button class="b" onclick="loadSends()">새로고침</button></div>
    <div class="card"><table>
      <thead><tr><th>시각</th><th>DAY</th><th>chat_id</th><th>구분</th><th>결과</th><th>오류</th><th></th></tr></thead>
      <tbody id="logRows"></tbody>
    </table></div>
  </section>
</div>

<div class="modal" id="editor"><div class="sheet">
  <h2 id="edTitle">레슨 편집</h2>
  <input type="hidden" id="f-id">
  <div class="grid">
    <label>DAY 번호<input type="number" id="f-day" min="1"></label>
    <label>정렬 순서<input type="number" id="f-order" min="0"></label>
    <label>예약 일시 (KST)<input type="datetime-local" id="f-sched"></label>
    <label>상태
      <select id="f-status">
        <option value="draft">초안</option>
        <option value="scheduled">예약</option>
        <option value="sent">발송됨</option>
      </select>
    </label>
    <label class="full">제목 (한국어)<input type="text" id="f-title"></label>
    <label class="full">제목 (러시아어)<input type="text" id="f-titleru"></label>
    <label class="full">숙제/자료 링크<input type="text" id="f-link" placeholder="https://..."></label>
    <label class="full">본문 — 텔레그램으로 나가는 메시지 (HTML 태그 b, i, a 사용 가능)
      <textarea id="f-body"></textarea></label>
    <label class="full">복습 자료 — 본문 뒤에 별도 메시지로 발송 (비우면 안 보냄)
      <textarea id="f-review"></textarea></label>
  </div>
  <div id="mediaBox" class="muted"></div>
  <div class="foot">
    <button class="b" onclick="saveLesson('draft')">초안 저장</button>
    <button class="b" onclick="saveLesson('scheduled')">예약 저장</button>
    <button class="b" onclick="preview()">텔레그램 미리보기</button>
    <button class="b" onclick="sendLesson('test')">테스트 발송</button>
    <div class="right">
      <button class="b" onclick="closeEditor()">닫기</button>
      <button class="b p" onclick="sendLesson('all')">지금 전체 발송</button>
    </div>
  </div>
</div></div>

<div class="modal" id="previewBox"><div class="sheet">
  <h2>텔레그램 미리보기</h2>
  <pre class="prev" id="prevText"></pre>
  <div id="prevReviewWrap" class="hidden">
    <div class="muted" style="margin:14px 0 6px">복습 자료 (두 번째 메시지)</div>
    <pre class="prev" id="prevReview"></pre>
  </div>
  <div class="muted" id="prevMedia" style="margin-top:12px"></div>
  <div class="foot"><div class="right"><button class="b" onclick="closeModal('previewBox')">닫기</button></div></div>
</div></div>

<div class="toast" id="toast"></div>
"""

ADMIN_SCRIPT = """<script>
var LESSONS = [], USERS = [], CUR = null;

function toast(m){ var t=document.getElementById("toast"); t.textContent=m; t.classList.add("on");
  setTimeout(function(){ t.classList.remove("on"); }, 2600); }
function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function api(path, body){
  return fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body||{})})
    .then(function(r){ return r.json(); });
}
function tab(name){
  ["lessons","users","log"].forEach(function(n){
    document.getElementById("p-"+n).classList.toggle("hidden", n!==name);
    document.getElementById("t-"+n).classList.toggle("on", n===name);
  });
  if(name==="users") loadUsers();
  if(name==="log") loadSends();
}
function closeModal(id){ document.getElementById(id).classList.remove("on"); }
function closeEditor(){ closeModal("editor"); loadLessons(); }
function statusLabel(s){ return s==="sent"?"발송됨":(s==="scheduled"?"예약":"초안"); }

function loadLessons(){
  return fetch("/api/lessons").then(function(r){return r.json();}).then(function(d){
    LESSONS = d.lessons || [];
    var draft=0, sch=0, sent=0;
    var tb = document.getElementById("lessonRows"); tb.innerHTML="";
    LESSONS.forEach(function(L){
      if(L.status==="scheduled") sch++; else if(L.status==="sent") sent++; else draft++;
      var tr=document.createElement("tr");
      tr.innerHTML =
        "<td><b>DAY "+L.day+"</b></td>"+
        "<td>"+esc(L.title||"-")+"<div class=muted>"+esc(L.title_ru||"")+"</div></td>"+
        "<td>"+(L.scheduled_at? esc(L.scheduled_at) : "<span class=muted>-</span>")+"</td>"+
        "<td><span class='badge b-"+L.status+"'>"+statusLabel(L.status)+"</span></td>"+
        "<td>"+(L.audio? "O" : "<span class=muted>-</span>")+"</td>"+
        "<td><div class=row>"+
          "<button class=b data-act=edit data-id="+L.id+">편집</button>"+
          "<button class=b data-act=dup data-id="+L.id+">복제</button>"+
          "<button class='b d' data-act=del data-id="+L.id+">삭제</button>"+
        "</div></td>";
      tb.appendChild(tr);
    });
    document.getElementById("lessonMeta").textContent =
      "전체 "+LESSONS.length+"개 · 초안 "+draft+" · 예약 "+sch+" · 발송됨 "+sent+
      " · 무료 공개 DAY 1~"+d.free_days;
  });
}

function loadUsers(){
  fetch("/api/users").then(function(r){return r.json();}).then(function(d){
    USERS = d.users||[];
    var free=0, prem=0, act=0;
    var tb=document.getElementById("userRows"); tb.innerHTML="";
    USERS.forEach(function(u){
      if(u.plan==="premium") prem++; else free++;
      if(u.active) act++;
      var id = esc(u.chat_id);
      var tr=document.createElement("tr");
      tr.innerHTML =
        "<td>"+id+"</td>"+
        "<td>"+esc(u.name||"-")+"</td>"+
        "<td>"+(u.username? "@"+esc(u.username) : "<span class=muted>-</span>")+"</td>"+
        "<td class=muted>"+esc((u.created_at||"").slice(0,10))+"</td>"+
        "<td><div class=row><input type=number style='width:70px' value='"+(u.day||0)+"' data-day='"+id+"'>"+
          "<button class=b data-act=uday data-uid='"+id+"'>저장</button></div></td>"+
        "<td><span class='badge b-"+(u.plan==="premium"?"premium":"free")+"'>"+esc(u.plan||"free")+"</span> "+
          "<button class=b data-act=uplan data-uid='"+id+"' data-val='"+(u.plan==="premium"?"free":"premium")+"'>"+
          (u.plan==="premium"?"→ free":"→ premium")+"</button></td>"+
        "<td>"+(u.active? "활성" : "<span class=muted>중지</span>")+
          " <button class=b data-act=uactive data-uid='"+id+"' data-val='"+(u.active?0:1)+"'>"+(u.active?"중지":"활성")+"</button></td>"+
        "<td class=muted>"+esc(u.last_sent||"-")+"</td>"+
        "<td><div class=row><select data-send='"+id+"'></select>"+
          "<button class=b data-act=usend data-uid='"+id+"'>보내기</button></div></td>";
      tb.appendChild(tr);
      var sel = tr.querySelector("select");
      LESSONS.forEach(function(L){
        var o=document.createElement("option"); o.value=L.id;
        o.textContent="DAY "+L.day; if(L.day===(u.day||0)+1) o.selected=true;
        sel.appendChild(o);
      });
    });
    document.getElementById("userMeta").textContent =
      "전체 "+USERS.length+"명 · 활성 "+act+" · 무료 "+free+" · 유료 "+prem;
  });
}

function loadSends(){
  fetch("/api/sends").then(function(r){return r.json();}).then(function(d){
    var tb=document.getElementById("logRows"); tb.innerHTML="";
    (d.sends||[]).forEach(function(s){
      var tr=document.createElement("tr");
      var okTxt = s.status==="ok" ? "<span class='badge b-sent'>성공</span>"
        : "<span class='badge b-draft' style='color:#e79a96'>실패</span>";
      tr.innerHTML =
        "<td class=muted>"+esc(s.created_at)+"</td>"+
        "<td>DAY "+esc(s.day)+"</td>"+
        "<td>"+esc(s.chat_id)+"</td>"+
        "<td class=muted>"+esc(s.kind)+"</td>"+
        "<td>"+okTxt+"</td>"+
        "<td class=muted style='max-width:360px;overflow:hidden;text-overflow:ellipsis'>"+esc(s.error||"")+"</td>"+
        "<td>"+(s.status==="ok"?"":"<button class=b data-act=retry data-id='"+s.lesson_id+"' data-uid='"+esc(s.chat_id)+"'>재시도</button>")+"</td>";
      tb.appendChild(tr);
    });
  });
}

function openEditor(id){
  CUR = id ? LESSONS.filter(function(L){return L.id===id;})[0] : null;
  var L = CUR || {day:"", title:"", title_ru:"", body:"", review:"", link:"",
                  scheduled_at:"", status:"draft", sort_order:""};
  document.getElementById("edTitle").textContent = CUR ? ("DAY "+L.day+" 편집") : "새 레슨";
  document.getElementById("f-id").value = CUR ? L.id : "";
  document.getElementById("f-day").value = L.day || "";
  document.getElementById("f-order").value = L.sort_order || L.day || "";
  document.getElementById("f-sched").value = (L.scheduled_at||"").replace(" ","T").slice(0,16);
  document.getElementById("f-status").value = L.status || "draft";
  document.getElementById("f-title").value = L.title || "";
  document.getElementById("f-titleru").value = L.title_ru || "";
  document.getElementById("f-link").value = L.link || "";
  document.getElementById("f-body").value = L.body || "";
  document.getElementById("f-review").value = L.review || "";
  renderMedia();
  document.getElementById("editor").classList.add("on");
}

function renderMedia(){
  var box = document.getElementById("mediaBox");
  if(!CUR){ box.innerHTML = "파일은 레슨을 먼저 저장한 뒤 올릴 수 있습니다."; return; }
  var h = "<div style='margin:6px 0 8px'><b style='color:#e8eaed'>MP3 음성</b></div>";
  if(CUR.audio){
    h += "<div class=row><audio controls preload=none src='/media/"+encodeURIComponent(CUR.audio)+"'></audio>"+
         "<span class=muted>"+esc(CUR.audio)+"</span>"+
         "<button class='b d' data-act=delaudio>삭제</button></div>";
  } else {
    h += "<div class=muted>등록된 음성이 없습니다.</div>";
  }
  h += "<div class=row style='margin-top:8px'><input type=file id='f-audio' accept='audio/*'>"+
       "<button class=b data-act=upaudio>업로드</button></div>";
  h += "<div style='margin:16px 0 8px'><b style='color:#e8eaed'>추가 파일</b></div>";
  if((CUR.files||[]).length){
    h += "<div class=row>";
    (CUR.files||[]).forEach(function(f){
      h += "<span class=muted style='margin-right:6px'><a style='color:#9aa2b1' href='/media/"+encodeURIComponent(f)+"' target=_blank>"+esc(f)+"</a> "+
           "<button class='b d' data-act=delfile data-name='"+esc(f)+"'>x</button></span>";
    });
    h += "</div>";
  } else {
    h += "<div class=muted>없음</div>";
  }
  h += "<div class=row style='margin-top:8px'><input type=file id='f-file'>"+
       "<button class=b data-act=upfile>업로드</button></div>";
  box.innerHTML = h;
}

function collect(status){
  return {
    id: document.getElementById("f-id").value || null,
    day: document.getElementById("f-day").value,
    sort_order: document.getElementById("f-order").value,
    scheduled_at: document.getElementById("f-sched").value,
    status: status || document.getElementById("f-status").value,
    title: document.getElementById("f-title").value,
    title_ru: document.getElementById("f-titleru").value,
    link: document.getElementById("f-link").value,
    body: document.getElementById("f-body").value,
    review: document.getElementById("f-review").value
  };
}

function saveLesson(status){
  var data = collect(status);
  if(status==="scheduled" && !data.scheduled_at){
    toast("예약하려면 발송 일시를 정해주세요"); return Promise.resolve(false);
  }
  return api("/api/lesson/save", data).then(function(r){
    if(!r.ok){ toast("저장 실패: "+(r.error||"")); return false; }
    CUR = r.lesson;
    document.getElementById("f-id").value = r.lesson.id;
    document.getElementById("f-status").value = r.lesson.status;
    document.getElementById("edTitle").textContent = "DAY "+r.lesson.day+" 편집";
    renderMedia();
    loadLessons();
    toast(status==="scheduled" ? "예약 저장했습니다" : "저장했습니다");
    return true;
  });
}

function preview(){
  saveLesson().then(function(good){
    if(!good) return;
    fetch("/api/preview?id="+CUR.id).then(function(r){return r.json();}).then(function(d){
      document.getElementById("prevText").textContent = d.text || "";
      var w=document.getElementById("prevReviewWrap");
      if(d.review){ w.classList.remove("hidden"); document.getElementById("prevReview").textContent=d.review; }
      else { w.classList.add("hidden"); }
      var m = [];
      if(d.audio) m.push("음성 1개 (" + d.audio + ")");
      if((d.files||[]).length) m.push("추가 파일 " + d.files.length + "개");
      document.getElementById("prevMedia").textContent = m.length ? ("함께 발송: " + m.join(" · ")) : "첨부 파일 없음";
      document.getElementById("previewBox").classList.add("on");
    });
  });
}

function sendLesson(mode){
  saveLesson().then(function(good){
    if(!good) return;
    if(mode==="all" && !confirm("이 레슨을 지금 전체 학생에게 보냅니다. 계속할까요?")) return;
    toast(mode==="test" ? "테스트 발송 중..." : "발송 중...");
    api("/api/lesson/send", {id: CUR.id, mode: mode}).then(function(r){
      if(!r.ok){ toast("발송 실패: "+(r.error||"")); return; }
      if(mode==="test") toast("테스트 발송 완료 (" + r.target + ")");
      else toast("발송 완료 — 성공 "+r.sent+", 실패 "+r.failed+", 제외 "+r.skipped);
      loadLessons();
    });
  });
}

function upload(kind){
  var input = document.getElementById(kind==="audio" ? "f-audio" : "f-file");
  if(!input || !input.files.length){ toast("파일을 선택해주세요"); return; }
  var fd = new FormData();
  fd.append("id", CUR.id);
  fd.append("kind", kind);
  fd.append("file", input.files[0]);
  toast("업로드 중...");
  fetch("/api/upload", {method:"POST", body: fd}).then(function(r){return r.json();}).then(function(r){
    if(!r.ok){ toast("업로드 실패: "+(r.error||"")); return; }
    CUR = r.lesson; renderMedia(); loadLessons(); toast("업로드 완료");
  });
}

document.addEventListener("click", function(e){
  var b = e.target.closest("button[data-act]");
  if(!b) return;
  var act = b.getAttribute("data-act");
  var id = b.getAttribute("data-id");
  var uid = b.getAttribute("data-uid");
  var val = b.getAttribute("data-val");

  if(act==="edit"){ openEditor(parseInt(id,10)); return; }
  if(act==="dup"){
    api("/api/lesson/duplicate", {id: parseInt(id,10)}).then(function(r){
      if(r.ok){ toast("DAY "+r.day+" 로 복제했습니다"); loadLessons(); }
      else toast("복제 실패: "+(r.error||""));
    }); return;
  }
  if(act==="del"){
    if(!confirm("이 레슨을 삭제할까요? 되돌릴 수 없습니다.")) return;
    api("/api/lesson/delete", {id: parseInt(id,10)}).then(function(r){
      if(r.ok){ toast("삭제했습니다"); loadLessons(); } else toast("삭제 실패");
    }); return;
  }
  if(act==="upaudio"){ upload("audio"); return; }
  if(act==="upfile"){ upload("file"); return; }
  if(act==="delaudio"){
    if(!confirm("음성 파일을 삭제할까요?")) return;
    api("/api/lesson/media-delete", {id: CUR.id, kind:"audio"}).then(function(r){
      if(r.ok){ CUR=r.lesson; renderMedia(); loadLessons(); toast("삭제했습니다"); }
    }); return;
  }
  if(act==="delfile"){
    api("/api/lesson/media-delete", {id: CUR.id, kind:"file", name: b.getAttribute("data-name")})
      .then(function(r){ if(r.ok){ CUR=r.lesson; renderMedia(); toast("삭제했습니다"); } });
    return;
  }
  if(act==="uday"){
    var inp = document.querySelector("input[data-day='"+uid+"']");
    api("/api/user/update", {chat_id: uid, day: inp.value}).then(function(r){
      toast(r.ok ? "진도를 저장했습니다" : ("실패: "+(r.error||""))); loadUsers();
    }); return;
  }
  if(act==="uplan"){
    api("/api/user/update", {chat_id: uid, plan: val}).then(function(r){
      toast(r.ok ? "요금제를 변경했습니다" : ("실패: "+(r.error||""))); loadUsers();
    }); return;
  }
  if(act==="uactive"){
    api("/api/user/update", {chat_id: uid, active: val==="1"}).then(function(r){
      toast(r.ok ? "상태를 변경했습니다" : ("실패: "+(r.error||""))); loadUsers();
    }); return;
  }
  if(act==="usend"){
    var sel = document.querySelector("select[data-send='"+uid+"']");
    if(!sel || !sel.value){ toast("보낼 레슨을 고르세요"); return; }
    b.disabled = true;
    api("/api/lesson/send", {id: parseInt(sel.value,10), mode:"one", chat_id: uid}).then(function(r){
      b.disabled = false;
      toast(r.ok ? "발송했습니다" : ("발송 실패: "+(r.error||""))); loadUsers();
    }); return;
  }
  if(act==="retry"){
    b.disabled = true;
    api("/api/lesson/send", {id: parseInt(id,10), mode:"one", chat_id: uid}).then(function(r){
      b.disabled = false;
      toast(r.ok ? "재발송 성공" : ("재발송 실패: "+(r.error||""))); loadSends();
    }); return;
  }
});

document.addEventListener("keydown", function(e){
  if(e.key === "Escape"){ closeModal("previewBox"); }
});

loadLessons();
setInterval(function(){
  if(!document.getElementById("editor").classList.contains("on")) loadLessons();
}, 60000);
</script></body></html>"""

ADMIN_PAGE = ADMIN_PAGE + ADMIN_SCRIPT


def start_admin_server():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Admin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("관리자 CMS 시작 — 포트 %s, 비밀번호 %s", port,
             "설정됨" if ADMIN_PASSWORD else "미설정(접속 불가)")


async def _post_init(app):
    app.create_task(scheduler_loop())
    log.info("봇 준비 완료")


def main():
    if not BOT_TOKEN:
        raise SystemExit("TG_BOT_TOKEN 환경변수가 필요합니다.")
    init_db()
    start_admin_server()
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(cb_start_day1, pattern="^start_day1$"))
    log.info("봇 시작됨.")
    app.run_polling()


if __name__ == "__main__":
    main()
