import os
import sys
import re
import json
import time
import html
import signal
import asyncio
import sqlite3
import logging
import contextvars
import tempfile
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import quote
from typing import Any, Dict, Optional, Callable, Awaitable, List, Tuple

import aiohttp
import psutil
from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BufferedInputFile
)
from aiogram.exceptions import TelegramBadRequest

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================
MAIN_BOT_TOKEN = os.getenv("HOST_BOT_TOKEN", "9133832847:AAHfKfaPxqCNIXmxHogulQaMgEpkp6KjbLU").strip()
if not MAIN_BOT_TOKEN:
    raise SystemExit("❌ HOST_BOT_TOKEN environment variable set nahi hai (Render → Environment)")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8680144387"))
PORT = int(os.getenv("PORT", "10000"))
# ⚠️ Render pe ek Persistent Disk lagao aur DATA_DIR usi ka path do, warna redeploy par DB (premium emoji, plans, users) udd jayega.
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "."))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "hosting.db")
HOST_DIR = os.path.join(DATA_DIR, "hosted_files")

BRAND = "PRIME HOSTING"
VERSION = "11.0"
FREE_BOT_LIMIT = 2
PREMIUM_BOT_LIMIT = 10
FREE_LOG_LINES = 30
PREMIUM_LOG_LINES = 100
MAX_CRASH_RESTARTS = 5
MAX_UPLOAD_MB = 20
BANNED_TEXT = "🚫 <b>Aap is service se banned hain.</b>"
CHANNEL_CACHE_TTL = 20  # seconds: join check cache (chhota rakha hai taaki channel chhodne par jaldi pakda jaye)

