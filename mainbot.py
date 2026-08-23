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

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot.db")

# ======================================================================
# لاگ و منطقه زمانی
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

# --- جداول اصلی ---
c.execute("""CREATE TABLE IF NOT EXISTS seen (
    uuid TEXT, address TEXT, source TEXT DEFAULT '',
    first_seen TEXT, last_posted TEXT, UNIQUE(uuid, address))""")
c.execute("""CREATE TABLE IF NOT EXISTS cfg (k TEXT PRIMARY KEY, v TEXT)""")
c.execute("""CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, count INTEGER, created_at TEXT)""")
ensure_column("seen", "source", "TEXT DEFAULT ''")
ensure_column("seen", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS country_cache (ip TEXT PRIMARY KEY, country TEXT, flag TEXT)""")
c.execute("""CREATE TABLE IF NOT EXISTS source_passwords (
    source TEXT, password TEXT, UNIQUE(source, password))""")
ensure_column("source_passwords", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS sponsors (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER,
    name TEXT NOT NULL, url TEXT NOT NULL, button_text TEXT DEFAULT 'Advertisement',
    color TEXT DEFAULT 'blue', enabled INTEGER DEFAULT 1, created_at TEXT,
    FOREIGN KEY(profile_id) REFERENCES profiles(id))""")
ensure_column("sponsors", "profile_id", "INTEGER")

c.execute("""CREATE TABLE IF NOT EXISTS last_scrape (source TEXT PRIMARY KEY, last_scrape_time TEXT)""")
ensure_column("last_scrape", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS processed_messages (
    source TEXT, message_id INTEGER, PRIMARY KEY(source, message_id))""")
ensure_column("processed_messages", "profile_id", "INTEGER DEFAULT 1", 1)

c.execute("""CREATE TABLE IF NOT EXISTS proxies_seen (proxy_url TEXT PRIMARY KEY, first_seen TEXT, last_posted TEXT)""")
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
    show_date_proxy INTEGER DEFAULT 1)""")
conn.commit()
ensure_column("profiles", "show_numbers", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "custom_query", "TEXT DEFAULT ''", "")
ensure_column("profiles", "show_date_config", "INTEGER DEFAULT 1", 1)
ensure_column("profiles", "show_date_proxy", "INTEGER DEFAULT 1", 1)

# ======================================================================
# مهاجرت
# ======================================================================
def migrate_old_config():
    if c.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] > 0:
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
    dest_list = [x.strip() for x in old_dests.split(",") if x.strip()] or ["@VaslZone"]
    for dest in dest_list:
        c.execute("""INSERT INTO profiles (dest_name, sources, banner_config, banner_proxy,
            interval_min, max_post, max_proxies, post_configs, post_proxies,
            ping_mode, last_num, created_at, display_name, show_numbers, custom_query,
            show_date_config, show_date_proxy)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (dest, old_sources, old_banner_config, old_banner_proxy,
             old_interval, old_max_post, old_max_proxies,
             old_post_configs, old_post_proxies, old_ping_mode, old_last_num,
             datetime.now().isoformat(), "", 1, "", 1, 1))
    conn.commit()
migrate_old_config()

# ======================================================================
# توابع پروفایل
# ======================================================================
def get_profiles():
    rows = c.execute("SELECT * FROM profiles ORDER BY id").fetchall()
    return [{
        "id": r[0], "dest_name": r[1], "sources": r[2],
        "banner_config": r[3], "banner_proxy": r[4],
        "interval_min": r[5], "max_post": r[6], "max_proxies": r[7],
        "post_configs": r[8], "post_proxies": r[9], "ping_mode": r[10],
        "last_num": r[11], "created_at": r[12], "display_name": r[13] if len(r)>13 else "",
        "show_numbers": r[14] if len(r)>14 else 1,
        "custom_query": r[15] if len(r)>15 else "",
        "show_date_config": r[16] if len(r)>16 else 1,
        "show_date_proxy": r[17] if len(r)>17 else 1
    } for r in rows]

def get_profile(profile_id):
    r = c.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not r:
        return None
    return {
        "id": r[0], "dest_name": r[1], "sources": r[2],
        "banner_config": r[3], "banner_proxy": r[4],
        "interval_min": r[5], "max_post": r[6], "max_proxies": r[7],
        "post_configs": r[8], "post_proxies": r[9], "ping_mode": r[10],
        "last_num": r[11], "created_at": r[12], "display_name": r[13] if len(r)>13 else "",
        "show_numbers": r[14] if len(r)>14 else 1,
        "custom_query": r[15] if len(r)>15 else "",
        "show_date_config": r[16] if len(r)>16 else 1,
        "show_date_proxy": r[17] if len(r)>17 else 1
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
    c.execute("""INSERT INTO profiles (dest_name, sources, banner_config, banner_proxy,
        interval_min, max_post, max_proxies, post_configs, post_proxies,
        ping_mode, last_num, created_at, display_name, show_numbers, custom_query,
        show_date_config, show_date_proxy)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (dest_name, sources, banner_config, banner_proxy, interval_min, max_post,
         max_proxies, post_configs, post_proxies, ping_mode, last_num,
         get_tehran_time(), display_name, show_numbers, custom_query,
         show_date_config, show_date_proxy))
    conn.commit()
    return c.lastrowid

def update_profile(profile_id, **kwargs):
    allowed = ["dest_name", "sources", "banner_config", "banner_proxy",
               "interval_min", "max_post", "max_proxies", "post_configs",
               "post_proxies", "ping_mode", "last_num", "display_name",
               "show_numbers", "custom_query", "show_date_config", "show_date_proxy"]
    for key, value in kwargs.items():
        if key in allowed:
            c.execute(f"UPDATE profiles SET {key}=? WHERE id=?", (value, profile_id))
    conn.commit()

def delete_profile(profile_id):
    for table in ["sponsors", "seen", "proxies_seen", "source_passwords", "last_scrape", "processed_messages"]:
        c.execute(f"DELETE FROM {table} WHERE profile_id=?", (profile_id,))
    c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    conn.commit()

def get_profile_sources(profile_id):
    prof = get_profile(profile_id)
    return [x.strip() for x in prof["sources"].split(",") if x.strip()] if prof else []

def set_profile_sources(profile_id, sources_list):
    update_profile(profile_id, sources=",".join(sources_list))

def get_profile_dest(profile_id):
    prof = get_profile(profile_id)
    return prof["dest_name"] if prof else None

def set_profile_dest(profile_id, dest):
    update_profile(profile_id, dest_name=dest)

# --- getter/setter های پرکاربرد ---
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

# ======================================================================
# رمزها و اسپانسرها
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

def add_sponsor(profile_id, name, url, button_text="Advertisement", color="blue"):
    c.execute("""INSERT INTO sponsors (profile_id, name, url, button_text, color, enabled, created_at)
        VALUES (?,?,?,?,?,1,?)""", (profile_id, name, url, button_text, color, get_tehran_time()))
    conn.commit()

def remove_sponsor(sid):
    c.execute("DELETE FROM sponsors WHERE id=?", (sid,))
    conn.commit()

def toggle_sponsor(sid):
    c.execute("UPDATE sponsors SET enabled = CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE id=?", (sid,))
    conn.commit()

def update_sponsor(sid, **kwargs):
    for key, value in kwargs.items():
        if key in ["name", "url", "button_text", "color"]:
            c.execute(f"UPDATE sponsors SET {key}=? WHERE id=?", (value, sid))
    conn.commit()

def get_enabled_sponsors(profile_id):
    return c.execute(
        "SELECT id, name, url, button_text, color FROM sponsors WHERE enabled=1 AND profile_id=?",
        (profile_id,)).fetchall()

def get_all_sponsors(profile_id):
    return c.execute(
        "SELECT id, name, url, button_text, color, enabled FROM sponsors WHERE profile_id=? ORDER BY id DESC",
        (profile_id,)).fetchall()

# ======================================================================
# توابع کمکی اصلی
# ======================================================================
def country_to_flag(code):
    if not code or len(code) != 2 or not code.isalpha():
        return "🌐"
    return chr(ord(code[0]) + 127397) + chr(ord(code[1]) + 127397)

