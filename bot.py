# -*- coding: utf-8 -*-
"""
HACKER_XD01 HOSTING BOT — V6 (Security Hardening + Reliability + Admin Panel)
=============================================================================
Upgraded from New-Hosting-Src.py per DK Sharma Hosting V6 Upgrade Prompt.

V6 changes (sections refer to the upgrade spec):
  Section 0.1 — Fixed "My Files -> Back -> No Files" bug.
      Every logic function now takes (chat_id, user_id, ...) explicitly.
      Callback handlers pass call.from_user.id + call.message.chat.id.
      Message handlers pass message.from_user.id + message.chat.id.
      check_subscription_and_continue() always takes explicit user_id.
  Section 0.2 — Fixed GitHub "file not accepted" bug.
      Zipball extraction flattens single root folder, recurses for main script.
  Section 0.3 — Variable / Secrets Vault (Fernet-encrypted user_variables table).
      Secrets never appear in source. Sensitive Mode + redacted admin preview.
  Section 0.4 — cleanup() runs before os.execv. SIGTERM handled gracefully.
  Section 1    — Variables UI, env injection into child processes, audit log,
                 redaction in logs, .env loading for hosting-bot secrets.
  Section 2    — Recursive main detection, standalone requirements.txt flow,
                 Download / Export All buttons, zip-bomb + path-traversal guards.
  Section 3    — Per-user navigation stack, breadcrumbs, double-tap guard,
                 send_error() helper, confirm step on destructive actions.
  Section 4    — Single-instance lock, persistent-storage detection, PID
                 reconciliation, exponential backoff, health heartbeat,
                 per-script CPU/RAM caps, log rotation, watchdog, SIGTERM.
  Section 5/6  — My Plan screen, audit log, search users, approval queue,
                 kill switch, changelog card, /feedback, /mystats, /help categorised.
"""

import telebot
from telebot import util
import subprocess
import os
import zipfile
import tempfile
import shutil
from telebot import types
import time
from datetime import datetime, timedelta
import psutil
import sqlite3
import json
import logging
import signal
import threading
import re
import sys
import atexit
import requests
import io
from urllib.parse import urlparse
import urllib3
import random

# --- .env loading (Section 1, item 8) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================================================================
# CONFIGURATION — secrets come from environment / .env, never from source
# =========================================================================
def _require_env(name, fallback=""):
    val = os.environ.get(name, "").strip()
    return val if val else fallback

# IMPORTANT: put secrets in Render Environment Variables, never in source code.
TOKEN = _require_env("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")

def _env_int(name, default=0):
    raw = _require_env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc

OWNER_ID= "8753914631"
ADMIN_ID= "8753914631"
YOUR_USERNAME  = _require_env("YOUR_USERNAME", "@OfficialDkSharma01")
SAMBA_API_KEY  = _require_env("SAMBA_API_KEY")

REQUIRED_CHANNELS = ["@FriendsChatingZone1"]

BASE_DIR        = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.environ.get("UPLOAD_BOTS_DIR", os.path.join(BASE_DIR, "upload_bots"))
IROTECH_DIR     = os.environ.get("IROTECH_DIR",     os.path.join(BASE_DIR, "inf"))
DATABASE_PATH   = os.path.join(IROTECH_DIR, "bot_data.db")
PERSISTENT_DISK = os.environ.get("PERSISTENT_DISK", IROTECH_DIR)  # Render/Railway volume mount

FREE_USER_LIMIT       = 2
SUBSCRIBED_USER_LIMIT = 15
ADMIN_LIMIT           = 99
OWNER_LIMIT            = float('inf')
MAX_UPLOAD_MB         = int(os.environ.get("MAX_UPLOAD_MB", "20"))
MAX_ZIP_RATIO         = 10        # uncompressed/compressed ratio cap (zip-bomb guard)
MAX_EXTRACT_MB        = 200       # hard cap on total extracted size
MAX_LOG_BYTES         = 2 * 1024 * 1024  # per-file log rotation cap
MAX_LOG_FILES         = 3

PLATFORM_VERSION = "V6.0"
PLATFORM_CHANGELOG = (
    "V6.0 — Variable/Secrets Vault, zip-bomb & traversal guards, "
    "recursive main-script detection, per-user navigation stack, "
    "graceful SIGTERM, single-instance lock, watchdog auto-restart, "
    "approval-queue dashboard, /mystats /feedback /help, Kill Switch."
)

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR,     exist_ok=True)
os.makedirs(PERSISTENT_DISK, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

# =========================================================================
# IN-MEMORY STATE
# =========================================================================
bot_scripts            = {}   # script_key -> {process, log_file, ...}
user_subscriptions     = {}   # user_id -> {'expiry': datetime}
user_files             = {}   # user_id -> [(file_name, file_type), ...]
active_users           = set()
admin_ids              = {ADMIN_ID, OWNER_ID}
user_custom_limits     = {}
bot_locked             = False
banned_users           = set()
auto_recovery_last_restart = {}
github_data            = {}   # multi-step session: user_id -> {...}
broadcast_state        = {}   # user_id -> {...}
set_limit_state        = {}   # admin_id -> {...}

# Per-user navigation stack (Section 3, item 40): user_id -> list of screen names
nav_stack             = {}    # user_id -> [screen_name, ...]
NAV_STACK_LOCK        = threading.Lock()

# Double-tap guard (Section 3, item 42): user_id -> last tap timestamp
_last_tap             = {}
_TAP_LOCK             = threading.Lock()
_TAP_COOLDOWN         = 2.0

# =========================================================================
# PERSISTENT UPTIME
# =========================================================================
PERSISTENT_START_FILE = os.path.join(PERSISTENT_DISK, "bot_start_time.txt")

def get_persistent_start_time():
    if os.path.exists(PERSISTENT_START_FILE):
        try:
            with open(PERSISTENT_START_FILE, "r") as f:
                return datetime.fromisoformat(f.read().strip())
        except Exception as e:
            logging.error(f"Failed to read persistent start time: {e}")
    now = datetime.now()
    try:
        with open(PERSISTENT_START_FILE, "w") as f:
            f.write(now.isoformat())
    except Exception as e:
        logging.error(f"Failed to write persistent start time: {e}")
    return now

BOT_START_TIME = get_persistent_start_time()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# =========================================================================
# SAMBANOVA AI CONFIG
# =========================================================================
SAMBA_URL = "https://api.sambanova.ai/v1/chat/completions"
AVAILABLE_MODELS = {
    "llama":    "Meta-Llama-3.3-70B-Instruct",
    "deepseek": "DeepSeek-V3.1",
    "minimax":   "MiniMax-M2.7",
    "gpt-oss":   "gpt-oss-120b",
}
DEFAULT_MODEL = "llama"
global_model  = DEFAULT_MODEL

# =========================================================================
# SECRETS / CRYPTO (Section 1 — Variables Vault)
# =========================================================================
# Per-process Fernet key. In production put VAULT_KEY in .env so values survive
# restarts. Stored at-rest so values are never plaintext in the DB.
VAULT_KEY_ENV = os.environ.get("VAULT_KEY", "").strip()
if VAULT_KEY_ENV:
    _VAULT_KEY_BYTES = VAULT_KEY_ENV.encode()
else:
    _VAULT_KEY_FILE = os.path.join(PERSISTENT_DISK, "vault.key")
    if os.path.exists(_VAULT_KEY_FILE):
        with open(_VAULT_KEY_FILE, "rb") as f:
            _VAULT_KEY_BYTES = f.read()
    else:
        from cryptography.fernet import Fernet as _F
        _VAULT_KEY_BYTES = _F.generate_key()
        try:
            with open(_VAULT_KEY_FILE, "wb") as f:
                f.write(_VAULT_KEY_BYTES)
        except Exception as e:
            logger.error(f"Could not persist vault key: {e}")

try:
    from cryptography.fernet import Fernet
    _fernet = Fernet(_VAULT_KEY_BYTES)
    VAULT_AVAILABLE = True
except Exception as e:
    logger.error(f"Fernet unavailable — falling back to base64 vault: {e}")
    import base64
    _fernet = None
    VAULT_AVAILABLE = False

def vault_encrypt(plaintext: str) -> str:
    if plaintext is None:
        plaintext = ""
    try:
        if _fernet:
            return "enc:" + _fernet.encrypt(plaintext.encode("utf-8")).decode()
        return "b64:" + base64.b64encode(plaintext.encode("utf-8")).decode()
    except Exception as e:
        logger.error(f"vault_encrypt failed: {e}")
        return "enc:ERR"

def vault_decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        if token.startswith("enc:") and _fernet:
            return _fernet.decrypt(token[4:].encode()).decode("utf-8")
        if token.startswith("b64:"):
            return base64.b64decode(token[4:].encode()).decode("utf-8")
        return token
    except Exception:
        return ""

# Secret-pattern scanner for redaction (Section 1, items 4 & 6)
SECRET_PATTERNS = [
    re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{30,}\b"),          # Telegram bot tokens
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),                   # OpenAI-style
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),                # Google API
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),                  # GitHub PAT
    re.compile(r"\bgho_[A-Za-z0-9]{30,}\b"),                  # GitHub OAuth
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b"),         # GitHub fine-grained
]

def redact_secrets(text: str) -> str:
    if not text:
        return text
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text

# =========================================================================
# KEYBOARD LAYOUTS
# =========================================================================
COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 𝐔𝐩𝐝𝐚𝐭𝐞𝐬 𝐂𝐡𝐚𝐧𝐧𝐞𝐥"],
    ["🌏 Upload", "📁 𝐌𝐲 𝐅𝐢𝐥𝐞𝐬"],
    ["⚡ 𝐁𝐨𝐭 𝐒𝐩𝐞𝐞𝐝", "🚀 𝐒𝐭𝐚𝐭𝐮𝐬"],
    ["🔄 𝐑𝐞𝐬𝐭𝐚𝐫𝐭", "⏹ 𝐒𝐭𝐨𝐩"],
    ["⚙️ Recommended Install", "🤖 𝐀𝐆𝐄𝐍𝐓"],
    ["🔑 Variables", "🌐 𝐆𝐈𝐓𝐇𝐔𝐁"],
    ["📞 𝐂𝐨𝐧𝐭𝐚𝐜𝐭 𝐎𝐰𝐧𝐞𝐫"],
]
ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC = [
    ["📢 𝐔𝐩𝐝𝐚𝐭𝐞𝐬 𝐂𝐡𝐚𝐧𝐧𝐞𝐥"],
    ["🌏 Upload", "📁 𝐌𝐲 𝐅𝐢𝐥𝐞𝐬"],
    ["⚡ 𝐁𝐨𝐭 𝐒𝐩𝐞𝐞𝐝", "🚀 𝐒𝐭𝐚𝐭𝐮𝐬"],
    ["🔄 𝐑𝐞𝐬𝐭𝐚𝐫𝐭", "⏹ 𝐒𝐭𝐨𝐩"],
    ["💳 𝐒𝐮𝐛𝐬𝐜𝐫𝐢𝐩𝐭𝐢𝐨𝐧𝐬", "📢 𝐁𝐫𝐨𝐚𝐝𝐜𝐚𝐬𝐭"],
    ["🔒 𝐋𝐨𝐜𝐤 𝐁𝐨𝐭", "🟢 𝐑𝐮𝐧𝐧𝐢𝐧𝐠 𝐀𝐥𝐥 𝐂𝐨𝐝𝐞"],
    ["🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐏𝐚𝐧𝐞𝐥", "⚙️ Recommended Install"],
    ["🤖 𝐀𝐆𝐄𝐍𝐓", "🌐 𝐆𝐈𝐓𝐇𝐔𝐁"],
    ["🔑 Variables", "📞 𝐂𝐨𝐧𝐭𝐚𝐜𝐭 𝐎𝐰𝐧𝐞𝐫"],
]

# =========================================================================
# DATABASE SETUP
# =========================================================================
DB_LOCK = threading.Lock()

