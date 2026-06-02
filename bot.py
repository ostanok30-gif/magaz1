#!/usr/bin/env python3
"""
OxatovAccount API Server v3.0 - PRODUCTION
Полная версия с Telegram Stars, LolzTeam API, логированием и мониторингом
"""

import asyncio
import json
import random
import sqlite3
import re
import logging
import os
import uuid
import traceback
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from functools import wraps

from aiohttp import web, ClientSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from telegram.error import TelegramError

# ==================== ЛОГИРОВАНИЕ ====================
class ProductionLogger:
    def __init__(self):
        self.logger = logging.getLogger('oxatov_api')
        self.logger.setLevel(logging.INFO)
        
        # File handler
        if not os.path.exists('logs'):
            os.makedirs('logs')
        
        fh = logging.FileHandler(f'logs/{datetime.now().strftime("%Y-%m-%d")}.log')
        fh.setLevel(logging.INFO)
        
        # Console handler
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        fh.setFormatter(formatter)
        ch.setFormatter(formatter)
        
        self.logger.addHandler(fh)
        self.logger.addHandler(ch)
    
    def info(self, msg: str, **kwargs):
        self.logger.info(msg, extra=kwargs)
    
    def error(self, msg: str, **kwargs):
        self.logger.error(msg, extra=kwargs)
    
    def warning(self, msg: str, **kwargs):
        self.logger.warning(msg, extra=kwargs)
    
    def debug(self, msg: str, **kwargs):
        self.logger.debug(msg, extra=kwargs)

logger = ProductionLogger()

# ==================== КОНФИГ ====================
class Config:
    # Основные параметры
    PORT = int(os.getenv('PORT', 3000))
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
    WEBHOOK_URL = os.getenv('WEBHOOK_URL')
    
    # Timeout для кодов
    CODE_TIMEOUT_MINUTES = 3
    CODE_ATTEMPTS_MAX = 5
    
    # LolzTeam API
    LOLZTEAM_TOKEN = os.getenv('LOLZTEAM_TOKEN')
    LOLZTEAM_API_URL = "https://api.zelenka.guru/v1"
    LOLZTEAM_SYNC_INTERVAL = 3600  # Синхронизация каждый час
    
    # Telegram Stars (встроенная система платежей)
    USE_STARS_PAYMENT = True
    
    # Валидация
    MIN_BALANCE_FOR_PURCHASE = 50
    MAX_DEPOSIT_STARS = 10000
    MIN_DEPOSIT_STARS = 10
    
    @staticmethod
    def validate():
        """Проверка обязательных переменных окружения"""
        required = ['BOT_TOKEN', 'ADMIN_ID']
        missing = [k for k in required if not os.getenv(k)]
        
        if missing:
            logger.error(f"❌ Отсутствуют переменные окружения: {', '.join(missing)}")
            raise ValueError(f"Missing env vars: {missing}")
        
        logger.info("✅ Конфиг загружен успешно")
        return True

# ==================== БАЗА ДАННЫХ ====================
class Database:
    DB_FILE = 'shop.db'
    
    @staticmethod
    def get_db():
        conn = sqlite3.connect(Database.DB_FILE)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn
    
    @staticmethod
    def init_db():
        """Инициализация базы данных с полной схемой"""
        with Database.get_db() as conn:
            cursor = conn.cursor()
            
            # Пользователи
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    balance INTEGER DEFAULT 0,
                    total_spent INTEGER DEFAULT 0,
                    total_deposited INTEGER DEFAULT 0,
                    orders_count INTEGER DEFAULT 0,
                    created_at TEXT,
                    last_activity TEXT,
                    is_blocked INTEGER DEFAULT 0
                )
            ''')
            
            # Аккаунты
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    country TEXT NOT NULL,
                    phone TEXT NOT NULL UNIQUE,
                    dc TEXT DEFAULT 'DC1',
                    price INTEGER NOT NULL,
                    status TEXT DEFAULT 'available',
                    lolz_id INTEGER,
                    lolz_synced_at TEXT,
                    created_at TEXT,
                    sold_at TEXT
                )
            ''')
            
            # Заказы
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    country TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    status TEXT DEFAULT 'completed',
                    payment_method TEXT,
                    created_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Коды подтверждения
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS active_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    phone TEXT NOT NULL,
                    code TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    expires_at TEXT,
                    created_at TEXT,
                    FOREIGN KEY (order_id) REFERENCES orders(order_id),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Платежи Stars
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS star_payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    telegram_payment_id TEXT UNIQUE,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Новости
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS news (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    icon TEXT DEFAULT '📢',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
            ''')
            
            # Логи действий
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT
                )
            ''')
            
            conn.commit()
        logger.info("✅ База данных инициализирована")

