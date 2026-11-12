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

from telethon import TelegramClient, functions
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
PORT          = int(os.getenv("PORT", 10000))
PING_INTERVAL = int(os.getenv("PING_INTERVAL", 10))
WEBAPP_URL    = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
GAME_API_URL  = os.getenv("GAME_API_URL", "https://apifo88daigia.tele68.com/api")
LOGIN_URL     = os.getenv("LOGIN_URL", "https://wlb.tele68.com/v1/lobby/auth/login")

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

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._started = False


async_manager = AsyncLoopManager()


# ================================================================
#                     API DEBUG LOGGER
# ================================================================
class ApiLogger:
    """Log request/response có timestamp, redact dữ liệu nhạy cảm, thread-safe."""

    SENSITIVE_KEYS = {
        "pw", "password", "pass", "pwd",
        "api_hash", "apihash",
        "code", "otp",
        "accesstoken", "accesstoken_", "access_token",
        "token", "jwt",
        "authorization",
        "sessionkey", "session_key", "session",
        "phone_code_hash", "phonecodehash",
    }

    def __init__(self, max_entries=400):
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
                if k.lower() in ("authorization",):
                    out[k] = "Bearer ***"
                elif k.lower() in ("pw", "password", "pass", "pwd", "code", "otp"):
                    out[k] = "***"
                else:
                    out[k] = self._mask(v) if isinstance(v, str) else "***"
            elif isinstance(v, dict):
                out[k] = self._redact_dict(v)
            elif isinstance(v, list):
                out[k] = [
                    self._redact_dict(i) if isinstance(i, dict) else i
                    for i in v
                ]
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
            red = {}
            for k, v in headers.items():
                red[k] = "Bearer ***" if k.lower() == "authorization" else v
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
            "request_id": request_id,
            "status": status,
            "elapsed_ms": int(elapsed * 1000),
        }
        if account:
            payload["account"] = account
        if headers:
            payload["content_type"] = headers.get("Content-Type", "")
        if body is not None:
            payload["body"] = self._redact_dict(body) if isinstance(body, dict) else body
        if error:
            payload["error"] = error

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
                    method=method, url=url,
                    params=params, json=json_data,
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
                    "ok": 200 <= resp.status_code < 300,
                    "status": resp.status_code,
                    "body": body,
                    "text": resp.text,
                    "headers": dict(resp.headers),
                    "elapsed": elapsed,
                    "request_id": rid,
                }

            except requests.exceptions.ConnectTimeout as e:
                last_error = f"ConnectTimeout"
                self.logger.log("warning", f"Connect timeout (attempt {attempt+1}/{max_attempts})",
                                request_id=rid, account=account)
            except requests.exceptions.ReadTimeout as e:
                last_error = f"ReadTimeout"
                self.logger.log("warning", f"Read timeout (attempt {attempt+1}/{max_attempts})",
                                request_id=rid, account=account)
            except requests.exceptions.ConnectionError as e:
                last_error = f"ConnectionError: {str(e)[:120]}"
                self.logger.log("warning", f"Connection error (attempt {attempt+1}/{max_attempts})",
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
    """Đọc trường exp trong JWT. Trả 0 nếu không đọc được."""
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
        self.pw_hash = pw_hash          # MD5, không phải plaintext
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
        """JWT còn hạn không. JWT không có exp → coi là KHÔNG hợp lệ."""
        if not self.jwt or not self.jwt_exp:
            return False
        return time.time() < (self.jwt_exp - 60)

    def jwt_remaining_minutes(self):
        if not self.jwt_exp:
            return 0
        return max(0, int((self.jwt_exp - time.time()) / 60))


class GameAccountManager:
    """Quản lý account toàn cục. Reuse token giữa các session."""

    def __init__(self, api_client, logger_):
        self.accounts = {}
        self.lock = threading.Lock()
        self.api_client = api_client
        self.logger = logger_

    def get_or_create(self, username, pw_hash, nickname=None, vin=0, xu=0):
        with self.lock:
            if username in self.accounts:
                acc = self.accounts[username]
                # Password đổi → reset tokens
                if acc.pw_hash != pw_hash:
                    acc.pw_hash = pw_hash
                    acc.access_token = None
                    acc.access_token_time = 0.0
                    acc.jwt = None
                    acc.jwt_exp = 0
                if nickname:
                    acc.nickname = nickname
                if vin:
                    acc.vin = vin
                if xu:
                    acc.xu = xu
                acc.login_time = time.time()
                return acc
            acc = GameAccount(username, pw_hash, nickname or username, vin, xu)
            self.accounts[username] = acc
            return acc

    def get(self, username):
        with self.lock:
            return self.accounts.get(username)

    # ---------- internal (no lock) ----------
    def _fetch_access_token(self, account):
        self.logger.log("info", "Bắt đầu lấy Access Token", account=account.username)

        resp = self.api_client.get(
            GAME_API_URL,
            params={
                "c": "3", "un": account.username, "pw": account.pw_hash,
                "cp": "R", "cl": "R", "pf": "web", "at": "",
            },
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
            return {
                "status": "error",
                "message": body.get("message") or "API không trả success",
            }

        token = body.get("accessToken") or body.get("access_token")
        if not token:
            self.logger.log("warning",
                            "Không tìm thấy accessToken trong response",
                            account=account.username,
                            structure=self._structure_only(body))
            return {"status": "error", "message": "Response thiếu accessToken"}

        account.access_token = token
        account.access_token_time = time.time()
        self.logger.log("success", "Đã lấy Access Token",
                        account=account.username)
        return {
            "status": "ok",
            "token_preview": token[:6] + "..." + token[-4:] if len(token) > 12 else "***",
        }

    def _fetch_jwt(self, account):
        self.logger.log("info", "Bắt đầu login JWT", account=account.username)

        headers = {
            "content-type": "application/json",
            "origin": "https://lc79b.bet",
            "referer": "https://lc79b.bet/",
            "user-agent": "Mozilla/5.0",
        }
        payload = {
            "nickName": account.nickname,
            "accessToken": account.access_token,
        }

        resp = self.api_client.post(
            LOGIN_URL, headers=headers, json_data=payload,
            account=account.username,
        )

        if not resp["ok"]:
            msg = resp.get("error") or f"HTTP {resp['status']}"
            self.logger.log("error", f"JWT login thất bại: {msg}",
                            account=account.username)
            return {"status": "error", "message": msg}

        body = resp.get("body") or {}

        # Thử nhiều vị trí có thể chứa token
        token = (
            body.get("token")
            or body.get("jwt")
            or (body.get("data") or {}).get("token")
            or (body.get("data") or {}).get("jwt")
            or (body.get("data") or {}).get("accessToken")
        )

        if not token:
            self.logger.log("warning",
                            "Không xác định được trường JWT",
                            account=account.username,
                            structure=self._structure_only(body))
            return {
                "status": "error",
                "message": "Không xác định được trường JWT. Xem API Log để kiểm tra.",
            }

        account.jwt = token
        account.jwt_exp = parse_jwt_exp(token)
        remain = account.jwt_remaining_minutes()

        self.logger.log("success", f"Đã có JWT (còn ~{remain} phút)",
                        account=account.username)
        return {
            "status": "ok",
            "jwt_preview": token[:6] + "..." + token[-4:] if len(token) > 12 else "***",
            "exp": account.jwt_exp,
            "expires_in_minutes": remain,
            "has_exp": bool(account.jwt_exp),
        }

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

    # ---------- public ----------
    def get_access_token(self, account):
        if not account._refresh_lock.acquire(timeout=15):
            return {"status": "error", "message": "Đang xử lý, vui lòng chờ"}
        try:
            return self._fetch_access_token(account)
        finally:
            account._refresh_lock.release()

    def get_jwt(self, account, force=False):
        if not force and account.is_jwt_valid():
            return {
                "status": "ok", "cached": True,
                "exp": account.jwt_exp,
                "expires_in_minutes": account.jwt_remaining_minutes(),
                "has_exp": bool(account.jwt_exp),
            }
        if not account._refresh_lock.acquire(timeout=20):
            return {"status": "error", "message": "Đang xử lý, vui lòng chờ"}
        try:
            if not force and account.is_jwt_valid():
                return {
                    "status": "ok", "cached": True,
                    "exp": account.jwt_exp,
                    "expires_in_minutes": account.jwt_remaining_minutes(),
                    "has_exp": bool(account.jwt_exp),
                }
            if not account.access_token:
                res = self._fetch_access_token(account)
                if res["status"] != "ok":
                    return res
            return self._fetch_jwt(account)
        finally:
            account._refresh_lock.release()

    def refresh_jwt(self, account):
        """Force re-fetch cả access token + JWT."""
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


# ============ TELEGRAM SESSION STORAGE ============
sessions = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 600


def cleanup_old_sessions():
    while True:
        try:
            time.sleep(60)
            now = time.time()
            expired_ids = []
            with SESSIONS_LOCK:
                for sid, s in list(sessions.items()):
                    if now - s["created"] > SESSION_TTL:
                        expired_ids.append(sid)

            for sid in expired_ids:
                with SESSIONS_LOCK:
                    s = sessions.pop(sid, None)
                if s and s.get("client"):
                    try:
                        async_manager.run(s["client"].disconnect(), timeout=5)
                    except Exception:
                        pass
                    logger.info(f"🧹 Cleaned session {sid[:8]}")
        except Exception as e:
            logger.warning(f"cleanup err: {e}")


# ============ GAME LOGIN (SYNC) ============
def login_game_sync(username, password):
    """Giữ logic cũ, thêm capture accessToken + lưu account."""
    try:
        pw_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {
            "c": 3, "un": username, "pw": pw_hash,
            "cp": "R", "cl": "R", "pf": "web", "at": "",
        }
        api_logger.log_request(
            request_id="login-" + str(uuid.uuid4())[:6],
            method="GET", url=GAME_API_URL,
            params=params, account=username,
        )

        start = time.time()
        resp = requests.get(GAME_API_URL, params=params, timeout=15)
        elapsed = time.time() - start

        if resp.status_code >= 500:
            api_logger.log_response(
                request_id="login", status=resp.status_code,
                elapsed=elapsed, account=username,
            )
            return {"status": "error",
                    "message": f"Server game lỗi (HTTP {resp.status_code})"}

        try:
            result = resp.json()
        except Exception:
            api_logger.log_response(
                request_id="login", status=resp.status_code,
                elapsed=elapsed, account=username,
                error="JSON parse error",
            )
            return {"status": "error", "message": "API game trả dữ liệu không hợp lệ"}

        api_logger.log_response(
            request_id="login", status=resp.status_code,
            body=result, elapsed=elapsed, account=username,
        )

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

        account = account_manager.get_or_create(
            username, pw_hash, nickname, vin, xu
        )

        # Lưu accessToken luôn nếu response có
        access_token = result.get("accessToken") or result.get("access_token")
        if access_token:
            account.access_token = access_token
            account.access_token_time = time.time()
            api_logger.log("success", "Đã capture Access Token từ login",
                           account=username)

        return {
            "status": "ok",
            "username": nickname,
            "balance": vin,
            "xuTotal": xu,
            "access_token_ready": bool(account.access_token),
            "login_time": account.login_time,
        }
    except Exception as e:
        api_logger.log("error", f"login_game exception: {type(e).__name__}: {e}")
        return {"status": "error", "message": f"Lỗi kết nối: {e}"}


# ============ TELEGRAM CODE ENGINE ============
async def _async_fetch_codes(client, session_data):
    """Quét kênh LC79, trả về items có metadata + log quá trình."""
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
            _log("info", "Resolve kênh qua username thành công")
        except Exception as e:
            _log("warning", f"Resolve username thất bại: {str(e)[:80]}")

    if not channel_entity:
        try:
            channel_entity = await client.get_entity(-1002237214360)
            _log("info", "Resolve kênh qua ID thành công")
        except Exception:
            pass

    if not channel_entity:
        _log("error", "Không tìm thấy kênh LC79")
        return {
            "status": "error",
            "message": "Không tìm thấy kênh LC79. Tài khoản chưa join hoặc kênh ẩn.",
            "scan_log": scan_log,
        }

    _log("success", "Đã resolve kênh LC79")

    try:
        await client(functions.channels.JoinChannelRequest(channel=channel_entity))
    except Exception:
        pass

    # Đảm bảo session_data có codes_seen
    if "codes_seen" not in session_data:
        session_data["codes_seen"] = {}

    items = []
    seen_in_scan = set()
    msg_count = 0

    pattern_lc79 = re.compile(r'LC79[A-Z0-9]{3,}')
    pattern_ma = re.compile(r'Mã code là[:\s]*([A-Z0-9]+)', re.IGNORECASE)

    async for msg in client.iter_messages(channel_entity, limit=100):
        msg_count += 1
        raw_text = getattr(msg, 'message', '') or getattr(msg, 'text', '') or ''
        if not raw_text:
            continue

        msg_date = msg.date.timestamp() if getattr(msg, 'date', None) else now

        # Tìm cả 2 format
        found_raw = []
        for m in pattern_lc79.findall(raw_text):
            if m not in found_raw:
                found_raw.append(m)
        for m in pattern_ma.findall(raw_text):
            candidate = m.strip()
            if candidate and candidate not in found_raw:
                found_raw.append(candidate)

        for raw in found_raw:
            # Strip LC79 prefix (nếu có)
            stripped = raw
            if stripped.startswith("LC79"):
                stripped = stripped[4:].strip()
            if not stripped or stripped in seen_in_scan:
                continue
            seen_in_scan.add(stripped)

            is_new = stripped not in session_data["codes_seen"]
            first_seen = session_data["codes_seen"].get(stripped, {}).get(
                "first_seen", now
            )
            if is_new:
                session_data["codes_seen"][stripped] = {
                    "first_seen": now,
                    "msg_date": msg_date,
                    "msg_id": getattr(msg, "id", 0),
                }
                first_seen = now

            items.append({
                "code": stripped,
                "raw": raw,
                "found_at": now,
                "first_seen": first_seen,
                "msg_date": msg_date,
                "msg_id": getattr(msg, "id", 0),
                "status": "new" if is_new else "seen",
            })

            if len(items) >= 10:
                break
        if len(items) >= 10:
            break

    _log("info", f"Đã quét {msg_count} tin nhắn, tìm {len(items)} mã")

    if not items:
        _log("warning", "Không có mã LC79 trong 100 tin gần nhất")
        return {
            "status": "error",
            "message": "Không tìm thấy code LC79 trong 100 tin gần nhất.",
            "scan_log": scan_log,
        }

    new_count = sum(1 for it in items if it["status"] == "new")
    _log("success", f"Hoàn tất: {len(items)} mã ({new_count} mới)")

    # Lưu scan log vào session (giới hạn 300)
    session_data.setdefault("scan_log", [])
    session_data["scan_log"].extend(scan_log)
    session_data["scan_log"] = session_data["scan_log"][-300:]

    return {
        "status": "ok",
        "codes": [it["code"] for it in items],   # backward compat
        "items": items,
        "new_count": new_count,
        "total_count": len(items),
        "scan_log": scan_log,
    }


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
                    "client": client,
                    "phone": phone,
                    "phone_code_hash": sent.phone_code_hash,
                    "created": time.time(),
                    "codes_seen": {},
                    "scan_log": [],
                    "game_username": None,
                }
            logger.info(f"📱 Session {session_id[:8]} created for {phone}")
            return {"status": "ok", "session_id": session_id}

        except PhoneNumberInvalidError:
            if client and client.is_connected():
                try: await client.disconnect()
                except Exception: pass
            return {"status": "error",
                    "message": "SĐT không hợp lệ. Nhập dạng +84987654321."}
        except FloodWaitError as e:
            if client and client.is_connected():
                try: await client.disconnect()
                except Exception: pass
            return {"status": "error",
                    "message": f"Telegram giới hạn, chờ {e.seconds}s rồi thử lại."}
        except Exception as e:
            if client and client.is_connected():
                try: await client.disconnect()
                except Exception: pass
            logger.error(f"send_code err: {type(e).__name__}: {e}")
            return {"status": "error",
                    "message": f"Lỗi: {type(e).__name__}: {e}"}

    try:
        result = async_manager.run(_do(), timeout=45)
    except Exception as e:
        logger.error(f"async_manager send_code fail: {e}")
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json or {}
    session_id = data.get("session_id")
    code = (data.get("code") or "").strip().replace(" ", "").replace("-", "")

    if not session_id or not code:
        return jsonify({"status": "error",
                        "message": "Thiếu session hoặc mã OTP."})

    with SESSIONS_LOCK:
        s = sessions.get(session_id)

    if not s:
        return jsonify({"status": "error",
                        "message": "Phiên đã hết hạn (>10 phút). Bắt đầu lại."})

    async def _do():
        client = s["client"]
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            return {"status": "error", "message": "Mất kết nối Telegram."}

        try:
            await client.sign_in(
                phone=s["phone"], code=code,
                phone_code_hash=s["phone_code_hash"],
            )
            logger.info(f"✅ OTP OK session {session_id[:8]}")
            # Xoá phone_code_hash sau khi dùng xong
            s["phone_code_hash"] = None
            return {"status": "ok"}

        except SessionPasswordNeededError:
            return {"status": "2fa"}
        except PhoneCodeInvalidError:
            return {"status": "error", "message": "Mã OTP không đúng. Nhập lại."}
        except PhoneCodeExpiredError:
            return {"status": "error",
                    "message": "Mã OTP đã hết hạn. Bắt đầu lại để nhận mã mới."}
        except FloodWaitError as e:
            return {"status": "error",
                    "message": f"Thử quá nhiều lần. Chờ {e.seconds}s."}
        except AuthRestartError:
            return {"status": "error",
                    "message": "Telegram yêu cầu restart. Bắt đầu lại."}
        except Exception as e:
            logger.error(f"verify_code {type(e).__name__}: {e}")
            return {"status": "error",
                    "message": f"Lỗi: {type(e).__name__}: {str(e)[:200]}"}

    try:
        result = async_manager.run(_do(), timeout=45)
    except Exception as e:
        logger.error(f"async_manager verify_code fail: {e}")
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json or {}
    session_id = data.get("session_id")
    password = data.get("password") or ""

    if not session_id or not password:
        return jsonify({"status": "error",
                        "message": "Thiếu session hoặc mật khẩu."})

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
            return {"status": "ok"}
        except Exception as e:
            logger.warning(f"2FA fail: {type(e).__name__}: {e}")
            return {"status": "error", "message": "Mật khẩu 2FA không đúng."}

    try:
        result = async_manager.run(_do(), timeout=45)
    except Exception as e:
        logger.error(f"async_manager verify_2fa fail: {e}")
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


@flask_app.route("/api/login_game", methods=["POST"])
def api_login_game():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    session_id = data.get("session_id")

    if not username or not password:
        return jsonify({"status": "error",
                        "message": "Vui lòng nhập đủ Username và Mật khẩu."})

    result = login_game_sync(username, password)

    # Tie account vào session để dùng cho token endpoints
    if result.get("status") == "ok" and session_id:
        with SESSIONS_LOCK:
            s = sessions.get(session_id)
        if s:
            s["game_username"] = username

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
        return jsonify({"status": "error",
                        "message": "Phiên đã hết hạn. Bắt đầu lại."})

    async def _do():
        client = s["client"]
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            return {"status": "error", "message": "Mất kết nối. Bắt đầu lại."}
        return await _async_fetch_codes(client, s)

    try:
        result = async_manager.run(_do(), timeout=60)
    except Exception as e:
        logger.error(f"async_manager get_codes fail: {e}")
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


# ---------------- TOKEN / JWT / API ENDPOINTS ----------------

def _require_session_account(session_id):
    """Helper trả về (session, account) hoặc lỗi dict."""
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
    _, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)
    return jsonify(account_manager.get_access_token(acc))


