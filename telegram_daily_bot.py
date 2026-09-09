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
    work_status  TEXT DEFAULT 'editing',
    quiz         TEXT DEFAULT '',
    quiz_today   TEXT DEFAULT '',
    summary      TEXT DEFAULT '',
    homework     TEXT DEFAULT '',
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
    last_sent  TEXT DEFAULT '',
    course_started INTEGER DEFAULT 0,
    started_at TEXT DEFAULT '',
    status TEXT DEFAULT 'not_started',
    start_day INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS sends (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER,
    day       INTEGER,
    chat_id   TEXT,
    status    TEXT,
    error     TEXT DEFAULT '',
    kind      TEXT DEFAULT 'auto',
    created_at TEXT,
    opened_at TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sends_lesson ON sends(lesson_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_day ON lessons(day);
"""


def ensure_columns(conn):
    """기존 DB 에 없는 컬럼을 채운다 (한 번만 실행되는 효과)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    added = False
    if "course_started" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN course_started INTEGER DEFAULT 0")
        added = True
    if "started_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN started_at TEXT DEFAULT ''")
        added = True
    if added:
        # 이미 진도가 있는 사람은 코스를 시작한 것으로 본다.
        conn.execute(
            "UPDATE users SET course_started=1, started_at=COALESCE(NULLIF(started_at,''), created_at)"
            " WHERE day >= 1"
        )
        conn.commit()
        log.info("users 테이블에 course_started / started_at 컬럼을 추가했습니다.")

    lcols = {r["name"] for r in conn.execute("PRAGMA table_info(lessons)").fetchall()}
    if "work_status" not in lcols:
        conn.execute("ALTER TABLE lessons ADD COLUMN work_status TEXT DEFAULT 'editing'")
        conn.execute("UPDATE lessons SET work_status='editing' WHERE COALESCE(work_status,'')=''")
        conn.commit()
        log.info("lessons 테이블에 work_status 컬럼을 추가했습니다.")
    if "quiz" not in lcols:
        conn.execute("ALTER TABLE lessons ADD COLUMN quiz TEXT DEFAULT ''")
        conn.commit()
        log.info("lessons 테이블에 quiz 컬럼을 추가했습니다.")
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(sends)").fetchall()}
    if "opened_at" not in scols:
        conn.execute("ALTER TABLE sends ADD COLUMN opened_at TEXT DEFAULT ''")
        conn.commit()
        log.info("sends 테이블에 opened_at 컬럼을 추가했습니다.")

    for extra in ("summary", "homework", "quiz_today"):
        if extra not in lcols:
            conn.execute("ALTER TABLE lessons ADD COLUMN " + extra + " TEXT DEFAULT ''")
            conn.commit()
            log.info("lessons 테이블에 %s 컬럼을 추가했습니다.", extra)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    added2 = False
    if "status" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN status TEXT DEFAULT ''")
        added2 = True
    if "start_day" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN start_day INTEGER DEFAULT 1")
        added2 = True
    if added2:
        conn.execute(
            "UPDATE users SET status = CASE"
            " WHEN COALESCE(active,1)=0 THEN 'inactive'"
            " WHEN COALESCE(course_started,0)=0 THEN 'not_started'"
            " ELSE 'active' END"
            " WHERE COALESCE(status,'')=''"
        )
        conn.execute("UPDATE users SET start_day=1 WHERE COALESCE(start_day,0)<1")
        conn.commit()
        log.info("users 테이블에 status / start_day 컬럼을 추가했습니다.")


def init_db():
    with _db_lock:
        conn = db()
        conn.executescript(SCHEMA)
        conn.commit()
        ensure_columns(conn)
        migrate_legacy(conn)
        migrate_reviews(conn)
        restore_reviews(conn)
        import_legacy_audio(conn)
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


STATUSES = ("not_started", "active", "paused", "completed", "inactive")
SENDABLE = ("not_started", "active")


def all_users(active_only=False):
    conn = db()
    q = "SELECT * FROM users"
    if active_only:
        q += " WHERE COALESCE(status,'active') IN ('not_started','active') AND COALESCE(active,1)=1"
    q += " ORDER BY created_at ASC"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def read_today_map():
    """오늘 발송분에 대해서만 학생별 열람 여부를 계산한다. 과거 기록은 쓰지 않는다."""
    today = now_kst().strftime("%Y-%m-%d")
    out = {}
    try:
        conn = db()
        rows = conn.execute(
            "SELECT chat_id,"
            " MAX(CASE WHEN COALESCE(opened_at,'')<>'' THEN 1 ELSE 0 END) opened"
            " FROM sends WHERE status='ok' AND kind<>'test'"
            " AND substr(created_at,1,10)=? GROUP BY chat_id", (today,)).fetchall()
        conn.close()
        for r in rows:
            out[str(r["chat_id"])] = "read" if r["opened"] else "unread"
    except Exception as e:
        log.warning("열람 상태 조회 실패: %s", e)
    return out


def with_read_today(users):
    m = read_today_map()
    for u in users:
        u["read_today"] = m.get(str(u.get("chat_id")), "none")
    return users


def delete_user(chat_id):
    """학생 한 명만 지운다. 레슨/음성 등 공용 자료는 건드리지 않는다."""
    chat_id = str(chat_id)
    with _db_lock:
        conn = db()
        cur = conn.execute("DELETE FROM users WHERE chat_id=?", (chat_id,))
        conn.commit()
        n = cur.rowcount
        conn.close()
    return n > 0


def create_user(chat_id, fields):
    chat_id = str(chat_id).strip()
    if not chat_id.isdigit():
        return False, "텔레그램 ID 는 숫자여야 합니다"
    with _db_lock:
        conn = db()
        exists = conn.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if exists:
            conn.close()
            return False, "이미 등록된 텔레그램 사용자입니다"
        conn.execute("INSERT INTO users (chat_id,created_at) VALUES (?,?)", (chat_id, ts()))
        if fields:
            cols = ", ".join(k + "=?" for k in fields)
            conn.execute("UPDATE users SET " + cols + " WHERE chat_id=?",
                         tuple(fields.values()) + (chat_id,))
        conn.commit()
        conn.close()
    return True, ""


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


def tg_text(chat_id, text, buttons=None):
    payload = {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": "true"}
    if buttons:
        payload["reply_markup"] = json.dumps(buttons, ensure_ascii=False)
    return tg_api("sendMessage", payload)


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
    # 오디오/테스트 링크는 메시지 아래 버튼으로만 제공한다 (본문 중복 제거).
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
        tg_text(chat_id, render_lesson(lesson), buttons=review_buttons(lesson, chat_id))
        log_send(lesson, chat_id, "ok", "", kind)
        return True, ""
    except Exception as e:
        log_send(lesson, chat_id, "error", str(e), kind)
        log.warning("발송 실패 chat_id=%s day=%s: %s", chat_id, lesson.get("day"), e)
        return False, str(e)


def broadcast_lesson(lesson, kind="auto", only_chat_ids=None, mark_last_sent=False):
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
            # 진도(day)는 절대 건드리지 않는다. 자동 일일 발송만 진도를 올린다.
            if mark_last_sent:
                upsert_user(u["chat_id"], last_sent=now_kst().strftime("%Y-%m-%d"))
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
                ok, fail, skipped = broadcast_lesson(lesson, "scheduled", mark_last_sent=True)
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


SEND_HOUR = int(os.environ.get("SEND_HOUR", "11"))
SEND_MINUTE = int(os.environ.get("SEND_MINUTE", "0"))
DAILY_AUTO = os.environ.get("DAILY_AUTO", "1") != "0"


def run_daily_batch():
    """학생별 진도에 맞춰 다음 DAY 를 하루 한 번 보낸다. (발송, 잠금안내, 실패) 반환"""
    today = now_kst().strftime("%Y-%m-%d")
    sent = locked = fail = 0
    for u in all_users(active_only=True):
        if (u.get("last_sent") or "") == today:
            continue
        chat_id = u["chat_id"]
        day = int(u.get("day") or 0)
        nxt = day + 1 if day >= 1 else 1
        lesson = get_lesson_by_day(nxt)
        if not lesson:
            continue
        if is_locked(u, nxt):
            try:
                tg_text(chat_id, paywall_text())
                upsert_user(chat_id, last_sent=today)
                locked += 1
            except Exception:
                fail += 1
            continue
        good, _ = send_lesson_to(chat_id, lesson, "daily")
        if good:
            upsert_user(chat_id, day=nxt, last_sent=today)
            sent += 1
        else:
            fail += 1
    return sent, locked, fail


async def daily_loop():
    log.info("매일 자동 발송 루프 시작 (%02d:%02d KST)", SEND_HOUR, SEND_MINUTE)
    while True:
        try:
            n = now_kst()
            past = n.hour > SEND_HOUR or (n.hour == SEND_HOUR and n.minute >= SEND_MINUTE)
            if DAILY_AUTO and past:
                sent, locked, fail = await asyncio.to_thread(run_daily_batch)
                if sent or locked or fail:
                    log.info("자동 발송 — 발송 %d, 잠금안내 %d, 실패 %d", sent, locked, fail)
                    if ADMIN_CHAT_ID:
                        try:
                            tg_text(ADMIN_CHAT_ID,
                                    "\U0001F4E4 오늘의 자동 발송 완료\n발송 " + str(sent)
                                    + " / 잠금안내 " + str(locked)
                                    + " / 실패 " + str(fail))
                        except Exception:
                            pass
        except Exception as e:
            log.warning("자동 발송 루프 오류: %s", e)
        await asyncio.sleep(60)


def claim_course_start(chat_id):
    """코스 시작을 한 번만 허용한다. 처음 시작하는 경우에만 True 를 돌려준다.

    UPDATE ... WHERE course_started=0 한 문장으로 처리하므로
    버튼을 연타해도 두 번 시작되지 않는다.
    """
    chat_id = str(chat_id)
    with _db_lock:
        conn = db()
        row = conn.execute("SELECT chat_id FROM users WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (chat_id,created_at) VALUES (?,?)", (chat_id, ts()))
        cur = conn.execute(
            "UPDATE users SET course_started=1, started_at=?, day=1, status='active', active=1"
            " WHERE chat_id=? AND COALESCE(course_started,0)=0",
            (ts(), chat_id),
        )
        conn.commit()
        changed = cur.rowcount
        conn.close()
    return changed == 1


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    u = update.effective_user
    name = " ".join(x for x in [getattr(u, "first_name", None), getattr(u, "last_name", None)] if x)
    upsert_user(chat_id, name=name, username=getattr(u, "username", "") or "", active=1)

    user = get_user(chat_id) or {}
    already = int(user.get("course_started") or 0) == 1
    day = int(user.get("day") or 0)

    if already:
        # 이미 시작한 사람에게는 시작 버튼을 다시 보여주지 않는다. 진도도 건드리지 않는다.
        again_kb = InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F4B3 \uc720\ub8cc \uad6c\ub3c5 \uc2e0\uccad / \u041E\u0444\u043E\u0440\u043C\u0438\u0442\u044C \u043F\u043E\u0434\u043F\u0438\u0441\u043A\u0443", callback_data="apply_premium")]])
        await update.message.reply_text(
            "\ub2e4\uc2dc \uc624\uc168\ub124\uc694! \uc774\ubbf8 \ud559\uc2b5\uc744 \uc2dc\uc791\ud558\uc168\uc2b5\ub2c8\ub2e4.\n"
            "\ud604\uc7ac \uc9c4\ub3c4: DAY " + str(max(day, 1)) + "\n\n"
            "\u0421 \u0432\u043E\u0437\u0432\u0440\u0430\u0449\u0435\u043D\u0438\u0435\u043C! \u0412\u044B \u0443\u0436\u0435 \u043D\u0430\u0447\u0430\u043B\u0438 \u043A\u0443\u0440\u0441.\n"
            "\u0422\u0435\u043A\u0443\u0449\u0438\u0439 \u0434\u0435\u043D\u044C: DAY " + str(max(day, 1)) + "\n\n"
            "\uc624\ub298 \ubd84\ub7c9\uc744 \ub2e4\uc2dc \ubcf4\ub824\uba74 /today \ub97c \uc785\ub825\ud558\uc138\uc694.\n"
            "\u0427\u0442\u043E\u0431\u044B \u043F\u043E\u0441\u043C\u043E\u0442\u0440\u0435\u0442\u044C \u0443\u0440\u043E\u043A, \u0432\u0432\u0435\u0434\u0438\u0442\u0435 /today.",
            reply_markup=again_kb,
        )
        return

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\U0001F331 \uccab\ub0a0 \uc2dc\uc791\ud558\uae30 / \u041D\u0430\u0447\u0430\u0442\u044C DAY 1", callback_data="start_day1")],
            [InlineKeyboardButton("\U0001F4B3 \uc720\ub8cc \uad6c\ub3c5 \uc2e0\uccad / \u041E\u0444\u043E\u0440\u043C\u0438\u0442\u044C \u043F\u043E\u0434\u043F\u0438\u0441\u043A\u0443", callback_data="apply_premium")],
        ]
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
    chat_id = str(query.message.chat.id)

    # 시작 버튼만 없앤다 (연타 방지). 유료 신청 버튼은 남겨둔다.
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\U0001F4B3 \uc720\ub8cc \uad6c\ub3c5 \uc2e0\uccad / \u041E\u0444\u043E\u0440\u043C\u0438\u0442\u044C \u043F\u043E\u0434\u043F\u0438\u0441\u043A\u0443", callback_data="apply_premium")]])
        )
    except Exception:
        pass

    user = get_user(chat_id)
    if user and int(user.get("course_started") or 0) == 1:
        await query.answer("\uc774\ubbf8 \uc2dc\uc791\ud558\uc168\uc5b4\uc694 / \u0412\u044B \u0443\u0436\u0435 \u043D\u0430\u0447\u0430\u043B\u0438", show_alert=False)
        return

    lesson = get_lesson_by_day(1)
    if not lesson:
        await query.answer()
        await query.message.reply_text("DAY 1 \ub0b4\uc6a9\uc774 \uc544\uc9c1 \uc900\ube44\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4.")
        return

    # DB 한 문장으로 선점한다. 이미 시작된 상태면 False.
    if not claim_course_start(chat_id):
        await query.answer("\uc774\ubbf8 \uc2dc\uc791\ud558\uc168\uc5b4\uc694 / \u0412\u044B \u0443\u0436\u0435 \u043D\u0430\u0447\u0430\u043B\u0438", show_alert=False)
        return

    await query.answer()
    await asyncio.to_thread(send_lesson_to, chat_id, lesson, "manual")


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    upsert_user(chat_id)
    user = get_user(chat_id) or {}
    day = int(user.get("day") or 0)
    if day < 1:
        day = 1
        claim_course_start(chat_id)
    if is_locked(user, day):
        await update.message.reply_text(paywall_text())
        return
    lesson = get_lesson_by_day(day)
    if not lesson:
        await update.message.reply_text("DAY " + str(day) + " \ucf58\ud150\uce20\uac00 \uc544\uc9c1 \uc5c6\uc2b5\ub2c8\ub2e4.")
        return
    await asyncio.to_thread(send_lesson_to, chat_id, lesson, "manual")


