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
WEBAPP_URL       = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
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
GLOBAL_DEDUP_TTL   = int(os.getenv("GLOBAL_DEDUP_TTL", 86400))

SERVER_START_TIME = time.time()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger("AutoTool")

# Không cần template_folder nữa
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
        logger.info("✅ AsyncLoopManager started")
    def _run(self):
        asyncio.set_event_loop(self.loop)
        try: self.loop.run_forever()
        except Exception as e: logger.error(f"AsyncLoop crash: {e}")
    def run(self, coro, timeout=60):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)
    def submit_task(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

async_manager = AsyncLoopManager()

# ================================================================
#          AUTO PROXY MANAGER (Tự động cào & Test Proxy)
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
        logger.info(f"🔌 Đã tải {len(PROXIES)} proxy từ {PROXY_FILE}")

def _fetch_raw_proxies():
    logger.info("🔄 Đang cào proxy mới từ Internet...")
    proxies = set()
    for url in PROXY_SOURCES:
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                for line in res.text.strip().split('\n'):
                    line = line.strip()
                    if line and ":" in line:
                        proxies.add(line)
        except: pass
    logger.info(f"✅ Tìm thấy {len(proxies)} proxy thô.")
    return list(proxies)

def _check_proxy(proxy):
    proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
    try:
        start = time.time()
        res = requests.get(PROXY_TEST_URL, proxies=proxy_dict, timeout=5)
        if res.status_code == 200:
            return f"http://{proxy}", int((time.time() - start) * 1000)
    except: pass
    return None, None

def proxy_updater_loop():
    time.sleep(5)
    while True:
        try:
            raw_proxies = _fetch_raw_proxies()
            if raw_proxies:
                logger.info(f"🔄 Đang test tốc độ {len(raw_proxies)} proxy...")
                good_proxies = []
                
                with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:
                    futures = {executor.submit(_check_proxy, p): p for p in raw_proxies}
                    for future in concurrent.futures.as_completed(futures):
                        p, latency = future.result()
                        if p: good_proxies.append((p, latency))
                
                good_proxies.sort(key=lambda x: x[1])
                top_proxies = [p[0] for p in good_proxies[:500]]

                if top_proxies:
                    with PROXY_LOCK:
                        PROXIES.clear()
                        PROXIES.extend(top_proxies)
                    
                    with open(PROXY_FILE, "w") as f:
                        f.write(f"# Auto-generated by main.py at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write(f"# Total Live Proxies: {len(good_proxies)}\n\n")
                        for p in top_proxies: f.write(p + "\n")
                    
                    logger.info(f"🎉 Đã cập nhật {len(top_proxies)} proxy sống vào hệ thống.")
                else:
                    logger.warning("⚠️ Không tìm thấy proxy sống nào, giữ nguyên proxy cũ.")
        except Exception as e:
            logger.error(f"Proxy Manager Err: {e}")
            
        time.sleep(1800) 

# ================================================================
#                     API DEBUG LOGGER
# ================================================================
class ApiLogger:
    def __init__(self, max_entries=500):
        self.entries, self.lock = [], threading.Lock()
        self.max_entries = max_entries
        self.stats = {"total": 0, "success": 0, "failed": 0, "total_elapsed": 0.0}
    def log(self, level, message, **extra):
        entry = {"ts": time.time(), "time": time.strftime("%H:%M:%S"), "level": level, "message": message}
        entry.update(extra)
        with self.lock:
            self.entries.append(entry)
            if len(self.entries) > self.max_entries: self.entries = self.entries[-self.max_entries:]
    def get_logs(self, limit=200):
        with self.lock: return list(self.entries[-limit:])
    def get_stats(self):
        with self.lock:
            s = dict(self.stats)
        s["avg_ms"] = int((s["total_elapsed"] / s["total"]) * 1000) if s["total"] > 0 else 0
        s.pop("total_elapsed", None)
        return s

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
        self.vin, self.xu, self.login_time = vin, xu, time.time()
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
                acc.login_time = time.time()
                return acc
            acc = GameAccount(username, pw_hash, nickname or username, vin, xu)
            self.accounts[username] = acc
            return acc
    def get(self, username):
        with self.lock: return self.accounts.get(username)
    def refresh_jwt(self, account):
        if not account._refresh_lock.acquire(timeout=20): return {"status": "error", "message": "Đang xử lý"}
        try:
            resp = requests.get(GAME_API_URL, params={"c": "3", "un": account.username, "pw": account.pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""}, timeout=10)
            body = resp.json()
            if not body.get("success"): return {"status": "error", "message": "Lỗi login game"}
            account.access_token = body.get("accessToken") or body.get("access_token")
            
            h = {"content-type": "application/json", "origin": "https://lc79b.bet", "referer": "https://lc79b.bet/"}
            p = {"nickName": account.nickname, "accessToken": account.access_token}
            resp2 = requests.post(LOGIN_URL, headers=h, json=p, timeout=10)
            body2 = resp2.json()
            token = (body2.get("token") or body2.get("jwt") or (body2.get("data") or {}).get("token"))
            if not token: return {"status": "error", "message": "Lỗi lấy JWT"}
            
            account.jwt = token
            account.jwt_exp = parse_jwt_exp(token)
            return {"status": "ok", "exp": account.jwt_exp}
        except Exception as e:
            return {"status": "error", "message": str(e)[:50]}
        finally:
            account._refresh_lock.release()

account_manager = GameAccountManager()

# ================================================================
#                     REDEEM ENGINE (ULTRA FAST)
# ================================================================
_adapter = HTTPAdapter(pool_connections=1000, pool_maxsize=1000, max_retries=0)
_fast_session = requests.Session()
_fast_session.mount("https://", _adapter)
_fast_session.mount("http://", _adapter)
_fast_session.headers.update({
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    "origin": "https://lc79.to",
    "referer": "https://lc79.to/",
    "accept": "application/json, text/plain, */*",
    "connection": "keep-alive"
})

def code_variants(raw):
    if not raw: return []
    raw = raw.strip().upper()
    if raw.startswith("LC79") and len(raw) > 4:
        s = raw[4:].strip()
        return [raw, s] if s else [raw]
    return [raw]

def code_variants_auto(raw):
    if not raw: return []
    raw = raw.strip().upper()
    if raw.startswith("LC79") and len(raw) > 4:
        s = raw[4:].strip()
        return [s] if s else [raw]
    return [raw]

def extract_code_candidates(text):
    if not text: return []
    out = []
    for m in re.findall(r'LC79[A-Z0-9]{3,}', text):
        if m not in out: out.append(m)
    for m in re.findall(r'Mã code là[:\s]*([A-Z0-9]+)', text, re.IGNORECASE):
        c = m.strip().upper()
        if c and c not in out: out.append(c)
    return out

def _single_redeem(account, code):
    if not account.jwt: return {"status": "error", "http": 0, "code": code, "message": "no JWT", "elapsed": 0}
    headers = {"content-type": "application/json", "authorization": f"Bearer {account.jwt}"}
    
    proxy_dict = None
    with PROXY_LOCK:
        if PROXIES:
            p = random.choice(PROXIES)
            proxy_dict = {"http": p, "https": p}
        
    start = time.time()
    try:
        resp = _fast_session.post(
            REDEEM_BASE_URL + REDEEM_PARAMS,
            headers=headers, json={"code": code, "captcha": ""},
            proxies=proxy_dict, timeout=(3, 8)
        )
        elapsed = time.time() - start
        try: body = resp.json()
        except: body = {"raw_text": resp.text[:200]}
        
        with api_logger.lock:
            api_logger.stats["total"] += 1
            if 200 <= resp.status_code < 300: api_logger.stats["success"] += 1
            else: api_logger.stats["failed"] += 1
            api_logger.stats["total_elapsed"] += elapsed
            
        return {"status": "ok" if 200 <= resp.status_code < 300 else "error", "http": resp.status_code, "body": body, "code": code, "elapsed": elapsed}
    except requests.exceptions.ProxyError:
        return {"status": "error", "http": 0, "code": code, "message": "proxy die", "elapsed": time.time() - start}
    except requests.exceptions.Timeout:
        return {"status": "error", "http": 0, "code": code, "message": "timeout", "elapsed": time.time() - start}
    except Exception as e:
        return {"status": "error", "http": 0, "code": code, "message": str(e)[:150], "elapsed": time.time() - start}

def redeem_code_fast(account, candidates):
    if not candidates: return {"status": "error", "http": 0, "message": "no candidates"}
    if not account.is_jwt_valid():
        r = account_manager.refresh_jwt(account)
        if r.get("status") != "ok":
            return {"status": "error", "http": 0, "message": "JWT invalid"}
    return _single_redeem(account, candidates[0])

# ================================================================
#                     SESSION STORAGE
# ================================================================
persistent_sessions, sessions = {}, {}
PERSIST_LOCK, SESSIONS_LOCK = threading.Lock(), threading.Lock()
_redeemed_global = {}

def _load_persist():
    global persistent_sessions
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f: persistent_sessions = json.load(f)
    except: persistent_sessions = {}

def _save_persist():
    try:
        with PERSIST_LOCK:
            data = dict(persistent_sessions)
        with open(PERSIST_FILE + ".tmp", "w") as f: json.dump(data, f)
        os.replace(PERSIST_FILE + ".tmp", PERSIST_FILE)
    except: pass

def _update_persist(token, **fields):
    if not token: return
    with PERSIST_LOCK:
        if token in persistent_sessions: persistent_sessions[token].update(fields)
    _save_persist()

def _add_auto_event(session_id, event):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if s:
        s.setdefault("auto_events", []).append(event)
        if len(s["auto_events"]) > 100: s["auto_events"] = s["auto_events"][-100:]

def _auto_redeem_worker(session_id, variants, raw_code, msg_ts):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
        if not s or not s.get("game_username"): return
        
        redeemed = s.setdefault("redeemed_codes", [])
        if any(v in redeemed for v in variants): return
        for v in variants: redeemed.append(v)
        if len(redeemed) > 2000: s["redeemed_codes"] = redeemed[-2000:]
    
    account = account_manager.get(s.get("game_username"))
    if not account: return
    if not account.is_jwt_valid():
        account_manager.refresh_jwt(account)
        if not account.is_jwt_valid(): return

    code_to_fire = variants[0]
    fire_ts = time.time()
    result = redeem_code_fast(account, variants)
    
    http = result.get("http", 0)
    is_ok = http in (200, 201)
    body = result.get("body") or {}
    amount = body.get("money") or body.get("amount") if isinstance(body, dict) else None
    msg = body.get("message") if isinstance(body, dict) else result.get("message")

    _add_auto_event(session_id, {
        "ts": fire_ts, "msg_ts": msg_ts, "code": code_to_fire, "raw_code": raw_code,
        "http": http, "elapsed_ms": int(result.get("elapsed",0)*1000),
        "delay_ms": int((fire_ts - msg_ts) * 1000),
        "status": "success" if is_ok else "fail", "amount": amount, "message": msg,
        "is_proxy": bool(PROXIES)
    })

# ================================================================
#                     SMART POLL & AUTO WATCH
# ================================================================
async def _smart_poll_loop(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s: return
    client, channel_entity = s["client"], s.get("auto_watch_channel")
    if not channel_entity: return
    
    last_seen_id = 0
    try:
        msgs = await client.get_messages(channel_entity, limit=1)
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
            msgs = await client.get_messages(channel_entity, limit=FAST_POLL_BATCH, min_id=last_seen_id)
            if msgs:
                fast_mode_until = time.time() + POLL_FAST_DURATION
                for msg in reversed(msgs):
                    last_seen_id = max(last_seen_id, msg.id)
                    for raw in extract_code_candidates(msg.message or ""):
                        variants = code_variants_auto(raw)
                        if variants:
                            threading.Thread(target=_auto_redeem_worker, args=(session_id, variants, raw, time.time()), daemon=True).start()
        except FloodWaitError as e: 
            await asyncio.sleep(min(int(e.seconds) + 1, 60)); continue
        except Exception: pass

        await asyncio.sleep(POLL_INTERVAL_FAST if time.time() < fast_mode_until else POLL_INTERVAL_IDLE)

async def _start_auto_watch(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s: return {"status": "error", "message": "Session invalid"}
    if s.get("auto_watch_handler"): return {"status": "ok", "message": "Already enabled"}
    
    client = s["client"]
    if not client.is_connected(): await client.connect()

    channel = await client.get_entity('codelaucuagame')
    try: await client(functions.channels.JoinChannelRequest(channel=channel))
    except: pass

    async def on_new_msg(event):
        try:
            for raw in extract_code_candidates(event.raw_text or ""):
                variants = code_variants_auto(raw)
                if variants:
                    threading.Thread(target=_auto_redeem_worker, args=(session_id, variants, raw, time.time()), daemon=True).start()
        except: pass

    client.add_event_handler(on_new_msg, events.NewMessage(chats=[channel]))
    s["auto_watch_handler"], s["auto_watch_channel"] = on_new_msg, channel
    s["smart_poll_task"] = async_manager.submit_task(_smart_poll_loop(session_id))
    logger.info(f"🎯 Auto-watch ON: {session_id[:8]}")
    return {"status": "ok"}

# ================================================================
#                     BACKGROUND WORKERS
# ================================================================
def jwt_prewarm_loop():
    time.sleep(20)
    while True:
        try:
            time.sleep(45)
            with SESSIONS_LOCK: active_ids = [sid for sid, s in sessions.items() if s.get("auto_watch_handler")]
            for sid in active_ids:
                with SESSIONS_LOCK: s = sessions.get(sid)
                if not s: continue
                acc = account_manager.get(s.get("game_username"))
                if acc and acc.jwt_exp and (acc.jwt_exp - time.time()) < 300:
                    account_manager.refresh_jwt(acc)
        except: pass

def session_keepalive_loop():
    time.sleep(30)
    while True:
        try:
            time.sleep(30)
            with SESSIONS_LOCK: active = [(sid, s) for sid, s in sessions.items() if s.get("auto_watch_handler")]
            for sid, s in active:
                if not s["client"].is_connected():
                    async_manager.run(s["client"].connect(), timeout=20)
        except: pass

# ================================================================
#                     API ENDPOINTS
# ================================================================
@flask_app.route("/")
def index():
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base_dir, 'index.html'), 'r', encoding='utf-8') as f:
            return Response(f.read(), mimetype="text/html")
    except Exception as e:
        return Response(f"Lỗi đọc file index.html: {e}", status=500)

@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    data = request.json or {}
    async def _do():
        try:
            client = TelegramClient(StringSession(), int(data["api_id"]), data["api_hash"], auto_reconnect=True, connection_retries=5)
            await client.connect()
            sent = await client.send_code_request(data["phone"])
            sid = str(uuid.uuid4())
            with SESSIONS_LOCK:
                sessions[sid] = {
                    "client": client, "phone": data["phone"], "phone_code_hash": sent.phone_code_hash, 
                    "api_id": int(data["api_id"]), "api_hash": data["api_hash"], "redeemed_codes": [], "auto_events": []
                }
            return {"status": "ok", "session_id": sid}
        except Exception as e: return {"status": "error", "message": str(e)}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json or {}
    with SESSIONS_LOCK: s = sessions.get(data.get("session_id"))
    if not s: return jsonify({"status": "error", "message": "Session invalid"})
    async def _do():
        try:
            await s["client"].sign_in(phone=s["phone"], code=data["code"], phone_code_hash=s["phone_code_hash"])
            token = str(uuid.uuid4())
            with PERSIST_LOCK:
                persistent_sessions[token] = {
                    "session_string": s["client"].session.save(), "api_id": s["api_id"], "api_hash": s["api_hash"], 
                    "phone": s["phone"], "redeemed_codes": [], "auto_enabled": False
                }
            _save_persist(); s["persist_token"] = token
            return {"status": "ok", "persist_token": token}
        except SessionPasswordNeededError: return {"status": "2fa"}
        except Exception as e: return {"status": "error", "message": str(e)}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json or {}
    with SESSIONS_LOCK: s = sessions.get(data.get("session_id"))
    if not s: return jsonify({"status": "error", "message": "Session invalid"})
    async def _do():
        try:
            await s["client"].sign_in(password=data["password"])
            token = str(uuid.uuid4())
            with PERSIST_LOCK:
                persistent_sessions[token] = {
                    "session_string": s["client"].session.save(), "api_id": s["api_id"], "api_hash": s["api_hash"], 
                    "phone": s["phone"], "redeemed_codes": [], "auto_enabled": False
                }
            _save_persist(); s["persist_token"] = token
            return {"status": "ok", "persist_token": token}
        except Exception: return {"status": "error", "message": "Sai 2FA"}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/login_game", methods=["POST"])
def api_login_game():
    data = request.json or {}
    try:
        pw_hash = hashlib.md5(data["password"].encode()).hexdigest()
        resp = requests.get(GAME_API_URL, params={"c": 3, "un": data["username"], "pw": pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""}, timeout=15).json()
        if not resp.get("success"): return jsonify({"status": "error", "message": "Sai tài khoản game"})
        
        user_info = json.loads(base64.b64decode(resp.get("sessionKey", "")).decode()) if resp.get("sessionKey") else {}
        acc = account_manager.get_or_create(data["username"], pw_hash, user_info.get("nickname", data["username"]), user_info.get("vinTotal", 0), user_info.get("xuTotal", 0))
        acc.access_token = resp.get("accessToken")
        
        if data.get("session_id"):
            with SESSIONS_LOCK:
                if data["session_id"] in sessions: sessions[data["session_id"]]["game_username"] = data["username"]
                
        if data.get("persist_token"):
            _update_persist(data["persist_token"], game_username=data["username"], game_pw_hash=pw_hash)
            
        return jsonify({"status": "ok", "username": acc.nickname, "balance": acc.vin, "xuTotal": acc.xu})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@flask_app.route("/api/auto_status", methods=["POST"])
def api_auto_status():
    with SESSIONS_LOCK: s = sessions.get((request.json or {}).get("session_id"))
    if not s: return jsonify({"status": "error"})
    acc = account_manager.get(s.get("game_username"))
    return jsonify({
        "status": "ok", "enabled": bool(s.get("auto_watch_handler")), 
        "events": s.get("auto_events", [])[-20:], 
        "jwt_remaining_minutes": int((acc.jwt_exp - time.time())/60) if acc and acc.jwt_exp else 0,
        "proxy_count": len(PROXIES)
    })

@flask_app.route("/api/toggle_auto", methods=["POST"])
def api_toggle_auto():
    data = request.json or {}
    if data.get("enabled"):
        try: return jsonify(async_manager.run(_start_auto_watch(data["session_id"]), 15))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})
    else:
        with SESSIONS_LOCK: s = sessions.get(data["session_id"])
        if s:
            if s.get("auto_watch_handler"): s["client"].remove_event_handler(s["auto_watch_handler"])
            if s.get("smart_poll_task"): s["smart_poll_task"].cancel()
            s["auto_watch_handler"], s["smart_poll_task"] = None, None
        return jsonify({"status": "ok"})

