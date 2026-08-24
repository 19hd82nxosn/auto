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
from telegram.error import BadRequest

# ======================================================================
# متغیرهای محیطی
# ======================================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
if not ADMIN_ID:
    raise ValueError("ADMIN_ID environment variable not set")

# ======================================================================
# مسیر پایدار (ولوم ثابت /app/data)
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

# ======================================================================
# توابع کمکی برای اطمینان از وجود ستون
# ======================================================================
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
    UNIQUE(uuid, address))""")

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
    source TEXT PRIMARY KEY,
    last_scrape_time TEXT)""")
ensure_column("last_scrape", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
    source TEXT,
    message_id INTEGER,
    PRIMARY KEY(source, message_id))""")
ensure_column("processed_messages", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS proxies_seen (
    proxy_url TEXT PRIMARY KEY,
    first_seen TEXT,
    last_posted TEXT)""")
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
    last_backup_count INTEGER DEFAULT 0)""")
conn.commit()

ensure_column("profiles", "show_numbers", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "custom_query", "TEXT DEFAULT ''", "")
ensure_column("profiles", "show_date_config", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "show_date_proxy", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "schedule_cron", "TEXT DEFAULT ''", "")
ensure_column("profiles", "last_backup_count", "INTEGER DEFAULT 0", 0)

c.execute("""CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    word TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(profile_id, word))""")
conn.commit()

# ======================================================================
# مهاجرت از تنظیمات قدیمی و تعمیر ستون‌ها
# ======================================================================
def fix_column_types():
    """بررسی و اصلاح نوع ستون custom_query در جدول profiles"""
    c.execute("PRAGMA table_info(profiles)")
    cols = c.fetchall()
    for col in cols:
        if col[1] == "custom_query" and "TEXT" not in col[2].upper():
            log.warning("custom_query column is not TEXT, fixing...")
            # ساخت جدول جدید با نوع صحیح
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
                    last_backup_count INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                INSERT INTO profiles_new
                    (id, dest_name, sources, banner_config, banner_proxy, interval_min,
                     max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num,
                     created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                     schedule_cron, last_backup_count)
                SELECT id, dest_name, sources, banner_config, banner_proxy, interval_min,
                       max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num,
                       created_at, show_numbers, custom_query, show_date_config, show_date_proxy,
                       schedule_cron, last_backup_count
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
             show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             datetime.now().isoformat(), 1, "", 1, 1, "", 0))
    conn.commit()
    log.info(f"✅ Migrated {len(dest_list)} profiles.")

migrate_old_config()

# ======================================================================
# توابع پروفایل (با استفاده از نام ستون‌ها به جای ایندکس)
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
                   show_date_config=1, show_date_proxy=1, schedule_cron=""):
    if not banner_config:
        banner_config = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    if not banner_proxy:
        banner_proxy = "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━"
    c.execute("""INSERT INTO profiles
        (dest_name, sources, banner_config, banner_proxy, interval_min,
         max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at,
         show_numbers, custom_query, show_date_config, show_date_proxy, schedule_cron, last_backup_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, last_num,
         get_tehran_time(), show_numbers, custom_query,
         show_date_config, show_date_proxy, schedule_cron, 0))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "last_num",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy",
               "schedule_cron", "last_backup_count"]
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

