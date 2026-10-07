"""HS Bingo: Telegram bot + game engine + web server + admin API."""
import asyncio, json, os, random, hmac, hashlib, sqlite3, re, subprocess, base64, sys
from urllib.parse import parse_qsl
from aiohttp import web, WSMsgType
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, BotCommand)

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)

def load_config():  # reads config.env (KEY=value lines)
    p = os.path.join(ROOT, "config.env")
    for line in open(p, encoding="utf-8") if os.path.exists(p) else []:
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())
load_config()
TOKEN = os.environ.get("BOT_TOKEN", "")
if ":" not in TOKEN: sys.exit("ERROR: open config.env and put your BOT_TOKEN (from @BotFather).")
BASE = os.environ.get("BASE_URL", "").rstrip("/")
ADMIN = int(os.environ.get("ADMIN_ID") or 0); ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
ACCT = {"Telebirr": os.environ.get("TELEBIRR_NO", "0911000000"), "CBEBirr": os.environ.get("CBE_NO", "0911000000")}
STAKE, BONUS, LOBBY, CALL_EVERY, CUT = 20, 20, 40, 4, 0.2
CARDS = json.load(open(os.path.join(HERE, "cards.json")))  # 432 different cards
NCARDS = len(CARDS)

db = sqlite3.connect(os.path.join(ROOT, "bingo.db"), isolation_level=None)
db.execute("create table if not exists u(id integer primary key, phone text, name text, bal real)")
db.execute("create table if not exists log(ts integer, kind text, uid integer, amt real)")
def logx(k, uid, amt): db.execute("insert into log values(strftime('%s','now'),?,?,?)", (k, uid, amt))
def user(i): return db.execute("select phone,name,bal from u where id=?", (i,)).fetchone()
def bal(i): u = user(i); return u[2] if u else 0
def add(i, x): db.execute("update u set bal=bal+? where id=?", (x, i))

# ---------- game engine ----------
def card(n): return CARDS[n - 1]
def is_bingo(n, called):
    m = set(called) | {0}; g = card(n)
    lines = g + [list(c) for c in zip(*g)] + [[g[i][i] for i in range(5)], [g[i][4 - i] for i in range(5)]]
    return any(all(x in m for x in L) for L in lines)

class G: phase = "lobby"; left = 0; of_user = {}; by_card = {}; called = []; prize = 0
conns = {}
bot = Bot(TOKEN); dp = Dispatcher()

async def send(ws, d):
    try: await ws.send_str(json.dumps(d))
    except Exception: pass

async def push_lobby():
    for ws, uid in list(conns.items()):
        mine = G.of_user.get(uid)
        await send(ws, {"t": "lobby", "left": G.left, "taken": list(G.by_card), "mine": mine, "bal": bal(uid),
                        "card": card(mine) if mine else None, "stake": STAKE, "total": NCARDS})

async def bc_call(n):
    for ws, uid in list(conns.items()):
        mine = G.of_user.get(uid)
        await send(ws, {"t": "call", "n": n, "called": G.called, "prize": G.prize, "mine": mine,
                        "card": card(mine) if mine else None})

def pick(uid, n):
    if G.phase != "lobby" or not 1 <= n <= NCARDS or n in G.by_card: return
    old = G.of_user.get(uid)
    if old is None:
        if bal(uid) < STAKE: return
        add(uid, -STAKE)
    else: del G.by_card[old]
    G.by_card[n] = uid; G.of_user[uid] = n

async def game_loop():
    while True:
        G.phase = "lobby"; G.of_user = {}; G.by_card = {}; G.called = []
        for s in range(LOBBY, 0, -1):
            G.left = s; await push_lobby(); await asyncio.sleep(1)
        if len(G.of_user) < 2:
            for u in G.of_user: add(u, STAKE)
            continue
        G.phase = "play"; pool = len(G.of_user) * STAKE; G.prize = round(pool * (1 - CUT), 2)
        nums = list(range(1, 76)); random.shuffle(nums); winners = []
        for n in nums:
            G.called.append(n); await bc_call(n)
            winners = [u for u, c in G.of_user.items() if is_bingo(c, G.called)]
            if winners: break
            await asyncio.sleep(CALL_EVERY)
        share = round(G.prize / len(winners), 2)
        for u in winners: add(u, share); logx("win", u, share)
        logx("house", 0, round(pool - G.prize, 2)); logx("round", 0, len(G.of_user))
        names = [user(u)[1] for u in winners]
        for ws in list(conns):
            await send(ws, {"t": "win", "names": names, "prize": share, "cards": [G.of_user[u] for u in winners],
                            "grid": card(G.of_user[winners[0]]), "ids": [str(u)[-4:] for u in winners],
                            "called": G.called, "next": 10})
        await asyncio.sleep(10)

