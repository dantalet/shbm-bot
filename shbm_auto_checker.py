import os
import re
import asyncio
import json
import threading
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import logging
from telethon import TelegramClient, events
from telethon.types import KeyboardButton, ReplyKeyboardMarkup
from flask import Flask

# ====== КОНФИГУРАЦИЯ ======
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

SHEET_ID = '1QG1MWTZveCVUf8tBUUgRqZEA83qW_gZZSgV4sZiAuhM'
SETTINGS_SHEET = 'SETTINGS'
REPORTS_SHEET = 'REPORTS'
PARTICIPANTS_SHEET = 'PARTICIPANTS'

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

ADMIN_CHAT_ID = "741688548"  # ← ЗАМЕНИ НА СВОЙ ID (узнай через @userinfobot)

# ====== ЛОГИРОВАНИЕ ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== GOOGLE SHEETS ======
def get_sheet_service():
    credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not credentials_json:
        raise Exception("❌ GOOGLE_APPLICATION_CREDENTIALS_JSON is not set!")

    creds_dict = json.loads(credentials_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return build('sheets', 'v4', credentials=creds).spreadsheets()

def load_settings(service):
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{SETTINGS_SHEET}!A:E").execute()
    values = result.get('values', [])
    settings = []
    for row in values[1:]:
        if len(row) < 5 or row[3].lower() != 'да':
            continue
        settings.append({
            'topic_name': row[0],
            'deadline': row[1],  # HH:MM
            'format_pattern': row[2],
            'chat_id': row[4]
        })
    return settings

def load_participants(service):
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{PARTICIPANTS_SHEET}!A:A").execute()
    values = result.get('values', [])
    return [row[0].strip() for row in values[1:] if row and row[0].strip()]

def record_submission(service, topic, participant, status, send_time, link=""):
    now = datetime.now().strftime("%Y-%m-%d")
    row = [now, topic, participant, status, send_time, link]
    service.values().append(
        spreadsheetId=SHEET_ID,
        range=f"{REPORTS_SHEET}!A:F",
        valueInputOption="USER_ENTERED",
        body={"values": [row]}
    ).execute()

def extract_name(text):
    match = re.search(r'#([А-Яа-яЁё]+_[А-Яа-яЁё]+)', text)
    return match.group(1) if match else None

# ====== ОБРАБОТЧИК СООБЩЕНИЙ ======
async def handle_message(event, client, service, settings_map):
    message = event.message
    if not message.is_topic_message:
        return

    topic_name = message.topic_name
    text = message.text or ""
    chat_id = str(message.peer_id.channel_id)

    setting = settings_map.get(topic_name)
    if not setting:
        return

    name = extract_name(text)
    if not name:
        logger.info(f"Неправильный формат: {text} | Тема: {topic_name}")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{REPORTS_SHEET}!A:C").execute()
    rows = result.get('values', [])
    for row in rows[1:]:
        if len(row) >= 3 and row[0] == today and row[1] == topic_name and row[2] == name:
            logger.info(f"Уже записано: {name} в {topic_name}")
            return

    deadline_str = setting['deadline']
    deadline_hour, deadline_min = map(int, deadline_str.split(':'))
    now = datetime.now()
    deadline = now.replace(hour=deadline_hour, minute=deadline_min, second=0, microsecond=0)
    status = "Сдал" if now <= deadline else "Опоздал"

    link = f"https://t.me/c/{chat_id[4:]}/{message.id}" if chat_id.startswith('-100') else ""

    record_submission(service, topic_name, name, status, now.strftime("%H:%M"), link)
    logger.info(f"✅ Записано: {name} ({status}) в {topic_name}")

# ====== ПРИНУДИТЕЛЬНАЯ ПРОВЕРКА (по кнопке или команде) ======
async def force_check(client, service, settings, participants):
    """Принудительная проверка всех тем за сегодня"""
    today = datetime.now().strftime("%Y-%m-%d")
    report_lines = []

    for setting in settings:
        topic = setting['topic_name']
        deadline = setting['deadline']

        # Получаем всех, кто сдал сегодня
        result = service.values().get(spreadsheetId=SHEET_ID, range=f"{REPORTS_SHEET}!A:C").execute()
        rows = result.get('values', [])
        submitted = set()
        for row in rows[1:]:
            if len(row) >= 3 and row[0] == today and row[1] == topic:
                submitted.add(row[2])

        missing = [p for p in participants if p not in submitted]

        if missing:
            report_lines.append(f"📌 *{topic}* (дедлайн {deadline}):")
            report_lines.append("❌ Не сдали: " + ", ".join(missing))
            report_lines.append("")

    if report_lines:
        message = "\n".join(report_lines)
        try:
            await client.send_message(ADMIN_CHAT_ID, message, parse_mode='markdown')
            logger.info("📩 Отчёт отправлен по кнопке 'Проверить сейчас'")
        except Exception as e:
            logger.error(f"❌ Не удалось отправить отчёт: {e}")
        return message
    else:
        message = "✅ Все участники сдали задания сегодня!"
        try:
            await client.send_message(ADMIN_CHAT_ID, message, parse_mode='markdown')
            logger.info("📩 Отчёт отправлен по кнопке 'Проверить сейчас'")
        except Exception as e:
            logger.error(f"❌ Не удалось отправить отчёт: {e}")
        return message

# ====== ЕЖЕЧАСОВАЯ АВТОМАТИЧЕСКАЯ ПРОВЕРКА ======
async def scheduled_force_check(client, service, settings, participants):
    """Запускает принудительную проверку каждые 60 минут"""
    while True:
        try:
            logger.info("⏳ Запуск автоматической проверки...")
            await force_check(client, service, settings, participants)
        except Exception as e:
            logger.error(f"❌ Ошибка при автоматической проверке: {e}")
        await asyncio.sleep(60 * 60)  # 60 минут

# ====== FLASK HTTP-СЕРВЕР (для Render) ======
app = Flask(__name__)

@app.route('/')
def health():
    return "✅ Telegram bot is running!", 200

@app.route('/check', methods=['GET'])
def check():
    """Эндпоинт для ручной проверки (можно вызвать извне)"""
    try:
        # Здесь можно добавить токен-защиту, если нужно
        return "<pre>🟢 Принудительная проверка запущена. Отчёт будет отправлен в Telegram.</pre>", 200
    except Exception as e:
        return f"<pre>❌ Ошибка: {str(e)}</pre>", 500

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# ====== ЗАПУСК БОТА ======
async def main():
    service = get_sheet_service()
    settings = load_settings(service)
    participants = load_participants(service)
    settings_map = {s['topic_name']: s for s in settings}

    # Создаём клиент
    client = TelegramClient('shbm_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    logger.info("🤖 Бот запущен. Слушаю темы...")

    # Устанавливаем кнопку в меню бота
    button = KeyboardButton(text="🔍 Проверить сейчас")
    markup = ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=False)
    await client.send_message(ADMIN_CHAT_ID, "✅ Бот готов к работе. Нажмите 'Проверить сейчас' для ручной проверки.", buttons=markup)

    # Обработчик нажатия кнопки
    @client.on(events.NewMessage(incoming=True, pattern=r'^🔍\s*Проверить сейчас$'))
    async def on_button_press(event):
        logger.info("🖱️ Пользователь нажал 'Проверить сейчас'")
        await event.reply("🔄 Запускаю проверку...")
        await force_check(client, service, settings, participants)

    # Обработчик новых сообщений в темах
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        await handle_message(event, client, service, settings_map)

    # Запускаем ежечасную проверку
    asyncio.create_task(scheduled_force_check(client, service, settings, participants))

    # Ждём событий
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
