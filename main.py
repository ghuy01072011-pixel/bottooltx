import re, time, json, base64, threading, queue, asyncio
import requests
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

# ==================== CONFIG ====================
TARGET_CHAT_LINK    = "https://t.me/codelaucuagame"
TARGET_CHAT_HARD_ID = -1002237214360
POLL_INTERVAL       = 1.0     # polling backup
FAST_STAGGER_MS     = 20      # stagger song song
CATCHUP_WINDOW_SEC  = 120     # quét 2 phút gần nhất khi start
PORT                = 5000

# Regex: match "Mã code là: LC79PFKQWLHQH" (bỏ dấu, hoa/thường)
CODE_REGEX = re.compile(
    r"M[ãÃaA]\s*code\s*l[àÀaA][:\s]+([A-Za-z0-9]{6,20})",
    re.IGNORECASE,
)

# ==================== STATE ====================
STATE = {
    "running": False,
    "chat_id": None,
    "group_joined": False,
    "accounts": [],
    "logs": [],
    "stats": {"success": 0, "fail": 0, "codes": 0},
    "latest_code": None,
    "tg_user": None,
    # Quản lý trạng thái OTP Telegram realtime
    "auth_step": "IDLE", # IDLE, WAIT_PHONE, WAIT_CODE, WAIT_PASSWORD, SUCCESS
    "auth_prompt": "",
}
LOG_Q = queue.Queue()
PROCESSED_MSG_IDS = set()
AUTH_INPUT_QUEUE = queue.Queue()

def log(msg, level="info"):
    e = {"t": time.strftime("%H:%M:%S"), "msg": msg, "lvl": level}
    STATE["logs"].append(e)
    STATE["logs"] = STATE["logs"][-400:]
    try: LOG_Q.put_nowait(e)
    except: pass

# ==================== LC79 ACCOUNT ====================
class Account:
    def __init__(self, un, pw, nick):
        self.un, self.pw, self.nick = un, pw, nick
        self.jwt, self.jwt_exp = None, 0
        self.balance = 0
        self.used = set()
        self.lock = threading.Lock()

    def hdr(self, auth=None):
        h = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
            "origin": "https://lc79.to",
            "referer": "https://lc79.to/",
            "accept": "application/json, text/plain, */*",
            "connection": "close",
        }
        if auth: h["authorization"] = f"Bearer {auth}"
        return h

    @staticmethod
    def parse_exp(tok):
        try:
            p = tok.split(".")[1]
            p += "=" * (-len(p) % 4)
            return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
        except:
            return 0

    def valid(self):
        return self.jwt and time.time() < self.jwt_exp - 60

    def fetch_balance(self):
        """Lấy số dư thực tế từ API LC79"""
        if not self.valid() and not self.login():
            return
        try:
            r = requests.get(
                "https://gameapi.tele68.com/v1/user/profile?cp=R&cl=R&pf=web&at=",
                headers=self.hdr(self.jwt),
                timeout=(3, 10))
            if r.status_code == 200:
                d = r.json()
                # Tùy cấu trúc trả về của api profile
                bal = d.get("balance") or d.get("money") or (d.get("data") or {}).get("balance") or 0
                self.balance = float(bal)
        except Exception as e:
            log(f"[{self.nick}] Không lấy được số dư: {e}", "warn")

    def login(self):
        try:
            r = requests.get(
                "https://apifo88daigia.tele68.com/api",
                params={"c": "3", "un": self.un, "pw": self.pw,
                        "cp": "R", "cl": "R", "pf": "web", "at": ""},
                headers=self.hdr(), timeout=(5, 20))
            d = r.json()
            at = d.get("accessToken") if d.get("success") else None
            if not at:
                return False
            r2 = requests.post(
                "https://wlb.tele68.com/v1/lobby/auth/login",
                headers=self.hdr(),
                json={"nickName": self.nick, "accessToken": at},
                timeout=(5, 20))
            res_data = r2.json()
            tok = res_data.get("token")
            if tok:
                self.jwt = tok
                self.jwt_exp = self.parse_exp(tok)
                # Lấy số dư ngay sau khi login thành công
                self.fetch_balance()
                return True
            return False
        except Exception as e:
            log(f"[{self.nick}] login error: {e}", "err")
            return False

    def redeem(self, code, _retry=False):
        with self.lock:
            if code in self.used: return
            self.used.add(code)

        if not self.valid() and not self.login():
            log(f"[{self.nick}] ❌ login fail, skip {code}", "err")
            STATE["stats"]["fail"] += 1
            return

        try:
            r = requests.post(
                "https://gameapi.tele68.com/v1/giftcode/redemption"
                "?cp=R&cl=R&pf=web&at=",
                headers=self.hdr(self.jwt),
                json={"code": code.strip(), "captcha": ""},
                timeout=(3, 15))

            if r.status_code == 401 and not _retry:
                self.jwt = None
                if self.login():
                    with self.lock: self.used.discard(code)
                    return self.redeem(code, _retry=True)
                return

            d = {}
            if "json" in (r.headers.get("content-type") or ""):
                try: d = r.json()
                except: d = {}

            if r.status_code in (200, 201):
                amt = d.get("money") or d.get("amount") or (d.get("data") or {}).get("money")
                if amt:
                    log(f"[{self.nick}] 💰 +{int(amt):,} — {code}", "ok")
                else:
                    log(f"[{self.nick}] ✅ SUCCESS — {code}", "ok")
                STATE["stats"]["success"] += 1
                # Cập nhật lại số dư sau khi redeem thành công
                self.fetch_balance()
            elif r.status_code == 400:
                log(f"[{self.nick}] ❌ 400 {d.get('message','')} — {code}", "err")
                STATE["stats"]["fail"] += 1
            elif r.status_code == 429:
                log(f"[{self.nick}] ⚠️ 429 rate-limit — {code}", "warn")
                STATE["stats"]["fail"] += 1
            else:
                log(f"[{self.nick}] ❌ {r.status_code} — {code}", "err")
                STATE["stats"]["fail"] += 1
        except Exception as e:
            log(f"[{self.nick}] ❌ net error {e}", "err")
            STATE["stats"]["fail"] += 1

