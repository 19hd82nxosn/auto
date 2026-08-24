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
# مسیر پایدار
# ======================================================================
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")

# ======================================================================
# لاگ
# ======================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(DATA_DIR, "bot.log"), mode='w', encoding='utf-8')
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
# دیتابیس
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

# ---------- ایجاد جداول ----------
c.execute("""CREATE TABLE IF NOT EXISTS seen (
    uuid TEXT,
    address TEXT,
    source TEXT DEFAULT '',
    first_seen TEXT,
    last_posted TEXT,
    UNIQUE(uuid, address))""")
c.execute("""CREATE TABLE IF NOT EXISTS cfg (k TEXT PRIMARY KEY, v TEXT)""")
c.execute("""CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    count INTEGER,
    created_at TEXT)""")
ensure_column("seen", "source", "TEXT DEFAULT ''")
ensure_column("seen", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS country_cache (
    ip TEXT PRIMARY KEY,
    country TEXT,
    flag TEXT)""")
c.execute("""CREATE TABLE IF NOT EXISTS source_passwords (
    source TEXT,
    password TEXT,
    UNIQUE(source, password))""")
ensure_column("source_passwords", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    button_text TEXT DEFAULT 'Advertisement',
    color TEXT DEFAULT 'blue',
    enabled INTEGER DEFAULT 1,
    created_at TEXT,
    FOREIGN KEY(profile_id) REFERENCES profiles(id))""")
ensure_column("sponsors", "profile_id", "INTEGER")

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
    display_name TEXT DEFAULT '',
    show_numbers INTEGER DEFAULT 1,
    custom_query TEXT DEFAULT '',
    show_date_config INTEGER DEFAULT 1,
    show_date_proxy INTEGER DEFAULT 1,
    last_cycle_time TEXT DEFAULT '')""")
conn.commit()
ensure_column("profiles", "show_numbers", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "custom_query", "TEXT DEFAULT ''", "")
ensure_column("profiles", "show_date_config", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "show_date_proxy", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "last_cycle_time", "TEXT DEFAULT ''", "")

# ======================================================================
# مهاجرت
# ======================================================================
def migrate_old_config():
    existing = c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    if existing > 0:
        return

    def old_cfg(k, default=""):
        r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    old_dests = old_cfg("destinations", "@VaslZone")
    old_sources = old_cfg("sources", "@Cfox_Server,@v2rayHub1200,@V2Ray_Protocol")
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
             max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at, display_name,
             show_numbers, custom_query, show_date_config, show_date_proxy, last_cycle_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             datetime.now().isoformat(), "", 1, "", 1, 1, ""))
    conn.commit()
    log.info(f"✅ Migrated {len(dest_list)} profiles.")

migrate_old_config()

# ======================================================================
# توابع پروفایل
# ======================================================================
def get_profiles():
    rows = c.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    profiles = []
    for row in rows:
        profiles.append({
            "id": row[0],
            "dest_name": row[1],
            "sources": row[2],
            "banner_config": row[3],
            "banner_proxy": row[4],
            "interval_min": row[5],
            "max_post": row[6],
            "max_proxies": row[7],
            "post_configs": row[8],
            "post_proxies": row[9],
            "ping_mode": row[10],
            "last_num": row[11],
            "created_at": row[12],
            "display_name": row[13] if len(row) > 13 else "",
            "show_numbers": row[14] if len(row) > 14 else 1,
            "custom_query": row[15] if len(row) > 15 else "",
            "show_date_config": row[16] if len(row) > 16 else 1,
            "show_date_proxy": row[17] if len(row) > 17 else 1,
            "last_cycle_time": row[18] if len(row) > 18 else ""
        })
    return profiles

def get_profile(profile_id):
    row = c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "dest_name": row[1],
        "sources": row[2],
        "banner_config": row[3],
        "banner_proxy": row[4],
        "interval_min": row[5],
        "max_post": row[6],
        "max_proxies": row[7],
        "post_configs": row[8],
        "post_proxies": row[9],
        "ping_mode": row[10],
        "last_num": row[11],
        "created_at": row[12],
        "display_name": row[13] if len(row) > 13 else "",
        "show_numbers": row[14] if len(row) > 14 else 1,
        "custom_query": row[15] if len(row) > 15 else "",
        "show_date_config": row[16] if len(row) > 16 else 1,
        "show_date_proxy": row[17] if len(row) > 17 else 1,
        "last_cycle_time": row[18] if len(row) > 18 else ""
    }

def create_profile(dest_name, sources="", banner_config=None, banner_proxy=None,
                   interval_min=5, max_post=8, max_proxies=10,
                   post_configs=1, post_proxies=1, ping_mode="iran", last_num=0,
                   display_name="", show_numbers=1, custom_query="",
                   show_date_config=1, show_date_proxy=1):
    if not banner_config:
        banner_config = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    if not banner_proxy:
        banner_proxy = "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━"
    c.execute("""INSERT INTO profiles
        (dest_name, sources, banner_config, banner_proxy, interval_min,
         max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at, display_name,
         show_numbers, custom_query, show_date_config, show_date_proxy, last_cycle_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, last_num,
         get_tehran_time(), display_name, show_numbers, custom_query,
         show_date_config, show_date_proxy, ""))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "last_num", "display_name",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy",
               "last_cycle_time"]
    for key, value in kwargs.items():
        if key in allowed:
            c.execute(f"UPDATE profiles SET {key}=? WHERE id=?", (value, profile_id))
    conn.commit()

def delete_profile(profile_id):
    c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM source_passwords WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM last_scrape WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM processed_messages WHERE profile_id=?", (profile_id,))
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

def get_profile_display_name(profile_id):
    prof = get_profile(profile_id)
    return prof["display_name"] if prof else ""

def set_profile_display_name(profile_id, name):
    update_profile(profile_id, display_name=name)

def get_profile_show_numbers(profile_id):
    prof = get_profile(profile_id)
    return prof["show_numbers"] == 1 if prof else True

def set_profile_show_numbers(profile_id, enabled):
    update_profile(profile_id, show_numbers=1 if enabled else 0)

def get_profile_custom_query(profile_id):
    prof = get_profile(profile_id)
    return prof["custom_query"] if prof else ""

def set_profile_custom_query(profile_id, query):
    update_profile(profile_id, custom_query=query)

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

def get_profile_last_cycle_time(profile_id):
    prof = get_profile(profile_id)
    return prof["last_cycle_time"] if prof else ""

def set_profile_last_cycle_time(profile_id, time_str):
    update_profile(profile_id, last_cycle_time=time_str)

# ======================================================================
# رمزها
# ======================================================================
def get_pw(profile_id, source):
    return [r[0] for r in c.execute(
        "SELECT password FROM source_passwords WHERE source=? AND profile_id=?",
        (source.lower(), profile_id)).fetchall()]

def add_pw(profile_id, source, password):
    c.execute("INSERT OR IGNORE INTO source_passwords (source, password, profile_id) VALUES (?,?,?)",
              (source.lower(), password, profile_id))
    conn.commit()

def del_pw(profile_id, source, password):
    c.execute("DELETE FROM source_passwords WHERE source=? AND password=? AND profile_id=?",
              (source.lower(), password, profile_id))
    conn.commit()

# ======================================================================
# اسپانسرها
# ======================================================================
def add_sponsor(profile_id, name, url, button_text="Advertisement", color="blue"):
    c.execute("""INSERT INTO sponsors
        (profile_id, name, url, button_text, color, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, 1, ?)""",
        (profile_id, name, url, button_text, color, get_tehran_time()))
    conn.commit()

def remove_sponsor(sid):
    c.execute("DELETE FROM sponsors WHERE id=?", (sid,))
    conn.commit()

def toggle_sponsor(sid):
    c.execute(
        "UPDATE sponsors SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?",
        (sid,))
    conn.commit()

def update_sponsor(sid, name=None, url=None, button_text=None, color=None):
    if name is not None:
        c.execute("UPDATE sponsors SET name=? WHERE id=?", (name, sid))
    if url is not None:
        c.execute("UPDATE sponsors SET url=? WHERE id=?", (url, sid))
    if button_text is not None:
        c.execute("UPDATE sponsors SET button_text=? WHERE id=?", (button_text, sid))
    if color is not None:
        c.execute("UPDATE sponsors SET color=? WHERE id=?", (color, sid))
    conn.commit()

def get_enabled_sponsors(profile_id):
    return c.execute(
        "SELECT id, name, url, button_text, color FROM sponsors WHERE enabled=1 AND profile_id=?",
        (profile_id,)
    ).fetchall()

