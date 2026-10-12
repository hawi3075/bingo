"""HS Bingo: Telegram bot + game engine + web server + admin API (ledger database)."""
import asyncio, json, os, random, hmac, hashlib, sqlite3, re, subprocess, base64, sys, traceback, glob
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
# ALL MONEY IS STORED IN SANTIM (100 santim = 1 birr) AS WHOLE NUMBERS
STAKE, BONUS, CUT = 2000, 2000, 0.2
MIN_DEP, MIN_WD = 1000, 2000
LOBBY = 30        # seconds to pick a card
WIN_SHOW = 7      # seconds the winner card stays on screen
FAST_N, CALL_FAST, CALL_SLOW = 10, 1.5, 3
BOTS = int(os.environ.get("TEST_BOTS") or 0)  # test mode: fake players (0 = off)
CARDS = json.load(open(os.path.join(HERE, "cards.json")))  # 432 different cards
NCARDS = len(CARDS)

# ---------- database (ledger) ----------
db = sqlite3.connect(os.path.join(ROOT, "bingo.db"), isolation_level=None)
db.execute("pragma journal_mode=wal"); db.execute("pragma synchronous=full"); db.execute("pragma foreign_keys=on")
db.executescript("""
create table if not exists users(id integer primary key, phone text, name text, balance integer not null default 0 check(balance>=0), created integer);
create table if not exists ledger(id integer primary key autoincrement, ts integer, uid integer, kind text, amount integer,
  balance_after integer, ref text unique, note text);
create index if not exists ledger_uid on ledger(uid);
create table if not exists rounds(id integer primary key autoincrement, ts integer, status text, test integer default 0,
  players integer, prize integer, house integer, winner integer, card integer, called text);
create table if not exists round_players(round_id integer, uid integer, card integer, primary key(round_id, uid));
create table if not exists requests(id integer primary key autoincrement, ts integer, uid integer, kind text, amount integer,
  ref text, status text default 'pending');
""")

def move(uid, kind, amt, ref=None, note=""):
    """One atomic money movement (+credit / -debit). Returns the new balance, or None if refused
    (not enough money, or this ref was already used)."""
    try:
        db.execute("begin immediate")
        row = db.execute("select balance from users where id=?", (uid,)).fetchone()
        if not row or row[0] + amt < 0: db.execute("rollback"); return None
        nb = row[0] + amt
        db.execute("insert into ledger(ts,uid,kind,amount,balance_after,ref,note) values(strftime('%s','now'),?,?,?,?,?,?)",
                   (uid, kind, amt, nb, ref, note))
        db.execute("update users set balance=? where id=?", (nb, uid))
        db.execute("commit"); return nb
    except sqlite3.IntegrityError:
        db.execute("rollback"); return None
    except Exception:
        try: db.execute("rollback")
        except Exception: pass
        raise

def ref_used(ref): return db.execute("select 1 from ledger where ref=?", (ref,)).fetchone() is not None
def user(i): return db.execute("select phone,name,balance from users where id=?", (i,)).fetchone()
def bal(i): u = user(i); return u[2] if u else 0
def B(x): return x // 100 if x % 100 == 0 else round(x / 100, 2)  # santim -> birr for display
def name_of(u):
    if u < 0: return "Bot"
    r = user(u); return r[1] if r else "Player"

def migrate_old():  # one time: copy balances from the old table `u` (birr, real numbers)
    has_old = db.execute("select 1 from sqlite_master where name='u'").fetchone()
    if has_old and not db.execute("select 1 from users limit 1").fetchone():
        for uid, phone, name, b in db.execute("select id,phone,name,bal from u").fetchall():
            db.execute("insert into users(id,phone,name,balance,created) values(?,?,?,0,strftime('%s','now'))", (uid, phone, name))
            move(uid, "migrate", int(round((b or 0) * 100)), ref=f"migrate:{uid}", note="from old database")
        print("Old users copied into the new ledger database.")

def recover_rounds():  # after a crash: refund stakes of rounds that never finished
    for rid, in db.execute("select id from rounds where status in ('lobby','playing')").fetchall():
        if ref_used(f"win:{rid}"):
            db.execute("update rounds set status='done' where id=?", (rid,)); continue
        for uid, in db.execute("select uid from round_players where round_id=?", (rid,)).fetchall():
            move(uid, "refund", STAKE, ref=f"refund:{rid}:{uid}", note="round interrupted")
        db.execute("update rounds set status='cancelled' where id=?", (rid,))

