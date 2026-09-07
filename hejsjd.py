import asyncio
import json
import os
import logging
import zipfile
import io
import shutil
import sqlite3
import binascii
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
from dataclasses import dataclass

from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.errors import PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired, SessionPasswordNeeded
from telethon import TelegramClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ==================== ЗАГРУЗКА .ENV ====================
load_dotenv()

# ==================== КОНФИГ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
PASSWORD = os.getenv("PASSWORD")

if not all([BOT_TOKEN, API_ID, API_HASH, ADMIN_ID, PASSWORD]):
    raise ValueError("❌ Проверь .env файл!")

# ==================== ПУТИ ====================
ROOT = Path(__file__).resolve().parent
SESSIONS_DIR = ROOT / "sessions"
TDATA_DIR = ROOT / "tdata_temp"
USERS_FILE = ROOT / "users.json"
LOGS_FILE = ROOT / "logs.txt"
DEBUG_LOG_PATH = ROOT / "debug-fca127.log"

SESSIONS_DIR.mkdir(exist_ok=True)
TDATA_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

auth_data = {}
DEBUG_SESSION_ID = "fca127"

# ==================== ЛОГГЕР ====================

def agent_log(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    payload = {
        "sessionId": DEBUG_SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "id": f"log_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

# ==================== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ====================

def load_users() -> Dict:
    if USERS_FILE.exists():
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"users": []}

def save_users(data: Dict):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_user(user_id: int) -> Dict:
    data = load_users()
    for user in data["users"]:
        if user["user_id"] == user_id:
            return user
    return None

def create_user(user_id: int, username: str = None):
    data = load_users()
    for user in data["users"]:
        if user["user_id"] == user_id:
            return user
    new_user = {
        "user_id": user_id,
        "username": username or str(user_id),
        "is_admin": user_id == ADMIN_ID,
        "subscription_end": None,
        "sessions": [],
        "reports_sent": 0,
        "created_at": datetime.now().isoformat()
    }
    data["users"].append(new_user)
    save_users(data)
    return new_user

def has_subscription(user_id: int) -> bool:
    user = get_user(user_id)
    if not user:
        return False
    if user.get("is_admin"):
        return True
    subscription_end = user.get("subscription_end")
    if not subscription_end:
        return False
    try:
        end_date = datetime.fromisoformat(subscription_end)
        return datetime.now() < end_date
    except:
        return False

def get_subscription_info(user_id: int) -> str:
    user = get_user(user_id)
    if not user:
        return "❌ Не найден"
    if user.get("is_admin"):
        return "👑 Админ (бессрочно)"
    subscription_end = user.get("subscription_end")
    if not subscription_end:
        return "❌ Нет подписки"
    try:
        end_date = datetime.fromisoformat(subscription_end)
        if datetime.now() < end_date:
            days_left = (end_date - datetime.now()).days
            hours_left = (end_date - datetime.now()).seconds // 3600
            return f"✅ {days_left}д {hours_left}ч"
        else:
            return "❌ Истекла"
    except:
        return "❌ Ошибка"

def add_subscription(user_id: int, days: int = 1):
    data = load_users()
    for user in data["users"]:
        if user["user_id"] == user_id:
            if user.get("subscription_end"):
                try:
                    current_end = datetime.fromisoformat(user["subscription_end"])
                    new_end = max(current_end, datetime.now()) + timedelta(days=days)
                except:
                    new_end = datetime.now() + timedelta(days=days)
            else:
                new_end = datetime.now() + timedelta(days=days)
            user["subscription_end"] = new_end.isoformat()
            save_users(data)
            save_log(f"✅ Подписка {days}д для {user_id}")
            return True
    return False

def get_user_sessions(user_id: int) -> List[Dict]:
    user = get_user(user_id)
    if not user:
        return []
    return user.get("sessions", [])

def add_user_session(user_id: int, session_name: str, phone: str):
    data = load_users()
    for user in data["users"]:
        if user["user_id"] == user_id:
            for s in user["sessions"]:
                if s["session_name"] == session_name:
                    return False
            user["sessions"].append({
                "session_name": session_name,
                "phone": phone,
                "added_at": datetime.now().isoformat(),
                "reports_sent": 0
            })
            save_users(data)
            save_log(f"📱 Добавлена сессия {session_name} для {user_id}")
            return True
    return False

def increment_report_count(user_id: int, session_name: str):
    data = load_users()
    for user in data["users"]:
        if user["user_id"] == user_id:
            user["reports_sent"] = user.get("reports_sent", 0) + 1
            for s in user["sessions"]:
                if s["session_name"] == session_name:
                    s["reports_sent"] = s.get("reports_sent", 0) + 1
                    break
            save_users(data)
            return True
    return False

def save_log(message: str):
    with open(LOGS_FILE, 'a', encoding='utf-8') as f:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"[{timestamp}] {message}\n")