def get_all_sponsors(profile_id):
    return c.execute(
        "SELECT id, name, url, button_text, color, enabled FROM sponsors WHERE profile_id=? ORDER BY id DESC",
        (profile_id,)
    ).fetchall()

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
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        return False
    return c.execute(
        "SELECT 1 FROM seen WHERE uuid=? AND address=? AND profile_id=?",
        (uid, host, profile_id)).fetchone() is not None

def mark_as_posted(profile_id, url, source=""):
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        return
    now = get_tehran_time()
    c.execute(
        "INSERT INTO seen (uuid,address,source,first_seen,last_posted,profile_id) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(uuid,address) DO UPDATE SET "
        "last_posted=excluded.last_posted",
        (uid, host, source, now, now, profile_id))
    conn.commit()

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

def change_link_display_name(url, new_name, flag, custom_query=""):
    protocol = url.split('://')[0].lower() if '://' in url else ''
    if custom_query and protocol != 'vmess':
        url = add_custom_query_to_url(url, custom_query, protocol)
    base = strip_url_fragment(url)
    fragment = f"{new_name} {flag}"
    encoded = quote(fragment, safe='')
    return f"{base}#{encoded}"

def append_channel_and_flag_encoded(url, channel, flag, custom_query=""):
    protocol = url.split('://')[0].lower() if '://' in url else ''
    if custom_query and protocol != 'vmess':
        url = add_custom_query_to_url(url, custom_query, protocol)
    base = strip_url_fragment(url)
    fragment = f"{channel} {flag}"
    encoded = quote(fragment, safe='')
    return f"{base}#{encoded}"

# ======================================================================
# پینگ (سریع)
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
    log.info(f"🔍 Ping target: {target} (host: {host}, port: {port})")

    try:
        async with httpx.AsyncClient(timeout=8) as cl:
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
# اسکرپ (فقط جدید)
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
    for pw in passwords:
        if not pw:
            continue
        try:
            raw = base64.b64decode(data + b'=' * (-len(data) % 4))
            kiv = hashlib.md5(pw.encode('utf-8')).digest()
            try:
                from Crypto.Cipher import AES
                from Crypto.Util.Padding import unpad
                text = unpad(AES.new(kiv, AES.MODE_CBC, kiv).decrypt(raw), 16) \
                    .decode('utf-8', errors='ignore')
                if text and any(p in text for p in protocols):
                    return text
            except ImportError:
                pass
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
        messages = await bot.get_chat_history(chat_id, limit=10)
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

            pws = get_pw(profile_id, source)
            text = decrypt_subscription(bytes(data), pws)
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

async def scrape_channel_with_retry(profile_id, channel, only_new=True, max_retries=1):
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
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as cl:
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

async def post_configs(bot, profile_id, working, source_for_seen=""):
    if not working:
        return 0, None

    max_post = get_profile_max_post(profile_id)
    items = sorted(working, key=lambda x: x[1])[:max_post]
    last_n = get_profile_last_num(profile_id)
    show_numbers = get_profile_show_numbers(profile_id)
    custom_query = get_profile_custom_query(profile_id)
    show_date = get_profile_show_date_config(profile_id)
    dest = get_profile_dest(profile_id)
    configs_text = ""
    all_buttons = []

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
        configs_text += header + "\n" + block + "\n"
        mark_as_posted(profile_id, modified_url, source_for_seen)

    if items:
        set_profile_last_num(profile_id, last_n + len(items))
        config_count = len(items)
    else:
        return 0, None

    sponsors = get_enabled_sponsors(profile_id)
    if sponsors:
        sid, sname, surl, stxt, scolor = sponsors[0]
        clean_txt = stxt.replace('★', '').replace('☆', '').strip()
        style_map = {"blue": "primary", "green": "success", "red": "danger", "yellow": "primary", "purple": "primary"}
        style = style_map.get(scolor, "primary")
        all_buttons.append([InlineKeyboardButton(clean_txt, url=surl, style=style)])

    banner_config = get_profile_banner_config(profile_id)
    configs_text = configs_text.rstrip()
    try:
        text = banner_config.format(configs=configs_text)
    except KeyError:
        log.error("❌ Banner config missing {configs} placeholder, using default")
        default_banner = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
        text = default_banner.format(configs=configs_text)

    return config_count, (text, all_buttons if all_buttons else None)

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
    return proxy_count, (text, None)

async def post_working_configs(bot, profile_id, working, proxies_with_ping, source_for_seen="", force=False):
    dest = get_profile_dest(profile_id)
    if not dest:
        return 0, "❌ هیچ مقصدی تنظیم نشده!"

    post_configs_enabled = get_profile_post_configs(profile_id) if not force else True
    post_proxies_enabled = get_profile_post_proxies(profile_id) if not force else True

    total_configs = 0
    total_proxies = 0
    results = []

    if post_configs_enabled and working:
        max_post = get_profile_max_post(profile_id)
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
            for i in range(0, len(unique_working), max_post):
                chunk = unique_working[i:i+max_post]
                config_count, config_payload = await post_configs(bot, profile_id, chunk, source_for_seen)
                if config_count > 0 and config_payload:
                    text, buttons = config_payload
                    sent = await send_to_destination(bot, profile_id, text, buttons)
                    if sent:
                        total_configs += config_count
                        results.append(f"{config_count} configs")
                    else:
                        plain_text = re.sub(r'<[^>]+>', '', text)
                        sent = await send_to_destination(bot, profile_id, plain_text, None)
                        if sent:
                            total_configs += config_count
                            results.append(f"{config_count} configs (plain)")

    if post_proxies_enabled and proxies_with_ping:
        valid_proxies = [p for p in proxies_with_ping if "t.me/proxy" in p[0].lower()]
        if valid_proxies:
            unique_proxies = []
            for p in valid_proxies:
                if not is_proxy_posted(profile_id, p[0]):
                    unique_proxies.append(p)
            if unique_proxies:
                proxy_count, proxy_payload = await post_proxies(bot, profile_id, unique_proxies)
                if proxy_count > 0 and proxy_payload:
                    text, _ = proxy_payload
                    sent = await send_to_destination(bot, profile_id, text, None)
                    if sent:
                        total_proxies = proxy_count
                        results.append(f"{proxy_count} proxies")

    if not results:
        return 0, "no new content to send"

    result_msg = "posted " + " and ".join(results)
    return total_configs, result_msg

# ======================================================================
# سیکل کامل (فقط جدید و سریع)
# ======================================================================
async def run_full_cycle_for_profile(bot, profile_id, only_new=True):
    log.info("=" * 50)
    log.info(f"🔄 run_full_cycle for profile {profile_id} STARTED")

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

    for src in sources:
        log.info(f"🔍 Processing source: {src}")
        config_links, proxy_links = await scrape_channel_with_retry(profile_id, src, only_new=True)
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

        file_links = await fetch_files_from_channel(bot, profile_id, src, src)
        log.info(f"  {src}: {len(file_links)} links from files")
        for link in file_links:
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
    if new_configs:
        to_test = new_configs[:10]
        log.info(f"📊 Testing {len(to_test)} configs...")
        sem = asyncio.Semaphore(10)
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
            log.info(f"📊 Testing {len(valid_proxies)} proxies...")
            sem = asyncio.Semaphore(10)
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
    result = await post_working_configs(bot, profile_id, working, proxy_with_ping)
    log.info(f"✅ Cycle result for profile {profile_id}: {result}")
    log.info("=" * 50)

    # ذخیره زمان آخرین سیکل
    set_profile_last_cycle_time(profile_id, get_tehran_time())
    return result