# ==================== LolzTeam API ИНТЕГРАЦИЯ ====================
class LolzTeamAPI:
    def __init__(self, token: str, api_url: str):
        self.token = token
        self.api_url = api_url
        self.session: Optional[ClientSession] = None
    
    async def init_session(self):
        if not self.session:
            self.session = ClientSession()
    
    async def close_session(self):
        if self.session:
            await self.session.close()
    
    def _get_headers(self):
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json'
        }
    
    async def get_accounts(self, country: str = None, limit: int = 10):
        """Получить список аккаунтов с Lolzteam"""
        try:
            await self.init_session()
            
            params = {'limit': limit}
            if country:
                params['category'] = f'telegram_{country.lower()}'
            
            async with self.session.get(
                f'{self.api_url}/lots/user',
                headers=self._get_headers(),
                params=params,
                timeout=10
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"✅ Получены аккаунты с LolzTeam: {len(data.get('data', []))} штук")
                    return data.get('data', [])
                else:
                    logger.warning(f"⚠️ LolzTeam API вернул статус {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"❌ Ошибка при запросе к LolzTeam: {str(e)}")
            return []
    
    async def sync_with_db(self):
        """Синхронизировать аккаунты с базой данных"""
        try:
            lolz_accounts = await self.get_accounts(limit=50)
            
            if not lolz_accounts:
                logger.warning("⚠️ LolzTeam аккаунты не получены")
                return
            
            with Database.get_db() as conn:
                cursor = conn.cursor()
                
                for acc in lolz_accounts:
                    try:
                        # Извлекаем данные (зависит от формата LolzTeam)
                        lolz_id = acc.get('id')
                        phone = acc.get('login', '')  # может быть в другом поле
                        country = acc.get('category', 'RU').split('_')[-1].upper()
                        
                        # Проверяем наличие в БД
                        cursor.execute(
                            "SELECT id FROM accounts WHERE lolz_id = ?",
                            (lolz_id,)
                        )
                        
                        if not cursor.fetchone():
                            # Добавляем новый аккаунт
                            cursor.execute('''
                                INSERT INTO accounts 
                                (country, phone, price, lolz_id, lolz_synced_at, created_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                            ''', (country, phone, 200, lolz_id, datetime.now().isoformat(), datetime.now().isoformat()))
                    except Exception as e:
                        logger.warning(f"⚠️ Ошибка при синхронизации аккаунта: {str(e)}")
                
                conn.commit()
                logger.info("✅ Синхронизация с LolzTeam завершена")
        except Exception as e:
            logger.error(f"❌ Критическая ошибка синхронизации: {str(e)}")

# ==================== УПРАВЛЕНИЕ ПЛАТЕЖАМИ ====================
class PaymentManager:
    @staticmethod
    async def create_star_invoice(user_id: int, amount: int) -> Dict:
        """Создать инвойс для оплаты Stars"""
        try:
            if amount < Config.MIN_DEPOSIT_STARS or amount > Config.MAX_DEPOSIT_STARS:
                return {'success': False, 'error': 'invalid_amount'}
            
            payment_id = str(uuid.uuid4())[:8].upper()
            
            with Database.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO star_payments (user_id, amount, telegram_payment_id, status, created_at)
                    VALUES (?, ?, ?, 'pending', ?)
                ''', (user_id, amount, payment_id, datetime.now().isoformat()))
                conn.commit()
            
            logger.info(f"✅ Создан инвойс Stars для пользователя {user_id}: {amount}★")
            return {'success': True, 'payment_id': payment_id}
        except Exception as e:
            logger.error(f"❌ Ошибка создания инвойса: {str(e)}")
            return {'success': False, 'error': str(e)}
    
    @staticmethod
    async def process_star_payment(user_id: int, amount: int) -> bool:
        """Обработать платеж Stars"""
        try:
            with Database.get_db() as conn:
                cursor = conn.cursor()
                
                # Добавляем баланс
                cursor.execute(
                    "UPDATE users SET balance = balance + ?, total_deposited = total_deposited + ? WHERE user_id = ?",
                    (amount, amount, user_id)
                )
                
                # Обновляем статус платежа
                cursor.execute(
                    "UPDATE star_payments SET status = 'completed', completed_at = ? WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
                    (datetime.now().isoformat(), user_id)
                )
                
                conn.commit()
            
            logger.info(f"✅ Платеж Stars обработан для {user_id}: +{amount}★")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка обработки платежа: {str(e)}")
            return False

# ==================== ВАЛИДАЦИЯ ====================
class Validator:
    @staticmethod
    def validate_phone(phone: str) -> bool:
        """Проверить валидность номера телефона"""
        # +XXXXXXXXXXX или XXXXXXXXXXX (минимум 10 цифр)
        phone_pattern = r'^\+?[\d]{10,15}$'
        return bool(re.match(phone_pattern, phone.replace(' ', '').replace('-', '')))
    
    @staticmethod
    def validate_country_code(code: str) -> bool:
        """Проверить код страны"""
        valid_codes = ['US', 'RU', 'BY', 'KZ', 'UA', 'DE', 'GB', 'EU', 'TR', 'FR', 'PL', 'NL']
        return code.upper() in valid_codes
    
    @staticmethod
    def validate_price(price: int) -> bool:
        """Проверить цену"""
        return isinstance(price, int) and 50 <= price <= 10000
    
    @staticmethod
    def validate_amount(amount: int) -> bool:
        """Проверить сумму платежа"""
        return isinstance(amount, int) and Config.MIN_DEPOSIT_STARS <= amount <= Config.MAX_DEPOSIT_STARS

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def log_action(user_id: int = None, action: str = "", details: str = ""):
    """Логировать действия пользователей"""
    try:
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO action_logs (user_id, action, details, created_at) VALUES (?, ?, ?, ?)",
                (user_id, action, details, datetime.now().isoformat())
            )
            conn.commit()
    except:
        pass

def generate_order_id() -> str:
    return f"ORD-{uuid.uuid4().hex[:8].upper()}"

def generate_code() -> str:
    return str(random.randint(100000, 999999))

def is_admin(user_id: int) -> bool:
    return user_id == Config.ADMIN_ID

def require_admin(func):
    """Декоратор для проверки админ-статуса"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Доступ запрещён")
            return
        return await func(update, context)
    return wrapper

# ==================== АДМИН-КЛАВИАТУРЫ ====================
def get_admin_keyboard():
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 Управление новостями", callback_data="admin_news")],
        [InlineKeyboardButton("📱 Управление аккаунтами", callback_data="admin_accounts")],
        [InlineKeyboardButton("💰 Управление балансами", callback_data="admin_balance")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔄 Синхронизация LolzTeam", callback_data="admin_sync")],
        [InlineKeyboardButton("🔐 Системные логи", callback_data="admin_logs")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="admin_close")]
    ])
    return keyboard

# ==================== СОСТОЯНИЯ РАЗГОВОРА ====================
WAITING_NEWS_TITLE = 1
WAITING_NEWS_CONTENT = 2
WAITING_NEWS_ICON = 3
WAITING_ACCOUNT_COUNTRY = 10
WAITING_ACCOUNT_PHONE = 11
WAITING_ACCOUNT_DC = 12
WAITING_ACCOUNT_PRICE = 13
WAITING_USER_ID = 20
WAITING_BALANCE_AMOUNT = 21
WAITING_DELETE_NEWS_ID = 30
WAITING_DELETE_ACCOUNT_ID = 31

# ==================== CALLBACK HANDLERS ====================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    
    await update.message.reply_text(
        "🛠️ **Админ-панель OxatovAccount**\n\n"
        "Выберите действие:",
        reply_markup=get_admin_keyboard(),
        parse_mode="Markdown"
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    if not is_admin(user_id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    action = query.data
    
    try:
        if action == "admin_close":
            await query.message.delete()
            return
        
        elif action == "admin_news":
            news_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить", callback_data="news_add")],
                [InlineKeyboardButton("📝 Список", callback_data="news_list")],
                [InlineKeyboardButton("❌ Удалить", callback_data="news_delete")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin_news_back")]
            ])
            await query.message.edit_text("📰 Управление новостями", reply_markup=news_keyboard)
        
        elif action == "admin_accounts":
            accounts_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить", callback_data="account_add")],
                [InlineKeyboardButton("📋 Список", callback_data="account_list")],
                [InlineKeyboardButton("❌ Удалить", callback_data="account_delete")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin_accounts_back")]
            ])
            await query.message.edit_text("📱 Управление аккаунтами", reply_markup=accounts_keyboard)
        
        elif action == "admin_balance":
            balance_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Пополнить", callback_data="balance_add")],
                [InlineKeyboardButton("➖ Снять", callback_data="balance_remove")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="admin_balance_back")]
            ])
            await query.message.edit_text("💰 Управление балансами", reply_markup=balance_keyboard)
        
        elif action == "admin_stats":
            with Database.get_db() as conn:
                cursor = conn.cursor()
                users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                orders = cursor.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
                accounts = cursor.execute("SELECT COUNT(*) FROM accounts WHERE status='available'").fetchone()[0]
                sold = cursor.execute("SELECT COUNT(*) FROM accounts WHERE status='sold'").fetchone()[0]
                total_revenue = cursor.execute("SELECT SUM(price) FROM orders").fetchone()[0] or 0
                total_deposited = cursor.execute("SELECT SUM(total_deposited) FROM users").fetchone()[0] or 0
            
            stats_text = (
                f"📊 **Статистика OxatovAccount**\n\n"
                f"👥 Пользователей: {users}\n"
                f"📦 Заказов: {orders}\n"
                f"📱 Аккаунтов в наличии: {accounts}\n"
                f"✅ Продано: {sold}\n"
                f"💰 Выручка: {total_revenue}★\n"
                f"💳 Пополнено Stars: {total_deposited}★"
            )
            
            await query.message.edit_text(stats_text, reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel_back")
            ]]), parse_mode="Markdown")
        
        elif action == "admin_sync":
            await query.message.edit_text("⏳ Синхронизирую с LolzTeam...")
            lolz = LolzTeamAPI(Config.LOLZTEAM_TOKEN, Config.LOLZTEAM_API_URL)
            await lolz.sync_with_db()
            await lolz.close_session()
            await query.message.edit_text(
                "✅ Синхронизация завершена",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="admin_panel_back")]])
            )
        
        elif action in ["admin_news_back", "admin_accounts_back", "admin_balance_back", "admin_panel_back"]:
            await query.message.edit_text(
                "🛠️ **Админ-панель OxatovAccount**\n\nВыберите действие:",
                reply_markup=get_admin_keyboard(),
                parse_mode="Markdown"
            )
        
        elif action == "news_add":
            await query.message.edit_text("📰 Введите **иконку** (например 🆕, ⭐):", parse_mode="Markdown")
            context.user_data['admin_action'] = 'news_add'
            return WAITING_NEWS_ICON
        
        elif action == "account_add":
            await query.message.edit_text(
                "📱 Введите **страну** (US, RU, BY, KZ, UA, DE, GB, TR, FR, PL, NL):",
                parse_mode="Markdown"
            )
            context.user_data['admin_action'] = 'account_add'
            return WAITING_ACCOUNT_COUNTRY
        
        elif action == "balance_add":
            await query.message.edit_text("💰 Введите **ID пользователя** для пополнения:", parse_mode="Markdown")
            context.user_data['admin_action'] = 'add_balance'
            return WAITING_USER_ID
        
        await query.answer()
    except Exception as e:
        logger.error(f"❌ Ошибка в admin_callback: {str(e)}")
        await query.answer("Ошибка обработки команды", show_alert=True)

# ==================== ВЕБ API ====================
async def handle_balance(request: web.Request) -> web.Response:
    """GET /api/balance?user_id=123"""
    try:
        user_id = int(request.query.get('user_id', 0))
        if not user_id:
            return web.json_response({'error': 'user_id required'}, status=400)
        
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            
            if not row:
                # Создаём новго пользователя
                cursor.execute(
                    "INSERT INTO users (user_id, balance, created_at) VALUES (?, ?, ?)",
                    (user_id, 0, datetime.now().isoformat())
                )
                conn.commit()
                balance = 0
            else:
                balance = row['balance']
        
        return web.json_response({'success': True, 'balance': balance})
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_balance: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_catalog(request: web.Request) -> web.Response:
    """GET /api/catalog"""
    try:
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, country, phone, price, dc, status 
                FROM accounts 
                WHERE status = 'available'
                ORDER BY country, price
            ''')
            accounts = cursor.fetchall()
        
        items = []
        for acc in accounts:
            items.append({
                'id': acc['id'],
                'country': acc['country'],
                'phone': acc['phone'],
                'price': acc['price'],
                'dc': acc['dc'],
                'status': acc['status']
            })
        
        return web.json_response({'success': True, 'items': items})
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_catalog: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_orders(request: web.Request) -> web.Response:
    """GET /api/orders?user_id=123"""
    try:
        user_id = int(request.query.get('user_id', 0))
        if not user_id:
            return web.json_response({'error': 'user_id required'}, status=400)
        
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT order_id, phone, country, price, status, created_at
                FROM orders
                WHERE user_id = ?
                ORDER BY created_at DESC
            ''', (user_id,))
            orders = cursor.fetchall()
        
        orders_list = []
        for o in orders:
            orders_list.append({
                'order_id': o['order_id'],
                'phone': o['phone'],
                'country': o['country'],
                'amount': o['price'],
                'status': o['status'],
                'created_at': o['created_at']
            })
        
        return web.json_response({'success': True, 'orders': orders_list})
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_orders: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_codes(request: web.Request) -> web.Response:
    """GET /api/codes?user_id=123"""
    try:
        user_id = int(request.query.get('user_id', 0))
        if not user_id:
            return web.json_response({'error': 'user_id required'}, status=400)
        
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT order_id, code, expires_at
                FROM active_codes
                WHERE user_id = ? AND expires_at > ?
                ORDER BY created_at DESC
            ''', (user_id, datetime.now().isoformat()))
            codes = cursor.fetchall()
        
        codes_list = []
        for c in codes:
            codes_list.append({
                'order_id': c['order_id'],
                'code': c['code'],
                'expires': c['expires_at']
            })
        
        return web.json_response({'success': True, 'codes': codes_list})
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_codes: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_news(request: web.Request) -> web.Response:
    """GET /api/news"""
    try:
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT icon, title, content, created_at
                FROM news
                WHERE is_active = 1
                ORDER BY created_at DESC
            ''')
            news = cursor.fetchall()
        
        news_list = []
        for n in news:
            news_list.append({
                'icon': n['icon'],
                'title': n['title'],
                'content': n['content'],
                'date': datetime.fromisoformat(n['created_at']).strftime('%d.%m.%Y')
            })
        
        return web.json_response({'success': True, 'news': news_list})
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_news: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_buy(request: web.Request) -> web.Response:
    """POST /api/buy"""
    try:
        data = await request.json()
        user_id = data.get('user_id')
        account_id = data.get('account_id')
        
        if not all([user_id, account_id]):
            return web.json_response({'error': 'missing_params'}, status=400)
        
        with Database.get_db() as conn:
            cursor = conn.cursor()
            
            # Получаем баланс пользователя
            cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
            user = cursor.fetchone()
            
            if not user:
                return web.json_response({'error': 'user_not_found'}, status=404)
            
            # Получаем аккаунт
            cursor.execute("SELECT * FROM accounts WHERE id = ? AND status = 'available'", (account_id,))
            account = cursor.fetchone()
            
            if not account:
                return web.json_response({'error': 'sold_out'}, status=409)
            
            # Проверяем баланс
            if user['balance'] < account['price']:
                return web.json_response({'error': 'insufficient_balance'}, status=400)
            
            # Создаём заказ
            order_id = generate_order_id()
            cursor.execute('''
                INSERT INTO orders (order_id, user_id, account_id, phone, country, price, status, payment_method, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, 'completed', 'balance', ?, ?)
            ''', (order_id, user_id, account_id, account['phone'], account['country'], account['price'], 
                  datetime.now().isoformat(), datetime.now().isoformat()))
            
            # Списываем баланс
            cursor.execute(
                "UPDATE users SET balance = balance - ?, total_spent = total_spent + ?, orders_count = orders_count + 1 WHERE user_id = ?",
                (account['price'], account['price'], user_id)
            )
            
            # Помечаем аккаунт как проданный
            cursor.execute(
                "UPDATE accounts SET status = 'sold', sold_at = ? WHERE id = ?",
                (datetime.now().isoformat(), account_id)
            )
            
            # Генерируем код
            code = generate_code()
            expires_at = datetime.now() + timedelta(minutes=Config.CODE_TIMEOUT_MINUTES)
            cursor.execute('''
                INSERT INTO active_codes (order_id, user_id, phone, code, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (order_id, user_id, account['phone'], code, expires_at.isoformat(), datetime.now().isoformat()))
            
            conn.commit()
            
            log_action(user_id, 'buy_account', f'Account {account_id}, code {code}')
            logger.info(f"✅ Заказ {order_id} создан для пользователя {user_id}")
            
            return web.json_response({
                'success': True,
                'order_id': order_id,
                'code': code
            })
    
    except json.JSONDecodeError:
        return web.json_response({'error': 'invalid_json'}, status=400)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_buy: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_refresh(request: web.Request) -> web.Response:
    """POST /api/refresh - Получить новый код"""
    try:
        data = await request.json()
        user_id = data.get('user_id')
        order_id = data.get('order_id')
        
        if not all([user_id, order_id]):
            return web.json_response({'error': 'missing_params'}, status=400)
        
        with Database.get_db() as conn:
            cursor = conn.cursor()
            
            # Проверяем наличие заказа
            cursor.execute("SELECT * FROM orders WHERE order_id = ? AND user_id = ?", (order_id, user_id))
            order = cursor.fetchone()
            
            if not order:
                return web.json_response({'error': 'order_not_found'}, status=404)
            
            # Проверяем количество попыток
            cursor.execute("SELECT attempts FROM active_codes WHERE order_id = ? ORDER BY created_at DESC LIMIT 1", (order_id,))
            code_row = cursor.fetchone()
            
            if code_row and code_row['attempts'] >= Config.CODE_ATTEMPTS_MAX:
                return web.json_response({'error': 'max_attempts_reached'}, status=429)
            
            # Генерируем новый код
            code = generate_code()
            expires_at = datetime.now() + timedelta(minutes=Config.CODE_TIMEOUT_MINUTES)
            
            cursor.execute('''
                INSERT INTO active_codes (order_id, user_id, phone, code, expires_at, created_at, attempts)
                VALUES (?, ?, ?, ?, ?, ?, 1)
            ''', (order_id, user_id, order['phone'], code, expires_at.isoformat(), datetime.now().isoformat()))
            
            conn.commit()
            
            log_action(user_id, 'refresh_code', f'Order {order_id}')
            logger.info(f"✅ Новый код для заказа {order_id}: {code}")
            
            return web.json_response({
                'success': True,
                'code': code,
                'expires_at': expires_at.isoformat()
            })
    
    except json.JSONDecodeError:
        return web.json_response({'error': 'invalid_json'}, status=400)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_refresh: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def handle_deposit(request: web.Request) -> web.Response:
    """POST /api/deposit - Создать инвойс для пополнения"""
    try:
        data = await request.json()
        user_id = data.get('user_id')
        amount = data.get('amount')
        
        if not all([user_id, amount]):
            return web.json_response({'error': 'missing_params'}, status=400)
        
        if not Validator.validate_amount(amount):
            return web.json_response({
                'error': 'invalid_amount',
                'min': Config.MIN_DEPOSIT_STARS,
                'max': Config.MAX_DEPOSIT_STARS
            }, status=400)
        
        result = await PaymentManager.create_star_invoice(user_id, amount)
        
        if result['success']:
            log_action(user_id, 'create_invoice', f'Amount: {amount}')
            return web.json_response(result)
        else:
            return web.json_response(result, status=400)
    
    except json.JSONDecodeError:
        return web.json_response({'error': 'invalid_json'}, status=400)
    except Exception as e:
        logger.error(f"❌ Ошибка в handle_deposit: {str(e)}")
        return web.json_response({'error': str(e)}, status=500)

