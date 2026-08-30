import os
import re
import asyncio
import sqlite3
import socket
import base64
import hashlib
import json
import logging
import traceback
import sys
import html
import random
import time
from datetime import datetime, timedelta
from urllib.parse import quote, unquote, urlparse, parse_qs, urlencode, urlunparse
import httpx
import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import BadRequest, TimedOut, RetryAfter

# ======================================================================
# متغیرهای محیطی
# ======================================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")
MAIN_ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
if not MAIN_ADMIN_ID:
    raise ValueError("ADMIN_ID environment variable not set")

RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN", "")
RAILWAY_PROJECT_ID = os.getenv("RAILWAY_PROJECT_ID", "")
CREDIT_THRESHOLD = float(os.getenv("CREDIT_THRESHOLD", "0.05"))

# ======================================================================
# مسیر پایدار
# ======================================================================
DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
os.makedirs(BACKUP_DIR, exist_ok=True)

# ======================================================================
# تنظیم لاگ
# ======================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log"), mode='a', encoding='utf-8')
    ]
)
log = logging.getLogger("bot")

# ======================================================================
# منطقه زمانی تهران
# ======================================================================
TEHRAN_TZ = pytz.timezone('Asia/Tehran')

def get_tehran_time() -> str:
    return datetime.now(TEHRAN_TZ).strftime('%Y-%m-%d %H:%M:%S')

def get_tehran_date() -> str:
    return datetime.now(TEHRAN_TZ).strftime('%Y-%m-%d')

# ======================================================================
# اتصال به دیتابیس
# ======================================================================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
c = conn.cursor()

def ensure_column(table, column, col_type, default=None):
    try:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        conn.commit()
        if default is not None:
            c.execute(f"UPDATE {table} SET {column}=?", (default,))
            conn.commit()
    except sqlite3.OperationalError:
        pass

# ======================================================================
# ایجاد جداول
# ======================================================================
c.execute("""CREATE TABLE IF NOT EXISTS seen (
    uuid TEXT,
    address TEXT,
    source TEXT DEFAULT '',
    first_seen TEXT,
    last_posted TEXT,
    profile_id INTEGER DEFAULT 1,
    full_url TEXT DEFAULT '',
    backup_num INTEGER DEFAULT 0,
    UNIQUE(uuid, address, profile_id))""")

c.execute("""CREATE TABLE IF NOT EXISTS cfg (
    k TEXT PRIMARY KEY,
    v TEXT)""")

c.execute("""CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    count INTEGER,
    created_at TEXT)""")

ensure_column("seen", "source", "TEXT DEFAULT ''")
ensure_column("seen", "profile_id", "INTEGER DEFAULT 1", 1)
ensure_column("seen", "full_url", "TEXT DEFAULT ''", "")
ensure_column("seen", "backup_num", "INTEGER DEFAULT 0", 0)

c.execute("""CREATE TABLE IF NOT EXISTS country_cache (
    ip TEXT PRIMARY KEY,
    country TEXT,
    flag TEXT)""")

c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
    profile_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    button_text TEXT DEFAULT 'Advertisement',
    enabled INTEGER DEFAULT 1,
    color TEXT DEFAULT 'primary',
    created_at TEXT)""")
ensure_column("sponsors", "color", "TEXT DEFAULT 'primary'", "primary")

c.execute("""CREATE TABLE IF NOT EXISTS last_scrape (
    source TEXT,
    last_scrape_time TEXT,
    profile_id INTEGER DEFAULT 1,
    last_message_id TEXT DEFAULT '',
    UNIQUE(source, profile_id))""")
ensure_column("last_scrape", "last_message_id", "TEXT DEFAULT ''", "")

c.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
    source TEXT,
    message_id INTEGER,
    profile_id INTEGER DEFAULT 1,
    PRIMARY KEY(source, message_id, profile_id))""")
ensure_column("processed_messages", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS proxies_seen (
    proxy_url TEXT,
    first_seen TEXT,
    last_posted TEXT,
    profile_id INTEGER DEFAULT 1,
    UNIQUE(proxy_url, profile_id))""")
ensure_column("proxies_seen", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dest_name TEXT UNIQUE NOT NULL,
    sources TEXT DEFAULT '',
    banner_config TEXT,
    banner_proxy TEXT,
    interval_min INTEGER DEFAULT 5,
    max_post INTEGER DEFAULT 8,
    max_proxies INTEGER DEFAULT 10,
    post_configs INTEGER DEFAULT 1,
    post_proxies INTEGER DEFAULT 1,
    ping_mode TEXT DEFAULT 'iran',
    last_num INTEGER DEFAULT 0,
    created_at TEXT,
    show_numbers INTEGER DEFAULT 1,
    custom_query TEXT DEFAULT '',
    show_date_config INTEGER DEFAULT 1,
    show_date_proxy INTEGER DEFAULT 1,
    schedule_cron TEXT DEFAULT '',
    last_backup_count INTEGER DEFAULT 0,
    timer_expiry TEXT DEFAULT NULL,
    timer_duration INTEGER DEFAULT 0,
    backup_interval INTEGER DEFAULT 1000,
    interval_config INTEGER DEFAULT 5,
    interval_proxy INTEGER DEFAULT 5,
    max_post_config INTEGER DEFAULT 8,
    max_post_proxy INTEGER DEFAULT 10,
    naming_template TEXT DEFAULT '{Flag} | ⚡️Telegram = {CHANNEL_ID}',
    channel_link TEXT DEFAULT '',
    ping_enabled INTEGER DEFAULT 1,
    profile_enabled INTEGER DEFAULT 1
)""")
conn.commit()

