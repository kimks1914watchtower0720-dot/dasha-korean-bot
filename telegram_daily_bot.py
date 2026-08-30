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
from datetime import datetime
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dasha-korean-bot")

BASE_DIR = Path(__file__).parent
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "여기에_봇토큰_입력")
CURRICULUM_PATH = BASE_DIR / "curriculum.json"
SUBSCRIBERS_PATH = BASE_DIR / "subscribers.json"
KST = pytz.timezone("Asia/Seoul")

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


# ---------- 텔레그램 명령어 ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = get_subscribers()
    subs.setdefault(chat_id, {"day": 0, "plan": "free"})
    save_subscribers(subs)
    await update.message.reply_text(
        "안녕하세요! 다샤의 한국어 30일 봇입니다.\n"
        "매일 오전 11시(KST)에 그날의 문법·단어·예문을 보내드려요.\n\n"
        "Здравствуйте! Это бот «Корейский за 30 дней с Дашей».\n"
        "Каждый день в 11:00 (по Сеулу) вы будете получать урок.\n\n"
        "지금 바로 오늘의 학습을 받고 싶다면 /today 를 입력하세요."
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = get_subscribers()
    day = subs.get(chat_id, {}).get("day", 0) + 1
    text = build_lesson_message(day)
    await update.message.reply_text(text, parse_mode="HTML")
    subs.setdefault(chat_id, {"day": 0, "plan": "free"})
    subs[chat_id]["day"] = day
    save_subscribers(subs)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    subs = get_subscribers()
    subs.pop(chat_id, None)
    save_subscribers(subs)
    await update.message.reply_text("알림을 중단했습니다. 다시 시작하려면 /start 를 입력하세요.")


def build_lesson_message(day: int) -> str:
    curriculum = get_curriculum()
    lesson = curriculum.get(str(day))
    if not lesson:
        return f"DAY {day} 콘텐츠가 아직 준비되지 않았습니다. 조금만 기다려주세요! 🙏"
    lines = [f"📅 <b>DAY {day} · {lesson.get('title_kr','')}</b>", f"<i>{lesson.get('title_ru','')}</i>", ""]
    if lesson.get("grammar"):
        lines.append("📖 <b>문법</b>")
        for g in lesson["grammar"]:
            lines.append(f"• {g['p']} — {g['ru']}")
        lines.append("")
    if lesson.get("vocab"):
        lines.append("🔤 <b>단어</b>")
        for v in lesson["vocab"]:
            lines.append(f"• {v['kr']} [{v['rom']}] — {v['ru']}")
        lines.append("")
    if lesson.get("sentences"):
        lines.append("💬 <b>예문</b>")
        for s in lesson["sentences"]:
            lines.append(f"• {s['kr']} — {s['ru']}")
    return "\n".join(lines)


# ---------- 매일 11시 자동 발송 ----------

async def send_daily_lessons(app):
    subs = get_subscribers()
    now = datetime.now(KST)
    log.info("11시 자동 발송 시작: %s, 구독자 %d명", now, len(subs))
    for chat_id, info in subs.items():
        if info.get("plan") != "premium":
            continue  # 무료 사용자는 텔레그램 자동발송 제외 (요금제 정책에 맞게 조정)
        next_day = info.get("day", 0) + 1
        text = build_lesson_message(next_day)
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            info["day"] = next_day
        except Exception as e:
            log.warning("발송 실패 chat_id=%s: %s", chat_id, e)
    save_subscribers(subs)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("stop", stop))

    scheduler = AsyncIOScheduler(timezone=KST)
    scheduler.add_job(lambda: app.create_task(send_daily_lessons(app)), "cron", hour=11, minute=0)
    scheduler.start()

    log.info("봇 시작됨. 매일 11:00(KST)에 자동 발송됩니다.")
    app.run_polling()


if __name__ == "__main__":
    main()