async def get_flag_for_ip(ip):
    cached = c.execute("SELECT country, flag FROM country_cache WHERE ip=?", (ip,)).fetchone()
    if cached and len(cached[1]) > 1:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=3) as cl:
            r = await cl.get(f"http://ip-api.com/json/{ip}?fields=countryCode",
                             headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                data = r.json()
                country = data.get("countryCode", "").upper()
                if country:
                    flag = country_to_flag(country)
                    c.execute("INSERT OR REPLACE INTO country_cache VALUES (?,?,?)", (ip, country, flag))
                    conn.commit()
                    return flag
    except:
        pass
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
            except:
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
    for m in re.finditer(r'https?://t\.me/proxy\?[^\s<>"\']+', text, re.IGNORECASE):
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
                data = json.loads(base64.b64decode(b64).decode('utf-8', errors='ignore'))
                uid = data.get("id", "") or data.get("uuid", "")
                host = f"{data.get('add','')}:{data.get('port','')}"
                return uid, host
            except:
                return ("vmess_" + hashlib.md5(after.encode()).hexdigest()[:16]), after
        else:
            if "@" in after:
                uid, host = after.split("@", 1)
            else:
                uid, host = after, ""
            if "/" in host:
                host = host.split("/")[0]
            return (uid.split("?")[0].split("#")[0], host.split("?")[0].split("#")[0])
    except:
        return "", ""

def is_already_posted(profile_id, url):
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        return False
    return c.execute("SELECT 1 FROM seen WHERE uuid=? AND address=? AND profile_id=?", (uid, host, profile_id)).fetchone() is not None

def mark_as_posted(profile_id, url, source=""):
    uid, host = extract_uuid_and_address(url)
    if not uid or not host:
        return
    now = get_tehran_time()
    c.execute("""INSERT INTO seen (uuid,address,source,first_seen,last_posted,profile_id)
        VALUES (?,?,?,?,?,?) ON CONFLICT(uuid,address) DO UPDATE SET last_posted=excluded.last_posted""",
        (uid, host, source, now, now, profile_id))
    conn.commit()

def is_message_processed(profile_id, source, message_id):
    return c.execute("SELECT 1 FROM processed_messages WHERE source=? AND message_id=? AND profile_id=?",
                     (source, message_id, profile_id)).fetchone() is not None

def mark_message_processed(profile_id, source, message_id):
    c.execute("INSERT OR REPLACE INTO processed_messages (source, message_id, profile_id) VALUES (?,?,?)",
              (source, message_id, profile_id))
    conn.commit()

def is_proxy_posted(profile_id, proxy_url):
    return c.execute("SELECT 1 FROM proxies_seen WHERE proxy_url=? AND profile_id=?", (proxy_url, profile_id)).fetchone() is not None

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
    return url.split('#')[0] if '#' in url else url

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
    except:
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
    existing = parse_qs(parsed.query)
    custom = parse_qs(custom_query)
    merged = {}
    for k, v in custom.items():
        merged[k] = v[-1] if v else ""
    for k, v in existing.items():
        if k not in merged:
            merged[k] = v[-1] if v else ""
    new_query = urlencode(merged, doseq=True)
    new_base = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, ''))
    return new_base + ('#' + fragment if fragment else '')

def change_link_display_name(url, new_name, flag, custom_query=""):
    protocol = url.split('://')[0].lower() if '://' in url else ''
    if custom_query and protocol != 'vmess':
        url = add_custom_query_to_url(url, custom_query, protocol)
    base = strip_url_fragment(url)
    fragment = f"{new_name} {flag}"
    return f"{base}#{quote(fragment, safe='')}"

def append_channel_and_flag_encoded(url, channel, flag, custom_query=""):
    protocol = url.split('://')[0].lower() if '://' in url else ''
    if custom_query and protocol != 'vmess':
        url = add_custom_query_to_url(url, custom_query, protocol)
    base = strip_url_fragment(url)
    fragment = f"{channel} {flag}"
    return f"{base}#{quote(fragment, safe='')}"

# ======================================================================
# پینگ و اسکرپ
# ======================================================================
async def host_to_ip(host):
    try:
        return socket.gethostbyname(host)
    except:
        return None

async def test_tcp_ping(host, port):
    try:
        loop = asyncio.get_running_loop()
        start = loop.time()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.5)
        writer.close()
        await writer.wait_closed()
        return True, round((loop.time() - start) * 1000)
    except:
        return False, 0

async def ping_from_iran_only(host, port=None):
    ip = await host_to_ip(host) or host
    try:
        async with httpx.AsyncClient(timeout=8) as cl:
            r = await cl.get(f"https://check-host.net/check-ping?host={ip}&json=1",
                             headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
            if r.status_code == 200:
                data = r.json()
                iran_pings = []
                iran_keywords = ["ir1","ir2","ir3","ir4","ir5","ir6","ir7","ir8","ir9","iran","tehran",
                                 "ir-","-ir","ir_","_ir","mci","hamrahe","rightel","shatel","iranel",
                                 "teh","shiraz","isfahan","mashhad","tabriz","ahvaz"]
                for node_name, results in data.get("nodes", {}).items():
                    if not isinstance(results, list):
                        continue
                    node_lower = node_name.lower()
                    for v in results:
                        if isinstance(v, (int, float)) and v > 0:
                            if any(kw in node_lower for kw in iran_keywords):
                                iran_pings.append(int(v))
                            break
                if iran_pings:
                    return int(sum(iran_pings) / len(iran_pings)), True, len(iran_pings)
    except:
        pass
    if port is not None:
        ok, ping = await test_tcp_ping(host, port)
        if ok:
            return ping, True, 0
    else:
        for test_port in [443, 80, 8080, 8443, 2053, 2096, 2087, 2083]:
            ok, ping = await test_tcp_ping(host, test_port)
            if ok:
                return ping, True, 0
    return 0, False, 0

async def check_full_link_ping(url):
    host, port = extract_host(url)
    if not host:
        return 0, False, 0
    return await ping_from_iran_only(host, port)

def decrypt_subscription(data: bytes, passwords: list):
    protocols = ("vless://", "vmess://", "trojan://", "hy2://", "tuic://")
    try:
        text = data.decode('utf-8', errors='ignore')
        if any(p in text for p in protocols):
            return text
    except:
        pass
    try:
        decoded = base64.b64decode(data + b'=' * (-len(data) % 4))
        text = decoded.decode('utf-8', errors='ignore')
        if any(p in text for p in protocols):
            return text
    except:
        pass
    try:
        decoded = base64.b64decode(data + b'=' * (-len(data) % 4)).decode('utf-8', errors='ignore')
        text = unquote(decoded)
        if any(p in text for p in protocols):
            return text
    except:
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
                text = unpad(AES.new(kiv, AES.MODE_CBC, kiv).decrypt(raw), 16).decode('utf-8', errors='ignore')
                if text and any(p in text for p in protocols):
                    return text
            except:
                pass
        except:
            pass
    return None

def get_v2ray_links_from_text(text):
    results = []
    for pattern in [r'vless://[^\s<>"\']+', r'vmess://[^\s<>"\']+', r'trojan://[^\s<>"\']+',
                    r'hy2://[^\s<>"\']+', r'tuic://[^\s<>"\']+']:
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
            except:
                continue
            pws = get_pw(profile_id, source)
            text = decrypt_subscription(bytes(data), pws)
            if not text:
                text = bytes(data).decode('utf-8', errors='ignore')
            links = get_v2ray_links_from_text(text) or extract_links_from_text(text)
            if links:
                new_links.extend(links)
            mark_message_processed(profile_id, source, msg.message_id)
        return new_links
    except:
        return []

_scrape_cache = {}
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
]

async def scrape_channel_with_retry(profile_id, channel, only_new=True):
    try:
        return await _scrape_channel_internal(profile_id, channel, only_new)
    except Exception as e:
        log.error(f"scrape {channel} error: {e}")
        return [], []

async def _scrape_channel_internal(profile_id, channel, only_new=True):
    import time as _t
    current_time = _t.time()
    url = f"https://t.me/s/{channel.lstrip('@')}"
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
            await asyncio.sleep(30)
            r = await cl.get(url, headers=headers)
        if r.status_code != 200:
            return [], []
        html_text = r.text
        config_links = extract_links_from_text(html_text)
        proxy_links = extract_proxy_links_from_text(html_text)
        update_last_scrape_time(profile_id, channel, get_tehran_time())
        cached = _scrape_cache.get((profile_id, channel), (0, [], []))
        old_configs = cached[1] if len(cached) > 1 else []
        old_proxies = cached[2] if len(cached) > 2 else []
        new_configs = [link for link in config_links if link not in old_configs]
        new_proxies = [link for link in proxy_links if link not in old_proxies]
        _scrape_cache[(profile_id, channel)] = (current_time, config_links, proxy_links)
        return new_configs, new_proxies

# ======================================================================
# ارسال به مقصد
# ======================================================================
async def send_to_destination(bot, profile_id, text, buttons=None):
    dest = get_profile_dest(profile_id)
    if not dest:
        return False
    chunks = split_text(text, 4096)
    success = True
    for idx, chunk in enumerate(chunks):
        try:
            reply_markup = InlineKeyboardMarkup(buttons) if buttons and idx == 0 else None
            await bot.send_message(dest, chunk, parse_mode="HTML", reply_markup=reply_markup,
                                   disable_web_page_preview=True)
        except:
            try:
                plain = re.sub(r'<[^>]+>', '', chunk)
                await bot.send_message(dest, plain[:4096], disable_web_page_preview=True)
            except:
                success = False
    return success