# ==================== BOT ====================
BOT = {"client": None, "loop": None, "thread": None, "accounts": [], "entity": None}

def bot_start(cfg):
    if STATE["running"]:
        return False, "Bot đang chạy"
    try:
        accs = [Account(a["un"], a["pw"], a["nick"]) for a in cfg["accounts"]]
        api_id = int(cfg["api_id"])
    except Exception as e:
        return False, f"Config lỗi: {e}"

    BOT["accounts"] = accs
    STATE["accounts"] = [{"un": a.un, "nick": a.nick, "ok": False, "balance": 0} for a in accs]
    STATE["chat_id"] = None
    STATE["group_joined"] = False
    STATE["stats"] = {"success": 0, "fail": 0, "codes": 0}
    STATE["latest_code"] = None
    STATE["tg_user"] = None
    STATE["running"] = True
    STATE["auth_step"] = "IDLE"
    PROCESSED_MSG_IDS.clear()

    def runner():
        BOT["loop"] = asyncio.new_event_loop()
        asyncio.set_event_loop(BOT["loop"])
        BOT["loop"].run_until_complete(_bot_main(cfg, accs, api_id))

    BOT["thread"] = threading.Thread(target=runner, daemon=True)
    BOT["thread"].start()
    return True, "Đã khởi động bot"

async def _bot_main(cfg, accs, api_id):
    from telethon import TelegramClient, events, utils
    from telethon.tl.functions.channels import JoinChannelRequest

    # Custom input callback để bắt yêu cầu SĐT/OTP gửi thẳng ra Web UI thay vì terminal
    async def interactive_phone():
        STATE["auth_step"] = "WAIT_PHONE"
        STATE["auth_prompt"] = "Vui lòng nhập số điện thoại Telegram (ví dụ: +84912345678):"
        log("📱 Yêu cầu nhập Số điện thoại Telegram trên giao diện Web...", "warn")
        while True:
            val = await asyncio.get_event_loop().run_in_executor(None, AUTH_INPUT_QUEUE.get)
            if val:
                STATE["auth_step"] = "IDLE"
                return val

    async def interactive_code():
        STATE["auth_step"] = "WAIT_CODE"
        STATE["auth_prompt"] = "Vui lòng nhập mã OTP Telegram nhận được:"
        log("🔑 Yêu cầu nhập Mã OTP Telegram trên giao diện Web...", "warn")
        while True:
            val = await asyncio.get_event_loop().run_in_executor(None, AUTH_INPUT_QUEUE.get)
            if val:
                STATE["auth_step"] = "IDLE"
                return val

    async def interactive_password():
        STATE["auth_step"] = "WAIT_PASSWORD"
        STATE["auth_prompt"] = "Vui lòng nhập mật khẩu Mật khẩu 2 lớp (Cloud Password):"
        log("🔒 Yêu cầu nhập Mật khẩu 2 lớp (2FA) Telegram trên giao diện Web...", "warn")
        while True:
            val = await asyncio.get_event_loop().run_in_executor(None, AUTH_INPUT_QUEUE.get)
            if val:
                STATE["auth_step"] = "IDLE"
                return val

    client = TelegramClient(
        "session", api_id, cfg["api_hash"],
        connection_retries=5, retry_delay=3,
        auto_reconnect=True, request_retries=5,
        flood_sleep_threshold=60)
    BOT["client"] = client

    log("🔌 Kết nối Telegram thực tế...", "info")
    try:
        # Thực hiện login real qua telethon interactive
        await client.start(
            phone=interactive_phone,
            code=interactive_code,
            password=interactive_password
        )
        me = await client.get_me()
        STATE["tg_user"] = f"@{me.username}" if me.username else f"{me.first_name} ({me.phone})"
        log(f"✅ Đăng nhập Telegram thành công với tài khoản: {STATE['tg_user']}", "ok")
    except Exception as e:
        log(f"❌ Telegram login fail: {e}", "err")
        STATE["running"] = False
        STATE["auth_step"] = "IDLE"
        return

    username = TARGET_CHAT_LINK.rstrip("/").split("/")[-1].lstrip("@")
    entity = None
    try:
        entity = await client.get_entity(username)
    except Exception as e:
        log(f"⚠️ get_entity(@{username}) fail: {e}, thử hard ID...", "warn")
        try: entity = await client.get_entity(TARGET_CHAT_HARD_ID)
        except Exception as e2:
            log(f"❌ Không resolve được chat: {e2}", "err")
            STATE["running"] = False
            return

    try:
        await client(JoinChannelRequest(entity))
        log(f"✅ Đã join group @{username}", "ok")
        STATE["group_joined"] = True
    except Exception as e:
        msg = str(e)
        if "USER_ALREADY_PARTICIPANT" in msg or "already" in msg.lower():
            log(f"✅ Đã là thành viên @{username}", "ok")
            STATE["group_joined"] = True
        else:
            log(f"⚠️ Join warn: {e}", "warn")

    BOT["entity"] = entity
    STATE["chat_id"] = utils.get_peer_id(entity)
    log(f"🎯 Chat ID: {STATE['chat_id']}", "ok")

    log(f"🔐 Đăng nhập và lấy số dư {len(accs)} account LC79...", "info")
    ok = 0
    for i, a in enumerate(accs):
        if a.login():
            ok += 1
            STATE["accounts"][i]["ok"] = True
            STATE["accounts"][i]["balance"] = a.balance
            log(f"[{a.nick}] ✅ JWT OK — Số dư: {a.balance:,.0f} đ", "ok")
        else:
            log(f"[{a.nick}] ❌ login fail", "err")
    log(f"✅ {ok}/{len(accs)} account sẵn sàng", "ok")

    await _catchup(client, entity)

    @client.on(events.NewMessage)
    async def on_new(event):
        if not STATE["running"]: return
        if event.chat_id != STATE["chat_id"]: return
        await _process_message(event.message)

    async def polling():
        last_id = 0
        try:
            async for m in client.iter_messages(entity, limit=1):
                last_id = m.id
                break
        except Exception:
            pass
        while STATE["running"]:
            try:
                msgs = await client.get_messages(entity, min_id=last_id, limit=20)
                for m in reversed(msgs):
                    if m.id > last_id:
                        last_id = m.id
                        await _process_message(m)
            except Exception:
                pass
            await asyncio.sleep(POLL_INTERVAL)

    asyncio.create_task(polling())
    log("🎯 Bot sẵn sàng — chờ code (event + polling 1s)...", "ok")

    try:
        await client.run_until_disconnected()
    except Exception as e:
        log(f"⚠️ Bot dừng: {e}", "err")
    finally:
        STATE["running"] = False
        STATE["auth_step"] = "IDLE"
        log("🛑 Bot đã dừng", "warn")

