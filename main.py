import os
import time
import threading
import logging
import requests
import re
import asyncio
import hashlib
import base64
import json
from collections import deque
from flask import Flask, Response, request, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

from telethon import TelegramClient, functions, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ============ CONFIG ============
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8584201446:AAHAINg4IALEX1sJ-dmiinLsPLZ-u_S0nTc")
PORT          = int(os.getenv("PORT", 10000))
PING_INTERVAL = int(os.getenv("PING_INTERVAL", 10))
WEBAPP_URL    = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")

# LC79 Endpoints
LC79_API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
LC79_LOGIN_URL      = "https://wlb.tele68.com/v1/lobby/auth/login"
LC79_BASE_URL       = "https://gameapi.tele68.com"
LC79_PARAMS         = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="

TARGET_CHANNEL_USERNAME = "codelaucuagame"
TARGET_CHANNEL_ID       = -1002237214360

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger("AutoTool")

flask_app = Flask(__name__)

# ================================================================
#                    LC79 ACCOUNT (MD5 PASSWORD)
# ================================================================
class LC79Account:
    def __init__(self):
        self.un = None
        self.pw_md5 = None
        self.nick = None
        self.jwt = None
        self.jwt_exp = 0
        self.lock = threading.Lock()
        self.redeemed = set()

    def configure(self, username, raw_password, nickname=None):
        """Nhận password THÔ → tự băm MD5"""
        self.un = username.strip()
        self.pw_md5 = hashlib.md5(raw_password.strip().encode("utf-8")).hexdigest()
        self.nick = (nickname or username).strip()
        logger.info(f"[LC79] Đã cấu hình acc {self.nick}")

    @staticmethod
    def parse_jwt_exp(token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            return json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0)
        except Exception:
            return 0

    def is_token_valid(self):
        return self.jwt is not None and time.time() < (self.jwt_exp - 60)

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

    def get_access_token(self):
        try:
            with requests.Session() as s:
                res = s.get(LC79_API_GET_ACCESS, params={
                    "c": "3", "un": self.un, "pw": self.pw_md5,
                    "cp": "R", "cl": "R", "pf": "web", "at": ""
                }, headers=self._headers(), timeout=(5, 30))
            data = res.json()
            if data.get("success"):
                return data.get("accessToken")
            logger.warning(f"[LC79] get_access_token fail: {data}")
            return None
        except Exception as e:
            logger.error(f"[LC79] get_access_token: {e}")
            return None

    def refresh_token(self):
        access = self.get_access_token()
        if not access:
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
                res = s.post(LC79_LOGIN_URL, headers=login_headers,
                             json={"nickName": self.nick, "accessToken": access},
                             timeout=(5, 30))
            data = res.json()
            token = data.get("token")
            if token:
                self.jwt = token
                self.jwt_exp = self.parse_jwt_exp(token)
                remain = int((self.jwt_exp - time.time()) / 60) if self.jwt_exp else 0
                logger.info(f"[LC79] ✅ Login OK, JWT còn {remain} phút")
                return True
            logger.warning(f"[LC79] Login fail: {data}")
            return False
        except Exception as e:
            logger.error(f"[LC79] refresh_token: {e}")
            return False

    def redeem(self, code):
        with self.lock:
            if code in self.redeemed:
                return {"status": "duplicate", "message": "Đã nhập"}

        if not self.is_token_valid():
            if not self.refresh_token():
                return {"status": "fail", "message": "Login fail"}

        try:
            with requests.Session() as s:
                res = s.post(LC79_BASE_URL + LC79_PARAMS,
                             headers=self._headers(self.jwt),
                             json={"code": code.strip(), "captcha": ""},
                             timeout=(5, 30))
            try:
                data = res.json()
            except Exception:
                data = {}

            if res.status_code == 401:
                if self.refresh_token():
                    return self.redeem(code)
                return {"status": "fail", "message": "Token expired"}

            if res.status_code in (200, 201):
                with self.lock:
                    self.redeemed.add(code)
                amount = (data.get("money") or data.get("amount") or data.get("reward")
                          or (data.get("data") or {}).get("money"))
                if amount:
                    return {"status": "success", "message": f"+{int(amount):,}"}
                return {"status": "success", "message": "OK"}

            if res.status_code == 400:
                msg = data.get("message", "Invalid") if isinstance(data, dict) else "Invalid"
                return {"status": "fail", "message": str(msg)[:40]}
            if res.status_code == 428:
                return {"status": "fail", "message": "Captcha"}
            if res.status_code == 429:
                return {"status": "fail", "message": "Rate limit"}
            return {"status": "fail", "message": f"HTTP {res.status_code}"}
        except Exception as e:
            return {"status": "fail", "message": str(e)[:40]}

