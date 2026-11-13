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
from flask import Flask, Response, request, jsonify

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

    def __init__(self, max_entries=500):
        self.entries = []
        self.lock = threading.Lock()
        self.max_entries = max_entries
        self.stats = {"total": 0, "success": 0, "failed": 0, "total_elapsed": 0.0}

    def _mask(self, s, show=6):
        if not s or not isinstance(s, str):
            return "***"
        if len(s) <= show * 2:
            return "***"
        return s[:show] + "..." + s[-show:]

    def _is_sensitive(self, key):
        kl = key.lower().replace("_", "").replace("-", "")
        return kl in self.SENSITIVE_KEYS

    def _redact_dict(self, data):
        if not isinstance(data, dict):
            return data
        out = {}
        for k, v in data.items():
            if self._is_sensitive(k):
                if k.lower() == "authorization":
                    out[k] = "Bearer ***"
                elif k.lower() in ("pw", "password", "pass", "pwd", "code", "otp"):
                    out[k] = "***"
                else:
                    out[k] = self._mask(v) if isinstance(v, str) else "***"
            elif isinstance(v, dict):
                out[k] = self._redact_dict(v)
            elif isinstance(v, list):
                out[k] = [self._redact_dict(i) if isinstance(i, dict) else i for i in v]
            else:
                out[k] = v
        return out

    def _add(self, entry):
        with self.lock:
            self.entries.append(entry)
            if len(self.entries) > self.max_entries:
                self.entries = self.entries[-self.max_entries:]

    def log(self, level, message, **extra):
        entry = {
            "ts": time.time(),
            "time": time.strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }
        entry.update(extra)
        self._add(entry)
        lvl = {
            "info": logging.INFO, "success": logging.INFO,
            "warning": logging.WARNING, "error": logging.ERROR,
            "request": logging.INFO, "response": logging.INFO,
        }.get(level, logging.INFO)
        logger.log(lvl, f"[{level.upper()}] {message}")

    def log_request(self, request_id, method, url, params=None,
                    json_data=None, headers=None, account=None):
        payload = {"request_id": request_id, "method": method, "url": url}
        if params:
            payload["params"] = self._redact_dict(params)
        if json_data:
            payload["json"] = self._redact_dict(json_data)
        if headers:
            red = {k: ("Bearer ***" if k.lower() == "authorization" else v)
                   for k, v in headers.items()}
            payload["headers"] = red
        if account:
            payload["account"] = account
        self.log("request", f"{method} {url}", **payload)

    def log_response(self, request_id, status, body=None, elapsed=0,
                     account=None, error=None, headers=None):
        with self.lock:
            self.stats["total"] += 1
            if 200 <= status < 300:
                self.stats["success"] += 1
            else:
                self.stats["failed"] += 1
            self.stats["total_elapsed"] += elapsed
        payload = {
            "request_id": request_id, "status": status,
            "elapsed_ms": int(elapsed * 1000),
        }
        if account: payload["account"] = account
        if headers: payload["content_type"] = headers.get("Content-Type", "")
        if body is not None:
            payload["body"] = self._redact_dict(body) if isinstance(body, dict) else body
        if error: payload["error"] = error
        level = "response" if 200 <= status < 300 else "error"
        self.log(level, f"HTTP {status} ({int(elapsed*1000)}ms)", **payload)

    def get_logs(self, limit=200):
        with self.lock:
            return list(self.entries[-limit:])

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

    def request(self, method, url, params=None, json_data=None,
                headers=None, account=None, retry=True):
        rid = str(uuid.uuid4())[:8]
        start = time.time()
        self.logger.log_request(
            request_id=rid, method=method, url=url,
            params=params, json_data=json_data,
            headers=headers, account=account,
        )
        attempt = 0
        last_error = None
        max_attempts = (self.max_retries + 1) if retry else 1
        while attempt < max_attempts:
            try:
                resp = self.session.request(
                    method=method, url=url, params=params, json=json_data,
                    headers=headers,
                    timeout=(self.connect_timeout, self.read_timeout),
                )
                elapsed = time.time() - start
                try:
                    body = resp.json()
                except Exception:
                    body = None
                self.logger.log_response(
                    request_id=rid, status=resp.status_code,
                    body=body if body is not None else (resp.text[:300] or None),
                    elapsed=elapsed, account=account,
                    headers=dict(resp.headers),
                )
                return {
                    "ok": 200 <= resp.status_code < 300, "status": resp.status_code,
                    "body": body, "text": resp.text,
                    "headers": dict(resp.headers),
                    "elapsed": elapsed, "request_id": rid,
                }
            except (requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.ConnectionError) as e:
                last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.logger.log("warning",
                    f"Network error (attempt {attempt+1}/{max_attempts})",
                    request_id=rid, account=account)
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)[:120]}"
                self.logger.log("error", f"Request exception: {last_error}",
                                request_id=rid, account=account)
                break
            attempt += 1
            if attempt < max_attempts:
                time.sleep(0.6 * attempt)
        return {
            "ok": False, "status": 0, "body": None, "text": "",
            "error": last_error or "unknown error",
            "request_id": rid, "elapsed": time.time() - start,
        }

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)


# ================================================================
#                     GAME ACCOUNT + MANAGER
# ================================================================
def parse_jwt_exp(token):
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return 0
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("exp", 0))
    except Exception:
        return 0


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
        if not self.jwt or not self.jwt_exp:
            return False
        return time.time() < (self.jwt_exp - 60)

    def jwt_remaining_minutes(self):
        if not self.jwt_exp:
            return 0
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
        with self.lock:
            return self.accounts.get(username)

    def _fetch_access_token(self, account):
        self.logger.log("info", "Bắt đầu lấy Access Token", account=account.username)
        resp = self.api_client.get(
            GAME_API_URL,
            params={"c": "3", "un": account.username, "pw": account.pw_hash,
                    "cp": "R", "cl": "R", "pf": "web", "at": ""},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            account=account.username,
        )
        if not resp["ok"]:
            msg = resp.get("error") or f"HTTP {resp['status']}"
            self.logger.log("error", f"Access Token thất bại: {msg}",
                            account=account.username)
            return {"status": "error", "message": msg}
        body = resp.get("body") or {}
        if not body.get("success"):
            return {"status": "error",
                    "message": body.get("message") or "API không trả success"}
        token = body.get("accessToken") or body.get("access_token")
        if not token:
            self.logger.log("warning", "Không tìm thấy accessToken",
                            account=account.username,
                            structure=self._structure_only(body))
            return {"status": "error", "message": "Response thiếu accessToken"}
        account.access_token = token
        account.access_token_time = time.time()
        self.logger.log("success", "Đã lấy Access Token", account=account.username)
        return {"status": "ok",
                "token_preview": token[:6] + "..." + token[-4:] if len(token) > 12 else "***"}

    def _fetch_jwt(self, account):
        self.logger.log("info", "Bắt đầu login JWT", account=account.username)
        headers = {
            "content-type": "application/json",
            "origin": "https://lc79b.bet",
            "referer": "https://lc79b.bet/",
            "user-agent": "Mozilla/5.0",
        }
        payload = {"nickName": account.nickname, "accessToken": account.access_token}
        resp = self.api_client.post(LOGIN_URL, headers=headers,
                                    json_data=payload, account=account.username)
        if not resp["ok"]:
            msg = resp.get("error") or f"HTTP {resp['status']}"
            self.logger.log("error", f"JWT login thất bại: {msg}",
                            account=account.username)
            return {"status": "error", "message": msg}
        body = resp.get("body") or {}
        token = (body.get("token") or body.get("jwt")
                 or (body.get("data") or {}).get("token")
                 or (body.get("data") or {}).get("jwt")
                 or (body.get("data") or {}).get("accessToken"))
        if not token:
            self.logger.log("warning", "Không xác định được trường JWT",
                            account=account.username,
                            structure=self._structure_only(body))
            return {"status": "error",
                    "message": "Không xác định được trường JWT. Xem API Log."}
        account.jwt = token
        account.jwt_exp = parse_jwt_exp(token)
        remain = account.jwt_remaining_minutes()
        self.logger.log("success", f"Đã có JWT (còn ~{remain} phút)",
                        account=account.username)
        return {"status": "ok",
                "jwt_preview": token[:6] + "..." + token[-4:] if len(token) > 12 else "***",
                "exp": account.jwt_exp, "expires_in_minutes": remain,
                "has_exp": bool(account.jwt_exp)}

    @staticmethod
    def _structure_only(data, depth=0):
        if not isinstance(data, dict):
            return type(data).__name__
        out = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out[k] = GameAccountManager._structure_only(v, depth + 1)
            elif isinstance(v, list):
                out[k] = [GameAccountManager._structure_only(v[0], depth + 1)] if v else []
            else:
                out[k] = f"<{type(v).__name__}>"
        return out

    def get_access_token(self, account):
        if not account._refresh_lock.acquire(timeout=15):
            return {"status": "error", "message": "Đang xử lý, vui lòng chờ"}
        try:
            return self._fetch_access_token(account)
        finally:
            account._refresh_lock.release()

    def get_jwt(self, account, force=False):
        if not force and account.is_jwt_valid():
            return {"status": "ok", "cached": True, "exp": account.jwt_exp,
                    "expires_in_minutes": account.jwt_remaining_minutes(),
                    "has_exp": bool(account.jwt_exp)}
        if not account._refresh_lock.acquire(timeout=20):
            return {"status": "error", "message": "Đang xử lý, vui lòng chờ"}
        try:
            if not force and account.is_jwt_valid():
                return {"status": "ok", "cached": True, "exp": account.jwt_exp,
                        "expires_in_minutes": account.jwt_remaining_minutes(),
                        "has_exp": bool(account.jwt_exp)}
            if not account.access_token:
                res = self._fetch_access_token(account)
                if res["status"] != "ok":
                    return res
            return self._fetch_jwt(account)
        finally:
            account._refresh_lock.release()

    def refresh_jwt(self, account):
        if not account._refresh_lock.acquire(timeout=20):
            return {"status": "error", "message": "Đang xử lý, vui lòng chờ"}
        try:
            res = self._fetch_access_token(account)
            if res["status"] != "ok":
                return res
            return self._fetch_jwt(account)
        finally:
            account._refresh_lock.release()


# ================================================================
#                     SINGLETONS
# ================================================================
api_logger = ApiLogger()
api_client = ApiClient(api_logger)
account_manager = GameAccountManager(api_client, api_logger)


# ================================================================
#             GLOBAL REDEEM DEDUP (cross-session, cross-restart)
# ================================================================
_redeemed_global = {}   # code -> ts
_redeemed_lock = threading.Lock()


def is_redeemed_global(code):
    with _redeemed_lock:
        return code in _redeemed_global


def mark_redeemed_global(code):
    with _redeemed_lock:
        _redeemed_global[code] = time.time()
        cutoff = time.time() - GLOBAL_DEDUP_TTL
        stale = [k for k, v in _redeemed_global.items() if v < cutoff]
        for k in stale:
            del _redeemed_global[k]


def _try_claim_code(session_id, variants):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
        if not s:
            return False
        redeemed = s.setdefault("redeemed_codes", [])
        if any(v in redeemed for v in variants):
            return False
        if any(is_redeemed_global(v) for v in variants):
            return False
        for v in variants:
            redeemed.append(v)
            mark_redeemed_global(v)
        if len(redeemed) > 2000:
            s["redeemed_codes"] = redeemed[-2000:]
        return True


# ================================================================
#             REDEEM ENGINE — FAST, PARALLEL
# ================================================================
_fast_session = requests.Session()
_fast_session.headers.update({
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "origin": "https://lc79.to",
    "referer": "https://lc79.to/",
    "accept": "application/json, text/plain, */*",
    "accept-language": "vi-VN,vi;q=0.9,en;q=0.8",
    "connection": "keep-alive"
})


def _warm_redeem_connection():
    try:
        _fast_session.head(REDEEM_BASE_URL, timeout=3, allow_redirects=False)
        logger.info("🔥 Redeem connection pre-warmed")
    except Exception as e:
        logger.debug(f"warm failed: {e}")


def code_variants(raw):
    if not raw:
        return []
    raw = raw.strip().upper()
    if raw.startswith("LC79") and len(raw) > 4:
        s = raw[4:].strip()
        return [raw, s] if s else [raw]
    if raw.endswith("LC79") and len(raw) > 4:
        s = raw[:-4].strip()
        return [raw, s] if s else [raw]
    return [raw]


def code_variants_auto(raw):
    """Bỏ hoàn toàn 4 chữ LC79 ở đầu cho Auto."""
    if not raw:
        return []
    raw = raw.strip().upper()
    if raw.startswith("LC79") and len(raw) > 4:
        s = raw[4:].strip()
        return [s] if s else [raw]
    return [raw]


def extract_code_candidates(text):
    if not text:
        return []
    out = []
    for m in re.findall(r'LC79[A-Z0-9]{3,}', text):
        if m not in out:
            out.append(m)
    for m in re.findall(r'Mã code là[:\s]*([A-Z0-9]+)', text, re.IGNORECASE):
        c = m.strip().upper()
        if c and c not in out:
            out.append(c)
    return out