# ======================================================================
# حلقه خودکار (دقیق بر اساس زمان)
# ======================================================================
async def profile_loop(bot, profile_id):
    while True:
        try:
            profile = get_profile(profile_id)
            if not profile:
                log.error(f"❌ Profile {profile_id} not found, stopping loop.")
                break
            interval = profile["interval_min"]
            last_cycle = profile.get("last_cycle_time") or ""

            # اگر زمان آخرین سیکل ثبت شده است، محاسبه می‌کنیم که چه مدت گذشته
            if last_cycle:
                try:
                    last_dt = datetime.fromisoformat(last_cycle)
                    now = datetime.now()
                    diff_seconds = (now - last_dt).total_seconds()
                    if diff_seconds >= interval * 60:
                        # زمان گذشته است، بلافاصله اجرا کن
                        log.info(f"⏰ Auto trigger for profile {profile_id} (interval passed)")
                        await run_full_cycle_for_profile(bot, profile_id, only_new=True)
                        continue
                    else:
                        # منتظر بمان تا زمان باقی‌مانده تمام شود
                        sleep_seconds = (interval * 60) - diff_seconds
                        await asyncio.sleep(sleep_seconds)
                except:
                    # اگر خطا در parse زمان بود، fallback
                    await asyncio.sleep(interval * 60)
            else:
                # اولین بار است، بعد از interval دقیقه اجرا کن
                await asyncio.sleep(interval * 60)

            log.info(f"⏰ AUTO TICK for profile {profile_id} ({profile['dest_name']})")
            await run_full_cycle_for_profile(bot, profile_id, only_new=True)

        except asyncio.CancelledError:
            log.info(f"🛑 Profile loop {profile_id} cancelled.")
            break
        except Exception as e:
            log.error(f"❌ profile_loop error for {profile_id}: {e}")
            log.error(traceback.format_exc())
            await asyncio.sleep(60)

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
                       "📝 نام نمایشی: {display_name}\n"
                       "🔢 شماره‌گذاری: {numbers_status}\n"
                       "🔗 کوئری سفارشی: {custom_query}\n"
                       "📅 تاریخ کانفیگ: {date_cfg}\n"
                       "📅 تاریخ پروکسی: {date_prx}\n"
                       "⏳ آخرین اجرا: {last_cycle}",
        "btn_back": "🔙 برگشت",
        "btn_add_source": "➕ منبع",
        "btn_add_dest": "➕ مقصد جدید",
        "btn_dest_list": "📋 مقصدها",
        "btn_sponsors": "📢 اسپانسر",
        "btn_pw": "🔑 رمزها",
        "btn_set_dest": "🎯 تنظیم مقصد",
        "btn_set_name": "🎨 نام",
        "btn_set_display_name": "📝 نام نمایشی (تغییر نام سرور)",
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
        "btn_manual_send": "📤 ارسال دستی",
        "btn_rename_server": "📝 تغییر نام سرور",
        "btn_manage_sources": "📡 مدیریت منابع",
        "btn_toggle_numbers": "🔢 شماره‌گذاری: {status}",
        "btn_set_custom_query": "🔗 تنظیم کوئری سفارشی",
        "btn_empty": "🗑 خالی",
        "send_prompt": "📝 نام کانال (با @ یا بدون):",
        "added": "✅ {item}",
        "removed": "✅ حذف شد",
        "test_ok": "✅ به {dest} ارسال شد",
        "test_err": "❌ خطا:\n<code>{err}</code>",
        "no_pings": "❌ پینگ نداد",
        "clear_q1": "⚠️ پاک کنم؟ (۱/۲)\n⛔ غیرقابل برگشت",
        "dest_set": "✅ مقصد: {dest}",
        "name_set": "✅ نام: {name}",
        "display_name_set": "✅ نام نمایشی: {name}",
        "banner_ok": "✅ بنر ذخیره شد",
        "banner_err": "❌ باید {configs} یا {proxies} داشته باشه",
        "interval_ok": "✅ هر {n} دقیقه",
        "interval_err": "❌ ۱ تا ۱۴۴۰ دقیقه",
        "interval_wrong": "❌ فقط عدد",
        "max_ok": "✅ حداکثر {n}",
        "max_err": "❌ ۱ تا ۵۰",
        "pw_added": "✅ رمز برای {source}",
        "pw_prompt": "🔐 رمز جدید رو بفرست (چند تا با کاما یا خط جدید):",
        "pw_title": "🔐 رمزهای {source}:",
        "pw_none": "(خالی)",
        "src_title": "📡 منابع ({n}):",
        "src_none": "خالی",
        "reset_ok": "✅ ریست شد (#۱)",
        "lang_ok": "✅ فارسی شد",
        "sp_added": "✅ '{name}' اضافه شد",
        "sp_removed": "✅ حذف شد",
        "sp_title": "📢 اسپانسرها:",
        "sp_none": "خالی",
        "sp_err": "❌ فرمت: name|url|text|color (رنگ‌ها: blue, green, red)",
        "pw_common_added": "✅ +۳ رمز رایج",
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
        "profile_deleted": "✅ پروفایل حذف شد.",
        "profile_not_found": "❌ پروفایل یافت نشد.",
        "manual_send_prompt": "📤 لطفاً پیام (متن یا فایل) حاوی لینک‌های کانفیگ/پروکسی را ارسال کنید.\n\n⏳ بات به‌طور خودکار تشخیص داده و با بنر مناسب ارسال می‌کند.\n\n⚠️ **توجه:** در حالت دستی، تست پینگ انجام نمی‌شود و همه لینک‌ها حتی اگر قبلاً پست شده باشند، دوباره ارسال می‌شوند.",
        "manual_send_cancel": "❌ ارسال دستی لغو شد.",
        "manual_send_processing": "⏳ در حال پردازش...",
        "manual_send_done": "✅ ارسال دستی کامل شد.",
        "rename_prompt": "📝 پیام یا فایل حاوی لینک‌های کانفیگ را بفرستید.\n\nمن نام آن‌ها را با نام نمایشی تنظیم‌شده تغییر داده و فقط برای شما (ادمین) ارسال می‌کنم.",
        "custom_query_set": "✅ کوئری سفارشی تنظیم شد: {query}",
        "custom_query_prompt": "🔗 کوئری سفارشی را وارد کنید (مثلا Telegram=@MyChannel) یا خالی بفرستید برای حذف:",
        "source_list": "📡 **منابع پروفایل {name}**\n\n{sources}\n\nبرای حذف هر کدام روی دکمه مربوطه کلیک کنید.",
        "source_deleted": "✅ منبع حذف شد.",
        "toggle_numbers_ok": "✅ شماره‌گذاری {'فعال' if status else 'غیرفعال'} شد.",
        "date_cfg_toggle": "✅ نمایش تاریخ در بنر کانفیگ {'فعال' if status else 'غیرفعال'} شد.",
        "date_prx_toggle": "✅ نمایش تاریخ در بنر پروکسی {'فعال' if status else 'غیرفعال'} شد.",
        "sp_edit_prompt": "📢 **ویرایش اسپانسر**\n\nنام: {name}\nلینک: {url}\nمتن: {text}\nرنگ: {color}",
        "sp_edit_name": "نام جدید (خالی برای عدم تغییر، یا دکمه خالی):",
        "sp_edit_url": "لینک جدید (خالی برای عدم تغییر، یا دکمه خالی):",
        "sp_edit_text": "متن جدید دکمه (خالی برای عدم تغییر، یا دکمه خالی):",
        "sp_edit_color": "رنگ جدید (blue/green/red) - خالی برای عدم تغییر:",
        "sp_updated": "✅ اسپانسر به‌روزرسانی شد.",
        "btn_edit_sponsor": "✏️ ویرایش",
        "last_cycle": "⏳ آخرین اجرا: {time}",
        "empty_value": "🗑 خالی",
    },
    "en": {
        "welcome": "🤖 **Config & Proxy Aggregator**\n\n"
                  "📡 Profiles: {profiles}\n"
                  "🔢 Next #: {next_n}\n"
                  "⏰ intervals vary",
        "private": "🔒 Private.",
        "admin_panel": "🔐 **Profile Admin Panel**\n\n"
                       "📡 Sources: {srcs} | 🎯 Destination: {dest}\n"
                       "🎨 Name: {name} | 🔢 #{num}\n"
                       "⏰ {interval}m | 🎯 max:{max_post}\n"
                       "📢 Sponsor: {sponsor}\n"
                       "🌍 Ping mode: {ping_mode}\n"
                       "📡 Configs: {cfg_status} | 🌐 Proxies: {prx_status}\n"
                       "📝 Display Name: {display_name}\n"
                       "🔢 Numbering: {numbers_status}\n"
                       "🔗 Custom Query: {custom_query}\n"
                       "📅 Config Date: {date_cfg}\n"
                       "📅 Proxy Date: {date_prx}\n"
                       "⏳ Last cycle: {last_cycle}",
        "btn_back": "🔙 Back",
        "btn_add_source": "➕ Source",
        "btn_add_dest": "➕ Add Destination",
        "btn_dest_list": "📋 Destinations",
        "btn_sponsors": "📢 Sponsors",
        "btn_pw": "🔑 Passwords",
        "btn_set_dest": "🎯 Set Destination",
        "btn_set_name": "🎨 Name",
        "btn_set_display_name": "📝 Display Name (Rename Server)",
        "btn_set_banner": "📝 Banner",
        "btn_set_banner_config": "📝 Config Banner",
        "btn_set_banner_proxy": "📝 Proxy Banner",
        "btn_set_time": "⏰ Interval",
        "btn_set_max": "🎯 Max Post",
        "btn_private": "🔒 Private",
        "btn_stats": "📊 Stats",
        "btn_test": "🧪 Test",
        "btn_clear": "🗑 Clear",
        "btn_reset": "🔢 Reset Num",
        "btn_lang": "🌐 فارسی",
        "btn_ping_mode": "🌍 Iran-Only",
        "btn_runnow": "▶️ Run Now",
        "btn_manual_send": "📤 Manual Send",
        "btn_rename_server": "📝 Rename Server",
        "btn_manage_sources": "📡 Manage Sources",
        "btn_toggle_numbers": "🔢 Numbering: {status}",
        "btn_set_custom_query": "🔗 Set Custom Query",
        "btn_empty": "🗑 Empty",
        "send_prompt": "📝 Channel name (with/without @):",
        "added": "✅ {item}",
        "removed": "✅ Removed",
        "test_ok": "✅ Sent to {dest}",
        "test_err": "❌ ERR:\n<code>{err}</code>",
        "no_pings": "❌ No ping",
        "clear_q1": "⚠️ Clear DB? (1/2)",
        "dest_set": "✅ Destination: {dest}",
        "name_set": "✅ Name: {name}",
        "display_name_set": "✅ Display name set to {name}",
        "banner_ok": "✅ Banner saved",
        "banner_err": "❌ must include {configs} or {proxies}",
        "interval_ok": "✅ every {n} min",
        "interval_err": "❌ 1-1440 min",
        "interval_wrong": "❌ number only",
        "max_ok": "✅ Max {n}",
        "max_err": "❌ 1-50",
        "pw_added": "✅ PW for {source}",
        "pw_prompt": "🔐 Send new password (multiple with comma or newline):",
        "pw_title": "🔐 Passwords for {source}:",
        "pw_none": "(none)",
        "src_title": "📡 Sources ({n}):",
        "src_none": "none",
        "reset_ok": "✅ Reset (#1)",
        "lang_ok": "✅ Switched to English",
        "sp_added": "✅ '{name}' added",
        "sp_removed": "✅ Removed",
        "sp_title": "📢 Sponsors:",
        "sp_none": "none",
        "sp_err": "❌ Bad format: name|url|text|color (colors: blue, green, red)",
        "pw_common_added": "✅ +3 common PW",
        "doc_select": "Which source is this from?",
        "doc_no_src": "❌ No sources. Add first",
        "doc_decoding": "🔐 Decrypting...",
        "doc_no_pw": "❌ No password matched",
        "doc_no_links": "❌ No links",
        "doc_done": "🎉 {n} configs and {p} proxies posted",
        "doc_dup": "all duplicates",
        "no_sources": "❌ No sources configured",
        "test_link_prompt": "🔗 Send config link (e.g. vless:// or vmess://)",
        "btn_toggle_configs": "📡 Configs: {status}",
        "btn_toggle_proxies": "🌐 Proxies: {status}",
        "toggle_configs": "✅ Config posting {'enabled' if status else 'disabled'}",
        "toggle_proxies": "✅ Proxy posting {'enabled' if status else 'disabled'}",
        "profile_list": "📋 **Profiles**\n\n{list}\n\nClick to manage.",
        "profile_add_prompt": "📝 Enter new destination name (with/without @):",
        "profile_added": "✅ Profile '{name}' created.",
        "profile_deleted": "✅ Profile deleted.",
        "profile_not_found": "❌ Profile not found.",
        "manual_send_prompt": "📤 Please send a message (text or file) containing config/proxy links.\n\n⏳ Bot will detect and send with appropriate banners.\n\n⚠️ **Note:** In manual mode, no ping test is performed and all links will be posted even if they were posted before.",
        "manual_send_cancel": "❌ Manual send cancelled.",
        "manual_send_processing": "⏳ Processing...",
        "manual_send_done": "✅ Manual send completed.",
        "rename_prompt": "📝 Send a message or file containing config links.\n\nI will rename them using the display name you set and send only to you (admin).",
        "custom_query_set": "✅ Custom query set: {query}",
        "custom_query_prompt": "🔗 Enter custom query (e.g. Telegram=@MyChannel) or leave empty to remove:",
        "source_list": "📡 **Sources for profile {name}**\n\n{sources}\n\nClick delete to remove.",
        "source_deleted": "✅ Source deleted.",
        "toggle_numbers_ok": "✅ Numbering {'enabled' if status else 'disabled'}.",
        "date_cfg_toggle": "✅ Config date display {'enabled' if status else 'disabled'}.",
        "date_prx_toggle": "✅ Proxy date display {'enabled' if status else 'disabled'}.",
        "sp_edit_prompt": "📢 **Edit Sponsor**\n\nName: {name}\nURL: {url}\nText: {text}\nColor: {color}",
        "sp_edit_name": "New name (leave empty to keep, or use Empty button):",
        "sp_edit_url": "New URL (leave empty to keep, or use Empty button):",
        "sp_edit_text": "New button text (leave empty to keep, or use Empty button):",
        "sp_edit_color": "New color (blue/green/red - leave empty to keep):",
        "sp_updated": "✅ Sponsor updated.",
        "btn_edit_sponsor": "✏️ Edit",
        "last_cycle": "⏳ Last cycle: {time}",
        "empty_value": "🗑 Empty",
    },
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
        btns.append([InlineKeyboardButton(f"{p['dest_name']} (ID:{p['id']})", callback_data=f"prof_{p['id']}", style="primary")])
    btns.append([InlineKeyboardButton("➕ Add Profile", callback_data="prof_add", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data="back_home", style="primary")])
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

    cfg_btn = msg("btn_toggle_configs", status=cfg_status)
    prx_btn = msg("btn_toggle_proxies", status=prx_status)
    num_btn = msg("btn_toggle_numbers", status=num_status)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_sources"), callback_data=f"src_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success")],
        [InlineKeyboardButton(msg("btn_dest_list"), callback_data=f"dl_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_sponsors"), callback_data=f"sp_menu_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_pw"), callback_data=f"pw_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_name"), callback_data=f"ac_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_display_name"), callback_data=f"adn_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_rename_server"), callback_data=f"rename_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_banner_config"), callback_data=f"ab_config_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_banner_proxy"), callback_data=f"ab_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_time"), callback_data=f"ai_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_max"), callback_data=f"setmax_{profile_id}", style="primary")],
        [InlineKeyboardButton(ping_label, callback_data=f"tglping_{profile_id}", style="primary"),
         InlineKeyboardButton(cfg_btn, callback_data=f"tglcfg_{profile_id}", style="primary")],
        [InlineKeyboardButton(prx_btn, callback_data=f"tglproxy_{profile_id}", style="primary"),
         InlineKeyboardButton(num_btn, callback_data=f"togglenum_{profile_id}", style="primary")],
        [InlineKeyboardButton(f"📅 تاریخ کانفیگ: {date_cfg_status}", callback_data=f"tgl_date_cfg_{profile_id}", style="primary"),
         InlineKeyboardButton(f"📅 تاریخ پروکسی: {date_prx_status}", callback_data=f"tgl_date_prx_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_custom_query"), callback_data=f"setquery_{profile_id}", style="primary"),
         InlineKeyboardButton("🧹 پاک کردن کوئری", callback_data=f"clearquery_{profile_id}", style="danger")],
        [InlineKeyboardButton(msg("btn_stats"), callback_data=f"ast_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_test"), callback_data=f"sendtest_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_runnow"), callback_data=f"runnow_{profile_id}", style="success"),
         InlineKeyboardButton(msg("btn_manual_send"), callback_data=f"manual_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_reset"), callback_data=f"rn_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_clear"), callback_data=f"cd1_{profile_id}", style="danger")],
        [InlineKeyboardButton("❌ Delete Profile", callback_data=f"delprof_{profile_id}", style="danger"),
         InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list", style="primary")],
    ])