# ================================================================
#                    GLOBAL STATE (1 USER)
# ================================================================
class AppState:
    def __init__(self):
        self.session_str = None
        self.api_id = None
        self.api_hash = None
        self.phone = None
        self.recent_codes = []
        self.listener_started = False
        self.listener_client = None
        self.lock = threading.Lock()
        self.lc79 = LC79Account()
        self.version = 0  # tăng mỗi khi có thay đổi

state = AppState()

def _bump_version():
    state.version += 1

def add_recent_code(code, status="pending", message=""):
    with state.lock:
        for c in state.recent_codes:
            if c["code"] == code:
                return False
        state.recent_codes.insert(0, {
            "code": code, "ts": time.time(),
            "status": status, "message": message,
        })
        if len(state.recent_codes) > 10:
            state.recent_codes = state.recent_codes[:10]
        _bump_version()
    return True

def update_code_status(code, status, message):
    with state.lock:
        for c in state.recent_codes:
            if c["code"] == code:
                c["status"] = status
                c["message"] = message
                _bump_version()
                return

def auto_redeem(code):
    if not state.lc79.un:
        update_code_status(code, "pending", "Chưa login LC79")
        return
    result = state.lc79.redeem(code)
    update_code_status(code, result["status"], result["message"])
    logger.info(f"[REDEEM] {code} → {result['status']} ({result['message']})")

def add_and_redeem(code):
    if not add_recent_code(code, "pending", "Đang nhập..."):
        return
    logger.info(f"🆕 Code mới: {code}")
    # Redeem ngay lập tức (không chờ)
    threading.Thread(target=auto_redeem, args=(code,), daemon=True).start()

# ================================================================
#                    TELETHON LISTENER (REAL-TIME)
# ================================================================
async def telethon_listener_main():
    try:
        client = TelegramClient(StringSession(state.session_str), state.api_id, state.api_hash)
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("Session không hợp lệ")
            return

        # Resolve channel
        channel = None
        for getter in (
            lambda: client.get_entity(TARGET_CHANNEL_USERNAME),
            lambda: client.get_entity(TARGET_CHANNEL_ID),
        ):
            try:
                channel = await getter()
                if channel:
                    break
            except Exception:
                continue

        if not channel:
            logger.error("Không tìm thấy channel")
            return

        # Auto join nếu chưa
        try:
            await client(functions.channels.JoinChannelRequest(channel=channel))
        except Exception:
            pass

        state.listener_client = client

        # Scan 100 tin gần nhất (chỉ load, không auto-redeem)
        logger.info("📡 Scan 100 tin gần nhất...")
        seen = set()
        async for msg in client.iter_messages(channel, limit=100):
            text = getattr(msg, "message", "") or ""
            for full in re.findall(r"LC79[A-Z0-9]+", text):
                code = full[4:].strip()
                if code and code not in seen:
                    seen.add(code)
                    add_recent_code(code, "pending", "Chưa nhập")

        logger.info(f"✅ Đã load {len(seen)} code cũ | Listener ON")
        state.listener_started = True
        _bump_version()

        # ⚡ EVENT HANDLER REAL-TIME — bắt tin mới tức thì
        @client.on(events.NewMessage(chats=channel))
        async def on_new(event):
            text = event.raw_text or ""
            for full in re.findall(r"LC79[A-Z0-9]+", text):
                code = full[4:].strip()
                if code:
                    add_and_redeem(code)

        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Listener error: {e}")
        state.listener_started = False
        _bump_version()

def _run_listener_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telethon_listener_main())

def start_listener():
    if state.listener_started:
        return False
    threading.Thread(target=_run_listener_loop, daemon=True).start()
    return True