def get_logs() -> str:
    if LOGS_FILE.exists():
        with open(LOGS_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return ''.join(lines[-50:])
    return "Логов пока нет"

# ==================== НОРМАЛИЗАЦИЯ НОМЕРА ====================

def normalize_phone(phone: str) -> str:
    """Приводит номер к формату +XXXXXXXXXXX"""
    phone = re.sub(r'[^0-9]', '', phone)
    if not phone.startswith('+'):
        phone = '+' + phone
    return phone

def session_name_for_phone(phone: str) -> str:
    """Генерирует имя сессии для номера"""
    phone_clean = re.sub(r'[^0-9]', '', phone)
    return f"snoser_{phone_clean}"

# ==================== АВТОРИЗАЦИЯ ПО НОМЕРУ (Telethon) ====================

async def auth_by_phone_telethon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Авторизация через Telethon с auth_key"""
    user_id = update.effective_user.id
    if not has_subscription(user_id) and user_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет подписки!")
        return
    
    await update.message.reply_text(
        "🔑 **Авторизация через auth_key (Telethon)**\n\n"
        "Отправь auth_key в HEX формате (как в authoreg.py):\n"
        "`a5043b32b455fa3f2421b7e52a7f...`\n\n"
        "Или отправь .session файл",
        parse_mode='Markdown'
    )
    context.user_data['step'] = 'waiting_auth_key'

async def handle_auth_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка auth_key и создание сессии"""
    user_id = update.effective_user.id
    auth_key_hex = update.message.text.strip()
    
    # Проверяем, что это HEX
    if not re.match(r'^[0-9a-fA-F]+$', auth_key_hex):
        await update.message.reply_text("❌ Неверный формат! Отправь HEX строку auth_key")
        return
    
    try:
        auth_key_bytes = binascii.unhexlify(auth_key_hex)
        
        # Создаем временный клиент Telethon
        client = TelegramClient(f'temp_{user_id}', API_ID, API_HASH)
        
        # Устанавливаем auth_key
        client.session.auth_key.key = auth_key_bytes
        client.session.dc_id = 1  # Стандартный DC
        
        await client.connect()
        
        if await client.is_user_authorized():
            # Получаем номер
            me = await client.get_me()
            phone = me.phone_number
            
            # Сохраняем сессию (Telethon)
            session_name = session_name_for_phone(phone)
            session_path = SESSIONS_DIR / f"{session_name}.session"
            
            # Сохраняем сессию Telethon
            await client.session.save()
            await client.disconnect()
            
            # Копируем файл сессии
            temp_session = Path(f"temp_{user_id}.session")
            if temp_session.exists():
                shutil.copy2(temp_session, session_path)
                temp_session.unlink()
            
            if add_user_session(user_id, session_name, phone):
                await update.message.reply_text(
                    f"✅ **Авторизация успешна!**\n\n"
                    f"📞 Телефон: `{phone}`\n"
                    f"📝 Сессия: `{session_name}`",
                    parse_mode='Markdown'
                )
                save_log(f"✅ {phone} авторизован через auth_key")
            else:
                await update.message.reply_text(f"⚠️ Сессия уже существует!")
        else:
            await update.message.reply_text("❌ Не удалось авторизоваться по auth_key")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
    
    context.user_data['step'] = None

# ==================== АВТОРИЗАЦИЯ ПО НОМЕРУ (Pyrogram) ====================

async def cmd_start_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not has_subscription(user_id) and user_id != ADMIN_ID:
        await update.message.reply_text("❌ Нет подписки!")
        return
    await update.message.reply_text(
        "📱 **Введите номер телефона**\n\nФормат: `+79991234567`\n\n"
        "Или отправь файл .session / .zip / .txt",
        parse_mode='Markdown'
    )
    context.user_data['step'] = 'waiting_phone'

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    phone = update.message.text.strip()
    
    if not re.match(r'^\+\d{10,15}$', phone):
        await update.message.reply_text("❌ Неверный формат! Используй: +79991234567")
        return
    
    try:
        client = Client(
            f"temp_{user_id}_{int(datetime.now().timestamp())}",
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=str(SESSIONS_DIR)
        )
        await client.connect()
        sent_code = await client.send_code(phone)
        
        auth_data[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent_code.phone_code_hash
        }
        
        await update.message.reply_text(
            f"✅ **Код отправлен на {phone}**\n\nТеперь отправь код:",
            parse_mode='Markdown'
        )
        context.user_data['step'] = 'waiting_code'
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    code = update.message.text.strip()
    
    if user_id not in auth_data:
        await update.message.reply_text("❌ Сессия не найдена! Начни заново: /start_auth")
        context.user_data['step'] = None
        return
    
    if not code or not code.isdigit():
        await update.message.reply_text("❌ Введи код цифрами! Пример: 22573")
        return
    
    auth_info = auth_data[user_id]
    client = auth_info["client"]
    phone = auth_info["phone"]
    
    try:
        await client.sign_in(phone, code, auth_info["phone_code_hash"])
        session_name = session_name_for_phone(phone)
        await client.stop()
        
        if add_user_session(user_id, session_name, phone):
            await update.message.reply_text(
                f"✅ **Авторизация успешна!**\n\n📞 {phone}\n📝 {session_name}",
                parse_mode='Markdown'
            )
            save_log(f"✅ {phone} авторизован")
        else:
            await update.message.reply_text(f"⚠️ Сессия {session_name} уже существует!")
        
        del auth_data[user_id]
        context.user_data['step'] = None
    except PhoneCodeInvalid:
        await update.message.reply_text("❌ Неверный код! Попробуй еще раз")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def cancel_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in auth_data:
        try:
            await auth_data[user_id]["client"].disconnect()
        except:
            pass
        del auth_data[user_id]
    context.user_data['step'] = None
    await update.message.reply_text("❌ Авторизация отменена")

# ==================== ОБРАБОТКА ФАЙЛОВ (С ТВОЕЙ ЛОГИКОЙ) ====================

def copy_session_files(src_dir: Path, dst_dir: Path, phone: str, session_name: str) -> None:
    """Копирует .session и .json файлы"""
    src_session = src_dir / f"{phone}.session"
    src_json = src_dir / f"{phone}.json"
    dst_session = dst_dir / f"{session_name}.session"
    dst_json = dst_dir / f"{session_name}.json"
    if src_session.exists():
        shutil.copy2(src_session, dst_session)
    if src_json.exists():
        shutil.copy2(src_json, dst_json)

def prune_stale_files(sessions_dir: Path, keep_names: set[str]) -> int:
    """Удаляет старые файлы сессий"""
    removed = 0
    managed_prefixes = ("snoser_", "ss_")
    for p in sessions_dir.glob("*"):
        if not p.is_file():
            continue
        if p.suffix not in (".session", ".json"):
            continue
        stem = p.stem
        if not stem.startswith(managed_prefixes):
            continue
        if stem in keep_names:
            continue
        p.unlink(missing_ok=True)
        removed += 1
    return removed

async def process_uploaded_file(file_content, filename: str, user_id: int) -> Dict:
    """Обработка файлов с твоей логикой"""
    results = {"added": 0, "errors": [], "sessions": []}
    run_id = f"run-{int(time.time())}"
    
    # ===== .session файл =====
    if filename.endswith('.session'):
        session_name = filename.replace('.session', '')
        session_path = SESSIONS_DIR / filename
        with open(session_path, 'wb') as f:
            f.write(file_content)
        
        # Пробуем извлечь номер из имени
        phone_match = re.search(r'(\+?\d{10,15})', session_name)
        if phone_match:
            phone = phone_match.group(0)
        else:
            # Пробуем прочитать из сессии
            try:
                client = Client(str(session_path.stem), api_id=API_ID, api_hash=API_HASH, workdir=str(SESSIONS_DIR))
                await client.start()
                me = await client.get_me()
                phone = me.phone_number or session_name
                await client.stop()
            except:
                phone = session_name
        
        if add_user_session(user_id, session_name, phone):
            results["added"] += 1
            results["sessions"].append({"session_name": session_name, "phone": phone})
        else:
            results["errors"].append(f"Сессия {session_name} уже существует")
        return results
    
    # ===== .zip архив =====
    if filename.endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(file_content)) as zip_file:
                file_list = zip_file.namelist()
                keep_names = set()
                
                # .session внутри zip
                session_files = [f for f in file_list if f.endswith('.session')]
                if session_files:
                    for session_file in session_files:
                        session_name = session_file.replace('.session', '')
                        # Нормализуем имя
                        phone_match = re.search(r'(\+?\d{10,15})', session_name)
                        if phone_match:
                            phone = phone_match.group(0)
                            session_name = session_name_for_phone(phone)
                        else:
                            phone = session_name
                        
                        session_path = SESSIONS_DIR / f"{session_name}.session"
                        with open(session_path, 'wb') as f:
                            f.write(zip_file.read(session_file))
                        
                        keep_names.add(session_name)
                        if add_user_session(user_id, session_name, phone):
                            results["added"] += 1
                            results["sessions"].append({"session_name": session_name, "phone": phone})
                        else:
                            results["errors"].append(f"Сессия {session_name} уже существует")
                    
                    # Удаляем старые файлы
                    pruned = prune_stale_files(SESSIONS_DIR, keep_names)
                    agent_log(run_id, "IM1", "process_uploaded_file", "Pruned stale files", {"pruned": pruned})
                    return results
                
                # tdata внутри zip
                tdata_files = [f for f in file_list if 'tdata' in f.lower() or 'D877F783D5D3EF8C' in f]
                if tdata_files:
                    tdata_folder = TDATA_DIR / f"tdata_{user_id}_{int(datetime.now().timestamp())}"
                    tdata_folder.mkdir(exist_ok=True)
                    
                    for file in tdata_files:
                        if not file.endswith('/'):
                            target_path = tdata_folder / os.path.basename(file)
                            with open(target_path, 'wb') as f:
                                f.write(zip_file.read(file))
                    
                    # Пытаемся конвертировать tdata в .session через Pyrogram
                    try:
                        temp_client = Client(
                            f"tdata_{user_id}_{int(datetime.now().timestamp())}",
                            api_id=API_ID,
                            api_hash=API_HASH,
                            workdir=str(SESSIONS_DIR)
                        )
                        
                        await temp_client.start()
                        me = await temp_client.get_me()
                        phone = me.phone_number
                        session_name = session_name_for_phone(phone)
                        await temp_client.stop()
                        
                        # Переименовываем сессию
                        old_session = SESSIONS_DIR / f"{temp_client.name}.session"
                        new_session = SESSIONS_DIR / f"{session_name}.session"
                        if old_session.exists():
                            shutil.move(old_session, new_session)
                        
                        if add_user_session(user_id, session_name, phone):
                            results["added"] += 1
                            results["sessions"].append({"session_name": session_name, "phone": phone})
                            save_log(f"📦 Конвертирован tdata в {session_name}")
                        else:
                            results["errors"].append(f"Сессия {session_name} уже существует")
                        
                        shutil.rmtree(tdata_folder, ignore_errors=True)
                    except Exception as e:
                        results["errors"].append(f"Ошибка конвертации tdata: {e}")
                        shutil.rmtree(tdata_folder, ignore_errors=True)
                    
                    return results
                
                results["errors"].append("В ZIP нет .session или tdata")
        except Exception as e:
            results["errors"].append(f"Ошибка ZIP: {e}")
        return results
    
    # ===== .txt файл =====
    if filename.endswith('.txt'):
        try:
            content = file_content.decode('utf-8')
            lines = content.strip().split('\n')
            keep_names = set()
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # Строка сессии Pyrogram
                if ':' in line and len(line) > 30:
                    session_name = f"ss_{int(datetime.now().timestamp())}_{uuid.uuid4().hex[:4]}"
                    try:
                        client = Client(
                            session_name,
                            api_id=API_ID,
                            api_hash=API_HASH,
                            workdir=str(SESSIONS_DIR),
                            session_string=line
                        )
                        await client.start()
                        me = await client.get_me()
                        phone = me.phone_number or "unknown"
                        await client.stop()
                        
                        keep_names.add(session_name)
                        if add_user_session(user_id, session_name, phone):
                            results["added"] += 1
                            results["sessions"].append({"session_name": session_name, "phone": phone})
                        else:
                            results["errors"].append(f"Сессия {session_name} уже существует")
                    except Exception as e:
                        results["errors"].append(f"Ошибка строки: {e}")
                    continue
                
                # Телефон|пароль
                if '|' in line:
                    parts = line.split('|')
                    if len(parts) >= 2:
                        phone = normalize_phone(parts[0].strip())
                        password = parts[1].strip()
                        session_name = session_name_for_phone(phone)
                        try:
                            client = Client(
                                session_name,
                                api_id=API_ID,
                                api_hash=API_HASH,
                                workdir=str(SESSIONS_DIR)
                            )
                            await client.start()
                            await client.stop()
                            
                            keep_names.add(session_name)
                            if add_user_session(user_id, session_name, phone):
                                results["added"] += 1
                                results["sessions"].append({"session_name": session_name, "phone": phone})
                            else:
                                results["errors"].append(f"Сессия {session_name} уже существует")
                        except Exception as e:
                            results["errors"].append(f"{phone}: {e}")
                    continue
                
                # Просто номер телефона
                if re.match(r'^\+?\d{10,15}$', line):
                    phone = normalize_phone(line)
                    session_name = session_name_for_phone(phone)
                    session_path = SESSIONS_DIR / f"{session_name}.session"
                    
                    if session_path.exists():
                        if add_user_session(user_id, session_name, phone):
                            results["added"] += 1
                            results["sessions"].append({"session_name": session_name, "phone": phone})
                        else:
                            results["errors"].append(f"Сессия {session_name} уже существует")
                    else:
                        results["errors"].append(f"{session_name}.session не найден")
            
            # Удаляем старые файлы
            if keep_names:
                pruned = prune_stale_files(SESSIONS_DIR, keep_names)
                agent_log(run_id, "IM2", "process_uploaded_file", "Pruned stale files from txt", {"pruned": pruned})
        except Exception as e:
            results["errors"].append(f"Ошибка TXT: {e}")
        return results
    
    results["errors"].append("Неизвестный формат файла")
    return results

