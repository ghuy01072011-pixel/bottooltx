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
GAME_API_URL  = "https://apifo88daigia.tele68.com/api"

SERVER_START_TIME = time.time()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AutoTool")

flask_app = Flask(__name__)


# ================================================================
#              ASYNC LOOP MANAGER (FIX CHÍNH)
# ================================================================
class AsyncLoopManager:
    """Chạy 1 event loop duy nhất trong background thread.
    Mọi coroutine Telethon đều chạy trên loop này → không bị
    'event loop must not change after connection'."""

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
        """Chạy coroutine trên loop chính, block đến khi xong."""
        if not self._started or not self.loop:
            raise RuntimeError("AsyncLoopManager chưa start!")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._started = False


async_manager = AsyncLoopManager()


# ============ SESSION STORAGE ============
# {session_id: {"client": TelegramClient, "phone": str, "phone_code_hash": str,
#               "created": float}}
sessions = {}
SESSIONS_LOCK = threading.Lock()
SESSION_TTL = 600  # 10 phút


def cleanup_old_sessions():
    """Dọn session >10 phút — chạy trong thread riêng."""
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
                        # Disconnect client qua async_manager để chạy đúng loop
                        async_manager.run(s["client"].disconnect(), timeout=5)
                    except Exception:
                        pass
                    logger.info(f"🧹 Cleaned session {sid[:8]}")
        except Exception as e:
            logger.warning(f"cleanup err: {e}")