# ================================================================
#                    FLASK API
# ================================================================
@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    d = request.json
    if not all([d.get("api_id"), d.get("api_hash"), d.get("phone")]):
        return jsonify({"status": "error", "message": "Vui lòng nhập đủ thông tin."})

    async def _send():
        try:
            c = TelegramClient(StringSession(), int(d["api_id"]), d["api_hash"])
            await c.connect()
            sent = await c.send_code_request(d["phone"])
            s = c.session.save()
            await c.disconnect()
            return {"status": "ok", "phone_code_hash": sent.phone_code_hash, "session": s}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return jsonify(asyncio.run(_send()))

@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    d = request.json

    async def _verify():
        try:
            c = TelegramClient(StringSession(d["session"]), int(d["api_id"]), d["api_hash"])
            await c.connect()
            try:
                await c.sign_in(phone=d["phone"], code=d["code"],
                                phone_code_hash=d["phone_code_hash"])
            except SessionPasswordNeededError:
                s = c.session.save(); await c.disconnect()
                return {"status": "2fa", "session": s}
            except Exception:
                await c.disconnect()
                return {"status": "error", "message": "OTP sai hoặc hết hạn."}
            s = c.session.save(); await c.disconnect()
            return {"status": "ok", "session": s}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return jsonify(asyncio.run(_verify()))

@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    d = request.json

    async def _verify():
        try:
            c = TelegramClient(StringSession(d["session"]), int(d["api_id"]), d["api_hash"])
            await c.connect()
            try:
                await c.sign_in(password=d["password"])
            except Exception:
                await c.disconnect()
                return {"status": "error", "message": "Mật khẩu 2FA không đúng."}
            s = c.session.save(); await c.disconnect()
            return {"status": "ok", "session": s}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return jsonify(asyncio.run(_verify()))

@flask_app.route("/api/lc79_login", methods=["POST"])
def api_lc79_login():
    """Đăng nhập LC79 + lưu Telegram session + khởi động listener"""
    d = request.json
    username = (d.get("username") or "").strip()
    password = (d.get("password") or "").strip()

    if not username or not password:
        return jsonify({"status": "error", "message": "Nhập tài khoản + mật khẩu LC79."})

    # Cấu hình LC79 (băm MD5 tự động)
    state.lc79.configure(username, password)

    if not state.lc79.refresh_token():
        return jsonify({"status": "error",
                        "message": "Login LC79 thất bại. Kiểm tra tài khoản/mật khẩu."})

    # Lưu Telegram session
    state.session_str = d.get("session")
    state.api_id = int(d["api_id"]) if d.get("api_id") else None
    state.api_hash = d.get("api_hash")
    state.phone = d.get("phone")

    # Khởi động listener real-time
    start_listener()

    return jsonify({"status": "ok", "message": "Đăng nhập thành công!"})

@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    with state.lock:
        codes = [dict(c) for c in state.recent_codes]
    return jsonify({
        "status": "ok",
        "codes": codes,
        "listener": state.listener_started,
        "lc79": state.lc79.is_token_valid() if state.lc79.un else False,
        "version": state.version,
    })

@flask_app.route("/api/redeem", methods=["POST"])
def api_redeem():
    code = (request.json.get("code") or "").strip()
    if not code:
        return jsonify({"status": "error", "message": "Missing code"})
    if not state.lc79.un:
        return jsonify({"status": "error", "message": "Chưa login LC79"})
    threading.Thread(target=auto_redeem, args=(code,), daemon=True).start()
    return jsonify({"status": "ok"})

@flask_app.route("/health")
def health():
    return Response("ok", mimetype="text/plain")

