import asyncio
import csv
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ChatType, ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "8753914631").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///bot_data.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in environment variables.")

try:
    OWNER_ID = int(OWNER_ID_RAW)
except (TypeError, ValueError):
    raise RuntimeError("OWNER_ID must be a valid Telegram numeric user ID.")

if DATABASE_URL.startswith("sqlite:///"):
    DB_PATH = Path(DATABASE_URL.replace("sqlite:///", "", 1))
else:
    # This single-file build uses SQLite.
    # PostgreSQL migration can be done later without changing the
    # application-level tables/services.
    DB_PATH = Path("bot_data.db")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

BACKUP_DIR = DB_PATH.parent / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
BROADCAST_BATCH_SIZE = 25
BROADCAST_DELAY = 0.05
MAX_RETRIES = 3

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("join_request_bot")


# ============================================================
# DATABASE
# ============================================================

class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        if self.conn:
            return

        self.conn = sqlite3.connect(
            str(self.path),
            timeout=30,
            check_same_thread=False,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.create_schema()
        self.seed_defaults()

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                logger.exception("Database close failed")
            finally:
                self.conn = None

    def execute(self, query: str, params=(), commit=False):
        self.connect()
        cur = self.conn.execute(query, params)
        if commit:
            self.conn.commit()
        return cur

    def executemany(self, query: str, rows, commit=False):
        self.connect()
        cur = self.conn.executemany(query, rows)
        if commit:
            self.conn.commit()
        return cur

    def fetchone(self, query: str, params=()):
        cur = self.execute(query, params)
        return cur.fetchone()

    def fetchall(self, query: str, params=()):
        cur = self.execute(query, params)
        return cur.fetchall()

    def create_schema(self):
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            language_code TEXT,
            is_bot INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'admin',
            permissions TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS channels (
            channel_id INTEGER PRIMARY KEY,
            username TEXT,
            title TEXT,
            type TEXT,
            enabled INTEGER DEFAULT 1,
            required INTEGER DEFAULT 1,
            sort_order INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            media_type TEXT DEFAULT 'none',
            file_id TEXT,
            caption TEXT DEFAULT '',
            parse_mode TEXT DEFAULT 'HTML',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS message_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            url TEXT NOT NULL,
            row_number INTEGER DEFAULT 0,
            position INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS join_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            requested_at TEXT NOT NULL,
            message_sent INTEGER DEFAULT 0,
            message_sent_at TEXT,
            error TEXT,
            status TEXT DEFAULT 'received',
            event_key TEXT UNIQUE,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_join_requests_user
        ON join_requests(user_id);

        CREATE INDEX IF NOT EXISTS idx_join_requests_channel
        ON join_requests(channel_id);

        CREATE INDEX IF NOT EXISTS idx_join_requests_requested
        ON join_requests(requested_at);

        CREATE TABLE IF NOT EXISTS broadcasts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            text TEXT,
            media_type TEXT DEFAULT 'none',
            file_id TEXT,
            caption TEXT,
            parse_mode TEXT DEFAULT 'HTML',
            buttons_json TEXT DEFAULT '[]',
            status TEXT DEFAULT 'pending',
            total INTEGER DEFAULT 0,
            sent INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            blocked INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS broadcast_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(broadcast_id) REFERENCES broadcasts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_broadcast_logs_broadcast
        ON broadcast_logs(broadcast_id);

        CREATE TABLE IF NOT EXISTS bot_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            user_id INTEGER,
            channel_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS error_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT,
            module TEXT,
            event TEXT,
            exception TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            created_at TEXT NOT NULL,
            created_by INTEGER,
            size INTEGER
        );
        """
        self.conn.executescript(schema)
        self.conn.commit()

    def seed_defaults(self):
        now = utc_now()

        defaults = {
            "maintenance_mode": "0",
            "auto_message_enabled": "1",
            "start_message": "Please join our channel to continue.",
            "start_button_text": "JOIN NOW",
            "auto_media_type": "none",
            "auto_file_id": "",
            "auto_caption": "",
            "auto_parse_mode": "HTML",
            "auto_buttons": "[]",
            "bot_name": "Join Request Bot",
        }

        for key, value in defaults.items():
            self.execute(
                """
                INSERT OR IGNORE INTO bot_settings(key, value)
                VALUES (?, ?)
                """,
                (key, value),
            )

        self.execute(
            """
            INSERT OR IGNORE INTO admins(user_id, role, permissions, created_at)
            VALUES (?, 'owner', ?, ?)
            """,
            (
                OWNER_ID,
                json.dumps({"all": True}),
                now,
            ),
            commit=True,
        )

    def setting(self, key: str, default: str = "") -> str:
        row = self.fetchone(
            "SELECT value FROM bot_settings WHERE key=?",
            (key,),
        )
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self.execute(
            """
            INSERT INTO bot_settings(key,value)
            VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
            commit=True,
        )

    def upsert_user(self, user):
        now = utc_now()

        self.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, last_name,
                language_code, is_bot, first_seen, last_seen
            )
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                language_code=excluded.language_code,
                last_seen=excluded.last_seen
            """,
            (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                user.language_code,
                int(user.is_bot),
                now,
                now,
            ),
            commit=True,
        )

    def save_join_request(
        self,
        user_id: int,
        channel_id: int,
        event_key: str,
    ) -> Optional[int]:
        try:
            cur = self.execute(
                """
                INSERT INTO join_requests(
                    user_id, channel_id, requested_at, event_key
                )
                VALUES(?,?,?,?)
                """,
                (
                    user_id,
                    channel_id,
                    utc_now(),
                    event_key,
                ),
                commit=True,
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None

    def update_join_request(
        self,
        row_id: int,
        sent: bool,
        error: Optional[str] = None,
        status: str = "completed",
    ):
        self.execute(
            """
            UPDATE join_requests
            SET message_sent=?,
                message_sent_at=?,
                error=?,
                status=?
            WHERE id=?
            """,
            (
                int(sent),
                utc_now() if sent else None,
                error,
                status,
                row_id,
            ),
            commit=True,
        )

    def log_event(
        self,
        event_type: str,
        user_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        details: str = "",
    ):
        self.execute(
            """
            INSERT INTO bot_events(
                event_type,user_id,channel_id,details,created_at
            )
            VALUES(?,?,?,?,?)
            """,
            (
                event_type,
                user_id,
                channel_id,
                details,
                utc_now(),
            ),
            commit=True,
        )

    def log_error(
        self,
        level: str,
        module: str,
        event: str,
        exception: str,
    ):
        try:
            self.execute(
                """
                INSERT INTO error_logs(
                    level,module,event,exception,created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    level,
                    module,
                    event,
                    exception[:4000],
                    utc_now(),
                ),
                commit=True,
            )
        except Exception:
            logger.exception("Unable to save error log")

    def get_channels(self, enabled_only=False):
        if enabled_only:
            return self.fetchall(
                """
                SELECT * FROM channels
                WHERE enabled=1
                ORDER BY sort_order, title
                """
            )

        return self.fetchall(
            """
            SELECT * FROM channels
            ORDER BY sort_order, title
            """
        )

    def get_message(self):
        return self.fetchone(
            """
            SELECT * FROM messages
            WHERE name='join_request'
            """
        )

    def ensure_message(self):
        now = utc_now()
        self.execute(
            """
            INSERT OR IGNORE INTO messages(
                name, media_type, caption, parse_mode,
                enabled, created_at, updated_at
            )
            VALUES('join_request','none','','HTML',1,?,?)
            """,
            (now, now),
            commit=True,
        )

    def get_buttons(self, message_id: int):
        return self.fetchall(
            """
            SELECT * FROM message_buttons
            WHERE message_id=? AND enabled=1
            ORDER BY row_number, position, id
            """,
            (message_id,),
        )

    def set_auto_message(
        self,
        media_type: str,
        file_id: str,
        caption: str,
        parse_mode: str,
    ):
        self.ensure_message()
        self.execute(
            """
            UPDATE messages
            SET media_type=?,
                file_id=?,
                caption=?,
                parse_mode=?,
                updated_at=?
            WHERE name='join_request'
            """,
            (
                media_type,
                file_id,
                caption,
                parse_mode,
                utc_now(),
            ),
            commit=True,
        )

    def clear_buttons(self, message_id: int):
        self.execute(
            "DELETE FROM message_buttons WHERE message_id=?",
            (message_id,),
            commit=True,
        )

    def add_button(
        self,
        message_id: int,
        text: str,
        url: str,
        row: int,
        position: int,
    ):
        self.execute(
            """
            INSERT INTO message_buttons(
                message_id,text,url,row_number,position,enabled
            )
            VALUES(?,?,?,?,?,1)
            """,
            (
                message_id,
                text,
                url,
                row,
                position,
            ),
            commit=True,
        )

    def get_stats(self):
        total_users = self.fetchone(
            "SELECT COUNT(*) c FROM users"
        )["c"]

        active_users = self.fetchone(
            "SELECT COUNT(*) c FROM users WHERE is_blocked=0"
        )["c"]

        blocked_users = self.fetchone(
            "SELECT COUNT(*) c FROM users WHERE is_blocked=1"
        )["c"]

        total_requests = self.fetchone(
            "SELECT COUNT(*) c FROM join_requests"
        )["c"]

        today_requests = self.fetchone(
            """
            SELECT COUNT(*) c FROM join_requests
            WHERE date(requested_at)=date('now')
            """
        )["c"]

        week_requests = self.fetchone(
            """
            SELECT COUNT(*) c FROM join_requests
            WHERE requested_at >= datetime('now','-7 days')
            """
        )["c"]

        month_requests = self.fetchone(
            """
            SELECT COUNT(*) c FROM join_requests
            WHERE requested_at >= datetime('now','-30 days')
            """
        )["c"]

        sent = self.fetchone(
            "SELECT COUNT(*) c FROM join_requests WHERE message_sent=1"
        )["c"]

        failed = self.fetchone(
            """
            SELECT COUNT(*) c FROM join_requests
            WHERE message_sent=0 AND status='failed'
            """
        )["c"]

        channels = self.fetchone(
            "SELECT COUNT(*) c FROM channels"
        )["c"]

        return {
            "users": total_users,
            "active": active_users,
            "blocked": blocked_users,
            "requests": total_requests,
            "today": today_requests,
            "week": week_requests,
            "month": month_requests,
            "sent": sent,
            "failed": failed,
            "channels": channels,
        }