async def webapp_handler(request):
    """Отдаём HTML приложение"""
    if os.path.exists('bot.html'):
        return web.FileResponse('bot.html')
    return web.Response(text="Ошибка: файл bot.html не найден", status=404)

# ==================== ВЕБ-СЕРВЕР ====================
async def run_webapp():
    """Запуск веб-сервера"""
    app = web.Application()
    
    # Маршруты
    app.router.add_get('/', webapp_handler)
    app.router.add_get('/app', webapp_handler)
    app.router.add_get('/api/balance', handle_balance)
    app.router.add_get('/api/catalog', handle_catalog)
    app.router.add_get('/api/orders', handle_orders)
    app.router.add_get('/api/codes', handle_codes)
    app.router.add_get('/api/news', handle_news)
    app.router.add_post('/api/buy', handle_buy)
    app.router.add_post('/api/refresh', handle_refresh)
    app.router.add_post('/api/deposit', handle_deposit)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', Config.PORT)
    await site.start()
    logger.info(f"✅ WebApp сервер запущен на порту {Config.PORT}")

# ==================== TELEGRAM БОТ ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    
    try:
        with Database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
            
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO users (user_id, username, first_name, balance, created_at) VALUES (?, ?, ?, ?, ?)",
                    (user.id, user.username, user.first_name, 0, datetime.now().isoformat())
                )
                conn.commit()
                logger.info(f"✅ Новый пользователь: {user.id} (@{user.username})")
        
        webapp_url = os.getenv('WEBAPP_URL', 'https://oxatovshop.onrender.com/app')
        
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 Открыть магазин", web_app=WebAppInfo(url=webapp_url))
        ]])
        
        await update.message.reply_text(
            f"✨ **Добро пожаловать в OxatovAccount, {user.first_name}!**\n\n"
            f"📱 У нас есть Telegram аккаунты из разных стран\n"
            f"🔐 После покупки ты получишь код подтверждения\n"
            f"⭐ Пополни баланс через Telegram Stars\n\n"
            f"Нажми на кнопку ниже, чтобы открыть магазин:",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        
        log_action(user.id, 'start', '')
    except Exception as e:
        logger.error(f"❌ Ошибка в start_command: {str(e)}")

@require_admin
async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stats"""
    try:
        with Database.get_db() as conn:
            cursor = conn.cursor()
            users = cursor.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            orders = cursor.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            accounts = cursor.execute("SELECT COUNT(*) FROM accounts WHERE status='available'").fetchone()[0]
            sold = cursor.execute("SELECT COUNT(*) FROM accounts WHERE status='sold'").fetchone()[0]
            total_revenue = cursor.execute("SELECT SUM(price) FROM orders").fetchone()[0] or 0
        
        await update.message.reply_text(
            f"📊 **Статистика OxatovAccount**\n\n"
            f"👥 Пользователей: {users}\n"
            f"📦 Заказов: {orders}\n"
            f"📱 Аккаунтов в наличии: {accounts}\n"
            f"✅ Продано: {sold}\n"
            f"💰 Выручка: {total_revenue}★",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ Ошибка в admin_stats_command: {str(e)}")

async def run_bot():
    """Запуск Telegram бота"""
    application = Application.builder().token(Config.BOT_TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", admin_stats_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^(admin_|news_|account_|balance_)"))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("✅ Telegram бот запущен")
    
    while True:
        await asyncio.sleep(1)

# ==================== ФОНОВЫЕ ЗАДАЧИ ====================
async def background_tasks(lolz: LolzTeamAPI):
    """Фоновые задачи: синхронизация, очистка кодов"""
    while True:
        try:
            # Синхронизация с LolzTeam каждый час
            await lolz.sync_with_db()
            
            # Очистка просроченных кодов
            with Database.get_db() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM active_codes WHERE expires_at < ?",
                    (datetime.now().isoformat(),)
                )
                conn.commit()
            
            logger.info("✅ Фоновые задачи выполнены")
        except Exception as e:
            logger.error(f"❌ Ошибка в background_tasks: {str(e)}")
        
        await asyncio.sleep(Config.LOLZTEAM_SYNC_INTERVAL)

# ==================== MAIN ====================
async def main():
    """Главная функция"""
    try:
        # Инициализация
        Config.validate()
        Database.init_db()
        
        # LolzTeam API
        lolz = LolzTeamAPI(Config.LOLZTEAM_TOKEN, Config.LOLZTEAM_API_URL)
        
        # Запуск всех сервисов
        await asyncio.gather(
            run_webapp(),
            run_bot(),
            background_tasks(lolz)
        )
    except Exception as e:
        logger.error(f"💥 Критическая ошибка: {str(e)}")
        traceback.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹️ Сервер остановлен")
    except Exception as e:
        logger.error(f"💥 Критическая ошибка при запуске: {str(e)}")
        traceback.print_exc()