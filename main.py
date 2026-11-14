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
PING_INTERVAL    = int(os.getenv("PING_INTERVAL", 10))
WEBAPP_URL       = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
GAME_API_URL     = os.getenv("GAME_API_URL", "https://apifo88daigia.tele68.com/api")
LOGIN_URL        = os.getenv("LOGIN_URL", "https://wlb.tele68.com/v1/lobby/auth/login")
REDEEM_BASE_URL  = os.getenv("REDEEM_BASE_URL", "https://gameapi.tele68.com")
REDEEM_PARAMS    = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="
PERSIST_FILE     = os.getenv("PERSIST_FILE", "sessions_store.json")
PROXY_FILE       = os.getenv("PROXY_FILE", "proxies.txt")

# ===== REAL-TIME AUTO-REDEEM CONFIG =====
FAST_POLL_INTERVAL = float(os.getenv("FAST_POLL_INTERVAL", "0.1"))  # 0.1s = 10Hz
FAST_POLL_BATCH    = int(os.getenv("FAST_POLL_BATCH", "20"))       # max msgs per poll
FAST_POLL_IDLE_BACKOFF = int(os.getenv("FAST_POLL_IDLE_BACKOFF", "200"))
GLOBAL_DEDUP_TTL   = int(os.getenv("GLOBAL_DEDUP_TTL", 86400))     # 24h

SERVER_START_TIME = time.time()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AutoTool")

flask_app = Flask(__name__)


# ================================================================
#              ASYNC LOOP MANAGER
# ================================================================
class AsyncLoopManager:
    def __init__(self):
        self.loop = None
        self.thread = None
        self._started = False

    def start(self):
        if self._started:
            return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="AsyncLoop")
        self.thread.start()
        self._started = True
        logger.info("✅ AsyncLoopManager started")

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        except Exception as e:
            logger.error(f"AsyncLoop crash: {e}")

    def run(self, coro, timeout=60):
        if not self._started or not self.loop:
            raise RuntimeError("AsyncLoopManager chưa start!")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def submit_task(self, coro):
        if not self._started or not self.loop:
            raise RuntimeError("AsyncLoopManager chưa start!")
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
        logger.info(f"🔌 Đã tải {len(PROXIES)} proxy từ file.")

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
    proxy_dict = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
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
#                     API DEBUG LOGGER
# ================================================================
class ApiLogger:
    SENSITIVE_KEYS = {
        "pw", "password", "pass", "pwd",
        "api_hash", "apihash", "code", "otp",
        "accesstoken", "access_token", "token", "jwt",
        "authorization", "sessionkey", "session_key",
        "session", "phone_code_hash", "phonecodehash",
    }

    def __init__(self, max_entries=200):
        self.entries = []
        self.lock = threading.Lock()
        self.max_entries = max_entries
        self.stats = {"total": 0, "success": 0, "failed": 0, "total_elapsed": 0.0}

    def _mask(self, s, show=6):
        if not s or not isinstance(s, str): return "***"
        if len(s) <= show * 2: return "***"
        return s[:show] + "..." + s[-show:]

    def _is_sensitive(self, key):
        kl = key.lower().replace("_", "").replace("-", "")
        return kl in self.SENSITIVE_KEYS

    def _redact_dict(self, data):
        if not isinstance(data, dict): return data
        out = {}
        for k, v in data.items():
            if self._is_sensitive(k):
                if k.lower() == "authorization": out[k] = "Bearer ***"
                elif k.lower() in ("pw", "password", "pass", "pwd", "code", "otp"): out[k] = "***"
                else: out[k] = self._mask(v) if isinstance(v, str) else "***"
            elif isinstance(v, dict): out[k] = self._redact_dict(v)
            elif isinstance(v, list): out[k] = [self._redact_dict(i) if isinstance(i, dict) else i for i in v]
            else: out[k] = v
        return out

    def _add(self, entry):
        with self.lock:
            self.entries.append(entry)
            if len(self.entries) > self.max_entries: self.entries = self.entries[-self.max_entries:]

    def log(self, level, message, **extra):
        entry = {"ts": time.time(), "time": time.strftime("%H:%M:%S"), "level": level, "message": message}
        entry.update(extra)
        self._add(entry)
        lvl = {"info": logging.INFO, "success": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO)
        logger.log(lvl, f"[{level.upper()}] {message}")

    def log_request(self, request_id, method, url, params=None, json_data=None, headers=None, account=None):
        payload = {"request_id": request_id, "method": method, "url": url}
        if params: payload["params"] = self._redact_dict(params)
        if json_data: payload["json"] = self._redact_dict(json_data)
        if headers:
            red = {k: ("Bearer ***" if k.lower() == "authorization" else v) for k, v in headers.items()}
            payload["headers"] = red
        if account: payload["account"] = account
        self.log("request", f"{method} {url}", **payload)

    def log_response(self, request_id, status, body=None, elapsed=0, account=None, error=None, headers=None):
        with self.lock:
            self.stats["total"] += 1
            if 200 <= status < 300: self.stats["success"] += 1
            else: self.stats["failed"] += 1
            self.stats["total_elapsed"] += elapsed
        payload = {"request_id": request_id, "status": status, "elapsed_ms": int(elapsed * 1000)}
        if account: payload["account"] = account
        if body is not None: payload["body"] = self._redact_dict(body) if isinstance(body, dict) else body
        if error: payload["error"] = error
        level = "response" if 200 <= status < 300 else "error"
        self.log(level, f"HTTP {status} ({int(elapsed*1000)}ms)", **payload)

    def get_logs(self, limit=100):
        with self.lock: return list(self.entries[-limit:])

    def clear(self):
        with self.lock:
            self.entries = []
            self.stats = {"total": 0, "success": 0, "failed": 0, "total_elapsed": 0.0}

    def get_stats(self):
        with self.lock:
            s = dict(self.stats)
        total = s["total"]
        s["avg_ms"] = int((s["total_elapsed"] / total) * 1000) if total > 0 else 0
        s.pop("total_elapsed", None)
        return s


# ================================================================
#                       API CLIENT
# ================================================================
class ApiClient:
    def __init__(self, logger_, connect_timeout=5, read_timeout=30, max_retries=2):
        self.logger = logger_
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_retries = max_retries
        self.session = requests.Session()

    def request(self, method, url, params=None, json_data=None, headers=None, account=None, retry=True):
        rid = str(uuid.uuid4())[:8]
        start = time.time()
        self.logger.log_request(request_id=rid, method=method, url=url, params=params, json_data=json_data, headers=headers, account=account)
        attempt = 0
        last_error = None
        max_attempts = (self.max_retries + 1) if retry else 1
        while attempt < max_attempts:
            try:
                resp = self.session.request(method=method, url=url, params=params, json=json_data, headers=headers, timeout=(self.connect_timeout, self.read_timeout))
                elapsed = time.time() - start
                try: body = resp.json()
                except Exception: body = None
                self.logger.log_response(request_id=rid, status=resp.status_code, body=body if body is not None else (resp.text[:300] or None), elapsed=elapsed, account=account, headers=dict(resp.headers))
                return {"ok": 200 <= resp.status_code < 300, "status": resp.status_code, "body": body, "text": resp.text, "headers": dict(resp.headers), "elapsed": elapsed, "request_id": rid}
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.logger.log("warning", f"Network error (attempt {attempt+1}/{max_attempts})", request_id=rid, account=account)
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.logger.log("error", f"Request exception: {last_error}", request_id=rid, account=account)
                break
            attempt += 1
            if attempt < max_attempts: time.sleep(0.6 * attempt)
        return {"ok": False, "status": 0, "body": None, "text": "", "error": last_error or "unknown error", "request_id": rid, "elapsed": time.time() - start}

    def get(self, url, **kw): return self.request("GET", url, **kw)
    def post(self, url, **kw): return self.request("POST", url, **kw)