def split_text(text, max_len=4096):
    if len(text) <= max_len:
        return [text]
    lines = text.split('\n')
    chunks, current, current_len = [], [], 0
    for line in lines:
        if len(line) > max_len:
            if current:
                chunks.append('\n'.join(current))
                current, current_len = [], 0
            for i in range(0, len(line), max_len):
                chunks.append(line[i:i+max_len])
            continue
        if current_len + len(line) + 1 > max_len:
            chunks.append('\n'.join(current))
            current, current_len = [line], len(line)
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
            header = f"<b>#{n}</b> {channel_display} {flag} {ping}ms" if ping > 0 else f"<b>#{n}</b> {channel_display} {flag}"
        else:
            header = f"{channel_display} {flag} {ping}ms" if ping > 0 else f"{channel_display} {flag}"
        configs_text += header + "\n" + f"<pre>{modified_url}</pre>" + "\n"
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
        style_map = {"blue": "primary", "green": "success", "red": "danger"}
        all_buttons.append([InlineKeyboardButton(clean_txt, url=surl, style=style_map.get(scolor, "primary"))])
    banner_config = get_profile_banner_config(profile_id)
    configs_text = configs_text.rstrip()
    try:
        text = banner_config.format(configs=configs_text)
    except:
        text = "✦ V2Ray Config List\n\n{configs}\n\n◈ 📢 Channel\n↳ @Auto_Server\n◈ #کانفیگ #ویتوری".format(configs=configs_text)
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
        normalized = normalize_telegram_proxy(proxy_url)
        clean = clean_proxy_link(normalized)
        safe = html.escape(clean, quote=False)
        proxy_text += f"• {flag} <a href=\"{safe}\">Telegram Proxy</a>\n"
        mark_proxy_posted(profile_id, clean)
    proxy_count = len(proxies_with_ping[:max_proxies])
    banner_proxy = get_profile_banner_proxy(profile_id)
    date_str = get_tehran_date() if show_date else ""
    try:
        text = banner_proxy.format(date=date_str, count=proxy_count, proxies=proxy_text)
    except:
        text = f"🌐 Proxies\n{proxy_text}"
    return proxy_count, (text, None)

async def post_working_configs(bot, profile_id, working, proxies_with_ping, source_for_seen="", force=False):
    dest = get_profile_dest(profile_id)
    if not dest:
        return 0, "no destination"
    post_configs_enabled = get_profile_post_configs(profile_id) if not force else True
    post_proxies_enabled = get_profile_post_proxies(profile_id) if not force else True
    total_configs, total_proxies, results = 0, 0, []
    if post_configs_enabled and working:
        max_post = get_profile_max_post(profile_id)
        unique = [(u, p, c) for u, p, c in working if not is_already_posted(profile_id, u)]
        if unique:
            for i in range(0, len(unique), max_post):
                chunk = unique[i:i+max_post]
                cnt, payload = await post_configs(bot, profile_id, chunk, source_for_seen)
                if cnt > 0 and payload:
                    text, buttons = payload
                    if await send_to_destination(bot, profile_id, text, buttons):
                        total_configs += cnt
                        results.append(f"{cnt} configs")
                    else:
                        plain = re.sub(r'<[^>]+>', '', text)
                        if await send_to_destination(bot, profile_id, plain, None):
                            total_configs += cnt
                            results.append(f"{cnt} configs (plain)")
    if post_proxies_enabled and proxies_with_ping:
        valid = [p for p in proxies_with_ping if "t.me/proxy" in p[0].lower()]
        if valid:
            unique = [p for p in valid if not is_proxy_posted(profile_id, p[0])]
            if unique:
                cnt, payload = await post_proxies(bot, profile_id, unique)
                if cnt > 0 and payload:
                    if await send_to_destination(bot, profile_id, payload[0], None):
                        total_proxies = cnt
                        results.append(f"{cnt} proxies")
    if not results:
        return 0, "no new content"
    return total_configs, "posted " + " and ".join(results)

# ======================================================================
# سیکل کامل و حلقه خودکار
# ======================================================================
async def run_full_cycle_for_profile(bot, profile_id, only_new=True):
    log.info(f"🔄 run_full_cycle profile {profile_id}")
    profile = get_profile(profile_id)
    if not profile:
        return 0, "profile not found"
    sources = get_profile_sources(profile_id)
    if not sources:
        return 0, "no sources"
    dest = get_profile_dest(profile_id)
    if not dest:
        return 0, "no destination"

    all_configs, all_proxies = [], []
    seen_configs, seen_proxies, seen_urls = set(), set(), set()
    for src in sources:
        config_links, proxy_links = await scrape_channel_with_retry(profile_id, src, only_new=True)
        for link in config_links:
            if link not in seen_urls and link not in seen_configs:
                seen_urls.add(link)
                seen_configs.add(link)
                all_configs.append((link, src))
        for link in proxy_links:
            if link not in seen_urls:
                seen_urls.add(link)
                norm = normalize_proxy_url(link)
                if norm and norm not in seen_proxies and not is_proxy_posted(profile_id, norm):
                    seen_proxies.add(norm)
                    all_proxies.append(norm)
        file_links = await fetch_files_from_channel(bot, profile_id, src, src)
        for link in file_links:
            if link not in seen_urls:
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
    working = []
    if new_configs:
        to_test = new_configs[:10]
        log.info(f"Testing {len(to_test)} configs...")
        sem = asyncio.Semaphore(10)
        async def _check(u):
            async with sem:
                try:
                    ping, ok, cnt = await check_full_link_ping(u)
                    return (u, True, ping, cnt) if ok else (u, False, 0, 0)
                except:
                    return (u, False, 0, 0)
        rs = await asyncio.gather(*[_check(u) for u in to_test], return_exceptions=True)
        for r in rs:
            if isinstance(r, Exception) or not r[1]:
                continue
            working.append((r[0], r[2], r[3]))

    proxy_with_ping = []
    if all_proxies:
        valid = [p for p in all_proxies if "t.me/proxy" in p.lower()]
        if valid:
            sem = asyncio.Semaphore(10)
            async def check_proxy(proxy_url):
                async with sem:
                    ping, ok, cnt = await check_full_link_ping(proxy_url)
                    host, _ = extract_host(proxy_url)
                    ip = await host_to_ip(host) if host else None
                    flag = await get_flag_for_ip(ip) if ip else "🌐"
                    return proxy_url, ping if ok else 0, flag
            rs = await asyncio.gather(*[check_proxy(p) for p in valid[:10]], return_exceptions=True)
            for r in rs:
                if isinstance(r, Exception):
                    continue
                proxy_with_ping.append(r)

    if not working and not proxy_with_ping:
        return 0, "no working configs or proxies"
    return await post_working_configs(bot, profile_id, working, proxy_with_ping)

async def profile_loop(bot, profile_id):
    log.info(f"🔄 profile_loop started for {profile_id}")
    while True:
        try:
            profile = get_profile(profile_id)
            if not profile:
                break
            interval = profile["interval_min"]
            start_time = datetime.now()
            log.info(f"⏰ Auto run for profile {profile_id} at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
            n, m = await run_full_cycle_for_profile(bot, profile_id, only_new=True)
            log.info(f"[auto {profile_id}] {n} - {m}")
            elapsed = (datetime.now() - start_time).total_seconds()
            sleep_time = max(0, interval * 60 - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)
            else:
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"profile_loop error: {e}")
            await asyncio.sleep(60)

# ======================================================================
# کیبوردها و پیام‌ها
# ======================================================================
BOT_REF, BOT_LANG = None, "fa"

