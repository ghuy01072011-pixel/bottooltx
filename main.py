import re, time, random, threading, base64, json, queue, asyncio
import requests
from flask import Flask, Response, request, jsonify

app = Flask(__name__)

# ==================== CONFIG & STATE ====================
TARGET_CHAT_LINK = "https://t.me/codelaucuagame"
TARGET_CHAT_ID = None

API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"
BASE_URL = "https://gameapi.tele68.com"
PARAMS = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="

STAGGER_SEC = 1.0
STAGGER_JITTER = 0.3
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 45
MAX_RETRY = 3

STATE = {
    "running": False,
    "group_joined": False,
    "accounts": [],
    "logs": [],
    "stats": {"success": 0, "fail": 0, "codes": 0},
    "latest_code": None,
    "tg_user": None,
    "auth_step": "IDLE", # IDLE, WAIT_PHONE, WAIT_CODE, WAIT_PASSWORD
    "auth_prompt": "",
}

LOG_Q = queue.Queue()
AUTH_INPUT_QUEUE = queue.Queue()
PROCESSED_MSG_IDS = set()

def log(msg, level="info"):
    t = time.strftime("%H:%M:%S")
    e = {"t": t, "msg": msg, "lvl": level}
    STATE["logs"].append(e)
    STATE["logs"] = STATE["logs"][-400:]
    try:
        LOG_Q.put_nowait(e)
    except:
        pass
    print(f"[{t}] [{level.upper()}] {msg}")