def fmt_num(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


# ============ GAME LOGIN (sync — requests) ============
def login_game_sync(username, password):
    try:
        pw_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {"c": 3, "un": username, "pw": pw_hash,
                  "cp": "R", "cl": "R", "pf": "web", "at": ""}
        resp = requests.get(GAME_API_URL, params=params, timeout=15)
        result = resp.json()
        if not result.get("success"):
            return {"status": "error", "message": "Sai tài khoản hoặc mật khẩu game!"}
        user_info = {}
        session_key = result.get("sessionKey", "")
        if session_key:
            try:
                user_info = json.loads(base64.b64decode(session_key).decode("utf-8"))
            except Exception:
                pass
        return {
            "status": "ok",
            "username": user_info.get("nickname") or username,
            "balance": user_info.get("vinTotal", 0),
            "xuTotal": user_info.get("xuTotal", 0),
        }
    except Exception as e:
        return {"status": "error", "message": f"Lỗi kết nối: {e}"}


# ============ TELEGRAM ASYNC HELPERS ============
async def _async_fetch_codes(client):
    channel_entity = None
    try:
        async for dialog in client.iter_dialogs(limit=200):
            if dialog.id == -1002237214360 or (
                dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame'
            ):
                channel_entity = dialog.entity
                break
    except Exception as e:
        logger.warning(f"iter_dialogs: {e}")

    if not channel_entity:
        try: channel_entity = await client.get_entity('codelaucuagame')
        except Exception: pass
    if not channel_entity:
        try: channel_entity = await client.get_entity(-1002237214360)
        except Exception: pass
    if not channel_entity:
        return {"status": "error",
                "message": "Không tìm thấy kênh LC79. Tài khoản chưa join hoặc kênh ẩn."}

    try:
        await client(functions.channels.JoinChannelRequest(channel=channel_entity))
    except Exception:
        pass

    codes = []
    pattern = re.compile(r'LC79[A-Z0-9]+')
    async for msg in client.iter_messages(channel_entity, limit=100):
        raw_text = getattr(msg, 'message', '') or getattr(msg, 'text', '') or ''
        if not raw_text:
            continue
        for m in pattern.findall(raw_text):
            stripped = m[4:].strip()
            if stripped and stripped not in codes:
                codes.append(stripped)
            if len(codes) >= 10:
                break
        if len(codes) >= 10:
            break

    if not codes:
        return {"status": "error",
                "message": "Không tìm thấy code LC79 trong 100 tin gần nhất."}
    return {"status": "ok", "codes": codes}


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
            return {"status": "error",
                    "message": "Mất kết nối Telegram. Bắt đầu lại."}

        try:
            await client.sign_in(
                phone=s["phone"],
                code=code,
                phone_code_hash=s["phone_code_hash"],
            )
            logger.info(f"✅ OTP OK session {session_id[:8]}")
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
                    "message": "Telegram yêu cầu restart. Bắt đầu lại từ đầu."}

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
            return {"status": "error",
                    "message": "Mất kết nối. Bắt đầu lại."}

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

    if not username or not password:
        return jsonify({"status": "error",
                        "message": "Vui lòng nhập đủ Username và Mật khẩu."})

    result = login_game_sync(username, password)
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
            return {"status": "error",
                    "message": "Mất kết nối. Bắt đầu lại."}
        return await _async_fetch_codes(client)

    try:
        result = async_manager.run(_do(), timeout=60)
    except Exception as e:
        logger.error(f"async_manager get_codes fail: {e}")
        return jsonify({"status": "error", "message": f"Server lỗi: {e}"})
    return jsonify(result)


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
  --bg:#05050a; --card:rgba(20,24,39,0.6); --card-hover:rgba(30,35,55,0.8);
  --accent:#8b5cf6; --accent-glow:rgba(139,92,246,0.4);
  --accent2:#06b6d4; --text:#f8fafc; --muted:#94a3b8;
  --border:rgba(255,255,255,0.08); --danger:#ef4444; --success:#10b981;
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
.alert{padding:14px 16px;border-radius:12px;font-size:13px;font-weight:600;margin-bottom:16px;display:none;line-height:1.5}
.alert.show{display:block}
.alert-err{background:rgba(239,68,68,0.15);border:1px solid rgba(239,68,68,0.3);color:#fca5a5}
.alert-ok{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#6ee7b7}
.alert-info{background:rgba(6,182,212,0.15);border:1px solid rgba(6,182,212,0.3);color:#67e8f9}
.code-box{
  background:rgba(0,0,0,0.3);border:1px solid rgba(16,185,129,0.3);
  border-radius:12px;padding:14px 16px;margin-bottom:12px;
  display:flex;justify-content:space-between;align-items:center;
  font-family:'Courier New',monospace;font-size:16px;color:#34d399;
  font-weight:800;letter-spacing:1.5px;
}
.copy-btn{background:rgba(16,185,129,0.15);border:1px solid rgba(16,185,129,0.3);color:#34d399;padding:8px 14px;border-radius:8px;cursor:pointer;font-weight:700;font-size:12px;transition:0.2s;font-family:inherit}
.copy-btn:active{background:#10b981;color:#000}
.info-card{
  background:linear-gradient(135deg,rgba(139,92,246,0.1),rgba(6,182,212,0.05));
  border:1px solid rgba(139,92,246,0.3);border-radius:20px;
  padding:20px;margin-bottom:16px;
}
.info-row{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid var(--border)}
.info-row:last-child{border-bottom:none;padding-bottom:0}
.info-row:first-child{padding-top:0}
.info-label{color:var(--muted);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px}
.info-value{font-size:16px;font-weight:800}
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
        Đăng nhập Telegram → Đăng nhập game → Nhận 10 code mới nhất & số dư tài khoản
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
    <p>Xem số dư + nhận code</p>
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

<!-- STEP 5: RESULT -->
<div id="view-result" class="view">
  <div class="lc-header" style="border-color:rgba(16,185,129,0.3)">
    <div class="lc-icon" style="color:var(--success);background:rgba(16,185,129,0.1);border-color:rgba(16,185,129,0.3)">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
    </div>
    <h2>Đăng Nhập Thành Công</h2>
    <p>Thông tin tài khoản & 10 code mới nhất</p>
  </div>
  <div class="info-card" id="game-info">
    <div class="info-row"><span class="info-label">Tài khoản</span><span class="info-value" id="info-username">-</span></div>
    <div class="info-row"><span class="info-label">Số dư Vin</span><span class="info-value" id="info-balance" style="color:var(--success)">0</span></div>
    <div class="info-row"><span class="info-label">Xu</span><span class="info-value" id="info-xu" style="color:var(--accent2)">0</span></div>
  </div>
  <div class="section-title">Code LC79 mới nhất</div>
  <div class="panel" id="codes-container" style="padding:16px">
    <div style="text-align:center;color:var(--muted);padding:20px">Đang quét kênh...</div>
  </div>
  <button class="btn btn-ghost" style="margin-top:20px" onclick="restart()">🔄 Làm Lại Từ Đầu</button>
</div>

<script>
let state = { session_id:'', gameUser:'', gameBalance:0, gameXu:0 };

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
      body: JSON.stringify({username, password})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      state.gameUser = data.username;
      state.gameBalance = data.balance;
      state.gameXu = data.xuTotal;
      await fetchCodes();
    } else {
      showAlert('alert-game', data.message);
    }
  } catch (e) {
    showAlert('alert-game', 'Lỗi kết nối server!');
  } finally {
    btn.innerText = 'Đăng Nhập →'; btn.disabled = false;
  }
}

async function fetchCodes() {
  document.getElementById('info-username').textContent = state.gameUser || '-';
  document.getElementById('info-balance').textContent = Number(state.gameBalance||0).toLocaleString('vi-VN');
  document.getElementById('info-xu').textContent = Number(state.gameXu||0).toLocaleString('vi-VN');
  showView('view-result');
  const container = document.getElementById('codes-container');
  container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Đang quét kênh LC79...</div>';
  try {
    const res = await fetch('/api/get_codes', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: state.session_id})
    });
    const data = await res.json();
    if (data.status === 'ok') {
      container.innerHTML = '';
      if (!data.codes || data.codes.length === 0) {
        container.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">Không tìm thấy code mới.</div>';
      } else {
        data.codes.forEach((code, i) => {
          const div = document.createElement('div');
          div.className = 'code-box';
          div.innerHTML = `<span>${i+1}. LC79${code}</span><button class="copy-btn" onclick="copyCode('LC79${code}')">COPY</button>`;
          container.appendChild(div);
        });
      }
    } else {
      container.innerHTML = `<div style="color:var(--danger);text-align:center;padding:20px">${data.message}</div>`;
    }
  } catch (e) {
    container.innerHTML = '<div style="color:var(--danger);text-align:center;padding:20px">Lỗi kết nối!</div>';
  }
}