def _single_redeem(account, code):
    jwt = account.jwt
    if not jwt:
        return {"status": "error", "http": 0, "code": code,
                "message": "no JWT", "elapsed": 0}
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {jwt}",
    }
    start = time.time()
    try:
        resp = _fast_session.post(
            REDEEM_BASE_URL + REDEEM_PARAMS,
            headers=headers, json={"code": code, "captcha": ""},
            timeout=(3, 10), # Tăng read timeout lên 10s để đợi server game xử lý
        )
        elapsed = time.time() - start
        try:
            body = resp.json()
        except Exception:
            body = {"raw_text": resp.text[:200]}
        return {
            "status": "ok" if 200 <= resp.status_code < 300 else "error",
            "http": resp.status_code, "body": body,
            "code": code, "elapsed": elapsed,
        }
    except requests.exceptions.Timeout:
        return {"status": "error", "http": 0, "code": code,
                "message": "timeout", "elapsed": time.time() - start}
    except Exception as e:
        return {"status": "error", "http": 0, "code": code,
                "message": str(e)[:150], "elapsed": time.time() - start}


def redeem_code_fast(account, candidates):
    if not candidates:
        return {"status": "error", "http": 0, "message": "no candidates"}
    if not account.is_jwt_valid():
        r = account_manager.refresh_jwt(account)
        if r.get("status") != "ok":
            return {"status": "error", "http": 0,
                    "message": "JWT invalid: " + r.get("message", "")}
    if len(candidates) == 1:
        return _single_redeem(account, candidates[0])

    from concurrent.futures import ThreadPoolExecutor, as_completed
    ex = ThreadPoolExecutor(max_workers=len(candidates))
    futures = [ex.submit(_single_redeem, account, c) for c in candidates]
    winner = None
    try:
        for f in as_completed(futures):
            try:
                r = f.result()
            except Exception:
                continue
            if r.get("http") in (200, 201):
                winner = r
                break
    finally:
        ex.shutdown(wait=False)
    if winner:
        return winner
    for f in futures:
        if f.done() and not f.cancelled():
            try:
                return f.result()
            except Exception:
                pass
    return {"status": "error", "http": 0, "message": "all attempts failed"}


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
            with open(PERSIST_FILE, "r") as f:
                persistent_sessions = json.load(f)
            for token, p in persistent_sessions.items():
                for c in p.get("redeemed_codes", []):
                    mark_redeemed_global(c)
            logger.info(f"📂 Loaded {len(persistent_sessions)} persistent session(s)")
    except Exception as e:
        logger.warning(f"load_persist: {e}")
        persistent_sessions = {}


def _save_persist():
    global _persist_dirty
    try:
        with PERSIST_LOCK:
            data = dict(persistent_sessions)
            _persist_dirty = False
        tmp = PERSIST_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, PERSIST_FILE)
        try:
            os.chmod(PERSIST_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"save_persist: {e}")


def _update_persist(token, **fields):
    if not token:
        return
    with PERSIST_LOCK:
        if token in persistent_sessions:
            persistent_sessions[token].update(fields)
    _save_persist()


def _mark_dirty():
    global _persist_dirty
    _persist_dirty = True


def _create_persist_token(session_data):
    try:
        client = session_data["client"]
        if not client.is_connected():
            logger.warning("client not connected — cannot save session")
            return None
        session_string = client.session.save()
    except Exception as e:
        logger.warning(f"session.save fail: {e}")
        return None

    token = str(uuid.uuid4())
    with PERSIST_LOCK:
        persistent_sessions[token] = {
            "session_string": session_string,
            "api_id": session_data.get("api_id"),
            "api_hash": session_data.get("api_hash"),
            "phone": session_data.get("phone", ""),
            "game_username": session_data.get("game_username"),
            "game_pw_hash": None,
            "game_nickname": None,
            "game_vin": 0,
            "game_xu": 0,
            "access_token": None,
            "access_token_time": 0,
            "jwt": None,
            "jwt_exp": 0,
            "auto_enabled": False,
            "redeemed_codes": [],
            "created": time.time(),
        }
    _save_persist()
    logger.info(f"💾 Persist token created: {token[:8]}")
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
                    if s.get("persist_token"):
                        continue
                    if now - s["created"] > 3600:
                        expired_ids.append(sid)
            for sid in expired_ids:
                with SESSIONS_LOCK:
                    s = sessions.pop(sid, None)
                if s and s.get("client"):
                    task = s.get("fast_poll_task")
                    if task and not task.done():
                        try: task.cancel()
                        except Exception: pass
                    try:
                        async_manager.run(s["client"].disconnect(), timeout=5)
                    except Exception:
                        pass
                    logger.info(f"🧹 Cleaned temp session {sid[:8]}")
        except Exception as e:
            logger.warning(f"cleanup err: {e}")


def _persist_saver_loop():
    while True:
        try:
            time.sleep(60)
            if not _persist_dirty:
                continue
            with SESSIONS_LOCK:
                active = [(sid, s) for sid, s in sessions.items()
                          if s.get("persist_token") and s.get("redeemed_codes")]
            with PERSIST_LOCK:
                for sid, s in active:
                    pt = s["persist_token"]
                    if pt in persistent_sessions:
                        persistent_sessions[pt]["redeemed_codes"] = s["redeemed_codes"][-500:]
            _save_persist()
        except Exception as e:
            logger.warning(f"persist_saver: {e}")


# ============ GAME LOGIN ============
def login_game_sync(username, password):
    try:
        pw_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {"c": 3, "un": username, "pw": pw_hash,
                  "cp": "R", "cl": "R", "pf": "web", "at": ""}
        api_logger.log_request(
            request_id="login-" + str(uuid.uuid4())[:6],
            method="GET", url=GAME_API_URL,
            params=params, account=username,
        )
        start = time.time()
        resp = requests.get(GAME_API_URL, params=params, timeout=15)
        elapsed = time.time() - start
        if resp.status_code >= 500:
            api_logger.log_response(request_id="login", status=resp.status_code,
                                    elapsed=elapsed, account=username)
            return {"status": "error",
                    "message": f"Server game lỗi (HTTP {resp.status_code})"}
        try:
            result = resp.json()
        except Exception:
            api_logger.log_response(request_id="login", status=resp.status_code,
                                    elapsed=elapsed, account=username,
                                    error="JSON parse error")
            return {"status": "error", "message": "API game trả dữ liệu không hợp lệ"}
        api_logger.log_response(request_id="login", status=resp.status_code,
                                body=result, elapsed=elapsed, account=username)
        if not result.get("success"):
            return {"status": "error",
                    "message": result.get("message") or "Sai tài khoản hoặc mật khẩu game!"}
        user_info = {}
        session_key = result.get("sessionKey", "")
        if session_key:
            try:
                user_info = json.loads(base64.b64decode(session_key).decode("utf-8"))
            except Exception:
                pass
        nickname = user_info.get("nickname") or username
        vin = user_info.get("vinTotal", 0)
        xu = user_info.get("xuTotal", 0)
        account = account_manager.get_or_create(username, pw_hash, nickname, vin, xu)
        access_token = result.get("accessToken") or result.get("access_token")
        if access_token:
            account.access_token = access_token
            account.access_token_time = time.time()
            api_logger.log("success", "Đã capture Access Token từ login",
                           account=username)
        return {"status": "ok", "username": nickname, "balance": vin, "xuTotal": xu,
                "access_token_ready": bool(account.access_token),
                "login_time": account.login_time,
                "_pw_hash": pw_hash}
    except Exception as e:
        api_logger.log("error", f"login_game exception: {type(e).__name__}: {e}")
        return {"status": "error", "message": f"Lỗi kết nối: {e}"}


# ============ TELEGRAM CODE ENGINE ============
async def _async_fetch_codes(client, session_data):
    scan_log = []
    now = time.time()

    def _log(level, msg):
        scan_log.append({"ts": time.time(), "level": level, "message": msg})

    _log("info", "Bắt đầu quét kênh LC79")
    channel_entity = None
    try:
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.id == -1002237214360 or (
                dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame'
            ):
                channel_entity = dialog.entity
                break
    except Exception as e:
        _log("warning", f"iter_dialogs: {str(e)[:80]}")
    if not channel_entity:
        try:
            channel_entity = await client.get_entity('codelaucuagame')
            _log("info", "Resolve username thành công")
        except Exception as e:
            _log("warning", f"Resolve username fail: {str(e)[:80]}")
    if not channel_entity:
        try:
            channel_entity = await client.get_entity(-1002237214360)
            _log("info", "Resolve ID thành công")
        except Exception:
            pass
    if not channel_entity:
        _log("error", "Không tìm thấy kênh LC79")
        return {"status": "error",
                "message": "Không tìm thấy kênh LC79. Tài khoản chưa join hoặc kênh ẩn.",
                "scan_log": scan_log}
    _log("success", "Đã resolve kênh LC79")
    try:
        await client(functions.channels.JoinChannelRequest(channel=channel_entity))
    except Exception:
        pass

    session_data.setdefault("codes_seen", {})
    items = []
    seen_in_scan = set()
    msg_count = 0

    async for msg in client.iter_messages(channel_entity, limit=100):
        msg_count += 1
        raw_text = getattr(msg, 'message', '') or getattr(msg, 'text', '') or ''
        if not raw_text:
            continue
        msg_date = msg.date.timestamp() if getattr(msg, 'date', None) else now
        for raw in extract_code_candidates(raw_text):
            variants = code_variants(raw)
            if not variants:
                continue
            canonical = variants[-1] if len(variants) > 1 else variants[0]
            if canonical in seen_in_scan:
                continue
            seen_in_scan.add(canonical)
            is_new = canonical not in session_data["codes_seen"]
            first_seen = session_data["codes_seen"].get(canonical, {}).get(
                "first_seen", now)
            if is_new:
                session_data["codes_seen"][canonical] = {
                    "first_seen": now, "msg_date": msg_date,
                    "msg_id": getattr(msg, "id", 0),
                }
                first_seen = now
            items.append({
                "code": canonical, "raw": raw,
                "found_at": now, "first_seen": first_seen,
                "msg_date": msg_date, "msg_id": getattr(msg, "id", 0),
                "status": "new" if is_new else "seen",
            })
            if len(items) >= 10:
                break
        if len(items) >= 10:
            break

    _log("info", f"Đã quét {msg_count} tin, tìm {len(items)} mã")
    if not items:
        _log("warning", "Không có mã LC79 trong 100 tin gần nhất")
        return {"status": "error",
                "message": "Không tìm thấy code LC79 trong 100 tin gần nhất.",
                "scan_log": scan_log}

    new_count = sum(1 for it in items if it["status"] == "new")
    _log("success", f"Hoàn tất: {len(items)} mã ({new_count} mới)")
    session_data.setdefault("scan_log", [])
    session_data["scan_log"].extend(scan_log)
    session_data["scan_log"] = session_data["scan_log"][-300:]

    return {"status": "ok",
            "codes": [it["code"] for it in items],
            "items": items, "new_count": new_count, "total_count": len(items),
            "scan_log": scan_log}


# ================================================================
#                   AUTO-REDEEM WORKER
# ================================================================
def _add_auto_event(session_id, event):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return
    s.setdefault("auto_events", [])
    s["auto_events"].append(event)
    if len(s["auto_events"]) > 100:
        s["auto_events"] = s["auto_events"][-100:]


def _auto_redeem_worker(session_id, candidates, raw_code, msg_ts):
    try:
        if not _try_claim_code(session_id, candidates):
            return
        _mark_dirty()

        with SESSIONS_LOCK:
            s = sessions.get(session_id)
        if not s:
            return
        username = s.get("game_username")
        if not username:
            return

        account = account_manager.get(username)
        if not account:
            return

        fire_ts = time.time()
        api_logger.log("info",
                       f"⚡ Auto-redeem: {candidates[0]} (raw={raw_code})",
                       account=username, candidates=candidates)
        result = redeem_code_fast(account, candidates)
        elapsed = time.time() - fire_ts
        total_elapsed = time.time() - msg_ts
        http = result.get("http", 0)
        body = result.get("body") or {}
        is_ok = http in (200, 201)
        amount = None
        msg = result.get("message")
        if isinstance(body, dict):
            amount = (body.get("money") or body.get("amount")
                      or body.get("reward")
                      or (body.get("data") or {}).get("money"))
            msg = msg or body.get("message")

        _add_auto_event(session_id, {
            "ts": fire_ts, "msg_ts": msg_ts,
            "code": candidates[0] if candidates else raw_code,
            "raw_code": raw_code,
            "candidates": candidates, "http": http,
            "elapsed_ms": int(elapsed * 1000),
            "total_ms": int(total_elapsed * 1000),
            "delay_ms": int((fire_ts - msg_ts) * 1000),
            "status": "success" if is_ok else "fail",
            "amount": amount, "message": msg,
        })
        api_logger.log("success" if is_ok else "error",
                       f"Redeem {candidates[0]}: HTTP {http} ({int(elapsed*1000)}ms)",
                       account=username)
    except Exception as e:
        logger.error(f"_auto_redeem_worker err: {e}")