db = Database(DB_PATH)
db.connect()
db.ensure_message()


# ============================================================
# HELPERS
# ============================================================

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def is_admin(user_id: Optional[int]) -> bool:
    if not user_id:
        return False

    row = db.fetchone(
        "SELECT role, permissions FROM admins WHERE user_id=?",
        (user_id,),
    )

    if not row:
        return False

    if row["role"] == "owner":
        return True

    try:
        permissions = json.loads(row["permissions"] or "{}")
        return bool(permissions.get("all"))
    except Exception:
        return False


def require_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and is_admin(user.id))


def clean_url(url: str) -> bool:
    return bool(
        re.match(
            r"^https?://[^\s]+$",
            url.strip(),
            re.IGNORECASE,
        )
    )


def safe_json(value: str, fallback):
    try:
        return json.loads(value)
    except Exception:
        return fallback


def parse_buttons():
    raw = db.setting("auto_buttons", "[]")
    data = safe_json(raw, [])
    if not isinstance(data, list):
        return []
    return data


def build_keyboard(buttons):
    if not buttons:
        return None

    rows = {}

    for button in buttons:
        if not isinstance(button, dict):
            continue

        text = str(button.get("text", "")).strip()
        url = str(button.get("url", "")).strip()

        if not text or not clean_url(url):
            continue

        row = int(button.get("row", 0))
        position = int(button.get("position", 0))

        rows.setdefault(row, []).append(
            (
                position,
                InlineKeyboardButton(text=text, url=url),
            )
        )

    result = []

    for row_number in sorted(rows):
        result.append(
            [
                button
                for _, button in sorted(
                    rows[row_number],
                    key=lambda item: item[0],
                )
            ]
        )

    return InlineKeyboardMarkup(result) if result else None


def build_start_keyboard():
    channels = db.get_channels(enabled_only=True)

    rows = []

    for channel in channels:
        username = channel["username"]

        if username:
            username = username.lstrip("@")
            url = f"https://t.me/{username}"
        else:
            url = db.setting(
                f"channel_url_{channel['channel_id']}",
                "",
            )

        if url:
            rows.append(
                [
                    InlineKeyboardButton(
                        "JOIN NOW",
                        url=url,
                    )
                ]
            )

    if db.setting("check_join_enabled", "0") == "1":
        rows.append(
            [
                InlineKeyboardButton(
                    "I HAVE JOINED",
                    callback_data="check_join",
                )
            ]
        )

    return InlineKeyboardMarkup(rows) if rows else None