ensure_column("profiles", "show_numbers", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "custom_query", "TEXT DEFAULT ''", "")
ensure_column("profiles", "show_date_config", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "show_date_proxy", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "schedule_cron", "TEXT DEFAULT ''", "")
ensure_column("profiles", "last_backup_count", "INTEGER DEFAULT 0", 0)
ensure_column("profiles", "timer_expiry", "TEXT DEFAULT NULL", None)
ensure_column("profiles", "timer_duration", "INTEGER DEFAULT 0", 0)
ensure_column("profiles", "backup_interval", "INTEGER DEFAULT 1000", 1000)
ensure_column("profiles", "interval_config", "INTEGER DEFAULT 5", 5)
ensure_column("profiles", "interval_proxy", "INTEGER DEFAULT 5", 5)
ensure_column("profiles", "max_post_config", "INTEGER DEFAULT 8", 8)
ensure_column("profiles", "max_post_proxy", "INTEGER DEFAULT 10", 10)
ensure_column("profiles", "naming_template", "TEXT DEFAULT '{Flag} | ⚡️Telegram = {CHANNEL_ID}'", "{Flag} | ⚡️Telegram = {CHANNEL_ID}")
ensure_column("profiles", "channel_link", "TEXT DEFAULT ''", "")
ensure_column("profiles", "ping_enabled", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "profile_enabled", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    word TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(profile_id, word))""")

c.execute("""CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT)""")
ensure_column("admins", "added_by", "INTEGER", 0)
ensure_column("admins", "added_at", "TEXT", "")
conn.commit()

# ======================================================================
# تعمیر نوع ستون custom_query
# ======================================================================
def fix_column_types():
    c.execute("PRAGMA table_info(profiles)")
    cols = c.fetchall()
    for col in cols:
        if col[1] == "custom_query" and "TEXT" not in col[2].upper():
            log.warning("custom_query column is not TEXT, fixing...")
            c.execute("""
                CREATE TABLE profiles_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dest_name TEXT UNIQUE NOT NULL,
                    sources TEXT DEFAULT '',
                    banner_config TEXT,
                    banner_proxy TEXT,
                    interval_min INTEGER DEFAULT 5,
                    max_post INTEGER DEFAULT 8,
                    max_proxies INTEGER DEFAULT 10,
                    post_configs INTEGER DEFAULT 1,
                    post_proxies INTEGER DEFAULT 1,
                    ping_mode TEXT DEFAULT 'iran',
                    last_num INTEGER DEFAULT 0,
                    created_at TEXT,
                    show_numbers INTEGER DEFAULT 1,
                    custom_query TEXT DEFAULT '',
                    show_date_config INTEGER DEFAULT 1,
                    show_date_proxy INTEGER DEFAULT 1,
                    schedule_cron TEXT DEFAULT '',
                    last_backup_count INTEGER DEFAULT 0,
                    timer_expiry TEXT DEFAULT NULL,
                    timer_duration INTEGER DEFAULT 0,
                    backup_interval INTEGER DEFAULT 1000,
                    interval_config INTEGER DEFAULT 5,
                    interval_proxy INTEGER DEFAULT 5,
                    max_post_config INTEGER DEFAULT 8,
                    max_post_proxy INTEGER DEFAULT 10,
                    naming_template TEXT DEFAULT '{Flag} | ⚡️Telegram = {CHANNEL_ID}',
                    channel_link TEXT DEFAULT '',
                    ping_enabled INTEGER DEFAULT 1,
                    profile_enabled INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                INSERT INTO profiles_new
                    (id, dest_name, sources, banner_config, banner_proxy, interval_min,
                     max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num,
                     created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                     schedule_cron, last_backup_count, timer_expiry, timer_duration, backup_interval,
                     interval_config, interval_proxy, max_post_config, max_post_proxy,
                     naming_template, channel_link, ping_enabled, profile_enabled)
                SELECT id, dest_name, sources, banner_config, banner_proxy, interval_min,
                       max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num,
                       created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                       schedule_cron, last_backup_count, timer_expiry, timer_duration, backup_interval,
                       interval_config, interval_proxy, max_post_config, max_post_proxy,
                       naming_template, channel_link, ping_enabled, profile_enabled
                FROM profiles
            """)
            c.execute("DROP TABLE profiles")
            c.execute("ALTER TABLE profiles_new RENAME TO profiles")
            conn.commit()
            log.info("✅ custom_query column fixed to TEXT.")
            return
    log.info("✅ custom_query column is TEXT (no fix needed).")

fix_column_types()

# ======================================================================
# مهاجرت جداول
# ======================================================================
def migrate_tables_for_profile_isolation():
    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='seen'")
        row = c.fetchone()
        if row and "UNIQUE(uuid, address, profile_id)" not in row[0]:
            log.warning("Migrating seen table...")
            c.execute("ALTER TABLE seen RENAME TO seen_old")
            c.execute("""
                CREATE TABLE seen (
                    uuid TEXT,
                    address TEXT,
                    source TEXT DEFAULT '',
                    first_seen TEXT,
                    last_posted TEXT,
                    profile_id INTEGER DEFAULT 1,
                    full_url TEXT DEFAULT '',
                    backup_num INTEGER DEFAULT 0,
                    UNIQUE(uuid, address, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO seen (uuid, address, source, first_seen, last_posted, profile_id, full_url, backup_num)
                SELECT uuid, address, source, first_seen, last_posted, profile_id, full_url, backup_num FROM seen_old
            """)
            c.execute("DROP TABLE seen_old")
            conn.commit()
            log.info("✅ seen table migrated.")
    except Exception as e:
        log.error(f"seen migration failed: {e}")

    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='proxies_seen'")
        row = c.fetchone()
        if row and "UNIQUE(proxy_url, profile_id)" not in row[0]:
            log.warning("Migrating proxies_seen table...")
            c.execute("ALTER TABLE proxies_seen RENAME TO proxies_seen_old")
            c.execute("""
                CREATE TABLE proxies_seen (
                    proxy_url TEXT,
                    first_seen TEXT,
                    last_posted TEXT,
                    profile_id INTEGER DEFAULT 1,
                    UNIQUE(proxy_url, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO proxies_seen (proxy_url, first_seen, last_posted, profile_id)
                SELECT proxy_url, first_seen, last_posted, profile_id FROM proxies_seen_old
            """)
            c.execute("DROP TABLE proxies_seen_old")
            conn.commit()
            log.info("✅ proxies_seen table migrated.")
    except Exception as e:
        log.error(f"proxies_seen migration failed: {e}")

    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='processed_messages'")
        row = c.fetchone()
        if row and "PRIMARY KEY(source, message_id, profile_id)" not in row[0]:
            log.warning("Migrating processed_messages table...")
            c.execute("ALTER TABLE processed_messages RENAME TO processed_messages_old")
            c.execute("""
                CREATE TABLE processed_messages (
                    source TEXT,
                    message_id INTEGER,
                    profile_id INTEGER DEFAULT 1,
                    PRIMARY KEY(source, message_id, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO processed_messages (source, message_id, profile_id)
                SELECT source, message_id, profile_id FROM processed_messages_old
            """)
            c.execute("DROP TABLE processed_messages_old")
            conn.commit()
            log.info("✅ processed_messages table migrated.")
    except Exception as e:
        log.error(f"processed_messages migration failed: {e}")

    try:
        c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='last_scrape'")
        row = c.fetchone()
        if row and "UNIQUE(source, profile_id)" not in row[0]:
            log.warning("Migrating last_scrape table...")
            c.execute("ALTER TABLE last_scrape RENAME TO last_scrape_old")
            c.execute("""
                CREATE TABLE last_scrape (
                    source TEXT,
                    last_scrape_time TEXT,
                    profile_id INTEGER DEFAULT 1,
                    last_message_id TEXT DEFAULT '',
                    UNIQUE(source, profile_id)
                )
            """)
            c.execute("""
                INSERT INTO last_scrape (source, last_scrape_time, profile_id, last_message_id)
                SELECT source, last_scrape_time, profile_id, '' FROM last_scrape_old
            """)
            c.execute("DROP TABLE last_scrape_old")
            conn.commit()
            log.info("✅ last_scrape table migrated.")
    except Exception as e:
        log.error(f"last_scrape migration failed: {e}")

migrate_tables_for_profile_isolation()

# ======================================================================
# مهاجرت از تنظیمات قدیمی
# ======================================================================
def migrate_old_config():
    existing = c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    if existing > 0:
        return

    def old_cfg(k, default=""):
        r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    old_dests = old_cfg("destinations", "@VaslZone")
    old_sources = old_cfg("sources", "@Cfox_Server")
    old_banner_config = old_cfg("banner_config", "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری")
    old_banner_proxy = old_cfg("banner_proxy", "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━")
    old_interval = int(old_cfg("interval_min", "5"))
    old_max_post = int(old_cfg("max_post", "8"))
    old_max_proxies = int(old_cfg("max_proxies", "10"))
    old_post_configs = int(old_cfg("post_configs", "1"))
    old_post_proxies = int(old_cfg("post_proxies", "1"))
    old_ping_mode = old_cfg("ping_mode", "iran")
    old_last_num = int(old_cfg("last_num", "0"))

    dest_list = [x.strip() for x in old_dests.split(",") if x.strip()]
    if not dest_list:
        dest_list = ["@VaslZone"]

    for dest in dest_list:
        c.execute("""INSERT INTO profiles
            (dest_name, sources, banner_config, banner_proxy, interval_min,
             max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at,
             show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count,
             timer_expiry, timer_duration, backup_interval,
             interval_config, interval_proxy, max_post_config, max_post_proxy,
             naming_template, channel_link, ping_enabled, profile_enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             datetime.now().isoformat(), 1, "", 1, 1, "", 0, None, 0, 1000,
             old_interval, old_interval, old_max_post, old_max_proxies,
             "{Flag} | ⚡️Telegram = {CHANNEL_ID}", "", 1, 1))
    conn.commit()
    log.info(f"✅ Migrated {len(dest_list)} profiles.")

migrate_old_config()

# ======================================================================
# توابع کمکی
# ======================================================================
def clean_source_name(name: str) -> str:
    if not name:
        return ""
    name = name.strip()
    if "t.me/" in name.lower():
        match = re.search(r't\.me/([^/?]+)', name, re.IGNORECASE)
        if match:
            name = match.group(1)
    if name and not name.startswith("@"):
        name = "@" + name
    cleaned = re.sub(r'[^@a-zA-Z0-9_]', '', name)
    if len(cleaned) <= 1:
        if re.match(r'^@[a-zA-Z0-9_]+$', name):
            return name
        return ""
    return cleaned

def normalize_channel_input(text: str) -> str:
    return clean_source_name(text)

# ======================================================================
# توابع پروفایل
# ======================================================================
def get_profiles():
    c.execute("SELECT * FROM profiles ORDER BY id")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    profiles = []
    for row in rows:
        prof = dict(zip(cols, row))
        profiles.append(prof)
    return profiles

def get_profile(profile_id):
    c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,))
    row = c.fetchone()
    if not row:
        return None
    cols = [d[0] for d in c.description]
    return dict(zip(cols, row))

def create_profile(dest_name, sources="", banner_config=None, banner_proxy=None,
                   interval_min=5, max_post=8, max_proxies=10,
                   post_configs=1, post_proxies=1, ping_mode="iran", last_num=0,
                   show_numbers=1, custom_query="",
                   show_date_config=1, show_date_proxy=1, schedule_cron="", backup_interval=1000,
                   interval_config=5, interval_proxy=5, max_post_config=8, max_post_proxy=10,
                   naming_template="{Flag} | ⚡️Telegram = {CHANNEL_ID}", channel_link="",
                   ping_enabled=1, profile_enabled=1):
    if not banner_config:
        banner_config = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    if not banner_proxy:
        banner_proxy = "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━"
    c.execute("""INSERT INTO profiles
        (dest_name, sources, banner_config, banner_proxy, interval_min,
         max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at,
         show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count,
         timer_expiry, timer_duration, backup_interval,
         interval_config, interval_proxy, max_post_config, max_post_proxy,
         naming_template, channel_link, ping_enabled, profile_enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, last_num,
         get_tehran_time(), show_numbers, custom_query,
         show_date_config, show_date_proxy, schedule_cron, 0, None, 0, backup_interval,
         interval_config, interval_proxy, max_post_config, max_post_proxy,
         naming_template, channel_link, ping_enabled, profile_enabled))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "last_num",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy",
               "schedule_cron", "last_backup_count", "timer_expiry", "timer_duration",
               "backup_interval", "interval_config", "interval_proxy", "max_post_config", "max_post_proxy",
               "naming_template", "channel_link", "ping_enabled", "profile_enabled"]
    for key, value in kwargs.items():
        if key in allowed:
            c.execute(f"UPDATE profiles SET {key}=? WHERE id=?", (value, profile_id))
    conn.commit()
    log.info(f"Updated profile {profile_id}: {kwargs}")

def delete_profile(profile_id):
    c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM last_scrape WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM processed_messages WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM blacklist WHERE profile_id=?", (profile_id,))
    conn.commit()

def get_profile_interval_config(profile_id):
    prof = get_profile(profile_id)
    return prof.get("interval_config", 5) if prof else 5

def set_profile_interval_config(profile_id, val):
    update_profile(profile_id, interval_config=val)

def get_profile_interval_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof.get("interval_proxy", 5) if prof else 5

def set_profile_interval_proxy(profile_id, val):
    update_profile(profile_id, interval_proxy=val)

def get_profile_max_post_config(profile_id):
    prof = get_profile(profile_id)
    return prof.get("max_post_config", 8) if prof else 8

def set_profile_max_post_config(profile_id, val):
    update_profile(profile_id, max_post_config=val)

def get_profile_max_post_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof.get("max_post_proxy", 10) if prof else 10

def set_profile_max_post_proxy(profile_id, val):
    update_profile(profile_id, max_post_proxy=val)

def get_profile_sources(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return []
    s = prof["sources"]
    items = [x.strip() for x in s.split(",") if x.strip()]
    items = [normalize_channel_input(x) for x in items if normalize_channel_input(x)]
    return items

def set_profile_sources(profile_id, sources_list):
    normalized = [normalize_channel_input(s) for s in sources_list if normalize_channel_input(s)]
    s = ",".join(normalized)
    update_profile(profile_id, sources=s)

def get_profile_dest(profile_id):
    prof = get_profile(profile_id)
    return prof["dest_name"] if prof else None

def set_profile_dest(profile_id, dest):
    update_profile(profile_id, dest_name=dest)

def get_profile_banner_config(profile_id):
    prof = get_profile(profile_id)
    return prof["banner_config"] if prof else ""

def get_profile_banner_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof["banner_proxy"] if prof else ""

def get_profile_last_num(profile_id):
    prof = get_profile(profile_id)
    return prof["last_num"] if prof else 0

def set_profile_last_num(profile_id, num):
    update_profile(profile_id, last_num=num)

def get_profile_ping_mode(profile_id):
    prof = get_profile(profile_id)
    return prof["ping_mode"] if prof else "iran"

def set_profile_ping_mode(profile_id, mode):
    update_profile(profile_id, ping_mode=mode)

def get_profile_ping_enabled(profile_id):
    prof = get_profile(profile_id)
    return prof.get("ping_enabled", 1) if prof else 1

def set_profile_ping_enabled(profile_id, enabled):
    update_profile(profile_id, ping_enabled=1 if enabled else 0)

def get_profile_enabled(profile_id):
    prof = get_profile(profile_id)
    return prof.get("profile_enabled", 1) if prof else 1

def set_profile_enabled(profile_id, enabled):
    update_profile(profile_id, profile_enabled=1 if enabled else 0)

def get_profile_post_configs(profile_id):
    prof = get_profile(profile_id)
    return prof["post_configs"] == 1 if prof else True

def set_profile_post_configs(profile_id, enabled):
    update_profile(profile_id, post_configs=1 if enabled else 0)

def get_profile_post_proxies(profile_id):
    prof = get_profile(profile_id)
    return prof["post_proxies"] == 1 if prof else True

def set_profile_post_proxies(profile_id, enabled):
    update_profile(profile_id, post_proxies=1 if enabled else 0)

def get_profile_show_numbers(profile_id):
    prof = get_profile(profile_id)
    return prof["show_numbers"] == 1 if prof else True

def set_profile_show_numbers(profile_id, enabled):
    update_profile(profile_id, show_numbers=1 if enabled else 0)

def get_profile_custom_query(profile_id):
    prof = get_profile(profile_id)
    return prof["custom_query"] if prof else ""

def set_profile_custom_query(profile_id, query):
    query = query.strip()
    c.execute("UPDATE profiles SET custom_query=? WHERE id=?", (query, profile_id))
    conn.commit()

def get_profile_show_date_config(profile_id):
    prof = get_profile(profile_id)
    return prof["show_date_config"] == 1 if prof else True

def set_profile_show_date_config(profile_id, enabled):
    update_profile(profile_id, show_date_config=1 if enabled else 0)

def get_profile_show_date_proxy(profile_id):
    prof = get_profile(profile_id)
    return prof["show_date_proxy"] == 1 if prof else True

def set_profile_show_date_proxy(profile_id, enabled):
    update_profile(profile_id, show_date_proxy=1 if enabled else 0)

def get_profile_schedule_cron(profile_id):
    prof = get_profile(profile_id)
    return prof["schedule_cron"] if prof else ""

def set_profile_schedule_cron(profile_id, cron):
    update_profile(profile_id, schedule_cron=cron)

def get_profile_last_backup_count(profile_id):
    prof = get_profile(profile_id)
    return prof["last_backup_count"] if prof else 0

def set_profile_last_backup_count(profile_id, count):
    update_profile(profile_id, last_backup_count=count)

def get_profile_backup_interval(profile_id):
    prof = get_profile(profile_id)
    return prof.get("backup_interval", 1000) if prof else 1000

def set_profile_backup_interval(profile_id, interval):
    update_profile(profile_id, backup_interval=interval)

def get_profile_naming_template(profile_id):
    prof = get_profile(profile_id)
    return prof.get("naming_template", "{Flag} | ⚡️Telegram = {CHANNEL_ID}") if prof else "{Flag} | ⚡️Telegram = {CHANNEL_ID}"

def set_profile_naming_template(profile_id, template):
    update_profile(profile_id, naming_template=template)

def get_profile_channel_link(profile_id):
    prof = get_profile(profile_id)
    return prof.get("channel_link", "") if prof else ""

def set_profile_channel_link(profile_id, channel_link):
    update_profile(profile_id, channel_link=channel_link)

def set_profile_timer(profile_id, minutes):
    if minutes <= 0:
        clear_profile_timer(profile_id)
        return
    expiry = (datetime.now(TEHRAN_TZ) + timedelta(minutes=minutes)).isoformat()
    update_profile(profile_id, timer_expiry=expiry, timer_duration=minutes)
    log.info(f"Timer set for profile {profile_id}: {minutes} minutes, expires at {expiry}")

def clear_profile_timer(profile_id):
    update_profile(profile_id, timer_expiry=None, timer_duration=0)
    log.info(f"Timer cleared for profile {profile_id}")

def get_profile_timer(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return None, 0
    expiry_str = prof.get("timer_expiry")
    if not expiry_str:
        return None, 0
    try:
        expiry = datetime.fromisoformat(expiry_str)
        now = datetime.now(TEHRAN_TZ)
        if expiry > now:
            remaining = (expiry - now).total_seconds() / 60
            return expiry, int(remaining)
        else:
            clear_profile_timer(profile_id)
            return None, 0
    except Exception:
        clear_profile_timer(profile_id)
        return None, 0

# ======================================================================
# توابع لیست سیاه
# ======================================================================
def get_blacklist(profile_id):
    rows = c.execute("SELECT word FROM blacklist WHERE profile_id=? ORDER BY id", (profile_id,)).fetchall()
    return [r[0] for r in rows]

def add_blacklist_word(profile_id, word):
    word = word.strip().lower()
    if not word:
        return False
    try:
        c.execute("INSERT INTO blacklist (profile_id, word, created_at) VALUES (?,?,?)",
                  (profile_id, word, get_tehran_time()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def remove_blacklist_word(profile_id, word):
    word = word.strip().lower()
    c.execute("DELETE FROM blacklist WHERE profile_id=? AND word=?", (profile_id, word))
    conn.commit()
    return c.rowcount > 0

def clear_blacklist(profile_id):
    c.execute("DELETE FROM blacklist WHERE profile_id=?", (profile_id,))
    conn.commit()

def is_word_blacklisted(profile_id, text):
    if not text:
        return False
    words = get_blacklist(profile_id)
    if not words:
        return False
    text_lower = text.lower()
    for w in words:
        if w in text_lower:
            return True
    return False

# ======================================================================
# توابع اسپانسر
# ======================================================================
def get_sponsor(profile_id):
    row = c.execute(
        "SELECT name, url, button_text, color, enabled FROM sponsors WHERE profile_id=?",
        (profile_id,)
    ).fetchone()
    if row:
        return {
            "name": row[0],
            "url": row[1],
            "button_text": row[2],
            "color": row[3],
            "enabled": bool(row[4])
        }
    return None

def set_sponsor(profile_id, name, url, button_text="Advertisement", color="primary", enabled=1):
    now = get_tehran_time()
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    c.execute(
        "INSERT INTO sponsors (profile_id, name, url, button_text, enabled, color, created_at) VALUES (?,?,?,?,?,?,?)",
        (profile_id, name, url, button_text, enabled, color, now)
    )
    conn.commit()

def clear_sponsor(profile_id):
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    conn.commit()

def toggle_sponsor(profile_id):
    row = c.execute("SELECT enabled FROM sponsors WHERE profile_id=?", (profile_id,)).fetchone()
    if row:
        new_enabled = 0 if row[0] else 1
        c.execute("UPDATE sponsors SET enabled=? WHERE profile_id=?", (new_enabled, profile_id))
        conn.commit()
        return new_enabled
    return None

def update_sponsor_color(profile_id, color):
    c.execute("UPDATE sponsors SET color=? WHERE profile_id=?", (color, profile_id))
    conn.commit()

# ======================================================================
# توابع کمکی (پینگ، استخراج لینک و ...)
# ======================================================================
def country_to_flag(code):
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

async def get_flag_for_ip(ip):
    cached = c.execute(
        "SELECT country, flag FROM country_cache WHERE ip=?", (ip,)
    ).fetchone()
    if cached and len(cached[1]) > 1:
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=3) as cl:
            r = await cl.get(
                f"http://ip-api.com/json/{ip}?fields=countryCode",
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if r.status_code == 200:
                data = r.json()
                country = data.get("countryCode", "").upper()
                if country:
                    flag = country_to_flag(country)
                    c.execute(
                        "INSERT OR REPLACE INTO country_cache VALUES (?,?,?)",
                        (ip, country, flag))
                    conn.commit()
                    return flag
    except Exception as e:
        log.warning(f"flag API fail for {ip}: {e}")

    return "🌐"

def clean_proxy_link(url):
    if not url:
        return url
    url = url.strip()
    while url and url[-1] in ".,;:!؟\"'`(){}[]<>":
        url = url[:-1]
    return url

def normalize_telegram_proxy(url):
    if not url:
        return url
    url = clean_proxy_link(url)
    url = url.replace('&amp;', '&')
    url = re.sub(r'&amp;', '&', url, flags=re.IGNORECASE)
    try:
        url = unquote(url)
    except:
        pass
    return url

def clean_config_url(url: str) -> str:
    if not url:
        return url
    url = url.replace('&amp;', '&')
    return url

def extract_links_from_text(text):
    results = []
    pattern = re.compile(
        r'(vless|vmess|trojan|hy2|tuic|ss|socks|hysteria2|wireguard|wireguard://|http|https?)://[^\s<>"\'{}()\[\]]+',
        re.IGNORECASE
    )
    for m in pattern.finditer(text):
        link = m.group(0).strip()
        link = re.sub(r'[.,;:!؟\'"`]+$', '', link)
        if len(link) > 10:
            results.append(link)

    if not results:
        telegram_msg_pattern = r'https?://t\.me/([^/\s?]+)/(\d+)(?:\?[^\s]*)?(?:#.*)?'
        for m in re.finditer(telegram_msg_pattern, text, re.IGNORECASE):
            link = m.group(0).strip()
            if link and "proxy" not in link.lower():
                results.append(link)

    if not results:
        text_clean = text.replace('\n', '').replace('\r', '').strip()
        if re.match(r'^[A-Za-z0-9+/=]+$', text_clean):
            try:
                decoded = base64.b64decode(text_clean, validate=True).decode('utf-8', errors='ignore')
                for proto in ["vless://", "vmess://", "trojan://", "hy2://", "tuic://", "ss://", "socks://", "hysteria2://"]:
                    for m in re.finditer(re.escape(proto) + r"[^\s<>\"']+", decoded):
                        link = m.group().rstrip().strip(".,;(){}[]!؟'")
                        if len(link) > len(proto) + 10:
                            results.append(link)
            except Exception:
                pass

        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) > 2000:
                continue
            if re.match(r'^[A-Za-z0-9+/=]+$', line):
                try:
                    pad = "=" * (-len(line) % 4)
                    decoded = base64.b64decode(line + pad, validate=False).decode('utf-8', errors='ignore')
                    for proto in ["vless://", "vmess://", "trojan://", "hy2://", "tuic://", "ss://", "socks://", "hysteria2://"]:
                        for m in re.finditer(re.escape(proto) + r"[^\s<>\"']+", decoded):
                            link = m.group().rstrip().strip(".,;(){}[]!؟'")
                            if len(link) > len(proto) + 10:
                                results.append(link)
                except:
                    pass

    if not results:
        for line in text.splitlines():
            line = line.strip()
            for proto in ['vless://', 'vmess://', 'trojan://', 'hy2://', 'tuic://', 'ss://', 'socks://', 'hysteria2://']:
                if line.lower().startswith(proto):
                    results.append(line)
                    break

    return list(set(results))

def normalize_proxy_url(url):
    if not url:
        return None
    url = clean_proxy_link(url.strip())
    if url.startswith("tg://proxy") or "t.me/proxy" in url.lower():
        return normalize_telegram_proxy(url)
    return None

def extract_proxy_links_from_text(text):
    results = []
    telegram_proxy_pattern = r'https?://t\.me/proxy\?[^\s<>"\']+'
    for m in re.finditer(telegram_proxy_pattern, text, re.IGNORECASE):
        link = m.group().strip()
        link = clean_proxy_link(link)
        link = normalize_telegram_proxy(link)
        if link and link not in results:
            results.append(link)

    tg_proxy_pattern = r'tg://proxy\?[^\s<>"\']+'
    for m in re.finditer(tg_proxy_pattern, text, re.IGNORECASE):
        link = m.group().strip()
        link = clean_proxy_link(link)
        if link and link not in results:
            results.append(link)

    return results

def extract_uuid_and_address(url):
    try:
        clean = url.split("#")[0] if "#" in url else url
        clean = clean.split("?")[0] if "?" in clean else clean
        after = clean.split("://", 1)[1] if "://" in clean else clean
        if url.startswith("vmess://"):
            try:
                b64 = after + "=" * (-len(after) % 4)
                data = json.loads(
                    base64.b64decode(b64).decode('utf-8', errors='ignore'))
                uid = data.get("id", "") or data.get("uuid", "")
                host = f"{data.get('add','')}:{data.get('port','')}"
                return uid, host
            except Exception:
                return ("vmess_" +
                        hashlib.md5(after.encode()).hexdigest()[:16]), after
        else:
            if "@" in after:
                uid, host = after.split("@", 1)
            else:
                uid, host = after, ""
            if "/" in host:
                host = host.split("/")[0]
            return (uid.split("?")[0].split("#")[0],
                    host.split("?")[0].split("#")[0])
    except Exception:
        return "", ""

def is_already_posted(profile_id, url):
    url = clean_config_url(url)
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        uid = url[:200]
        host = ""
    return c.execute(
        "SELECT 1 FROM seen WHERE uuid=? AND address=? AND profile_id=?",
        (uid, host, profile_id)).fetchone() is not None

def is_proxy_posted(profile_id, proxy_url):
    r = c.execute("SELECT 1 FROM proxies_seen WHERE proxy_url=? AND profile_id=?", (proxy_url, profile_id)).fetchone()
    return r is not None

def mark_proxy_posted(profile_id, proxy_url):
    now = get_tehran_time()
    c.execute("INSERT OR REPLACE INTO proxies_seen (proxy_url, first_seen, last_posted, profile_id) VALUES (?,?,?,?)",
              (proxy_url, now, now, profile_id))
    conn.commit()

def mark_as_posted(profile_id, url, source, full_url=""):
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        uid = url[:200]
        host = ""
    now = get_tehran_time()
    max_bn = c.execute("SELECT COALESCE(MAX(backup_num),0) FROM seen WHERE profile_id=?", (profile_id,)).fetchone()[0]
    new_bn = max_bn + 1 if full_url else 0
    c.execute("""
        INSERT OR REPLACE INTO seen (uuid, address, source, first_seen, last_posted, profile_id, full_url, backup_num)
        VALUES (?,?,?,?,?,?,?,?)
    """, (uid, host, source, now, now, profile_id, full_url or url, new_bn))
    conn.commit()

def is_message_processed(profile_id, source, message_id):
    r = c.execute("SELECT 1 FROM processed_messages WHERE source=? AND message_id=? AND profile_id=?", (source, message_id, profile_id)).fetchone()
    return r is not None

def mark_message_processed(profile_id, source, message_id):
    c.execute("INSERT OR REPLACE INTO processed_messages (source, message_id, profile_id) VALUES (?,?,?)",
              (source, message_id, profile_id))
    conn.commit()

def get_last_scrape_time(profile_id, source):
    r = c.execute("SELECT last_scrape_time FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else None

def update_last_scrape_time(profile_id, source, time_str, last_message_id=""):
    c.execute("INSERT OR REPLACE INTO last_scrape (source, last_scrape_time, profile_id, last_message_id) VALUES (?,?,?,?)",
              (source, time_str, profile_id, last_message_id))
    conn.commit()

def get_last_message_id(profile_id, source):
    r = c.execute("SELECT last_message_id FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else ""

def update_last_message_id(profile_id, source, msg_id):
    c.execute("UPDATE last_scrape SET last_message_id=? WHERE source=? AND profile_id=?", (msg_id, source, profile_id))
    conn.commit()

def strip_url_fragment(url):
    if '#' in url:
        return url.split('#')[0]
    return url

def extract_host(url):
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port

        if host and host.lower() == 't.me' and parsed.path.startswith('/proxy'):
            query = parse_qs(parsed.query)
            server = query.get('server', [None])[0]
            if server:
                if ':' in server:
                    host, port_str = server.rsplit(':', 1)
                    port = int(port_str)
                else:
                    host = server
                    port_str = query.get('port', [None])[0]
                    if port_str:
                        port = int(port_str)
                return host, port
            return host, port

        if host:
            if parsed.port:
                return host, parsed.port
            else:
                if ':' in parsed.netloc:
                    host_part, port_part = parsed.netloc.rsplit(':', 1)
                    if port_part.isdigit():
                        return host_part, int(port_part)
                return host, None
        if "://" in url:
            url = url.split("://", 1)[1]
        for c in '?#':
            if c in url:
                url = url.split(c)[0]
        if "@" in url:
            url = url.split("@")[-1]
        if ":" in url:
            host, port = url.rsplit(":", 1)
            return host.strip(), int(port)
        else:
            return url.strip(), None
    except Exception as e:
        log.warning(f"extract_host error for {url}: {e}")
        return None, None

def add_custom_query_to_url(url, custom_query, protocol):
    if not custom_query or protocol.lower() == 'vmess':
        return url
    url = clean_config_url(url)
    if '#' in url:
        base, fragment = url.split('#', 1)
    else:
        base = url
        fragment = None
    parsed = urlparse(base)
    existing_params = parse_qs(parsed.query)
    custom_params = parse_qs(custom_query)
    new_query_dict = {}
    for k, v in custom_params.items():
        new_query_dict[k] = v[-1] if v else ""
    for k, v in existing_params.items():
        if k not in new_query_dict:
            new_query_dict[k] = v[-1] if v else ""
    new_query = urlencode(new_query_dict, doseq=True)
    new_base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ''))
    if fragment:
        new_base += '#' + fragment
    return new_base

# ======================================================================
# پینگ
# ======================================================================
async def host_to_ip(host):
    try:
        return socket.gethostbyname(host)
    except Exception as e:
        log.warning(f"DNS resolution failed for {host}: {e}")
        return None

async def test_tcp_ping(host, port):
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=1.5
        )
        writer.close()
        await writer.wait_closed()
        ping_ms = round((loop.time() - start) * 1000)
        return True, ping_ms
    except Exception:
        return False, 0

async def ping_from_iran_only(host, port=None, allow_tcp_fallback=True):
    ip = await host_to_ip(host)
    if not ip:
        ip = host
    target = ip

    try:
        async with httpx.AsyncClient(timeout=5) as cl:
            r = await cl.get(
                f"https://check-host.net/check-ping?host={target}&json=1",
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True
            )
            if r.status_code == 200:
                try:
                    data = r.json()
                except json.JSONDecodeError:
                    log.warning(f"⚠️ check-host.net returned invalid JSON for {target}")
                    data = None
                if data:
                    nodes = data.get("nodes", {})
                    iran_pings = []
                    iran_keywords = [
                        "ir1", "ir2", "ir3", "ir4", "ir5", "ir6", "ir7", "ir8", "ir9",
                        "iran", "tehran", "ir-", "-ir", "ir_", "_ir",
                        "mci", "hamrahe", "rightel", "shatel", "iranel",
                        "teh", "shiraz", "isfahan", "mashhad", "tabriz", "ahvaz"
                    ]
                    for node_name, results in nodes.items():
                        if not isinstance(results, list):
                            continue
                        node_lower = node_name.lower()
                        for v in results:
                            if isinstance(v, (int, float)) and v > 0:
                                if any(kw in node_lower for kw in iran_keywords):
                                    iran_pings.append(int(v))
                                break
                    if iran_pings:
                        avg_ping = int(sum(iran_pings) / len(iran_pings))
                        log.info(f"✅ Iran ping OK: {target} -> {len(iran_pings)} nodes, avg {avg_ping}ms")
                        return avg_ping, True, len(iran_pings)
                    else:
                        log.info(f"⚠️ No Iran nodes responded for {target}")
            else:
                log.warning(f"check-host.net status: {r.status_code}")
    except Exception as e:
        log.warning(f"check-host.net request failed: {e}")

    if not allow_tcp_fallback:
        log.info(f"❌ Iran ping failed and TCP fallback disabled for {target}")
        return 0, False, 0

    if port is not None:
        log.info(f"🔄 TCP fallback with config port: {host}:{port}")
        ok, ping = await test_tcp_ping(host, port)
        if ok:
            log.info(f"✅ TCP fallback OK: {host}:{port} -> {ping}ms")
            return ping, True, 0
        else:
            log.info(f"❌ TCP fallback FAILED: {host}:{port}")
    else:
        log.info(f"🔄 No port in config, trying common ports...")
        ports_to_try = [443, 80, 8080, 8443, 2053, 2096, 2087, 2083]
        for test_port in ports_to_try:
            ok, ping = await test_tcp_ping(host, test_port)
            if ok:
                log.info(f"✅ TCP fallback OK: {host}:{test_port} -> {ping}ms")
                return ping, True, 0

    log.info(f"❌ All ping attempts failed for {target}")
    return 0, False, 0

async def check_full_link_ping(url, ping_mode="iran"):
    host, port = extract_host(url)
    if not host:
        return 0, False, 0
    allow_tcp = (ping_mode != "iran")
    ping, ok, cnt = await ping_from_iran_only(host, port, allow_tcp_fallback=allow_tcp)
    return ping, ok, cnt

# ======================================================================
# اسکرپ
# ======================================================================
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

async def scrape_channel_with_retry(profile_id, channel, max_retries=2):
    try:
        return await scrape_channel_paginated(profile_id, channel)
    except Exception as e:
        log.error(f"❌ scrape {channel} error: {e}")
        return [], []

async def scrape_channel_paginated(profile_id, channel):
    clean_channel = normalize_channel_input(channel)
    if not clean_channel:
        log.warning(f"Invalid channel name: {channel}")
        return [], []

    last_seen_msg_id = get_last_message_id(profile_id, clean_channel)
    base_url = f"https://t.me/s/{clean_channel.lstrip('@')}"
    all_configs = []
    all_proxies = []
    current_url = base_url
    max_pages = 20
    page_count = 0
    log.info(f"🔍 Starting scrape for {clean_channel}, last_seen_msg_id={last_seen_msg_id}")

    while page_count < max_pages:
        page_count += 1
        log.info(f"🔍 Scraping page {page_count} for {clean_channel} (url: {current_url})")
        config_links, proxy_links, msg_ids = await _scrape_single_page(current_url, clean_channel)
        if not msg_ids:
            log.info(f"⚠️ No messages found on page {page_count} for {clean_channel}")
            break

        if last_seen_msg_id and last_seen_msg_id in msg_ids:
            log.info(f"✅ Reached last seen message {last_seen_msg_id} for {clean_channel}. Stopping pagination.")
            all_configs.extend(config_links)
            all_proxies.extend(proxy_links)
            break

        all_configs.extend(config_links)
        all_proxies.extend(proxy_links)

        numeric_ids = []
        for mid in msg_ids:
            parts = mid.split('/')
            if len(parts) == 2 and parts[1].isdigit():
                numeric_ids.append(int(parts[1]))
        if numeric_ids:
            oldest = min(numeric_ids)
            current_url = f"{base_url}?before={oldest}"
        else:
            break
        await asyncio.sleep(0.5)

    if msg_ids:
        newest_msg_id = msg_ids[0] if msg_ids else ""
        if newest_msg_id:
            update_last_message_id(profile_id, clean_channel, newest_msg_id)
            log.info(f"📝 Updated last_message_id for {clean_channel} to {newest_msg_id}")

    update_last_scrape_time(profile_id, clean_channel, get_tehran_time(), last_message_id=newest_msg_id if msg_ids else "")

    all_configs = list(set(all_configs))
    all_proxies = list(set(all_proxies))

    log.info(f"📊 {clean_channel}: total found {len(all_configs)} configs, {len(all_proxies)} proxies (paginated)")
    return all_configs, all_proxies

async def _scrape_single_page(url, channel):
    headers = {
        "User-Agent": _USER_AGENTS[hash(datetime.now().timestamp()) % len(_USER_AGENTS)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cl:
        r = await cl.get(url, headers=headers)
        if r.status_code == 429:
            log.warning(f"Rate limit for {channel}, waiting 30s")
            await asyncio.sleep(30)
            r = await cl.get(url, headers=headers)
        if r.status_code != 200:
            log.warning(f"⚠️ {channel} returned status {r.status_code} for url {url}")
            return [], [], []
        html_text = r.text

        config_links = extract_links_from_text(html_text)
        proxy_links = extract_proxy_links_from_text(html_text)

        msg_ids = re.findall(r'data-post="([^"]+)"', html_text)
        if not msg_ids:
            msg_ids = re.findall(r'href="/([^/]+/\d+)"', html_text)
        msg_ids = list(set(msg_ids))

        log.info(f"📄 Page {url}: found {len(config_links)} configs, {len(proxy_links)} proxies, {len(msg_ids)} messages")
        return config_links, proxy_links, msg_ids

# ======================================================================
# اسکرپ لینک پیام تلگرام (بهبود یافته)
# ======================================================================
async def scrape_single_message_link(profile_id, url):
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cl:
            headers = {"User-Agent": _USER_AGENTS[0]}
            r = await cl.get(url, headers=headers)
            if r.status_code != 200:
                log.warning(f"⚠️ Failed to fetch message link {url}, status {r.status_code}")
                return [], []
            html = r.text

            configs = extract_links_from_text(html)
            proxies = extract_proxy_links_from_text(html)

            file_links = re.findall(r'https?://cdn\d+\.telesco\.pe/file/[^\s<>"\']+', html)
            for file_url in file_links:
                try:
                    log.info(f"📥 Downloading file from {file_url}")
                    file_r = await cl.get(file_url, headers=headers)
                    if file_r.status_code == 200:
                        data = file_r.content
                        text = decrypt_subscription(data, [])
                        if not text:
                            text = data.decode('utf-8', errors='ignore')
                        new_configs = extract_links_from_text(text)
                        new_proxies = extract_proxy_links_from_text(text)
                        configs.extend(new_configs)
                        proxies.extend(new_proxies)
                        log.info(f"✅ Extracted {len(new_configs)} configs and {len(new_proxies)} proxies from file")
                except Exception as e:
                    log.warning(f"Error downloading file {file_url}: {e}")

            configs = [c for c in configs if not c.startswith("https://t.me/") or "proxy" in c.lower()]
            proxies = [p for p in proxies if p.startswith("https://t.me/proxy") or p.startswith("tg://proxy")]

            log.info(f"📩 Scraped message {url}: {len(configs)} configs, {len(proxies)} proxies")
            return configs, proxies
    except Exception as e:
        log.warning(f"Error scraping message link {url}: {e}")
        return [], []

# ======================================================================
# فایل‌ها
# ======================================================================
async def fetch_files_from_channel(bot, profile_id, channel, source):
    try:
        chat_id = channel if channel.startswith('@') else '@' + channel
        messages = await bot.get_chat_history(chat_id, limit=50)
        new_links = []
        for msg in messages:
            if is_message_processed(profile_id, source, msg.message_id):
                continue
            doc = msg.document
            if not doc:
                continue
            try:
                file_obj = await doc.get_file()
                data = await file_obj.download_as_bytearray()
            except Exception as e:
                log.warning(f"Download file from {source} failed: {e}")
                continue

            text = decrypt_subscription(bytes(data), [])
            if not text:
                text = bytes(data).decode('utf-8', errors='ignore')

            links = get_v2ray_links_from_text(text)
            if not links:
                links = extract_links_from_text(text)

            if links:
                new_links.extend(links)
                log.info(f"✅ Extracted {len(links)} links from file in {source} (msg {msg.message_id})")

            mark_message_processed(profile_id, source, msg.message_id)

        return new_links
    except Exception as e:
        log.warning(f"fetch_files_from_channel({channel}) error: {e}")
        return []

def decrypt_subscription(data: bytes, passwords: list):
    protocols = ("vless://", "vmess://", "trojan://",
                  "hy2://", "tuic://", "hysteria2://")
    try:
        text = data.decode('utf-8', errors='ignore')
        if any(p in text for p in protocols):
            return text
    except Exception:
        pass
    try:
        decoded = base64.b64decode(data + b'=' * (-len(data) % 4))
        text = decoded.decode('utf-8', errors='ignore')
        if any(p in text for p in protocols):
            return text
    except Exception:
        pass
    try:
        decoded = base64.b64decode(data + b'=' * (-len(data) % 4)) \
            .decode('utf-8', errors='ignore')
        text = unquote(decoded)
        if any(p in text for p in protocols):
            return text
    except Exception:
        pass
    return None

def get_v2ray_links_from_text(text):
    results = []
    patterns = [
        r'vless://[^\s<>"\']+',
        r'vmess://[^\s<>"\']+',
        r'trojan://[^\s<>"\']+',
        r'hy2://[^\s<>"\']+',
        r'tuic://[^\s<>"\']+',
        r'hysteria2://[^\s<>"\']+'
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            link = m.group().strip()
            if len(link) > 10:
                results.append(link)
    return list(set(results))

# ======================================================================
# ارسال
# ======================================================================
async def send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True, max_retries=5):
    retry_count = 0
    while retry_count < max_retries:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview
            )
            return True
        except RetryAfter as e:
            wait = e.retry_after + 2
            log.warning(f"Flood control, waiting {wait} seconds...")
            await asyncio.sleep(wait)
            retry_count += 1
        except TimedOut:
            log.warning(f"Timeout, retrying... ({retry_count+1}/{max_retries})")
            await asyncio.sleep(3)
            retry_count += 1
        except BadRequest as e:
            if "can't parse entities" in str(e):
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=re.sub(r'<[^>]+>', '', text)[:4096],
                        reply_markup=reply_markup,
                        disable_web_page_preview=disable_web_page_preview
                    )
                    return True
                except Exception:
                    pass
            log.error(f"BadRequest: {e}")
            return False
        except Exception as e:
            log.error(f"Send error: {e}")
            await asyncio.sleep(2)
            retry_count += 1
    return False

async def send_to_destination(bot, profile_id, text, buttons=None):
    dest = get_profile_dest(profile_id)
    if not dest:
        log.error(f"❌ Profile {profile_id} has no destination!")
        return False

    log.info(f"📤 Sending to {dest} (profile {profile_id})")
    chunks = split_text(text, 4096)
    success = True
    for idx, chunk in enumerate(chunks):
        reply_markup = InlineKeyboardMarkup(buttons) if buttons and idx == 0 else None
        ok = await send_with_retry(
            bot, dest, chunk,
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )
        if not ok:
            plain = re.sub(r'<[^>]+>', '', chunk)
            ok2 = await send_with_retry(
                bot, dest, plain[:4096],
                parse_mode=None,
                reply_markup=reply_markup if idx == 0 else None,
                disable_web_page_preview=True
            )
            if not ok2:
                success = False
        if idx < len(chunks) - 1:
            await asyncio.sleep(1)
    return success

def split_text(text, max_len=4096):
    if len(text) <= max_len:
        return [text]
    lines = text.split('\n')
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        if len(line) > max_len:
            if current:
                chunks.append('\n'.join(current))
                current = []
                current_len = 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i+max_len])
            continue
        if current_len + len(line) + 1 > max_len:
            chunks.append('\n'.join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1
    if current:
        chunks.append('\n'.join(current))
    return chunks

# ======================================================================
# ارسال کانفیگ‌ها (اصلاح شده برای دکمه کانال)
# ======================================================================
async def post_configs(bot, profile_id, working, source_for_seen="", is_instant=False, max_post_override=None):
    if not working:
        return 0

    max_post = max_post_override if max_post_override is not None else get_profile_max_post_config(profile_id)
    if is_instant:
        max_post = min(max_post, 5)

    blacklist_words = get_blacklist(profile_id)
    filtered_working = []
    for url, ping, cnt in working:
        if blacklist_words and is_word_blacklisted(profile_id, url):
            log.info(f"⛔ Blacklisted config skipped: {url[:50]}...")
            continue
        filtered_working.append((url, ping, cnt))

    items = sorted(filtered_working, key=lambda x: x[1])[:max_post]
    if not items:
        return 0

    last_n = get_profile_last_num(profile_id)
    show_numbers = get_profile_show_numbers(profile_id)
    custom_query = get_profile_custom_query(profile_id)
    dest = get_profile_dest(profile_id)
    banner_template = get_profile_banner_config(profile_id) or "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    naming_template = get_profile_naming_template(profile_id)
    channel_link = get_profile_channel_link(profile_id)
    if not channel_link:
        channel_link = dest if dest else ""

    sponsor = get_sponsor(profile_id)
    sponsor_button = None
    if sponsor and sponsor["enabled"]:
        btn_style = sponsor["color"] if sponsor["color"] in ["primary", "success", "danger"] else "primary"
        sponsor_button = InlineKeyboardButton(sponsor["button_text"], url=sponsor["url"], style=btn_style)

    config_blocks = []
    for i, (url, ping, node_count) in enumerate(items, 1):
        n = last_n + i
        host, _ = extract_host(url)
        flag = "🌐"
        if host:
            ip = await host_to_ip(host)
            if ip:
                flag = await get_flag_for_ip(ip)

        fragment_text = naming_template.replace("{Flag}", flag).replace("{CHANNEL_ID}", channel_link).replace("{COUNT}", str(n))
        encoded_fragment = quote(fragment_text, safe='')
        base_url = strip_url_fragment(url)
        modified_url = base_url + "#" + encoded_fragment

        protocol = url.split('://')[0].lower() if '://' in url else ''
        if custom_query and protocol != 'vmess':
            modified_url = add_custom_query_to_url(modified_url, custom_query, protocol)

        if show_numbers:
            if ping > 0:
                header = f"<b>#{n}</b> {dest if dest else '@VaslZone'} {flag} {ping}ms"
            else:
                header = f"<b>#{n}</b> {dest if dest else '@VaslZone'} {flag}"
        else:
            if ping > 0:
                header = f"{dest if dest else '@VaslZone'} {flag} {ping}ms"
            else:
                header = f"{dest if dest else '@VaslZone'} {flag}"

        block = f"<pre>{modified_url}</pre>"
        config_blocks.append(header + "\n" + block)

    configs_text = "\n\n".join(config_blocks)
    try:
        full_text = banner_template.format(configs=configs_text)
    except KeyError:
        full_text = f"✦ V2Ray Config List\n\n{configs_text}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

    # ===== اضافه کردن دکمه‌های اضافی (اسپانسر + کانال) =====
    buttons = []
    if sponsor_button:
        buttons.append(sponsor_button)
    # دکمه کانال
    channel_link_display = get_profile_channel_link(profile_id)
    if channel_link_display:
        if channel_link_display.startswith("@"):
            channel_url = f"https://t.me/{channel_link_display[1:]}"
        else:
            channel_url = channel_link_display
        channel_button = InlineKeyboardButton("📢 کانال", url=channel_url)
        buttons.append(channel_button)
    reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None

    ok = await send_with_retry(
        bot, dest, full_text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
        max_retries=5
    )
    if not ok:
        plain_text = re.sub(r'<[^>]+>', '', full_text)
        ok2 = await send_with_retry(
            bot, dest, plain_text,
            parse_mode=None,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            max_retries=3
        )
        if not ok2:
            log.error(f"❌ Failed to send configs after all retries")
            return 0

    sent_count = len(items)
    for i, (url, ping, node_count) in enumerate(items, 1):
        n = last_n + i
        flag = "🌐"
        host, _ = extract_host(url)
        if host:
            ip = await host_to_ip(host)
            if ip:
                flag = await get_flag_for_ip(ip)
        fragment_text = naming_template.replace("{Flag}", flag).replace("{CHANNEL_ID}", channel_link).replace("{COUNT}", str(n))
        encoded_fragment = quote(fragment_text, safe='')
        base_url = strip_url_fragment(url)
        modified_url = base_url + "#" + encoded_fragment
        if custom_query:
            protocol = url.split('://')[0].lower() if '://' in url else ''
            if custom_query and protocol != 'vmess':
                modified_url = add_custom_query_to_url(modified_url, custom_query, protocol)
        if not is_already_posted(profile_id, modified_url):
            mark_as_posted(profile_id, modified_url, source_for_seen, full_url=modified_url)

    if sent_count > 0:
        set_profile_last_num(profile_id, last_n + sent_count)

    log.info(f"✅ Sent {sent_count} configs in one message to {dest}")
    return sent_count

# ======================================================================
# ارسال پروکسی‌ها (اصلاح شده برای دکمه کانال)
# ======================================================================
async def post_proxies(bot, profile_id, proxies_with_ping, is_instant=False, max_proxies_override=None):
    if not proxies_with_ping:
        return 0, None

    max_proxies = max_proxies_override if max_proxies_override is not None else get_profile_max_post_proxy(profile_id)
    if is_instant:
        max_proxies = min(max_proxies, 3)

    show_date = get_profile_show_date_proxy(profile_id)
    proxy_text = ""
    count = 0
    for proxy_url, ping, flag in proxies_with_ping[:max_proxies]:
        if "t.me/proxy" in proxy_url.lower() or proxy_url.startswith("tg://proxy"):
            if is_proxy_posted(profile_id, proxy_url):
                continue
            normalized_url = normalize_telegram_proxy(proxy_url)
            clean_url = clean_proxy_link(normalized_url)
            safe_url = html.escape(clean_url, quote=False)
            proxy_text += f"• {flag} <a href=\"{safe_url}\">Telegram Proxy</a>\n"
            mark_proxy_posted(profile_id, clean_url)
            count += 1

    if count == 0:
        return 0, None

    banner_proxy = get_profile_banner_proxy(profile_id)
    date_str = get_tehran_date() if show_date else ""
    try:
        text = banner_proxy.format(
            date=date_str,
            count=count,
            proxies=proxy_text,
        )
    except KeyError:
        log.error("❌ Banner proxy missing placeholders")
        text = f"🌐 Proxies\n{proxy_text}"

    sponsor = get_sponsor(profile_id)
    sponsor_button = None
    if sponsor and sponsor["enabled"]:
        btn_style = sponsor["color"] if sponsor["color"] in ["primary", "success", "danger"] else "primary"
        sponsor_button = InlineKeyboardButton(sponsor["button_text"], url=sponsor["url"], style=btn_style)
    buttons = []
    if sponsor_button:
        buttons.append(sponsor_button)
    # دکمه کانال
    channel_link_display = get_profile_channel_link(profile_id)
    if channel_link_display:
        if channel_link_display.startswith("@"):
            channel_url = f"https://t.me/{channel_link_display[1:]}"
        else:
            channel_url = channel_link_display
        channel_button = InlineKeyboardButton("📢 کانال", url=channel_url)
        buttons.append(channel_button)
    reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None
    return count, (text, reply_markup)

# ======================================================================
# چرخه اصلی (بدون تغییر عمده، فقط لاگ بیشتر)
# ======================================================================
async def run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=True, is_instant=False):
    log.info("=" * 50)
    log.info(f"🔄 run_cycle for profile {profile_id} (cfg={enable_configs}, prx={enable_proxies}, instant={is_instant})")

    profile = get_profile(profile_id)
    if not profile:
        log.error(f"❌ Profile {profile_id} not found!")
        return 0, "profile not found"

    if not get_profile_enabled(profile_id):
        log.info(f"⏸️ Profile {profile_id} is disabled, skipping cycle.")
        return 0, "profile disabled"

    sources = get_profile_sources(profile_id)
    sources = [normalize_channel_input(s) for s in sources if normalize_channel_input(s)]
    if not sources:
        log.error(f"❌ Profile {profile_id} has no valid sources!")
        try:
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"⚠️ پروفایل {profile.get('dest_name', '')} (ID:{profile_id}) هیچ منبعی ندارد. لطفاً یک منبع اضافه کنید."
            )
        except:
            pass
        return 0, "no valid sources"

    dest = get_profile_dest(profile_id)
    if not dest:
        log.error(f"❌ Profile {profile_id} has no destination!")
        try:
            await bot.send_message(
                MAIN_ADMIN_ID,
                f"⚠️ پروفایل {profile.get('dest_name', '')} (ID:{profile_id}) مقصدی ندارد. لطفاً یک مقصد تنظیم کنید."
            )
        except:
            pass
        return 0, "no destination"

    ping_mode = get_profile_ping_mode(profile_id)
    ping_enabled = get_profile_ping_enabled(profile_id)
    log.info(f"📡 Sources: {len(sources)} | 🎯 {dest} | 🌍 Ping: {ping_mode} | Ping enabled: {ping_enabled}")

    all_configs = []
    all_proxies = []
    seen_urls = set()
    message_links_to_scrape = set()

    async def scrape_one(src):
        config_links, proxy_links = await scrape_channel_with_retry(profile_id, src)
        return src, config_links, proxy_links

    scrape_tasks = [scrape_one(src) for src in sources]
    results = await asyncio.gather(*scrape_tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            log.warning(f"Scrape error: {res}")
            continue
        src, config_links, proxy_links = res
        log.info(f"  {src}: {len(config_links)} configs, {len(proxy_links)} proxies from web")
        for link in config_links:
            if link not in seen_urls:
                seen_urls.add(link)
                if link.startswith("https://t.me/") and "proxy" not in link.lower():
                    message_links_to_scrape.add(link)
                else:
                    all_configs.append((link, src))
        for link in proxy_links:
            if link not in seen_urls:
                seen_urls.add(link)
                norm = normalize_proxy_url(link)
                if norm:
                    all_proxies.append(norm)

    file_tasks = [fetch_files_from_channel(bot, profile_id, src, src) for src in sources]
    file_results = await asyncio.gather(*file_tasks, return_exceptions=True)
    for i, res in enumerate(file_results):
        if isinstance(res, Exception):
            log.warning(f"File fetch error for {sources[i]}: {res}")
            continue
        links = res
        src = sources[i]
        log.info(f"  {src}: {len(links)} links from files")
        for link in links:
            if link not in seen_urls:
                seen_urls.add(link)
                if "t.me/proxy" in link.lower() or link.startswith("tg://proxy"):
                    norm = normalize_proxy_url(link)
                    if norm:
                        all_proxies.append(norm)
                elif link.startswith("https://t.me/") and "proxy" not in link.lower():
                    message_links_to_scrape.add(link)
                else:
                    all_configs.append((link, src))

    if message_links_to_scrape:
        log.info(f"🔍 Scraping {len(message_links_to_scrape)} message links...")
        msg_scrape_tasks = [scrape_single_message_link(profile_id, link) for link in message_links_to_scrape]
        msg_results = await asyncio.gather(*msg_scrape_tasks, return_exceptions=True)
        for idx, res in enumerate(msg_results):
            if isinstance(res, Exception):
                log.warning(f"Message link scrape error for {list(message_links_to_scrape)[idx]}: {res}")
                continue
            configs, proxies = res
            src = list(message_links_to_scrape)[idx]
            for c in configs:
                if c not in seen_urls:
                    seen_urls.add(c)
                    all_configs.append((c, src))
            for p in proxies:
                if p not in seen_urls:
                    seen_urls.add(p)
                    norm = normalize_proxy_url(p)
                    if norm:
                        all_proxies.append(norm)

    new_configs = []
    for u, s in all_configs:
        if not is_already_posted(profile_id, u):
            new_configs.append((u, s))
        else:
            log.debug(f"⏭️ Config already posted: {u[:50]}...")

    new_proxies = []
    for p in all_proxies:
        if not is_proxy_posted(profile_id, p):
            new_proxies.append(p)
        else:
            log.debug(f"⏭️ Proxy already posted: {p[:50]}...")

    log.info(f"📊 New configs: {len(new_configs)}, New proxies: {len(new_proxies)}")

    working = []
    if enable_configs and new_configs:
        test_limit = get_profile_max_post_config(profile_id) * 5
        if is_instant:
            test_limit = min(test_limit, 30)
        else:
            test_limit = min(test_limit, 100)
        to_test = new_configs[:test_limit]
        log.info(f"📊 Testing {len(to_test)} configs...")
        sem = asyncio.Semaphore(50)

        async def _check(item):
            u, src = item
            async with sem:
                try:
                    if ping_enabled:
                        ping, ok, cnt = await check_full_link_ping(u, ping_mode)
                    else:
                        ping, ok, cnt = 0, True, 0
                    if ok:
                        log.info(f"✅ Config OK: {u[:50]}... ping={ping}ms")
                        return u, True, ping, cnt, src
                    else:
                        log.info(f"❌ Config FAIL: {u[:50]}...")
                        return u, False, 0, 0, src
                except Exception as e:
                    log.debug(f"ping failed for {u[:30]}: {e}")
                    return u, False, 0, 0, src

        rs = await asyncio.gather(*[_check(item) for item in to_test], return_exceptions=True)
        for r in rs:
            if isinstance(r, Exception):
                continue
            if r[1]:
                working.append((r[0], r[2], r[3]))
        log.info(f"📊 Working configs: {len(working)}")
    else:
        log.info("ℹ️ No configs to test")

    proxy_with_ping = []
    if enable_proxies and new_proxies:
        valid_proxies = [p for p in new_proxies if "t.me/proxy" in p.lower() or p.startswith("tg://proxy")]
        if valid_proxies:
            log.info(f"📊 Processing {len(valid_proxies)} proxies...")
            sem = asyncio.Semaphore(50)

            async def check_proxy(proxy_url):
                async with sem:
                    if ping_enabled:
                        ping, ok, cnt = await check_full_link_ping(proxy_url, ping_mode)
                    else:
                        ping, ok = 0, True
                    host, _ = extract_host(proxy_url)
                    ip = await host_to_ip(host) if host else None
                    flag = "🌐"
                    if ip:
                        flag = await get_flag_for_ip(ip)
                    return proxy_url, ping if ok else 0, flag

            results = await asyncio.gather(
                *[check_proxy(p) for p in valid_proxies[:100]], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    continue
                proxy_with_ping.append(r)
            log.info(f"📊 Proxies with ping: {len(proxy_with_ping)}")
        else:
            log.info("ℹ️ No valid Telegram proxies found.")

    total_configs = 0
    total_proxies = 0
    if working and enable_configs:
        total_configs = await post_configs(bot, profile_id, working, source_for_seen="auto", is_instant=is_instant)

    if proxy_with_ping and enable_proxies:
        cnt, payload = await post_proxies(bot, profile_id, proxy_with_ping, is_instant=is_instant)
        if cnt > 0 and payload:
            text, buttons = payload
            sent = await send_to_destination(bot, profile_id, text, buttons)
            if sent:
                total_proxies = cnt

    result_msg = f"posted {total_configs} configs and {total_proxies} proxies"
    if total_configs == 0 and total_proxies == 0:
        result_msg = "no new content to send"

    log.info(f"✅ Cycle result for profile {profile_id}: {result_msg}")
    log.info("=" * 50)
    return total_configs + total_proxies, result_msg

# ======================================================================
# حلقه‌های خودکار (با لاگ بیشتر و مدیریت بهتر)
# ======================================================================
async def profile_loop_config(bot, profile_id):
    log.info(f"🔄 Starting config loop for profile {profile_id}")
    while True:
        try:
            profile = get_profile(profile_id)
            if not profile:
                log.error(f"❌ Profile {profile_id} not found, stopping config loop.")
                break

            if not get_profile_enabled(profile_id):
                log.info(f"⏸️ Profile {profile_id} is disabled, sleeping 60s")
                await asyncio.sleep(60)
                continue

            if not get_profile_post_configs(profile_id):
                log.info(f"ℹ️ Config posting disabled for profile {profile_id}, sleeping 60s")
                await asyncio.sleep(60)
                continue

            interval = get_profile_interval_config(profile_id)
            dest_name = profile.get("dest_name", "unknown")
            timer_expiry = profile.get("timer_expiry")
            if timer_expiry:
                try:
                    expiry = datetime.fromisoformat(timer_expiry)
                    now = datetime.now(TEHRAN_TZ)
                    if expiry > now:
                        remaining_seconds = (expiry - now).total_seconds()
                        log.info(f"⏳ Timer active for profile {profile_id}: {remaining_seconds/60:.1f} minutes remaining")
                        await asyncio.sleep(min(remaining_seconds, 60))
                        continue
                    else:
                        clear_profile_timer(profile_id)
                        await bot.send_message(
                            MAIN_ADMIN_ID,
                            f"⏰ تایمر پروفایل {dest_name} (ID: {profile_id}) به پایان رسید. ارسال خودکار از سر گرفته شد."
                        )
                        log.info(f"✅ Timer expired for profile {profile_id}, running cycle immediately")
                        n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=(interval == 0))
                        log.info(f"[config loop] result: {n} - {m}")
                        continue
                except Exception as e:
                    log.error(f"Error parsing timer: {e}")
                    clear_profile_timer(profile_id)

            if interval == 0:
                log.info(f"⚡ INSTANT CONFIG UPDATE for profile {profile_id} ({dest_name})")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=True)
                log.info(f"[instant config] result: {n} - {m}")
                await asyncio.sleep(5)
            else:
                now = datetime.now(TEHRAN_TZ)
                next_run = now + timedelta(minutes=interval)
                sleep_seconds = (next_run - now).total_seconds()
                if sleep_seconds > 0:
                    log.info(f"⏳ Config loop sleeping for {sleep_seconds:.0f}s until {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                    await asyncio.sleep(sleep_seconds)
                else:
                    await asyncio.sleep(1)

                log.info(f"⏰ CONFIG AUTO TICK for profile {profile_id}")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=True, enable_proxies=False, is_instant=False)
                log.info(f"[config auto] result: {n} - {m}")

        except asyncio.CancelledError:
            log.info(f"🛑 Config loop for profile {profile_id} cancelled.")
            break
        except Exception as e:
            log.error(f"❌ profile_loop_config error: {e}")
            log.error(traceback.format_exc())
            await asyncio.sleep(60)

async def profile_loop_proxy(bot, profile_id):
    log.info(f"🔄 Starting proxy loop for profile {profile_id}")
    while True:
        try:
            profile = get_profile(profile_id)
            if not profile:
                log.error(f"❌ Profile {profile_id} not found, stopping proxy loop.")
                break

            if not get_profile_enabled(profile_id):
                log.info(f"⏸️ Profile {profile_id} is disabled, sleeping 60s")
                await asyncio.sleep(60)
                continue

            if not get_profile_post_proxies(profile_id):
                log.info(f"ℹ️ Proxy posting disabled for profile {profile_id}, sleeping 60s")
                await asyncio.sleep(60)
                continue

            interval = get_profile_interval_proxy(profile_id)
            dest_name = profile.get("dest_name", "unknown")
            timer_expiry = profile.get("timer_expiry")
            if timer_expiry:
                try:
                    expiry = datetime.fromisoformat(timer_expiry)
                    now = datetime.now(TEHRAN_TZ)
                    if expiry > now:
                        remaining_seconds = (expiry - now).total_seconds()
                        log.info(f"⏳ Timer active for profile {profile_id}: {remaining_seconds/60:.1f} minutes remaining")
                        await asyncio.sleep(min(remaining_seconds, 60))
                        continue
                    else:
                        clear_profile_timer(profile_id)
                        await bot.send_message(
                            MAIN_ADMIN_ID,
                            f"⏰ تایمر پروفایل {dest_name} (ID: {profile_id}) به پایان رسید. ارسال خودکار از سر گرفته شد."
                        )
                        log.info(f"✅ Timer expired for profile {profile_id}, running proxy cycle immediately")
                        n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=(interval == 0))
                        log.info(f"[proxy loop] result: {n} - {m}")
                        continue
                except Exception as e:
                    log.error(f"Error parsing timer: {e}")
                    clear_profile_timer(profile_id)

            if interval == 0:
                log.info(f"⚡ INSTANT PROXY UPDATE for profile {profile_id} ({dest_name})")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=True)
                log.info(f"[instant proxy] result: {n} - {m}")
                await asyncio.sleep(5)
            else:
                now = datetime.now(TEHRAN_TZ)
                next_run = now + timedelta(minutes=interval)
                sleep_seconds = (next_run - now).total_seconds()
                if sleep_seconds > 0:
                    log.info(f"⏳ Proxy loop sleeping for {sleep_seconds:.0f}s until {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                    await asyncio.sleep(sleep_seconds)
                else:
                    await asyncio.sleep(1)

                log.info(f"⏰ PROXY AUTO TICK for profile {profile_id}")
                n, m = await run_cycle_for_profile(bot, profile_id, enable_configs=False, enable_proxies=True, is_instant=False)
                log.info(f"[proxy auto] result: {n} - {m}")

        except asyncio.CancelledError:
            log.info(f"🛑 Proxy loop for profile {profile_id} cancelled.")
            break
        except Exception as e:
            log.error(f"❌ profile_loop_proxy error: {e}")
            log.error(traceback.format_exc())
            await asyncio.sleep(60)

# ======================================================================
# بک‌آپ خودکار (بدون تغییر)
# ======================================================================
backup_locks = {}

async def check_and_auto_backup(profile_id):
    try:
        profile = get_profile(profile_id)
        if not profile:
            return
        profile_name = profile["dest_name"].replace("@", "").strip() or f"profile_{profile_id}"
        backup_interval = get_profile_backup_interval(profile_id) or 1000

        lock = backup_locks.get(profile_id)
        if not lock:
            lock = asyncio.Lock()
            backup_locks[profile_id] = lock

        async with lock:
            total = c.execute(
                "SELECT COUNT(*) FROM seen WHERE profile_id=? AND full_url != '' AND backup_num > 0",
                (profile_id,)).fetchone()[0]
            last_backup = get_profile_last_backup_count(profile_id)

            last_backup_block = last_backup // backup_interval if backup_interval > 0 else 0
            current_block = total // backup_interval if backup_interval > 0 else 0

            if current_block <= last_backup_block:
                return

            for block in range(last_backup_block + 1, current_block + 1):
                start_num = (block - 1) * backup_interval + 1
                end_num = block * backup_interval

                rows = c.execute(
                    "SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' AND backup_num BETWEEN ? AND ? ORDER BY backup_num",
                    (profile_id, start_num, end_num)
                ).fetchall()
                links = [r[0] for r in rows if r[0]]
                if not links:
                    continue

                filename = f"configs_backup_{get_tehran_date()}_{profile_name}_{start_num}_{end_num}.txt"
                content = f"# Backup for {profile_name} (ID: {profile_id})\n# Range: {start_num} - {end_num}\n# Total: {len(links)}\n\n" + "\n".join(links)
                filepath = os.path.join(DATA_DIR, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(content)

                bot = BOT_REF
                if bot:
                    with open(filepath, "rb") as f:
                        await bot.send_document(
                            MAIN_ADMIN_ID,
                            document=f,
                            filename=filename,
                            caption=f"📤 بک‌آپ خودکار پروفایل {profile_name} (ID:{profile_id}) - {start_num} تا {end_num} (تعداد: {len(links)})"
                        )
                    asyncio.create_task(delete_file_after_delay(filepath, 1800))
                else:
                    log.warning("BOT_REF is None, cannot send backup.")

            set_profile_last_backup_count(profile_id, total)
    except Exception as e:
        log.error(f"Auto backup check error for profile {profile_id}: {e}")

async def delete_file_after_delay(filepath, delay_seconds):
    await asyncio.sleep(delay_seconds)
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
            log.info(f"🗑️ Deleted file: {filepath}")
    except Exception as e:
        log.error(f"Error deleting file {filepath}: {e}")

# ======================================================================
# متغیر سراسری
# ======================================================================
BOT_START_TIME = datetime.utcnow()

# ======================================================================
# توابع دریافت لاگ و گزارش روزانه
# ======================================================================
async def get_logs(update, context, profile_id, log_type="full", time_range_minutes=30):
    log_file_path = os.path.join(DATA_DIR, "bot.log")
    if not os.path.exists(log_file_path):
        await update.message.reply_text("❌ فایل لاگ وجود ندارد.")
        return

    now_utc = datetime.utcnow()
    cutoff_utc = now_utc - timedelta(minutes=time_range_minutes)
    start_cutoff = BOT_START_TIME

    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در خواندن لاگ: {e}")
        return

    filtered = []
    error_keywords = ["ERROR", "❌", "CRITICAL", "Exception", "Traceback"]
    for line in lines:
        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
        if not match:
            continue
        ts_str = match.group(1)
        try:
            ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        except:
            continue
        if ts < start_cutoff or ts < cutoff_utc:
            continue

        if log_type == "errors":
            if any(kw in line for kw in error_keywords):
                filtered.append(line)
        else:
            filtered.append(line)

    if not filtered:
        await update.message.reply_text(f"❌ هیچ لاگ {log_type} در {time_range_minutes} دقیقهٔ اخیر و پس از استارت یافت نشد.")
        return

    max_lines = 500
    if len(filtered) > max_lines:
        filtered = filtered[-max_lines:]

    content = "\n".join(filtered)
    range_label = f"{time_range_minutes}m" if time_range_minutes < 1440 else "24h"
    filename = f"logs_{log_type}_{range_label}_{get_tehran_date()}.txt"
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)

    with open(filepath, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=filename,
            caption=f"📋 لاگ {log_type} ({range_label} اخیر پس از استارت) - {len(filtered)} خط"
        )
    os.remove(filepath)

async def send_daily_report(app):
    try:
        profiles = get_profiles()
        total_profiles = len(profiles)
        total_seen = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
        total_proxies = c.execute("SELECT COUNT(*) FROM proxies_seen").fetchone()[0]
        total_blacklist = c.execute("SELECT COUNT(*) FROM blacklist").fetchone()[0]

        lines = []
        lines.append("📊 **گزارش روزانه بات**")
        lines.append(f"📅 تاریخ: {get_tehran_date()}")
        lines.append(f"🕐 زمان: {get_tehran_time()}")
        lines.append("")
        lines.append(f"📌 تعداد پروفایل‌ها: {total_profiles}")
        lines.append(f"📡 کانفیگ‌های دیده‌شده: {total_seen}")
        lines.append(f"🌐 پروکسی‌های دیده‌شده: {total_proxies}")
        lines.append(f"🚫 کلمات لیست سیاه: {total_blacklist}")
        lines.append("")
        for p in profiles:
            src_count = len(get_profile_sources(p['id']))
            last_num = p['last_num']
            interval_cfg = p.get('interval_config', 5)
            interval_prx = p.get('interval_proxy', 5)
            timer = ""
            if p.get('timer_expiry'):
                timer = " (⏳ تایمر فعال)"
            enabled_status = "✅ فعال" if get_profile_enabled(p['id']) else "⛔ غیرفعال"
            lines.append(f"• {p['dest_name']} (ID:{p['id']}) – {src_count} منبع, بازه کانفیگ:{interval_cfg}m, بازه پروکسی:{interval_prx}m, #{last_num+1}{timer} {enabled_status}")

        msg = "\n".join(lines)
        await app.bot.send_message(MAIN_ADMIN_ID, msg, parse_mode="HTML")
        log.info("✅ Daily report sent.")
    except Exception as e:
        log.error(f"❌ Failed to send daily report: {e}")

# ======================================================================
# توابع Railway Credit
# ======================================================================
async def get_railway_credit():
    if not RAILWAY_TOKEN:
        return None
    try:
        project_id = RAILWAY_PROJECT_ID
        if not project_id:
            return None
        query = f"""
        query {{
            project(id: "{project_id}") {{
                id
                name
                credit
            }}
        }}
        """
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {
                "Authorization": f"Bearer {RAILWAY_TOKEN}",
                "Content-Type": "application/json"
            }
            resp = await client.post(
                "https://api.railway.app/graphql",
                json={"query": query},
                headers=headers
            )
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    log.warning(f"Railway API errors: {data['errors']}")
                    return None
                credit = data.get("data", {}).get("project", {}).get("credit")
                if credit is not None:
                    return float(credit)
            else:
                log.warning(f"Railway API returned {resp.status_code}")
                return None
    except Exception as e:
        log.warning(f"Failed to fetch railway credit: {e}")
        return None
    return None

async def check_credit_and_backup():
    balance = await get_railway_credit()
    if balance is None:
        return
    if balance < CREDIT_THRESHOLD:
        log.info(f"Credit low: {balance} < {CREDIT_THRESHOLD}, sending full backup")
        bot = BOT_REF
        if bot:
            try:
                with open(DB_PATH, "rb") as f:
                    await bot.send_document(
                        MAIN_ADMIN_ID,
                        document=f,
                        filename=f"full_db_backup_{get_tehran_date()}.db",
                        caption=f"💾 بک‌آپ کامل دیتابیس (اعتبار: {balance} دلار)"
                    )
                log.info("✅ Full database backup sent due to low credit.")
            except Exception as e:
                log.error(f"Failed to send full backup: {e}")

async def periodic_credit_check():
    while True:
        await check_credit_and_backup()
        await asyncio.sleep(3600)

# ======================================================================
# پاکسازی دوره‌ای فایل‌ها
# ======================================================================
async def periodic_cleanup():
    while True:
        try:
            log_file_path = os.path.join(DATA_DIR, "bot.log")
            if os.path.exists(log_file_path):
                now_utc = datetime.utcnow()
                cutoff_utc = now_utc - timedelta(minutes=30)
                lines_to_keep = []
                with open(log_file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', line)
                        if match:
                            ts_str = match.group(1)
                            try:
                                ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                                if ts >= cutoff_utc:
                                    lines_to_keep.append(line)
                            except:
                                lines_to_keep.append(line)
                        else:
                            lines_to_keep.append(line)
                if len(lines_to_keep) > 0:
                    with open(log_file_path, 'w', encoding='utf-8') as f:
                        f.writelines(lines_to_keep)
                else:
                    with open(log_file_path, 'w', encoding='utf-8') as f:
                        pass

            now_ts = time.time()
            for fname in os.listdir(DATA_DIR):
                if fname == "bot.db":
                    continue
                filepath = os.path.join(DATA_DIR, fname)
                if os.path.isfile(filepath):
                    if fname == "bot.log":
                        continue
                    if (fname.startswith("configs_backup_") or 
                        fname.startswith("proxies_backup_") or 
                        fname.startswith("logs_") or 
                        fname.startswith("bot_backup_")):
                        try:
                            mtime = os.path.getmtime(filepath)
                            if now_ts - mtime > 3600:
                                os.remove(filepath)
                                log.info(f"🗑️ Cleanup: deleted old file {fname}")
                        except Exception as e:
                            log.warning(f"Could not delete {fname}: {e}")

            if os.path.exists(BACKUP_DIR):
                for fname in os.listdir(BACKUP_DIR):
                    filepath = os.path.join(BACKUP_DIR, fname)
                    if os.path.isfile(filepath):
                        try:
                            mtime = os.path.getmtime(filepath)
                            if now_ts - mtime > 3600:
                                os.remove(filepath)
                                log.info(f"🗑️ Cleanup: deleted old backup file {fname}")
                        except Exception as e:
                            log.warning(f"Could not delete {fname}: {e}")

        except Exception as e:
            log.error(f"Error in periodic_cleanup: {e}")
        await asyncio.sleep(300)

# ======================================================================
# مدیریت ادمین‌ها
# ======================================================================
def is_admin(user_id: int) -> bool:
    if user_id == MAIN_ADMIN_ID:
        return True
    row = c.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,)).fetchone()
    return row is not None

def add_admin(user_id: int, added_by: int):
    c.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?,?,?)",
              (user_id, added_by, get_tehran_time()))
    conn.commit()

def remove_admin(user_id: int):
    if user_id == MAIN_ADMIN_ID:
        return False
    c.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
    conn.commit()
    return True

def list_admins():
    rows = c.execute("SELECT user_id, added_by, added_at FROM admins ORDER BY added_at").fetchall()
    admins = []
    for row in rows:
        admins.append({"user_id": row[0], "added_by": row[1], "added_at": row[2]})
    return admins

# ======================================================================
# مدیریت زبان
# ======================================================================
def get_lang() -> str:
    row = c.execute("SELECT v FROM cfg WHERE k='lang'").fetchone()
    if row and row[0] in ['fa', 'en']:
        return row[0]
    return 'fa'

def set_lang(lang: str):
    if lang not in ['fa', 'en']:
        return
    c.execute("INSERT OR REPLACE INTO cfg (k, v) VALUES ('lang', ?)", (lang,))
    conn.commit()

# ======================================================================
# کیبوردها و پیام‌ها
# ======================================================================
BOT_REF = None

T = {
    "fa": {
        "welcome": "🤖 **بات جمع‌آوری کانفیگ و پروکسی**\n\n"
                  "📡 تعداد پروفایل‌ها: {profiles}\n"
                  "🔢 بعدی: #{next_n}\n"
                  "💰 اعتبار: {credit}",
        "admin_panel": "🔐 **پنل مدیریت پروفایل**\n\n"
                       "📡 منابع: {srcs} | 🎯 مقصد: {dest}\n"
                       "🎨 نام: {name} | 🔢 #{num}\n"
                       "⏰ بازه کانفیگ: {cfg_interval}m | بازه پروکسی: {prx_interval}m\n"
                       "📊 حداکثر کانفیگ: {max_cfg} | حداکثر پروکسی: {max_prx}\n"
                       "📢 اسپانسر: {sponsor}\n"
                       "🌍 حالت پینگ: {ping_mode}\n"
                       "📡 کانفیگ: {cfg_status} | 🌐 پروکسی: {prx_status}\n"
                       "🔢 شماره‌گذاری: {numbers_status}\n"
                       "🔗 کوئری سفارشی: {custom_query}\n"
                       "📅 تاریخ کانفیگ: {date_cfg}\n"
                       "📅 تاریخ پروکسی: {date_prx}\n"
                       "⏰ کرون: {cron}\n"
                       "⏱️ تایمر: {timer_status}\n"
                       "📦 بک‌آپ هر {backup_interval} عدد\n"
                       "🏷️ قالب نام: {naming}\n"
                       "🔗 لینک کانال: {channel_link}\n"
                       "🌍 پینگ: {ping_status}\n"
                       "🔘 وضعیت: {profile_status}",
        "general_settings": "⚙️ **تنظیمات عمومی**\n\n"
                            "زبان فعلی: {lang}\n"
                            "تعداد ادمین‌ها: {admins_count}",
        "btn_back": "🔙 برگشت",
        "btn_add_source": "➕ منبع",
        "btn_add_dest": "➕ مقصد جدید",
        "btn_dest_list": "📋 مقصدها",
        "btn_sponsors": "📢 اسپانسر",
        "btn_set_dest": "🎯 تنظیم مقصد",
        "btn_set_name": "🎨 نام",
        "btn_set_banner": "📝 بنر",
        "btn_set_banner_config": "📝 بنر کانفیگ",
        "btn_set_banner_proxy": "📝 بنر پروکسی",
        "btn_set_time": "⏰ زمان‌بندی",
        "btn_set_max": "🎯 حداکثر پست",
        "btn_stats": "📊 آمار",
        "btn_test": "🧪 تست",
        "btn_clear": "🗑 پاک DB",
        "btn_reset": "🔢 ریست شماره",
        "btn_ping_mode": "🌍 ایران‌فقط",
        "btn_runnow": "▶️ اجرا کن",
        "btn_instant": "⚡ اپدیت لحظه‌ای",
        "btn_manual_send": "📤 ارسال دستی",
        "btn_manage_sources": "📡 مدیریت منابع",
        "btn_toggle_numbers": "🔢 شماره‌گذاری: {status}",
        "btn_set_custom_query": "🔗 تنظیم کوئری سفارشی",
        "btn_empty": "🧹 خالی کردن",
        "send_prompt": "📝 نام کانال (با @ یا بدون):",
        "added": "✅ {item}",
        "removed": "✅ حذف شد",
        "test_ok": "✅ به {dest} ارسال شد",
        "test_err": "❌ خطا:\n<code>{err}</code>",
        "no_pings": "❌ پینگ نداد",
        "clear_q1": "⚠️ پاک کنم؟ (۱/۲)\n⛔ غیرقابل برگشت",
        "dest_set": "✅ مقصد: {dest}",
        "name_set": "✅ نام: {name}",
        "banner_ok": "✅ بنر ذخیره شد",
        "banner_err": "❌ باید {configs} یا {proxies} داشته باشه",
        "interval_ok": "✅ هر {n} دقیقه",
        "interval_err": "❌ ۱ تا ۱۴۴۰ دقیقه (۰ برای لحظه‌ای)",
        "interval_wrong": "❌ فقط عدد",
        "max_ok": "✅ حداکثر {n}",
        "max_err": "❌ ۱ تا ۵۰",
        "src_title": "📡 منابع ({n}):",
        "src_none": "خالی",
        "reset_ok": "✅ ریست شد (#۱)",
        "sp_prompt": "📢 اسپانسر:\nفرمت: نام|url|متن دکمه|رنگ\nرنگ‌ها: primary (آبی), success (سبز), danger (قرمز)",
        "sp_added": "✅ '{name}' اضافه شد",
        "sp_removed": "✅ حذف شد",
        "sp_title": "📢 اسپانسر:",
        "sp_none": "خالی",
        "sp_err": "❌ فرمت: name|url|text|color (primary/success/danger)",
        "doc_select": "این فایل از کدوم منبعه؟",
        "doc_no_src": "❌ منبعی نیست، اول اضافه کن",
        "doc_decoding": "🔐 رمزگشایی...",
        "doc_no_pw": "❌ هیچ رمزی جواب نداد",
        "doc_no_links": "❌ لینکی پیدا نشد",
        "doc_done": "🎉 {n} کانفیگ و {p} پروکسی پست شد",
        "doc_dup": "همه تکراری بودن",
        "no_sources": "❌ هیچ منبعی تنظیم نشده",
        "test_link_prompt": "🔗 لینک کانفیگ رو بفرست (مثل vless:// یا vmess://)",
        "btn_toggle_configs": "📡 کانفیگ: {status}",
        "btn_toggle_proxies": "🌐 پروکسی: {status}",
        "toggle_configs": "✅ ارسال کانفیگ {'فعال' if status else 'غیرفعال'} شد",
        "toggle_proxies": "✅ ارسال پروکسی {'فعال' if status else 'غیرفعال'} شد",
        "profile_list": "📋 **لیست پروفایل‌ها**\n\n{list}\n\nبرای مدیریت هر کدام کلیک کنید.",
        "profile_add_prompt": "📝 نام مقصد جدید را وارد کنید (با @ یا بدون):",
        "profile_added": "✅ پروفایل '{name}' ساخته شد.",
        "profile_deleted": "❌ پروفایل حذف شد.",
        "profile_not_found": "❌ پروفایل یافت نشد.",
        "manual_send_prompt": "📤 لطفاً پیام (متن یا فایل) حاوی لینک‌های کانفیگ/پروکسی را ارسال کنید.\n\n⏳ بات به‌طور خودکار تشخیص داده و با بنر مناسب ارسال می‌کند.\n\n⚠️ **توجه:** در حالت دستی، تست پینگ انجام نمی‌شود و همه لینک‌ها حتی اگر قبلاً پست شده باشند، دوباره ارسال می‌شوند.",
        "manual_send_cancel": "❌ ارسال دستی لغو شد.",
        "manual_send_processing": "⏳ در حال پردازش...",
        "manual_send_done": "✅ ارسال دستی کامل شد.",
        "custom_query_set": "✅ کوئری سفارشی تنظیم شد: {query}",
        "custom_query_prompt": "🔗 کوئری سفارشی را وارد کنید (مثلا Telegram=@MyChannel) یا دکمه خالی را بزنید:",
        "source_list": "📡 **منابع پروفایل {name}**\n\n{sources}\n\nبرای حذف هر کدام روی دکمه مربوطه کلیک کنید.",
        "source_deleted": "✅ منبع حذف شد.",
        "toggle_numbers_ok": "✅ شماره‌گذاری {'فعال' if status else 'غیرفعال'} شد.",
        "date_cfg_toggle": "✅ نمایش تاریخ در بنر کانفیگ {'فعال' if status else 'غیرفعال'} شد.",
        "date_prx_toggle": "✅ نمایش تاریخ در بنر پروکسی {'فعال' if status else 'غیرفعال'} شد.",
        "sp_edit_prompt": "📢 **ویرایش اسپانسر**\n\nنام: {name}\nلینک: {url}\nمتن: {text}\nرنگ: {color}\nوضعیت: {'فعال' if enabled else 'غیرفعال'}\n\nبرای ویرایش هر بخش، دکمه مربوطه را بزنید.",
        "sp_edit_name": "نام جدید (خالی برای عدم تغییر):",
        "sp_edit_url": "لینک جدید (خالی برای عدم تغییر):",
        "sp_edit_text": "متن جدید دکمه (خالی برای عدم تغییر):",
        "sp_edit_color": "رنگ جدید (primary/success/danger) یا خالی برای عدم تغییر:",
        "sp_updated": "✅ اسپانسر به‌روزرسانی شد.",
        "btn_edit_sponsor": "✏️ ویرایش",
        "delete_confirm1": "⚠️ **آیا مطمئن هستید که می‌خواهید این پروفایل را حذف کنید؟**\n\nنام: {name}\nشناسه: {id}\n\nاین عملیات غیرقابل برگشت است و تمام داده‌های مربوط به این پروفایل (منابع، اسپانسرها، تاریخچه) پاک می‌شود.\n\nبرای تأیید، دکمه **«بله، حذف شود»** را بزنید.",
        "delete_confirm2": "⚠️ **تأیید نهایی حذف پروفایل**\n\nنام: {name}\nشناسه: {id}\n\n**آیا از حذف این پروفایل اطمینان دارید؟**\n\nبرای حذف نهایی، دکمه **«حذف نهایی»** را بزنید.",
        "delete_cancelled": "❌ حذف پروفایل لغو شد.",
        "btn_blacklist": "🚫 مدیریت لیست سیاه",
        "blacklist_title": "🚫 **لیست سیاه پروفایل {name}**\n\nکلمات ممنوعه:\n{words}\n\nهر کانفیگی که شامل این کلمات باشد، پست نمی‌شود.",
        "blacklist_empty": "هیچ کلمه‌ای در لیست سیاه نیست.",
        "blacklist_add_prompt": "📝 کلمه یا عبارت ممنوع را وارد کنید (چند مورد با کاما یا خط جدید):",
        "blacklist_added": "✅ کلمات اضافه شدند: {words}",
        "blacklist_removed": "✅ کلمه حذف شد.",
        "blacklist_clear": "✅ لیست سیاه پاک شد.",
        "btn_blacklist_add": "➕ افزودن",
        "btn_blacklist_clear": "🗑 پاک کردن همه",
        "btn_backup": "💾 بک‌آپ دیتابیس",
        "backup_sent": "✅ فایل دیتابیس ارسال شد.",
        "backup_failed": "❌ ارسال بک‌آپ ناموفق.",
        "btn_set_schedule_cron": "⏰ زمان‌بندی پیشرفته (cron)",
        "schedule_cron_prompt": "⏰ عبارت cron را وارد کنید (مثلاً `*/5 * * * *` برای هر ۵ دقیقه).\n\nخالی بگذارید تا از بازه‌ی دقیقه‌ای استفاده شود.",
        "schedule_cron_set": "✅ زمان‌بندی cron تنظیم شد: {cron}",
        "btn_backup_export": "📤 بک‌آپ کانفیگ/پروکسی",
        "backup_export_type": "📤 **بک‌آپ**\n\nکدام نوع را می‌خواهید؟",
        "backup_export_scope": "📤 **محدوده**\n\nهمه، ۱۰۰ تای آخر، یا تعداد دلخواه؟",
        "backup_export_count_prompt": "🔢 تعداد دلخواه را وارد کنید (عدد):",
        "backup_export_scope_all": "همه",
        "backup_export_scope_100": "۱۰۰ تای آخر",
        "backup_export_scope_custom": "تعداد دلخواه",
        "btn_timer": "⏱️ تایمر",
        "timer_menu": "⏱️ **مدیریت تایمر پروفایل {name}**\n\nوضعیت فعلی: {status}\n\nمدت زمان مکث قبل از شروع خودکار را انتخاب کنید.",
        "timer_set": "✅ تایمر {minutes} دقیقه‌ای تنظیم شد. ارسال خودکار تا پایان تایمر متوقف می‌شود.",
        "timer_cleared": "✅ تایمر لغو شد.",
        "timer_expired_notify": "⏰ تایمر پروفایل {name} (ID: {id}) به پایان رسید. ارسال خودکار از سر گرفته شد.",
        "timer_option_30m": "⏱️ ۳۰ دقیقه",
        "timer_option_1h": "⏱️ ۱ ساعت",
        "timer_option_2h": "⏱️ ۲ ساعت",
        "timer_option_4h": "⏱️ ۴ ساعت",
        "timer_option_8h": "⏱️ ۸ ساعت",
        "timer_option_custom": "⏱️ سفارشی",
        "timer_disable": "⛔ غیرفعال",
        "timer_custom_prompt": "⏱️ تعداد دقیقه را وارد کنید:",
        "timer_status_active": "⏳ {remaining} دقیقه باقی‌مانده",
        "timer_status_inactive": "غیرفعال",
        "btn_log_menu": "📋 دریافت لاگ",
        "log_menu_title": "📋 **دریافت لاگ**\n\nنوع لاگ مورد نظر را انتخاب کنید:",
        "log_range_title": "📋 **دریافت لاگ {log_type}**\n\nبازه‌ی زمانی مورد نظر را انتخاب کنید:",
        "log_range_30m": "📅 ۳۰ دقیقه",
        "log_range_1h": "📅 ۱ ساعت",
        "log_range_6h": "📅 ۶ ساعت",
        "log_range_24h": "📅 ۲۴ ساعت",
        "btn_set_backup_interval": "📦 تنظیم بازه بک‌آپ",
        "backup_interval_prompt": "📦 تعداد کانفیگ در هر فایل بک‌آپ (پیش‌فرض ۱۰۰۰):",
        "backup_interval_set": "✅ بازه بک‌آپ به {n} عدد تنظیم شد.",
        "btn_balance": "💰 مانده اعتبار",
        "balance_info": "💰 **مانده اعتبار:** {balance}",
        "credit_low": "⚠️ اعتبار کمتر از {threshold} دلار است. بک‌آپ کامل دیتابیس ارسال شد.",
        "btn_general": "⚙️ تنظیمات عمومی",
        "btn_manage_profiles": "📋 مدیریت پروفایل‌ها",
        "btn_language": "🌐 زبان",
        "lang_changed": "✅ زبان به {lang} تغییر کرد.",
        "btn_admins": "👥 مدیریت ادمین‌ها",
        "admin_list": "👥 **لیست ادمین‌ها**\n\nادمین اصلی: {main}\n\nسایر ادمین‌ها:\n{admins}",
        "admin_add_prompt": "➕ شناسه عددی ادمین جدید را وارد کنید:",
        "admin_added": "✅ ادمین با شناسه {id} اضافه شد.",
        "admin_removed": "✅ ادمین با شناسه {id} حذف شد.",
        "admin_cannot_remove_main": "❌ نمی‌توانید ادمین اصلی را حذف کنید.",
        "btn_add_admin": "➕ افزودن ادمین",
        "btn_remove_admin": "❌ حذف ادمین",
        "btn_list_admins": "📋 لیست ادمین‌ها",
        "only_admin": "❌ فقط ادمین‌ها می‌توانند از این بات استفاده کنند.",
        "btn_set_naming_template": "🏷️ قالب نام‌گذاری",
        "naming_template_prompt": "🏷️ قالب نام‌گذاری را وارد کنید.\n\nمتغیرهای قابل استفاده:\n- `{Flag}` : پرچم کشور\n- `{CHANNEL_ID}` : لینک کانال (تنظیم شده در پروفایل)\n- `{COUNT}` : شماره کانفیگ\n\nمثال:\n`{Flag} | ⚡️Telegram = {CHANNEL_ID}`\n\nبرای استفاده از شمارنده، حتماً `{COUNT}` را در قالب قرار دهید.",
        "naming_template_set": "✅ قالب نام‌گذاری تنظیم شد: {template}",
        "btn_set_channel_link": "🔗 لینک کانال",
        "channel_link_prompt": "🔗 لینک کانال را وارد کنید (مثلاً `MyChannel`):\n\nاین مقدار در قالب نام‌گذاری به جای `{CHANNEL_ID}` قرار می‌گیرد.\nاگر خالی بگذارید، از نام پروفایل استفاده می‌شود.",
        "channel_link_set": "✅ لینک کانال تنظیم شد: {link}",
        "btn_toggle_ping": "🌍 پینگ: {status}",
        "toggle_ping": "✅ پینگ {'فعال' if status else 'غیرفعال'} شد.",
        "btn_toggle_profile": "🔘 پروفایل: {status}",
        "toggle_profile": "✅ پروفایل {'فعال' if status else 'غیرفعال'} شد.",
    },
    "en": {
        "welcome": "🤖 **Config & Proxy Bot**\n\n"
                  "📡 Profiles: {profiles}\n"
                  "🔢 Next: #{next_n}\n"
                  "💰 Credit: {credit}",
        "admin_panel": "🔐 **Profile Management**\n\n"
                       "📡 Sources: {srcs} | 🎯 Dest: {dest}\n"
                       "🎨 Name: {name} | 🔢 #{num}\n"
                       "⏰ Config interval: {cfg_interval}m | Proxy interval: {prx_interval}m\n"
                       "📊 Max configs: {max_cfg} | Max proxies: {max_prx}\n"
                       "📢 Sponsor: {sponsor}\n"
                       "🌍 Ping mode: {ping_mode}\n"
                       "📡 Configs: {cfg_status} | 🌐 Proxies: {prx_status}\n"
                       "🔢 Numbering: {numbers_status}\n"
                       "🔗 Custom query: {custom_query}\n"
                       "📅 Date config: {date_cfg}\n"
                       "📅 Date proxy: {date_prx}\n"
                       "⏰ Cron: {cron}\n"
                       "⏱️ Timer: {timer_status}\n"
                       "📦 Backup every {backup_interval}\n"
                       "🏷️ Naming: {naming}\n"
                       "🔗 Channel link: {channel_link}\n"
                       "🌍 Ping: {ping_status}\n"
                       "🔘 Status: {profile_status}",
        "general_settings": "⚙️ **General Settings**\n\n"
                            "Language: {lang}\n"
                            "Admins count: {admins_count}",
        "btn_back": "🔙 Back",
        "btn_add_source": "➕ Source",
        "btn_add_dest": "➕ New Destination",
        "btn_dest_list": "📋 Destinations",
        "btn_sponsors": "📢 Sponsor",
        "btn_set_dest": "🎯 Set Destination",
        "btn_set_name": "🎨 Name",
        "btn_set_banner": "📝 Banner",
        "btn_set_banner_config": "📝 Config Banner",
        "btn_set_banner_proxy": "📝 Proxy Banner",
        "btn_set_time": "⏰ Schedule",
        "btn_set_max": "🎯 Max Post",
        "btn_stats": "📊 Stats",
        "btn_test": "🧪 Test",
        "btn_clear": "🗑 Clear DB",
        "btn_reset": "🔢 Reset Number",
        "btn_ping_mode": "🌍 Iran-only",
        "btn_runnow": "▶️ Run Now",
        "btn_instant": "⚡ Instant Update",
        "btn_manual_send": "📤 Manual Send",
        "btn_manage_sources": "📡 Manage Sources",
        "btn_toggle_numbers": "🔢 Numbering: {status}",
        "btn_set_custom_query": "🔗 Custom Query",
        "btn_empty": "🧹 Empty",
        "send_prompt": "📝 Enter channel name (with @ or without):",
        "added": "✅ {item} added",
        "removed": "✅ Removed",
        "test_ok": "✅ Sent to {dest}",
        "test_err": "❌ Error:\n<code>{err}</code>",
        "no_pings": "❌ No ping",
        "clear_q1": "⚠️ Are you sure? (1/2)\n⛔ Irreversible",
        "dest_set": "✅ Destination: {dest}",
        "name_set": "✅ Name: {name}",
        "banner_ok": "✅ Banner saved",
        "banner_err": "❌ Must contain {configs} or {proxies}",
        "interval_ok": "✅ Every {n} minutes",
        "interval_err": "❌ 1 to 1440 (0 for instant)",
        "interval_wrong": "❌ Only numbers",
        "max_ok": "✅ Max {n}",
        "max_err": "❌ 1 to 50",
        "src_title": "📡 Sources ({n}):",
        "src_none": "Empty",
        "reset_ok": "✅ Reset (#1)",
        "sp_prompt": "📢 Sponsor:\nFormat: name|url|button_text|color\nColors: primary (blue), success (green), danger (red)",
        "sp_added": "✅ '{name}' added",
        "sp_removed": "✅ Removed",
        "sp_title": "📢 Sponsor:",
        "sp_none": "None",
        "sp_err": "❌ Format: name|url|text|color (primary/success/danger)",
        "doc_select": "Which source is this file from?",
        "doc_no_src": "❌ No sources, add one first",
        "doc_decoding": "🔐 Decoding...",
        "doc_no_pw": "❌ No password worked",
        "doc_no_links": "❌ No links found",
        "doc_done": "🎉 {n} configs and {p} proxies posted",
        "doc_dup": "All duplicates",
        "no_sources": "❌ No sources set",
        "test_link_prompt": "🔗 Send config link (e.g. vless:// or vmess://)",
        "btn_toggle_configs": "📡 Configs: {status}",
        "btn_toggle_proxies": "🌐 Proxies: {status}",
        "toggle_configs": "✅ Config posting {'enabled' if status else 'disabled'}",
        "toggle_proxies": "✅ Proxy posting {'enabled' if status else 'disabled'}",
        "profile_list": "📋 **Profile List**\n\n{list}\n\nClick to manage.",
        "profile_add_prompt": "📝 Enter new destination name (with @ or without):",
        "profile_added": "✅ Profile '{name}' created.",
        "profile_deleted": "❌ Profile deleted.",
        "profile_not_found": "❌ Profile not found.",
        "manual_send_prompt": "📤 Please send a message (text or file) containing config/proxy links.\n\n⏳ The bot will automatically detect and send with appropriate banner.\n\n⚠️ **Note:** In manual mode, ping test is skipped and all links are posted even if already posted.",
        "manual_send_cancel": "❌ Manual send cancelled.",
        "manual_send_processing": "⏳ Processing...",
        "manual_send_done": "✅ Manual send complete.",
        "custom_query_set": "✅ Custom query set: {query}",
        "custom_query_prompt": "🔗 Enter custom query (e.g. Telegram=@MyChannel) or press Empty button:",
        "source_list": "📡 **Sources for {name}**\n\n{sources}\n\nClick to remove.",
        "source_deleted": "✅ Source removed.",
        "toggle_numbers_ok": "✅ Numbering {'enabled' if status else 'disabled'}.",
        "date_cfg_toggle": "✅ Date display in config banner {'enabled' if status else 'disabled'}.",
        "date_prx_toggle": "✅ Date display in proxy banner {'enabled' if status else 'disabled'}.",
        "sp_edit_prompt": "📢 **Edit Sponsor**\n\nName: {name}\nURL: {url}\nText: {text}\nColor: {color}\nStatus: {'enabled' if enabled else 'disabled'}\n\nClick button to edit.",
        "sp_edit_name": "New name (empty to keep):",
        "sp_edit_url": "New URL (empty to keep):",
        "sp_edit_text": "New button text (empty to keep):",
        "sp_edit_color": "New color (primary/success/danger) or empty to keep:",
        "sp_updated": "✅ Sponsor updated.",
        "btn_edit_sponsor": "✏️ Edit",
        "delete_confirm1": "⚠️ **Are you sure you want to delete this profile?**\n\nName: {name}\nID: {id}\n\nThis is irreversible and all data (sources, sponsors, history) will be removed.\n\nPress **'Yes, Delete'** to confirm.",
        "delete_confirm2": "⚠️ **Final confirmation to delete profile**\n\nName: {name}\nID: {id}\n\n**Are you absolutely sure?**\n\nPress **'Delete Permanently'** to finish.",
        "delete_cancelled": "❌ Profile deletion cancelled.",
        "btn_blacklist": "🚫 Blacklist",
        "blacklist_title": "🚫 **Blacklist for {name}**\n\nBlocked words:\n{words}\n\nAny config containing these words will not be posted.",
        "blacklist_empty": "No words in blacklist.",
        "blacklist_add_prompt": "📝 Enter word or phrase to block (multiple separated by comma or newline):",
        "blacklist_added": "✅ Added: {words}",
        "blacklist_removed": "✅ Word removed.",
        "blacklist_clear": "✅ Blacklist cleared.",
        "btn_blacklist_add": "➕ Add",
        "btn_blacklist_clear": "🗑 Clear All",
        "btn_backup": "💾 Backup DB",
        "backup_sent": "✅ Database file sent.",
        "backup_failed": "❌ Backup failed.",
        "btn_set_schedule_cron": "⏰ Advanced Schedule (cron)",
        "schedule_cron_prompt": "⏰ Enter cron expression (e.g. `*/5 * * * *` for every 5 minutes).\n\nLeave empty to use minute interval.",
        "schedule_cron_set": "✅ Cron schedule set: {cron}",
        "btn_backup_export": "📤 Export Configs/Proxies",
        "backup_export_type": "📤 **Backup**\n\nWhich type?",
        "backup_export_scope": "📤 **Scope**\n\nAll, last 100, or custom count?",
        "backup_export_count_prompt": "🔢 Enter custom count (number):",
        "backup_export_scope_all": "All",
        "backup_export_scope_100": "Last 100",
        "backup_export_scope_custom": "Custom Count",
        "btn_timer": "⏱️ Timer",
        "timer_menu": "⏱️ **Timer Management for {name}**\n\nStatus: {status}\n\nSelect pause duration before auto-posting resumes.",
        "timer_set": "✅ Timer set for {minutes} minutes. Auto-posting paused until timer ends.",
        "timer_cleared": "✅ Timer cleared.",
        "timer_expired_notify": "⏰ Timer for profile {name} (ID: {id}) expired. Auto-posting resumed.",
        "timer_option_30m": "⏱️ 30 min",
        "timer_option_1h": "⏱️ 1 hour",
        "timer_option_2h": "⏱️ 2 hours",
        "timer_option_4h": "⏱️ 4 hours",
        "timer_option_8h": "⏱️ 8 hours",
        "timer_option_custom": "⏱️ Custom",
        "timer_disable": "⛔ Disable",
        "timer_custom_prompt": "⏱️ Enter minutes:",
        "timer_status_active": "⏳ {remaining} min remaining",
        "timer_status_inactive": "Inactive",
        "btn_log_menu": "📋 Get Logs",
        "log_menu_title": "📋 **Get Logs**\n\nSelect log type:",
        "log_range_title": "📋 **Get {log_type} Logs**\n\nSelect time range:",
        "log_range_30m": "📅 30 min",
        "log_range_1h": "📅 1 hour",
        "log_range_6h": "📅 6 hours",
        "log_range_24h": "📅 24 hours",
        "btn_set_backup_interval": "📦 Set Backup Interval",
        "backup_interval_prompt": "📦 Number of configs per backup file (default 1000):",
        "backup_interval_set": "✅ Backup interval set to {n}.",
        "btn_balance": "💰 Balance",
        "balance_info": "💰 **Balance:** {balance}",
        "credit_low": "⚠️ Credit below {threshold} USD. Full database backup sent.",
        "btn_general": "⚙️ General Settings",
        "btn_manage_profiles": "📋 Manage Profiles",
        "btn_language": "🌐 Language",
        "lang_changed": "✅ Language changed to {lang}.",
        "btn_admins": "👥 Manage Admins",
        "admin_list": "👥 **Admin List**\n\nMain admin: {main}\n\nOther admins:\n{admins}",
        "admin_add_prompt": "➕ Enter new admin user ID (numeric):",
        "admin_added": "✅ Admin with ID {id} added.",
        "admin_removed": "✅ Admin with ID {id} removed.",
        "admin_cannot_remove_main": "❌ Cannot remove main admin.",
        "btn_add_admin": "➕ Add Admin",
        "btn_remove_admin": "❌ Remove Admin",
        "btn_list_admins": "📋 List Admins",
        "only_admin": "❌ Only admins can use this bot.",
        "btn_set_naming_template": "🏷️ Naming Template",
        "naming_template_prompt": "🏷️ Enter naming template.\n\nAvailable variables:\n- `{Flag}` : Country flag\n- `{CHANNEL_ID}` : Channel link (set in profile)\n- `{COUNT}` : Config number\n\nExample:\n`{Flag} | ⚡️Telegram = {CHANNEL_ID}`\n\nMake sure to include `{COUNT}` if you want numbering.",
        "naming_template_set": "✅ Naming template set: {template}",
        "btn_set_channel_link": "🔗 Channel Link",
        "channel_link_prompt": "🔗 Enter channel link (e.g. `MyChannel`):\n\nThis value replaces `{CHANNEL_ID}` in the naming template.\nIf left empty, profile name will be used.",
        "channel_link_set": "✅ Channel link set: {link}",
        "btn_toggle_ping": "🌍 Ping: {status}",
        "toggle_ping": "✅ Ping {'enabled' if status else 'disabled'}.",
        "btn_toggle_profile": "🔘 Profile: {status}",
        "toggle_profile": "✅ Profile {'enabled' if status else 'disabled'}.",
    }
}

def msg(key, **kwargs):
    lang = get_lang()
    text = T[lang].get(key, T["fa"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

# ======================================================================
# کیبوردها
# ======================================================================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_profiles"), callback_data="profiles_list", style="primary")],
        [InlineKeyboardButton(msg("btn_general"), callback_data="general_settings", style="primary")],
        [InlineKeyboardButton(msg("btn_balance"), callback_data="show_balance", style="primary")],
    ])

def profiles_kb():
    profiles = get_profiles()
    btns = []
    for p in profiles:
        status = "✅" if get_profile_enabled(p['id']) else "⛔"
        btns.append([InlineKeyboardButton(f"{status} {p['dest_name']} (ID:{p['id']})", callback_data=f"prof_{p['id']}", style="primary")])
    btns.append([InlineKeyboardButton("➕ Add Profile", callback_data="prof_add", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")])
    return InlineKeyboardMarkup(btns)

def profile_admin_kb(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return None
    ping_mode = prof["ping_mode"]
    ping_label = "🌍 ایران‌فقط" if ping_mode == "iran" else "🌍 جهانی"
    ping_enabled = get_profile_ping_enabled(profile_id)
    ping_status = "✅" if ping_enabled else "❌"
    profile_enabled = get_profile_enabled(profile_id)
    profile_status = "✅" if profile_enabled else "❌"

    post_cfg = prof["post_configs"] == 1
    post_prx = prof["post_proxies"] == 1
    show_num = prof["show_numbers"] == 1
    show_date_cfg = prof["show_date_config"] == 1
    show_date_prx = prof["show_date_proxy"] == 1
    cfg_status = "✅" if post_cfg else "❌"
    prx_status = "✅" if post_prx else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"

    sponsor = get_sponsor(profile_id)
    if sponsor and sponsor["enabled"]:
        sponsor_status = f"✅ {sponsor['name']}"
    elif sponsor:
        sponsor_status = f"❌ {sponsor['name']} (غیرفعال)"
    else:
        sponsor_status = "خالی"

    expiry, remaining = get_profile_timer(profile_id)
    if expiry:
        timer_status = msg("timer_status_active", remaining=remaining)
    else:
        timer_status = msg("timer_status_inactive")

    cfg_btn = msg("btn_toggle_configs", status=cfg_status)
    prx_btn = msg("btn_toggle_proxies", status=prx_status)
    num_btn = msg("btn_toggle_numbers", status=num_status)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_sources"), callback_data=f"src_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_dest_list"), callback_data=f"dl_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📢 اسپانسر: {sponsor_status}", callback_data=f"sp_menu_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_name"), callback_data=f"ac_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_banner_config"), callback_data=f"ab_config_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_banner_proxy"), callback_data=f"ab_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton("⏰ بازه کانفیگ", callback_data=f"set_cfg_interval_{profile_id}", style="primary"),
         InlineKeyboardButton("⏰ بازه پروکسی", callback_data=f"set_prx_interval_{profile_id}", style="primary")],
        [InlineKeyboardButton("📊 تعداد کانفیگ", callback_data=f"set_cfg_max_{profile_id}", style="primary"),
         InlineKeyboardButton("📊 تعداد پروکسی", callback_data=f"set_prx_max_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"{ping_label} {ping_status}", callback_data=f"tglping_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_toggle_ping", status=ping_status), callback_data=f"tgl_ping_{profile_id}", style="primary")],
        [InlineKeyboardButton(cfg_btn, callback_data=f"tglcfg_{profile_id}", style="primary"),
         InlineKeyboardButton(prx_btn, callback_data=f"tglproxy_{profile_id}", style="primary")],
        [InlineKeyboardButton(num_btn, callback_data=f"togglenum_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_toggle_profile", status=profile_status), callback_data=f"tgl_profile_{profile_id}", style="danger")],
        [InlineKeyboardButton(f"📅 تاریخ کانفیگ: {date_cfg_status}", callback_data=f"tgl_date_cfg_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📅 تاریخ پروکسی: {date_prx_status}", callback_data=f"tgl_date_prx_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_custom_query"), callback_data=f"setquery_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_stats"), callback_data=f"ast_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_backup_interval"), callback_data=f"setbackupinterval_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_test"), callback_data=f"sendtest_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_runnow"), callback_data=f"runnow_{profile_id}", style="success"),
         InlineKeyboardButton(msg("btn_instant"), callback_data=f"instant_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_manual_send"), callback_data=f"manual_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_blacklist"), callback_data=f"bl_list_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_set_schedule_cron"), callback_data=f"setcron_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_backup"), callback_data=f"backup_{profile_id}", style="success")],
        [InlineKeyboardButton(msg("btn_backup_export"), callback_data=f"backup_export_menu_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_timer"), callback_data=f"timer_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"⏱️ {timer_status}", callback_data="dummy", style="primary"),
         InlineKeyboardButton(msg("btn_log_menu"), callback_data=f"log_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_naming_template"), callback_data=f"set_naming_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_channel_link"), callback_data=f"set_channel_link_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_reset"), callback_data=f"rn_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_clear"), callback_data=f"cd1_{profile_id}", style="danger")],
        [InlineKeyboardButton("❌ Delete Profile", callback_data=f"delprof_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list", style="primary")],
    ])

def destinations_kb(profile_id):
    dest = get_profile_dest(profile_id)
    btns = []
    if dest:
        btns.append([InlineKeyboardButton(f"❌ {dest}", callback_data=f"dd_{profile_id}", style="danger")])
    btns.append([
        InlineKeyboardButton(msg("btn_add_dest"), callback_data=f"da_{profile_id}", style="success"),
    ])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def sponsor_kb(profile_id):
    sponsor = get_sponsor(profile_id)
    btns = []
    if sponsor:
        status_text = "✅ فعال" if sponsor["enabled"] else "❌ غیرفعال"
        btn_style = "success" if sponsor["enabled"] else "danger"
        btns.append([InlineKeyboardButton(f"{sponsor['name']} - {status_text}", callback_data=f"sp_toggle_{profile_id}", style=btn_style)])
        btns.append([InlineKeyboardButton("✏️ ویرایش", callback_data=f"sp_edit_{profile_id}", style="success")])
        btns.append([InlineKeyboardButton("🗑 حذف", callback_data=f"sp_clear_{profile_id}", style="danger")])
    else:
        btns.append([InlineKeyboardButton("➕ افزودن اسپانسر", callback_data=f"sp_add_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def source_list_kb(profile_id):
    sources = get_profile_sources(profile_id)
    btns = []
    if sources:
        for i, src in enumerate(sources):
            btns.append([InlineKeyboardButton(f"❌ {src}", callback_data=f"src_del_{profile_id}_{i}", style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def empty_button_kb(profile_id, callback):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_empty"), callback_data=callback, style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]
    ])

def blacklist_kb(profile_id):
    words = get_blacklist(profile_id)
    btns = []
    if words:
        for w in words:
            btns.append([InlineKeyboardButton(f"❌ {w}", callback_data=f"bl_del_{profile_id}_{w}", style="danger")])
    btns.append([InlineKeyboardButton("➕ افزودن", callback_data=f"bl_add_{profile_id}", style="success")])
    if words:
        btns.append([InlineKeyboardButton("🗑 پاک کردن همه", callback_data=f"bl_clear_{profile_id}", style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def backup_export_type_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 کانفیگ", callback_data=f"backup_export_type_{profile_id}_configs", style="primary")],
        [InlineKeyboardButton("🌐 پروکسی", callback_data=f"backup_export_type_{profile_id}_proxies", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def backup_export_scope_kb(profile_id, backup_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("backup_export_scope_all"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_all", style="primary")],
        [InlineKeyboardButton(msg("backup_export_scope_100"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_100", style="primary")],
        [InlineKeyboardButton(msg("backup_export_scope_custom"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_custom", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"backup_export_menu_{profile_id}", style="primary")],
    ])

def timer_menu_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("timer_option_30m"), callback_data=f"timer_set_{profile_id}_30", style="primary")],
        [InlineKeyboardButton(msg("timer_option_1h"), callback_data=f"timer_set_{profile_id}_60", style="primary")],
        [InlineKeyboardButton(msg("timer_option_2h"), callback_data=f"timer_set_{profile_id}_120", style="primary")],
        [InlineKeyboardButton(msg("timer_option_4h"), callback_data=f"timer_set_{profile_id}_240", style="primary")],
        [InlineKeyboardButton(msg("timer_option_8h"), callback_data=f"timer_set_{profile_id}_480", style="primary")],
        [InlineKeyboardButton(msg("timer_option_custom"), callback_data=f"timer_custom_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("timer_disable"), callback_data=f"timer_clear_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def log_range_kb(profile_id, log_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("log_range_30m"), callback_data=f"log_range_{profile_id}_{log_type}_30", style="primary")],
        [InlineKeyboardButton(msg("log_range_1h"), callback_data=f"log_range_{profile_id}_{log_type}_60", style="primary")],
        [InlineKeyboardButton(msg("log_range_6h"), callback_data=f"log_range_{profile_id}_{log_type}_360", style="primary")],
        [InlineKeyboardButton(msg("log_range_24h"), callback_data=f"log_range_{profile_id}_{log_type}_1440", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def log_menu_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 لاگ کامل", callback_data=f"log_full_{profile_id}", style="primary")],
        [InlineKeyboardButton("🚨 فقط خطاها", callback_data=f"log_errors_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")],
    ])

def general_settings_kb():
    lang = get_lang()
    lang_text = "فارسی" if lang == "fa" else "English"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🌐 زبان: {lang_text}", callback_data="toggle_lang", style="primary")],
        [InlineKeyboardButton(msg("btn_admins"), callback_data="manage_admins", style="primary")],
        [InlineKeyboardButton(msg("btn_backup"), callback_data="backup_db", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")],
    ])

def manage_admins_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_add_admin"), callback_data="add_admin", style="success")],
        [InlineKeyboardButton(msg("btn_remove_admin"), callback_data="remove_admin", style="danger")],
        [InlineKeyboardButton(msg("btn_list_admins"), callback_data="list_admins", style="primary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="general_settings", style="primary")],
    ])

# ======================================================================
# دستورات
# ======================================================================
async def cmd_start(u, ctx):
    if not is_admin(u.effective_user.id):
        return await u.message.reply_text(msg("only_admin"))
    profiles = get_profiles()
    total = len(profiles)
    next_n = 0
    if profiles:
        last_num = max(p["last_num"] for p in profiles)
        next_n = last_num + 1
    balance = await get_railway_credit()
    credit_str = f"${balance:.2f}" if balance is not None else "نامشخص"
    txt = msg("welcome", profiles=total, next_n=next_n, credit=credit_str)
    await u.message.reply_text(txt, parse_mode="HTML", reply_markup=main_menu_kb())

async def cmd_admin(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    await show_profiles_list(u.message)

async def cmd_balance(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    balance = await get_railway_credit()
    if balance is not None:
        txt = msg("balance_info", balance=f"${balance:.2f}")
    else:
        txt = "💰 اعتبار: نامشخص (توکن Railway تنظیم نشده یا خطا در دریافت)"
    await u.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")]]))

async def show_profiles_list(msg_or_q):
    profiles = get_profiles()
    if not profiles:
        txt = "❌ هیچ پروفایلی وجود ندارد.\nبرای ساخت، دکمه Add را بزنید."
    else:
        lines = []
        for p in profiles:
            status = "✅" if get_profile_enabled(p['id']) else "⛔"
            lines.append(f"• {status} `{p['dest_name']}` (ID: {p['id']}) – {len(get_profile_sources(p['id']))} منبع, بازه کانفیگ:{p.get('interval_config',5)}m, بازه پروکسی:{p.get('interval_proxy',5)}m")
        txt = msg("profile_list", list="\n".join(lines))
    kb = profiles_kb()
    try:
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_q.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise

async def cmd_runnow(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    p = await u.message.reply_text("⏳ در حال اجرا برای همه پروفایل‌ها...")
    try:
        profiles = get_profiles()
        results = []
        for prof in profiles:
            if not get_profile_enabled(prof['id']):
                results.append(f"{prof['dest_name']}: غیرفعال")
                continue
            log.info(f"🚀 /runnow for profile {prof['id']}")
            n, m = await run_cycle_for_profile(u.get_bot(), prof['id'], enable_configs=True, enable_proxies=True, is_instant=False)
            results.append(f"{prof['dest_name']}: {n} - {m}")
        await p.edit_text("✅ Done:\n" + "\n".join(results))
    except Exception as e:
        log.error(f"❌ /runnow error: {e}")
        log.error(traceback.format_exc())
        await p.edit_text(f"❌ {str(e)[:200]}")

async def cmd_runall(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    p = await u.message.reply_text("⏳ در حال اجرا (همه) برای همه پروفایل‌ها...")
    try:
        profiles = get_profiles()
        results = []
        for prof in profiles:
            if not get_profile_enabled(prof['id']):
                results.append(f"{prof['dest_name']}: غیرفعال")
                continue
            log.info(f"🚀 /runall for profile {prof['id']}")
            n, m = await run_cycle_for_profile(u.get_bot(), prof['id'], enable_configs=True, enable_proxies=True, is_instant=False)
            results.append(f"{prof['dest_name']}: {n} - {m}")
        await p.edit_text("✅ Done:\n" + "\n".join(results))
    except Exception as e:
        log.error(f"❌ /runall error: {e}")
        log.error(traceback.format_exc())
        await p.edit_text(f"❌ {str(e)[:200]}")

async def cmd_sendtest(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    profiles = get_profiles()
    if not profiles:
        return await u.message.reply_text("❌ No profiles!")
    for prof in profiles:
        dest = prof["dest_name"]
        try:
            await u.get_bot().send_message(dest, f"Test {get_tehran_time()}")
        except Exception as e:
            await u.message.reply_text(f"❌ Failed for {dest}: {e}")
    await u.message.reply_text(f"✅ Test sent to {len(profiles)} destinations")

async def cmd_diag(update: Update, context):
    if not is_admin(update.effective_user.id):
        return
    msg_lines = []
    msg_lines.append("🔍 **گزارش عیب‌یابی جامع بات**")
    msg_lines.append("")
    profiles = get_profiles()
    msg_lines.append(f"📌 تعداد پروفایل‌ها: {len(profiles)}")
    for prof in profiles:
        timer_status = "⏳ فعال" if prof.get("timer_expiry") else "⏹ غیرفعال"
        enabled_status = "✅ فعال" if get_profile_enabled(prof['id']) else "⛔ غیرفعال"
        ping_enabled = "✅" if get_profile_ping_enabled(prof['id']) else "❌"
        msg_lines.append(f"  • {prof['dest_name']} (ID:{prof['id']}) - {len(get_profile_sources(prof['id']))} منبع, بازه کانفیگ:{prof.get('interval_config',5)}m, بازه پروکسی:{prof.get('interval_proxy',5)}m, پینگ {prof['ping_mode']}, پینگ فعال:{ping_enabled}, تایمر: {timer_status}, وضعیت:{enabled_status}")
    msg_lines.append("")
    seen_cfg = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    seen_prx = c.execute("SELECT COUNT(*) FROM proxies_seen").fetchone()[0]
    msg_lines.append("💾 **دیتابیس:**")
    msg_lines.append(f"• کانفیگ‌های دیده‌شده: {seen_cfg}")
    msg_lines.append(f"• پروکسی‌های دیده‌شده: {seen_prx}")
    msg_lines.append("")
    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")

# ======================================================================
# دستور status جدید
# ======================================================================
async def cmd_status(update: Update, context):
    if not is_admin(update.effective_user.id):
        return
    profiles = get_profiles()
    lines = ["📊 **وضعیت پروفایل‌ها**"]
    for p in profiles:
        enabled = get_profile_enabled(p['id'])
        post_cfg = get_profile_post_configs(p['id'])
        post_prx = get_profile_post_proxies(p['id'])
        interval_cfg = get_profile_interval_config(p['id'])
        interval_prx = get_profile_interval_proxy(p['id'])
        timer_expiry = p.get('timer_expiry')
        timer = "⏳ فعال" if timer_expiry else "⏹ غیرفعال"
        sources_count = len(get_profile_sources(p['id']))
        dest = p['dest_name'] or "❌"
        lines.append(
            f"• {p['dest_name']} (ID:{p['id']}) – "
            f"فعال: {'✅' if enabled else '❌'}, "
            f"کانفیگ: {'✅' if post_cfg else '❌'}, "
            f"پروکسی: {'✅' if post_prx else '❌'}, "
            f"بازه‌ی کانفیگ: {interval_cfg}m, "
            f"بازه‌ی پروکسی: {interval_prx}m, "
            f"منابع: {sources_count}, "
            f"تایمر: {timer}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ======================================================================
# کالبک (اصلاح شده برای جلوگیری از خطای Query)
# ======================================================================
async def on_callback(u, ctx):
    q = u.callback_query
    # ابتدا سعی می‌کنیم به query پاسخ دهیم، اما اگر خطا رخ داد، آن را نادیده می‌گیریم
    try:
        if not is_admin(q.from_user.id):
            await q.answer(msg("only_admin"), show_alert=True)
            return
        await q.answer()  # پاسخ ساده
    except Exception as e:
        log.warning(f"Failed to answer callback query: {e}")
        # ادامه می‌دهیم، ممکن است پیام هنوز قابل ویرایش باشد

    # پردازش اصلی با try/except جامع
    try:
        d = q.data or ""
        log.info(f"📨 Callback data: {d}")

        if d == "dummy":
            return

        if d == "back_home":
            profiles = get_profiles()
            total = len(profiles)
            next_n = 0
            if profiles:
                last_num = max(p["last_num"] for p in profiles)
                next_n = last_num + 1
            balance = await get_railway_credit()
            credit_str = f"${balance:.2f}" if balance is not None else "نامشخص"
            txt = msg("welcome", profiles=total, next_n=next_n, credit=credit_str)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=main_menu_kb())
            return

        if d == "profiles_list":
            await show_profiles_list(q.message)
            return

        if d == "general_settings":
            lang = get_lang()
            lang_text = "فارسی" if lang == "fa" else "English"
            admins = list_admins()
            txt = msg("general_settings", lang=lang_text, admins_count=len(admins)+1)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=general_settings_kb())
            return

        if d == "show_balance":
            balance = await get_railway_credit()
            if balance is not None:
                txt = msg("balance_info", balance=f"${balance:.2f}")
            else:
                txt = "💰 اعتبار: نامشخص (توکن Railway تنظیم نشده یا خطا در دریافت)"
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")]]))
            return

        if d == "toggle_lang":
            current = get_lang()
            new_lang = "en" if current == "fa" else "fa"
            set_lang(new_lang)
            await q.answer(msg("lang_changed", lang=new_lang))
            lang_text = "فارسی" if new_lang == "fa" else "English"
            admins = list_admins()
            txt = msg("general_settings", lang=lang_text, admins_count=len(admins)+1)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=general_settings_kb())
            return

        if d == "manage_admins":
            admins = list_admins()
            main = MAIN_ADMIN_ID
            admin_lines = [f"• {a['user_id']} (added by {a['added_by']})" for a in admins]
            admin_list = "\n".join(admin_lines) if admin_lines else "هیچ"
            txt = msg("admin_list", main=main, admins=admin_list)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=manage_admins_kb())
            return

        if d == "add_admin":
            ctx.user_data["action"] = "add_admin"
            await q.edit_message_text(msg("admin_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="manage_admins", style="primary")]]))
            return

        if d == "list_admins":
            admins = list_admins()
            main = MAIN_ADMIN_ID
            admin_lines = [f"• {a['user_id']} (added by {a['added_by']})" for a in admins]
            admin_list = "\n".join(admin_lines) if admin_lines else "هیچ"
            txt = msg("admin_list", main=main, admins=admin_list)
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="manage_admins", style="primary")]]))
            return

        if d == "remove_admin":
            ctx.user_data["action"] = "remove_admin"
            await q.edit_message_text("❌ شناسه ادمین مورد نظر برای حذف را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="manage_admins", style="primary")]]))
            return

        if d == "backup_db":
            try:
                await q.edit_message_text("⏳ در حال تهیه بک‌آپ...")
                with open(DB_PATH, "rb") as f:
                    await q.message.reply_document(
                        document=f,
                        filename=f"bot_backup_{get_tehran_date()}.db",
                        caption=f"💾 بک‌آپ دیتابیس - {get_tehran_time()}"
                    )
                await q.message.edit_text("✅ " + msg("backup_sent"))
            except Exception as e:
                log.error(f"Backup error: {e}")
                await q.message.edit_text("❌ " + msg("backup_failed"))
            return

        if d == "prof_add":
            ctx.user_data["action"] = "prof_add"
            await q.edit_message_text(msg("profile_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list", style="primary")]]))
            return

        if d.startswith("prof_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # اسپانسر
        if d.startswith("sp_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                sponsor = get_sponsor(profile_id)
                txt = f"📢 **اسپانسر پروفایل {profile_id}**\n\n"
                if sponsor:
                    txt += f"نام: {sponsor['name']}\nلینک: {sponsor['url']}\nمتن دکمه: {sponsor['button_text']}\nرنگ: {sponsor['color']}\nوضعیت: {'فعال' if sponsor['enabled'] else 'غیرفعال'}"
                else:
                    txt += "هیچ اسپانسری تنظیم نشده است."
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["sponsor_step"] = "name"
                ctx.user_data["sponsor_profile_id"] = profile_id
                await q.edit_message_text(
                    "📝 **نام اسپانسر را وارد کنید:**",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_clear_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                clear_sponsor(profile_id)
                await q.answer("✅ اسپانسر حذف شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_toggle_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                new_status = toggle_sponsor(profile_id)
                if new_status is not None:
                    await q.answer(f"اسپانسر {'فعال' if new_status else 'غیرفعال'} شد.")
                else:
                    await q.answer("اسپانسری وجود ندارد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                sponsor = get_sponsor(profile_id)
                if not sponsor:
                    await q.answer("اسپانسری وجود ندارد.")
                    return
                ctx.user_data["sponsor_edit_profile_id"] = profile_id
                await q.edit_message_text(
                    msg("sp_edit_prompt", name=sponsor["name"], url=sponsor["url"], text=sponsor["button_text"], color=sponsor["color"], enabled=sponsor["enabled"]),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("نام", callback_data=f"sp_edit_field_{profile_id}_name", style="primary"),
                         InlineKeyboardButton("لینک", callback_data=f"sp_edit_field_{profile_id}_url", style="primary")],
                        [InlineKeyboardButton("متن دکمه", callback_data=f"sp_edit_field_{profile_id}_text", style="primary"),
                         InlineKeyboardButton("رنگ", callback_data=f"sp_edit_field_{profile_id}_color", style="primary")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_field_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    profile_id = int(parts[3])
                    field = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if field == "color":
                    await q.edit_message_text(
                        "🎨 **رنگ دکمه را انتخاب کنید:**",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔵 Primary (آبی)", callback_data=f"sp_setcolor_{profile_id}_primary", style="primary")],
                            [InlineKeyboardButton("🟢 Success (سبز)", callback_data=f"sp_setcolor_{profile_id}_success", style="success")],
                            [InlineKeyboardButton("🔴 Danger (قرمز)", callback_data=f"sp_setcolor_{profile_id}_danger", style="danger")],
                            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}", style="primary")]
                        ])
                    )
                    return
                else:
                    ctx.user_data["sponsor_edit_profile_id"] = profile_id
                    ctx.user_data["sponsor_edit_field"] = field
                    prompt = {
                        "name": "نام جدید (خالی برای عدم تغییر):",
                        "url": "لینک جدید (خالی برای عدم تغییر):",
                        "text": "متن جدید دکمه (خالی برای عدم تغییر):"
                    }.get(field, "ورودی:")
                    await q.edit_message_text(
                        prompt,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}", style="primary")]
                        ])
                    )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_setcolor_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    color = parts[3]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                update_sponsor_color(profile_id, color)
                await q.answer(f"✅ رنگ به {color} تغییر کرد.")
                if ctx.user_data.get("sponsor_step") == "color_wait":
                    name = ctx.user_data.get("sponsor_name")
                    url = ctx.user_data.get("sponsor_url")
                    btn_text = ctx.user_data.get("sponsor_button_text", "Advertisement")
                    if name and url:
                        set_sponsor(profile_id, name, url, btn_text, color, enabled=1)
                        await q.message.reply_text(msg("sp_added", name=name), parse_mode="HTML")
                        ctx.user_data.pop("sponsor_step", None)
                        ctx.user_data.pop("sponsor_name", None)
                        ctx.user_data.pop("sponsor_url", None)
                        ctx.user_data.pop("sponsor_button_text", None)
                        ctx.user_data.pop("sponsor_profile_id", None)
                        await show_profile_admin(q.message, profile_id)
                    else:
                        await q.answer("⚠️ اطلاعات اسپانسر کامل نیست.")
                else:
                    ctx.user_data.pop("sponsor_edit_field", None)
                    ctx.user_data.pop("sponsor_edit_profile_id", None)
                    await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # منابع
        if d.startswith("src_list_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                name = prof["dest_name"] if prof else ""
                sources = get_profile_sources(profile_id)
                src_text = "\n".join([f"• {s}" for s in sources]) if sources else "هیچ منبعی"
                txt = msg("source_list", name=name, sources=src_text)
                await q.edit_message_text(txt, reply_markup=source_list_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("src_del_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    idx = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                sources = get_profile_sources(profile_id)
                if 0 <= idx < len(sources):
                    removed = sources.pop(idx)
                    set_profile_sources(profile_id, sources)
                    await q.answer(msg("source_deleted"))
                    prof = get_profile(profile_id)
                    name = prof["dest_name"] if prof else ""
                    src_text = "\n".join([f"• {s}" for s in sources]) if sources else "هیچ منبعی"
                    txt = msg("source_list", name=name, sources=src_text)
                    await q.edit_message_text(txt, reply_markup=source_list_kb(profile_id))
                else:
                    await q.answer("❌ خطا در ایندکس")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sa_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"sa_{profile_id}"
                await q.edit_message_text(msg("send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"src_list_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # مقصد
        if d.startswith("dl_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                dest = get_profile_dest(profile_id)
                body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
                await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("da_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"da_{profile_id}"
                await q.edit_message_text("📝 کانال مقصد جدید رو بفرست (با @ یا بدون):\nمثال: `@MyChannel`", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"dl_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("dd_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_dest(profile_id, "")
                await q.answer(msg("removed"))
                dest = get_profile_dest(profile_id)
                body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
                await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # تنظیمات
        if d.startswith("ac_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ac_{profile_id}"
                current_name = get_profile_dest(profile_id) or "نامشخص"
                await q.edit_message_text(f"نام فعلی: {current_name}\nنام جدید را بفرست (یا دکمه خالی):", reply_markup=empty_button_kb(profile_id, f"empty_ac_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_ac_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_dest(profile_id, "")
                await q.answer("✅ نام پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ab_config_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ab_config_{profile_id}"
                cur = html.escape(get_profile_banner_config(profile_id))
                await q.edit_message_text(f"Current Config Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{configs}}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ab_proxy_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ab_proxy_{profile_id}"
                cur = html.escape(get_profile_banner_proxy(profile_id))
                await q.edit_message_text(f"Current Proxy Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{proxies}}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # بازه‌ها و max
        if d.startswith("set_cfg_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_cfg_interval_{profile_id}"
                current = get_profile_interval_config(profile_id)
                await q.edit_message_text(f"بازه فعلی کانفیگ: {current} دقیقه\n\nعدد جدید (۰ تا ۱۴۴۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_cfg_interval_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_cfg_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_prx_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_prx_interval_{profile_id}"
                current = get_profile_interval_proxy(profile_id)
                await q.edit_message_text(f"بازه فعلی پروکسی: {current} دقیقه\n\nعدد جدید (۰ تا ۱۴۴۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_prx_interval_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_prx_interval_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_cfg_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_cfg_max_{profile_id}"
                current = get_profile_max_post_config(profile_id)
                await q.edit_message_text(f"حداکثر تعداد کانفیگ فعلی: {current}\n\nعدد جدید (۱ تا ۵۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_cfg_max_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_cfg_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("set_prx_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_prx_max_{profile_id}"
                current = get_profile_max_post_proxy(profile_id)
                await q.edit_message_text(f"حداکثر تعداد پروکسی فعلی: {current}\n\nعدد جدید (۱ تا ۵۰) یا دکمه خالی:", reply_markup=empty_button_kb(profile_id, f"empty_prx_max_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_prx_max_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ast_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                n_seen = c.execute("SELECT COUNT(*) FROM seen WHERE profile_id=?", (profile_id,)).fetchone()[0]
                n_sp = c.execute("SELECT COUNT(*) FROM sponsors WHERE profile_id=?", (profile_id,)).fetchone()[0]
                next_n = get_profile_last_num(profile_id) + 1
                dest = get_profile_dest(profile_id)
                txt = f"📊 مقصد: {dest}\nمنابع: {len(get_profile_sources(profile_id))}\nاسپانسر: {n_sp}\nبعدی: #{next_n}\nحداکثر کانفیگ: {get_profile_max_post_config(profile_id)}\nحداکثر پروکسی: {get_profile_max_post_proxy(profile_id)}"
                await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sendtest_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                dest = get_profile_dest(profile_id)
                if not dest:
                    await q.answer("❌ No destination set!", show_alert=True)
                    return
                try:
                    await u.get_bot().send_message(dest, f"Test {get_tehran_time()}")
                    await q.answer("✅ Test sent")
                except Exception as e:
                    await q.answer(f"❌ {str(e)[:80]}", show_alert=True)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("runnow_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if not get_profile_enabled(profile_id):
                    await q.answer("⛔ پروفایل غیرفعال است!", show_alert=True)
                    return
                p = await q.edit_message_text("⏳ در حال اجرا...")
                try:
                    n, m = await run_cycle_for_profile(u.get_bot(), profile_id, enable_configs=True, enable_proxies=True, is_instant=False)
                    await p.edit_text(f"✅ Done: {n} - {m}")
                except Exception as e:
                    log.error(f"❌ runnow error: {e}")
                    await p.edit_text(f"❌ {str(e)[:200]}")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("instant_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_interval_config(profile_id, 0)
                set_profile_interval_proxy(profile_id, 0)
                await q.answer("⚡ حالت اپدیت لحظه‌ای برای کانفیگ و پروکسی فعال شد")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tglping_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_ping_mode(profile_id)
                new_mode = "global" if current == "iran" else "iran"
                set_profile_ping_mode(profile_id, new_mode)
                await q.answer(f"حالت پینگ: {'جهانی' if new_mode == 'global' else 'ایران'}")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_ping_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_ping_enabled(profile_id)
                new_val = not current
                set_profile_ping_enabled(profile_id, new_val)
                await q.answer(msg("toggle_ping", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_profile_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_enabled(profile_id)
                new_val = not current
                set_profile_enabled(profile_id, new_val)
                await q.answer(msg("toggle_profile", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tglcfg_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_post_configs(profile_id)
                new_val = not current
                set_profile_post_configs(profile_id, new_val)
                await q.answer(msg("toggle_configs", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tglproxy_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_post_proxies(profile_id)
                new_val = not current
                set_profile_post_proxies(profile_id, new_val)
                await q.answer(msg("toggle_proxies", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("togglenum_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_show_numbers(profile_id)
                new_val = not current
                set_profile_show_numbers(profile_id, new_val)
                await q.answer(msg("toggle_numbers_ok", status=new_val))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_date_cfg_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_show_date_config(profile_id)
                set_profile_show_date_config(profile_id, not current)
                await q.answer(msg("date_cfg_toggle", status=not current))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("tgl_date_prx_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_show_date_proxy(profile_id)
                set_profile_show_date_proxy(profile_id, not current)
                await q.answer(msg("date_prx_toggle", status=not current))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("clearquery_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_custom_query(profile_id, "")
                await q.answer("✅ کوئری سفارشی پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setquery_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setquery_{profile_id}"
                current = get_profile_custom_query(profile_id) or "خالی"
                await q.edit_message_text(f"کوئری فعلی: {current}\n" + msg("custom_query_prompt"), reply_markup=empty_button_kb(profile_id, f"empty_query_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_query_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_custom_query(profile_id, "")
                await q.answer("✅ کوئری پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("rn_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_last_num(profile_id, 0)
                await q.answer(msg("reset_ok"))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("cd1_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(msg("clear_q1"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("YES", callback_data=f"cd2_{profile_id}", style="danger")], [InlineKeyboardButton("NO", callback_data=f"prof_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("cd2_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM posts")
                c.execute("DELETE FROM country_cache")
                c.execute("DELETE FROM last_scrape WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM processed_messages WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
                c.execute("DELETE FROM blacklist WHERE profile_id=?", (profile_id,))
                set_profile_last_num(profile_id, 0)
                conn.commit()
                await q.answer("پاک شد")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("manual_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"manual_{profile_id}"
                await q.edit_message_text(msg("manual_send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"prof_{profile_id}", style="danger")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # بخش‌های جدید: backup export، timer، log، backup interval، cron، blacklist
        if d.startswith("backup_export_menu_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(msg("backup_export_type"), reply_markup=backup_export_type_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("backup_export_type_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    profile_id = int(parts[3])
                    backup_type = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["backup_export"] = {"profile_id": profile_id, "type": backup_type}
                await q.edit_message_text(msg("backup_export_scope"), reply_markup=backup_export_scope_kb(profile_id, backup_type))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("backup_export_scope_"):
            parts = d.split("_")
            if len(parts) >= 6:
                try:
                    profile_id = int(parts[3])
                    backup_type = parts[4]
                    scope = parts[5]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if scope == "all":
                    await export_backup(q, ctx, profile_id, backup_type, -1)
                    await q.edit_message_text("✅ بک‌آپ ارسال شد.")
                elif scope == "100":
                    await export_backup(q, ctx, profile_id, backup_type, 100)
                    await q.edit_message_text("✅ بک‌آپ ارسال شد.")
                elif scope == "custom":
                    ctx.user_data["backup_export_custom"] = {"profile_id": profile_id, "type": backup_type}
                    await q.edit_message_text(msg("backup_export_count_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"backup_export_menu_{profile_id}", style="primary")]]))
                else:
                    await q.answer("⚠️ محدوده نامعتبر")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                expiry, remaining = get_profile_timer(profile_id)
                if expiry:
                    status = msg("timer_status_active", remaining=remaining)
                else:
                    status = msg("timer_status_inactive")
                txt = msg("timer_menu", name=prof["dest_name"], status=status)
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=timer_menu_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_set_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    minutes = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_timer(profile_id, minutes)
                await q.answer(msg("timer_set", minutes=minutes))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_clear_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                clear_profile_timer(profile_id)
                await q.answer(msg("timer_cleared"))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("timer_custom_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"timer_custom_{profile_id}"
                await q.edit_message_text(msg("timer_custom_prompt"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(msg("btn_back"), callback_data=f"timer_menu_{profile_id}", style="primary")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("log_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(
                    msg("log_menu_title"),
                    parse_mode="HTML",
                    reply_markup=log_menu_kb(profile_id)
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("log_full_") or d.startswith("log_errors_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                log_type = "full" if d.startswith("log_full_") else "errors"
                await q.edit_message_text(
                    msg("log_range_title", log_type=log_type),
                    parse_mode="HTML",
                    reply_markup=log_range_kb(profile_id, log_type)
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("log_range_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    profile_id = int(parts[2])
                    log_type = parts[3]
                    minutes = int(parts[4])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await get_logs(q, ctx, profile_id, log_type, minutes)
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setbackupinterval_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setbackupinterval_{profile_id}"
                current = get_profile_backup_interval(profile_id)
                await q.edit_message_text(f"بازه فعلی: {current}\n{msg('backup_interval_prompt')}", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"prof_{profile_id}", style="primary")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setcron_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setcron_{profile_id}"
                current = get_profile_schedule_cron(profile_id) or "خالی"
                await q.edit_message_text(f"⏰ کرون فعلی: {current}\n\n" + msg("schedule_cron_prompt"), reply_markup=empty_button_kb(profile_id, f"empty_cron_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_cron_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_schedule_cron(profile_id, "")
                await q.answer("✅ کرون پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("bl_list_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                name = prof["dest_name"] if prof else ""
                words = get_blacklist(profile_id)
                words_text = "\n".join([f"• `{w}`" for w in words]) if words else msg("blacklist_empty")
                txt = msg("blacklist_title", name=name, words=words_text)
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=blacklist_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("bl_add_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"bl_add_{profile_id}"
                await q.edit_message_text(msg("blacklist_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"bl_list_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("bl_del_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    word = "_".join(parts[3:])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                remove_blacklist_word(profile_id, word)
                await q.answer(msg("blacklist_removed"))
                prof = get_profile(profile_id)
                name = prof["dest_name"] if prof else ""
                words = get_blacklist(profile_id)
                words_text = "\n".join([f"• `{w}`" for w in words]) if words else msg("blacklist_empty")
                txt = msg("blacklist_title", name=name, words=words_text)
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=blacklist_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("bl_clear_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                clear_blacklist(profile_id)
                await q.answer(msg("blacklist_clear"))
                prof = get_profile(profile_id)
                name = prof["dest_name"] if prof else ""
                txt = msg("blacklist_title", name=name, words=msg("blacklist_empty"))
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=blacklist_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("backup_"):
            try:
                await q.edit_message_text("⏳ در حال تهیه بک‌آپ...")
                with open(DB_PATH, "rb") as f:
                    await q.message.reply_document(
                        document=f,
                        filename=f"bot_backup_{get_tehran_date()}.db",
                        caption=f"💾 بک‌آپ دیتابیس - {get_tehran_time()}"
                    )
                await q.message.edit_text("✅ " + msg("backup_sent"))
            except Exception as e:
                log.error(f"Backup error: {e}")
                await q.message.edit_text("❌ " + msg("backup_failed"))
            return

        if d.startswith("delprof_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                txt = msg("delete_confirm1", name=prof["dest_name"], id=profile_id)
                await q.edit_message_text(
                    txt,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delprof_confirm1_{profile_id}", style="danger")],
                        [InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("delprof_confirm1_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                if not prof:
                    await q.edit_message_text(msg("profile_not_found"))
                    return
                txt = msg("delete_confirm2", name=prof["dest_name"], id=profile_id)
                await q.edit_message_text(
                    txt,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 حذف نهایی", callback_data=f"delprof_confirm2_{profile_id}", style="danger")],
                        [InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("delprof_confirm2_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                delete_profile(profile_id)
                await q.answer("✅ پروفایل حذف شد.")
                await q.edit_message_text(msg("profile_deleted"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="profiles_list", style="primary")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ===== تنظیم قالب نام‌گذاری =====
        if d.startswith("set_naming_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_naming_{profile_id}"
                current = get_profile_naming_template(profile_id)
                await q.edit_message_text(
                    f"قالب فعلی:\n`{current}`\n\n" + msg("naming_template_prompt"),
                    parse_mode="HTML",
                    reply_markup=empty_button_kb(profile_id, f"empty_naming_{profile_id}")
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_naming_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_naming_template(profile_id, "{Flag} | ⚡️Telegram = {CHANNEL_ID}")
                await q.answer("✅ قالب به پیش‌فرض برگردانده شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ===== تنظیم لینک کانال =====
        if d.startswith("set_channel_link_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"set_channel_link_{profile_id}"
                current = get_profile_channel_link(profile_id) or "خالی"
                await q.edit_message_text(
                    f"🔗 لینک کانال فعلی: `{current}`\n\n" + msg("channel_link_prompt"),
                    parse_mode="HTML",
                    reply_markup=empty_button_kb(profile_id, f"empty_channel_link_{profile_id}")
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_channel_link_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                set_profile_channel_link(profile_id, "")
                await q.answer("✅ لینک کانال پاک شد.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        await show_profiles_list(q.message)

    except Exception as e:
        log.error(f"❌ on_callback ERROR: {e}\n{traceback.format_exc()}")
        try:
            await q.edit_message_text(f"⚠️ خطا: {str(e)[:100]}")
        except:
            pass

async def show_profile_admin(msg_or_q, profile_id):
    prof = get_profile(profile_id)
    if not prof:
        txt = msg("profile_not_found")
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt)
        else:
            await msg_or_q.reply_text(txt)
        return
    srcs = get_profile_sources(profile_id)
    dest = prof["dest_name"] or "تنظیم نشده"
    last_num = prof["last_num"]
    interval_cfg = prof.get("interval_config", 5)
    interval_prx = prof.get("interval_proxy", 5)
    max_cfg = prof.get("max_post_config", 8)
    max_prx = prof.get("max_post_proxy", 10)
    show_num = prof["show_numbers"] == 1
    custom_query = prof["custom_query"] or "خالی"
    show_date_cfg = prof["show_date_config"] == 1
    show_date_prx = prof["show_date_proxy"] == 1
    cron = prof["schedule_cron"] or "خالی"
    backup_interval = get_profile_backup_interval(profile_id)
    sponsor = get_sponsor(profile_id)
    sponsor_st = f"{sponsor['name']} ({'فعال' if sponsor['enabled'] else 'غیرفعال'})" if sponsor else "خالی"
    ping_mode = prof["ping_mode"]
    ping_display = "ایران" if ping_mode == "iran" else "جهانی"
    ping_enabled = get_profile_ping_enabled(profile_id)
    ping_status = "✅" if ping_enabled else "❌"
    profile_enabled = get_profile_enabled(profile_id)
    profile_status = "✅" if profile_enabled else "❌"

    post_cfg = prof["post_configs"] == 1
    post_prx = prof["post_proxies"] == 1
    cfg_status = "✅" if post_cfg else "❌"
    prx_status = "✅" if post_prx else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"

    naming_template = get_profile_naming_template(profile_id)
    channel_link = get_profile_channel_link(profile_id) or "خالی"

    expiry, remaining = get_profile_timer(profile_id)
    if expiry:
        timer_status = msg("timer_status_active", remaining=remaining)
    else:
        timer_status = msg("timer_status_inactive")

    txt = msg(
        "admin_panel",
        srcs=len(srcs), dest=dest,
        name=dest, num=last_num,
        cfg_interval=interval_cfg, prx_interval=interval_prx,
        max_cfg=max_cfg, max_prx=max_prx,
        sponsor=sponsor_st,
        ping_mode=ping_display,
        cfg_status=cfg_status,
        prx_status=prx_status,
        numbers_status=num_status,
        custom_query=custom_query,
        date_cfg=date_cfg_status,
        date_prx=date_prx_status,
        cron=cron,
        timer_status=timer_status,
        backup_interval=backup_interval,
        naming=naming_template,
        channel_link=channel_link,
        ping_status=ping_status,
        profile_status=profile_status,
    )
    kb = profile_admin_kb(profile_id)
    try:
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_q.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise

# ======================================================================
# هندلرهای متنی و سند
# ======================================================================
async def on_text(u, ctx):
    if not is_admin(u.effective_user.id):
        return

    if ctx.user_data.get("action", "").startswith("timer_custom_"):
        profile_id = int(ctx.user_data["action"].split("_")[2])
        try:
            minutes = int(u.message.text.strip())
            if minutes <= 0:
                await u.message.reply_text("❌ عدد باید مثبت باشد.")
                return
            set_profile_timer(profile_id, minutes)
            await u.message.reply_text(msg("timer_set", minutes=minutes))
        except ValueError:
            await u.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if ctx.user_data.get("backup_export_custom"):
        data = ctx.user_data["backup_export_custom"]
        profile_id = data["profile_id"]
        backup_type = data["type"]
        try:
            count = int(u.message.text.strip())
            if count < 1:
                raise ValueError
        except:
            await u.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
            return
        await export_backup(u, ctx, profile_id, backup_type, count)
        del ctx.user_data["backup_export_custom"]
        await u.message.reply_text("✅ بک‌آپ ارسال شد.")
        return

    if ctx.user_data.get("sponsor_edit_field"):
        field = ctx.user_data["sponsor_edit_field"]
        profile_id = ctx.user_data.get("sponsor_edit_profile_id")
        if not profile_id:
            del ctx.user_data["sponsor_edit_field"]
            return
        txt = u.message.text.strip()
        sponsor = get_sponsor(profile_id)
        if not sponsor:
            await u.message.reply_text("اسپانسری وجود ندارد.")
            del ctx.user_data["sponsor_edit_field"]
            del ctx.user_data["sponsor_edit_profile_id"]
            return
        name, url, btn_text, color, enabled = sponsor["name"], sponsor["url"], sponsor["button_text"], sponsor["color"], sponsor["enabled"]
        if field == "name":
            if txt:
                name = txt
        elif field == "url":
            if txt:
                url = txt
        elif field == "text":
            if txt:
                btn_text = txt
        else:
            await u.message.reply_text("فیلد نامعتبر.")
            del ctx.user_data["sponsor_edit_field"]
            return
        set_sponsor(profile_id, name, url, btn_text, color, enabled=int(enabled))
        await u.message.reply_text(msg("sp_updated"), parse_mode="HTML")
        del ctx.user_data["sponsor_edit_field"]
        del ctx.user_data["sponsor_edit_profile_id"]
        await show_profile_admin(u.message, profile_id)
        return

    if ctx.user_data.get("sponsor_step") == "color_wait":
        await u.message.reply_text("🎨 لطفاً رنگ را با دکمه‌های بالا انتخاب کنید.")
        return

    if ctx.user_data.get("sponsor_step"):
        profile_id = ctx.user_data.get("sponsor_profile_id")
        if not profile_id:
            del ctx.user_data["sponsor_step"]
            return
        step = ctx.user_data["sponsor_step"]

        if step == "name":
            name = u.message.text.strip()
            if not name:
                await u.message.reply_text("❌ نام خالی است.")
                return
            ctx.user_data["sponsor_name"] = name
            ctx.user_data["sponsor_step"] = "url"
            await u.message.reply_text(
                "🔗 **لینک اسپانسر را وارد کنید (مثلاً https://example.com):**",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                ])
            )
            return

        if step == "url":
            url = u.message.text.strip()
            if not url:
                await u.message.reply_text("❌ لینک خالی است.")
                return
            ctx.user_data["sponsor_url"] = url
            ctx.user_data["sponsor_step"] = "button_text"
            await u.message.reply_text(
                "📝 **متن دکمه را وارد کنید (مثلاً «بازدید»):**",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                ])
            )
            return

        if step == "button_text":
            btn_text = u.message.text.strip() or "Advertisement"
            ctx.user_data["sponsor_button_text"] = btn_text
            ctx.user_data["sponsor_step"] = "color_wait"
            await u.message.reply_text(
                "🎨 **رنگ دکمه را انتخاب کنید:**",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 Primary (آبی)", callback_data=f"sp_setcolor_{profile_id}_primary", style="primary")],
                    [InlineKeyboardButton("🟢 Success (سبز)", callback_data=f"sp_setcolor_{profile_id}_success", style="success")],
                    [InlineKeyboardButton("🔴 Danger (قرمز)", callback_data=f"sp_setcolor_{profile_id}_danger", style="danger")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                ])
            )
            return

    a = ctx.user_data.get("action")
    if not a:
        return

    t = u.message.text.strip()

    if a.startswith("setbackupinterval_"):
        profile_id = int(a.split("_")[1])
        try:
            interval = int(t)
            if interval < 1:
                raise ValueError
            set_profile_backup_interval(profile_id, interval)
            await u.message.reply_text(msg("backup_interval_set", n=interval))
        except ValueError:
            await u.message.reply_text("❌ لطفاً یک عدد صحیح بزرگتر از صفر وارد کنید.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("bl_add_"):
        profile_id = int(a.split("_")[2])
        if not t:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', t)
        items = [x.strip().lower() for x in items if x.strip()]
        added = []
        for word in items:
            if add_blacklist_word(profile_id, word):
                added.append(word)
        if added:
            await u.message.reply_text(msg("blacklist_added", words=", ".join(added)))
        else:
            await u.message.reply_text("❌ هیچ کلمه‌ای اضافه نشد (تکراری یا نامعتبر).")
        del ctx.user_data["action"]
        prof = get_profile(profile_id)
        name = prof["dest_name"] if prof else ""
        words = get_blacklist(profile_id)
        words_text = "\n".join([f"• `{w}`" for w in words]) if words else msg("blacklist_empty")
        txt = msg("blacklist_title", name=name, words=words_text)
        await u.message.reply_text(txt, parse_mode="HTML", reply_markup=blacklist_kb(profile_id))
        return

    if a.startswith("setcron_"):
        profile_id = int(a.split("_")[1])
        cron = t.strip()
        if cron:
            parts = cron.split()
            if len(parts) == 5:
                set_profile_schedule_cron(profile_id, cron)
                await u.message.reply_text(msg("schedule_cron_set", cron=cron))
            else:
                await u.message.reply_text("❌ فرمت cron نامعتبر. مثال: `*/5 * * * *`")
        else:
            set_profile_schedule_cron(profile_id, "")
            await u.message.reply_text("✅ کرون پاک شد.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a == "add_admin":
        try:
            new_id = int(t.strip())
        except ValueError:
            await u.message.reply_text("❌ شناسه باید عدد باشد.")
            return
        if new_id == MAIN_ADMIN_ID:
            await u.message.reply_text("❌ این ادمین اصلی است و قبلاً وجود دارد.")
            return
        if is_admin(new_id):
            await u.message.reply_text("❌ این کاربر قبلاً ادمین است.")
            return
        add_admin(new_id, u.effective_user.id)
        await u.message.reply_text(msg("admin_added", id=new_id))
        del ctx.user_data["action"]
        await u.message.reply_text("📋 لیست ادمین‌ها:", reply_markup=manage_admins_kb())
        return

    if a == "remove_admin":
        try:
            rem_id = int(t.strip())
        except ValueError:
            await u.message.reply_text("❌ شناسه باید عدد باشد.")
            return
        if rem_id == MAIN_ADMIN_ID:
            await u.message.reply_text(msg("admin_cannot_remove_main"))
            return
        if not is_admin(rem_id):
            await u.message.reply_text("❌ این کاربر ادمین نیست.")
            return
        if remove_admin(rem_id):
            await u.message.reply_text(msg("admin_removed", id=rem_id))
        else:
            await u.message.reply_text("❌ حذف انجام نشد.")
        del ctx.user_data["action"]
        await u.message.reply_text("📋 لیست ادمین‌ها:", reply_markup=manage_admins_kb())
        return

    if a == "prof_add":
        dest_name = t if t else None
        if not dest_name:
            await u.message.reply_text("❌ نام مقصد خالی است.")
            return
        dest_name = normalize_channel_input(dest_name)
        if not dest_name:
            await u.message.reply_text("❌ نام مقصد نامعتبر است.")
            return
        profiles = get_profiles()
        if any(p["dest_name"] == dest_name for p in profiles):
            await u.message.reply_text("❌ این مقصد قبلاً وجود دارد.")
            return
        new_id = create_profile(dest_name)
        await u.message.reply_text(msg("profile_added", name=dest_name))
        del ctx.user_data["action"]
        if ENABLE_AUTO:
            app = u.get_bot()
            app.create_task(profile_loop_config(app, new_id))
            app.create_task(profile_loop_proxy(app, new_id))
            log.info(f"⏰ Started auto loops for new profile {new_id}")
        await show_profiles_list(u.message)
        return

    if a.startswith("sa_"):
        profile_id = int(a.split("_")[1])
        if not t:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', t)
        normalized_items = []
        for item in items:
            item = normalize_channel_input(item.strip())
            if item:
                normalized_items.append(item)
        if not normalized_items:
            await u.message.reply_text("❌ هیچ منبع معتبری یافت نشد.")
            return
        srcs = get_profile_sources(profile_id)
        added = []
        for item in normalized_items:
            if item not in srcs:
                srcs.append(item)
                added.append(item)
        if added:
            set_profile_sources(profile_id, srcs)
            await u.message.reply_text(msg("added", item=", ".join(added)))
        else:
            await u.message.reply_text("همه موارد تکراری بودند.")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("da_"):
        profile_id = int(a.split("_")[1])
        dest = t if t else None
        if not dest:
            set_profile_dest(profile_id, "")
            await u.message.reply_text(msg("removed"))
        else:
            dest = normalize_channel_input(dest)
            if not dest:
                await u.message.reply_text("❌ مقصد نامعتبر است.")
                return
            set_profile_dest(profile_id, dest)
            await u.message.reply_text(msg("dest_set", dest=dest))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ac_"):
        profile_id = int(a.split("_")[1])
        name = t if t else ""
        if name:
            name = normalize_channel_input(name)
        set_profile_dest(profile_id, name)
        await u.message.reply_text(msg("name_set", name=name if name else "حذف شد"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ab_config_"):
        profile_id = int(a.split("_")[2])
        if not t:
            await u.message.reply_text("❌ بنر خالی است.")
            return
        if "{configs}" in t:
            update_profile(profile_id, banner_config=t)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ab_proxy_"):
        profile_id = int(a.split("_")[2])
        if not t:
            await u.message.reply_text("❌ بنر خالی است.")
            return
        if "{proxies}" in t:
            update_profile(profile_id, banner_proxy=t)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_cfg_interval_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 0 <= n <= 1440:
            set_profile_interval_config(profile_id, n)
            await u.message.reply_text(msg("interval_ok", n=n))
        else:
            return await u.message.reply_text(msg("interval_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_prx_interval_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 0 <= n <= 1440:
            set_profile_interval_proxy(profile_id, n)
            await u.message.reply_text(msg("interval_ok", n=n))
        else:
            return await u.message.reply_text(msg("interval_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_cfg_max_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 1 <= n <= 50:
            set_profile_max_post_config(profile_id, n)
            await u.message.reply_text(msg("max_ok", n=n))
        else:
            return await u.message.reply_text(msg("max_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_prx_max_"):
        profile_id = int(a.split("_")[3])
        if not t:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(t)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 1 <= n <= 50:
            set_profile_max_post_proxy(profile_id, n)
            await u.message.reply_text(msg("max_ok", n=n))
        else:
            return await u.message.reply_text(msg("max_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=False)
        del ctx.user_data["action"]
        return

    if a.startswith("setquery_"):
        profile_id = int(a.split("_")[1])
        query = t if t else ""
        set_profile_custom_query(profile_id, query)
        await u.message.reply_text(msg("custom_query_set", query=query if query else "خالی"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_naming_"):
        profile_id = int(a.split("_")[2])
        template = t.strip()
        if not template:
            await u.message.reply_text("❌ قالب خالی است.")
            return
        if "{Flag}" not in template and "{CHANNEL_ID}" not in template and "{COUNT}" not in template:
            await u.message.reply_text("❌ قالب باید حداقل یکی از متغیرهای {Flag}، {CHANNEL_ID} یا {COUNT} را داشته باشد.")
            return
        set_profile_naming_template(profile_id, template)
        await u.message.reply_text(msg("naming_template_set", template=template))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("set_channel_link_"):
        profile_id = int(a.split("_")[2])
        channel_link = t.strip()
        set_profile_channel_link(profile_id, channel_link)
        await u.message.reply_text(msg("channel_link_set", link=channel_link if channel_link else "خالی"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

async def on_document(u, ctx):
    if not is_admin(u.effective_user.id):
        return
    a = ctx.user_data.get("action")
    if not a:
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=True)
        del ctx.user_data["action"]
        return

# ======================================================================
# ارسال دستی
# ======================================================================
async def process_manual_text(u, message, profile_id, is_document=False):
    p = await message.reply_text(msg("manual_send_processing"))
    try:
        if is_document:
            doc = message.document
            if doc.file_size and doc.file_size > 3 * 1024 * 1024:
                return await p.edit_text(">3MB")
            file = await doc.get_file()
            data = await file.download_as_bytearray()
            text = data.decode('utf-8', errors='ignore')
            if re.match(r'^[A-Za-z0-9+/=\s]+$', text):
                try:
                    decoded = base64.b64decode(text.strip(), validate=True).decode('utf-8', errors='ignore')
                    if decoded:
                        text = decoded
                except:
                    pass
        else:
            text = message.text or ""

        config_links = extract_links_from_text(text)
        proxy_links = extract_proxy_links_from_text(text)

        if not config_links and not proxy_links:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            for line in lines:
                if line.startswith("http") and "t.me/proxy" in line:
                    proxy_links.append(line)
                else:
                    config_links.extend(extract_links_from_text(line))
            config_links = list(set(config_links))
            proxy_links = list(set(proxy_links))

        if not config_links and not proxy_links:
            return await p.edit_text("❌ هیچ لینک معتبری یافت نشد.")

        new_configs = [link for link in config_links if not is_already_posted(profile_id, link)]
        new_proxies = []
        for pl in proxy_links:
            norm = normalize_proxy_url(pl)
            if norm and not is_proxy_posted(profile_id, norm):
                new_proxies.append(norm)

        if not new_configs and not new_proxies:
            return await p.edit_text("❌ هیچ لینک جدیدی برای ارسال وجود ندارد.")

        max_post_cfg = get_profile_max_post_config(profile_id)
        max_post_prx = get_profile_max_post_proxy(profile_id)

        config_chunks = [new_configs[i:i+max_post_cfg] for i in range(0, len(new_configs), max_post_cfg)]
        proxy_chunks = []
        if new_proxies:
            valid_proxies = [p for p in new_proxies if "t.me/proxy" in p.lower()]
            if valid_proxies:
                proxy_chunks = [valid_proxies[i:i+max_post_prx] for i in range(0, len(valid_proxies), max_post_prx)]

        total_configs_sent = 0
        total_proxies_sent = 0

        for chunk in config_chunks:
            working = [(url, 0, 0) for url in chunk]
            sent = await post_configs(u.get_bot(), profile_id, working, source_for_seen="manual", is_instant=False, max_post_override=len(chunk))
            if sent > 0:
                total_configs_sent += sent
            await asyncio.sleep(1)

        for chunk in proxy_chunks:
            proxy_with_ping = []
            for proxy_url in chunk:
                host, _ = extract_host(proxy_url)
                flag = "🌐"
                if host:
                    ip = await host_to_ip(host)
                    if ip:
                        flag = await get_flag_for_ip(ip)
                proxy_with_ping.append((proxy_url, 0, flag))
            cnt, payload = await post_proxies(u.get_bot(), profile_id, proxy_with_ping, is_instant=False, max_proxies_override=len(chunk))
            if cnt > 0 and payload:
                text_p, buttons = payload
                sent = await send_to_destination(u.get_bot(), profile_id, text_p, buttons)
                if sent:
                    total_proxies_sent += cnt
            await asyncio.sleep(1)

        await p.edit_text(msg("doc_done", n=total_configs_sent, p=total_proxies_sent))
    except Exception as e:
        log.error(f"manual send error: {e}")
        await p.edit_text(f"❌ {str(e)[:200]}")

async def post_working_configs(bot, profile_id, working, proxies_with_ping, force=False, skip_duplicate=False):
    total_configs = 0
    total_proxies = 0
    if working:
        total_configs = await post_configs(bot, profile_id, working, source_for_seen="manual", is_instant=False)
    if proxies_with_ping:
        cnt, payload = await post_proxies(bot, profile_id, proxies_with_ping)
        if cnt > 0 and payload:
            text, buttons = payload
            sent = await send_to_destination(bot, profile_id, text, buttons)
            if sent:
                total_proxies = cnt
    return total_configs, total_proxies

async def export_backup(update, context, profile_id, backup_type, count=None):
    try:
        profile = get_profile(profile_id)
        profile_name = profile["dest_name"].replace("@", "").strip() if profile else f"profile_{profile_id}"

        if backup_type == "configs":
            if count is None or count == -1:
                rows = c.execute("SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' ORDER BY last_posted DESC", (profile_id,)).fetchall()
            else:
                rows = c.execute("SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' ORDER BY last_posted DESC LIMIT ?", (profile_id, count)).fetchall()
            links = [row[0] for row in rows if row[0]]
            if not links:
                await update.message.reply_text("❌ هیچ کانفیگی برای بک‌آپ یافت نشد.")
                return
            filename = f"configs_backup_{profile_name}_{get_tehran_date()}.txt"
            content = f"# Backup for {profile_name} (ID: {profile_id})\n# Total: {len(links)}\n\n" + "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(DATA_DIR, filename), "rb") as f:
                await update.message.reply_document(document=f, filename=filename, caption=f"📤 {len(links)} کانفیگ - {profile_name}")
            os.remove(os.path.join(DATA_DIR, filename))
            return

        elif backup_type == "proxies":
            if count is None or count == -1:
                rows = c.execute("SELECT proxy_url FROM proxies_seen WHERE profile_id=? ORDER BY last_posted DESC", (profile_id,)).fetchall()
            else:
                rows = c.execute("SELECT proxy_url FROM proxies_seen WHERE profile_id=? ORDER BY last_posted DESC LIMIT ?", (profile_id, count)).fetchall()
            links = [row[0] for row in rows if row[0]]
            if not links:
                await update.message.reply_text("❌ هیچ پروکسی برای بک‌آپ یافت نشد.")
                return
            filename = f"proxies_backup_{profile_name}_{get_tehran_date()}.txt"
            content = f"# Backup for {profile_name} (ID: {profile_id})\n# Total: {len(links)}\n\n" + "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(DATA_DIR, filename), "rb") as f:
                await update.message.reply_document(document=f, filename=filename, caption=f"📤 {len(links)} پروکسی - {profile_name}")
            os.remove(os.path.join(DATA_DIR, filename))
            return
        else:
            await update.message.reply_text("❌ نوع نامعتبر.")
    except Exception as e:
        log.error(f"Backup export error: {e}")
        await update.message.reply_text(f"❌ خطا در بک‌آپ: {str(e)[:100]}")

# ======================================================================
# راه‌اندازی
# ======================================================================
BOT_REF = None
ENABLE_AUTO = True

async def post_init(app):
    global BOT_REF, BOT_START_TIME
    BOT_REF = app.bot
    BOT_START_TIME = datetime.utcnow()
    # پاکسازی تایمرهای منقضی‌شده در ابتدا
    for prof in get_profiles():
        expiry_str = prof.get("timer_expiry")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if expiry < datetime.now(TEHRAN_TZ):
                    clear_profile_timer(prof['id'])
                    log.info(f"Cleared expired timer for profile {prof['id']}")
            except:
                clear_profile_timer(prof['id'])

    profiles = get_profiles()
    if not profiles:
        new_id = create_profile("@VaslZone", sources="@Cfox_Server")
        log.info(f"✅ Created default profile with id {new_id}.")
        profiles = get_profiles()
    log.info(f"✅ INIT done: {len(profiles)} profiles, AUTO={ENABLE_AUTO}")

    job_queue = app.job_queue
    if job_queue:
        now = datetime.now(TEHRAN_TZ)
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        seconds_until = (target - now).total_seconds()
        job_queue.run_once(send_daily_report, when=seconds_until, chat_id=MAIN_ADMIN_ID)
        log.info(f"📅 Daily report scheduled for {target.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        log.warning("⚠️ JobQueue not available, daily report disabled.")

    if ENABLE_AUTO:
        for prof in profiles:
            log.info(f"⏰ Creating config loop for profile {prof['id']} ({prof['dest_name']})")
            app.create_task(profile_loop_config(app.bot, prof["id"]))
            log.info(f"⏰ Creating proxy loop for profile {prof['id']} ({prof['dest_name']})")
            app.create_task(profile_loop_proxy(app.bot, prof["id"]))
        log.info("⏰ Scheduler started for all profiles (config and proxy loops)")

    app.create_task(periodic_cleanup())
    log.info("🧹 Periodic cleanup task started")

    if RAILWAY_TOKEN and RAILWAY_PROJECT_ID:
        app.create_task(periodic_credit_check())
        log.info("💰 Credit check task started (Railway)")
    else:
        log.info("💰 Credit check disabled (no RAILWAY_TOKEN or PROJECT_ID)")

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("runnow", cmd_runnow))
    app.add_handler(CommandHandler("runall", cmd_runall))
    app.add_handler(CommandHandler("sendtest", cmd_sendtest))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("status", cmd_status))  # دستور جدید
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("✅ Bot is ready, polling...")
    app.run_polling()

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("🚀 Starting bot...")
    main()