@flask_app.route("/api/get_jwt", methods=["POST"])
def api_get_jwt():
    data = request.json or {}
    _, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)
    force = bool(data.get("force"))
    return jsonify(account_manager.get_jwt(acc, force=force))


@flask_app.route("/api/refresh_jwt", methods=["POST"])
def api_refresh_jwt():
    data = request.json or {}
    _, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)
    return jsonify(account_manager.refresh_jwt(acc))


@flask_app.route("/api/account_status", methods=["POST"])
def api_account_status():
    data = request.json or {}
    _, acc, err = _require_session_account(data.get("session_id"))
    if err:
        return jsonify(err)

    return jsonify({
        "status": "ok",
        "username": acc.username,
        "nickname": acc.nickname,
        "vin": acc.vin,
        "xu": acc.xu,
        "login_time": acc.login_time,
        "access_token_ready": bool(acc.access_token),
        "access_token_time": acc.access_token_time,
        "jwt_ready": bool(acc.jwt),
        "jwt_valid": acc.is_jwt_valid(),
        "jwt_exp": acc.jwt_exp,
        "jwt_expires_in_minutes": acc.jwt_remaining_minutes(),
    })


@flask_app.route("/api/check_api", methods=["POST"])
def api_check_api():
    """Ping nhanh 2 endpoint để xác định reachability."""
    results = {}
    overall_ok = True

    start = time.time()
    try:
        resp = requests.get(
            GAME_API_URL,
            params={"c": "3", "un": "", "pw": "", "cp": "R",
                    "cl": "R", "pf": "web", "at": ""},
            timeout=10,
        )
        results["game_api"] = {
            "name": "Game API",
            "url": GAME_API_URL,
            "http": resp.status_code,
            "elapsed_ms": int((time.time() - start) * 1000),
            "ok": resp.status_code < 500,
        }
    except Exception as e:
        results["game_api"] = {
            "name": "Game API", "url": GAME_API_URL,
            "error": str(e)[:150], "ok": False,
        }
        overall_ok = False

    start = time.time()
    try:
        resp = requests.post(LOGIN_URL, json={}, timeout=10)
        results["login_url"] = {
            "name": "JWT Login",
            "url": LOGIN_URL,
            "http": resp.status_code,
            "elapsed_ms": int((time.time() - start) * 1000),
            "ok": resp.status_code < 500,
        }
    except Exception as e:
        results["login_url"] = {
            "name": "JWT Login", "url": LOGIN_URL,
            "error": str(e)[:150], "ok": False,
        }
        overall_ok = False

    return jsonify({
        "status": "ok" if overall_ok else "warning",
        "results": results,
        "ts": time.time(),
    })


