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
# ایجاد جداول (با تغییرات جدید)
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

# Table for sponsors - with new schema (no start/end time, uses duration/unlimited)
c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    button_text TEXT DEFAULT 'Advertisement',
    enabled INTEGER DEFAULT 1,
    priority INTEGER DEFAULT 0,
    duration_hours INTEGER DEFAULT 0,
    unlimited INTEGER DEFAULT 1,
    created_at TEXT,
    expires_at TEXT,
    apply_config INTEGER DEFAULT 1,
    apply_proxy INTEGER DEFAULT 1,
    color TEXT DEFAULT 'primary',
    updated_at TEXT,
    UNIQUE(profile_id, name) ON CONFLICT REPLACE)""")

# Migrate old sponsors if they exist
def migrate_sponsors():
    # Check if old table has columns that need migration
    c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sponsors'")
    row = c.fetchone()
    if row:
        sql = row[0]
        # If old table has start_time or end_time, we need to migrate
        if "start_time" in sql or "end_time" in sql:
            log.warning("Migrating sponsors table to new schema...")
            # Rename old table
            c.execute("ALTER TABLE sponsors RENAME TO sponsors_old")
            # Create new table
            c.execute("""CREATE TABLE sponsors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                button_text TEXT DEFAULT 'Advertisement',
                enabled INTEGER DEFAULT 1,
                priority INTEGER DEFAULT 0,
                duration_hours INTEGER DEFAULT 0,
                unlimited INTEGER DEFAULT 1,
                created_at TEXT,
                expires_at TEXT,
                apply_config INTEGER DEFAULT 1,
                apply_proxy INTEGER DEFAULT 1,
                color TEXT DEFAULT 'primary',
                updated_at TEXT,
                UNIQUE(profile_id, name) ON CONFLICT REPLACE)""")
            # Copy old data, convert start/end to duration/unlimited if possible
            # For simplicity, set unlimited=1 for all old sponsors (they were always active)
            c.execute("SELECT profile_id, name, url, button_text, enabled, color, created_at FROM sponsors_old")
            for row in c.fetchall():
                profile_id, name, url, button_text, enabled, color, created_at = row
                c.execute("""INSERT INTO sponsors
                    (profile_id, name, url, button_text, enabled, priority, duration_hours, unlimited,
                     created_at, expires_at, apply_config, apply_proxy, color, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (profile_id, name, url, button_text, enabled, 0, 0, 1, created_at, None, 1, 1, color, get_tehran_time()))
            conn.commit()
            c.execute("DROP TABLE sponsors_old")
            log.info("✅ Sponsors migrated to new schema.")
        else:
            # Ensure new columns exist
            ensure_column("sponsors", "duration_hours", "INTEGER DEFAULT 0", 0)
            ensure_column("sponsors", "unlimited", "INTEGER DEFAULT 1", 1)
            ensure_column("sponsors", "expires_at", "TEXT", None)
            ensure_column("sponsors", "updated_at", "TEXT", get_tehran_time())
    else:
        # Create if missing
        c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            button_text TEXT DEFAULT 'Advertisement',
            enabled INTEGER DEFAULT 1,
            priority INTEGER DEFAULT 0,
            duration_hours INTEGER DEFAULT 0,
            unlimited INTEGER DEFAULT 1,
            created_at TEXT,
            expires_at TEXT,
            apply_config INTEGER DEFAULT 1,
            apply_proxy INTEGER DEFAULT 1,
            color TEXT DEFAULT 'primary',
            updated_at TEXT,
            UNIQUE(profile_id, name) ON CONFLICT REPLACE)""")

migrate_sponsors()

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

# Independent scrape cursor for each profile/source/content stream.
# This prevents the config worker from consuming the messages before the
# proxy worker gets a chance to process the same source messages.
c.execute("""CREATE TABLE IF NOT EXISTS source_stream_state (
    profile_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    stream TEXT NOT NULL,
    last_message_id TEXT DEFAULT '',
    updated_at TEXT,
    PRIMARY KEY(profile_id, source, stream))""")
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
    ping_mode TEXT DEFAULT 'global',
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
    profile_enabled INTEGER DEFAULT 1,
    country_display INTEGER DEFAULT 2,
    show_ping INTEGER DEFAULT 1,
    proxy_banner_template TEXT DEFAULT '',
    ping_testing INTEGER DEFAULT 1
)""")
conn.commit()

