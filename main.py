import os
import time
import threading
import logging
import requests
import re
import asyncio
import json
import hashlib
import base64
import uuid
import random
import concurrent.futures
from flask import Flask, Response, request, jsonify
from requests.adapters import HTTPAdapter

from telethon import TelegramClient, functions, events
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
    AuthRestartError,
)

# ============ CONFIG ============
PORT             = int(os.getenv("PORT", 10000))
GAME_API_URL     = os.getenv("GAME_API_URL", "https://apifo88daigia.tele68.com/api")
LOGIN_URL        = os.getenv("LOGIN_URL", "https://wlb.tele68.com/v1/lobby/auth/login")
REDEEM_BASE_URL  = os.getenv("REDEEM_BASE_URL", "https://gameapi.tele68.com")
REDEEM_PARAMS    = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="
PERSIST_FILE     = os.getenv("PERSIST_FILE", "sessions_store.json")
PROXY_FILE       = os.getenv("PROXY_FILE", "proxies.txt")

# ===== SMART POLL CONFIG =====
POLL_INTERVAL_IDLE = float(os.getenv("POLL_INTERVAL_IDLE", "5.0"))
POLL_INTERVAL_FAST = float(os.getenv("POLL_INTERVAL_FAST", "0.5"))
POLL_FAST_DURATION = 30
FAST_POLL_BATCH    = int(os.getenv("FAST_POLL_BATCH", "10"))

SERVER_START_TIME = time.time()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger("AutoTool")
flask_app = Flask(__name__)

# ================================================================
#              ASYNC LOOP MANAGER
# ================================================================
class AsyncLoopManager:
    def __init__(self):
        self.loop, self.thread, self._started = None, None, False
    def start(self):
        if self._started: return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="AsyncLoop")
        self.thread.start()
        self._started = True
    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
    def run(self, coro, timeout=60):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)
    def submit_task(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

async_manager = AsyncLoopManager()

# ================================================================
#          AUTO PROXY MANAGER
# ================================================================
PROXIES = []
PROXY_LOCK = threading.Lock()
PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt"
]
PROXY_TEST_URL = "http://httpbin.org/ip"

def _load_local_proxies():
    global PROXIES
    if os.path.exists(PROXY_FILE):
        with open(PROXY_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if "://" not in line: line = "http://" + line
                    PROXIES.append(line)

def _fetch_raw_proxies():
    proxies = set()
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                for line in res.text.strip().split('\n'):
                    line = line.strip()
                    if line and ":" in line: proxies.add(line)
        except: pass
    return list(proxies)

def _check_proxy(proxy):
    proxy_dict = {"http": f"http://{proxy}", "https://": f"http://{proxy}"}
    try:
        res = requests.get(PROXY_TEST_URL, proxies=proxy_dict, timeout=5)
        if res.status_code == 200: return f"http://{proxy}"
    except: pass
    return None

def proxy_updater_loop():
    time.sleep(5)
    while True:
        try:
            raw = _fetch_raw_proxies()
            if raw:
                good = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
                    futures = {executor.submit(_check_proxy, p): p for p in raw}
                    for future in concurrent.futures.as_completed(futures):
                        p = future.result()
                        if p: good.append(p)
                
                if good:
                    with PROXY_LOCK:
                        PROXIES.clear()
                        PROXIES.extend(good[:500]) # Lấy top 500
                    with open(PROXY_FILE, "w") as f:
                        for p in PROXIES: f.write(p + "\n")
                    logger.info(f"🎉 Đã cập nhật {len(PROXIES)} proxy sống.")
        except Exception as e:
            logger.error(f"Proxy Error: {e}")
        time.sleep(1800) # 30 phút

# ================================================================
#                     API LOGGER (JSON CONSOLE)
# ================================================================
class ApiLogger:
    def __init__(self, max_entries=200):
        self.entries, self.lock = [], threading.Lock()
        self.max_entries = max_entries
    def log(self, level, method, url, status, req_body=None, res_body=None):
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "method": method,
            "url": url,
            "status": status,
            "request": req_body,
            "response": res_body
        }
        with self.lock:
            self.entries.append(entry)
            if len(self.entries) > self.max_entries: self.entries = self.entries[-self.max_entries:]
    def get_logs(self):
        with self.lock: return list(reversed(self.entries))

api_logger = ApiLogger()

# ================================================================
#                     GAME ACCOUNT + MANAGER
# ================================================================
def parse_jwt_exp(token):
    try:
        parts = token.split(".")
        if len(parts) < 2: return 0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except: return 0

class GameAccount:
    def __init__(self, username, pw_hash, nickname, vin=0, xu=0):
        self.username, self.pw_hash, self.nickname = username, pw_hash, nickname
        self.vin, self.xu = vin, xu
        self.access_token, self.jwt, self.jwt_exp = None, None, 0
        self._refresh_lock = threading.Lock()
    def is_jwt_valid(self):
        return bool(self.jwt and self.jwt_exp and time.time() < (self.jwt_exp - 60))