async def answer(
    query,
    text: str = "",
    show_alert=False,
):
    try:
        await query.answer(
            text=text[:200],
            show_alert=show_alert,
        )
    except TelegramError:
        pass


async def safe_send_message(
    bot,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    reply_markup=None,
):
    for attempt in range(MAX_RETRIES):
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)
        except (NetworkError, TimedOut):
            if attempt >= MAX_RETRIES - 1:
                raise
            await asyncio.sleep(2 ** attempt)

    raise RuntimeError("Message send retry limit reached")


async def send_configured_message(
    bot,
    chat_id: int,
):
    msg = db.get_message()

    if not msg or not msg["enabled"]:
        return True

    caption = msg["caption"] or ""
    parse_mode = msg["parse_mode"] or None
    keyboard = build_keyboard(
        [
            {
                "text": row["text"],
                "url": row["url"],
                "row": row["row_number"],
                "position": row["position"],
            }
            for row in db.get_buttons(msg["id"])
        ]
    )

    media_type = msg["media_type"] or "none"
    file_id = msg["file_id"] or ""

    for attempt in range(MAX_RETRIES):
        try:
            if media_type == "photo" and file_id:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption=caption or None,
                    parse_mode=parse_mode,
                    reply_markup=keyboard,
                )

            if media_type == "document" and file_id:
                return await bot.send_document(
                    chat_id=chat_id,
                    document=file_id,
                    caption=caption or None,
                    parse_mode=parse_mode,
                    reply_markup=keyboard,
                )

            return await bot.send_message(
                chat_id=chat_id,
                text=caption or " ",
                parse_mode=parse_mode,
                reply_markup=keyboard,
            )

        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 1)

        except (NetworkError, TimedOut):
            if attempt >= MAX_RETRIES - 1:
                raise
            await asyncio.sleep(2 ** attempt)

    raise RuntimeError("Configured message retry limit reached")


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 Dashboard",
                    callback_data="admin_dashboard",
                ),
                InlineKeyboardButton(
                    "⚙️ Settings",
                    callback_data="admin_settings",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📩 Join Request",
                    callback_data="admin_join",
                ),
                InlineKeyboardButton(
                    "💬 Message Builder",
                    callback_data="admin_message",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📢 Channels",
                    callback_data="admin_channels",
                ),
                InlineKeyboardButton(
                    "👥 Users",
                    callback_data="admin_users",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📢 Broadcast",
                    callback_data="admin_broadcast",
                ),
                InlineKeyboardButton(
                    "📈 Statistics",
                    callback_data="admin_stats",
                ),
            ],
            [
                InlineKeyboardButton(
                    "💾 Backup",
                    callback_data="admin_backup",
                ),
                InlineKeyboardButton(
                    "📤 Export",
                    callback_data="admin_export",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧪 Test Message",
                    callback_data="admin_test",
                ),
                InlineKeyboardButton(
                    "📝 Logs",
                    callback_data="admin_logs",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔐 Admins",
                    callback_data="admin_admins",
                )
            ],
        ]
    )


# ============================================================
# /START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    db.upsert_user(user)
    db.log_event("start", user.id)

    if db.setting("maintenance_mode", "0") == "1" and not is_admin(user.id):
        return

    text = db.setting(
        "start_message",
        "Please join our channel to continue.",
    )

    keyboard = build_start_keyboard()

    if keyboard:
        await update.message.reply_text(
            text,
            reply_markup=keyboard,
        )
    else:
        # No channel configured means no unnecessary promotional message.
        if is_admin(user.id):
            await update.message.reply_text(
                "No channel is configured yet.",
                reply_markup=admin_menu(),
            )


# ============================================================
# JOIN REQUEST
# ============================================================

async def handle_join_request(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    request = update.chat_join_request

    if not request:
        return

    user = request.from_user
    chat = request.chat

    try:
        db.upsert_user(user)

        configured = db.fetchone(
            """
            SELECT * FROM channels
            WHERE channel_id=? AND enabled=1
            """,
            (chat.id,),
        )

        if not configured:
            db.log_event(
                "join_request_ignored",
                user.id,
                chat.id,
                "Channel not configured/enabled",
            )
            return

        event_key = (
            f"{chat.id}:{user.id}:"
            f"{request.date.timestamp() if request.date else time.time()}"
        )

        row_id = db.save_join_request(
            user.id,
            chat.id,
            event_key,
        )

        # Telegram normally won't resend the same update to the same
        # running bot, but this protects against duplicate processing.
        if row_id is None:
            db.log_event(
                "duplicate_join_request",
                user.id,
                chat.id,
            )
            return

        db.log_event(
            "join_request_received",
            user.id,
            chat.id,
        )

        if db.setting("auto_message_enabled", "1") != "1":
            db.update_join_request(
                row_id,
                sent=False,
                status="disabled",
            )
            return

        try:
            await send_configured_message(
                context.bot,
                user.id,
            )

            db.update_join_request(
                row_id,
                sent=True,
                status="sent",
            )

            db.log_event(
                "join_request_message_sent",
                user.id,
                chat.id,
            )

        except Forbidden as exc:
            error = str(exc)
            db.execute(
                "UPDATE users SET is_blocked=1 WHERE user_id=?",
                (user.id,),
                commit=True,
            )
            db.update_join_request(
                row_id,
                sent=False,
                error=error,
                status="blocked",
            )
            db.log_error(
                "WARNING",
                "join_request",
                "send_message_forbidden",
                error,
            )

        except Exception as exc:
            error = str(exc)
            db.update_join_request(
                row_id,
                sent=False,
                error=error,
                status="failed",
            )
            db.log_error(
                "ERROR",
                "join_request",
                "send_message_failed",
                error,
            )

    except Exception as exc:
        logger.exception("Join request handler failed")
        db.log_error(
            "EXCEPTION",
            "join_request",
            "handler",
            repr(exc),
        )


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not require_admin(update):
        await update.message.reply_text("Access Denied")
        return

    db.upsert_user(update.effective_user)

    await update.message.reply_text(
        "🔐 Admin Panel",
        reply_markup=admin_menu(),
    )


async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not require_admin(update):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "Cancelled.",
        reply_markup=admin_menu(),
    )