migrate_old(); recover_rounds()

async def backup_loop():  # copy of the database every 6 hours, keeps the newest 7
    os.makedirs(os.path.join(ROOT, "backups"), exist_ok=True)
    while True:
        try:
            dst = sqlite3.connect(os.path.join(ROOT, "backups", f"bingo-{int(asyncio.get_running_loop().time())}-{random.randint(1000,9999)}.db"))
            db.backup(dst); dst.close()
            files = sorted(glob.glob(os.path.join(ROOT, "backups", "bingo-*.db")), key=os.path.getmtime)
            for f in files[:-7]: os.remove(f)
        except Exception as e: print("Backup failed:", e)
        await asyncio.sleep(6 * 3600)

# ---------- game engine ----------
def card(n): return CARDS[n - 1]
def is_bingo(n, called):
    m = set(called) | {0}; g = card(n)
    lines = g + [list(c) for c in zip(*g)] + [[g[i][i] for i in range(5)], [g[i][4 - i] for i in range(5)]]
    return any(all(x in m for x in L) for L in lines)

class G: phase = "lobby"; rid = 0; left = 0; of_user = {}; by_card = {}; called = []; prize = 0
conns = {}
bot = Bot(TOKEN); dp = Dispatcher()

async def send(ws, d):
    try: await ws.send_str(json.dumps(d))
    except Exception: pass

async def push_lobby():
    for ws, uid in list(conns.items()):
        mine = G.of_user.get(uid)
        await send(ws, {"t": "lobby", "left": G.left, "taken": list(G.by_card), "mine": mine, "bal": B(bal(uid)),
                        "card": card(mine) if mine else None, "stake": B(STAKE), "total": NCARDS})

async def bc_call(n):
    for ws, uid in list(conns.items()):
        mine = G.of_user.get(uid)
        await send(ws, {"t": "call", "n": n, "called": G.called, "prize": B(G.prize), "mine": mine,
                        "players": len(G.of_user), "card": card(mine) if mine else None})

def pick(uid, n):
    if G.phase != "lobby" or not 1 <= n <= NCARDS or n in G.by_card: return
    old = G.of_user.get(uid)
    if old is None:
        if move(uid, "stake", -STAKE, ref=f"stake:{G.rid}:{uid}") is None: return
    else: del G.by_card[old]
    G.by_card[n] = uid; G.of_user[uid] = n
    db.execute("insert into round_players values(?,?,?) on conflict(round_id,uid) do update set card=excluded.card", (G.rid, uid, n))

async def run_game():  # one endless series of rounds
    while True:
        G.phase = "lobby"; G.of_user = {}; G.by_card = {}; G.called = []
        G.rid = db.execute("insert into rounds(ts,status,test) values(strftime('%s','now'),'lobby',?)", (1 if BOTS else 0,)).lastrowid
        for s in range(LOBBY, 0, -1):
            G.left = s; await push_lobby(); await asyncio.sleep(1)
        if BOTS and len(G.of_user) >= 1:  # test mode: add fake players
            free = [n for n in range(1, NCARDS + 1) if n not in G.by_card]
            for i, n in enumerate(random.sample(free, BOTS), 1):
                G.by_card[n] = -i; G.of_user[-i] = n
            await push_lobby()
        if len(G.of_user) < 2:  # a round needs 2+ players: refund everyone
            for u in [u for u in G.of_user if u > 0]: move(u, "refund", STAKE, ref=f"refund:{G.rid}:{u}", note="not enough players")
            db.execute("update rounds set status='cancelled' where id=?", (G.rid,))
            continue
        G.phase = "play"; pool = len(G.of_user) * STAKE; G.prize = int(pool * (1 - CUT))
        db.execute("update rounds set status='playing' where id=?", (G.rid,))
        nums = list(range(1, 76)); random.shuffle(nums); winners = []
        for n in nums:
            G.called.append(n); await bc_call(n)
            winners = [u for u, c in G.of_user.items() if is_bingo(c, G.called)]
            if winners: break
            await asyncio.sleep(CALL_FAST if len(G.called) < FAST_N else CALL_SLOW)
        winner = random.choice(winners)  # exactly ONE winner, even if cards tie
        print(f"ROUND {G.rid}: {len(G.of_user)} players | numbers called: {len(G.called)} | tie between {len(winners)} | winner: {name_of(winner)} | card {G.of_user[winner]}")
        if winner > 0: move(winner, "win", G.prize, ref=f"win:{G.rid}", note=f"round {G.rid}")
        db.execute("update rounds set status='done', winner=?, card=?, called=?, prize=?, house=?, players=? where id=?",
                   (winner, G.of_user[winner], json.dumps(G.called), G.prize, pool - G.prize, len(G.of_user), G.rid))
        for ws, uid in list(conns.items()):
            await send(ws, {"t": "win", "names": [name_of(winner)], "prize": B(G.prize), "cards": [G.of_user[winner]],
                            "grid": card(G.of_user[winner]), "ids": [str(abs(winner))[-4:]],
                            "called": G.called, "next": WIN_SHOW, "you": uid == winner})
        await asyncio.sleep(WIN_SHOW)