# ==================== ОТПРАВКА ЖАЛОБ ====================

async def send_report_from_session(session_name: str, link: str) -> bool:
    try:
        client = Client(session_name, api_id=API_ID, api_hash=API_HASH, workdir=str(SESSIONS_DIR))
        await client.start()
        await client.send_message("AUReportBot", "/start")
        await asyncio.sleep(1)
        await client.send_message("AUReportBot", link)
        await asyncio.sleep(1.5)
        await client.send_message("AUReportBot", "Drug-related material")
        await asyncio.sleep(1.5)
        await client.send_message("AUReportBot", "It's not illegal, but I want it removed")
        await asyncio.sleep(1)
        await client.stop()
        return True
    except Exception as e:
        logger.error(f"Ошибка {session_name}: {e}")
        return False

# ==================== КЛАВИАТУРЫ ====================

def get_main_keyboard(user_id: int = None):
    keyboard = [
        [InlineKeyboardButton("📱 По номеру (Pyrogram)", callback_data="add_by_phone")],
        [InlineKeyboardButton("🔑 По auth_key (Telethon)", callback_data="add_by_auth")],
        [InlineKeyboardButton("📁 Загрузить файл", callback_data="upload_file")],
        [InlineKeyboardButton("📋 Мои сессии", callback_data="my_sessions")],
        [InlineKeyboardButton("🚀 Атака", callback_data="start_attack")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📜 Логи", callback_data="logs")],
    ]
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("👤 Выдать подписку", callback_data="admin_give_sub")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("📜 Логи", callback_data="admin_logs")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main")]
    ])