# ============================================================
# ADMIN CALLBACK ROUTER
# ============================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query:
        return

    user = query.from_user

    if not is_admin(user.id):
        await answer(
            query,
            "Access Denied",
            show_alert=True,
        )
        return

    data = query.data or ""

    try:
        await answer(query)

        if data == "admin_home":
            await query.edit_message_text(
                "🔐 Admin Panel",
                reply_markup=admin_menu(),
            )
            return

        if data == "admin_dashboard":
            await show_dashboard(query)
            return

        if data == "admin_stats":
            await show_statistics(query)
            return

        if data == "admin_settings":
            await show_settings(query)
            return

        if data == "admin_join":
            await show_join_settings(query)
            return

        if data == "admin_message":
            await show_message_builder(query)
            return

        if data == "admin_channels":
            await show_channels(query)
            return

        if data == "admin_users":
            await show_users(query)
            return

        if data == "admin_broadcast":
            await show_broadcast_menu(query)
            return

        if data == "admin_backup":
            await show_backup_menu(query)
            return

        if data == "admin_export":
            await show_export_menu(query)
            return

        if data == "admin_test":
            await test_message(query, context)
            return

        if data == "admin_logs":
            await show_logs(query)
            return

        if data == "admin_admins":
            await show_admins(query)
            return

        if data == "toggle_auto":
            current = db.setting(
                "auto_message_enabled",
                "1",
            )
            db.set_setting(
                "auto_message_enabled",
                "0" if current == "1" else "1",
            )
            await show_join_settings(query)
            return

        if data == "toggle_maintenance":
            current = db.setting(
                "maintenance_mode",
                "0",
            )
            db.set_setting(
                "maintenance_mode",
                "0" if current == "1" else "1",
            )
            await show_settings(query)
            return

        if data == "toggle_check":
            current = db.setting(
                "check_join_enabled",
                "0",
            )
            db.set_setting(
                "check_join_enabled",
                "0" if current == "1" else "1",
            )
            await show_settings(query)
            return

        if data == "set_caption":
            context.user_data["awaiting"] = "caption"
            await query.message.reply_text(
                "Send the new caption now.\n"
                "Use HTML or Markdown according to the selected parse mode.\n\n"
                "Use /cancel to cancel."
            )
            return

        if data == "set_parse":
            current = db.setting(
                "auto_parse_mode",
                "HTML",
            )

            new_mode = "MarkdownV2" if current == "HTML" else "HTML"

            db.set_setting(
                "auto_parse_mode",
                new_mode,
            )

            msg = db.get_message()
            if msg:
                db.execute(
                    """
                    UPDATE messages
                    SET parse_mode=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        new_mode,
                        utc_now(),
                        msg["id"],
                    ),
                    commit=True,
                )

            await show_message_builder(query)
            return

        if data == "set_photo":
            context.user_data["awaiting"] = "photo"
            await query.message.reply_text(
                "Send the photo now.\n\n"
                "Use /cancel to cancel."
            )
            return

        if data == "remove_photo":
            db.set_setting(
                "auto_media_type",
                "none",
            )
            db.set_setting(
                "auto_file_id",
                "",
            )

            msg = db.get_message()
            if msg:
                db.execute(
                    """
                    UPDATE messages
                    SET media_type='none',
                        file_id='',
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        utc_now(),
                        msg["id"],
                    ),
                    commit=True,
                )

            await show_message_builder(query)
            return

        if data == "clear_buttons":
            msg = db.get_message()
            if msg:
                db.clear_buttons(msg["id"])

            db.set_setting(
                "auto_buttons",
                "[]",
            )

            await show_message_builder(query)
            return

        if data == "set_buttons":
            context.user_data["awaiting"] = "buttons"
            await query.message.reply_text(
                "Send buttons as JSON.\n\n"
                'Example:\n'
                '[{"text":"JOIN NOW","url":"https://t.me/example",'
                '"row":0,"position":0},'
                '{"text":"CHANNEL","url":"https://t.me/example2",'
                '"row":0,"position":1}]\n\n'
                "Only http/https URLs are accepted.\n"
                "Use /cancel to cancel."
            )
            return

        if data == "preview":
            await preview_message(query, context)
            return

        if data == "add_channel":
            context.user_data["awaiting"] = "channel"
            await query.message.reply_text(
                "Send the channel ID.\n\n"
                "Example:\n"
                "-1001234567890\n\n"
                "The bot must be administrator in the channel "
                "with permission to manage join requests."
            )
            return

        if data.startswith("remove_channel:"):
            channel_id = int(data.split(":", 1)[1])

            if user.id != OWNER_ID:
                await answer(
                    query,
                    "Only Owner can remove channels.",
                    True,
                )
                return

            db.execute(
                "DELETE FROM channels WHERE channel_id=?",
                (channel_id,),
                commit=True,
            )

            await show_channels(query)
            return

        if data.startswith("channel_toggle:"):
            channel_id = int(data.split(":", 1)[1])

            db.execute(
                """
                UPDATE channels
                SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END,
                    updated_at=?
                WHERE channel_id=?
                """,
                (
                    utc_now(),
                    channel_id,
                ),
                commit=True,
            )

            await show_channels(query)
            return

        if data == "broadcast_start":
            context.user_data["awaiting"] = "broadcast"
            await query.message.reply_text(
                "Send the broadcast text now.\n\n"
                "You can send a text message or a photo with caption.\n"
                "Use /cancel to cancel."
            )
            return

        if data == "backup_create":
            await create_backup(query)
            return

        if data == "export_users":
            await export_csv(query, "users")
            return

        if data == "export_requests":
            await export_csv(query, "join_requests")
            return

        if data == "export_broadcasts":
            await export_csv(query, "broadcast_logs")
            return

        if data == "check_join":
            await answer(
                query,
                "Join verification can only be performed against "
                "the configured channels.",
            )
            return

        await answer(
            query,
            "Unknown or expired action.",
            True,
        )

    except Exception as exc:
        logger.exception("Admin callback failed")
        db.log_error(
            "EXCEPTION",
            "admin_callback",
            data,
            repr(exc),
        )

        try:
            await query.message.reply_text(
                "⚠️ Operation failed safely.\n"
                "Check logs for details."
            )
        except Exception:
            pass


# ============================================================
# DASHBOARD / SETTINGS
# ============================================================