# ================================================================
#                     GAME ACCOUNT + MANAGER
# ================================================================
def parse_jwt_exp(token):
    try:
        parts = token.split(".")
        if len(parts) < 2: return 0
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception: return 0

class GameAccount:
    def __init__(self, username, pw_hash, nickname, vin=0, xu=0):
        self.username = username
        self.pw_hash = pw_hash
        self.nickname = nickname
        self.vin = vin
        self.xu = xu
        self.login_time = time.time()
        self.access_token = None
        self.access_token_time = 0.0
        self.jwt = None
        self.jwt_exp = 0
        self._refresh_lock = threading.Lock()

    def is_jwt_valid(self):
        if not self.jwt or not self.jwt_exp: return False
        return time.time() < (self.jwt_exp - 60)

    def jwt_remaining_minutes(self):
        if not self.jwt_exp: return 0
        return max(0, int((self.jwt_exp - time.time()) / 60))

class GameAccountManager:
    def __init__(self, api_client, logger_):
        self.accounts = {}
        self.lock = threading.Lock()
        self.api_client = api_client
        self.logger = logger_

    def get_or_create(self, username, pw_hash, nickname=None, vin=0, xu=0):
        with self.lock:
            if username in self.accounts:
                acc = self.accounts[username]
                if acc.pw_hash != pw_hash:
                    acc.pw_hash = pw_hash
                    acc.access_token = None
                    acc.access_token_time = 0.0
                    acc.jwt = None
                    acc.jwt_exp = 0
                if nickname: acc.nickname = nickname
                if vin: acc.vin = vin
                if xu: acc.xu = xu
                acc.login_time = time.time()
                return acc
            acc = GameAccount(username, pw_hash, nickname or username, vin, xu)
            self.accounts[username] = acc
            return acc

    def get(self, username):
        with self.lock: return self.accounts.get(username)

    def refresh_account_info(self, account):
        self.logger.log("info", "Cập nhật thông tin tài khoản", account=account.username)
        try:
            resp = self.api_client.get(
                GAME_API_URL,
                params={"c": "3", "un": account.username, "pw": account.pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""},
                headers={"User-Agent": "Mozilla/5.0"},
                account=account.username,
            )
            body = resp.get("body") or {}
            if not body.get("success"): return {"status": "error", "message": body.get("message", "Lỗi login game")}
            
            user_info = json.loads(base64.b64decode(body.get("sessionKey", "")).decode()) if body.get("sessionKey") else {}
            account.nickname = user_info.get("nickname") or account.username
            account.vin = user_info.get("vinTotal", 0)
            account.xu = user_info.get("xuTotal", 0)
            account.access_token = body.get("accessToken") or body.get("access_token")
            account.access_token_time = time.time()
            self.logger.log("success", "Đã cập nhật thông tin", account=account.username)
            return {"status": "ok", "vin": account.vin, "xu": account.xu}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _fetch_access_token(self, account):
        self.logger.log("info", "Bắt đầu lấy Access Token", account=account.username)
        resp = self.api_client.get(
            GAME_API_URL,
            params={"c": "3", "un": account.username, "pw": account.pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            account=account.username,
        )
        if not resp["ok"]: return {"status": "error", "message": resp.get("error") or f"HTTP {resp['status']}"}
        body = resp.get("body") or {}
        if not body.get("success"): return {"status": "error", "message": body.get("message") or "API không trả success"}
        token = body.get("accessToken") or body.get("access_token")
        if not token: return {"status": "error", "message": "Response thiếu accessToken"}
        account.access_token = token
        account.access_token_time = time.time()
        self.logger.log("success", "Đã lấy Access Token", account=account.username)
        return {"status": "ok"}

    def _fetch_jwt(self, account):
        self.logger.log("info", "Bắt đầu login JWT", account=account.username)
        headers = {"content-type": "application/json", "origin": "https://lc79b.bet", "referer": "https://lc79b.bet/", "user-agent": "Mozilla/5.0"}
        payload = {"nickName": account.nickname, "accessToken": account.access_token}
        resp = self.api_client.post(LOGIN_URL, headers=headers, json_data=payload, account=account.username)
        if not resp["ok"]: return {"status": "error", "message": resp.get("error") or f"HTTP {resp['status']}"}
        body = resp.get("body") or {}
        token = (body.get("token") or body.get("jwt") or (body.get("data") or {}).get("token") or (body.get("data") or {}).get("jwt") or (body.get("data") or {}).get("accessToken"))
        if not token: return {"status": "error", "message": "Không xác định được trường JWT."}
        account.jwt = token
        account.jwt_exp = parse_jwt_exp(token)
        self.logger.log("success", f"Đã có JWT (còn ~{account.jwt_remaining_minutes()} phút)", account=account.username)
        return {"status": "ok", "exp": account.jwt_exp, "expires_in_minutes": account.jwt_remaining_minutes()}

    def get_access_token(self, account):
        if not account._refresh_lock.acquire(timeout=15): return {"status": "error", "message": "Đang xử lý"}
        try: return self._fetch_access_token(account)
        finally: account._refresh_lock.release()

    def get_jwt(self, account, force=False):
        if not force and account.is_jwt_valid(): return {"status": "ok", "cached": True}
        if not account._refresh_lock.acquire(timeout=20): return {"status": "error", "message": "Đang xử lý"}
        try:
            if not force and account.is_jwt_valid(): return {"status": "ok", "cached": True}
            if not account.access_token:
                res = self._fetch_access_token(account)
                if res["status"] != "ok": return res
            return self._fetch_jwt(account)
        finally: account._refresh_lock.release()

    def refresh_jwt(self, account):
        if not account._refresh_lock.acquire(timeout=20): return {"status": "error", "message": "Đang xử lý"}
        try:
            res = self._fetch_access_token(account)
            if res["status"] != "ok": return res
            return self._fetch_jwt(account)
        finally: account._refresh_lock.release()


# ================================================================
#                     SINGLETONS
# ================================================================
api_logger = ApiLogger()
api_client = ApiClient(api_logger)
account_manager = GameAccountManager(api_client, api_logger)


# ================================================================
#             GLOBAL REDEEM DEDUP
# ================================================================
_redeemed_global = {}
_redeemed_lock = threading.Lock()

def is_redeemed_global(code):
    with _redeemed_lock: return code in _redeemed_global

def mark_redeemed_global(code):
    with _redeemed_lock:
        _redeemed_global[code] = time.time()
        cutoff = time.time() - GLOBAL_DEDUP_TTL
        stale = [k for k, v in _redeemed_global.items() if v < cutoff]
        for k in stale: del _redeemed_global[k]

def _try_claim_code(session_id, variants):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
        if not s: return False
        redeemed = s.setdefault("redeemed_codes", [])
        if any(v in redeemed for v in variants): return False
        if any(is_redeemed_global(v) for v in variants): return False
        for v in variants:
            redeemed.append(v)
            mark_redeemed_global(v)
        if len(redeemed) > 2000: s["redeemed_codes"] = redeemed[-2000:]
        return True


# ================================================================
#          REDEEM ENGINE — ULTRA FAST & PARALLEL
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

def _warm_redeem_connection():
    try:
        _fast_session.head(REDEEM_BASE_URL, timeout=3, allow_redirects=False)
    except: pass

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
    
    # Lấy Proxy
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
        api_logger.log("success" if resp.status_code == 200 else "error", "POST REDEEM", status=resp.status_code, account=account.username, body=body)
        return {"status": "ok" if resp.status_code == 200 else "error", "http": resp.status_code, "body": body, "code": code, "elapsed": elapsed}
    except requests.exceptions.ProxyError:
        return {"status": "error", "http": 0, "code": code, "message": "proxy die", "elapsed": time.time() - start}
    except requests.exceptions.Timeout:
        return {"status": "error", "http": 0, "code": code, "message": "timeout", "elapsed": time.time() - start}
    except Exception as e:
        return {"status": "error", "http": 0, "code": code, "message": str(e), "elapsed": time.time() - start}

def redeem_code_fast(account, candidates):
    if not candidates: return {"status": "error", "http": 0, "message": "no candidates"}
    if not account.is_jwt_valid():
        if account_manager.refresh_jwt(account).get("status") != "ok":
            return {"status": "error", "http": 0, "message": "JWT invalid"}
    return _single_redeem(account, candidates[0])


# ================================================================
#        PERSISTENT SESSION STORAGE
# ================================================================
persistent_sessions = {}
PERSIST_LOCK = threading.Lock()
_persist_dirty = False

def _load_persist():
    global persistent_sessions
    try:
        if os.path.exists(PERSIST_FILE):
            with open(PERSIST_FILE, "r") as f: persistent_sessions = json.load(f)
            for token, p in persistent_sessions.items():
                for c in p.get("redeemed_codes", []): mark_redeemed_global(c)
    except Exception: persistent_sessions = {}

def _save_persist():
    global _persist_dirty
    try:
        with PERSIST_LOCK:
            data = dict(persistent_sessions)
            _persist_dirty = False
        tmp = PERSIST_FILE + ".tmp"
        with open(tmp, "w") as f: json.dump(data, f)
        os.replace(tmp, PERSIST_FILE)
    except Exception: pass

def _update_persist(token, **fields):
    if not token: return
    with PERSIST_LOCK:
        if token in persistent_sessions: persistent_sessions[token].update(fields)
    _save_persist()

def _mark_dirty():
    global _persist_dirty
    _persist_dirty = True

def _create_persist_token(session_data):
    try:
        client = session_data["client"]
        if not client.is_connected(): return None
        session_string = client.session.save()
    except Exception: return None

    token = str(uuid.uuid4())
    with PERSIST_LOCK:
        persistent_sessions[token] = {
            "session_string": session_string,
            "api_id": session_data.get("api_id"), "api_hash": session_data.get("api_hash"),
            "phone": session_data.get("phone", ""), "game_username": session_data.get("game_username"),
            "game_pw_hash": None, "game_nickname": None, "game_vin": 0, "game_xu": 0,
            "access_token": None, "access_token_time": 0, "jwt": None, "jwt_exp": 0,
            "auto_enabled": False, "redeemed_codes": [], "created": time.time(),
        }
    _save_persist()
    return token


# ============ TELEGRAM SESSION STORAGE ============
sessions = {}
SESSIONS_LOCK = threading.Lock()

def cleanup_old_sessions():
    while True:
        try:
            time.sleep(60)
            now = time.time()
            expired_ids = []
            with SESSIONS_LOCK:
                for sid, s in list(sessions.items()):
                    if s.get("persist_token"): continue
                    if now - s["created"] > 3600: expired_ids.append(sid)
            for sid in expired_ids:
                with SESSIONS_LOCK: s = sessions.pop(sid, None)
                if s and s.get("client"):
                    task = s.get("fast_poll_task")
                    if task and not task.done(): task.cancel()
                    try: async_manager.run(s["client"].disconnect(), timeout=5)
                    except: pass
        except: pass

def _persist_saver_loop():
    while True:
        try:
            time.sleep(60)
            if not _persist_dirty: continue
            with SESSIONS_LOCK:
                active = [(sid, s) for sid, s in sessions.items() if s.get("persist_token") and s.get("redeemed_codes")]
            with PERSIST_LOCK:
                for sid, s in active:
                    pt = s["persist_token"]
                    if pt in persistent_sessions: persistent_sessions[pt]["redeemed_codes"] = s["redeemed_codes"][-500:]
            _save_persist()
        except: pass

# ============ GAME LOGIN ============
def login_game_sync(username, password):
    try:
        pw_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {"c": 3, "un": username, "pw": pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""}
        start = time.time()
        resp = requests.get(GAME_API_URL, params=params, timeout=15)
        result = resp.json()
        if not result.get("success"): return {"status": "error", "message": result.get("message") or "Sai tài khoản!"}
        
        user_info = json.loads(base64.b64decode(result.get("sessionKey", "")).decode("utf-8")) if result.get("sessionKey") else {}
        account = account_manager.get_or_create(username, pw_hash, user_info.get("nickname") or username, user_info.get("vinTotal", 0), user_info.get("xuTotal", 0))
        access_token = result.get("accessToken") or result.get("access_token")
        if access_token:
            account.access_token = access_token
            account.access_token_time = time.time()
        return {"status": "ok", "username": account.nickname, "balance": account.vin, "xuTotal": account.xu, "_pw_hash": pw_hash}
    except Exception as e: return {"status": "error", "message": f"Lỗi kết nối: {e}"}


# ============ TELEGRAM CODE ENGINE ============
async def _async_fetch_codes(client, session_data):
    scan_log = []
    now = time.time()
    channel_entity = None
    try:
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.id == -1002237214360 or (dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame'):
                channel_entity = dialog.entity; break
    except: pass
    if not channel_entity:
        try: channel_entity = await client.get_entity('codelaucuagame')
        except: pass
    if not channel_entity:
        try: channel_entity = await client.get_entity(-1002237214360)
        except: pass
    if not channel_entity: return {"status": "error", "message": "Không tìm thấy kênh LC79", "scan_log": scan_log}
    
    try: await client(functions.channels.JoinChannelRequest(channel=channel_entity))
    except: pass

    session_data.setdefault("codes_seen", {})
    items, seen_in_scan = [], set()

    async for msg in client.iter_messages(channel_entity, limit=100):
        raw_text = getattr(msg, 'message', '') or getattr(msg, 'text', '') or ''
        if not raw_text: continue
        msg_date = msg.date.timestamp() if getattr(msg, 'date', None) else now
        for raw in extract_code_candidates(raw_text):
            variants = code_variants(raw)
            if not variants: continue
            canonical = variants[-1] if len(variants) > 1 else variants[0]
            if canonical in seen_in_scan: continue
            seen_in_scan.add(canonical)
            is_new = canonical not in session_data["codes_seen"]
            if is_new: session_data["codes_seen"][canonical] = {"first_seen": now}
            items.append({"code": canonical, "status": "new" if is_new else "seen", "msg_date": msg_date})
            if len(items) >= 10: break
        if len(items) >= 10: break

    return {"status": "ok", "items": items}


# ================================================================
#                   AUTO-REDEEM WORKER
# ================================================================
def _add_auto_event(session_id, event, is_success=False):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s: return
    s.setdefault("auto_events", []).append(event)
    if is_success:
        s.setdefault("success_history", []).insert(0, event)
        if len(s["success_history"]) > 50: s["success_history"] = s["success_history"][:50]
    if len(s["auto_events"]) > 100: s["auto_events"] = s["auto_events"][-100:]

def _auto_redeem_worker(session_id, candidates, raw_code, msg_ts):
    try:
        if not _try_claim_code(session_id, candidates): return
        _mark_dirty()
        with SESSIONS_LOCK: s = sessions.get(session_id)
        if not s or not s.get("game_username"): return

        account = account_manager.get(s.get("game_username"))
        if not account: return

        fire_ts = time.time()
        code_to_fire = candidates[0]
        result = redeem_code_fast(account, candidates)
        
        elapsed = time.time() - fire_ts
        http = result.get("http", 0)
        body = result.get("body") or {}
        is_ok = http in (200, 201)
        amount = body.get("money") or body.get("amount") if isinstance(body, dict) else None
        msg = body.get("message") if isinstance(body, dict) else result.get("message")

        event = {
            "ts": fire_ts, "code": code_to_fire, "raw_code": raw_code,
            "http": http, "elapsed_ms": int(elapsed * 1000),
            "status": "success" if is_ok else "fail", "amount": amount, "message": msg,
            "is_proxy": bool(PROXIES)
        }
        _add_auto_event(session_id, event, is_success=is_ok)
    except Exception as e: logger.error(f"Worker err: {e}")


# ================================================================
#              FAST POLL LOOP
# ================================================================
async def _fast_poll_loop(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s: return
    client = s["client"]
    channel_entity = s.get("auto_watch_channel")
    
    last_seen_id = 0
    try:
        msgs = await client.get_messages(channel_entity, limit=1)
        if msgs: last_seen_id = msgs[0].id
    except: pass

    consecutive_empty = 0
    while True:
        with SESSIONS_LOCK:
            if not sessions.get(session_id) or not sessions[session_id].get("auto_watch_handler"): break
        if not client.is_connected():
            try: await client.connect()
            except: await asyncio.sleep(2.0); continue

        try:
            msgs = await client.get_messages(channel_entity, limit=FAST_POLL_BATCH, min_id=last_seen_id)
            if msgs:
                consecutive_empty = 0
                for msg in reversed(msgs):
                    last_seen_id = max(last_seen_id, msg.id)
                    for raw in extract_code_candidates(msg.message or ""):
                        variants = code_variants_auto(raw)
                        if variants: threading.Thread(target=_auto_redeem_worker, args=(session_id, variants, raw, time.time()), daemon=True).start()
            else: consecutive_empty += 1
        except FloodWaitError as e: await asyncio.sleep(min(int(e.seconds) + 1, 60)); continue
        except: await asyncio.sleep(1.0); continue

        if consecutive_empty > FAST_POLL_IDLE_BACKOFF: await asyncio.sleep(0.5)
        else: await asyncio.sleep(FAST_POLL_INTERVAL)


async def _async_resolve_channel(client):
    channel = None
    try:
        async for dialog in client.iter_dialogs(limit=200):
            if (dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame') or dialog.id == -1002237214360:
                channel = dialog.entity; break
    except: pass
    if not channel:
        try: channel = await client.get_entity('codelaucuagame')
        except: pass
    if not channel:
        try: channel = await client.get_entity(-1002237214360)
        except: pass
    return channel


# ================================================================
#                   AUTO-WATCH
# ================================================================
async def _async_start_auto_watch(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s or s.get("auto_watch_handler"): return {"status": "ok"}
    if not s.get("game_username"): return {"status": "error", "message": "Chưa login game"}

    account = account_manager.get(s.get("game_username"))
    if not account.is_jwt_valid():
        if account_manager.refresh_jwt(account).get("status") != "ok":
            return {"status": "error", "message": "Không lấy được JWT"}

    client = s["client"]
    if not client.is_connected(): await client.connect()

    channel_entity = await _async_resolve_channel(client)
    if not channel_entity: return {"status": "error", "message": "Không tìm thấy kênh"}
    try: await client(functions.channels.JoinChannelRequest(channel=channel_entity))
    except: pass

    async def on_new_msg(event):
        try:
            for raw in extract_code_candidates(event.raw_text or ""):
                variants = code_variants_auto(raw)
                if variants: threading.Thread(target=_auto_redeem_worker, args=(session_id, variants, raw, time.time()), daemon=True).start()
        except: pass

    client.add_event_handler(on_new_msg, events.NewMessage(chats=[channel_entity]))
    s["auto_watch_handler"], s["auto_watch_channel"] = on_new_msg, channel_entity
    s["fast_poll_task"] = async_manager.submit_task(_fast_poll_loop(session_id))
    return {"status": "ok"}

async def _async_stop_auto_watch(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s or not s.get("auto_watch_handler"): return {"status": "ok"}
    try: s["client"].remove_event_handler(s["auto_watch_handler"])
    except: pass
    s["auto_watch_handler"], s["auto_watch_channel"] = None, None
    if s.get("fast_poll_task"): s["fast_poll_task"].cancel()
    s["fast_poll_task"] = None
    return {"status": "ok"}


async def _async_force_scan(session_id):
    with SESSIONS_LOCK: s = sessions.get(session_id)
    if not s or not s.get("game_username"): return {"status": "error"}
    client = s["client"]
    if not client.is_connected(): await client.connect()

    channel_entity = s.get("auto_watch_channel") or await _async_resolve_channel(client)
    fired = []
    now = time.time()
    async for msg in client.iter_messages(channel_entity, limit=20):
        for raw in extract_code_candidates(msg.message or ""):
            variants = code_variants_auto(raw)
            with SESSIONS_LOCK: redeemed = s.get("redeemed_codes", [])
            if any(v in redeemed for v in variants): continue
            threading.Thread(target=_auto_redeem_worker, args=(session_id, variants, raw, now), daemon=True).start()
            fired.append(raw)
    return {"status": "ok", "fired": len(fired)}


# ================================================================
#                    BACKGROUND WORKERS
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
            time.sleep(60)
            with SESSIONS_LOCK: active = [(sid, s) for sid, s in sessions.items() if s.get("auto_watch_handler")]
            for sid, s in active:
                if s.get("client") and not s["client"].is_connected():
                    async_manager.run(s["client"].connect(), timeout=20)
        except: pass

def restore_persistent_sessions_loop():
    time.sleep(10)
    with PERSIST_LOCK:
        items = [(t, dict(p)) for t, p in persistent_sessions.items() if p.get("auto_enabled")]
    if not items: return
    for token, p in items:
        try:
            async def _restore(token=token, p=p):
                client = TelegramClient(StringSession(p["session_string"]), int(p["api_id"]), p["api_hash"])
                await client.connect()
                if not await client.is_user_authorized(): return None
                if p.get("game_username") and p.get("game_pw_hash"):
                    acc = account_manager.get_or_create(p["game_username"], p["game_pw_hash"], p.get("game_nickname") or p["game_username"], p.get("game_vin", 0), p.get("game_xu", 0))
                    acc.access_token = p.get("access_token"); acc.jwt = p.get("jwt"); acc.jwt_exp = p.get("jwt_exp", 0)
                new_sid = str(uuid.uuid4())
                with SESSIONS_LOCK:
                    sessions[new_sid] = {"client": client, "phone": p.get("phone", ""), "created": time.time(), "codes_seen": {}, "game_username": p.get("game_username"), "auto_events": [], "success_history": [], "redeemed_codes": list(p.get("redeemed_codes", [])), "auto_watch_handler": None, "fast_poll_task": None, "persist_token": token, "api_id": p.get("api_id"), "api_hash": p.get("api_hash")}
                await _async_start_auto_watch(new_sid)
                return new_sid
            async_manager.run(_restore(), timeout=90)
        except: pass


# ================================================================
#                    API ENDPOINTS
# ================================================================
@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    data = request.json or {}
    api_id, api_hash, phone = data.get("api_id"), data.get("api_hash"), data.get("phone")
    if not all([api_id, api_hash, phone]): return jsonify({"status": "error", "message": "Thiếu thông tin"})
    phone = phone.strip().replace(" ", "").replace("-", "")

    async def _do():
        client = None
        try:
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            session_id = str(uuid.uuid4())
            with SESSIONS_LOCK:
                sessions[session_id] = {"client": client, "phone": phone, "phone_code_hash": sent.phone_code_hash, "created": time.time(), "codes_seen": {}, "game_username": None, "auto_events": [], "success_history": [], "redeemed_codes": [], "auto_watch_handler": None, "fast_poll_task": None, "persist_token": None, "api_id": int(api_id), "api_hash": api_hash}
            return {"status": "ok", "session_id": session_id}
        except Exception as e:
            if client and client.is_connected(): await client.disconnect()
            return {"status": "error", "message": str(e)}

    return jsonify(async_manager.run(_do(), timeout=45))

@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json or {}
    with SESSIONS_LOCK: s = sessions.get(data.get("session_id"))
    if not s: return jsonify({"status": "error", "message": "Session lỗi"})

    async def _do():
        try:
            await s["client"].sign_in(phone=s["phone"], code=data.get("code"), phone_code_hash=s["phone_code_hash"])
            s["phone_code_hash"] = None
            persist_token = _create_persist_token(s)
            if persist_token: s["persist_token"] = persist_token
            return {"status": "ok", "persist_token": persist_token}
        except SessionPasswordNeededError: return {"status": "2fa"}
        except Exception as e: return {"status": "error", "message": str(e)}

    return jsonify(async_manager.run(_do(), timeout=45))

@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json or {}
    with SESSIONS_LOCK: s = sessions.get(data.get("session_id"))
    if not s: return jsonify({"status": "error", "message": "Session lỗi"})

    async def _do():
        try:
            await s["client"].sign_in(password=data.get("password"))
            persist_token = _create_persist_token(s)
            if persist_token: s["persist_token"] = persist_token
            return {"status": "ok", "persist_token": persist_token}
        except: return {"status": "error", "message": "Sai 2FA"}

    return jsonify(async_manager.run(_do(), timeout=45))

@flask_app.route("/api/login_game", methods=["POST"])
def api_login_game():
    data = request.json or {}
    result = login_game_sync(data.get("username"), data.get("password"))
    if result.get("status") == "ok":
        pw_hash = result.pop("_pw_hash", None)
        if data.get("session_id"):
            with SESSIONS_LOCK: s = sessions.get(data["session_id"])
            if s:
                s["game_username"] = data["username"]
                if not s.get("persist_token") and data.get("persist_token"): s["persist_token"] = data["persist_token"]
        if data.get("persist_token"):
            acc = account_manager.get(data["username"])
            _update_persist(data["persist_token"], game_username=data["username"], game_pw_hash=pw_hash, game_nickname=result.get("username"), game_vin=result.get("balance", 0), game_xu=result.get("xuTotal", 0))
    return jsonify(result)

@flask_app.route("/api/reset_account", methods=["POST"])
def api_reset_account():
    sid = (request.json or {}).get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s or not s.get("game_username"): return jsonify({"status": "error", "message": "Chưa login game"})
    acc = account_manager.get(s["game_username"])
    res = account_manager.refresh_account_info(acc)
    if res["status"] == "ok":
        return jsonify({"status": "ok", "vin": acc.vin, "xu": acc.xu})
    return jsonify(res)

@flask_app.route("/api/toggle_auto", methods=["POST"])
def api_toggle_auto():
    data = request.json or {}
    try:
        if data.get("enabled"):
            result = async_manager.run(_async_start_auto_watch(data.get("session_id")), timeout=30)
        else:
            result = async_manager.run(_async_stop_auto_watch(data.get("session_id")), timeout=15)
    except Exception as e: return jsonify({"status": "error", "message": str(e)})
    if result.get("status") == "ok" and data.get("session_id"):
        with SESSIONS_LOCK: s = sessions.get(data["session_id"])
        if s and s.get("persist_token"): _update_persist(s["persist_token"], auto_enabled=data.get("enabled"))
    return jsonify(result)

@flask_app.route("/api/auto_status", methods=["POST"])
def api_auto_status():
    sid = (request.json or {}).get("session_id")
    with SESSIONS_LOCK: s = sessions.get(sid)
    if not s: return jsonify({"status": "error", "message": "Session lỗi"})
    acc = account_manager.get(s.get("game_username")) if s.get("game_username") else None
    return jsonify({
        "status": "ok", "enabled": bool(s.get("auto_watch_handler")), 
        "events": s.get("auto_events", [])[-20:],
        "success_history": s.get("success_history", [])[:20],
        "proxy_count": len(PROXIES),
        "jwt_remaining_minutes": acc.jwt_remaining_minutes() if acc else 0
    })

@flask_app.route("/api/force_scan", methods=["POST"])
def api_force_scan():
    try: return jsonify(async_manager.run(_async_force_scan((request.json or {}).get("session_id")), timeout=30))
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@flask_app.route("/api/redeem_manual", methods=["POST"])
def api_redeem_manual():
    data = request.json or {}
    with SESSIONS_LOCK: s = sessions.get(data.get("session_id"))
    if not s or not s.get("game_username"): return jsonify({"status": "error", "message": "Lỗi session"})
    acc = account_manager.get(s["game_username"])
    variants = code_variants((data.get("code") or "").strip().upper())
    start = time.time()
    result = redeem_code_fast(acc, variants)
    
    is_ok = result.get("http") in (200, 201)
    body = result.get("body") or {}
    amount = body.get("money") or body.get("amount") if isinstance(body, dict) else None
    msg = body.get("message") if isinstance(body, dict) else result.get("message")
    event = {"ts": time.time(), "code": variants[0] if variants else "", "http": result.get("http"), "elapsed_ms": int((time.time()-start)*1000), "status": "success" if is_ok else "fail", "amount": amount, "message": msg, "is_proxy": bool(PROXIES)}
    _add_auto_event(data.get("session_id"), event, is_success=is_ok)
    return jsonify({**result, "amount": amount})

@flask_app.route("/api/resume", methods=["POST"])
def api_resume():
    persist_token = (request.json or {}).get("persist_token")
    with PERSIST_LOCK: p = persistent_sessions.get(persist_token)
    if not p: return jsonify({"status": "not_found"})
    
    with SESSIONS_LOCK:
        for sid, s in sessions.items():
            if s.get("persist_token") == persist_token and s.get("client") and s["client"].is_connected():
                return jsonify({"status": "ok", "session_id": sid, "game_username": p.get("game_username"), "auto_enabled": bool(s.get("auto_watch_handler"))})

    async def _do():
        client = TelegramClient(StringSession(p["session_string"]), int(p["api_id"]), p["api_hash"])
        await client.connect()
        if not await client.is_user_authorized(): return {"status": "expired"}
        if p.get("game_username") and p.get("game_pw_hash"):
            acc = account_manager.get_or_create(p["game_username"], p["game_pw_hash"], p.get("game_nickname"), p.get("game_vin", 0), p.get("game_xu", 0))
            acc.access_token = p.get("access_token"); acc.jwt = p.get("jwt"); acc.jwt_exp = p.get("jwt_exp", 0)
        new_sid = str(uuid.uuid4())
        with SESSIONS_LOCK:
            sessions[new_sid] = {"client": client, "created": time.time(), "game_username": p.get("game_username"), "auto_events": [], "success_history": [], "redeemed_codes": list(p.get("redeemed_codes", [])), "auto_watch_handler": None, "fast_poll_task": None, "persist_token": persist_token, "api_id": p.get("api_id"), "api_hash": p.get("api_hash")}
        if p.get("auto_enabled") and p.get("game_username"): await _async_start_auto_watch(new_sid)
        return {"status": "ok", "session_id": new_sid, "game_username": p.get("game_username"), "auto_enabled": bool(p.get("auto_enabled"))}
    return jsonify(async_manager.run(_do(), timeout=45))

@flask_app.route("/api/get_access_token", methods=["POST"])
def api_get_access_token():
    with SESSIONS_LOCK: s = sessions.get((request.json or {}).get("session_id"))
    if not s or not s.get("game_username"): return jsonify({"status": "error"})
    acc = account_manager.get(s["game_username"])
    res = account_manager.get_access_token(acc)
    if res.get("status") == "ok" and s.get("persist_token"):
        _update_persist(s["persist_token"], access_token=acc.access_token, access_token_time=acc.access_token_time)
    return jsonify(res)

@flask_app.route("/api/refresh_jwt", methods=["POST"])
def api_refresh_jwt():
    with SESSIONS_LOCK: s = sessions.get((request.json or {}).get("session_id"))
    if not s or not s.get("game_username"): return jsonify({"status": "error"})
    acc = account_manager.get(s["game_username"])
    res = account_manager.refresh_jwt(acc)
    if res.get("status") == "ok" and s.get("persist_token"):
        _update_persist(s["persist_token"], jwt=acc.jwt, jwt_exp=acc.jwt_exp)
    return jsonify(res)

@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    with SESSIONS_LOCK: s = sessions.get((request.json or {}).get("session_id"))
    if not s: return jsonify({"status": "error"})
    async def _do():
        if not s["client"].is_connected(): await s["client"].connect()
        return await _async_fetch_codes(s["client"], s)
    return jsonify(async_manager.run(_do(), timeout=30))

@flask_app.route("/api/api_log", methods=["GET"])
def api_api_log():
    return jsonify({"status": "ok", "logs": api_logger.get_logs(limit=50)})

@flask_app.route("/api/stats", methods=["GET"])
def api_stats(): return jsonify({"status": "ok", "stats": api_logger.get_stats()})

@flask_app.route("/api/ping")
def ping_json(): return jsonify({"status": "alive", "uptime": int(time.time() - SERVER_START_TIME), "active_sessions": len(sessions)})

@flask_app.route("/health")
def health(): return Response("ok")


# ================================================================
#                    WEB UI (LIGHT THEME)
# ================================================================
MAIN_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Auto Tool VIP</title>
<style>
:root{--bg:#f1f5f9;--card:#ffffff;--accent:#4f46e5;--text:#1e293b;--muted:#64748b;--border:#e2e8f0;--danger:#ef4444;--success:#10b981;--warning:#f59e0b;}
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,sans-serif}
body{background:var(--bg);color:var(--text);padding:16px;display:flex;justify-content:center}
.container{width:100%;max-width:500px}
.card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px;margin-bottom:16px;box-shadow:0 2px 4px rgba(0,0,0,0.02)}
h2{font-size:13px;font-weight:700;text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px;display:flex;justify-content:space-between}
label{display:block;font-size:12px;font-weight:700;color:var(--muted);margin-bottom:6px;text-transform:uppercase}
input{width:100%;padding:12px;border:1px solid var(--border);border-radius:8px;font-size:14px;margin-bottom:12px;outline:none;background:#f8fafc}
input:focus{border-color:var(--accent);background:#fff}
button{width:100%;padding:14px;border:none;border-radius:8px;font-size:14px;font-weight:700;color:#fff;cursor:pointer;background:var(--accent);transition:0.2s}
button:disabled{opacity:0.6;pointer-events:none}
.btn-success{background:var(--success)}.btn-danger{background:var(--danger)}.btn-warning{background:var(--warning)}
.btn-outline{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.btn-sm{padding:6px 12px;font-size:11px;width:auto;border-radius:6px}
.view{display:none}.view.active{display:block}
.alert{padding:12px;border-radius:8px;font-size:13px;font-weight:600;margin-bottom:16px;display:none;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca}
.info-box{display:flex;justify-content:space-between;background:#f8fafc;border:1px solid var(--border);padding:12px;border-radius:8px;margin-bottom:12px;font-size:13px;font-weight:700}
.token-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px}
.t-box{border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center;font-size:11px;font-weight:700}
.t-box.ok{border-color:#a7f3d0;background:#ecfdf5;color:#059669}
.t-box.err{border-color:#fecaca;background:#fef2f2;color:#dc2626}
.t-box button{margin-top:6px;padding:6px;font-size:10px;background:rgba(79,70,229,0.1);color:var(--accent);border:1px solid rgba(79,70,229,0.2)}
.tabs{display:flex;gap:6px;margin-bottom:10px}
.tabs button{padding:8px;font-size:12px;background:#e2e8f0;color:var(--text);border:none}
.tabs button.active{background:var(--accent);color:#fff}
.log-container{background:#0f172a;color:#38bdf8;border-radius:8px;padding:12px;height:220px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.5}
.log-item{padding:6px 0;border-bottom:1px dashed #334155}
.success-item{display:flex;justify-content:space-between;background:#ecfdf5;padding:8px;border-radius:6px;margin-bottom:6px;font-size:12px;border-left:3px solid var(--success)}
.toast{position:fixed;top:20px;left:50%;transform:translateX(-50%);background:#1e293b;color:#fff;padding:10px 20px;border-radius:8px;font-size:13px;z-index:9999}
</style>
</head>
<body>
<div class="container">
    <!-- LOGIN -->
    <div id="view-login" class="view active">
        <div class="card">
            <h1 style="text-align:center;margin-bottom:16px;color:var(--accent)">🚀 Auto Tool VIP</h1>
            <div id="alert-login" class="alert"></div>
            <div id="step-api">
                <label>API ID</label><input type="number" id="apiId">
                <label>API HASH</label><input type="password" id="apiHash">
                <label>SĐT Telegram</label><input type="tel" id="phone">
                <button onclick="sendOTP()" id="btn-send">Gửi OTP</button>
            </div>
            <div id="step-otp" style="display:none;">
                <label>Mã OTP</label><input type="text" id="otpCode">
                <button onclick="verifyOTP()" id="btn-verify">Xác Nhận</button>
            </div>
            <div id="step-2fa" style="display:none;">
                <label>Mật khẩu 2FA</label><input type="password" id="pass2fa">
                <button onclick="verify2FA()" id="btn-2fa">Xác Nhận</button>
            </div>
            <div id="step-game" style="display:none;">
                <label>Tài khoản Game</label><input type="text" id="gameUser" placeholder="Username">
                <input type="password" id="gamePass" placeholder="Password">
                <button class="btn-success" onclick="loginGame()" id="btn-game">Đăng Nhập</button>
            </div>
        </div>
    </div>

    <!-- DASHBOARD -->
    <div id="view-dash" class="view">
        <div class="card">
            <h2>👤 Thông Tin Tài Khoản <button class="btn-outline btn-sm" onclick="resetAcc()" id="btn-reset">🔄 Update</button></h2>
            <div class="info-box">
                <span id="info-name" style="color:var(--accent)">User</span>
                <span>Vin: <span id="info-vin" style="color:var(--success)">0</span></span>
                <span>Xu: <span id="info-xu" style="color:var(--accent)">0</span></span>
            </div>
            <div class="token-grid">
                <div class="t-box" id="box-jwt">
                    JWT: <span id="jwt-status">Checking...</span><br>
                    <button onclick="fetchJWT()">Lấy JWT</button>
                </div>
                <div class="t-box">
                    Proxy: <span id="proxy-count" style="color:var(--accent)">0</span>
                </div>
            </div>
        </div>

        <div class="card">
            <h2>⚡ Auto Watch & Redeem</h2>
            <button id="btn-auto" class="btn-success" onclick="toggleAuto()">BẬT AUTO</button>
        </div>

        <div class="card">
            <h2>🎯 Nhập Code Thủ Công</h2>
            <input type="text" id="manualCode" placeholder="Nhập code (VD: ABCXYZ)">
            <button class="btn-warning" onclick="redeemManual()" id="btn-manual">⚡ NHẬP NGAY</button>
        </div>

        <div class="card" style="padding:10px">
            <div class="tabs">
                <button onclick="switchTab('tab-auto', this)" class="active" id="btn-tab-auto">Auto Log</button>
                <button onclick="switchTab('tab-success', this)" id="btn-tab-success">Thành Công</button>
                <button onclick="switchTab('tab-json', this)" id="btn-tab-json">JSON API</button>
            </div>
            
            <div id="tab-auto" class="tab-content">
                <div class="log-container" id="auto-log"><div style="text-align:center;color:#94a3b8">Chưa có log...</div></div>
            </div>
            <div id="tab-success" class="tab-content" style="display:none;">
                <div style="max-height:220px;overflow-y:auto;" id="success-list"><div style="text-align:center;color:#94a3b8;font-size:12px;">Chưa có code thành công</div></div>
            </div>
            <div id="tab-json" class="tab-content" style="display:none;">
                <div class="log-container" id="json-log"><div style="text-align:center;color:#94a3b8">Chưa có log...</div></div>
            </div>
        </div>

        <div style="text-align:center;margin-bottom:30px;">
            <button class="btn-outline btn-sm" onclick="logout()">Đăng xuất</button>
        </div>
    </div>
</div>

<script>
const state = { sid: '', token: '', auto: false };
let lastLogJson = '', lastCodeJson = '';
const $ = id => document.getElementById(id);
const showView = id => { document.querySelectorAll('.view').forEach(v => v.classList.remove('active')); $(id).classList.add('active'); };
const err = m => { $('alert-login').style.display='block'; $('alert-login').innerText=m; };
const toast = m => { const t=document.createElement('div'); t.className='toast'; t.innerText=m; document.body.appendChild(t); setTimeout(()=>t.remove(),2500); };
const switchTab = (id, btn) => {
    document.querySelectorAll('.tab-content').forEach(t => t.style.display='none');
    document.querySelectorAll('.tabs button').forEach(b => b.classList.remove('active'));
    $(id).style.display='block'; btn.classList.add('active');
    if(id==='tab-json') loadLogs();
}

// LOGIN
async function sendOTP() {
    $('btn-send').disabled=true; $('btn-send').innerText='Đang gửi...';
    try {
        const res = await fetch('/api/send_code', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_id:$('apiId').value,api_hash:$('apiHash').value,phone:$('phone').value})});
        const d = await res.json();
        if(d.status==='ok') { state.sid=d.session_id; $('step-api').style.display='none'; $('step-otp').style.display='block'; }
        else err(d.message);
    } catch(e) { err('Lỗi mạng'); }
    $('btn-send').disabled=false; $('btn-send').innerText='Gửi OTP';
}
async function verifyOTP() {
    $('btn-verify').disabled=true;
    const res = await fetch('/api/verify_code', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid,code:$('otpCode').value})});
    const d = await res.json();
    if(d.status==='ok') { state.token=d.persist_token; $('step-otp').style.display='none'; $('step-game').style.display='block'; }
    else if(d.status==='2fa') { $('step-otp').style.display='none'; $('step-2fa').style.display='block'; }
    else err(d.message);
    $('btn-verify').disabled=false;
}
async function verify2FA() {
    $('btn-2fa').disabled=true;
    const res = await fetch('/api/verify_2fa', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid,password:$('pass2fa').value})});
    const d = await res.json();
    if(d.status==='ok') { state.token=d.persist_token; $('step-2fa').style.display='none'; $('step-game').style.display='block'; }
    else err(d.message);
    $('btn-2fa').disabled=false;
}
async function loginGame() {
    $('btn-game').disabled=true;
    const res = await fetch('/api/login_game', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('gameUser').value,password:$('gamePass').value,session_id:state.sid,persist_token:state.token})});
    const d = await res.json();
    if(d.status==='ok') { 
        localStorage.setItem('lc79_token', state.token);
        $('info-name').innerText=d.username; $('info-vin').innerText=d.balance; $('info-xu').innerText=d.xuTotal;
        showView('view-dash'); poll(); 
    } else err(d.message);
    $('btn-game').disabled=false;
}

// DASHBOARD
async function resetAcc() {
    $('btn-reset').innerText='Đang lấy...'; $('btn-reset').disabled=true;
    const res = await fetch('/api/reset_account', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid})});
    const d = await res.json();
    if(d.status==='ok') { $('info-vin').innerText=d.vin; $('info-xu').innerText=d.xu; toast('Cập nhật thành công!'); }
    else toast('Lỗi: ' + d.message);
    $('btn-reset').innerText='🔄 Update'; $('btn-reset').disabled=false;
}
async function fetchJWT() {
    toast('Đang lấy JWT...');
    const res = await fetch('/api/refresh_jwt', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid})});
    const d = await res.json();
    if(d.status==='ok') toast('Đã lấy JWT thành công!');
    else toast('Lỗi: ' + d.message);
}
async function toggleAuto() {
    const enable = !state.auto;
    const res = await fetch('/api/toggle_auto', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid,enabled:enable})});
    const d = await res.json();
    if(d.status==='ok') { setAuto(enable); toast(enable?'Bật Auto':'Tắt Auto'); }
    else toast('Lỗi: ' + d.message);
}
function setAuto(isOn) {
    state.auto = isOn;
    $('btn-auto').innerText = isOn ? 'TẮT AUTO' : 'BẬT AUTO';
    $('btn-auto').className = isOn ? 'btn-danger' : 'btn-success';
}
async function redeemManual() {
    const code = $('manualCode').value; if(!code) return;
    $('btn-manual').disabled=true; $('btn-manual').innerText='Đang bắn...';
    try {
        const res = await fetch('/api/redeem_manual', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid,code})});
        const d = await res.json();
        if(d.http===200) { toast('✅ Nhập thành công!'); $('manualCode').value=''; }
        else toast('❌ ' + (d.body?.message || d.message || 'Lỗi'));
    } catch(e) { toast('Lỗi mạng'); }
    $('btn-manual').disabled=false; $('btn-manual').innerText='⚡ NHẬP NGAY';
}