@flask_app.route("/api/api_log", methods=["GET"])
def api_api_log():
    limit = request.args.get("limit", 200, type=int)
    return jsonify({
        "status": "ok",
        "logs": api_logger.get_logs(limit=limit),
        "stats": api_logger.get_stats(),
    })


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
<title>Auto Tool VIP</title>
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
body{
  font-family:'Plus Jakarta Sans',-apple-system,BlinkMacSystemFont,sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;
  padding:20px 16px 60px;overflow-x:hidden;
  background-image:
    radial-gradient(circle at 0% 0%,rgba(139,92,246,0.15) 0%,transparent 50%),
    radial-gradient(circle at 100% 100%,rgba(6,182,212,0.15) 0%,transparent 50%);
  background-attachment:fixed;
}
body>*{position:relative;z-index:1}
.icon{width:24px;height:24px;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;fill:none}
.icon-sm{width:18px;height:18px}
.icon-lg{width:32px;height:32px}
.view{display:none;animation:viewIn .4s cubic-bezier(0.16,1,0.3,1)}
.view.active{display:block}
@keyframes viewIn{from{opacity:0;transform:translateY(15px) scale(0.98)}to{opacity:1;transform:none}}
.header{text-align:center;padding:30px 0 20px}
.logo{
  display:inline-flex;align-items:center;justify-content:center;
  width:80px;height:80px;border-radius:24px;
  background:linear-gradient(135deg,rgba(139,92,246,0.2),rgba(6,182,212,0.2));
  border:1px solid rgba(139,92,246,0.4);color:var(--accent);
  margin-bottom:20px;box-shadow:0 10px 40px rgba(139,92,246,0.2);
  backdrop-filter:blur(10px);
}
.header h1{font-size:26px;font-weight:800;letter-spacing:-0.8px;margin-bottom:8px}
.header h1 span{background:linear-gradient(to right,#a78bfa,#22d3ee);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{color:var(--muted);font-size:14px;font-weight:500}
.steps{display:flex;justify-content:center;gap:6px;margin:20px 0 24px;flex-wrap:wrap}
.step-dot{
  width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,0.05);
  border:1px solid var(--border);display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:800;color:var(--muted);transition:0.3s;
}
.step-dot.done{background:var(--success);border-color:var(--success);color:#fff}
.step-dot.active{background:var(--accent);border-color:var(--accent);color:#fff;box-shadow:0 0 15px var(--accent-glow)}
.section-title{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:1.5px;margin:24px 4px 16px;display:flex;align-items:center;gap:8px}
.section-title::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--border),transparent)}
.lc-header{
  background:linear-gradient(135deg,rgba(30,35,55,0.8),rgba(20,24,39,0.6));
  border:1px solid var(--border);border-radius:24px;padding:32px 20px;
  margin-bottom:20px;text-align:center;backdrop-filter:blur(12px);
  position:relative;overflow:hidden;
}
.lc-header::before{content:'';position:absolute;top:0;left:0;width:100%;height:2px;
  background:linear-gradient(90deg,transparent,var(--accent),transparent)}
