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
from telethon.tl.types import KeyboardButton, ReplyKeyboardMarkup
from flask import Flask

# ====== КОНФИГУРАЦИЯ — ВСЁ НА ЛАТИНИЦЕ! ======
API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

SHEET_ID = '1QG1MWTZveCVUf8tBUUgRqZEA83qW_gZZSgV4sZiAuhM'  # ← Замените на свой!
SETTINGS_SHEET = 'SETTINGS'      # ✅ Латиница
REPORTS_SHEET = 'REPORTS'        # ✅ Латиница
PARTICIPANTS_SHEET = 'PARTICIPANTS'  # ✅ Латиница

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# 🔑 ADMIN_CHAT_ID — ЦЕЛОЕ ЧИСЛО, не строка!
ADMIN_CHAT_ID = 741688548  # ← Узнайте через @userinfobot

# ====== ЛОГИРОВАНИЕ — ПОДРОБНОЕ ДЛЯ ОТЛАДКИ ======
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ====== GOOGLE SHEETS ======
def get_sheet_service():
    credentials_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not credentials_json:
        logger.critical("❌ GOOGLE_APPLICATION_CREDENTIALS_JSON не установлен!")
        raise Exception("❌ GOOGLE_APPLICATION_CREDENTIALS_JSON is not set!")

    creds_dict = json.loads(credentials_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    service = build('sheets', 'v4', credentials=creds)
    logger.info("✅ Google Sheets API успешно инициализирован")
    return service.spreadsheets()

def load_settings(service):
    logger.info("🔄 Загрузка настроек из листа SETTINGS...")
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
        logger.info(f"   📌 Настройка добавлена: {row[0]} | дедлайн {row[1]}")
    logger.info(f"✅ Загружено {len(settings)} активных тем")
    return settings

def load_participants(service):
    logger.info("🔄 Загрузка списка участников из листа PARTICIPANTS...")
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{PARTICIPANTS_SHEET}!A:A").execute()
    values = result.get('values', [])
    participants = [row[0].strip() for row in values[1:] if row and row[0].strip()]
    logger.info(f"✅ Загружено {len(participants)} участников: {participants}")
    return participants

def record_submission(service, topic, participant, status, send_time, link=""):
    now = datetime.now().strftime("%Y-%m-%d")
    row = [now, topic, participant, status, send_time, link]
    logger.info(f"📝 Запись в таблицу REPORTS: {row}")
    try:
        service.values().append(
            spreadsheetId=SHEET_ID,
            range=f"{REPORTS_SHEET}!A:F",
            valueInputOption="USER_ENTERED",
            body={"values": [row]}
        ).execute()
        logger.info(f"✅ Запись сохранена: {participant} в {topic}")
    except Exception as e:
        logger.error(f"❌ Ошибка записи в Google Sheets: {e}")

# ====== ПАРСИНГ ХЭШТЕГА — ГИБКАЯ ЛОГИКА ======
def extract_name(text):
    """Извлекает #Фамилия_Имя и преобразует в Фамилия Имя"""
    match = re.search(r'#([А-Яа-яЁё]+_[А-Яа-яЁё]+)', text)
    if not match:
        logger.debug(f"🔍 Не найден хэштег в сообщении: {text[:50]}...")
        return None
    name_with_underscore = match.group(1)
    # Преобразуем подчёркивание в пробел для совпадения с таблицей
    name_normalized = name_with_underscore.replace('_', ' ')
    logger.info(f"🏷️ Обнаружен хэштег: '{name_with_underscore}' → нормализовано: '{name_normalized}'")
    return name_normalized

# ====== ПРИНУДИТЕЛЬНАЯ ПРОВЕРКА (по команде или расписанию) ======
async def force_check(client, service, settings, participants):
    """Принудительная проверка всех тем за сегодня"""
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"⏳ Запуск принудительной проверки на дату: {today}")

    report_lines = []

    for setting in settings:
        topic = setting['topic_name']
        deadline = setting['deadline']

        logger.info(f"📊 Проверка темы: {topic} (дедлайн {deadline})")

        # Получаем всех, кто сдал сегодня
        result = service.values().get(spreadsheetId=SHEET_ID, range=f"{REPORTS_SHEET}!A:C").execute()
        rows = result.get('values', [])
        submitted = set()
        for row in rows[1:]:
            if len(row) >= 3 and row[0] == today and row[1] == topic:
                submitted.add(row[2])
                logger.debug(f"   👤 {row[2]} сдал в {row[4]}")

        missing = [p for p in participants if p not in submitted]

        if missing:
            report_lines.append(f"📌 *{topic}* (дедлайн {deadline}):")
            report_lines.append("❌ Не сдали: " + ", ".join(missing))
            report_lines.append("")
            logger.info(f"   ❌ Не сдали: {missing}")
        else:
            logger.info(f"   ✅ Все сдали: {len(participants)} участников")

    if report_lines:
        message = "\n".join(report_lines)
        logger.info(f"📩 Отчёт будет отправлен админу:\n{message}")
        try:
            await client.send_message(ADMIN_CHAT_ID, message, parse_mode='markdown')
            logger.info("✅ Отчёт успешно отправлен в Telegram")
        except Exception as e:
            logger.error(f"❌ Не удалось отправить отчёт: {e}")
        return message
    else:
        message = "✅ Все участники сдали задания сегодня!"
        logger.info(f"📩 Отчёт: {message}")
        try:
            await client.send_message(ADMIN_CHAT_ID, message, parse_mode='markdown')
            logger.info("✅ Отчёт успешно отправлен в Telegram")
        except Exception as e:
            logger.error(f"❌ Не удалось отправить отчёт: {e}")
        return message