@flask_app.route("/api/redeem_manual", methods=["POST"])
def api_redeem_manual():
    data = request.json or {}
    with SESSIONS_LOCK: s = sessions.get(data.get("session_id"))
    if not s or not s.get("game_username"): return jsonify({"status": "error", "message": "Chưa login game"})
    
    acc = account_manager.get(s.get("game_username"))
    if not acc: return jsonify({"status": "error", "message": "Không tìm thấy account"})

    variants = code_variants(data.get("code", ""))
    result = redeem_code_fast(acc, variants)
    
    http = result.get("http", 0)
    is_ok = http in (200, 201)
    body = result.get("body") or {}
    amount = body.get("money") or body.get("amount") if isinstance(body, dict) else None
    msg = body.get("message") if isinstance(body, dict) else result.get("message")
    
    _add_auto_event(data["session_id"], {
        "ts": time.time(), "code": data.get("code"), "candidates": variants,
        "http": http, "elapsed_ms": int(result.get("elapsed",0)*1000),
        "status": "success" if is_ok else "fail", "amount": amount, "message": msg, "manual": True
    })
    return jsonify({**result, "amount": amount})

@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    sid = (request.json or {}).get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s: return jsonify({"status": "error"})
    async def _do():
        if not s["client"].is_connected(): await s["client"].connect()
        channel = s.get("auto_watch_channel") or await s["client"].get_entity('codelaucuagame')
        msgs = await s["client"].get_messages(channel, limit=10)
        items, seen = [], set()
        for msg in msgs:
            for raw in extract_code_candidates(msg.message or ""):
                v = code_variants_auto(raw)
                if v and v[0] not in seen:
                    seen.add(v[0]); items.append({"code": v[0], "raw": raw, "msg_date": msg.date.timestamp()})
        return {"status": "ok", "items": items[:10]}
    try: return jsonify(async_manager.run(_do(), 15))
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@flask_app.route("/api/force_scan", methods=["POST"])
def api_force_scan():
    sid = (request.json or {}).get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s or not s.get("game_username"): return jsonify({"status": "error", "message": "Lỗi session"})
    
    async def _do():
        if not s["client"].is_connected(): await s["client"].connect()
        channel = s.get("auto_watch_channel") or await s["client"].get_entity('codelaucuagame')
        msgs = await s["client"].get_messages(channel, limit=20)
        fired = 0
        for msg in msgs:
            for raw in extract_code_candidates(msg.message or ""):
                variants = code_variants_auto(raw)
                with SESSIONS_LOCK: redeemed = s.get("redeemed_codes", [])
                if any(v in redeemed for v in variants): continue
                threading.Thread(target=_auto_redeem_worker, args=(sid, variants, raw, time.time()), daemon=True).start()
                fired += 1
        return {"status": "ok", "fired": fired}
    try: return jsonify(async_manager.run(_do(), 20))
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@flask_app.route("/api/resume", methods=["POST"])
def api_resume():
    token = (request.json or {}).get("persist_token")
    with PERSIST_LOCK: p = persistent_sessions.get(token)
    if not p: return jsonify({"status": "not_found"})
    
    async def _do():
        client = TelegramClient(StringSession(p["session_string"]), p["api_id"], p["api_hash"], auto_reconnect=True, connection_retries=5)
        await client.connect()
        if not await client.is_user_authorized(): return {"status": "expired"}
        
        if p.get("game_username") and p.get("game_pw_hash"):
            account_manager.get_or_create(p["game_username"], p["game_pw_hash"])
            
        sid = str(uuid.uuid4())
        with SESSIONS_LOCK:
            sessions[sid] = {
                "client": client, "phone": p.get("phone"), "game_username": p.get("game_username"), 
                "persist_token": token, "redeemed_codes": list(p.get("redeemed_codes", [])), "auto_events": []
            }
        
        if p.get("auto_enabled") and p.get("game_username"):
            await _start_auto_watch(sid)
            
        return {"status": "ok", "session_id": sid, "game_username": p.get("game_username"), "auto_enabled": bool(p.get("auto_enabled"))}
    return jsonify(async_manager.run(_do(), 45))

@flask_app.route("/api/stats", methods=["GET"])
def api_stats(): return jsonify({"status": "ok", "stats": api_logger.get_stats()})

@flask_app.route("/health")
def health(): return Response("ok", mimetype="text/plain")

# ============ MAIN ============
if __name__ == "__main__":
    _load_local_proxies()
    _load_persist()
    
    async_manager.start()
    
    threading.Thread(target=proxy_updater_loop, daemon=True, name="ProxyUpdater").start()
    threading.Thread(target=jwt_prewarm_loop, daemon=True).start()
    threading.Thread(target=session_keepalive_loop, daemon=True).start()
    
    logger.info(f"🚀 Server khởi động tại port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)