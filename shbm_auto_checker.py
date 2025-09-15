import os
import re
import asyncio
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import logging
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityHashtag

# ====== КОНФИГУРАЦИЯ ======
API_ID = '20299753'
API_HASH = '946ca1572df8a667a3bd81d78370310d'
BOT_TOKEN = '8363948497:AAGvcnuftvrZbaHMBubIbtevRlPRPXaLFfw'

SHEET_ID = '1QG1MWTZveCVUf8tBUUgRqZEA83qW_gZZSgV4sZiAuhM'  # ← ЗАМЕНИ НА СВОЙ!
CREDENTIALS_FILE = 'credentials.json'

SETTINGS_SHEET = 'Настройки'
REPORTS_SHEET = 'Отчеты'
PARTICIPANTS_SHEET = 'Участники'

# ====== ЛОГИРОВАНИЕ ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== GOOGLE SHEETS ======
def get_sheet_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return build('sheets', 'v4', credentials=creds).spreadsheets()

def load_settings(service):
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{SETTINGS_SHEET}!A:E").execute()
    values = result.get('values', [])
    settings = []
    for row in values[1:]:
        if len(row) < 5 or row[3].lower() != 'да':  # активна?
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

# ====== ПАРСИНГ ======
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
    sender = message.from_user.first_name
    username = getattr(message.from_user, 'username', None)
    chat_id = str(message.peer_id.channel_id)  # ID группы

    # Получаем настройки для этой темы
    setting = settings_map.get(topic_name)
    if not setting:
        return

    # Проверяем формат
    name = extract_name(text)
    if not name:
        logger.info(f"Неправильный формат: {text} | Тема: {topic_name}")
        return

    # Проверяем, не было ли уже такого же имени сегодня
    today = datetime.now().strftime("%Y-%m-%d")
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{REPORTS_SHEET}!A:C").execute()
    rows = result.get('values', [])
    for row in rows[1:]:
        if len(row) >= 3 and row[0] == today and row[1] == topic_name and row[2] == name:
            logger.info(f"Уже записано: {name} в {topic_name}")
            return

    # Проверяем дедлайн
    deadline_str = setting['deadline']
    deadline_hour, deadline_min = map(int, deadline_str.split(':'))
    now = datetime.now()
    deadline = now.replace(hour=deadline_hour, minute=deadline_min, second=0, microsecond=0)
    status = "Сдал" if now <= deadline else "Опоздал"

    # Формируем ссылку на сообщение
    link = f"https://t.me/c/{chat_id[4:]}/{message.id}" if chat_id.startswith('-100') else ""

    # Сохраняем
    record_submission(service, topic_name, name, status, now.strftime("%H:%M"), link)
    logger.info(f"✅ Записано: {name} ({status}) в {topic_name}")

# ====== ЕЖЕДНЕВНЫЙ ОТЧЁТ ======
async def daily_report(service, settings, participants):
    today = datetime.now().strftime("%Y-%m-%d")
    report_lines = []

    for setting in settings:
        topic = setting['topic_name']
        deadline = setting['deadline']

        # Получаем всех, кто сдал
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
        admin_chat_id = "741688548"  # ← ЗАМЕНИ НА СВОЙ (узнай через @userinfobot)
        await send_telegram_message(admin_chat_id, "\n".join(report_lines))

async def send_telegram_message(chat_id, text):
    """Простая отправка сообщения через бота (если нужно)"""
    # Здесь можно использовать Telethon для отправки
    # Но если ты хочешь только сбор данных — можно пока пропустить
    print("📩 Отчёт:", text)

# ====== ЗАПУСК БОТА ======
async def main():
    service = get_sheet_service()
    settings = load_settings(service)
    participants = load_participants(service)

    # Словарь: название темы -> настройка
    settings_map = {s['topic_name']: s for s in settings}

    # Создаём клиент
    client = TelegramClient('shbm_session', API_ID, API_HASH)

    await client.start(bot_token=BOT_TOKEN)
    logger.info("🤖 Бот запущен. Слушаю темы...")

    # Регистрируем обработчик
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        await handle_message(event, client, service, settings_map)

    # Запускаем ежедневный отчёт в 12:00 (можно сделать через cron — проще)
    # Для теста — запустим один раз сейчас
    await daily_report(service, settings, participants)

    # Ждём событий
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())