async def game_loop():  # keeps the game alive even if a round fails
    while True:
        try:
            await run_game()
        except Exception:
            traceback.print_exc()
            G.phase = "lobby"; await asyncio.sleep(2)

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
            await (push_lobby() if G.phase == "lobby" or not G.called else bc_call(G.called[-1]))
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

def total(sql, *a): return db.execute(sql, a).fetchone()[0] or 0

async def api_data(req):
    need_auth(req)
    users = [dict(id=i, phone=p, name=n, bal=B(b)) for i, p, n, b in db.execute("select id,phone,name,balance from users order by rowid desc")]
    st = {"deposit": B(total("select sum(amount) from ledger where kind='deposit'")),
          "withdraw": B(-total("select sum(amount) from ledger where kind in ('withdraw_hold','withdraw_refund')")),
          "house": B(total("select sum(house) from rounds where status='done' and test=0")),
          "round": total("select count(*) from rounds where status='done' and test=0")}
    logs = [dict(ts=t, kind=k, uid=u, amt=B(a)) for t, k, u, a in db.execute("select ts,kind,uid,amount from ledger order by id desc limit 30")]
    return web.json_response({"users": users, "stats": st, "logs": logs, "online": len(conns), "phase": G.phase})

async def api_adjust(req):
    need_auth(req); d = await req.json(); uid, amt = int(d["id"]), int(round(float(d["amt"]) * 100))
    nb = move(uid, "adjust", amt, note="admin")
    if nb is None: return web.json_response({"ok": False, "error": "refused (balance cannot go below 0)"}, status=400)
    try: await bot.send_message(uid, f"Balance updated by admin: {amt/100:+} birr. Balance: {B(nb)}")
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
    u = m.from_user.id
    db.execute("insert or ignore into users(id,phone,name,balance,created) values(?,?,?,0,strftime('%s','now'))",
               (u, m.contact.phone_number, m.from_user.first_name))
    move(u, "bonus", BONUS, ref=f"bonus:{u}", note="welcome bonus")  # only once per user
    await m.answer(f"✅ Registered successfully! You got {B(BONUS)} birr to play. Stake per game: {B(STAKE)} birr.", reply_markup=menu())

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
    act, rid = c.data.split(":"); rid = int(rid)
    r = db.execute("select uid,kind,amount,ref,status from requests where id=?", (rid,)).fetchone()
    if not r or r[4] != "pending": return await c.answer("Already processed")
    uid, kind, amt, ref, _ = r
    if kind == "dep":
        if act == "ok":
            if move(uid, "deposit", amt, ref=f"dep:{ref.lower()}", note=f"request {rid}") is None:
                return await c.answer("Refused: this transaction ref was already credited")
    else:
        if act == "no": move(uid, "withdraw_refund", amt, ref=f"wdref:{rid}", note="rejected")
    db.execute("update requests set status=? where id=?", ("approved" if act == "ok" else "rejected", rid))
    await bot.send_message(uid, f"{'✅ Approved' if act == 'ok' else '❌ Rejected'}: {'deposit' if kind == 'dep' else 'withdrawal'} {B(amt)} birr. Balance: {B(bal(uid))}")
    await c.message.edit_text(c.message.text + f"\n→ {act.upper()}")