.lc-icon{
  display:inline-flex;align-items:center;justify-content:center;width:64px;height:64px;
  border-radius:20px;background:rgba(139,92,246,0.15);color:var(--accent);
  margin-bottom:16px;border:1px solid rgba(139,92,246,0.3);
}
.lc-header h2{font-size:22px;font-weight:800;margin-bottom:8px;letter-spacing:-0.5px}
.lc-header p{font-size:13px;color:var(--muted);font-weight:500}
.panel{background:var(--card);border:1px solid var(--border);border-radius:24px;padding:24px 20px;backdrop-filter:blur(12px)}
.panel label{font-size:12px;font-weight:700;color:var(--muted);display:flex;align-items:center;gap:6px;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px}
.panel input{width:100%;background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:14px;padding:16px;color:var(--text);font-size:15px;font-weight:500;outline:none;transition:0.3s;margin-bottom:20px;font-family:inherit}
.panel input:focus{border-color:var(--accent);box-shadow:0 0 0 4px rgba(139,92,246,0.15);background:rgba(0,0,0,0.5)}
.panel input::placeholder{color:rgba(148,163,184,0.5)}
.btn{
  width:100%;padding:16px;border:none;border-radius:14px;font-size:16px;font-weight:700;
  cursor:pointer;transition:0.2s;display:flex;align-items:center;justify-content:center;
  gap:8px;color:#fff;background:linear-gradient(135deg,#8b5cf6,#6366f1);
  box-shadow:0 8px 24px var(--accent-glow);font-family:inherit;
}
.btn:active{transform:scale(0.97);box-shadow:none}
.btn:disabled{opacity:0.6;pointer-events:none}
.btn-ghost{background:rgba(255,255,255,0.05);border:1px solid var(--border);box-shadow:none}
.btn-sm{padding:10px;font-size:13px}
.alert{padding:14px 16px;border-radius:12px;font-size:13px;font-weight:600;margin-bottom:16px;display:none;line-height:1.5}
.alert.show{display:block}
.alert-err{background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#fca5a5}
.alert-ok{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#6ee7b7}
.alert-info{background:rgba(6,182,212,0.15);border:1px solid rgba(6,182,212,0.3);color:#67e8f9}
.code-box{
  background:rgba(0,0,0,0.3);border:1px solid rgba(16,185,129,0.3);
  border-radius:12px;padding:14px 16px;margin-bottom:12px;
  display:flex;justify-content:space-between;align-items:center;gap:12px;
}
.code-box .code-content{flex:1;min-width:0}
.code-box .code-main{
  font-family:'Courier New',monospace;font-size:16px;color:#34d399;
  font-weight:800;letter-spacing:1.5px;word-break:break-all;
}
.code-box .code-meta{font-size:11px;color:var(--muted);margin-top:4px;font-family:'Plus Jakarta Sans',sans-serif;letter-spacing:0;font-weight:600}
.code-badge{
  display:inline-block;padding:2px 8px;border-radius:6px;
  font-size:9px;font-weight:800;margin-left:6px;
  letter-spacing:0.5px;vertical-align:middle;
}
.code-badge.new{background:rgba(16,185,129,0.2);color:var(--success)}
.code-badge.seen{background:rgba(148,163,184,0.2);color:var(--muted)}
.copy-btn{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#34d399;padding:8px 12px;border-radius:8px;cursor:pointer;font-weight:700;font-size:11px;transition:0.2s;font-family:inherit;flex-shrink:0}
.copy-btn:active{background:#10b981;color:#000}
.info-card{
  background:linear-gradient(135deg,rgba(139,92,246,0.1),rgba(6,182,212,0.05));
  border:1px solid rgba(139,92,246,0.3);border-radius:20px;
  padding:20px;margin-bottom:16px;
}
.info-row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);gap:10px}
.info-row:last-child{border-bottom:none;padding-bottom:0}
.info-row:first-child{padding-top:0}
.info-label{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;flex-shrink:0}
.info-value{font-size:14px;font-weight:800;text-align:right;word-break:break-all}

/* Token cards */
.token-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
@media(max-width:420px){.token-grid{grid-template-columns:1fr}}
.token-card{
  background:var(--card);border:1px solid var(--border);
  border-radius:16px;padding:16px;
}
.token-card.ok{border-color:rgba(16,185,129,0.4)}
.token-card.err{border-color:rgba(239,68,68,0.4)}
.token-label{font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.token-status{font-size:12px;font-weight:700;margin-bottom:10px;line-height:1.5}
.token-status.ok{color:var(--success)}
.token-status.err{color:var(--danger)}
.token-status.pending{color:var(--muted)}
.token-card button{
  width:100%;padding:8px;font-size:11px;margin-top:6px;
  background:rgba(139,92,246,0.15);border:1px solid rgba(139,92,246,0.3);
  color:var(--accent);border-radius:8px;font-weight:700;cursor:pointer;
  font-family:inherit;transition:0.2s;
}
.token-card button:hover{background:rgba(139,92,246,0.25)}
.token-card button:active{transform:scale(0.97)}

/* Stats grid */
.stats-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:20px}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:12px}
.stat-box .stat-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;font-weight:700;margin-bottom:4px}
.stat-box .stat-val{font-size:17px;font-weight:800;color:var(--text)}
.stat-box .stat-val.ok{color:var(--success)}
.stat-box .stat-val.err{color:var(--danger)}

/* Actions */
.code-actions{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
.code-actions button{
  flex:1;min-width:100px;padding:10px;
  background:rgba(255,255,255,0.05);border:1px solid var(--border);
  color:var(--text);border-radius:10px;font-size:12px;font-weight:700;
  cursor:pointer;font-family:inherit;transition:0.2s;
}
.code-actions button:hover{background:rgba(255,255,255,0.1)}
.code-actions button:active{transform:scale(0.97)}
.code-actions button.primary{background:linear-gradient(135deg,#8b5cf6,#6366f1);border-color:transparent;box-shadow:0 4px 12px var(--accent-glow)}

/* API Log viewer */
.api-log{
  max-height:400px;overflow-y:auto;
  font-family:'Courier New',monospace;font-size:11px;
  background:rgba(0,0,0,0.5);border-radius:12px;
  padding:12px;line-height:1.7;
}
.api-log .log-entry{margin-bottom:6px;word-break:break-word;padding-bottom:4px;border-bottom:1px dashed rgba(255,255,255,0.04)}
.api-log .log-entry:last-child{border-bottom:none}
.log-info{color:#67e8f9}
.log-success{color:#34d399}
.log-warning{color:#fbbf24}
.log-error{color:#f87171}
.log-request{color:#a78bfa}
.log-response{color:#94a3b8}
.log-time{color:#64748b;margin-right:6px}
.log-tag{font-weight:800;margin-right:6px}

.footer{text-align:center;color:var(--muted);font-size:12px;margin-top:40px;font-weight:500}
.footer a{color:var(--accent2);text-decoration:none}
</style>
</head>
<body>

<!-- HOME -->
<div id="view-home" class="view active">
  <div class="header">
    <div class="logo"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg></div>
    <h1>Auto Tool <span>VIP</span></h1>
    <p>Lấy code LC79 tự động — Không cần cài app</p>
  </div>
  <div class="panel" style="text-align:center">
    <div style="padding:20px 0">
      <div style="font-size:52px;margin-bottom:16px">🎮</div>
      <div style="font-size:16px;font-weight:700;margin-bottom:8px">Tool Auto LC79</div>
      <div style="font-size:13px;color:var(--muted);margin-bottom:24px;line-height:1.6">
        Đăng nhập Telegram → Đăng nhập game → Nhận 10 code mới nhất, Access Token & JWT
      </div>
    </div>
    <button class="btn" onclick="startFlow()">
      🚀 Bắt Đầu
      <svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
    </button>
  </div>
  <div class="footer">Powered by <a href="/alive" target="_blank">Auto Tool Server</a></div>
</div>

<!-- STEP 1: API INFO -->
<div id="view-api" class="view">
  <button class="btn btn-ghost" style="margin-bottom:16px;padding:10px" onclick="showView('view-home')">← Quay lại</button>
  <div class="lc-header">
    <div class="lc-icon"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
    <h2>Xác Thực Telegram</h2>
    <p>Kết nối tài khoản Telegram của bạn</p>
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
    <div class="lc-icon" style="color:var(--accent2);background:rgba(6,182,212,0.1);border-color:rgba(6,182,212,0.3)">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg>
    </div>
    <h2>Nhập Mã OTP</h2>
    <p>Kiểm tra Telegram để lấy mã</p>
  </div>
  <div class="steps" id="steps2"></div>
  <div class="panel">
    <div id="alert-otp" class="alert alert-err"></div>
    <label>Mã OTP</label>
    <input type="text" id="otpCode" placeholder="VD: 12345" inputmode="numeric" autocomplete="one-time-code" maxlength="7">
    <button class="btn" id="btn-verify-otp" onclick="verifyOTP()">Xác Nhận OTP</button>
    <div style="margin-top:12px;text-align:center;font-size:12px;color:var(--muted)">
      Không nhận được mã? <a href="#" onclick="restart();return false" style="color:var(--accent2)">Gửi lại</a>
    </div>
  </div>
</div>

<!-- STEP 3: 2FA -->
<div id="view-2fa" class="view">
  <button class="btn btn-ghost" style="margin-bottom:16px;padding:10px" onclick="restart()">← Làm lại</button>
  <div class="lc-header" style="border-color:rgba(239,68,68,0.3)">
    <div class="lc-icon" style="color:var(--danger);background:rgba(239,68,68,0.1);border-color:rgba(239,68,68,0.3)">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    </div>
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
    <div class="lc-icon" style="color:var(--accent2);background:rgba(6,182,212,0.1);border-color:rgba(6,182,212,0.3)">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 12h4m-2-2v4"/></svg>
    </div>
    <h2>Đăng Nhập Game LC79</h2>
    <p>Xem số dư + nhận code + token</p>
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

<!-- STEP 5: DASHBOARD / RESULT -->
<div id="view-result" class="view">
  <div class="lc-header" style="border-color:rgba(16,185,129,0.3)">
    <div class="lc-icon" style="color:var(--success);background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.3)">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
    </div>
    <h2>Dashboard</h2>
    <p>Tài khoản game & Token Manager</p>
  </div>

  <!-- Account info -->
  <div class="info-card">
    <div class="info-row"><span class="info-label">Tài khoản</span><span class="info-value" id="info-username">-</span></div>
    <div class="info-row"><span class="info-label">Số dư Vin</span><span class="info-value" id="info-balance" style="color:var(--success)">0</span></div>
    <div class="info-row"><span class="info-label">Xu</span><span class="info-value" id="info-xu" style="color:var(--accent2)">0</span></div>
    <div class="info-row"><span class="info-label">Đăng nhập lúc</span><span class="info-value" id="info-login-time" style="font-size:12px;color:var(--muted)">-</span></div>
  </div>

  <!-- Token status -->
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

  <!-- Stats -->
  <div class="section-title">API Statistics</div>
  <div class="stats-grid">
    <div class="stat-box"><div class="stat-lbl">Total Requests</div><div class="stat-val" id="stat-total">0</div></div>
    <div class="stat-box"><div class="stat-lbl">Success</div><div class="stat-val ok" id="stat-success">0</div></div>
    <div class="stat-box"><div class="stat-lbl">Failed</div><div class="stat-val err" id="stat-failed">0</div></div>
    <div class="stat-box"><div class="stat-lbl">Avg Time</div><div class="stat-val" id="stat-avg">0 ms</div></div>
  </div>

  <!-- Giftcodes -->
  <div class="section-title">Giftcode LC79</div>
  <div class="code-actions">
    <button onclick="fetchCodes()" class="primary">🔄 REFRESH CODE</button>
    <button onclick="copyAllCodes()">📋 COPY ALL</button>
  </div>
  <div class="panel" id="codes-container" style="padding:16px">
    <div style="text-align:center;color:var(--muted);padding:20px">Đang quét kênh...</div>
  </div>

  <!-- API Console -->
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

  <button class="btn btn-ghost" style="margin-top:20px" onclick="restart()">🔄 Làm Lại Từ Đầu</button>
</div>

<script>
let state = {
  session_id: '',
  gameUser: '',
  gameBalance: 0,
  gameXu: 0,
  loginTime: 0,
  codes: [],
  logVisible: false,
  atReady: false,
  jwtReady: false,
  jwtExp: 0,
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

function startFlow() {
  state.session_id = '';
  showView('view-api');
  updateSteps('steps1', 1);
}
function restart() {
  state.session_id = '';
  showView('view-home');
}

async function sendOTP() {
  const api_id = document.getElementById('apiId').value.trim();
  const api_hash = document.getElementById('apiHash').value.trim();
  const phone = document.getElementById('phone').value.trim();
  hideAlert('alert-api');
  if (!api_id || !api_hash || !phone) {
    showAlert('alert-api', 'Vui lòng nhập đầy đủ thông tin!');
    return;
  }
  const btn = document.getElementById('btn-send-otp');
  btn.innerText = 'Đang gửi OTP...'; btn.disabled = true;
  try {
    const res = await fetch('/api/send_code', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({api_id, api_hash, phone})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      state.session_id = data.session_id;
      showView('view-otp');
      updateSteps('steps2', 2);
      document.getElementById('otpCode').value = '';
      document.getElementById('otpCode').focus();
    } else {
      showAlert('alert-api', data.message);
    }
  } catch (e) {
    showAlert('alert-api', 'Lỗi kết nối server!');
  } finally {
    btn.innerText = 'Gửi OTP →'; btn.disabled = false;
  }
}

async function verifyOTP() {
  const code = document.getElementById('otpCode').value.trim();
  hideAlert('alert-otp');
  if (!code) { showAlert('alert-otp', 'Vui lòng nhập mã OTP!'); return; }
  const btn = document.getElementById('btn-verify-otp');
  btn.innerText = 'Đang xác thực...'; btn.disabled = true;
  try {
    const res = await fetch('/api/verify_code', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id, code})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      showView('view-game');
      updateSteps('steps4', 4);
    } else if (data.status === '2fa') {
      showView('view-2fa');
      updateSteps('steps3', 3);
      document.getElementById('password2fa').value = '';
      document.getElementById('password2fa').focus();
    } else {
      showAlert('alert-otp', data.message);
    }
  } catch (e) {
    showAlert('alert-otp', 'Lỗi kết nối server!');
  } finally {
    btn.innerText = 'Xác Nhận OTP'; btn.disabled = false;
  }
}

async function verify2FA() {
  const password = document.getElementById('password2fa').value.trim();
  hideAlert('alert-2fa');
  if (!password) { showAlert('alert-2fa', 'Vui lòng nhập mật khẩu 2FA!'); return; }
  const btn = document.getElementById('btn-verify-2fa');
  btn.innerText = 'Đang xác thực...'; btn.disabled = true;
  try {
    const res = await fetch('/api/verify_2fa', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id, password})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      showView('view-game');
      updateSteps('steps4', 4);
    } else {
      showAlert('alert-2fa', data.message);
    }
  } catch (e) {
    showAlert('alert-2fa', 'Lỗi kết nối server!');
  } finally {
    btn.innerText = 'Xác Nhận'; btn.disabled = false;
  }
}

async function gameLogin() {
  const username = document.getElementById('gameUser').value.trim();
  const password = document.getElementById('gamePass').value;
  hideAlert('alert-game');
  if (!username || !password) {
    showAlert('alert-game', 'Vui lòng nhập đủ Username và Mật khẩu game!');
    return;
  }
  const btn = document.getElementById('btn-game-login');
  btn.innerText = 'Đang đăng nhập...'; btn.disabled = true;
  try {
    const res = await fetch('/api/login_game', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username, password, session_id: state.session_id})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      state.gameUser = data.username;
      state.gameBalance = data.balance;
      state.gameXu = data.xuTotal;
      state.loginTime = data.login_time || (Date.now() / 1000);
      state.atReady = !!data.access_token_ready;
      // Move to dashboard
      showView('view-result');
      renderAccountInfo();
      updateTokenStatusFromState();
      await fetchCodes();
      await refreshStats();
    } else {
      showAlert('alert-game', data.message);
    }
  } catch (e) {
    showAlert('alert-game', 'Lỗi kết nối server!');
  } finally {
    btn.innerText = 'Đăng Nhập →'; btn.disabled = false;
  }
}

function renderAccountInfo() {
  document.getElementById('info-username').textContent = state.gameUser || '-';
  document.getElementById('info-balance').textContent = Number(state.gameBalance||0).toLocaleString('vi-VN');
  document.getElementById('info-xu').textContent = Number(state.gameXu||0).toLocaleString('vi-VN');
  if (state.loginTime) {
    document.getElementById('info-login-time').textContent =
      new Date(state.loginTime * 1000).toLocaleString('vi-VN');
  }
}

function updateTokenStatusFromState() {
  const atCard = document.getElementById('token-card-at');
  const atStatus = document.getElementById('at-status');
  if (state.atReady) {
    atCard.classList.add('ok');
    atStatus.className = 'token-status ok';
    atStatus.textContent = '✅ Đã có token';
  } else {
    atCard.classList.remove('ok');
    atStatus.className = 'token-status pending';
    atStatus.textContent = '⚠ Chưa lấy — bấm GET TOKEN';
  }

  const jwtCard = document.getElementById('token-card-jwt');
  const jwtStatus = document.getElementById('jwt-status');
  if (state.jwtReady && state.jwtExp > 0) {
    const remain = Math.max(0, Math.floor((state.jwtExp - Date.now()/1000) / 60));
    if (remain > 0) {
      jwtCard.classList.add('ok');
      jwtCard.classList.remove('err');
      jwtStatus.className = 'token-status ok';
      jwtStatus.textContent = `✅ Hợp lệ (còn ~${remain} phút)`;
      return;
    }
  }
  jwtCard.classList.remove('ok');
  jwtStatus.className = 'token-status pending';
  jwtStatus.textContent = state.jwtReady ? '⚠ Hết hạn — bấm REFRESH' : '⚠ Chưa có — bấm GET JWT';
}

async function fetchAccessToken() {
  const btn = document.getElementById('btn-get-at');
  btn.textContent = 'Đang lấy...'; btn.disabled = true;
  try {
    const res = await fetch('/api/get_access_token', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      state.atReady = true;
      toast('Đã lấy Access Token ✓');
    } else {
      toast('Lỗi: ' + data.message);
    }
    updateTokenStatusFromState();
    await refreshStats();
  } catch (e) {
    toast('Lỗi kết nối server!');
  } finally {
    btn.textContent = 'GET TOKEN'; btn.disabled = false;
  }
}

async function fetchJWT(force) {
  const btn = document.getElementById(force ? 'btn-refresh-jwt' : 'btn-get-jwt');
  btn.textContent = 'Đang xử lý...'; btn.disabled = true;
  try {
    const endpoint = force ? '/api/refresh_jwt' : '/api/get_jwt';
    const res = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id, force})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      state.jwtReady = true;
      state.jwtExp = data.exp || 0;
      state.atReady = true;  // refresh_jwt cũng lấy lại AT
      toast('Đã có JWT' + (data.has_exp ? ` (còn ~${data.expires_in_minutes} phút)` : ' (không có exp)'));
    } else {
      toast('Lỗi: ' + data.message);
    }
    updateTokenStatusFromState();
    await refreshStats();
  } catch (e) {
    toast('Lỗi kết nối server!');
  } finally {
    btn.textContent = force ? 'REFRESH' : 'GET JWT'; btn.disabled = false;
  }
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

async function fetchCodes() {
  const container = document.getElementById('codes-container');
  container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Đang quét kênh LC79...</div>';
  try {
    const res = await fetch('/api/get_codes', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id})
    });
    const data = await res.json();
    if (data.status === 'ok' && data.items && data.items.length > 0) {
      state.codes = data.items.map(it => it.code);
      container.innerHTML = '';
      data.items.forEach((item, i) => {
        const div = document.createElement('div');
        div.className = 'code-box';
        const badge = item.status === 'new'
          ? '<span class="code-badge new">NEW</span>'
          : '<span class="code-badge seen">SEEN</span>';
        let timeStr = '';
        if (item.msg_date) {
          const d = new Date(item.msg_date * 1000);
          timeStr = d.toLocaleString('vi-VN', {hour:'2-digit', minute:'2-digit', day:'2-digit', month:'2-digit'});
        }
        div.innerHTML = `
          <div class="code-content">
            <div class="code-main">${i+1}. ${item.code}${badge}</div>
            <div class="code-meta">Phát hiện: ${timeStr}</div>
          </div>
          <button class="copy-btn" onclick="copyCode('${item.code}')">COPY</button>
        `;
        container.appendChild(div);
      });
    } else if (data.status === 'ok' && (!data.items || data.items.length === 0)) {
      container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Không tìm thấy code.</div>';
      state.codes = [];
    } else {
      container.innerHTML = `<div style="color:var(--danger);text-align:center;padding:20px">${data.message || 'Lỗi'}</div>`;
      state.codes = [];
    }
  } catch (e) {
    container.innerHTML = '<div style="color:var(--danger);text-align:center;padding:20px">Lỗi kết nối!</div>';
  }
  await refreshStats();
}

function copyCode(text) {
  navigator.clipboard.writeText(text).then(() => toast('Đã copy: ' + text));
}

function copyAllCodes() {
  if (!state.codes || state.codes.length === 0) {
    toast('Chưa có code để copy');
    return;
  }
  const text = state.codes.join('\n');
  navigator.clipboard.writeText(text).then(() => toast(`Đã copy ${state.codes.length} code`));
}

function toast(msg) {
  const t = document.createElement('div');
  t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,#10b981,#059669);color:white;padding:12px 24px;border-radius:12px;font-size:13px;font-weight:700;z-index:9999;transition:0.3s;box-shadow:0 10px 20px rgba(16,185,129,0.3);max-width:90vw;text-align:center';
  t.innerText = msg;
  document.body.appendChild(t);
  setTimeout(() => {
    t.style.opacity='0';
    t.style.transform='translateX(-50%) translateY(20px)';
    setTimeout(()=>t.remove(),300);
  }, 2200);
}

async function checkApi() {
  toast('Đang kiểm tra API...');
  try {
    const res = await fetch('/api/check_api', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id})
    });
    const data = await res.json();
    const r = data.results || {};
    let msg = '';
    for (const k of Object.keys(r)) {
      const item = r[k];
      msg += `${item.name}: ${item.ok ? '✅' : '❌'} ${item.http || item.error || ''}  `;
    }
    toast(msg.trim());
    if (state.logVisible) await loadLogs();
  } catch (e) {
    toast('Lỗi kết nối server');
  }
}