# ================================================================
#              FAST POLL LOOP — 0.1s REAL-TIME BACKUP
# ================================================================
async def _fast_poll_loop(session_id):
    logger.info(f"⚡ Fast-poll ON (interval={FAST_POLL_INTERVAL}s) for session {session_id[:8]}")

    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return

    client = s["client"]
    channel_entity = s.get("auto_watch_channel")
    if not channel_entity:
        channel_entity = await _async_resolve_channel(client)
        if not channel_entity:
            logger.warning(f"Fast-poll: no channel for {session_id[:8]}")
            return
        s["auto_watch_channel"] = channel_entity

    last_seen_id = 0
    try:
        msgs = await client.get_messages(channel_entity, limit=1)
        if msgs:
            last_seen_id = msgs[0].id
    except Exception as e:
        logger.debug(f"init last_seen_id fail: {e}")

    consecutive_empty = 0

    while True:
        with SESSIONS_LOCK:
            s = sessions.get(session_id)
            if not s or not s.get("auto_watch_handler"):
                break
        if not client.is_connected():
            try:
                await client.connect()
                logger.info(f"♻️ Fast-poll reconnected {session_id[:8]}")
            except Exception as e:
                logger.debug(f"Fast-poll reconnect fail: {e}")
                await asyncio.sleep(2.0)
                continue

        try:
            msgs = await client.get_messages(
                channel_entity, limit=FAST_POLL_BATCH, min_id=last_seen_id
            )
            if msgs:
                consecutive_empty = 0
                for msg in reversed(msgs):
                    last_seen_id = max(last_seen_id, msg.id)
                    text = msg.message or ""
                    for raw in extract_code_candidates(text):
                        variants = code_variants_auto(raw)
                        if not variants:
                            continue
                        threading.Thread(
                            target=_auto_redeem_worker,
                            args=(session_id, variants, raw, time.time()),
                            daemon=True,
                        ).start()
            else:
                consecutive_empty += 1
        except FloodWaitError as e:
            wait = min(int(e.seconds) + 1, 60)
            logger.warning(f"Fast-poll FloodWait {wait}s (session {session_id[:8]})")
            await asyncio.sleep(wait)
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Fast-poll err (session {session_id[:8]}): {type(e).__name__}: {e}")
            await asyncio.sleep(1.0)
            continue

        if consecutive_empty > FAST_POLL_IDLE_BACKOFF:
            await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(FAST_POLL_INTERVAL)

    logger.info(f"🛑 Fast-poll OFF for session {session_id[:8]}")


async def _async_resolve_channel(client, session_data=None):
    channel = None
    try:
        async for dialog in client.iter_dialogs(limit=200):
            if (dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame') \
               or dialog.id == -1002237214360:
                channel = dialog.entity
                break
    except Exception:
        pass
    if not channel:
        try: channel = await client.get_entity('codelaucuagame')
        except Exception: pass
    if not channel:
        try: channel = await client.get_entity(-1002237214360)
        except Exception: pass
    return channel


# ================================================================
#                   AUTO-WATCH (REAL-TIME)
# ================================================================
async def _async_start_auto_watch(session_id):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return {"status": "error", "message": "Session không tồn tại"}
    if s.get("auto_watch_handler"):
        return {"status": "ok", "message": "Đã bật từ trước"}

    username = s.get("game_username")
    if not username:
        return {"status": "error", "message": "Chưa login game — không thể bật auto"}
    account = account_manager.get(username)
    if not account:
        return {"status": "error", "message": "Không tìm thấy account"}

    if not account.is_jwt_valid():
        r = account_manager.refresh_jwt(account)
        if r.get("status") != "ok":
            return {"status": "error",
                    "message": f"Không lấy được JWT: {r.get('message', '')}"}

    client = s["client"]
    if not client.is_connected():
        try:
            await client.connect()
        except Exception as e:
            return {"status": "error", "message": f"Mất kết nối: {e}"}

    channel_entity = await _async_resolve_channel(client)
    if not channel_entity:
        return {"status": "error", "message": "Không tìm thấy kênh LC79"}

    try:
        await client(functions.channels.JoinChannelRequest(channel=channel_entity))
    except Exception:
        pass

    async def on_new_msg(event):
        try:
            text = event.raw_text or ""
            raw_codes = extract_code_candidates(text)
            if not raw_codes:
                return
            received_ts = time.time()
            logger.info(f"📥 [AUTO-EVENT] {len(raw_codes)} code từ tin mới")
            for raw in raw_codes:
                variants = code_variants_auto(raw)
                threading.Thread(
                    target=_auto_redeem_worker,
                    args=(session_id, variants, raw, received_ts),
                    daemon=True,
                ).start()
        except Exception as e:
            logger.error(f"on_new_msg err: {e}")

    client.add_event_handler(on_new_msg, events.NewMessage(chats=[channel_entity]))
    s["auto_watch_handler"] = on_new_msg
    s["auto_watch_channel"] = channel_entity

    try:
        task_future = async_manager.submit_task(_fast_poll_loop(session_id))
        s["fast_poll_task"] = task_future
    except Exception as e:
        logger.warning(f"cannot start fast-poll: {e}")

    threading.Thread(target=_warm_redeem_connection, daemon=True).start()
    logger.info(f"🎯 Auto-watch ON: {session_id[:8]} / {username}")
    return {"status": "ok", "username": username, "poll_interval": FAST_POLL_INTERVAL}


async def _async_stop_auto_watch(session_id):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return {"status": "error", "message": "Session không tồn tại"}
    handler = s.get("auto_watch_handler")
    if not handler:
        return {"status": "ok", "message": "Chưa bật"}

    try:
        s["client"].remove_event_handler(handler)
    except Exception:
        pass
    s["auto_watch_handler"] = None
    s["auto_watch_channel"] = None

    task_future = s.get("fast_poll_task")
    if task_future:
        try:
            task_future.cancel()
        except Exception:
            pass
    s["fast_poll_task"] = None

    logger.info(f"🛑 Auto-watch OFF: {session_id[:8]}")
    return {"status": "ok"}


async def _async_force_scan(session_id):
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return {"status": "error", "message": "Session hết hạn"}
    username = s.get("game_username")
    if not username:
        return {"status": "error", "message": "Chưa login game"}
    if not account_manager.get(username):
        return {"status": "error", "message": "Account không tồn tại"}

    client = s["client"]
    if not client.is_connected():
        try:
            await client.connect()
        except Exception as e:
            return {"status": "error", "message": f"Mất kết nối: {e}"}

    channel_entity = s.get("auto_watch_channel") or await _async_resolve_channel(client)
    if not channel_entity:
        return {"status": "error", "message": "Không tìm thấy kênh"}

    fired = []
    now = time.time()

    async for msg in client.iter_messages(channel_entity, limit=20):
        text = msg.message or ""
        for raw in extract_code_candidates(text):
            variants = code_variants_auto(raw)
            with SESSIONS_LOCK:
                redeemed = s.get("redeemed_codes", [])
            if any(v in redeemed for v in variants):
                continue
            threading.Thread(
                target=_auto_redeem_worker,
                args=(session_id, variants, raw, now),
                daemon=True,
            ).start()
            fired.append(raw)

    logger.info(f"🔔 Force scan: fired {len(fired)} code")
    return {"status": "ok", "fired": len(fired), "codes": fired}


# ================================================================
#                    BACKGROUND WORKERS
# ================================================================
def jwt_prewarm_loop():
    time.sleep(20)
    while True:
        try:
            time.sleep(45)
            with SESSIONS_LOCK:
                active_ids = [sid for sid, s in sessions.items()
                              if s.get("auto_watch_handler")]
            for sid in active_ids:
                with SESSIONS_LOCK:
                    s = sessions.get(sid)
                if not s:
                    continue
                username = s.get("game_username")
                if not username:
                    continue
                acc = account_manager.get(username)
                if not acc or not acc.jwt_exp:
                    continue
                remain = acc.jwt_exp - time.time()
                if remain < 300:
                    try:
                        r = account_manager.refresh_jwt(acc)
                        if r.get("status") == "ok":
                            logger.info(f"🔄 Pre-warmed JWT: {username}")
                            pt = s.get("persist_token")
                            if pt:
                                _update_persist(pt,
                                    access_token=acc.access_token,
                                    access_token_time=acc.access_token_time,
                                    jwt=acc.jwt, jwt_exp=acc.jwt_exp)
                        else:
                            logger.warning(f"Prewarm fail {username}: {r.get('message')}")
                    except Exception as e:
                        logger.warning(f"Prewarm err: {e}")
        except Exception as e:
            logger.error(f"jwt_prewarm_loop: {e}")


def session_keepalive_loop():
    time.sleep(30)
    while True:
        try:
            time.sleep(60)
            with SESSIONS_LOCK:
                active = [(sid, s) for sid, s in sessions.items()
                          if s.get("auto_watch_handler")]
            for sid, s in active:
                client = s.get("client")
                if not client:
                    continue
                try:
                    if not client.is_connected():
                        async_manager.run(client.connect(), timeout=20)
                        logger.info(f"♻️ Reconnected {sid[:8]}")
                except Exception as e:
                    logger.warning(f"reconnect fail {sid[:8]}: {e}")
        except Exception as e:
            logger.error(f"session_keepalive: {e}")


def restore_persistent_sessions_loop():
    time.sleep(10)
    with PERSIST_LOCK:
        items = [(t, dict(p)) for t, p in persistent_sessions.items()
                 if p.get("auto_enabled")]
    if not items:
        logger.info("♻️ No persistent sessions to restore")
        return
    logger.info(f"♻️ Restoring {len(items)} persistent session(s)...")
    for token, p in items:
        try:
            async def _restore(token=token, p=p):
                client = TelegramClient(
                    StringSession(p["session_string"]),
                    int(p["api_id"]), p["api_hash"]
                )
                await client.connect()
                if not await client.is_user_authorized():
                    logger.warning(f"session {token[:8]} unauthorized")
                    return None
                if p.get("game_username") and p.get("game_pw_hash"):
                    acc = account_manager.get_or_create(
                        p["game_username"], p["game_pw_hash"],
                        p.get("game_nickname") or p["game_username"],
                        p.get("game_vin", 0), p.get("game_xu", 0),
                    )
                    acc.access_token = p.get("access_token")
                    acc.access_token_time = p.get("access_token_time", 0)
                    acc.jwt = p.get("jwt")
                    acc.jwt_exp = p.get("jwt_exp", 0)
                new_sid = str(uuid.uuid4())
                with SESSIONS_LOCK:
                    sessions[new_sid] = {
                        "client": client,
                        "phone": p.get("phone", ""),
                        "phone_code_hash": None,
                        "created": time.time(),
                        "codes_seen": {}, "scan_log": [],
                        "game_username": p.get("game_username"),
                        "auto_events": [], 
                        "redeemed_codes": list(p.get("redeemed_codes", [])),
                        "auto_watch_handler": None,
                        "fast_poll_task": None,
                        "persist_token": token,
                        "api_id": p.get("api_id"),
                        "api_hash": p.get("api_hash"),
                    }
                try:
                    await _async_start_auto_watch(new_sid)
                except Exception as e:
                    logger.warning(f"auto-watch restore fail {token[:8]}: {e}")
                return new_sid
            new_sid = async_manager.run(_restore(), timeout=90)
            if new_sid:
                logger.info(f"♻️ Restored {token[:8]} -> {new_sid[:8]}")
        except Exception as e:
            logger.warning(f"restore {token[:8]} fail: {e}")


# ================================================================
#                    API ENDPOINTS
# ================================================================
@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    data = request.json or {}
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    phone = data.get("phone")
    if not all([api_id, api_hash, phone]):
        return jsonify({"status": "error", "message": "Vui lòng nhập đủ thông tin."})
    phone = phone.strip().replace(" ", "").replace("-", "")

    async def _do():
        client = None
        try:
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            session_id = str(uuid.uuid4())
            with SESSIONS_LOCK:
                sessions[session_id] = {
                    "client": client, "phone": phone,
                    "phone_code_hash": sent.phone_code_hash,
                    "created": time.time(),
                    "codes_seen": {}, "scan_log": [],
                    "game_username": None,
                    "auto_events": [], "redeemed_codes": [],
                    "auto_watch_handler": None,
                    "fast_poll_task": None,
                    "persist_token": None,
                    "api_id": int(api_id), "api_hash": api_hash,
                }
            logger.info(f"📱 Session {session_id[:8]} created for {phone}")
            return {"status": "ok", "session_id": session_id}
        except PhoneNumberInvalidError:
            if client and client.is_connected():
                try: await client.disconnect()
                except Exception: pass
            return {"status": "error", "message": "SĐT không hợp lệ. Nhập dạng +84987654321."}
        except FloodWaitError as e:
            if client and client.is_connected():
                try: await client.disconnect()
                except Exception: pass
            return {"status": "error", "message": f"Telegram giới hạn, chờ {e.seconds}s."}
        except Exception as e:
            if client and client.is_connected():
                try: await client.disconnect()
                except Exception: pass
            logger.error(f"send_code err: {type(e).__name__}: {e}")
            return {"status": "error", "message": f"Lỗi: {type(e).__name__}: {e}"}

    try:
        result = async_manager.run(_do(), timeout=45)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json or {}
    session_id = data.get("session_id")
    code = (data.get("code") or "").strip().replace(" ", "").replace("-", "")
    if not session_id or not code:
        return jsonify({"status": "error", "message": "Thiếu session hoặc mã OTP."})
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return jsonify({"status": "error", "message": "Phiên đã hết hạn."})

    async def _do():
        client = s["client"]
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            return {"status": "error", "message": "Mất kết nối Telegram."}
        try:
            await client.sign_in(phone=s["phone"], code=code,
                                 phone_code_hash=s["phone_code_hash"])
            logger.info(f"✅ OTP OK session {session_id[:8]}")
            s["phone_code_hash"] = None
            persist_token = _create_persist_token(s)
            if persist_token:
                s["persist_token"] = persist_token
            return {"status": "ok", "persist_token": persist_token}
        except SessionPasswordNeededError:
            return {"status": "2fa"}
        except PhoneCodeInvalidError:
            return {"status": "error", "message": "Mã OTP không đúng. Nhập lại."}
        except PhoneCodeExpiredError:
            return {"status": "error", "message": "Mã OTP đã hết hạn."}
        except FloodWaitError as e:
            return {"status": "error", "message": f"Thử quá nhiều. Chờ {e.seconds}s."}
        except AuthRestartError:
            return {"status": "error", "message": "Telegram yêu cầu restart."}
        except Exception as e:
            return {"status": "error", "message": f"Lỗi: {type(e).__name__}: {str(e)[:200]}"}

    try:
        result = async_manager.run(_do(), timeout=45)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json or {}
    session_id = data.get("session_id")
    password = data.get("password") or ""
    if not session_id or not password:
        return jsonify({"status": "error", "message": "Thiếu session hoặc mật khẩu."})
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return jsonify({"status": "error", "message": "Phiên đã hết hạn."})

    async def _do():
        client = s["client"]
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            return {"status": "error", "message": "Mất kết nối."}
        try:
            await client.sign_in(password=password)
            logger.info(f"✅ 2FA OK session {session_id[:8]}")
            persist_token = _create_persist_token(s)
            if persist_token:
                s["persist_token"] = persist_token
            return {"status": "ok", "persist_token": persist_token}
        except Exception as e:
            logger.warning(f"2FA fail: {type(e).__name__}: {e}")
            return {"status": "error", "message": "Mật khẩu 2FA không đúng."}

    try:
        result = async_manager.run(_do(), timeout=45)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


@flask_app.route("/api/login_game", methods=["POST"])
def api_login_game():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    session_id = data.get("session_id")
    persist_token = data.get("persist_token")
    if not username or not password:
        return jsonify({"status": "error", "message": "Vui lòng nhập đủ Username và Mật khẩu."})
    result = login_game_sync(username, password)

    if result.get("status") == "ok":
        pw_hash = result.pop("_pw_hash", None)
        if session_id:
            with SESSIONS_LOCK:
                s = sessions.get(session_id)
            if s:
                s["game_username"] = username
                if not s.get("persist_token") and persist_token:
                    s["persist_token"] = persist_token
        if persist_token:
            acc = account_manager.get(username)
            _update_persist(persist_token,
                game_username=username,
                game_pw_hash=pw_hash,
                game_nickname=result.get("username"),
                game_vin=result.get("balance", 0),
                game_xu=result.get("xuTotal", 0),
                access_token=acc.access_token if acc else None,
                access_token_time=acc.access_token_time if acc else 0,
            )
    return jsonify(result)


@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    data = request.json or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"status": "error", "message": "Thiếu session."})
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return jsonify({"status": "error", "message": "Phiên đã hết hạn."})

    async def _do():
        client = s["client"]
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            return {"status": "error", "message": "Mất kết nối."}
        return await _async_fetch_codes(client, s)

    try:
        result = async_manager.run(_do(), timeout=60)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