def get_sub_keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 1 день", callback_data=f"sub_1_{user_id}"),
         InlineKeyboardButton("📅 3 дня", callback_data=f"sub_3_{user_id}")],
        [InlineKeyboardButton("📅 7 дней", callback_data=f"sub_7_{user_id}"),
         InlineKeyboardButton("📅 30 дней", callback_data=f"sub_30_{user_id}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]
    ])

def get_sessions_keyboard(user_id: int, page: int = 0):
    sessions = get_user_sessions(user_id)
    if not sessions:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 По номеру", callback_data="add_by_phone")],
            [InlineKeyboardButton("📁 Загрузить файл", callback_data="upload_file")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="main")]
        ])
    start_idx = page * 5
    end_idx = min(start_idx + 5, len(sessions))
    keyboard = []
    for i in range(start_idx, end_idx):
        s = sessions[i]
        keyboard.append([
            InlineKeyboardButton(
                f"📱 {s['phone']} ({s.get('reports_sent', 0)})",
                callback_data=f"select_session_{i}"
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"sessions_page_{page-1}"))
    if end_idx < len(sessions):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"sessions_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(keyboard)

def get_attack_keyboard(session_idx: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ Выбрать время", callback_data=f"attack_time_{session_idx}")],
        [InlineKeyboardButton("⚡ Начать сейчас", callback_data=f"attack_now_{session_idx}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="my_sessions")]
    ])

def get_time_keyboard(session_idx: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("08:00", callback_data=f"time_{session_idx}_0800"),
         InlineKeyboardButton("09:00", callback_data=f"time_{session_idx}_0900"),
         InlineKeyboardButton("10:00", callback_data=f"time_{session_idx}_1000")],
        [InlineKeyboardButton("12:00", callback_data=f"time_{session_idx}_1200"),
         InlineKeyboardButton("15:00", callback_data=f"time_{session_idx}_1500"),
         InlineKeyboardButton("18:00", callback_data=f"time_{session_idx}_1800")],
        [InlineKeyboardButton("20:00", callback_data=f"time_{session_idx}_2000"),
         InlineKeyboardButton("21:00", callback_data=f"time_{session_idx}_2100"),
         InlineKeyboardButton("22:00", callback_data=f"time_{session_idx}_2200")],
        [InlineKeyboardButton("🕐 Свое время", callback_data=f"custom_time_{session_idx}")],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"attack_back_{session_idx}")]
    ])