async def show_dashboard(query):
    s = db.get_stats()

    text = (
        "📊 BOT DASHBOARD\n\n"
        f"👥 Total Users: {s['users']}\n"
        f"🟢 Active: {s['active']}\n"
        f"🚫 Blocked: {s['blocked']}\n\n"
        f"📩 Total Join Requests: {s['requests']}\n"
        f"📅 Today: {s['today']}\n"
        f"📆 This Week: {s['week']}\n"
        f"🗓 This Month: {s['month']}\n\n"
        f"📤 Messages Sent: {s['sent']}\n"
        f"❌ Messages Failed: {s['failed']}\n"
        f"📢 Channels: {s['channels']}\n\n"
        f"🗄 Database: {DB_PATH.name}\n"
        f"📦 Size: {DB_PATH.stat().st_size:,} bytes"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data="admin_dashboard",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]
        ),
    )


async def show_statistics(query):
    s = db.get_stats()

    success_rate = 0.0
    total = s["sent"] + s["failed"]

    if total:
        success_rate = (s["sent"] / total) * 100

    text = (
        "📈 STATISTICS\n\n"
        f"Users: {s['users']}\n"
        f"New/Active: {s['active']}\n"
        f"Blocked: {s['blocked']}\n\n"
        f"Requests today: {s['today']}\n"
        f"Requests 7d: {s['week']}\n"
        f"Requests 30d: {s['month']}\n\n"
        f"Messages: {s['sent']}\n"
        f"Failures: {s['failed']}\n"
        f"Success rate: {success_rate:.2f}%"
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ]
            ]
        ),
    )


async def show_settings(query):
    maintenance = db.setting(
        "maintenance_mode",
        "0",
    )

    check_join = db.setting(
        "check_join_enabled",
        "0",
    )

    bot_name = db.setting(
        "bot_name",
        "Join Request Bot",
    )

    text = (
        "⚙️ BOT SETTINGS\n\n"
        f"Bot Name: {bot_name}\n"
        f"Maintenance: {'ON' if maintenance == '1' else 'OFF'}\n"
        f"Join Check Button: {'ON' if check_join == '1' else 'OFF'}\n"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Maintenance {'OFF' if maintenance == '1' else 'ON'}",
                    callback_data="toggle_maintenance",
                )
            ],
            [
                InlineKeyboardButton(
                    f"Check Join {'OFF' if check_join == '1' else 'ON'}",
                    callback_data="toggle_check",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin_home",
                )
            ],
        ]
    )

    await query.edit_message_text(
        text,
        reply_markup=keyboard,
    )


async def show_join_settings(query):
    enabled = db.setting(
        "auto_message_enabled",
        "1",
    )

    text = (
        "📩 JOIN REQUEST SETTINGS\n\n"
        f"Auto Message: {'ON' if enabled == '1' else 'OFF'}\n\n"
        "The bot processes ChatJoinRequest updates only "
        "for configured/enabled channels."
    )

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Auto Message {'OFF' if enabled == '1' else 'ON'}",
                        callback_data="toggle_auto",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]
        ),
    )


# ============================================================
# MESSAGE BUILDER
# ============================================================

async def show_message_builder(query):
    msg = db.get_message()

    if not msg:
        db.ensure_message()
        msg = db.get_message()

    media = msg["media_type"] or "none"
    parse_mode = msg["parse_mode"] or "HTML"
    caption = msg["caption"] or ""

    button_count = len(db.get_buttons(msg["id"]))

    text = (
        "💬 MESSAGE BUILDER\n\n"
        f"Media: {media}\n"
        f"Parse Mode: {parse_mode}\n"
        f"Buttons: {button_count}\n\n"
        "Caption preview:\n"
        f"{caption[:700] if caption else '(empty)'}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📝 Caption",
                    callback_data="set_caption",
                ),
                InlineKeyboardButton(
                    "🔤 Parse Mode",
                    callback_data="set_parse",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🖼 Set Photo",
                    callback_data="set_photo",
                ),
                InlineKeyboardButton(
                    "🗑 Remove Photo",
                    callback_data="remove_photo",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔘 Set Buttons",
                    callback_data="set_buttons",
                ),
                InlineKeyboardButton(
                    "🗑 Clear Buttons",
                    callback_data="clear_buttons",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👁 Preview",
                    callback_data="preview",
                ),
                InlineKeyboardButton(
                    "🧪 Test",
                    callback_data="admin_test",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="admin_home",
                )
            ],
        ]
    )

    await query.edit_message_text(
        text,
        reply_markup=keyboard,
    )


async def preview_message(
    query,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        await send_configured_message(
            context.bot,
            query.from_user.id,
        )

        await query.message.reply_text(
            "👁 Preview sent above."
        )

    except Exception as exc:
        await query.message.reply_text(
            f"Preview failed: {str(exc)[:500]}"
        )


async def test_message(
    query,
    context: ContextTypes.DEFAULT_TYPE,
):
    try:
        await send_configured_message(
            context.bot,
            query.from_user.id,
        )

        await query.message.reply_text(
            "🧪 Test message sent."
        )

    except Exception as exc:
        await query.message.reply_text(
            f"Test failed: {str(exc)[:500]}"
        )


# ============================================================
# CHANNEL MANAGER
# ============================================================

async def show_channels(query):
    channels = db.get_channels()

    lines = ["📢 CHANNEL MANAGER\n"]

    if not channels:
        lines.append("No channels configured.")
    else:
        for channel in channels:
            status = "ON" if channel["enabled"] else "OFF"

            title = (
                channel["title"]
                or channel["username"]
                or str(channel["channel_id"])
            )

            lines.append(
                f"• {title}\n"
                f"  ID: {channel['channel_id']}\n"
                f"  Status: {status}\n"
            )

    rows = [
        [
            InlineKeyboardButton(
                "➕ Add Channel",
                callback_data="add_channel",
            )
        ]
    ]

    for channel in channels:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'Disable' if channel['enabled'] else 'Enable'} "
                    f"{channel['channel_id']}",
                    callback_data=(
                        f"channel_toggle:{channel['channel_id']}"
                    ),
                ),
                InlineKeyboardButton(
                    "🗑",
                    callback_data=(
                        f"remove_channel:{channel['channel_id']}"
                    ),
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data="admin_home",
            )
        ]
    )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def add_channel_from_id(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
):
    try:
        channel_id = int(text.strip())
    except ValueError:
        await update.message.reply_text(
            "Invalid channel ID."
        )
        return

    try:
        chat = await context.bot.get_chat(channel_id)

        if chat.type not in (
            "channel",
            "supergroup",
        ):
            await update.message.reply_text(
                "The specified chat is not a channel/supergroup."
            )
            return

        member = await context.bot.get_chat_member(
            chat.id,
            context.bot.id,
        )

        status = getattr(member, "status", "")

        if status not in ("administrator", "creator"):
            await update.message.reply_text(
                "Bot must be administrator in this channel."
            )
            return

        username = chat.username
        title = chat.title or username or str(chat.id)

        now = utc_now()

        db.execute(
            """
            INSERT INTO channels(
                channel_id,username,title,type,
                enabled,required,sort_order,
                created_at,updated_at
            )
            VALUES(?,?,?,?,1,1,0,?,?)
            ON CONFLICT(channel_id) DO UPDATE SET
                username=excluded.username,
                title=excluded.title,
                type=excluded.type,
                updated_at=excluded.updated_at
            """,
            (
                chat.id,
                username,
                title,
                chat.type,
                now,
                now,
            ),
            commit=True,
        )

        await update.message.reply_text(
            f"✅ Channel configured:\n"
            f"{title}\n"
            f"ID: {chat.id}"
        )

    except TelegramError as exc:
        await update.message.reply_text(
            "Could not access this channel.\n\n"
            f"{str(exc)[:700]}"
        )


