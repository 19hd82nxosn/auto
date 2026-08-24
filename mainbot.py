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

# ======================================================================
# تنظیم لاگ
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

c.execute("""CREATE TABLE IF NOT EXISTS country_cache (
    ip TEXT PRIMARY KEY,
    country TEXT,
    flag TEXT)""")

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
    show_numbers INTEGER DEFAULT 1,
    custom_query TEXT DEFAULT '',
    show_date_config INTEGER DEFAULT 1,
    show_date_proxy INTEGER DEFAULT 1)""")
conn.commit()

ensure_column("profiles", "show_numbers", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "custom_query", "TEXT DEFAULT ''", "")
ensure_column("profiles", "show_date_config", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "show_date_proxy", "INTEGER DEFAULT 1", 1)

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
             show_numbers, custom_query, show_date_config, show_date_proxy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             datetime.now().isoformat(), 1, "", 1, 1))
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
            "show_numbers": row[13] if len(row) > 13 else 1,
            "custom_query": row[14] if len(row) > 14 else "",
            "show_date_config": row[15] if len(row) > 15 else 1,
            "show_date_proxy": row[16] if len(row) > 16 else 1
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
        "show_numbers": row[13] if len(row) > 13 else 1,
        "custom_query": row[14] if len(row) > 14 else "",
        "show_date_config": row[15] if len(row) > 15 else 1,
        "show_date_proxy": row[16] if len(row) > 16 else 1
    }

def create_profile(dest_name, sources="", banner_config=None, banner_proxy=None,
                   interval_min=5, max_post=8, max_proxies=10,
                   post_configs=1, post_proxies=1, ping_mode="iran", last_num=0,
                   show_numbers=1, custom_query="",
                   show_date_config=1, show_date_proxy=1):
    if not banner_config:
        banner_config = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"
    if not banner_proxy:
        banner_proxy = "🌐 <b>Proxies</b>\n━━━━━━━━━━━━━━━━━━\n📅 {date}\n✅ {count} proxies\n━━━━━━━━━━━━━━━━━━\n\n{proxies}\n━━━━━━━━━━━━━━━━━━"
    c.execute("""INSERT INTO profiles
        (dest_name, sources, banner_config, banner_proxy, interval_min,
         max_post, max_proxies, post_configs, post_proxies, ping_mode, last_num, created_at,
         show_numbers, custom_query, show_date_config, show_date_proxy)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dest_name, sources, banner_config, banner_proxy,
         interval_min, max_post, max_proxies,
         post_configs, post_proxies, ping_mode, last_num,
         get_tehran_time(), show_numbers, custom_query,
         show_date_config, show_date_proxy))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "last_num",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy"]
    for key, value in kwargs.items():
        if key in allowed:
            c.execute(f"UPDATE profiles SET {key}=? WHERE id=?", (value, profile_id))
    conn.commit()

def delete_profile(profile_id):
    c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    c.execute("DELETE FROM sponsors WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM seen WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM proxies_seen WHERE profile_id=?", (profile_id,))
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