# ==================== ACCOUNT CLASS (GIỮ NGUYÊN GỐC + PROFILE) ====================
class Account:
    def __init__(self, un, pw, nick):
        self.un = un
        self.pw = pw
        self.nick = nick
        self.used_codes = set()
        self.lock = threading.Lock()
        self.jwt = None
        self.jwt_exp = 0
        self.balance = 0

    def _headers(self, auth=None):
        h = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
            "origin": "https://lc79.to",
            "referer": "https://lc79.to/",
            "accept": "application/json, text/plain, */*",
            "connection": "close",
        }
        if auth:
            h["authorization"] = f"Bearer {auth}"
        return h

    @staticmethod
    def parse_jwt_exp(token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return data.get("exp", 0)
        except Exception:
            return 0

    def is_token_valid(self):
        return self.jwt is not None and time.time() < (self.jwt_exp - 60)

    def fetch_balance(self):
        """Lấy số dư thực tế từ API LC79"""
        if not self.is_token_valid() and not self.refresh_token():
            return
        try:
            with requests.Session() as s:
                res = s.get(
                    BASE_URL + "/v1/user/profile?cp=R&cl=R&pf=web&at=",
                    headers=self._headers(self.jwt),
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
            if res.status_code == 200:
                data = res.json()
                bal = data.get("balance") or data.get("money") or (data.get("data") or {}).get("balance") or 0
                self.balance = float(bal)
        except Exception as e:
            log(f"[{self.nick}] Không lấy được số dư: {e}", "warn")

    def get_access_token(self):
        try:
            with requests.Session() as s:
                res = s.get(API_GET_ACCESS, params={
                    "c": "3", "un": self.un, "pw": self.pw,
                    "cp": "R", "cl": "R", "pf": "web", "at": ""
                }, headers=self._headers(), timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            data = res.json()
            if data.get("success"):
                return data.get("accessToken")
            log(f"[{self.nick}] ❌ get_access_token fail", "err")
            return None
        except Exception as e:
            log(f"[{self.nick}] ❌ get_access_token: {e}", "err")
            return None

    def refresh_token(self):
        access_token = self.get_access_token()
        if not access_token:
            return False

        try:
            login_headers = {
                "content-type": "application/json",
                "origin": "https://lc79b.bet",
                "referer": "https://lc79b.bet/",
                "user-agent": "Mozilla/5.0",
                "connection": "close",
            }
            with requests.Session() as s:
                res = s.post(
                    LOGIN_URL,
                    headers=login_headers,
                    json={"nickName": self.nick, "accessToken": access_token},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
            data = res.json()
            new_token = data.get("token")
            if new_token:
                self.jwt = new_token
                self.jwt_exp = self.parse_jwt_exp(new_token)
                remain = int((self.jwt_exp - time.time()) / 60) if self.jwt_exp else 0
                log(f"[{self.nick}] ✅ JWT OK (còn {remain} phút)", "ok")
                self.fetch_balance()
                return True
            log(f"[{self.nick}] ❌ login fail", "err")
            return False
        except Exception as e:
            log(f"[{self.nick}] ❌ refresh_token: {e}", "err")
            return False

    def redeem_code(self, code, _retry=False, _attempt=0):
        with self.lock:
            if code in self.used_codes:
                return
            self.used_codes.add(code)

        if not self.is_token_valid():
            log(f"[{self.nick}] 🔄 Token hết hạn → login lại", "warn")
            if not self.refresh_token():
                log(f"[{self.nick}] ❌ Không login được → bỏ qua {code}", "err")
                STATE["stats"]["fail"] += 1
                return

        log(f"[{self.nick}] 🚀 Redeem: {code}", "info")

        try:
            with requests.Session() as s:
                res = s.post(
                    BASE_URL + PARAMS,
                    headers=self._headers(self.jwt),
                    json={"code": code.strip(), "captcha": ""},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )

            try:
                data = res.json()
            except Exception:
                data = {}

            if res.status_code == 401:
                log(f"[{self.nick}] 🔑 Token hết hạn giữa chừng → login lại", "warn")
                if self.refresh_token():
                    with self.lock:
                        self.used_codes.discard(code)
                    return self.redeem_code(code, _retry=True)
                STATE["stats"]["fail"] += 1
                return

            if res.status_code in [200, 201]:
                amount = (
                    data.get("money")
                    or data.get("amount")
                    or data.get("reward")
                    or (data.get("data") or {}).get("money")
                )
                if amount:
                    log(f"[{self.nick}] 💰 SUCCESS: +{int(amount):,}", "ok")
                else:
                    log(f"[{self.nick}] ✅ SUCCESS", "ok")
                STATE["stats"]["success"] += 1
                self.fetch_balance()
            elif res.status_code == 400:
                msg = data.get("message") if isinstance(data, dict) else res.text
                log(f"[{self.nick}] ❌ FAIL (400): {msg}", "err")
                STATE["stats"]["fail"] += 1
            elif res.status_code == 428:
                log(f"[{self.nick}] ⚠️ CAPTCHA", "warn")
                STATE["stats"]["fail"] += 1
            elif res.status_code == 429:
                log(f"[{self.nick}] ⚠️ RATE LIMIT", "warn")
                STATE["stats"]["fail"] += 1
            else:
                log(f"[{self.nick}] ❌ FAIL ({res.status_code})", "err")
                STATE["stats"]["fail"] += 1
        except requests.exceptions.ConnectTimeout:
            log(f"[{self.nick}] 🔌 CONNECT TIMEOUT (lần {_attempt+1})", "err")
            if _attempt < MAX_RETRY:
                time.sleep(1 + _attempt * 1.0 + random.uniform(0, 0.5))
                return self.redeem_code(code, _retry=_retry, _attempt=_attempt+1)
            STATE["stats"]["fail"] += 1
        except requests.exceptions.ReadTimeout:
            log(f"[{self.nick}] ⏳ READ TIMEOUT (lần {_attempt+1})", "err")
            if _attempt < MAX_RETRY:
                time.sleep(1 + _attempt * 1.0 + random.uniform(0, 0.5))
                return self.redeem_code(code, _retry=_retry, _attempt=_attempt+1)
            STATE["stats"]["fail"] += 1
        except requests.exceptions.ConnectionError as e:
            log(f"[{self.nick}] 🔌 CONN ERROR (lần {_attempt+1}): {e}", "err")
            if _attempt < MAX_RETRY:
                time.sleep(1 + _attempt * 1.0 + random.uniform(0, 0.5))
                return self.redeem_code(code, _retry=_retry, _attempt=_attempt+1)
            STATE["stats"]["fail"] += 1
        except Exception as e:
            log(f"[{self.nick}] ❌ ERROR: {e}", "err")
            STATE["stats"]["fail"] += 1

# ==================== RUNTIME BOT CONTROL ====================
BOT = {"client": None, "loop": None, "thread": None, "account_objs": []}

def start_bot_worker(cfg):
    global BOT
    if STATE["running"]:
        return False, "Bot đang chạy"

    try:
        api_id = int(cfg["api_id"])
        api_hash = cfg["api_hash"]
        raw_accounts = cfg["accounts"]
        target_link = cfg.get("chat_link", TARGET_CHAT_LINK)
    except Exception as e:
        return False, f"Cấu hình lỗi: {e}"

    account_objs = [Account(a["un"], a["pw"], a["nick"]) for a in raw_accounts]
    BOT["account_objs"] = account_objs

    STATE["accounts"] = [{"un": a.un, "nick": a.nick, "ok": False, "balance": 0} for a in account_objs]
    STATE["stats"] = {"success": 0, "fail": 0, "codes": 0}
    STATE["running"] = True
    STATE["group_joined"] = False
    STATE["auth_step"] = "IDLE"
    PROCESSED_MSG_IDS.clear()

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        BOT["loop"] = loop
        loop.run_until_complete(_main_async(api_id, api_hash, target_link, account_objs))

    BOT["thread"] = threading.Thread(target=runner, daemon=True)
    BOT["thread"].start()
    return True, "Đã khởi động bot thành công"

async def _main_async(api_id, api_hash, target_link, account_objs):
    from telethon import TelegramClient, events, utils
    from telethon.tl.functions.channels import JoinChannelRequest

    async def interactive_phone():
        STATE["auth_step"] = "WAIT_PHONE"
        STATE["auth_prompt"] = "Vui lòng nhập Số điện thoại Telegram (ví dụ: +84912345678):"
        log("📱 Yêu cầu nhập Số điện thoại Telegram trên web...", "warn")
        while True:
            val = await asyncio.get_event_loop().run_in_executor(None, AUTH_INPUT_QUEUE.get)
            if val:
                STATE["auth_step"] = "IDLE"
                return val

    async def interactive_code():
        STATE["auth_step"] = "WAIT_CODE"
        STATE["auth_prompt"] = "Vui lòng nhập Mã OTP Telegram nhận được:"
        log("🔑 Yêu cầu nhập Mã OTP Telegram trên web...", "warn")
        while True:
            val = await asyncio.get_event_loop().run_in_executor(None, AUTH_INPUT_QUEUE.get)
            if val:
                STATE["auth_step"] = "IDLE"
                return val

    async def interactive_password():
        STATE["auth_step"] = "WAIT_PASSWORD"
        STATE["auth_prompt"] = "Vui lòng nhập Mật khẩu 2 lớp (Cloud Password):"
        log("🔒 Yêu cầu nhập Mật khẩu 2 lớp (2FA) Telegram trên web...", "warn")
        while True:
            val = await asyncio.get_event_loop().run_in_executor(None, AUTH_INPUT_QUEUE.get)
            if val:
                STATE["auth_step"] = "IDLE"
                return val

    client = TelegramClient(
        "session", api_id, api_hash,
        connection_retries=5,
        retry_delay=10,
        auto_reconnect=True,
        request_retries=5,
        flood_sleep_threshold=60,
    )
    BOT["client"] = client

    log("🔌 Đang kết nối Telegram thực tế...", "info")
    try:
        await client.start(
            phone=interactive_phone,
            code=interactive_code,
            password=interactive_password
        )
        me = await client.get_me()
        STATE["tg_user"] = f"@{me.username}" if me.username else f"{me.first_name} ({me.phone})"
        log(f"✅ Đăng nhập Telegram thành công: {STATE['tg_user']}", "ok")
    except Exception as e:
        log(f"❌ Telegram login fail: {e}", "err")
        STATE["running"] = False
        STATE["auth_step"] = "IDLE"
        return

    # Login các account LC79
    log(f"🔐 Đang làm mới token cho {len(account_objs)} tài khoản LC79...", "info")
    for i, a in enumerate(account_objs):
        success = a.refresh_token()
        STATE["accounts"][i]["ok"] = success
        STATE["accounts"][i]["balance"] = a.balance

    # Resolve chat link
    username = target_link.rstrip("/").split("/")[-1].lstrip("@")
    log(f"🔍 Resolve chat @{username}...", "info")
    try:
        entity = await client.get_entity(username)
        global TARGET_CHAT_ID
        TARGET_CHAT_ID = utils.get_peer_id(entity)
        log(f"✅ Chat ID: {TARGET_CHAT_ID}", "ok")
        
        try:
            await client(JoinChannelRequest(entity))
            log(f"✅ Đã join group @{username}", "ok")
            STATE["group_joined"] = True
        except Exception as ex:
            if "USER_ALREADY_PARTICIPANT" in str(ex) or "already" in str(ex).lower():
                log(f"✅ Đã là thành viên group @{username}", "ok")
                STATE["group_joined"] = True
            else:
                log(f"⚠️ Join group warning: {ex}", "warn")
    except Exception as e:
        log(f"❌ Resolve chat fail: {e}", "err")
        STATE["running"] = False
        return

    @client.on(events.NewMessage)
    async def handler(event):
        if not STATE["running"] or TARGET_CHAT_ID is None:
            return
        if event.chat_id != TARGET_CHAT_ID:
            return
        if event.id in PROCESSED_MSG_IDS:
            return
        PROCESSED_MSG_IDS.add(event.id)
        if len(PROCESSED_MSG_IDS) > 1000:
            PROCESSED_MSG_IDS.clear()

        text = event.raw_text or ""
        match = re.search(r"Mã code là:\s*([A-Z0-9]+)", text)
        if match:
            full_code = match.group(1)
            candidates = [full_code]
            if full_code.endswith("LC79"):
                candidates.append(full_code[:-4])
            for c in candidates:
                STATE["stats"]["codes"] += 1
                STATE["latest_code"] = c
                _redeem_all_concurrent(c, account_objs)
                # Cập nhật số dư lên state UI
                for idx, acc_item in enumerate(account_objs):
                    if idx < len(STATE["accounts"]):
                        STATE["accounts"][idx]["balance"] = acc_item.balance

    log("🚀 Bot đã sẵn sàng lắng nghe tin nhắn...", "ok")
    try:
        await client.run_until_disconnected()
    except Exception as e:
        log(f"⚠️ Bot dừng: {e}", "warn")
    finally:
        STATE["running"] = False
        STATE["auth_step"] = "IDLE"
        log("🛑 Bot đã dừng hoàn toàn", "warn")

def _redeem_all_concurrent(code, account_objs):
    log(f"🎯 CODE: {code} → Gửi đến {len(account_objs)} acc", "ok")
    def worker(acc, idx):
        if idx > 0:
            delay = idx * STAGGER_SEC + random.uniform(0, STAGGER_JITTER)
            time.sleep(delay)
        acc.redeem_code(code)

    threads = []
    for i, a in enumerate(account_objs):
        t = threading.Thread(target=worker, args=(a, i))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

def stop_bot_worker():
    STATE["running"] = False
    c = BOT.get("client")
    if c and BOT.get("loop"):
        async def _disconnect():
            try:
                await c.disconnect()
            except:
                pass
        try:
            asyncio.run_coroutine_threadsafe(_disconnect(), BOT["loop"])
        except:
            pass
    log("🛑 Yêu cầu dừng bot...", "warn")
    return True

# ==================== FLASK API ROUTES ====================
@app.route("/")
def index():
    return Response(HTML_TEMPLATE, mimetype="text/html; charset=utf-8")

@app.route("/api/state")
def api_state():
    return jsonify({
        "running": STATE["running"],
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
        return jsonify(ok=True, msg="Đã nhận giá trị xác thực")
    return jsonify(ok=False, msg="Giá trị không được trống"), 400

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
            return jsonify(ok=False, msg=f"Thiếu thông tin {k}"), 400
    ok, msg = start_bot_worker(cfg)
    return jsonify(ok=ok, msg=msg)

@app.route("/api/stop", methods=["POST"])
def api_stop():
    stop_bot_worker()
    return jsonify(ok=True, msg="Đã dừng bot")

@app.route("/api/redeem-code", methods=["POST"])
def api_redeem_code():
    if not STATE["running"]:
        return jsonify(ok=False, msg="Bot chưa khởi động"), 400
    code = (request.json or {}).get("code", "").strip().upper()
    if not code:
        return jsonify(ok=False, msg="Thiếu mã code"), 400
    
    STATE["latest_code"] = code
    STATE["stats"]["codes"] += 1
    
    def manual_run():
        _redeem_all_concurrent(code, BOT.get("account_objs", []))
    
    threading.Thread(target=manual_run, daemon=True).start()
    return jsonify(ok=True, msg=f"Đang redeem thủ công mã: {code}")

@app.route("/api/clear-logs", methods=["POST"])
def api_clear():
    STATE["logs"].clear()
    STATE["stats"] = {"success": 0, "fail": 0, "codes": 0}
    PROCESSED_MSG_IDS.clear()
    return jsonify(ok=True)

# ==================== HTML GIAO DIỆN WEB ====================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width,initial-scale=1.0" />
<title>LC79 Telegram Bot Web Dashboard</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:20px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0f1419;color:#e7e9ea}
.wrap{max-width:1280px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;padding:18px 22px;margin-bottom:18px;background:#1a2028;border:1px solid #262d36;border-radius:16px}
.logo-row{display:flex;align-items:center;gap:14px}
.logo-badge{width:46px;height:46px;border-radius:12px;background:linear-gradient(135deg,#2AABEE,#229ED9);display:flex;align-items:center;justify-content:center;font-size:22px}
.title-wrap h1{margin:0;font-size:18px;font-weight:700}
.title-wrap p{margin:2px 0 0;font-size:12.5px;color:#8b98a5}
.status-pill{display:flex;align-items:center;gap:8px;padding:8px 14px;border-radius:999px;font-size:13px;font-weight:600;background:#1f2937;border:1px solid #374151;color:#8b98a5}
.status-pill.on{background:rgba(34,158,217,.12);border-color:rgba(42,171,238,.5);color:#2AABEE}
.status-dot{width:8px;height:8px;border-radius:50%;background:#6b7280}
.status-pill.on .status-dot{background:#2AABEE;box-shadow:0 0 8px #2AABEE}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.btn{padding:10px 18px;border:none;border-radius:10px;font-size:14px;font-weight:600;cursor:pointer;color:#fff;transition:opacity .2s}
.btn:hover:not(:disabled){opacity:0.9}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-primary{background:#229ED9}
.btn-danger{background:#ef4444}
.btn-warn{background:#f59e0b}
.btn-ghost{background:transparent;border:1px solid #374151;color:#e7e9ea}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:18px}
.stat{background:#1a2028;border:1px solid #262d36;border-radius:14px;padding:14px 16px}
.stat .lbl{font-size:11.5px;color:#8b98a5;text-transform:uppercase}
.stat .val{font-size:20px;font-weight:700;margin-top:4px}
.stat.ok .val{color:#22c55e}
.stat.err .val{color:#ef4444}
.stat.info .val{color:#2AABEE}
.stat.warn .val{color:#f59e0b}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{background:#1a2028;border:1px solid #262d36;border-radius:16px;display:flex;flex-direction:column;overflow:hidden}
.panel-head{padding:14px 18px;border-bottom:1px solid #262d36;display:flex;align-items:center;justify-content:space-between;font-weight:600;font-size:14px}
.panel-body{padding:16px 18px;flex:1}
.field{margin-bottom:12px}
.field label{display:block;font-size:12px;color:#8b98a5;margin-bottom:5px}
.field input{width:100%;height:40px;padding:0 12px;font-size:14px;color:#e7e9ea;background:#0f1419;border:1px solid #2d3644;border-radius:8px;outline:none}
.field input:focus{border-color:#2AABEE}
.row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.acc-list{max-height:200px;overflow-y:auto;margin-top:8px}
.acc-item{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;margin-bottom:6px;background:#0f1419;border:1px solid #2d3644;border-radius:8px;font-size:13px}
.acc-item .bal{color:#22c55e;font-weight:600;font-size:12px}
.acc-item .badge{padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600;margin-left:6px}
.acc-item .badge.ok{background:rgba(34,197,94,.15);color:#22c55e}
.acc-item .badge.off{background:rgba(107,114,128,.15);color:#6b7280}
.empty{text-align:center;color:#6b7280;font-size:13px;padding:15px}
.divider{border-top:1px solid #262d36;margin:16px 0;padding-top:14px}
.manual-row{display:flex;gap:8px}
.manual-row input{flex:1;height:40px;padding:0 12px;background:#0f1419;border:1px solid #2d3644;border-radius:8px;color:#e7e9ea;outline:none}
.manual-row button{height:40px;padding:0 18px}
.log-body{padding:12px 16px;font-family:monospace;font-size:12.5px;line-height:1.6;height:480px;overflow-y:auto;background:#0a0e13}
.log-line{display:flex;gap:8px;padding:2px 0}
.log-line .t{color:#4b5563;flex-shrink:0}
.log-line.ok .msg{color:#22c55e}
.log-line.err .msg{color:#ef4444}
.log-line.warn .msg{color:#f59e0b}
.log-line.info .msg{color:#8b98a5}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(80px);padding:12px 22px;border-radius:10px;font-size:14px;background:#1f2937;border:1px solid #374151;color:#fff;transition:transform .3s;z-index:999}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.ok{border-color:#22c55e;background:#052e16;color:#86efac}
.toast.err{border-color:#ef4444;background:#450a0a;color:#fca5a5}
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;z-index:1000;opacity:0;pointer-events:none;transition:opacity .2s}
.modal-overlay.active{opacity:1;pointer-events:auto}
.modal-box{background:#1a2028;border:1px solid #374151;border-radius:16px;width:90%;max-width:400px;padding:24px}
.modal-box h3{margin:0 0 10px;font-size:18px;color:#2AABEE}
.modal-box p{margin:0 0 16px;font-size:13.5px;color:#8b98a5}
.modal-box input{width:100%;height:42px;padding:0 12px;background:#0f1419;border:1px solid #2d3644;border-radius:8px;color:#e7e9ea;font-size:15px;outline:none;margin-bottom:16px}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo-row">
      <div class="logo-badge">🤖</div>
      <div class="title-wrap">
        <h1>LC79 Telegram Bot Web Dashboard</h1>
        <p>Phiên bản Web kết hợp Telegram Real Login & Multi-Account LC79</p>
      </div>
    </div>
    <div class="status-pill" id="statusPill">
      <span class="status-dot"></span><span id="statusText">Offline</span>
    </div>
    <div class="actions">
      <button class="btn btn-primary" id="btnStart">▶ Bắt đầu</button>
      <button class="btn btn-danger" id="btnStop" disabled>■ Dừng</button>
    </div>
  </div>

  <div class="stats">
    <div class="stat ok"><div class="lbl">Thành công</div><div class="val" id="statOk">0</div></div>
    <div class="stat err"><div class="lbl">Thất bại</div><div class="val" id="statErr">0</div></div>
    <div class="stat info"><div class="lbl">Codes đã nhận</div><div class="val" id="statCodes">0</div></div>
    <div class="stat info"><div class="lbl">Telegram User</div><div class="val" id="statTgUser" style="font-size:14px">—</div></div>
    <div class="stat warn"><div class="lbl">Code gần nhất</div><div class="val" id="statLatest">—</div></div>
    <div class="stat info"><div class="lbl">Group Status</div><div class="val" id="statGroup" style="font-size:14px">—</div></div>
  </div>

  <div class="grid">
    <div class="panel">
      <div class="panel-head">⚙️ Cấu hình Bot</div>
      <div class="panel-body">
        <div class="row-2">
          <div class="field"><label>API ID</label><input id="apiId" type="number" value="12345678" /></div>
          <div class="field"><label>API Hash</label><input id="apiHash" type="text" value="abcdef1234567890abcdef1234567890" /></div>
        </div>
        <div class="field"><label>Chat Link Telegram</label><input id="chatLink" type="text" value="https://t.me/codelaucuagame" /></div>

        <div style="margin:16px 0 8px;font-weight:600;font-size:14px">👥 Danh sách Tài khoản LC79 (<span id="accCount">1</span>)</div>
        <div class="row-2">
          <div class="field"><label>Username</label><input id="accUn" type="text" placeholder="username" /></div>
          <div class="field"><label>Password (Hash)</label><input id="accPw" type="text" placeholder="password hash" /></div>
        </div>
        <div class="row-2">
          <div class="field"><label>Nickname</label><input id="accNick" type="text" placeholder="nickname" /></div>
          <div class="field" style="display:flex;align-items:flex-end"><button class="btn btn-ghost" id="btnAddAcc" style="width:100%;height:40px">+ Thêm tài khoản</button></div>
        </div>
        <div class="acc-list" id="accList"><div class="empty">Chưa có tài khoản</div></div>

        <div class="divider">
          <div style="font-weight:600;font-size:13px;margin-bottom:8px">🎯 Redeem Code Thủ Công</div>
          <div class="manual-row">
            <input id="manualCode" type="text" placeholder="Nhập mã code..." />
            <button class="btn btn-warn" id="btnRedeemManual">Gửi</button>
          </div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <span>📡 Live Log Trực Tiếp</span>
        <button class="btn btn-ghost" id="btnClear" style="padding:4px 10px;font-size:12px">Xoá log</button>
      </div>
      <div class="log-body" id="logBody">
        <div class="log-line info"><span class="t">--:--</span><span class="msg">Chờ khởi động hệ thống...</span></div>
      </div>
    </div>
  </div>
</div>

<!-- Modal Nhập OTP / Phone / 2FA Telegram -->
<div class="modal-overlay" id="authModal">
  <div class="modal-box">
    <h3 id="authTitle">Xác Thực Telegram</h3>
    <p id="authPromptText">Vui lòng nhập thông tin...</p>
    <input id="authInputVal" type="text" placeholder="Nhập vào đây..." autocomplete="off" />
    <button class="btn btn-primary" id="btnSendAuth" style="width:100%;height:40px">Xác nhận</button>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(() => {
  const $ = id => document.getElementById(id);
  const LS_KEY = 'lc79_web_cfg_v1';
  let localAccounts = [
    { un: "your_username", pw: "your_password_hash", nick: "your_nickname" }
  ];
  let state = { running: false, accounts: [] };
  let lastAuthStep = 'IDLE';

  function loadStorage() {
    try {
      const d = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
      if(d.apiId) $('apiId').value = d.apiId;
      if(d.apiHash) $('apiHash').value = d.apiHash;
      if(d.chatLink) $('chatLink').value = d.chatLink;
      if(Array.isArray(d.accounts) && d.accounts.length > 0) localAccounts = d.accounts;
    } catch(e){}
    renderAccounts();
  }
  function saveStorage() {
    localStorage.setItem(LS_KEY, JSON.stringify({
      apiId: $('apiId').value,
      apiHash: $('apiHash').value,
      chatLink: $('chatLink').value,
      accounts: localAccounts
    }));
  }
  ['apiId','apiHash','chatLink'].forEach(id => $(id).addEventListener('input', saveStorage));

  function toast(msg, type='info') {
    const el = $('toast');
    el.textContent = msg;
    el.className = 'toast show ' + type;
    setTimeout(() => el.className = 'toast', 3000);
  }

  function renderAccounts() {
    const list = $('accList');
    $('accCount').textContent = localAccounts.length;
    if(localAccounts.length === 0) {
      list.innerHTML = '<div class="empty">Chưa có tài khoản</div>';
      return;
    }
    list.innerHTML = localAccounts.map((a, i) => {
      const st = state.accounts[i];
      const online = st?.ok;
      const badge = online ? '<span class="badge ok">Online</span>' : '<span class="badge off">Offline</span>';
      const bal = online && st.balance !== undefined ? '<div>💰 Số dư: ' + Number(st.balance).toLocaleString() + ' đ</div>' : '<div style="color:#6b7280">💰 Số dư: —</div>';
      return '<div class="acc-item"><div><strong>' + a.nick + '</strong> ' + badge + '<div style="color:#8b98a5;font-size:11px">' + a.un + '</div>' + bal + '</div>' +
             '<button class="btn btn-danger" style="padding:4px 8px;font-size:11px" onclick="removeAcc(' + i + ')">Xoá</button></div>';
    }).join('');
  }
  window.removeAcc = i => {
    localAccounts.splice(i, 1);
    saveStorage(); renderAccounts();
  };

  $('btnAddAcc').addEventListener('click', () => {
    const un = $('accUn').value.trim();
    const pw = $('accPw').value.trim();
    const nick = $('accNick').value.trim();
    if(!un || !pw || !nick) return toast('Điền đủ thông tin tài khoản', 'err');
    localAccounts.push({un, pw, nick});
    $('accUn').value=''; $('accPw').value=''; $('accNick').value='';
    saveStorage(); renderAccounts();
    toast('Đã thêm tài khoản', 'ok');
  });

  $('btnStart').addEventListener('click', async () => {
    const apiId = $('apiId').value.trim();
    const apiHash = $('apiHash').value.trim();
    const chatLink = $('chatLink').value.trim();
    if(!apiId || !apiHash) return toast('Nhập đầy đủ API ID và Hash', 'err');
    if(localAccounts.length === 0) return toast('Cần ít nhất 1 tài khoản LC79', 'err');

    $('btnStart').disabled = true;
    try {
      const res = await fetch('/api/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({api_id: apiId, api_hash: apiHash, chat_link: chatLink, accounts: localAccounts})
      });
      const d = await res.json();
      toast(d.msg, d.ok ? 'ok' : 'err');
      if(!d.ok) $('btnStart').disabled = false;
    } catch(e) {
      toast('Lỗi kết nối server', 'err');
      $('btnStart').disabled = false;
    }
  });

  $('btnStop').addEventListener('click', async () => {
    try {
      await fetch('/api/stop', {method:'POST'});
      toast('Đã dừng bot', 'warn');
    } catch(e) {}
  });

  $('btnRedeemManual').addEventListener('click', async () => {
    const code = $('manualCode').value.trim();
    if(!code) return toast('Nhập mã code', 'err');
    try {
      const res = await fetch('/api/redeem-code', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({code})
      });
      const d = await res.json();
      toast(d.msg, 'ok');
      $('manualCode').value = '';
    } catch(e) { toast('Lỗi kết nối', 'err'); }
  });

  $('btnClear').addEventListener('click', async () => {
    $('logBody').innerHTML = '';
    await fetch('/api/clear-logs', {method:'POST'});
  });

  $('btnSendAuth').addEventListener('click', async () => {
    const val = $('authInputVal').value.trim();
    if(!val) return toast('Nhập giá trị yêu cầu', 'err');
    try {
      const res = await fetch('/api/auth-input', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({value: val})
      });
      const d = await res.json();
      if(d.ok) {
        $('authInputVal').value = '';
        $('authModal').classList.remove('active');
        toast('Đã gửi thông tin xác thực', 'ok');
      }
    } catch(e) {}
  });

  async function pollState() {
    try {
      const res = await fetch('/api/state');
      const d = await res.json();
      state = d;
      
      const pill = $('statusPill');
      if(d.running) {
        pill.classList.add('on');
        $('statusText').textContent = 'Online';
        $('btnStart').disabled = true;
        $('btnStop').disabled = false;
      } else {
        pill.classList.remove('on');
        $('statusText').textContent = 'Offline';
        $('btnStart').disabled = false;
        $('btnStop').disabled = true;
      }

      $('statOk').textContent = d.stats.success;
      $('statErr').textContent = d.stats.fail;
      $('statCodes').textContent = d.stats.codes;
      $('statLatest').textContent = d.latest_code || '—';
      $('statTgUser').textContent = d.tg_user || '—';
      $('statGroup').textContent = d.group_joined ? 'Đã Join' : (d.running ? 'Đang vào...' : '—');

      if(d.auth_step && d.auth_step !== 'IDLE') {
        if(lastAuthStep !== d.auth_step) {
          lastAuthStep = d.auth_step;
          $('authPromptText').textContent = d.auth_prompt;
          if(d.auth_step === 'WAIT_PHONE') $('authTitle').textContent = 'Nhập Số Điện Thoại Telegram';
          else if(d.auth_step === 'WAIT_CODE') $('authTitle').textContent = 'Nhập Mã OTP Telegram';
          else if(d.auth_step === 'WAIT_PASSWORD') $('authTitle').textContent = 'Nhập Mật Khẩu 2 Lớp (2FA)';
          $('authModal').classList.add('active');
          $('authInputVal').focus();
        }
      } else {
        if(lastAuthStep !== 'IDLE') {
          lastAuthStep = 'IDLE';
          $('authModal').classList.remove('active');
        }
      }
      renderAccounts();
    } catch(e) {}
  }
  setInterval(pollState, 2000);

  function appendLog(e) {
    const body = $('logBody');
    const line = document.createElement('div');
    line.className = 'log-line ' + (e.lvl || 'info');
    line.innerHTML = '<span class="t">' + e.t + '</span><span class="msg">' + escapeHtml(e.msg) + '</span>';
    body.appendChild(line);
    while(body.children.length > 250) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
  }
  function escapeHtml(str) {
    return str.replace(/[&<>'"]/g, tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[tag] || tag));
  }

  if(window.EventSource) {
    const es = new EventSource('/api/stream');
    es.onmessage = ev => {
      try { appendLog(JSON.parse(ev.data)); } catch(e){}
    };
  }

  loadStorage();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 LC79 Telegram Bot Web Dashboard đang chạy...")
    print("🌐 Truy cập trình duyệt tại: http://localhost:5000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)