def sources_kb(profile_id):
    btns = []
    sources = get_profile_sources(profile_id)
    for i, s in enumerate(sources):
        btns.append([InlineKeyboardButton(f"❌ {s}", callback_data=f"sd_{profile_id}_{i}", style="danger")])
    btns.append([
        InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success"),
        InlineKeyboardButton(msg("btn_pw"), callback_data=f"pw_list_{profile_id}", style="primary"),
    ])
    btns.append([InlineKeyboardButton(msg("btn_sponsors"), callback_data=f"sp_menu_{profile_id}", style="primary")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

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

def source_passwords_kb(profile_id, idx):
    sources = get_profile_sources(profile_id)
    if idx >= len(sources):
        return None
    src = sources[idx]
    btns = []
    for p in get_pw(profile_id, src):
        btns.append([
            InlineKeyboardButton(f"❌ {p}", callback_data=f"pr_{profile_id}_{idx}|{p}", style="danger")
        ])
    btns.append([
        InlineKeyboardButton(msg("btn_pw"), callback_data=f"pa_{profile_id}_{idx}", style="primary"),
        InlineKeyboardButton("🎯 +3 common", callback_data=f"sauto_{profile_id}_{idx}", style="success"),
    ])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"pw_list_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def sponsors_kb(profile_id):
    rows = get_all_sponsors(profile_id)
    btns = []
    if rows:
        for row in rows:
            sid, name, url, btn_text, color, enabled = row
            st = "✅" if enabled else "❌"
            style_map = {"blue": "primary", "green": "success", "red": "danger", "yellow": "primary", "purple": "primary"}
            style = style_map.get(color, "primary")
            btns.append([
                InlineKeyboardButton(f"{st} {name[:25]}", callback_data=f"spt_{profile_id}_{sid}", style=style),
                InlineKeyboardButton("🗑", callback_data=f"spd_{profile_id}_{sid}", style="danger"),
            ])
            btns.append([
                InlineKeyboardButton(msg("btn_edit_sponsor"), callback_data=f"sp_edit_{profile_id}_{sid}", style="primary")
            ])
    btns.append([InlineKeyboardButton("➕ add sponsor", callback_data=f"sp_add_{profile_id}", style="success")])
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

def empty_kb(callback_back):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_empty"), callback_data="empty_value", style="secondary")],
        [InlineKeyboardButton(msg("btn_back"), callback_data=callback_back, style="primary")]
    ])