async def _catchup(client, entity):
    log("🔍 Quét tin nhắn cũ gần đây...", "info")
    now = time.time()
    cnt = 0
    try:
        async for m in client.iter_messages(entity, limit=30):
            if now - m.date.timestamp() > CATCHUP_WINDOW_SEC:
                break
            text = m.raw_text or ""
            if CODE_REGEX.search(text):
                await _process_message(m)
                cnt += 1
    except Exception as e:
        log(f"⚠️ Catchup warn: {e}", "warn")
    log(f"✅ Catchup xong ({cnt} tin khớp)", "ok")

async def _process_message(msg, force=False):
    """Parse code → redeem. force=True bỏ qua dedup."""
    if not force and msg.id in PROCESSED_MSG_IDS: return
    PROCESSED_MSG_IDS.add(msg.id)
    if len(PROCESSED_MSG_IDS) > 1000:
        PROCESSED_MSG_IDS.clear()

    text = msg.raw_text or ""
    m = CODE_REGEX.search(text)
    if not m: return

    full = m.group(1).upper()
    candidates = []
    if full.startswith("LC79") and len(full) > 4:
        candidates.append(full[4:])
    candidates.append(full)

    STATE["stats"]["codes"] += 1
    STATE["latest_code"] = candidates[0]
    t0 = time.time()
    log(f"🎁 Nhận code: {full} → thử {candidates}", "ok")

    await _redeem_all_parallel(candidates)

    elapsed_ms = (time.time() - t0) * 1000
    lvl = "ok" if elapsed_ms < 1000 else "warn"
    log(f"⚡ Xong {len(candidates)} biến thể trong {elapsed_ms:.0f}ms (real API)", lvl)
    
    # Cập nhật lại số dư mới nhất vào STATE cho UI hiển thị
    for i, a in enumerate(BOT["accounts"]):
        if i < len(STATE["accounts"]):
            STATE["accounts"][i]["balance"] = a.balance

async def _redeem_all_parallel(candidates):
    """Redeem song song tất cả account × mọi biến thể code."""
    if not STATE["running"]: return
    loop = asyncio.get_running_loop()
    tasks = []
    idx = 0
    for code in candidates:
        for acc in BOT["accounts"]:
            if not STATE["running"]: return
            async def worker(a=acc, c=code, i=idx):
                if i > 0:
                    await asyncio.sleep(i * FAST_STAGGER_MS / 1000)
                await loop.run_in_executor(None, a.redeem, c)
            tasks.append(worker())
            idx += 1
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