# ============================================================
# ADMIN USER / ADMIN LIST
# ============================================================

async def show_users(query):
    stats = db.get_stats()

    latest = db.fetchall(
        """
        SELECT user_id, username, first_name, last_seen
        FROM users
        ORDER BY last_seen DESC
        LIMIT 10
        """
    )

    lines = [
        "👥 USERS",
        "",
        f"Total: {stats['users']}",
        f"Active: {stats['active']}",
        f"Blocked: {stats['blocked']}",
        "",
        "Latest users:",
    ]

    for row in latest:
        name = (
            row["username"]
            or row["first_name"]
            or str(row["user_id"])
        )
        lines.append(
            f"• {name} — {row['user_id']}"
        )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📤 Export CSV",
                        callback_data="export_users",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]
        ),
    )


async def show_admins(query):
    rows = db.fetchall(
        """
        SELECT user_id, role, created_at
        FROM admins
        ORDER BY role, user_id
        """
    )

    lines = ["🔐 ADMINS\n"]

    for row in rows:
        owner_mark = " 👑" if row["role"] == "owner" else ""
        lines.append(
            f"{row['user_id']} — {row['role']}{owner_mark}"
        )

    lines.append(
        "\nOwner management is restricted to OWNER_ID."
    )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ]
            ]
        ),
    )


# ============================================================
# BROADCAST
# ============================================================

async def show_broadcast_menu(query):
    await query.edit_message_text(
        "📢 BROADCAST\n\n"
        "Create a broadcast and send it to active users in "
        "controlled batches.\n\n"
        "Blocked users are automatically marked.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ New Broadcast",
                        callback_data="broadcast_start",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]
        ),
    )


async def create_broadcast_from_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    text = update.message.text or ""
    media_type = "none"
    file_id = ""
    caption = text

    if update.message.photo:
        media_type = "photo"
        file_id = update.message.photo[-1].file_id
        caption = update.message.caption or ""

    now = utc_now()

    cur = db.execute(
        """
        INSERT INTO broadcasts(
            admin_id,text,media_type,file_id,caption,
            parse_mode,buttons_json,status,created_at
        )
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            user.id,
            text if media_type == "none" else "",
            media_type,
            file_id,
            caption,
            db.setting("auto_parse_mode", "HTML"),
            db.setting("auto_buttons", "[]"),
            "pending",
            now,
        ),
        commit=True,
    )

    broadcast_id = cur.lastrowid

    context.user_data["pending_broadcast_id"] = broadcast_id

    await update.message.reply_text(
        f"📢 Broadcast #{broadcast_id} created.\n\n"
        "Send /broadcast_confirm to start.\n"
        "Send /cancel to cancel."
    )


async def broadcast_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not require_admin(update):
        await update.message.reply_text("Access Denied")
        return

    broadcast_id = context.user_data.get(
        "pending_broadcast_id"
    )

    if not broadcast_id:
        await update.message.reply_text(
            "No pending broadcast."
        )
        return

    row = db.fetchone(
        "SELECT * FROM broadcasts WHERE id=?",
        (broadcast_id,),
    )

    if not row:
        await update.message.reply_text(
            "Broadcast not found."
        )
        return

    db.execute(
        """
        UPDATE broadcasts
        SET status='running',started_at=?
        WHERE id=?
        """,
        (
            utc_now(),
            broadcast_id,
        ),
        commit=True,
    )

    context.user_data.pop(
        "pending_broadcast_id",
        None,
    )

    await update.message.reply_text(
        f"📢 Broadcast #{broadcast_id} started."
    )

    context.application.create_task(
        run_broadcast(
            context.application,
            broadcast_id,
        )
    )


async def run_broadcast(
    application: Application,
    broadcast_id: int,
):
    row = db.fetchone(
        "SELECT * FROM broadcasts WHERE id=?",
        (broadcast_id,),
    )

    if not row:
        return

    users = db.fetchall(
        """
        SELECT user_id FROM users
        WHERE is_blocked=0
        ORDER BY user_id
        """
    )

    total = len(users)

    db.execute(
        "UPDATE broadcasts SET total=? WHERE id=?",
        (
            total,
            broadcast_id,
        ),
        commit=True,
    )

    sent = 0
    failed = 0
    blocked = 0

    keyboard = build_keyboard(
        safe_json(
            row["buttons_json"] or "[]",
            [],
        )
    )

    for index, user in enumerate(users):
        user_id = user["user_id"]

        try:
            if row["media_type"] == "photo" and row["file_id"]:
                await application.bot.send_photo(
                    chat_id=user_id,
                    photo=row["file_id"],
                    caption=row["caption"] or None,
                    parse_mode=row["parse_mode"] or None,
                    reply_markup=keyboard,
                )
            else:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=row["text"] or row["caption"] or " ",
                    parse_mode=row["parse_mode"] or None,
                    reply_markup=keyboard,
                )

            sent += 1

            db.execute(
                """
                INSERT INTO broadcast_logs(
                    broadcast_id,user_id,status,error,created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    broadcast_id,
                    user_id,
                    "sent",
                    None,
                    utc_now(),
                ),
                commit=True,
            )

        except Forbidden as exc:
            blocked += 1
            error = str(exc)

            db.execute(
                """
                UPDATE users SET is_blocked=1
                WHERE user_id=?
                """,
                (user_id,),
                commit=True,
            )

            db.execute(
                """
                INSERT INTO broadcast_logs(
                    broadcast_id,user_id,status,error,created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    broadcast_id,
                    user_id,
                    "blocked",
                    error,
                    utc_now(),
                ),
                commit=True,
            )

        except RetryAfter as exc:
            try:
                await asyncio.sleep(
                    float(exc.retry_after) + 1
                )

                if row["media_type"] == "photo" and row["file_id"]:
                    await application.bot.send_photo(
                        chat_id=user_id,
                        photo=row["file_id"],
                        caption=row["caption"] or None,
                        parse_mode=row["parse_mode"] or None,
                        reply_markup=keyboard,
                    )
                else:
                    await application.bot.send_message(
                        chat_id=user_id,
                        text=row["text"] or row["caption"] or " ",
                        parse_mode=row["parse_mode"] or None,
                        reply_markup=keyboard,
                    )

                sent += 1

            except Exception as retry_exc:
                failed += 1
                db.log_error(
                    "WARNING",
                    "broadcast",
                    "retry_failed",
                    repr(retry_exc),
                )

        except (NetworkError, TimedOut) as exc:
            failed += 1

            db.execute(
                """
                INSERT INTO broadcast_logs(
                    broadcast_id,user_id,status,error,created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    broadcast_id,
                    user_id,
                    "failed",
                    str(exc),
                    utc_now(),
                ),
                commit=True,
            )

        except Exception as exc:
            failed += 1

            db.execute(
                """
                INSERT INTO broadcast_logs(
                    broadcast_id,user_id,status,error,created_at
                )
                VALUES(?,?,?,?,?)
                """,
                (
                    broadcast_id,
                    user_id,
                    "failed",
                    str(exc),
                    utc_now(),
                ),
                commit=True,
            )

        if index % BROADCAST_BATCH_SIZE == 0:
            db.execute(
                """
                UPDATE broadcasts
                SET sent=?,failed=?,blocked=?
                WHERE id=?
                """,
                (
                    sent,
                    failed,
                    blocked,
                    broadcast_id,
                ),
                commit=True,
            )

        await asyncio.sleep(BROADCAST_DELAY)

    db.execute(
        """
        UPDATE broadcasts
        SET sent=?,failed=?,blocked=?,
            status='completed',finished_at=?
        WHERE id=?
        """,
        (
            sent,
            failed,
            blocked,
            utc_now(),
            broadcast_id,
        ),
        commit=True,
    )

    db.log_event(
        "broadcast_completed",
        details=(
            f"id={broadcast_id};sent={sent};"
            f"failed={failed};blocked={blocked}"
        ),
    )