def _require_session_account(session_id):
    if not session_id:
        return None, None, {"status": "error", "message": "Thiếu session."}
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return None, None, {"status": "error", "message": "Phiên đã hết hạn."}
    username = s.get("game_username")
    if not username:
        return s, None, {"status": "error", "message": "Chưa đăng nhập game."}
    acc = account_manager.get(username)
    if not acc:
        return s, None, {"status": "error", "message": "Không tìm thấy tài khoản."}
    return s, acc, None


@flask_app.route("/api/get_access_token", methods=["POST"])
def api_get_access_token():
    data = request.json or {}
    s, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)
    result = account_manager.get_access_token(acc)
    if result.get("status") == "ok" and s and s.get("persist_token"):
        _update_persist(s["persist_token"],
            access_token=acc.access_token,
            access_token_time=acc.access_token_time)
    return jsonify(result)


@flask_app.route("/api/get_jwt", methods=["POST"])
def api_get_jwt():
    data = request.json or {}
    s, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)
    result = account_manager.get_jwt(acc, force=bool(data.get("force")))
    if result.get("status") == "ok" and s and s.get("persist_token"):
        _update_persist(s["persist_token"],
            access_token=acc.access_token,
            access_token_time=acc.access_token_time,
            jwt=acc.jwt, jwt_exp=acc.jwt_exp)
    return jsonify(result)


@flask_app.route("/api/refresh_jwt", methods=["POST"])
def api_refresh_jwt():
    data = request.json or {}
    s, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)
    result = account_manager.refresh_jwt(acc)
    if result.get("status") == "ok" and s and s.get("persist_token"):
        _update_persist(s["persist_token"],
            access_token=acc.access_token,
            access_token_time=acc.access_token_time,
            jwt=acc.jwt, jwt_exp=acc.jwt_exp)
    return jsonify(result)


# ==================== PERSIST / RESUME ====================

@flask_app.route("/api/resume", methods=["POST"])
def api_resume():
    data = request.json or {}
    persist_token = data.get("persist_token")
    if not persist_token:
        return jsonify({"status": "error", "message": "Thiếu persist_token"})
    with PERSIST_LOCK:
        p = persistent_sessions.get(persist_token)
    if not p:
        return jsonify({"status": "not_found", "message": "Token không tồn tại"})

    with SESSIONS_LOCK:
        active_sid = None
        for sid, s in sessions.items():
            if s.get("persist_token") == persist_token:
                client = s.get("client")
                try:
                    if client and client.is_connected():
                        active_sid = sid
                        break
                except Exception:
                    continue

    if active_sid:
        sid = active_sid
    else:
        async def _do():
            try:
                client = TelegramClient(
                    StringSession(p["session_string"]),
                    int(p["api_id"]), p["api_hash"]
                )
                await client.connect()
                if not await client.is_user_authorized():
                    return {"status": "expired"}
                if p.get("game_username") and p.get("game_pw_hash"):
                    acc = account_manager.get_or_create(
                        p["game_username"], p["game_pw_hash"],
                        p.get("game_nickname") or p["game_username"],
                        p.get("game_vin", 0), p.get("game_xu", 0),
                    )
                    acc.access_token = p.get("access_token")
                    acc.access_token_time = p.get("access_token_time", 0)
                    acc.jwt = p.get("jwt")
                    acc.jwt_exp = p.get("jwt_exp", 0)

                new_sid = str(uuid.uuid4())
                with SESSIONS_LOCK:
                    sessions[new_sid] = {
                        "client": client,
                        "phone": p.get("phone", ""),
                        "phone_code_hash": None,
                        "created": time.time(),
                        "codes_seen": {}, "scan_log": [],
                        "game_username": p.get("game_username"),
                        "auto_events": [], 
                        "redeemed_codes": list(p.get("redeemed_codes", [])),
                        "auto_watch_handler": None,
                        "fast_poll_task": None,
                        "persist_token": persist_token,
                        "api_id": p.get("api_id"),
                        "api_hash": p.get("api_hash"),
                    }
                return {"status": "ok", "session_id": new_sid}
            except Exception as e:
                logger.error(f"resume err: {e}")
                return {"status": "error", "message": str(e)}

        try:
            result = async_manager.run(_do(), timeout=45)
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)})
        if result.get("status") != "ok":
            return jsonify(result)
        sid = result["session_id"]

    if p.get("auto_enabled") and p.get("game_username"):
        try:
            async_manager.run(_async_start_auto_watch(sid), timeout=30)
        except Exception as e:
            logger.warning(f"resume auto-watch fail: {e}")

    with SESSIONS_LOCK:
        s = sessions.get(sid) or {}
    acc = account_manager.get(p.get("game_username")) if p.get("game_username") else None

    return jsonify({
        "status": "ok",
        "session_id": sid,
        "persist_token": persist_token,
        "game_username": p.get("game_username"),
        "nickname": p.get("game_nickname"),
        "vin": p.get("game_vin", 0),
        "xu": p.get("game_xu", 0),
        "login_time": acc.login_time if acc else 0,
        "access_token_ready": bool(acc and acc.access_token),
        "jwt_ready": bool(acc and acc.jwt),
        "jwt_valid": acc.is_jwt_valid() if acc else False,
        "jwt_exp": acc.jwt_exp if acc else 0,
        "jwt_expires_in_minutes": acc.jwt_remaining_minutes() if acc else 0,
        "auto_enabled": bool(s.get("auto_watch_handler")),
        "fast_poll_interval": FAST_POLL_INTERVAL,
    })


@flask_app.route("/api/forget", methods=["POST"])
def api_forget():
    data = request.json or {}
    persist_token = data.get("persist_token")
    session_id = data.get("session_id")
    if not persist_token:
        return jsonify({"status": "error", "message": "Thiếu persist_token"})

    to_remove = []
    with SESSIONS_LOCK:
        for sid, s in sessions.items():
            if s.get("persist_token") == persist_token or sid == session_id:
                to_remove.append(sid)
    for sid in to_remove:
        with SESSIONS_LOCK:
            s = sessions.pop(sid, None)
        if s:
            task = s.get("fast_poll_task")
            if task:
                try: task.cancel()
                except Exception: pass
            if s.get("client"):
                try:
                    async_manager.run(s["client"].disconnect(), timeout=10)
                except Exception:
                    pass

    with PERSIST_LOCK:
        persistent_sessions.pop(persist_token, None)
    _save_persist()
    logger.info(f"🚪 Forgot persist_token {persist_token[:8]}")
    return jsonify({"status": "ok"})


# ==================== MANUAL / AUTO / FORCE ====================

@flask_app.route("/api/redeem_manual", methods=["POST"])
def api_redeem_manual():
    data = request.json or {}
    session_id = data.get("session_id")
    code_input = (data.get("code") or "").strip().upper()
    if not session_id or not code_input:
        return jsonify({"status": "error", "message": "Thiếu session hoặc code"})
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return jsonify({"status": "error", "message": "Session hết hạn"})
    username = s.get("game_username")
    if not username:
        return jsonify({"status": "error", "message": "Chưa login game"})
    acc = account_manager.get(username)
    if not acc:
        return jsonify({"status": "error", "message": "Không tìm thấy account"})

    variants = code_variants(code_input)
    start = time.time()
    result = redeem_code_fast(acc, variants)
    elapsed = time.time() - start
    http = result.get("http", 0)
    body = result.get("body") or {}
    is_ok = http in (200, 201)
    amount = (body.get("money") or body.get("amount")
              or body.get("reward")
              or (body.get("data") or {}).get("money")) if isinstance(body, dict) else None
    msg = body.get("message") if isinstance(body, dict) else result.get("message")

    _add_auto_event(session_id, {
        "ts": time.time(), "code": code_input, "candidates": variants,
        "http": http, "elapsed_ms": int(elapsed * 1000),
        "status": "success" if is_ok else "fail",
        "amount": amount, "message": msg, "manual": True,
    })
    return jsonify({**result, "amount": amount})


@flask_app.route("/api/toggle_auto", methods=["POST"])
def api_toggle_auto():
    data = request.json or {}
    session_id = data.get("session_id")
    enabled = bool(data.get("enabled"))
    if not session_id:
        return jsonify({"status": "error", "message": "Thiếu session"})
    with SESSIONS_LOCK:
        if session_id not in sessions:
            return jsonify({"status": "error", "message": "Session hết hạn"})
    try:
        if enabled:
            result = async_manager.run(_async_start_auto_watch(session_id), timeout=30)
        else:
            result = async_manager.run(_async_stop_auto_watch(session_id), timeout=15)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server: {e}"})
    if result.get("status") == "ok":
        with SESSIONS_LOCK:
            s = sessions.get(session_id)
        pt = s.get("persist_token") if s else None
        if pt:
            _update_persist(pt, auto_enabled=enabled)
    return jsonify(result)


@flask_app.route("/api/auto_status", methods=["POST"])
def api_auto_status():
    data = request.json or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"status": "error", "message": "Thiếu session"})
    with SESSIONS_LOCK:
        s = sessions.get(session_id)
    if not s:
        return jsonify({"status": "error", "message": "Session hết hạn"})
    username = s.get("game_username")
    acc = account_manager.get(username) if username else None
    return jsonify({
        "status": "ok",
        "enabled": bool(s.get("auto_watch_handler")),
        "events": s.get("auto_events", [])[-25:],
        "jwt_ready": bool(acc and acc.is_jwt_valid()),
        "jwt_remaining_minutes": acc.jwt_remaining_minutes() if acc else 0,
        "poll_interval": FAST_POLL_INTERVAL,
    })


@flask_app.route("/api/force_scan", methods=["POST"])
def api_force_scan():
    data = request.json or {}
    session_id = data.get("session_id")
    if not session_id:
        return jsonify({"status": "error", "message": "Thiếu session"})
    try:
        result = async_manager.run(_async_force_scan(session_id), timeout=30)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    return jsonify(result)


