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
from datetime import datetime, timedelta
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
    subs.setdefault(chat_id, {"day": 0, "plan": "free"})
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