# ======================================================================
# دستورات
# ======================================================================
async def cmd_start(u, ctx):
    if PRIVATE_MODE and u.effective_user.id != ADMIN_ID:
        return await u.message.reply_text(msg("private"))
    profiles = get_profiles()
    total = len(profiles)
    next_n = 0
    if profiles:
        last_num = max(p["last_num"] for p in profiles)
        next_n = last_num + 1
    txt = msg("welcome", profiles=total, next_n=next_n)
    btns = [[InlineKeyboardButton("📋 Manage Profiles", callback_data="profiles_list", style="primary")]]
    if u.effective_user.id == ADMIN_ID:
        btns.append([InlineKeyboardButton("🔐 Admin", callback_data="profiles_list", style="primary")])
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
            n, m = await run_full_cycle_for_profile(u.get_bot(), prof['id'], only_new=True)
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
            n, m = await run_full_cycle_for_profile(u.get_bot(), prof['id'], only_new=False)
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
    pw_count = c.execute("SELECT COUNT(*) FROM source_passwords").fetchone()[0]
    msg_lines.append("💾 **دیتابیس:**")
    msg_lines.append(f"• کانفیگ‌های دیده‌شده: {seen_cfg}")
    msg_lines.append(f"• پروکسی‌های دیده‌شده: {seen_prx}")
    msg_lines.append(f"• رمزهای ذخیره‌شده: {pw_count}")
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

        # دکمه خالی
        if d == "empty_value":
            # اینجا باید مقدار خالی را به action جاری بفرستیم
            ctx.user_data["empty_value"] = True
            await q.answer("مقدار خالی شد")
            # ادامه در on_text با بررسی empty_value
            return

        if d == "profiles_list":
            await show_profiles_list(q.message)
            return

        if d == "prof_add":
            ctx.user_data["action"] = "prof_add"
            await q.edit_message_text(msg("profile_add_prompt") + "\n\n" + "می‌توانید از دکمه خالی استفاده کنید.",
                                      parse_mode="HTML",
                                      reply_markup=empty_kb("profiles_list"))
            return

        if d == "back_home":
            await show_profiles_list(q.message)
            return

        if d.startswith("prof_"):
            profile_id = int(d.split("_")[1])
            prof = get_profile(profile_id)
            if not prof:
                await q.edit_message_text(msg("profile_not_found"))
                return
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("delprof_"):
            profile_id = int(d.split("_")[1])
            delete_profile(profile_id)
            await q.answer(msg("profile_deleted"))
            await show_profiles_list(q.message)
            return

        import re as regex
        match = regex.search(r'(\d+)', d)
        if not match:
            await q.answer("⚠️ خطا: شناسه پیدا نشد")
            await show_profiles_list(q.message)
            return
        profile_id = int(match.group(1))

        # ---------- اسپانسرها ----------
        if d.startswith("sp_menu_"):
            await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
            return

        if d.startswith("sp_add_"):
            ctx.user_data["sponsor_step"] = "name"
            ctx.user_data["sponsor_profile_id"] = profile_id
            await q.edit_message_text(
                "📝 **نام اسپانسر را وارد کنید:**\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=empty_kb(f"sp_menu_{profile_id}")
            )
            return

        if d.startswith("sp_color_"):
            parts = d.split("_")
            color = parts[3]
            name = ctx.user_data.get("sponsor_name")
            url = ctx.user_data.get("sponsor_url")
            btn_text = ctx.user_data.get("sponsor_button_text")
            if name and url and btn_text:
                add_sponsor(profile_id, name, url, btn_text, color)
                await q.answer("✅ اسپانسر اضافه شد.")
                await q.edit_message_text(
                    f"✅ اسپانسر **{name}** با موفقیت اضافه شد.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 بازگشت به اسپانسرها", callback_data=f"sp_menu_{profile_id}", style="primary")]
                    ])
                )
                del ctx.user_data["sponsor_step"]
                del ctx.user_data["sponsor_name"]
                del ctx.user_data["sponsor_url"]
                del ctx.user_data["sponsor_button_text"]
            else:
                await q.answer("❌ اطلاعات ناقص است.")
            return

        if d.startswith("sp_edit_"):
            parts = d.split("_")
            sid = int(parts[2])
            sponsor = c.execute("SELECT id, name, url, button_text, color FROM sponsors WHERE id=?", (sid,)).fetchone()
            if not sponsor:
                await q.answer("❌ اسپانسر یافت نشد.")
                return
            ctx.user_data["sponsor_edit_id"] = sid
            ctx.user_data["sponsor_edit_profile_id"] = profile_id
            txt = msg("sp_edit_prompt",
                      name=sponsor[1],
                      url=sponsor[2],
                      text=sponsor[3],
                      color=sponsor[4])
            await q.edit_message_text(
                txt,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("نام", callback_data=f"sp_edit_name_{sid}", style="primary"),
                     InlineKeyboardButton("لینک", callback_data=f"sp_edit_url_{sid}", style="primary")],
                    [InlineKeyboardButton("متن دکمه", callback_data=f"sp_edit_text_{sid}", style="primary"),
                     InlineKeyboardButton("رنگ", callback_data=f"sp_edit_color_{sid}", style="primary")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                ])
            )
            return

        if d.startswith("sp_edit_name_"):
            sid = int(d.split("_")[3])
            ctx.user_data["sponsor_edit_field"] = "name"
            await q.edit_message_text(
                msg("sp_edit_name") + "\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=empty_kb(f"sp_edit_{profile_id}_{sid}")
            )
            return

        if d.startswith("sp_edit_url_"):
            sid = int(d.split("_")[3])
            ctx.user_data["sponsor_edit_field"] = "url"
            await q.edit_message_text(
                msg("sp_edit_url") + "\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=empty_kb(f"sp_edit_{profile_id}_{sid}")
            )
            return

        if d.startswith("sp_edit_text_"):
            sid = int(d.split("_")[3])
            ctx.user_data["sponsor_edit_field"] = "text"
            await q.edit_message_text(
                msg("sp_edit_text") + "\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=empty_kb(f"sp_edit_{profile_id}_{sid}")
            )
            return

        if d.startswith("sp_edit_color_"):
            sid = int(d.split("_")[3])
            ctx.user_data["sponsor_edit_field"] = "color"
            await q.edit_message_text(
                "🎨 **رنگ جدید (blue / green / red):**\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 آبی", callback_data=f"sp_color_update_{sid}_blue", style="primary"),
                     InlineKeyboardButton("🟢 سبز", callback_data=f"sp_color_update_{sid}_green", style="success"),
                     InlineKeyboardButton("🔴 قرمز", callback_data=f"sp_color_update_{sid}_red", style="danger")],
                    [InlineKeyboardButton("🗑 خالی", callback_data=f"sp_color_empty_{sid}", style="secondary")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}_{sid}", style="primary")]
                ])
            )
            return

        if d.startswith("sp_color_update_"):
            parts = d.split("_")
            sid = int(parts[3])
            color = parts[4]
            if color in ("blue", "green", "red"):
                update_sponsor(sid, color=color)
                await q.answer("✅ رنگ به‌روزرسانی شد.")
                await q.edit_message_text(msg("sp_updated"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت به اسپانسرها", callback_data=f"sp_menu_{profile_id}", style="primary")]
                ]))
            else:
                await q.answer("❌ رنگ نامعتبر.")
            return

        if d.startswith("sp_color_empty_"):
            sid = int(d.split("_")[3])
            update_sponsor(sid, color="blue")  # پیش‌فرض آبی
            await q.answer("✅ رنگ به پیش‌فرض (آبی) تنظیم شد.")
            await q.edit_message_text(msg("sp_updated"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 بازگشت به اسپانسرها", callback_data=f"sp_menu_{profile_id}", style="primary")]
            ]))
            return

        if d.startswith("spt_"):
            parts = d.split("_")
            if len(parts) == 3:
                sid = int(parts[2])
                toggle_sponsor(sid)
                await q.answer("تغییر وضعیت داده شد")
                await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
            return

        if d.startswith("spd_"):
            parts = d.split("_")
            if len(parts) == 3:
                sid = int(parts[2])
                remove_sponsor(sid)
                await q.answer(msg("sp_removed"))
                await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
            return

        # ---------- مدیریت منابع ----------
        if d.startswith("src_list_"):
            prof = get_profile(profile_id)
            name = prof["dest_name"] if prof else ""
            sources = get_profile_sources(profile_id)
            src_text = "\n".join([f"• {s}" for s in sources]) if sources else "هیچ منبعی"
            txt = msg("source_list", name=name, sources=src_text)
            await q.edit_message_text(txt, reply_markup=source_list_kb(profile_id))
            return

        if d.startswith("src_del_"):
            parts = d.split("_")
            if len(parts) == 3:
                idx = int(parts[2])
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
            return

        if d.startswith("sa_"):
            ctx.user_data["action"] = f"sa_{profile_id}"
            await q.edit_message_text(msg("send_prompt") + "\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      parse_mode="HTML",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("sd_"):
            parts = d.split("_")
            if len(parts) == 3:
                idx = int(parts[2])
                sources = get_profile_sources(profile_id)
                if 0 <= idx < len(sources):
                    removed = sources.pop(idx)
                    set_profile_sources(profile_id, sources)
                    await q.answer(f"✅ منبع {removed} حذف شد")
                    await show_profile_admin(q.message, profile_id)
                else:
                    await q.answer("❌ خطا در ایندکس")
            return

        # ---------- رمزها ----------
        if d.startswith("pw_list_"):
            sources = get_profile_sources(profile_id)
            if not sources:
                await q.answer(msg("src_none"))
                return
            btns = [[InlineKeyboardButton(f"{s} ({len(get_pw(profile_id, s))})", callback_data=f"sp_{profile_id}_{i}", style="primary")] for i, s in enumerate(sources)]
            btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
            await q.edit_message_text("🔑 از کدوم منبع؟", reply_markup=InlineKeyboardMarkup(btns))
            return

        if d.startswith("sp_"):
            parts = d.split("_")
            if len(parts) == 3:
                idx = int(parts[2])
                sources = get_profile_sources(profile_id)
                if idx >= len(sources):
                    return
                src = sources[idx]
                pws = get_pw(profile_id, src)
                body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
                await q.edit_message_text(msg("pw_title", source=src) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))
            return

        if d.startswith("pa_"):
            parts = d.split("_")
            if len(parts) == 3:
                idx = int(parts[2])
                ctx.user_data["action"] = f"pa_{profile_id}_{idx}"
                await q.edit_message_text(msg("pw_prompt") + "\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                          parse_mode="HTML",
                                          reply_markup=empty_kb(f"sp_{profile_id}_{idx}"))
            return

        if d.startswith("pr_"):
            try:
                prefix, pwd = d.split("|", 1)
                parts = prefix.split("_")
                if len(parts) == 3:
                    idx = int(parts[2])
                    sources = get_profile_sources(profile_id)
                    if idx >= len(sources):
                        return
                    del_pw(profile_id, sources[idx], pwd)
                    await q.answer(msg("removed"))
                    src = sources[idx]
                    pws = get_pw(profile_id, src)
                    body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
                    await q.edit_message_text(msg("pw_title", source=src) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))
            except:
                pass
            return

        if d.startswith("sauto_"):
            parts = d.split("_")
            if len(parts) == 3:
                idx = int(parts[2])
                sources = get_profile_sources(profile_id)
                if idx >= len(sources):
                    return
                for pw in ["v2ray", "free", "1234"]:
                    add_pw(profile_id, sources[idx], pw)
                await q.answer(msg("pw_common_added"))
                src = sources[idx]
                pws = get_pw(profile_id, src)
                body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
                await q.edit_message_text(msg("pw_title", source=src) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))
            return

        # ---------- مقصد ----------
        if d.startswith("dl_"):
            dest = get_profile_dest(profile_id)
            body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
            await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))
            return

        if d.startswith("da_"):
            ctx.user_data["action"] = f"da_{profile_id}"
            await q.edit_message_text("📝 کانال مقصد جدید رو بفرست (با @ یا بدون):\nمثال: `@MyChannel`\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      parse_mode="HTML",
                                      reply_markup=empty_kb(f"dl_{profile_id}"))
            return

        if d.startswith("dd_"):
            set_profile_dest(profile_id, "")
            await q.answer(msg("removed"))
            dest = get_profile_dest(profile_id)
            body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
            await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))
            return

        # ---------- سایر تنظیمات ----------
        if d.startswith("ac_"):
            ctx.user_data["action"] = f"ac_{profile_id}"
            current_name = get_profile_dest(profile_id) or "نامشخص"
            await q.edit_message_text(f"نام فعلی: {current_name}\nنام جدید را بفرست (خالی برای حذف):\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("adn_"):
            ctx.user_data["action"] = f"adn_{profile_id}"
            current_display = get_profile_display_name(profile_id) or "تنظیم نشده"
            await q.edit_message_text(f"نام نمایشی فعلی: {current_display}\nنام جدید را بفرست (خالی برای حذف):\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("rename_"):
            ctx.user_data["action"] = f"rename_{profile_id}"
            await q.edit_message_text(msg("rename_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            return

        if d.startswith("ab_config_"):
            ctx.user_data["action"] = f"ab_config_{profile_id}"
            cur = html.escape(get_profile_banner_config(profile_id))
            await q.edit_message_text(f"Current Config Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{configs}}):\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      parse_mode="HTML",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("ab_proxy_"):
            ctx.user_data["action"] = f"ab_proxy_{profile_id}"
            cur = html.escape(get_profile_banner_proxy(profile_id))
            await q.edit_message_text(f"Current Proxy Banner:\n<code>{cur}</code>\n\nSend new banner (must contain {{proxies}}):\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      parse_mode="HTML",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("ai_"):
            ctx.user_data["action"] = f"ai_{profile_id}"
            current = get_profile_interval(profile_id)
            await q.edit_message_text(f"Now: {current}m\nSend 1-1440 (خالی برای عدم تغییر):\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("setmax_"):
            ctx.user_data["action"] = f"setmax_{profile_id}"
            current = get_profile_max_post(profile_id)
            await q.edit_message_text(f"Now: {current}\nSend 1-50 (خالی برای عدم تغییر):\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("ast_"):
            n_seen = c.execute("SELECT COUNT(*) FROM seen WHERE profile_id=?", (profile_id,)).fetchone()[0]
            n_pw = c.execute("SELECT COUNT(*) FROM source_passwords WHERE profile_id=?", (profile_id,)).fetchone()[0]
            n_sp = c.execute("SELECT COUNT(*) FROM sponsors WHERE profile_id=?", (profile_id,)).fetchone()[0]
            next_n = get_profile_last_num(profile_id) + 1
            dest = get_profile_dest(profile_id)
            txt = f"📊 مقصد: {dest}\nمنابع: {len(get_profile_sources(profile_id))}\nرمزها: {n_pw}\nاسپانسر: {n_sp}\nبعدی: #{next_n}\nحداکثر: {get_profile_max_post(profile_id)}"
            await q.edit_message_text(txt, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))
            return

        if d.startswith("sendtest_"):
            dest = get_profile_dest(profile_id)
            if not dest:
                await q.answer("❌ No destination set!", show_alert=True)
                return
            try:
                await u.get_bot().send_message(dest, f"Test {get_tehran_time()}")
                await q.answer("✅ Test sent")
            except Exception as e:
                await q.answer(f"❌ {str(e)[:80]}", show_alert=True)
            return

        if d.startswith("runnow_"):
            p = await q.edit_message_text("⏳ در حال اجرا...")
            try:
                n, m = await run_full_cycle_for_profile(u.get_bot(), profile_id, only_new=True)
                await p.edit_text(f"✅ Done: {n} - {m}")
            except Exception as e:
                log.error(f"❌ runnow error: {e}")
                await p.edit_text(f"❌ {str(e)[:200]}")
            return

        if d.startswith("tglping_"):
            current = get_profile_ping_mode(profile_id)
            new_mode = "global" if current == "iran" else "iran"
            set_profile_ping_mode(profile_id, new_mode)
            await q.answer(f"حالت پینگ: {'جهانی' if new_mode == 'global' else 'ایران'}")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tglcfg_"):
            current = get_profile_post_configs(profile_id)
            new_val = not current
            set_profile_post_configs(profile_id, new_val)
            await q.answer(msg("toggle_configs", status=new_val))
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tglproxy_"):
            current = get_profile_post_proxies(profile_id)
            new_val = not current
            set_profile_post_proxies(profile_id, new_val)
            await q.answer(msg("toggle_proxies", status=new_val))
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("togglenum_"):
            current = get_profile_show_numbers(profile_id)
            new_val = not current
            set_profile_show_numbers(profile_id, new_val)
            await q.answer(msg("toggle_numbers_ok", status=new_val))
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tgl_date_cfg_"):
            profile_id = int(d.split("_")[3])
            current = get_profile_show_date_config(profile_id)
            set_profile_show_date_config(profile_id, not current)
            await q.answer(msg("date_cfg_toggle", status=not current))
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("tgl_date_prx_"):
            profile_id = int(d.split("_")[3])
            current = get_profile_show_date_proxy(profile_id)
            set_profile_show_date_proxy(profile_id, not current)
            await q.answer(msg("date_prx_toggle", status=not current))
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("clearquery_"):
            profile_id = int(d.split("_")[1])
            set_profile_custom_query(profile_id, "")
            await q.answer("✅ کوئری سفارشی پاک شد.")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("setquery_"):
            ctx.user_data["action"] = f"setquery_{profile_id}"
            current = get_profile_custom_query(profile_id) or "خالی"
            await q.edit_message_text(f"کوئری فعلی: {current}\n" + msg("custom_query_prompt") + "\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                                      reply_markup=empty_kb(f"prof_{profile_id}"))
            return

        if d.startswith("rn_"):
            set_profile_last_num(profile_id, 0)
            await q.answer(msg("reset_ok"))
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("cd1_"):
            await q.edit_message_text(msg("clear_q1"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("YES", callback_data=f"cd2_{profile_id}", style="danger")], [InlineKeyboardButton("NO", callback_data=f"prof_{profile_id}", style="primary")]]))
            return

        if d.startswith("cd2_"):
            c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
            c.execute("DELETE FROM posts")
            c.execute("DELETE FROM country_cache")
            c.execute("DELETE FROM source_passwords WHERE profile_id=?", (profile_id,))
            c.execute("DELETE FROM last_scrape WHERE profile_id=?", (profile_id,))
            c.execute("DELETE FROM processed_messages WHERE profile_id=?", (profile_id,))
            c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
            c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
            set_profile_last_num(profile_id, 0)
            conn.commit()
            await q.answer("پاک شد")
            await show_profile_admin(q.message, profile_id)
            return

        if d.startswith("manual_"):
            ctx.user_data["action"] = f"manual_{profile_id}"
            await q.edit_message_text(msg("manual_send_prompt"), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"prof_{profile_id}", style="danger")]]))
            return

        await show_profile_admin(q.message, profile_id)

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
    display_name = prof["display_name"] or "تنظیم نشده"
    show_num = prof["show_numbers"] == 1
    custom_query = prof["custom_query"] or "خالی"
    show_date_cfg = prof["show_date_config"] == 1
    show_date_prx = prof["show_date_proxy"] == 1
    n_sp = c.execute("SELECT COUNT(*) FROM sponsors WHERE profile_id=? AND enabled=1", (profile_id,)).fetchone()[0]
    sponsor_st = f"{n_sp}✓" if n_sp else "OFF"
    ping_mode = prof["ping_mode"]
    ping_display = "ایران" if ping_mode == "iran" else "جهانی"
    post_cfg = prof["post_configs"] == 1
    post_prx = prof["post_proxies"] == 1
    cfg_status = "✅" if post_cfg else "❌"
    prx_status = "✅" if post_prx else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"
    last_cycle = prof["last_cycle_time"] or "هنوز اجرا نشده"

    txt = msg(
        "admin_panel",
        srcs=len(srcs), dest=dest,
        name=dest, num=last_num,
        interval=interval, max_post=max_post,
        sponsor=sponsor_st,
        ping_mode=ping_display,
        cfg_status=cfg_status,
        prx_status=prx_status,
        display_name=display_name,
        numbers_status=num_status,
        custom_query=custom_query,
        date_cfg=date_cfg_status,
        date_prx=date_prx_status,
        last_cycle=last_cycle,
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
    if PRIVATE_MODE and u.effective_user.id != ADMIN_ID:
        return

    # اگر دکمه خالی زده شده بود
    if ctx.user_data.get("empty_value"):
        txt = ""
        ctx.user_data["empty_value"] = False
    else:
        txt = u.message.text.strip()

    # ویرایش اسپانسر
    if ctx.user_data.get("sponsor_edit_field"):
        field = ctx.user_data["sponsor_edit_field"]
        sid = ctx.user_data.get("sponsor_edit_id")
        if not sid:
            del ctx.user_data["sponsor_edit_field"]
            return

        if field == "name":
            if txt:
                update_sponsor(sid, name=txt)
            else:
                # اگر خالی بود، حذف نمی‌کنیم بلکه هیچ تغییری نمی‌دهیم (چون name نمی‌تواند خالی باشد)
                await u.message.reply_text("❌ نام نمی‌تواند خالی باشد.")
                return
        elif field == "url":
            if txt:
                if not txt.startswith(("http://", "https://")):
                    txt = "https://" + txt
                update_sponsor(sid, url=txt)
            else:
                # اگر خالی بود، حذف نمی‌کنیم
                await u.message.reply_text("❌ لینک نمی‌تواند خالی باشد.")
                return
        elif field == "text":
            if txt:
                update_sponsor(sid, button_text=txt)
            else:
                await u.message.reply_text("❌ متن دکمه نمی‌تواند خالی باشد.")
                return
        elif field == "color":
            if txt in ("blue", "green", "red"):
                update_sponsor(sid, color=txt)
            else:
                await u.message.reply_text("❌ رنگ نامعتبر.")
                return
        del ctx.user_data["sponsor_edit_field"]
        await u.message.reply_text(msg("sp_updated"), parse_mode="HTML")
        profile_id = ctx.user_data.get("sponsor_edit_profile_id")
        if profile_id:
            await show_profile_admin(u.message, profile_id)
        return

    # مراحل اسپانسر جدید
    if ctx.user_data.get("sponsor_step"):
        profile_id = ctx.user_data.get("sponsor_profile_id")
        if not profile_id:
            del ctx.user_data["sponsor_step"]
            return
        step = ctx.user_data["sponsor_step"]

        if step == "name":
            if not txt:
                await u.message.reply_text("❌ نام نمی‌تواند خالی باشد.")
                return
            ctx.user_data["sponsor_name"] = txt
            ctx.user_data["sponsor_step"] = "url"
            await u.message.reply_text(
                "🔗 **لینک اسپانسر را وارد کنید (با http یا https):**\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=empty_kb(f"sp_menu_{profile_id}")
            )
            return

        if step == "url":
            if not txt:
                await u.message.reply_text("❌ لینک نمی‌تواند خالی باشد.")
                return
            if not txt.startswith(("http://", "https://")):
                txt = "https://" + txt
            ctx.user_data["sponsor_url"] = txt
            ctx.user_data["sponsor_step"] = "button_text"
            await u.message.reply_text(
                "📝 **متن دکمه را وارد کنید (مثلاً «بازدید»):**\n\n(می‌توانید از دکمه خالی استفاده کنید)",
                parse_mode="HTML",
                reply_markup=empty_kb(f"sp_menu_{profile_id}")
            )
            return

        if step == "button_text":
            if not txt:
                await u.message.reply_text("❌ متن دکمه نمی‌تواند خالی باشد.")
                return
            ctx.user_data["sponsor_button_text"] = txt
            ctx.user_data["sponsor_step"] = "color"
            await u.message.reply_text(
                "🎨 **رنگ دکمه را انتخاب کنید:**",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 آبی", callback_data=f"sp_color_{profile_id}_blue", style="primary")],
                    [InlineKeyboardButton("🟢 سبز", callback_data=f"sp_color_{profile_id}_green", style="success")],
                    [InlineKeyboardButton("🔴 قرمز", callback_data=f"sp_color_{profile_id}_red", style="danger")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}", style="primary")]
                ])
            )
            return

    a = ctx.user_data.get("action")
    if not a:
        return

    # اگر از دکمه خالی استفاده شده باشد، txt خالی است
    if txt == "" and not ctx.user_data.get("empty_value"):
        # اگر کاربر چیزی نفرستاده و دکمه خالی هم نزده، احتمالاً می‌خواهد لغو کند
        await u.message.reply_text("❌ ورودی خالی است. از دکمه خالی استفاده کنید یا چیزی وارد کنید.")
        return

    # حالا action ها
    if a == "prof_add":
        dest_name = txt if txt else None
        if not dest_name:
            await u.message.reply_text("❌ نام مقصد خالی است.")
            return
        if not dest_name.startswith("@") and not dest_name.isdigit():
            dest_name = "@" + dest_name
        profiles = get_profiles()
        if any(p["dest_name"] == dest_name for p in profiles):
            await u.message.reply_text("❌ این مقصد قبلاً وجود دارد.")
            return
        create_profile(dest_name)
        await u.message.reply_text(msg("profile_added", name=dest_name))
        del ctx.user_data["action"]
        await show_profiles_list(u.message)
        return

    if a.startswith("sa_"):
        profile_id = int(a.split("_")[1])
        if not txt:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', txt)
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
        dest = txt if txt else ""
        if dest:
            if not dest.startswith("@") and not dest.isdigit():
                dest = "@" + dest
            set_profile_dest(profile_id, dest)
            await u.message.reply_text(msg("dest_set", dest=dest))
        else:
            set_profile_dest(profile_id, "")
            await u.message.reply_text(msg("removed"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ac_"):
        profile_id = int(a.split("_")[1])
        name = txt if txt else ""
        set_profile_dest(profile_id, name)
        await u.message.reply_text(msg("name_set", name=name if name else "حذف شد"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("adn_"):
        profile_id = int(a.split("_")[1])
        new_name = txt if txt else ""
        set_profile_display_name(profile_id, new_name)
        await u.message.reply_text(msg("display_name_set", name=new_name if new_name else "پاک شد"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("rename_"):
        profile_id = int(a.split("_")[1])
        await process_rename(u, u.message, profile_id, is_document=False)
        del ctx.user_data["action"]
        return

    if a.startswith("ab_config_"):
        profile_id = int(a.split("_")[2])
        if not txt:
            await u.message.reply_text("❌ بنر خالی است.")
            return
        if "{configs}" in txt:
            update_profile(profile_id, banner_config=txt)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ab_proxy_"):
        profile_id = int(a.split("_")[2])
        if not txt:
            await u.message.reply_text("❌ بنر خالی است.")
            return
        if "{proxies}" in txt:
            update_profile(profile_id, banner_proxy=txt)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("ai_"):
        profile_id = int(a.split("_")[1])
        if not txt:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(txt)
        except:
            return await u.message.reply_text(msg("interval_wrong"))
        if 1 <= n <= 1440:
            update_profile(profile_id, interval_min=n)
            await u.message.reply_text(msg("interval_ok", n=n))
        else:
            return await u.message.reply_text(msg("interval_err"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

    if a.startswith("setmax_"):
        profile_id = int(a.split("_")[1])
        if not txt:
            await u.message.reply_text("✅ بدون تغییر.")
            del ctx.user_data["action"]
            await show_profile_admin(u.message, profile_id)
            return
        try:
            n = int(txt)
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

    if a.startswith("pa_"):
        parts = a.split("_")
        if len(parts) < 3:
            return
        profile_id = int(parts[1])
        idx = int(parts[2])
        sources = get_profile_sources(profile_id)
        if idx >= len(sources):
            return
        source = sources[idx]
        if not txt:
            await u.message.reply_text("❌ ورودی خالی است.")
            return
        items = re.split(r'[,،\n]+', txt)
        items = [x.strip() for x in items if x.strip()]
        added = []
        for pw in items:
            add_pw(profile_id, source, pw)
            added.append(pw)
        del ctx.user_data["action"]
        await u.message.reply_text(msg("pw_added", source=source) + f"\nرمزهای اضافه شده: {', '.join(added)}")
        pws = get_pw(profile_id, source)
        body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
        await u.message.reply_text(msg("pw_title", source=source) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=False)
        del ctx.user_data["action"]
        return

    if a.startswith("setquery_"):
        profile_id = int(a.split("_")[1])
        query = txt if txt else ""
        set_profile_custom_query(profile_id, query)
        await u.message.reply_text(msg("custom_query_set", query=query if query else "خالی"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

async def on_document(u, ctx):
    if PRIVATE_MODE and u.effective_user.id != ADMIN_ID:
        return
    a = ctx.user_data.get("action")
    if not a:
        return

    if a.startswith("rename_"):
        profile_id = int(a.split("_")[1])
        await process_rename(u, u.message, profile_id, is_document=True)
        del ctx.user_data["action"]
        return

    if a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=True)
        del ctx.user_data["action"]
        return

# ======================================================================
# پردازش تغییر نام
# ======================================================================
async def process_rename(u, message, profile_id, is_document=False):
    p = await message.reply_text("⏳ در حال پردازش برای تغییر نام...")
    try:
        if is_document:
            doc = message.document
            if doc.file_size and doc.file_size > 3 * 1024 * 1024:
                return await p.edit_text("❌ فایل بزرگتر از ۳ مگابایت")
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
        if not config_links:
            for line in text.splitlines():
                line = line.strip()
                if line:
                    config_links.extend(extract_links_from_text(line))
            config_links = list(set(config_links))

        if not config_links:
            return await p.edit_text("❌ هیچ لینک کانفیگ معتبری یافت نشد.")

        display_name = get_profile_display_name(profile_id)
        if not display_name:
            display_name = get_profile_dest(profile_id) or "Server"
        custom_query = get_profile_custom_query(profile_id)

        async def get_renamed_link(url):
            host, _ = extract_host(url)
            flag = "🌐"
            if host:
                ip = await host_to_ip(host)
                if ip:
                    flag = await get_flag_for_ip(ip)
            return change_link_display_name(url, display_name, flag, custom_query)

        tasks = [get_renamed_link(link) for link in config_links[:30]]
        renamed_links = await asyncio.gather(*tasks)

        msg_text = f"📝 **سرورهای با نام {display_name}**\n\n"
        for link in renamed_links:
            msg_text += f"<code>{link}</code>\n\n"

        await u.get_bot().send_message(
            ADMIN_ID,
            msg_text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        await p.edit_text(f"✅ {len(renamed_links)} لینک با نام '{display_name}' تغییر نام داده و برای شما ارسال شد.")
    except Exception as e:
        log.error(f"rename error: {e}")
        await p.edit_text(f"❌ {str(e)[:200]}")

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

        new_configs = config_links
        new_proxies = []
        for pl in proxy_links:
            norm = normalize_proxy_url(pl)
            if norm:
                new_proxies.append(norm)

        if not new_configs and not new_proxies:
            return await p.edit_text("❌ هیچ لینک معتبری برای ارسال وجود ندارد.")

        await p.edit_text(f"⏳ آماده‌سازی {len(new_configs)} کانفیگ و {len(new_proxies)} پروکسی...")

        working = [(url, 0, 0) for url in new_configs[:30]]
        proxy_with_ping = []
        for proxy_url in new_proxies[:10]:
            host, _ = extract_host(proxy_url)
            flag = "🌐"
            if host:
                ip = await host_to_ip(host)
                if ip:
                    flag = await get_flag_for_ip(ip)
            proxy_with_ping.append((proxy_url, 0, flag))

        if not working and not proxy_with_ping:
            return await p.edit_text("⚠️ هیچ لینک جدیدی برای ارسال وجود ندارد.")

        n, m = await post_working_configs(u.get_bot(), profile_id, working, proxy_with_ping, source_for_seen="manual", force=True)
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
        create_profile("@VaslZone", sources="@Cfox_Server,@v2rayHub1200,@V2Ray_Protocol")
        log.info("✅ Created default profile.")
    log.info(f"✅ INIT done: {len(profiles)} profiles, AUTO={ENABLE_AUTO}, LANG={BOT_LANG}")
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