# ======================================================================
# اسپانسرها (بازنویسی کامل با منطق ساده و قابل اطمینان)
# ======================================================================
def add_sponsor(profile_id, name, url, button_text="Advertisement", color="blue"):
    try:
        c.execute("""INSERT INTO sponsors
            (profile_id, name, url, button_text, color, enabled, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (profile_id, name, url, button_text, color, get_tehran_time()))
        conn.commit()
        return c.lastrowid
    except Exception as e:
        log.error(f"add_sponsor error: {e}")
        return None

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

def get_sponsor(sid):
    row = c.execute("SELECT id, profile_id, name, url, button_text, color, enabled FROM sponsors WHERE id=?", (sid,)).fetchone()
    if row:
        return {
            "id": row[0],
            "profile_id": row[1],
            "name": row[2],
            "url": row[3],
            "button_text": row[4],
            "color": row[5],
            "enabled": row[6]
        }
    return None

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

def append_channel_and_flag_encoded(url, channel, flag, custom_query=""):
    protocol = url.split('://')[0].lower() if '://' in url else ''
    if custom_query and protocol != 'vmess':
        url = add_custom_query_to_url(url, custom_query, protocol)
    base = strip_url_fragment(url)
    fragment = f"{channel} {flag}"
    encoded = quote(fragment, safe='')
    return f"{base}#{encoded}"

# ======================================================================
# پینگ (سریع‌تر)
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
# اسکرپ (فقط جدید و سریع) – بهینه‌سازی برای حالت لحظه‌ای
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
# ارسال کانفیگ‌ها (با بنر و اسپانسر) - هر کانفیگ جداگانه با بنر کامل
# ======================================================================
async def post_configs(bot, profile_id, working, source_for_seen=""):
    if not working:
        return 0

    max_post = get_profile_max_post(profile_id)
    items = sorted(working, key=lambda x: x[1])[:max_post]
    last_n = get_profile_last_num(profile_id)
    show_numbers = get_profile_show_numbers(profile_id)
    custom_query = get_profile_custom_query(profile_id)
    dest = get_profile_dest(profile_id)
    banner_template = get_profile_banner_config(profile_id) or "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

    # دریافت اسپانسر فعال (فقط اولین اسپانسر فعال)
    sponsors = get_enabled_sponsors(profile_id)
    sponsor_button = None
    if sponsors:
        sid, sname, surl, stxt, scolor = sponsors[0]
        clean_txt = stxt.strip()
        # رنگ‌ها فقط برای نمایش، در دکمه واقعی تأثیری ندارند (telegram不支持style)
        sponsor_button = InlineKeyboardButton(clean_txt, url=surl)

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

        # ساخت هدر
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

        # ساخت بلاک کانفیگ با تگ pre
        # مهم: URL را برای HTML escape کنیم تا کاراکترهای & و ... مشکلی ایجاد نکنند
        safe_url = html.escape(modified_url)
        block = f"<pre>{safe_url}</pre>"
        configs_text = header + "\n" + block

        # قرار دادن در بنر
        try:
            full_text = banner_template.format(configs=configs_text)
        except KeyError:
            full_text = f"✦ V2Ray Config List\n\n{configs_text}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری"

        # دکمه‌ها: فقط اسپانسر (در انتهای پیام)
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
            # تلاش با متن ساده
            try:
                plain = re.sub(r'<[^>]+>', '', full_text)
                await bot.send_message(dest, plain[:4096], disable_web_page_preview=True)
                sent_count += 1
                log.info(f"✅ Sent config #{n} to {dest} (plain)")
            except Exception as e2:
                log.error(f"Failed even plain for config {n}: {e2}")

        mark_as_posted(profile_id, modified_url, source_for_seen)

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
            config_count = await post_configs(bot, profile_id, unique_working, source_for_seen)
            if config_count > 0:
                total_configs = config_count
                results.append(f"{config_count} configs")

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
# سیکل کامل (فقط جدید و تست محدود) – بهینه‌سازی برای سرعت
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

    if is_instant:
        scrape_limit = 3
        test_limit = 5
        ping_timeout = 8
        max_concurrent = 30
    else:
        scrape_limit = 10
        test_limit = 15
        ping_timeout = 10
        max_concurrent = 20

    async def scrape_one(src):
        config_links, proxy_links = await scrape_channel_with_retry(profile_id, src, only_new=True)
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
            log.info(f"📊 Testing {len(valid_proxies)} proxies...")
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
    result = await post_working_configs(bot, profile_id, working, proxy_with_ping)
    log.info(f"✅ Cycle result for profile {profile_id}: {result}")
    log.info("=" * 50)
    return result

# ======================================================================
# حلقه خودکار (دقیق بر اساس فاصله زمانی) با پشتیبانی از حالت لحظه‌ای و سرعت بالا
# ======================================================================
async def profile_loop(bot, profile_id):
    profile = get_profile(profile_id)
    if not profile:
        log.error(f"❌ Profile {profile_id} not found, stopping loop.")
        return
    interval = profile["interval_min"]
    if interval == 0:
        log.info(f"⚡ Instant update mode for profile {profile_id} (every 1 sec)")
        while True:
            try:
                await asyncio.sleep(1)
                log.info(f"⚡ INSTANT UPDATE for profile {profile_id}")
                n, m = await run_full_cycle_for_profile(bot, profile_id, only_new=True, is_instant=True)
                log.info(f"[instant profile {profile_id}] {n} - {m}")
            except asyncio.CancelledError:
                log.info(f"🛑 Profile loop {profile_id} cancelled.")
                break
            except Exception as e:
                log.error(f"❌ instant update error for {profile_id}: {e}")
                await asyncio.sleep(1)
    else:
        next_run = datetime.now(TEHRAN_TZ) + timedelta(minutes=interval)
        while True:
            try:
                now = datetime.now(TEHRAN_TZ)
                sleep_seconds = (next_run - now).total_seconds()
                if sleep_seconds > 0:
                    await asyncio.sleep(sleep_seconds)
                else:
                    log.warning(f"Missed schedule for profile {profile_id}, running now.")
                    next_run = now + timedelta(minutes=interval)
                    continue

                log.info(f"⏰ AUTO TICK for profile {profile_id} ({profile['dest_name']}) at {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
                n, m = await run_full_cycle_for_profile(bot, profile_id, only_new=True, is_instant=False)
                log.info(f"[auto profile {profile_id}] {n} - {m}")

                next_run = next_run + timedelta(minutes=interval)
            except asyncio.CancelledError:
                log.info(f"🛑 Profile loop {profile_id} cancelled.")
                break
            except Exception as e:
                log.error(f"❌ profile_loop error for {profile_id}: {e}")
                log.error(traceback.format_exc())
                await asyncio.sleep(60)
                now = datetime.now(TEHRAN_TZ)
                next_run = now + timedelta(minutes=interval)

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
                       "📅 تاریخ پروکسی: {date_prx}",
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
        "sp_prompt": "📢 اسپانسر:\nفرمت: نام|url|متن دکمه|رنگ\nرنگ‌ها: blue, green, red",
        "sp_added": "✅ '{name}' اضافه شد",
        "sp_removed": "✅ حذف شد",
        "sp_title": "📢 اسپانسرها:",
        "sp_none": "خالی",
        "sp_err": "❌ فرمت: name|url|text|color (رنگ‌ها: blue, green, red)",
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
        "sp_edit_prompt": "📢 **ویرایش اسپانسر**\n\nنام: {name}\nلینک: {url}\nمتن: {text}\nرنگ: {color}\n\nبرای ویرایش هر بخش، دکمه مربوطه را بزنید.",
        "sp_edit_name": "نام جدید (خالی برای عدم تغییر):",
        "sp_edit_url": "لینک جدید (خالی برای عدم تغییر):",
        "sp_edit_text": "متن جدید دکمه (خالی برای عدم تغییر):",
        "sp_edit_color": "رنگ جدید (blue/green/red) - خالی برای عدم تغییر:",
        "sp_updated": "✅ اسپانسر به‌روزرسانی شد.",
        "btn_edit_sponsor": "✏️ ویرایش",
        "delete_confirm1": "⚠️ **آیا مطمئن هستید که می‌خواهید این پروفایل را حذف کنید؟**\n\nنام: {name}\nشناسه: {id}\n\nاین عملیات غیرقابل برگشت است و تمام داده‌های مربوط به این پروفایل (منابع، اسپانسرها، تاریخچه) پاک می‌شود.\n\nبرای تأیید، دکمه **«بله، حذف شود»** را بزنید.",
        "delete_confirm2": "⚠️ **تأیید نهایی حذف پروفایل**\n\nنام: {name}\nشناسه: {id}\n\n**آیا از حذف این پروفایل اطمینان دارید؟**\n\nبرای حذف نهایی، دکمه **«حذف نهایی»** را بزنید.",
        "delete_cancelled": "❌ حذف پروفایل لغو شد.",
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
                       "🔢 Numbering: {numbers_status}\n"
                       "🔗 Custom Query: {custom_query}\n"
                       "📅 Config Date: {date_cfg}\n"
                       "📅 Proxy Date: {date_prx}",
        "btn_back": "🔙 Back",
        "btn_add_source": "➕ Source",
        "btn_add_dest": "➕ Add Destination",
        "btn_dest_list": "📋 Destinations",
        "btn_sponsors": "📢 Sponsors",
        "btn_set_dest": "🎯 Set Destination",
        "btn_set_name": "🎨 Name",
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
        "btn_instant": "⚡ Instant Update",
        "btn_manual_send": "📤 Manual Send",
        "btn_manage_sources": "📡 Manage Sources",
        "btn_toggle_numbers": "🔢 Numbering: {status}",
        "btn_set_custom_query": "🔗 Set Custom Query",
        "btn_empty": "🧹 Empty",
        "send_prompt": "📝 Channel name (with/without @):",
        "added": "✅ {item}",
        "removed": "✅ Removed",
        "test_ok": "✅ Sent to {dest}",
        "test_err": "❌ ERR:\n<code>{err}</code>",
        "no_pings": "❌ No ping",
        "clear_q1": "⚠️ Clear DB? (1/2)",
        "dest_set": "✅ Destination: {dest}",
        "name_set": "✅ Name: {name}",
        "banner_ok": "✅ Banner saved",
        "banner_err": "❌ must include {configs} or {proxies}",
        "interval_ok": "✅ every {n} min",
        "interval_err": "❌ 1-1440 min (0 for instant)",
        "interval_wrong": "❌ number only",
        "max_ok": "✅ Max {n}",
        "max_err": "❌ 1-50",
        "src_title": "📡 Sources ({n}):",
        "src_none": "none",
        "reset_ok": "✅ Reset (#1)",
        "lang_ok": "✅ Switched to English",
        "sp_prompt": "📢 Sponsor:\nFormat: name|url|button_text|color\nColors: blue, green, red",
        "sp_added": "✅ '{name}' added",
        "sp_removed": "✅ Removed",
        "sp_title": "📢 Sponsors:",
        "sp_none": "none",
        "sp_err": "❌ Bad format: name|url|text|color (colors: blue, green, red)",
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
        "profile_deleted": "❌ Profile deleted.",
        "profile_not_found": "❌ Profile not found.",
        "manual_send_prompt": "📤 Please send a message (text or file) containing config/proxy links.\n\n⏳ Bot will detect and send with appropriate banners.\n\n⚠️ **Note:** In manual mode, no ping test is performed and all links will be posted even if they were posted before.",
        "manual_send_cancel": "❌ Manual send cancelled.",
        "manual_send_processing": "⏳ Processing...",
        "manual_send_done": "✅ Manual send completed.",
        "custom_query_set": "✅ Custom query set: {query}",
        "custom_query_prompt": "🔗 Enter custom query (e.g. Telegram=@MyChannel) or use Empty button:",
        "source_list": "📡 **Sources for profile {name}**\n\n{sources}\n\nClick delete to remove.",
        "source_deleted": "✅ Source deleted.",
        "toggle_numbers_ok": "✅ Numbering {'enabled' if status else 'disabled'}.",
        "date_cfg_toggle": "✅ Config date display {'enabled' if status else 'disabled'}.",
        "date_prx_toggle": "✅ Proxy date display {'enabled' if status else 'disabled'}.",
        "sp_edit_prompt": "📢 **Edit Sponsor**\n\nName: {name}\nURL: {url}\nText: {text}\nColor: {color}\n\nClick a button to edit.",
        "sp_edit_name": "New name (leave empty to keep):",
        "sp_edit_url": "New URL (leave empty to keep):",
        "sp_edit_text": "New button text (leave empty to keep):",
        "sp_edit_color": "New color (blue/green/red - leave empty to keep):",
        "sp_updated": "✅ Sponsor updated.",
        "btn_edit_sponsor": "✏️ Edit",
        "delete_confirm1": "⚠️ **Are you sure you want to delete this profile?**\n\nName: {name}\nID: {id}\n\nThis action is irreversible and all data related to this profile (sources, sponsors, history) will be deleted.\n\nPress **«Yes, delete»** to confirm.",
        "delete_confirm2": "⚠️ **Final confirmation to delete profile**\n\nName: {name}\nID: {id}\n\n**Are you absolutely sure?**\n\nPress **«Delete permanently»** to delete.",
        "delete_cancelled": "❌ Profile deletion cancelled.",
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

    cfg_btn = msg("btn_toggle_configs", status=cfg_status)
    prx_btn = msg("btn_toggle_proxies", status=prx_status)
    num_btn = msg("btn_toggle_numbers", status=num_status)

    return InlineKeyboardMarkup([
        [InlineKeyboardButton(msg("btn_manage_sources"), callback_data=f"src_list_{profile_id}"),
         InlineKeyboardButton(msg("btn_dest_list"), callback_data=f"dl_{profile_id}")],
        [InlineKeyboardButton(msg("btn_sponsors"), callback_data=f"sp_menu_{profile_id}"),
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

def sponsors_kb(profile_id):
    rows = get_all_sponsors(profile_id)
    btns = []
    if rows:
        for row in rows:
            sid, name, url, btn_text, color, enabled = row
            st = "✅" if enabled else "❌"
            btns.append([
                InlineKeyboardButton(f"{st} {name[:25]}", callback_data=f"spt_{profile_id}_{sid}"),
                InlineKeyboardButton("🗑", callback_data=f"spd_{profile_id}_{sid}"),
                InlineKeyboardButton(msg("btn_edit_sponsor"), callback_data=f"sp_edit_{profile_id}_{sid}")
            ])
    btns.append([InlineKeyboardButton("➕ add sponsor", callback_data=f"sp_add_{profile_id}")])
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

        # ===== دکمه اپدیت لحظه‌ای =====
        if d.startswith("instant_"):
            parts = d.split("_")
            if len(parts) >= 2:
                try:
                    profile_id = int(parts[1])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                update_profile(profile_id, interval_min=0)
                await q.answer("⚡ حالت اپدیت لحظه‌ای فعال شد (هر ۱ ثانیه)")
                await show_profile_admin(q.message, profile_id)
            else:
                await q.answer("⚠️ خطا در داده")
            return

        # ===== حذف پروفایل با دو مرحله تایید =====
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
                ctx.user_data["delete_profile_id"] = profile_id
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

        # ===== پروفایل management =====
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

        # ===== اسپانسرها =====
        if d.startswith("sp_menu_"):
            parts = d.split("_")
            if len(parts) >= 3:
                try:
                    profile_id = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
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

        if d.startswith("sp_color_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    color = parts[3]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
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
                            [InlineKeyboardButton("🔙 بازگشت به اسپانسرها", callback_data=f"sp_menu_{profile_id}")]
                        ])
                    )
                    del ctx.user_data["sponsor_step"]
                    del ctx.user_data["sponsor_name"]
                    del ctx.user_data["sponsor_url"]
                    del ctx.user_data["sponsor_button_text"]
                else:
                    await q.answer("❌ اطلاعات ناقص است.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[2])
                    sid = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                sponsor = get_sponsor(sid)
                if not sponsor:
                    await q.answer("❌ اسپانسر یافت نشد.")
                    return
                ctx.user_data["sponsor_edit_id"] = sid
                ctx.user_data["sponsor_edit_profile_id"] = profile_id
                txt = msg("sp_edit_prompt",
                          name=sponsor["name"],
                          url=sponsor["url"],
                          text=sponsor["button_text"],
                          color=sponsor["color"])
                await q.edit_message_text(
                    txt,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("نام", callback_data=f"sp_edit_name_{sid}"),
                         InlineKeyboardButton("لینک", callback_data=f"sp_edit_url_{sid}")],
                        [InlineKeyboardButton("متن دکمه", callback_data=f"sp_edit_text_{sid}"),
                         InlineKeyboardButton("رنگ", callback_data=f"sp_edit_color_{sid}")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_name_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    sid = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                profile_id = ctx.user_data.get("sponsor_edit_profile_id")
                if not profile_id:
                    await q.answer("❌ خطا در پروفایل")
                    return
                ctx.user_data["sponsor_edit_field"] = "name"
                await q.edit_message_text(
                    msg("sp_edit_name"),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}_{sid}")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_url_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    sid = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                profile_id = ctx.user_data.get("sponsor_edit_profile_id")
                if not profile_id:
                    await q.answer("❌ خطا در پروفایل")
                    return
                ctx.user_data["sponsor_edit_field"] = "url"
                await q.edit_message_text(
                    msg("sp_edit_url"),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}_{sid}")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_text_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    sid = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                profile_id = ctx.user_data.get("sponsor_edit_profile_id")
                if not profile_id:
                    await q.answer("❌ خطا در پروفایل")
                    return
                ctx.user_data["sponsor_edit_field"] = "text"
                await q.edit_message_text(
                    msg("sp_edit_text"),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}_{sid}")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_edit_color_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    sid = int(parts[3])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                profile_id = ctx.user_data.get("sponsor_edit_profile_id")
                if not profile_id:
                    await q.answer("❌ خطا در پروفایل")
                    return
                ctx.user_data["sponsor_edit_field"] = "color"
                await q.edit_message_text(
                    "🎨 **رنگ جدید (blue / green / red):**",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔵 آبی", callback_data=f"sp_color_update_{sid}_blue"),
                         InlineKeyboardButton("🟢 سبز", callback_data=f"sp_color_update_{sid}_green"),
                         InlineKeyboardButton("🔴 قرمز", callback_data=f"sp_color_update_{sid}_red")],
                        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_edit_{profile_id}_{sid}")]
                    ])
                )
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("sp_color_update_"):
            parts = d.split("_")
            if len(parts) >= 5:
                try:
                    sid = int(parts[3])
                    color = parts[4]
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                profile_id = ctx.user_data.get("sponsor_edit_profile_id")
                if not profile_id:
                    await q.answer("❌ خطا در پروفایل")
                    return
                if color in ("blue", "green", "red"):
                    update_sponsor(sid, color=color)
                    await q.answer("✅ رنگ به‌روزرسانی شد.")
                    await q.edit_message_text(msg("sp_updated"), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 بازگشت به اسپانسرها", callback_data=f"sp_menu_{profile_id}")]
                    ]))
                else:
                    await q.answer("❌ رنگ نامعتبر.")
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("spt_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[1])
                    sid = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                toggle_sponsor(sid)
                await q.answer("تغییر وضعیت داده شد")
                await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
            else:
                await q.answer("⚠️ خطا در داده")
            return

        if d.startswith("spd_"):
            parts = d.split("_")
            if len(parts) >= 4:
                try:
                    profile_id = int(parts[1])
                    sid = int(parts[2])
                except ValueError:
                    await q.answer("⚠️ شناسه نامعتبر")
                    return
                remove_sponsor(sid)
                await q.answer(msg("sp_removed"))
                await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
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

    # ویرایش اسپانسر
    if ctx.user_data.get("sponsor_edit_field"):
        field = ctx.user_data["sponsor_edit_field"]
        sid = ctx.user_data.get("sponsor_edit_id")
        if not sid:
            del ctx.user_data["sponsor_edit_field"]
            return
        txt = u.message.text.strip()
        if field == "name":
            if txt:
                update_sponsor(sid, name=txt)
            else:
                await u.message.reply_text("❌ نام خالی ماند.")
        elif field == "url":
            if txt:
                if not txt.startswith(("http://", "https://")):
                    txt = "https://" + txt
                update_sponsor(sid, url=txt)
            else:
                await u.message.reply_text("❌ لینک خالی ماند.")
        elif field == "text":
            if txt:
                update_sponsor(sid, button_text=txt)
            else:
                await u.message.reply_text("❌ متن خالی ماند.")
        elif field == "color":
            if txt in ("blue", "green", "red"):
                update_sponsor(sid, color=txt)
            else:
                await u.message.reply_text("❌ رنگ نامعتبر.")
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
            ctx.user_data["sponsor_name"] = u.message.text.strip()
            ctx.user_data["sponsor_step"] = "url"
            await u.message.reply_text(
                "🔗 **لینک اسپانسر را وارد کنید (با http یا https):**",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
                ])
            )
            return

        if step == "url":
            url = u.message.text.strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
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
            ctx.user_data["sponsor_button_text"] = u.message.text.strip()
            ctx.user_data["sponsor_step"] = "color"
            await u.message.reply_text(
                "🎨 **رنگ دکمه را انتخاب کنید:**",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔵 آبی", callback_data=f"sp_color_{profile_id}_blue")],
                    [InlineKeyboardButton("🟢 سبز", callback_data=f"sp_color_{profile_id}_green")],
                    [InlineKeyboardButton("🔴 قرمز", callback_data=f"sp_color_{profile_id}_red")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data=f"sp_menu_{profile_id}")]
                ])
            )
            return

    a = ctx.user_data.get("action")
    if not a:
        return

    t = u.message.text.strip()

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
        set_profile_custom_query(profile_id, query)
        await u.message.reply_text(msg("custom_query_set", query=query if query else "خالی"))
        del ctx.user_data["action"]
        await show_profile_admin(u.message, profile_id)
        return

async def on_document(u, ctx):
    if u.effective_user.id != ADMIN_ID:
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
# ارسال دستی (با تقسیم)
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
        new_id = create_profile("@VaslZone", sources="@Cfox_Server")
        log.info(f"✅ Created default profile with id {new_id}.")
        profiles = get_profiles()
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