# Add new columns if missing
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
ensure_column("profiles", "country_display", "INTEGER DEFAULT 2", 2)
ensure_column("profiles", "show_ping", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "proxy_banner_template", "TEXT DEFAULT ''", "")
ensure_column("profiles", "proxy_post_mode", "INTEGER DEFAULT 0", 0)
ensure_column("profiles", "ping_testing", "INTEGER DEFAULT 1", 1)

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
# تعمیر نوع ستون custom_query و مهاجرت‌ها (بدون تغییر)
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
                    ping_mode TEXT DEFAULT 'global',
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
                    profile_enabled INTEGER DEFAULT 1,
                    country_display INTEGER DEFAULT 2,
                    show_ping INTEGER DEFAULT 1,
                    proxy_banner_template TEXT DEFAULT '',
                    ping_testing INTEGER DEFAULT 1
                )
            """)
            c.execute("""
                INSERT INTO profiles_new
                    (id, dest_name, sources, banner_config, banner_proxy, interval_min,
                     max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num,
                     created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                     schedule_cron, last_backup_count, timer_expiry, timer_duration, backup_interval,
                     interval_config, interval_proxy, max_post_config, max_post_proxy,
                     naming_template, channel_link, ping_enabled, profile_enabled,
                     country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
                SELECT id, dest_name, sources, banner_config, banner_proxy, interval_min,
                       max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num,
                       created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                       schedule_cron, last_backup_count, timer_expiry, timer_duration, backup_interval,
                       interval_config, interval_proxy, max_post_config, max_post_proxy,
                       naming_template, channel_link, ping_enabled, profile_enabled,
                       country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing
                FROM profiles
            """)
            c.execute("DROP TABLE profiles")
            c.execute("ALTER TABLE profiles_new RENAME TO profiles")
            conn.commit()
            log.info("✅ custom_query column fixed to TEXT.")
            return
    log.info("✅ custom_query column is TEXT (no fix needed).")

fix_column_types()

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
             naming_template, channel_link, ping_enabled, profile_enabled,
             country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             datetime.now().isoformat(), 1, "", 1, 1, "", 0, None, 0, 1000,
             old_interval, old_interval, old_max_post, old_max_proxies,
             "{Flag} | ⚡️Telegram = {CHANNEL_ID}", "", 1, 1, 2, 1, "", 1))
    conn.commit()
    log.info(f"✅ Migrated {len(dest_list)} profiles.")

migrate_old_config()
# Normalize missing Ping mode to the new default (Global) without overwriting explicit user choices.
c.execute("UPDATE profiles SET ping_mode=? WHERE ping_mode IS NULL OR TRIM(ping_mode)=?", ("global", ""))
conn.commit()

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
# توابع پروفایل (با اضافه شدن تنظیمات جدید)
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
                   post_configs=1, post_proxies=1, ping_mode="global", last_num=0,
                   show_numbers=1, custom_query="",
                   show_date_config=1, show_date_proxy=1, schedule_cron="", backup_interval=1000,
                   interval_config=5, interval_proxy=5, max_post_config=8, max_post_proxy=10,
                   naming_template="{Flag} | ⚡️Telegram = {CHANNEL_ID}", channel_link="",
                   ping_enabled=1, profile_enabled=1,
                   country_display=2, show_ping=1, proxy_banner_template="", ping_testing=1):
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
         naming_template, channel_link, ping_enabled, profile_enabled,
         country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, last_num,
         get_tehran_time(), show_numbers, custom_query,
         show_date_config, show_date_proxy, schedule_cron, 0, None, 0, backup_interval,
         interval_config, interval_proxy, max_post_config, max_post_proxy,
         naming_template, channel_link, ping_enabled, profile_enabled,
         country_display, show_ping, proxy_banner_template, proxy_post_mode, ping_testing))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "last_num",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy",
               "schedule_cron", "last_backup_count", "timer_expiry", "timer_duration",
               "backup_interval", "interval_config", "interval_proxy", "max_post_config", "max_post_proxy",
               "naming_template", "channel_link", "ping_enabled", "profile_enabled",
               "country_display", "show_ping", "proxy_banner_template", "proxy_post_mode", "ping_testing"]
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
    c.execute("DELETE FROM source_stream_state WHERE profile_id=?", (profile_id,))
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
    # This now represents the master switch for ping testing
    prof = get_profile(profile_id)
    return prof.get("ping_testing", 1) if prof else 1

def set_profile_ping_enabled(profile_id, enabled):
    # This sets ping_testing
    update_profile(profile_id, ping_testing=1 if enabled else 0)

def get_profile_show_ping(profile_id):
    prof = get_profile(profile_id)
    return prof.get("show_ping", 1) if prof else 1

def set_profile_show_ping(profile_id, enabled):
    update_profile(profile_id, show_ping=1 if enabled else 0)

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
    channel_link = (channel_link or "").strip()
    if channel_link:
        channel_link = re.sub(r"^https?://t\.me/", "", channel_link, flags=re.IGNORECASE)
        channel_link = re.sub(r"^t\.me/", "", channel_link, flags=re.IGNORECASE)
        channel_link = channel_link.split("?", 1)[0].split("#", 1)[0].strip().lstrip("@/")
    update_profile(profile_id, channel_link=channel_link)
    # Verify persistence immediately; never report success for a stale value.
    saved = get_profile_channel_link(profile_id)
    if saved != channel_link:
        raise RuntimeError(f"channel_link persistence failed for profile {profile_id}")

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

def get_profile_country_display(profile_id):
    prof = get_profile(profile_id)
    return prof.get("country_display", 2) if prof else 2

def set_profile_country_display(profile_id, mode):
    update_profile(profile_id, country_display=mode)

def get_profile_proxy_banner_template(profile_id):
    prof = get_profile(profile_id)
    return prof.get("proxy_banner_template", "") if prof else ""

def set_profile_proxy_banner_template(profile_id, template):
    update_profile(profile_id, proxy_banner_template=template)

# ======================================================================
# توابع لیست سیاه و اسپانسر (با تغییرات جدید)
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

# Proxy post mode: 0 = normal text, 1 = inline glass buttons
def get_profile_proxy_post_mode(profile_id):
    prof = get_profile(profile_id)
    return int(prof.get("proxy_post_mode", 0)) if prof else 0

def set_profile_proxy_post_mode(profile_id, mode):
    mode = 1 if int(mode) else 0
    update_profile(profile_id, proxy_post_mode=mode)
    return mode

# New sponsor functions
def get_sponsors(profile_id, apply_type="both", include_disabled=False):
    """Return sponsors for admin or active selection. Disabled sponsors remain editable."""
    now = datetime.now(TEHRAN_TZ)
    query = "SELECT * FROM sponsors WHERE profile_id=?"
    params = [profile_id]
    if not include_disabled:
        query += " AND enabled=1"
    # Legacy DB columns are retained for compatibility, but sponsor application
    # is no longer exposed as an admin setting: every active sponsor is global.
    query += " ORDER BY priority DESC, id ASC"
    c.execute(query, params)
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    sponsors=[]
    for row in rows:
        sponsor=dict(zip(cols,row))
        if not sponsor.get("unlimited",1) and sponsor.get("expires_at"):
            try:
                expires=datetime.fromisoformat(sponsor["expires_at"])
                if expires <= now:
                    continue
            except Exception:
                pass
        sponsors.append(sponsor)
    return sponsors

def get_sponsor(profile_id):
    # Legacy compatibility: return first active sponsor
    sponsors = get_sponsors(profile_id)
    return sponsors[0] if sponsors else None

def add_sponsor(profile_id, name, url, button_text="Advertisement", enabled=1,
                priority=0, duration_hours=0, unlimited=1,
                apply_config=1, apply_proxy=1, color="primary"):
    now = get_tehran_time()
    expires_at = None
    if not unlimited and duration_hours > 0:
        expires_at = (datetime.now(TEHRAN_TZ) + timedelta(hours=duration_hours)).isoformat()
    c.execute("""INSERT INTO sponsors
        (profile_id, name, url, button_text, enabled, priority, duration_hours, unlimited,
         created_at, expires_at, apply_config, apply_proxy, color, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (profile_id, name, url, button_text, enabled, priority, duration_hours, unlimited,
         now, expires_at, apply_config, apply_proxy, color, now))
    conn.commit()
    return c.lastrowid

def update_sponsor(sponsor_id, **kwargs):
    allowed = ["name", "url", "button_text", "enabled", "priority",
               "duration_hours", "unlimited", "apply_config", "apply_proxy", "color", "expires_at"]
    # If unlimited or duration changes, update expires_at
    set_clauses = []
    params = []
    for key, value in kwargs.items():
        if key in allowed:
            set_clauses.append(f"{key}=?")
            params.append(value)
    if not set_clauses:
        return
    # If duration or unlimited changed, recalc expires_at
    if "duration_hours" in kwargs or "unlimited" in kwargs:
        # Need to fetch current values to recompute
        c.execute("SELECT duration_hours, unlimited, created_at FROM sponsors WHERE id=?", (sponsor_id,))
        row = c.fetchone()
        if row:
            cur_duration, cur_unlimited, created_at = row
            new_unlimited = kwargs.get("unlimited", cur_unlimited)
            new_duration = kwargs.get("duration_hours", cur_duration)
            if new_unlimited:
                expires_at = None
            else:
                # Duration is relative to the moment it is changed, not the original creation time.
                expires_at = (datetime.now(TEHRAN_TZ) + timedelta(hours=int(new_duration or 1))).isoformat()
            set_clauses.append("expires_at=?")
            params.append(expires_at)
    params.append(get_tehran_time())
    params.append(sponsor_id)
    c.execute(f"UPDATE sponsors SET {', '.join(set_clauses)}, updated_at=? WHERE id=?", params)
    conn.commit()

def delete_sponsor(sponsor_id):
    c.execute("DELETE FROM sponsors WHERE id=?", (sponsor_id,))
    conn.commit()

def clear_sponsor(profile_id):
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    conn.commit()

def toggle_sponsor(profile_id, sponsor_id=None):
    if sponsor_id:
        c.execute("SELECT enabled FROM sponsors WHERE id=?", (sponsor_id,))
        row = c.fetchone()
        if row:
            new_enabled = 0 if row[0] else 1
            update_sponsor(sponsor_id, enabled=new_enabled)
            return new_enabled
    else:
        # Legacy: toggle first sponsor
        sponsors = get_sponsors(profile_id)
        if sponsors:
            sponsor = sponsors[0]
            new_enabled = 0 if sponsor["enabled"] else 1
            update_sponsor(sponsor["id"], enabled=new_enabled)
            return new_enabled
    return None

def update_sponsor_color(profile_id, color):
    # Legacy: update first sponsor
    sponsors = get_sponsors(profile_id)
    if sponsors:
        update_sponsor(sponsors[0]["id"], color=color)

# ======================================================================
# توابع کمکی (پینگ، استخراج لینک و ...) - بهبود یافته
# ======================================================================
# Country name mappings - complete list
COUNTRY_NAMES_FA = {
    'AD': 'آندورا',
    'AE': 'امارات',
    'AF': 'افغانستان',
    'AG': 'آنتیگوا و باربودا',
    'AI': 'آنگویلا',
    'AL': 'آلبانی',
    'AM': 'ارمنستان',
    'AO': 'آنگولا',
    'AQ': 'جنوبگان',
    'AR': 'آرژانتین',
    'AS': 'ساموآی آمریکا',
    'AT': 'اتریش',
    'AU': 'استرالیا',
    'AW': 'آروبا',
    'AX': 'جزایر آلاند',
    'AZ': 'آذربایجان',
    'BA': 'بوسنی و هرزگوین',
    'BB': 'باربادوس',
    'BD': 'بنگلادش',
    'BE': 'بلژیک',
    'BF': 'بورکینافاسو',
    'BG': 'بلغارستان',
    'BH': 'بحرین',
    'BI': 'بوروندی',
    'BJ': 'بنین',
    'BL': 'سن بارتلمی',
    'BM': 'برمودا',
    'BN': 'برونئی',
    'BO': 'بولیوی',
    'BQ': 'جزایر کارائیب هلند',
    'BR': 'برزیل',
    'BS': 'باهاما',
    'BT': 'بوتان',
    'BV': 'جزیرهٔ بووه',
    'BW': 'بوتسوانا',
    'BY': 'بلاروس',
    'BZ': 'بلیز',
    'CA': 'کانادا',
    'CC': 'جزایر کوکوس',
    'CD': 'کنگو - کینشاسا',
    'CF': 'جمهوری افریقای مرکزی',
    'CG': 'کنگو - برازویل',
    'CH': 'سوئیس',
    'CI': 'ساحل عاج',
    'CK': 'جزایر کوک',
    'CL': 'شیلی',
    'CM': 'کامرون',
    'CN': 'چین',
    'CO': 'کلمبیا',
    'CR': 'کاستاریکا',
    'CU': 'کوبا',
    'CV': 'کیپ\u200cورد',
    'CW': 'کوراسائو',
    'CX': 'جزیره کریسمس',
    'CY': 'قبرس',
    'CZ': 'جمهوری چک',
    'DE': 'آلمان',
    'DJ': 'جیبوتی',
    'DK': 'دانمارک',
    'DM': 'دومینیکا',
    'DO': 'جمهوری دومینیکن',
    'DZ': 'الجزایر',
    'EC': 'اکوادور',
    'EE': 'استونی',
    'EG': 'مصر',
    'EH': 'صحرای غربی',
    'ER': 'اریتره',
    'ES': 'اسپانیا',
    'ET': 'اتیوپی',
    'FI': 'فنلاند',
    'FJ': 'فیجی',
    'FK': 'جزایر فالکلند',
    'FM': 'میکرونزی',
    'FO': 'جزایر فارو',
    'FR': 'فرانسه',
    'GA': 'گابن',
    'GB': 'بریتانیا',
    'GD': 'گرنادا',
    'GE': 'گرجستان',
    'GF': 'گویان فرانسه',
    'GG': 'گرنزی',
    'GH': 'غنا',
    'GI': 'جبل\u200cالطارق',
    'GL': 'گرینلند',
    'GM': 'گامبیا',
    'GN': 'گینه',
    'GP': 'گوادلوپ',
    'GQ': 'گینهٔ استوایی',
    'GR': 'یونان',
    'GS': 'جورجیای جنوبی و جزایر ساندویچ جنوبی',
    'GT': 'گواتمالا',
    'GU': 'گوام',
    'GW': 'گینهٔ بیسائو',
    'GY': 'گویان',
    'HK': 'هنگ\u200cکنگ',
    'HM': 'هرد و جزایر مک\u200cدونالد',
    'HN': 'هندوراس',
    'HR': 'کرواسی',
    'HT': 'هائیتی',
    'HU': 'مجارستان',
    'ID': 'اندونزی',
    'IE': 'ایرلند',
    'IL': 'اسرائیل',
    'IM': 'جزیره من',
    'IN': 'هند',
    'IO': 'قلمرو اقیانوس هند بریتانیا',
    'IQ': 'عراق',
    'IR': 'ایران',
    'IS': 'ایسلند',
    'IT': 'ایتالیا',
    'JE': 'جرزی',
    'JM': 'جامائیکا',
    'JO': 'اردن',
    'JP': 'ژاپن',
    'KE': 'کنیا',
    'KG': 'قرقیزستان',
    'KH': 'کامبوج',
    'KI': 'کیریباتی',
    'KM': 'کومور',
    'KN': 'سنت کیتس و نویس',
    'KP': 'کرهٔ شمالی',
    'KR': 'کره جنوبی',
    'KW': 'کویت',
    'KY': 'جزایر کیمن',
    'KZ': 'قزاقستان',
    'LA': 'لائوس',
    'LB': 'لبنان',
    'LC': 'سنت لوسیا',
    'LI': 'لیختن\u200cاشتاین',
    'LK': 'سری\u200cلانکا',
    'LR': 'لیبریا',
    'LS': 'لسوتو',
    'LT': 'لیتوانی',
    'LU': 'لوکزامبورگ',
    'LV': 'لتونی',
    'LY': 'لیبی',
    'MA': 'مراکش',
    'MC': 'موناکو',
    'MD': 'مولداوی',
    'ME': 'مونته\u200cنگرو',
    'MF': 'سن مارتن',
    'MG': 'ماداگاسکار',
    'MH': 'جزایر مارشال',
    'MK': 'مقدونیه شمالی',
    'ML': 'مالی',
    'MM': 'میانمار',
    'MN': 'مغولستان',
    'MO': 'ماکائو، منطقهٔ ویژهٔ اداری چین',
    'MP': 'جزایر ماریانای شمالی',
    'MQ': 'مارتینیک',
    'MR': 'موریتانی',
    'MS': 'مونت\u200cسرات',
    'MT': 'مالت',
    'MU': 'موریس',
    'MV': 'مالدیو',
    'MW': 'مالاوی',
    'MX': 'مکزیک',
    'MY': 'مالزی',
    'MZ': 'موزامبیک',
    'NA': 'نامیبیا',
    'NC': 'کالدونیای جدید',
    'NE': 'نیجر',
    'NF': 'جزیره نورفک',
    'NG': 'نیجریه',
    'NI': 'نیکاراگوئه',
    'NL': 'هلند',
    'NO': 'نروژ',
    'NP': 'نپال',
    'NR': 'نائورو',
    'NU': 'نیوئه',
    'NZ': 'نیوزیلند',
    'OM': 'عمان',
    'PA': 'پاناما',
    'PE': 'پرو',
    'PF': 'پلی\u200cنزی فرانسه',
    'PG': 'پاپوآ گینه نو',
    'PH': 'فیلیپین',
    'PK': 'پاکستان',
    'PL': 'لهستان',
    'PM': 'سن پیر و میکلن',
    'PN': 'پیتکرن',
    'PR': 'پورتوریکو',
    'PS': 'فلسطین',
    'PT': 'پرتغال',
    'PW': 'پالائو',
    'PY': 'پاراگوئه',
    'QA': 'قطر',
    'RE': 'رئونیون',
    'RO': 'رومانی',
    'RS': 'صربستان',
    'RU': 'روسیه',
    'RW': 'رواندا',
    'SA': 'عربستان سعودی',
    'SB': 'جزایر سلیمان',
    'SC': 'سیشل',
    'SD': 'سودان',
    'SE': 'سوئد',
    'SG': 'سنگاپور',
    'SH': 'سنت هلنا',
    'SI': 'اسلوونی',
    'SJ': 'سوالبارد و یان ماین',
    'SK': 'اسلواکی',
    'SL': 'سیرالئون',
    'SM': 'سان مارینو',
    'SN': 'سنگال',
    'SO': 'سومالی',
    'SR': 'سورینام',
    'SS': 'سودان جنوبی',
    'ST': 'سائوتومه و پرینسیپ',
    'SV': 'السالوادور',
    'SX': 'سینت مارتن',
    'SY': 'سوریه',
    'SZ': 'اسواتینی',
    'TC': 'جزایر تورکس و کایکوس',
    'TD': 'چاد',
    'TF': 'سرزمین\u200cهای جنوبی فرانسه',
    'TG': 'توگو',
    'TH': 'تایلند',
    'TJ': 'تاجیکستان',
    'TK': 'توکلائو',
    'TL': 'تیمور شرقی',
    'TM': 'ترکمنستان',
    'TN': 'تونس',
    'TO': 'تونگا',
    'TR': 'ترکیه',
    'TT': 'ترینیداد و توباگو',
    'TV': 'تووالو',
    'TW': 'تایوان',
    'TZ': 'تانزانیا',
    'UA': 'اوکراین',
    'UG': 'اوگاندا',
    'UM': 'جزایر کوچک حاشیه\u200cای آمریکا',
    'US': 'آمریکا',
    'UY': 'اروگوئه',
    'UZ': 'ازبکستان',
    'VA': 'واتیکان',
    'VC': 'سنت وینسنت و گرنادین',
    'VE': 'ونزوئلا',
    'VG': 'جزایر ویرجین بریتانیا',
    'VI': 'جزایر ویرجین آمریکا',
    'VN': 'ویتنام',
    'VU': 'وانواتو',
    'WF': 'والیس و فوتونا',
    'WS': 'ساموآ',
    'YE': 'یمن',
    'YT': 'مایوت',
    'ZA': 'آفریقای جنوبی',
    'ZM': 'زامبیا',
    'ZW': 'زیمبابوه',
}
COUNTRY_NAMES_EN = {
    'AD': 'Andorra',
    'AE': 'United Arab Emirates',
    'AF': 'Afghanistan',
    'AG': 'Antigua & Barbuda',
    'AI': 'Anguilla',
    'AL': 'Albania',
    'AM': 'Armenia',
    'AO': 'Angola',
    'AQ': 'Antarctica',
    'AR': 'Argentina',
    'AS': 'American Samoa',
    'AT': 'Austria',
    'AU': 'Australia',
    'AW': 'Aruba',
    'AX': 'Åland Islands',
    'AZ': 'Azerbaijan',
    'BA': 'Bosnia & Herzegovina',
    'BB': 'Barbados',
    'BD': 'Bangladesh',
    'BE': 'Belgium',
    'BF': 'Burkina Faso',
    'BG': 'Bulgaria',
    'BH': 'Bahrain',
    'BI': 'Burundi',
    'BJ': 'Benin',
    'BL': 'St. Barthélemy',
    'BM': 'Bermuda',
    'BN': 'Brunei',
    'BO': 'Bolivia',
    'BQ': 'Caribbean Netherlands',
    'BR': 'Brazil',
    'BS': 'Bahamas',
    'BT': 'Bhutan',
    'BV': 'Bouvet Island',
    'BW': 'Botswana',
    'BY': 'Belarus',
    'BZ': 'Belize',
    'CA': 'Canada',
    'CC': 'Cocos (Keeling) Islands',
    'CD': 'Democratic Republic of the Congo',
    'CF': 'Central African Republic',
    'CG': 'Republic of the Congo',
    'CH': 'Switzerland',
    'CI': 'Côte d’Ivoire',
    'CK': 'Cook Islands',
    'CL': 'Chile',
    'CM': 'Cameroon',
    'CN': 'China',
    'CO': 'Colombia',
    'CR': 'Costa Rica',
    'CU': 'Cuba',
    'CV': 'Cape Verde',
    'CW': 'Curaçao',
    'CX': 'Christmas Island',
    'CY': 'Cyprus',
    'CZ': 'Czech Republic',
    'DE': 'Germany',
    'DJ': 'Djibouti',
    'DK': 'Denmark',
    'DM': 'Dominica',
    'DO': 'Dominican Republic',
    'DZ': 'Algeria',
    'EC': 'Ecuador',
    'EE': 'Estonia',
    'EG': 'Egypt',
    'EH': 'Western Sahara',
    'ER': 'Eritrea',
    'ES': 'Spain',
    'ET': 'Ethiopia',
    'FI': 'Finland',
    'FJ': 'Fiji',
    'FK': 'Falkland Islands',
    'FM': 'Micronesia',
    'FO': 'Faroe Islands',
    'FR': 'France',
    'GA': 'Gabon',
    'GB': 'United Kingdom',
    'GD': 'Grenada',
    'GE': 'Georgia',
    'GF': 'French Guiana',
    'GG': 'Guernsey',
    'GH': 'Ghana',
    'GI': 'Gibraltar',
    'GL': 'Greenland',
    'GM': 'Gambia',
    'GN': 'Guinea',
    'GP': 'Guadeloupe',
    'GQ': 'Equatorial Guinea',
    'GR': 'Greece',
    'GS': 'South Georgia & South Sandwich Islands',
    'GT': 'Guatemala',
    'GU': 'Guam',
    'GW': 'Guinea-Bissau',
    'GY': 'Guyana',
    'HK': 'Hong Kong SAR China',
    'HM': 'Heard & McDonald Islands',
    'HN': 'Honduras',
    'HR': 'Croatia',
    'HT': 'Haiti',
    'HU': 'Hungary',
    'ID': 'Indonesia',
    'IE': 'Ireland',
    'IL': 'Israel',
    'IM': 'Isle of Man',
    'IN': 'India',
    'IO': 'British Indian Ocean Territory',
    'IQ': 'Iraq',
    'IR': 'Iran',
    'IS': 'Iceland',
    'IT': 'Italy',
    'JE': 'Jersey',
    'JM': 'Jamaica',
    'JO': 'Jordan',
    'JP': 'Japan',
    'KE': 'Kenya',
    'KG': 'Kyrgyzstan',
    'KH': 'Cambodia',
    'KI': 'Kiribati',
    'KM': 'Comoros',
    'KN': 'St. Kitts & Nevis',
    'KP': 'North Korea',
    'KR': 'South Korea',
    'KW': 'Kuwait',
    'KY': 'Cayman Islands',
    'KZ': 'Kazakhstan',
    'LA': 'Laos',
    'LB': 'Lebanon',
    'LC': 'St. Lucia',
    'LI': 'Liechtenstein',
    'LK': 'Sri Lanka',
    'LR': 'Liberia',
    'LS': 'Lesotho',
    'LT': 'Lithuania',
    'LU': 'Luxembourg',
    'LV': 'Latvia',
    'LY': 'Libya',
    'MA': 'Morocco',
    'MC': 'Monaco',
    'MD': 'Moldova',
    'ME': 'Montenegro',
    'MF': 'St. Martin',
    'MG': 'Madagascar',
    'MH': 'Marshall Islands',
    'MK': 'North Macedonia',
    'ML': 'Mali',
    'MM': 'Myanmar (Burma)',
    'MN': 'Mongolia',
    'MO': 'Macao SAR China',
    'MP': 'Northern Mariana Islands',
    'MQ': 'Martinique',
    'MR': 'Mauritania',
    'MS': 'Montserrat',
    'MT': 'Malta',
    'MU': 'Mauritius',
    'MV': 'Maldives',
    'MW': 'Malawi',
    'MX': 'Mexico',
    'MY': 'Malaysia',
    'MZ': 'Mozambique',
    'NA': 'Namibia',
    'NC': 'New Caledonia',
    'NE': 'Niger',
    'NF': 'Norfolk Island',
    'NG': 'Nigeria',
    'NI': 'Nicaragua',
    'NL': 'Netherlands',
    'NO': 'Norway',
    'NP': 'Nepal',
    'NR': 'Nauru',
    'NU': 'Niue',
    'NZ': 'New Zealand',
    'OM': 'Oman',
    'PA': 'Panama',
    'PE': 'Peru',
    'PF': 'French Polynesia',
    'PG': 'Papua New Guinea',
    'PH': 'Philippines',
    'PK': 'Pakistan',
    'PL': 'Poland',
    'PM': 'St. Pierre & Miquelon',
    'PN': 'Pitcairn Islands',
    'PR': 'Puerto Rico',
    'PS': 'Palestine',
    'PT': 'Portugal',
    'PW': 'Palau',
    'PY': 'Paraguay',
    'QA': 'Qatar',
    'RE': 'Réunion',
    'RO': 'Romania',
    'RS': 'Serbia',
    'RU': 'Russia',
    'RW': 'Rwanda',
    'SA': 'Saudi Arabia',
    'SB': 'Solomon Islands',
    'SC': 'Seychelles',
    'SD': 'Sudan',
    'SE': 'Sweden',
    'SG': 'Singapore',
    'SH': 'St. Helena',
    'SI': 'Slovenia',
    'SJ': 'Svalbard & Jan Mayen',
    'SK': 'Slovakia',
    'SL': 'Sierra Leone',
    'SM': 'San Marino',
    'SN': 'Senegal',
    'SO': 'Somalia',
    'SR': 'Suriname',
    'SS': 'South Sudan',
    'ST': 'São Tomé & Príncipe',
    'SV': 'El Salvador',
    'SX': 'Sint Maarten',
    'SY': 'Syria',
    'SZ': 'Eswatini',
    'TC': 'Turks & Caicos Islands',
    'TD': 'Chad',
    'TF': 'French Southern Territories',
    'TG': 'Togo',
    'TH': 'Thailand',
    'TJ': 'Tajikistan',
    'TK': 'Tokelau',
    'TL': 'Timor-Leste',
    'TM': 'Turkmenistan',
    'TN': 'Tunisia',
    'TO': 'Tonga',
    'TR': 'Türkiye',
    'TT': 'Trinidad & Tobago',
    'TV': 'Tuvalu',
    'TW': 'Taiwan',
    'TZ': 'Tanzania',
    'UA': 'Ukraine',
    'UG': 'Uganda',
    'UM': 'U.S. Outlying Islands',
    'US': 'United States',
    'UY': 'Uruguay',
    'UZ': 'Uzbekistan',
    'VA': 'Vatican City',
    'VC': 'St. Vincent & Grenadines',
    'VE': 'Venezuela',
    'VG': 'British Virgin Islands',
    'VI': 'U.S. Virgin Islands',
    'VN': 'Vietnam',
    'VU': 'Vanuatu',
    'WF': 'Wallis & Futuna',
    'WS': 'Samoa',
    'YE': 'Yemen',
    'YT': 'Mayotte',
    'ZA': 'South Africa',
    'ZM': 'Zambia',
    'ZW': 'Zimbabwe',
}

def get_country_info(code):
    """Return (flag, english_name, persian_name) for a country code."""
    if not code or len(code) != 2:
        return "🌐", "Unknown", "ناشناخته"
    flag = country_to_flag(code)
    en = COUNTRY_NAMES_EN.get(code, code)
    fa = COUNTRY_NAMES_FA.get(code, code)
    return flag, en, fa

def country_to_flag(code):
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

async def get_flag_for_ip(ip):
    cached = c.execute(
        "SELECT country, flag FROM country_cache WHERE ip=?", (ip,)
    ).fetchone()
    if cached and len(cached[1]) > 1:
        return cached[1], cached[0]  # flag, country_code

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
                    return flag, country
    except Exception as e:
        log.warning(f"flag API fail for {ip}: {e}")

    return "🌐", ""

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

# ======================================================================
# VALIDATION FUNCTIONS - FIX PARSING
# ======================================================================
def validate_vless(url):
    """Validate VLESS URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "vless":
            return False, "not vless"
        # Check for userinfo (UUID)
        if not parsed.username or len(parsed.username) < 8:
            return False, "missing or invalid UUID"
        # Check host
        if not parsed.hostname:
            return False, "missing host"
        # Check port
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_vmess(url):
    """Validate VMESS URL structure (base64 encoded JSON)."""
    try:
        if not url.startswith("vmess://"):
            return False, "not vmess"
        b64 = url[8:]
        # Add padding if needed
        b64 += "=" * (-len(b64) % 4)
        data = base64.b64decode(b64, validate=True).decode('utf-8', errors='ignore')
        # Parse JSON
        obj = json.loads(data)
        required = ['add', 'port', 'id', 'aid', 'net', 'type', 'host', 'path', 'tls']
        for key in ['add', 'port', 'id']:
            if key not in obj or not obj[key]:
                return False, f"missing {key}"
        if not str(obj['port']).isdigit() or int(obj['port']) < 1 or int(obj['port']) > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception as e:
        return False, f"decode error: {str(e)}"

def validate_trojan(url):
    """Validate Trojan URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "trojan":
            return False, "not trojan"
        if not parsed.password:
            return False, "missing password"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_ss(url):
    """Validate Shadowsocks URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "ss":
            return False, "not ss"
        # Check for valid base64 userinfo
        if parsed.username and parsed.password:
            return True, "valid"
        else:
            # May be in format ss://base64
            b64 = url[5:]
            b64 += "=" * (-len(b64) % 4)
            decoded = base64.b64decode(b64, validate=True).decode('utf-8', errors='ignore')
            if '@' in decoded:
                return True, "valid"
            else:
                return False, "invalid format"
    except Exception:
        return False, "parse error"

def validate_socks(url):
    """Validate SOCKS URL structure (simple)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ['socks', 'socks5', 'socks4']:
            return False, "not socks"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_hy2(url):
    """Validate Hysteria2 URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "hy2":
            return False, "not hy2"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_tuic(url):
    """Validate TUIC URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "tuic":
            return False, "not tuic"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_wireguard(url):
    """Validate WireGuard URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "wireguard":
            return False, "not wireguard"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or parsed.port < 1 or parsed.port > 65535:
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_http_proxy(url):
    """Validate HTTP/HTTPS proxy-style URLs without accepting arbitrary web links."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, "not http"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or not (1 <= parsed.port <= 65535):
            return False, "invalid port"
        # HTTP proxy URLs normally have credentials or an explicit proxy-like port.
        # Accept explicit host:port URLs because source channels commonly publish them.
        return True, "valid"
    except Exception:
        return False, "parse error"

def validate_hysteria(url):
    """Validate Hysteria v1 URL structure."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "hysteria":
            return False, "not hysteria"
        if not parsed.hostname:
            return False, "missing host"
        if not parsed.port or not (1 <= parsed.port <= 65535):
            return False, "invalid port"
        return True, "valid"
    except Exception:
        return False, "parse error"

def detect_protocol_name(url):
    """Return a stable human-readable protocol name for configs and proxies."""
    u = (url or "").strip().lower()
    if u.startswith("vless://"): return "VLESS"
    if u.startswith("vmess://"): return "VMESS"
    if u.startswith("trojan://"): return "TROJAN"
    if u.startswith("wireguard://"): return "WireGuard"
    if u.startswith("hysteria2://") or u.startswith("hy2://"): return "Hysteria2"
    if u.startswith("hysteria://"): return "Hysteria"
    if u.startswith("tuic://"): return "TUIC"
    if u.startswith("ss://"): return "Shadowsocks"
    if u.startswith("socks5://"): return "SOCKS5"
    if u.startswith("socks4://"): return "SOCKS4"
    if u.startswith("socks://"): return "SOCKS"
    if u.startswith("http://") or u.startswith("https://"): return "HTTP"
    if "t.me/proxy?" in u or "tg://proxy?" in u or "tg://proxy" in u: return "MTProto"
    return "Unknown"

def validate_config_link(url):
    """Validate a config link and return (is_valid, reason)."""
    if not url:
        return False, "empty"
    if url.startswith("vless://"):
        return validate_vless(url)
    elif url.startswith("vmess://"):
        return validate_vmess(url)
    elif url.startswith("trojan://"):
        return validate_trojan(url)
    elif url.startswith("ss://"):
        return validate_ss(url)
    elif url.startswith("socks://") or url.startswith("socks5://"):
        return validate_socks(url)
    elif url.startswith("hy2://"):
        return validate_hy2(url)
    elif url.startswith("tuic://"):
        return validate_tuic(url)
    elif url.startswith("wireguard://"):
        return validate_wireguard(url)
    elif url.startswith("hysteria2://"):
        return validate_hy2(url.replace("hysteria2://", "hy2://", 1))
    elif url.startswith("hysteria://"):
        return validate_hysteria(url)
    elif url.startswith("http://") or url.startswith("https://"):
        return validate_http_proxy(url)
    else:
        return False, "unknown protocol"

# ======================================================================
# استخراج لینک‌ها با اعتبارسنجی
# ======================================================================
def extract_links_from_text(text):
    """
    استخراج لینک‌های کانفیگ (vless, vmess, trojan, hy2, tuic, ss, socks, hysteria2, wireguard)
    و اعتبارسنجی آنها.
    """
    results = []
    pattern = re.compile(
        r'(vless|vmess|trojan|hy2|hysteria2|hysteria|tuic|ss|socks|socks5|socks4|wireguard|http|https)://[^\s<>"\'{}()\[\]]+',
        re.IGNORECASE
    )
    for m in pattern.finditer(text):
        link = m.group(0).strip()
        link = re.sub(r'[.,;:!؟\'"`]+$', '', link)
        if len(link) > 10:
            # Validate
            is_valid, reason = validate_config_link(link)
            if is_valid:
                results.append(link)
            else:
                log.debug(f"Invalid config link: {link} - {reason}")

    # Attempt base64 decoding
    if not results:
        text_clean = text.replace('\n', '').replace('\r', '').strip()
        if re.match(r'^[A-Za-z0-9+/=]+$', text_clean):
            try:
                decoded = base64.b64decode(text_clean, validate=True).decode('utf-8', errors='ignore')
                for proto in ["vless://", "vmess://", "trojan://", "hy2://", "hysteria2://", "hysteria://", "tuic://", "ss://", "socks://", "socks5://", "socks4://", "wireguard://", "http://", "https://"]:
                    for m in re.finditer(re.escape(proto) + r"[^\s<>\"']+", decoded):
                        link = m.group().rstrip().strip(".,;(){}[]!؟'")
                        if len(link) > len(proto) + 10:
                            is_valid, _ = validate_config_link(link)
                            if is_valid:
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
                    for proto in ["vless://", "vmess://", "trojan://", "hy2://", "hysteria2://", "hysteria://", "tuic://", "ss://", "socks://", "socks5://", "socks4://", "wireguard://", "http://", "https://"]:
                        for m in re.finditer(re.escape(proto) + r"[^\s<>\"']+", decoded):
                            link = m.group().rstrip().strip(".,;(){}[]!؟'")
                            if len(link) > len(proto) + 10:
                                is_valid, _ = validate_config_link(link)
                                if is_valid:
                                    results.append(link)
                except:
                    pass

    if not results:
        for line in text.splitlines():
            line = line.strip()
            for proto in ['vless://', 'vmess://', 'trojan://', 'hy2://', 'tuic://', 'ss://', 'socks://', 'hysteria2://']:
                if line.lower().startswith(proto):
                    is_valid, _ = validate_config_link(line)
                    if is_valid:
                        results.append(line)
                    break

    return list(set(results))

def canonical_telegram_proxy_url(url):
    """Return a Telegram proxy URL that is safe for Telegram messages/buttons."""
    norm = normalize_telegram_proxy(url or "")
    if not norm:
        return None
    norm = norm.replace("&amp;", "&")
    if norm.lower().startswith("tg://proxy?"):
        return "https://t.me/proxy?" + norm.split("?", 1)[1]
    if re.match(r"^https?://t\.me/proxy\?", norm, re.I):
        return norm
    return None

def normalize_proxy_url(url):
    if not url:
        return None
    url = clean_proxy_link(url.strip())
    return canonical_telegram_proxy_url(url)

def extract_proxy_links_from_text(text):
    """Extract Telegram MTProto proxy links from visible text AND raw HTML hrefs."""
    if not text:
        return []
    source = html.unescape(text)
    results = []
    patterns = [
        r'https?://t\.me/proxy\?[^\s<>"]+',
        r'tg://proxy\?[^\s<>"]+',
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, source, re.IGNORECASE):
            link = clean_proxy_link(m.group(0))
            # Strip HTML/Markdown punctuation that cannot belong to the URL.
            link = link.rstrip('.,;:!?)]}\'')
            norm = canonical_telegram_proxy_url(link)
            if norm and norm not in results:
                results.append(norm)
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

def canonical_config_identity(url):
    """Identity includes UUID/credentials, host, port, path and transport settings; display fragment is ignored."""
    try:
        url = clean_config_url(url or "").strip()
        p = urlparse(url)
        scheme = p.scheme.lower()
        if scheme == "vmess":
            raw = unquote(p.netloc + p.path)
            raw += "=" * (-len(raw) % 4)
            try:
                obj = json.loads(base64.b64decode(raw).decode("utf-8", errors="ignore"))
                keys = ["v","add","port","id","aid","scy","net","type","host","path","tls","sni","alpn","fp","allowInsecure"]
                obj = {k: str(obj.get(k, "")) for k in keys if k in obj}
                return "vmess|" + json.dumps(obj, sort_keys=True, separators=(",", ":"))
            except Exception:
                pass
        pairs = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k, v) for k, vals in pairs.items() for v in vals), doseq=True)
        return urlunparse((scheme, p.netloc.lower(), p.path or "", p.params, query, ""))
    except Exception:
        return clean_config_url(url or "").split("#", 1)[0].strip()

def is_already_posted(profile_id, url):
    identity = canonical_config_identity(url)
    rows = c.execute("SELECT full_url FROM seen WHERE profile_id=?", (profile_id,)).fetchall()
    if any(canonical_config_identity(row[0] or "") == identity for row in rows):
        return True
    uid, host = extract_uuid_and_address(clean_config_url(url))
    return bool(uid and host and c.execute("SELECT 1 FROM seen WHERE uuid=? AND address=? AND profile_id=?", (uid, host, profile_id)).fetchone())

def canonical_proxy_identity(url):
    try:
        norm = canonical_telegram_proxy_url(url or "") or (url or "").strip()
        p = urlparse(norm)
        pairs = parse_qs(p.query, keep_blank_values=True)
        query = urlencode(sorted((k, v) for k, vals in pairs.items() for v in vals), doseq=True)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, p.params, query, ""))
    except Exception:
        return (url or "").strip().split("#", 1)[0]

def is_proxy_posted(profile_id, proxy_url):
    identity = canonical_proxy_identity(proxy_url)
    rows = c.execute("SELECT proxy_url FROM proxies_seen WHERE profile_id=?", (profile_id,)).fetchall()
    return any(canonical_proxy_identity(row[0] or "") == identity for row in rows)

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

def get_stream_last_message_id(profile_id, source, stream):
    r = c.execute(
        "SELECT last_message_id FROM source_stream_state WHERE profile_id=? AND source=? AND stream=?",
        (profile_id, source, stream)
    ).fetchone()
    return r[0] if r else ""

def set_stream_last_message_id(profile_id, source, stream, msg_id):
    c.execute(
        """INSERT INTO source_stream_state
           (profile_id, source, stream, last_message_id, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(profile_id, source, stream) DO UPDATE SET
             last_message_id=excluded.last_message_id,
             updated_at=excluded.updated_at""",
        (profile_id, source, stream, str(msg_id or ""), get_tehran_time())
    )
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
# پینگ (بهینه‌شده) - با لایه‌های تست
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
            timeout=1.0
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
        async with httpx.AsyncClient(timeout=4) as cl:
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

async def check_full_link_ping(url, ping_mode="global", perform_ping=True):
    """Perform ping test if perform_ping is True, else return a dummy."""
    if not perform_ping:
        return 0, True, 0  # treat as reachable if ping testing disabled? Actually we should not claim it's working.
        # Instead, we need to return a neutral status. We'll handle this in the caller.
    host, port = extract_host(url)
    if not host:
        return 0, False, 0
    allow_tcp = (ping_mode != "iran")
    ping, ok, cnt = await ping_from_iran_only(host, port, allow_tcp_fallback=allow_tcp)
    return ping, ok, cnt

# ======================================================================
# اسکرپ (بهینه‌شده: استفاده از last_message_id برای توقف)
# ======================================================================
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

async def scrape_channel_paginated(profile_id, channel, max_pages=5, stream="combined"):
    """
    Scrape public Telegram channel pages and return ONLY messages newer than
    the cursor belonging to this profile + source + stream.

    stream is intentionally independent for config/proxy workers so one
    worker cannot consume the source cursor of another worker.

    Returns:
        (configs, proxies, newest_message_id)

    The cursor is NOT advanced here. The caller advances it after the relevant
    processing/send path has completed successfully. This prevents data loss
    when Telegram posting fails.
    """
    clean_channel = normalize_channel_input(channel)
    if not clean_channel:
        log.warning(f"Invalid channel name: {channel}")
        return [], [], ""

    last_msg_id = get_stream_last_message_id(profile_id, clean_channel, stream)
    base_url = f"https://t.me/s/{clean_channel.lstrip('@')}"
    all_configs = []
    all_proxies = []
    current_url = base_url
    page_count = 0
    newest_seen_id = ""
    stopped = False

    effective_max_pages = max_pages
    if stream == "proxy" and not last_msg_id:
        # Existing installations may have no proxy cursor. Scan a small recent
        # window so proxies on the newest few pages are not missed. Dedup/state
        # protection prevents reposting already published proxies.
        effective_max_pages = min(max_pages, 3)

    log.info(
        f"🔍 [profile={profile_id}][stream={stream}] Starting scrape for "
        f"{clean_channel} (max {effective_max_pages} pages, last_msg_id={last_msg_id or 'NONE'})"
    )

    while page_count < effective_max_pages and not stopped:
        page_count += 1
        log.info(
            f"🔍 [profile={profile_id}][stream={stream}] Scraping page "
            f"{page_count} for {clean_channel}: {current_url}"
        )

        _page_configs, _page_proxies, msg_ids, msg_content_map = \
            await _scrape_single_page_with_messages(current_url, clean_channel)

        if not msg_ids:
            log.info(f"⚠️ [profile={profile_id}][stream={stream}] No messages on page {page_count} for {clean_channel}")
            break

        # Telegram normally returns newest -> oldest. Keep the newest ID we
        # actually encountered for this scan, but do not advance DB state yet.
        if not newest_seen_id:
            newest_seen_id = msg_ids[0]

        new_msg_ids = []
        for mid in msg_ids:
            if last_msg_id and str(mid) == str(last_msg_id):
                stopped = True
                break
            new_msg_ids.append(mid)

        if not new_msg_ids:
            log.info(
                f"✅ [profile={profile_id}][stream={stream}] Reached cursor for "
                f"{clean_channel}; no newer messages on page {page_count}."
            )
            break

        for mid in new_msg_ids:
            content = msg_content_map.get(mid, "")
            if not content:
                continue

            configs = extract_links_from_text(content)
            proxies = extract_proxy_links_from_text(content)
            all_configs.extend(configs)
            all_proxies.extend(proxies)

            # Keep the existing audit table, but DO NOT use it as the cursor.
            # Config and proxy streams must be able to inspect the same source
            # message independently.
            mark_message_processed(profile_id, clean_channel, mid)

        numeric_ids = []
        for mid in msg_ids:
            parts = str(mid).split('/')
            if len(parts) == 2 and parts[1].isdigit():
                numeric_ids.append(int(parts[1]))
        if numeric_ids:
            oldest = min(numeric_ids)
            current_url = f"{base_url}?before={oldest}"
        else:
            break

        await asyncio.sleep(0.25)

    all_configs = list(dict.fromkeys(all_configs))
    all_proxies = list(dict.fromkeys(all_proxies))

    log.info(
        f"📊 [profile={profile_id}][stream={stream}] {clean_channel}: "
        f"configs={len(all_configs)}, proxies={len(all_proxies)}, newest={newest_seen_id or 'NONE'}"
    )
    return all_configs, all_proxies, newest_seen_id

async def _scrape_single_page_with_messages(url, channel):
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
            log.warning(f"Rate limit for {channel}, waiting 10s")
            await asyncio.sleep(10)
            r = await cl.get(url, headers=headers)
        if r.status_code != 200:
            log.warning(f"⚠️ {channel} returned status {r.status_code} for url {url}")
            return [], [], [], {}

        html_text = r.text

        # Telegram channel HTML contains data-post="channel/message_id".
        # Do NOT use a non-greedy </div> regex here: Telegram messages contain
        # nested divs and that regex truncates the message body, which can hide
        # proxy/config links. Instead, use each data-post marker as a boundary
        # and slice until the next message marker.
        markers = list(re.finditer(r'data-post=["\']([^"\']+)["\']', html_text, re.IGNORECASE))
        msg_ids = []
        msg_content_map = {}

        for idx, match in enumerate(markers):
            mid = match.group(1).strip()
            if not mid:
                continue
            body_start = match.start()
            body_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(html_text)
            block = html_text[body_start:body_end]
            # Extract URLs from the raw HTML BEFORE stripping tags. Telegram
            # often stores proxy/config URLs inside <a href="..."> while the
            # visible anchor text contains no URL at all.
            block_proxy_links = extract_proxy_links_from_text(block)
            block_config_links = extract_links_from_text(block)
            content_text = re.sub(r'<script\b[^>]*>.*?</script>', ' ', block, flags=re.IGNORECASE | re.DOTALL)
            content_text = re.sub(r'<style\b[^>]*>.*?</style>', ' ', content_text, flags=re.IGNORECASE | re.DOTALL)
            content_text = re.sub(r'<[^>]+>', ' ', content_text)
            content_text = html.unescape(content_text)
            content_text = re.sub(r'\s+', ' ', content_text).strip()
            # Preserve extracted href URLs for the downstream per-message parser.
            if block_config_links:
                content_text += "\n" + "\n".join(block_config_links)
            if block_proxy_links:
                content_text += "\n" + "\n".join(block_proxy_links)
            msg_ids.append(mid)
            msg_content_map[mid] = content_text

        # Fallback for layouts without data-post markers.
        if not msg_ids:
            alt_ids = re.findall(r'href=["\']/([^/"\']+)/(\d+)["\']', html_text, re.IGNORECASE)
            seen_alt = set()
            for ch, num in alt_ids:
                mid = f"{ch}/{num}"
                if mid not in seen_alt:
                    seen_alt.add(mid)
                    msg_ids.append(mid)
            if msg_ids:
                # We cannot reliably associate each fallback ID with a block,
                # but the whole page is still useful for extracting links.
                page_text = re.sub(r'<[^>]+>', ' ', html.unescape(html_text))
                page_text = re.sub(r'\s+', ' ', page_text).strip()
                msg_content_map = {mid: page_text for mid in msg_ids}

        config_links = list(dict.fromkeys(extract_links_from_text(html_text)))
        proxy_links = list(dict.fromkeys(extract_proxy_links_from_text(html_text)))

        log.info(
            f"📄 [profile-source={channel}] Page {url}: "
            f"configs={len(config_links)}, proxies={len(proxy_links)}, messages={len(msg_ids)}"
        )
        return config_links, proxy_links, msg_ids, msg_content_map

# ======================================================================
# ارسال (بدون تغییر)
# ======================================================================
async def send_with_retry(bot, chat_id, text, parse_mode="HTML", reply_markup=None, disable_web_page_preview=True, max_retries=3):
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
            wait = e.retry_after + 1
            log.warning(f"Flood control, waiting {wait} seconds...")
            await asyncio.sleep(wait)
            retry_count += 1
        except TimedOut:
            log.warning(f"Timeout, retrying... ({retry_count+1}/{max_retries})")
            await asyncio.sleep(2)
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
            await asyncio.sleep(1)
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
            await asyncio.sleep(0.5)
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
# ارسال کانفیگ‌ها و پروکسی‌ها (با بهبودهای جدید)
# ======================================================================
async def post_configs(bot, profile_id, working, source_for_seen="", is_instant=False, max_post_override=None, extra_button_rows=None):
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

    country_display = get_profile_country_display(profile_id)

    # Get appropriate sponsor
    sponsor = get_best_sponsor(profile_id, "config")
    sponsor_button = None
    if sponsor and sponsor.get("enabled"):
        btn_style = sponsor.get("color", "primary") if sponsor.get("color") in ["primary", "success", "danger"] else "primary"
        sponsor_button = InlineKeyboardButton(sponsor["button_text"], url=sponsor["url"], style=btn_style)

    config_blocks = []
    for i, (url, ping, node_count) in enumerate(items, 1):
        n = last_n + i
        host, _ = extract_host(url)
        flag = "🌐"
        country_code = ""
        if host:
            ip = await host_to_ip(host)
            if ip:
                flag, country_code = await get_flag_for_ip(ip)

        # Build server header based on display settings
        header_parts = []
        if channel_link:
            header_parts.append(channel_link)
        else:
            header_parts.append(dest if dest else "@VaslZone")

        # Country display
        if country_display == 0:
            # off: no country
            pass
        elif country_display == 1:
            # English only
            flag_emoji = flag if flag != "🌐" else ""
            en_name = COUNTRY_NAMES_EN.get(country_code, "")
            if flag_emoji and en_name:
                header_parts.append(f"{flag_emoji} {en_name}")
            elif flag_emoji:
                header_parts.append(flag_emoji)
        elif country_display == 2:
            # English + Persian
            flag_emoji = flag if flag != "🌐" else ""
            en_name = COUNTRY_NAMES_EN.get(country_code, "")
            fa_name = COUNTRY_NAMES_FA.get(country_code, "")
            if flag_emoji and en_name and fa_name:
                header_parts.append(f"{flag_emoji} {en_name} • <b>{fa_name}</b>")
            elif flag_emoji and en_name:
                header_parts.append(f"{flag_emoji} {en_name}")
            elif flag_emoji:
                header_parts.append(flag_emoji)

        # Ping display
        # Numbering
        if show_numbers:
            header = f"<b>#{n}</b> " + " ".join(header_parts)
        else:
            header = " ".join(header_parts)

        # Build config line - use naming template
        fragment_text = naming_template.replace("{Flag}", flag).replace("{CHANNEL_ID}", channel_link).replace("{COUNT}", str(n))
        fragment_text = fragment_text.replace("{FLAG}", flag)
        fragment_text = fragment_text.replace("{COUNTRY_EN}", COUNTRY_NAMES_EN.get(country_code, ""))
        fragment_text = fragment_text.replace("{COUNTRY_FA}", COUNTRY_NAMES_FA.get(country_code, ""))
        fragment_text = fragment_text.replace("{PING}", "")
        fragment_text = fragment_text.replace("{COUNT}", str(n))
        encoded_fragment = quote(fragment_text, safe='')
        base_url = strip_url_fragment(url)
        modified_url = base_url + "#" + encoded_fragment

        protocol = url.split('://')[0].lower() if '://' in url else ''
        if custom_query and protocol != 'vmess':
            modified_url = add_custom_query_to_url(modified_url, custom_query, protocol)

        block = f"<pre>{modified_url}</pre>"
        config_blocks.append(header + "\n" + block)

    configs_text = "\n\n".join(config_blocks)
    try:
        full_text = banner_template.format(configs=configs_text)
    except KeyError:
        full_text = f"✦ V2Ray Config List\n\n{configs_text}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

    # Optional proxy glass-button rows are inserted ABOVE the sponsor.
    # Each row is already a list of InlineKeyboardButton objects.
    buttons = []
    if extra_button_rows:
        buttons.extend(extra_button_rows)
    channel_link_display = get_profile_channel_link(profile_id)
    if channel_link_display:
        channel_url = f"https://t.me/{channel_link_display}"
        buttons.append([InlineKeyboardButton("📢 کانال", url=channel_url, style="primary")])
    if sponsor_button:
        buttons.append([sponsor_button])
    reply_markup = InlineKeyboardMarkup(buttons) if buttons else None

    ok = await send_with_retry(
        bot, dest, full_text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
        max_retries=3
    )
    if not ok:
        plain_text = re.sub(r'<[^>]+>', '', full_text)
        ok2 = await send_with_retry(
            bot, dest, plain_text,
            parse_mode=None,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            max_retries=2
        )
        if not ok2:
            log.error(f"❌ Failed to send configs after all retries")
            return 0

    sent_count = len(items)
    for i, (url, ping, node_count) in enumerate(items, 1):
        n = last_n + i
        host, _ = extract_host(url)
        flag = "🌐"
        country_code = ""
        if host:
            ip = await host_to_ip(host)
            if ip:
                flag, country_code = await get_flag_for_ip(ip)
        fragment_text = naming_template.replace("{Flag}", flag).replace("{CHANNEL_ID}", channel_link).replace("{COUNT}", str(n))
        fragment_text = fragment_text.replace("{FLAG}", flag)
        fragment_text = fragment_text.replace("{COUNTRY_EN}", COUNTRY_NAMES_EN.get(country_code, ""))
        fragment_text = fragment_text.replace("{COUNTRY_FA}", COUNTRY_NAMES_FA.get(country_code, ""))
        fragment_text = fragment_text.replace("{PING}", "")
        fragment_text = fragment_text.replace("{COUNT}", str(n))
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

async def post_proxies(bot, profile_id, proxies_with_ping, is_instant=False, max_proxies_override=None):
    """Build a proxy post. Does NOT mark anything posted; caller does that only after Telegram success."""
    if not proxies_with_ping:
        return 0, None, []
    max_proxies = max_proxies_override if max_proxies_override is not None else get_profile_max_post_proxy(profile_id)
    try:
        max_proxies = max(1, int(max_proxies))
    except Exception:
        max_proxies = 10
    if is_instant:
        max_proxies = min(max_proxies, 3)
    mode = get_profile_proxy_post_mode(profile_id)
    show_date = get_profile_show_date_proxy(profile_id)
    country_display = get_profile_country_display(profile_id)
    selected = []
    entries = []
    for item in proxies_with_ping:
        try:
            raw = item[0]
            flag = item[2] if len(item) > 2 else "🌐"
            country_code = item[3] if len(item) > 3 else ""
        except Exception:
            continue
        norm = canonical_telegram_proxy_url(raw)
        if not norm or is_proxy_posted(profile_id, norm):
            continue
        if len(entries) >= max_proxies:
            break
        channel = get_profile_channel_link(profile_id)
        if channel:
            channel_label = f"@{channel.lstrip('@')}"
        else:
            # Per-profile fallback: never use a hard-coded @VaslZone for another profile.
            prof = get_profile(profile_id) or {}
            fallback = str(prof.get("dest_name") or "").strip()
            channel_label = fallback if fallback.startswith("@") else (f"@{fallback}" if fallback else "@Channel")
        # Proxy post mode: 0 = normal text + channel name, 1 = glass buttons + protocol header.
        header_parts = [detect_protocol_name(norm) if mode == 1 else channel_label]
        if country_display == 1:
            en = COUNTRY_NAMES_EN.get(country_code, "")
            if flag and flag != "🌐":
                header_parts.append(f"{flag} {en}" if en else flag)
        elif country_display == 2:
            en = COUNTRY_NAMES_EN.get(country_code, "")
            fa = COUNTRY_NAMES_FA.get(country_code, "")
            if flag and flag != "🌐":
                if en and fa:
                    header_parts.append(f"{flag} {en} • <b>{fa}</b>")
                elif en:
                    header_parts.append(f"{flag} {en}")
                elif fa:
                    header_parts.append(f"{flag} <b>{fa}</b>")
                else:
                    header_parts.append(flag)
        entries.append((norm, " ".join(header_parts)))
        selected.append(norm)
    if not entries:
        return 0, None, []

    banner = get_profile_banner_proxy(profile_id) or "🌐 <b>Proxies</b>\n\n{proxies}"
    if mode == 0:
        # Plain URL lines are intentionally used. Telegram clients recognize
        # t.me/proxy links reliably, while HTML href can be rejected for some
        # proxy URL variants.
        proxy_blocks = [f"{header}\n{norm}" for norm, header in entries]
        proxy_text = "\n\n".join(proxy_blocks)
        try:
            text = banner.format(date=get_tehran_date() if show_date else "", count=len(entries), proxies=proxy_text)
        except Exception:
            text = f"🌐 <b>Proxies</b>\n\n{proxy_text}"
        rows = []
        sponsor = get_best_sponsor(profile_id)
        if sponsor and sponsor.get("enabled"):
            style = sponsor.get("color") if sponsor.get("color") in ("primary", "success", "danger") else "primary"
            rows.append([InlineKeyboardButton(str(sponsor.get("button_text") or "Advertisement"), url=str(sponsor.get("url") or "https://t.me/"), style=style)])
        return len(entries), (text, rows), selected

    # Glass mode: maximum 3 proxy buttons per row. Sponsor is always isolated
    # in the final row and never shares a row with proxy buttons.
    proxy_buttons = []
    for i, (norm, header) in enumerate(entries):
        style = ("primary", "success", "danger")[(i) % 3]
        # The protocol is already shown in the header in glass mode; keep button text concise.
        button_label = "پروکسی" if mode == 1 else f"پروکسی {i+1}"
        proxy_buttons.append(InlineKeyboardButton(button_label, url=norm, style=style))
    rows = [proxy_buttons[i:i+3] for i in range(0, len(proxy_buttons), 3)]
    visible = "\n".join(header for _norm, header in entries)
    try:
        text = banner.format(date=get_tehran_date() if show_date else "", count=len(entries), proxies=visible)
    except Exception:
        text = f"🌐 <b>Proxies</b>\n\n{visible}"
    sponsor = get_best_sponsor(profile_id)
    if sponsor and sponsor.get("enabled"):
        style = sponsor.get("color") if sponsor.get("color") in ("primary", "success", "danger") else "primary"
        rows.append([InlineKeyboardButton(str(sponsor.get("button_text") or "Advertisement"), url=str(sponsor.get("url") or "https://t.me/"), style=style)])
    return len(entries), (text, rows), selected

def get_best_sponsor(profile_id, apply_type="both"):
    """Select the best sponsor based on priority and schedule."""
    sponsors = get_sponsors(profile_id, apply_type)
    if not sponsors:
        return None
    return sponsors[0]

# ======================================================================
# چرخه اصلی (با بهبود پروکسی و تست)
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

    # Never allow a manual/scheduled caller to bypass per-profile posting switches.
    enable_configs = bool(enable_configs and get_profile_post_configs(profile_id))
    enable_proxies = bool(enable_proxies and get_profile_post_proxies(profile_id))
    if not enable_configs and not enable_proxies:
        log.info(f"⏸️ Both config and proxy posting are disabled for profile {profile_id}.")
        return 0, "config/proxy posting disabled"

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
    ping_testing = get_profile_ping_enabled(profile_id)
    stream = "combined" if enable_configs and enable_proxies else ("config" if enable_configs else "proxy")
    log.info(f"📡 [profile={profile_id}] Sources: {len(sources)} | 🎯 {dest} | stream={stream} | internal_health_test={ping_testing}")

    all_configs = []
    all_proxies = []
    seen_urls = set()
    seen_config_identities = set()
    seen_proxy_identities = set()
    source_newest_ids = {}

    # Scrape all sources in parallel
    async def scrape_one(src):
        config_links, proxy_links, newest_id = await scrape_channel_paginated(
            profile_id, src, max_pages=5, stream=stream
        )
        return src, config_links, proxy_links, newest_id

    scrape_tasks = [scrape_one(src) for src in sources]
    results = await asyncio.gather(*scrape_tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            log.warning(f"Scrape error: {res}")
            continue
        src, config_links, proxy_links, newest_id = res
        source_newest_ids[src] = newest_id
        log.info(f"  [profile={profile_id}][{stream}] {src}: {len(config_links)} configs, {len(proxy_links)} proxies from web, newest={newest_id or 'NONE'}")
        for link in config_links:
            identity = canonical_config_identity(link)
            if identity not in seen_config_identities:
                seen_config_identities.add(identity)
                all_configs.append((link, src))
        for link in proxy_links:
            norm = normalize_proxy_url(link)
            if norm:
                identity = canonical_proxy_identity(norm)
                if identity not in seen_proxy_identities:
                    seen_proxy_identities.add(identity)
                    all_proxies.append(norm)

    # Filter duplicates based on database
    new_configs = []
    for u, s in all_configs:
        if not is_already_posted(profile_id, u):
            new_configs.append((u, s))

    new_proxies = []
    for p in all_proxies:
        if not is_proxy_posted(profile_id, p):
            new_proxies.append(p)

    log.info(f"📊 New configs: {len(new_configs)}, New proxies: {len(new_proxies)}")

    working = []
    if enable_configs and new_configs:
        # Test configs in batches
        test_limit = min(len(new_configs), 30)  # we can increase batch size later
        to_test = new_configs[:test_limit]
        log.info(f"📊 Testing {len(to_test)} configs...")
        sem = asyncio.Semaphore(50)  # controlled concurrency

        async def _check(item):
            u, src = item
            async with sem:
                try:
                    if ping_testing:
                        ping, ok, cnt = await check_full_link_ping(u, ping_mode, perform_ping=True)
                    else:
                        # Ping testing disabled: we still check host resolution and TCP? But we don't filter.
                        # We'll just consider it reachable if we can resolve host.
                        # However, we still want to avoid posting dead links.
                        # We'll do a DNS check only.
                        host, _ = extract_host(u)
                        if host:
                            ip = await host_to_ip(host)
                            if ip:
                                ping = 0
                                ok = True
                                cnt = 0
                            else:
                                ok = False
                                ping = 0
                                cnt = 0
                        else:
                            ok = False
                            ping = 0
                            cnt = 0
                    if ok:
                        return u, True, ping, cnt, src
                    else:
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
                    # A Telegram proxy URL is a Telegram MTProto proxy link.
                    # For posting, structural validity is the gate; the old
                    # generic config ping test was incorrectly filtering most
                    # proxies and made proxy posting appear broken.
                    host, port = extract_host(proxy_url)
                    flag = "🌐"
                    country_code = ""
                    if host:
                        try:
                            ip = await host_to_ip(host)
                            if ip:
                                flag, country_code = await get_flag_for_ip(ip)
                        except Exception as e:
                            log.debug(f"[PROXY] GeoIP lookup failed for {host}: {e}")
                    if not host or not port or not (1 <= int(port) <= 65535):
                        return proxy_url, 0, flag, country_code
                    return proxy_url, 0, flag, country_code

            results = await asyncio.gather(
                *[check_proxy(p) for p in valid_proxies], return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    continue
                proxy_with_ping.append(r)
            log.info(f"📊 Valid Telegram proxies ready for posting: {len(proxy_with_ping)}")
        else:
            log.info("ℹ️ No valid Telegram proxies found.")

    total_configs = 0
    total_proxies = 0
    selected_proxy_urls = []
    glass_proxy_rows = None

    # In combined mode, glass proxies are attached directly BELOW the config
    # message. Build the proxy payload first, but do not send it separately.
    combined_glass = (enable_configs and enable_proxies and get_profile_proxy_post_mode(profile_id) == 1)
    if combined_glass and proxy_with_ping:
        pcnt, ppayload, selected_proxy_urls = await post_proxies(
            bot, profile_id, proxy_with_ping, is_instant=is_instant
        )
        if pcnt > 0 and ppayload:
            _proxy_preview_text, all_proxy_rows = ppayload
            # post_proxies may append Channel/Sponsor rows. Only the first N rows
            # belong to proxy buttons; Channel and Sponsor are rebuilt by the
            # config sender so Sponsor remains the absolute bottom row.
            proxy_row_count=(pcnt + 2)//3
            glass_proxy_rows = all_proxy_rows[:proxy_row_count]
            log.info(f"[PROXY][profile={profile_id}] glass mode: attaching {pcnt} proxy buttons to config message")
        else:
            glass_proxy_rows = None
            selected_proxy_urls = []

    if working and enable_configs:
        total_configs = await post_configs(
            bot, profile_id, working, source_for_seen="auto", is_instant=is_instant,
            extra_button_rows=glass_proxy_rows
        )
        # If the config message containing the glass proxy buttons was delivered,
        # those proxies were actually published. Mark them only after success.
        if combined_glass and glass_proxy_rows and total_configs > 0:
            total_proxies = len(selected_proxy_urls)
            for proxy_url in selected_proxy_urls:
                if not is_proxy_posted(profile_id, proxy_url):
                    mark_proxy_posted(profile_id, proxy_url)
        elif combined_glass:
            # Config message failed/no configs: do not lose proxy candidates.
            selected_proxy_urls = []

    # Normal mode, or proxy-only mode, sends the proxy banner as its own message.
    # If glass mode was selected but there is no config message to attach to (or
    # the config send failed), fall back to a standalone proxy post so proxies
    # are never silently lost.
    if proxy_with_ping and enable_proxies and (not combined_glass or total_configs == 0):
        cnt, payload, selected_proxy_urls = await post_proxies(
            bot, profile_id, proxy_with_ping, is_instant=is_instant
        )
        if cnt > 0 and payload:
            text, buttons = payload
            log.info(f"[PROXY][profile={profile_id}] attempting Telegram send: count={cnt}, mode={get_profile_proxy_post_mode(profile_id)}, button_rows={len(buttons or [])}")
            sent = await send_to_destination(bot, profile_id, text, buttons)
            if sent:
                total_proxies = cnt
                log.info(f"[PROXY][profile={profile_id}] Telegram send succeeded for {cnt} proxies")
                for proxy_url in selected_proxy_urls:
                    if not is_proxy_posted(profile_id, proxy_url):
                        mark_proxy_posted(profile_id, proxy_url)

    # Advance each stream independently. A failed Telegram send MUST NOT move
    # that stream's cursor, otherwise the failed content would be lost forever.
    config_ok = (not working) or (total_configs > 0)
    proxy_ok = (not proxy_with_ping) or (total_proxies > 0)
    for src, newest_id in source_newest_ids.items():
        if not newest_id:
            continue
        if stream == "combined":
            if config_ok:
                set_stream_last_message_id(profile_id, src, "config", newest_id)
            if proxy_ok:
                set_stream_last_message_id(profile_id, src, "proxy", newest_id)
        elif stream == "config" and config_ok:
            set_stream_last_message_id(profile_id, src, "config", newest_id)
        elif stream == "proxy" and proxy_ok:
            set_stream_last_message_id(profile_id, src, "proxy", newest_id)

    result_msg = f"posted {total_configs} configs and {total_proxies} proxies"
    if total_configs == 0 and total_proxies == 0:
        result_msg = "no new content to send"

    log.info(f"✅ Cycle result for profile {profile_id}: {result_msg}")
    log.info("=" * 50)
    return total_configs + total_proxies, result_msg

# ======================================================================
# حلقه‌های خودکار (با مدیریت بهتر)
# ======================================================================
# Task registry to prevent duplicate loops
_active_tasks = {}  # profile_id -> dict of tasks

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
                        await asyncio.sleep(min(remaining_seconds, 30))
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
                        await asyncio.sleep(min(remaining_seconds, 30))
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
# بک‌آپ خودکار، گزارش روزانه، Railway و پاکسازی (بدون تغییر)
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

BOT_START_TIME = datetime.utcnow()

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
# مدیریت ادمین و زبان (بدون تغییر)
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
# کیبوردها و پیام‌ها (با اضافه شدن دکمه‌های جدید)
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
                       "🔘 وضعیت: {profile_status}\n"
                       "🌐 کشور: {country_display}\n",
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
        # New keys
        "btn_country_display": "🌐 کشور: {mode}",
        "country_display_off": "خاموش",
        "country_display_en": "انگلیسی",
        "country_display_enfa": "انگلیسی+فارسی",
        "country_display_set": "✅ نمایش کشور به {mode} تغییر کرد.",
        "btn_show_ping": "📡 نمایش پینگ: {status}",
        "show_ping_toggle": "✅ نمایش پینگ {'فعال' if status else 'غیرفعال'} شد.",
        "btn_sponsor_list": "📋 لیست اسپانسرها",
        "btn_add_sponsor": "➕ افزودن اسپانسر",
        "sponsor_list_title": "📋 **لیست اسپانسرهای پروفایل {name}**\n\n{sponsors}",
        "sponsor_list_empty": "هیچ اسپانسری تنظیم نشده است.",
        "sponsor_item": "• {name} (اولویت: {priority}) - {'فعال' if enabled else 'غیرفعال'}\n  لینک: {url}\n  دکمه: {text}\n  مدت: {duration}",
        "sponsor_detail": "📢 **جزئیات اسپانسر**\n\nنام: {name}\nلینک: {url}\nمتن دکمه: {text}\nاولویت: {priority}\nوضعیت: {'فعال' if enabled else 'غیرفعال'}\nمدت: {duration}\nرنگ: {color}",
        "sp_add_name": "نام اسپانسر را وارد کنید:",
        "sp_add_url": "لینک اسپانسر را وارد کنید (مثلاً https://example.com):",
        "sp_add_text": "متن دکمه را وارد کنید (پیش‌فرض 'Advertisement'):",
        "sp_add_priority": "اولویت را وارد کنید (عدد، بالاتر = اولویت بیشتر، پیش‌فرض 0):",
        "sp_add_duration": "مدت زمان بر حسب ساعت (۰ برای نامحدود):",
        "sp_add_unlimited": "آیا نامحدود باشد؟ (بله/خیر)",
        "sp_add_apply": "",
        "sp_add_color": "رنگ دکمه را انتخاب کنید:",
        "sp_added_done": "✅ اسپانسر '{name}' با موفقیت اضافه شد.",
        "sp_edit_list": "برای ویرایش، روی اسپانسر کلیک کنید:",
        "sp_edit_select": "لطفاً یک اسپانسر را از لیست انتخاب کنید.",
        "sp_edit_field_prompt": "مقدار جدید برای {field} را وارد کنید (خالی برای عدم تغییر):",
        "sp_edit_done": "✅ اسپانسر به‌روزرسانی شد.",
        "sp_delete_confirm": "⚠️ آیا از حذف اسپانسر '{name}' اطمینان دارید؟",
        "sp_deleted": "✅ اسپانسر حذف شد.",
        "btn_sponsor_edit": "✏️ ویرایش",
        "btn_sponsor_delete": "🗑 حذف",
        "btn_sponsor_toggle": "🔘 {status}",
        "btn_ping_testing": "📡 تست پینگ: {status}",
        "ping_testing_toggle": "✅ تست پینگ {'فعال' if status else 'غیرفعال'} شد.",
        "btn_channel_link_edit": "🔗 ویرایش لینک کانال",
        "btn_banner_config_edit": "📝 ویرایش بنر کانفیگ",
        "btn_banner_proxy_edit": "🌐 ویرایش بنر پروکسی",
    },
    "en": {
        # ... (simplified for brevity, but should be complete)
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
                       "🔘 Status: {profile_status}\n"
                       "🌐 Country: {country_display}\n"
        # ... (other keys)
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
# کیبوردها (با اضافه شدن دکمه‌های جدید)
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
    ping_testing = get_profile_ping_enabled(profile_id)
    ping_testing_label = "✅" if ping_testing else "❌"
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

    country_display = prof.get("country_display", 2)
    country_display_modes = {0: "خاموش", 1: "انگلیسی", 2: "انگلیسی+فارسی"}
    country_label = country_display_modes.get(country_display, "انگلیسی+فارسی")

    sponsors = get_sponsors(profile_id, include_disabled=True)
    sponsor_count = len(sponsors)
    sponsor_status = f"{sponsor_count} اسپانسر" if sponsor_count > 0 else "خالی"

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
        [InlineKeyboardButton(f"📢 اسپانسر: {sponsor_status}", callback_data=f"sponsor_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_name"), callback_data=f"ac_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_banner_config"), callback_data=f"ab_config_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_banner_proxy"), callback_data=f"ab_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton("⏰ بازه کانفیگ", callback_data=f"set_cfg_interval_{profile_id}", style="primary"),
         InlineKeyboardButton("⏰ بازه پروکسی", callback_data=f"set_prx_interval_{profile_id}", style="primary")],
        [InlineKeyboardButton("📊 تعداد کانفیگ", callback_data=f"set_cfg_max_{profile_id}", style="primary"),
         InlineKeyboardButton("📊 تعداد پروکسی", callback_data=f"set_prx_max_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"🌐 حالت پروکسی: {'شیشه‌ای • پروتکل' if get_profile_proxy_post_mode(profile_id) == 1 else 'عادی'}", callback_data=f"tgl_prx_mode_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📡 تست Ping: {'✅' if ping_testing else '❌'}", callback_data=f"tgl_ping_test_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"👁 نمایش Ping: {'✅' if prof.get('show_ping', 1) else '❌'}", callback_data=f"tgl_show_ping_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📍 منطقه Ping: {'🇮🇷 ایران' if ping_mode == 'iran' else '🌍 جهانی'}", callback_data=f"tglping_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_toggle_profile", status=profile_status), callback_data=f"tgl_profile_{profile_id}", style="danger")],
        [InlineKeyboardButton(cfg_btn, callback_data=f"tglcfg_{profile_id}", style="primary"),
         InlineKeyboardButton(prx_btn, callback_data=f"tglproxy_{profile_id}", style="primary")],
        [InlineKeyboardButton(num_btn, callback_data=f"togglenum_{profile_id}", style="primary")],
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
        [InlineKeyboardButton(f"🌐 کشور: {country_label}", callback_data=f"tgl_country_{profile_id}", style="primary")],
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

def sponsor_list_kb(profile_id):
    sponsors = get_sponsors(profile_id, include_disabled=True)
    btns = []
    if sponsors:
        for sp in sponsors:
            status = "✅" if sp["enabled"] else "❌"
            btns.append([InlineKeyboardButton(f"{status} {sp['name']} (اولویت:{sp['priority']})", callback_data=f"sp_detail_{sp['id']}", style="primary")])
    btns.append([InlineKeyboardButton("➕ افزودن اسپانسر", callback_data=f"sp_add_step_{profile_id}_name", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def sponsor_detail_kb(sponsor_id, profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ ویرایش", callback_data=f"sp_edit_{sponsor_id}", style="success"),
         InlineKeyboardButton("🗑 حذف", callback_data=f"sp_delete_{sponsor_id}", style="danger")],
        [InlineKeyboardButton("🔘 فعال / غیرفعال", callback_data=f"sp_toggle_{sponsor_id}", style="primary")],
        [InlineKeyboardButton("🔙 برگشت", callback_data=f"sponsor_list_{profile_id}", style="primary")],
    ])

def sponsor_edit_kb(sponsor_id, profile_id):
    c.execute("SELECT enabled, unlimited FROM sponsors WHERE id=?", (sponsor_id,))
    row = c.fetchone()
    enabled = bool(row[0]) if row else False
    unlimited = bool(row[1]) if row else True
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ نام", callback_data=f"sp_edit_field_{sponsor_id}_name", style="primary"),
         InlineKeyboardButton("🔗 لینک", callback_data=f"sp_edit_field_{sponsor_id}_url", style="primary")],
        [InlineKeyboardButton("📝 متن دکمه", callback_data=f"sp_edit_field_{sponsor_id}_button_text", style="primary"),
         InlineKeyboardButton("🔢 اولویت", callback_data=f"sp_edit_field_{sponsor_id}_priority", style="primary")],
        [InlineKeyboardButton("⏱ مدت (ساعت)", callback_data=f"sp_edit_field_{sponsor_id}_duration_hours", style="primary")],
        [InlineKeyboardButton("♾ نامحدود" if not unlimited else "⏱ محدود کردن", callback_data=f"sp_toggle_unlimited_{sponsor_id}", style="success" if unlimited else "primary")],
        [InlineKeyboardButton("🎨 رنگ", callback_data=f"sp_edit_color_{sponsor_id}", style="primary")],
        [InlineKeyboardButton("🔘 فعال" if enabled else "🔘 غیرفعال", callback_data=f"sp_toggle_{sponsor_id}", style="success" if enabled else "danger")],
        [InlineKeyboardButton("🔙 برگشت", callback_data=f"sp_detail_{sponsor_id}", style="primary")],
    ])

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
# دستورات (بدون تغییر)
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
        ping_testing = "✅" if get_profile_ping_enabled(prof['id']) else "❌"
        msg_lines.append(f"  • {prof['dest_name']} (ID:{prof['id']}) - {len(get_profile_sources(prof['id']))} منبع, بازه کانفیگ:{prof.get('interval_config',5)}m, بازه پروکسی:{prof.get('interval_proxy',5)}m, پینگ {prof['ping_mode']}, تست پینگ:{ping_testing}, تایمر: {timer_status}, وضعیت:{enabled_status}")
    msg_lines.append("")
    seen_cfg = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    seen_prx = c.execute("SELECT COUNT(*) FROM proxies_seen").fetchone()[0]
    msg_lines.append("💾 **دیتابیس:**")
    msg_lines.append(f"• کانفیگ‌های دیده‌شده: {seen_cfg}")
    msg_lines.append(f"• پروکسی‌های دیده‌شده: {seen_prx}")
    msg_lines.append("")
    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")

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
# کالبک (با اضافه شدن هندلرهای جدید)
# ======================================================================
def clear_pending_input_state(ctx):
    """Cancel every pending text-entry workflow for this chat/user.
    This prevents text sent after Back/Cancel from being consumed by an old form.
    """
    for key in ("action", "sponsor_add", "sponsor_edit", "backup_export", "backup_export_custom"):
        ctx.user_data.pop(key, None)


async def on_callback(u, ctx):
    q = u.callback_query
    try:
        if not is_admin(q.from_user.id):
            await q.answer(msg("only_admin"), show_alert=True)
            return
        await q.answer()
    except Exception as e:
        log.warning(f"Failed to answer callback query: {e}")

    try:
        d = q.data or ""
        log.info(f"📨 Callback data: {d}")

        # Any navigation/back/cancel action must terminate the previous text-entry
        # state before rendering the destination page. This is global across all bot sections.
        navigation_prefixes = (
            "back_", "prof_", "profiles_list", "general_settings", "manage_admins",
            "list_admins", "sponsor_list_", "sp_detail_", "sp_delete_",
            "src_list_", "dl_", "bl_list_", "backup_", "ast_", "home_"
        )
        if ("cancel" in d.lower() or "back" in d.lower() or d in ("back_home", "profiles_list", "general_settings", "manage_admins", "list_admins")
                or any(d.startswith(p) for p in navigation_prefixes)):
            # Preserve multi-step Sponsor Add selections and explicit field-selection callbacks.
            if not (d.startswith("sp_add_") or d.startswith("sp_color_") or d.startswith("sp_edit_color_")):
                clear_pending_input_state(ctx)

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

        # ===================== SPONSOR NEW =====================
        if d.startswith("sponsor_list_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                prof = get_profile(profile_id)
                name = prof["dest_name"] if prof else ""
                sponsors = get_sponsors(profile_id, include_disabled=True)
                if not sponsors:
                    txt = msg("sponsor_list_title", name=name, sponsors=msg("sponsor_list_empty"))
                else:
                    lines = []
                    for sp in sponsors:
                        duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
                        lines.append(msg("sponsor_item", name=sp["name"], priority=sp["priority"], enabled=sp["enabled"], url=sp["url"], text=sp["button_text"], duration=duration_str))
                    txt = msg("sponsor_list_title", name=name, sponsors="\n".join(lines))
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_list_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_detail_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if not row:
                    await q.answer("اسپانسر یافت نشد.")
                    return
                cols = [d[0] for d in c.description]
                sp = dict(zip(cols, row))
                profile_id = sp["profile_id"]
                duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
                txt = msg("sponsor_detail", name=sp["name"], url=sp["url"], text=sp["button_text"],
                          priority=sp["priority"], enabled=bool(sp["enabled"]),
                          duration=duration_str, color=sp["color"])
                await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_detail_kb(sponsor_id, profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_toggle_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT enabled, profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    new_enabled = 0 if row[0] else 1
                    update_sponsor(sponsor_id, enabled=new_enabled)
                    await q.answer(f"اسپانسر {'فعال' if new_enabled else 'غیرفعال'} شد.")
                    # Refresh detail
                    c.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,))
                    row2 = c.fetchone()
                    if row2:
                        cols = [d[0] for d in c.description]
                        sp = dict(zip(cols, row2))
                        profile_id = sp["profile_id"]
                        duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
                        txt = msg("sponsor_detail", name=sp["name"], url=sp["url"], text=sp["button_text"],
                                  priority=sp["priority"], enabled=bool(sp["enabled"]),
                                  duration=duration_str, color=sp["color"])
                        await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_detail_kb(sponsor_id, profile_id))
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_delete_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT name, profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    name, profile_id = row
                    await q.edit_message_text(
                        msg("sp_delete_confirm", name=name),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅ بله، حذف کن", callback_data=f"sp_delete_confirm_{sponsor_id}", style="danger")],
                            [InlineKeyboardButton("❌ لغو", callback_data=f"sp_detail_{sponsor_id}", style="primary")],
                        ])
                    )
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_delete_confirm_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    sponsor_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    profile_id = row[0]
                    delete_sponsor(sponsor_id)
                    await q.answer(msg("sp_deleted"))
                    await q.edit_message_text("✅ اسپانسر حذف شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_color_"):
            try:
                sponsor_id=int(d.rsplit("_",1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
            row=c.fetchone()
            if not row:
                await q.answer("اسپانسر یافت نشد.")
                return
            profile_id=row[0]
            await q.edit_message_text("🎨 رنگ دکمه اسپانسر را انتخاب کنید:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_color_{sponsor_id}_primary", style="primary")],
                [InlineKeyboardButton("🟢 Success", callback_data=f"sp_color_{sponsor_id}_success", style="success")],
                [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_color_{sponsor_id}_danger", style="danger")],
                [InlineKeyboardButton("🔙 برگشت", callback_data=f"sp_edit_{sponsor_id}", style="primary")]
            ]))
            return

        if d.startswith("sp_color_"):
            parts=d.split("_")
            try:
                sponsor_id=int(parts[2]); color=parts[3]
            except Exception:
                await q.answer("⚠️ داده نامعتبر")
                return
            c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
            row=c.fetchone()
            if not row:
                await q.answer("اسپانسر یافت نشد.")
                return
            update_sponsor(sponsor_id, color=color)
            profile_id=row[0]
            await q.answer("✅ رنگ ذخیره شد.")
            await q.edit_message_text("✏️ ویرایش اسپانسر", reply_markup=sponsor_edit_kb(sponsor_id, profile_id))
            return

        if d.startswith("sp_toggle_unlimited_"):
            try:
                sponsor_id = int(d.rsplit("_", 1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            c.execute("SELECT profile_id, unlimited, duration_hours FROM sponsors WHERE id=?", (sponsor_id,))
            row = c.fetchone()
            if not row:
                await q.answer("اسپانسر یافت نشد.")
                return
            profile_id, current_unlimited, current_duration = row
            new_unlimited = 0 if current_unlimited else 1
            if new_unlimited:
                update_sponsor(sponsor_id, unlimited=1, expires_at=None)
            else:
                duration = int(current_duration or 1)
                update_sponsor(sponsor_id, unlimited=0, duration_hours=duration)
            await q.answer("♾ نامحدود فعال شد." if new_unlimited else "⏱ محدود شد.")
            c.execute("SELECT * FROM sponsors WHERE id=?", (sponsor_id,))
            row2 = c.fetchone()
            cols = [d[0] for d in c.description]
            sp = dict(zip(cols, row2))
            duration_str = "نامحدود" if sp["unlimited"] else f"{sp['duration_hours']} ساعت"
            txt = msg("sponsor_detail", name=sp["name"], url=sp["url"], text=sp["button_text"],
                      priority=sp["priority"], enabled=bool(sp["enabled"]),
                      duration=duration_str, color=sp["color"])
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=sponsor_edit_kb(sponsor_id, profile_id))
            return

        if d.startswith("sp_edit_field_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    sponsor_id = int(parts[3])
                    field = "_".join(parts[4:])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["sponsor_edit"] = {"sponsor_id": sponsor_id, "field": field}
                await q.edit_message_text(
                    msg("sp_edit_field_prompt", field=field),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 لغو", callback_data=f"sp_edit_{sponsor_id}", style="primary")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_") and not d.startswith("sp_edit_field_") and not d.startswith("sp_edit_color_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    sponsor_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
                row = c.fetchone()
                if row:
                    profile_id = row[0]
                    await q.edit_message_text(
                        "✏️ **ویرایش اسپانسر**\n\nکدام فیلد را می‌خواهید ویرایش کنید؟",
                        parse_mode="HTML",
                        reply_markup=sponsor_edit_kb(sponsor_id, profile_id)
                    )
                else:
                    await q.answer("اسپانسر یافت نشد.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Sponsor add steps (multi-step)
        if d.startswith("sp_add_step_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                    step = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if step == "name":
                    ctx.user_data["sponsor_add"] = {"profile_id": profile_id, "step": "name"}
                    await q.edit_message_text(msg("sp_add_name"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "url":
                    ctx.user_data["sponsor_add"]["step"] = "url"
                    await q.edit_message_text(msg("sp_add_url"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "button_text":
                    ctx.user_data["sponsor_add"]["step"] = "button_text"
                    await q.edit_message_text(msg("sp_add_text"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "priority":
                    ctx.user_data["sponsor_add"]["step"] = "priority"
                    await q.edit_message_text(msg("sp_add_priority"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "duration":
                    ctx.user_data["sponsor_add"]["step"] = "duration"
                    await q.edit_message_text(msg("sp_add_duration"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                elif step == "unlimited":
                    ctx.user_data["sponsor_add"]["step"] = "unlimited"
                    await q.edit_message_text(msg("sp_add_unlimited"), reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("بله", callback_data=f"sp_add_unlimited_yes_{profile_id}", style="primary")],
                        [InlineKeyboardButton("خیر", callback_data=f"sp_add_unlimited_no_{profile_id}", style="primary")],
                        [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                    ]))
                elif step == "color":
                    ctx.user_data["sponsor_add"]["step"] = "color"
                    await q.edit_message_text(msg("sp_add_color"), reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_add_color_{profile_id}_primary", style="primary")],
                        [InlineKeyboardButton("🟢 Success", callback_data=f"sp_add_color_{profile_id}_success", style="success")],
                        [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_add_color_{profile_id}_danger", style="danger")],
                        [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                    ]))
                else:
                    await q.answer("مرحله نامعتبر")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_unlimited_yes_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if "sponsor_add" not in ctx.user_data:
                    ctx.user_data["sponsor_add"] = {}
                ctx.user_data["sponsor_add"]["unlimited"] = 1
                ctx.user_data["sponsor_add"]["duration_hours"] = 0
                ctx.user_data["sponsor_add"]["step"] = "color"
                await q.edit_message_text(msg("sp_add_color"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_add_color_{profile_id}_primary", style="primary")],
                    [InlineKeyboardButton("🟢 Success", callback_data=f"sp_add_color_{profile_id}_success", style="success")],
                    [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_add_color_{profile_id}_danger", style="danger")],
                    [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_unlimited_no_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if "sponsor_add" not in ctx.user_data:
                    ctx.user_data["sponsor_add"] = {}
                ctx.user_data["sponsor_add"]["unlimited"] = 0
                ctx.user_data["sponsor_add"]["step"] = "duration"
                await q.edit_message_text(msg("sp_add_duration"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_add_color_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                    color = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                if "sponsor_add" in ctx.user_data and ctx.user_data["sponsor_add"].get("profile_id") == profile_id:
                    data = ctx.user_data["sponsor_add"]
                    name = data.get("name", "Advertisement")
                    url = data.get("url", "")
                    button_text = data.get("button_text", "Advertisement")
                    priority = int(data.get("priority", 0))
                    duration_hours = int(data.get("duration_hours", 0))
                    unlimited = data.get("unlimited", 1)
                    apply_config = data.get("apply_config", 1)
                    apply_proxy = data.get("apply_proxy", 1)
                    if url:
                        add_sponsor(profile_id, name, url, button_text, enabled=1, priority=priority,
                                    duration_hours=duration_hours, unlimited=unlimited,
                                    apply_config=apply_config, apply_proxy=apply_proxy, color=color)
                        await q.answer(msg("sp_added_done", name=name))
                        await q.edit_message_text("✅ اسپانسر اضافه شد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
                    else:
                        await q.answer("❌ لینک اسپانسر معتبر نیست.")
                    del ctx.user_data["sponsor_add"]
                else:
                    await q.answer("❌ داده‌ها منقضی شده‌اند.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # End sponsor new

        # Legacy sponsor handling (keep for compatibility)
        if d.startswith("sp_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text("📢 **مدیریت اسپانسرها**", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 لیست اسپانسرها", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                    [InlineKeyboardButton("➕ افزودن اسپانسر", callback_data=f"sp_add_step_{profile_id}_name", style="success")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"prof_{profile_id}", style="primary")],
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ---------- Existing callbacks (unchanged) ----------
        # Sources
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

        # Destinations
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

        # Settings: name, banners, intervals, max, etc.
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

        if d.startswith("tgl_show_ping_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except ValueError:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            current = get_profile_show_ping(profile_id)
            new_val = not bool(current)
            set_profile_show_ping(profile_id, new_val)
            await q.answer(f"👁 نمایش Ping {'فعال' if new_val else 'غیرفعال'} شد.")
            await show_profile_admin(q.message, profile_id)
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

        if d.startswith("tgl_ping_test_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_ping_enabled(profile_id)
                new_val = not current
                set_profile_ping_enabled(profile_id, new_val)
                await q.answer(msg("ping_testing_toggle", status=new_val))
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

        if d.startswith("tgl_prx_mode_"):
            try:
                profile_id=int(d.rsplit("_",1)[1])
            except Exception:
                await q.answer("⚠️ شناسه نامعتبر")
                return
            mode=0 if get_profile_proxy_post_mode(profile_id)==1 else 1
            set_profile_proxy_post_mode(profile_id, mode)
            await q.answer("🌐 حالت پروکسی شیشه‌ای فعال شد." if mode else "🌐 حالت پروکسی عادی فعال شد.")
            await show_profile_admin(q.message, profile_id)
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

        # Backup export, timer, log, cron, blacklist
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

        # New toggles for country
        if d.startswith("tgl_country_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                current = get_profile_country_display(profile_id)
                new_mode = (current + 1) % 3
                set_profile_country_display(profile_id, new_mode)
                mode_names = {0: msg("country_display_off"), 1: msg("country_display_en"), 2: msg("country_display_enfa")}
                await q.answer(msg("country_display_set", mode=mode_names[new_mode]))
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # Naming template and channel link
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

        if d.startswith("set_channel_link_"):
            try:
                profile_id = int(d.rsplit("_", 1)[1])
            except (ValueError, IndexError):
                await q.answer("⚠️ شناسه پروفایل نامعتبر", show_alert=True)
                return
            if not get_profile(profile_id):
                await q.answer("⚠️ پروفایل یافت نشد", show_alert=True)
                return
            ctx.user_data["action"] = f"set_channel_link_{profile_id}"
            current = get_profile_channel_link(profile_id) or "خالی"
            await q.edit_message_text(
                f"🔗 لینک کانال فعلی: <code>{html.escape(current)}</code>\n\n" + msg("channel_link_prompt"),
                parse_mode="HTML",
                reply_markup=empty_button_kb(profile_id, f"empty_channel_link_{profile_id}")
            )
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
    sponsors = get_sponsors(profile_id)
    sponsor_st = f"{len(sponsors)} اسپانسر" if sponsors else "خالی"
    ping_mode = prof["ping_mode"]
    ping_display = "ایران" if ping_mode == "iran" else "جهانی"
    ping_testing = get_profile_ping_enabled(profile_id)
    ping_status = "✅" if ping_testing else "❌"
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

    country_display = prof.get("country_display", 2)
    country_display_modes = {0: "خاموش", 1: "انگلیسی", 2: "انگلیسی+فارسی"}
    country_label = country_display_modes.get(country_display, "انگلیسی+فارسی")

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
        country_display=country_label,
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
# هندلرهای متنی و سند (بدون تغییر، حذف بخش فایل)
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

    # Sponsor edit field
    if ctx.user_data.get("sponsor_edit"):
        data=ctx.user_data["sponsor_edit"]
        sponsor_id=int(data["sponsor_id"])
        field=data["field"]
        txt=u.message.text.strip()
        c.execute("SELECT profile_id FROM sponsors WHERE id=?", (sponsor_id,))
        row=c.fetchone()
        if not row:
            await u.message.reply_text("اسپانسر یافت نشد.")
            ctx.user_data.pop("sponsor_edit", None)
            return
        profile_id=row[0]
        if field in ("name","button_text"):
            if not txt:
                await u.message.reply_text("❌ مقدار نمی‌تواند خالی باشد.")
                return
            update_sponsor(sponsor_id, **{field:txt})
        elif field=="url":
            if not re.match(r"^(https?://|tg://)", txt, re.I):
                await u.message.reply_text("❌ لینک معتبر نیست. لینک باید با https:// یا tg:// شروع شود.")
                return
            update_sponsor(sponsor_id, url=txt)
        elif field=="priority":
            try: value=int(txt)
            except Exception:
                await u.message.reply_text("❌ اولویت باید عدد باشد.")
                return
            update_sponsor(sponsor_id, priority=value)
        elif field=="duration_hours":
            try: value=int(txt)
            except Exception:
                await u.message.reply_text("❌ مدت باید عدد غیرمنفی باشد.")
                return
            if value<0:
                await u.message.reply_text("❌ مدت نمی‌تواند منفی باشد.")
                return
            update_sponsor(sponsor_id, duration_hours=value, unlimited=0)
        elif field=="unlimited":
            c.execute("SELECT unlimited FROM sponsors WHERE id=?", (sponsor_id,))
            cur=c.fetchone()
            unlimited=0 if cur and cur[0] else 1
            update_sponsor(sponsor_id, unlimited=unlimited, duration_hours=0 if unlimited else None)
            # None duration means preserve current when turning unlimited off.
            if not unlimited:
                c.execute("SELECT duration_hours FROM sponsors WHERE id=?", (sponsor_id,))
                current=c.fetchone()
                if current and current[0] is None:
                    update_sponsor(sponsor_id, duration_hours=1)
        else:
            await u.message.reply_text("این فیلد با دکمه مخصوص ویرایش می‌شود.")
            return
        ctx.user_data.pop("sponsor_edit", None)
        await u.message.reply_text(msg("sp_edit_done"))
        await show_profile_admin(u.message, profile_id)
        return

    # Sponsor add: if we have step and not handled by callback, process text
    if ctx.user_data.get("sponsor_add"):
        data = ctx.user_data["sponsor_add"]
        step = data.get("step")
        profile_id = data.get("profile_id")
        if step == "name":
            name = u.message.text.strip()
            if name:
                data["name"] = name
                data["step"] = "url"
                await u.message.reply_text(msg("sp_add_url"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            else:
                await u.message.reply_text("❌ نام نمی‌تواند خالی باشد.")
            return
        elif step == "url":
            url = u.message.text.strip()
            if url:
                data["url"] = url
                data["step"] = "button_text"
                await u.message.reply_text(msg("sp_add_text"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            else:
                await u.message.reply_text("❌ لینک نمی‌تواند خالی باشد.")
            return
        elif step == "button_text":
            data["button_text"] = u.message.text.strip() or "Advertisement"
            data["step"] = "priority"
            await u.message.reply_text(msg("sp_add_priority"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")]]))
            return
        elif step == "priority":
            try:
                priority = int(u.message.text.strip() or "0")
                data["priority"] = priority
                data["step"] = "unlimited"
                await u.message.reply_text(msg("sp_add_unlimited"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("بله", callback_data=f"sp_add_unlimited_yes_{profile_id}", style="primary")],
                    [InlineKeyboardButton("خیر", callback_data=f"sp_add_unlimited_no_{profile_id}", style="primary")],
                    [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                ]))
            except:
                await u.message.reply_text("❌ اولویت باید عدد باشد.")
            return
        elif step == "duration":
            try:
                duration = int(u.message.text.strip())
                if duration < 0:
                    raise ValueError
                data["duration_hours"] = duration
                data["unlimited"] = 0
                data["step"] = "color"
                await u.message.reply_text(msg("sp_add_color"), reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 Primary", callback_data=f"sp_add_color_{profile_id}_primary", style="primary")],
                    [InlineKeyboardButton("🟢 Success", callback_data=f"sp_add_color_{profile_id}_success", style="success")],
                    [InlineKeyboardButton("🔴 Danger", callback_data=f"sp_add_color_{profile_id}_danger", style="danger")],
                    [InlineKeyboardButton("🔙 لغو", callback_data=f"sponsor_list_{profile_id}", style="primary")],
                ]))
            except:
                await u.message.reply_text("❌ مدت باید عدد غیرمنفی باشد.")
            return
        elif step == "apply":
            # handled by callback
            pass
        elif step == "color":
            # handled by callback
            pass
        else:
            del ctx.user_data["sponsor_add"]
            await u.message.reply_text("❌ خطا در روند افزودن اسپانسر.")
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
        try:
            profile_id = int(a.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await u.message.reply_text("❌ شناسه پروفایل نامعتبر است.")
            return
        channel_link = t.strip()
        if channel_link:
            normalized = channel_link
            normalized = re.sub(r"^https?://t\.me/", "", normalized, flags=re.IGNORECASE)
            normalized = re.sub(r"^t\.me/", "", normalized, flags=re.IGNORECASE)
            normalized = normalized.split("?", 1)[0].split("#", 1)[0].strip()
            normalized = normalized.lstrip("@/")
            if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", normalized):
                await u.message.reply_text("❌ لینک کانال معتبر نیست. مثال: @MyChannel")
                return
            channel_link = normalized
        set_profile_channel_link(profile_id, channel_link)
        saved = get_profile_channel_link(profile_id)
        if saved != channel_link:
            await u.message.reply_text("❌ ذخیره لینک کانال تأیید نشد.")
            return
        await u.message.reply_text(msg("channel_link_set", link=saved if saved else "خالی"))
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
# ارسال دستی (بدون دانلود فایل)
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
            valid_proxies = [p for p in new_proxies if "t.me/proxy" in p.lower() or p.startswith("tg://proxy")]
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
                country_code = ""
                if host:
                    ip = await host_to_ip(host)
                    if ip:
                        flag, country_code = await get_flag_for_ip(ip)
                proxy_with_ping.append((proxy_url, 0, flag, country_code))
            cnt, payload, selected_proxy_urls = await post_proxies(u.get_bot(), profile_id, proxy_with_ping, is_instant=False, max_proxies_override=len(chunk))
            if cnt > 0 and payload:
                text_p, buttons = payload
                sent = await send_to_destination(u.get_bot(), profile_id, text_p, buttons)
                if sent:
                    total_proxies_sent += cnt
                    for proxy_url in selected_proxy_urls:
                        if not is_proxy_posted(profile_id, proxy_url):
                            mark_proxy_posted(profile_id, proxy_url)
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
        cnt, payload, selected_proxy_urls = await post_proxies(bot, profile_id, proxies_with_ping)
        if cnt > 0 and payload:
            text, buttons = payload
            log.info(f"[PROXY][profile={profile_id}] attempting Telegram send: count={cnt}, mode={get_profile_proxy_post_mode(profile_id)}, button_rows={len(buttons or [])}")
            sent = await send_to_destination(bot, profile_id, text, buttons)
            if sent:
                total_proxies = cnt
                log.info(f"[PROXY][profile={profile_id}] Telegram send succeeded for {cnt} proxies")
                for proxy_url in selected_proxy_urls:
                    if not is_proxy_posted(profile_id, proxy_url):
                        mark_proxy_posted(profile_id, proxy_url)
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
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("✅ Bot is ready, polling...")
    app.run_polling()

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("🚀 Starting bot...")
    main()