# ---------- web ----------
def check(init):
    d = dict(parse_qsl(init)); h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        return json.loads(d["user"])["id"]

async def ws_handler(req):
    ws = web.WebSocketResponse(heartbeat=20); await ws.prepare(req); uid = None
    async for m in ws:
        if m.type != WSMsgType.TEXT: continue
        d = json.loads(m.data)
        if "auth" in d:
            uid = check(d["auth"])
            if not uid or not user(uid): await ws.close(); break
            conns[ws] = uid
            await (push_lobby() if G.phase == "lobby" else bc_call(G.called[-1]))
        elif uid and "pick" in d:
            pick(uid, int(d["pick"])); await push_lobby()
    conns.pop(ws, None); return ws

def need_auth(req):
    try: ok = base64.b64decode(req.headers["Authorization"].split()[1]).decode().split(":", 1)[1] == ADMIN_PASS
    except Exception: ok = False
    if not ok: raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="admin"'})

async def admin_files(req):  # serves the built React admin
    need_auth(req); rel = req.match_info.get("p", "") or "index.html"
    f = os.path.normpath(os.path.join(ROOT, "admin", rel))
    if not f.startswith(os.path.join(ROOT, "admin")) or not os.path.isfile(f): f = os.path.join(ROOT, "admin", "index.html")
    return web.FileResponse(f)

async def api_data(req):
    need_auth(req)
    users = [dict(id=i, phone=p, name=n, bal=b) for i, p, n, b in db.execute("select id,phone,name,bal from u order by rowid desc")]
    st = {k: db.execute("select coalesce(sum(amt),0) from log where kind=?", (k,)).fetchone()[0]
          for k in ("deposit", "withdraw", "house", "round")}
    logs = [dict(ts=t, kind=k, uid=u, amt=a) for t, k, u, a in db.execute("select * from log order by ts desc limit 30")]
    return web.json_response({"users": users, "stats": st, "logs": logs, "online": len(conns), "phase": G.phase})

async def api_adjust(req):
    need_auth(req); d = await req.json(); uid, amt = int(d["id"]), float(d["amt"])
    add(uid, amt); logx("adjust", uid, amt)
    try: await bot.send_message(uid, f"Balance updated by admin: {amt:+} birr. Balance: {bal(uid)}")
    except Exception: pass
    return web.json_response({"ok": True})

async def game_page(_): return web.FileResponse(os.path.join(ROOT, "webapp", "index.html"))

# ---------- bot ----------
def menu():
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text="🎮 Play"), KeyboardButton(text="💰 Balance")],
        [KeyboardButton(text="➕ Deposit"), KeyboardButton(text="➖ Withdraw")],
        [KeyboardButton(text="🤝 Agent"), KeyboardButton(text="🆘 Support")],
        [KeyboardButton(text="✏️ Change username")]])
pend = {}

@dp.message(CommandStart())
async def start(m: Message):
    if user(m.from_user.id): return await m.answer("Welcome back!", reply_markup=menu())
    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True,
                             keyboard=[[KeyboardButton(text="📱 Share phone to register", request_contact=True)]])
    await m.answer("Welcome to HS Bingo! Please register with your phone number.", reply_markup=kb)

@dp.message(F.contact)
async def contact(m: Message):
    if m.contact.user_id != m.from_user.id: return await m.answer("Please share your own number.")
    db.execute("insert or ignore into u values(?,?,?,?)", (m.from_user.id, m.contact.phone_number, m.from_user.first_name, BONUS))
    await m.answer(f"✅ Registered successfully! You got {BONUS} birr to play. Stake per game: {STAKE} birr.", reply_markup=menu())

def pay_menu(kind):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=n, callback_data=f"{kind}:{n}") for n in ACCT]])

@dp.callback_query(F.data.startswith(("dep:", "wd:")))
async def choose(c: CallbackQuery):
    kind, method = c.data.split(":"); pend[c.from_user.id] = (kind, method)
    if kind == "dep": await c.message.answer(f"Send money to {method}: {ACCT[method]}\nThen reply: <amount> <transaction ref>\nExample: 100 DKX123")
    else: await c.message.answer(f"Reply: <amount> <your {method} number>\nExample: 100 0912345678")
    await c.answer()