# ============================================================
# BACKUP / RESTORE
# ============================================================

async def show_backup_menu(query):
    backups = db.fetchall(
        """
        SELECT filename,created_at,size
        FROM backups
        ORDER BY id DESC
        LIMIT 5
        """
    )

    lines = ["💾 BACKUP\n"]

    if not backups:
        lines.append("No backups yet.")
    else:
        for backup in backups:
            lines.append(
                f"• {backup['filename']}\n"
                f"  {backup['size']:,} bytes\n"
                f"  {backup['created_at']}"
            )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💾 Create Backup",
                        callback_data="backup_create",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]
        ),
    )


async def create_backup(query):
    if not DB_PATH.exists():
        await query.message.reply_text(
            "Database file does not exist."
        )
        return

    filename = (
        f"backup_"
        f"{datetime.now().strftime('%Y_%m_%d_%H%M%S')}.db"
    )

    destination = BACKUP_DIR / filename

    source_conn = None
    destination_conn = None

    try:
        source_conn = sqlite3.connect(
            str(DB_PATH),
            timeout=30,
        )

        destination_conn = sqlite3.connect(
            str(destination),
            timeout=30,
        )

        source_conn.backup(destination_conn)

        destination_conn.commit()

        size = destination.stat().st_size

        db.execute(
            """
            INSERT INTO backups(
                filename,created_at,created_by,size
            )
            VALUES(?,?,?,?)
            """,
            (
                filename,
                utc_now(),
                query.from_user.id,
                size,
            ),
            commit=True,
        )

        await query.message.reply_document(
            document=InputFile(
                str(destination),
                filename=filename,
            ),
            caption=(
                f"💾 Backup created successfully.\n"
                f"Size: {size:,} bytes"
            ),
        )

    except Exception as exc:
        logger.exception("Backup failed")
        db.log_error(
            "ERROR",
            "backup",
            "create",
            repr(exc),
        )

        await query.message.reply_text(
            f"Backup failed: {str(exc)[:500]}"
        )

    finally:
        if source_conn:
            source_conn.close()

        if destination_conn:
            destination_conn.close()


# ============================================================
# CSV EXPORT
# ============================================================

async def show_export_menu(query):
    await query.edit_message_text(
        "📤 DATABASE EXPORT",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "👥 Users CSV",
                        callback_data="export_users",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📩 Join Requests CSV",
                        callback_data="export_requests",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📢 Broadcast Logs CSV",
                        callback_data="export_broadcasts",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ],
            ]
        ),
    )


async def export_csv(query, export_type: str):
    if export_type == "users":
        rows = db.fetchall(
            "SELECT * FROM users ORDER BY user_id"
        )
        filename = "users.csv"

    elif export_type == "join_requests":
        rows = db.fetchall(
            """
            SELECT * FROM join_requests
            ORDER BY id
            """
        )
        filename = "join_requests.csv"

    elif export_type == "broadcast_logs":
        rows = db.fetchall(
            """
            SELECT * FROM broadcast_logs
            ORDER BY id
            """
        )
        filename = "broadcast_logs.csv"

    else:
        await query.message.reply_text(
            "Unknown export type."
        )
        return

    output = io.StringIO()

    if rows:
        writer = csv.writer(output)
        writer.writerow(rows[0].keys())

        for row in rows:
            writer.writerow(list(row))
    else:
        output.write("No records\n")

    output.seek(0)

    data = io.BytesIO(
        output.getvalue().encode("utf-8")
    )
    data.name = filename

    await query.message.reply_document(
        document=InputFile(data, filename=filename),
        caption=f"📤 {filename}",
    )


# ============================================================
# LOGS
# ============================================================