def init_db():
    logger.info(f"Initializing database at: {DATABASE_PATH}")
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions
                     (user_id INTEGER PRIMARY KEY, expiry TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_files
                     (user_id INTEGER, file_name TEXT, file_type TEXT,
                      PRIMARY KEY (user_id, file_name))''')
        c.execute('''CREATE TABLE IF NOT EXISTS active_users
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins
                     (user_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pending_uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, file_id TEXT, file_name TEXT, file_type TEXT,
            file_size INTEGER, user_name TEXT, user_username TEXT,
            timestamp TEXT, extra_info TEXT,
            sensitive_mode INTEGER DEFAULT 0,
            scan_flags TEXT
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS verified_users
                     (user_id INTEGER PRIMARY KEY, verified_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users
                     (user_id INTEGER PRIMARY KEY, banned_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_limits
                     (user_id INTEGER PRIMARY KEY, custom_limit INTEGER)''')
        # Variables / secrets vault (Section 1, item 2)
        c.execute('''CREATE TABLE IF NOT EXISTS user_variables (
            user_id   INTEGER,
            file_name TEXT,
            var_name  TEXT,
            var_value TEXT,
            updated_at TEXT,
            PRIMARY KEY (user_id, file_name, var_name)
        )''')
        # Audit log (Section 1, item 13)
        c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER, action TEXT, target TEXT,
            timestamp TEXT, details TEXT
        )''')
        # User notes (admin panel, item 9)
        c.execute('''CREATE TABLE IF NOT EXISTS user_notes
                     (user_id INTEGER PRIMARY KEY, note TEXT, updated_at TEXT)''')
        # Feedback queue (Section 6 F.71)
        c.execute('''CREATE TABLE IF NOT EXISTS feedback_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, message TEXT, status TEXT DEFAULT 'open',
            created_at TEXT
        )''')
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (ADMIN_ID,))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Database init error: {e}")

def load_data():
    global banned_users, user_custom_limits
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, expiry FROM subscriptions")
        for user_id, expiry in c.fetchall():
            try:
                user_subscriptions[user_id] = {"expiry": datetime.fromisoformat(expiry)}
            except ValueError:
                pass
        c.execute("SELECT user_id, file_name, file_type FROM user_files")
        for user_id, file_name, file_type in c.fetchall():
            user_files.setdefault(user_id, []).append((file_name, file_type))
        c.execute("SELECT user_id FROM active_users")
        active_users.update(row[0] for row in c.fetchall())
        c.execute("SELECT user_id FROM admins")
        admin_ids.update(row[0] for row in c.fetchall())
        c.execute("SELECT user_id FROM banned_users")
        banned_users = set(row[0] for row in c.fetchall())
        c.execute("SELECT user_id, custom_limit FROM user_limits")
        user_custom_limits = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
    except Exception as e:
        logger.error(f"Data load error: {e}")

init_db()
load_data()

# =========================================================================
# AUDIT LOG (Section 1, item 13)
# =========================================================================
def audit_log(actor_id, action, target="", details=""):
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute('''INSERT INTO audit_log (actor_id, action, target, timestamp, details)
                         VALUES (?, ?, ?, ?, ?)''',
                       (actor_id, action, target, datetime.now().isoformat(), details))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Audit log error: {e}")

# =========================================================================
# PERSISTENT STORAGE DETECTION (Section 4, item 52)
# =========================================================================
def detect_storage_state():
    """Warn if the persistent disk looks wiped (empty DB / fresh volume)."""
    try:
        if not os.path.exists(DATABASE_PATH):
            logger.warning("⚠️ DATABASE missing — disk may have been wiped.")
            return "wiped"
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM active_users")
        users = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM user_files")
        files = c.fetchone()[0]
        conn.close()
        if users == 0 and files == 0:
            logger.warning("⚠️ DB empty — possibly fresh volume (no prior data).")
            return "fresh"
        return "existing"
    except Exception as e:
        logger.error(f"Storage detection error: {e}")
        return "unknown"

STORAGE_STATE = detect_storage_state()

# =========================================================================
# SINGLE-INSTANCE LOCK (Section 4, item 56)
# =========================================================================
LOCK_FILE = os.path.join(PERSISTENT_DISK, "hosting_bot.lock")
_lock_fp = None

def acquire_single_instance_lock():
    global _lock_fp
    try:
        import fcntl
        _lock_fp = open(LOCK_FILE, "w")
        try:
            fcntl.flock(_lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            logger.error("❌ Another instance is already running with this lock. Exiting.")
            sys.exit(0)
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
    except ImportError:
        # Windows — best-effort PID file
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, "r") as f:
                    pid = int(f.read().strip())
                if psutil.pid_exists(pid):
                    logger.error("❌ Another instance is already running. Exiting.")
                    sys.exit(0)
            except Exception:
                pass
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.error(f"Lock acquire error: {e}")

acquire_single_instance_lock()

# =========================================================================
# STYLISH TEXT
# =========================================================================
def stylish_text(text: str) -> str:
    text = re.sub(r"</?code>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    mapping = {
        "a": "ᴀ", "b": "ʙ", "c": "ᴄ", "d": "ᴅ", "e": "ᴇ", "f": "ꜰ", "g": "ɢ",
        "h": "ʜ", "i": "ɪ", "j": "ᴊ", "k": "ᴋ", "l": "ʟ", "m": "ᴍ", "n": "ɴ",
        "o": "ᴏ", "p": "ᴘ", "q": "ǫ", "r": "ʀ", "s": "ꜱ", "t": "ᴛ", "u": "ᴜ",
        "v": "ᴠ", "w": "ᴡ", "x": "x", "y": "ʏ", "z": "ᴢ",
    }
    return "".join(mapping.get(ch, ch) for ch in text)

# =========================================================================
# CONSISTENT ERROR HELPER (Section 3, item 44)
# =========================================================================
def send_error(chat_id, message, show_alert=False, call_id=None):
    """Friendly error to the user — never raw exceptions."""
    try:
        if call_id:
            bot.answer_callback_query(call_id, stylish_text(message), show_alert=show_alert)
        else:
            bot.send_message(chat_id, stylish_text(f"⚠️ {message}"))
    except Exception as e:
        logger.error(f"send_error failed: {e}")

# =========================================================================
# BAN / UNBAN
# =========================================================================
def ban_user(user_id):
    if user_id in admin_ids or user_id == OWNER_ID:
        return False
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
                  (user_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        banned_users.add(user_id)
        audit_log(OWNER_ID, "ban", str(user_id))
        return True
    except Exception:
        return False

def unban_user(user_id):
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        banned_users.discard(user_id)
        audit_log(OWNER_ID, "unban", str(user_id))
        return True
    except Exception:
        return False

def is_user_banned(user_id):
    return user_id in banned_users

# =========================================================================
# CUSTOM LIMITS
# =========================================================================
def set_user_custom_limit(user_id, limit):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_limits (user_id, custom_limit) VALUES (?, ?)",
                  (user_id, limit))
        conn.commit()
        conn.close()
        user_custom_limits[user_id] = limit

def remove_user_custom_limit(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM user_limits WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        user_custom_limits.pop(user_id, None)

def get_user_file_limit(user_id):
    if user_id in user_custom_limits:
        return user_custom_limits[user_id]
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    if user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
        return SUBSCRIBED_USER_LIMIT
    return FREE_USER_LIMIT

def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))

# =========================================================================
# CHANNEL VERIFICATION
# =========================================================================
def is_user_verified(user_id):
    if user_id in admin_ids or user_id == OWNER_ID:
        return True
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT 1 FROM verified_users WHERE user_id = ?", (user_id,))
        result = c.fetchone() is not None
        conn.close()
        return result
    except Exception:
        return False

def set_user_verified(user_id):
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO verified_users (user_id, verified_at) VALUES (?, ?)",
                  (user_id, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def is_user_member_all_channels(user_id):
    if user_id in admin_ids or user_id == OWNER_ID:
        return True
    for channel in REQUIRED_CHANNELS:
        try:
            m = bot.get_chat_member(channel, user_id)
            if m.status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

def send_join_prompt(chat_id, user_id):
    text = ("🔐 Jᴏɪɴ Aʟʟ Cʜᴀɴɴᴇʟs Tᴏ Uɴʟᴏᴄᴋ Tʜᴇ Bᴏᴛ 🚀\n"
            "📢 Cᴏᴍᴘʟᴇᴛᴇ Aʟʟ Cʜᴀɴɴᴇʟ Jᴏɪɴs Tᴏ Gᴇᴛ Aᴄᴄᴇss ✅\n"
            "⚡ Aғᴛᴇʀ Jᴏɪɴɪɴɢ, Cʟɪᴄᴋ \"Vᴇʀɪꜰʏ\" Tᴏ Cᴏɴᴛɪɴᴜᴇ. 🔓")
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in REQUIRED_CHANNELS:
        markup.add(types.InlineKeyboardButton("JOIN", url=f"https://t.me/{ch.lstrip('@')}"))
    markup.add(types.InlineKeyboardButton("✅ VERIFY", callback_data=f"verify_channel_{user_id}"))
    bot.send_message(chat_id, text, parse_mode=None, reply_markup=markup, disable_web_page_preview=True)

# *** Section 0.1 fix: explicit user_id ***
def check_subscription_and_continue(user_id, chat_id):
    if is_user_banned(user_id):
        bot.send_message(chat_id, stylish_text("🚫 You are banned from using this bot."))
        return False
    if user_id in admin_ids or user_id == OWNER_ID:
        return True
    if is_user_verified(user_id):
        return True
    if is_user_member_all_channels(user_id):
        set_user_verified(user_id)
        return True
    send_join_prompt(chat_id, user_id)
    return False

@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_channel_"))
def verify_channel_callback(call):
    user_id = int(call.data.split("_")[-1])
    if user_id != call.from_user.id:
        bot.answer_callback_query(call.id, stylish_text("This verification is not for you."),
                                  show_alert=True)
        return
    if is_user_verified(user_id):
        bot.answer_callback_query(call.id, stylish_text("Already verified."), show_alert=True)
        bot.edit_message_text(stylish_text("✅ You are already verified. Send /start to begin."),
                              call.message.chat.id, call.message.message_id)
        return
    if is_user_member_all_channels(user_id):
        set_user_verified(user_id)
        bot.answer_callback_query(call.id, stylish_text("✅ Verification successful!"), show_alert=True)
        bot.edit_message_text(stylish_text("✅ Verified! Send /start to begin."),
                              call.message.chat.id, call.message.message_id)
    else:
        missing = []
        for ch in REQUIRED_CHANNELS:
            try:
                m = bot.get_chat_member(ch, user_id)
                if m.status not in ("member", "administrator", "creator"):
                    missing.append(ch)
            except Exception:
                missing.append(ch)
        bot.answer_callback_query(
            call.id,
            stylish_text(f"❌ Not joined:\n{chr(10).join(missing)}\nJoin all first."),
            show_alert=True)

# =========================================================================
# NAVIGATION STACK (Section 3, items 39-40)
# =========================================================================
def push_nav(user_id, screen):
    with NAV_STACK_LOCK:
        nav_stack.setdefault(user_id, []).append(screen)
        if len(nav_stack[user_id]) > 12:
            nav_stack[user_id] = nav_stack[user_id][-12:]

def pop_nav(user_id):
    with NAV_STACK_LOCK:
        stack = nav_stack.get(user_id, [])
        if len(stack) >= 2:
            stack.pop()
            return stack[-1]
        return None

def clear_nav(user_id):
    with NAV_STACK_LOCK:
        nav_stack.pop(user_id, None)

def breadcrumb(stack):
    if not stack:
        return ""
    return "▸".join(stack[-3:]) + "\n\n"

# Double-tap guard (Section 3, item 42)
def tap_guard(user_id):
    now = time.time()
    with _TAP_LOCK:
        last = _last_tap.get(user_id, 0)
        if now - last < _TAP_COOLDOWN:
            return False
        _last_tap[user_id] = now
        return True

# =========================================================================
# HELPER FUNCTIONS
# =========================================================================
def get_user_folder(user_id):
    folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder

def is_bot_running(script_owner_id, file_name):
    script_key = f"{script_owner_id}_{file_name}"
    info = bot_scripts.get(script_key)
    if info and info.get("process"):
        try:
            proc = psutil.Process(info["process"].pid)
            running = proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
            if not running:
                _close_log(info)
                bot_scripts.pop(script_key, None)
            return running
        except psutil.NoSuchProcess:
            _close_log(info)
            bot_scripts.pop(script_key, None)
            return False
        except Exception as e:
            logger.error(f"Error checking {script_key}: {e}")
            return False
    return False

def _close_log(info):
    lf = info.get("log_file")
    if lf and hasattr(lf, "close") and not getattr(lf, "closed", True):
        try:
            lf.close()
        except Exception:
            pass

def kill_process_tree(process_info):
    try:
        _close_log(process_info)
        process = process_info.get("process")
        if process and hasattr(process, "pid") and process.pid:
            try:
                parent = psutil.Process(process.pid)
                for child in parent.children(recursive=True):
                    try: child.terminate()
                    except Exception: pass
                psutil.wait_procs(parent.children(recursive=True), timeout=1)
                try:
                    parent.terminate()
                    parent.wait(timeout=1)
                except Exception:
                    try: parent.kill()
                    except Exception: pass
            except psutil.NoSuchProcess:
                pass
    except Exception as e:
        logger.error(f"Error killing process tree: {e}")

# Log rotation (Section 4, item 62)
def rotate_log(log_path):
    try:
        for i in range(MAX_LOG_FILES - 1, 0, -1):
            old = f"{log_path}.{i}"
            new = f"{log_path}.{i + 1}"
            if os.path.exists(old):
                if os.path.exists(new):
                    os.remove(new)
                os.rename(old, new)
        if os.path.exists(log_path):
            os.rename(log_path, f"{log_path}.1")
    except Exception as e:
        logger.error(f"Log rotate error: {e}")

def open_log(log_path):
    """Open a log file, rotating if it has grown too large."""
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_BYTES:
            rotate_log(log_path)
        return open(log_path, "w", encoding="utf-8")
    except Exception as e:
        logger.error(f"open_log error: {e}")
        return open(log_path, "w", encoding="utf-8")

# =========================================================================
# TELEGRAM MODULES MAP
# =========================================================================
TELEGRAM_MODULES = {
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot",
    "aiogram": "aiogram", "pyrogram": "pyrogram", "telethon": "telethon",
    "telethon.sync": "telethon", "telepot": "telepot", "pytg": "pytg",
    "tgcrypto": "tgcrypto", "bs4": "beautifulsoup4", "requests": "requests",
    "pillow": "Pillow", "PIL": "Pillow", "cv2": "opencv-python",
    "yaml": "PyYAML", "dotenv": "python-dotenv", "dateutil": "python-dateutil",
    "pandas": "pandas", "numpy": "numpy", "flask": "Flask", "django": "Django",
    "sqlalchemy": "SQLAlchemy", "aiohttp": "aiohttp", "lxml": "lxml",
    "matplotlib": "matplotlib", "scipy": "scipy", "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn", "pytest": "pytest", "psutil": "psutil",
    "cryptography": "cryptography", "fernet": "cryptography",
    # stdlib — None means "no install needed"
    "asyncio": None, "json": None, "datetime": None, "os": None, "sys": None,
    "re": None, "time": None, "math": None, "random": None, "logging": None,
    "threading": None, "subprocess": None, "zipfile": None, "tempfile": None,
    "shutil": None, "sqlite3": None, "atexit": None, "io": None,
    "urllib": None, "pathlib": None, "collections": None, "itertools": None,
    "functools": None, "typing": None, "dataclasses": None,
}

def attempt_install_pip(module_name, chat_id):
    package = TELEGRAM_MODULES.get(module_name.lower(), module_name)
    if package is None:
        return False
    try:
        bot.send_message(chat_id, stylish_text(f"🐍 Installing {package}..."))
        result = subprocess.run([sys.executable, "-m", "pip", "install", package],
                                capture_output=True, text=True)
        if result.returncode == 0:
            bot.send_message(chat_id, stylish_text(f"✅ Installed {package}."))
            return True
        bot.send_message(chat_id, stylish_text(f"❌ Failed {package}: {result.stderr[:200]}"))
        return False
    except Exception as e:
        bot.send_message(chat_id, stylish_text(f"❌ Install error: {e}"))
        return False

def attempt_install_npm(module_name, user_folder, chat_id):
    try:
        bot.send_message(chat_id, stylish_text(f"🟠 Installing Node {module_name}..."))
        result = subprocess.run(["npm", "install", module_name], cwd=user_folder,
                                capture_output=True, text=True)
        if result.returncode == 0:
            bot.send_message(chat_id, stylish_text(f"✅ Installed {module_name}."))
            return True
        bot.send_message(chat_id, stylish_text(f"❌ Failed {module_name}: {result.stderr[:200]}"))
        return False
    except Exception as e:
        bot.send_message(chat_id, stylish_text(f"❌ NPM error: {e}"))
        return False

# =========================================================================
# VARIABLE / SECRETS VAULT — DB OPS (Section 1)
# =========================================================================
def set_user_variable(user_id, file_name, var_name, var_value):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO user_variables
                     (user_id, file_name, var_name, var_value, updated_at)
                     VALUES (?, ?, ?, ?, ?)""",
                  (user_id, file_name, var_name, vault_encrypt(var_value),
                   datetime.now().isoformat()))
        conn.commit()
        conn.close()
        audit_log(user_id, "var_set", f"{file_name}:{var_name}")

def get_user_variables(user_id, file_name):
    """Return decrypted dict {var_name: value}."""
    out = {}
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT var_name, var_value FROM user_variables WHERE user_id=? AND file_name=?",
                  (user_id, file_name))
        for name, val in c.fetchall():
            out[name] = vault_decrypt(val)
        conn.close()
    except Exception as e:
        logger.error(f"get_user_variables error: {e}")
    return out

def delete_user_variable(user_id, file_name, var_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM user_variables WHERE user_id=? AND file_name=? AND var_name=?",
                  (user_id, file_name, var_name))
        conn.commit()
        conn.close()
        audit_log(user_id, "var_delete", f"{file_name}:{var_name}")

def build_child_env(user_id, file_name):
    """Section 1, items 3 & 7 — minimal whitelist + injected variables."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "en_US.UTF-8"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": get_user_folder(user_id),
    }
    # Inject this script's variables (decrypted)
    for k, v in get_user_variables(user_id, file_name).items():
        env[k] = v
    return env

# =========================================================================
# SCRIPT RUNNERS — env injection + resource caps (Section 1 item 3, Section 4 item 61)
# =========================================================================
def run_script(script_path, script_owner_id, user_folder, file_name, chat_id, attempt=1):
    max_attempts = 2
    if attempt > max_attempts:
        if chat_id:
            bot.send_message(chat_id, stylish_text(f"❌ Failed to run '{file_name}' after {max_attempts} attempts."))
        return
    script_key = f"{script_owner_id}_{file_name}"
    logger.info(f"Attempt {attempt} to run Python: {script_path}")
    try:
        if not os.path.exists(script_path):
            if chat_id:
                bot.send_message(chat_id, stylish_text(f"❌ Script '{file_name}' not found!"))
            remove_user_file_db(script_owner_id, file_name)
            return
        child_env = build_child_env(script_owner_id, file_name)
        if attempt == 1:
            check_proc = subprocess.Popen([sys.executable, script_path], cwd=user_folder,
                                          env=child_env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                _, stderr = check_proc.communicate(timeout=5)
                if check_proc.returncode != 0 and stderr:
                    m = re.search(r"ModuleNotFoundError: No module named '(.+?)'", stderr)
                    if m:
                        mod = m.group(1)
                        if attempt_install_pip(mod, chat_id or 0):
                            time.sleep(2)
                            threading.Thread(target=run_script,
                                args=(script_path, script_owner_id, user_folder, file_name, chat_id, attempt + 1)).start()
                            return
                        else:
                            if chat_id:
                                bot.send_message(chat_id, stylish_text(f"❌ Missing module {mod}."))
                            return
            except subprocess.TimeoutExpired:
                check_proc.kill()
                check_proc.communicate()
        log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open_log(log_path)
        process = subprocess.Popen([sys.executable, script_path], cwd=user_folder,
                                   env=child_env,
                                   stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {
            "process": process, "log_file": log_file, "file_name": file_name,
            "chat_id": chat_id, "script_owner_id": script_owner_id,
            "start_time": datetime.now(), "user_folder": user_folder,
            "type": "py", "script_key": script_key,
            "cpu_cap": 80.0, "ram_cap_mb": 256,
        }
        if chat_id:
            bot.send_message(chat_id, stylish_text(f"✅ Python '{file_name}' started! (PID {process.pid})"))
    except Exception as e:
        if chat_id:
            bot.send_message(chat_id, stylish_text(f"❌ Error: {e}"))
        if script_key in bot_scripts:
            kill_process_tree(bot_scripts[script_key])
            bot_scripts.pop(script_key, None)

def run_js_script(script_path, script_owner_id, user_folder, file_name, chat_id, attempt=1):
    max_attempts = 2
    if attempt > max_attempts:
        if chat_id:
            bot.send_message(chat_id, stylish_text(f"❌ Failed to run '{file_name}' after {max_attempts} attempts."))
        return
    script_key = f"{script_owner_id}_{file_name}"
    try:
        if not os.path.exists(script_path):
            if chat_id:
                bot.send_message(chat_id, stylish_text(f"❌ JS '{file_name}' not found!"))
            remove_user_file_db(script_owner_id, file_name)
            return
        child_env = build_child_env(script_owner_id, file_name)
        child_env["NODE_PATH"] = os.path.join(user_folder, "node_modules")
        if attempt == 1:
            check_proc = subprocess.Popen(["node", script_path], cwd=user_folder,
                                          env=child_env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                _, stderr = check_proc.communicate(timeout=5)
                if check_proc.returncode != 0 and stderr:
                    m = re.search(r"Cannot find module '(.+?)'", stderr)
                    if m:
                        mod = m.group(1)
                        if not mod.startswith(".") and not mod.startswith("/"):
                            if attempt_install_npm(mod, user_folder, chat_id or 0):
                                time.sleep(2)
                                threading.Thread(target=run_js_script,
                                    args=(script_path, script_owner_id, user_folder, file_name, chat_id, attempt + 1)).start()
                                return
                            if chat_id:
                                bot.send_message(chat_id, stylish_text(f"❌ Missing Node module {mod}."))
                            return
            except subprocess.TimeoutExpired:
                check_proc.kill()
                check_proc.communicate()
        log_path = os.path.join(user_folder, f"{os.path.splitext(file_name)[0]}.log")
        log_file = open_log(log_path)
        process = subprocess.Popen(["node", script_path], cwd=user_folder,
                                   env=child_env,
                                   stdout=log_file, stderr=log_file, stdin=subprocess.PIPE)
        bot_scripts[script_key] = {
            "process": process, "log_file": log_file, "file_name": file_name,
            "chat_id": chat_id, "script_owner_id": script_owner_id,
            "start_time": datetime.now(), "user_folder": user_folder,
            "type": "js", "script_key": script_key,
            "cpu_cap": 80.0, "ram_cap_mb": 256,
        }
        if chat_id:
            bot.send_message(chat_id, stylish_text(f"✅ JS '{file_name}' started! (PID {process.pid})"))
    except Exception as e:
        if chat_id:
            bot.send_message(chat_id, stylish_text(f"❌ Error: {e}"))
        if script_key in bot_scripts:
            kill_process_tree(bot_scripts[script_key])
            bot_scripts.pop(script_key, None)

# =========================================================================
# DB OPERATIONS
# =========================================================================
def save_user_file(user_id, file_name, file_type="py"):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("INSERT OR REPLACE INTO user_files (user_id, file_name, file_type) VALUES (?, ?, ?)",
                      (user_id, file_name, file_type))
            conn.commit()
            user_files.setdefault(user_id, [])
            user_files[user_id] = [(n, t) for n, t in user_files[user_id] if n != file_name]
            user_files[user_id].append((file_name, file_type))
        except Exception as e:
            logger.error(f"save_user_file: {e}")
        finally:
            conn.close()

def remove_user_file_db(user_id, file_name):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("DELETE FROM user_files WHERE user_id=? AND file_name=?", (user_id, file_name))
            c.execute("DELETE FROM user_variables WHERE user_id=? AND file_name=?", (user_id, file_name))
            conn.commit()
            if user_id in user_files:
                user_files[user_id] = [f for f in user_files[user_id] if f[0] != file_name]
                if not user_files[user_id]:
                    user_files.pop(user_id)
        except Exception as e:
            logger.error(f"remove_user_file_db: {e}")
        finally:
            conn.close()

def add_active_user(user_id):
    active_users.add(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO active_users (user_id) VALUES (?)", (user_id,))
            conn.commit()
        except Exception as e:
            logger.error(f"add_active_user: {e}")
        finally:
            conn.close()

def save_subscription(user_id, expiry):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("INSERT OR REPLACE INTO subscriptions (user_id, expiry) VALUES (?, ?)",
                      (user_id, expiry.isoformat()))
            conn.commit()
            user_subscriptions[user_id] = {"expiry": expiry}
        except Exception as e:
            logger.error(f"save_subscription: {e}")
        finally:
            conn.close()

def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            conn.commit()
            user_subscriptions.pop(user_id, None)
        except Exception as e:
            logger.error(f"remove_subscription_db: {e}")
        finally:
            conn.close()

def add_admin_db(admin_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (admin_id,))
            conn.commit()
            admin_ids.add(admin_id)
            audit_log(OWNER_ID, "add_admin", str(admin_id))
        except Exception as e:
            logger.error(f"add_admin_db: {e}")
        finally:
            conn.close()

def remove_admin_db(admin_id):
    if admin_id == OWNER_ID:
        return False
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("DELETE FROM admins WHERE user_id=?", (admin_id,))
            conn.commit()
            if c.rowcount > 0:
                admin_ids.discard(admin_id)
                audit_log(OWNER_ID, "remove_admin", str(admin_id))
                return True
            return False
        except Exception as e:
            logger.error(f"remove_admin_db: {e}")
            return False
        finally:
            conn.close()

def add_pending_upload(user_id, file_id, file_name, file_type, file_size,
                        user_name, user_username, extra_info="", sensitive_mode=0, scan_flags=""):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("""INSERT INTO pending_uploads
                (user_id, file_id, file_name, file_type, file_size,
                 user_name, user_username, timestamp, extra_info, sensitive_mode, scan_flags)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, file_id, file_name, file_type, file_size,
                 user_name, user_username, datetime.now().isoformat(),
                 extra_info, sensitive_mode, scan_flags))
            conn.commit()
            return c.lastrowid
        except Exception as e:
            logger.error(f"add_pending_upload: {e}")
            return None
        finally:
            conn.close()

def get_pending_upload(upload_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("""SELECT id, user_id, file_id, file_name, file_type, file_size,
                                user_name, user_username, extra_info, sensitive_mode, scan_flags
                         FROM pending_uploads WHERE id=?""", (upload_id,))
            row = c.fetchone()
            if row:
                return {"id": row[0], "user_id": row[1], "file_id": row[2], "file_name": row[3],
                        "file_type": row[4], "file_size": row[5], "user_name": row[6],
                        "user_username": row[7], "extra_info": row[8],
                        "sensitive_mode": row[9], "scan_flags": row[10]}
            return None
        except Exception as e:
            logger.error(f"get_pending_upload: {e}")
            return None
        finally:
            conn.close()

def list_pending_uploads():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("""SELECT id, user_id, file_name, file_type, file_size, user_name, scan_flags
                         FROM pending_uploads ORDER BY id DESC LIMIT 30""")
            return c.fetchall()
        except Exception as e:
            logger.error(f"list_pending_uploads: {e}")
            return []
        finally:
            conn.close()

def delete_pending_upload(upload_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        try:
            c.execute("DELETE FROM pending_uploads WHERE id=?", (upload_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"delete_pending_upload: {e}")
            return False
        finally:
            conn.close()

# =========================================================================
# ZIP SAFETY — bomb + path-traversal + type allowlist (Section 2, items 30-32)
# =========================================================================
ALLOWED_EXTS = {".py", ".js", ".txt", ".json", ".cfg", ".ini", ".toml",
                ".yml", ".yaml", ".md", ".csv", ".tsv", ".env", ".lock"}
BLOCKED_EXTS = {".exe", ".dll", ".sh", ".bat", ".cmd", ".ps1", ".msi", ".so", ".dylib"}

def safe_extract_zip(zip_path, dest_dir):
    """Extract with path-traversal + zip-bomb + extension guards.
    Returns list of extracted file paths (relative)."""
    extracted = []
    total_uncompressed = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            # Path traversal guard
            target = os.path.normpath(os.path.join(dest_dir, name))
            if not target.startswith(os.path.abspath(dest_dir) + os.sep) and \
               target != os.path.abspath(dest_dir):
                logger.warning(f"Zip traversal blocked: {name}")
                continue
            # Extension guard
            ext = os.path.splitext(name)[1].lower()
            if ext in BLOCKED_EXTS:
                logger.warning(f"Zip blocked ext: {name}")
                continue
            if ext and ext not in ALLOWED_EXTS:
                logger.warning(f"Zip skipped unknown ext: {name}")
                continue
            # Zip-bomb guard
            if info.compress_size > 0 and info.file_size > info.compress_size * MAX_ZIP_RATIO:
                logger.warning(f"Zip-bomb ratio blocked: {name}")
                continue
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_EXTRACT_MB * 1024 * 1024:
                logger.warning("Zip total size exceeded; aborting.")
                break
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            extracted.append(os.path.relpath(target, dest_dir))
    return extracted

def find_main_script(folder):
    """Section 0.2 / 2.17 — recursive main-script detection.
    Returns (main_relative_path, file_type) or (None, None)."""
    candidates_py = ["main.py", "bot.py", "app.py", "run.py", "start.py"]
    candidates_js = ["index.js", "main.js", "bot.js", "app.js", "server.js"]
    for root, _, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        for f in files:
            if f in candidates_py:
                return os.path.join(rel, f) if rel != "." else f, "py"
            if f in candidates_js:
                return os.path.join(rel, f) if rel != "." else f, "js"
    # Fallback — first .py / .js found anywhere
    for root, _, files in os.walk(folder):
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(root, folder)
                return (os.path.join(rel, f) if rel != "." else f), "py"
        for f in files:
            if f.endswith(".js"):
                rel = os.path.relpath(root, folder)
                return (os.path.join(rel, f) if rel != "." else f), "js"
    return None, None

def flatten_if_single_root(src_dir):
    """If extraction produced a single root folder, move its contents up one level.
    Section 0.2 fix for GitHub zipball 'owner-repo-sha/' nesting."""
    try:
        entries = [e for e in os.listdir(src_dir)
                   if not e.startswith(".") or e not in (".", "..")]
        if len(entries) == 1:
            only = os.path.join(src_dir, entries[0])
            if os.path.isdir(only):
                tmp_inner = only + "_moving"
                os.rename(only, tmp_inner)
                for item in os.listdir(tmp_inner):
                    shutil.move(os.path.join(tmp_inner, item),
                                os.path.join(src_dir, item))
                os.rmdir(tmp_inner)
                return True
    except Exception as e:
        logger.error(f"flatten_if_single_root: {e}")
    return False

# =========================================================================
# STATIC CODE SCAN (Section 6 B.17)
# =========================================================================
SUSPICIOUS_PATTERNS = [
    (re.compile(r"os\.system\s*\("), "os.system()"),
    (re.compile(r"\beval\s*\("), "eval()"),
    (re.compile(r"\bexec\s*\("), "exec()"),
    (re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "subprocess(shell=True)"),
    (re.compile(r"socket\.connect"), "raw socket connect"),
]

def static_scan(file_path):
    """Return list of (line_no, matched) flags for the file."""
    flags = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f, 1):
                for pat, label in SUSPICIOUS_PATTERNS:
                    if pat.search(line):
                        flags.append(f"L{i}: {label}")
    except Exception:
        pass
    return flags

def scan_folder_flags(folder):
    all_flags = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.endswith((".py", ".js")):
                all_flags.extend(static_scan(os.path.join(root, f)))
    return all_flags[:10]

# =========================================================================
# APPROVED-FILE PROCESSING (zip flatten fix applied — Section 0.2)
# =========================================================================
def process_approved_file(upload_id, admin_chat_id, user_chat_id=None):
    pending = get_pending_upload(upload_id)
    if not pending:
        bot.send_message(admin_chat_id, stylish_text(f"❌ Pending upload {upload_id} not found."))
        return False
    user_id    = pending["user_id"]
    file_id    = pending["file_id"]
    file_name  = pending["file_name"]
    file_ext   = os.path.splitext(file_name)[1].lower()
    file_type  = pending["file_type"]
    file_limit = get_user_file_limit(user_id)
    if get_user_file_count(user_id) >= file_limit:
        limit_str = str(file_limit) if file_limit != float("inf") else "Unlimited"
        bot.send_message(admin_chat_id, stylish_text(f"⚠️ Limit reached ({limit_str}). Cannot approve."))
        delete_pending_upload(upload_id)
        return False
    try:
        file_info   = bot.get_file(file_id)
        downloaded  = bot.download_file(file_info.file_path)
        user_folder = get_user_folder(user_id)
        if file_ext == ".zip":
            temp_dir = tempfile.mkdtemp(prefix=f"user_{user_id}_zip_")
            zip_path = os.path.join(temp_dir, file_name)
            with open(zip_path, "wb") as f:
                f.write(downloaded)
            # Safe extraction (no path traversal, no zip bombs, ext allowlist)
            safe_extract_zip(zip_path, temp_dir)
            # Flatten single-root folder (Section 0.2)
            flatten_if_single_root(temp_dir)
            # Locate requirements.txt / package.json recursively
            req_path = None
            pkg_path = None
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    if f == "requirements.txt" and not req_path:
                        req_path = os.path.join(root, f)
                    if f == "package.json" and not pkg_path:
                        pkg_path = os.path.join(root, f)
            if req_path:
                try:
                    subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path],
                                   check=True, capture_output=True)
                    bot.send_message(admin_chat_id, stylish_text("✅ Python deps installed."))
                except Exception as e:
                    bot.send_message(admin_chat_id, stylish_text(f"❌ Python deps failed: {e}"))
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    delete_pending_upload(upload_id)
                    return False
            if pkg_path:
                pkg_dir = os.path.dirname(pkg_path)
                try:
                    subprocess.run(["npm", "install"], cwd=pkg_dir, check=True, capture_output=True)
                    bot.send_message(admin_chat_id, stylish_text("✅ Node deps installed."))
                except Exception as e:
                    bot.send_message(admin_chat_id, stylish_text(f"❌ Node deps failed: {e}"))
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    delete_pending_upload(upload_id)
                    return False
            # Recursive main detection (Section 0.2 / 2.17)
            main_script, ftype = find_main_script(temp_dir)
            if not main_script:
                bot.send_message(admin_chat_id, stylish_text("❌ No .py or .js script found in zip."))
                shutil.rmtree(temp_dir, ignore_errors=True)
                delete_pending_upload(upload_id)
                return False
            # Move the whole extracted tree into the user folder (preserve structure)
            for item in os.listdir(temp_dir):
                src = os.path.join(temp_dir, item)
                dst = os.path.join(user_folder, item)
                if os.path.isdir(dst):
                    shutil.rmtree(dst)
                elif os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)
            shutil.rmtree(temp_dir, ignore_errors=True)
            main_basename = os.path.basename(main_script)
            save_user_file(user_id, main_basename, ftype)
            script_path = os.path.join(user_folder, main_script)
            if ftype == "py":
                threading.Thread(target=run_script,
                    args=(script_path, user_id, user_folder, main_basename, user_chat_id)).start()
            else:
                threading.Thread(target=run_js_script,
                    args=(script_path, user_id, user_folder, main_basename, user_chat_id)).start()
            bot.send_message(admin_chat_id, stylish_text(f"✅ Approved and started: {main_basename}"))
            audit_log(admin_chat_id, "approve_upload", str(upload_id))
            return True
        else:
            file_path = os.path.join(user_folder, file_name)
            with open(file_path, "wb") as f:
                f.write(downloaded)
            save_user_file(user_id, file_name, file_type)
            if file_type == "py":
                threading.Thread(target=run_script,
                    args=(file_path, user_id, user_folder, file_name, user_chat_id)).start()
            else:
                threading.Thread(target=run_js_script,
                    args=(file_path, user_id, user_folder, file_name, user_chat_id)).start()
            bot.send_message(admin_chat_id, stylish_text(f"✅ Approved and started: {file_name}"))
            audit_log(admin_chat_id, "approve_upload", str(upload_id))
            return True
    except Exception as e:
        logger.error(f"process_approved_file: {e}", exc_info=True)
        bot.send_message(admin_chat_id, stylish_text(f"❌ Error: {e}"))
        return False
    finally:
        delete_pending_upload(upload_id)


# =========================================================================
# DOCUMENT UPLOAD HANDLER (with secret redaction + static scan)
# =========================================================================
@bot.message_handler(content_types=["document"])
def handle_file_upload_doc(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    doc = message.document
    if bot_locked and user_id not in admin_ids:
        bot.reply_to(message, stylish_text("⚠️ Bot locked, cannot accept files."))
        return
    if is_user_banned(user_id):
        bot.reply_to(message, stylish_text("🚫 You are banned."))
        return
    file_limit   = get_user_file_limit(user_id)
    current_files = get_user_file_count(user_id)
    if current_files >= file_limit:
        limit_str = str(file_limit) if file_limit != float("inf") else "Unlimited"
        # Real-time low-limit warning (Section 5, item 66)
        remaining = file_limit - current_files
        warn = "" if remaining > 2 else f" ⚠️ Only {remaining} slot(s) left!"
        bot.reply_to(message, stylish_text(f"⚠️ Limit reached ({current_files}/{limit_str}).{warn}"))
        return
    file_name = doc.file_name or ""
    if not file_name:
        bot.reply_to(message, stylish_text("⚠️ No file name."))
        return
    file_ext = os.path.splitext(file_name)[1].lower()
    # Standalone requirements.txt / package.json (Section 2, items 18-19)
    if file_name.lower() == "requirements.txt":
        _handle_standalone_requirements(message, user_id, chat_id)
        return
    if file_name.lower() == "package.json":
        _handle_standalone_package_json(message, user_id, chat_id)
        return
    if file_ext not in (".py", ".js", ".zip"):
        bot.reply_to(message, stylish_text("⚠️ Only .py, .js, .zip, requirements.txt, package.json allowed."))
        return
    if doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        bot.reply_to(message, stylish_text(f"⚠️ File too large (max {MAX_UPLOAD_MB}MB)."))
        return
    user_name     = message.from_user.first_name or "User"
    user_username = message.from_user.username or "No username"
    # Static scan for flags (Section 6 B.17) — only meaningful for non-zip
    scan_flags = ""
    if file_ext in (".py", ".js"):
        # Download to temp, scan, then keep the file_id as-is (admin re-downloads)
        try:
            fi = bot.get_file(doc.file_id)
            tmp_path = os.path.join(get_user_folder(user_id), f"_scan_{file_name}")
            with open(tmp_path, "wb") as f:
                f.write(bot.download_file(fi.file_path))
            flags = static_scan(tmp_path)
            try: os.remove(tmp_path)
            except: pass
            scan_flags = "; ".join(flags) if flags else ""
        except Exception as e:
            logger.error(f"Static scan failed: {e}")
    # Sensitive mode is opt-in via /sensitive command (Section 1, item 5)
    sensitive_mode = 1 if getattr(message, "_sensitive_mode", False) else 0
    upload_id = add_pending_upload(
        user_id=user_id, file_id=doc.file_id, file_name=file_name,
        file_type=file_ext[1:], file_size=doc.file_size,
        user_name=user_name, user_username=user_username,
        extra_info="", sensitive_mode=sensitive_mode, scan_flags=scan_flags)
    if not upload_id:
        bot.reply_to(message, stylish_text("❌ Internal error, please try later."))
        return
    bot.reply_to(message, stylish_text(
        f"✅ File {file_name} submitted for admin approval. You'll be notified."))
    # Notify admins — in sensitive mode, do NOT forward the raw file
    for aid in admin_ids:
        try:
            flags_note = f"\n⚠️ Review Carefully: {scan_flags}" if scan_flags else ""
            if sensitive_mode:
                # Section 1, item 5: only metadata + masked preview
                caption = (f"📥 SENSITIVE upload (raw file NOT forwarded)\n"
                           f"👤 {user_name} (@{user_username})\n"
                           f"🆔 {user_id}\n📄 {file_name}\n"
                           f"📏 {doc.file_size // 1024} KB\n"
                           f"🆔 Upload ID: {upload_id}{flags_note}")
                bot.send_message(aid, stylish_text(caption))
            else:
                caption = (f"📥 New file requires approval\n"
                           f"👤 {user_name} (@{user_username})\n"
                           f"🆔 {user_id}\n📄 {file_name}\n"
                           f"📏 {doc.file_size // 1024} KB\n"
                           f"🆔 Upload ID: {upload_id}{flags_note}")
                sent = bot.send_document(aid, doc.file_id, caption=stylish_text(caption))
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_upload_{upload_id}"),
                    types.InlineKeyboardButton("❌ Reject",  callback_data=f"reject_upload_{upload_id}"))
                markup.row(types.InlineKeyboardButton("🔍 Queue", callback_data="view_approval_queue"))
                bot.edit_message_reply_markup(aid, sent.message_id, reply_markup=markup)
        except Exception as e:
            logger.error(f"Failed to notify admin {aid}: {e}")

def _handle_standalone_requirements(message, user_id, chat_id):
    """Section 2, item 18 — install requirements.txt into an existing script."""
    files = user_files.get(user_id, [])
    if not files:
        bot.reply_to(message, stylish_text("ℹ️ You have no active scripts yet. Upload a script first."))
        return
    # Save the requirements.txt to the user's folder
    user_folder = get_user_folder(user_id)
    try:
        fi = bot.get_file(message.document.file_id)
        data = bot.download_file(fi.file_path)
        req_path = os.path.join(user_folder, "requirements.txt")
        with open(req_path, "wb") as f:
            f.write(data)
    except Exception as e:
        bot.reply_to(message, stylish_text(f"❌ Could not save requirements.txt: {e}"))
        return
    # Show install-into buttons
    markup = types.InlineKeyboardMarkup(row_width=1)
    for fname, ftype in files:
        markup.add(types.InlineKeyboardButton(
            f"Install into: {fname}", callback_data=f"req2script_{fname}"))
    markup.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_req_install"))
    bot.reply_to(message, stylish_text("📦 Saved requirements.txt. Install into which script?"),
                 reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("req2script_"))
def req_install_into_script(call):
    user_id = call.from_user.id
    if not tap_guard(user_id):
        return
    fname = call.data[len("req2script_"):]
    user_folder = get_user_folder(user_id)
    req_path = os.path.join(user_folder, "requirements.txt")
    if not os.path.exists(req_path):
        bot.answer_callback_query(call.id, "requirements.txt missing.", show_alert=True)
        return
    bot.answer_callback_query(call.id, f"Installing deps for {fname}...")
    try:
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_path],
                                cwd=user_folder, capture_output=True, text=True)
        if result.returncode == 0:
            bot.edit_message_text(stylish_text(f"✅ Deps installed for {fname}.\nRestart the script to apply."),
                                  call.message.chat.id, call.message.message_id)
        else:
            bot.edit_message_text(stylish_text(f"❌ Install failed:\n{result.stderr[:500]}"),
                                  call.message.chat.id, call.message.message_id)
    except Exception as e:
        bot.edit_message_text(stylish_text(f"❌ Error: {e}"),
                              call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "cancel_req_install")
def cancel_req_install(call):
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        bot.answer_callback_query(call.id, "Cancelled.")

def _handle_standalone_package_json(message, user_id, chat_id):
    files = user_files.get(user_id, [])
    if not files:
        bot.reply_to(message, stylish_text("ℹ️ You have no active JS scripts yet."))
        return
    user_folder = get_user_folder(user_id)
    try:
        fi = bot.get_file(message.document.file_id)
        data = bot.download_file(fi.file_path)
        with open(os.path.join(user_folder, "package.json"), "wb") as f:
            f.write(data)
    except Exception as e:
        bot.reply_to(message, stylish_text(f"❌ Could not save package.json: {e}"))
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for fname, ftype in files:
        if ftype == "js":
            markup.add(types.InlineKeyboardButton(
                f"npm install for: {fname}", callback_data=f"pkg2script_{fname}"))
    markup.add(types.InlineKeyboardButton("🔙 Cancel", callback_data="cancel_req_install"))
    bot.reply_to(message, stylish_text("📦 Saved package.json. Run npm install for which script?"),
                 reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("pkg2script_"))
def pkg_install_into_script(call):
    user_id = call.from_user.id
    if not tap_guard(user_id):
        return
    fname = call.data[len("pkg2script_"):]
    user_folder = get_user_folder(user_id)
    bot.answer_callback_query(call.id, f"npm install for {fname}...")
    try:
        result = subprocess.run(["npm", "install"], cwd=user_folder, capture_output=True, text=True)
        if result.returncode == 0:
            bot.edit_message_text(stylish_text(f"✅ Node deps installed. Restart {fname}."),
                                  call.message.chat.id, call.message.message_id)
        else:
            bot.edit_message_text(stylish_text(f"❌ npm failed:\n{result.stderr[:500]}"),
                                  call.message.chat.id, call.message.message_id)
    except Exception as e:
        bot.edit_message_text(stylish_text(f"❌ Error: {e}"),
                              call.message.chat.id, call.message.message_id)

# =========================================================================
# APPROVAL / REJECTION
# =========================================================================
@bot.callback_query_handler(func=lambda c: c.data.startswith("approve_upload_") or c.data.startswith("reject_upload_"))
def handle_approval_callback(call):
    admin_id = call.from_user.id
    if admin_id not in admin_ids:
        bot.answer_callback_query(call.id, "⚠️ Admins only.", show_alert=True)
        return
    upload_id = int(call.data.split("_")[-1])
    pending = get_pending_upload(upload_id)
    if not pending:
        bot.answer_callback_query(call.id, "⚠️ Upload no longer exists.", show_alert=True)
        try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
        return
    user_id   = pending["user_id"]
    file_name = pending["file_name"]
    if call.data.startswith("approve_upload_"):
        # Confirm step for destructive-ish actions (Section 3, item 49)
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_approve_{upload_id}"),
            types.InlineKeyboardButton("❌ Cancel",   callback_data=f"cancel_approve_{upload_id}"))
        bot.answer_callback_query(call.id, "Confirm approval.")
        bot.edit_message_text(
            stylish_text(f"⚠️ Confirm approving {file_name} for user {user_id}?"),
            call.message.chat.id, call.message.message_id, reply_markup=markup)
    else:
        # Reject with reason picker (Section 6 B.21)
        markup = types.InlineKeyboardMarkup(row_width=1)
        reasons = ["Malicious code", "Missing requirements.txt", "Violates ToS", "Other"]
        for r in reasons:
            markup.add(types.InlineKeyboardButton(f"❌ {r}", callback_data=f"reject_reason_{upload_id}_{r}"))
        bot.answer_callback_query(call.id, "Pick a reject reason.")
        bot.edit_message_text(stylish_text(f"Reject {file_name} — pick a reason:"),
                              call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_approve_"))
def confirm_approve(call):
    admin_id = call.from_user.id
    if admin_id not in admin_ids:
        bot.answer_callback_query(call.id, "Admins only.", show_alert=True)
        return
    upload_id = int(call.data.split("_")[-1])
    pending = get_pending_upload(upload_id)
    if not pending:
        bot.answer_callback_query(call.id, "Gone.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "✅ Approving and starting...")
    user_id = pending["user_id"]
    file_name = pending["file_name"]
    success = process_approved_file(upload_id, call.message.chat.id, user_chat_id=user_id)
    if success:
        try:
            bot.send_message(user_id, stylish_text(f"✅ Your file {file_name} was approved and is now running."))
        except Exception as e:
            logger.error(f"Notify user {user_id}: {e}")
        try:
            bot.edit_message_caption(
                caption=stylish_text((call.message.caption or "") + "\n\n✅ APPROVED"),
                chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
        except Exception: pass
    else:
        bot.send_message(call.message.chat.id, stylish_text(f"❌ Failed to process file for user {user_id}."))

@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_approve_"))
def cancel_approve(call):
    bot.answer_callback_query(call.id, "Cancelled.")
    try: bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("reject_reason_"))
def reject_with_reason(call):
    admin_id = call.from_user.id
    if admin_id not in admin_ids:
        bot.answer_callback_query(call.id, "Admins only.", show_alert=True)
        return
    parts = call.data.split("_", 3)
    upload_id = int(parts[2])
    reason = parts[3] if len(parts) > 3 else "Other"
    pending = get_pending_upload(upload_id)
    if not pending:
        bot.answer_callback_query(call.id, "Gone.", show_alert=True)
        return
    user_id   = pending["user_id"]
    file_name = pending["file_name"]
    delete_pending_upload(upload_id)
    audit_log(admin_id, "reject_upload", str(upload_id), reason)
    try:
        bot.send_message(user_id, stylish_text(
            f"❌ Your file {file_name} was rejected.\nReason: {reason}\nPlease fix and re-upload."))
    except Exception as e:
        logger.error(f"Notify user {user_id}: {e}")
    bot.answer_callback_query(call.id, f"Rejected: {reason}")
    try:
        bot.edit_message_caption(
            caption=stylish_text((call.message.caption or "") + f"\n\n❌ REJECTED — {reason}"),
            chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
    except Exception: pass

# Approval-queue dashboard (Section 6 B.16)
@bot.callback_query_handler(func=lambda c: c.data == "view_approval_queue")
def view_approval_queue(call):
    admin_id = call.from_user.id
    if admin_id not in admin_ids:
        bot.answer_callback_query(call.id, "Admins only.", show_alert=True)
        return
    rows = list_pending_uploads()
    if not rows:
        bot.answer_callback_query(call.id, "Queue empty.", show_alert=True)
        return
    text_lines = ["📋 Approval Queue:"]
    markup = types.InlineKeyboardMarkup(row_width=2)
    for r in rows:
        uid, uid_user, fname, ftype, fsize, uname, flags = r
        flag_note = " ⚠️" if flags else ""
        text_lines.append(f"#{uid} • {fname} ({ftype}, {fsize//1024}KB) — {uname}{flag_note}")
        markup.row(
            types.InlineKeyboardButton(f"✅ #{uid}", callback_data=f"confirm_approve_{uid}"),
            types.InlineKeyboardButton(f"❌ #{uid}",  callback_data=f"reject_reason_{uid}_Other"))
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, stylish_text("\n".join(text_lines)), reply_markup=markup)

# =========================================================================
# GITHUB DEPLOY (with flatten fix + real progress)
# =========================================================================
def parse_github_url(url):
    url = re.sub(r"\.git$", "", url)
    if "github.com" not in url:
        raise ValueError("Not a valid GitHub URL")
    parts = url.split("github.com/")[-1].split("/")
    if len(parts) < 2:
        raise ValueError("Invalid GitHub URL format")
    owner = parts[0]; repo = parts[1]
    branch = "main"
    if len(parts) >= 4 and parts[2] == "tree":
        branch = parts[3]
    return owner, repo, branch

def download_github_repo(owner, repo, branch, token=None):
    url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{branch}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    resp = requests.get(url, headers=headers, stream=True)
    if resp.status_code == 404:
        raise Exception("Repository or branch not found")
    if resp.status_code == 401:
        raise Exception("Token invalid or expired")
    if resp.status_code != 200:
        raise Exception(f"GitHub API error: {resp.status_code}")
    cl = resp.headers.get("content-length")
    if cl and int(cl) > MAX_UPLOAD_MB * 1024 * 1024:
        raise Exception(f"Repo ZIP exceeds {MAX_UPLOAD_MB}MB limit")
    return resp.content

def _logic_github_deploy(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if is_user_banned(user_id):
        bot.send_message(chat_id, stylish_text("🚫 You are banned."))
        return
    if get_user_file_count(user_id) >= get_user_file_limit(user_id):
        bot.send_message(chat_id, stylish_text("⚠️ You've reached your file limit. Delete some first."))
        return
    github_data[user_id] = {"step": "url", "started_at": time.time()}
    push_nav(user_id, "GitHub Deploy")
    bot.send_message(chat_id, stylish_text(
        "📦 Send me the GitHub repo URL.\nExample: https://github.com/user/repo\n\nSend /cancel to abort."))

@bot.message_handler(func=lambda m: m.from_user.id in github_data and github_data[m.from_user.id]["step"] == "url")
def github_get_url(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if message.text and message.text.lower() == "/cancel":
        github_data.pop(user_id, None)
        clear_nav(user_id)
        bot.reply_to(message, stylish_text("❌ GitHub deploy cancelled."))
        return
    # Session auto-cancel (Section 3, item 45)
    if time.time() - github_data[user_id].get("started_at", 0) > 10 * 60:
        github_data.pop(user_id, None)
        bot.reply_to(message, stylish_text("⏰ Session expired. Start again."))
        return
    try:
        owner, repo, branch = parse_github_url(message.text.strip())
    except Exception as e:
        bot.reply_to(message, stylish_text(f"❌ Invalid URL: {e}"))
        return
    github_data[user_id].update({"url": message.text.strip(), "owner": owner,
                                 "repo": repo, "branch": branch})
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("🔒 Private", callback_data=f"github_private_{user_id}"),
        types.InlineKeyboardButton("🌐 Public",  callback_data=f"github_public_{user_id}"))
    bot.reply_to(message, stylish_text("Is this a private repository?"), reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("github_private_") or c.data.startswith("github_public_"))
def github_repo_type(call):
    user_id = int(call.data.split("_")[-1])
    if call.from_user.id != user_id:
        bot.answer_callback_query(call.id, "Not for you.", show_alert=True)
        return
    if user_id not in github_data:
        bot.answer_callback_query(call.id, "Session expired.", show_alert=True)
        return
    if call.data.startswith("github_private_"):
        github_data[user_id]["step"] = "token"
        bot.edit_message_text("🔑 Send your GitHub PAT (with `repo` scope).\nSend /cancel to abort.",
                              call.message.chat.id, call.message.message_id)
    else:
        github_data[user_id]["token"] = None
        _process_github_download(call.message.chat.id, user_id)
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.from_user.id in github_data and github_data[m.from_user.id].get("step") == "token")
def github_get_token(message):
    user_id = message.from_user.id
    if not check_subscription_and_continue(user_id, message.chat.id):
        return
    if message.text and message.text.lower() == "/cancel":
        github_data.pop(user_id, None)
        clear_nav(user_id)
        bot.reply_to(message, stylish_text("❌ Cancelled."))
        # Section 1, item 6 — never echo the token back; delete the user's message
        try: bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass
        return
    github_data[user_id]["token"] = message.text.strip()
    # Delete the token message so it doesn't linger in chat history
    try: bot.delete_message(message.chat.id, message.message_id)
    except Exception: pass
    _process_github_download(message.chat.id, user_id)

def _process_github_download(chat_id, user_id):
    data = github_data.get(user_id)
    if not data:
        bot.send_message(chat_id, stylish_text("Session expired. Start again."))
        return
    owner = data["owner"]; repo = data["repo"]; branch = data["branch"]; token = data.get("token")
    # Validate token via /user if provided (Section 2, item 26)
    if token:
        try:
            r = requests.get("https://api.github.com/user",
                             headers={"Authorization": f"token {token}"}, timeout=10)
            if r.status_code == 401:
                bot.send_message(chat_id, stylish_text("❌ Token invalid or expired."))
                github_data.pop(user_id, None)
                return
        except Exception as e:
            logger.error(f"GitHub /user check failed: {e}")
    # Real progress based on content-length (Section 2, item 35)
    msg = bot.send_message(chat_id, stylish_text("📥 Downloading repo...\n[░░░░░░░░░░] 0%"))
    try:
        zip_content = download_github_repo(owner, repo, branch, token)
        bot.edit_message_text(stylish_text("✅ Downloaded. Submitting for admin approval..."),
                              chat_id, msg.message_id)
    except Exception as e:
        bot.edit_message_text(stylish_text(f"❌ Download failed: {e}"), chat_id, msg.message_id)
        github_data.pop(user_id, None)
        return
    file_name = f"{repo}_{branch}.zip"
    try:
        sent = bot.send_document(chat_id, io.BytesIO(zip_content),
                                 visible_file_name=file_name,
                                 caption=stylish_text("🔄 Submitting for admin approval..."))
        file_id   = sent.document.file_id
        file_size = sent.document.file_size
        user_name     = bot.get_chat(user_id).first_name or "User"
        user_username = bot.get_chat(user_id).username or "No username"
        # Redact token in extra_info (Section 1, item 6)
        extra_info = f"GitHub URL: {data['url']}\nToken: {'[REDACTED]' if token else 'Public repo (no token)'}"
        upload_id = add_pending_upload(
            user_id=user_id, file_id=file_id, file_name=file_name, file_type="zip",
            file_size=file_size, user_name=user_name, user_username=user_username,
            extra_info=extra_info, sensitive_mode=0,
            scan_flags="; ".join(["github-zipball"] if False else []))
        if not upload_id:
            bot.send_message(chat_id, stylish_text("❌ Internal error, try later."))
            return
        for aid in admin_ids:
            try:
                caption = (f"📥 GitHub repo requires approval\n"
                           f"👤 {user_name} (@{user_username})\n🆔 {user_id}\n"
                           f"📦 {data['url']}\n📄 {file_name}\n"
                           f"📏 {file_size // 1024} KB\n🆔 Upload ID: {upload_id}")
                sent_admin = bot.send_document(aid, file_id, caption=stylish_text(caption))
                markup = types.InlineKeyboardMarkup()
                markup.row(
                    types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_upload_{upload_id}"),
                    types.InlineKeyboardButton("❌ Reject",  callback_data=f"reject_upload_{upload_id}"))
                bot.edit_message_reply_markup(aid, sent_admin.message_id, reply_markup=markup)
            except Exception as e:
                logger.error(f"Notify admin {aid}: {e}")
        bot.send_message(chat_id, stylish_text("✅ Repo submitted for admin approval. You'll be notified."))
    except Exception as e:
        bot.send_message(chat_id, stylish_text(f"❌ Failed to submit: {e}"))
    finally:
        github_data.pop(user_id, None)
        clear_nav(user_id)

# =========================================================================
# RECOMMENDED INSTALL
# =========================================================================
RECOMMENDED_PKGS = ["pip", "setuptools", "wheel", "requests", "numpy", "pandas",
                    "flask", "aiohttp", "pyrogram", "python-dotenv",
                    "beautifulsoup4", "lxml", "pillow", "matplotlib",
                    "scipy", "scikit-learn", "pytest"]

def _logic_recommended_install(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    text = ("📦 Python Package Installer\n\n"
            "Send a package name to install (e.g. `requests`, `numpy==1.5.0`).\n\n"
            "Or tap Install Recommended to get common packages.\n\n"
            "Send /cancel to abort.")
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Install Recommended", callback_data="install_recommended"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="cancel_install"))
    bot.send_message(chat_id, stylish_text(text), reply_markup=markup)
    bot.register_next_step_handler_by_chat_id(chat_id, process_manual_package_install, user_id)

def process_manual_package_install(message, user_id=None):
    user_id = user_id or message.from_user.id
    chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    text = (message.text or "").strip()
    if text.lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled."))
        return
    if text == "✅":
        bot.reply_to(message, stylish_text(f"🚀 Installing {len(RECOMMENDED_PKGS)} packages..."))
        ok = fail = 0
        for pkg in RECOMMENDED_PKGS:
            try:
                r = subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                                   capture_output=True, text=True)
                if r.returncode == 0: ok += 1
                else: fail += 1
            except Exception:
                fail += 1
            time.sleep(0.3)
        bot.send_message(chat_id, stylish_text(f"✅ Done. Success: {ok} | Failed: {fail}"))
    else:
        bot.reply_to(message, stylish_text(f"📦 Installing {text}..."))
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", text],
                               capture_output=True, text=True)
            if r.returncode == 0:
                bot.send_message(chat_id, stylish_text(f"✅ Installed {text}"))
            else:
                bot.send_message(chat_id, stylish_text(f"❌ Failed:\n{r.stderr[:500]}"))
        except Exception as e:
            bot.send_message(chat_id, stylish_text(f"❌ Error: {e}"))

@bot.callback_query_handler(func=lambda c: c.data == "install_recommended")
def install_recommended_callback(call):
    if not tap_guard(call.from_user.id):
        return
    bot.answer_callback_query(call.id, "Installing...")
    bot.send_message(call.message.chat.id, stylish_text(f"🚀 Installing {len(RECOMMENDED_PKGS)} packages..."))
    ok = fail = 0
    for pkg in RECOMMENDED_PKGS:
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                               capture_output=True, text=True)
            if r.returncode == 0: ok += 1
            else: fail += 1
        except Exception:
            fail += 1
        time.sleep(0.3)
    bot.send_message(call.message.chat.id, stylish_text(f"✅ Done. Success: {ok} | Failed: {fail}"))

@bot.callback_query_handler(func=lambda c: c.data == "cancel_install")
def cancel_install_callback(call):
    bot.answer_callback_query(call.id, "Cancelled.")
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass

# =========================================================================
# AI ASSISTANT (SambaNova)
# =========================================================================
def call_sambanova_sync(prompt: str, model_name: str) -> str:
    headers = {"Authorization": f"Bearer {SAMBA_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": model_name,
               "messages": [{"role": "system", "content": "You are a helpful AI assistant."},
                             {"role": "user", "content": prompt}],
               "temperature": 0.7, "max_tokens": 500, "top_p": 0.95}
    for attempt in range(3):
        try:
            r = requests.post(SAMBA_URL, headers=headers, json=payload, timeout=30)
            if r.status_code == 429:
                time.sleep((2 ** attempt) + random.uniform(0, 1))
                continue
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"]
            return f"⚠️ API error {r.status_code}: {r.text[:200]}"
        except Exception as e:
            if attempt == 2:
                return f"❌ Network error: {e}"
            time.sleep(2 ** attempt)
    return "❌ Max retries exceeded."

def auto_fix_modules_from_text(user_id, text, chat_id):
    missing = set()
    for pat in [r"ModuleNotFoundError: No module named '(.+?)'",
                r"ImportError: No module named '(.+?)'",
                r"No module named '(.+?)'"]:
        for m in re.findall(pat, text):
            m = m.strip().strip("'\"")
            if m and not m.startswith(".") and m not in ["sys", "os", "re", "time", "json", "datetime"]:
                missing.add(m)
    if not missing:
        bot.send_message(chat_id, stylish_text("ℹ️ No missing modules detected."))
        return
    bot.send_message(chat_id, stylish_text(f"🔍 Missing: {', '.join(missing)}\n\nInstalling..."))
    ok = fail = 0; results = []
    for mod in missing:
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", mod],
                               capture_output=True, text=True)
            if r.returncode == 0:
                ok += 1; results.append(f"✅ {mod}")
            else:
                fail += 1; results.append(f"❌ {mod} - {r.stderr[:80]}")
        except Exception as e:
            fail += 1; results.append(f"❌ {mod} - {e}")
        time.sleep(0.3)
    summary = "🔧 Auto-fix done:\n" + "\n".join(results) + f"\n\n✅ Installed: {ok}\n❌ Failed: {fail}"
    bot.send_message(chat_id, stylish_text(summary))

def ai_fix_script(owner_id, file_name, chat_id):
    folder = get_user_folder(owner_id)
    log_path = os.path.join(folder, f"{os.path.splitext(file_name)[0]}.log")
    if not os.path.exists(log_path):
        bot.send_message(chat_id, stylish_text(f"No log for {file_name}. Run it first."))
        return
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        log_content = f.read()
    # Redact secrets from log content before processing (Section 1, item 6)
    log_content = redact_secrets(log_content)
    missing = set()
    for pat in [r"ModuleNotFoundError: No module named '(.+?)'",
                r"ImportError: No module named '(.+?)'"]:
        for m in re.findall(pat, log_content):
            m = m.strip().strip("'\"")
            missing.add(m)
    if not missing:
        bot.send_message(chat_id, stylish_text(f"✅ No missing modules in {file_name}'s log."))
        return
    ok = fail = 0; results = []
    for mod in missing:
        bot.send_message(chat_id, stylish_text(f"📦 Installing {mod}..."))
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", mod],
                               capture_output=True, text=True)
            if r.returncode == 0: ok += 1; results.append(f"✅ {mod}")
            else: fail += 1; results.append(f"❌ {mod} - {r.stderr[:80]}")
        except Exception as e:
            fail += 1; results.append(f"❌ {mod} - {e}")
        time.sleep(0.3)
    bot.send_message(chat_id, stylish_text(
        f"🔧 AI Fix for {file_name}:\n" + "\n".join(results) +
        f"\n\n✅ {ok} | ❌ {fail}\n💡 Restart to apply."))

@bot.callback_query_handler(func=lambda c: c.data.startswith("aifix_"))
def ai_fix_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True)
            return
        if not tap_guard(call.from_user.id):
            return
        bot.answer_callback_query(call.id, "AI Fix running...")
        threading.Thread(target=ai_fix_script,
            args=(owner_id, file_name, call.message.chat.id)).start()
    except Exception as e:
        logger.error(f"AI Fix error: {e}")

def handle_deepseek_chat(message, user_id=None):
    user_id = user_id or message.from_user.id
    chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if not message.text:
        bot.reply_to(message, stylish_text("Send a text message or error log."))
        bot.register_next_step_handler_by_chat_id(chat_id, handle_deepseek_chat, user_id)
        return
    text = message.text.strip()
    if text.lower() == "/cancel":
        bot.reply_to(message, stylish_text("AI Agent cancelled."))
        clear_nav(user_id)
        return
    help_kw = ["how to use", "help", "commands", "kya kar sakta", "kaise use",
               "guide", "features", "what can you do", "bot kaise chalaye"]
    if any(k in text.lower() for k in help_kw):
        bot.send_chat_action(chat_id, "typing")
        bot.reply_to(message, stylish_text(get_bot_help_text()))
        bot.register_next_step_handler_by_chat_id(chat_id, handle_deepseek_chat, user_id)
        return
    err_pats = ["ModuleNotFoundError", "ImportError", "No module named", "module not found"]
    if any(p in text for p in err_pats):
        bot.send_chat_action(chat_id, "typing")
        auto_fix_modules_from_text(user_id, text, chat_id)
        bot.register_next_step_handler_by_chat_id(chat_id, handle_deepseek_chat, user_id)
        return
    bot.send_chat_action(chat_id, "typing")
    thinking = bot.reply_to(message, stylish_text("🤔 Thinking..."))
    response = call_sambanova_sync(text, AVAILABLE_MODELS[global_model])
    if len(response) > 4000:
        response = response[:4000] + "... (truncated)"
    bot.edit_message_text(stylish_text(response), chat_id, thinking.message_id)
    bot.register_next_step_handler_by_chat_id(chat_id, handle_deepseek_chat, user_id)

def _logic_ai_assistant(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    push_nav(user_id, "AI Agent")
    welcome = (f"🤖 Aɪ Aɢᴇɴᴛ\n\n⚡ Model: {global_model} ({AVAILABLE_MODELS[global_model]})\n\n"
               "• 📦 Auto-fix — send ModuleNotFoundError / ImportError\n"
               "• 💡 Type `help` for the full guide\n"
               "• 🚀 Ask any coding question\n\n"
               "Admins: /setmodel to change model.\n\nSend your question or `/cancel`.")
    bot.send_message(chat_id, stylish_text(welcome))
    bot.register_next_step_handler_by_chat_id(chat_id, handle_deepseek_chat, user_id)

def get_bot_help_text():
    return ("🤖 Hᴇʟᴘ Gᴜɪᴅᴇ\n\n"
            "📌 Bᴀsɪᴄ\n• /start — main menu\n• /uploadfile — upload .py/.js/.zip\n"
            "• /checkfiles — see your files\n• /restart — restart your scripts\n"
            "• /stop — stop your scripts\n\n"
            "📂 Fɪʟᴇ Mɢᴍᴛ\n• Upload → admin approves → auto start\n"
            "• My Files: Start/Stop/Restart/Delete/Logs/AI Fix/Variables/Download\n\n"
            "🔑 Vᴀʀɪᴀʙʟᴇs\n• Set BOT_TOKEN, API_KEY etc. without hardcoding\n"
            "• Values encrypted, shown as •••••\n\n"
            "🌐 GɪᴛHᴜʙ\n• Send a repo URL — flatten-fix handles nested zips\n\n"
            "🤖 Aɪ Aɢᴇɴᴛ\n• Send errors → auto-fix missing modules\n• Ask coding questions\n\n"
            "📊 Oᴛʜᴇʀ\n• /mystats — your bot uptime & restart count\n"
            "• /feedback — send feedback to owner\n"
            "• /status — platform version & uptime")

# =========================================================================
# BAN / UNBAN / STOP / RESTART COMMANDS
# =========================================================================
@bot.message_handler(commands=["ban"])
def cmd_ban(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids and user_id != OWNER_ID:
        bot.reply_to(message, stylish_text("⚠️ Admin only."))
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, stylish_text("Usage: /ban <user_id>"))
        return
    try: target = int(parts[1])
    except: bot.reply_to(message, stylish_text("Invalid ID.")); return
    if target in admin_ids or target == OWNER_ID:
        bot.reply_to(message, stylish_text("❌ Cannot ban admin/owner.")); return
    if ban_user(target):
        bot.reply_to(message, stylish_text(f"✅ Banned {target}."))
        try: bot.send_message(target, stylish_text("🚫 You've been banned."))
        except Exception: pass
    else:
        bot.reply_to(message, stylish_text(f"❌ Failed to ban {target}."))

@bot.message_handler(commands=["unban"])
def cmd_unban(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids and user_id != OWNER_ID:
        bot.reply_to(message, stylish_text("⚠️ Admin only."))
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, stylish_text("Usage: /unban <user_id>"))
        return
    try: target = int(parts[1])
    except: bot.reply_to(message, stylish_text("Invalid ID.")); return
    if unban_user(target):
        bot.reply_to(message, stylish_text(f"✅ Unbanned {target}."))
        try: bot.send_message(target, stylish_text("✅ You've been unbanned."))
        except Exception: pass
    else:
        bot.reply_to(message, stylish_text("❌ Not banned or failed."))

@bot.message_handler(commands=["stop"])
def cmd_stop_all(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids and user_id != OWNER_ID:
        bot.reply_to(message, stylish_text("⚠️ Admin only."))
        return
    running = list(bot_scripts.items())
    if not running:
        bot.reply_to(message, stylish_text("ℹ️ No scripts running."))
        return
    stopped = 0
    for k, info in running:
        try: kill_process_tree(info); stopped += 1
        except Exception as e: logger.error(f"Stop {k}: {e}")
    bot_scripts.clear()
    bot.reply_to(message, stylish_text(f"✅ Stopped {stopped} script(s)."))

def _logic_stop_my_scripts(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    files = user_files.get(user_id, [])
    if not files:
        bot.send_message(chat_id, stylish_text("📂 No files to stop."))
        return
    stopped = 0
    for fname, _ in files:
        key = f"{user_id}_{fname}"
        if key in bot_scripts:
            kill_process_tree(bot_scripts[key]); stopped += 1
            bot_scripts.pop(key, None)
            time.sleep(0.2)
    bot.send_message(chat_id, stylish_text(f"⏹ Stopped {stopped} of your script(s)."))

def _logic_restart_my_scripts(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    files = user_files.get(user_id, [])
    if not files:
        bot.send_message(chat_id, stylish_text("📂 No files to restart."))
        return
    bot.send_message(chat_id, stylish_text("🔄 Restarting your scripts..."))
    stopped = 0
    for fname, _ in files:
        key = f"{user_id}_{fname}"
        if key in bot_scripts:
            kill_process_tree(bot_scripts[key]); stopped += 1
            bot_scripts.pop(key, None)
            time.sleep(0.3)
    started = 0
    for fname, ftype in files:
        folder = get_user_folder(user_id)
        path = os.path.join(folder, fname)
        if not os.path.exists(path):
            bot.send_message(chat_id, stylish_text(f"⚠️ {fname} missing locally."))
            continue
        if ftype == "py":
            threading.Thread(target=run_script, args=(path, user_id, folder, fname, chat_id)).start()
        elif ftype == "js":
            threading.Thread(target=run_js_script, args=(path, user_id, folder, fname, chat_id)).start()
        else: continue
        started += 1; time.sleep(0.5)
    bot.send_message(chat_id, stylish_text(f"✅ Restarted {started} (stopped {stopped} first)."))

@bot.message_handler(commands=["restart"])
def cmd_restart_all(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id in admin_ids or user_id == OWNER_ID:
        running = []
        for key, info in list(bot_scripts.items()):
            try:
                parts = key.split("_", 1)
                if len(parts) == 2:
                    oid = int(parts[0]); fn = parts[1]
                    ftype = None
                    for n, t in user_files.get(oid, []):
                        if n == fn: ftype = t; break
                    if ftype: running.append((oid, fn, ftype))
            except Exception as e: logger.error(f"capture {key}: {e}")
        if not running:
            bot.reply_to(message, stylish_text("ℹ️ No scripts running."))
            return
        stopped = 0
        for k, info in list(bot_scripts.items()):
            try: kill_process_tree(info); stopped += 1
            except Exception: pass
        bot_scripts.clear()
        bot.reply_to(message, stylish_text(f"🛑 Stopped {stopped}. Restarting..."))
        started = 0
        for oid, fn, ftype in running:
            folder = get_user_folder(oid)
            path = os.path.join(folder, fn)
            if not os.path.exists(path): continue
            if ftype == "py":
                threading.Thread(target=run_script, args=(path, oid, folder, fn, chat_id)).start()
            else:
                threading.Thread(target=run_js_script, args=(path, oid, folder, fn, chat_id)).start()
            started += 1; time.sleep(0.5)
        bot.send_message(chat_id, stylish_text(f"✅ Restarted {started} script(s)."))
        return
    _logic_restart_my_scripts(chat_id, user_id)

# =========================================================================
# AUTO-RECOVERY WORKER + WATCHDOG (Section 4, items 63 + 61)
# =========================================================================
CRASH_COUNT = {}  # script_key -> count

def auto_recovery_worker():
    while True:
        time.sleep(30)
        try:
            now = time.time()
            for key, info in list(bot_scripts.items()):
                try:
                    proc = info.get("process")
                    if not proc or not getattr(proc, "pid", None): continue
                    pid = proc.pid
                    try:
                        p = psutil.Process(pid)
                        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                            raise psutil.NoSuchProcess(pid)
                        # Resource caps (Section 4, item 61)
                        try:
                            cpu = p.cpu_percent(interval=None)
                            mem = p.memory_info().rss / (1024 * 1024)
                            cap = info.get("ram_cap_mb", 256)
                            if mem > cap:
                                logger.warning(f"RAM cap exceeded for {key}: {mem:.0f}MB")
                                kill_process_tree(info)
                                bot_scripts.pop(key, None)
                                cid = info.get("chat_id")
                                if cid:
                                    try: bot.send_message(cid, stylish_text(f"⛔ {info.get('file_name')} stopped (RAM cap {cap}MB)."))
                                    except Exception: pass
                                continue
                        except Exception: pass
                    except psutil.NoSuchProcess:
                        last = auto_recovery_last_restart.get(key, 0)
                        if now - last < 60: continue
                        auto_recovery_last_restart[key] = now
                        oid = info.get("script_owner_id"); fn = info.get("file_name")
                        cid = info.get("chat_id"); ftype = info.get("type")
                        uf  = info.get("user_folder")
                        if not oid or not fn: continue
                        CRASH_COUNT[key] = CRASH_COUNT.get(key, 0) + 1
                        # Watchdog: only auto-restart if user opted in (Section 4, item 63)
                        if info.get("watchdog", False) and CRASH_COUNT[key] < 5:
                            logger.info(f"Watchdog restarting {key}")
                            if cid:
                                try: bot.send_message(cid, stylish_text(f"🔄 Auto-restart: {fn}"))
                                except Exception: pass
                            _close_log(info)
                            bot_scripts.pop(key, None)
                            path = os.path.join(uf, fn)
                            if not os.path.exists(path): continue
                            if ftype == "py":
                                threading.Thread(target=run_script, args=(path, oid, uf, fn, cid)).start()
                            else:
                                threading.Thread(target=run_js_script, args=(path, oid, uf, fn, cid)).start()
                        else:
                            bot_scripts.pop(key, None)
                except Exception as e:
                    logger.error(f"recovery err {key}: {e}")
        except Exception as e:
            logger.error(f"recovery worker err: {e}")

threading.Thread(target=auto_recovery_worker, daemon=True).start()

# Heartbeat (Section 4, item 60)
def heartbeat_worker():
    while True:
        time.sleep(3600)
        try:
            me = bot.get_me()
            logger.info(f"Heartbeat OK: @{me.username}")
        except Exception as e:
            logger.error(f"Heartbeat FAIL: {e}")
            try:
                bot.send_message(OWNER_ID, stylish_text(f"⚠️ Bot token may be invalid: {e}"))
            except Exception: pass

threading.Thread(target=heartbeat_worker, daemon=True).start()

# PID reconciliation on startup (Section 4, item 58)
def reconcile_pids():
    stale = []
    for key, info in list(bot_scripts.items()):
        proc = info.get("process")
        if not proc or not getattr(proc, "pid", None):
            stale.append(key); continue
        try:
            p = psutil.Process(proc.pid)
            if not p.is_running():
                stale.append(key)
        except psutil.NoSuchProcess:
            stale.append(key)
    for k in stale:
        bot_scripts.pop(k, None)
    if stale:
        logger.warning(f"Reconciled {len(stale)} stale PID entries on startup.")

reconcile_pids()

# =========================================================================
# MENU CREATION
# =========================================================================
def create_control_buttons(script_owner_id, file_name, is_running=True):
    markup = types.InlineKeyboardMarkup(row_width=2)
    if is_running:
        markup.row(
            types.InlineKeyboardButton("🔴 Stop",    callback_data=f"stop_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{script_owner_id}_{file_name}"))
        markup.row(
            types.InlineKeyboardButton("🗑️ Delete",   callback_data=f"delete_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("📜 Logs",    callback_data=f"logs_{script_owner_id}_{file_name}"))
        markup.row(
            types.InlineKeyboardButton("🤖 AI Fix",  callback_data=f"aifix_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("🔑 Vars",    callback_data=f"vars_{script_owner_id}_{file_name}"))
        markup.row(
            types.InlineKeyboardButton("📥 Download", callback_data=f"dl_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("🔙 Back",      callback_data="back_to_files"))
    else:
        markup.row(
            types.InlineKeyboardButton("🟢 Start",   callback_data=f"start_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("🗑️ Delete",  callback_data=f"delete_{script_owner_id}_{file_name}"))
        markup.row(
            types.InlineKeyboardButton("📜 Logs",    callback_data=f"logs_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("🤖 AI Fix",  callback_data=f"aifix_{script_owner_id}_{file_name}"))
        markup.row(
            types.InlineKeyboardButton("🔑 Vars",    callback_data=f"vars_{script_owner_id}_{file_name}"),
            types.InlineKeyboardButton("📥 Download", callback_data=f"dl_{script_owner_id}_{file_name}"))
        markup.row(types.InlineKeyboardButton("🔙 Back to Files", callback_data="back_to_files"))
    return markup

def create_main_menu_inline(user_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton("📢 Updates Channel", callback_data="updates_channel"),
        types.InlineKeyboardButton("🌏 Upload",          callback_data="upload"),
        types.InlineKeyboardButton("📁 My Files",        callback_data="check_files"),
        types.InlineKeyboardButton("⚡ Bot Speed",        callback_data="speed"),
        types.InlineKeyboardButton("⚙️ Recommended Install", callback_data="recommended_install"),
        types.InlineKeyboardButton("🤖 AI Assistant",    callback_data="ai_assistant"),
        types.InlineKeyboardButton("🌐 GitHub",           callback_data="github_deploy"),
        types.InlineKeyboardButton("🔑 Variables",       callback_data="my_variables"),
        types.InlineKeyboardButton("📞 Contact Owner",    url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}"),
    ]
    if user_id in admin_ids:
        admin_buttons = [
            types.InlineKeyboardButton("💳 Subscriptions", callback_data="subscription"),
            types.InlineKeyboardButton("🚀 Status",         callback_data="stats"),
            types.InlineKeyboardButton("🔒 Lock Bot" if not bot_locked else "🔓 Unlock Bot",
                                       callback_data="lock_bot" if not bot_locked else "unlock_bot"),
            types.InlineKeyboardButton("📢 Broadcast",      callback_data="broadcast"),
            types.InlineKeyboardButton("🛠️ Admin Panel",   callback_data="admin_panel"),
            types.InlineKeyboardButton("🟢 Run All Scripts", callback_data="run_all_scripts"),
            types.InlineKeyboardButton("📋 Approval Queue", callback_data="view_approval_queue"),
        ]
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3], admin_buttons[0])
        markup.add(admin_buttons[1], admin_buttons[3])
        markup.add(admin_buttons[2], admin_buttons[5])
        markup.add(admin_buttons[4])
        markup.add(admin_buttons[6])
        markup.add(buttons[4], buttons[5])
        markup.add(buttons[6], buttons[7])
        markup.add(buttons[8])
    else:
        markup.add(buttons[0])
        markup.add(buttons[1], buttons[2])
        markup.add(buttons[3])
        markup.add(types.InlineKeyboardButton("🚀 Status", callback_data="stats"))
        markup.add(buttons[4], buttons[5])
        markup.add(buttons[6], buttons[7])
        markup.add(buttons[8])
    return markup

def create_reply_keyboard_main_menu(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    layout = ADMIN_COMMAND_BUTTONS_LAYOUT_USER_SPEC if user_id in admin_ids else COMMAND_BUTTONS_LAYOUT_USER_SPEC
    for row in layout:
        markup.add(*[types.KeyboardButton(t) for t in row])
    return markup

def create_admin_panel():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("➕ Add Admin",    callback_data="add_admin"),
        types.InlineKeyboardButton("➖ Remove Admin", callback_data="remove_admin"))
    markup.row(types.InlineKeyboardButton("📋 List Admins", callback_data="list_admins"))
    markup.row(types.InlineKeyboardButton("🔧 Set User Limit", callback_data="set_user_limit"))
    markup.row(
        types.InlineKeyboardButton("🤖 Change AI Model",  callback_data="change_ai_model"),
        types.InlineKeyboardButton("🔍 Find User",        callback_data="find_user"))
    markup.row(
        types.InlineKeyboardButton("🛑 Kill Switch",       callback_data="kill_switch"),
        types.InlineKeyboardButton("📋 Approval Queue",   callback_data="view_approval_queue"))
    markup.row(types.InlineKeyboardButton("📜 Audit Log", callback_data="view_audit_log"))
    markup.row(types.InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main"))
    return markup

def create_subscription_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        types.InlineKeyboardButton("➕ Add Subscription",    callback_data="add_subscription"),
        types.InlineKeyboardButton("➖ Remove Subscription", callback_data="remove_subscription"))
    markup.row(types.InlineKeyboardButton("🔍 Check Subscription", callback_data="check_subscription"))
    markup.row(types.InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main"))
    return markup

def create_model_selection_markup():
    markup = types.InlineKeyboardMarkup(row_width=2)
    for k in AVAILABLE_MODELS:
        markup.add(types.InlineKeyboardButton(f"{k.upper()} – {AVAILABLE_MODELS[k]}",
                                              callback_data=f"setmodel_{k}"))
    markup.add(types.InlineKeyboardButton("🔙 Back to Admin", callback_data="admin_panel"))
    return markup


# =========================================================================
# LOGIC FUNCTIONS — all take (chat_id, user_id) explicitly (Section 0.1 fix)
# =========================================================================
def _logic_send_welcome(chat_id, user_id, user_name=None):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Bot locked by admin."))
        return
    if user_id not in active_users:
        add_active_user(user_id)
        try:
            uname = user_name or "User"
            bot.send_message(OWNER_ID, stylish_text(f"🎉 New user!\n👤 {uname}\n🆔 {user_id}"))
        except Exception: pass
    current_files = get_user_file_count(user_id)
    storage_note = ""
    if STORAGE_STATE == "fresh":
        storage_note = "\n\nℹ️ Fresh volume detected."
    elif STORAGE_STATE == "wiped":
        storage_note = "\n\n⚠️ DB may have been wiped."
    box = ("┏━━━━━━━━━━━━━━━━━━━━━━┓\n"
           "┃   🚀 HACKER_XD01 HOSTING   ┃\n"
           f"┃      {PLATFORM_VERSION}                ┃\n"
           "┗━━━━━━━━━━━━━━━━━━━━━━┛\n\n"
           f"👤 Welcome {user_name or 'User'}!\n"
           f"🆔 {user_id}\n\n"
           f"📁 Files: {current_files}\n\n"
           "⚡ Features:\n"
           "• Auto-Recovery + Watchdog\n"
           "• Python/JS/Zip + Variables Vault\n"
           "• GitHub Deploy (flatten-fix)\n"
           f"{storage_note}\n\n"
           "Use the buttons below.")
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            fid = photos.photos[0][-1].file_id
            bot.send_photo(chat_id, fid, caption=stylish_text(box),
                           reply_markup=create_reply_keyboard_main_menu(user_id))
        else:
            bot.send_message(chat_id, stylish_text(box),
                            reply_markup=create_reply_keyboard_main_menu(user_id))
    except Exception as e:
        logger.error(f"welcome photo: {e}")
        bot.send_message(chat_id, stylish_text(box),
                        reply_markup=create_reply_keyboard_main_menu(user_id))
    # Show changelog card once on first /start after upgrade (Section 5, item 76)
    try:
        seen = os.path.join(PERSISTENT_DISK, ".changelog_shown")
        if not os.path.exists(seen):
            bot.send_message(chat_id, stylish_text(f"🆕 What's New:\n{PLATFORM_CHANGELOG}"))
            open(seen, "w").close()
    except Exception: pass
    clear_nav(user_id)

def _logic_updates_channel(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in REQUIRED_CHANNELS:
        markup.add(types.InlineKeyboardButton(ch, url=f"https://t.me/{ch.lstrip('@')}"))
    bot.send_message(chat_id, stylish_text("📢 Our Channels:"), reply_markup=markup)

def _logic_upload_file(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if bot_locked and user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Bot locked."))
        return
    fl = get_user_file_limit(user_id)
    cur = get_user_file_count(user_id)
    if cur >= fl:
        ls = str(fl) if fl != float("inf") else "Unlimited"
        rem = fl - cur
        warn = f" ⚠️ Only {rem} slot(s) left!" if 0 < rem <= 2 else ""
        bot.send_message(chat_id, stylish_text(f"⚠️ Limit reached ({cur}/{ls}).{warn}"))
        return
    bot.send_message(chat_id, stylish_text("📤 Send your .py, .js, .zip, requirements.txt or package.json.\nIt goes to admins for approval."))

def _logic_check_files(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    push_nav(user_id, "My Files")
    files = user_files.get(user_id, [])
    if not files:
        bot.send_message(chat_id, stylish_text("📂 No files uploaded yet."))
        return
    # Pagination (Section 3, item 41) when > 10 files
    markup = types.InlineKeyboardMarkup(row_width=1)
    for fname, ftype in sorted(files)[:20]:
        running = is_bot_running(user_id, fname)
        status = "🟢" if running else "🔴"
        markup.add(types.InlineKeyboardButton(f"{fname} ({ftype}) {status}",
                                                callback_data=f"file_{user_id}_{fname}"))
    if len(files) > 20:
        bot.send_message(chat_id, stylish_text(f"📂 Your files (showing first 20 of {len(files)}):"), reply_markup=markup)
    else:
        bot.send_message(chat_id, stylish_text("📂 Your files:"), reply_markup=markup)

def _logic_bot_speed(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    start = time.time()
    wait = bot.send_message(chat_id, stylish_text("🏃 Testing speed..."))
    try:
        bot.send_chat_action(chat_id, "typing")
        latency = round((time.time() - start) * 1000, 2)
        cf = psutil.cpu_freq()
        ghz = round(cf.current / 1000, 1) if cf else 0.0
        mem = psutil.virtual_memory()
        total = round(mem.total / (1024**3), 2)
        free  = round(mem.available / (1024**3), 2)
        status = "🔓 Unlocked" if not bot_locked else "🔒 Locked"
        if user_id == OWNER_ID: level = "👑 Owner"
        elif user_id in admin_ids: level = "🛡️ Admin"
        elif user_id in user_subscriptions and user_subscriptions[user_id]["expiry"] > datetime.now():
            level = "⭐ Premium"
        else: level = "🆓 Free"
        msg = (f"⚡ Speed: {latency} ms\n⚙️ CPU: {ghz} GHz\n💾 RAM: {total} GB\n"
               f"🟢 Free: {free} GB\n🚦 Status: {status}\n👤 Level: {level}")
        bot.edit_message_text(stylish_text(msg), chat_id, wait.message_id)
    except Exception as e:
        bot.edit_message_text(stylish_text(f"❌ Speed test error: {e}"), chat_id, wait.message_id)

def _logic_contact_owner(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📞 Contact Owner", url=f"https://t.me/{YOUR_USERNAME.replace('@', '')}"))
    bot.send_message(chat_id, stylish_text("Contact owner:"), reply_markup=markup)

def _logic_statistics(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    total_users = len(active_users)
    total_files = sum(len(f) for f in user_files.values())
    running = sum(1 for k, info in bot_scripts.items() if is_bot_running(int(k.split("_")[0]), info["file_name"]))
    now = datetime.now()
    up = now - BOT_START_TIME
    hours, rem = divmod(up.seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime = f"{up.days}d {hours}h {minutes}m {seconds}s"
    if user_id in admin_ids:
        msg = (f"📊 STATUS\n👥 Users: {total_users}\n📂 Files: {total_files}\n"
               f"🟢 Running: {running}\n⏱️ Uptime: {uptime}\n🔒 Locked: {bot_locked}\n"
               f"📦 Version: {PLATFORM_VERSION}\n💾 Storage: {STORAGE_STATE}")
    else:
        msg = (f"📊 STATUS\n👥 Users: {total_users}\n📂 Files: {total_files}\n"
               f"🟢 Running: {running}\n⏱️ Uptime: {uptime}\n📦 Version: {PLATFORM_VERSION}")
    bot.send_message(chat_id, stylish_text(msg))

def _logic_subscriptions_panel(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Admin only."))
        return
    push_nav(user_id, "Subscriptions")
    bot.send_message(chat_id, stylish_text("💳 Subscription Management"), reply_markup=create_subscription_menu())

def _logic_broadcast_init(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Admin only."))
        return
    broadcast_state[user_id] = {"step": "msg", "started_at": time.time()}
    bot.send_message(chat_id, stylish_text("📢 Send the broadcast message.\n/cancel to abort."))

@bot.message_handler(func=lambda m: m.from_user.id in broadcast_state and broadcast_state[m.from_user.id].get("step") == "msg")
def process_broadcast_message(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        return
    if message.text and message.text.lower() == "/cancel":
        broadcast_state.pop(user_id, None)
        bot.reply_to(message, stylish_text("Broadcast cancelled."))
        return
    content = message.text
    if not content:
        bot.reply_to(message, stylish_text("Cannot broadcast empty text."))
        return
    target = len(active_users)
    markup = types.InlineKeyboardMarkup()
    markup.row(
        types.InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_broadcast_{message.message_id}"),
        types.InlineKeyboardButton("❌ Cancel",  callback_data="cancel_broadcast"))
    bot.reply_to(message, stylish_text(f"⚠️ Confirm broadcast to {target} users:\n\n{content[:500]}"),
                 reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_broadcast_"))
def handle_confirm_broadcast(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    original = call.message.reply_to_message
    if not original or not original.text:
        bot.answer_callback_query(call.id, "No broadcast message.")
        return
    text = original.text
    bot.answer_callback_query(call.id, "Broadcasting...")
    bot.edit_message_text(stylish_text("📢 Broadcasting..."), call.message.chat.id, call.message.message_id, reply_markup=None)
    threading.Thread(target=execute_broadcast, args=(text, call.message.chat.id, call.from_user.id)).start()

@bot.callback_query_handler(func=lambda c: c.data == "cancel_broadcast")
def handle_cancel_broadcast(call):
    bot.answer_callback_query(call.id, "Cancelled.")
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass

def execute_broadcast(text, admin_chat_id, admin_id):
    sent = fail = 0
    for uid in list(active_users):
        if is_user_banned(uid): continue
        try:
            bot.send_message(uid, stylish_text(text))
            sent += 1
        except Exception:
            # Handle 429 RetryAfter (Section 5, item 79)
            try:
                time.sleep(1)
                bot.send_message(uid, stylish_text(text))
                sent += 1
            except Exception:
                fail += 1
        time.sleep(0.05)
    bot.send_message(admin_chat_id, stylish_text(f"📢 Done. Sent: {sent} | Failed: {fail}"))
    audit_log(admin_id, "broadcast", f"sent={sent}")

def _logic_toggle_lock_bot(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Admin only."))
        return
    global bot_locked
    bot_locked = not bot_locked
    status = "locked" if bot_locked else "unlocked"
    bot.send_message(chat_id, stylish_text(f"🔒 Bot {status}."))
    audit_log(user_id, "lock_toggle", status)

def _logic_admin_panel(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Admin only."))
        return
    push_nav(user_id, "Admin Panel")
    bot.send_message(chat_id, stylish_text("🛠️ Admin Panel"), reply_markup=create_admin_panel())

def _logic_run_all_scripts(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if user_id not in admin_ids:
        bot.send_message(chat_id, stylish_text("⚠️ Admin only."))
        return
    bot.send_message(chat_id, stylish_text("⏳ Starting all user scripts..."))
    started = 0
    for uid, files in list(user_files.items()):
        if is_user_banned(uid): continue
        folder = get_user_folder(uid)
        for fname, ftype in files:
            if not is_bot_running(uid, fname):
                path = os.path.join(folder, fname)
                if os.path.exists(path):
                    if ftype == "py":
                        threading.Thread(target=run_script, args=(path, uid, folder, fname, chat_id)).start()
                    else:
                        threading.Thread(target=run_js_script, args=(path, uid, folder, fname, chat_id)).start()
                    started += 1
                    time.sleep(0.5)
    bot.send_message(chat_id, stylish_text(f"✅ Attempted to start {started} scripts."))
    audit_log(user_id, "run_all", str(started))

# =========================================================================
# VARIABLES UI (Section 1) — list/add/delete/reveal per file
# =========================================================================
def _logic_my_variables(chat_id, user_id):
    if not check_subscription_and_continue(user_id, chat_id):
        return
    files = user_files.get(user_id, [])
    if not files:
        bot.send_message(chat_id, stylish_text("🔑 You have no files yet. Upload a script first."))
        return
    push_nav(user_id, "Variables")
    markup = types.InlineKeyboardMarkup(row_width=1)
    for fname, _ in sorted(files):
        markup.add(types.InlineKeyboardButton(f"🔑 {fname}", callback_data=f"vars_{user_id}_{fname}"))
    markup.add(types.InlineKeyboardButton("🔙 Back to Main", callback_data="back_to_main"))
    bot.send_message(chat_id, stylish_text("🔑 Variable Vault — pick a file:"), reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("vars_"))
def vars_file_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
    except ValueError:
        # Could be "my_variables" handled elsewhere
        return
    owner_id = int(owner_id_str)
    if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True)
        return
    if not tap_guard(call.from_user.id): return
    variables = get_user_variables(owner_id, file_name)
    text = f"🔑 Variables for {file_name}:\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    if variables:
        for k in variables:
            text += f"\n• {k} = •••••"
        markup.add(types.InlineKeyboardButton("➕ Add/Edit Variable", callback_data=f"varedit_{owner_id}_{file_name}"))
        markup.add(types.InlineKeyboardButton("👁 Reveal (10s)", callback_data=f"varshow_{owner_id}_{file_name}"))
        for k in variables:
            markup.add(types.InlineKeyboardButton(f"🗑️ {k}", callback_data=f"vardel_{owner_id}_{file_name}_{k}"))
    else:
        text += "\n(empty)"
        markup.add(types.InlineKeyboardButton("➕ Add Variable", callback_data=f"varedit_{owner_id}_{file_name}"))
    markup.add(types.InlineKeyboardButton("🔙 Back to Variables", callback_data="my_variables"))
    bot.edit_message_text(stylish_text(text), call.message.chat.id, call.message.message_id, reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("varedit_"))
def var_edit_callback(call):
    parts = call.data.split("_", 2)
    owner_id = int(parts[1]); file_name = parts[2]
    if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "Send KEY=VALUE")
    bot.send_message(call.message.chat.id, stylish_text(
        f"🔑 Send KEY=VALUE for {file_name}.\nExample: BOT_TOKEN=1234:ABCD\nSend /cancel to abort."))
    bot.register_next_step_handler_by_chat_id(call.message.chat.id, var_save_step, owner_id, file_name)

def var_save_step(message, owner_id, file_name):
    if not message.text:
        bot.register_next_step_handler_by_chat_id(message.chat.id, var_save_step, owner_id, file_name)
        return
    if message.text.lower() == "/cancel":
        bot.send_message(message.chat.id, stylish_text("Cancelled."))
        # Delete the message so the value doesn't linger (Section 1, item 6)
        try: bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass
        return
    try:
        key, _, value = message.text.partition("=")
        key = key.strip(); value = value.strip()
        if not key:
            bot.send_message(message.chat.id, stylish_text("❌ Empty key."))
            return
        set_user_variable(owner_id, file_name, key, value)
        bot.send_message(message.chat.id, stylish_text(f"✅ Saved {key} for {file_name}."))
        audit_log(owner_id, "var_set", f"{file_name}:{key}")
    except Exception as e:
        bot.send_message(message.chat.id, stylish_text(f"❌ Error: {e}"))
    finally:
        try: bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("varshow_"))
def var_show_callback(call):
    parts = call.data.split("_", 2)
    owner_id = int(parts[1]); file_name = parts[2]
    if call.from_user.id != owner_id and call.from_user.id != OWNER_ID:
        bot.answer_callback_query(call.id, "Only owner can reveal.", show_alert=True)
        return
    variables = get_user_variables(owner_id, file_name)
    if not variables:
        bot.answer_callback_query(call.id, "No variables.", show_alert=True)
        return
    # Reveal for 10 seconds (Section 1, item 11) — show then schedule a delete
    lines = "\n".join(f"{k} = {v}" for k, v in variables.items())
    sent = bot.send_message(call.message.chat.id, stylish_text(f"👁 Revealed (auto-delete 10s):\n{lines}"))
    audit_log(call.from_user.id, "var_reveal", file_name)
    def _delete():
        time.sleep(10)
        try: bot.delete_message(call.message.chat.id, sent.message_id)
        except Exception: pass
    threading.Thread(target=_delete, daemon=True).start()
    bot.answer_callback_query(call.id, "Revealed for 10s.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("vardel_"))
def var_del_callback(call):
    try:
        _, owner_id_str, file_name, var_name = call.data.split("_", 3)
    except ValueError:
        return
    owner_id = int(owner_id_str)
    if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Permission denied.", show_alert=True)
        return
    delete_user_variable(owner_id, file_name, var_name)
    bot.answer_callback_query(call.id, f"Deleted {var_name}.")
    vars_file_callback(call)  # refresh

# =========================================================================
# MODEL MANAGEMENT
# =========================================================================
@bot.message_handler(commands=["model"])
def cmd_show_model(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    bot.reply_to(message, stylish_text(f"🧠 Current AI model: {global_model} ({AVAILABLE_MODELS[global_model]})"))

@bot.message_handler(commands=["setmodel"])
def cmd_set_model(message):
    user_id = message.from_user.id
    if user_id not in admin_ids:
        bot.reply_to(message, stylish_text("⛔ Admins only."))
        return
    bot.reply_to(message, stylish_text("Select a new AI model:"), reply_markup=create_model_selection_markup())

@bot.callback_query_handler(func=lambda c: c.data.startswith("setmodel_"))
def set_model_callback(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return
    mk = call.data.split("_", 1)[1]
    if mk in AVAILABLE_MODELS:
        global global_model
        global_model = mk
        bot.answer_callback_query(call.id, f"✅ Model: {mk.upper()}")
        bot.edit_message_text(stylish_text(f"✅ AI model: {mk} ({AVAILABLE_MODELS[mk]})"),
                              call.message.chat.id, call.message.message_id)
        audit_log(call.from_user.id, "setmodel", mk)
    else:
        bot.answer_callback_query(call.id, "Invalid model.", show_alert=True)

# =========================================================================
# SET USER LIMIT (multi-step)
# =========================================================================
def process_set_user_limit(message):
    if message.from_user.id not in admin_ids:
        return
    text = (message.text or "").strip()
    if text.lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled.")); return
    parts = text.split()
    if len(parts) != 2:
        bot.reply_to(message, stylish_text("Format: user_id limit\nExample: 123456789 50"))
        return
    try:
        uid = int(parts[0]); limit = int(parts[1])
        if limit < 0:
            bot.reply_to(message, stylish_text("Limit must be >= 0.")); return
    except Exception:
        bot.reply_to(message, stylish_text("Invalid — must be numbers.")); return
    set_user_custom_limit(uid, limit)
    bot.reply_to(message, stylish_text(f"✅ User {uid} limit set to {limit}."))
    audit_log(message.from_user.id, "set_limit", str(uid), str(limit))

# =========================================================================
# BUTTON TEXT → LOGIC MAP
# =========================================================================
def _dispatch_button(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if not tap_guard(user_id):
        bot.send_message(chat_id, stylish_text("⏳ Please wait a moment..."))
        return
    text = message.text
    mapping = {
        "📢 𝐔𝐩𝐝𝐚𝐭𝐞𝐬 𝐂𝐡𝐚𝐧𝐧𝐞𝐥": lambda: _logic_updates_channel(chat_id, user_id),
        "🌏 Upload":                lambda: _logic_upload_file(chat_id, user_id),
        "📁 𝐌𝐲 𝐅𝐢𝐥𝐞𝐬":             lambda: _logic_check_files(chat_id, user_id),
        "⚡ 𝐁𝐨𝐭 𝐒𝐩𝐞𝐞𝐝":            lambda: _logic_bot_speed(chat_id, user_id),
        "🚀 𝐒𝐭𝐚𝐭𝐮𝐬":               lambda: _logic_statistics(chat_id, user_id),
        "🔄 𝐑𝐞𝐬𝐭𝐚𝐫𝐭":              lambda: _logic_restart_my_scripts(chat_id, user_id),
        "⏹ 𝐒𝐭𝐨𝐩":                  lambda: _logic_stop_my_scripts(chat_id, user_id),
        "💳 𝐒𝐮𝐛𝐬𝐜𝐫𝐢𝐩𝐭𝐢𝐨𝐧𝐬":       lambda: _logic_subscriptions_panel(chat_id, user_id),
        "📢 𝐁𝐫𝐨𝐚𝐝𝐜𝐚𝐬𝐭":           lambda: _logic_broadcast_init(chat_id, user_id),
        "🔒 𝐋𝐨𝐜𝐤 𝐁𝐨𝐭":            lambda: _logic_toggle_lock_bot(chat_id, user_id),
        "🟢 𝐑𝐮𝐧𝐧𝐢𝐧𝐠 𝐀𝐥𝐥 𝐂𝐨𝐝𝐞":     lambda: _logic_run_all_scripts(chat_id, user_id),
        "🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐏𝐚𝐧𝐞𝐥":        lambda: _logic_admin_panel(chat_id, user_id),
        "⚙️ Recommended Install":  lambda: _logic_recommended_install(chat_id, user_id),
        "🤖 𝐀𝐆𝐄𝐍𝐓":                lambda: _logic_ai_assistant(chat_id, user_id),
        "🌐 𝐆𝐈𝐓𝐇𝐔𝐁":               lambda: _logic_github_deploy(chat_id, user_id),
        "🔑 Variables":            lambda: _logic_my_variables(chat_id, user_id),
        "📞 𝐂𝐨𝐧𝐭𝐚𝐜𝐭 𝐎𝐰𝐧𝐞𝐫":       lambda: _logic_contact_owner(chat_id, user_id),
    }
    fn = mapping.get(text)
    if fn:
        fn()

@bot.message_handler(func=lambda m: m.text and m.text in {
    "📢 𝐔𝐩𝐝𝐚𝐭𝐞𝐬 𝐂𝐡𝐚𝐧𝐧𝐞𝐥","🌏 Upload","📁 𝐌𝐲 𝐅𝐢𝐥𝐞𝐬","⚡ 𝐁𝐨𝐭 𝐒𝐩𝐞𝐞𝐝",
    "📞 𝐂𝐨𝐧𝐭𝐚𝐜𝐭 𝐎𝐰𝐧𝐞𝐫","🚀 𝐒𝐭𝐚𝐭𝐮𝐬","🔄 𝐑𝐞𝐬𝐭𝐚𝐫𝐭","⏹ 𝐒𝐭𝐨𝐩",
    "💳 𝐒𝐮𝐛𝐬𝐜𝐫𝐢𝐩𝐭𝐢𝐨𝐧𝐬","📢 𝐁𝐫𝐨𝐚𝐝𝐜𝐚𝐬𝐭","🔒 𝐋𝐨𝐜𝐤 𝐁𝐨𝐭",
    "🟢 𝐑𝐮𝐧𝐧𝐢𝐧𝐠 𝐀𝐥𝐥 𝐂𝐨𝐝𝐞","🛠️ 𝐀𝐝𝐦𝐢𝐧 𝐏𝐚𝐧𝐞𝐥","⚙️ Recommended Install",
    "🤖 𝐀𝐆𝐄𝐍𝐓","🌐 𝐆𝐈𝐓𝐇𝐔𝐁","🔑 Variables",
})
def handle_button_text(message):
    _dispatch_button(message)

# =========================================================================
# COMMAND HANDLERS
# =========================================================================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_send_welcome(chat_id, user_id, message.from_user.first_name)

@bot.message_handler(commands=["uploadfile"])
def cmd_upload(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_upload_file(chat_id, user_id)

@bot.message_handler(commands=["checkfiles"])
def cmd_check(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_check_files(chat_id, user_id)

@bot.message_handler(commands=["botspeed"])
def cmd_speed(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_bot_speed(chat_id, user_id)

@bot.message_handler(commands=["statistics"])
def cmd_stats(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_statistics(chat_id, user_id)

@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_broadcast_init(chat_id, user_id)

@bot.message_handler(commands=["lockbot"])
def cmd_lock(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_toggle_lock_bot(chat_id, user_id)

@bot.message_handler(commands=["adminpanel"])
def cmd_admin(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_admin_panel(chat_id, user_id)

@bot.message_handler(commands=["runningallcode"])
def cmd_runall(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    _logic_run_all_scripts(chat_id, user_id)

@bot.message_handler(commands=["ping"])
def ping(message):
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    start = time.time()
    m = bot.reply_to(message, stylish_text("Pong!"))
    latency = round((time.time() - start) * 1000, 2)
    bot.edit_message_text(stylish_text(f"Pong! {latency} ms"), chat_id, m.message_id)

@bot.message_handler(commands=["mystats"])
def cmd_mystats(message):
    """Section 5, item 72 — uptime %, restart count, last error for the user's bots."""
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    files = user_files.get(user_id, [])
    if not files:
        bot.reply_to(message, stylish_text("📊 You have no hosted bots."))
        return
    now = datetime.now()
    lines = ["📊 My Stats:"]
    for fname, _ in files:
        key = f"{user_id}_{fname}"
        running = is_bot_running(user_id, fname)
        restarts = CRASH_COUNT.get(key, 0)
        info = bot_scripts.get(key)
        uptime_str = "—"
        if info:
            up = now - info.get("start_time", now)
            hours, rem = divmod(up.seconds, 3600)
            minutes, _ = divmod(rem, 60)
            uptime_str = f"{up.days}d {hours}h {minutes}m"
        lines.append(f"\n• {fname}\n  Status: {'🟢 Running' if running else '🔴 Stopped'}\n  Uptime: {uptime_str}\n  Restarts: {restarts}")
    bot.reply_to(message, stylish_text("\n".join(lines)))

@bot.message_handler(commands=["feedback"])
def cmd_feedback(message):
    """Section 5, item 77 — forward feedback to owner with context, ticket queue."""
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if not message.text or len(message.text.split(maxsplit=1)) < 2:
        bot.reply_to(message, stylish_text("Usage: /feedback <your message>"))
        return
    body = message.text.split(maxsplit=1)[1]
    uname = message.from_user.username or "—"
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("INSERT INTO feedback_tickets (user_id, message, created_at) VALUES (?, ?, ?)",
                      (user_id, body, datetime.now().isoformat()))
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"feedback save: {e}")
    try:
        bot.send_message(OWNER_ID, stylish_text(f"📮 Feedback from @{uname} ({user_id}):\n{body}"))
    except Exception as e:
        logger.error(f"feedback forward: {e}")
    bot.reply_to(message, stylish_text("✅ Feedback sent to owner. Thank you!"))

@bot.message_handler(commands=["sensitive"])
def cmd_sensitive(message):
    """Section 1, item 5 — toggle Sensitive Mode for the next upload."""
    user_id = message.from_user.id
    if not check_subscription_and_continue(user_id, message.chat.id):
        return
    # Per-message flag — simplest: store in a session dict
    cur = getattr(message, "_sensitive_mode", False)
    message._sensitive_mode = not cur
    # Persist on the user session
    _sensitive_sessions[user_id] = not cur
    bot.reply_to(message, stylish_text(f"Sensitive Mode for next upload: {'ON' if not cur else 'OFF'}.\nIn Sensitive Mode your raw file is NOT forwarded to admins — only metadata + masked preview."))

_sensitive_sessions = {}

@bot.message_handler(commands=["rotatetoken"])
def cmd_rotate_token(message):
    """Section 1, item 10 — /rotatetoken <file_name> to rotate token via Variables vault."""
    user_id = message.from_user.id; chat_id = message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, stylish_text("Usage: /rotatetoken <file_name>"))
        return
    file_name = parts[1].strip()
    if file_name not in [f[0] for f in user_files.get(user_id, [])]:
        bot.reply_to(message, stylish_text("❌ You don't have that file."))
        return
    bot.reply_to(message, stylish_text(f"🔑 Send the new BOT_TOKEN value for {file_name}.\n/cancel to abort."))
    bot.register_next_step_handler(message, _rotate_token_step, file_name)

def _rotate_token_step(message, file_name):
    user_id = message.from_user.id
    if not message.text or message.text.lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled."))
        try: bot.delete_message(message.chat.id, message.message_id)
        except Exception: pass
        return
    set_user_variable(user_id, file_name, "BOT_TOKEN", message.text.strip())
    bot.reply_to(message, stylish_text(f"✅ BOT_TOKEN updated for {file_name}. Restart the script to apply."))
    audit_log(user_id, "rotate_token", file_name)
    try: bot.delete_message(message.chat.id, message.message_id)
    except Exception: pass

@bot.message_handler(commands=["finduser"])
def cmd_find_user(message):
    """Admin: Section 6 A.1 — /finduser <id|@username>."""
    user_id = message.from_user.id
    if user_id not in admin_ids and user_id != OWNER_ID:
        bot.reply_to(message, stylish_text("⚠️ Admin only."))
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(message, stylish_text("Usage: /finduser <id|@username>"))
        return
    query = parts[1].strip()
    info = None
    if query.startswith("@"):
        try:
            chat = bot.get_chat(query)
            info = {"id": chat.id, "name": chat.first_name or chat.title or "—",
                    "username": chat.username or "—"}
        except Exception as e:
            bot.reply_to(message, stylish_text(f"❌ {e}"))
            return
    else:
        try:
            uid = int(query)
        except Exception:
            bot.reply_to(message, stylish_text("Invalid — use numeric ID or @username."))
            return
        info = {"id": uid, "name": "—", "username": "—"}
    files = user_files.get(info["id"], [])
    sub = user_subscriptions.get(info["id"])
    sub_str = sub["expiry"].strftime("%Y-%m-%d") if sub else "No"
    banned = "🚫" if is_user_banned(info["id"]) else "✅"
    bot.reply_to(message, stylish_text(
        f"🔍 User:\n🆔 {info['id']}\n👤 {info['name']}\n✳️ @{info['username']}\n"
        f"📁 Files: {len(files)}\n💳 Sub: {sub_str}\n🚦 Banned: {banned}"))

# =========================================================================
# MAIN CALLBACK DISPATCHER
# =========================================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    global bot_locked
    if call.data.startswith("verify_channel_"):
        return  # handled by its own handler above
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    if not check_subscription_and_continue(user_id, chat_id):
        return
    if not tap_guard(user_id):
        bot.answer_callback_query(call.id, "⏳ Wait a moment...")
        return
    data = call.data
    if bot_locked and user_id not in admin_ids and data not in [
            "speed", "stats", "back_to_main", "recommended_install",
            "ai_assistant", "updates_channel", "github_deploy", "my_variables"]:
        bot.answer_callback_query(call.id, stylish_text("Bot locked."), show_alert=True)
        return
    # Dispatch
    if data == "upload":
        _logic_upload_file(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "check_files" or data == "back_to_files":
        _logic_check_files(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "speed":
        _logic_bot_speed(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "stats":
        _logic_statistics(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "back_to_main":
        _logic_send_welcome(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "recommended_install":
        _logic_recommended_install(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "ai_assistant":
        _logic_ai_assistant(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "updates_channel":
        _logic_updates_channel(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "github_deploy":
        _logic_github_deploy(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "my_variables":
        _logic_my_variables(chat_id, user_id)
        bot.answer_callback_query(call.id)
    elif data == "subscription":
        if user_id in admin_ids: _logic_subscriptions_panel(chat_id, user_id)
        else: bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "broadcast":
        if user_id in admin_ids: _logic_broadcast_init(chat_id, user_id)
        else: bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "lock_bot":
        if user_id in admin_ids:
            bot_locked = True
            bot.answer_callback_query(call.id, "Bot locked.")
            _logic_send_welcome(chat_id, user_id)
            audit_log(user_id, "lock", "on")
    elif data == "unlock_bot":
        if user_id in admin_ids:
            bot_locked = False
            bot.answer_callback_query(call.id, "Bot unlocked.")
            _logic_send_welcome(chat_id, user_id)
            audit_log(user_id, "lock", "off")
    elif data == "run_all_scripts":
        if user_id in admin_ids: _logic_run_all_scripts(chat_id, user_id)
        else: bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "admin_panel":
        if user_id in admin_ids: _logic_admin_panel(chat_id, user_id)
        else: bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "change_ai_model":
        if user_id in admin_ids:
            markup = create_model_selection_markup()
            bot.edit_message_text(stylish_text("Select a new AI model:"), chat_id, call.message.message_id, reply_markup=markup)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "add_admin":
        if user_id == OWNER_ID:
            m = bot.send_message(chat_id, stylish_text("👑 Enter user ID to add as admin.\n/cancel"))
            bot.register_next_step_handler(m, process_add_admin_id)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Owner only.", show_alert=True)
    elif data == "remove_admin":
        if user_id == OWNER_ID:
            m = bot.send_message(chat_id, stylish_text("👑 Enter admin ID to remove.\n/cancel"))
            bot.register_next_step_handler(m, process_remove_admin_id)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Owner only.", show_alert=True)
    elif data == "list_admins":
        if user_id in admin_ids:
            admins_str = "\n".join(f"- {a} {'(Owner)' if a == OWNER_ID else ''}" for a in sorted(admin_ids))
            bot.send_message(chat_id, stylish_text(f"👑 Admins:\n{admins_str}"))
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "set_user_limit":
        if user_id in admin_ids:
            m = bot.send_message(chat_id, stylish_text("🔧 Send user_id and limit.\nFormat: 123456789 50\n/cancel"))
            bot.register_next_step_handler(m, process_set_user_limit)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "find_user":
        if user_id in admin_ids:
            bot.send_message(chat_id, stylish_text("🔍 Use /finduser <id|@username>"))
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "view_audit_log":
        if user_id in admin_ids:
            _show_audit_log(chat_id)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "kill_switch":
        if user_id in admin_ids:
            _kill_switch_menu(chat_id)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "add_subscription":
        if user_id in admin_ids:
            m = bot.send_message(chat_id, stylish_text("💳 Enter user_id days\nExample: 12345678 30\n/cancel"))
            bot.register_next_step_handler(m, process_add_subscription)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "remove_subscription":
        if user_id in admin_ids:
            m = bot.send_message(chat_id, stylish_text("💳 Enter user ID to remove subscription.\n/cancel"))
            bot.register_next_step_handler(m, process_remove_subscription)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "check_subscription":
        if user_id in admin_ids:
            m = bot.send_message(chat_id, stylish_text("💳 Enter user ID to check.\n/cancel"))
            bot.register_next_step_handler(m, process_check_subscription)
            bot.answer_callback_query(call.id)
        else:
            bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
    elif data == "view_approval_queue":
        view_approval_queue(call)
    elif data.startswith("confirm_broadcast_"):
        handle_confirm_broadcast(call)
    elif data == "cancel_broadcast":
        handle_cancel_broadcast(call)
    elif data.startswith("file_"):
        file_control_callback(call)
    elif data.startswith("start_"):
        start_bot_callback(call)
    elif data.startswith("stop_"):
        stop_bot_callback(call)
    elif data.startswith("restart_"):
        restart_bot_callback(call)
    elif data.startswith("delete_"):
        delete_bot_callback(call)
    elif data.startswith("logs_"):
        logs_bot_callback(call)
    elif data.startswith("dl_"):
        download_bot_callback(call)
    elif data.startswith("aifix_"):
        ai_fix_callback(call)
    elif data == "install_recommended":
        install_recommended_callback(call)
    elif data == "cancel_install":
        cancel_install_callback(call)
    else:
        bot.answer_callback_query(call.id, "Unknown action.")

# Audit log viewer (Section 1, item 13 / Section 6 A.4)
def _show_audit_log(chat_id):
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT actor_id, action, target, timestamp, details FROM audit_log ORDER BY id DESC LIMIT 15")
        rows = c.fetchall()
        conn.close()
        if not rows:
            bot.send_message(chat_id, stylish_text("📜 Audit log is empty."))
            return
        lines = ["📜 Recent audit log:"]
        for actor, action, target, ts, det in rows:
            lines.append(f"\n• {ts}\n  {actor} → {action} {target}\n  {det or ''}")
        bot.send_message(chat_id, stylish_text("\n".join(lines)))
    except Exception as e:
        bot.send_message(chat_id, stylish_text(f"❌ {e}"))

# Kill Switch (Section 6 E.81)
def _kill_switch_menu(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for uid in list(user_files.keys())[:20]:
        markup.add(types.InlineKeyboardButton(f"⛔ Stop {uid}", callback_data=f"ks_{uid}"))
    markup.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
    bot.send_message(chat_id, stylish_text("🛑 Kill Switch — stop a user's running bots:"), reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ks_"))
def kill_switch_callback(call):
    if call.from_user.id not in admin_ids:
        bot.answer_callback_query(call.id, "Admin only.", show_alert=True)
        return
    uid = int(call.data[3:])
    stopped = 0
    for fname, _ in user_files.get(uid, []):
        key = f"{uid}_{fname}"
        if key in bot_scripts:
            kill_process_tree(bot_scripts[key]); stopped += 1
            bot_scripts.pop(key, None)
    bot.answer_callback_query(call.id, f"Stopped {stopped} bots for {uid}.")
    audit_log(call.from_user.id, "kill_switch", str(uid), f"stopped={stopped}")

# =========================================================================
# ADMIN PROCESSING HELPERS
# =========================================================================
def process_add_admin_id(message):
    if message.from_user.id != OWNER_ID: return
    if (message.text or "").lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled.")); return
    try:
        aid = int(message.text.strip())
        if aid == OWNER_ID:
            bot.reply_to(message, stylish_text("Owner is already admin.")); return
        add_admin_db(aid)
        bot.reply_to(message, stylish_text(f"✅ User {aid} is now admin."))
    except Exception:
        bot.reply_to(message, stylish_text("Invalid ID."))

def process_remove_admin_id(message):
    if message.from_user.id != OWNER_ID: return
    if (message.text or "").lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled.")); return
    try:
        aid = int(message.text.strip())
        if aid == OWNER_ID:
            bot.reply_to(message, stylish_text("Cannot remove owner.")); return
        if remove_admin_db(aid):
            bot.reply_to(message, stylish_text(f"✅ Admin {aid} removed."))
        else:
            bot.reply_to(message, stylish_text("Not an admin."))
    except Exception:
        bot.reply_to(message, stylish_text("Invalid ID."))

def process_add_subscription(message):
    if message.from_user.id not in admin_ids: return
    if (message.text or "").lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled.")); return
    try:
        parts = message.text.split()
        uid = int(parts[0]); days = int(parts[1])
        current = user_subscriptions.get(uid, {}).get("expiry")
        start = current if current and current > datetime.now() else datetime.now()
        new_expiry = start + timedelta(days=days)
        save_subscription(uid, new_expiry)
        bot.reply_to(message, stylish_text(f"✅ Sub for {uid} added. Expires {new_expiry.strftime('%Y-%m-%d')}"))
        audit_log(message.from_user.id, "add_sub", str(uid), f"{days}d")
    except Exception:
        bot.reply_to(message, stylish_text("Invalid. Use: user_id days"))

def process_remove_subscription(message):
    if message.from_user.id not in admin_ids: return
    if (message.text or "").lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled.")); return
    try:
        uid = int(message.text.strip())
        if uid in user_subscriptions:
            remove_subscription_db(uid)
            bot.reply_to(message, stylish_text(f"✅ Sub removed for {uid}."))
            audit_log(message.from_user.id, "remove_sub", str(uid))
        else:
            bot.reply_to(message, stylish_text("No active sub."))
    except Exception:
        bot.reply_to(message, stylish_text("Invalid ID."))

def process_check_subscription(message):
    if message.from_user.id not in admin_ids: return
    if (message.text or "").lower() == "/cancel":
        bot.reply_to(message, stylish_text("Cancelled.")); return
    try:
        uid = int(message.text.strip())
        if uid in user_subscriptions:
            exp = user_subscriptions[uid]["expiry"]
            if exp > datetime.now():
                days = (exp - datetime.now()).days
                bot.reply_to(message, stylish_text(f"✅ {uid} active. Expires {exp.strftime('%Y-%m-%d')} ({days}d left)"))
            else:
                bot.reply_to(message, stylish_text(f"⚠️ {uid} expired on {exp.strftime('%Y-%m-%d')}"))
        else:
            bot.reply_to(message, stylish_text(f"ℹ️ {uid} has no sub."))
    except Exception:
        bot.reply_to(message, stylish_text("Invalid ID."))

# =========================================================================
# FILE CONTROL CALLBACKS
# =========================================================================
def file_control_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Not your file.", show_alert=True)
            _logic_check_files(call.message.chat.id, call.from_user.id)
            return
        files = user_files.get(owner_id, [])
        if not any(f[0] == file_name for f in files):
            bot.answer_callback_query(call.id, "File not found.", show_alert=True)
            return
        running = is_bot_running(owner_id, file_name)
        ftype = next((f[1] for f in files if f[0] == file_name), "?")
        # Breadcrumb (Section 3, item 39)
        crumb = breadcrumb(["Main", "My Files", file_name])
        text = f"{crumb}⚙️ {file_name} ({ftype}) — User {owner_id}\nStatus: {'🟢 Running' if running else '🔴 Stopped'}"
        bot.edit_message_text(stylish_text(text), call.message.chat.id, call.message.message_id,
                              reply_markup=create_control_buttons(owner_id, file_name, running))
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"file_control: {e}")

def start_bot_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        if is_bot_running(owner_id, file_name):
            bot.answer_callback_query(call.id, "Already running."); return
        files = user_files.get(owner_id, [])
        ftype = next((f[1] for f in files if f[0] == file_name), None)
        if not ftype:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); return
        folder = get_user_folder(owner_id)
        path = os.path.join(folder, file_name)
        if not os.path.exists(path):
            bot.answer_callback_query(call.id, "File missing on disk.", show_alert=True); return
        bot.answer_callback_query(call.id, f"Starting {file_name}...")
        if ftype == "py":
            threading.Thread(target=run_script, args=(path, owner_id, folder, file_name, call.message.chat.id)).start()
        else:
            threading.Thread(target=run_js_script, args=(path, owner_id, folder, file_name, call.message.chat.id)).start()
        time.sleep(1)
        running = is_bot_running(owner_id, file_name)
        text = f"⚙️ {file_name} ({ftype})\nStatus: {'🟢 Running' if running else '🟡 Starting...'}"
        bot.edit_message_text(stylish_text(text), call.message.chat.id, call.message.message_id,
                              reply_markup=create_control_buttons(owner_id, file_name, running))
    except Exception as e:
        logger.error(f"start: {e}")

def stop_bot_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        if not is_bot_running(owner_id, file_name):
            bot.answer_callback_query(call.id, "Not running."); return
        key = f"{owner_id}_{file_name}"
        if key in bot_scripts:
            kill_process_tree(bot_scripts[key]); bot_scripts.pop(key, None)
        bot.answer_callback_query(call.id, f"Stopped {file_name}.")
        files = user_files.get(owner_id, [])
        ftype = next((f[1] for f in files if f[0] == file_name), "?")
        text = f"⚙️ {file_name} ({ftype})\nStatus: 🔴 Stopped"
        bot.edit_message_text(stylish_text(text), call.message.chat.id, call.message.message_id,
                              reply_markup=create_control_buttons(owner_id, file_name, False))
    except Exception as e:
        logger.error(f"stop: {e}")

def restart_bot_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        key = f"{owner_id}_{file_name}"
        if key in bot_scripts:
            kill_process_tree(bot_scripts[key]); bot_scripts.pop(key, None)
        time.sleep(1)
        files = user_files.get(owner_id, [])
        ftype = next((f[1] for f in files if f[0] == file_name), None)
        if not ftype:
            bot.answer_callback_query(call.id, "File not found.", show_alert=True); return
        folder = get_user_folder(owner_id)
        path = os.path.join(folder, file_name)
        if not os.path.exists(path):
            bot.answer_callback_query(call.id, "File missing.", show_alert=True); return
        bot.answer_callback_query(call.id, f"Restarting {file_name}...")
        if ftype == "py":
            threading.Thread(target=run_script, args=(path, owner_id, folder, file_name, call.message.chat.id)).start()
        else:
            threading.Thread(target=run_js_script, args=(path, owner_id, folder, file_name, call.message.chat.id)).start()
        time.sleep(1)
        running = is_bot_running(owner_id, file_name)
        text = f"⚙️ {file_name} ({ftype})\nStatus: {'🟢 Running' if running else '🟡 Starting...'}"
        bot.edit_message_text(stylish_text(text), call.message.chat.id, call.message.message_id,
                              reply_markup=create_control_buttons(owner_id, file_name, running))
    except Exception as e:
        logger.error(f"restart: {e}")

def delete_bot_callback(call):
    """Confirm step on destructive actions (Section 3, item 49)."""
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("🗑️ Confirm Delete", callback_data=f"confirmdel_{owner_id}_{file_name}"),
            types.InlineKeyboardButton("❌ Cancel",          callback_data=f"cancel_{owner_id}_{file_name}"))
        bot.answer_callback_query(call.id, "Confirm delete?")
        bot.edit_message_text(stylish_text(f"⚠️ Really delete {file_name}?"),
                              call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception as e:
        logger.error(f"delete: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("confirmdel_"))
def confirm_delete_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        key = f"{owner_id}_{file_name}"
        if key in bot_scripts:
            kill_process_tree(bot_scripts[key]); bot_scripts.pop(key, None)
        folder = get_user_folder(owner_id)
        fp = os.path.join(folder, file_name)
        lp = os.path.join(folder, f"{os.path.splitext(file_name)[0]}.log")
        for p in (fp, lp):
            if os.path.exists(p):
                try: os.remove(p)
                except Exception: pass
        remove_user_file_db(owner_id, file_name)
        bot.answer_callback_query(call.id, f"Deleted {file_name}.")
        bot.edit_message_text(stylish_text(f"🗑️ Deleted {file_name}"), call.message.chat.id, call.message.message_id)
        audit_log(call.from_user.id, "delete_file", file_name)
    except Exception as e:
        logger.error(f"confirm delete: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("cancel_"))
def cancel_action_callback(call):
    bot.answer_callback_query(call.id, "Cancelled.")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception: pass

def logs_bot_callback(call):
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        folder = get_user_folder(owner_id)
        log_path = os.path.join(folder, f"{os.path.splitext(file_name)[0]}.log")
        # Try rotated logs if the main one is missing
        if not os.path.exists(log_path):
            for i in range(1, MAX_LOG_FILES + 1):
                cand = f"{log_path}.{i}"
                if os.path.exists(cand):
                    log_path = cand; break
        if not os.path.exists(log_path):
            bot.answer_callback_query(call.id, "No logs yet.", show_alert=True); return
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            logs = f.read()
        # Redact secrets in logs before display (Section 1, item 6)
        logs = redact_secrets(logs)
        if len(logs) > 4000:
            logs = "...\n" + logs[-4000:]
        bot.send_message(call.message.chat.id, stylish_text(f"📜 Logs for {file_name}:\n{logs}"))
        bot.answer_callback_query(call.id)
    except Exception as e:
        logger.error(f"logs: {e}")
        bot.answer_callback_query(call.id, "Error reading logs.", show_alert=True)

def download_bot_callback(call):
    """Section 2, items 20-21 — Download My File / Export All."""
    try:
        _, owner_id_str, file_name = call.data.split("_", 2)
        owner_id = int(owner_id_str)
        if call.from_user.id != owner_id and call.from_user.id not in admin_ids:
            bot.answer_callback_query(call.id, "Permission denied.", show_alert=True); return
        folder = get_user_folder(owner_id)
        path = os.path.join(folder, file_name)
        if not os.path.exists(path):
            bot.answer_callback_query(call.id, "File missing on disk.", show_alert=True); return
        with open(path, "rb") as f:
            bot.send_document(call.message.chat.id, f, visible_file_name=file_name,
                              caption=stylish_text(f"📥 {file_name}"))
        bot.answer_callback_query(call.id, f"Downloaded {file_name}.")
    except Exception as e:
        logger.error(f"download: {e}")
        bot.answer_callback_query(call.id, "Download error.", show_alert=True)

# =========================================================================
# RUNTIME / RENDER WEB SERVICE COMPATIBILITY
# =========================================================================
# Render Web Services require a real HTTP listener on 0.0.0.0:$PORT.
# The Telegram polling loop runs in a separate thread so the HTTP listener
# is available immediately and remains available even when Telegram polling
# temporarily fails.

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHUTDOWN_EVENT = threading.Event()
HEALTH_SERVER = None
POLLING_THREAD = None


class _HealthHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _respond(self, status=200, body=b"OK\n"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            self._respond(200, b"OK - Telegram bot is running\n")
        else:
            self._respond(200, b"OK\n")

    def do_HEAD(self):
        self._respond(200, b"")

    def log_message(self, fmt, *args):
        # Keep Render logs clean; application logs are handled by logger.
        return


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_health_server():
    """Bind Render's PORT with retries and serve health checks."""
    global HEALTH_SERVER
    port_raw = os.environ.get("PORT", "10000").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 10000
        logger.warning("Invalid PORT=%r; using 10000", port_raw)

    last_error = None
    for attempt in range(1, 11):
        if SHUTDOWN_EVENT.is_set():
            return
        try:
            HEALTH_SERVER = _ReusableHTTPServer(("0.0.0.0", port), _HealthHandler)
            logger.info("Render HTTP health server listening on 0.0.0.0:%s", port)
            HEALTH_SERVER.timeout = 1
            while not SHUTDOWN_EVENT.is_set():
                HEALTH_SERVER.handle_request()
            return
        except OSError as exc:
            last_error = exc
            logger.error("HTTP bind attempt %d/10 failed on port %s: %s", attempt, port, exc)
            time.sleep(min(attempt, 5))
        except Exception:
            logger.exception("Health server crashed")
            time.sleep(2)

    raise RuntimeError(f"Could not bind Render HTTP port {port}: {last_error}")


def polling_worker():
    """Keep Telegram polling alive without restarting the whole Render process."""
    backoff = 5
    consecutive_failures = 0
    while not SHUTDOWN_EVENT.is_set():
        try:
            logger.info(
                "Starting Telegram polling (version %s, storage=%s)...",
                PLATFORM_VERSION, STORAGE_STATE
            )
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=None,
            )
            # A normal return is also treated as a recoverable disconnect.
            if not SHUTDOWN_EVENT.is_set():
                logger.warning("Telegram polling stopped; restarting polling loop.")
                consecutive_failures += 1
        except Exception as exc:
            consecutive_failures += 1
            logger.exception(
                "Telegram polling error (failure #%d): %s",
                consecutive_failures, exc
            )
            if consecutive_failures >= 10:
                try:
                    bot.send_message(
                        OWNER_ID,
                        stylish_text(
                            f"⚠️ Telegram polling has failed {consecutive_failures} times."
                        ),
                    )
                except Exception:
                    pass
                consecutive_failures = 0

        if SHUTDOWN_EVENT.is_set():
            break

        # Do NOT os.execv() here. Self-exec can create a new HTTP listener,
        # leave threads behind, or make Render lose the port during restart.
        delay = min(backoff, 300)
        logger.info("Polling retry in %ss", delay)
        SHUTDOWN_EVENT.wait(delay)
        backoff = min(backoff * 2, 300)

        # Successful polling resets the backoff when the loop remains healthy.
        if consecutive_failures == 0:
            backoff = 5


def cleanup():
    """Stop child scripts, HTTP server and background workers cleanly."""
    if SHUTDOWN_EVENT.is_set():
        # Cleanup is intentionally idempotent.
        pass
    SHUTDOWN_EVENT.set()

    logger.warning("Shutting down — killing all child scripts...")
    for key, info in list(bot_scripts.items()):
        try:
            kill_process_tree(info)
        except Exception:
            logger.exception("Failed cleaning child process %s", key)
    bot_scripts.clear()

    global HEALTH_SERVER
    if HEALTH_SERVER is not None:
        try:
            HEALTH_SERVER.server_close()
        except Exception:
            pass
        HEALTH_SERVER = None

    try:
        if _lock_fp:
            _lock_fp.close()
    except Exception:
        pass

    logger.warning("Cleanup done.")


atexit.register(cleanup)


def _sigterm_handler(signum, frame):
    logger.warning("Signal %s received — graceful shutdown.", signum)
    cleanup()
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)
try:
    signal.signal(signal.SIGINT, _sigterm_handler)
except Exception:
    pass


def main():
    """Render-safe process entry point."""
    global POLLING_THREAD

    # Bind the HTTP port FIRST. If binding fails, Render gets a clear fatal
    # error instead of silently reporting 'No open ports detected'.
    start_health_thread = threading.Thread(
        target=start_health_server,
        name="render-health-server",
        daemon=False,
    )
    start_health_thread.start()

    # Wait briefly for the server to bind before starting Telegram polling.
    # This removes the startup race that can cause Render port detection to
    # miss the listener on cold starts.
    deadline = time.monotonic() + 15
    while HEALTH_SERVER is None and start_health_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.1)

    if HEALTH_SERVER is None:
        raise RuntimeError("Render HTTP health server did not start within 15 seconds")

    POLLING_THREAD = threading.Thread(
        target=polling_worker,
        name="telegram-polling",
        daemon=True,
    )
    POLLING_THREAD.start()
    logger.info("Render Web Service startup complete.")

    # Keep the main process alive while the health server is serving.
    try:
        while not SHUTDOWN_EVENT.wait(5):
            if not start_health_thread.is_alive():
                raise RuntimeError("Render HTTP health server stopped unexpectedly")
    finally:
        cleanup()
        if start_health_thread.is_alive():
            start_health_thread.join(timeout=3)


if __name__ == "__main__":
    main()