# ================================================================
#                    MINI APP HTML — WHITE THEME
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Auto Tool VIP</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#ffffff; --bg-alt:#f8fafc;
  --text:#0f172a; --muted:#64748b;
  --border:#e2e8f0; --border-strong:#cbd5e1;
  --accent:#6366f1; --accent-bg:#eef2ff;
  --success:#059669; --success-bg:#ecfdf5;
  --danger:#dc2626; --danger-bg:#fef2f2;
  --warn:#d97706; --warn-bg:#fffbeb;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;touch-action:pan-x pan-y}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:var(--bg);color:var(--text);
  padding:20px 14px 40px;overflow-x:hidden;
}
.view{display:none;animation:in .28s ease}
.view.active{display:block}
@keyframes in{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

.icon{width:22px;height:22px;stroke:currentColor;stroke-width:2.2;stroke-linecap:round;stroke-linejoin:round;fill:none}
.icon-sm{width:16px;height:16px}
.icon-lg{width:28px;height:28px}

/* HEADER */
.hero{padding:22px 0 18px;text-align:center}
.logo{
  display:inline-flex;align-items:center;justify-content:center;
  width:64px;height:64px;border-radius:20px;
  background:linear-gradient(135deg,#6366f1,#8b5cf6);
  color:#fff;margin-bottom:14px;
  box-shadow:0 8px 24px rgba(99,102,241,.3);
}
.hero h1{font-size:22px;font-weight:800;letter-spacing:-.5px;margin-bottom:6px}
.hero p{color:var(--muted);font-size:13px;font-weight:500}

/* CARD (service) */
.card{
  background:#fff;border:1.5px solid var(--border);
  border-radius:16px;padding:16px;
  display:flex;align-items:center;gap:14px;
  cursor:pointer;transition:.2s;
  box-shadow:0 1px 3px rgba(0,0,0,.02);
}
.card:active{transform:scale(.98);border-color:var(--accent)}
.card .ic{color:var(--accent);background:var(--accent-bg);width:44px;height:44px;border-radius:12px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.card .info{flex:1}
.card .ttl{font-size:15px;font-weight:700;margin-bottom:2px}
.card .sub{font-size:12px;color:var(--muted)}
.card .badge{background:var(--danger);color:#fff;font-size:9px;font-weight:800;padding:3px 8px;border-radius:99px;letter-spacing:.3px}
.card .arrow{color:var(--muted)}

/* BACK */
.back{display:inline-flex;align-items:center;gap:6px;background:var(--bg-alt);border:1px solid var(--border);color:var(--text);padding:8px 14px;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;margin-bottom:16px}
.back:active{transform:scale(.95)}

/* PANEL */
.panel{background:#fff;border:1.5px solid var(--border);border-radius:18px;padding:20px 18px}
.panel .ttl{font-size:17px;font-weight:800;margin-bottom:4px}
.panel .desc{font-size:12px;color:var(--muted);margin-bottom:18px}

.field{margin-bottom:14px}
.field label{display:block;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px}
.field input{
  width:100%;background:var(--bg-alt);border:1.5px solid var(--border);
  border-radius:12px;padding:13px 14px;color:var(--text);
  font-size:15px;font-weight:500;outline:none;transition:.2s;
}
.field input:focus{border-color:var(--accent);background:#fff;box-shadow:0 0 0 3px var(--accent-bg)}
.field input::placeholder{color:#94a3b8;font-weight:400}

.btn{
  width:100%;padding:14px;border:none;border-radius:12px;
  font-size:15px;font-weight:700;cursor:pointer;transition:.15s;
  background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;
  box-shadow:0 4px 14px rgba(99,102,241,.3);
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.btn:active{transform:scale(.97);box-shadow:none}
.btn:disabled{opacity:.6;pointer-events:none}
.btn.ghost{background:var(--bg-alt);color:var(--text);border:1.5px solid var(--border);box-shadow:none}

/* RESULT VIEW */
.status-bar{
  display:flex;align-items:center;gap:8px;
  padding:10px 14px;border-radius:12px;
  background:var(--bg-alt);border:1px solid var(--border);
  font-size:12px;font-weight:600;color:var(--muted);
  margin-bottom:14px;
}
.status-bar .dot{width:8px;height:8px;border-radius:50%;background:#94a3b8;flex-shrink:0}
.status-bar.on .dot{background:var(--success);box-shadow:0 0 0 3px rgba(5,150,105,.15);animation:pulse 1.4s infinite}
.status-bar.on{color:var(--success);background:var(--success-bg);border-color:#a7f3d0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}

.codes-wrap{
  background:#fff;border:1.5px solid var(--border);
  border-radius:16px;overflow:hidden;
}
.codes-head{
  padding:12px 16px;border-bottom:1px solid var(--border);
  font-size:12px;font-weight:800;color:var(--muted);
  text-transform:uppercase;letter-spacing:.6px;
  display:flex;justify-content:space-between;align-items:center;
  background:var(--bg-alt);
}
.codes-head .count{background:var(--accent-bg);color:var(--accent);padding:2px 8px;border-radius:6px;font-size:11px}

.code-row{
  display:flex;align-items:center;gap:10px;
  padding:11px 14px;border-bottom:1px solid #f1f5f9;
  font-size:13px;
  animation:slideIn .3s ease;
}
.code-row:last-child{border-bottom:none}
@keyframes slideIn{from{background:var(--accent-bg)}to{background:transparent}}
.code-num{
  width:22px;height:22px;flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  background:var(--bg-alt);color:var(--muted);
  font-size:10px;font-weight:800;border-radius:6px;
}
.code-val{flex:1;font-family:'SF Mono','Monaco',monospace;font-weight:700;font-size:13.5px;letter-spacing:.5px;color:var(--text);overflow:hidden;text-overflow:ellipsis}
.code-status{font-size:10.5px;font-weight:700;padding:3px 8px;border-radius:6px;letter-spacing:.2px;flex-shrink:0;max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.code-status.success{background:var(--success-bg);color:var(--success)}
.code-status.fail{background:var(--danger-bg);color:var(--danger)}
.code-status.pending{background:var(--warn-bg);color:var(--warn)}
.code-status.duplicate{background:#f1f5f9;color:var(--muted)}

.empty{padding:40px 20px;text-align:center;color:var(--muted);font-size:13px}

/* toast */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;padding:10px 18px;border-radius:10px;font-size:13px;font-weight:600;z-index:9999;transition:.25s;box-shadow:0 8px 24px rgba(0,0,0,.25)}
</style>
</head>
<body>

<!-- HOME -->
<div id="view-home" class="view active">
  <div class="hero">
    <div class="logo"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg></div>
    <h1>Auto Tool VIP</h1>
    <p>Tự động hóa thế hệ mới</p>
  </div>
  <div class="card" onclick="showView('view-login')">
    <div class="ic"><svg class="icon" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="12" x2="10" y2="12"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="15" y1="13" x2="15.01" y2="13"/><line x1="18" y1="11" x2="18.01" y2="11"/></svg></div>
    <div class="info">
      <div class="ttl">Auto LC79 VIP</div>
      <div class="sub">Auto lấy & nhập code siêu tốc</div>
    </div>
    <div class="badge">HOT</div>
    <div class="arrow"><svg class="icon icon-sm" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg></div>
  </div>
</div>

<!-- LOGIN TELEGRAM -->
<div id="view-login" class="view">
  <button class="back" onclick="showView('view-home')">
    <svg class="icon icon-sm" viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg> Quay lại
  </button>
  <div class="panel">
    <div class="ttl">Kết nối Telegram</div>
    <div class="desc">Nhập API ID & Hash từ my.telegram.org</div>
    <div class="field"><label>API ID</label><input type="number" id="apiId" placeholder="12345678" autocomplete="off"></div>
    <div class="field"><label>API HASH</label><input type="password" id="apiHash" placeholder="Nhập API HASH" autocomplete="off"></div>
    <div class="field"><label>Số điện thoại</label><input type="tel" id="phone" placeholder="+84987654321" autocomplete="off"></div>
    <button class="btn" id="btn-send" onclick="sendCode()">Gửi mã OTP</button>
  </div>
</div>

<!-- OTP -->
<div id="view-otp" class="view">
  <button class="back" onclick="showView('view-login')">
    <svg class="icon icon-sm" viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg> Quay lại
  </button>
  <div class="panel">
    <div class="ttl">Nhập mã OTP</div>
    <div class="desc">Mã đã gửi đến Telegram của bạn</div>
    <div class="field"><label>Mã OTP</label><input type="text" id="otpCode" placeholder="12345" inputmode="numeric" autocomplete="one-time-code"></div>
    <button class="btn" id="btn-otp" onclick="verifyCode()">Xác nhận</button>
  </div>
</div>

<!-- 2FA -->
<div id="view-2fa" class="view">
  <button class="back" onclick="showView('view-otp')">
    <svg class="icon icon-sm" viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg> Quay lại
  </button>
  <div class="panel">
    <div class="ttl">Xác thực 2 lớp</div>
    <div class="desc">Tài khoản được bảo vệ bằng 2FA</div>
    <div class="field"><label>Mật khẩu 2FA</label><input type="password" id="pass2fa" placeholder="Nhập mật khẩu 2FA" autocomplete="off"></div>
    <button class="btn" id="btn-2fa" onclick="verify2FA()">Đăng nhập</button>
  </div>
</div>

<!-- LC79 LOGIN -->
<div id="view-lc79" class="view">
  <div class="panel">
    <div class="ttl">Đăng nhập LC79</div>
    <div class="desc">Mật khẩu sẽ được tự động mã hóa MD5 bảo mật</div>
    <div class="field"><label>Tài khoản LC79</label><input type="text" id="lc79User" placeholder="username" autocomplete="off"></div>
    <div class="field"><label>Mật khẩu LC79 (thô)</label><input type="password" id="lc79Pass" placeholder="Nhập mật khẩu thật" autocomplete="off"></div>
    <button class="btn" id="btn-lc79" onclick="loginLC79()">Bắt đầu Auto</button>
  </div>
</div>

<!-- RESULT -->
<div id="view-result" class="view">
  <div class="status-bar" id="statusBar">
    <div class="dot"></div>
    <span id="statusText">Đang khởi động...</span>
  </div>
  <div class="codes-wrap">
    <div class="codes-head">
      <span>10 code mới nhất</span>
      <span class="count" id="codesCount">0</span>
    </div>
    <div id="codesList">
      <div class="empty">Đang chờ code...</div>
    </div>
  </div>
  <button class="btn ghost" style="margin-top:14px" onclick="showView('view-home')">Về trang chủ</button>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

let auth = { api_id:'', api_hash:'', phone:'', phone_code_hash:'', session:'', password:'' };
let pollTimer = null;
let lastVersion = -1;

function showView(id){
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  window.scrollTo({top:0,behavior:'smooth'});
}

async function sendCode(){
  const api_id = document.getElementById('apiId').value.trim();
  const api_hash = document.getElementById('apiHash').value.trim();
  const phone = document.getElementById('phone').value.trim();
  if (!api_id || !api_hash || !phone) return alert('Nhập đủ thông tin!');
  const btn = document.getElementById('btn-send');
  btn.disabled = true; btn.innerText = 'Đang gửi...';
  try {
    const r = await fetch('/api/send_code', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_id,api_hash,phone})});
    const d = await r.json();
    if (d.status === 'ok') {
      auth = {...auth, api_id, api_hash, phone, phone_code_hash:d.phone_code_hash, session:d.session};
      showView('view-otp');
    } else alert('Lỗi: '+d.message);
  } catch(e){ alert('Lỗi kết nối!'); }
  finally { btn.disabled=false; btn.innerText='Gửi mã OTP'; }
}

async function verifyCode(){
  const code = document.getElementById('otpCode').value.trim();
  if (!code) return alert('Nhập OTP!');
  const btn = document.getElementById('btn-otp');
  btn.disabled = true; btn.innerText = 'Đang xác thực...';
  try {
    const r = await fetch('/api/verify_code', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...auth, code})});
    const d = await r.json();
    if (d.status === 'ok') { auth.session = d.session; showView('view-lc79'); }
    else if (d.status === '2fa') { auth.session = d.session; showView('view-2fa'); }
    else alert('Lỗi: '+d.message);
  } catch(e){ alert('Lỗi!'); }
  finally { btn.disabled=false; btn.innerText='Xác nhận'; }
}

async function verify2FA(){
  const password = document.getElementById('pass2fa').value.trim();
  if (!password) return alert('Nhập 2FA!');
  const btn = document.getElementById('btn-2fa');
  btn.disabled = true; btn.innerText = 'Đang xác thực...';
  try {
    const r = await fetch('/api/verify_2fa', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...auth, password})});
    const d = await r.json();
    if (d.status === 'ok') { auth.session = d.session; showView('view-lc79'); }
    else alert('Lỗi: '+d.message);
  } catch(e){ alert('Lỗi!'); }
  finally { btn.disabled=false; btn.innerText='Đăng nhập'; }
}

async function loginLC79(){
  const username = document.getElementById('lc79User').value.trim();
  const password = document.getElementById('lc79Pass').value.trim();
  if (!username || !password) return alert('Nhập đủ tài khoản + mật khẩu LC79!');
  const btn = document.getElementById('btn-lc79');
  btn.disabled = true; btn.innerText = 'Đang đăng nhập...';
  try {
    const r = await fetch('/api/lc79_login', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...auth, username, password})});
    const d = await r.json();
    if (d.status === 'ok') {
      showView('view-result');
      startPolling();
    } else alert('Lỗi: '+d.message);
  } catch(e){ alert('Lỗi!'); }
  finally { btn.disabled=false; btn.innerText='Bắt đầu Auto'; }
}