def notify_admin_premium(chat_id, tg_user):
    """프리미엄 신청을 관리자에게 알린다. (기존 로직 그대로, 한 곳으로 모음)"""
    if not ADMIN_CHAT_ID:
        return
    user = get_user(chat_id) or {}
    name = " ".join(
        x for x in [getattr(tg_user, "first_name", None), getattr(tg_user, "last_name", None)] if x
    )
    uname = "@" + tg_user.username if getattr(tg_user, "username", None) else "(\uc5c6\uc74c)"
    try:
        tg_text(
            ADMIN_CHAT_ID,
            "\U0001F514 \ud504\ub9ac\ubbf8\uc5c4 \uc804\ud658 \uc694\uccad\n"
            "\uc774\ub984: " + (name or "(\uc5c6\uc74c)") + "\n"
            "\uc544\uc774\ub514: " + uname + "\n"
            "chat_id: " + str(chat_id) + "\n"
            "\ud604\uc7ac DAY: " + str(user.get("day", 0)) + " / plan: " + str(user.get("plan", "free")),
        )
    except Exception as e:
        log.warning("관리자 알림 실패: %s", e)


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    upsert_user(chat_id)
    await update.message.reply_text(payment_text())
    await asyncio.to_thread(notify_admin_premium, chat_id, update.effective_user)