# ====== ЕЖЕЧАСОВАЯ АВТОМАТИЧЕСКАЯ ПРОВЕРКА ======
async def scheduled_force_check(client, service, settings, participants):
    """Запускает принудительную проверку каждые 60 минут"""
    while True:
        try:
            logger.info("⏳ Запуск автоматической проверки (каждые 60 минут)...")
            await force_check(client, service, settings, participants)
        except Exception as e:
            logger.error(f"❌ Ошибка при автоматической проверке: {e}")
        await asyncio.sleep(60 * 60)  # 60 минут

# ====== FLASK HTTP-СЕРВЕР (для Render) ======
app = Flask(__name__)

@app.route('/')
def health():
    logger.info("🌐 Получен HTTP-запрос /health от Render")
    return "✅ Telegram bot is running!", 200

@app.route('/check', methods=['GET'])
def check():
    """Эндпоинт для ручной проверки"""
    logger.info("🌐 Получен запрос /check — запускаю принудительную проверку...")
    try:
        # Это просто "подтверждение", реальная проверка делается через бота
        return "<pre>🟢 Принудительная проверка запущена. Отчёт будет отправлен в Telegram.</pre>", 200
    except Exception as e:
        logger.error(f"❌ Ошибка в /check: {e}")
        return f"<pre>❌ Ошибка: {str(e)}</pre>", 500

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🌐 Запуск Flask-сервера на порту {port}")
    app.run(host='0.0.0.0', port=port, threaded=True, debug=False)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