# Shuru me ye 3 channels join karne zaroori hain (admin panel se badal / add / remove ho sakte hain)
DEFAULT_CHANNELS = ["@yt_honter17", "@ghost16karan", "@yt_karan2010"]
# Premium plans (admin panel se price badal / add / delete ho sakte hain)
DEFAULT_PLANS = [
    {"days": 1, "price": "19"},
    {"days": 7, "price": "49"},
    {"days": 30, "price": "149"},
    {"days": 90, "price": "399"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("prime_hosting")

bot = Bot(token=MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ==============================================================================
# 🗄️ DATABASE
# ==============================================================================
def db_query(query: str, params: tuple = (), fetchone: bool = False, fetchall: bool = False) -> Any:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        res = c.fetchone() if fetchone else (c.fetchall() if fetchall else None)
        conn.commit()
        return res
    except Exception as e:
        logger.error("DB Error: %s", e)
        return None
    finally:
        conn.close()


def db_insert(query: str, params: tuple = ()) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c.lastrowid
    except Exception as e:
        logger.error("DB Insert Error: %s", e)
        return None
    finally:
        conn.close()


def db_update_count(query: str, params: tuple = ()) -> int:
    """UPDATE chalata hai aur kitni rows badli wo batata hai (atomic check ke liye)."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c.rowcount
    except Exception as e:
        logger.error("DB Update Error: %s", e)
        return 0
    finally:
        conn.close()


def _columns(table: str) -> set:
    rows = db_query(f"PRAGMA table_info({table})", fetchall=True) or []
    return {r[1] for r in rows}


_SETTINGS: Optional[Dict[str, str]] = None   # in-memory copy, write ke baad refresh hota hai


def _load_settings() -> None:
    global _SETTINGS
    rows = db_query("SELECT key, value FROM settings", fetchall=True) or []
    _SETTINGS = {k: v for k, v in rows}


def get_setting(key: str, default: str = "") -> str:
    if _SETTINGS is None:
        _load_settings()
    v = (_SETTINGS or {}).get(key)
    return v if v not in (None, "") else default


def set_setting(key: str, value: Any) -> None:
    global _SETTINGS
    db_query("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    _SETTINGS = None


def del_setting(key: str) -> None:
    global _SETTINGS
    db_query("DELETE FROM settings WHERE key=?", (key,))
    _SETTINGS = None


def del_settings_like(pattern: str) -> None:
    global _SETTINGS
    db_query("DELETE FROM settings WHERE key LIKE ?", (pattern,))
    _SETTINGS = None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_premium INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0)")
    c.execute("""CREATE TABLE IF NOT EXISTS hosted_bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    bot_name TEXT,
                    file_path TEXT,
                    status TEXT DEFAULT 'Stopped',
                    auto_restart INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    days INTEGER,
                    amount TEXT,
                    method TEXT,
                    status TEXT DEFAULT 'pending',
                    proof_id TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    decided_at TEXT DEFAULT '')""")
    conn.commit()
    conn.close()

    # Purane database ke liye migration (naye columns add karna)
    user_cols = _columns("users")
    migrations = {
        "first_name": "TEXT DEFAULT ''",
        "username": "TEXT DEFAULT ''",
        "premium_until": "TEXT",
        "joined_at": "TEXT DEFAULT ''",
    }
    for col, ddl in migrations.items():
        if col not in user_cols:
            db_query(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    bot_cols = _columns("hosted_bots")
    if "created_at" not in bot_cols:
        db_query("ALTER TABLE hosted_bots ADD COLUMN created_at TEXT DEFAULT ''")
    if "approval" not in bot_cols:
        # Purane bots already approved maane jayenge
        db_query("ALTER TABLE hosted_bots ADD COLUMN approval TEXT DEFAULT 'approved'")

    if "proof_unique" not in _columns("payments"):
        db_query("ALTER TABLE payments ADD COLUMN proof_unique TEXT DEFAULT ''")
    try:
        os.chmod(DB_PATH, 0o600)   # sirf owner padh/likh sake
    except OSError:
        pass
    global _SETTINGS
    _SETTINGS = None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


USER_KEYS = ("user_id", "is_premium", "premium_until", "is_banned", "first_name", "username", "joined_at")
BOT_KEYS = ("id", "user_id", "bot_name", "file_path", "status", "auto_restart", "created_at", "approval")
PAY_KEYS = ("id", "user_id", "days", "amount", "method", "status", "proof_id", "created_at", "decided_at")


def ensure_user(uid: int, first_name: str = "", username: str = ""):
    db_query("INSERT OR IGNORE INTO users (user_id, first_name, username, joined_at) VALUES (?,?,?,?)",
             (uid, first_name, username or "", now_str()))
    if first_name or username:
        db_query("UPDATE users SET first_name=?, username=? WHERE user_id=?", (first_name, username or "", uid))


def is_premium_active(u: dict) -> bool:
    if not u or not u.get("is_premium"):
        return False
    until = u.get("premium_until")
    if not until:  # NULL = lifetime
        return True
    try:
        return datetime.fromisoformat(until) > datetime.now()
    except ValueError:
        return False


def get_user(uid: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(USER_KEYS)} FROM users WHERE user_id=?", (uid,), fetchone=True)
    if not row:
        return None
    u = dict(zip(USER_KEYS, row))
    u["premium"] = is_premium_active(u)
    u["banned"] = bool(u["is_banned"])
    return u


def set_premium(uid: int, days: int):
    """Premium do. Agar pehle se premium hai to usi expiry se aage badhata hai."""
    ensure_user(uid)
    if days <= 0:
        db_query("UPDATE users SET is_premium=1, premium_until=NULL WHERE user_id=?", (uid,))
        return
    base = datetime.now()
    u = get_user(uid)
    if u and u["premium"] and u["premium_until"]:
        try:
            base = max(base, datetime.fromisoformat(u["premium_until"]))
        except ValueError:
            pass
    until = (base + timedelta(days=days)).isoformat(timespec="seconds")
    db_query("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until, uid))


def remove_premium(uid: int):
    db_query("UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?", (uid,))


def set_ban(uid: int, banned: bool):
    db_query("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, uid))


def bot_limit(u: dict) -> int:
    key, default = ("premium_limit", PREMIUM_BOT_LIMIT) if u["premium"] else ("free_limit", FREE_BOT_LIMIT)
    try:
        return max(1, int(get_setting(key, str(default))))
    except ValueError:
        return default


def user_bot_count(uid: int) -> int:
    row = db_query("SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,), fetchone=True)
    return row[0] if row else 0


def get_bot(bot_id: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(BOT_KEYS)} FROM hosted_bots WHERE id=?", (bot_id,), fetchone=True)
    return dict(zip(BOT_KEYS, row)) if row else None


def set_status(bot_id: int, status: str):
    db_query("UPDATE hosted_bots SET status=? WHERE id=?", (status, bot_id))


def set_approval(bot_id: int, approval: str):
    db_query("UPDATE hosted_bots SET approval=? WHERE id=?", (approval, bot_id))


def can_access(uid: int, hb: Optional[dict]) -> bool:
    return bool(hb) and (hb["user_id"] == uid or uid == ADMIN_ID)


# ---------------------------- Plans / channels / payment config (sab DB me) ----------------------------
def get_plans() -> List[dict]:
    raw = get_setting("plans", "")
    try:
        data = json.loads(raw) if raw else DEFAULT_PLANS
    except ValueError:
        data = DEFAULT_PLANS
    out = []
    for p in data:
        try:
            out.append({"days": int(p["days"]), "price": str(p["price"])})
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda p: p["days"])


def save_plans(plans: List[dict]) -> None:
    set_setting("plans", json.dumps(sorted(plans, key=lambda p: p["days"])))


def get_plan(days: int) -> Optional[dict]:
    for p in get_plans():
        if p["days"] == days:
            return p
    return None


def get_channels() -> List[str]:
    raw = get_setting("force_channels", "")
    if not raw:
        return list(DEFAULT_CHANNELS)
    try:
        data = json.loads(raw)
    except ValueError:
        return list(DEFAULT_CHANNELS)
    return [c for c in data if isinstance(c, str) and c]


def save_channels(channels: List[str]) -> None:
    set_setting("force_channels", json.dumps(channels))


def fast_approve_on() -> bool:
    return get_setting("fast_approve", "OFF") == "ON"


def get_payment(pid: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(PAY_KEYS)} FROM payments WHERE id=?", (pid,), fetchone=True)
    return dict(zip(PAY_KEYS, row)) if row else None


# ==============================================================================
# 🎨 PREMIUM / NORMAL EMOJI ENGINE + EDITABLE UI TEXTS
#   * Har emoji ka ek "slot" hota hai. Slot me premium ID ho to premium emoji dikhta hai,
#     ID na ho to normal emoji dikhta hai (kabhi blank nahi).
#   * Har message / caption / button ke plain emoji automatically premium ban jate hain.
#   * Admin -> Edit Emojis se koi bhi slot ka premium ID badalta hai (DB me save hota hai).
#   * Admin -> Edit UI Texts se har screen ka text, YouTube link aur emoji alag-alag badal sakta hai.
# ==============================================================================
# (slot, normal emoji, premium emoji ID or "" = abhi premium ID nahi, normal emoji dikhega)
EMOJI_SLOTS = [
    ("brand", "🎬", "5994502837327892086"), ("profile", "👤", "5258011929993026890"),
    ("user_id", "🆔", "5474625972751837256"), ("name", "📛", "5215399540814781035"),
    ("gem", "💎", "5886505193180239900"), ("crown", "👑", "5886505193180239900"),
    ("free", "🆓", ""), ("slots", "📦", "6160968017304888311"),
    ("shield", "🛡", "6086672466132865380"), ("settings", "⚙️", ""),
    ("check", "✅", "6161241250239356403"), ("cross", "❌", "6037421444789440735"),
    ("warn", "⚠️", "6037421444789440735"), ("stats", "📊", "6161437856662298090"),
    ("server", "🖥", ""), ("ram", "💾", ""), ("disk", "💽", ""),
    ("bots", "🤖", "5312361253610475399"), ("back", "🔙", "5877536313623711363"),
    ("help", "❓", "5967280668885913944"), ("chat", "💬", "5967280668885913944"),
    ("logs", "📋", "6005986106703613755"), ("file", "📄", ""),
    ("download", "📥", ""), ("upload", "📤", ""), ("phone", "📱", ""),
    ("play", "▶️", ""), ("stop", "⏹", ""), ("restart", "🔁", ""),
    ("clean", "🧹", ""), ("delete", "🗑", ""), ("pip", "📦", ""),
    ("bolt", "⚡", ""), ("broadcast", "📢", "6005986106703613755"),
    ("users", "👥", "5904630315946611415"), ("search", "🔍", ""),
    ("point_down", "👇", "6161302621027049305"), ("calendar", "📅", "5433614043006903194"),
    ("clock", "⏳", ""), ("link", "🔗", "5875465628285931233"),
    ("video", "▶️", ""), ("channel", "📌", ""), ("backup", "🗂", ""),
    ("maint", "🔧", ""), ("ban", "⛔", ""), ("limit", "📏", ""),
    ("emoji", "🎨", ""), ("edit", "✏️", ""),
    ("money", "💰", ""), ("card", "💳", ""),
]
DEFAULT_EMOJIS: Dict[str, str] = {s: i for s, _f, i in EMOJI_SLOTS}
EMOJI_FALLBACK: Dict[str, str] = {s: f for s, f, _i in EMOJI_SLOTS}

# plain emoji (jo code / text me likhe hain) -> slot
_EMOJI_SLOT_MAP = {
    "🎬": "brand", "✨": "brand", "👤": "profile", "🆔": "user_id", "📛": "name",
    "💎": "gem", "👑": "crown", "🆓": "free", "📦": "slots", "🛡": "shield", "🔒": "shield",
    "⚙": "settings", "✅": "check", "🟢": "check", "🚫": "cross", "❌": "cross", "🔴": "cross",
    "🛑": "cross", "✖": "cross", "⚠": "warn", "😅": "warn", "📊": "stats", "📈": "stats",
    "🖥": "server", "💾": "ram", "💽": "disk", "🤖": "bots", "📱": "phone", "🔙": "back",
    "❓": "help", "💬": "chat", "📋": "logs", "📄": "file", "📥": "download", "📤": "upload",
    "▶": "play", "⏹": "stop", "🔁": "restart", "🔄": "restart", "🧹": "clean", "🗑": "delete",
    "⚡": "bolt", "📢": "broadcast", "👥": "users", "🔍": "search", "🔎": "search",
    "👇": "point_down", "📅": "calendar", "🗓": "calendar", "⏳": "clock", "⌛": "clock",
    "🔗": "link", "📌": "channel", "🗂": "backup", "🔧": "maint", "⛔": "ban", "📏": "limit",
    "🎨": "emoji", "✏": "edit", "💰": "money", "💳": "card",
}
_EMOJI_RE = re.compile(
    "(" + "|".join(sorted((re.escape(k) for k in _EMOJI_SLOT_MAP), key=len, reverse=True)) + ")\ufe0f?"
)
_TRAIL_EMOJI_RE = re.compile(
    "(" + "|".join(sorted((re.escape(k) for k in _EMOJI_SLOT_MAP), key=len, reverse=True)) + ")\ufe0f?\\s*$"
)
# <pre>/<code>/existing <tg-emoji>/tags ko haath nahi lagate (logs safe rehte hain)
_SPLIT_RE = re.compile(r"(<pre.*?</pre>|<code.*?</code>|<tg-emoji.*?</tg-emoji>|<[^>]+>)", re.S)

_CUR_UI = contextvars.ContextVar("cur_ui", default=None)   # (ui_key, text prefix) of the screen being sent
YT_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/\S+$", re.I)


def get_emoji_icon(slot: str, ui_key: Optional[str] = None) -> str:
    """Premium emoji ID: per-screen override -> admin global -> default. '' = normal emoji dikhao."""
    if get_setting("premium_emoji", "ON") == "OFF":
        return ""
    if ui_key:
        ov = get_setting(f"uiemoji_{ui_key}_{slot}", "")
        if ov.isdigit():
            return ov
    stored = get_setting(f"emoji_{slot}", "")
    if stored.isdigit():
        return stored
    return DEFAULT_EMOJIS.get(slot, "")


def get_emoji(slot: str, ui_key: Optional[str] = None) -> str:
    fb = EMOJI_FALLBACK.get(slot, "✨")
    icon = get_emoji_icon(slot, ui_key)
    return f'<tg-emoji emoji-id="{icon}">{fb}</tg-emoji>' if icon else fb


def _emap_key(ch: str) -> str:
    """Emoji character -> DB key (e.g. 😀 -> emap_1f600). Variation selector hata ke."""
    return "emap_" + "_".join(f"{ord(c):x}" for c in ch.replace("\ufe0f", ""))


def custom_emoji_map() -> Dict[str, str]:
    """Admin ka 'Emoji Map': koi bhi normal emoji -> premium ID (DB se, restart ke baad bhi)."""
    if _SETTINGS is None:
        _load_settings()
    out: Dict[str, str] = {}
    for k, v in (_SETTINGS or {}).items():
        if k.startswith("emap_") and str(v).isdigit():
            try:
                out["".join(chr(int(h, 16)) for h in k[5:].split("_"))] = str(v)
            except ValueError:
                pass
    return out


def _icon_for_char(ch: str) -> str:
    base = ch.replace("\ufe0f", "")
    cmap = custom_emoji_map()
    if base in cmap:
        return cmap[base]
    if base in _EMOJI_SLOT_MAP:
        return get_emoji_icon(_EMOJI_SLOT_MAP[base])
    return ""


_PLAIN_RE_CACHE: Dict[tuple, Any] = {}


def _plain_emoji_re():
    """Slot emojis + admin ke mapped emojis, dono ke liye regex (cache ke saath)."""
    keys = tuple(sorted(set(_EMOJI_SLOT_MAP) | set(custom_emoji_map())))
    rx = _PLAIN_RE_CACHE.get(keys)
    if rx is None:
        rx = re.compile("(" + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True)) + ")\ufe0f?")
        if len(_PLAIN_RE_CACHE) > 20:
            _PLAIN_RE_CACHE.clear()
        _PLAIN_RE_CACHE[keys] = rx
    return rx


def premiumize_text(text: str) -> str:
    """Plain emoji -> premium <tg-emoji> (jis emoji ka premium ID nahi, wo normal hi rehta hai)."""
    if not text or not isinstance(text, str):
        return text
    rx = _plain_emoji_re()
    parts = _SPLIT_RE.split(text)

    def _sub(m):
        icon = _icon_for_char(m.group(1))
        return f'<tg-emoji emoji-id="{icon}">{m.group(0)}</tg-emoji>' if icon else m.group(0)

    for i in range(0, len(parts), 2):
        parts[i] = rx.sub(_sub, parts[i])
    return "".join(parts)


def premiumize_markup(markup):
    """Button ke shuru/aakhir ka emoji -> premium icon (ID na ho to button jaisa hai waisa rahega)."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            try:
                if b.text and not getattr(b, "icon_custom_emoji_id", None):
                    t = b.text.strip()
                    m = _plain_emoji_re().match(t)
                    if m:
                        rest = t[m.end():].strip()
                    else:
                        m = _TRAIL_EMOJI_RE.search(t)
                        rest = t[:m.start()].strip() if m else ""
                    if m:
                        icon = _icon_for_char(m.group(1))
                        if icon:
                            b = b.model_copy(update={"text": rest or b.text, "icon_custom_emoji_id": icon})
                            changed = True
            except Exception:
                pass
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup


def ui_video_button(key: str) -> Optional[InlineKeyboardButton]:
    link = get_setting(f"ui_yt_{key}", "")
    if not link:
        return None
    return InlineKeyboardButton(text="▶️ Watch Video", url=link)


def _attach_ui_video(method) -> None:
    """Jis screen ka YouTube link set hai, us message me 'Watch Video' button jod deta hai."""
    try:
        cur = _CUR_UI.get()
        if not cur:
            return
        key, prefix = cur
        if "reply_markup" not in getattr(type(method), "model_fields", {}):
            return
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if not isinstance(text, str) or prefix not in text:
            return
        _CUR_UI.set(None)
        btn_ = ui_video_button(key)
        if not btn_:
            return
        rm = getattr(method, "reply_markup", None)
        if rm is None:
            rows = []
        elif isinstance(rm, InlineKeyboardMarkup):
            rows = [list(r) for r in rm.inline_keyboard]
        else:
            return
        if any(getattr(b, "url", None) == btn_.url for r in rows for b in r):
            return
        rows.insert(0, [btn_])
        setattr(method, "reply_markup", InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as e:
        logger.warning("UI video attach skipped: %s", e)


async def premium_emoji_request_middleware(make_request, bot_, method):
    """Bot ki har Telegram API call par chalta hai. Premium emoji reject ho to normal emoji se retry."""
    orig: Dict[str, Any] = {}
    try:
        _attach_ui_video(method)
        if get_setting("premium_emoji", "ON") != "OFF":
            fields = getattr(type(method), "model_fields", {})
            pm = getattr(method, "parse_mode", None)
            if "parse_mode" in fields and (pm is None or not isinstance(pm, str) or pm.upper() == "HTML"):
                for fld in ("text", "caption"):
                    val = getattr(method, fld, None)
                    if isinstance(val, str) and val:
                        new = premiumize_text(val)
                        if new != val:
                            orig[fld] = val
                            setattr(method, fld, new)
            rm = getattr(method, "reply_markup", None)
            if rm is not None:
                new_rm = premiumize_markup(rm)
                if new_rm is not rm:
                    orig["reply_markup"] = rm
                    setattr(method, "reply_markup", new_rm)
    except Exception as e:
        logger.warning("Premium emoji middleware skipped: %s", e)
    try:
        return await make_request(bot_, method)
    except TelegramBadRequest as e:
        if orig and "not modified" not in str(e).lower():
            for k, v in orig.items():
                setattr(method, k, v)
            logger.warning("Premium emoji rejected (%s) -> normal emoji se dobara bhej raha hu", e)
            return await make_request(bot_, method)
        raise


try:
    bot.session.middleware(premium_emoji_request_middleware)
except Exception as _e:
    logger.warning("Auto premium emoji enable nahi hua: %s", _e)

# ---------------------------------------------------------------- editable UI texts
DEFAULT_UI: Dict[str, str] = {
    "home": (
        "{brand} <b>{v_brand} SERVER v{v_version}</b> {brand}\n"
        "{check} Welcome, <b>{v_name}</b>!\n\n"
        "{profile} <b>Profile Card</b>\n"
        "┣ {user_id} User ID: <code>{v_id}</code>\n"
        "┣ {gem} Plan: {v_plan}\n"
        "┣ {slots} Slots: {v_slots}\n"
        "┗ {shield} Crash Guard: {check} Active\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "{point_down} <i>Ek option select karo:</i>"
    ),
    "profile": (
        "{profile} <b>My Profile</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{name} Name: {v_name}\n"
        "{user_id} ID: <code>{v_id}</code>\n"
        "{gem} Plan: {v_plan}\n"
        "{calendar} Premium Till: {v_till}\n"
        "{slots} Slots: {v_slots}\n"
        "{calendar} Joined: {v_joined}\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
    "status": (
        "{stats} <b>Server Status</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{server} CPU: <b>{v_cpu}%</b>\n"
        "{ram} RAM: <b>{v_ram}%</b>\n"
        "{disk} Disk: <b>{v_disk}%</b>\n"
        "{bots} Active Bots: <b>{v_active}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
    "help": (
        "{help} <b>Help Guide</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{upload} 1. Apni <b>.py</b> file upload karo\n"
        "{check} 2. Admin approval ke baad bot start hoga\n"
        "{play} 3. <b>Start</b> button dabao\n"
        "{logs} 4. Crash ho to <b>Live Logs</b> dekho\n"
        "{pip} 5. Missing module ke liye <b>Pip Install</b>\n"
        "{gem} 6. Zyada bots ke liye <b>Premium</b> lo\n\n"
        "{chat} Commands: /start /help /cancel"
    ),
    "upload_prompt": (
        "{upload} <b>Apni Python (.py) file bhejo</b>\n\n"
        "{slots} Slots: {v_slots}\n"
        "{limit} Max size: {v_max} MB"
    ),
    "maintenance": (
        "{maint} <b>Maintenance Mode</b>\n\n"
        "Service abhi update ho rahi hai. Thodi der baad dobara try karo."
    ),
    "join_required": (
        "{channel} <b>Pehle hamare channels join karo</b>\n\n"
        "Bot use karne ke liye ye sare channels join karna zaroori hai.\n"
        "Join karne ke baad <b>✅ Verify</b> dabao.\n\n"
        "{point_down} Neeche wale channels join karo."
    ),
    "buy": (
        "{gem} <b>Premium Plans</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Free me sirf <b>{v_free}</b> bots host kar sakte ho.\n"
        "Premium me <b>{v_prem}</b> bots tak.\n\n"
        "{point_down} <i>Plan chuno, phir payment karo:</i>"
    ),
    "pending": (
        "{clock} <b>Admin Approval Pending</b>\n\n"
        "Aapki file admin check kar raha hai. Approve hote hi bot start ho sakta hai."
    ),
}

# key -> (label, placeholders help)
UI_EDITABLE: Dict[str, Tuple[str, str]] = {
    "home": ("Home / Start", "{v_brand} {v_version} {v_name} {v_id} {v_plan} {v_slots} + emoji slots"),
    "profile": ("My Profile", "{v_name} {v_id} {v_plan} {v_till} {v_slots} {v_joined} + emoji slots"),
    "status": ("Server Status", "{v_cpu} {v_ram} {v_disk} {v_active} + emoji slots"),
    "help": ("Help Guide", "emoji slots like {upload} {play} {logs}"),
    "upload_prompt": ("Upload Prompt", "{v_slots} {v_max} + emoji slots"),
    "maintenance": ("Maintenance Message", "emoji slots like {maint}"),
    "join_required": ("Join Channels Message", "{channel} {point_down} + emoji slots"),
    "buy": ("Premium Plans Text", "{v_free} {v_prem} + emoji slots"),
    "pending": ("Pending Approval Text", "{clock} + emoji slots"),
}


def ui(key: str, **kwargs) -> str:
    """Admin ka edit kiya text (ui_<key>) -> warna default. {slot} = premium/normal emoji, {v_*} = live values."""
    template = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    for slot in DEFAULT_EMOJIS:
        tag = "{" + slot + "}"
        if tag in template:
            template = template.replace(tag, get_emoji(slot, ui_key=key))
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", str(v))
    try:
        _CUR_UI.set((key, template[:40]))
    except Exception:
        pass
    return template


# ==============================================================================
# 🧠 STATES
# ==============================================================================
class HostingStates(StatesGroup):
    wait_file = State()
    wait_pip_package = State()


class PayStates(StatesGroup):
    wait_proof = State()


class AdminStates(StatesGroup):
    wait_input = State()
    wait_ui_text = State()
    wait_ui_video = State()
    wait_ui_emoji = State()
    wait_emoji_value = State()
    wait_emap = State()


# ==============================================================================
# 🤖 BOT MANAGER
# ==============================================================================
# ---------------------------- Child process sandbox ----------------------------
CHILD_ENV_WHITELIST = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")
# Optional (Linux + root host): hosted bots ko alag non-root user ki permission me chalao.
CHILD_UID = int(os.getenv("CHILD_UID", "0") or 0)


def child_env(home_dir: str) -> Dict[str, str]:
    """Hosted bot ko sirf safe env milta hai: HOST_BOT_TOKEN, DB path, API keys nahi milte."""
    env = {k: os.environ[k] for k in CHILD_ENV_WHITELIST if k in os.environ}
    env.update({"HOME": home_dir, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _child_preexec():
    """Fork ke baad, exec se pehle: memory / file / core limits + optional non-root user."""
    try:
        import resource
        mem = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        fsize = 100 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    except Exception:
        pass
    if CHILD_UID and os.getuid() == 0:
        try:
            os.setgid(CHILD_UID)
            os.setuid(CHILD_UID)
        except Exception:
            pass


class BotInstance:
    def __init__(self, bot_id: int, user_id: int, file_path: str, auto_restart: bool):
        self.bot_id = bot_id
        self.user_id = user_id
        self.file_path = file_path
        self.auto_restart = auto_restart
        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: deque = deque(maxlen=300)
        self.is_running = False
        self.should_run = False
        self.crashes = 0
        self.started_at = 0.0

    def log(self, text: str):
        self.logs.append(text)

    async def clear_webhook(self):
        try:
            with open(self.file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"[0-9]{8,10}:[a-zA-Z0-9_-]{35}", content)
            if match:
                token = match.group(0)
                async with aiohttp.ClientSession() as session:
                    await session.get(f"https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=true")
                self.log("✅ Webhook cleared.")
        except Exception:
            pass

    async def start(self) -> bool:
        if self.is_running:
            return True
        self.should_run = True
        self.logs.clear()
        self.log("⏳ Booting process...")

        if not os.path.exists(self.file_path):
            self.log("❌ Error: File not found.")
            return False

        await self.clear_webhook()

        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", self.file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(self.file_path),
                env=child_env(os.path.dirname(self.file_path)),
                preexec_fn=_child_preexec,
                start_new_session=True,
            )
        except Exception as e:
            self.log(f"❌ System Error: {e}")
            self.is_running = False
            return False

        self.is_running = True
        self.started_at = time.time()
        set_status(self.bot_id, "Running")
        self.log("🚀 Bot is live.")
        asyncio.create_task(self._watch(self.process))
        return True

    async def _watch(self, proc):
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").rstrip()
            if text:
                self.log(text)

        code = await proc.wait()
        if proc is not self.process:
            return  # ye purana process tha (restart ho chuka hai)
        self.is_running = False
        self.log(f"⚠️ Bot stopped (Code: {code})")
        set_status(self.bot_id, "Stopped")

        if not self.should_run or not self.auto_restart:
            return

        # Lamba chala aur crash hua to counter reset
        if time.time() - self.started_at > 300:
            self.crashes = 0
        self.crashes += 1

        if self.crashes <= MAX_CRASH_RESTARTS:
            self.log(f"🔄 Crash Guard: restart {self.crashes}/{MAX_CRASH_RESTARTS} in 5s...")
            await asyncio.sleep(5)
            if self.should_run:
                await self.start()
        else:
            self.should_run = False
            self.log("🛑 Crash Guard: bahut zyada crashes, bot band kar diya gaya.")

    async def stop(self):
        self.should_run = False
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.process.kill()
        self.is_running = False
        set_status(self.bot_id, "Stopped")

    def kill_silently(self):
        """Shutdown ke time use hota hai: DB status change nahi karta taaki restart pe bot wapas chale."""
        self.should_run = False
        if self.process and self.process.returncode is None:
            self.process.terminate()


ACTIVE_BOTS: Dict[int, BotInstance] = {}


def get_instance(bot_id: int) -> Optional[BotInstance]:
    if bot_id in ACTIVE_BOTS:
        return ACTIVE_BOTS[bot_id]
    hb = get_bot(bot_id)
    if not hb:
        return None
    inst = BotInstance(bot_id, hb["user_id"], hb["file_path"], bool(hb["auto_restart"]))
    ACTIVE_BOTS[bot_id] = inst
    return inst


# ==============================================================================
# 🎨 UI KEYBOARDS & TEXT
# ==============================================================================
def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("📤 Upload Bot (.py)", "upload")],
        [btn("📱 My Hosted Bots", "my_bots")],
        [btn("👤 My Profile", "profile"), btn("📊 Server Status", "status")],
        [btn("💎 Buy Premium", "buy"), btn("❓ Help", "help")],
    ]
    if uid == ADMIN_ID:
        rows.append([btn("👑 ADMIN PANEL", "admin_main")])
    return kb(*rows)


def bot_panel_kb(bot_id: int, auto_on: bool, viewer_premium: bool, approval: str = "approved") -> InlineKeyboardMarkup:
    gem = "" if viewer_premium else " 💎"
    auto_label = f"⚡ Auto-Restart: {'🟢 ON' if auto_on else '🔴 OFF'}{gem}"
    dl_label = "📥 Download Logs 💎" if not viewer_premium else "📥 Download Logs"
    return kb(
        [btn("▶️ Start", f"start:{bot_id}"), btn("⏹ Stop", f"stop:{bot_id}"), btn("🔁 Restart", f"restart:{bot_id}")],
        [btn("📋 Live Logs", f"logs:{bot_id}"), btn("🧹 Clear Logs", f"clearlog:{bot_id}")],
        [btn("📦 Pip Install", f"pip:{bot_id}"), btn(auto_label, f"toggle:{bot_id}")],
        [btn(dl_label, f"dllog:{bot_id}"), btn("📄 Download File", f"dlfile:{bot_id}")],
        [btn("🗑 Delete Bot", f"del:{bot_id}")],
        [btn("🔙 Back", "my_bots")],
    )


def admin_main_menu() -> InlineKeyboardMarkup:
    maint = get_setting("maintenance", "OFF")
    pe = get_setting("premium_emoji", "ON")
    fa = "ON" if fast_approve_on() else "OFF"
    return kb(
        [btn("📢 Broadcast", "adm_in:broadcast"), btn("📢 Broadcast Premium", "adm_in:broadcast_prem")],
        [btn("📈 Stats", "adm_stats"), btn("👥 Users", "adm_users")],
        [btn("🤖 All Bots", "adm_bots"), btn("🔎 Open Bot by ID", "adm_in:open_bot")],
        [btn("⏳ Pending Bots", "adm_pending"), btn("💳 Payments", "adm_pay")],
        [btn("💎 Give Premium", "adm_in:give_prem"), btn("🚫 Remove Premium", "adm_in:rm_prem")],
        [btn("💎 Premium List", "adm_prem_list"), btn("⛔ Banned List", "adm_ban_list")],
        [btn("⛔ Ban User", "adm_in:ban"), btn("✅ Unban User", "adm_in:unban")],
        [btn("🔍 User Info", "adm_in:info"), btn("🛑 Stop User Bots", "adm_in:stop_user")],
        [btn("💰 Plans & Prices", "adm_plans"), btn("💳 Payment Setup", "adm_paycfg")],
        [btn("📌 Force Join Channels", "adm_chans")],
        [btn(f"⚡ Fast Approve: {fa}", "adm_fast")],
        [btn("✏️ Edit UI Texts", "adm_ui"), btn("🎨 Edit Emojis", "adm_emoji")],
        [btn("🗺 Emoji Map (any emoji → premium)", "adm_emap")],
        [btn("📏 Free Limit", "adm_in:limit_free"), btn("📏 Premium Limit", "adm_in:limit_prem")],
        [btn("🗂 Backup DB", "adm_backup")],
        [btn(f"🔧 Maintenance: {maint}", "adm_maint")],
        [btn(f"✨ Premium Emoji: {pe}", "adm_toggle_pe")],
        [btn("🛑 Stop All Bots", "adm_stopall")],
        [btn("🔙 Back to Main", "back_main")],
    )


ADMIN_PROMPTS = {
    "broadcast": "📢 <b>Broadcast Message</b>\nJo bhi bhejna hai (text / photo / file) wo abhi send karo.",
    "broadcast_prem": "📢 <b>Broadcast (sirf Premium users)</b>\nJo bhejna hai (text / photo / file) wo abhi send karo.",
    "give_prem": "👑 <b>Give Premium</b>\nFormat: <code>USER_ID DAYS</code>\n(DAYS = 0 matlab Lifetime ♾)",
    "rm_prem": "🚫 <b>Remove Premium</b>\nUser ID bhejo.",
    "ban": "⛔ <b>Ban User</b>\nUser ID bhejo.",
    "unban": "✅ <b>Unban User</b>\nUser ID bhejo.",
    "info": "🔍 <b>User Info</b>\nUser ID bhejo.",
    "open_bot": "🔎 <b>Open Bot by ID</b>\nHosted Bot ka ID bhejo (kisi ka bhi bot control kar sakte ho).",
    "stop_user": "🛑 <b>Stop User Bots</b>\nUser ID bhejo — uske saare running bots band ho jayenge.",
    "limit_free": "📏 <b>Free Bot Limit</b>\nFree users ke liye naya max bots number bhejo (1-1000).",
    "limit_prem": "📏 <b>Premium Bot Limit</b>\nPremium users ke liye naya max bots number bhejo (1-1000).",
    "chan_add": "📌 <b>Naya Force-Join Channel add karo</b>\nChannel ka <code>@username</code> ya link bhejo.\n⚠️ Bot ko us channel me <b>admin</b> banana zaroori hai.",
    "upi_set": "💳 <b>UPI ID set karo</b>\nFormat: <code>name@bank</code> (e.g. <code>yourname@okaxis</code>)",
    "api_url": "🔗 <b>Payment API URL set karo</b>\nPayment order banane wala endpoint bhejo (https://...).\nBot isko POST karega: <code>order_id, amount, currency, user_id, days</code>.\nResponse me <code>payment_url</code> ho to user ko Pay button milega.",
    "api_key": "🔑 <b>Payment API Key set karo</b>\nKey bhejo (ye message delete kar diya jayega).",
    "plan_add": "➕ <b>Naya Plan add karo</b>\nFormat: <code>DAYS PRICE</code> (e.g. <code>15 79</code>)",
}


def admin_prompt_text(action: str, extra: str = "") -> str:
    text = ADMIN_PROMPTS.get(action, "")
    if action == "chan_add":
        chans = get_channels()
        text += "\n\nCurrent: " + (", ".join(esc(c) for c in chans) if chans else "<i>none</i>")
    elif action == "upi_set":
        text += f"\n\nCurrent: <b>{esc(get_setting('upi_id', '') or 'not set')}</b>"
    elif action == "limit_free":
        text += f"\nCurrent: <b>{bot_limit({'premium': False})}</b>"
    elif action == "limit_prem":
        text += f"\nCurrent: <b>{bot_limit({'premium': True})}</b>"
    elif action == "plan_price":
        text = f"💰 <b>Plan {extra} day(s) — naya price bhejo</b>\nSirf number bhejo (e.g. <code>99</code>). Currency: ₹"
    return text


def premium_expiry_text(u: dict) -> str:
    if not u["is_premium"]:
        return "—"
    if not u["premium_until"]:
        return "♾ Lifetime"
    try:
        until = datetime.fromisoformat(u["premium_until"])
    except ValueError:
        return "—"
    if until > datetime.now():
        return f"⏳ {until.strftime('%d %b %Y, %H:%M')}"
    return "⌛ Expired"


def home_text(u: dict, name: str) -> str:
    plan = f"{get_emoji('crown')} PREMIUM" if u["premium"] else f"{get_emoji('free')} FREE"
    return ui("home", v_brand=BRAND, v_version=VERSION, v_name=esc(name), v_id=u["user_id"], v_plan=plan,
              v_slots=f"{user_bot_count(u['user_id'])}/{bot_limit(u)}")


def profile_text(u: dict, name: str) -> str:
    plan = f"{get_emoji('crown')} PREMIUM" if u["premium"] else f"{get_emoji('free')} FREE"
    return ui("profile", v_name=esc(name), v_id=u["user_id"], v_plan=plan, v_till=premium_expiry_text(u),
              v_slots=f"{user_bot_count(u['user_id'])}/{bot_limit(u)}", v_joined=u["joined_at"] or "—")


def server_stats_text() -> str:
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    active = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    return ui("status", v_cpu=cpu, v_ram=ram, v_disk=disk, v_active=active)


def my_bots_view(uid: int):
    rows = db_query("SELECT id, bot_name, approval FROM hosted_bots WHERE user_id=? ORDER BY id DESC", (uid,), fetchall=True) or []
    if not rows:
        return (
            "📭 <b>Abhi koi bot hosted nahi hai.</b>\n\n📤 Pehla bot upload karo!",
            kb([btn("📤 Upload Bot (.py)", "upload")], [btn("🔙 Back", "back_main")]),
        )
    buttons = []
    for bid, name, approval in rows:
        inst = ACTIVE_BOTS.get(bid)
        if approval == "pending":
            icon = "⏳"
        elif approval == "rejected":
            icon = "❌"
        else:
            icon = "🟢" if inst and inst.is_running else "🔴"
        buttons.append([btn(f"{icon} {name}", f"view:{bid}")])
    buttons.append([btn("🔙 Back", "back_main")])
    text = f"📱 <b>My Hosted Bots</b> ({len(rows)})\n━━━━━━━━━━━━━━━━━━\n🟢 Running  |  🔴 Stopped  |  ⏳ Pending  |  ❌ Rejected"
    return text, kb(*buttons)


def stats_text() -> str:
    users = db_query("SELECT user_id, is_premium, premium_until, is_banned FROM users", fetchall=True) or []
    total = len(users)
    premium = sum(1 for r in users if is_premium_active({"is_premium": r[1], "premium_until": r[2]}))
    banned = sum(1 for r in users if r[3] == 1)
    bots_total = (db_query("SELECT COUNT(*) FROM hosted_bots", fetchone=True) or (0,))[0]
    pending = (db_query("SELECT COUNT(*) FROM hosted_bots WHERE approval='pending'", fetchone=True) or (0,))[0]
    pay_pending = (db_query("SELECT COUNT(*) FROM payments WHERE status='pending' AND proof_id!=''", fetchone=True) or (0,))[0]
    running = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    return (
        f"📈 <b>Platform Stats</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: <b>{total}</b>\n"
        f"💎 Premium Users: <b>{premium}</b>\n"
        f"🚫 Banned Users: <b>{banned}</b>\n"
        f"🤖 Total Bots: <b>{bots_total}</b>\n"
        f"🟢 Running Bots: <b>{running}</b>\n"
        f"⏳ Pending Bot Approvals: <b>{pending}</b>\n"
        f"💳 Pending Payment Approvals: <b>{pay_pending}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


async def safe_edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=markup)


# ==============================================================================
# 🛡️ MIDDLEWARE (Ban check on every message & button)
# ==============================================================================
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user and not user.is_bot and user.id != ADMIN_ID:
            ensure_user(user.id, user.first_name or "", user.username or "")
            row = get_user(user.id)
            if row and row["banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Aap banned hain.", show_alert=True)
                else:
                    await event.answer(BANNED_TEXT)
                return None
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    """Flood/spam guard: ek user 5 second me max 8 events. Admin exempt."""
    WINDOW = 5.0
    MAX_EVENTS = 8

    def __init__(self):
        super().__init__()
        self._hits: Dict[int, deque] = {}

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user and user.id != ADMIN_ID:
            now = time.time()
            if len(self._hits) > 20000:
                self._hits.clear()
            q = self._hits.setdefault(user.id, deque())
            while q and now - q[0] > self.WINDOW:
                q.popleft()
            if len(q) >= self.MAX_EVENTS:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳ Thoda ruko...")
                    except Exception:
                        pass
                return None
            q.append(now)
        return await handler(event, data)


dp.message.outer_middleware(RateLimitMiddleware())
dp.callback_query.outer_middleware(RateLimitMiddleware())
dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())


# ---------------------------- Force-join channels (multiple) ----------------------------
_join_cache: Dict[Tuple[int, str], float] = {}
_join_warned: set = set()


def normalize_channel(text: str) -> Optional[str]:
    """'' = galat input (None) ... warna '@username'."""
    t = text.strip()
    m = re.search(r"(?:t\.me/|@)?([A-Za-z0-9_]{5,32})/?$", t)
    return "@" + m.group(1) if m else None


async def _is_member(uid: int, channel: str) -> bool:
    key = (uid, channel)
    now = time.time()
    if _join_cache.get(key, 0) > now:
        return True
    try:
        cm = await bot.get_chat_member(channel, uid)
        status = str(getattr(cm.status, "value", cm.status))
        ok = status in ("member", "administrator", "creator") or (
            status == "restricted" and bool(getattr(cm, "is_member", False)))
    except Exception as e:
        # Bot channel ka admin/member nahi hai. Sabko lock karne ke bajay allow karo + admin ko batao.
        if channel not in _join_warned:
            _join_warned.add(channel)
            logger.error("Force-join check failed for %s (%s)", channel, e)
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Force-join check fail: {esc(channel)}\n<code>{esc(e)}</code>\n"
                                                 f"Bot ko is channel me <b>admin</b> banao.")
            except Exception:
                pass
        return True
    if ok:
        _join_cache[key] = now + CHANNEL_CACHE_TTL
    else:
        _join_cache.pop(key, None)
    return ok


async def missing_channels(uid: int) -> List[str]:
    missing = []
    for ch in get_channels():
        if not await _is_member(uid, ch):
            missing.append(ch)
    return missing


def join_kb(missing: List[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📌 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in missing]
    rows.append([btn("✅ Verify", "check_join")])
    return kb(*rows)


class GateMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if not user or user.is_bot or user.id == ADMIN_ID:
            return await handler(event, data)

        if get_setting("maintenance", "OFF") == "ON":
            if isinstance(event, CallbackQuery):
                await event.answer("🔧 Maintenance chal raha hai. Thodi der baad try karo.", show_alert=True)
            else:
                await event.answer(ui("maintenance"))
            return None

        if isinstance(event, CallbackQuery) and event.data == "check_join":
            return await handler(event, data)

        missing = await missing_channels(user.id)
        if missing:
            text = ui("join_required")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("❌ Pehle sare channels join karo!", show_alert=True)
                except Exception:
                    pass
                try:
                    await event.message.answer(text, reply_markup=join_kb(missing))
                except Exception:
                    pass
            else:
                await event.answer(text, reply_markup=join_kb(missing))
            return None
        return await handler(event, data)


dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())


# ==============================================================================
# 🏠 GENERAL HANDLERS
# ==============================================================================
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id, msg.from_user.first_name or "", msg.from_user.username or "")
    u = get_user(msg.from_user.id)
    await msg.answer(home_text(u, msg.from_user.first_name or "User"), reply_markup=main_menu(msg.from_user.id))


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("✖️ Cancelled.", reply_markup=main_menu(msg.from_user.id))


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(help_text())


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("⚙️ <b>👑 SUPER ADMIN PANEL</b>", reply_markup=admin_main_menu())


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(stats_text())


@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.first_name or "", call.from_user.username or "")
    u = get_user(call.from_user.id)
    await safe_edit(call, home_text(u, call.from_user.first_name or "User"), main_menu(call.from_user.id))