async def _redeem_latest():
    """Quét 20 tin gần nhất, tìm code, force redeem lại."""
    if not BOT["entity"] or not BOT["client"]:
        log("❌ Bot chưa sẵn sàng", "err")
        return
    log("🔎 Đang quét 20 tin gần nhất để tìm code...", "info")
    try:
        msgs = await BOT["client"].get_messages(BOT["entity"], limit=20)
        for m in msgs:
            text = m.raw_text or ""
            mt = CODE_REGEX.search(text)
            if mt:
                full = mt.group(1).upper()
                log(f"🔎 Tìm thấy code gần nhất: {full}", "ok")
                await _process_message(m, force=True)
                return
        log("❌ Không tìm thấy code nào trong 20 tin gần nhất", "err")
    except Exception as e:
        log(f"❌ Redeem latest error: {e}", "err")

def _spawn(coro):
    """Schedule coroutine vào bot loop (an toàn từ thread Flask)."""
    if not STATE["running"] or not BOT["loop"] or BOT["loop"].is_closed():
        return False
    try:
        asyncio.run_coroutine_threadsafe(coro, BOT["loop"])
        return True
    except Exception as e:
        log(f"❌ spawn: {e}", "err")
        return False

def bot_stop():
    STATE["running"] = False
    c = BOT["client"]
    if c and BOT["loop"]:
        async def _d():
            try: await c.disconnect()
            except: pass
        try:
            asyncio.run_coroutine_threadsafe(_d(), BOT["loop"])
        except: pass
    log("🛑 Đang dừng bot...", "warn")
    return True

# ==================== FLASK ROUTES ====================
@app.route("/")
def index():
    return Response(HTML, mimetype="text/html; charset=utf-8")

@app.route("/api/state")
def api_state():
    return jsonify({
        "running": STATE["running"],
        "chat_id": STATE["chat_id"],
        "group_joined": STATE["group_joined"],
        "accounts": STATE["accounts"],
        "stats": STATE["stats"],
        "latest_code": STATE["latest_code"],
        "tg_user": STATE["tg_user"],
        "auth_step": STATE["auth_step"],
        "auth_prompt": STATE["auth_prompt"],
    })

@app.route("/api/auth-input", methods=["POST"])
def api_auth_input():
    val = (request.json or {}).get("value", "").strip()
    if val:
        AUTH_INPUT_QUEUE.put(val)
        return jsonify(ok=True, msg="Đã nhận giá trị")
    return jsonify(ok=False, msg="Giá trị trống"), 400

@app.route("/api/stream")
def api_stream():
    def gen():
        yield "retry: 3000\n\n"
        while True:
            try:
                e = LOG_Q.get(timeout=30)
                yield f"data: {json.dumps(e)}\n\n"
            except queue.Empty:
                yield ": ping\n\n"
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/start", methods=["POST"])
def api_start():
    cfg = request.json or {}
    for k in ("api_id", "api_hash", "accounts"):
        if not cfg.get(k):
            return jsonify(ok=False, msg=f"Thiếu {k}"), 400
    cfg["chat_link"] = TARGET_CHAT_LINK
    ok, msg = bot_start(cfg)
    return jsonify(ok=ok, msg=msg)

@app.route("/api/stop", methods=["POST"])
def api_stop():
    bot_stop()
    return jsonify(ok=True, msg="Đã dừng bot")

@app.route("/api/redeem-latest", methods=["POST"])
def api_redeem_latest():
    if not STATE["running"]:
        return jsonify(ok=False, msg="Bot chưa chạy — bấm Start trước"), 400
    ok = _spawn(_redeem_latest())
    return jsonify(ok=ok, msg="🔎 Đang lấy code gần nhất và redeem real..." if ok else "Lỗi spawn")

@app.route("/api/redeem-code", methods=["POST"])
def api_redeem_code():
    if not STATE["running"]:
        return jsonify(ok=False, msg="Bot chưa chạy — bấm Start trước"), 400
    code = (request.json or {}).get("code", "").strip().upper()
    if not code:
        return jsonify(ok=False, msg="Thiếu code"), 400
    if not re.match(r"^[A-Z0-9]{4,24}$", code):
        return jsonify(ok=False, msg="Code không hợp lệ (A-Z0-9, 4-24 ký tự)"), 400

    candidates = []
    if code.startswith("LC79") and len(code) > 4:
        candidates.append(code[4:])
    candidates.append(code)

    STATE["latest_code"] = candidates[0]
    log(f"🎯 Redeem thủ công: {candidates}", "ok")
    ok = _spawn(_redeem_all_parallel(candidates))
    return jsonify(ok=ok, msg=f"🎯 Đang redeem real {candidates[0]}" if ok else "Lỗi spawn")

@app.route("/api/clear-logs", methods=["POST"])
def api_clear():
    STATE["logs"].clear()
    STATE["stats"] = {"success": 0, "fail": 0, "codes": 0}
    PROCESSED_MSG_IDS.clear()
    return jsonify(ok=True)