class GameAccountManager:
    def __init__(self):
        self.accounts, self.lock = {}, threading.Lock()
    def get_or_create(self, username, pw_hash, nickname=None, vin=0, xu=0):
        with self.lock:
            if username in self.accounts:
                acc = self.accounts[username]
                if acc.pw_hash != pw_hash:
                    acc.pw_hash, acc.access_token, acc.jwt, acc.jwt_exp = pw_hash, None, None, 0
                return acc
            acc = GameAccount(username, pw_hash, nickname or username, vin, xu)
            self.accounts[username] = acc
            return acc
    def get(self, username):
        with self.lock: return self.accounts.get(username)
    
    def refresh_account_info(self, account):
        """Lấy lại Vin, Xu và Token"""
        try:
            resp = requests.get(GAME_API_URL, params={"c": "3", "un": account.username, "pw": account.pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""}, timeout=10)
            body = resp.json()
            api_logger.log("info", "GET", GAME_API_URL, resp.status_code, {"username": account.username}, body)
            
            if body.get("success"):
                user_info = json.loads(base64.b64decode(body.get("sessionKey", "")).decode()) if body.get("sessionKey") else {}
                account.vin = user_info.get("vinTotal", 0)
                account.xu = user_info.get("xuTotal", 0)
                account.access_token = body.get("accessToken") or body.get("access_token")
                return {"status": "ok", "vin": account.vin, "xu": account.xu}
            return {"status": "error", "message": "Login game thất bại"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def refresh_jwt(self, account):
        if not account._refresh_lock.acquire(timeout=20): return {"status": "error", "message": "Đang xử lý"}
        try:
            if not account.access_token:
                self.refresh_account_info(account)
            
            h = {"content-type": "application/json", "origin": "https://lc79b.bet", "referer": "https://lc79b.bet/"}
            p = {"nickName": account.nickname, "accessToken": account.access_token}
            resp = requests.post(LOGIN_URL, headers=h, json=p, timeout=10)
            body = resp.json()
            api_logger.log("info", "POST", LOGIN_URL, resp.status_code, p, body)
            
            token = (body.get("token") or body.get("jwt") or (body.get("data") or {}).get("token"))
            if token:
                account.jwt = token
                account.jwt_exp = parse_jwt_exp(token)
                return {"status": "ok", "exp": account.jwt_exp}
            return {"status": "error", "message": "Lỗi lấy JWT"}
        except Exception as e:
            return {"status": "error", "message": str(e)[:50]}
        finally:
            account._refresh_lock.release()

account_manager = GameAccountManager()

# ================================================================
#                     REDEEM ENGINE
# ================================================================
_adapter = HTTPAdapter(pool_connections=1000, pool_maxsize=1000, max_retries=0)
_fast_session = requests.Session()
_fast_session.mount("https://", _adapter)
_fast_session.mount("http://", _adapter)

def _single_redeem(account, code):
    if not account.jwt: return {"status": "error", "http": 0, "message": "no JWT"}
    headers = {"content-type": "application/json", "authorization": f"Bearer {account.jwt}"}
    proxy_dict = None
    with PROXY_LOCK:
        if PROXIES:
            p = random.choice(PROXIES)
            proxy_dict = {"http": p, "https": p}
    
    url = REDEEM_BASE_URL + REDEEM_PARAMS
    payload = {"code": code, "captcha": ""}
    try:
        resp = _fast_session.post(url, headers=headers, json=payload, proxies=proxy_dict, timeout=(3, 8))
        body = resp.json()
        api_logger.log("success" if resp.status_code == 200 else "error", "POST", url, resp.status_code, payload, body)
        return {"status": "ok" if resp.status_code == 200 else "error", "http": resp.status_code, "body": body, "code": code}
    except Exception as e:
        api_logger.log("error", "POST", url, 0, payload, str(e))
        return {"status": "error", "http": 0, "message": str(e)}

def redeem_code_fast(account, candidates):
    if not account.is_jwt_valid():
        if account_manager.refresh_jwt(account).get("status") != "ok":
            return {"status": "error", "http": 0, "message": "JWT invalid"}
    return _single_redeem(account, candidates[0])

# ================================================================
#                     SESSION STORAGE
# ================================================================
sessions, PERSIST_LOCK, SESSIONS_LOCK = {}, threading.Lock(), threading.Lock()

def _add_auto_event(session_id, event, is_success=False):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if s:
        s.setdefault("auto_events", []).append(event)
        if is_success:
            s.setdefault("success_history", []).insert(0, event) # Lưu code thành công lên đầu
            if len(s["success_history"]) > 50: s["success_history"] = s["success_history"][:50]
        if len(s["auto_events"]) > 100: s["auto_events"] = s["auto_events"][-100:]

def _auto_redeem_worker(session_id, variants, raw_code, msg_ts):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
        if not s or not s.get("game_username"): return
        redeemed = s.setdefault("redeemed_codes", [])
        if any(v in redeemed for v in variants): return
        for v in variants: redeemed.append(v)
    
    account = account_manager.get(s.get("game_username"))
    if not account: return
    
    code = variants[0]
    result = redeem_code_fast(account, variants)
    is_ok = result.get("http") in (200, 201)
    body = result.get("body") or {}
    amount = body.get("money") or body.get("amount") if isinstance(body, dict) else None

    event = {
        "ts": time.time(), "code": code, "http": result.get("http"),
        "status": "success" if is_ok else "fail", "amount": amount,
        "message": body.get("message") if isinstance(body, dict) else result.get("message"),
        "is_proxy": bool(PROXIES)
    }
    _add_auto_event(session_id, event, is_success=is_ok)

# ================================================================
#                     SMART POLL & AUTO WATCH
# ================================================================
async def _smart_poll_loop(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s: return
    client, channel = s["client"], s.get("auto_watch_channel")
    last_seen_id = 0
    try: 
        msgs = await client.get_messages(channel, limit=1)
        if msgs: last_seen_id = msgs[0].id
    except: pass

    fast_mode_until = 0
    while True:
        with SESSIONS_LOCK:
            if not sessions.get(session_id) or not sessions[session_id].get("auto_watch_handler"): break
        if not client.is_connected():
            try: await client.connect()
            except: await asyncio.sleep(5); continue

        try:
            msgs = await client.get_messages(channel, limit=10, min_id=last_seen_id)
            if msgs:
                fast_mode_until = time.time() + 30
                for msg in reversed(msgs):
                    last_seen_id = max(last_seen_id, msg.id)
                    for raw in re.findall(r'LC79[A-Z0-9]{3,}', msg.message or ""):
                        threading.Thread(target=_auto_redeem_worker, args=(session_id, [raw.replace("LC79", "")], raw, time.time()), daemon=True).start()
        except: pass
        await asyncio.sleep(0.5 if time.time() < fast_mode_until else 5.0)

async def _start_auto_watch(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s: return {"status": "error"}
    client = s["client"]
    if not client.is_connected(): await client.connect()

    channel = await client.get_entity('codelaucuagame')
    try: await client(functions.channels.JoinChannelRequest(channel=channel))
    except: pass

    async def on_new_msg(event):
        for raw in re.findall(r'LC79[A-Z0-9]{3,}', event.raw_text or ""):
            threading.Thread(target=_auto_redeem_worker, args=(session_id, [raw.replace("LC79", "")], raw, time.time()), daemon=True).start()

    client.add_event_handler(on_new_msg, events.NewMessage(chats=[channel]))
    s["auto_watch_handler"], s["auto_watch_channel"] = on_new_msg, channel
    s["smart_poll_task"] = async_manager.submit_task(_smart_poll_loop(session_id))
    return {"status": "ok"}

# ================================================================
#                     BACKGROUND WORKERS
# ================================================================
def jwt_prewarm_loop():
    """Tự động gia hạn Token ngầm khi sắp hết hạn (dưới 5 phút)"""
    time.sleep(20)
    while True:
        try:
            time.sleep(45)
            with SESSIONS_LOCK: active = [sid for sid, s in sessions.items() if s.get("auto_watch_handler")]
            for sid in active:
                with SESSIONS_LOCK: s = sessions.get(sid)
                acc = account_manager.get(s.get("game_username")) if s else None
                if acc and acc.jwt_exp and (acc.jwt_exp - time.time()) < 300:
                    logger.info(f"🔄 Auto-refreshing JWT cho {acc.username}")
                    account_manager.refresh_jwt(acc)
        except: pass

# ================================================================
#                     API ENDPOINTS
# ================================================================
@flask_app.route("/")
def index():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base_dir, 'index.html'), 'r', encoding='utf-8') as f:
        return Response(f.read(), mimetype="text/html")

@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    data = request.json
    async def _do():
        client = TelegramClient(StringSession(), int(data["api_id"]), data["api_hash"], auto_reconnect=True)
        await client.connect()
        sent = await client.send_code_request(data["phone"])
        sid = str(uuid.uuid4())
        with SESSIONS_LOCK:
            sessions[sid] = {"client": client, "phone": data["phone"], "hash": sent.phone_code_hash, "api_id": data["api_id"], "api_hash": data["api_hash"], "redeemed_codes": [], "auto_events": [], "success_history": []}
        return {"status": "ok", "session_id": sid}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json
    with SESSIONS_LOCK: s = sessions.get(data["session_id"])
    async def _do():
        try:
            await s["client"].sign_in(phone=s["phone"], code=data["code"], phone_code_hash=s["hash"])
            token = str(uuid.uuid4())
            s["persist_token"] = token
            return {"status": "ok", "persist_token": token}
        except SessionPasswordNeededError: return {"status": "2fa"}
        except Exception as e: return {"status": "error", "message": str(e)}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json
    with SESSIONS_LOCK: s = sessions.get(data["session_id"])
    async def _do():
        try:
            await s["client"].sign_in(password=data["password"])
            token = str(uuid.uuid4())
            s["persist_token"] = token
            return {"status": "ok", "persist_token": token}
        except: return {"status": "error", "message": "Sai 2FA"}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/login_game", methods=["POST"])
def api_login_game():
    data = request.json
    try:
        pw_hash = hashlib.md5(data["password"].encode()).hexdigest()
        acc = account_manager.get_or_create(data["username"], pw_hash, data["username"])
        res = account_manager.refresh_account_info(acc)
        if res["status"] == "ok":
            with SESSIONS_LOCK:
                if data["session_id"] in sessions: sessions[data["session_id"]]["game_username"] = data["username"]
            return jsonify({"status": "ok", "username": acc.username, "vin": acc.vin, "xu": acc.xu})
        return jsonify(res)
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@flask_app.route("/api/reset_account", methods=["POST"])
def api_reset_account():
    sid = request.json.get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s or not s.get("game_username"): return jsonify({"status": "error"})
    
    acc = account_manager.get(s["game_username"])
    res = account_manager.refresh_account_info(acc)
    if res["status"] == "ok":
        account_manager.refresh_jwt(acc)
        return jsonify({"status": "ok", "vin": acc.vin, "xu": acc.xu})
    return jsonify(res)

@flask_app.route("/api/get_tokens", methods=["POST"])
def api_get_tokens():
    sid = request.json.get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s or not s.get("game_username"): return jsonify({"status": "error"})
    
    acc = account_manager.get(s["game_username"])
    account_manager.refresh_account_info(acc) # Lấy Access Token
    res = account_manager.refresh_jwt(acc)    # Lấy JWT
    
    if res["status"] == "ok":
        return jsonify({"status": "ok", "jwt_remaining": int((acc.jwt_exp - time.time())/60)})
    return jsonify(res)

@flask_app.route("/api/toggle_auto", methods=["POST"])
def api_toggle_auto():
    data = request.json
    if data.get("enabled"): return jsonify(async_manager.run(_start_auto_watch(data["session_id"]), 15))
    else:
        with SESSIONS_LOCK: s = sessions.get(data["session_id"])
        if s:
            if s.get("auto_watch_handler"): s["client"].remove_event_handler(s["auto_watch_handler"])
            if s.get("smart_poll_task"): s["smart_poll_task"].cancel()
            s["auto_watch_handler"], s["smart_poll_task"] = None, None
        return jsonify({"status": "ok"})

@flask_app.route("/api/status", methods=["POST"])
def api_status():
    sid = request.json.get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s: return jsonify({"status": "error"})
    
    acc = account_manager.get(s.get("game_username"))
    return jsonify({
        "status": "ok", "enabled": bool(s.get("auto_watch_handler")), 
        "jwt_remaining": int((acc.jwt_exp - time.time())/60) if acc and acc.jwt_exp else 0,
        "events": s.get("auto_events", [])[-20:],
        "success_history": s.get("success_history", [])[:20],
        "proxy_count": len(PROXIES)
    })

@flask_app.route("/api/logs", methods=["GET"])
def api_logs(): return jsonify(api_logger.get_logs())

@flask_app.route("/api/redeem_manual", methods=["POST"])
def api_redeem_manual():
    data = request.json
    with SESSIONS_LOCK: s = sessions.get(data["session_id"])
    if not s or not s.get("game_username"): return jsonify({"status": "error"})
    
    acc = account_manager.get(s["game_username"])
    code = data["code"].replace("LC79", "").strip()
    res = redeem_code_fast(acc, [code])
    
    is_ok = res.get("http") == 200
    if is_ok: _add_auto_event(data["session_id"], {"ts": time.time(), "code": code, "status": "success", "amount": res.get("body",{}).get("amount",0)}, True)
    return jsonify(res)

@flask_app.route("/health")
def health(): return Response("ok")

# ============ MAIN ============
if __name__ == "__main__":
    _load_local_proxies()
    async_manager.start()
    threading.Thread(target=proxy_updater_loop, daemon=True).start()
    threading.Thread(target=jwt_prewarm_loop, daemon=True).start()
    logger.info(f"🚀 Server khởi động tại port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)