@flask_app.route("/api/check_api", methods=["POST"])
def api_check_api():
    results = {}
    overall_ok = True
    start = time.time()
    try:
        resp = requests.get(GAME_API_URL,
            params={"c": "3", "un": "", "pw": "", "cp": "R",
                    "cl": "R", "pf": "web", "at": ""}, timeout=10)
        results["game_api"] = {"name": "Game API", "url": GAME_API_URL,
            "http": resp.status_code,
            "elapsed_ms": int((time.time() - start) * 1000),
            "ok": resp.status_code < 500}
    except Exception as e:
        results["game_api"] = {"name": "Game API", "url": GAME_API_URL,
            "error": str(e)[:150], "ok": False}
        overall_ok = False
    start = time.time()
    try:
        resp = requests.post(LOGIN_URL, json={}, timeout=10)
        results["login_url"] = {"name": "JWT Login", "url": LOGIN_URL,
            "http": resp.status_code,
            "elapsed_ms": int((time.time() - start) * 1000),
            "ok": resp.status_code < 500}
    except Exception as e:
        results["login_url"] = {"name": "JWT Login", "url": LOGIN_URL,
            "error": str(e)[:150], "ok": False}
        overall_ok = False
    start = time.time()
    try:
        resp = _fast_session.head(REDEEM_BASE_URL, timeout=5, allow_redirects=False)
        results["redeem_api"] = {"name": "Redeem API", "url": REDEEM_BASE_URL,
            "http": resp.status_code,
            "elapsed_ms": int((time.time() - start) * 1000),
            "ok": resp.status_code < 500}
    except Exception as e:
        results["redeem_api"] = {"name": "Redeem API", "url": REDEEM_BASE_URL,
            "error": str(e)[:150], "ok": False}
        overall_ok = False
    return jsonify({"status": "ok" if overall_ok else "warning",
                    "results": results, "ts": time.time()})


@flask_app.route("/api/api_log", methods=["GET"])
def api_api_log():
    limit = request.args.get("limit", 200, type=int)
    return jsonify({"status": "ok", "logs": api_logger.get_logs(limit=limit),
                    "stats": api_logger.get_stats()})


@flask_app.route("/api/clear_log", methods=["POST"])
def api_clear_log():
    api_logger.clear()
    return jsonify({"status": "ok", "message": "Đã xóa log"})


@flask_app.route("/api/stats", methods=["GET"])
def api_stats():
    return jsonify({"status": "ok", "stats": api_logger.get_stats()})


