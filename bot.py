# -*- coding: utf-8 -*-
"""
Бот-офферница для Telegram NFT-подарков.
Работает в фоне, сам восстанавливается, сам ищет канал.
"""

import html
import json
import os
import re
import signal
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

try:
    import socks as _socks
except Exception:
    _socks = None

# ═══════════════════════ НАСТРОЙКИ ═══════════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "9135617510:AAH-y438L8o7xkpDKU6M8AjuXCTdge67-4U")
ADMIN_ID  = int(os.getenv("ADMIN_ID", "6130974573"))
# Все ID администраторов: основной ADMIN_ID + дополнительные из ADMIN_IDS.
ADMIN_IDS = {ADMIN_ID, 5082864281}
ADMIN_IDS.update(int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API      = f"https://api.telegram.org/bot{BOT_TOKEN}/"
DB_PATH  = os.getenv("DB_PATH", os.path.join(BASE_DIR, "offers.sqlite3"))
LOG_PATH = os.getenv("LOG_PATH", os.path.join(BASE_DIR, "bot.log"))
LOCK_PORT = int(os.getenv("LOCK_PORT", "47821"))

# Прокси нужен только если бот за файрволом. На облаке оставь PROXY пустым.
PROXY_CANDIDATES = [p.strip() for p in os.getenv("PROXY", "").split(",") if p.strip()]

DEFAULT_TTL_H    = int(os.getenv("DEFAULT_TTL_H", "6"))
TRANSFER_TTL_MIN = int(os.getenv("TRANSFER_TTL_MIN", "30"))
TICK             = 60
HINT_ENABLED     = True
LOG_KEEP_DAYS    = 30

E_STARS, E_GRAM   = "5848259999763011021", "5377620962390857342"
E_DECLINE, E_OK   = "5893081007153746175", "5409029658794537988"

HINT_TEXT = (
    "\u26a1\ufe0f <b>\u041e\u0444\u0444\u0435\u0440 \u0441\u043e\u0437\u0434\u0430\u043d</b>\n"
    "\n"
    "<blockquote>\u21a9\ufe0f <b>\u041e\u0442\u0432\u0435\u0442\u044c\u0442\u0435 "
    "\u043d\u0430 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435</b>, "
    "\u0447\u0442\u043e\u0431\u044b \u043e\u0444\u0444\u0435\u0440 "
    "\u043f\u0440\u043e\u0433\u0440\u0443\u0437\u0438\u043b\u0441\u044f "
    "\u0434\u043e \u043a\u043e\u043d\u0446\u0430</blockquote>\n"
    "\n"
    "<i>\u041f\u043e\u0441\u043b\u0435 \u043e\u0442\u0432\u0435\u0442\u0430 "
    "\u043a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043e\u0431\u043d\u043e\u0432\u0438\u0442\u0441\u044f "
    "\u043f\u043e\u043b\u043d\u043e\u0441\u0442\u044c\u044e</i>"
)

REMIND_TEXT = "\u274c <b>\u041f\u0435\u0440\u0435\u0434\u0430\u0439\u0442\u0435 \u043f\u043e\u0434\u0430\u0440\u043e\u043a \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044e.</b>"

SELLER = "@aluystrax"
BUY_TEXT = (f"\U0001f512 <b>Купите подписку у {SELLER}</b>\n\n"
            f"<i>Доступ активируется после оплаты</i>")

HELP_TEXT = (
    "<b>Бот-офферница для NFT-подарков</b>\n\n"
    "1. Подключи меня в Настройках → <b>Автоматизация чатов</b>\n"
    "2. Выдай права: отвечать и удалять сообщения\n"
    "3. В чате с продавцом напиши:\n"
    "<code>.buy https://t.me/nft/ИмяКоллекции-12345 1500 stars</code>\n\n"
    "Валюта: <code>stars</code> или <code>gram</code>.\n"
    "В личке можно указать покупателя: <code>@user</code> или <code>id:123456</code>.\n"
    "Служебные: <code>.status</code>, <code>.cancel</code>"
)

OFFERS, CONN, PAUSED, CONNECTIONS = {}, {}, {}, {}
LOCK = threading.Lock()
STOP = threading.Event()
OPENER = urllib.request.build_opener()
CHANNEL = "напрямую"
TICKER_ON = False
LOCK_SOCK = None
_ORIG_SOCKET = socket.socket
# ══════════════════════════════════════════════════════════


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + " ".join(str(x) for x in a)
    if sys.stdout:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def single_instance() -> bool:
    global LOCK_SOCK
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        LOCK_SOCK = s
        return True
    except OSError:
        return False


# ─────────────────────── канал и HTTP ───────────────────────
def make_opener(proxy: str):
    if not proxy:
        socket.socket = _ORIG_SOCKET
        return urllib.request.build_opener()
    if proxy.startswith("socks"):
        if not _socks:
            return None
        hp = proxy.split("://", 1)[1]
        host, port = hp.rsplit(":", 1)
        _socks.set_default_proxy(_socks.SOCKS5, host, int(port))
        socket.socket = _socks.socksocket
        return urllib.request.build_opener()
    socket.socket = _ORIG_SOCKET
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def probe(proxy: str, timeout=5) -> bool:
    op = make_opener(proxy)
    if op is None:
        return False
    try:
        with op.open(API + "getMe", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)
    except Exception:
        return False


def pick_channel() -> bool:
    global OPENER, CHANNEL
    for p in PROXY_CANDIDATES:
        if probe(p):
            OPENER = make_opener(p) or urllib.request.build_opener()
            CHANNEL = p
            log("канал:", p)
            return True
    if probe(""):
        OPENER = make_opener("") or urllib.request.build_opener()
        CHANNEL = "напрямую"
        log("канал: напрямую")
        return True
    return False


def api(method: str, **params):
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API + method, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with OPENER.open(req, timeout=70) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            res = json.loads(raw)
        except Exception:
            res = {"ok": False, "description": raw or str(e)}
        res.setdefault("ok", False)
        return res
    except Exception as e:
        return {"ok": False, "description": f"network: {e}"}


# ─────────────────────────── база ───────────────────────────
@contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def db_init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS offers(
            chat_id INTEGER, message_id INTEGER, bc TEXT, link TEXT, title TEXT,
            price INTEGER, cur TEXT, deadline INTEGER, status TEXT, buyer TEXT,
            created INTEGER, hint_id INTEGER DEFAULT 0, buyer_id INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, message_id))""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(offers)")]
        if "hint_id" not in cols:
            c.execute("ALTER TABLE offers ADD COLUMN hint_id INTEGER DEFAULT 0")
        if "buyer_id" not in cols:
            c.execute("ALTER TABLE offers ADD COLUMN buyer_id INTEGER DEFAULT 0")
        c.execute("""CREATE TABLE IF NOT EXISTS log(
            ts INTEGER, chat_id INTEGER, event TEXT, payload TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS conn(
            id TEXT PRIMARY KEY, user TEXT, del_all INTEGER, ts INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, until INTEGER, added INTEGER)""")