# ==================== ОБРАБОТЧИКИ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id, update.effective_user.username)
    if not has_subscription(user_id) and user_id != ADMIN_ID:
        await update.message.reply_text("🔐 Нет подписки! Обратитесь к @moonlight_ais")
        return
    await update.message.reply_text(
        f"🤖 **Главное меню**\n👤 {get_subscription_info(user_id)}\n📱 Сессий: {len(get_user_sessions(user_id))}",
        reply_markup=get_main_keyboard(user_id),
        parse_mode='Markdown'
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "main":
        await query.edit_message_text(
            "🤖 **Главное меню**",
            reply_markup=get_main_keyboard(user_id),
            parse_mode='Markdown'
        )
        return

    # ===== АДМИН-ПАНЕЛЬ =====
    if data == "admin_panel":
        if user_id != ADMIN_ID:
            await query.edit_message_text("❌ Доступ запрещен!")
            return
        await query.edit_message_text("👑 **Админ-панель**", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        return

    if data == "admin_users":
        if user_id != ADMIN_ID:
            return
        users = load_users()
        text = "📋 **Пользователи:**\n\n"
        for u in users["users"]:
            text += f"👤 {u['username']} ({u['user_id']})\n"
            text += f"   Подписка: {get_subscription_info(u['user_id'])}\n"
            text += f"   Сессий: {len(u.get('sessions', []))}\n"
            text += f"   Жалоб: {u.get('reports_sent', 0)}\n\n"
        await query.edit_message_text(text[:4000], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]), parse_mode='Markdown')
        return

    if data == "admin_give_sub":
        if user_id != ADMIN_ID:
            return
        await query.edit_message_text(
            "👤 **Выдача подписки**\n\nОтправь ID пользователя:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]),
            parse_mode='Markdown'
        )
        context.user_data['awaiting_sub_user'] = True
        return

    if data.startswith("sub_"):
        if user_id != ADMIN_ID:
            return
        parts = data.split("_")
        days = int(parts[1])
        target_user = int(parts[2])
        if add_subscription(target_user, days):
            await query.edit_message_text(f"✅ Подписка на {days} дней выдана!", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        else:
            await query.edit_message_text("❌ Пользователь не найден!", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        return

    if data == "admin_stats":
        if user_id != ADMIN_ID:
            return
        users = load_users()
        total = len(users["users"])
        total_reports = sum(u.get("reports_sent", 0) for u in users["users"])
        total_sessions = sum(len(u.get("sessions", [])) for u in users["users"])
        await query.edit_message_text(
            f"📊 **Статистика бота**\n\n"
            f"👥 Пользователей: {total}\n"
            f"📱 Сессий всего: {total_sessions}\n"
            f"📤 Жалоб всего: {total_reports}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]),
            parse_mode='Markdown'
        )
        return

    if data == "admin_logs":
        if user_id != ADMIN_ID:
            return
        logs = get_logs()
        await query.edit_message_text(
            f"📜 **Логи:**\n\n```\n{logs[:3000]}\n```",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel")]]),
            parse_mode='Markdown'
        )
        return

    # ===== ОСНОВНЫЕ ФУНКЦИИ =====
    if data == "add_by_phone":
        await query.edit_message_text("📱 Отправь номер: `+79991234567`", parse_mode='Markdown')
        context.user_data['step'] = 'waiting_phone'
        return

    if data == "add_by_auth":
        await query.edit_message_text(
            "🔑 Отправь auth_key в HEX:\n`a5043b32b455fa3f2421b7e52a7f...`",
            parse_mode='Markdown'
        )
        context.user_data['step'] = 'waiting_auth_key'
        return

    if data == "upload_file":
        await query.edit_message_text(
            "📁 **Отправь файл:**\n\n"
            "• `.session` — готовый файл сессии\n"
            "• `.zip` с `.session` файлами\n"
            "• `.zip` с `tdata` папкой\n"
            "• `.txt` с номерами / паролями / строками",
            parse_mode='Markdown'
        )
        context.user_data['waiting_for_file'] = True
        return

    if data == "my_sessions":
        await query.edit_message_text(
            "📋 **Мои сессии**",
            reply_markup=get_sessions_keyboard(user_id),
            parse_mode='Markdown'
        )
        return

    if data.startswith("sessions_page_"):
        page = int(data.split("_")[2])
        await query.edit_message_text(
            "📋 **Мои сессии**",
            reply_markup=get_sessions_keyboard(user_id, page),
            parse_mode='Markdown'
        )
        return

    if data.startswith("select_session_"):
        idx = int(data.split("_")[2])
        sessions = get_user_sessions(user_id)
        if idx >= len(sessions):
            return
        session = sessions[idx]
        context.user_data['selected_session_idx'] = idx
        await query.edit_message_text(
            f"📱 **Сессия:** {session['phone']}\n"
            f"📤 Жалоб: {session.get('reports_sent', 0)}\n"
            f"📅 Добавлена: {session['added_at'][:10]}\n\n"
            f"Выбери действие:",
            reply_markup=get_attack_keyboard(idx),
            parse_mode='Markdown'
        )
        return

    if data == "start_attack":
        sessions = get_user_sessions(user_id)
        if not sessions:
            await query.edit_message_text("❌ Нет сессий! Добавь сначала.", reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')
            return
        await query.edit_message_text(
            "📎 **Введите ссылку для атаки:**\n`t.me/username`",
            parse_mode='Markdown'
        )
        context.user_data['awaiting_attack_link'] = True
        return

    if data.startswith("attack_back_"):
        idx = int(data.split("_")[2])
        sessions = get_user_sessions(user_id)
        if idx >= len(sessions):
            return
        session = sessions[idx]
        await query.edit_message_text(
            f"📱 **Сессия:** {session['phone']}",
            reply_markup=get_attack_keyboard(idx),
            parse_mode='Markdown'
        )
        return

    if data.startswith("attack_time_"):
        idx = int(data.split("_")[2])
        await query.edit_message_text(
            "⏰ **Выбери время:**",
            reply_markup=get_time_keyboard(idx),
            parse_mode='Markdown'
        )
        return

    if data.startswith("attack_now_"):
        idx = int(data.split("_")[2])
        sessions = get_user_sessions(user_id)
        if idx >= len(sessions):
            return
        session = sessions[idx]
        
        link = context.user_data.get('attack_link')
        if not link:
            await query.edit_message_text("❌ Ссылка не найдена!", reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')
            return
        
        await query.edit_message_text(
            f"🚀 **Атака запущена!**\n"
            f"📱 Сессия: {session['phone']}\n"
            f"📎 Ссылка: {link}\n\n"
            f"⏳ Отправка...",
            parse_mode='Markdown'
        )
        
        result = await send_report_from_session(session['session_name'], link)
        if result:
            increment_report_count(user_id, session['session_name'])
            save_log(f"✅ {session['phone']} отправил жалобу на {link}")
            await query.edit_message_text(
                f"✅ **Готово!**\n"
                f"📱 Сессия: {session['phone']}\n"
                f"📎 Ссылка: {link}\n"
                f"📤 Статус: Успешно",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="my_sessions")]]),
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                f"❌ **Ошибка!**\n"
                f"📱 Сессия: {session['phone']}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="my_sessions")]]),
                parse_mode='Markdown'
            )
        return

    if data.startswith("time_"):
        parts = data.split("_")
        idx = int(parts[1])
        time_str = parts[2]
        hours = int(time_str[:2])
        minutes = int(time_str[2:])
        
        link = context.user_data.get('attack_link')
        if not link:
            await query.edit_message_text("❌ Ссылка не найдена!", reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')
            return
        
        await query.edit_message_text(
            f"⏰ **Атака запланирована!**\n"
            f"📎 Ссылка: {link}\n"
            f"⏰ Время: {hours:02d}:{minutes:02d}\n\n"
            f"Нажми 'Подтвердить' для запуска:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_time_{idx}_{hours:02d}{minutes:02d}")],
                [InlineKeyboardButton("❌ Отмена", callback_data=f"attack_back_{idx}")]
            ]),
            parse_mode='Markdown'
        )
        return

    if data.startswith("custom_time_"):
        idx = int(data.split("_")[2])
        await query.edit_message_text(
            "🕐 Введи время в формате: `08:15`",
            parse_mode='Markdown'
        )
        context.user_data['awaiting_custom_time'] = True
        context.user_data['custom_time_idx'] = idx
        return

    if data.startswith("confirm_time_"):
        parts = data.split("_")
        idx = int(parts[2])
        time_str = parts[3]
        hours = int(time_str[:2])
        minutes = int(time_str[2:])
        
        sessions = get_user_sessions(user_id)
        if idx >= len(sessions):
            return
        session = sessions[idx]
        link = context.user_data.get('attack_link')
        if not link:
            await query.edit_message_text("❌ Ссылка не найдена!", reply_markup=get_main_keyboard(user_id), parse_mode='Markdown')
            return
        
        await query.edit_message_text(
            f"⏳ **Ожидание до {hours:02d}:{minutes:02d}...**\n"
            f"📱 Сессия: {session['phone']}\n"
            f"📎 Ссылка: {link}",
            parse_mode='Markdown'
        )
        
        asyncio.create_task(schedule_attack(update, context, session['session_name'], session['phone'], link, hours, minutes))
        return

    if data == "stats":
        user = get_user(user_id)
        sessions = get_user_sessions(user_id)
        await query.edit_message_text(
            f"📊 **Твоя статистика**\n\n"
            f"👤 Пользователь: {user['username']}\n"
            f"📅 Статус: {get_subscription_info(user_id)}\n"
            f"📱 Сессий: {len(sessions)}\n"
            f"📤 Жалоб: {user.get('reports_sent', 0)}\n\n"
            f"📱 Сессии:\n" + "\n".join([f"• {s['phone']} ({s.get('reports_sent', 0)} жалоб)" for s in sessions[:5]]) if sessions else "Нет сессий",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main")]]),
            parse_mode='Markdown'
        )
        return

    if data == "logs":
        logs = get_logs()
        await query.edit_message_text(
            f"📜 **Последние логи:**\n\n```\n{logs[:3000]}\n```",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main")]]),
            parse_mode='Markdown'
        )
        return