@dp.message(F.text)
async def text(m: Message):
    uid, t = m.from_user.id, m.text
    if not user(uid): return await start(m)
    if t.startswith("🎮") or t == "/play":
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Play Bingo", web_app=WebAppInfo(url=BASE))]])
        return await m.answer("Tap to play 👇", reply_markup=kb)
    if t.startswith("💰") or t == "/balance": return await m.answer(f"Balance: {B(bal(uid))} birr")
    if t.startswith("➕") or t == "/deposit": return await m.answer("Choose payment method:", reply_markup=pay_menu("dep"))
    if t.startswith("➖") or t == "/withdraw":
        if bal(uid) < MIN_WD: return await m.answer(f"❌ Insufficient balance. Minimum withdrawal is {B(MIN_WD)} birr. Your balance is {B(bal(uid))} birr")
        return await m.answer("Choose withdrawal method:", reply_markup=pay_menu("wd"))
    if t.startswith("🤝") or t == "/agent": return await m.answer("Contact an agent: @jobingosupport1")
    if t.startswith("🆘") or t == "/support": return await m.answer("Support: @jobingosupport1")
    if t.startswith("✏️") or t == "/change_username":
        pend[uid] = ("name", ""); return await m.answer("Send your new username:")
    p = pend.pop(uid, None)
    if not p: return
    if p[0] == "name":
        db.execute("update users set name=? where id=?", (t[:20], uid)); return await m.answer("✅ Username updated.", reply_markup=menu())
    parts = t.split()
    try: amt = int(round(float(parts[0]) * 100)); assert amt > 0; ref = parts[1]
    except Exception: return await m.answer("Invalid format. Write: <amount> <reference>  (example: 100 DKX123). Start again from the menu.")
    kind = p[0]
    if kind == "dep":
        if amt < MIN_DEP: return await m.answer(f"Minimum deposit is {B(MIN_DEP)} birr.")
        if db.execute("select 1 from requests where kind='dep' and lower(ref)=? and status!='rejected'", (ref.lower(),)).fetchone() or ref_used(f"dep:{ref.lower()}"):
            return await m.answer("❌ This transaction reference was already used.")
    else:
        if amt < MIN_WD: return await m.answer(f"Minimum withdrawal is {B(MIN_WD)} birr.")
    rid = db.execute("insert into requests(ts,uid,kind,amount,ref) values(strftime('%s','now'),?,?,?,?)", (uid, kind, amt, ref)).lastrowid
    if kind == "wd" and move(uid, "withdraw_hold", -amt, ref=f"wdreq:{rid}", note=f"to {ref}") is None:
        db.execute("update requests set status='rejected' where id=?", (rid,)); return await m.answer("❌ Insufficient balance.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"ok:{rid}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"no:{rid}")]])
    await bot.send_message(ADMIN, f"{'DEPOSIT' if kind == 'dep' else 'WITHDRAW'} #{rid} via {p[1]}\nUser {uid} ({user(uid)[0]})\nAmount: {B(amt)} birr\nRef/Account: {ref}", reply_markup=kb)
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
    try:
        await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in [
            ("play", "Play"), ("balance", "Balance"), ("deposit", "Deposit"), ("withdraw", "Withdraw"),
            ("agent", "Agent"), ("support", "Support"), ("change_username", "Change name")]])
    except Exception as e:
        print("Could not set menu commands (will retry next start):", e)
    print("\n=== RUNNING ===\nGame address :", BASE, "\nAdmin website:", BASE + "/admin  (password = ADMIN_PASS)")
    print("Test players:", BOTS, "| Pick time:", LOBBY, "s | Winner screen:", WIN_SHOW, "s | Database: ledger + backups\n")
    asyncio.create_task(game_loop()); asyncio.create_task(backup_loop())
    while True:  # if the internet drops, wait and reconnect instead of crashing
        try:
            await dp.start_polling(bot)
        except Exception as e:
            print("Network problem, retrying in 5 seconds:", e)
            await asyncio.sleep(5)

if __name__ == "__main__": asyncio.run(main())