# ================================================================
#                    WEB UI
# ================================================================
MAIN_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#05050a">
<title>Auto Tool VIP — Real-time 24/7</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
:root{
  --bg:#05050a; --card:rgba(20,24,39,0.6);
  --accent:#8b5cf6; --accent-glow:rgba(139,92,246,0.4);
  --accent2:#06b6d4; --text:#f8fafc; --muted:#94a3b8;
  --border:rgba(255,255,255,0.08); --danger:#ef4444; --success:#10b981;
  --warning:#f59e0b;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{touch-action:pan-x pan-y;height:100%}
body{font-family:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:20px 16px 60px;overflow-x:hidden;background-image:radial-gradient(circle at 0% 0%,rgba(139,92,246,0.15) 0%,transparent 50%),radial-gradient(circle at 100% 100%,rgba(6,182,212,0.15) 0%,transparent 50%);background-attachment:fixed}
body>*{position:relative;z-index:1}
.icon{width:24px;height:24px;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;fill:none}
.icon-sm{width:18px;height:18px}
.icon-lg{width:32px;height:32px}
.view{display:none;animation:viewIn .4s cubic-bezier(0.16,1,0.3,1)}
.view.active{display:block}
@keyframes viewIn{from{opacity:0;transform:translateY(15px) scale(0.98)}to{opacity:1;transform:none}}
.header{text-align:center;padding:30px 0 20px}
.logo{display:inline-flex;align-items:center;justify-content:center;width:80px;height:80px;border-radius:24px;background:linear-gradient(135deg,rgba(139,92,246,0.2),rgba(6,182,212,0.2));border:1px solid rgba(139,92,246,0.4);color:var(--accent);margin-bottom:20px;box-shadow:0 10px 40px rgba(139,92,246,0.2);backdrop-filter:blur(10px)}
.header h1{font-size:26px;font-weight:800;letter-spacing:-0.8px;margin-bottom:8px}
.header h1 span{background:linear-gradient(to right,#a78bfa,#22d3ee);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{color:var(--muted);font-size:14px;font-weight:500}
.steps{display:flex;justify-content:center;gap:6px;margin:20px 0 24px;flex-wrap:wrap}
.step-dot{width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,0.05);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:800;color:var(--muted);transition:0.3s}
.step-dot.done{background:var(--success);border-color:var(--success);color:#fff}
.step-dot.active{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 0 15px var(--accent-glow)}
.section-title{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin:24px 4px 16px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}
.lc-header{background:linear-gradient(135deg,rgba(30,35,55,0.8),rgba(20,24,39,0.6));border:1px solid var(--border);border-radius:24px;padding:32px 20px;margin-bottom:20px;text-align:center;backdrop-filter:blur(12px);position:relative;overflow:hidden}
.lc-header::before{content:'';position:absolute;top:0;left:0;width:100%;height:2px;background:linear-gradient(90deg,transparent,var(--accent),transparent)}
.lc-icon{display:inline-flex;align-items:center;justify-content:center;width:64px;height:64px;border-radius:20px;background:rgba(139,92,246,0.15);color:var(--accent);margin-bottom:16px;border:1px solid rgba(139,92,246,0.3)}
.lc-header h2{font-size:22px;font-weight:800;margin-bottom:8px;letter-spacing:-0.5px}
.lc-header p{font-size:13px;color:var(--muted);font-weight:500}
.panel{background:var(--card);border:1px solid var(--border);border-radius:24px;padding:24px 20px;backdrop-filter:blur(12px)}
.panel label{font-size:12px;font-weight:700;color:var(--muted);display:flex;align-items:center;gap:6px;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px}
.panel input{width:100%;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:14px;padding:16px;color:var(--text);font-size:15px;font-weight:500;outline:none;transition:0.3s;margin-bottom:20px;font-family:inherit}
.panel input:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(139,92,246,0.15);background:rgba(0,0,0,0.5)}
.panel input::placeholder{color:rgba(148,163,184,0.5)}
.btn{width:100%;padding:16px;border:none;border-radius:14px;font-size:16px;font-weight:700;cursor:pointer;transition:0.2s;display:flex;align-items:center;justify-content:center;gap:8px;color:#fff;background:linear-gradient(135deg,#8b5cf6,#6366f1);box-shadow:0 8px 24px var(--accent-glow);font-family:inherit}
.btn:active{transform:scale(0.97);box-shadow:none}
.btn:disabled{opacity:0.6;pointer-events:none}
.btn-ghost{background:rgba(255,255,255,0.05);border:1px solid var(--border);box-shadow:none}
.btn-danger{background:linear-gradient(135deg,#ef4444,#dc2626);box-shadow:0 8px 24px rgba(239,68,68,0.4)}
.btn-success{background:linear-gradient(135deg,#10b981,#059669);box-shadow:0 8px 24px rgba(16,185,129,0.4)}
.btn-warning{background:linear-gradient(135deg,#f59e0b,#d97706);box-shadow:0 8px 24px rgba(245,158,11,0.4)}
.alert{padding:14px 16px;border-radius:12px;font-size:13px;font-weight:600;margin-bottom:16px;display:none;line-height:1.5}
.alert.show{display:block}
.alert-err{background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#fca5a5}
.alert-ok{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#6ee7b7}
.code-box{background:rgba(0,0,0,0.3);border:1px solid rgba(16,185,129,0.3);border-radius:12px;padding:14px 16px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.code-box .code-content{flex:1;min-width:0}
.code-box .code-main{font-family:'Courier New',monospace;font-size:16px;color:#34d399;font-weight:800;letter-spacing:1.5px;word-break:break-all}
.code-box .code-meta{font-size:11px;color:var(--muted);margin-top:4px;letter-spacing:0;font-weight:600}
.code-badge{display:inline-block;padding:2px 8px;border-radius:6px;font-size:9px;font-weight:800;margin-left:6px;letter-spacing:0.5px;vertical-align:middle}
.code-badge.new{background:rgba(16,185,129,0.2);color:var(--success)}
.code-badge.seen{background:rgba(148,163,184,0.2);color:var(--muted)}
.copy-btn{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#34d399;padding:8px 12px;border-radius:8px;cursor:pointer;font-weight:700;font-size:11px;transition:0.2s;font-family:inherit;flex-shrink:0}
.copy-btn:active{background:#10b981;color:#000}
.info-card{background:linear-gradient(135deg,rgba(139,92,246,0.1),rgba(6,182,212,0.05));border:1px solid rgba(139,92,246,0.3);border-radius:20px;padding:20px;margin-bottom:16px}
.info-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);gap:10px}
.info-row:last-child{border-bottom:none;padding-bottom:0}
.info-row:first-child{padding-top:0}
.info-label{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;flex-shrink:0}
.info-value{font-size:14px;font-weight:800;text-align:right;word-break:break-all}
.token-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
@media(max-width:420px){.token-grid{grid-template-columns:1fr}}
.token-card{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:16px}
.token-card.ok{border-color:rgba(16,185,129,0.4)}
.token-card.err{border-color:rgba(239,68,68,0.4)}
.token-label{font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.token-status{font-size:12px;font-weight:700;margin-bottom:10px;line-height:1.5}
.token-status.ok{color:var(--success)}
.token-status.err{color:var(--danger)}
.token-status.pending{color:var(--muted)}
.token-card button{width:100%;padding:8px;font-size:11px;margin-top:6px;background:rgba(139,92,246,0.15);border:1px solid rgba(139,92,246,0.3);color:var(--accent);border-radius:8px;font-weight:700;cursor:pointer;font-family:inherit;transition:0.2s}
.token-card button:hover{background:rgba(139,92,246,0.25)}
.token-card button:active{transform:scale(0.97)}
.stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:20px}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px}
.stat-box .stat-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;font-weight:700;margin-bottom:4px}
.stat-box .stat-val{font-size:17px;font-weight:800;color:var(--text)}
.stat-box .stat-val.ok{color:var(--success)}
.stat-box .stat-val.err{color:var(--danger)}
.code-actions{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.code-actions button{flex:1;min-width:100px;padding:10px;background:rgba(255,255,255,0.05);border:1px solid var(--border);color:var(--text);border-radius:10px;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit;transition:0.2s}
.code-actions button:hover{background:rgba(255,255,255,0.1)}
.code-actions button:active{transform:scale(0.97)}
.code-actions button.primary{background:linear-gradient(135deg,#8b5cf6,#6366f1);border-color:transparent;box-shadow:0 4px 12px var(--accent-glow)}
.api-log{max-height:400px;overflow-y:auto;font-family:'Courier New',monospace;font-size:11px;background:rgba(0,0,0,0.5);border-radius:12px;padding:12px;line-height:1.7}
.api-log .log-entry{margin-bottom:6px;word-break:break-word;padding-bottom:4px;border-bottom:1px dashed rgba(255,255,255,0.04)}
.api-log .log-entry:last-child{border-bottom:none}
.log-info{color:#67e8f9}.log-success{color:#34d399}.log-warning{color:#fbbf24}
.log-error{color:#f87171}.log-request{color:#a78bfa}.log-response{color:#94a3b8}
.log-time{color:#64748b;margin-right:6px}
.log-tag{font-weight:800;margin-right:6px}
.auto-section{border:1px solid rgba(6,182,212,0.3);background:linear-gradient(135deg,rgba(6,182,212,0.08),rgba(139,92,246,0.05));border-radius:20px;padding:16px;margin-bottom:16px}
.auto-toggle-row{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px}
.auto-info{flex:1;min-width:0}
.auto-title{font-size:13px;font-weight:800;color:var(--text);margin-bottom:4px}
.auto-status{font-size:11px;color:var(--muted);font-weight:600}
.auto-btn{flex-shrink:0;padding:10px 20px;font-size:12px;font-weight:800;border-radius:10px;cursor:pointer;font-family:inherit;transition:0.2s;border:none}
.auto-btn[data-state="on"]{background:linear-gradient(135deg,#ef4444,#dc2626);color:#fff;box-shadow:0 4px 12px rgba(239,68,68,0.3)}
.auto-btn[data-state="off"]{background:linear-gradient(135deg,#10b981,#059669);color:#fff;box-shadow:0 4px 12px rgba(16,185,129,0.3)}
.realtime-badge{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:800;color:#22d3ee;background:rgba(34,211,238,0.1);border:1px solid rgba(34,211,238,0.3);border-radius:6px;padding:2px 6px;margin-left:6px;letter-spacing:0.5px}
.realtime-badge .dot{width:6px;height:6px;border-radius:50%;background:#22d3ee;box-shadow:0 0 6px #22d3ee;animation:pulse-rt 1.2s infinite}
@keyframes pulse-rt{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.4;transform:scale(1.4)}}
.manual-panel{background:linear-gradient(135deg,rgba(245,158,11,0.08),rgba(239,68,68,0.05));border:1px solid rgba(245,158,11,0.3);border-radius:20px;padding:16px;margin-bottom:16px}
.manual-panel input{margin-bottom:10px;padding:14px}
.manual-panel button{padding:14px;font-size:15px}
.force-panel{background:linear-gradient(135deg,rgba(6,182,212,0.1),rgba(139,92,246,0.05));border:1px solid rgba(6,182,212,0.4);border-radius:20px;padding:16px;margin-bottom:16px}
.force-panel p{font-size:11px;color:var(--muted);line-height:1.6;margin-bottom:10px}
.auto-log{max-height:240px;overflow-y:auto;font-size:12px;line-height:1.6}
.auto-log .event{padding:8px 10px;border-radius:8px;margin-bottom:6px;border-left:3px solid transparent}
.auto-log .event.success{background:rgba(16,185,129,0.08);border-left-color:#10b981}
.auto-log .event.fail{background:rgba(239,68,68,0.08);border-left-color:#ef4444}
.auto-log .event.skipped{background:rgba(148,163,184,0.08);border-left-color:#94a3b8}
.auto-log .event .ev-code{font-family:'Courier New',monospace;font-weight:800;color:#34d399;font-size:13px}
.auto-log .event .ev-meta{color:var(--muted);font-size:10px;margin-top:2px}
.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:40px;font-weight:500}
.footer a{color:var(--accent2);text-decoration:none}
.session-banner{background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:12px;padding:10px 14px;font-size:11px;color:#6ee7b7;margin-bottom:16px;display:none}
.session-banner.show{display:flex;justify-content:space-between;align-items:center;gap:10px}
.session-banner button{background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#fca5a5;padding:6px 10px;border-radius:8px;font-size:10px;font-weight:700;cursor:pointer;font-family:inherit}
</style>
</head>
<body>

<!-- HOME -->
<div id="view-home" class="view active">
  <div class="header">
    <div class="logo"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg></div>
    <h1>Auto Tool <span>VIP</span></h1>
    <p>Real-time 24/7 · Auto-redeem &lt;100ms</p>
  </div>
  <div class="panel" style="text-align:center">
    <div style="padding:20px 0">
      <div style="font-size:52px;margin-bottom:16px">🎮</div>
      <div style="font-size:16px;font-weight:700;margin-bottom:8px">Tool Auto LC79 VIP</div>
      <div style="font-size:13px;color:var(--muted);margin-bottom:24px;line-height:1.6">
        Đăng nhập 1 lần — dùng mãi mãi<br>
        <strong style="color:var(--accent2)">⚡ Event + Fast-poll 0.1s</strong><br>
        <strong style="color:var(--success)">💾 Tự cập nhật code mới không cần F5</strong><br>
        <strong style="color:var(--warning)">🔁 Auto-restore sau server restart</strong>
      </div>
    </div>
    <button class="btn" onclick="startFlow()">🚀 Bắt Đầu</button>
  </div>
  <div class="footer">Powered by <a href="/alive" target="_blank">Auto Tool Server</a></div>
</div>

<!-- STEP 1: API INFO -->
<div id="view-api" class="view">
  <button class="btn btn-ghost" style="margin-bottom:16px;padding:10px" onclick="showView('view-home')">← Quay lại</button>
  <div class="lc-header">
    <div class="lc-icon"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
    <h2>Xác Thực Telegram</h2>
    <p>Đăng nhập 1 lần — lưu lại dùng tiếp</p>
  </div>
  <div class="steps" id="steps1"></div>
  <div class="panel">
    <div id="alert-api" class="alert alert-err"></div>
    <label>API ID</label>
    <input type="number" id="apiId" placeholder="VD: 12345678" autocomplete="off">
    <label>API HASH</label>
    <input type="password" id="apiHash" placeholder="Nhập API HASH" autocomplete="off">
    <label>Số điện thoại</label>
    <input type="tel" id="phone" placeholder="+84987654321" autocomplete="off">
    <button class="btn" id="btn-send-otp" onclick="sendOTP()">Gửi OTP →</button>
  </div>
</div>

<!-- STEP 2: OTP -->
<div id="view-otp" class="view">
  <button class="btn btn-ghost" style="margin-bottom:16px;padding:10px" onclick="restart()">← Làm lại</button>
  <div class="lc-header">
    <div class="lc-icon" style="color:var(--accent2);background:rgba(6,182,212,0.1);border-color:rgba(6,182,212,0.3)"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg></div>
    <h2>Nhập Mã OTP</h2>
    <p>Kiểm tra Telegram để lấy mã</p>
  </div>
  <div class="steps" id="steps2"></div>
  <div class="panel">
    <div id="alert-otp" class="alert alert-err"></div>
    <label>Mã OTP</label>
    <input type="text" id="otpCode" placeholder="VD: 12345" inputmode="numeric" autocomplete="one-time-code" maxlength="7">
    <button class="btn" id="btn-verify-otp" onclick="verifyOTP()">Xác Nhận OTP</button>
  </div>
</div>

<!-- STEP 3: 2FA -->
<div id="view-2fa" class="view">
  <button class="btn btn-ghost" style="margin-bottom:16px;padding:10px" onclick="restart()">← Làm lại</button>
  <div class="lc-header" style="border-color:rgba(239,68,68,0.3)">
    <div class="lc-icon" style="color:var(--danger);background:rgba(239,68,68,0.1);border-color:rgba(239,68,68,0.3)"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
    <h2>Xác Thực 2 Lớp</h2>
    <p>Tài khoản được bảo vệ bằng 2FA</p>
  </div>
  <div class="steps" id="steps3"></div>
  <div class="panel">
    <div id="alert-2fa" class="alert alert-err"></div>
    <label>Mật khẩu 2FA</label>
    <input type="password" id="password2fa" placeholder="Nhập mật khẩu 2FA" autocomplete="off">
    <button class="btn" id="btn-verify-2fa" onclick="verify2FA()">Xác Nhận</button>
  </div>
</div>

<!-- STEP 4: GAME LOGIN -->
<div id="view-game" class="view">
  <button class="btn btn-ghost" style="margin-bottom:16px;padding:10px" onclick="restart()">← Làm lại</button>
  <div class="lc-header" style="border-color:rgba(6,182,212,0.3)">
    <div class="lc-icon" style="color:var(--accent2);background:rgba(6,182,212,0.1);border-color:rgba(6,182,212,0.3)"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h4m-2-2v4"/></svg></div>
    <h2>Đăng Nhập Game LC79</h2>
    <p>Nhập 1 lần — lưu vĩnh viễn</p>
  </div>
  <div class="steps" id="steps4"></div>
  <div class="panel">
    <div id="alert-game" class="alert alert-err"></div>
    <label>Username Game</label>
    <input type="text" id="gameUser" placeholder="VD: HuyDaiXuLc79" autocomplete="off">
    <label>Mật khẩu Game</label>
    <input type="password" id="gamePass" placeholder="Nhập mật khẩu game" autocomplete="off">
    <button class="btn" id="btn-game-login" onclick="gameLogin()">Đăng Nhập →</button>
  </div>
</div>

<!-- STEP 5: DASHBOARD -->
<div id="view-result" class="view">
  <div class="session-banner" id="session-banner">
    <span>💾 Phiên đã được lưu — lần sau không cần đăng nhập lại</span>
    <button onclick="forgetDevice()">Đăng xuất</button>
  </div>

  <div class="lc-header" style="border-color:rgba(16,185,129,0.3)">
    <div class="lc-icon" style="color:var(--success);background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.3)"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div>
    <h2>Dashboard VIP</h2>
    <p>Real-time · Auto-restore · No reset</p>
  </div>

  <div class="info-card">
    <div class="info-row"><span class="info-label">Tài khoản</span><span class="info-value" id="info-username">-</span></div>
    <div class="info-row"><span class="info-label">Số dư Vin</span><span class="info-value" id="info-balance" style="color:var(--success)">0</span></div>
    <div class="info-row"><span class="info-label">Xu</span><span class="info-value" id="info-xu" style="color:var(--accent2)">0</span></div>
    <div class="info-row"><span class="info-label">Đăng nhập lúc</span><span class="info-value" id="info-login-time" style="font-size:12px;color:var(--muted)">-</span></div>
  </div>

  <div class="section-title">Token Status</div>
  <div class="token-grid">
    <div class="token-card" id="token-card-at">
      <div class="token-label">Access Token</div>
      <div class="token-status pending" id="at-status">Chưa có</div>
      <button onclick="fetchAccessToken()" id="btn-get-at">GET TOKEN</button>
    </div>
    <div class="token-card" id="token-card-jwt">
      <div class="token-label">JWT Token</div>
      <div class="token-status pending" id="jwt-status">Chưa có</div>
      <button onclick="fetchJWT(false)" id="btn-get-jwt">GET JWT</button>
      <button onclick="fetchJWT(true)" id="btn-refresh-jwt">REFRESH</button>
    </div>
  </div>

  <div class="section-title">⚡ Auto Watch & Redeem</div>
  <div class="auto-section">
    <div class="auto-toggle-row">
      <div class="auto-info">
        <div class="auto-title">Auto Watch + Redeem
          <span class="realtime-badge"><span class="dot"></span>REAL-TIME</span>
        </div>
        <div class="auto-status" id="auto-status">⚪ Chưa bật</div>
      </div>
      <button class="auto-btn" id="btn-auto-toggle" data-state="off" onclick="toggleAuto()">BẬT</button>
    </div>
    <div style="font-size:11px;color:var(--muted);line-height:1.6">
      📡 Event handler (push, &lt;100ms) + Fast-poll 0.1s (safety net) — double layer, không bỏ lỡ code nào.<br>
      💾 Auto-watch <strong style="color:var(--success)">treo 24/7</strong> — tắt tab / reload / restart server vẫn tiếp tục chạy.<br>
      🚀 Bỏ prefix <strong style="color:var(--accent2)">LC79</strong> tự động trước khi gửi API.
    </div>
  </div>

  <div class="section-title">🔔 Nhắc Tool Kiểm Tra</div>
  <div class="force-panel">
    <p>Nhận thông báo có code mới nhưng web chưa kịp xử lý? Bấm nút này để <strong>quét lại 20 tin gần nhất</strong> và fire ngay các code chưa redeem.</p>
    <button class="btn btn-warning" id="btn-force-scan" onclick="forceScan()" style="font-size:14px">🔔 NHẮC TOOL CHECK CODE</button>
  </div>

  <div class="section-title">🎯 Nhập Code Thủ Công</div>
  <div class="manual-panel">
    <input type="text" id="manualCode" placeholder="VD: DVANXYZGE hoặc LC79DVANXYZGE" autocomplete="off" style="text-transform:uppercase">
    <button class="btn btn-success" id="btn-manual-redeem" onclick="redeemManual()">⚡ NHẬP NGAY</button>
  </div>

  <div class="section-title">Auto Redeem Log</div>
  <div class="panel" id="auto-events-panel" style="padding:10px">
    <div class="auto-log" id="auto-events">
      <div style="color:var(--muted);text-align:center;padding:12px">Chưa có hoạt động</div>
    </div>
  </div>

  <div class="section-title">API Statistics</div>
  <div class="stats-grid">
    <div class="stat-box"><div class="stat-lbl">Total Requests</div><div class="stat-val" id="stat-total">0</div></div>
    <div class="stat-box"><div class="stat-lbl">Success</div><div class="stat-val ok" id="stat-success">0</div></div>
    <div class="stat-box"><div class="stat-lbl">Failed</div><div class="stat-val err" id="stat-failed">0</div></div>
    <div class="stat-box"><div class="stat-lbl">Avg Time</div><div class="stat-val" id="stat-avg">0 ms</div></div>
  </div>

  <div class="section-title">Giftcode LC79</div>
  <div class="code-actions">
    <button onclick="fetchCodes()" class="primary">🔄 REFRESH CODE</button>
    <button onclick="copyAllCodes()">📋 COPY ALL</button>
  </div>
  <div class="panel" id="codes-container" style="padding:16px">
    <div style="text-align:center;color:var(--muted);padding:20px">Đang quét kênh...</div>
  </div>

  <div class="section-title">API Console</div>
  <div class="code-actions">
    <button onclick="checkApi()">🔍 CHECK API</button>
    <button onclick="toggleLog()">📜 VIEW LOG</button>
    <button onclick="clearLog()">🗑️ CLEAR LOG</button>
  </div>
  <div class="panel" id="api-log-panel" style="padding:12px;display:none">
    <div class="api-log" id="api-log-content">
      <div style="color:var(--muted);text-align:center;padding:12px">Nhấn VIEW LOG để tải log</div>
    </div>
  </div>
</div>

<script>
const PERSIST_KEY = 'lc79_persist_token';

let state = {
  session_id: '', persistToken: '',
  gameUser: '', gameBalance: 0, gameXu: 0,
  loginTime: 0, codes: [], logVisible: false,
  atReady: false, jwtReady: false, jwtExp: 0,
  autoEnabled: false, booting: true,
  pollInterval: 0.1,
};

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  window.scrollTo({top:0,behavior:'smooth'});
}

function updateSteps(containerId, current) {
  const el = document.getElementById(containerId);
  if (!el) return;
  let html = '';
  for (let i = 1; i <= 4; i++) {
    let cls = i < current ? 'done' : i === current ? 'active' : '';
    html += `<div class="step-dot ${cls}">${i}</div>`;
  }
  el.innerHTML = html;
}

function showAlert(id, message, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = 'alert alert-' + (type || 'err') + ' show';
  el.textContent = message;
}
function hideAlert(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('show');
}

function startFlow() { state.session_id = ''; showView('view-api'); updateSteps('steps1', 1); }
function restart() { state.session_id = ''; showView('view-home'); }

function savePersist(token) {
  if (token) {
    localStorage.setItem(PERSIST_KEY, token);
    state.persistToken = token;
    document.getElementById('session-banner').classList.add('show');
  }
}

async function sendOTP() {
  const api_id = document.getElementById('apiId').value.trim();
  const api_hash = document.getElementById('apiHash').value.trim();
  const phone = document.getElementById('phone').value.trim();
  hideAlert('alert-api');
  if (!api_id || !api_hash || !phone) { showAlert('alert-api', 'Vui lòng nhập đầy đủ thông tin!'); return; }
  const btn = document.getElementById('btn-send-otp');
  btn.innerText = 'Đang gửi OTP...'; btn.disabled = true;
  try {
    const res = await fetch('/api/send_code', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({api_id, api_hash, phone})});
    const data = await res.json();
    if (data.status === 'ok') {
      state.session_id = data.session_id;
      showView('view-otp'); updateSteps('steps2', 2);
      document.getElementById('otpCode').value = '';
      document.getElementById('otpCode').focus();
    } else { showAlert('alert-api', data.message); }
  } catch (e) { showAlert('alert-api', 'Lỗi kết nối server!'); }
  finally { btn.innerText = 'Gửi OTP →'; btn.disabled = false; }
}

async function verifyOTP() {
  const code = document.getElementById('otpCode').value.trim();
  hideAlert('alert-otp');
  if (!code) { showAlert('alert-otp', 'Vui lòng nhập mã OTP!'); return; }
  const btn = document.getElementById('btn-verify-otp');
  btn.innerText = 'Đang xác thực...'; btn.disabled = true;
  try {
    const res = await fetch('/api/verify_code', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id, code})});
    const data = await res.json();
    if (data.status === 'ok') {
      if (data.persist_token) savePersist(data.persist_token);
      showView('view-game'); updateSteps('steps4', 4);
    } else if (data.status === '2fa') {
      showView('view-2fa'); updateSteps('steps3', 3);
      document.getElementById('password2fa').value = '';
      document.getElementById('password2fa').focus();
    } else { showAlert('alert-otp', data.message); }
  } catch (e) { showAlert('alert-otp', 'Lỗi kết nối server!'); }
  finally { btn.innerText = 'Xác Nhận OTP'; btn.disabled = false; }
}

async function verify2FA() {
  const password = document.getElementById('password2fa').value.trim();
  hideAlert('alert-2fa');
  if (!password) { showAlert('alert-2fa', 'Vui lòng nhập mật khẩu 2FA!'); return; }
  const btn = document.getElementById('btn-verify-2fa');
  btn.innerText = 'Đang xác thực...'; btn.disabled = true;
  try {
    const res = await fetch('/api/verify_2fa', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id, password})});
    const data = await res.json();
    if (data.status === 'ok') {
      if (data.persist_token) savePersist(data.persist_token);
      showView('view-game'); updateSteps('steps4', 4);
    } else { showAlert('alert-2fa', data.message); }
  } catch (e) { showAlert('alert-2fa', 'Lỗi kết nối server!'); }
  finally { btn.innerText = 'Xác Nhận'; btn.disabled = false; }
}

async function gameLogin() {
  const username = document.getElementById('gameUser').value.trim();
  const password = document.getElementById('gamePass').value;
  hideAlert('alert-game');
  if (!username || !password) { showAlert('alert-game', 'Vui lòng nhập đủ Username và Mật khẩu game!'); return; }
  const btn = document.getElementById('btn-game-login');
  btn.innerText = 'Đang đăng nhập...'; btn.disabled = true;
  try {
    const res = await fetch('/api/login_game', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username, password, session_id: state.session_id, persist_token: state.persistToken})});
    const data = await res.json();
    if (data.status === 'ok') {
      state.gameUser = data.username; state.gameBalance = data.balance; state.gameXu = data.xuTotal;
      state.loginTime = data.login_time || (Date.now() / 1000);
      state.atReady = !!data.access_token_ready;
      showView('view-result');
      renderAccountInfo();
      updateTokenStatusFromState();
      await fetchCodes();
      await refreshStats();
      await refreshAutoEvents();
    } else { showAlert('alert-game', data.message); }
  } catch (e) { showAlert('alert-game', 'Lỗi kết nối server!'); }
  finally { btn.innerText = 'Đăng Nhập →'; btn.disabled = false; }
}

function renderAccountInfo() {
  document.getElementById('info-username').textContent = state.gameUser || '-';
  document.getElementById('info-balance').textContent = Number(state.gameBalance||0).toLocaleString('vi-VN');
  document.getElementById('info-xu').textContent = Number(state.gameXu||0).toLocaleString('vi-VN');
  if (state.loginTime) {
    document.getElementById('info-login-time').textContent = new Date(state.loginTime * 1000).toLocaleString('vi-VN');
  }
}

function updateTokenStatusFromState() {
  const atCard = document.getElementById('token-card-at');
  const atStatus = document.getElementById('at-status');
  if (state.atReady) {
    atCard.classList.add('ok'); atStatus.className = 'token-status ok';
    atStatus.textContent = '✅ Đã có token';
  } else {
    atCard.classList.remove('ok'); atStatus.className = 'token-status pending';
    atStatus.textContent = '⚠ Chưa lấy — bấm GET TOKEN';
  }
  const jwtCard = document.getElementById('token-card-jwt');
  const jwtStatus = document.getElementById('jwt-status');
  if (state.jwtReady && state.jwtExp > 0) {
    const remain = Math.max(0, Math.floor((state.jwtExp - Date.now()/1000) / 60));
    if (remain > 0) {
      jwtCard.classList.add('ok'); jwtCard.classList.remove('err');
      jwtStatus.className = 'token-status ok';
      jwtStatus.textContent = `✅ Hợp lệ (còn ~${remain} phút)`;
      return;
    }
  }
  jwtCard.classList.remove('ok'); jwtStatus.className = 'token-status pending';
  jwtStatus.textContent = state.jwtReady ? '⚠ Hết hạn — bấm REFRESH' : '⚠ Chưa có — bấm GET JWT';
}

async function fetchAccessToken() {
  const btn = document.getElementById('btn-get-at');
  btn.textContent = 'Đang lấy...'; btn.disabled = true;
  try {
    const res = await fetch('/api/get_access_token', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id})});
    const data = await res.json();
    if (data.status === 'ok') { state.atReady = true; toast('Đã lấy Access Token ✓'); }
    else { toast('Lỗi: ' + data.message); }
    updateTokenStatusFromState();
    await refreshStats();
  } catch (e) { toast('Lỗi kết nối server!'); }
  finally { btn.textContent = 'GET TOKEN'; btn.disabled = false; }
}

async function fetchJWT(force) {
  const btn = document.getElementById(force ? 'btn-refresh-jwt' : 'btn-get-jwt');
  btn.textContent = 'Đang xử lý...'; btn.disabled = true;
  try {
    const endpoint = force ? '/api/refresh_jwt' : '/api/get_jwt';
    const res = await fetch(endpoint, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id, force})});
    const data = await res.json();
    if (data.status === 'ok') {
      state.jwtReady = true; state.jwtExp = data.exp || 0;
      state.atReady = true;
      toast('Đã có JWT' + (data.has_exp ? ` (còn ~${data.expires_in_minutes} phút)` : ''));
    } else { toast('Lỗi: ' + data.message); }
    updateTokenStatusFromState();
    await refreshStats();
  } catch (e) { toast('Lỗi kết nối server!'); }
  finally { btn.textContent = force ? 'REFRESH' : 'GET JWT'; btn.disabled = false; }
}

async function refreshStats() {
  try {
    const res = await fetch('/api/stats');
    const data = await res.json();
    if (data.status === 'ok') {
      const s = data.stats;
      document.getElementById('stat-total').textContent = s.total;
      document.getElementById('stat-success').textContent = s.success;
      document.getElementById('stat-failed').textContent = s.failed;
      document.getElementById('stat-avg').textContent = s.avg_ms + ' ms';
    }
  } catch (e) {}
}

async function redeemManual() {
  const code = document.getElementById('manualCode').value.trim();
  if (!code) { toast('Nhập code trước'); return; }
  const btn = document.getElementById('btn-manual-redeem');
  btn.disabled = true; btn.textContent = '⚡ Đang bắn...';
  const t0 = performance.now();
  try {
    const res = await fetch('/api/redeem_manual', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id, code})});
    const data = await res.json();
    const ms = Math.round(performance.now() - t0);
    if (data.http === 200 || data.http === 201) {
      const amt = data.amount || (data.body && (data.body.money || data.body.amount));
      toast(amt ? `✅ +${Number(amt).toLocaleString('vi-VN')} (${ms}ms)` : `✅ Nhập thành công (${ms}ms)`);
      document.getElementById('manualCode').value = '';
    } else {
      const msg = (data.body && data.body.message) || data.message || `HTTP ${data.http}`;
      toast(`❌ ${msg} (${ms}ms)`);
    }
    await refreshAutoEvents();
    await refreshStats();
  } catch (e) { toast('Lỗi kết nối server'); }
  finally { btn.disabled = false; btn.textContent = '⚡ NHẬP NGAY'; }
}

async function forceScan() {
  const btn = document.getElementById('btn-force-scan');
  btn.disabled = true; btn.textContent = '🔍 Đang quét...';
  try {
    const res = await fetch('/api/force_scan', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id})});
    const data = await res.json();
    if (data.status === 'ok') {
      toast(data.fired > 0 ? `🔔 Đã fire ${data.fired} code mới` : '✅ Không có code mới để fire');
    } else {
      toast('Lỗi: ' + (data.message || 'unknown'));
    }
    await refreshAutoEvents();
  } catch (e) { toast('Lỗi kết nối server'); }
  finally { btn.disabled = false; btn.textContent = '🔔 NHẮC TOOL CHECK CODE'; }
}

async function toggleAuto() {
  const btn = document.getElementById('btn-auto-toggle');
  const isOn = btn.dataset.state === 'on';
  btn.disabled = true; btn.textContent = '...';
  try {
    const res = await fetch('/api/toggle_auto', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id, enabled: !isOn})});
    const data = await res.json();
    if (data.status === 'ok') {
      setAutoState(!isOn);
      toast(!isOn ? '🎯 Đã bật Auto — Event + Poll đang chạy' : '🛑 Đã tắt Auto Watch');
    } else { toast('Lỗi: ' + (data.message || 'unknown')); }
  } catch (e) { toast('Lỗi kết nối server'); }
  finally { btn.disabled = false; }
}

function setAutoState(on) {
  const btn = document.getElementById('btn-auto-toggle');
  state.autoEnabled = on;
  btn.dataset.state = on ? 'on' : 'off';
  btn.textContent = on ? 'TẮT' : 'BẬT';
  document.getElementById('auto-status').textContent = on
    ? `🟢 Đang theo dõi realtime (event + poll)`
    : '⚪ Chưa bật';
}

async function refreshAutoEvents() {
  if (!state.session_id) return;
  try {
    const res = await fetch('/api/auto_status', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id})});
    const data = await res.json();
    if (data.status !== 'ok') return;
    if (data.poll_interval !== undefined) state.pollInterval = data.poll_interval;
    if (data.enabled !== state.autoEnabled) setAutoState(!!data.enabled);
    if (data.jwt_remaining_minutes > 0) {
      state.jwtReady = true;
      state.jwtExp = Date.now()/1000 + data.jwt_remaining_minutes * 60;
      updateTokenStatusFromState();
    }
    const container = document.getElementById('auto-events');
    if (!data.events || data.events.length === 0) {
      container.innerHTML = '<div style="color:var(--muted);text-align:center;padding:12px">Chưa có hoạt động</div>';
      return;
    }
    container.innerHTML = '';
    const events = data.events.slice().reverse();
    events.forEach(e => {
      const time = new Date(e.ts * 1000).toLocaleTimeString('vi-VN');
      const isSuccess = e.status === 'success';
      const isSkipped = e.status === 'skipped';
      const cls = isSuccess ? 'success' : isSkipped ? 'skipped' : 'fail';
      const icon = isSuccess ? '✅' : isSkipped ? '⏸' : '❌';
      const amtStr = e.amount ? ` <strong style="color:var(--success)">+${Number(e.amount).toLocaleString('vi-VN')}</strong>` : '';
      const rawStr = e.raw_code && e.raw_code !== e.code
        ? ` <span style="color:var(--muted);font-size:10px">(raw: ${e.raw_code})</span>` : '';
      const timing = [];
      if (e.delay_ms !== undefined) timing.push(`fire +${e.delay_ms}ms`);
      if (e.elapsed_ms !== undefined) timing.push(`HTTP ${e.elapsed_ms}ms`);
      if (e.total_ms !== undefined) timing.push(`total ${e.total_ms}ms`);
      const div = document.createElement('div');
      div.className = 'event ' + cls;
      div.innerHTML = `<div><span class="ev-code">${e.code}</span>${amtStr}${rawStr}</div><div class="ev-meta">${icon} ${time} · HTTP ${e.http || '-'} ${e.manual ? '· MANUAL' : '· AUTO'}${timing.length ? ' · ' + timing.join(' · ') : ''}${e.message ? '<br>' + e.message : ''}</div>`;
      container.appendChild(div);
    });
  } catch (e) {}
}