# ==================== ПЛАНИРОВЩИК ====================

async def schedule_attack(update: Update, context: ContextTypes.DEFAULT_TYPE, session_name: str, phone: str, link: str, hours: int, minutes: int):
    now = datetime.now()
    target = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if target <= now:
        target = target.replace(day=now.day + 1)
    wait_seconds = (target - now).total_seconds()
    
    await asyncio.sleep(wait_seconds)
    
    user_id = update.effective_user.id
    result = await send_report_from_session(session_name, link)
    
    if result:
        increment_report_count(user_id, session_name)
        save_log(f"✅ {phone} отправил жалобу (по расписанию) на {link}")
        try:
            await update.effective_chat.send_message(
                f"✅ **Атака выполнена!**\n"
                f"📱 Сессия: {phone}\n"
                f"📎 Ссылка: {link}\n"
                f"⏰ По расписанию: {hours:02d}:{minutes:02d}"
            )
        except:
            pass

# ==================== ОБРАБОТКА СООБЩЕНИЙ ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message

    # Ожидание номера
    if context.user_data.get('step') == 'waiting_phone':
        await handle_phone_input(update, context)
        return

    # Ожидание кода
    if context.user_data.get('step') == 'waiting_code':
        await handle_code_input(update, context)
        return

    # Ожидание auth_key
    if context.user_data.get('step') == 'waiting_auth_key':
        await handle_auth_key(update, context)
        return

    # Ожидание ссылки для атаки
    if context.user_data.get('awaiting_attack_link'):
        if message.text and message.text.startswith(('t.me/', 'https://t.me/')):
            context.user_data['attack_link'] = message.text.strip()
            context.user_data['awaiting_attack_link'] = False
            sessions = get_user_sessions(user_id)
            await message.reply_text(
                f"📎 Ссылка сохранена: {message.text}\n\n"
                f"Выбери сессию для отправки:",
                reply_markup=get_sessions_keyboard(user_id)
            )
        else:
            await message.reply_text("❌ Неверный формат! Используй: t.me/username")
        return

    # Ожидание кастомного времени
    if context.user_data.get('awaiting_custom_time'):
        time_str = message.text.strip()
        try:
            if ':' in time_str:
                hours, minutes = map(int, time_str.split(':'))
                if 0 <= hours <= 23 and 0 <= minutes <= 59:
                    idx = context.user_data.get('custom_time_idx')
                    link = context.user_data.get('attack_link')
                    if link:
                        await message.reply_text(
                            f"⏰ Время установлено: {hours:02d}:{minutes:02d}\n"
                            f"Нажми 'Подтвердить':",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_time_{idx}_{hours:02d}{minutes:02d}")],
                                [InlineKeyboardButton("❌ Отмена", callback_data=f"attack_back_{idx}")]
                            ])
                        )
                        context.user_data['awaiting_custom_time'] = False
                    else:
                        await message.reply_text("❌ Ссылка не найдена!")
                else:
                    await message.reply_text("❌ Неверное время! Используй: 08:15")
            else:
                await message.reply_text("❌ Неверный формат! Используй: 08:15")
        except:
            await message.reply_text("❌ Неверный формат! Используй: 08:15")
        return

    # Ожидание ID для подписки (админ)
    if context.user_data.get('awaiting_sub_user'):
        if user_id != ADMIN_ID:
            return
        try:
            target_id = int(message.text.strip())
            user = get_user(target_id)
            if user:
                await message.reply_text(
                    f"👤 Пользователь: {user['username']}\n"
                    f"📅 Статус: {get_subscription_info(target_id)}\n\n"
                    f"Выбери срок:",
                    reply_markup=get_sub_keyboard(target_id)
                )
                context.user_data['awaiting_sub_user'] = False
            else:
                await message.reply_text("❌ Пользователь не найден!")
        except:
            await message.reply_text("❌ Введи числовой ID!")
        return

    # Загрузка файла
    if context.user_data.get('waiting_for_file') and message.document:
        file = message.document
        if file.file_name.endswith(('.zip', '.txt', '.session')):
            await message.reply_text("⏳ Обработка файла...")
            file_obj = await file.get_file()
            file_content = await file_obj.download_as_bytearray()
            result = await process_uploaded_file(file_content, file.file_name, user_id)
            
            if result["added"] > 0:
                text = f"✅ **Добавлено:** {result['added']} сессий\n"
                if result["sessions"]:
                    text += "\n📱 Сессии:\n" + "\n".join([f"• {s['phone']}" for s in result["sessions"]])
                if result["errors"]:
                    text += f"\n\n❌ Ошибок: {len(result['errors'])}\n" + "\n".join(result["errors"][:3])
                await message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main")]]), parse_mode='Markdown')
            else:
                await message.reply_text(
                    f"❌ **Не добавлено!**\n" + "\n".join(result["errors"][:5]),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main")]])
                )
            context.user_data['waiting_for_file'] = False
        else:
            await message.reply_text("❌ Неподдерживаемый формат! Используй: .session, .zip, .txt")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("⏹ Бот остановлен. /start для входа")

# ==================== ЗАПУСК ====================

def main():
    print("=" * 50)
    print("🤖 БОТ ЗАПУЩЕН!")
    print(f"🔐 API ID: {API_ID}")
    print(f"👑 Админ: {ADMIN_ID}")
    print(f"📁 Сессии: {SESSIONS_DIR}")
    print("=" * 50)
    
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("start_auth", cmd_start_auth))
    app.add_handler(CommandHandler("cancel_auth", cancel_auth))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()