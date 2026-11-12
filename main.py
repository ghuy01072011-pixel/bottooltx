import os
import time
import threading
import logging
import requests
import re
import asyncio
import hashlib
import json
import base64

from flask import Flask, Response, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

# Telethon Imports
from telethon import TelegramClient, functions, events, utils
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ============ CONFIG ============
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8584201446:AAHAINg4IALEX1sJ-dmiinLsPLZ-u_S0nTc")
PORT          = int(os.getenv("PORT", 10000))                    
PING_INTERVAL = int(os.getenv("PING_INTERVAL", 10))              
WEBAPP_URL    = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AutoToolVIP")

flask_app = Flask(__name__)

# ================================================================
#                    LC79 AUTO REDEEM CONFIG & CLASS
# ================================================================
TARGET_CHAT_LINK = "https://t.me/codelaucuagame"
TARGET_CHAT_ID = -1002237214360

API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"
BASE_URL = "https://gameapi.tele68.com"
PARAMS = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="

# Danh sách tài khoản LC79 (Mật khẩu nhập thô, tự động mã hóa MD5)
RAW_ACCOUNTS = [
    # {"un": "username_1", "pw": "matkhau_tho_1", "nick": "Nickname1"},
]

class LC79Account:
    def __init__(self, un, raw_pw, nick):
        self.un = un
        # Tự động mã hoá mật khẩu thô sang MD5
        self.pw = hashlib.md5(str(raw_pw).encode('utf-8')).hexdigest()
        self.nick = nick
        self.used_codes = set()
        self.lock = threading.Lock()
        self.jwt = None
        self.jwt_exp = 0

    def _headers(self, auth=None):
        h = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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

    def get_access_token(self):
        try:
            with requests.Session() as s:
                res = s.get(API_GET_ACCESS, params={
                    "c": "3", "un": self.un, "pw": self.pw,
                    "cp": "R", "cl": "R", "pf": "web", "at": ""
                }, headers=self._headers(), timeout=10)
            data = res.json()
            if data.get("success"):
                return data.get("accessToken")
            return None
        except Exception as e:
            logger.error(f"[{self.nick}] Lỗi get_access_token: {e}")
            return None

    def refresh_token(self):
        access_token = self.get_access_token()
        if not access_token:
            return False
        try:
            with requests.Session() as s:
                res = s.post(LOGIN_URL, headers=self._headers(), json={
                    "nickName": self.nick, "accessToken": access_token
                }, timeout=10)
            data = res.json()
            new_token = data.get("token")
            if new_token:
                self.jwt = new_token
                self.jwt_exp = self.parse_jwt_exp(new_token)
                return True
            return False
        except Exception as e:
            logger.error(f"[{self.nick}] Lỗi refresh_token: {e}")
            return False

    def redeem_code(self, code):
        with self.lock:
            if code in self.used_codes:
                return
            self.used_codes.add(code)

        if not self.is_token_valid():
            if not self.refresh_token():
                return

        try:
            with requests.Session() as s:
                res = s.post(BASE_URL + PARAMS, headers=self._headers(self.jwt), json={
                    "code": code.strip(), "captcha": ""
                }, timeout=10)
            data = res.json() if res.content else {}
            if res.status_code in [200, 201]:
                logger.info(f"[{self.nick}] Nạp code {code} thành công!")
            else:
                msg = data.get("message", res.text)
                logger.warning(f"[{self.nick}] Nạp code {code} thất bại: {msg}")
        except Exception as e:
            logger.error(f"[{self.nick}] Lỗi redeem code {code}: {e}")

LC79_ACCOUNTS = [LC79Account(a["un"], a["pw"], a["nick"]) for a in RAW_ACCOUNTS]

def redeem_all_lc79(code):
    for acc in LC79_ACCOUNTS:
        threading.Thread(target=acc.redeem_code, args=(code,), daemon=True).start()


# ================================================================
#                    TELEGRAM API ENDPOINTS (TELETHON)
# ================================================================

@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    data = request.json
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    phone = data.get("phone")

    if not all([api_id, api_hash, phone]):
        return jsonify({"status": "error", "message": "Vui lòng nhập đủ thông tin."})

    async def _send():
        try:
            client = TelegramClient(StringSession(), int(api_id), api_hash)
            await client.connect()
            sent = await client.send_code_request(phone)
            session_str = client.session.save()
            await client.disconnect()
            return {"status": "ok", "phone_code_hash": sent.phone_code_hash, "session": session_str}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return jsonify(asyncio.run(_send()))