// POLLING
async function poll() {
    if(!state.sid) return;
    try {
        const r1 = await fetch('/api/auto_status', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:state.sid})});
        const d1 = await r1.json();
        if(d1.status==='ok') {
            if(d1.enabled !== state.auto) setAuto(d1.enabled);
            $('proxy-count').innerText = d1.proxy_count;
            
            const bx = $('box-jwt');
            if(d1.jwt_remaining_minutes > 0) { bx.className='t-box ok'; $('jwt-status').innerText=`Còn ${d1.jwt_remaining_minutes}p`; }
            else { bx.className='t-box err'; $('jwt-status').innerText='Hết hạn'; }

            // Auto Log
            const logStr = JSON.stringify(d1.events);
            if(logStr !== lastLogJson) {
                lastLogJson = logStr;
                if(d1.events.length > 0) {
                    $('auto-log').innerHTML = d1.events.slice().reverse().map(e => `
                        <div class="log-item" style="color:${e.status==='success'?'#10b981':'#ef4444'}">
                            [${new Date(e.ts*1000).toLocaleTimeString()}] ${e.code} ${e.is_proxy?'[P]':''} - ${e.status} ${e.message?'<br><span style="color:#94a3b8">'+e.message+'</span>':''}
                        </div>
                    `).join('');
                }
            }

            // Success History
            if(d1.success_history && d1.success_history.length > 0) {
                $('success-list').innerHTML = d1.success_history.map(e => `
                    <div class="success-item">
                        <span style="font-weight:700; color:var(--accent)">${e.code}</span>
                        <span style="color:var(--success); font-weight:700">+${Number(e.amount||0).toLocaleString('vi-VN')}</span>
                        <span style="font-size:10px; color:var(--muted)">${new Date(e.ts*1000).toLocaleTimeString()}</span>
                    </div>
                `).join('');
            }
        }
        if($('tab-json').style.display==='block') loadLogs();
    } catch(e) {}
    setTimeout(poll, 2000);
}