@dp.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    u = get_user(call.from_user.id)
    await safe_edit(call, profile_text(u, call.from_user.first_name or "User"),
                    kb([btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery):
    await safe_edit(call, server_stats_text(), kb([btn("🔙 Back", "back_main")]))


def help_text() -> str:
    return ui("help")


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await safe_edit(call, help_text(), kb([btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "check_join")
async def cb_check_join(call: CallbackQuery, state: FSMContext):
    for ch in get_channels():
        _join_cache.pop((call.from_user.id, ch), None)
    still = await missing_channels(call.from_user.id)
    if still:
        return await call.answer("❌ Abhi tak sare channels join nahi kiye! Join karke Verify dabao.", show_alert=True)
    await call.answer("✅ Verified! Welcome.")
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.first_name or "", call.from_user.username or "")
    u = get_user(call.from_user.id)
    await call.message.answer(home_text(u, call.from_user.first_name or "User"), reply_markup=main_menu(call.from_user.id))


# ==============================================================================
# 💎 BUY PREMIUM (plan -> payment -> screenshot -> admin approve)
# ==============================================================================
UPI_RE = re.compile(r"^[A-Za-z0-9._\-]{2,}@[A-Za-z]{2,}$")


def pay_api_key() -> str:
    """API key pehle env (PAY_API_KEY) se, warna DB se. Env me rakhna zyada safe hai."""
    return os.getenv("PAY_API_KEY", "").strip() or get_setting("pay_api_key", "")


def upi_link(upi: str, name: str, amount: str, note: str) -> str:
    return f"upi://pay?pa={quote(upi)}&pn={quote(name)}&am={quote(str(amount))}&cu=INR&tn={quote(note)}"


async def create_api_payment(pid: int, uid: int, days: int, amount: str) -> Optional[str]:
    url = get_setting("pay_api_url", "")
    key = pay_api_key()
    if not url:
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"order_id": f"PH{pid}", "amount": amount, "currency": "INR",
               "user_id": uid, "days": days, "description": f"Premium {days} days"}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status >= 400:
                    logger.warning("Payment API HTTP %s", r.status)
                    return None
                data = await r.json(content_type=None)
        if not isinstance(data, dict):
            return None
        link = data.get("payment_url") or data.get("url") or data.get("link")
        return link if isinstance(link, str) and link.startswith("http") else None
    except Exception as e:
        logger.warning("Payment API error: %s", e)
        return None


@dp.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery, state: FSMContext):
    await state.clear()
    plans = get_plans()
    rows = [[btn(f"🗓 {p['days']} Day{'s' if p['days'] > 1 else ''} — ₹{p['price']}", f"plan:{p['days']}")] for p in plans]
    rows.append([btn("🔙 Back", "back_main")])
    text = ui("buy", v_free=get_setting("free_limit", str(FREE_BOT_LIMIT)),
              v_prem=get_setting("premium_limit", str(PREMIUM_BOT_LIMIT)))
    if not plans:
        text += "\n\n<i>Abhi koi plan available nahi hai.</i>"
    await safe_edit(call, text, kb(*rows))


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery, state: FSMContext):
    try:
        days = int(call.data.split(":")[1])
    except ValueError:
        return await call.answer("Galat plan", show_alert=True)
    plan = get_plan(days)
    if not plan:
        return await call.answer("Ye plan ab available nahi hai.", show_alert=True)
    uid = call.from_user.id
    ensure_user(uid, call.from_user.first_name or "", call.from_user.username or "")
    pid = db_insert(
        "INSERT INTO payments (user_id, days, amount, method, status, created_at) VALUES (?,?,?,?, 'pending', ?)",
        (uid, days, plan["price"], get_setting("pay_mode", "upi"), now_str()),
    )
    if not pid:
        return await call.answer("Database error, dobara try karo.", show_alert=True)

    mode = get_setting("pay_mode", "upi")
    rows: List[List[InlineKeyboardButton]] = []
    if mode == "api":
        pay_url = await create_api_payment(pid, uid, days, plan["price"])
        if pay_url:
            text = (f"💳 <b>Payment — {days} Day(s)</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Amount: <b>₹{esc(plan['price'])}</b>\n\n"
                    "Neeche <b>Pay Now</b> dabao, payment karo, phir <b>I Paid</b> dabakar screenshot bhejo.")
            rows.append([InlineKeyboardButton(text="💳 Pay Now", url=pay_url)])
        else:
            text = "❌ Payment link abhi nahi bana. Admin ko contact karo."
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Payment API fail (payment #{pid}, user {uid}). Payment Setup check karo.")
            except Exception:
                pass
    else:
        upi = get_setting("upi_id", "")
        if not upi:
            text = "❌ Admin ne abhi UPI ID set nahi kiya. Thodi der baad try karo."
        else:
            text = (f"💳 <b>Payment — {days} Day(s)</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Amount: <b>₹{esc(plan['price'])}</b>\n"
                    f"UPI ID: <code>{esc(upi)}</code>\n"
                    f"Note: <code>PH{pid}</code>\n\n"
                    "UPI se pay karo, phir <b>I Paid</b> dabakar payment ka screenshot bhejo. "
                    "Admin approve karega to premium active ho jayega.")
    rows.append([btn("📸 I Paid — Send Screenshot", f"paid:{pid}")])
    rows.append([btn("🔙 Back", "buy")])
    await safe_edit(call, text, kb(*rows))


@dp.callback_query(F.data.startswith("paid:"))
async def cb_paid(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    pay = get_payment(pid)
    if not pay or pay["user_id"] != call.from_user.id or pay["status"] != "pending":
        return await call.answer("Ye payment valid nahi hai.", show_alert=True)
    await state.set_state(PayStates.wait_proof)
    await state.update_data(pay_id=pid)
    await safe_edit(
        call,
        f"📸 <b>Payment screenshot bhejo</b>\nPayment #{pid} — {pay['days']} day(s), ₹{esc(pay['amount'])}\n\n"
        "Photo ya screenshot is chat me bhejo. Cancel karne ke liye /cancel.",
        kb([btn("🔙 Back", "buy")]),
    )


async def _submit_proof(msg: Message, state: FSMContext, file_id: str, is_photo: bool):
    data = await state.get_data()
    pid = data.get("pay_id")
    pay = get_payment(pid) if pid else None
    if not pay or pay["user_id"] != msg.from_user.id or pay["status"] != "pending":
        await state.clear()
        return await msg.answer("❌ Ye payment ab valid nahi hai. Buy Premium se dobara try karo.")
    uniq = msg.photo[-1].file_unique_id if is_photo else msg.document.file_unique_id
    # Ek hi screenshot se do payments approve na ho sakein
    if db_query("SELECT id FROM payments WHERE proof_unique=? AND status!='rejected'", (uniq,), fetchone=True):
        return await msg.answer("❌ Ye screenshot pehle hi kisi payment me use ho chuka hai. Naya screenshot bhejo.")
    db_query("UPDATE payments SET proof_id=?, proof_unique=? WHERE id=?", (file_id, uniq, pid))
    await state.clear()
    caption = (f"💳 <b>Payment Proof</b> #{pid}\n👤 User: <code>{msg.from_user.id}</code> "
               f"{esc(msg.from_user.first_name or '')}\n🗓 Plan: {pay['days']} day(s)\n💰 Amount: ₹{esc(pay['amount'])}")
    markup = kb([btn("✅ Approve Premium", f"payok:{pid}"), btn("❌ Reject", f"payno:{pid}")])
    try:
        if is_photo:
            await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=markup)
        else:
            await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=markup)
    except Exception as e:
        logger.warning("Admin ko proof bhejne me error: %s", e)
    await msg.answer("⏳ Screenshot admin ko bhej diya. Approve hote hi premium active ho jayega.",
                     reply_markup=kb([btn("🔙 Back", "back_main")]))