let lastCodesHash = '';

async function fetchCodes(silent = false) {
  const container = document.getElementById('codes-container');
  if (!silent) {
    container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Đang quét kênh LC79...</div>';
  }
  try {
    const res = await fetch('/api/get_codes', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: state.session_id})});
    const data = await res.json();
    if (data.status === 'ok' && data.items && data.items.length > 0) {
      state.codes = data.items.map(it => it.code);
      
      const newHash = JSON.stringify(data.items.map(it => it.code));
      if (silent && newHash === lastCodesHash) return; // Không vẽ lại nếu không có code mới
      lastCodesHash = newHash;

      container.innerHTML = '';
      data.items.forEach((item, i) => {
        const div = document.createElement('div');
        div.className = 'code-box';
        const badge = item.status === 'new' ? '<span class="code-badge new">NEW</span>' : '<span class="code-badge seen">SEEN</span>';
        let timeStr = '';
        if (item.msg_date) timeStr = new Date(item.msg_date * 1000).toLocaleString('vi-VN', {hour:'2-digit', minute:'2-digit', day:'2-digit', month:'2-digit'});
        div.innerHTML = `<div class="code-content"><div class="code-main">${i+1}. ${item.code}${badge}</div><div class="code-meta">Phát hiện: ${timeStr}</div></div><button class="copy-btn" onclick="copyCode('${item.code}')">COPY</button>`;
        container.appendChild(div);
      });
    } else if (data.status === 'ok') {
      container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Không tìm thấy code.</div>';
      state.codes = [];
    } else {
      container.innerHTML = `<div style="color:var(--danger);text-align:center;padding:20px">${data.message || 'Lỗi'}</div>`;
      state.codes = [];
    }
  } catch (e) { if(!silent) container.innerHTML = '<div style="color:var(--danger);text-align:center;padding:20px">Lỗi kết nối!</div>'; }
  if (!silent) await refreshStats();
}