# ==================== HTML ====================
HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no,viewport-fit=cover" />
<title>LC79 Auto Redeem - Real Telegram Login</title>
<style>
*{box-sizing:border-box;-webkit-text-size-adjust:100%;text-size-adjust:100%}
html,body{margin:0;padding:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;background:#0f1419;color:#e7e9ea;touch-action:pan-y;overflow-x:hidden}
body{padding:20px}
.wrap{max-width:1280px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;padding:18px 22px;margin-bottom:18px;background:linear-gradient(135deg,#1a2028,#151b22);border:1px solid #262d36;border-radius:16px}
.logo-row{display:flex;align-items:center;gap:14px}
.logo-badge{width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,#2AABEE,#229ED9);display:flex;align-items:center;justify-content:center;font-size:22px;box-shadow:0 6px 18px rgba(42,171,238,.35)}
.title-wrap h1{margin:0;font-size:18px;font-weight:700}
.title-wrap p{margin:2px 0 0;font-size:12.5px;color:#8b98a5}
.status-pill{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;font-size:13px;font-weight:600;background:#1f2937;border:1px solid #374151;color:#8b98a5}
.status-pill.on{background:rgba(34,158,217,.12);border-color:rgba(42,171,238,.5);color:#2AABEE}
.status-dot{width:8px;height:8px;border-radius:50%;background:#6b7280}
.status-pill.on .status-dot{background:#2AABEE;box-shadow:0 0 8px #2AABEE;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:10px 18px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;transition:transform .12s,opacity .2s,box-shadow .2s}
.btn:hover:not(:disabled){transform:translateY(-1px)}
.btn:active:not(:disabled){transform:scale(.98)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:linear-gradient(135deg,#2AABEE,#229ED9);color:#fff;box-shadow:0 6px 16px rgba(42,171,238,.35)}
.btn-danger{background:#ef4444;color:#fff}
.btn-warn{background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff;box-shadow:0 6px 16px rgba(245,158,11,.35)}
.btn-ghost{background:transparent;border:1px solid #374151;color:#e7e9ea}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.stat{background:linear-gradient(135deg,#1a2028,#151b22);border:1px solid #262d36;border-radius:14px;padding:14px 16px}
.stat .lbl{font-size:11.5px;color:#8b98a5;text-transform:uppercase;letter-spacing:.5px}
.stat .val{font-size:22px;font-weight:700;margin-top:4px}
.stat.ok .val{color:#22c55e}
.stat.err .val{color:#ef4444}
.stat.info .val{color:#2AABEE}
.stat.warn .val{color:#f59e0b}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{background:linear-gradient(135deg,#1a2028,#151b22);border:1px solid #262d36;border-radius:16px;overflow:hidden;display:flex;flex-direction:column}
.panel-head{padding:14px 18px;border-bottom:1px solid #262d36;display:flex;align-items:center;justify-content:space-between;font-weight:600;font-size:14px}
.panel-head .h{display:flex;align-items:center;gap:8px}
.panel-body{padding:16px 18px;flex:1}
.field{margin-bottom:12px}
.field label{display:block;font-size:12px;color:#8b98a5;margin-bottom:5px;font-weight:500}
.field input{width:100%;height:40px;padding:0 12px;font-size:14px;color:#e7e9ea;background:#0f1419;border:1px solid #2d3644;border-radius:8px;outline:none;font-family:inherit;transition:border-color .2s,box-shadow .2s}
.field input:focus{border-color:#2AABEE;box-shadow:0 0 0 3px rgba(42,171,238,.15)}
.field input[readonly]{opacity:.7;cursor:not-allowed}
.row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.acc-list{max-height:220px;overflow-y:auto;margin-top:8px}
.acc-list::-webkit-scrollbar{width:6px}
.acc-list::-webkit-scrollbar-thumb{background:#374151;border-radius:3px}
.acc-item{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;margin-bottom:6px;background:#0f1419;border:1px solid #2d3644;border-radius:8px;font-size:13px;animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.acc-item .nick{font-weight:600}
.acc-item .un{color:#8b98a5;font-size:12px;margin-top:2px}
.acc-item .bal{color:#22c55e;font-weight:600;font-size:12.5px;margin-top:2px}
.acc-item .del{background:transparent;border:1px solid #374151;color:#ef4444;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;transition:all .2s}
.acc-item .del:hover{background:rgba(239,68,68,.1);border-color:#ef4444}
.acc-item .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-left:6px}
.acc-item .badge.ok{background:rgba(34,197,94,.15);color:#22c55e}
.acc-item .badge.off{background:rgba(107,114,128,.15);color:#6b7280}
.empty{text-align:center;color:#6b7280;font-size:13px;padding:20px 10px}
.divider{border-top:1px solid #262d36;margin:16px 0 14px;padding-top:14px}
.manual-row{display:flex;gap:8px}
.manual-row input{flex:1;height:40px;padding:0 12px;background:#0f1419;border:1px solid #2d3644;border-radius:8px;color:#e7e9ea;font-size:14px;outline:none;font-family:inherit;transition:border-color .2s}
.manual-row input:focus{border-color:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.15)}
.manual-row button{height:40px;padding:0 18px}
.log-body{padding:12px 16px;font-family:"SF Mono",Menlo,Consolas,monospace;font-size:12.5px;line-height:1.7;height:520px;overflow-y:auto;background:#0a0e13}
.log-body::-webkit-scrollbar{width:6px}
.log-body::-webkit-scrollbar-thumb{background:#374151;border-radius:3px}
.log-line{display:flex;gap:8px;padding:2px 0;animation:fadeIn .15s}
.log-line .t{color:#4b5563;flex-shrink:0}
.log-line.ok .msg{color:#22c55e}
.log-line.err .msg{color:#ef4444}
.log-line.warn .msg{color:#f59e0b}
.log-line.info .msg{color:#8b98a5}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);padding:12px 22px;border-radius:10px;font-size:14px;font-weight:500;background:#1f2937;border:1px solid #374151;color:#fff;box-shadow:0 10px 30px rgba(0,0,0,.4);transition:transform .3s;z-index:999;max-width:90vw}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:#22c55e;background:#052e16;color:#86efac}
.toast.err{border-color:#ef4444;background:#450a0a;color:#fca5a5}

/* Modal nhập OTP Telegram Real */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.75);display:flex;align-items:center;justify-content:center;z-index:1000;opacity:0;pointer-events:none;transition:opacity .2s}
.modal-overlay.active{opacity:1;pointer-events:auto}
.modal-box{background:#1a2028;border:1px solid #374151;border-radius:16px;width:90%;max-width:400px;padding:24px;box-shadow:0 20px 40px rgba(0,0,0,.6)}
.modal-box h3{margin:0 0 10px;font-size:18px;color:#2AABEE}
.modal-box p{margin:0 0 16px;font-size:13.5px;color:#8b98a5;line-height:1.5}
.modal-box input{width:100%;height:42px;padding:0 12px;background:#0f1419;border:1px solid #2d3644;border-radius:8px;color:#e7e9ea;font-size:15px;outline:none;margin-bottom:16px}
.modal-box input:focus{border-color:#2AABEE}
.modal-box button{width:100%;height:42px}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo-row">
      <div class="logo-badge">🤖</div>
      <div class="title-wrap">
        <h1>LC79 Auto Redeem</h1>
        <p>Telegram Real Login & OTP × LC79 Live Balance</p>
      </div>
    </div>
    <div class="status-pill" id="statusPill">
      <span class="status-dot"></span><span id="statusText">Offline</span>
    </div>
    <div class="actions">
      <button class="btn btn-primary" id="btnStart">▶ Bắt đầu</button>
      <button class="btn btn-danger" id="btnStop" disabled>■ Dừng</button>
      <button class="btn btn-warn" id="btnLatest" disabled>⚡ Redeem Latest</button>
    </div>
  </div>

  <div class="stats">
    <div class="stat ok"><div class="lbl">Thành công</div><div class="val" id="statOk">0</div></div>
    <div class="stat err"><div class="lbl">Thất bại</div><div class="val" id="statErr">0</div></div>
    <div class="stat info"><div class="lbl">Codes</div><div class="val" id="statCodes">0</div></div>
    <div class="stat info"><div class="lbl">Telegram Account</div><div class="val" id="statTgUser" style="font-size:14px;font-weight:600">—</div></div>
    <div class="stat warn"><div class="lbl">Code gần nhất</div><div class="val" id="statLatest" style="font-size:16px">—</div></div>
    <div class="stat info"><div class="lbl">Group</div><div class="val" id="statGroup" style="font-size:15px">—</div></div>
  </div>

  <div class="grid">
    <div class="panel">
      <div class="panel-head"><div class="h">⚙️ Cấu hình</div></div>
      <div class="panel-body">
        <div class="row-2">
          <div class="field"><label>API ID</label><input id="apiId" type="number" placeholder="12345678" /></div>
          <div class="field"><label>API Hash</label><input id="apiHash" type="text" placeholder="abcdef1234..." /></div>
        </div>
        <div class="field"><label>Chat nguồn (cố định)</label><input id="chatLink" type="text" value="https://t.me/codelaucuagame" readonly /></div>

        <div style="display:flex;align-items:center;justify-content:space-between;margin:16px 0 8px;font-weight:600;font-size:14px">
          <span>👥 LC79 Accounts (<span id="accCount">0</span>)</span>
        </div>

        <div class="row-2">
          <div class="field"><label>Username</label><input id="accUn" type="text" placeholder="user123" /></div>
          <div class="field"><label>Password (hash)</label><input id="accPw" type="text" placeholder="md5/sha hash" /></div>
        </div>
        <div class="row-2">
          <div class="field"><label>Nickname</label><input id="accNick" type="text" placeholder="nick1" /></div>
          <div class="field" style="display:flex;align-items:flex-end">
            <button class="btn btn-ghost" id="btnAddAcc" style="width:100%;height:40px">+ Thêm account</button>
          </div>
        </div>

        <div class="acc-list" id="accList"><div class="empty">Chưa có account nào</div></div>

        <div class="divider">
          <div style="font-weight:600;font-size:13px;margin-bottom:8px">🎯 Nhập code thủ công (real API)</div>
          <div class="manual-row">
            <input id="manualCode" type="text" placeholder="LC79PFKQWLHQH hoặc PFKQWLHQH" maxlength="24" />
            <button class="btn btn-warn" id="btnRedeemManual" disabled>Redeem</button>
          </div>
          <div style="font-size:11.5px;color:#6b7280;margin-top:6px;line-height:1.5">Tự bỏ prefix LC79 nếu có. Gọi API thật 100%.</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <div class="h">📡 Live Log</div>
        <button class="btn btn-ghost" id="btnClear" style="padding:6px 12px;font-size:12px">Xoá</button>
      </div>
      <div class="log-body" id="logBody">
        <div class="log-line info"><span class="t">--:--</span><span class="msg">Chưa có log...</span></div>
      </div>
    </div>
  </div>
</div>

<!-- Modal yêu cầu nhập thông tin xác thực Telegram Real -->
<div class="modal-overlay" id="authModal">
  <div class="modal-box">
    <h3 id="authTitle">Xác thực Telegram Real</h3>
    <p id="authPromptText">Đang chờ thông tin...</p>
    <input id="authInputVal" type="text" placeholder="Nhập vào đây..." autocomplete="off" />
    <button class="btn btn-primary" id="btnSendAuth">Gửi đi</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const LS_KEY = 'lc79_config_v3';
  let state = { running:false, accounts:[] };
  let localAccounts = [];
  let lastAuthStep = 'IDLE';

  function loadCfg() {
    try {
      const c = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
      if (c.apiId) $('apiId').value = c.apiId;
      if (c.apiHash) $('apiHash').value = c.apiHash;
      if (Array.isArray(c.accounts)) localAccounts = c.accounts;
    } catch {}
    renderAcc();
  }
  function saveCfg() {
    localStorage.setItem(LS_KEY, JSON.stringify({
      apiId: $('apiId').value,
      apiHash: $('apiHash').value,
      accounts: localAccounts,
    }));
  }
  ['apiId','apiHash'].forEach(id => $(id).addEventListener('input', saveCfg));

  let toastT;
  function toast(msg, type='info') {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast show ' + type;
    clearTimeout(toastT);
    toastT = setTimeout(() => el.className = 'toast', 3000);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function renderAcc() {
    const list = $('accList');
    $('accCount').textContent = localAccounts.length;
    if (localAccounts.length === 0) {
      list.innerHTML = '<div class="empty">Chưa có account nào</div>';
      return;
    }
    list.innerHTML = localAccounts.map((a, i) => {
      const st = state.accounts[i];
      const isOnline = st?.ok;
      const badge = isOnline
        ? '<span class="badge ok">● Online</span>'
        : '<span class="badge off">○ Offline</span>';
      const bal = isOnline && st.balance !== undefined 
        ? '<div class="bal">💰 Số dư: ' + Number(st.balance).toLocaleString() + ' đ</div>' 
        : '<div class="bal" style="color:#6b7280">💰 Số dư: —</div>';

      return '<div class="acc-item">' +
        '<div><div class="nick">' + escapeHtml(a.nick) + badge + '</div>' +
        '<div class="un">' + escapeHtml(a.un) + '</div>' + bal + '</div>' +
        '<button class="del" data-idx="' + i + '" title="Xoá">✕</button>' +
        '</div>';
    }).join('');
    list.querySelectorAll('.del').forEach(btn => {
      btn.addEventListener('click', e => {
        localAccounts.splice(+e.target.dataset.idx, 1);
        saveCfg(); renderAcc();
      });
    });
  }

  $('btnAddAcc').addEventListener('click', () => {
    const un = $('accUn').value.trim();
    const pw = $('accPw').value.trim();
    const nick = $('accNick').value.trim();
    if (!un || !pw || !nick) return toast('Nhập đủ username / password / nickname', 'err');
    if (localAccounts.some(a => a.un === un)) return toast('Username đã tồn tại', 'err');
    localAccounts.push({ un, pw, nick });
    $('accUn').value = ''; $('accPw').value = ''; $('accNick').value = '';
    saveCfg(); renderAcc();
    toast('Đã thêm account', 'ok');
  });

  $('btnStart').addEventListener('click', async () => {
    const apiId = $('apiId').value.trim();
    const apiHash = $('apiHash').value.trim();
    if (!apiId || !apiHash) return toast('Điền đủ API ID và Hash', 'err');
    if (localAccounts.length === 0) return toast('Chưa có account', 'err');

    $('btnStart').disabled = true;
    try {
      const res = await fetch('/api/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_id: apiId, api_hash: apiHash, accounts: localAccounts }),
      });
      const d = await res.json();
      if (!d.ok) { toast(d.msg || 'Lỗi', 'err'); $('btnStart').disabled = false; return; }
      toast(d.msg || 'Đã khởi động', 'ok');
    } catch { toast('Lỗi kết nối server', 'err'); $('btnStart').disabled = false; }
  });

  $('btnStop').addEventListener('click', async () => {
    $('btnStop').disabled = true;
    try { await fetch('/api/stop', { method:'POST' }); toast('Đã dừng', 'ok'); }
    catch { toast('Lỗi', 'err'); }
  });

  $('btnLatest').addEventListener('click', async () => {
    $('btnLatest').disabled = true;
    try {
      const res = await fetch('/api/redeem-latest', { method: 'POST' });
      const d = await res.json();
      toast(d.msg || 'OK', d.ok ? 'ok' : 'err');
    } catch { toast('Lỗi kết nối', 'err'); }
    setTimeout(() => { $('btnLatest').disabled = !state.running; }, 1500);
  });

  $('btnRedeemManual').addEventListener('click', async () => {
    const code = $('manualCode').value.trim().toUpperCase();
    if (!code) return toast('Nhập code trước', 'err');
    $('btnRedeemManual').disabled = true;
    try {
      const res = await fetch('/api/redeem-code', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      const d = await res.json();
      toast(d.msg || 'OK', d.ok ? 'ok' : 'err');
      if (d.ok) $('manualCode').value = '';
    } catch { toast('Lỗi kết nối', 'err'); }
    $('btnRedeemManual').disabled = false;
  });
  $('manualCode').addEventListener('keydown', e => {
    if (e.key === 'Enter') $('btnRedeemManual').click();
  });

  $('btnClear').addEventListener('click', async () => {
    $('logBody').innerHTML = '';
    await fetch('/api/clear-logs', { method:'POST' });
  });

  // Xử lý gửi OTP/Phone lên server
  $('btnSendAuth').addEventListener('click', async () => {
    const val = $('authInputVal').value.trim();
    if (!val) return toast('Vui lòng nhập giá trị', 'err');
    try {
      const res = await fetch('/api/auth-input', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: val })
      });
      const d = await res.json();
      if (d.ok) {
        $('authInputVal').value = '';
        $('authModal').classList.remove('active');
        toast('Đã gửi thông tin thành công', 'ok');
      } else {
        toast(d.msg, 'err');
      }
    } catch {
      toast('Lỗi kết nối', 'err');
    }
  });
  $('authInputVal').addEventListener('keydown', e => {
    if (e.key === 'Enter') $('btnSendAuth').click();
  });

  async function pollState() {
    try {
      const d = await (await fetch('/api/state')).json();
      state = d;
      const pill = $('statusPill');
      if (d.running) {
        pill.classList.add('on'); $('statusText').textContent = 'Online';
        $('btnStart').disabled = true; $('btnStop').disabled = false;
        $('btnLatest').disabled = false; $('btnRedeemManual').disabled = false;
      } else {
        pill.classList.remove('on'); $('statusText').textContent = 'Offline';
        $('btnStart').disabled = false; $('btnStop').disabled = true;
        $('btnLatest').disabled = true; $('btnRedeemManual').disabled = true;
      }
      $('statOk').textContent = d.stats.success;
      $('statErr').textContent = d.stats.fail;
      $('statCodes').textContent = d.stats.codes;
      $('statLatest').textContent = d.latest_code || '—';
      $('statTgUser').textContent = d.tg_user || '—';
      $('statGroup').textContent = d.group_joined ? '✅ Joined' : (d.running ? '⏳ Joining' : '—');
      
      // Kiểm tra xem backend có đang cần hỏi thông tin đăng nhập Telegram Real không
      if (d.auth_step && d.auth_step !== 'IDLE') {
        if (lastAuthStep !== d.auth_step) {
          lastAuthStep = d.auth_step;
          $('authPromptText').textContent = d.auth_prompt;
          if (d.auth_step === 'WAIT_PHONE') {
            $('authTitle').textContent = '📲 Nhập Số Điện Thoại';
            $('authInputVal').placeholder = '+84912345678';
            $('authInputVal').type = 'text';
          } else if (d.auth_step === 'WAIT_CODE') {
            $('authTitle').textContent = '🔑 Nhập Mã OTP Telegram';
            $('authInputVal').placeholder = '12345';
            $('authInputVal').type = 'text';
          } else if (d.auth_step === 'WAIT_PASSWORD') {
            $('authTitle').textContent = '🔒 Nhập Mật Khẩu 2 Lớp (2FA)';
            $('authInputVal').placeholder = 'Mật khẩu cloud...';
            $('authInputVal').type = 'password';
          }
          $('authModal').classList.add('active');
          $('authInputVal').focus();
        }
      } else {
        if (lastAuthStep !== 'IDLE') {
          lastAuthStep = 'IDLE';
          $('authModal').classList.remove('active');
        }
      }

      renderAcc();
    } catch {}
  }
  pollState();
  setInterval(pollState, 2000);

  function appendLog(e) {
    const body = $('logBody');
    const line = document.createElement('div');
    line.className = 'log-line ' + (e.lvl || 'info');
    line.innerHTML = '<span class="t">' + e.t + '</span><span class="msg">' + escapeHtml(e.msg) + '</span>';
    body.appendChild(line);
    while (body.children.length > 300) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
  }
  if (window.EventSource) {
    const es = new EventSource('/api/stream');
    es.onmessage = ev => { try { appendLog(JSON.parse(ev.data)); } catch {} };
  }

  loadCfg();
})();
</script>
</body>
</html>
"""

# ==================== MAIN ====================
if __name__ == "__main__":
    print("=" * 60)
    print("🤖 LC79 Auto Redeem Tool — Real API (Không giả lập)")
    print("=" * 60)
    print(f"🌐 Mở trình duyệt: http://localhost:{PORT}")
    print(f"📡 Group nguồn: {TARGET_CHAT_LINK}")
    print(f"🎯 Chat ID:     {TARGET_CHAT_HARD_ID}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)