T = {
    "fa": {
        "welcome": "🤖 **بات جمع‌آوری کانفیگ و پروکسی**\n📡 پروفایل‌ها: {profiles}\n🔢 بعدی: #{next_n}",
        "admin_panel": "🔐 **مدیریت پروفایل**\n\n📡 منابع: {srcs} | 🎯 مقصد: {dest}\n🎨 نام: {name} | 🔢 #{num}\n⏰ {interval}m | 🎯 max:{max_post}\n📢 اسپانسر: {sponsor}\n🌍 پینگ: {ping_mode}\n📡 کانفیگ: {cfg_status} | 🌐 پروکسی: {prx_status}\n📝 نام نمایشی: {display_name}\n🔢 شماره‌گذاری: {numbers_status}\n🔗 کوئری: {custom_query}\n📅 تاریخ کانفیگ: {date_cfg}\n📅 تاریخ پروکسی: {date_prx}",
        "btn_back": "🔙 برگشت",
        "btn_add_source": "➕ منبع",
        "btn_add_dest": "➕ مقصد",
        "btn_dest_list": "📋 مقصدها",
        "btn_sponsors": "📢 اسپانسر",
        "btn_pw": "🔑 رمزها",
        "btn_set_dest": "🎯 تنظیم مقصد",
        "btn_set_name": "🎨 نام",
        "btn_set_display_name": "📝 نام نمایشی",
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
        "btn_manual_send": "📤 ارسال دستی",
        "btn_rename_server": "📝 تغییر نام سرور",
        "btn_manage_sources": "📡 مدیریت منابع",
        "btn_toggle_numbers": "🔢 شماره‌گذاری: {status}",
        "btn_set_custom_query": "🔗 کوئری سفارشی",
        "btn_toggle_date_cfg": "📅 تاریخ کانفیگ: {status}",
        "btn_toggle_date_prx": "📅 تاریخ پروکسی: {status}",
        "btn_clear_custom_query": "🧹 خالی",
        "btn_clear_display_name": "🧹 خالی",
        "btn_clear_custom_query_prompt": "خالی",
        "btn_clear_banner": "🧹 خالی",
        "sp_edit_prompt": "📢 **ویرایش اسپانسر**\nنام: {name}\nلینک: {url}\nمتن: {text}\nرنگ: {color}",
        "sp_edit_name": "نام جدید (خالی برای عدم تغییر):",
        "sp_edit_url": "لینک جدید (خالی برای عدم تغییر):",
        "sp_edit_text": "متن جدید (خالی برای عدم تغییر):",
        "sp_edit_color": "رنگ جدید (blue/green/red):",
        "sp_updated": "✅ اسپانسر به‌روزرسانی شد.",
        "btn_edit_sponsor": "✏️ ویرایش",
        "custom_query_set": "✅ کوئری تنظیم شد: {query}",
        "custom_query_prompt": "🔗 کوئری سفارشی (مثلا Telegram=@MyChannel):",
        "profile_list": "📋 **پروفایل‌ها**\n\n{list}",
        "profile_add_prompt": "📝 نام مقصد جدید:",
        "profile_added": "✅ پروفایل '{name}' ساخته شد.",
        "profile_deleted": "✅ پروفایل حذف شد.",
        "profile_not_found": "❌ پروفایل یافت نشد.",
        "source_list": "📡 **منابع {name}**\n\n{sources}",
        "source_deleted": "✅ منبع حذف شد.",
        "no_sources": "❌ هیچ منبعی تنظیم نشده",
        "pw_title": "🔐 رمزهای {source}:",
        "pw_prompt": "🔐 رمز جدید (چند تا با کاما یا خط جدید):",
        "pw_added": "✅ رمز برای {source} اضافه شد.",
        "pw_none": "(خالی)",
        "sp_title": "📢 اسپانسرها:",
        "sp_none": "(خالی)",
        "sp_added": "✅ '{name}' اضافه شد.",
        "sp_removed": "✅ حذف شد.",
        "sp_prompt": "📢 اسپانسر جدید:\nمراحل: نام → لینک → متن دکمه → رنگ",
        "sp_err": "❌ فرمت نامعتبر.",
        "pw_common_added": "✅ +۳ رمز رایج.",
        "doc_done": "🎉 {n} کانفیگ و {p} پروکسی پست شد.",
        "doc_dup": "همه تکراری بودند.",
        "toggle_numbers_ok": "✅ شماره‌گذاری {'فعال' if status else 'غیرفعال'} شد.",
        "date_cfg_toggle": "✅ تاریخ کانفیگ {'فعال' if status else 'غیرفعال'} شد.",
        "date_prx_toggle": "✅ تاریخ پروکسی {'فعال' if status else 'غیرفعال'} شد.",
        "banner_ok": "✅ بنر ذخیره شد.",
        "banner_err": "❌ باید {configs} یا {proxies} داشته باشد.",
        "interval_ok": "✅ هر {n} دقیقه.",
        "interval_err": "❌ ۱ تا ۱۴۴۰ دقیقه.",
        "interval_wrong": "❌ فقط عدد.",
        "max_ok": "✅ حداکثر {n}.",
        "max_err": "❌ ۱ تا ۵۰.",
        "max_wrong": "❌ فقط عدد.",
        "reset_ok": "✅ شماره ریست شد (#۱).",
        "clear_q1": "⚠️ همه چیز پاک شود؟",
        "test_ok": "✅ تست ارسال شد.",
        "test_err": "❌ خطا:\n<code>{err}</code>",
        "no_pings": "❌ پینگ نداد.",
        "dest_set": "✅ مقصد: {dest}",
        "name_set": "✅ نام: {name}",
        "display_name_set": "✅ نام نمایشی: {name}",
        "lang_ok": "✅ زبان تغییر کرد.",
        "private": "🔒 خصوصی.",
        "manual_send_prompt": "📤 پیام یا فایل حاوی لینک‌ها را بفرستید.",
        "manual_send_cancel": "❌ لغو شد.",
        "manual_send_processing": "⏳ پردازش...",
        "manual_send_done": "✅ ارسال دستی کامل شد.",
        "rename_prompt": "📝 پیام یا فایل کانفیگ را بفرستید تا نام‌ها تغییر کنند.",
        "btn_toggle_configs": "📡 کانفیگ: {status}",
        "btn_toggle_proxies": "🌐 پروکسی: {status}",
        "toggle_configs": "✅ ارسال کانفیگ {'فعال' if status else 'غیرفعال'} شد.",
        "toggle_proxies": "✅ ارسال پروکسی {'فعال' if status else 'غیرفعال'} شد.",
    },
    "en": {
        "welcome": "🤖 **Config & Proxy Bot**\n📡 Profiles: {profiles}\n🔢 Next #: {next_n}",
        "admin_panel": "🔐 **Profile Management**\n\n📡 Sources: {srcs} | 🎯 Dest: {dest}\n🎨 Name: {name} | 🔢 #{num}\n⏰ {interval}m | 🎯 max:{max_post}\n📢 Sponsor: {sponsor}\n🌍 Ping: {ping_mode}\n📡 Configs: {cfg_status} | 🌐 Proxies: {prx_status}\n📝 Display: {display_name}\n🔢 Numbering: {numbers_status}\n🔗 Custom Query: {custom_query}\n📅 Config Date: {date_cfg}\n📅 Proxy Date: {date_prx}",
        "btn_back": "🔙 Back",
        "btn_add_source": "➕ Source",
        "btn_add_dest": "➕ Destination",
        "btn_dest_list": "📋 Destinations",
        "btn_sponsors": "📢 Sponsors",
        "btn_pw": "🔑 Passwords",
        "btn_set_dest": "🎯 Set Destination",
        "btn_set_name": "🎨 Name",
        "btn_set_display_name": "📝 Display Name",
        "btn_set_banner_config": "📝 Config Banner",
        "btn_set_banner_proxy": "📝 Proxy Banner",
        "btn_set_time": "⏰ Interval",
        "btn_set_max": "🎯 Max Post",
        "btn_stats": "📊 Stats",
        "btn_test": "🧪 Test",
        "btn_clear": "🗑 Clear DB",
        "btn_reset": "🔢 Reset Num",
        "btn_ping_mode": "🌍 Iran-Only",
        "btn_runnow": "▶️ Run Now",
        "btn_manual_send": "📤 Manual Send",
        "btn_rename_server": "📝 Rename Server",
        "btn_manage_sources": "📡 Manage Sources",
        "btn_toggle_numbers": "🔢 Numbering: {status}",
        "btn_set_custom_query": "🔗 Custom Query",
        "btn_toggle_date_cfg": "📅 Config Date: {status}",
        "btn_toggle_date_prx": "📅 Proxy Date: {status}",
        "btn_clear_custom_query": "🧹 Clear",
        "btn_clear_display_name": "🧹 Clear",
        "btn_clear_custom_query_prompt": "Clear",
        "btn_clear_banner": "🧹 Clear",
        "sp_edit_prompt": "📢 **Edit Sponsor**\nName: {name}\nURL: {url}\nText: {text}\nColor: {color}",
        "sp_edit_name": "New name (empty to keep):",
        "sp_edit_url": "New URL (empty to keep):",
        "sp_edit_text": "New text (empty to keep):",
        "sp_edit_color": "New color (blue/green/red):",
        "sp_updated": "✅ Sponsor updated.",
        "btn_edit_sponsor": "✏️ Edit",
        "custom_query_set": "✅ Custom query set: {query}",
        "custom_query_prompt": "🔗 Custom query (e.g. Telegram=@MyChannel):",
        "profile_list": "📋 **Profiles**\n\n{list}",
        "profile_add_prompt": "📝 New destination name:",
        "profile_added": "✅ Profile '{name}' created.",
        "profile_deleted": "✅ Profile deleted.",
        "profile_not_found": "❌ Profile not found.",
        "source_list": "📡 **Sources for {name}**\n\n{sources}",
        "source_deleted": "✅ Source deleted.",
        "no_sources": "❌ No sources configured.",
        "pw_title": "🔐 Passwords for {source}:",
        "pw_prompt": "🔐 New password(s) (comma or newline):",
        "pw_added": "✅ Password for {source} added.",
        "pw_none": "(none)",
        "sp_title": "📢 Sponsors:",
        "sp_none": "(none)",
        "sp_added": "✅ '{name}' added.",
        "sp_removed": "✅ Removed.",
        "sp_prompt": "📢 New sponsor:\nSteps: Name → URL → Button text → Color",
        "sp_err": "❌ Invalid format.",
        "pw_common_added": "✅ +3 common passwords.",
        "doc_done": "🎉 {n} configs and {p} proxies posted.",
        "doc_dup": "All duplicates.",
        "toggle_numbers_ok": "✅ Numbering {'enabled' if status else 'disabled'}.",
        "date_cfg_toggle": "✅ Config date {'enabled' if status else 'disabled'}.",
        "date_prx_toggle": "✅ Proxy date {'enabled' if status else 'disabled'}.",
        "banner_ok": "✅ Banner saved.",
        "banner_err": "❌ Must contain {configs} or {proxies}.",
        "interval_ok": "✅ Every {n} min.",
        "interval_err": "❌ 1-1440 min.",
        "interval_wrong": "❌ Number only.",
        "max_ok": "✅ Max {n}.",
        "max_err": "❌ 1-50.",
        "max_wrong": "❌ Number only.",
        "reset_ok": "✅ Reset to #1.",
        "clear_q1": "⚠️ Clear everything?",
        "test_ok": "✅ Test sent.",
        "test_err": "❌ Error:\n<code>{err}</code>",
        "no_pings": "❌ No ping.",
        "dest_set": "✅ Destination: {dest}",
        "name_set": "✅ Name: {name}",
        "display_name_set": "✅ Display name: {name}",
        "lang_ok": "✅ Language changed.",
        "private": "🔒 Private.",
        "manual_send_prompt": "📤 Send message/file with links.",
        "manual_send_cancel": "❌ Cancelled.",
        "manual_send_processing": "⏳ Processing...",
        "manual_send_done": "✅ Manual send completed.",
        "rename_prompt": "📝 Send message/file with config links to rename.",
        "btn_toggle_configs": "📡 Configs: {status}",
        "btn_toggle_proxies": "🌐 Proxies: {status}",
        "toggle_configs": "✅ Config posting {'enabled' if status else 'disabled'}.",
        "toggle_proxies": "✅ Proxy posting {'enabled' if status else 'disabled'}.",
    },
}