def get_profile_sources(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return []
    s = prof["sources"]
    items = [x.strip() for x in s.split(",") if x.strip()]
    return items

def set_profile_sources(profile_id, sources_list):
    s = ",".join(sources_list)
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

def get_profile_interval(profile_id):
    prof = get_profile(profile_id)
    return prof["interval_min"] if prof else 5

def get_profile_max_post(profile_id):
    prof = get_profile(profile_id)
    return prof["max_post"] if prof else 8

def get_profile_max_proxies(profile_id):
    prof = get_profile(profile_id)
    return prof["max_proxies"] if prof else 10

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
    # مستقیم و با commit
    c.execute("UPDATE profiles SET custom_query=? WHERE id=?", (query, profile_id))
    conn.commit()
    saved = get_profile_custom_query(profile_id)
    if saved != query:
        log.error(f"custom_query not saved! expected '{query}', got '{saved}'")
        # تلاش مجدد
        c.execute("UPDATE profiles SET custom_query=? WHERE id=?", (query, profile_id))
        conn.commit()
        saved = get_profile_custom_query(profile_id)
        log.info(f"After retry: '{saved}'")
        if saved != query:
            raise ValueError("Unable to save custom_query")

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

def set_sponsor(profile_id, name, url, button_text="Advertisement", color="primary"):
    now = get_tehran_time()
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    c.execute(
        "INSERT INTO sponsors (profile_id, name, url, button_text, enabled, color, created_at) VALUES (?,?,?,?,1,?,?)",
        (profile_id, name, url, button_text, color, now)
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
# توابع کمکی
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
    pattern = re.compile(r'(vless|vmess|trojan|hy2|tuic|ss|socks)://[^\s<>"\'{}()\[\]]+', re.IGNORECASE)
    for m in pattern.finditer(text):
        link = m.group(0).strip()
        link = re.sub(r'[.,;:!؟\'"`]+$', '', link)
        if len(link) > 10:
            results.append(link)

    if not results:
        for token in text.split():
            token = token.strip()
            for proto in ['vless://', 'vmess://', 'trojan://', 'hy2://', 'tuic://', 'ss://', 'socks://']:
                if token.lower().startswith(proto):
                    clean = re.sub(r'[.,;:!؟\'"`]+$', '', token)
                    if len(clean) > len(proto) + 5:
                        results.append(clean)

    if not results:
        text_clean = text.replace('\n', '').replace('\r', '').strip()
        if re.match(r'^[A-Za-z0-9+/=]+$', text_clean):
            try:
                decoded = base64.b64decode(text_clean, validate=True).decode('utf-8', errors='ignore')
                for proto in ["vless://", "vmess://", "trojan://", "hy2://", "tuic://", "ss://", "socks://"]:
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
                    for proto in ["vless://", "vmess://", "trojan://", "hy2://", "tuic://", "ss://", "socks://"]:
                        for m in re.finditer(re.escape(proto) + r"[^\s<>\"']+", decoded):
                            link = m.group().rstrip().strip(".,;(){}[]!؟'")
                            if len(link) > len(proto) + 10:
                                results.append(link)
                except:
                    pass

    if not results:
        for line in text.splitlines():
            line = line.strip()
            for proto in ['vless://', 'vmess://', 'trojan://', 'hy2://', 'tuic://', 'ss://', 'socks://']:
                if line.lower().startswith(proto):
                    results.append(line)
                    break

    return list(set(results))

def normalize_proxy_url(url):
    if not url:
        return None
    url = clean_proxy_link(url.strip())
    if "t.me/proxy" in url.lower():
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
        return False
    return c.execute(
        "SELECT 1 FROM seen WHERE uuid=? AND address=? AND profile_id=?",
        (uid, host, profile_id)).fetchone() is not None

def mark_as_posted(profile_id, url, source="", full_url=None):
    url = clean_config_url(url)
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        return
    if full_url is None:
        full_url = url
    now = get_tehran_time()

    # بررسی وجود قبلی
    existing = c.execute(
        "SELECT backup_num FROM seen WHERE uuid=? AND address=? AND profile_id=?",
        (uid, host, profile_id)).fetchone()
    if existing:
        # به‌روزرسانی، بدون تغییر backup_num
        c.execute(
            "UPDATE seen SET last_posted=?, full_url=? WHERE uuid=? AND address=? AND profile_id=?",
            (now, full_url, uid, host, profile_id))
    else:
        # جدید: اختصاص شماره ترتیبی بر اساس بیشینه backup_num
        max_num = c.execute(
            "SELECT COALESCE(MAX(backup_num), 0) FROM seen WHERE profile_id=?",
            (profile_id,)).fetchone()[0]
        backup_num = max_num + 1
        c.execute(
            "INSERT INTO seen (uuid,address,source,first_seen,last_posted,profile_id,full_url,backup_num) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, host, source, now, now, profile_id, full_url, backup_num))
    conn.commit()
    # بررسی بک‌آپ خودکار
    asyncio.create_task(check_and_auto_backup(profile_id))

async def check_and_auto_backup(profile_id):
    """ارسال خودکار بک‌آپ برای هر ۱۰۰۰ کانفیگ جدید"""
    try:
        # تعداد کل کانفیگ‌های ثبت‌شده (با backup_num معتبر)
        total = c.execute(
            "SELECT COUNT(*) FROM seen WHERE profile_id=? AND full_url != '' AND backup_num > 0",
            (profile_id,)).fetchone()[0]
        last_backup = get_profile_last_backup_count(profile_id)

        # تعداد بلاک‌های ۱۰۰۰تایی که باید ارسال شوند
        needed_blocks = total // 1000 - last_backup // 1000
        if needed_blocks <= 0:
            return

        # ارسال هر بلاک
        for block in range(1, needed_blocks + 1):
            start_num = (last_backup // 1000 + block - 1) * 1000 + 1
            end_num = (last_backup // 1000 + block) * 1000

            rows = c.execute(
                "SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' AND backup_num BETWEEN ? AND ? ORDER BY backup_num",
                (profile_id, start_num, end_num)
            ).fetchall()
            links = [r[0] for r in rows if r[0]]
            if not links:
                continue

            filename = f"configs_backup_{get_tehran_date()}_profile_{profile_id}_{start_num}_{end_num}.txt"
            content = "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)

            # ارسال به ادمین
            bot = BOT_REF
            if bot:
                with open(os.path.join(DATA_DIR, filename), "rb") as f:
                    await bot.send_document(
                        ADMIN_ID,
                        document=f,
                        filename=filename,
                        caption=f"📤 بک‌آپ خودکار {start_num} تا {end_num} (تعداد: {len(links)})"
                    )
                os.remove(os.path.join(DATA_DIR, filename))
            else:
                log.warning("BOT_REF is None, cannot send backup.")

        # به‌روزرسانی last_backup_count بر اساس total فعلی
        set_profile_last_backup_count(profile_id, total)
    except Exception as e:
        log.error(f"Auto backup check error: {e}")

def is_message_processed(profile_id, source, message_id):
    r = c.execute("SELECT 1 FROM processed_messages WHERE source=? AND message_id=? AND profile_id=?", (source, message_id, profile_id)).fetchone()
    return r is not None

def mark_message_processed(profile_id, source, message_id):
    c.execute("INSERT OR REPLACE INTO processed_messages (source, message_id, profile_id) VALUES (?,?,?)",
              (source, message_id, profile_id))
    conn.commit()

def is_proxy_posted(profile_id, proxy_url):
    r = c.execute("SELECT 1 FROM proxies_seen WHERE proxy_url=? AND profile_id=?", (proxy_url, profile_id)).fetchone()
    return r is not None

def mark_proxy_posted(profile_id, proxy_url):
    now = get_tehran_time()
    c.execute("INSERT OR REPLACE INTO proxies_seen (proxy_url, first_seen, last_posted, profile_id) VALUES (?,?,?,?)",
              (proxy_url, now, now, profile_id))
    conn.commit()

def get_last_scrape_time(profile_id, source):
    r = c.execute("SELECT last_scrape_time FROM last_scrape WHERE source=? AND profile_id=?", (source, profile_id)).fetchone()
    return r[0] if r else None

def update_last_scrape_time(profile_id, source, time_str):
    c.execute("INSERT OR REPLACE INTO last_scrape (source, last_scrape_time, profile_id) VALUES (?,?,?)",
              (source, time_str, profile_id))
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

def append_channel_and_flag_encoded(url, channel, flag, custom_query=""):
    url = clean_config_url(url)
    protocol = url.split('://')[0].lower() if '://' in url else ''
    if custom_query and protocol != 'vmess':
        url = add_custom_query_to_url(url, custom_query, protocol)
    base = strip_url_fragment(url)
    fragment = f"{channel} {flag}"
    encoded = quote(fragment, safe='')
    return f"{base}#{encoded}"

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

async def ping_from_iran_only(host, port=None):
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

async def check_full_link_ping(url):
    host, port = extract_host(url)
    if not host:
        return 0, False, 0
    ping, ok, cnt = await ping_from_iran_only(host, port)
    return ping, ok, cnt

# ======================================================================
# اسکرپ
# ======================================================================
def decrypt_subscription(data: bytes, passwords: list):
    protocols = ("vless://", "vmess://", "trojan://",
                  "hy2://", "tuic://")
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
        r'tuic://[^\s<>"\']+'
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            link = m.group().strip()
            if len(link) > 10:
                results.append(link)
    return list(set(results))

async def fetch_files_from_channel(bot, profile_id, channel, source):
    try:
        chat_id = channel if channel.startswith('@') else '@' + channel
        messages = await bot.get_chat_history(chat_id, limit=5)
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

_scrape_cache = {}
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

async def scrape_channel_with_retry(profile_id, channel, only_new=True, max_retries=2):
    try:
        return await _scrape_channel_internal(profile_id, channel, only_new)
    except Exception as e:
        log.error(f"❌ scrape {channel} error: {e}")
        return [], []

async def _scrape_channel_internal(profile_id, channel, only_new=True):
    import time as _t
    current_time = _t.time()
    url = f"https://t.me/s/{channel.lstrip('@')}"
    log.info(f"🔍 Scraping {channel}...")
    headers = {
        "User-Agent": _USER_AGENTS[hash(datetime.now().timestamp()) % len(_USER_AGENTS)],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as cl:
        r = await cl.get(url, headers=headers)
        if r.status_code == 429:
            log.warning(f"Rate limit for {channel}, waiting 30s")
            await asyncio.sleep(30)
            r = await cl.get(url, headers=headers)
        if r.status_code != 200:
            log.warning(f"⚠️ {channel} returned status {r.status_code}")
            return [], []
        html_text = r.text

        config_links = extract_links_from_text(html_text)
        proxy_links = extract_proxy_links_from_text(html_text)

        log.info(f"📊 {channel}: found {len(config_links)} configs, {len(proxy_links)} proxies")
        update_last_scrape_time(profile_id, channel, get_tehran_time())

        cached = _scrape_cache.get((profile_id, channel), (0, [], []))
        old_configs = cached[1] if len(cached) > 1 else []
        old_proxies = cached[2] if len(cached) > 2 else []
        new_configs = [link for link in config_links if link not in old_configs]
        new_proxies = [link for link in proxy_links if link not in old_proxies]
        log.info(f"🆕 {channel}: {len(new_configs)} new configs, {len(new_proxies)} new proxies")
        _scrape_cache[(profile_id, channel)] = (current_time, config_links, proxy_links)
        return new_configs, new_proxies

# ======================================================================
# ارسال
# ======================================================================
async def send_to_destination(bot, profile_id, text, buttons=None):
    dest = get_profile_dest(profile_id)
    if not dest:
        log.error(f"❌ Profile {profile_id} has no destination!")
        return False

    log.info(f"📤 Sending to {dest} (profile {profile_id})")
    chunks = split_text(text, 4096)
    success = True
    for idx, chunk in enumerate(chunks):
        try:
            reply_markup = InlineKeyboardMarkup(buttons) if buttons and idx == 0 else None
            await bot.send_message(
                dest, chunk,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            log.info(f"✅ Sent chunk {idx+1}/{len(chunks)} to {dest} with HTML")
        except Exception as e:
            log.error(f"❌ HTML failed for {dest} chunk {idx+1}: {e}")
            try:
                plain = re.sub(r'<[^>]+>', '', chunk)
                await bot.send_message(dest, plain[:4096], disable_web_page_preview=True)
                log.info(f"✅ Sent chunk {idx+1}/{len(chunks)} to {dest} (plain)")
            except Exception as e3:
                log.error(f"❌ Even plain failed for {dest} chunk {idx+1}: {e3}")
                success = False
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
# ارسال کانفیگ‌ها (با اسپانسر رنگی - اصلاح شده)
# ======================================================================
async def post_configs(bot, profile_id, working, source_for_seen="", skip_duplicate=False):
    if not working:
        return 0

    max_post = get_profile_max_post(profile_id)
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
    log.info(f"🔧 Using custom_query: '{custom_query}'")
    dest = get_profile_dest(profile_id)
    banner_template = get_profile_banner_config(profile_id) or "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

    sponsor = get_sponsor(profile_id)
    sponsor_button = None
    if sponsor and sponsor["enabled"]:
        # استفاده از ایموجی برای وضعیت و رنگ
        color_emoji = "🟢"
        sponsor_button_text = f"{color_emoji} {sponsor['button_text']}"
        sponsor_button = InlineKeyboardButton(sponsor_button_text, url=sponsor["url"])
    elif sponsor and not sponsor["enabled"]:
        color_emoji = "🔴"
        sponsor_button_text = f"{color_emoji} {sponsor['button_text']} (غیرفعال)"
        sponsor_button = InlineKeyboardButton(sponsor_button_text, url=sponsor["url"])

    sent_count = 0
    for i, (url, ping, node_count) in enumerate(items, 1):
        n = last_n + i
        host, _ = extract_host(url)
        flag = "🌐"
        if host:
            ip = await host_to_ip(host)
            if ip:
                flag = await get_flag_for_ip(ip)

        channel_display = dest if dest else "@VaslZone"
        modified_url = append_channel_and_flag_encoded(url, channel_display, flag, custom_query)

        if show_numbers:
            if ping > 0:
                header = f"<b>#{n}</b> {channel_display} {flag} {ping}ms"
            else:
                header = f"<b>#{n}</b> {channel_display} {flag}"
        else:
            if ping > 0:
                header = f"{channel_display} {flag} {ping}ms"
            else:
                header = f"{channel_display} {flag}"

        block = f"<pre>{modified_url}</pre>"
        configs_text = header + "\n" + block

        try:
            full_text = banner_template.format(configs=configs_text)
        except KeyError:
            full_text = f"✦ V2Ray Config List\n\n{configs_text}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

        buttons = []
        if sponsor_button:
            buttons.append(sponsor_button)

        reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None

        try:
            await bot.send_message(
                dest,
                full_text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            sent_count += 1
            log.info(f"✅ Sent config #{n} to {dest}")
        except Exception as e:
            log.error(f"Failed to send config {n}: {e}")
            try:
                plain = re.sub(r'<[^>]+>', '', full_text)
                await bot.send_message(dest, plain[:4096], disable_web_page_preview=True)
                sent_count += 1
                log.info(f"✅ Sent config #{n} to {dest} (plain)")
            except Exception as e2:
                log.error(f"Failed even plain for config {n}: {e2}")

        if not skip_duplicate or not is_already_posted(profile_id, modified_url):
            mark_as_posted(profile_id, modified_url, source_for_seen, full_url=modified_url)

    if sent_count > 0:
        set_profile_last_num(profile_id, last_n + sent_count)

    return sent_count

async def post_proxies(bot, profile_id, proxies_with_ping):
    if not proxies_with_ping:
        return 0, None

    max_proxies = get_profile_max_proxies(profile_id)
    show_date = get_profile_show_date_proxy(profile_id)
    proxy_text = ""
    for proxy_url, ping, flag in proxies_with_ping[:max_proxies]:
        if "t.me/proxy" not in proxy_url.lower():
            continue
        normalized_url = normalize_telegram_proxy(proxy_url)
        clean_url = clean_proxy_link(normalized_url)
        safe_url = html.escape(clean_url, quote=False)
        proxy_text += f"• {flag} <a href=\"{safe_url}\">Telegram Proxy</a>\n"
        mark_proxy_posted(profile_id, clean_url)

    proxy_count = len(proxies_with_ping[:max_proxies])
    banner_proxy = get_profile_banner_proxy(profile_id)
    date_str = get_tehran_date() if show_date else ""
    try:
        text = banner_proxy.format(
            date=date_str,
            count=proxy_count,
            proxies=proxy_text,
        )
    except KeyError:
        log.error("❌ Banner proxy missing placeholders")
        text = f"🌐 Proxies\n{proxy_text}"

    sponsor = get_sponsor(profile_id)
    sponsor_button = None
    if sponsor and sponsor["enabled"]:
        color_emoji = "🟢"
        sponsor_button_text = f"{color_emoji} {sponsor['button_text']}"
        sponsor_button = InlineKeyboardButton(sponsor_button_text, url=sponsor["url"])
    elif sponsor and not sponsor["enabled"]:
        color_emoji = "🔴"
        sponsor_button_text = f"{color_emoji} {sponsor['button_text']} (غیرفعال)"
        sponsor_button = InlineKeyboardButton(sponsor_button_text, url=sponsor["url"])
    buttons = [sponsor_button] if sponsor_button else None
    return proxy_count, (text, buttons)

async def post_working_configs(bot, profile_id, working, proxies_with_ping, source_for_seen="", force=False, skip_duplicate=False):
    dest = get_profile_dest(profile_id)
    if not dest:
        return 0, "❌ هیچ مقصدی تنظیم نشده!"

    post_configs_enabled = get_profile_post_configs(profile_id) if not force else True
    post_proxies_enabled = get_profile_post_proxies(profile_id) if not force else True

    total_configs = 0
    total_proxies = 0
    results = []

    if post_configs_enabled and working:
        if skip_duplicate:
            # در حالت دستی، همه را بدون فیلتر ارسال کن
            unique_working = working
        else:
            unique_working = []
            seen_urls = set()
            for url, ping, cnt in working:
                if not is_already_posted(profile_id, url):
                    unique_working.append((url, ping, cnt))
                else:
                    log.info(f"⏭️ Skipping duplicate: {url[:50]}...")
        if not unique_working:
            log.info("ℹ️ No new configs to post (all duplicates).")
        else:
            config_count = await post_configs(bot, profile_id, unique_working, source_for_seen, skip_duplicate=skip_duplicate)
            if config_count > 0:
                total_configs = config_count
                results.append(f"{config_count} configs")

    if post_proxies_enabled and proxies_with_ping:
        valid_proxies = [p for p in proxies_with_ping if "t.me/proxy" in p[0].lower()]
        if valid_proxies:
            unique_proxies = []
            for p in valid_proxies:
                if skip_duplicate or not is_proxy_posted(profile_id, p[0]):
                    unique_proxies.append(p)
            if unique_proxies:
                proxy_count, proxy_payload = await post_proxies(bot, profile_id, unique_proxies)
                if proxy_count > 0 and proxy_payload:
                    text, buttons = proxy_payload
                    sent = await send_to_destination(bot, profile_id, text, buttons)
                    if sent:
                        total_proxies = proxy_count
                        results.append(f"{proxy_count} proxies")

    if not results:
        return 0, "no new content to send"

    result_msg = "posted " + " and ".join(results)
    return total_configs, result_msg

# ======================================================================
# سیکل کامل
# ======================================================================
async def run_full_cycle_for_profile(bot, profile_id, only_new=True, is_instant=False):
    log.info("=" * 50)
    log.info(f"🔄 run_full_cycle for profile {profile_id} STARTED (instant={is_instant})")

    profile = get_profile(profile_id)
    if not profile:
        log.error(f"❌ Profile {profile_id} not found!")
        return 0, "profile not found"

    sources = get_profile_sources(profile_id)
    if not sources:
        log.error(f"❌ Profile {profile_id} has no sources!")
        return 0, "no sources"

    dest = get_profile_dest(profile_id)
    if not dest:
        log.error(f"❌ Profile {profile_id} has no destination!")
        return 0, "no destination"

    log.info(f"📡 Sources ({len(sources)}): {sources}")
    log.info(f"🎯 Destination: {dest}")
    log.info(f"🆕 only_new: {only_new}")

    all_configs = []
    all_proxies = []
    seen_configs = set()
    seen_proxies = set()
    seen_urls = set()

    # تنظیمات سریع‌تر برای حالت لحظه‌ای
    if is_instant:
        scrape_limit = 1  # فقط یک منبع را اسکرپ کن (سریع‌تر)
        test_limit = 0
        ping_timeout = 3
        max_concurrent = 50
    else:
        scrape_limit = 10
        test_limit = 15
        ping_timeout = 10
        max_concurrent = 20

    async def scrape_one(src):
        config_links, proxy_links = await scrape_channel_with_retry(profile_id, src, only_new=True)
        return src, config_links, proxy_links

    # در حالت instant فقط یک منبع (اولی) را اسکرپ کن
    if is_instant and sources:
        sources_to_scrape = sources[:1]
    else:
        sources_to_scrape = sources

    scrape_tasks = [scrape_one(src) for src in sources_to_scrape]
    results = await asyncio.gather(*scrape_tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            log.warning(f"Scrape error: {res}")
            continue
        src, config_links, proxy_links = res
        log.info(f"  {src}: {len(config_links)} configs, {len(proxy_links)} proxies from web")

        for link in config_links:
            if link in seen_urls:
                continue
            seen_urls.add(link)
            if link not in seen_configs:
                seen_configs.add(link)
                all_configs.append((link, src))

        for link in proxy_links:
            if link in seen_urls:
                continue
            seen_urls.add(link)
            norm = normalize_proxy_url(link)
            if not norm:
                continue
            if norm not in seen_proxies and not is_proxy_posted(profile_id, norm):
                seen_proxies.add(norm)
                all_proxies.append(norm)

    # فقط در حالت عادی فایل‌ها را چک کن (در حالت instant برای سرعت کمتر)
    if not is_instant:
        file_tasks = [fetch_files_from_channel(bot, profile_id, src, src) for src in sources_to_scrape]
        file_results = await asyncio.gather(*file_tasks, return_exceptions=True)
        for i, res in enumerate(file_results):
            if isinstance(res, Exception):
                log.warning(f"File fetch error for {sources_to_scrape[i]}: {res}")
                continue
            links = res
            src = sources_to_scrape[i]
            log.info(f"  {src}: {len(links)} links from files")
            for link in links:
                if link in seen_urls:
                    continue
                seen_urls.add(link)
                if "t.me/proxy" in link.lower():
                    norm = normalize_proxy_url(link)
                    if norm and norm not in seen_proxies and not is_proxy_posted(profile_id, norm):
                        seen_proxies.add(norm)
                        all_proxies.append(norm)
                else:
                    if link not in seen_configs:
                        seen_configs.add(link)
                        all_configs.append((link, src))

    new_configs = [u for u, src in all_configs if not is_already_posted(profile_id, u)]
    log.info(f"📊 New configs: {len(new_configs)}, New proxies: {len(all_proxies)}")

    working = []
    if is_instant:
        # در حالت لحظه‌ای، همه کانفیگ‌ها را بدون پینگ معتبر در نظر بگیر
        working = [(u, 0, 0) for u in new_configs]
        log.info(f"⚡ Instant mode: all {len(working)} configs considered working")
    else:
        if new_configs:
            to_test = new_configs[:test_limit]
            log.info(f"📊 Testing {len(to_test)} configs...")
            sem = asyncio.Semaphore(max_concurrent)
            async def _check(u):
                async with sem:
                    try:
                        ping, ok, cnt = await check_full_link_ping(u)
                        if ok:
                            log.info(f"✅ Config OK: {u[:50]}... ping={ping}ms")
                            return u, True, ping, cnt
                        else:
                            log.info(f"❌ Config FAIL: {u[:50]}...")
                            return u, False, 0, 0
                    except Exception as e:
                        log.debug(f"ping failed for {u[:30]}: {e}")
                        return u, False, 0, 0

            rs = await asyncio.gather(*[_check(u) for u in to_test], return_exceptions=True)
            for r in rs:
                if isinstance(r, Exception):
                    continue
                if r[1]:
                    working.append((r[0], r[2], r[3]))
            log.info(f"📊 Working configs: {len(working)}")
        else:
            log.info("ℹ️ No new configs to test")

    proxy_with_ping = []
    if all_proxies:
        valid_proxies = [p for p in all_proxies if "t.me/proxy" in p.lower()]
        if valid_proxies:
            log.info(f"📊 Processing {len(valid_proxies)} proxies...")
            if is_instant:
                for proxy_url in valid_proxies[:10]:
                    flag = "🌐"
                    proxy_with_ping.append((proxy_url, 0, flag))
                log.info(f"⚡ Instant: {len(proxy_with_ping)} proxies ready")
            else:
                sem = asyncio.Semaphore(max_concurrent)
                async def check_proxy(proxy_url):
                    async with sem:
                        ping, ok, cnt = await check_full_link_ping(proxy_url)
                        host, _ = extract_host(proxy_url)
                        ip = await host_to_ip(host) if host else None
                        flag = "🌐"
                        if ip:
                            flag = await get_flag_for_ip(ip)
                        return proxy_url, ping if ok else 0, flag

                results = await asyncio.gather(
                    *[check_proxy(p) for p in valid_proxies[:10]], return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        continue
                    proxy_with_ping.append(r)
                log.info(f"📊 Proxies with ping: {len(proxy_with_ping)}")
        else:
            log.info("ℹ️ No valid Telegram proxies found.")
    else:
        log.info("ℹ️ No proxies found in sources.")

    if not working and not proxy_with_ping:
        log.warning(f"⚠️ No working configs or proxies found for profile {profile_id}!")
        return 0, "no working configs or proxies"

    log.info(f"📤 Posting {len(working)} configs and {len(proxy_with_ping)} proxies for profile {profile_id}...")
    result = await post_working_configs(bot, profile_id, working, proxy_with_ping, source_for_seen="auto", force=False)
    log.info(f"✅ Cycle result for profile {profile_id}: {result}")
    log.info("=" * 50)
    return result

# ======================================================================
# حلقه خودکار
# ======================================================================
async def profile_loop(bot, profile_id):
    profile = get_profile(profile_id)
    if not profile:
        log.error(f"❌ Profile {profile_id} not found, stopping loop.")
        return

    interval = profile.get("interval_min", 5)
    log.info(f"🔄 Starting auto loop for profile {profile_id} with interval {interval} min")
    while True:
        try:
            if interval == 0:
                # حالت لحظه‌ای: هر ۳ ثانیه یک بار اجرا کن
                await asyncio.sleep(3)
                log.info(f"⚡ INSTANT UPDATE for profile {profile_id}")
                n, m = await run_full_cycle_for_profile(bot, profile_id, only_new=True, is_instant=True)
                log.info(f"[instant profile {profile_id}] {n} - {m}")
            else:
                now = datetime.now(TEHRAN_TZ)
                next_run = now + timedelta(minutes=interval)
                sleep_seconds = (next_run - now).total_seconds()
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                log.info(f"⏰ AUTO TICK for profile {profile_id} ({profile['dest_name']})")
                n, m = await run_full_cycle_for_profile(bot, profile_id, only_new=True, is_instant=False)
                log.info(f"[auto profile {profile_id}] {n} - {m}")
        except asyncio.CancelledError:
            log.info(f"🛑 Auto loop for profile {profile_id} cancelled.")
            break
        except Exception as e:
            log.error(f"❌ profile_loop error for {profile_id}: {e}")
            await asyncio.sleep(60)

# ======================================================================
# گزارش روزانه
# ======================================================================
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
            interval = p['interval_min']
            lines.append(f"• {p['dest_name']} (ID:{p['id']}) – {src_count} منبع, بازه {interval}m, #{last_num+1}")

        msg = "\n".join(lines)
        await app.bot.send_message(ADMIN_ID, msg, parse_mode="HTML")
        log.info("✅ Daily report sent.")
    except Exception as e:
        log.error(f"❌ Failed to send daily report: {e}")

# ======================================================================
# بک‌آپ کانفیگ/پروکسی (اصلاح شده)
# ======================================================================
async def export_backup(update, context, profile_id, backup_type, count=None):
    """ارسال فایل متنی شامل لینک‌های کانفیگ یا پروکسی با تعداد دقیق"""
    try:
        if backup_type == "configs":
            if count is None or count == -1:
                rows = c.execute("SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' ORDER BY last_posted DESC", (profile_id,)).fetchall()
            else:
                rows = c.execute("SELECT full_url FROM seen WHERE profile_id=? AND full_url != '' ORDER BY last_posted DESC LIMIT ?", (profile_id, count)).fetchall()
            links = [row[0] for row in rows if row[0]]
            if not links:
                await update.message.reply_text("❌ هیچ کانفیگی برای بک‌آپ یافت نشد.")
                return
            filename = f"configs_backup_{get_tehran_date()}.txt"
            content = "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(DATA_DIR, filename), "rb") as f:
                await update.message.reply_document(document=f, filename=filename, caption=f"📤 {len(links)} کانفیگ")
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
            filename = f"proxies_backup_{get_tehran_date()}.txt"
            content = "\n".join(links)
            with open(os.path.join(DATA_DIR, filename), "w", encoding="utf-8") as f:
                f.write(content)
            with open(os.path.join(DATA_DIR, filename), "rb") as f:
                await update.message.reply_document(document=f, filename=filename, caption=f"📤 {len(links)} پروکسی")
            os.remove(os.path.join(DATA_DIR, filename))
            return
        else:
            await update.message.reply_text("❌ نوع نامعتبر.")
    except Exception as e:
        log.error(f"Backup export error: {e}")
        await update.message.reply_text(f"❌ خطا در بک‌آپ: {str(e)[:100]}")

# ======================================================================
# کیبوردها و پیام‌ها
# ======================================================================
BOT_REF = None
BOT_LANG = "fa"

T = {
    "fa": {
        "welcome": "🤖 **بات جمع‌آوری کانفیگ و پروکسی**\n\n"
                  "📡 تعداد پروفایل‌ها: {profiles}\n"
                  "🔢 بعدی: #{next_n}\n"
                  "⏰ بازه‌ها متغیر",
        "private": "🔒 خصوصیه.",
        "admin_panel": "🔐 **پنل مدیریت پروفایل**\n\n"
                       "📡 منابع: {srcs} | 🎯 مقصد: {dest}\n"
                       "🎨 نام: {name} | 🔢 #{num}\n"
                       "⏰ {interval}m | 🎯 max:{max_post}\n"
                       "📢 اسپانسر: {sponsor}\n"
                       "🌍 حالت پینگ: {ping_mode}\n"
                       "📡 کانفیگ: {cfg_status} | 🌐 پروکسی: {prx_status}\n"
                       "🔢 شماره‌گذاری: {numbers_status}\n"
                       "🔗 کوئری سفارشی: {custom_query}\n"
                       "📅 تاریخ کانفیگ: {date_cfg}\n"
                       "📅 تاریخ پروکسی: {date_prx}\n"
                       "⏰ کرون: {cron}",
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
        "btn_private": "🔒 خصوصی",
        "btn_stats": "📊 آمار",
        "btn_test": "🧪 تست",
        "btn_clear": "🗑 پاک DB",
        "btn_reset": "🔢 ریست شماره",
        "btn_lang": "🌐 English",
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
        "lang_ok": "✅ فارسی شد",
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
    },
    "en": {
        "btn_blacklist": "🚫 Blacklist",
        "blacklist_title": "🚫 **Blacklist for profile {name}**\n\nBlocked words:\n{words}\n\nAny config containing these words will be filtered.",
        "blacklist_empty": "No words in blacklist.",
        "blacklist_add_prompt": "📝 Enter blocked words (comma or newline separated):",
        "blacklist_added": "✅ Words added: {words}",
        "blacklist_removed": "✅ Word removed.",
        "blacklist_clear": "✅ Blacklist cleared.",
        "btn_blacklist_add": "➕ Add",
        "btn_blacklist_clear": "🗑 Clear all",
        "btn_backup": "💾 Backup DB",
        "backup_sent": "✅ Database file sent.",
        "backup_failed": "❌ Backup failed.",
        "btn_set_schedule_cron": "⏰ Advanced Schedule (cron)",
        "schedule_cron_prompt": "⏰ Enter cron expression (e.g. `*/5 * * * *` for every 5 minutes).\n\nLeave empty to use interval minutes.",
        "schedule_cron_set": "✅ Cron schedule set: {cron}",
        "btn_backup_export": "📤 Backup Configs/Proxies",
        "backup_export_type": "📤 **Backup**\n\nWhich type?",
        "backup_export_scope": "📤 **Scope**\n\nAll, last 100, or custom count?",
        "backup_export_count_prompt": "🔢 Enter custom count (number):",
        "backup_export_scope_all": "All",
        "backup_export_scope_100": "Last 100",
        "backup_export_scope_custom": "Custom count",
    }
}

def msg(key, **kwargs):
    text = T[BOT_LANG].get(key, T["fa"].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except Exception:
            return text
    return text

# ======================================================================
# کیبوردها
# ======================================================================
def profiles_kb():
    profiles = get_profiles()
    btns = []
    for p in profiles:
        btns.append([InlineKeyboardButton(f"{p['dest_name']} (ID:{p['id']})", callback_data=f"prof_{p['id']}")])
    btns.append([InlineKeyboardButton("➕ Add Profile", callback_data="prof_add")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data="back_home")])
    return InlineKeyboardMarkup(btns)

def profile_admin_kb(profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return None
    ping_mode = prof["ping_mode"]
    ping_label = "🌍 ایران‌فقط: روشن" if ping_mode == "iran" else "🌍 جهانی: روشن"
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

    cfg_btn = msg("btn_toggle_configs", status=cfg_status)
    prx_btn = msg("btn_toggle_proxies", status=prx_status)
    num_btn = msg("btn_toggle_numbers", status=num_status)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_sources"), callback_data=f"src_list_{profile_id}"),
         InlineKeyboardButton(msg("btn_dest_list"), callback_data=f"dl_{profile_id}")],
        [InlineKeyboardButton(f"📢 اسپانسر: {sponsor_status}", callback_data=f"sp_menu_{profile_id}"),
         InlineKeyboardButton(msg("btn_set_name"), callback_data=f"ac_{profile_id}")],
        [InlineKeyboardButton(msg("btn_set_banner_config"), callback_data=f"ab_config_{profile_id}"),
         InlineKeyboardButton(msg("btn_set_banner_proxy"), callback_data=f"ab_proxy_{profile_id}")],
        [InlineKeyboardButton(msg("btn_set_time"), callback_data=f"ai_{profile_id}"),
         InlineKeyboardButton(msg("btn_set_max"), callback_data=f"setmax_{profile_id}")],
        [InlineKeyboardButton(ping_label, callback_data=f"tglping_{profile_id}"),
         InlineKeyboardButton(cfg_btn, callback_data=f"tglcfg_{profile_id}")],
        [InlineKeyboardButton(prx_btn, callback_data=f"tglproxy_{profile_id}"),
         InlineKeyboardButton(num_btn, callback_data=f"togglenum_{profile_id}")],
        [InlineKeyboardButton(f"📅 تاریخ کانفیگ: {date_cfg_status}", callback_data=f"tgl_date_cfg_{profile_id}"),
         InlineKeyboardButton(f"📅 تاریخ پروکسی: {date_prx_status}", callback_data=f"tgl_date_prx_{profile_id}")],
        [InlineKeyboardButton(msg("btn_set_custom_query"), callback_data=f"setquery_{profile_id}"),
         InlineKeyboardButton(msg("btn_stats"), callback_data=f"ast_{profile_id}")],
        [InlineKeyboardButton(msg("btn_test"), callback_data=f"sendtest_{profile_id}"),
         InlineKeyboardButton(msg("btn_runnow"), callback_data=f"runnow_{profile_id}")],
        [InlineKeyboardButton(msg("btn_instant"), callback_data=f"instant_{profile_id}"),
         InlineKeyboardButton(msg("btn_manual_send"), callback_data=f"manual_{profile_id}")],
        [InlineKeyboardButton(msg("btn_blacklist"), callback_data=f"bl_list_{profile_id}"),
         InlineKeyboardButton(msg("btn_set_schedule_cron"), callback_data=f"setcron_{profile_id}")],
        [InlineKeyboardButton(msg("btn_backup"), callback_data=f"backup_{profile_id}"),
         InlineKeyboardButton(msg("btn_backup_export"), callback_data=f"backup_export_menu_{profile_id}")],
        [InlineKeyboardButton(msg("btn_reset"), callback_data=f"rn_{profile_id}"),
         InlineKeyboardButton(msg("btn_clear"), callback_data=f"cd1_{profile_id}")],
        [InlineKeyboardButton("❌ Delete Profile", callback_data=f"delprof_{profile_id}")],
        [InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list")],
    ])

def destinations_kb(profile_id):
    dest = get_profile_dest(profile_id)
    btns = []
    if dest:
        btns.append([InlineKeyboardButton(f"❌ {dest}", callback_data=f"dd_{profile_id}")])
    btns.append([
        InlineKeyboardButton(msg("btn_add_dest"), callback_data=f"da_{profile_id}"),
    ])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")])
    return InlineKeyboardMarkup(btns)

def sponsor_kb(profile_id):
    sponsor = get_sponsor(profile_id)
    btns = []
    if sponsor:
        status_text = "✅ فعال" if sponsor["enabled"] else "❌ غیرفعال"
        btns.append([InlineKeyboardButton(f"{sponsor['name']} - {status_text}", callback_data=f"sp_toggle_{profile_id}")])
        btns.append([InlineKeyboardButton("✏️ ویرایش", callback_data=f"sp_edit_{profile_id}")])
        btns.append([InlineKeyboardButton("🗑 حذف", callback_data=f"sp_clear_{profile_id}")])
    else:
        btns.append([InlineKeyboardButton("➕ افزودن اسپانسر", callback_data=f"sp_add_{profile_id}")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")])
    return InlineKeyboardMarkup(btns)

def source_list_kb(profile_id):
    sources = get_profile_sources(profile_id)
    btns = []
    if sources:
        for i, src in enumerate(sources):
            btns.append([InlineKeyboardButton(f"❌ {src}", callback_data=f"src_del_{profile_id}_{i}")])
    btns.append([InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")])
    return InlineKeyboardMarkup(btns)

def empty_button_kb(profile_id, callback):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_empty"), callback_data=callback)],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")]
    ])

def blacklist_kb(profile_id):
    words = get_blacklist(profile_id)
    btns = []
    if words:
        for w in words:
            btns.append([InlineKeyboardButton(f"❌ {w}", callback_data=f"bl_del_{profile_id}_{w}")])
    btns.append([InlineKeyboardButton("➕ افزودن", callback_data=f"bl_add_{profile_id}")])
    if words:
        btns.append([InlineKeyboardButton("🗑 پاک کردن همه", callback_data=f"bl_clear_{profile_id}")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")])
    return InlineKeyboardMarkup(btns)

def backup_export_type_kb(profile_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📡 کانفیگ", callback_data=f"backup_export_type_{profile_id}_configs")],
        [InlineKeyboardButton("🌐 پروکسی", callback_data=f"backup_export_type_{profile_id}_proxies")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")],
    ])

def backup_export_scope_kb(profile_id, backup_type):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("backup_export_scope_all"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_all")],
        [InlineKeyboardButton(msg("backup_export_scope_100"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_100")],
        [InlineKeyboardButton(msg("backup_export_scope_custom"), callback_data=f"backup_export_scope_{profile_id}_{backup_type}_custom")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=f"backup_export_menu_{profile_id}")],
    ])

# ======================================================================
# دستورات
# ======================================================================
async def cmd_start(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return await u.message.reply_text(msg("private"))
    profiles = get_profiles()
    total = len(profiles)
    next_n = 0
    if profiles:
        last_num = max(p["last_num"] for p in profiles)
        next_n = last_num + 1
    txt = msg("welcome", profiles=total, next_n=next_n)
    btns = [[InlineKeyboardButton("📋 Manage Profiles", callback_data="profiles_list")]]
    await u.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

async def cmd_admin(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return
    await show_profiles_list(u.message)

async def show_profiles_list(msg_or_q):
    profiles = get_profiles()
    if not profiles:
        txt = "❌ هیچ پروفایلی وجود ندارد.\nبرای ساخت، دکمه Add را بزنید."
    else:
        lines = []
        for p in profiles:
            lines.append(f"• `{p['dest_name']}` (ID: {p['id']}) – {len(get_profile_sources(p['id']))} منبع, {p['interval_min']}m")
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
    if u.effective_user.id != ADMIN_ID:
        return
    p = await u.message.reply_text("⏳ در حال اجرا برای همه پروفایل‌ها...")
    try:
        profiles = get_profiles()
        results = []
        for prof in profiles:
            log.info(f"🚀 /runnow for profile {prof['id']}")
            n, m = await run_full_cycle_for_profile(u.get_bot(), prof['id'], only_new=True, is_instant=False)
            results.append(f"{prof['dest_name']}: {n} - {m}")
        await p.edit_text("✅ Done:\n" + "\n".join(results))
    except Exception as e:
        log.error(f"❌ /runnow error: {e}")
        log.error(traceback.format_exc())
        await p.edit_text(f"❌ {str(e)[:200]}")

async def cmd_runall(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return
    p = await u.message.reply_text("⏳ در حال اجرا (همه) برای همه پروفایل‌ها...")
    try:
        profiles = get_profiles()
        results = []
        for prof in profiles:
            log.info(f"🚀 /runall for profile {prof['id']}")
            n, m = await run_full_cycle_for_profile(u.get_bot(), prof['id'], only_new=False, is_instant=False)
            results.append(f"{prof['dest_name']}: {n} - {m}")
        await p.edit_text("✅ Done:\n" + "\n".join(results))
    except Exception as e:
        log.error(f"❌ /runall error: {e}")
        log.error(traceback.format_exc())
        await p.edit_text(f"❌ {str(e)[:200]}")

async def cmd_sendtest(u, ctx):
    if u.effective_user.id != ADMIN_ID:
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
    if update.effective_user.id != ADMIN_ID:
        return
    msg_lines = []
    msg_lines.append("🔍 **گزارش عیب‌یابی جامع بات**")
    msg_lines.append("")
    profiles = get_profiles()
    msg_lines.append(f"📌 تعداد پروفایل‌ها: {len(profiles)}")
    for prof in profiles:
        msg_lines.append(f"  • {prof['dest_name']} (ID:{prof['id']}) - {len(get_profile_sources(prof['id']))} منبع, بازه {prof['interval_min']}m, پینگ {prof['ping_mode']}")
    msg_lines.append("")
    seen_cfg = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    seen_prx = c.execute("SELECT COUNT(*) FROM proxies_seen").fetchone()[0]
    msg_lines.append("💾 **دیتابیس:**")
    msg_lines.append(f"• کانفیگ‌های دیده‌شده: {seen_cfg}")
    msg_lines.append(f"• پروکسی‌های دیده‌شده: {seen_prx}")
    msg_lines.append("")
    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")

# ======================================================================
# کالبک
# ======================================================================
async def on_callback(u, ctx):
    q = u.callback_query
    try:
        if q.from_user.id != ADMIN_ID:
            return await q.answer("")
        await q.answer()
        d = q.data or ""
        log.info(f"📨 Callback data: {d}")

        # ===== بک‌آپ export =====
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
                    await q.edit_message_text(msg("backup_export_count_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"backup_export_menu_{profile_id}")]]))
                else:
                    await q.answer("⚠️ محدوده نامعتبر")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ===== لیست سیاه =====
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
                await q.edit_message_text(msg("blacklist_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"bl_list_{profile_id}")]]))
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

        # ===== بک‌آپ دیتابیس =====
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

        # ===== کرون =====
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

        # ===== بقیه =====
        if d == "profiles_list":
            await show_profiles_list(q.message)
            return

        if d == "prof_add":
            ctx.user_data["action"] = "prof_add"
            await q.edit_message_text(msg("profile_add_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list")]]))
            return

        if d == "back_home":
            await show_profiles_list(q.message)
            return

        if d.startswith("instant_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                update_profile(profile_id, interval_min=0)
                await q.answer("⚡ حالت اپدیت لحظه‌ای فعال شد (هر ۳ ثانیه)")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # حذف پروفایل با تأیید دو مرحله‌ای
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
                        [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delprof_confirm1_{profile_id}")],
                        [InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}")]
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
                        [InlineKeyboardButton("🗑 حذف نهایی", callback_data=f"delprof_confirm2_{profile_id}")],
                        [InlineKeyboardButton("❌ لغو", callback_data=f"prof_{profile_id}")]
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
                    [InlineKeyboardButton("🔙 بازگشت به لیست", callback_data="profiles_list")]
                ]))
            else:
                await q.answer("⚠️ خطا در داده")
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

        # ===== اسپانسر =====
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
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
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
                        [InlineKeyboardButton("نام", callback_data=f"sp_edit_field_{profile_id}_name"),
                         InlineKeyboardButton("لینک", callback_data=f"sp_edit_field_{profile_id}_url")],
                        [InlineKeyboardButton("متن دکمه", callback_data=f"sp_edit_field_{profile_id}_text"),
                         InlineKeyboardButton("رنگ", callback_data=f"sp_edit_field_{profile_id}_color")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
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
                            [InlineKeyboardButton("🔵 Primary (آبی)", callback_data=f"sp_setcolor_{profile_id}_primary")],
                            [InlineKeyboardButton("🟢 Success (سبز)", callback_data=f"sp_setcolor_{profile_id}_success")],
                            [InlineKeyboardButton("🔴 Danger (قرمز)", callback_data=f"sp_setcolor_{profile_id}_danger")],
                            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}")]
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
                            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}")]
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
                sponsor = get_sponsor(profile_id)
                if not sponsor:
                    await q.answer("اسپانسری وجود ندارد.")
                    return
                update_sponsor_color(profile_id, color)
                await q.answer(f"✅ رنگ به {color} تغییر کرد.")
                # اگر در مرحله افزودن اسپانسر جدید هستیم، اطلاعات را کامل و ذخیره می‌کنیم
                if ctx.user_data.get("sponsor_step") == "color_wait":
                    name = ctx.user_data.get("sponsor_name")
                    url = ctx.user_data.get("sponsor_url")
                    btn_text = ctx.user_data.get("sponsor_button_text", "Advertisement")
                    if name and url:
                        set_sponsor(profile_id, name, url, btn_text, color)
                        await q.message.reply_text(msg("sp_added", name=name), parse_mode="HTML")
                        del ctx.user_data["sponsor_step"]
                        del ctx.user_data["sponsor_name"]
                        del ctx.user_data["sponsor_url"]
                        del ctx.user_data["sponsor_button_text"]
                # بازگشت به منوی اسپانسر
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

        # ===== مدیریت منابع =====
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
                await q.edit_message_text(msg("send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"src_list_{profile_id}")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ===== مقصد =====
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
                await q.edit_message_text("📝 کانال مقصد جدید رو بفرست (با @ یا بدون):\nمثال: `@MyChannel`", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"dl_{profile_id}")]]))
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

        # ===== سایر تنظیمات =====
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
                await q.edit_message_text(f"Current Config Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{configs}}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")]]))
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
                await q.edit_message_text(f"Current Proxy Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{proxies}}):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("ai_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"ai_{profile_id}"
                current = get_profile_interval(profile_id)
                await q.edit_message_text(f"Now: {current}m\nSend 0-1440 (0 for instant):", reply_markup=empty_button_kb(profile_id, f"empty_ai_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_ai_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.answer("✅ بدون تغییر.")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("setmax_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                ctx.user_data["action"] = f"setmax_{profile_id}"
                current = get_profile_max_post(profile_id)
                await q.edit_message_text(f"Now: {current}\nSend 1-50 (یا دکمه خالی):", reply_markup=empty_button_kb(profile_id, f"empty_setmax_{profile_id}"))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("empty_setmax_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
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
                txt = f"📊 مقصد: {dest}\nمنابع: {len(get_profile_sources(profile_id))}\nاسپانسر: {n_sp}\nبعدی: #{next_n}\nحداکثر: {get_profile_max_post(profile_id)}"
                await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}")]]))
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
                p = await q.edit_message_text("⏳ در حال اجرا...")
                try:
                    n, m = await run_full_cycle_for_profile(u.get_bot(), profile_id, only_new=True, is_instant=False)
                    await p.edit_text(f"✅ Done: {n} - {m}")
                except Exception as e:
                    log.error(f"❌ runnow error: {e}")
                    await p.edit_text(f"❌ {str(e)[:200]}")
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
                await q.edit_message_text(msg("clear_q1"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("YES", callback_data=f"cd2_{profile_id}")], [InlineKeyboardButton("NO", callback_data=f"prof_{profile_id}")]]))
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
                await q.edit_message_text(msg("manual_send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"prof_{profile_id}")]]))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # fallback
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
    interval = prof["interval_min"]
    max_post = prof["max_post"]
    show_num = prof["show_numbers"] == 1
    custom_query = prof["custom_query"] or "خالی"
    show_date_cfg = prof["show_date_config"] == 1
    show_date_prx = prof["show_date_proxy"] == 1
    cron = prof["schedule_cron"] or "خالی"
    sponsor = get_sponsor(profile_id)
    sponsor_st = f"{sponsor['name']} ({'فعال' if sponsor['enabled'] else 'غیرفعال'})" if sponsor else "خالی"
    ping_mode = prof["ping_mode"]
    ping_display = "ایران" if ping_mode == "iran" else "جهانی"
    post_cfg = prof["post_configs"] == 1
    post_prx = prof["post_proxies"] == 1
    cfg_status = "✅" if post_cfg else "❌"
    prx_status = "✅" if post_prx else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"

    txt = msg(
        "admin_panel",
        srcs=len(srcs), dest=dest,
        name=dest, num=last_num,
        interval=interval, max_post=max_post,
        sponsor=sponsor_st,
        ping_mode=ping_display,
        cfg_status=cfg_status,
        prx_status=prx_status,
        numbers_status=num_status,
        custom_query=custom_query,
        date_cfg=date_cfg_status,
        date_prx=date_prx_status,
        cron=cron,
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
    if u.effective_user.id != ADMIN_ID:
        return

    # ---- بک‌آپ custom count ----
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

    # ---- ویرایش اسپانسر (فیلدهای name, url, text) ----
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
        set_sponsor(profile_id, name, url, btn_text, color)
        await u.message.reply_text(msg("sp_updated"), parse_mode="HTML")
        del ctx.user_data["sponsor_edit_field"]
        await show_profile_admin(u.message, profile_id)
        return

    # ---- مرحله رنگ در افزودن اسپانسر جدید (از طریق دکمه انجام می‌شود) ----
    if ctx.user_data.get("sponsor_step") == "color_wait":
        await u.message.reply_text("🎨 لطفاً رنگ را با دکمه‌های بالا انتخاب کنید.")
        return

    # ---- مراحل اسپانسر جدید ----
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
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
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
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
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
                    [InlineKeyboardButton("🔵 Primary (آبی)", callback_data=f"sp_setcolor_{profile_id}_primary")],
                    [InlineKeyboardButton("🟢 Success (سبز)", callback_data=f"sp_setcolor_{profile_id}_success")],
                    [InlineKeyboardButton("🔴 Danger (قرمز)", callback_data=f"sp_setcolor_{profile_id}_danger")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
                ])
            )
            return

    a = ctx.user_data.get("action")
    if not a:
        return

    t = u.message.text.strip()

    # ===== افزودن لیست سیاه =====
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

    # ===== تنظیم کرون =====
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

    # ===== بقیه اکشن‌ها =====
    if a == "prof_add":
        dest_name = t if t else None
        if not dest_name:
            await u.message.reply_text("❌ نام مقصد خالی است.")
            return
        if not dest_name.startswith("@") and not dest_name.isdigit():
            dest_name = "@" + dest_name
        profiles = get_profiles()
        if any(p["dest_name"] == dest_name for p in profiles):
            await u.message.reply_text("❌ این مقصد قبلاً وجود دارد.")
            return
        new_id = create_profile(dest_name)
        await u.message.reply_text(msg("profile_added", name=dest_name))
        del ctx.user_data["action"]
        if ENABLE_AUTO:
            app = u.get_bot()
            app.create_task(profile_loop(app, new_id))
            log.info(f"⏰ Started auto loop for new profile {new_id}")
        await show_profiles_list(u.message)
        return

    if a.startswith("sa_"):
        profile_id = int(a.split("_")[1])
        if not t:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', t)
        items = [x.strip() for x in items if x.strip()]
        srcs = get_profile_sources(profile_id)
        added = []
        for item in items:
            if not item.startswith("@"):
                item = "@" + item
            item = item.lower()
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
            if not dest.startswith("@") and not dest.isdigit():
                dest = "@" + dest
            set_profile_dest(profile_id, dest)
            await u.message.reply_text(msg("dest_set", dest=dest))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ac_"):
        profile_id = int(a.split("_")[1])
        name = t if t else ""
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

    if a.startswith("ai_"):
        profile_id = int(a.split("_")[1])
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
            update_profile(profile_id, interval_min=n)
            await u.message.reply_text(msg("interval_ok", n=n))
        else:
            return await u.message.reply_text(msg("interval_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("setmax_"):
        profile_id = int(a.split("_")[1])
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
            update_profile(profile_id, max_post=n)
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
        try:
            set_profile_custom_query(profile_id, query)
            saved = get_profile_custom_query(profile_id)
            if saved == query:
                await u.message.reply_text(msg("custom_query_set", query=query if query else "خالی"))
            else:
                await u.message.reply_text(f"❌ خطا در ذخیره‌سازی کوئری. مقدار فعلی: '{saved}'")
        except Exception as e:
            await u.message.reply_text(f"❌ خطا در ذخیره‌سازی کوئری: {str(e)}")
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

async def on_document(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return
    a = ctx.user_data.get("action")
    if not a:
        # اگر اکشنی نبود، اما فایل ارسال شد، به عنوان ارسال دستی در نظر بگیر
        # می‌توانیم اینجا هم پردازش کنیم
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=True)
        del ctx.user_data["action"]
        return

# ======================================================================
# ارسال دستی (اصلاح شده: همه کانفیگ‌ها را پست کن)
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
            # تلاش برای دیکد base64
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

        new_configs = config_links
        new_proxies = []
        for pl in proxy_links:
            norm = normalize_proxy_url(pl)
            if norm:
                new_proxies.append(norm)

        if not new_configs and not new_proxies:
            return await p.edit_text("❌ هیچ لینک معتبری برای ارسال وجود ندارد.")

        await p.edit_text(f"⏳ آماده‌سازی {len(new_configs)} کانفیگ و {len(new_proxies)} پروکسی...")

        # در حالت دستی، همه را بدون فیلتر تکراری ارسال کن
        working = [(url, 0, 0) for url in new_configs]
        proxy_with_ping = []
        for proxy_url in new_proxies:
            host, _ = extract_host(proxy_url)
            flag = "🌐"
            if host:
                ip = await host_to_ip(host)
                if ip:
                    flag = await get_flag_for_ip(ip)
            proxy_with_ping.append((proxy_url, 0, flag))

        if not working and not proxy_with_ping:
            return await p.edit_text("⚠️ هیچ لینک جدیدی برای ارسال وجود ندارد.")

        # ارسال هر کانفیگ به صورت جداگانه، بدون محدودیت max_post
        # از post_configs استفاده می‌کنیم ولی max_post را موقتاً بزرگ می‌کنیم
        # یا مستقیماً حلقه بزنیم
        dest = get_profile_dest(profile_id)
        if not dest:
            return await p.edit_text("❌ مقصد تنظیم نشده است.")

        # تنظیم موقت max_post به تعداد کانفیگ‌ها
        old_max = get_profile_max_post(profile_id)
        if len(working) > 0:
            update_profile(profile_id, max_post=len(working))
        try:
            n, m = await post_working_configs(u.get_bot(), profile_id, working, proxy_with_ping, source_for_seen="manual", force=True, skip_duplicate=True)
        finally:
            update_profile(profile_id, max_post=old_max)

        await p.edit_text(msg("doc_done", n=n, p=len(proxy_with_ping)))
    except Exception as e:
        log.error(f"manual send error: {e}")
        await p.edit_text(f"❌ {str(e)[:200]}")

# ======================================================================
# راه‌اندازی
# ======================================================================
ENABLE_AUTO = True
PRIVATE_MODE = True

async def post_init(app):
    global BOT_REF, BOT_LANG
    BOT_REF = app.bot
    saved = cfg_get("lang", "fa")
    if saved in ("fa", "en"):
        BOT_LANG = saved
    profiles = get_profiles()
    if not profiles:
        new_id = create_profile("@VaslZone", sources="@Cfox_Server")
        log.info(f"✅ Created default profile with id {new_id}.")
        profiles = get_profiles()
    log.info(f"✅ INIT done: {len(profiles)} profiles, AUTO={ENABLE_AUTO}, LANG={BOT_LANG}")

    # زمان‌بندی گزارش روزانه
    job_queue = app.job_queue
    if job_queue:
        now = datetime.now(TEHRAN_TZ)
        target = now.replace(hour=23, minute=59, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        seconds_until = (target - now).total_seconds()
        job_queue.run_once(send_daily_report, when=seconds_until, chat_id=ADMIN_ID)
        log.info(f"📅 Daily report scheduled for {target.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        log.warning("⚠️ JobQueue not available, daily report disabled.")

    # راه‌اندازی حلقه‌های خودکار
    if ENABLE_AUTO:
        for prof in profiles:
            app.create_task(profile_loop(app.bot, prof["id"]))
        log.info("⏰ Scheduler started for all profiles")

def cfg_get(k, default=""):
    r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
    return r[0] if r else default

def cfg_set(k, v):
    c.execute("INSERT OR REPLACE INTO cfg VALUES (?,?)", (k, str(v)))
    conn.commit()

def main():
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("runnow", cmd_runnow))
    app.add_handler(CommandHandler("runall", cmd_runall))
    app.add_handler(CommandHandler("sendtest", cmd_sendtest))
    app.add_handler(CommandHandler("diag", cmd_diag))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("✅ Bot is ready, polling...")
    app.run_polling()

if __name__ == "__main__":
    log.info("=" * 50)
    log.info("🚀 Starting bot...")
    main()