@dp.callback_query(F.data.startswith(("ok:", "no:")))
async def admin_cb(c: CallbackQuery):
    if c.from_user.id != ADMIN: return await c.answer("Admins only")
    act, kind, uid, amt = c.data.split(":"); uid, amt = int(uid), float(amt)
    if act == "ok" and kind == "dep": add(uid, amt); logx("deposit", uid, amt)
    if act == "ok" and kind == "wd": logx("withdraw", uid, amt)
    if act == "no" and kind == "wd": add(uid, amt)
    await bot.send_message(uid, f"{'✅ Approved' if act == 'ok' else '❌ Rejected'}: {kind} {amt} birr. Balance: {bal(uid)}")
    await c.message.edit_text(c.message.text + f"\n→ {act.upper()}")

@dp.message(F.text)
async def text(m: Message):
    uid, t = m.from_user.id, m.text
    if not user(uid): return await start(m)
    if t.startswith("🎮") or t == "/play":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Play Bingo", web_app=WebAppInfo(url=BASE))]])
        return await m.answer("Tap to play 👇", reply_markup=kb)
    if t.startswith("💰") or t == "/balance": return await m.answer(f"Balance: {bal(uid)} birr")
    if t.startswith("➕") or t == "/deposit": return await m.answer("Choose payment method:", reply_markup=pay_menu("dep"))
    if t.startswith("➖") or t == "/withdraw":
        if bal(uid) < 1: return await m.answer(f"❌ Insufficient balance. Your balance is {bal(uid)} birr")
        return await m.answer("Choose withdrawal method:", reply_markup=pay_menu("wd"))
    if t.startswith("🤝") or t == "/agent": return await m.answer("Contact an agent: @jobingosupport1")
    if t.startswith("🆘") or t == "/support": return await m.answer("Support: @jobingosupport1")
    if t.startswith("✏️") or t == "/change_username":
        pend[uid] = ("name", ""); return await m.answer("Send your new username:")
    p = pend.pop(uid, None)
    if not p: return
    if p[0] == "name":
        db.execute("update u set name=? where id=?", (t[:20], uid)); return await m.answer("✅ Username updated.", reply_markup=menu())
    try: amt = float(t.split()[0]); assert amt > 0
    except Exception: return await m.answer("Invalid format. Start again from the menu.")
    if p[0] == "wd":
        if amt > bal(uid): return await m.answer("❌ Insufficient balance.")
        add(uid, -amt)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"ok:{p[0]}:{uid}:{amt}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{p[0]}:{uid}:{amt}")]])
    await bot.send_message(ADMIN, f"{p[0].upper()} request via {p[1]}\nUser {uid} ({user(uid)[0]}): {t}", reply_markup=kb)
    await m.answer("⏳ Request sent. You'll be notified when processed.")

async def open_tunnel():  # free public https address, automatic
    global BASE
    exe = os.path.join(ROOT, "cloudflared.exe")
    if BASE or not os.path.exists(exe): return
    p = subprocess.Popen([exe, "tunnel", "--url", "http://localhost:8080"], stderr=subprocess.PIPE, text=True)
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, p.stderr.readline)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line or "")
        if m: BASE = m.group(0); return
        if not line: sys.exit("Tunnel failed. Set BASE_URL in config.env.")

async def main():
    app = web.Application()
    app.add_routes([web.get("/", game_page), web.get("/ws", ws_handler), web.get("/api/data", api_data),
                    web.post("/api/adjust", api_adjust), web.get("/admin", admin_files), web.get("/admin/{p:.*}", admin_files)])
    r = web.AppRunner(app); await r.setup(); await web.TCPSite(r, "0.0.0.0", 8080).start()
    await open_tunnel()
    if not BASE: sys.exit("ERROR: no public address. Put cloudflared.exe next to start.bat or set BASE_URL in config.env.")
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
        ("play", "Play"), ("balance", "Balance"), ("deposit", "Deposit"), ("withdraw", "Withdraw"),
        ("agent", "Agent"), ("support", "Support"), ("change_username", "Change name")]])
    print("\n=== RUNNING ===\nGame address :", BASE, "\nAdmin website:", BASE + "/admin  (password = ADMIN_PASS)\n")
    asyncio.create_task(game_loop()); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())