function copyCode(text) { navigator.clipboard.writeText(text).then(() => toast('Đã copy: ' + text)); }

function copyAllCodes() {
  if (!state.codes || state.codes.length === 0) { toast('Chưa có code để copy'); return; }
  navigator.clipboard.writeText(state.codes.join('\n')).then(() => toast(`Đã copy ${state.codes.length} code`));
}

function toast(msg) {
  const t = document.createElement('div');
  t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,#10b981,#059669);color:white;padding:12px 24px;border-radius:12px;font-size:13px;font-weight:700;z-index:9999;transition:0.3s;box-shadow:0 10px 20px rgba(16,185,129,0.3);max-width:90vw;text-align:center';
  t.innerText = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity='0'; t.style.transform='translateX(-50%) translateY(20px)'; setTimeout(()=>t.remove(),300); }, 2200);
}

async function checkApi() {
  toast('Đang kiểm tra API...');
  try {
    const res = await fetch('/api/check_api', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
    const data = await res.json();
    const r = data.results || {};
    let msg = '';
    for (const k of Object.keys(r)) {
      const item = r[k];
      msg += `${item.name}: ${item.ok ? '✅' : '❌'} ${item.http || item.error || ''}  `;
    }
    toast(msg.trim());
  } catch (e) { toast('Lỗi kết nối server'); }
}

async function toggleLog() {
  state.logVisible = !state.logVisible;
  const panel = document.getElementById('api-log-panel');
  if (state.logVisible) { panel.style.display = 'block'; await loadLogs(); }
  else { panel.style.display = 'none'; }
}

function renderLogEntry(e) {
  let extra = '';
  if (e.method) extra += ` ${e.method} ${e.url || ''}`;
  if (e.status) extra += ` [HTTP ${e.status}]`;
  if (e.elapsed_ms !== undefined) extra += ` ${e.elapsed_ms}ms`;
  if (e.account) extra += ` @${e.account}`;
  if (e.body) extra += ' ' + JSON.stringify(e.body).slice(0, 200);
  if (e.error) extra += ' ' + e.error;
  const safe = (s) => String(s).replace(/</g,'&lt;');
  return `<div class="log-entry log-${e.level}"><span class="log-time">[${e.time}]</span><span class="log-tag">[${e.level.toUpperCase()}]</span><span>${safe(e.message)}${safe(extra)}</span></div>`;
}

async function loadLogs() {
  try {
    const res = await fetch('/api/api_log?limit=200');
    const data = await res.json();
    const container = document.getElementById('api-log-content');
    if (data.status === 'ok' && data.logs.length > 0) {
      container.innerHTML = data.logs.map(renderLogEntry).join('');
      container.scrollTop = container.scrollHeight;
    } else {
      container.innerHTML = '<div style="color:var(--muted);text-align:center;padding:12px">Chưa có log</div>';
    }
  } catch (e) { toast('Lỗi tải log'); }
}

async function clearLog() {
  try {
    await fetch('/api/clear_log', {method:'POST'});
    if (state.logVisible) await loadLogs();
    await refreshStats();
    toast('Đã xoá log');
  } catch (e) { toast('Lỗi xoá log'); }
}

async function forgetDevice() {
  if (!confirm('Đăng xuất thiết bị này?\nLần sau sẽ cần đăng nhập Telegram + Game lại.')) return;
  try {
    await fetch('/api/forget', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({persist_token: state.persistToken, session_id: state.session_id})});
  } catch (e) {}
  localStorage.removeItem(PERSIST_KEY);
  state.persistToken = '';
  document.getElementById('session-banner').classList.remove('show');
  restart();
  toast('Đã đăng xuất');
}

async function tryResume() {
  const token = localStorage.getItem(PERSIST_KEY);
  if (!token) { state.booting = false; return false; }
  try {
    const res = await fetch('/api/resume', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({persist_token: token})});
    const data = await res.json();
    if (data.status === 'ok') {
      state.session_id = data.session_id;
      state.persistToken = token;
      state.gameUser = data.game_username || data.nickname || '';
      state.gameBalance = data.vin || 0;
      state.gameXu = data.xu || 0;
      state.loginTime = data.login_time || 0;
      state.atReady = !!data.access_token_ready;
      state.jwtReady = !!data.jwt_ready;
      state.jwtExp = data.jwt_exp || 0;
      if (data.fast_poll_interval !== undefined) state.pollInterval = data.fast_poll_interval;
      showView('view-result');
      document.getElementById('session-banner').classList.add('show');
      renderAccountInfo();
      updateTokenStatusFromState();
      setAutoState(!!data.auto_enabled);
      state.booting = false;
      await fetchCodes();
      await refreshStats();
      await refreshAutoEvents();
      toast('💾 Đã khôi phục phiên — auto vẫn đang chạy');
      return true;
    } else {
      localStorage.removeItem(PERSIST_KEY);
      state.booting = false;
      return false;
    }
  } catch (e) {
    state.booting = false;
    return false;
  }
}

document.getElementById('otpCode')?.addEventListener('keypress', e => { if (e.key === 'Enter') verifyOTP(); });
document.getElementById('password2fa')?.addEventListener('keypress', e => { if (e.key === 'Enter') verify2FA(); });
document.getElementById('manualCode')?.addEventListener('keypress', e => { if (e.key === 'Enter') redeemManual(); });

// Vòng lặp cập nhật Real-time (2s): Log auto + Danh sách Giftcode không cần F5
setInterval(() => {
  if (document.getElementById('view-result').classList.contains('active') && state.session_id) {
    refreshAutoEvents();
    if (state.autoEnabled) {
      fetchCodes(true); // Silent fetch để lấy danh sách code mới
    }
  }
}, 2000);

// Auto-resume on page load
(async () => {
  await tryResume();
})();
</script>
</body>
</html>
"""


# ================================================================
#                    ALIVE PAGE
# ================================================================
ALIVE_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Server Alive</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#05050a;color:#f8fafc;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background-image:radial-gradient(circle at 0% 0%,rgba(139,92,246,0.15) 0%,transparent 50%),radial-gradient(circle at 100% 100%,rgba(16,185,129,0.15) 0%,transparent 50%);background-attachment:fixed}
.card{background:rgba(20,24,39,0.6);backdrop-filter:blur(12px);border:1px solid rgba(16,185,129,0.3);border-radius:24px;padding:48px 40px;text-align:center;max-width:440px;width:100%;box-shadow:0 20px 60px rgba(16,185,129,0.15)}
.icon-wrap{width:88px;height:88px;margin:0 auto 24px;border-radius:24px;background:linear-gradient(135deg,rgba(16,185,129,0.2),rgba(6,182,212,0.2));border:1px solid rgba(16,185,129,0.4);display:flex;align-items:center;justify-content:center;font-size:44px;animation:breathe 2.5s ease-in-out infinite}
@keyframes breathe{0%,100%{transform:scale(1);box-shadow:0 0 30px rgba(16,185,129,0.3)}50%{transform:scale(1.05);box-shadow:0 0 50px rgba(16,185,129,0.5)}}
h1{font-size:28px;font-weight:800;margin-bottom:8px;background:linear-gradient(to right,#34d399,#22d3ee);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.status{display:inline-flex;align-items:center;gap:8px;color:#34d399;font-size:13px;font-weight:600;margin-bottom:24px}
.dot{width:10px;height:10px;border-radius:50%;background:#10b981;box-shadow:0 0 12px #10b981;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.4);opacity:.6}}
.stat{background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.08);border-radius:12px;padding:14px 18px;margin-top:12px;display:flex;justify-content:space-between;align-items:center}
.stat-label{color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px}
.stat-value{color:#34d399;font-size:15px;font-weight:800;font-family:'Courier New',monospace}
.link-back{display:inline-block;margin-top:24px;color:#22d3ee;text-decoration:none;font-size:13px;font-weight:600}
</style>
</head>
<body>
<div class="card">
  <div class="icon-wrap">🚀</div>
  <h1>SERVER ALIVE</h1>
  <div class="status"><span class="dot"></span>Real-time 24/7 · Auto-restore</div>
  <div class="stat"><span class="stat-label">Uptime</span><span class="stat-value" id="uptime">--:--:--</span></div>
  <div class="stat"><span class="stat-label">Active Sessions</span><span class="stat-value" id="active">0</span></div>
  <div class="stat"><span class="stat-label">API Requests</span><span class="stat-value" id="reqs">0</span></div>
  <div class="stat"><span class="stat-label">Ping</span><span class="stat-value" id="ping">--</span></div>
  <a class="link-back" href="/">← Về trang chủ</a>
</div>
<script>
function pad(n){return n.toString().padStart(2,'0')}
function fmt(s){var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),sec=s%60;return pad(h)+':'+pad(m)+':'+pad(sec)}
async function tick(){
  try{
    var t0=performance.now();
    var res=await fetch('/api/ping',{cache:'no-store'});
    var t1=performance.now();
    var data=await res.json();
    document.getElementById('uptime').textContent=fmt(data.uptime);
    document.getElementById('ping').textContent=Math.round(t1-t0)+' ms';
    document.getElementById('active').textContent=data.active_sessions;
    var sres = await fetch('/api/stats');
    var sdata = await sres.json();
    if (sdata.status==='ok') document.getElementById('reqs').textContent = sdata.stats.total;
  }catch(e){document.getElementById('ping').textContent='ERR'}
}
setInterval(tick,10000);tick();
</script>
</body>
</html>
"""


# ============ ROUTES ============
@flask_app.route("/")
def index():
    return Response(MAIN_HTML, mimetype="text/html")


@flask_app.route("/alive")
def alive_page():
    return Response(ALIVE_HTML, mimetype="text/html")


@flask_app.route("/health")
def health():
    return Response("ok", mimetype="text/plain")


@flask_app.route("/api/ping")
def ping_json():
    with SESSIONS_LOCK:
        active = len(sessions)
    return jsonify({
        "status": "alive",
        "uptime": int(time.time() - SERVER_START_TIME),
        "timestamp": int(time.time()),
        "active_sessions": active,
        "poll_interval": FAST_POLL_INTERVAL,
    })


# ============ SELF-PING ============
def keep_alive():
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL chưa set — self-ping tắt.")
        return
    url = WEBAPP_URL.rstrip("/") + "/health"
    time.sleep(15)
    while True:
        try:
            requests.get(url, timeout=10)
        except Exception:
            pass
        time.sleep(PING_INTERVAL)


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)


# ============ MAIN ============
if __name__ == "__main__":
    _load_persist()
    threading.Thread(target=_warm_redeem_connection, daemon=True, name="WarmRedeem").start()
    async_manager.start()
    threading.Thread(target=cleanup_old_sessions, daemon=True, name="SessionCleanup").start()
    threading.Thread(target=keep_alive, daemon=True, name="KeepAlive").start()
    threading.Thread(target=jwt_prewarm_loop, daemon=True, name="JwtPrewarm").start()
    threading.Thread(target=session_keepalive_loop, daemon=True, name="SessionKeepalive").start()
    threading.Thread(target=restore_persistent_sessions_loop, daemon=True, name="RestorePersist").start()
    threading.Thread(target=_persist_saver_loop, daemon=True, name="PersistSaver").start()
    logger.info(f"🚀 Web server khởi động tại port {PORT}")
    logger.info(f"⚡ Fast-poll interval: {FAST_POLL_INTERVAL}s | batch: {FAST_POLL_BATCH}")
    logger.info(f"💾 Persist file: {PERSIST_FILE}")
    run_flask()