function startPolling(){
  if (pollTimer) clearInterval(pollTimer);
  // Poll mỗi 100ms — cập nhật siêu nhanh
  pollTimer = setInterval(pollCodes, 100);
  pollCodes();
}

async function pollCodes(){
  try {
    const r = await fetch('/api/get_codes', {method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const d = await r.json();
    if (d.status !== 'ok') return;

    // Cập nhật status bar
    const bar = document.getElementById('statusBar');
    const txt = document.getElementById('statusText');
    if (d.listener && d.lc79) { bar.classList.add('on'); txt.innerText = '🟢 Auto ON — Đang lắng nghe'; }
    else if (d.listener) { bar.classList.add('on'); txt.innerText = '🟡 Listener ON — Chưa login LC79'; }
    else { bar.classList.remove('on'); txt.innerText = '🔴 Offline'; }

    // Cập nhật codes nếu version thay đổi
    if (d.version !== lastVersion) {
      lastVersion = d.version;
      renderCodes(d.codes);
    }
  } catch(e){}
}

function renderCodes(codes){
  const list = document.getElementById('codesList');
  document.getElementById('codesCount').innerText = codes.length;
  if (!codes.length) {
    list.innerHTML = '<div class="empty">Đang chờ code...</div>';
    return;
  }
  list.innerHTML = codes.map((c, i) => {
    const num = String(i+1).padStart(2,'0');
    const st = c.status || 'pending';
    const cls = ['success','fail','pending','duplicate'].includes(st) ? st : 'pending';
    const label = c.message || ({success:'✓',fail:'✗',pending:'...',duplicate:'Đã nhập'}[st] || st);
    return `<div class="code-row">
      <div class="code-num">${num}</div>
      <div class="code-val">${c.code}</div>
      <div class="code-status ${cls}">${label}</div>
    </div>`;
  }).join('');
}
</script>
</body>
</html>
"""

@flask_app.route("/")
def index():
    return Response(MINIAPP_HTML, mimetype="text/html")

# ================================================================
#                    BOT
# ================================================================
def keep_alive():
    if not WEBAPP_URL: return
    url = WEBAPP_URL.rstrip("/") + "/health"
    time.sleep(15)
    while True:
        try: requests.get(url, timeout=10)
        except Exception: pass
        time.sleep(PING_INTERVAL)

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)

START_MESSAGE = """
<tg-emoji emoji-id="5368324170671202286">💎</tg-emoji> <b>AUTO TOOL CENTER — VIP</b> <tg-emoji emoji-id="5368324170671202286">💎</tg-emoji>

<tg-emoji emoji-id="5447644880824181073">👑</tg-emoji> Chào <i>{name}</i> đến với hệ thống!
<tg-emoji emoji-id="4960917714325121485">🔥</tg-emoji> Auto lấy & nhập code LC79 siêu tốc.

━━━━━━━━━━━━━━━━━━━━
⚡ <b>TÍNH NĂNG</b>
• Đăng nhập Telegram an toàn (OTP + 2FA)
• Auto bắt tin nhắn code real-time (millisecond)
• Tự động nhập code khi vừa xuất hiện
• Hiển thị 10 code mới nhất, cập nhật 0.1s
• Mật khẩu LC79 tự động băm MD5 bảo mật
"""

KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("🚀 MỞ MINI APP VIP",
        web_app=WebAppInfo(url=WEBAPP_URL or "https://example.com"))
]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "bạn"
    await update.message.reply_text(
        START_MESSAGE.format(name=name), parse_mode="HTML",
        reply_markup=KEYBOARD, disable_web_page_preview=True,
    )

async def error_handler(update, context):
    if isinstance(context.error, Conflict):
        logger.warning("⚠️ Conflict bot, chờ 10s...")
        await asyncio.sleep(10)
    else:
        logger.error(f"Error: {context.error}")

async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "Khởi động Bot VIP")])

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN chưa set!")
        return
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Khởi động Bot VIP...")

    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
    except Exception: pass

    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_error_handler(error_handler)
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()