async def show_logs(query):
    rows = db.fetchall(
        """
        SELECT level,module,event,exception,created_at
        FROM error_logs
        ORDER BY id DESC
        LIMIT 15
        """
    )

    if not rows:
        text = "📝 LOGS\n\nNo errors recorded."
    else:
        parts = ["📝 LOGS\n"]

        for row in rows:
            parts.append(
                f"[{row['created_at']}] "
                f"{row['level']} "
                f"{row['module']}\n"
                f"{row['event']}\n"
                f"{(row['exception'] or '')[:300]}\n"
            )

        text = "\n".join(parts)

    await query.edit_message_text(
        text[:4000],
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Back",
                        callback_data="admin_home",
                    )
                ]
            ]
        ),
    )


# ============================================================
# ADMIN INPUT HANDLER
# ============================================================

async def admin_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not require_admin(update):
        return

    state = context.user_data.get("awaiting")

    if not state:
        return

    try:
        if state == "caption":
            text = update.message.text or ""

            db.set_setting(
                "auto_caption",
                text,
            )

            msg = db.get_message()

            if msg:
                db.execute(
                    """
                    UPDATE messages
                    SET caption=?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        text,
                        utc_now(),
                        msg["id"],
                    ),
                    commit=True,
                )

            context.user_data.pop("awaiting", None)

            await update.message.reply_text(
                "✅ Caption saved.",
                reply_markup=admin_menu(),
            )
            return

        if state == "photo":
            if not update.message.photo:
                await update.message.reply_text(
                    "Please send a photo."
                )
                return

            photo = update.message.photo[-1]

            db.set_setting(
                "auto_media_type",
                "photo",
            )
            db.set_setting(
                "auto_file_id",
                photo.file_id,
            )

            msg = db.get_message()

            if msg:
                db.execute(
                    """
                    UPDATE messages
                    SET media_type='photo',
                        file_id=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        photo.file_id,
                        utc_now(),
                        msg["id"],
                    ),
                    commit=True,
                )

            context.user_data.pop("awaiting", None)

            await update.message.reply_text(
                "✅ Photo saved.",
                reply_markup=admin_menu(),
            )
            return

        if state == "buttons":
            text = update.message.text or ""

            data = safe_json(text, None)

            if not isinstance(data, list):
                await update.message.reply_text(
                    "Invalid JSON array."
                )
                return

            validated = []

            for index, button in enumerate(data):
                if not isinstance(button, dict):
                    continue

                button_text = str(
                    button.get("text", "")
                ).strip()

                url = str(
                    button.get("url", "")
                ).strip()

                if not button_text:
                    raise ValueError(
                        f"Button {index + 1}: text missing."
                    )

                if not clean_url(url):
                    raise ValueError(
                        f"Button {index + 1}: invalid URL."
                    )

                validated.append(
                    {
                        "text": button_text[:64],
                        "url": url,
                        "row": int(
                            button.get("row", 0)
                        ),
                        "position": int(
                            button.get("position", index)
                        ),
                    }
                )

            msg = db.get_message()

            if msg:
                db.clear_buttons(msg["id"])

                for button in validated:
                    db.add_button(
                        msg["id"],
                        button["text"],
                        button["url"],
                        button["row"],
                        button["position"],
                    )

            db.set_setting(
                "auto_buttons",
                json.dumps(
                    validated,
                    ensure_ascii=False,
                ),
            )

            context.user_data.pop("awaiting", None)

            await update.message.reply_text(
                f"✅ {len(validated)} button(s) saved.",
                reply_markup=admin_menu(),
            )
            return

        if state == "channel":
            await add_channel_from_id(
                update,
                context,
                update.message.text or "",
            )

            context.user_data.pop("awaiting", None)
            return

        if state == "broadcast":
            await create_broadcast_from_message(
                update,
                context,
            )

            context.user_data.pop("awaiting", None)
            return

    except Exception as exc:
        logger.exception("Admin input failed")

        db.log_error(
            "EXCEPTION",
            "admin_input",
            state,
            repr(exc),
        )

        await update.message.reply_text(
            f"Operation failed safely:\n{str(exc)[:700]}"
        )


# ============================================================
# ERROR HANDLER
# ============================================================

async def global_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if isinstance(error, RetryAfter):
        logger.warning(
            "Telegram RetryAfter: %s",
            error.retry_after,
        )
        return

    if isinstance(error, Forbidden):
        logger.warning(
            "Telegram Forbidden: %s",
            error,
        )
        return

    if isinstance(error, (NetworkError, TimedOut)):
        logger.warning(
            "Temporary Telegram network error: %s",
            error,
        )
        return

    logger.exception(
        "Unhandled Telegram application error",
        exc_info=error,
    )

    try:
        db.log_error(
            "EXCEPTION",
            "application",
            "global_error",
            repr(error),
        )
    except Exception:
        logger.exception(
            "Failed to write global error to database"
        )


# ============================================================
# BOT COMMAND SETUP
# ============================================================

async def post_init(application: Application):
    db.connect()

    me = await application.bot.get_me()

    logger.info(
        "Bot connected: @%s (%s)",
        me.username,
        me.id,
    )

    try:
        await application.bot.set_my_commands(
            [
                ("start", "Start the bot"),
                ("admin", "Open admin panel"),
                ("cancel", "Cancel current action"),
                ("broadcast_confirm", "Confirm pending broadcast"),
            ]
        )
    except TelegramError:
        logger.exception("Failed to set bot commands")

    db.log_event(
        "startup",
        details=f"bot_id={me.id};username={me.username}",
    )


async def post_shutdown(application: Application):
    logger.info("Bot shutting down...")
    db.log_event("shutdown")
    db.close()


# ============================================================
# BUILD APPLICATION
# ============================================================

def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(False)
        .build()
    )

    # Start
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # Admin
    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "cancel",
            cancel_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "broadcast_confirm",
            broadcast_confirm,
        )
    )

    # Join request handler
    #
    # Current python-telegram-bot uses ChatJoinRequestHandler.
    # The Telegram Bot API requires the bot to be an administrator
    # with the can_invite_users permission to receive these updates.
    application.add_handler(
        ChatJoinRequestHandler(
            handle_join_request,
        )
    )

    # Admin callbacks
    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
        )
    )

    # Admin input
    application.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.PHOTO
            )
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            admin_input,
        )
    )

    application.add_error_handler(
        global_error_handler
    )

    return application


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "Initializing database at: %s",
        DB_PATH.resolve(),
    )

    db.connect()

    application = build_application()

    logger.info(
        "Starting polling..."
    )

    application.run_polling(
        allowed_updates=[
            "message",
            "callback_query",
            "chat_join_request",
        ],
        drop_pending_updates=False,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