def msg(key, **kwargs):
    text = T[BOT_LANG].get(key, T["fa"].get(key, key))
    return text.format(**kwargs) if kwargs else text

# ======================================================================
# ساخت کیبوردها (با طبقه‌بندی)
# ======================================================================
def profiles_kb():
    btns = []
    for p in get_profiles():
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
        # بخش مدیریت سرور
        [InlineKeyboardButton("⚙️ تنظیمات سرور", callback_data="dummy", style="primary")],
        [InlineKeyboardButton(msg("btn_set_dest"), callback_data=f"dl_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_name"), callback_data=f"ac_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_display_name"), callback_data=f"adn_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_rename_server"), callback_data=f"rename_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_manage_sources"), callback_data=f"src_list_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success")],
        # بخش تنظیمات بنر و زمان
        [InlineKeyboardButton("📝 تنظیمات بنر و زمان", callback_data="dummy", style="primary")],
        [InlineKeyboardButton(msg("btn_set_banner_config"), callback_data=f"ab_config_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_banner_proxy"), callback_data=f"ab_proxy_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_time"), callback_data=f"ai_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_set_max"), callback_data=f"setmax_{profile_id}", style="primary")],
        # بخش تنظیمات پیشرفته
        [InlineKeyboardButton("⚡ تنظیمات پیشرفته", callback_data="dummy", style="primary")],
        [InlineKeyboardButton(ping_label, callback_data=f"tglping_{profile_id}", style="primary"),
         InlineKeyboardButton(cfg_btn, callback_data=f"tglcfg_{profile_id}", style="primary")],
        [InlineKeyboardButton(prx_btn, callback_data=f"tglproxy_{profile_id}", style="primary"),
         InlineKeyboardButton(num_btn, callback_data=f"togglenum_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_toggle_date_cfg", status=date_cfg_status), callback_data=f"tgl_date_cfg_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_toggle_date_prx", status=date_prx_status), callback_data=f"tgl_date_prx_{profile_id}", style="primary")],
        [InlineKeyboardButton(msg("btn_set_custom_query"), callback_data=f"setquery_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_clear_custom_query"), callback_data=f"clearquery_{profile_id}", style="danger")],
        # بخش مدیریت اسپانسر و رمز
        [InlineKeyboardButton("📢 مدیریت اسپانسر و رمز", callback_data="dummy", style="primary")],
        [InlineKeyboardButton(msg("btn_sponsors"), callback_data=f"sp_menu_{profile_id}", style="primary"),
         InlineKeyboardButton(msg("btn_pw"), callback_data=f"pw_list_{profile_id}", style="primary")],
        # بخش عملیات
        [InlineKeyboardButton("⚡ عملیات", callback_data="dummy", style="primary")],
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
    for i, s in enumerate(get_profile_sources(profile_id)):
        btns.append([InlineKeyboardButton(f"❌ {s}", callback_data=f"sd_{profile_id}_{i}", style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def destinations_kb(profile_id):
    dest = get_profile_dest(profile_id)
    btns = []
    if dest:
        btns.append([InlineKeyboardButton(f"❌ {dest}", callback_data=f"dd_{profile_id}", style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_add_dest"), callback_data=f"da_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def source_passwords_kb(profile_id, idx):
    sources = get_profile_sources(profile_id)
    if idx >= len(sources):
        return None
    src = sources[idx]
    btns = []
    for p in get_pw(profile_id, src):
        btns.append([InlineKeyboardButton(f"❌ {p}", callback_data=f"pr_{profile_id}_{idx}|{p}", style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_pw"), callback_data=f"pa_{profile_id}_{idx}", style="primary"),
                  InlineKeyboardButton("🎯 +3 common", callback_data=f"sauto_{profile_id}_{idx}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"pw_list_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def sponsors_kb(profile_id):
    rows = get_all_sponsors(profile_id)
    btns = []
    if rows:
        for sid, name, url, btn_text, color, enabled in rows:
            st = "✅" if enabled else "❌"
            style_map = {"blue": "primary", "green": "success", "red": "danger"}
            style = style_map.get(color, "primary")
            btns.append([InlineKeyboardButton(f"{st} {name[:25]}", callback_data=f"spt_{profile_id}_{sid}", style=style)])
            btns.append([InlineKeyboardButton("🗑", callback_data=f"spd_{profile_id}_{sid}", style="danger")])
            btns.append([InlineKeyboardButton(msg("btn_edit_sponsor"), callback_data=f"sp_edit_{profile_id}_{sid}", style="primary")])
    btns.append([InlineKeyboardButton("➕ add sponsor", callback_data=f"sp_add_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

def source_list_kb(profile_id):
    btns = []
    for i, s in enumerate(get_profile_sources(profile_id)):
        btns.append([InlineKeyboardButton(f"❌ {s}", callback_data=f"src_del_{profile_id}_{i}", style="danger")])
    btns.append([InlineKeyboardButton(msg("btn_add_source"), callback_data=f"sa_{profile_id}", style="success")])
    btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
    return InlineKeyboardMarkup(btns)

# ======================================================================
# دستورات و کالبک‌ها (با دکمه خالی)
# ======================================================================
async def cmd_start(u, ctx):
    if PRIVATE_MODE and u.effective_user.id != ADMIN_ID:
        return await u.message.reply_text(msg("private"))
    profiles = get_profiles()
    next_n = max([p["last_num"] for p in profiles], default=0) + 1 if profiles else 1
    txt = msg("welcome", profiles=len(profiles), next_n=next_n)
    btns = [[InlineKeyboardButton("📋 Manage Profiles", callback_data="profiles_list", style="primary")]]
    await u.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(btns))

async def cmd_admin(u, ctx):
    if u.effective_user.id == ADMIN_ID:
        await show_profiles_list(u.message)

async def show_profiles_list(msg_or_q):
    profiles = get_profiles()
    if not profiles:
        txt = "❌ هیچ پروفایلی وجود ندارد."
    else:
        lines = [f"• `{p['dest_name']}` (ID:{p['id']}) – {len(get_profile_sources(p['id']))} منبع, {p['interval_min']}m" for p in profiles]
        txt = msg("profile_list", list="\n".join(lines))
    kb = profiles_kb()
    try:
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_q.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def show_profile_admin(msg_or_q, profile_id):
    prof = get_profile(profile_id)
    if not prof:
        return await msg_or_q.edit_text(msg("profile_not_found")) if hasattr(msg_or_q, "edit_text") else None
    srcs = len(get_profile_sources(profile_id))
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
    ping_display = "ایران" if prof["ping_mode"] == "iran" else "جهانی"
    cfg_status = "✅" if prof["post_configs"] == 1 else "❌"
    prx_status = "✅" if prof["post_proxies"] == 1 else "❌"
    num_status = "✅" if show_num else "❌"
    date_cfg_status = "✅" if show_date_cfg else "❌"
    date_prx_status = "✅" if show_date_prx else "❌"
    txt = msg("admin_panel",
        srcs=srcs, dest=dest, name=dest, num=last_num,
        interval=interval, max_post=max_post, sponsor=sponsor_st,
        ping_mode=ping_display, cfg_status=cfg_status, prx_status=prx_status,
        display_name=display_name, numbers_status=num_status,
        custom_query=custom_query, date_cfg=date_cfg_status, date_prx=date_prx_status)
    kb = profile_admin_kb(profile_id)
    try:
        if hasattr(msg_or_q, "edit_text"):
            await msg_or_q.edit_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            await msg_or_q.reply_text(txt, parse_mode="HTML", reply_markup=kb)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

async def cmd_runnow(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return
    p = await u.message.reply_text("⏳ در حال اجرا...")
    results = []
    for prof in get_profiles():
        n, m = await run_full_cycle_for_profile(u.get_bot(), prof["id"], only_new=True)
        results.append(f"{prof['dest_name']}: {n} - {m}")
    await p.edit_text("✅ Done:\n" + "\n".join(results))

# ======================================================================
# کالبک اصلی (با دکمه خالی)
# ======================================================================
async def on_callback(u, ctx):
    q = u.callback_query
    try:
        if q.from_user.id != ADMIN_ID:
            return await q.answer("")
        await q.answer()
        d = q.data
        log.info(f"Callback: {d}")

        if d == "profiles_list":
            return await show_profiles_list(q.message)
        if d == "prof_add":
            ctx.user_data["action"] = "prof_add"
            return await q.edit_message_text(msg("profile_add_prompt"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data="profiles_list", style="primary")]]))
        if d == "back_home":
            return await show_profiles_list(q.message)

        if d.startswith("prof_"):
            profile_id = int(d.split("_")[1])
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("delprof_"):
            profile_id = int(d.split("_")[1])
            delete_profile(profile_id)
            await q.answer(msg("profile_deleted"))
            return await show_profiles_list(q.message)

        import re
        match = re.search(r'(\d+)', d)
        if not match:
            return await q.edit_message_text("⚠️ خطا")
        profile_id = int(match.group(1))

        # ===== بخش اسپانسر =====
        if d.startswith("sp_menu_"):
            return await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))
        if d.startswith("sp_add_"):
            ctx.user_data["sponsor_step"] = "name"
            ctx.user_data["sponsor_profile_id"] = profile_id
            return await q.edit_message_text(msg("sp_prompt"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]]))

        if d.startswith("sp_color_"):
            parts = d.split("_")
            color = parts[3]
            name = ctx.user_data.get("sponsor_name")
            url = ctx.user_data.get("sponsor_url")
            btn_text = ctx.user_data.get("sponsor_button_text")
            if name and url and btn_text:
                add_sponsor(profile_id, name, url, btn_text, color)
                await q.answer(msg("sp_added", name=name))
                await q.edit_message_text(f"✅ اسپانسر **{name}** اضافه شد.", parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]]))
                del ctx.user_data["sponsor_step"], ctx.user_data["sponsor_name"], ctx.user_data["sponsor_url"], ctx.user_data["sponsor_button_text"]
            else:
                await q.answer("❌ اطلاعات ناقص")
            return

        if d.startswith("sp_edit_"):
            sid = int(d.split("_")[2])
            sponsor = c.execute("SELECT name, url, button_text, color FROM sponsors WHERE id=?", (sid,)).fetchone()
            if not sponsor:
                return await q.answer("❌ اسپانسر یافت نشد")
            ctx.user_data["sponsor_edit_id"] = sid
            ctx.user_data["sponsor_edit_profile_id"] = profile_id
            txt = msg("sp_edit_prompt", name=sponsor[0], url=sponsor[1], text=sponsor[2], color=sponsor[3])
            return await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("نام", callback_data=f"sp_edit_name_{sid}", style="primary"),
                 InlineKeyboardButton("لینک", callback_data=f"sp_edit_url_{sid}", style="primary")],
                [InlineKeyboardButton("متن", callback_data=f"sp_edit_text_{sid}", style="primary"),
                 InlineKeyboardButton("رنگ", callback_data=f"sp_edit_color_{sid}", style="primary")],
                [InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]
            ]))

        for field in ["name", "url", "text"]:
            if d.startswith(f"sp_edit_{field}_"):
                sid = int(d.split("_")[3])
                ctx.user_data["sponsor_edit_field"] = field
                return await q.edit_message_text(msg(f"sp_edit_{field}"), reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_edit_{profile_id}_{sid}", style="primary")]]))

        if d.startswith("sp_edit_color_"):
            sid = int(d.split("_")[3])
            ctx.user_data["sponsor_edit_field"] = "color"
            return await q.edit_message_text("🎨 رنگ جدید (blue/green/red):", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔵 آبی", callback_data=f"sp_color_update_{sid}_blue", style="primary"),
                 InlineKeyboardButton("🟢 سبز", callback_data=f"sp_color_update_{sid}_green", style="success"),
                 InlineKeyboardButton("🔴 قرمز", callback_data=f"sp_color_update_{sid}_red", style="danger")],
                [InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_edit_{profile_id}_{sid}", style="primary")]
            ]))

        if d.startswith("sp_color_update_"):
            parts = d.split("_")
            sid = int(parts[3])
            color = parts[4]
            if color in ("blue", "green", "red"):
                update_sponsor(sid, color=color)
                await q.answer(msg("sp_updated"))
                await q.edit_message_text(msg("sp_updated"), reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]]))
            return

        if d.startswith("spt_"):
            sid = int(d.split("_")[2])
            toggle_sponsor(sid)
            await q.answer("تغییر وضعیت")
            return await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))

        if d.startswith("spd_"):
            sid = int(d.split("_")[2])
            remove_sponsor(sid)
            await q.answer(msg("sp_removed"))
            return await q.edit_message_text(msg("sp_title"), reply_markup=sponsors_kb(profile_id))

        # ===== مدیریت منابع =====
        if d.startswith("src_list_"):
            prof = get_profile(profile_id)
            sources = get_profile_sources(profile_id)
            src_text = "\n".join([f"• {s}" for s in sources]) if sources else "هیچ منبعی"
            txt = msg("source_list", name=prof["dest_name"], sources=src_text)
            return await q.edit_message_text(txt, reply_markup=source_list_kb(profile_id))

        if d.startswith("src_del_"):
            idx = int(d.split("_")[2])
            sources = get_profile_sources(profile_id)
            if 0 <= idx < len(sources):
                sources.pop(idx)
                set_profile_sources(profile_id, sources)
                await q.answer(msg("source_deleted"))
                prof = get_profile(profile_id)
                src_text = "\n".join([f"• {s}" for s in sources]) if sources else "هیچ منبعی"
                txt = msg("source_list", name=prof["dest_name"], sources=src_text)
                return await q.edit_message_text(txt, reply_markup=source_list_kb(profile_id))

        if d.startswith("sa_"):
            ctx.user_data["action"] = f"sa_{profile_id}"
            return await q.edit_message_text(msg("send_prompt"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("sd_"):
            idx = int(d.split("_")[2])
            sources = get_profile_sources(profile_id)
            if 0 <= idx < len(sources):
                sources.pop(idx)
                set_profile_sources(profile_id, sources)
                await q.answer(msg("source_deleted"))
                await show_profile_admin(q.message, profile_id)
            return

        # ===== رمزها =====
        if d.startswith("pw_list_"):
            sources = get_profile_sources(profile_id)
            if not sources:
                return await q.answer(msg("src_none"))
            btns = [[InlineKeyboardButton(f"{s} ({len(get_pw(profile_id, s))})", callback_data=f"sp_{profile_id}_{i}", style="primary")] for i, s in enumerate(sources)]
            btns.append([InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")])
            return await q.edit_message_text("🔑 انتخاب منبع:", reply_markup=InlineKeyboardMarkup(btns))

        if d.startswith("sp_"):
            idx = int(d.split("_")[2])
            sources = get_profile_sources(profile_id)
            if idx >= len(sources):
                return
            src = sources[idx]
            pws = get_pw(profile_id, src)
            body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
            return await q.edit_message_text(msg("pw_title", source=src) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))

        if d.startswith("pa_"):
            idx = int(d.split("_")[2])
            ctx.user_data["action"] = f"pa_{profile_id}_{idx}"
            return await q.edit_message_text(msg("pw_prompt"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_{profile_id}_{idx}", style="primary")]]))

        if d.startswith("pr_"):
            prefix, pwd = d.split("|", 1)
            idx = int(prefix.split("_")[2])
            sources = get_profile_sources(profile_id)
            if idx < len(sources):
                del_pw(profile_id, sources[idx], pwd)
                await q.answer(msg("removed"))
                src = sources[idx]
                pws = get_pw(profile_id, src)
                body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
                await q.edit_message_text(msg("pw_title", source=src) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))
            return

        if d.startswith("sauto_"):
            idx = int(d.split("_")[2])
            sources = get_profile_sources(profile_id)
            if idx < len(sources):
                for pw in ["v2ray", "free", "1234"]:
                    add_pw(profile_id, sources[idx], pw)
                await q.answer(msg("pw_common_added"))
                src = sources[idx]
                pws = get_pw(profile_id, src)
                body = "\n".join(f"- {p}" for p in pws) if pws else msg("pw_none")
                await q.edit_message_text(msg("pw_title", source=src) + "\n\n" + body, reply_markup=source_passwords_kb(profile_id, idx))
            return

        # ===== مقصد =====
        if d.startswith("dl_"):
            dest = get_profile_dest(profile_id)
            body = f"مقصد فعلی: {dest}" if dest else "هیچ مقصدی تنظیم نشده"
            return await q.edit_message_text(f"📋 **تنظیم مقصد**\n\n{body}", reply_markup=destinations_kb(profile_id))

        if d.startswith("da_"):
            ctx.user_data["action"] = f"da_{profile_id}"
            return await q.edit_message_text("📝 کانال مقصد جدید (با @ یا بدون):", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"dl_{profile_id}", style="primary")]]))

        if d.startswith("dd_"):
            set_profile_dest(profile_id, "")
            await q.answer(msg("removed"))
            return await q.edit_message_text("📋 **تنظیم مقصد**\n\nهیچ مقصدی تنظیم نشده", reply_markup=destinations_kb(profile_id))

        # ===== سایر تنظیمات با دکمه خالی =====
        if d.startswith("ac_"):
            ctx.user_data["action"] = f"ac_{profile_id}"
            return await q.edit_message_text("🎨 نام جدید (خالی برای حذف):", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("adn_"):
            ctx.user_data["action"] = f"adn_{profile_id}"
            kb = [[InlineKeyboardButton(msg("btn_clear_display_name"), callback_data=f"cleardisplay_{profile_id}", style="danger")],
                  [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]
            return await q.edit_message_text("📝 نام نمایشی جدید (خالی برای حذف):", reply_markup=InlineKeyboardMarkup(kb))

        if d.startswith("cleardisplay_"):
            set_profile_display_name(profile_id, "")
            await q.answer("✅ نام نمایشی پاک شد")
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("rename_"):
            ctx.user_data["action"] = f"rename_{profile_id}"
            return await q.edit_message_text(msg("rename_prompt"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("ab_config_"):
            ctx.user_data["action"] = f"ab_config_{profile_id}"
            kb = [[InlineKeyboardButton(msg("btn_clear_banner"), callback_data=f"clearbanner_config_{profile_id}", style="danger")],
                  [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]
            return await q.edit_message_text("📝 بنر کانفیگ (باید شامل {configs} باشد):", reply_markup=InlineKeyboardMarkup(kb))

        if d.startswith("clearbanner_config_"):
            update_profile(profile_id, banner_config="")
            await q.answer("✅ بنر پاک شد")
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("ab_proxy_"):
            ctx.user_data["action"] = f"ab_proxy_{profile_id}"
            kb = [[InlineKeyboardButton(msg("btn_clear_banner"), callback_data=f"clearbanner_proxy_{profile_id}", style="danger")],
                  [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]
            return await q.edit_message_text("📝 بنر پروکسی (باید شامل {proxies} باشد):", reply_markup=InlineKeyboardMarkup(kb))

        if d.startswith("clearbanner_proxy_"):
            update_profile(profile_id, banner_proxy="")
            await q.answer("✅ بنر پاک شد")
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("ai_"):
            ctx.user_data["action"] = f"ai_{profile_id}"
            return await q.edit_message_text(f"⏰ زمان فعلی: {get_profile_interval(profile_id)}m\nعدد جدید (۱-۱۴۴۰):", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("setmax_"):
            ctx.user_data["action"] = f"setmax_{profile_id}"
            return await q.edit_message_text(f"🎯 حداکثر فعلی: {get_profile_max_post(profile_id)}\nعدد جدید (۱-۵۰):", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("ast_"):
            n_seen = c.execute("SELECT COUNT(*) FROM seen WHERE profile_id=?", (profile_id,)).fetchone()[0]
            n_pw = c.execute("SELECT COUNT(*) FROM source_passwords WHERE profile_id=?", (profile_id,)).fetchone()[0]
            n_sp = c.execute("SELECT COUNT(*) FROM sponsors WHERE profile_id=?", (profile_id,)).fetchone()[0]
            next_n = get_profile_last_num(profile_id) + 1
            dest = get_profile_dest(profile_id)
            txt = f"📊 **آمار**\nمقصد: {dest}\nمنابع: {len(get_profile_sources(profile_id))}\nرمزها: {n_pw}\nاسپانسر: {n_sp}\nبعدی: #{next_n}\nحداکثر: {get_profile_max_post(profile_id)}"
            return await q.edit_message_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("sendtest_"):
            dest = get_profile_dest(profile_id)
            if not dest:
                return await q.answer("❌ مقصد تنظیم نشده", show_alert=True)
            try:
                await u.get_bot().send_message(dest, f"Test {get_tehran_time()}")
                await q.answer(msg("test_ok"))
            except Exception as e:
                await q.answer(f"❌ {str(e)[:50]}", show_alert=True)
            return

        if d.startswith("runnow_"):
            p = await q.edit_message_text("⏳ در حال اجرا...")
            n, m = await run_full_cycle_for_profile(u.get_bot(), profile_id, only_new=True)
            await p.edit_text(f"✅ Done: {n} - {m}")
            return

        # ===== دکمه‌های toggles =====
        if d.startswith("tglping_"):
            current = get_profile_ping_mode(profile_id)
            set_profile_ping_mode(profile_id, "global" if current == "iran" else "iran")
            await q.answer("تغییر حالت پینگ")
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("tglcfg_"):
            current = get_profile_post_configs(profile_id)
            set_profile_post_configs(profile_id, not current)
            await q.answer(msg("toggle_configs", status=not current))
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("tglproxy_"):
            current = get_profile_post_proxies(profile_id)
            set_profile_post_proxies(profile_id, not current)
            await q.answer(msg("toggle_proxies", status=not current))
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("togglenum_"):
            current = get_profile_show_numbers(profile_id)
            set_profile_show_numbers(profile_id, not current)
            await q.answer(msg("toggle_numbers_ok", status=not current))
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("tgl_date_cfg_"):
            current = get_profile_show_date_config(profile_id)
            set_profile_show_date_config(profile_id, not current)
            await q.answer(msg("date_cfg_toggle", status=not current))
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("tgl_date_prx_"):
            current = get_profile_show_date_proxy(profile_id)
            set_profile_show_date_proxy(profile_id, not current)
            await q.answer(msg("date_prx_toggle", status=not current))
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("clearquery_"):
            set_profile_custom_query(profile_id, "")
            await q.answer("✅ کوئری پاک شد")
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("setquery_"):
            ctx.user_data["action"] = f"setquery_{profile_id}"
            kb = [[InlineKeyboardButton(msg("btn_clear_custom_query_prompt"), callback_data=f"clearquery_{profile_id}", style="danger")],
                  [InlineKeyboardButton(msg("btn_back"), callback_data=f"prof_{profile_id}", style="primary")]]
            return await q.edit_message_text(msg("custom_query_prompt"), reply_markup=InlineKeyboardMarkup(kb))

        if d.startswith("rn_"):
            set_profile_last_num(profile_id, 0)
            await q.answer(msg("reset_ok"))
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("cd1_"):
            return await q.edit_message_text(msg("clear_q1"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("YES", callback_data=f"cd2_{profile_id}", style="danger"),
                  InlineKeyboardButton("NO", callback_data=f"prof_{profile_id}", style="primary")]]))

        if d.startswith("cd2_"):
            for table in ["seen", "posts", "country_cache", "source_passwords", "last_scrape", "processed_messages", "proxies_seen", "sponsors"]:
                c.execute(f"DELETE FROM {table} WHERE profile_id=?" if table != "posts" else f"DELETE FROM {table}", (profile_id,) if table != "posts" else ())
            set_profile_last_num(profile_id, 0)
            conn.commit()
            await q.answer("پاک شد")
            return await show_profile_admin(q.message, profile_id)

        if d.startswith("manual_"):
            ctx.user_data["action"] = f"manual_{profile_id}"
            return await q.edit_message_text(msg("manual_send_prompt"), reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data=f"prof_{profile_id}", style="danger")]]))

        await show_profile_admin(q.message, profile_id)

    except Exception as e:
        log.error(f"Callback error: {e}\n{traceback.format_exc()}")
        try:
            await q.edit_message_text(f"⚠️ خطا: {str(e)[:100]}")
        except:
            pass

# ======================================================================
# هندلرهای متنی و سند (با دکمه خالی)
# ======================================================================
async def on_text(u, ctx):
    if PRIVATE_MODE and u.effective_user.id != ADMIN_ID:
        return

    # ویرایش اسپانسر
    if ctx.user_data.get("sponsor_edit_field"):
        field = ctx.user_data["sponsor_edit_field"]
        sid = ctx.user_data.get("sponsor_edit_id")
        if not sid:
            del ctx.user_data["sponsor_edit_field"]
            return
        txt = u.message.text.strip()
        if field == "name" and txt:
            update_sponsor(sid, name=txt)
        elif field == "url" and txt:
            if not txt.startswith(("http://", "https://")):
                txt = "https://" + txt
            update_sponsor(sid, url=txt)
        elif field == "text" and txt:
            update_sponsor(sid, button_text=txt)
        elif field == "color" and txt in ("blue", "green", "red"):
            update_sponsor(sid, color=txt)
        else:
            await u.message.reply_text("❌ مقدار نامعتبر")
        del ctx.user_data["sponsor_edit_field"]
        await u.message.reply_text(msg("sp_updated"))
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
            return await u.message.reply_text("🔗 لینک (با http/https):", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]]))
        if step == "url":
            url = u.message.text.strip()
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            ctx.user_data["sponsor_url"] = url
            ctx.user_data["sponsor_step"] = "button_text"
            return await u.message.reply_text("📝 متن دکمه:", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]]))
        if step == "button_text":
            ctx.user_data["sponsor_button_text"] = u.message.text.strip()
            ctx.user_data["sponsor_step"] = "color"
            return await u.message.reply_text("🎨 انتخاب رنگ:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔵 آبی", callback_data=f"sp_color_{profile_id}_blue", style="primary"),
                 InlineKeyboardButton("🟢 سبز", callback_data=f"sp_color_{profile_id}_green", style="success"),
                 InlineKeyboardButton("🔴 قرمز", callback_data=f"sp_color_{profile_id}_red", style="danger")],
                [InlineKeyboardButton(msg("btn_back"), callback_data=f"sp_menu_{profile_id}", style="primary")]
            ]))

    a = ctx.user_data.get("action")
    if not a:
        return
    t = u.message.text.strip()

    if a == "prof_add":
        if not t:
            return await u.message.reply_text("❌ نام خالی")
        dest_name = t if t.startswith("@") else "@" + t
        if any(p["dest_name"] == dest_name for p in get_profiles()):
            return await u.message.reply_text("❌ این مقصد قبلاً وجود دارد")
        create_profile(dest_name)
        await u.message.reply_text(msg("profile_added", name=dest_name))
        del ctx.user_data["action"]
        return await show_profiles_list(u.message)

    if a.startswith("sa_"):
        profile_id = int(a.split("_")[1])
        if not t:
            return await u.message.reply_text("❌ خالی")
        items = [x.strip() for x in re.split(r'[,،\n]+', t) if x.strip()]
        srcs = get_profile_sources(profile_id)
        added = []
        for item in items:
            item = ("@" + item) if not item.startswith("@") else item
            item = item.lower()
            if item not in srcs:
                srcs.append(item)
                added.append(item)
        if added:
            set_profile_sources(profile_id, srcs)
            await u.message.reply_text(msg("added", item=", ".join(added)))
        else:
            await u.message.reply_text("همه تکراری")
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("da_"):
        profile_id = int(a.split("_")[1])
        dest = t if t else ""
        if dest and not dest.startswith("@") and not dest.isdigit():
            dest = "@" + dest
        set_profile_dest(profile_id, dest)
        await u.message.reply_text(msg("dest_set", dest=dest if dest else "حذف شد"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("ac_"):
        profile_id = int(a.split("_")[1])
        set_profile_dest(profile_id, t if t else "")
        await u.message.reply_text(msg("name_set", name=t if t else "حذف شد"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("adn_"):
        profile_id = int(a.split("_")[1])
        set_profile_display_name(profile_id, t if t else "")
        await u.message.reply_text(msg("display_name_set", name=t if t else "پاک شد"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("rename_"):
        profile_id = int(a.split("_")[1])
        await process_rename(u, u.message, profile_id, is_document=False)
        del ctx.user_data["action"]
        return

    if a.startswith("ab_config_"):
        profile_id = int(a.split("_")[2])
        if not t:
            update_profile(profile_id, banner_config="")
            await u.message.reply_text("✅ بنر پاک شد")
        elif "{configs}" in t:
            update_profile(profile_id, banner_config=t)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("ab_proxy_"):
        profile_id = int(a.split("_")[2])
        if not t:
            update_profile(profile_id, banner_proxy="")
            await u.message.reply_text("✅ بنر پاک شد")
        elif "{proxies}" in t:
            update_profile(profile_id, banner_proxy=t)
            await u.message.reply_text(msg("banner_ok"))
        else:
            await u.message.reply_text(msg("banner_err"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("ai_"):
        profile_id = int(a.split("_")[1])
        if not t:
            return await u.message.reply_text("✅ بدون تغییر")
        try:
            n = int(t)
            if 1 <= n <= 1440:
                update_profile(profile_id, interval_min=n)
                await u.message.reply_text(msg("interval_ok", n=n))
            else:
                await u.message.reply_text(msg("interval_err"))
        except:
            await u.message.reply_text(msg("interval_wrong"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

    if a.startswith("setmax_"):
        profile_id = int(a.split("_")[1])
        if not t:
            return await u.message.reply_text("✅ بدون تغییر")
        try:
            n = int(t)
            if 1 <= n <= 50:
                update_profile(profile_id, max_post=n)
                await u.message.reply_text(msg("max_ok", n=n))
            else:
                await u.message.reply_text(msg("max_err"))
        except:
            await u.message.reply_text(msg("max_wrong"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

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
        items = [x.strip() for x in re.split(r'[,،\n]+', t) if x.strip()]
        for pw in items:
            add_pw(profile_id, source, pw)
        await u.message.reply_text(msg("pw_added", source=source) + f"\nرمزها: {', '.join(items)}")
        del ctx.user_data["action"]
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
        set_profile_custom_query(profile_id, t if t else "")
        await u.message.reply_text(msg("custom_query_set", query=t if t else "خالی"))
        del ctx.user_data["action"]
        return await show_profile_admin(u.message, profile_id)

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
    elif a.startswith("manual_"):
        profile_id = int(a.split("_")[1])
        await process_manual_text(u, u.message, profile_id, is_document=True)
        del ctx.user_data["action"]

# ======================================================================
# پردازش تغییر نام و ارسال دستی
# ======================================================================
async def process_rename(u, message, profile_id, is_document=False):
    p = await message.reply_text("⏳ در حال پردازش...")
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
                if line.strip():
                    config_links.extend(extract_links_from_text(line))
            config_links = list(set(config_links))

        if not config_links:
            return await p.edit_text("❌ هیچ لینک معتبری یافت نشد")

        display_name = get_profile_display_name(profile_id) or get_profile_dest(profile_id) or "Server"
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
        renamed = await asyncio.gather(*tasks)
        msg_text = f"📝 **سرورهای با نام {display_name}**\n\n" + "\n\n".join([f"<code>{link}</code>" for link in renamed])
        await u.get_bot().send_message(ADMIN_ID, msg_text, parse_mode="HTML", disable_web_page_preview=True)
        await p.edit_text(f"✅ {len(renamed)} لینک تغییر نام داده شد")
    except Exception as e:
        log.error(f"rename error: {e}")
        await p.edit_text(f"❌ {str(e)[:200]}")

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
            return await p.edit_text("❌ هیچ لینک معتبری یافت نشد")

        working = [(url, 0, 0) for url in config_links[:30]]
        proxy_with_ping = []
        for proxy_url in proxy_links[:10]:
            host, _ = extract_host(proxy_url)
            flag = "🌐"
            if host:
                ip = await host_to_ip(host)
                if ip:
                    flag = await get_flag_for_ip(ip)
            proxy_with_ping.append((proxy_url, 0, flag))

        if not working and not proxy_with_ping:
            return await p.edit_text("⚠️ هیچ لینک جدیدی برای ارسال وجود ندارد")

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
    log.info(f"✅ INIT: {len(profiles)} profiles, AUTO={ENABLE_AUTO}, LANG={BOT_LANG}")
    if ENABLE_AUTO:
        for prof in profiles:
            app.create_task(profile_loop(app.bot, prof["id"]))
        log.info("⏰ Scheduler started")

def cfg_get(k, default=""):
    r = c.execute("SELECT v FROM cfg WHERE k=?", (k,)).fetchone()
    return r[0] if r else default

def cfg_set(k, v):
    c.execute("INSERT OR REPLACE INTO cfg VALUES (?,?)", (k, str(v)))
    conn.commit()

async def cmd_runall(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return
    p = await u.message.reply_text("⏳ در حال اجرا (همه)...")
    results = []
    for prof in get_profiles():
        n, m = await run_full_cycle_for_profile(u.get_bot(), prof["id"], only_new=False)
        results.append(f"{prof['dest_name']}: {n} - {m}")
    await p.edit_text("✅ Done:\n" + "\n".join(results))

async def cmd_sendtest(u, ctx):
    if u.effective_user.id != ADMIN_ID:
        return
    for prof in get_profiles():
        try:
            await u.get_bot().send_message(prof["dest_name"], f"Test {get_tehran_time()}")
        except Exception as e:
            await u.message.reply_text(f"❌ {prof['dest_name']}: {e}")
    await u.message.reply_text("✅ Test sent")

async def cmd_diag(update, ctx):
    if update.effective_user.id != ADMIN_ID:
        return
    lines = ["🔍 **گزارش عیب‌یابی**", ""]
    profiles = get_profiles()
    lines.append(f"📌 پروفایل‌ها: {len(profiles)}")
    for p in profiles:
        lines.append(f"  • {p['dest_name']} (ID:{p['id']}) - {len(get_profile_sources(p['id']))} منبع, {p['interval_min']}m")
    lines.append(f"\n💾 دیده‌شده: {c.execute('SELECT COUNT(*) FROM seen').fetchone()[0]}")
    lines.append(f"💾 پروکسی‌ها: {c.execute('SELECT COUNT(*) FROM proxies_seen').fetchone()[0]}")
    lines.append(f"💾 رمزها: {c.execute('SELECT COUNT(*) FROM source_passwords').fetchone()[0]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

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
    log.info("✅ Bot ready, polling...")
    app.run_polling()

if __name__ == "__main__":
    log.info("="*50)
    log.info("🚀 Starting bot...")
    main()