@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json
    async def _verify():
        try:
            client = TelegramClient(StringSession(data.get("session")), int(data.get("api_id")), data.get("api_hash"))
            await client.connect()
            try:
                await client.sign_in(phone=data.get("phone"), code=data.get("code"), phone_code_hash=data.get("phone_code_hash"))
            except SessionPasswordNeededError:
                new_session = client.session.save()
                await client.disconnect()
                return {"status": "2fa", "session": new_session, "message": "Vui lòng nhập mật khẩu 2FA."}
            except Exception:
                await client.disconnect()
                return {"status": "error", "message": "Mã OTP không đúng hoặc đã hết hạn."}
            
            new_session = client.session.save()
            await client.disconnect()
            return {"status": "ok", "session": new_session}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return jsonify(asyncio.run(_verify()))

@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json
    async def _verify_2fa():
        try:
            client = TelegramClient(StringSession(data.get("session")), int(data.get("api_id")), data.get("api_hash"))
            await client.connect()
            try:
                await client.sign_in(password=data.get("password"))
            except Exception:
                await client.disconnect()
                return {"status": "error", "message": "Mật khẩu 2FA không chính xác."}
            
            new_session = client.session.save()
            await client.disconnect()
            return {"status": "ok", "session": new_session}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    return jsonify(asyncio.run(_verify_2fa()))

@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    data = request.json
    async def _get_codes():
        client = None
        try:
            client = TelegramClient(StringSession(data.get("session")), int(data.get("api_id")), data.get("api_hash"))
            await client.connect()

            channel_entity = None
            try:
                async for dialog in client.iter_dialogs(limit=100):
                    if dialog.id == -1002237214360 or (dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame'):
                        channel_entity = dialog.entity
                        break
            except Exception:
                pass

            if not channel_entity:
                try:
                    channel_entity = await client.get_entity('codelaucuagame')
                except Exception:
                    pass

            if not channel_entity:
                return {"status": "error", "message": "Không tìm thấy kênh codelaucuagame."}

            try:
                await client(functions.channels.JoinChannelRequest(channel=channel_entity))
            except Exception:
                pass

            codes = []
            pattern = re.compile(r'LC79[A-Z0-9]+')
            async for msg in client.iter_messages(channel_entity, limit=50):
                raw_text = getattr(msg, 'message', '') or getattr(msg, 'text', '')
                if not raw_text:
                    continue
                matches = pattern.findall(raw_text)
                for match in matches:
                    stripped_code = match[4:].strip()
                    if stripped_code and stripped_code not in codes:
                        codes.append(stripped_code)
                    if len(codes) >= 10:
                        break
                if len(codes) >= 10:
                    break

            if not codes:
                return {"status": "error", "message": "Không tìm thấy mã code nào."}

            return {"status": "ok", "codes": codes}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            if client and client.is_connected():
                await client.disconnect()

    return jsonify(asyncio.run(_get_codes()))


# ================================================================
#                    MINI APP HTML (CLEAN LIGHT & MODERN)
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Auto Code LC79 - VIP</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&display=swap');

:root {
  --bg: #f8fafc;
  --card: #ffffff;
  --text: #0f172a;
  --muted: #64748b;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --success: #10b981;
  --border: #e2e8f0;
  --shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05), 0 8px 10px -6px rgba(0, 0, 0, 0.05);
}

* { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Plus Jakarta Sans', sans-serif; -webkit-tap-highlight-color: transparent; }
body { background: var(--bg); color: var(--text); min-height: 100vh; padding: 20px 16px 40px; }

.view { display: none; animation: fadeIn 0.25s ease-in-out; }
.view.active { display: block; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

.header { text-align: center; margin-bottom: 24px; padding-top: 10px; }
.logo { width: 64px; height: 64px; margin: 0 auto 14px; background: linear-gradient(135deg, #3b82f6, #1d4ed8); border-radius: 18px; display: flex; align-items: center; justify-content: center; color: white; box-shadow: 0 10px 20px rgba(37, 99, 235, 0.25); }
.header h1 { font-size: 22px; font-weight: 800; color: var(--text); margin-bottom: 4px; }
.header p { font-size: 13px; color: var(--muted); font-weight: 500; }

.panel { background: var(--card); border: 1px solid var(--border); border-radius: 20px; padding: 20px; box-shadow: var(--shadow); margin-bottom: 16px; }
.panel label { display: block; font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.panel input { width: 100%; background: #f1f5f9; border: 1px solid var(--border); border-radius: 12px; padding: 14px; color: var(--text); font-size: 15px; font-weight: 600; outline: none; transition: 0.2s; margin-bottom: 16px; }
.panel input:focus { border-color: var(--primary); background: #fff; box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1); }

.btn { width: 100%; padding: 15px; border: none; border-radius: 14px; background: var(--primary); color: white; font-size: 15px; font-weight: 700; cursor: pointer; transition: 0.2s; box-shadow: 0 4px 12px rgba(37, 99, 235, 0.2); }
.btn:active { transform: scale(0.98); background: var(--primary-hover); }
.btn:disabled { opacity: 0.6; pointer-events: none; }

.code-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 12px 16px; margin-bottom: 10px; display: flex; align-items: center; justify-content: space-between; box-shadow: var(--shadow); }
.code-text { font-family: monospace; font-size: 16px; font-weight: 700; color: #059669; letter-spacing: 1px; }
.copy-btn { background: #d1fae5; color: #065f46; border: none; padding: 8px 14px; border-radius: 8px; font-weight: 700; font-size: 12px; cursor: pointer; transition: 0.2s; }
.copy-btn:active { background: #10b981; color: white; }

.back-btn { background: none; border: none; color: var(--primary); font-size: 14px; font-weight: 700; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; margin-bottom: 16px; }
</style>
</head>
<body>

<!-- VIEW 1: LOGIN TELEGRAM -->
<div id="view-login" class="view active">
  <div class="header">
    <div class="logo">⚡</div>
    <h1>Auto Code LC79</h1>
    <p>Nhập thông tin API Telegram để kết nối</p>
  </div>
  <div class="panel">
    <label>API ID</label>
    <input type="number" id="apiId" placeholder="VD: 1234567">
    <label>API HASH</label>
    <input type="password" id="apiHash" placeholder="Nhập API Hash">
    <label>Số điện thoại</label>
    <input type="tel" id="phone" placeholder="+84...">
    <button class="btn" id="btn-send" onclick="sendCode()">Gửi Mã OTP</button>
  </div>
</div>

<!-- VIEW 2: OTP -->
<div id="view-otp" class="view">
  <button class="back-btn" onclick="showView('view-login')">← Quay lại</button>
  <div class="header">
    <h1>Xác Thực OTP</h1>
    <p>Kiểm tra tin nhắn Telegram chính thức</p>
  </div>
  <div class="panel">
    <label>Mã OTP</label>
    <input type="text" id="otpCode" placeholder="Nhập mã OTP..." inputmode="numeric">
    <button class="btn" id="btn-verify" onclick="verifyCode()">Xác Nhận OTP</button>
  </div>
</div>

<!-- VIEW 3: 2FA -->
<div id="view-2fa" class="view">
  <button class="back-btn" onclick="showView('view-otp')">← Quay lại</button>
  <div class="header">
    <h1>Mật khẩu 2FA</h1>
    <p>Tài khoản bảo mật 2 lớp</p>
  </div>
  <div class="panel">
    <label>Mật khẩu</label>
    <input type="password" id="password2fa" placeholder="Mật khẩu 2FA...">
    <button class="btn" id="btn-verify-2fa" onclick="verify2FA()">Đăng Nhập</button>
  </div>
</div>

<!-- VIEW 4: RESULT CODES -->
<div id="view-result" class="view">
  <div class="header" style="margin-bottom: 16px;">
    <h1>10 Mã Code Mới Nhất</h1>
    <p>Tự động cập nhật tức thì (0.1s)</p>
  </div>
  <div id="codes-container">
    <div style="text-align: center; color: var(--muted); padding: 30px;">Đang quét mã code...</div>
  </div>
  <button class="btn" style="background: #e2e8f0; color: var(--text); margin-top: 10px;" onclick="fetchCodes()">Làm Mới Lại</button>
</div>

<script>
const tg = window.Telegram?.WebApp;
if(tg) { tg.ready(); tg.expand(); }

let authData = { api_id: '', api_hash: '', phone: '', phone_code_hash: '', session: '' };

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

async function sendCode() {
  const api_id = document.getElementById('apiId').value.trim();
  const api_hash = document.getElementById('apiHash').value.trim();
  const phone = document.getElementById('phone').value.trim();
  if(!api_id || !api_hash || !phone) { alert("Vui lòng nhập đủ thông tin!"); return; }

  const btn = document.getElementById('btn-send');
  btn.innerText = "Đang gửi..."; btn.disabled = true;
  try {
    const res = await fetch('/api/send_code', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({api_id, api_hash, phone}) });
    const data = await res.json();
    if(data.status === 'ok') {
      authData = {...authData, api_id, api_hash, phone, phone_code_hash: data.phone_code_hash, session: data.session};
      showView('view-otp');
    } else { alert(data.message); }
  } finally { btn.innerText = "Gửi Mã OTP"; btn.disabled = false; }
}

async function verifyCode() {
  const code = document.getElementById('otpCode').value.trim();
  if(!code) return;
  const res = await fetch('/api/verify_code', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...authData, code}) });
  const data = await res.json();
  if(data.status === 'ok') { authData.session = data.session; fetchCodes(); }
  else if(data.status === '2fa') { authData.session = data.session; showView('view-2fa'); }
  else { alert(data.message); }
}

async function verify2FA() {
  const password = document.getElementById('password2fa').value.trim();
  if(!password) return;
  const res = await fetch('/api/verify_2fa', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...authData, password}) });
  const data = await res.json();
  if(data.status === 'ok') { authData.session = data.session; fetchCodes(); }
  else { alert(data.message); }
}

async function fetchCodes() {
  showView('view-result');
  const container = document.getElementById('codes-container');
  container.innerHTML = '<div style="text-align: center; color: var(--muted); padding: 30px;">Đang tải danh sách code...</div>';
  
  const res = await fetch('/api/get_codes', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(authData) });
  const data = await res.json();
  
  if(data.status === 'ok' && data.codes.length > 0) {
    container.innerHTML = '';
    data.codes.forEach((code, idx) => {
      container.innerHTML += `<div class="code-card"><span class="code-text">${idx + 1}. ${code}</span><button class="copy-btn" onclick="copy('${code}')">COPY</button></div>`;
    });
  } else {
    container.innerHTML = `<div style="text-align: center; color: #ef4444; padding: 20px;">${data.message || 'Không tìm thấy code.'}</div>`;
  }
}

function copy(text) {
  navigator.clipboard.writeText(text);
  if(tg) tg.HapticFeedback?.notificationOccurred('success');
  alert("Đã copy: " + text);
}
</script>
</body>
</html>
"""

# ================================================================
#                    FLASK ROUTES & TELEGRAM BOT
# ================================================================
@flask_app.route("/")
def index():
    return Response(MINIAPP_HTML, mimetype="text/html")

@flask_app.route("/health")
def health():
    return Response("ok", mimetype="text/plain")

def keep_alive():
    if not WEBAPP_URL:
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


START_MESSAGE = """
💎 <b>AUTO CODE CENTER — VIP</b> 💎

👑 Chào <b>{name}</b>, hệ thống quét mã tự động tốc độ cao đã sẵn sàng!
⚡ Lấy code và Auto Nạp siêu tốc trong vòng 0.1s.

👇 <b>Bấm nút bên dưới để mở Mini App:</b>
"""

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🚀 MỞ MINI APP VIP", web_app=WebAppInfo(url=WEBAPP_URL or "https://example.com"))]
])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "bạn"
    await update.message.reply_text(START_MESSAGE.format(name=name), parse_mode="HTML", reply_markup=KEYBOARD)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        await asyncio.sleep(10)

def main():
    if not BOT_TOKEN:
        logger.error("Chưa cấu hình BOT_TOKEN!")
        return

    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    # Xóa Webhook cũ để tránh lỗi Conflict polling
    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
    except Exception:
        pass

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_error_handler(error_handler)
    
    logger.info("Bot Telegram & Flask Server đã khởi động thành công!")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