async def cb_apply_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """유료 구독 신청 버튼 — /premium 과 완전히 같은 안내와 같은 신청 처리를 쓴다."""
    query = update.callback_query
    await query.answer()
    chat_id = str(query.message.chat.id)
    upsert_user(chat_id)
    await query.message.reply_text(payment_text())
    await asyncio.to_thread(notify_admin_premium, chat_id, query.from_user)


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
:root{--bg:#0a0e16;--panel:#121826;--line:#232c40;--line2:#2c3750;--fg:#e8ecf5;--mut:#8792a8;--acc:#4f7cff;--accd:#3b64e0;--no:#ef4a4a}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
background:radial-gradient(900px 520px at 50% -12%,#18213a 0%,var(--bg) 60%) no-repeat,var(--bg);
color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif;font-size:14px;-webkit-font-smoothing:antialiased}
.box{width:100%;max-width:380px;background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:34px 30px 30px;
box-shadow:0 1px 2px rgba(0,0,0,.4),0 30px 70px -24px rgba(0,0,0,.85);position:relative}
.box::before{content:"";position:absolute;left:0;right:0;top:0;height:2px;border-radius:18px 18px 0 0;
background:linear-gradient(90deg,transparent,var(--acc),#8f6bff,transparent)}
h1{font-size:19px;font-weight:650;margin:0 0 8px;letter-spacing:-.015em;display:flex;align-items:center;gap:10px}
h1::before{content:"";width:10px;height:10px;border-radius:3px;background:linear-gradient(140deg,var(--acc),#8f6bff);box-shadow:0 0 0 4px rgba(79,124,255,.14)}
p.sub{color:var(--mut);font-size:13px;margin:0 0 24px;line-height:1.6}
input{width:100%;padding:13px 15px;border-radius:11px;border:1px solid var(--line2);
background:#0a0f19;color:var(--fg);font-size:17px;letter-spacing:.3em;text-align:center;font-family:inherit;
transition:border-color .15s,box-shadow .15s}
input::placeholder{letter-spacing:normal;color:var(--mut)}
input:focus{outline:0;border-color:var(--acc);box-shadow:0 0 0 3px rgba(79,124,255,.2)}
button{width:100%;margin-top:14px;padding:13px;border-radius:11px;border:1px solid #3f6ae8;
background:linear-gradient(180deg,#5b86ff,var(--accd));color:#fff;font-size:14.5px;font-weight:600;font-family:inherit;cursor:pointer;
box-shadow:0 1px 2px rgba(0,0,0,.4),0 10px 24px -12px rgba(79,124,255,.85);transition:background .15s,transform .06s,box-shadow .15s}
button:hover{background:linear-gradient(180deg,#6b92ff,#4470ea)}
button:active{transform:translateY(1px);box-shadow:0 1px 2px rgba(0,0,0,.4)}
button:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
.err{margin-top:16px;color:#ff9a95;font-size:12.5px;line-height:1.6;text-align:center;min-height:1px}
.err:not(:empty){background:rgba(239,74,74,.09);border:1px solid rgba(239,74,74,.28);border-radius:10px;padding:10px 12px}
</style></head><body>
<form class="box" method="post" action="/login" autocomplete="off">
  <h1>한국어365 관리자</h1>
  <p class="sub">비밀번호를 입력하세요</p>
  <input type="password" name="password" autofocus>
  <button type="submit">들어가기</button>
  <div class="err">__MSG__</div>
</form>
</body></html>"""


def _public_url():
    v = os.environ.get("PUBLIC_URL", "").strip()
    if not v:
        d = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
        if d:
            v = "https://" + d
    if not v:
        v = "https://dasha-korean-bot-production.up.railway.app"
    return v.rstrip("/")


PUBLIC_URL = _public_url()

# 기존 텔레그램 복습 테스트의 정답 (DAY 별, 문항 순서대로). 내용은 그대로 보존한다.
ANSWER_KEYS = {
    2: "2222214222", 3: "2132232122", 4: "2134223132", 5: "2132222422",
    6: "2221223222", 7: "2132213122", 8: "2122211221", 9: "1223121121",
    10: "2121222111", 11: "2134221232", 12: "2134222222", 13: "2134212112",
    14: "2313122123", 15: "1222122121", 16: "2111122122", 17: "2212323322",
    18: "2121123232", 19: "1231212212", 20: "2132422222", 21: "2221111211",
    22: "2321232122", 23: "2121311112", 24: "2221111222", 25: "1231211121",
    26: "1121212232", 27: "2221112211", 28: "2121111111",
}


def lesson_quiz_today(lesson):
    try:
        v = json.loads(lesson.get("quiz_today") or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def lesson_quiz(lesson):
    try:
        v = json.loads(lesson.get("quiz") or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def parse_quiz_text(text):
    """관리자 입력(한 줄 = 문제 | 보기1 | 보기2 | ... | 정답번호) 을 목록으로."""
    out = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        try:
            ans = int(parts[-1])
        except Exception:
            ans = 1
        choices = [p for p in parts[1:-1] if p]
        if not choices:
            continue
        if ans < 1 or ans > len(choices):
            ans = 1
        out.append({"q": parts[0], "choices": choices, "answer": ans})
    return out


def split_review(text):
    """기존 복습 자료에서 테스트 문항을 분리한다. (자료, 문항목록)"""
    text = text or ""
    idx = text.find("\U0001F4DD")
    if idx < 0:
        return text.strip(), []
    material = text[:idx].strip()
    items = []
    for line in text[idx:].split("\n"):
        line = line.strip()
        m = re.match(r"^\d+\.\s*(.+)$", line)
        if not m:
            continue
        chunks = re.split("[\u2460\u2461\u2462\u2463\u2464]", m.group(1))
        q = chunks[0].strip()
        choices = [c.strip() for c in chunks[1:] if c.strip()]
        if not choices:
            continue
        items.append({"q": q, "choices": choices, "answer": 1})
    return material, items


def migrate_reviews(conn):
    """복습 자료 안에 섞여 있던 테스트를 quiz 컬럼으로 옮긴다 (한 번만). 내용은 그대로."""
    try:
        rows = conn.execute("SELECT id, day, review, quiz FROM lessons").fetchall()
    except Exception:
        return
    n = 0
    for r in rows:
        if (r["quiz"] or "").strip():
            continue
        material, items = split_review(r["review"] or "")
        if not items:
            continue
        key = ANSWER_KEYS.get(int(r["day"] or 0), "")
        for i, it in enumerate(items):
            if i < len(key):
                a = int(key[i])
                if 1 <= a <= len(it["choices"]):
                    it["answer"] = a
        conn.execute("UPDATE lessons SET review=?, quiz=? WHERE id=?",
                     (material, json.dumps(items, ensure_ascii=False), r["id"]))
        n += 1
    if n:
        conn.commit()
        log.info("복습 테스트 %d개를 웹 페이지용으로 구조화했습니다.", n)


MARKS = "\u2460\u2461\u2462\u2463\u2464"
TEST_HEADER = ("\U0001F4DD \u0422\u0435\u0441\u0442: \u043f\u043e\u0432\u0442\u043e\u0440\u0435\u043d\u0438\u0435"
               " \u0432\u0447\u0435\u0440\u0430\u0448\u043d\u0435\u0433\u043e \u0443\u0440\u043e\u043a\u0430")


def quiz_to_review_text(items):
    """구조화된 문항을 원래 복습 자료에 있던 텍스트 형태로 되돌린다."""
    lines = []
    for i, it in enumerate(items or []):
        s = str(i + 1) + ". " + (it.get("q") or "")
        for k, ch in enumerate(it.get("choices") or []):
            mark = MARKS[k] if k < len(MARKS) else "-"
            s += " " + mark + " " + ch
        lines.append(s)
    return "\n".join(lines)


def restore_reviews(conn):
    """복습 자료 칸에서 사라진 테스트 문항을 원문 그대로 다시 채운다."""
    try:
        rows = conn.execute("SELECT id, review, quiz FROM lessons").fetchall()
    except Exception:
        return
    n = 0
    for r in rows:
        raw = r["quiz"] or ""
        if not raw.strip():
            continue
        review = r["review"] or ""
        if "\U0001F4DD" in review:
            continue
        try:
            items = json.loads(raw)
        except Exception:
            continue
        if not items:
            continue
        text = (review.strip() + "\n\n" + TEST_HEADER + "\n\n"
                + quiz_to_review_text(items)).strip()
        conn.execute("UPDATE lessons SET review=? WHERE id=?", (text, r["id"]))
        n += 1
    if n:
        conn.commit()
        log.info("복습 자료 %d개의 테스트 문항을 복구했습니다.", n)


LEGACY_AUDIO = {
    1: "https://boisterous-smakager-668cc1.netlify.app/day1-audio.mp3",
    2: "https://boisterous-smakager-668cc1.netlify.app/day2-audio.mp3",
}


def import_legacy_audio(conn):
    """학습 사이트에만 있던 음성 파일을 관리자 저장소로 한 번만 옮긴다."""
    for day, url in LEGACY_AUDIO.items():
        try:
            row = conn.execute("SELECT id, audio FROM lessons WHERE day=?", (day,)).fetchone()
        except Exception:
            return
        if not row or (row["audio"] or "").strip():
            continue
        name = "day" + str(day) + "-audio.mp3"
        target = MEDIA_DIR / name
        if not target.exists():
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
                target.write_bytes(data)
            except Exception as e:
                log.warning("기존 음성 가져오기 실패 day=%s: %s", day, e)
                continue
        conn.execute("UPDATE lessons SET audio=? WHERE id=?", (name, row["id"]))
        conn.commit()
        log.info("DAY %s 음성 파일을 관리자 저장소로 옮겼습니다.", day)


def review_url(lesson):
    link = (lesson.get("link") or "").strip()
    if link:
        return link
    return PUBLIC_URL + "/review?day=" + str(lesson.get("day") or 0)


def open_url(lesson, chat_id):
    """열람 확인용 중계 주소. 누르면 기록하고 학습 페이지로 보낸다."""
    if not chat_id:
        return review_url(lesson)
    return (PUBLIC_URL + "/o?u=" + urllib.parse.quote(str(chat_id))
            + "&d=" + str(lesson.get("day") or 0))


def review_buttons(lesson, chat_id=None):
    """복습 자료나 테스트가 있으면 웹 페이지로 가는 버튼을 붙인다."""
    if not (lesson.get("review") or lesson.get("audio") or lesson_quiz(lesson)):
        return None
    return {"inline_keyboard": [[{
        "text": "\U0001F4DA \u041f\u043e\u0432\u0442\u043e\u0440\u0435\u043d\u0438\u0435 \u0438 \u0442\u0435\u0441\u0442",
        "url": open_url(lesson, chat_id)}]]}


REVIEW_CSS = """
:root{--bg:#0b0d12;--card:#11141b;--line:#232838;--fg:#e8ecf5;--muted:#8a93a6;--acc:#4c8dff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);line-height:1.65;
  font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:26px 16px 64px}
h1{font-size:21px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}
.card h2{font-size:13px;margin:0 0 12px;color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font:inherit}
audio{width:100%}
.q{border-top:1px solid var(--line);padding:15px 0}
.q:first-of-type{border-top:0;padding-top:0}
.qt{font-weight:600;margin-bottom:9px}
label.ch{display:block;padding:8px 11px;border:1px solid var(--line);border-radius:9px;margin-bottom:6px;cursor:pointer}
label.ch:hover{background:#171b24}
label.ch input{margin-right:9px}
label.ok{border-color:#2f7d4f;background:#132018}
label.no{border-color:#8d3b3b;background:#201414}
button.go{width:100%;padding:13px;border:0;border-radius:11px;background:var(--acc);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
#res{margin-top:14px;text-align:center;font-size:17px;font-weight:600}
a{color:var(--acc)}
"""

REVIEW_JS = """<script>
var ANS = __ANS__;
function check(){
  var right = 0;
  for (var i = 0; i < ANS.length; i++) {
    var box = document.getElementById("q" + i);
    var labs = box.querySelectorAll("label.ch");
    for (var k = 0; k < labs.length; k++) { labs[k].className = "ch"; }
    labs[ANS[i] - 1].className = "ch ok";
    var sel = document.querySelector("input[name=q" + i + "]:checked");
    if (sel) {
      var v = parseInt(sel.value, 10);
      if (v === ANS[i]) { right++; }
      else { labs[v - 1].className = "ch no"; }
    }
  }
  document.getElementById("res").textContent =
    "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442: " + right + " / " + ANS.length;
}
</script>"""


REVIEW_JS2 = """<script>
var ANS2 = __ANS2__;
function checkToday(){
  var right = 0;
  for (var i = 0; i < ANS2.length; i++) {
    var box = document.getElementById("t" + i);
    var labs = box.querySelectorAll("label.ch");
    for (var k = 0; k < labs.length; k++) { labs[k].className = "ch"; }
    labs[ANS2[i] - 1].className = "ch ok";
    var sel = document.querySelector("input[name=t" + i + "]:checked");
    if (sel) {
      var v = parseInt(sel.value, 10);
      if (v === ANS2[i]) { right++; }
      else { labs[v - 1].className = "ch no"; }
    }
  }
  document.getElementById("res2").textContent =
    "\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442: " + right + " / " + ANS2.length;
}
</script>"""


def render_review_page(lesson, quiz):
    """복습 자료 + 오디오 + 테스트를 하나의 공개 페이지로 만든다."""
    day = str(lesson.get("day") or "")
    title = lesson.get("title") or ""
    title_ru = lesson.get("title_ru") or ""
    p = []
    p.append("<!doctype html><html lang=ru><head><meta charset=utf-8>")
    p.append("<meta name=viewport content='width=device-width,initial-scale=1'>")
    p.append("<meta name=robots content=noindex>")
    p.append("<title>DAY " + day + "</title>")
    p.append("<style>" + REVIEW_CSS + "</style></head><body><div class=wrap>")
    head = "DAY " + day
    if title:
        head += " &middot; " + html.escape(title)
    p.append("<h1>" + head + "</h1>")
    if title_ru:
        p.append("<p class=sub>" + html.escape(title_ru) + "</p>")
    if lesson.get("audio"):
        p.append("<div class=card><h2>1. Аудио · произношение</h2>"
                 + "<audio controls preload=none src='/media/"
                 + html.escape(lesson["audio"]) + "'></audio></div>")
    if lesson.get("summary"):
        p.append("<div class=card><h2>2. Итоги урока</h2><pre>"
                 + html.escape(lesson["summary"]) + "</pre></div>")
    if lesson.get("homework"):
        p.append("<div class=card><h2>3. Домашнее задание</h2><pre>"
                 + html.escape(lesson["homework"]) + "</pre></div>")
    files = lesson.get("files") or []
    if files:
        links = []
        for nm in files:
            links.append("<a href='/media/" + html.escape(nm) + "'>" + html.escape(nm) + "</a>")
        p.append("<div class=card><h2>Файлы</h2>"
                 + "<br>".join(links) + "</div>")
    today_quiz = lesson_quiz_today(lesson)
    if today_quiz:
        p.append("<div class=card><h2>Тест — сегодняшний урок</h2>")
        for i, it in enumerate(today_quiz):
            p.append("<div class=q id=t" + str(i) + "><div class=qt>"
                     + str(i + 1) + ". " + html.escape(it.get("q") or "") + "</div>")
            for k, ch in enumerate(it.get("choices") or []):
                p.append("<label class=ch><input type=radio name=t" + str(i)
                         + " value=" + str(k + 1) + ">" + html.escape(ch) + "</label>")
            p.append("</div>")
        p.append("</div>")
        p.append("<button class=go onclick='checkToday()'>Проверить</button>")
        p.append("<div id=res2></div>")
        p.append(REVIEW_JS2.replace("__ANS2__",
                 json.dumps([int(it.get("answer") or 1) for it in today_quiz])))
    material = lesson.get("review") or ""
    if quiz:
        material = split_review(material)[0]
    if material:
        p.append("<div class=card><h2>4. Повторение вчерашнего урока</h2><pre>"
                 + html.escape(material) + "</pre></div>")
    if quiz:
        p.append("<div class=card><h2>Тест — повторение вчерашнего урока</h2>")
        for i, it in enumerate(quiz):
            p.append("<div class=q id=q" + str(i) + "><div class=qt>"
                     + str(i + 1) + ". " + html.escape(it.get("q") or "") + "</div>")
            for k, ch in enumerate(it.get("choices") or []):
                p.append("<label class=ch><input type=radio name=q" + str(i)
                         + " value=" + str(k + 1) + ">" + html.escape(ch) + "</label>")
            p.append("</div>")
        p.append("</div>")
        p.append("<button class=go onclick='check()'>"
                 + "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c</button>")
        p.append("<div id=res></div>")
        ans = [int(it.get("answer") or 1) for it in quiz]
        p.append(REVIEW_JS.replace("__ANS__", json.dumps(ans)))
    p.append("</div></body></html>")
    return "\n".join(p)


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
        if path == "/o":
            return self._open_track(q)
        if path == "/review":
            return self._review_page(q)
        if path == "/api/public/lesson":
            return self._public_lesson(q)
        if path.startswith("/media/"):
            name = os.path.basename(path[len("/media/"):])
            p = MEDIA_DIR / name
            if not p.exists():
                return self._bytes(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            return self._bytes(200, p.read_bytes(), ctype)

        ok, why = self._auth()
        if not ok:
            return self._login_page(why)

        if path == "/api/lessons":
            return self._json(200, {"lessons": all_lessons(), "free_days": FREE_DAYS})
        if path == "/api/users":
            return self._json(200, {"users": with_read_today(all_users())})
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
            return self._user_update(b)
        if path == "/api/user/create":
            return self._user_create(b)
        if path == "/api/user/delete":
            return self._user_delete(b)
        if path == "/api/user/reset":
            return self._user_reset(b)
        return self._json(404, {"error": "알 수 없는 경로"})

    # --- 학생 관리 ---
    def _user_fields(self, b):
        fields = {}
        if "name" in b:
            fields["name"] = str(b.get("name") or "")[:80]
        if "username" in b:
            fields["username"] = str(b.get("username") or "").lstrip("@")[:60]
        if "plan" in b:
            fields["plan"] = "premium" if b.get("plan") == "premium" else "free"
        if "day" in b:
            try:
                fields["day"] = max(0, min(365, int(b.get("day") or 0)))
            except Exception:
                pass
        if "start_day" in b:
            try:
                fields["start_day"] = max(1, min(365, int(b.get("start_day") or 1)))
            except Exception:
                pass
        if "started_at" in b:
            fields["started_at"] = str(b.get("started_at") or "")[:19].replace("T", " ")
        if "status" in b:
            st = b.get("status")
            if st in STATUSES:
                fields["status"] = st
                fields["active"] = 0 if st in ("paused", "inactive") else 1
                if st == "not_started":
                    fields["course_started"] = 0
                elif st in ("active", "paused", "completed"):
                    fields["course_started"] = 1
        if "active" in b and "status" not in b:
            fields["active"] = 1 if b.get("active") else 0
        return fields

    def _user_update(self, b):
        cid = str(b.get("chat_id", ""))
        if not get_user(cid):
            return self._json(404, {"error": "등록되지 않은 학생입니다"})
        fields = self._user_fields(b)
        if fields:
            upsert_user(cid, **fields)
        return self._json(200, {"ok": True, "user": get_user(cid)})

    def _user_create(self, b):
        cid = str(b.get("chat_id", "")).strip()
        if not cid:
            return self._json(400, {"error": "텔레그램 ID 를 입력하세요"})
        fields = self._user_fields(b)
        fields.setdefault("status", "not_started")
        fields.setdefault("start_day", 1)
        if "day" not in fields:
            fields["day"] = max(0, int(fields.get("start_day", 1)) - 1)
        ok, err = create_user(cid, fields)
        if not ok:
            return self._json(409, {"error": err})
        return self._json(200, {"ok": True, "user": get_user(cid)})

    def _user_delete(self, b):
        cid = str(b.get("chat_id", ""))
        if not get_user(cid):
            return self._json(404, {"error": "등록되지 않은 학생입니다"})
        delete_user(cid)
        return self._json(200, {"ok": True})

    def _user_reset(self, b):
        cid = str(b.get("chat_id", ""))
        u = get_user(cid)
        if not u:
            return self._json(404, {"error": "등록되지 않은 학생입니다"})
        start_day = max(1, int(u.get("start_day") or 1))
        upsert_user(cid, day=start_day - 1, course_started=0, started_at="",
                    status="not_started", last_sent="")
        return self._json(200, {"ok": True, "user": get_user(cid)})

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
            "work_status": "completed" if b.get("work_status") == "completed" else "editing",
            "quiz": json.dumps(parse_quiz_text(b.get("quiz_text") or ""), ensure_ascii=False),
            "quiz_today": json.dumps(parse_quiz_text(b.get("quiz_today_text") or ""), ensure_ascii=False),
            "summary": b.get("summary", ""),
            "homework": b.get("homework", ""),
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
            # 수동 발송은 진도와 완전히 분리된 동작이다. 학생 진도를 바꾸지 않는다.
            good, err = send_lesson_to(target, lesson, "manual")
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

    def _cors_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _open_track(self, q):
        """학생이 버튼을 눌렀을 때 열람으로 기록하고 학습 페이지로 보낸다."""
        who = (q.get("u") or [""])[0]
        try:
            day = int((q.get("d") or ["0"])[0])
        except Exception:
            day = 0
        if who and day:
            try:
                with _db_lock:
                    conn = db()
                    conn.execute(
                        "UPDATE sends SET opened_at=? WHERE chat_id=? AND day=?"
                        " AND COALESCE(opened_at,'')=''",
                        (ts(), str(who), day))
                    conn.commit()
                    conn.close()
            except Exception as e:
                log.warning("열람 기록 실패: %s", e)
        lesson = get_lesson_by_day(day)
        dest = (lesson or {}).get("link") or (PUBLIC_URL + "/review?day=" + str(day))
        self.send_response(302)
        self.send_header("Location", dest)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _public_lesson(self, q):
        """학생용 공개 데이터 (외부 사이트에서 사용). 로그인 불필요."""
        try:
            day = int((q.get("day") or ["0"])[0])
        except Exception:
            day = 0
        lesson = get_lesson_by_day(day)
        if not lesson:
            return self._cors_json(404, {"error": "not found"})
        quiz = lesson_quiz(lesson)
        material = lesson.get("review") or ""
        if quiz:
            material = split_review(material)[0]
        audio = lesson.get("audio") or ""
        return self._cors_json(200, {
            "day": lesson.get("day"),
            "title": lesson.get("title") or "",
            "title_ru": lesson.get("title_ru") or "",
            "summary": lesson.get("summary") or "",
            "homework": lesson.get("homework") or "",
            "review": material,
            "audio": (PUBLIC_URL + "/media/" + audio) if audio else "",
            "quiz": quiz,
            "quiz_today": lesson_quiz_today(lesson),
        })

    def _review_page(self, q):
        """학생용 공개 페이지 — 복습 자료 + 오디오 + 테스트"""
        try:
            day = int((q.get("day") or ["0"])[0])
        except Exception:
            day = 0
        lesson = get_lesson_by_day(day)
        if not lesson:
            body = ("<!doctype html><meta charset=utf-8><style>" + REVIEW_CSS
                    + "</style><div class=wrap><h1>DAY " + str(day) + "</h1>"
                    + "<p class=sub>\u0423\u0440\u043e\u043a \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d</p></div>")
            return self._bytes(404, body.encode("utf-8"), "text/html; charset=utf-8")
        page = render_review_page(lesson, lesson_quiz(lesson))
        return self._bytes(200, page.encode("utf-8"), "text/html; charset=utf-8")

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
:root{--bg:#0a0e16;--bg2:#0d121c;--panel:#121826;--panel2:#151c2c;--line:#232c40;--line2:#2c3750;--fg:#e8ecf5;--mut:#8792a8;--acc:#4f7cff;--accd:#3b64e0;--ok:#22c55e;--warn:#f0b429;--no:#ef4a4a}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:radial-gradient(1200px 600px at 20% -10%,#141c2e 0%,var(--bg) 55%) no-repeat,var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif;font-size:14px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:26px 22px 96px}
header{display:flex;align-items:center;gap:16px;margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid var(--line)}
h1{font-size:17px;font-weight:650;margin:0;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
h1::before{content:"";width:9px;height:9px;border-radius:3px;background:linear-gradient(140deg,var(--acc),#8f6bff);box-shadow:0 0 0 4px rgba(79,124,255,.14)}
.tabs{display:flex;gap:4px;margin-left:auto;background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:4px}
.tabs button{padding:7px 15px;border-radius:8px;border:0;background:transparent;color:var(--mut);cursor:pointer;font-size:13px;font-weight:550;font-family:inherit;transition:background .15s,color .15s}
.tabs button:hover{color:var(--fg);background:rgba(255,255,255,.04)}
.tabs button.on{background:var(--acc);color:#fff;box-shadow:0 1px 2px rgba(0,0,0,.35)}
a.logout{color:var(--mut);text-decoration:none;font-size:13px;padding:7px 12px;border:1px solid var(--line);border-radius:9px;transition:color .15s,border-color .15s}
a.logout:hover{color:var(--fg);border-color:var(--line2)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:auto;box-shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.5)}
table{width:100%;border-collapse:collapse;min-width:760px}
th,td{padding:12px 14px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{color:var(--mut);font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.06em;background:var(--panel2);position:sticky;top:0}
tbody tr{transition:background .12s}
tbody tr:hover{background:rgba(255,255,255,.022)}
tr:last-child td{border-bottom:0}
.badge{display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;font-size:11.5px;font-weight:600;white-space:nowrap;border:1px solid transparent;line-height:1.6}
.b-draft{background:rgba(135,146,168,.14);color:#b8c1d1;border-color:rgba(135,146,168,.24)}
.b-scheduled{background:rgba(240,180,41,.13);color:#f0c765;border-color:rgba(240,180,41,.26)}
.b-sent{background:rgba(34,197,94,.13);color:#5fd695;border-color:rgba(34,197,94,.26)}
.b-free{background:rgba(135,146,168,.14);color:#b8c1d1;border-color:rgba(135,146,168,.24)}
.b-premium{background:rgba(34,197,94,.13);color:#5fd695;border-color:rgba(34,197,94,.26)}
button.b{padding:7px 12px;border-radius:9px;border:1px solid var(--line2);background:linear-gradient(180deg,#1b2333,#161d2b);color:var(--fg);cursor:pointer;font-size:12.5px;font-weight:550;font-family:inherit;box-shadow:0 1px 2px rgba(0,0,0,.35);transition:border-color .15s,transform .06s,box-shadow .15s,background .15s}
button.b:hover{border-color:#3c4a68;background:linear-gradient(180deg,#202a3d,#1a2231)}
button.b:active{transform:translateY(1px);box-shadow:0 0 0 rgba(0,0,0,0)}
button.b:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
button.p{background:linear-gradient(180deg,#5b86ff,var(--accd));border-color:#3f6ae8;color:#fff;box-shadow:0 1px 2px rgba(0,0,0,.4),0 6px 16px -8px rgba(79,124,255,.7)}
button.p:hover{background:linear-gradient(180deg,#6b92ff,#4470ea);border-color:#5a83ff}
button.d{color:#ff9a95;border-color:rgba(239,74,74,.32)}
button.d:hover{color:#ffb3ae;border-color:rgba(239,74,74,.55);background:linear-gradient(180deg,#2a1c22,#221619)}
.row{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.muted{color:var(--mut);font-size:12px}
.bar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
input,select,textarea{padding:9px 12px;border-radius:9px;border:1px solid var(--line2);background:var(--bg2);color:var(--fg);font-size:13px;font-family:inherit;transition:border-color .15s,box-shadow .15s}
input:hover,select:hover,textarea:hover{border-color:#3a4763}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--acc);box-shadow:0 0 0 3px rgba(79,124,255,.18)}
textarea{width:100%;min-height:150px;line-height:1.65;resize:vertical}
.modal{position:fixed;inset:0;background:rgba(6,9,15,.72);backdrop-filter:blur(3px);display:none;align-items:flex-start;justify-content:center;padding:34px 16px;overflow:auto;z-index:50}
.modal.on{display:flex}
.sheet{width:100%;max-width:900px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:26px;box-shadow:0 24px 60px -20px rgba(0,0,0,.8)}
.sheet h2{margin:0 0 20px;font-size:16px;font-weight:650;letter-spacing:-.01em;padding-bottom:14px;border-bottom:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:16px}
.grid label{display:flex;flex-direction:column;gap:6px;font-size:11.5px;font-weight:600;color:var(--mut);letter-spacing:.02em}
.grid.two{grid-template-columns:repeat(2,1fr)}
.full{grid-column:1/-1}
.foot{display:flex;gap:8px;flex-wrap:wrap;margin-top:20px;padding-top:18px;border-top:1px solid var(--line)}
.foot .right{margin-left:auto;display:flex;gap:8px}
pre.prev{white-space:pre-wrap;word-break:break-word;background:var(--bg2);border:1px solid var(--line);border-radius:11px;padding:16px;font-size:13px;line-height:1.65}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#161d2b;border:1px solid var(--line2);padding:12px 20px;border-radius:11px;opacity:0;transition:opacity .2s;z-index:60;box-shadow:0 12px 32px -10px rgba(0,0,0,.8);font-size:13px}
.toast.on{opacity:1}
.hidden{display:none}
audio{width:260px;height:34px}
.wk{border:1px solid var(--line);border-radius:13px;margin-bottom:12px;overflow:hidden;background:var(--panel);box-shadow:0 1px 2px rgba(0,0,0,.3)}
.wk-h{width:100%;display:flex;align-items:center;gap:11px;padding:14px 18px;background:var(--panel2);border:0;color:inherit;font:inherit;font-size:14px;cursor:pointer;text-align:left;transition:background .15s}
.wk-h:hover{background:#1a2233}
.wk-h b{font-weight:650;letter-spacing:-.01em}
.wk-ar{width:14px;display:inline-block;color:var(--mut);font-size:11px}
.wk-n{margin-left:auto;font-size:11.5px;color:var(--mut);background:rgba(255,255,255,.05);padding:3px 10px;border-radius:999px}
.wk-b{border-top:1px solid var(--line)}
.dr{display:grid;grid-template-columns:78px 1fr 84px 128px 88px 68px auto;gap:14px;align-items:center;padding:12px 18px;border-bottom:1px solid var(--line);cursor:pointer;transition:background .12s}
.dr:last-child{border-bottom:0}
.dr:hover{background:rgba(255,255,255,.028)}
.dr-d{font-weight:650;font-size:13px;letter-spacing:-.01em}
@media (max-width:900px){
.wrap{padding:18px 14px 80px}
header{flex-wrap:wrap}
.tabs{margin-left:0;width:100%;justify-content:space-between}
.grid{grid-template-columns:1fr}
.grid.two{grid-template-columns:1fr}
.dr{grid-template-columns:1fr;gap:8px}
.sheet{padding:20px}
}
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
    <div id="lessonWeeks"></div>
  </section>

  <section id="p-users" class="hidden">
    <div class="bar">
      <button class="b p" data-act="unew">+ 학생 추가</button>
      <input type="text" id="uq" placeholder="ID · 아이디 · 이름 검색" style="width:220px">
      <select id="ufs">
        <option value="">상태 전체</option>
        <option value="not_started">시작 전</option>
        <option value="active">진행 중</option>
        <option value="paused">일시정지</option>
        <option value="completed">수료</option>
        <option value="inactive">비활성</option>
      </select>
      <select id="ufd">
        <option value="">DAY 전체</option>
      </select>
      <span class="muted" id="userMeta"></span>
    </div>
    <div class="card"><table>
      <thead><tr><th>ID</th><th>이름 / 아이디</th><th>등록일</th><th>시작일</th><th>시작 DAY</th><th>현재 DAY</th><th>상태</th><th>요금제</th><th>오늘 열람</th><th>마지막 발송</th><th>작업</th></tr></thead>
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
    <label>발송 상태
      <select id="f-status">
        <option value="draft">초안</option>
        <option value="scheduled">예약</option>
        <option value="sent">발송됨</option>
      </select>
    </label>
    <label>작업 상태
      <select id="f-work">
        <option value="editing">편집 중</option>
        <option value="completed">완료</option>
      </select>
    </label>
    <label class="full">제목 (한국어)<input type="text" id="f-title"></label>
    <label class="full">제목 (러시아어)<input type="text" id="f-titleru"></label>
    <label class="full">숙제/자료 링크<input type="text" id="f-link" placeholder="https://..."></label>
    <label class="full">본문 — 텔레그램으로 나가는 메시지 (HTML 태그 b, i, a 사용 가능)
      <textarea id="f-body"></textarea></label>
    <label class="full">2. 수업 요약 — Итоги урока
      <textarea id="f-summary"></textarea></label>
    <label class="full">3. 숙제 — Домашнее задание
      <textarea id="f-homework"></textarea></label>
    <label class="full">4. 어제 복습 자료 — Повторение вчерашнего урока
      <textarea id="f-review"></textarea></label>
    <label class="full">당일 복습 테스트 — 오늘 배운 내용 (한 줄에 하나: 문제 | 보기1 | 보기2 | 보기3 | 보기4 | 정답번호)
      <textarea id="f-quiz-today"></textarea></label>
    <label class="full">어제 복습 테스트 — 전날 배운 내용 (한 줄에 하나: 문제 | 보기1 | 보기2 | 보기3 | 보기4 | 정답번호)
      <textarea id="f-quiz" placeholder="안녕하세요 | Спасибо | Здравствуйте | Извините | Нет | 2"></textarea></label>
  </div>
  <div id="mediaBox" class="muted"></div>
  <div class="foot">
    <button class="b" onclick="saveLesson('draft')">초안 저장</button>
    <button class="b" onclick="saveLesson('scheduled')">예약 저장</button>
    <button class="b" onclick="preview()">텔레그램 미리보기</button>
    <button class="b" onclick="sendLesson('test')">테스트 발송</button>
    <div class="right">
      <button class="b p" onclick="closeEditor()">닫기</button>
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

<div class="modal" id="student"><div class="sheet" style="max-width:620px">
  <h2 id="stTitle">학생 정보</h2>
  <div class="grid two">
    <label>텔레그램 ID<input type="text" id="s-id" placeholder="숫자만"></label>
    <label>텔레그램 아이디<input type="text" id="s-username" placeholder="@ 없이"></label>
    <label>이름<input type="text" id="s-name"></label>
    <label>요금제
      <select id="s-plan"><option value="free">free</option><option value="premium">premium</option></select>
    </label>
    <label>시작 DAY<input type="number" id="s-startday" min="1" value="1"></label>
    <label>현재 DAY<input type="number" id="s-day" min="0" value="0"></label>
    <label>코스 시작일<input type="datetime-local" id="s-startedat"></label>
    <label>상태
      <select id="s-status">
        <option value="not_started">시작 전</option>
        <option value="active">진행 중</option>
        <option value="paused">일시정지</option>
        <option value="completed">수료</option>
        <option value="inactive">비활성</option>
      </select>
    </label>
  </div>
  <div class="muted" id="stHint">일시정지 · 비활성 상태의 학생에게는 예약 발송이 나가지 않습니다.</div>
  <div class="foot">
    <div class="right">
      <button class="b" onclick="closeModal('student')">닫기</button>
      <button class="b p" data-act="ussave">저장</button>
    </div>
  </div>
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

var WEEKOPEN = {};
function weekOf(day){ return Math.floor((day-1)/7)+1; }
function toggleWeek(w){ WEEKOPEN[w] = !WEEKOPEN[w]; renderWeeks(); }
function renderWeeks(){
  var host = document.getElementById("lessonWeeks");
  if(!host) return;
  var groups = {};
  LESSONS.slice().sort(function(a,b){ return (a.day||0)-(b.day||0); }).forEach(function(L){
    var w = weekOf(L.day||1);
    if(!groups[w]) groups[w] = [];
    groups[w].push(L);
  });
  var ws = Object.keys(groups).map(Number).sort(function(a,b){ return a-b; });
  var html = "";
  ws.forEach(function(w){
    var items = groups[w];
    var from = (w-1)*7+1;
    var to = w*7;
    var last = items[items.length-1].day;
    if(last < to) to = last;
    var done = 0;
    items.forEach(function(L){ if(L.work_status==="completed") done++; });
    var open = (WEEKOPEN[w] === undefined) ? true : !!WEEKOPEN[w];
    html += "<div class=wk>";
    html += "<button class=wk-h onclick=toggleWeek(" + w + ")>"
          + "<span class=wk-ar>" + (open ? "\u25BC" : "\u25B6") + "</span>"
          + "<b>Week " + w + "</b>"
          + "<span class=muted> \u00b7 DAY " + from + " ~ DAY " + to + "</span>"
          + "<span class=wk-n>" + done + " / " + items.length + " 완료</span></button>";
    html += "<div class='wk-b" + (open ? "" : " hidden") + "'>";
    items.forEach(function(L){
      html += "<div class=dr data-act=edit data-id=" + L.id + ">"
        + "<div class=dr-d>DAY " + L.day + "</div>"
        + "<div>" + esc(L.title||"-") + "<div class=muted>" + esc(L.title_ru||"") + "</div></div>"
        + "<div><span class='badge " + (L.work_status==="completed" ? "b-premium" : "b-scheduled") + "'>"
        + (L.work_status==="completed" ? "완료" : "편집 중") + "</span></div>"
        + "<div class=muted>" + (L.scheduled_at ? esc(L.scheduled_at) : "-") + "</div>"
        + "<div><span class='badge b-" + L.status + "'>" + statusLabel(L.status) + "</span></div>"
        + "<div class=muted>" + (L.audio ? "MP3" : "-") + "</div>"
        + "<div class=row>"
        + "<button class=b data-act=edit data-id=" + L.id + ">편집</button>"
        + "<button class=b data-act=dup data-id=" + L.id + ">복제</button>"
        + "<button class='b d' data-act=del data-id=" + L.id + ">삭제</button>"
        + "</div></div>";
    });
    html += "</div></div>";
  });
  host.innerHTML = html || "<div class=muted>레슨이 없습니다.</div>";
}
function loadLessons(){
  return fetch("/api/lessons").then(function(r){ return r.json(); }).then(function(d){
    LESSONS = d.lessons || [];
    var draft = 0, sch = 0, sent = 0;
    LESSONS.forEach(function(L){
      if(L.status==="scheduled") sch++;
      else if(L.status==="sent") sent++;
      else draft++;
    });
    renderWeeks();
    document.getElementById("lessonMeta").textContent =
      "전체 "+LESSONS.length+"개 · 초안 "+draft+" · 예약 "+sch+" · 발송됨 "+sent+
      " · 무료 공개 DAY 1~"+d.free_days;
  });
}

function readToday(v){
  if (v === "read") return "<span class='badge b-premium'>🟢 열람</span>";
  if (v === "unread") return "<span class='badge b-draft'>🔴 미열람</span>";
  return "<span class=muted>오늘 발송 없음</span>";
}
function statusKo(s){
  return s==="active"?"진행 중":s==="paused"?"일시정지":s==="completed"?"수료":
         s==="inactive"?"비활성":"시작 전";
}
function statusClass(s){
  return s==="active"?"b-premium":s==="completed"?"b-scheduled":"b-draft";
}

function loadUsers(){
  return fetch("/api/users").then(function(r){return r.json();}).then(function(d){
    USERS = d.users||[];
    var sel = document.getElementById("ufd");
    if(sel && sel.options.length<=1){
      LESSONS.forEach(function(L){
        var o=document.createElement("option"); o.value=L.day; o.textContent="DAY "+L.day; sel.appendChild(o);
      });
    }
    renderUsers();
  });
}

function renderUsers(){
  var q = (document.getElementById("uq").value||"").toLowerCase().trim();
  var fs = document.getElementById("ufs").value;
  var fd = document.getElementById("ufd").value;
  var tb=document.getElementById("userRows"); tb.innerHTML="";
  var free=0, prem=0, act=0, shown=0;
  USERS.forEach(function(u){
    var st = u.status || "not_started";
    if(u.plan==="premium") prem++; else free++;
    if(st==="active") act++;
    if(q){
      var hay = (u.chat_id+" "+(u.username||"")+" "+(u.name||"")).toLowerCase();
      if(hay.indexOf(q)<0) return;
    }
    if(fs && st!==fs) return;
    if(fd && String(u.day||0)!==String(fd)) return;
    shown++;
    var id = esc(u.chat_id);
    var tr=document.createElement("tr");
    tr.innerHTML =
      "<td>"+id+"</td>"+
      "<td>"+esc(u.name||"-")+"<div class=muted>"+(u.username? "@"+esc(u.username):"")+"</div></td>"+
      "<td class=muted>"+esc((u.created_at||"").slice(0,10))+"</td>"+
      "<td class=muted>"+esc((u.started_at||"").slice(0,10)||"-")+"</td>"+
      "<td>"+(u.start_day||1)+"</td>"+
      "<td><b>"+(u.day||0)+"</b></td>"+
      "<td><span class='badge "+statusClass(st)+"'>"+statusKo(st)+"</span></td>"+
      "<td><span class='badge b-"+(u.plan==="premium"?"premium":"free")+"'>"+esc(u.plan||"free")+"</span></td>"+
      "<td>"+readToday(u.read_today)+"</td>"+
        "<td class=muted>"+esc(u.last_sent||"-")+"</td>"+
      "<td><div class=row>"+
        "<select data-send='"+id+"'></select>"+
        "<button class=b data-act=usend data-uid='"+id+"'>보내기</button>"+
        "<button class=b data-act=uedit data-uid='"+id+"'>편집</button>"+
        (st==="paused"||st==="inactive"
          ? "<button class=b data-act=ustatus data-uid='"+id+"' data-val=active>활성</button>"
          : "<button class=b data-act=ustatus data-uid='"+id+"' data-val=paused>일시정지</button>")+
        "<button class=b data-act=ureset data-uid='"+id+"'>진도 초기화</button>"+
        "<button class='b d' data-act=udel data-uid='"+id+"'>삭제</button>"+
      "</div></td>";
    tb.appendChild(tr);
    var s = tr.querySelector("select");
    LESSONS.forEach(function(L){
      var o=document.createElement("option"); o.value=L.id;
      o.textContent="DAY "+L.day; if(L.day===(u.day||0)+1) o.selected=true;
      s.appendChild(o);
    });
  });
  document.getElementById("userMeta").textContent =
    "표시 "+shown+" / 전체 "+USERS.length+"명 · 진행 중 "+act+" · 무료 "+free+" · 유료 "+prem;
}

function openStudent(uid){
  var u = uid ? USERS.filter(function(x){return String(x.chat_id)===String(uid);})[0] : null;
  document.getElementById("stTitle").textContent = u ? ("학생 편집 — "+u.chat_id) : "학생 추가";
  document.getElementById("s-id").value = u ? u.chat_id : "";
  document.getElementById("s-id").disabled = !!u;
  document.getElementById("s-username").value = u ? (u.username||"") : "";
  document.getElementById("s-name").value = u ? (u.name||"") : "";
  document.getElementById("s-plan").value = u ? (u.plan||"free") : "free";
  document.getElementById("s-startday").value = u ? (u.start_day||1) : 1;
  document.getElementById("s-day").value = u ? (u.day||0) : 0;
  document.getElementById("s-startedat").value = u ? (u.started_at||"").replace(" ","T").slice(0,16) : "";
  document.getElementById("s-status").value = u ? (u.status||"not_started") : "not_started";
  document.getElementById("student").classList.add("on");
}

function saveStudent(){
  var isNew = !document.getElementById("s-id").disabled;
  var body = {
    chat_id: document.getElementById("s-id").value.trim(),
    username: document.getElementById("s-username").value.trim(),
    name: document.getElementById("s-name").value.trim(),
    plan: document.getElementById("s-plan").value,
    start_day: document.getElementById("s-startday").value,
    day: document.getElementById("s-day").value,
    started_at: document.getElementById("s-startedat").value,
    status: document.getElementById("s-status").value
  };
  if(!body.chat_id){ toast("텔레그램 ID 를 입력하세요"); return; }
  api(isNew ? "/api/user/create" : "/api/user/update", body).then(function(r){
    if(!r.ok){ toast(r.error||"저장 실패"); return; }
    toast(isNew ? "학생을 추가했습니다" : "저장했습니다");
    closeModal("student");
    loadUsers();
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

function quizToText(q){
  var arr = [];
  try { arr = JSON.parse(q || "[]"); } catch(e) { arr = []; }
  if (!arr || !arr.length) return "";
  var out = [];
  for (var i = 0; i < arr.length; i++) {
    var it = arr[i] || {};
    out.push([it.q || ""].concat(it.choices || []).concat([String(it.answer || 1)]).join(" | "));
  }
  return out.join(String.fromCharCode(10));
}

function openEditor(id){
  CUR = id ? LESSONS.filter(function(L){return L.id===id;})[0] : null;
  var L = CUR || {day:"", title:"", title_ru:"", body:"", review:"", link:"",
                  scheduled_at:"", status:"draft", work_status:"editing", sort_order:""};
  document.getElementById("edTitle").textContent = CUR ? ("DAY "+L.day+" 편집") : "새 레슨";
  document.getElementById("f-id").value = CUR ? L.id : "";
  document.getElementById("f-day").value = L.day || "";
  document.getElementById("f-order").value = L.sort_order || L.day || "";
  document.getElementById("f-sched").value = (L.scheduled_at||"").replace(" ","T").slice(0,16);
  document.getElementById("f-status").value = L.status || "draft";
  document.getElementById("f-work").value = L.work_status || "editing";
  document.getElementById("f-title").value = L.title || "";
  document.getElementById("f-titleru").value = L.title_ru || "";
  document.getElementById("f-link").value = L.link || "";
  document.getElementById("f-body").value = L.body || "";
  document.getElementById("f-review").value = L.review || "";
  document.getElementById("f-quiz").value = quizToText(L.quiz);
  document.getElementById("f-quiz-today").value = quizToText(L.quiz_today);
  document.getElementById("f-summary").value = L.summary || "";
  document.getElementById("f-homework").value = L.homework || "";
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
    work_status: document.getElementById("f-work").value,
    title: document.getElementById("f-title").value,
    title_ru: document.getElementById("f-titleru").value,
    link: document.getElementById("f-link").value,
    body: document.getElementById("f-body").value,
    review: document.getElementById("f-review").value,
    summary: document.getElementById("f-summary").value,
    homework: document.getElementById("f-homework").value,
    quiz_text: document.getElementById("f-quiz").value,
    quiz_today_text: document.getElementById("f-quiz-today").value
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
  if(act==="unew"){ openStudent(null); return; }
  if(act==="uedit"){ openStudent(uid); return; }
  if(act==="ussave"){ saveStudent(); return; }
  if(act==="ustatus"){
    api("/api/user/update", {chat_id: uid, status: val}).then(function(r){
      toast(r.ok ? "상태를 변경했습니다" : ("실패: "+(r.error||""))); loadUsers();
    }); return;
  }
  if(act==="ureset"){
    if(!confirm("이 학생의 진도를 초기화할까요? 시작 DAY 이전 상태로 되돌립니다.")) return;
    api("/api/user/reset", {chat_id: uid}).then(function(r){
      toast(r.ok ? "진도를 초기화했습니다" : ("실패: "+(r.error||""))); loadUsers();
    }); return;
  }
  if(act==="udel"){
    if(!confirm("이 학생을 삭제할까요?\\n삭제하면 되돌릴 수 없습니다. 레슨과 음성 파일은 그대로 남습니다.")) return;
    api("/api/user/delete", {chat_id: uid}).then(function(r){
      toast(r.ok ? "삭제했습니다" : ("실패: "+(r.error||""))); loadUsers();
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

["uq","ufs","ufd"].forEach(function(id){
  var el = document.getElementById(id);
  if(el) el.addEventListener("input", renderUsers);
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
    app.create_task(daily_loop())
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
    app.add_handler(CallbackQueryHandler(cb_apply_premium, pattern="^apply_premium$"))
    log.info("봇 시작됨.")
    app.run_polling()


if __name__ == "__main__":
    main()