async function toggleLog() {
  state.logVisible = !state.logVisible;
  const panel = document.getElementById('api-log-panel');
  if (state.logVisible) {
    panel.style.display = 'block';
    await loadLogs();
  } else {
    panel.style.display = 'none';
  }
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
  return `<div class="log-entry log-${e.level}">
    <span class="log-time">[${e.time}]</span>
    <span class="log-tag">[${e.level.toUpperCase()}]</span>
    <span>${safe(e.message)}${safe(extra)}</span>
  </div>`;
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
  } catch (e) {
    toast('Lỗi tải log');
  }
}

async function clearLog() {
  try {
    await fetch('/api/clear_log', {method:'POST'});
    if (state.logVisible) await loadLogs();
    await refreshStats();
    toast('Đã xoá log');
  } catch (e) {
    toast('Lỗi xoá log');
  }
}

document.getElementById('otpCode')?.addEventListener('keypress', e => { if (e.key === 'Enter') verifyOTP(); });
document.getElementById('password2fa')?.addEventListener('keypress', e => { if (e.key === 'Enter') verify2FA(); });
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
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#05050a;color:#f8fafc;min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:20px;
  background-image:
    radial-gradient(circle at 0% 0%,rgba(139,92,246,0.15) 0%,transparent 50%),
    radial-gradient(circle at 100% 100%,rgba(16,185,129,0.15) 0%,transparent 50%);
  background-attachment:fixed;
}
.card{
  background:rgba(20,24,39,0.6);backdrop-filter:blur(12px);
  border:1px solid rgba(16,185,129,0.3);border-radius:24px;
  padding:48px 40px;text-align:center;max-width:440px;width:100%;
  box-shadow:0 20px 60px rgba(16,185,129,0.15);
}
.icon-wrap{
  width:88px;height:88px;margin:0 auto 24px;border-radius:24px;
  background:linear-gradient(135deg,rgba(16,185,129,0.2),rgba(6,182,212,0.2));
  border:1px solid rgba(16,185,129,0.4);
  display:flex;align-items:center;justify-content:center;font-size:44px;
  animation:breathe 2.5s ease-in-out infinite;
}
@keyframes breathe{
  0%,100%{transform:scale(1);box-shadow:0 0 30px rgba(16,185,129,0.3)}
  50%{transform:scale(1.05);box-shadow:0 0 50px rgba(16,185,129,0.5)}
}
h1{font-size:28px;font-weight:800;margin-bottom:8px;
  background:linear-gradient(to right,#34d399,#22d3ee);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.status{display:inline-flex;align-items:center;gap:8px;color:#34d399;font-size:13px;font-weight:600;margin-bottom:24px}
.dot{width:10px;height:10px;border-radius:50%;background:#10b981;
  box-shadow:0 0 12px #10b981;animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.4);opacity:.6}}
.stat{background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.08);
  border-radius:12px;padding:14px 18px;margin-top:12px;
  display:flex;justify-content:space-between;align-items:center}
.stat-label{color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px}
.stat-value{color:#34d399;font-size:15px;font-weight:800;font-family:'Courier New',monospace}
.link-back{display:inline-block;margin-top:24px;color:#22d3ee;text-decoration:none;font-size:13px;font-weight:600}
</style>
</head>
<body>
<div class="card">
  <div class="icon-wrap">🚀</div>
  <h1>SERVER ALIVE</h1>
  <div class="status"><span class="dot"></span>Hệ thống hoạt động ổn định</div>
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
    async_manager.start()
    threading.Thread(target=cleanup_old_sessions, daemon=True, name="SessionCleanup").start()
    threading.Thread(target=keep_alive, daemon=True, name="KeepAlive").start()
    logger.info(f"🚀 Web server khởi động tại port {PORT}")
    run_flask()