# ====== ОБРАБОТЧИК СООБЩЕНИЙ В ТЕМАХ ======
async def handle_message(event, client, service, settings_map):
    message = event.message
    logger.info(f"📩 ПОЛУЧЕНО СООБЩЕНИЕ: {message.text[:100]}...")

    # Проверяем, что это сообщение из темы группы (не личный чат)
    if not hasattr(message.peer_id, 'channel_id'):
        logger.debug("   ❌ Это не сообщение из группы — пропускаем")
        return

    topic_name = getattr(message, 'topic_name', None)
    if not topic_name:
        logger.debug("   ❌ Сообщение не в теме — пропускаем")
        return

    logger.info(f"   📌 Тема: {topic_name}")

    # Получаем настройки для этой темы
    setting = settings_map.get(topic_name)
    if not setting:
        logger.warning(f"   ⚠️ Настройки для темы '{topic_name}' не найдены в таблице SETTINGS")
        return

    text = message.text or ""
    chat_id = str(message.peer_id.channel_id)

    # Извлекаем имя из хэштега
    name = extract_name(text)
    if not name:
        logger.warning(f"   ❌ Нет корректного хэштега в сообщении: {text}")
        return

    # Проверяем, не было ли уже такого же имени сегодня
    today = datetime.now().strftime("%Y-%m-%d")
    result = service.values().get(spreadsheetId=SHEET_ID, range=f"{REPORTS_SHEET}!A:C").execute()
    rows = result.get('values', [])
    for row in rows[1:]:
        if len(row) >= 3 and row[0] == today and row[1] == topic_name and row[2] == name:
            logger.info(f"   ✅ Уже записано: {name} в {topic_name}")
            return

    # Проверяем дедлайн
    deadline_str = setting['deadline']
    try:
        deadline_hour, deadline_min = map(int, deadline_str.split(':'))
    except ValueError:
        logger.error(f"   ❌ Неверный формат дедлайна: {deadline_str}")
        return

    now = datetime.now()
    deadline = now.replace(hour=deadline_hour, minute=deadline_min, second=0, microsecond=0)
    status = "Сдал" if now <= deadline else "Опоздал"

    # Формируем ссылку на сообщение
    link = f"https://t.me/c/{chat_id[4:]}/{message.id}" if chat_id.startswith('-100') else ""

    # Записываем в таблицу
    record_submission(service, topic_name, name, status, now.strftime("%H:%M"), link)

    # Логируем успех
    logger.info(f"✅ УСПЕШНО: {name} ({status}) в {topic_name} — время: {now.strftime('%H:%M')}")

# ====== ЗАПУСК БОТА ======
async def main():
    logger.info("🚀 Запуск основного цикла бота...")

    service = get_sheet_service()
    settings = load_settings(service)
    participants = load_participants(service)
    settings_map = {s['topic_name']: s for s in settings}

    session_path = "/opt/render/project/src/shbm_session"
    client = TelegramClient(session_path, API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        logger.info("🤖 Бот успешно авторизован в Telegram")
    except Exception as e:
        if "FloodWaitError" in str(e):
            logger.critical("🛑 Телеграм заблокировал доступ — подождите 10 минут.")
            raise SystemExit(1)
        else:
            logger.critical(f"❌ Ошибка авторизации: {e}")
            raise

    # 💡 Устанавливаем постоянную клавиатуру
    button = KeyboardButton(text="🔍 Проверить сейчас")
    markup = ReplyKeyboardMarkup([[button]])  # Без лишних параметров!

    try:
        await client.send_message(ADMIN_CHAT_ID, "✅ Бот готов к работе. Нажмите кнопку ниже:", buttons=markup)
        logger.info("✅ Кнопка отправлена админу")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось отправить кнопку: {e}")

    # 👇 Команда /check — альтернатива кнопке
    @client.on(events.NewMessage(incoming=True, pattern=r'^/check$'))
    async def on_command_check(event):
        logger.info("👤 Пользователь использовал команду /check")
        await event.reply("🔄 Запускаю проверку...")
        await force_check(client, service, settings, participants)

    # 👇 Обработчик всех входящих сообщений
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        await handle_message(event, client, service, settings_map)

    # Запускаем ежечасную проверку
    asyncio.create_task(scheduled_force_check(client, service, settings, participants))

    logger.info("📡 Бот ожидает сообщений...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    logger.info("🏁 Запуск скрипта shbm_auto_checker.py...")
    asyncio.run(main())