def db_put(o):
    with db() as c:
        c.execute("""INSERT OR REPLACE INTO offers
            (chat_id,message_id,bc,link,title,price,cur,deadline,status,buyer,
             created,hint_id,buyer_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o["chat_id"], o["message_id"], o["bc"], o["link"], o["title"],
             o["price"], o["cur"], o["deadline"], o["status"], o.get("buyer", ""),
             o["created"], o.get("hint_id", 0), o.get("buyer_id", 0)))


def db_log(chat_id, event, payload=""):
    with db() as c:
        c.execute("INSERT INTO log(ts,chat_id,event,payload) VALUES(?,?,?,?)",
                  (int(time.time()), chat_id, event, payload))


def db_log_cleanup():
    edge = int(time.time()) - LOG_KEEP_DAYS * 86400
    with db() as c:
        c.execute("DELETE FROM log WHERE ts < ?", (edge,))


def db_load():
    with db() as c:
        rows = c.execute("""SELECT chat_id,message_id,bc,link,title,price,cur,
                            deadline,status,buyer,created,hint_id,buyer_id
                            FROM offers WHERE status NOT IN ('done','expired')""").fetchall()
    for (chat_id, msg_id, bc, link, title, price, cur, deadline,
         status, buyer, created, hint_id, buyer_id) in rows:
        OFFERS[(chat_id, msg_id)] = dict(
            chat_id=chat_id, message_id=msg_id, bc=bc, link=link, title=title,
            price=price, cur=cur, deadline=deadline, status=status,
            buyer=buyer, created=created, hint_id=hint_id or 0,
            buyer_id=buyer_id or 0)


def db_conn_save(cid, user, del_all):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO conn(id,user,del_all,ts) VALUES(?,?,?,?)",
                  (cid, user or "", int(bool(del_all)), int(time.time())))


def db_conn_del(cid):
    with db() as c:
        c.execute("DELETE FROM conn WHERE id=?", (cid,))


def db_conn_load():
    with db() as c:
        rows = c.execute("SELECT id,user,del_all,ts FROM conn").fetchall()
    for cid, user, del_all, ts in rows:
        CONNECTIONS[cid] = {"user": user,
                            "rights": {"can_delete_all_messages": bool(del_all)},
                            "ts": ts}


# ───────────────────────── доступ ─────────────────────────
def has_access(uid) -> tuple:
    """(есть_ли_доступ, до_какого_времени)"""
    if not uid:
        return False, 0
    if uid in ADMIN_IDS:
        return True, 0
    with db() as c:
        row = c.execute("SELECT until FROM users WHERE user_id=?", (uid,)).fetchone()
    if row and row[0] > int(time.time()):
        return True, row[0]
    return False, 0


def grant(uid, days):
    now = int(time.time())
    with db() as c:
        row = c.execute("SELECT until FROM users WHERE user_id=?", (uid,)).fetchone()
        base = row[0] if row and row[0] > now else now
        until = base + days * 86400
        c.execute("INSERT OR REPLACE INTO users(user_id,until,added) VALUES(?,?,?)",
                  (uid, until, now))
    return until


def revoke(uid):
    with db() as c:
        c.execute("DELETE FROM users WHERE user_id=?", (uid,))


def fmt_date(ts):
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))


def kb_buy():
    return {"inline_keyboard": [[{"text": f"Написать {SELLER}",
                                  "url": f"https://t.me/{SELLER.lstrip('@')}"}]]}


def send_buy(chat_id, bc=None):
    send(chat_id, BUY_TEXT, bc=bc, kb=kb_buy(), preview=False)


# ────────────────────────── утилиты ──────────────────────────
def esc(s):
    return html.escape(s or "")


def icon(o):
    if o["cur"] == "stars":
        return f'<tg-emoji emoji-id="{E_STARS}">\u2b50</tg-emoji>'
    return f'<tg-emoji emoji-id="{E_GRAM}">\U0001f48e</tg-emoji>'


def gift_title(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with OPENER.open(req, timeout=15) as r:
            page = r.read().decode("utf-8", "ignore")
        m = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        if m:
            return html.unescape(m.group(1))
    except Exception as e:
        log("og:title fail:", e)
    return url.rsplit("/", 1)[-1]


def card_text(o):
    ic = icon(o)
    link = esc(o["link"])
    if o["status"] == "pending":
        left = max(o["deadline"] - int(time.time()), 0)
        h, mn = divmod(left // 60, 60)
        return (f"<b>Пользователь предлагает Вам</b> {o['price']} {ic} "
                f"за подарок <b>{esc(o['title'])}</b>.\n\n"
                f"Действует ещё <b>{h} ч. {mn} мин.</b>\n\n{link}")
    if o["status"] == "accepted":
        left = max(o["deadline"] - int(time.time()), 0)
        return (f"⭐️ <b>Сделка подтверждена.</b>\n\n"
                f"Передайте <b>{esc(o['title'])}</b> покупателю, чтобы получить "
                f"{o['price']} {ic} в течение <b>{left} мин.</b>\n\n"
                f"<blockquote>Профиль покупателя → «Отправить подарок» → "
                f"выберите подарок</blockquote>\n\n{link}")
    if o["status"] == "overdue":
        return (f"❌ <b>Передайте подарок пользователю.</b>\n\n"
                f"<b>{esc(o['title'])}</b> — {o['price']} {ic}\n"
                f"Время на передачу истекло.\n\n"
                f"<blockquote>Профиль покупателя → «Отправить подарок» → "
                f"выберите подарок</blockquote>\n\n{link}")
    if o["status"] == "expired":
        return (f"⌛️ <b>Предложение истекло.</b>\n\n"
                f"{esc(o['title'])} — {o['price']} {ic}\n\n{link}")
    if o.get("cancelled"):
        return (f'<tg-emoji emoji-id="{E_DECLINE}">❌</tg-emoji> '
                f"<b>Сделка отменена.</b>\n\n"
                f"{esc(o['title'])} — {o['price']} {ic}\n\n{link}")
    return (f"✅ <b>Сделка закрыта.</b>\n\n"
            f"{esc(o['title'])} — {o['price']} {ic}\n\n{link}")


def kb_pending(custom=True):
    b = {"text": "Отклонить", "callback_data": "o:decline"}
    a = {"text": "Принять", "callback_data": "o:accept"}
    if custom:
        b["icon_custom_emoji_id"] = E_DECLINE
        a["icon_custom_emoji_id"] = E_OK
    return {"inline_keyboard": [[b, a]]}


def strip_icons(kb):
    if not kb:
        return kb
    return {"inline_keyboard": [
        [{k: v for k, v in b.items() if k != "icon_custom_emoji_id"} for b in row]
        for row in kb["inline_keyboard"]]}


def gift_link(buyer, buyer_id):
    if buyer:
        return f"tg://send_gift?to={buyer}"
    if buyer_id:
        return f"tg://user?id={buyer_id}"
    return "tg://send_gift"


def kb_accepted(buyer, buyer_id=0):
    return {"inline_keyboard": [
        [{"text": "Передать подарок", "url": gift_link(buyer, buyer_id),
          "icon_custom_emoji_id": E_OK}],
        [{"text": "Подтвердить передачу", "callback_data": "o:confirm",
          "icon_custom_emoji_id": E_OK}]]}


PREVIEW = {"prefer_large_media": True, "show_above_text": True}


def _plain_custom_emoji(text):
    """Заменяет Telegram premium custom emoji на обычные Unicode-эмодзи."""
    return re.sub(r'<tg-emoji\s+emoji-id="\d+">(.*?)</tg-emoji>', r'\1', text or "", flags=re.S)


def _custom_emoji_error(res):
    desc = (res or {}).get("description", "").upper()
    return any(mark in desc for mark in ("CUSTOM_EMOJI", "CUSTOM EMOJI", "ICON_CUSTOM_EMOJI"))


def send(chat_id, text, bc=None, kb=None, preview=True, reply_to=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if bc:
        p["business_connection_id"] = bc
    if preview:
        p["link_preview_options"] = PREVIEW
    if kb:
        p["reply_markup"] = kb
    if reply_to:
        p["reply_parameters"] = {"message_id": reply_to,
                                 "allow_sending_without_reply": True}
    res = api("sendMessage", **p)
    if not res.get("ok") and _custom_emoji_error(res):
        p["text"] = _plain_custom_emoji(text)
        if kb:
            p["reply_markup"] = strip_icons(kb)
        res = api("sendMessage", **p)
    return res


def edit(chat_id, msg_id, text, kb, bc):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text,
         "parse_mode": "HTML", "link_preview_options": PREVIEW}
    if bc:
        p["business_connection_id"] = bc
    if kb:
        p["reply_markup"] = kb
    res = api("editMessageText", **p)
    if not res.get("ok") and _custom_emoji_error(res):
        p["text"] = _plain_custom_emoji(text)
        if kb:
            p["reply_markup"] = strip_icons(kb)
        res = api("editMessageText", **p)
    return res


def delete_msg(chat_id, msg_id, bc):
    if not msg_id:
        return
    if bc:
        api("deleteBusinessMessages", business_connection_id=bc,
            message_ids=[msg_id])
    else:
        api("deleteMessage", chat_id=chat_id, message_id=msg_id)


def drop(key):
    with LOCK:
        OFFERS.pop(key, None)


def safe_edit(chat_id, msg_id, o, kb, force=False):
    now = time.time()
    until = PAUSED.get(chat_id, 0)
    if until <= now:
        PAUSED.pop(chat_id, None)
    elif not force:
        return
    res = edit(chat_id, msg_id, card_text(o), kb, o["bc"])
    if res.get("ok"):
        return
    desc = res.get("description", "")
    if res.get("error_code") == 429:
        wait = (res.get("parameters") or {}).get("retry_after", 3)
        log(f"retry_after {wait} c")
        time.sleep(wait)
        edit(chat_id, msg_id, card_text(o), kb, o["bc"])
    elif "PEER_FLOOD" in desc:
        PAUSED[chat_id] = time.time() + 3600
        log(f"PEER_FLOOD, чат {chat_id} на паузе 1ч")
    elif "not modified" not in desc:
        log("edit fail:", desc)


# ─────────────────── обработка апдейтов ───────────────────
def remember_conn(bc, usr):
    if usr.get("username"):
        CONN[bc] = {"username": usr["username"], "id": usr.get("id", 0)}


def on_connection(u):
    c = u.get("business_connection", {})
    cid = c.get("id")
    if not cid:
        return
    if not c.get("is_enabled"):
        CONNECTIONS.pop(cid, None)
        CONN.pop(cid, None)
        db_conn_del(cid)
        log(f"connection {cid} отключено")
        return
    usr = c.get("user") or {}
    remember_conn(cid, usr)
    rights = c.get("rights") or {}
    CONNECTIONS[cid] = {"user": usr.get("username"), "rights": rights}
    db_conn_save(cid, usr.get("username"), rights.get("can_delete_all_messages"))
    ok, _ = has_access(usr.get("id", 0))
    log(f"connection {cid} user={usr.get('username')} "
        f"reply={rights.get('can_reply')} "
        f"del_all={rights.get('can_delete_all_messages')} access={ok}")
    if not rights.get("can_delete_all_messages"):
        log("ВНИМАНИЕ: нет права can_delete_all_messages — команда .buy не удалится")
    if not ok:
        log(f"нет доступа у @{usr.get('username')}")


def resolve_buyer(bc):
    if bc in CONN:
        return CONN[bc].get("username", ""), CONN[bc].get("id", 0)
    r = api("getBusinessConnection", business_connection_id=bc)
    usr = ((r.get("result") or {}).get("user") or {})
    remember_conn(bc, usr)
    return usr.get("username", ""), usr.get("id", 0)


def parse_buy(text):
    parts = text.split()
    if len(parts) < 3:
        return None
    link = parts[1]
    if not link.lower().startswith(("http://", "https://")):
        return None
    rest = parts[2:]
    cur = "stars"
    for i, tok in enumerate(list(rest)):
        if tok.lower() in ("stars", "gram"):
            cur = tok.lower()
            del rest[i]
            break
    buyer, buyer_id = "", 0
    if rest:
        tail = rest[-1]
        if tail.startswith("@") and len(tail) > 1:
            buyer = rest.pop()[1:]
        elif tail.lower().startswith("id:") and tail[3:].isdigit():
            buyer_id = int(rest.pop()[3:])
    raw = "".join(rest).replace(" ", "").replace("_", "")
    if not raw.isdigit():
        return None
    price = int(raw)
    if price <= 0:
        return None
    return link, price, cur, buyer, buyer_id


def make_offer(m):
    text = m.get("text") or ""
    bc = m.get("business_connection_id")
    chat_id = m["chat"]["id"]
    from_user = m.get("from") or {}

    parsed = parse_buy(text)
    if not parsed:
        return send(chat_id,
                    "Формат: <code>.buy ССЫЛКА ЦЕНА [stars|gram] [@покупатель]</code>",
                    bc=bc, preview=False)
    link, price, cur, buyer, buyer_id = parsed

    if bc:
        if not buyer and not buyer_id:
            buyer, buyer_id = resolve_buyer(bc)
        gate_id = buyer_id
    else:
        gate_id = from_user.get("id", 0)
        if not buyer and not buyer_id:
            buyer = from_user.get("username", "")
            buyer_id = from_user.get("id", 0)

    ok, _ = has_access(gate_id)
    if not ok:
        log(f"отказ в доступе: {gate_id}")
        send_buy(chat_id, bc=bc)
        delete_msg(chat_id, m["message_id"], bc)
        return

    o = dict(chat_id=chat_id, message_id=0, bc=bc, link=link,
             title=gift_title(link), price=price, cur=cur,
             deadline=int(time.time()) + DEFAULT_TTL_H * 3600,
             status="pending", buyer=buyer, buyer_id=buyer_id,
             created=int(time.time()), hint_id=0)

    res = send(chat_id, card_text(o), bc=bc, kb=kb_pending())
    if not res.get("ok") and "CUSTOM_EMOJI" in res.get("description", "").upper():
        res = send(chat_id, card_text(o), bc=bc, kb=kb_pending(custom=False))
    if not res.get("ok"):
        log("send offer fail:", res.get("description"))
        return

    o["message_id"] = res["result"]["message_id"]

    if HINT_ENABLED:
        hint = send(chat_id, HINT_TEXT, bc=bc, preview=False,
                    reply_to=o["message_id"])
        if hint.get("ok"):
            o["hint_id"] = hint["result"]["message_id"]

    with LOCK:
        OFFERS[(chat_id, o["message_id"])] = o
    db_put(o)
    db_log(chat_id, "offer_created", f"{o['title']} {price} {cur}")
    log(f"оффер создан: {o['title']} {price} {cur} покупатель=@{buyer or buyer_id}")

    delete_msg(chat_id, m["message_id"], bc)
    for admin_id in ADMIN_IDS:
        send(admin_id, f"Новый оффер: {esc(o['title'])} — {price} {cur}")


def on_callback(cb):
    data = cb.get("data", "")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    user_id = (cb.get("from") or {}).get("id")

    with LOCK:
        o = OFFERS.get((chat_id, msg_id))
    if not o:
        return api("answerCallbackQuery", callback_query_id=cb["id"],
                   text="Карточка устарела", show_alert=True)

    if data == "o:accept":
        if o["status"] != "pending":
            return api("answerCallbackQuery", callback_query_id=cb["id"],
                       text="Уже обработано")
        o["status"] = "accepted"
        o["deadline"] = int(time.time()) + TRANSFER_TTL_MIN * 60
        if not o.get("buyer") and not o.get("buyer_id") and o["bc"]:
            o["buyer"], o["buyer_id"] = resolve_buyer(o["bc"])
        delete_msg(chat_id, o.get("hint_id"), o["bc"])
        o["hint_id"] = 0
        db_put(o)
        db_log(chat_id, "accepted", f"by {user_id}")
        safe_edit(chat_id, msg_id, o,
                  kb_accepted(o.get("buyer", ""), o.get("buyer_id", 0)),
                  force=True)
        api("answerCallbackQuery", callback_query_id=cb["id"], text="Сделка подтверждена")
        for admin_id in ADMIN_IDS:
            send(admin_id, f"Принято: {esc(o['title'])} — {o['price']} {o['cur']}")

    elif data == "o:decline":
        o["status"] = "done"
        o["cancelled"] = True
        delete_msg(chat_id, o.get("hint_id"), o["bc"])
        o["hint_id"] = 0
        db_put(o)
        db_log(chat_id, "declined", f"by {user_id}")
        safe_edit(chat_id, msg_id, o, None, force=True)
        api("answerCallbackQuery", callback_query_id=cb["id"], text="Сделка отменена")
        drop((chat_id, msg_id))

    elif data == "o:confirm":
        o["status"] = "done"
        delete_msg(chat_id, o.get("hint_id"), o["bc"])
        o["hint_id"] = 0
        db_put(o)
        db_log(chat_id, "transfer_confirmed", f"by {user_id}")
        safe_edit(chat_id, msg_id, o, None, force=True)
        api("answerCallbackQuery", callback_query_id=cb["id"], text="Готово")
        drop((chat_id, msg_id))


def on_business_message(m):
    text = (m.get("text") or "").strip()
    bc = m.get("business_connection_id")
    chat_id = m["chat"]["id"]
    msg_id = m["message_id"]

    if text.startswith(".buy"):
        return make_offer(m)

    if text == ".status":
        act = [o for o in OFFERS.values()
               if o["chat_id"] == chat_id and o["status"] not in ("done", "expired")]
        delete_msg(chat_id, msg_id, bc)
        if not act:
            return send(chat_id, "Активных офферов нет.", bc=bc, preview=False)
        return send(chat_id, "\n".join(
            f"• {esc(o['title'])} — {o['price']} {o['cur']} [{o['status']}]"
            for o in act), bc=bc, preview=False)

    if text == ".cancel":
        n = 0
        for o in list(OFFERS.values()):
            if o["chat_id"] == chat_id and o["status"] in ("pending", "accepted", "overdue"):
                o["status"] = "done"
                delete_msg(chat_id, o.get("hint_id"), o["bc"])
                o["hint_id"] = 0
                db_put(o)
                safe_edit(chat_id, o["message_id"], o, None, force=True)
                drop((o["chat_id"], o["message_id"]))
                n += 1
        delete_msg(chat_id, msg_id, bc)
        return send(chat_id, f"Отменено: {n}", bc=bc, preview=False)


def admin_panel(chat_id, uid):
    ok, until = has_access(uid)
    with db() as c:
        subs = c.execute("SELECT COUNT(*) FROM users WHERE until>?",
                         (int(time.time()),)).fetchone()[0]
        total = c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
    send(chat_id,
         f"<b>Панель владельца</b>\n\n"
         f"Доступ: {'бессрочно' if uid in ADMIN_IDS else fmt_date(until)}\n"
         f"Активных подписок: {subs}\n"
         f"Всего офферов: {total}\n\n"
         f"<b>Команды</b>\n"
         f"<code>/give ID ДНЕЙ</code> — выдать доступ\n"
         f"<code>/take ID</code> — забрать доступ\n"
         f"<code>/subs</code> — список подписок\n"
         f"<code>/admin</code> — журнал")


def on_private(m):
    text = (m.get("text") or "").strip()
    uid = (m.get("from") or {}).get("id")
    chat_id = m["chat"]["id"]
    is_admin = (uid in ADMIN_IDS)

    if text.startswith(".buy"):
        return make_offer(m)
    if text in (".status", ".cancel"):
        return on_business_message(m)

    # ── выдача доступа ──
    if text.startswith("/give"):
        if not is_admin:
            return send(chat_id, "Нет доступа")
        args = text.split()
        if len(args) < 3:
            return send(chat_id, "Формат: <code>/give ID ДНЕЙ</code>")
        try:
            target, days = int(args[1]), int(args[2])
        except ValueError:
            return send(chat_id, "ID и дни должны быть числами")
        until = grant(target, days)
        log(f"доступ выдан {target} на {days} дн.")
        return send(chat_id, f"✅ Доступ для <code>{target}</code> до {fmt_date(until)}")

    if text.startswith("/take"):
        if not is_admin:
            return send(chat_id, "Нет доступа")
        args = text.split()
        if len(args) < 2:
            return send(chat_id, "Формат: <code>/take ID</code>")
        try:
            target = int(args[1])
        except ValueError:
            return send(chat_id, "ID должен быть числом")
        revoke(target)
        log(f"доступ забран {target}")
        return send(chat_id, f"🚫 Доступ для <code>{target}</code> забран")

    if text == "/subs":
        if not is_admin:
            return send(chat_id, "Нет доступа")
        now = int(time.time())
        with db() as c:
            rows = c.execute("SELECT user_id,until FROM users WHERE until>? "
                             "ORDER BY until DESC", (now,)).fetchall()
        if not rows:
            return send(chat_id, "Активных подписок нет.")
        return send(chat_id, "\n".join(
            f"<code>{u}</code> — до {fmt_date(t)}" for u, t in rows))

    # ── старт ──
    if text == "/start":
        ok, until = has_access(uid)
        if not ok:
            return send_buy(chat_id)
        if is_admin:
            return admin_panel(chat_id, uid)
        send(chat_id, HELP_TEXT)
        return send(chat_id, f"Подписка активна до {fmt_date(until)}")

    if text == "/admin":
        if not is_admin:
            return send(chat_id, "Нет доступа")
        with db() as c:
            total = c.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
            done = c.execute("SELECT COUNT(*) FROM offers WHERE status='done'").fetchone()[0]
            rows = c.execute("SELECT ts,event,payload FROM log "
                             "ORDER BY ts DESC LIMIT 10").fetchall()
        live = len([o for o in OFFERS.values() if o["status"] != "done"])
        tail = "\n".join(f"{time.strftime('%H:%M', time.localtime(r[0]))} {r[1]} {r[2]}"
                         for r in rows) or "—"
        send(chat_id, f"<b>Журнал</b>\nВсего: {total}\nЗакрыто: {done}\n"
                      f"Активных: {live}\n\n<b>События:</b>\n{tail}")


# ─────────────────────── фоновый таймер ───────────────────────
def ticker():
    cycles = 0
    while not STOP.is_set():
        now = int(time.time())
        with LOCK:
            items = list(OFFERS.items())
        for (chat_id, msg_id), o in items:
            st = o["status"]
            if st == "done":
                drop((chat_id, msg_id))
                continue

            if st == "pending" and o["deadline"] <= now:
                o["status"] = "expired"
                delete_msg(chat_id, o.get("hint_id"), o["bc"])
                o["hint_id"] = 0
                db_put(o)
                db_log(chat_id, "expired")
                safe_edit(chat_id, msg_id, o, None)
                drop((chat_id, msg_id))
                continue

            if st == "accepted" and o["deadline"] <= now:
                o["status"] = "overdue"
                db_put(o)
                db_log(chat_id, "overdue")
                send(chat_id, REMIND_TEXT, bc=o["bc"], preview=False, reply_to=msg_id)
                safe_edit(chat_id, msg_id, o,
                          kb_accepted(o.get("buyer", ""), o.get("buyer_id", 0)))
                log(f"напоминание о передаче: {o['title']}")
                continue

            if st == "pending":
                safe_edit(chat_id, msg_id, o, kb_pending())
            elif st == "overdue":
                safe_edit(chat_id, msg_id, o,
                          kb_accepted(o.get("buyer", ""), o.get("buyer_id", 0)))

        cycles += 1
        if cycles % 720 == 0:
            try:
                db_log_cleanup()
            except Exception as e:
                log("cleanup fail:", e)
        STOP.wait(TICK)


# ────────────────────────── запуск ──────────────────────────
def poll():
    offset = 0
    fails = 0
    allowed = ["business_connection", "business_message", "callback_query", "message"]
    while not STOP.is_set():
        res = api("getUpdates", offset=offset, timeout=25, allowed_updates=allowed)
        if not res.get("ok"):
            fails += 1
            desc = res.get("description", "")
            wait = (res.get("parameters") or {}).get("retry_after", 2)
            log("getUpdates:", desc)
            if fails >= 5 and desc.startswith("network:"):
                log("связь потеряна, ищу канал заново")
                if not pick_channel():
                    STOP.wait(15)
                fails = 0
            STOP.wait(wait)
            continue
        fails = 0
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            try:
                if "business_connection" in upd:
                    on_connection(upd)
                elif "business_message" in upd:
                    on_business_message(upd["business_message"])
                elif "callback_query" in upd:
                    on_callback(upd["callback_query"])
                elif "message" in upd:
                    on_private(upd["message"])
            except Exception as e:
                log("handle error:", type(e).__name__, e)


def main():
    global TICKER_ON
    db_init()
    db_load()
    db_conn_load()
    log("=== старт, незакрытых офферов:", len(OFFERS))

    while not STOP.is_set():
        try:
            if not pick_channel():
                log("нет канала до Telegram, повтор через 15 с")
                STOP.wait(15)
                continue

            me = api("getMe")
            if not me.get("ok"):
                log("getMe fail:", me.get("description"))
                STOP.wait(15)
                continue

            log("@" + me["result"]["username"] + " started, канал:", CHANNEL)
            if not TICKER_ON:
                threading.Thread(target=ticker, daemon=True).start()
                TICKER_ON = True

            poll()
        except Exception as e:
            log("супервизор поймал:", type(e).__name__, e)
            STOP.wait(10)


def shutdown(signum, frame):
    log(f"сигнал {signum}, останавливаюсь")
    STOP.set()


# --- ЗАГЛУШКА ДЛЯ RENDER (чтобы не писал Application exited) ---
import http.server
import socketserver

def keep_alive():
    port = int(os.getenv("PORT", "8080"))
    handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("", port), handler)
    httpd.serve_forever()

threading.Thread(target=keep_alive, daemon=True).start()
# -------------------------------------------------------------

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if not single_instance():
        log("уже запущен другой экземпляр — выхожу")
        sys.exit(0)
    try:
        main()
    except KeyboardInterrupt:
        log("остановлено вручную")