// JSON LOGS
async function loadLogs() {
    try {
        const res = await fetch('/api/api_log');
        const d = await res.json();
        if(d.status==='ok' && d.logs.length > 0) {
            $('json-log').innerHTML = d.logs.map(l => `
                <div class="log-item">
                    <span style="color:#fbbf24">[${l.time}]</span> <span style="color:#a78bfa">${l.method || ''} ${l.url || ''}</span> - ${l.status || ''}<br>
                    <span style="color:#64748b">Req:</span> ${JSON.stringify(l.params||l.json||l.body||'').substring(0,100)}<br>
                    <span style="color:#64748b">Res:</span> ${JSON.stringify(l.body||l.error||'').substring(0,150)}
                </div>
            `).join('');
        }
    } catch(e) {}
}

// AUTO RESUME
async function init() {
    const token = localStorage.getItem('lc79_token');
    if(!token) return;
    try {
        const res = await fetch('/api/resume', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({persist_token:token})});
        const d = await res.json();
        if(d.status==='ok') {
            state.sid=d.session_id; state.token=token;
            $('info-name').innerText=d.game_username; showView('view-dash');
            if(d.auto_enabled) setAuto(true);
            poll();
        } else localStorage.removeItem('lc79_token');
    } catch(e) {}
}
function logout() { if(confirm('Đăng xuất?')) { localStorage.clear(); location.reload(); } }
init();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    _load_local_proxies()
    _load_persist()
    async_manager.start()
    
    # Chạy ngầm
    threading.Thread(target=proxy_updater_loop, daemon=True).start()
    threading.Thread(target=_warm_redeem_connection, daemon=True).start()
    threading.Thread(target=cleanup_old_sessions, daemon=True).start()
    threading.Thread(target=jwt_prewarm_loop, daemon=True).start()
    threading.Thread(target=session_keepalive_loop, daemon=True).start()
    threading.Thread(target=restore_persistent_sessions_loop, daemon=True).start()
    threading.Thread(target=_persist_saver_loop, daemon=True).start()
    
    logger.info(f"🚀 Web server khởi động tại port {PORT}")
    flask_app.run(host="0.0.0.0", port=PORT, threaded=True)