function copyCode(text) {
  navigator.clipboard.writeText(text).then(() => {
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg,#10b981,#059669);color:white;padding:12px 24px;border-radius:12px;font-size:14px;font-weight:700;z-index:9999;transition:0.3s;box-shadow:0 10px 20px rgba(16,185,129,0.3)';
    t.innerText = 'Đã copy: ' + text;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity='0'; t.style.transform='translateX(-50%) translateY(20px)'; setTimeout(()=>t.remove(),300); }, 1800);
  });
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
  <div class="stat"><span class="stat-label">Ping</span><span class="stat-value" id="ping">--</span></div>
  <div class="stat"><span class="stat-label">Last Update</span><span class="stat-value" id="lastUpdate">--</span></div>
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
    document.getElementById('lastUpdate').textContent=new Date().toLocaleTimeString('vi-VN');
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
    # 1. Khởi động event loop TRƯỚC KHI có bất kỳ request nào
    async_manager.start()

    # 2. Khởi động thread dọn session
    threading.Thread(target=cleanup_old_sessions, daemon=True, name="SessionCleanup").start()

    # 3. Khởi động self-ping
    threading.Thread(target=keep_alive, daemon=True, name="KeepAlive").start()

    logger.info(f"🚀 Web server khởi động tại port {PORT}")
    run_flask()