@dp.message(PayStates.wait_proof, F.photo)
async def handle_proof_photo(msg: Message, state: FSMContext):
    await _submit_proof(msg, state, msg.photo[-1].file_id, True)


@dp.message(PayStates.wait_proof, F.document)
async def handle_proof_doc(msg: Message, state: FSMContext):
    await _submit_proof(msg, state, msg.document.file_id, False)


@dp.message(PayStates.wait_proof)
async def handle_proof_other(msg: Message, state: FSMContext):
    await msg.answer("📸 Screenshot (photo) bhejo. Cancel ke liye /cancel.")


@dp.callback_query(F.data.startswith("payok:"))
async def cb_pay_ok(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    pid = int(call.data.split(":")[1])
    pay = get_payment(pid)
    if not pay or pay["status"] != "pending":
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    # Atomic: do admin clicks ek saath ho to bhi premium ek hi baar milega
    if db_update_count("UPDATE payments SET status='approved', decided_at=? WHERE id=? AND status='pending'",
                       (now_str(), pid)) != 1:
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    set_premium(pay["user_id"], pay["days"])
    u = get_user(pay["user_id"]) or {}
    try:
        await bot.send_message(
            pay["user_id"],
            f"🎉 <b>Premium Activate ho gaya!</b>\n🗓 +{pay['days']} day(s)\n⏳ Till: {premium_expiry_text(u)}",
        )
    except Exception:
        pass
    await call.answer("✅ Premium approved")
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n✅ <b>APPROVED</b>", reply_markup=None)
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("payno:"))
async def cb_pay_no(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    pid = int(call.data.split(":")[1])
    pay = get_payment(pid)
    if not pay or pay["status"] != "pending":
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    if db_update_count("UPDATE payments SET status='rejected', decided_at=? WHERE id=? AND status='pending'",
                       (now_str(), pid)) != 1:
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    try:
        await bot.send_message(pay["user_id"], f"❌ Payment #{pid} reject ho gaya. Sahi screenshot ke saath dobara try karo.")
    except Exception:
        pass
    await call.answer("❌ Rejected")
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ <b>REJECTED</b>", reply_markup=None)
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


# ==============================================================================
# 📤 UPLOAD & HOSTED BOT MANAGEMENT
# ==============================================================================
def approval_markup(bid: int) -> InlineKeyboardMarkup:
    return kb([btn("✅ Approve", f"bapp:{bid}"), btn("❌ Reject", f"brej:{bid}")])


async def notify_admin_bot(hb: dict, owner_id: int, owner_name: str, note: str,
                           markup: Optional[InlineKeyboardMarkup] = None, prefix: str = "📥 Bot file"):
    """Bot ki file admin ko bhejta hai (upload / run dono par)."""
    caption = (f"{prefix}\n👤 User: <code>{owner_id}</code> {esc(owner_name)}\n"
               f"🆔 Bot ID: <code>{hb['id']}</code>\n📄 {esc(hb['bot_name'])}\n{note}")
    try:
        if os.path.exists(hb["file_path"]):
            await bot.send_document(ADMIN_ID, FSInputFile(hb["file_path"], filename=hb["bot_name"]),
                                    caption=caption[:1000], reply_markup=markup)
        else:
            await bot.send_message(ADMIN_ID, caption + "\n❌ File server par nahi mili.", reply_markup=markup)
    except Exception as e:
        logger.warning("Admin ko file bhejne me error: %s", e)


@dp.callback_query(F.data == "upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    u = get_user(call.from_user.id)
    count = user_bot_count(call.from_user.id)
    limit = bot_limit(u)
    if count >= limit:
        rows = [[btn("💎 Buy Premium", "buy")], [btn("🔙 Back", "back_main")]]
        return await safe_edit(
            call,
            f"🚫 <b>Slot limit poori ho gayi ({count}/{limit}).</b>\n\nAur bots host karne ke liye Premium lo 💎",
            kb(*rows),
        )
    await safe_edit(
        call,
        ui("upload_prompt", v_slots=f"{count}/{limit}", v_max=MAX_UPLOAD_MB),
        kb([btn("🔙 Cancel", "back_main")]),
    )
    await state.set_state(HostingStates.wait_file)


@dp.message(HostingStates.wait_file, F.document)
async def handle_upload(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    u = get_user(uid)
    doc = msg.document

    if not doc.file_name or not doc.file_name.lower().endswith(".py"):
        return await msg.answer("❌ Sirf <b>.py</b> files allowed hain.")
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        return await msg.answer(f"❌ File {MAX_UPLOAD_MB} MB se badi hai.")

    count = user_bot_count(uid)
    if count >= bot_limit(u):
        await state.clear()
        return await msg.answer(f"🚫 Slot limit poori ho gayi ({count}/{bot_limit(u)}). Premium lo 💎")

    user_dir = os.path.join(HOST_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", doc.file_name)
    file_path = os.path.join(user_dir, f"{int(time.time())}_{safe_name}")

    status = await msg.answer("⏳ File download ho rahi hai...")
    try:
        await bot.download(doc, destination=file_path)
    except Exception as e:
        await state.clear()
        return await status.edit_text(f"❌ File download nahi hui: <code>{esc(e)}</code>")

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if MAIN_BOT_TOKEN in content:
        os.remove(file_path)
        await state.clear()
        return await status.edit_text(
            "❌ <b>CRITICAL ERROR:</b>\nAapne Hosting Bot ka hi token file me daal diya hai!\n\n"
            "@BotFather se <b>NAYA BOT</b> banao aur uska token use karo."
        )

    fast = fast_approve_on()
    approval = "approved" if fast else "pending"
    auto = 1 if u["premium"] else 0
    bot_id = db_insert(
        "INSERT INTO hosted_bots (user_id, bot_name, file_path, auto_restart, created_at, approval) VALUES (?,?,?,?,?,?)",
        (uid, doc.file_name, file_path, auto, now_str(), approval),
    )
    await state.clear()
    if not bot_id:
        return await status.edit_text("❌ Database error. Dobara try karo.")

    hb = get_bot(bot_id)
    name = msg.from_user.first_name or ""
    if fast:
        note = "⚡ <b>Fast Approve ON</b> — auto approved"
        markup = None
    else:
        note = "⏳ <b>Pending</b> — approve ya reject karo"
        markup = approval_markup(bot_id)
    await notify_admin_bot(hb, uid, name, note, markup=markup, prefix="📥 New bot file upload")

    # Bot ki file user ko bhi mil jaye
    try:
        await msg.answer_document(FSInputFile(file_path, filename=doc.file_name),
                                  caption=f"📄 Aapki bot file (copy) — ID <code>{bot_id}</code>")
    except Exception as e:
        logger.warning("User ko file bhejne me error: %s", e)

    if fast:
        text = (f"✅ <b>File Uploaded Successfully!</b>\n\n📄 File: <code>{esc(doc.file_name)}</code>\n"
                f"🆔 Bot ID: <code>{bot_id}</code>")
    else:
        text = (f"✅ <b>File Uploaded!</b>\n\n📄 File: <code>{esc(doc.file_name)}</code>\n"
                f"🆔 Bot ID: <code>{bot_id}</code>\n\n{ui('pending')}")
    await status.edit_text(text, reply_markup=bot_panel_kb(bot_id, bool(hb["auto_restart"]), u["premium"], approval))


@dp.message(HostingStates.wait_file)
async def handle_upload_other(msg: Message, state: FSMContext):
    await msg.answer("📤 Apni <b>.py</b> file document ki tarah bhejo. Cancel: /cancel")


@dp.callback_query(F.data == "my_bots")
async def cb_my_bots(call: CallbackQuery):
    text, markup = my_bots_view(call.from_user.id)
    await safe_edit(call, text, markup)


async def fetch_owned(call: CallbackQuery, bot_id: int) -> Optional[dict]:
    hb = get_bot(bot_id)
    if not can_access(call.from_user.id, hb):
        await call.answer("Bot nahi mila ya access nahi hai.", show_alert=True)
        return None
    return hb


def build_panel(viewer_id: int, bot_id: int):
    hb = get_bot(bot_id)
    inst = get_instance(bot_id)
    viewer = get_user(viewer_id) or {"premium": False}
    approval = hb.get("approval") or "approved"
    if approval == "pending":
        status = "⏳ Pending Approval"
    elif approval == "rejected":
        status = "❌ Rejected by admin"
    else:
        status = "🟢 Running" if inst and inst.is_running else "🔴 Stopped"
    owner_note = ""
    if viewer_id == ADMIN_ID and hb["user_id"] != ADMIN_ID:
        owner_note = f"👤 Owner: <code>{hb['user_id']}</code>\n"
    text = (
        f"🤖 <b>Bot Control Panel</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📄 Name: <code>{esc(hb['bot_name'])}</code>\n"
        f"🆔 Bot ID: <code>{bot_id}</code>\n"
        f"{owner_note}"
        f"📊 Status: {status}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    return text, bot_panel_kb(bot_id, inst.auto_restart if inst else bool(hb["auto_restart"]),
                              viewer["premium"], approval)


async def render_panel(call: CallbackQuery, bot_id: int):
    text, markup = build_panel(call.from_user.id, bot_id)
    await safe_edit(call, text, markup)


@dp.callback_query(F.data.startswith("view:"))
async def cb_view(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if hb.get("approval") == "pending":
        return await call.answer("⏳ Admin approval pending hai.", show_alert=True)
    if hb.get("approval") == "rejected":
        return await call.answer("❌ Admin ne ye bot reject kiya hai.", show_alert=True)
    inst = get_instance(bot_id)
    if inst.is_running:
        return await call.answer("Bot pehle se running hai ✅", show_alert=True)

    # Har run par file admin ko bhi milegi
    await notify_admin_bot(hb, hb["user_id"], call.from_user.first_name or "", "▶️ Run request",
                           prefix="▶️ Bot run by user")

    await call.answer("🚀 Starting...")
    await inst.start()
    await asyncio.sleep(3)

    if not inst.is_running:
        logs = "\n".join(list(inst.logs)[-20:])[-2500:]
        await safe_edit(
            call,
            f"❌ <b>Bot Crash Ho Gaya!</b>\n\n<b>Error:</b>\n<pre>{esc(logs)}</pre>",
            kb([btn("📦 Pip Install", f"pip:{bot_id}")], [btn("🔙 Back", f"view:{bot_id}")]),
        )
    else:
        await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    if inst.is_running:
        await inst.stop()
        await call.answer("⏹ Bot stopped.")
    else:
        await call.answer("Bot already stopped hai.")
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if hb.get("approval") != "approved":
        return await call.answer("⏳ Pehle admin approval chahiye.", show_alert=True)
    inst = get_instance(bot_id)
    await call.answer("🔁 Restarting...")
    await inst.stop()
    await inst.start()
    await asyncio.sleep(2)
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    viewer = get_user(call.from_user.id)
    if not viewer["premium"]:
        return await call.answer("💎 Auto-Restart sirf Premium users ke liye hai!", show_alert=True)

    inst = get_instance(bot_id)
    inst.auto_restart = not inst.auto_restart
    db_query("UPDATE hosted_bots SET auto_restart=? WHERE id=?", (1 if inst.auto_restart else 0, bot_id))
    await call.answer(f"Auto-Restart {'ON ⚡' if inst.auto_restart else 'OFF'}")
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    viewer = get_user(call.from_user.id)
    inst = get_instance(bot_id)
    limit = PREMIUM_LOG_LINES if viewer["premium"] else FREE_LOG_LINES
    lines = list(inst.logs)[-limit:]
    log_text = "\n".join(lines) if lines else "Abhi koi log nahi hai."
    log_text = log_text[-3500:]
    await safe_edit(
        call,
        f"📋 <b>Live Logs</b> (last {limit})\n<pre>{esc(log_text)}</pre>",
        kb([btn("🔄 Refresh", f"logs:{bot_id}")], [btn("🔙 Back", f"view:{bot_id}")]),
    )


@dp.callback_query(F.data.startswith("clearlog:"))
async def cb_clearlog(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    inst.logs.clear()
    inst.log("🧹 Logs cleared!")
    await call.answer("🧹 Logs Cleared!")
    await cb_logs(call)


@dp.callback_query(F.data.startswith("dllog:"))
async def cb_dllog(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    viewer = get_user(call.from_user.id)
    if not viewer["premium"]:
        return await call.answer("💎 Full log download sirf Premium users ke liye hai!", show_alert=True)
    inst = get_instance(bot_id)
    data = "\n".join(inst.logs).encode("utf-8") or b"No logs."
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"{hb['bot_name']}_logs.txt"),
        caption=f"📥 Logs of <code>{esc(hb['bot_name'])}</code>",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("dlfile:"))
async def cb_dlfile(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if not os.path.exists(hb["file_path"]):
        return await call.answer("File server par nahi mili.", show_alert=True)
    await call.message.answer_document(
        FSInputFile(hb["file_path"], filename=hb["bot_name"]),
        caption=f"📄 <code>{esc(hb['bot_name'])}</code>",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pip:"))
async def cb_pip(call: CallbackQuery, state: FSMContext):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    await state.update_data(pip_bot_id=bot_id)
    await safe_edit(
        call,
        "📦 <b>Pip package ka naam bhejo:</b>\n(e.g. <code>pyrogram</code>, <code>requests==2.31.0</code>)",
        kb([btn("🔙 Cancel", f"view:{bot_id}")]),
    )
    await state.set_state(HostingStates.wait_pip_package)


PIP_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]<>=!~, ]{1,120}$")


@dp.message(HostingStates.wait_pip_package)
async def execute_pip(msg: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("pip_bot_id")
    pkg = (msg.text or "").strip()
    if not PIP_RE.match(pkg) or any(p.startswith("-") for p in pkg.split()):
        return await msg.answer("❌ Galat package name. Sirf letters, numbers aur <code>=&lt;&gt;!~[]</code> allowed hain.")

    await state.clear()
    status = await msg.answer(f"⏳ Installing <code>{esc(pkg)}</code>...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", *pkg.split(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=child_env(HOST_DIR),
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return await status.edit_text("⌛ Timeout (5 min). Dobara try karo.")

    back = kb([btn("🔙 Back to Bot", f"view:{bot_id}")]) if bot_id else kb([btn("🔙 Back", "back_main")])
    if proc.returncode == 0:
        await status.edit_text(f"✅ <b>Package Installed!</b>\n<code>{esc(pkg)}</code> ready hai.", reply_markup=back)
    else:
        err = stderr.decode(errors="ignore")[-1000:]
        await status.edit_text(f"❌ <b>Install Failed:</b>\n<pre>{esc(err)}</pre>", reply_markup=back)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    inst = ACTIVE_BOTS.pop(bot_id, None)
    if inst:
        await inst.stop()
    if os.path.exists(hb["file_path"]):
        os.remove(hb["file_path"])
    db_query("DELETE FROM hosted_bots WHERE id=?", (bot_id,))
    await call.answer("🗑 Bot Deleted!", show_alert=True)
    text, markup = my_bots_view(call.from_user.id)
    await safe_edit(call, text, markup)


# ---------------------------- Bot approval (admin) ----------------------------
@dp.callback_query(F.data.startswith("bapp:"))
async def cb_bot_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    bid = int(call.data.split(":")[1])
    hb = get_bot(bid)
    if not hb:
        return await call.answer("Bot nahi mila.", show_alert=True)
    set_approval(bid, "approved")
    try:
        await bot.send_message(hb["user_id"], f"✅ <b>Aapka bot approve ho gaya!</b>\n📄 <code>{esc(hb['bot_name'])}</code>\nAb Start dabao.")
    except Exception:
        pass
    await call.answer("✅ Approved")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("brej:"))
async def cb_bot_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    bid = int(call.data.split(":")[1])
    hb = get_bot(bid)
    if not hb:
        return await call.answer("Bot nahi mila.", show_alert=True)
    set_approval(bid, "rejected")
    inst = ACTIVE_BOTS.get(bid)
    if inst and inst.is_running:
        await inst.stop()
    try:
        await bot.send_message(hb["user_id"], f"❌ <b>Aapka bot admin ne reject kar diya.</b>\n📄 <code>{esc(hb['bot_name'])}</code>")
    except Exception:
        pass
    await call.answer("❌ Rejected")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ==============================================================================
# 👑 ADMIN PANEL
# ==============================================================================
@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    await state.clear()
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await safe_edit(call, stats_text(), kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query(
        "SELECT user_id, first_name, username, is_premium, premium_until, is_banned FROM users ORDER BY user_id DESC LIMIT 15",
        fetchall=True,
    ) or []
    lines = []
    for uid, name, uname, prem, until, banned in rows:
        p = "💎" if is_premium_active({"is_premium": prem, "premium_until": until}) else "·"
        b = "🚫" if banned else ""
        handle = f"@{uname}" if uname else (name or "—")
        lines.append(f"{p}{b} <code>{uid}</code> {esc(handle)}")
    body = "\n".join(lines) if lines else "Koi user nahi."
    await safe_edit(call, f"👥 <b>Latest Users</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\n💎 Premium  🚫 Banned",
                    kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_bots")
async def cb_adm_bots(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, bot_name, approval FROM hosted_bots ORDER BY id DESC LIMIT 20", fetchall=True) or []
    lines = []
    for bid, uid, name, approval in rows:
        inst = ACTIVE_BOTS.get(bid)
        icon = "⏳" if approval == "pending" else ("❌" if approval == "rejected" else ("🟢" if inst and inst.is_running else "🔴"))
        lines.append(f"{icon} <code>{bid}</code> {esc(name)} · <code>{uid}</code>")
    body = "\n".join(lines) if lines else "Koi bot nahi."
    await safe_edit(call, f"🤖 <b>All Hosted Bots (latest 20)</b>\n━━━━━━━━━━━━━━━━━━\n{body}",
                    kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_pending")
async def cb_adm_pending(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, bot_name FROM hosted_bots WHERE approval='pending' ORDER BY id DESC LIMIT 15",
                    fetchall=True) or []
    buttons = []
    for bid, uid, name in rows:
        buttons.append([btn(f"✅ #{bid} {name[:18]}", f"bapp:{bid}"), btn("❌", f"brej:{bid}")])
    buttons.append([btn("🔙 Back", "admin_main")])
    body = "\n".join(f"⏳ <code>{bid}</code> {esc(name)} · <code>{uid}</code>" for bid, uid, name in rows) or "Koi pending bot nahi."
    await safe_edit(call, f"⏳ <b>Pending Bot Approvals</b>\n━━━━━━━━━━━━━━━━━━\n{body}", kb(*buttons))


@dp.callback_query(F.data == "adm_stopall")
async def cb_adm_stopall(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    count = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.is_running:
            await inst.stop()
            count += 1
    await call.answer(f"🛑 {count} bots stopped.", show_alert=True)
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data.startswith("adm_in:"))
async def cb_adm_input(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data.split(":", 1)[1]
    if action not in ADMIN_PROMPTS:
        return await call.answer("Unknown action")
    await state.set_state(AdminStates.wait_input)
    await state.update_data(action=action)
    await safe_edit(call, admin_prompt_text(action), kb([btn("🔙 Cancel", "admin_main")]))


def parse_int(s: str) -> Optional[int]:
    s = s.strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


async def do_broadcast(msg: Message, only_premium: bool = False):
    rows = db_query("SELECT user_id, is_premium, premium_until FROM users WHERE is_banned=0", fetchall=True) or []
    if only_premium:
        rows = [r for r in rows if is_premium_active({"is_premium": r[1], "premium_until": r[2]})]
    users = [r[0] for r in rows]
    status = await msg.answer(f"⏳ Broadcasting to {len(users)} users...")
    sent = failed = 0
    for uid in users:
        try:
            await msg.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ <b>Broadcast Complete</b>\n📨 Sent: {sent}\n❌ Failed: {failed}")


async def stop_user_bots(uid: int) -> int:
    n = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.user_id == uid and inst.is_running:
            await inst.stop()
            n += 1
    return n


async def test_api_connection() -> str:
    url = get_setting("pay_api_url", "")
    key = pay_api_key()
    if not url:
        return "❌ Payment API URL set nahi hai."
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"test": True, "order_id": "TEST", "amount": "1", "currency": "INR", "user_id": 0, "days": 0}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                body = (await r.text())[:300]
                return f"{'✅' if r.status < 400 else '⚠️'} HTTP <b>{r.status}</b>\n<pre>{esc(body)}</pre>"
    except Exception as e:
        return f"❌ Connect nahi hua: <code>{esc(e)}</code>"


@dp.message(AdminStates.wait_input)
async def admin_input_handler(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    action = data.get("action")
    text = (msg.text or "").strip()
    back = kb([btn("🔙 Admin Panel", "admin_main")])

    if action == "broadcast":
        await state.clear()
        return await do_broadcast(msg)

    if action == "broadcast_prem":
        await state.clear()
        return await do_broadcast(msg, only_premium=True)

    if action == "open_bot":
        bid = parse_int(text)
        if bid is None or not get_bot(bid):
            return await msg.answer("❌ Is ID ka koi bot nahi mila. Sahi Bot ID bhejo.")
        await state.clear()
        panel_text, panel_markup = build_panel(ADMIN_ID, bid)
        return await msg.answer(panel_text, reply_markup=panel_markup)

    if action in ("limit_free", "limit_prem"):
        n = parse_int(text)
        if n is None or not 1 <= n <= 1000:
            return await msg.answer("❌ 1 se 1000 ke beech ka number bhejo.")
        set_setting("free_limit" if action == "limit_free" else "premium_limit", n)
        await state.clear()
        return await msg.answer(f"✅ {'Free' if action == 'limit_free' else 'Premium'} limit ab <b>{n}</b> bots hai.", reply_markup=back)

    if action == "stop_user":
        target = parse_int(text)
        if target is None:
            return await msg.answer("❌ Sahi numeric User ID bhejo.")
        await state.clear()
        n = await stop_user_bots(target)
        return await msg.answer(f"🛑 User <code>{target}</code> ke <b>{n}</b> bots band kiye.", reply_markup=back)

    if action == "chan_add":
        ch = normalize_channel(text)
        if ch is None:
            return await msg.answer("❌ Sahi channel <code>@username</code> ya link bhejo.")
        await state.clear()
        chans = get_channels()
        if ch in chans:
            return await msg.answer("ℹ️ Ye channel pehle se added hai.", reply_markup=kb([btn("🔙 Channels", "adm_chans")]))
        chans.append(ch)
        save_channels(chans)
        note = ""
        try:
            me = await bot.me()
            cm = await bot.get_chat_member(ch, me.id)
            if str(getattr(cm.status, "value", cm.status)) not in ("administrator", "creator"):
                note = "\n⚠️ Bot is channel me admin nahi hai — admin banao, warna verify kaam nahi karega."
        except Exception as e:
            note = f"\n⚠️ Channel check nahi hua ({esc(e)}). Bot ko channel me <b>admin</b> banao."
        return await msg.answer(f"✅ Channel add ho gaya: <b>{esc(ch)}</b>{note}", reply_markup=kb([btn("🔙 Channels", "adm_chans")]))

    if action == "upi_set":
        if not UPI_RE.match(text):
            return await msg.answer("❌ Sahi UPI ID bhejo (e.g. <code>name@okaxis</code>).")
        set_setting("upi_id", text)
        await state.clear()
        return await msg.answer(f"✅ UPI ID save: <code>{esc(text)}</code>", reply_markup=kb([btn("🔙 Payment Setup", "adm_paycfg")]))

    if action == "api_url":
        if not text.lower().startswith(("http://", "https://")):
            return await msg.answer("❌ https:// se shuru hone wala URL bhejo.")
        set_setting("pay_api_url", text)
        await state.clear()
        return await msg.answer("✅ Payment API URL save ho gaya.", reply_markup=kb([btn("🔙 Payment Setup", "adm_paycfg")]))

    if action == "api_key":
        set_setting("pay_api_key", text)
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        return await msg.answer("✅ Payment API key save ho gayi.", reply_markup=kb([btn("🔙 Payment Setup", "adm_paycfg")]))

    if action == "plan_add":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>DAYS PRICE</code> (e.g. <code>15 79</code>)")
        days, price = int(parts[0]), parts[1]
        if days <= 0:
            return await msg.answer("❌ Days 1 ya usse zyada hone chahiye.")
        plans = [p for p in get_plans() if p["days"] != days]
        plans.append({"days": days, "price": price})
        save_plans(plans)
        await state.clear()
        return await msg.answer(f"✅ Plan add/update: {days} day(s) — ₹{esc(price)}", reply_markup=kb([btn("🔙 Plans", "adm_plans")]))

    if action == "plan_price":
        days = data.get("plan_days")
        if not text.isdigit() or int(text) <= 0:
            return await msg.answer("❌ Sirf positive number bhejo (e.g. 99).")
        plans = get_plans()
        for p in plans:
            if p["days"] == days:
                p["price"] = text
        save_plans(plans)
        await state.clear()
        return await msg.answer(f"✅ {days} day plan ka price ab ₹{text} hai.", reply_markup=kb([btn("🔙 Plans", "adm_plans")]))

    if action == "give_prem":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>USER_ID DAYS</code> (e.g. <code>123456 30</code>)")
        uid, days = int(parts[0]), int(parts[1])
        ensure_user(uid)
        set_premium(uid, days)
        await state.clear()
        period = "Lifetime ♾" if days <= 0 else f"{days} days"
        try:
            await bot.send_message(uid, f"💎 <b>Premium activate ho gaya!</b>\n🗓 {period}")
        except Exception:
            pass
        return await msg.answer(f"💎 User <code>{uid}</code> ab Premium hai ({period})!", reply_markup=back)

    uid = parse_int(text)
    if uid is None:
        return await msg.answer("❌ Sahi numeric User ID bhejo.")
    await state.clear()

    if action == "rm_prem":
        remove_premium(uid)
        await msg.answer(f"🚫 Premium removed for <code>{uid}</code>.", reply_markup=back)
    elif action == "ban":
        if uid == ADMIN_ID:
            return await msg.answer("😅 Admin ko ban nahi kar sakte.", reply_markup=back)
        ensure_user(uid)
        set_ban(uid, True)
        stopped = await stop_user_bots(uid)
        await msg.answer(f"⛔ User <code>{uid}</code> banned. ({stopped} running bots band kiye)", reply_markup=back)
    elif action == "unban":
        set_ban(uid, False)
        await msg.answer(f"✅ User <code>{uid}</code> unbanned.", reply_markup=back)
    elif action == "info":
        u = get_user(uid)
        if not u:
            return await msg.answer("❌ User database me nahi mila.", reply_markup=back)
        await msg.answer(
            f"🔍 <b>User Info</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📛 Name: {esc(u['first_name'])}\n"
            f"🔗 Username: @{esc(u['username']) if u['username'] else '—'}\n"
            f"💎 Plan: {'PREMIUM' if u['premium'] else 'FREE'}\n"
            f"📅 Premium Till: {premium_expiry_text(u)}\n"
            f"🚫 Banned: {'Yes' if u['banned'] else 'No'}\n"
            f"📦 Bots: {user_bot_count(uid)}/{bot_limit(u)}\n"
            f"🗓 Joined: {u['joined_at'] or '—'}",
            reply_markup=back,
        )


@dp.callback_query(F.data == "adm_prem_list")
async def cb_adm_prem_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT user_id, first_name, username, is_premium, premium_until FROM users WHERE is_premium=1 ORDER BY user_id DESC LIMIT 60", fetchall=True) or []
    lines = []
    for uid, name, uname, prem, until in rows:
        if not is_premium_active({"is_premium": prem, "premium_until": until}):
            continue
        handle = f"@{uname}" if uname else (name or "—")
        lines.append(f"💎 <code>{uid}</code> {esc(handle)} · {'♾' if not until else until[:10]}")
    body = "\n".join(lines[:40]) if lines else "Koi active premium user nahi."
    await safe_edit(call, f"💎 <b>Premium Users</b> ({len(lines)})\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_ban_list")
async def cb_adm_ban_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT user_id, first_name, username FROM users WHERE is_banned=1 ORDER BY user_id DESC LIMIT 40", fetchall=True) or []
    body = "\n".join(f"⛔ <code>{uid}</code> {esc(('@' + uname) if uname else (name or '—'))}" for uid, name, uname in rows) or "Koi banned user nahi."
    await safe_edit(call, f"⛔ <b>Banned Users</b> ({len(rows)})\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Back", "admin_main")]))


@dp.callback_query(F.data == "adm_backup")
async def cb_adm_backup(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("🗂 Backup ban raha hai...")
    name = f"hosting_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    tmp = os.path.join(tempfile.gettempdir(), name)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        await call.message.answer_document(FSInputFile(tmp, filename=name), caption="🗂 Database backup")
    finally:
        os.remove(tmp)


@dp.callback_query(F.data == "adm_maint")
async def cb_adm_maint(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if get_setting("maintenance", "OFF") == "ON" else "ON"
    set_setting("maintenance", new)
    await call.answer(f"🔧 Maintenance {new}")
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data == "adm_toggle_pe")
async def cb_adm_toggle_pe(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if get_setting("premium_emoji", "ON") == "ON" else "ON"
    set_setting("premium_emoji", new)
    await call.answer(f"✨ Premium Emoji {new}")
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


@dp.callback_query(F.data == "adm_fast")
async def cb_adm_fast(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if fast_approve_on() else "ON"
    set_setting("fast_approve", new)
    await call.answer(f"⚡ Fast Approve {new}")
    await safe_edit(call, "⚙️ <b>👑 SUPER ADMIN PANEL</b>", admin_main_menu())


# ---------------------------- Force-join channels manager ----------------------------
@dp.callback_query(F.data == "adm_chans")
async def cb_adm_chans(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    chans = get_channels()
    rows = [[btn(f"🗑 {ch}", f"adm_cdel:{ch}")] for ch in chans]
    rows.append([btn("➕ Add Channel", "adm_in:chan_add")])
    rows.append([btn("🔙 Back", "admin_main")])
    body = "\n".join(f"📌 {esc(c)}" for c in chans) or "<i>Koi channel nahi (sab open hai)</i>"
    await safe_edit(call, f"📌 <b>Force Join Channels</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\nChannel pe tap karke remove karo.", kb(*rows))


@dp.callback_query(F.data.startswith("adm_cdel:"))
async def cb_adm_cdel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    ch = call.data.split(":", 1)[1]
    save_channels([c for c in get_channels() if c != ch])
    await call.answer("Removed")
    chans = get_channels()
    rows = [[btn(f"🗑 {c}", f"adm_cdel:{c}")] for c in chans]
    rows.append([btn("➕ Add Channel", "adm_in:chan_add")])
    rows.append([btn("🔙 Back", "admin_main")])
    body = "\n".join(f"📌 {esc(c)}" for c in chans) or "<i>Koi channel nahi (sab open hai)</i>"
    await safe_edit(call, f"📌 <b>Force Join Channels</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\nChannel pe tap karke remove karo.", kb(*rows))


# ---------------------------- Plans & prices ----------------------------
@dp.callback_query(F.data == "adm_plans")
async def cb_adm_plans(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    rows = []
    for p in get_plans():
        rows.append([btn(f"✏️ {p['days']}d — ₹{p['price']}", f"adm_plan:{p['days']}"),
                     btn("🗑", f"adm_pdel:{p['days']}")])
    rows.append([btn("➕ Add Plan", "adm_in:plan_add")])
    rows.append([btn("🔙 Back", "admin_main")])
    await safe_edit(call, "💰 <b>Plans & Prices</b>\nPlan pe tap karke price badlo, 🗑 se delete karo.", kb(*rows))


@dp.callback_query(F.data.startswith("adm_plan:"))
async def cb_adm_plan(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    days = int(call.data.split(":")[1])
    await state.set_state(AdminStates.wait_input)
    await state.update_data(action="plan_price", plan_days=days)
    await safe_edit(call, admin_prompt_text("plan_price", extra=str(days)), kb([btn("🔙 Cancel", "adm_plans")]))


@dp.callback_query(F.data.startswith("adm_pdel:"))
async def cb_adm_pdel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    days = int(call.data.split(":")[1])
    save_plans([p for p in get_plans() if p["days"] != days])
    await call.answer("Plan deleted")
    await cb_adm_plans(call, state)


# ---------------------------- Payments (admin) ----------------------------
@dp.callback_query(F.data == "adm_pay")
async def cb_adm_pay(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, days, amount FROM payments WHERE status='pending' AND proof_id!='' ORDER BY id DESC LIMIT 15",
                    fetchall=True) or []
    buttons = []
    for pid, uid, days, amount in rows:
        buttons.append([btn(f"✅ #{pid} {days}d ₹{amount}", f"payok:{pid}"), btn("❌", f"payno:{pid}")])
    buttons.append([btn("🔙 Back", "admin_main")])
    body = "\n".join(f"💳 #{pid} · <code>{uid}</code> · {days} day(s) · ₹{esc(amount)}" for pid, uid, days, amount in rows) \
        or "Koi pending payment nahi."
    await safe_edit(call, f"💳 <b>Pending Payments</b>\n━━━━━━━━━━━━━━━━━━\n{body}", kb(*buttons))


# ---------------------------- Payment setup + tests ----------------------------
def paycfg_text() -> str:
    mode = get_setting("pay_mode", "upi")
    key = get_setting("pay_api_key", "")
    masked = ("•" * 6 + key[-4:]) if key else "not set"
    return (
        "💳 <b>Payment Setup</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Mode: <b>{mode.upper()}</b>\n"
        f"UPI ID: <code>{esc(get_setting('upi_id', '') or 'not set')}</code>\n"
        f"API URL: <code>{esc(get_setting('pay_api_url', '') or 'not set')}</code>\n"
        f"API Key: <code>{esc(masked)}</code>\n━━━━━━━━━━━━━━━━━━"
    )


def paycfg_markup() -> InlineKeyboardMarkup:
    mode = get_setting("pay_mode", "upi")
    return kb(
        [btn(f"🔁 Mode: {mode.upper()} (tap to switch)", "adm_paymode")],
        [btn("✏️ Set UPI ID", "adm_in:upi_set"), btn("🧪 Test UPI", "adm_test_upi")],
        [btn("🔗 Set API URL", "adm_in:api_url"), btn("🔑 Set API Key", "adm_in:api_key")],
        [btn("🧪 Test API Connection", "adm_test_api")],
        [btn("🔙 Back", "admin_main")],
    )


@dp.callback_query(F.data == "adm_paycfg")
async def cb_adm_paycfg(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await safe_edit(call, paycfg_text(), paycfg_markup())


@dp.callback_query(F.data == "adm_paymode")
async def cb_adm_paymode(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "api" if get_setting("pay_mode", "upi") == "upi" else "upi"
    set_setting("pay_mode", new)
    await call.answer(f"Mode: {new.upper()}")
    await safe_edit(call, paycfg_text(), paycfg_markup())


@dp.callback_query(F.data == "adm_test_upi")
async def cb_adm_test_upi(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    upi = get_setting("upi_id", "")
    if not upi:
        return await call.answer("Pehle UPI ID set karo.", show_alert=True)
    if not UPI_RE.match(upi):
        return await call.answer("UPI ID ka format galat hai.", show_alert=True)
    link = upi_link(upi, BRAND, "1", "TEST")
    await call.answer("✅ UPI format valid")
    await call.message.answer(
        f"🧪 <b>UPI Test</b>\n✅ Format valid: <code>{esc(upi)}</code>\n"
        f"Test payment link (₹1):\n<code>{esc(link)}</code>\n\n"
        "Is link ko UPI app me kholkar check kar sakte ho. Payment hone se pehle cancel kar dena.",
    )


@dp.callback_query(F.data == "adm_test_api")
async def cb_adm_test_api(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("🧪 Testing...")
    result = await test_api_connection()
    await call.message.answer(f"🧪 <b>API Test</b>\n{result}")


# ---------------------------- Edit global emojis (har slot ka premium ID) ----------------------------
EMOJI_PAGE = 12


@dp.callback_query(F.data.startswith("adm_emoji"))
async def cb_adm_emoji(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    page = int(call.data.split(":")[1]) if ":" in call.data and call.data.split(":")[1].isdigit() else 0
    slots = list(DEFAULT_EMOJIS)
    pages = (len(slots) + EMOJI_PAGE - 1) // EMOJI_PAGE
    page = max(0, min(page, pages - 1))
    rows, pair = [], []
    for sl in slots[page * EMOJI_PAGE:(page + 1) * EMOJI_PAGE]:
        has_premium = bool(get_setting(f"emoji_{sl}", "") or DEFAULT_EMOJIS.get(sl))
        pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{' *' if has_premium else ''}", f"emo:{sl}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"adm_emoji:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"adm_emoji:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 Back", "admin_main")])
    await safe_edit(
        call,
        f"🎨 <b>Edit Emojis</b> (page {page + 1}/{pages})\n\nSlot chuno, phir premium emoji (ya ID) bhejo.\n"
        "<code>*</code> = premium ID set hai, baaki me normal emoji dikhta hai.\n"
        "💡 Koi premium emoji bot ko bhejoge to wo uska ID bata dega. Ye DB me save hota hai (restart ke baad bhi).",
        kb(*rows),
    )


@dp.callback_query(F.data.startswith("emo:"))
async def cb_emoji_pick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    slot = call.data.split(":", 1)[1]
    if slot not in DEFAULT_EMOJIS:
        return await call.answer("Unknown slot", show_alert=True)
    await state.update_data(slot=slot)
    await state.set_state(AdminStates.wait_emoji_value)
    cur = get_setting(f"emoji_{slot}", "") or DEFAULT_EMOJIS.get(slot) or "—"
    await safe_edit(
        call,
        f"🎨 <b>{slot}</b> {EMOJI_FALLBACK[slot]}\nCurrent ID: <code>{cur}</code>\n\n"
        "Ek <b>premium emoji</b> (ya numeric ID) bhejo.\n<code>default</code> = code wala default.\n<code>none</code> = sirf normal emoji.",
        kb([btn("🔙 Back", "adm_emoji")]),
    )


def _read_emoji_input(msg: Message) -> Tuple[Optional[str], bool]:
    """(emoji_id, is_default_reset)"""
    txt = (msg.text or msg.caption or "").strip()
    ids = [e.custom_emoji_id for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if txt.lower() in ("default", "reset"):
        return None, True
    if ids:
        return str(ids[0]), False
    if txt.isdigit():
        return txt, False
    return None, False


@dp.message(AdminStates.wait_emoji_value)
async def admin_save_emoji(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    slot = (await state.get_data()).get("slot")
    if slot not in DEFAULT_EMOJIS:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    txt = (msg.text or "").strip().lower()
    eid, reset = _read_emoji_input(msg)
    if txt == "none":
        set_setting(f"emoji_{slot}", "x")          # non-numeric => normal emoji hi dikhega
        done = "ab normal emoji dikhayega"
    elif reset:
        del_setting(f"emoji_{slot}")
        done = "default par wapas"
    elif eid:
        set_setting(f"emoji_{slot}", eid)
        done = f"set: <code>{eid}</code>"
    else:
        return await msg.answer("❌ Premium emoji, numeric ID, <code>default</code> ya <code>none</code> bhejo.")
    await state.clear()
    await msg.answer(f"✅ <b>{slot}</b> {done}.", reply_markup=kb([btn("🔙 Back to emojis", "adm_emoji")]))


# ---------------------------- Edit UI texts: har screen ka apna admin panel ----------------------------
def _ui_slots_in(key: str) -> List[str]:
    tpl = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    out: List[str] = []
    for sl in re.findall(r"\{([a-z_]+)\}", tpl):
        if sl in DEFAULT_EMOJIS and sl not in out:
            out.append(sl)
    return out


def _ui_key(call: CallbackQuery) -> Optional[str]:
    key = call.data.split(":", 1)[1].split(":", 1)[0]
    return key if key in UI_EDITABLE else None


@dp.callback_query(F.data == "adm_ui")
async def cb_adm_ui(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    rows, pair = [], []
    for key, (label, _h) in UI_EDITABLE.items():
        tags = []
        if get_setting(f"ui_{key}", ""):
            tags.append("edited")
        if get_setting(f"ui_yt_{key}", ""):
            tags.append("video")
        pair.append(btn(label + (f" ({', '.join(tags)})" if tags else ""), f"uip:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔙 Back", "admin_main")])
    await safe_edit(call, "✏️ <b>Edit UI Texts</b>\n\nHar screen ka alag admin panel hai: text, YouTube link aur emojis.\nEk screen chuno:", kb(*rows))


async def _show_ui_panel(call: CallbackQuery, key: str):
    yt = get_setting(f"ui_yt_{key}", "")
    edited = bool(get_setting(f"ui_{key}", ""))
    n_emoji = len([1 for sl in _ui_slots_in(key) if get_setting(f"uiemoji_{key}_{sl}", "")])
    rows = [
        [btn("✏️ Edit Text", f"uied:{key}"), btn("👁 Preview", f"uiprev:{key}")],
        [btn("▶️ " + ("Change YouTube Link" if yt else "Set YouTube Link"), f"uiyt:{key}")],
    ]
    if yt:
        rows.append([btn("🗑 Remove YouTube Link", f"uiytd:{key}")])
    rows.append([btn("🎨 Set Emojis of this screen", f"uiem:{key}")])
    rows.append([btn("🔄 Reset text to default", f"uirs:{key}")])
    rows.append([btn("🔙 Back to list", "adm_ui")])
    await safe_edit(
        call,
        f"✏️ <b>{UI_EDITABLE[key][0]}</b> — admin panel\n\n"
        f"▶️ YouTube: {esc(yt) if yt else '<i>not set</i>'}\n"
        f"✅ Custom text: <b>{'yes' if edited else 'no (default)'}</b>\n"
        f"🎨 Custom emojis: <b>{n_emoji}</b>",
        kb(*rows),
    )


@dp.callback_query(F.data.startswith("uip:"))
async def cb_ui_panel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.clear()
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiprev:"))
async def cb_ui_preview(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await call.answer()
    text = re.sub(r"\{(v_\w+)\}", "—", ui(key))
    btn_ = ui_video_button(key)
    try:
        await call.message.answer(text, reply_markup=kb([btn_]) if btn_ else None)
    except Exception as e:
        await call.message.answer(f"❌ Is text me galat HTML / emoji hai:\n<code>{esc(str(e))[:300]}</code>")


@dp.callback_query(F.data.startswith("uied:"))
async def cb_ui_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.wait_ui_text)
    current = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    await safe_edit(
        call,
        f"✏️ <b>{UI_EDITABLE[key][0]}</b>\n\nNaya text bhejo (HTML / formatting / premium emoji chalega).\n"
        f"Placeholders: <code>{esc(UI_EDITABLE[key][1])}</code>\n\nCurrent template:\n<pre>{esc(current)}</pre>",
        kb([btn("🔙 Back", f"uip:{key}")]),
    )


@dp.message(AdminStates.wait_ui_text)
async def admin_save_ui_text(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    key = (await state.get_data()).get("ui_key")
    new_text = (msg.html_text or "").strip()
    if key not in UI_EDITABLE or not new_text:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    if len(new_text) > 3500:
        return await msg.answer("❌ Bahut lamba (max 3500 characters).")
    set_setting(f"ui_{key}", new_text)
    await state.clear()
    await msg.answer(f"✅ <b>{UI_EDITABLE[key][0]}</b> update ho gaya!", reply_markup=kb([btn("🔙 Back to this screen", f"uip:{key}")]))


@dp.callback_query(F.data.startswith("uirs:"))
async def cb_ui_reset(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_setting(f"ui_{key}")
    await state.clear()
    await call.answer("Default par reset")
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiyt:"))
async def cb_ui_yt(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.wait_ui_video)
    cur = get_setting(f"ui_yt_{key}", "")
    await safe_edit(
        call,
        f"▶️ <b>YouTube link — {UI_EDITABLE[key][0]}</b>\n\nYouTube video ka link bhejo. Is screen par users ko <b>Watch Video</b> button dikhega.\n"
        f"Example: <code>https://youtu.be/xxxxxxxxxxx</code>\nCurrent: {esc(cur) if cur else '<i>none</i>'}",
        kb([btn("🔙 Back", f"uip:{key}")]),
    )


@dp.message(AdminStates.wait_ui_video)
async def admin_save_ui_video(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    key = (await state.get_data()).get("ui_key")
    link = (msg.text or "").strip()
    if key not in UI_EDITABLE:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    if not YT_RE.match(link):
        return await msg.answer("❌ Sahi YouTube link bhejo (youtube.com ya youtu.be).")
    set_setting(f"ui_yt_{key}", link)
    await state.clear()
    await msg.answer(f"✅ <b>{UI_EDITABLE[key][0]}</b> ke liye YouTube link save ho gaya.", reply_markup=kb([btn("🔙 Back to this screen", f"uip:{key}")]))


@dp.callback_query(F.data.startswith("uiytd:"))
async def cb_ui_yt_delete(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_setting(f"ui_yt_{key}")
    await call.answer("YouTube link hata diya")
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiem:"))
async def cb_ui_emoji_list(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.clear()
    rows, pair = [], []
    for sl in _ui_slots_in(key):
        mark = " *" if get_setting(f"uiemoji_{key}_{sl}", "") else ""
        pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{mark}", f"uies:{key}:{sl}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔄 Reset all emojis of this screen", f"uiemr:{key}")])
    rows.append([btn("🔙 Back", f"uip:{key}")])
    await safe_edit(
        call,
        f"🎨 <b>Emojis — {UI_EDITABLE[key][0]}</b>\n\nIs screen ka emoji slot chuno, phir <b>premium emoji</b> (ya uska ID) bhejo.\n"
        "<code>*</code> = is screen ke liye custom emoji set hai.\nJis slot me premium ID nahi, wahan normal emoji dikhta hai.",
        kb(*rows),
    )


@dp.callback_query(F.data.startswith("uies:"))
async def cb_ui_emoji_pick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        _, key, slot = call.data.split(":", 2)
    except ValueError:
        return await call.answer("Unknown slot", show_alert=True)
    if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
        return await call.answer("Unknown slot", show_alert=True)
    await state.update_data(ui_key=key, slot=slot)
    await state.set_state(AdminStates.wait_ui_emoji)
    await safe_edit(
        call,
        f"🎨 <b>{slot}</b> — {UI_EDITABLE[key][0]}\n\nEk <b>premium emoji</b> (ya numeric ID) bhejo.\n"
        "<code>default</code> bhejo to global emoji wapas aa jayega.",
        kb([btn("🔙 Back", f"uiem:{key}")]),
    )


@dp.message(AdminStates.wait_ui_emoji)
async def admin_save_ui_emoji(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key, slot = data.get("ui_key"), data.get("slot")
    if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    eid, reset = _read_emoji_input(msg)
    if reset:
        del_setting(f"uiemoji_{key}_{slot}")
        done = "global emoji par wapas"
    elif eid:
        set_setting(f"uiemoji_{key}_{slot}", eid)
        done = f"set: <code>{eid}</code>"
    else:
        return await msg.answer("❌ Premium emoji, numeric ID ya <code>default</code> bhejo.")
    await state.clear()
    await msg.answer(f"✅ <b>{slot}</b> {done}.", reply_markup=kb([btn("🔙 Back to emojis", f"uiem:{key}")]))


@dp.callback_query(F.data.startswith("uiemr:"))
async def cb_ui_emoji_reset(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_settings_like(f"uiemoji_{key}_%")
    await call.answer("Emojis reset")
    await cb_ui_emoji_list(call, state)


# ---------------------------- Emoji Map: koi bhi emoji -> premium (sab jagah apply) ----------------------------
async def _render_emap(call: CallbackQuery):
    rows = []
    for ch, pid in list(custom_emoji_map().items())[:30]:
        rows.append([btn(f"{ch} → {pid[:10]}…", f"emapdel:{_emap_key(ch)}")])
    rows.append([btn("🔙 Back", "admin_main")])
    await safe_edit(
        call,
        "🗺 <b>Emoji Map</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Koi bhi <b>premium emoji</b> is chat me bhejo. Wo mapping save ho jayegi, aur bot ke har message / button me "
        "us normal emoji ki jagah premium dikhega.\n"
        "Mapping delete karne ke liye neeche tap karo.\n\n<i>Band karne ke liye /cancel.</i>",
        kb(*rows),
    )


@dp.callback_query(F.data == "adm_emap")
async def cb_adm_emap(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.wait_emap)
    await _render_emap(call)


@dp.callback_query(F.data.startswith("emapdel:"))
async def cb_emap_del(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = call.data.split(":", 1)[1]
    if not key.startswith("emap_"):
        return await call.answer("Invalid", show_alert=True)
    del_setting(key)
    await call.answer("Mapping removed")
    await state.set_state(AdminStates.wait_emap)
    await _render_emap(call)


@dp.message(AdminStates.wait_emap)
async def admin_save_emap(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    txt = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    raw = txt.encode("utf-16-le")          # Telegram offsets UTF-16 me hote hain
    saved = []
    for e in ents:
        if e.type == "custom_emoji" and e.custom_emoji_id:
            ch = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", errors="ignore")
            if ch:
                set_setting(_emap_key(ch), e.custom_emoji_id)
                saved.append(f"{ch} → <code>{e.custom_emoji_id}</code>")
    if not saved:
        return await msg.answer("❌ Premium emoji bhejo (normal emoji nahi). Band karne ke liye /cancel.")
    await msg.answer("✅ <b>Mapping save ho gayi:</b>\n" + "\n".join(saved) + "\n\nAur bhejo, ya /cancel se band karo.")


# ---------------------------- Premium emoji ID finder (admin kisi bhi premium emoji bheje) ----------------------------
@dp.message(F.from_user.id == ADMIN_ID, F.entities)
async def admin_emoji_id_finder(msg: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    ids = [e.custom_emoji_id for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if not ids:
        return
    lines = "\n".join(f"<code>{i}</code>" for i in dict.fromkeys(ids))
    await msg.answer(f"🎨 <b>Premium emoji ID(s):</b>\n{lines}\n\nAdmin → Edit Emojis me jakar kisi slot par set kar do.")


# ==============================================================================
# 🚀 STARTUP / SHUTDOWN + WEB SERVER (Render ke liye)
# ==============================================================================
async def restore_bots():
    rows = db_query("SELECT id FROM hosted_bots WHERE status='Running' AND approval='approved'", fetchall=True) or []
    for (bid,) in rows:
        inst = get_instance(bid)
        if inst:
            await inst.start()
            logger.info("♻️ Restored bot %s", bid)


async def on_startup():
    init_db()
    os.makedirs(HOST_DIR, exist_ok=True)
    try:
        os.chmod(HOST_DIR, 0o700)
    except OSError:
        pass
    logger.info("🚀 %s v%s started!", BRAND, VERSION)
    asyncio.create_task(restore_bots())


async def on_shutdown():
    # Status DB me change nahi karte, taaki restart ke baad bots wapas chal jayein
    for inst in ACTIVE_BOTS.values():
        inst.kill_silently()
    logger.info("🛑 Hosting bot shutting down.")


async def health(_request: web.Request) -> web.Response:
    running = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    return web.json_response({"status": "ok", "brand": BRAND, "version": VERSION, "running_bots": running})


async def start_web() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("🌐 Web server port %s par chal raha hai", PORT)
    return runner


async def polling_forever(stop: asyncio.Event):
    """Polling crash ho to 5 second baad apne aap restart hoti hai."""
    while not stop.is_set():
        try:
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Polling crash: %s, 5s me restart", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Pehle port khulega, taaki Render ka health check turant pass ho
    runner = await start_web()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    poller = asyncio.create_task(polling_forever(stop))
    try:
        await stop.wait()
    finally:
        try:
            await dp.stop_polling()
        except Exception:
            pass
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        await on_shutdown()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
