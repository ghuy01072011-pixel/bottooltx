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
from flask import Flask, Response, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

# Telethon Imports
from telethon import TelegramClient, events, functions
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
logger = logging.getLogger("AutoTool")

flask_app = Flask(__name__)

# ============ LC79 CONSTANTS ============
API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"
BASE_URL = "https://gameapi.tele68.com"
PARAMS = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 45

# ============ GLOBAL STATE ============
STATE = {
    "codes": [],
    "lc79_account": None,
    "listener_running": False,
    "user_chat_id": None
}

def notify_user(text):
    if not STATE["user_chat_id"]: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": STATE["user_chat_id"], "text": text, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        logger.error(f"Lỗi gửi thông báo: {e}")

# ================================================================
#                    LC79 REDEMPTION LOGIC
# ================================================================
class Account:
    def __init__(self, un, pw, nick):
        self.un = un
        self.pw = pw 
        self.nick = nick
        self.used_codes = set()
        self.lock = threading.Lock()
        self.jwt = None
        self.jwt_exp = 0

    def _headers(self, auth=None):
        h = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "origin": "https://lc79.to",
            "referer": "https://lc79.to/",
            "accept": "application/json, text/plain, */*",
        }
        if auth: h["authorization"] = f"Bearer {auth}"
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
                }, headers=self._headers(), timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            data = res.json()
            logger.info(f"[BƯỚC 1] Response: {data}")
            
            if data.get("success"):
                return data.get("accessToken") or data.get("sessionKey")
            else:
                logger.warning(f"[BƯỚC 1] Lỗi API: {data}")
                return None
        except Exception as e:
            logger.error(f"[BƯỚC 1] Lỗi kết nối: {e}")
            return None

    def refresh_token(self):
        access_token = self.get_access_token()
        if not access_token:
            return False, "Sai tài khoản hoặc mật khẩu (Bước 1 thất bại)."

        try:
            login_headers = self._headers()
            login_headers["origin"] = "https://lc79b.bet"
            login_headers["referer"] = "https://lc79b.bet/"
            
            with requests.Session() as s:
                res = s.post(LOGIN_URL, headers=login_headers, json={
                    "nickName": self.nick, 
                    "accessToken": access_token
                }, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            data = res.json()
            logger.info(f"[BƯỚC 2] Response: {data}")
            
            new_token = data.get("accessToken") or data.get("sessionKey") or data.get("token")
            if new_token:
                self.jwt = new_token
                self.jwt_exp = self.parse_jwt_exp(new_token)
                if self.jwt_exp == 0:
                    self.jwt_exp = time.time() + 3600
                return True, "OK"
            else:
                msg = data.get("message") or data.get("msg") or "Lỗi không xác định từ server LC79."
                return False, msg
        except Exception as e:
            logger.error(f"[BƯỚC 2] Lỗi kết nối: {e}")
            return False, "Lỗi kết nối đến server LC79."

    def redeem_code(self, code, _retry=False, _attempt=0):
        with self.lock:
            if code in self.used_codes:
                return False, "Đã sử dụng"
            self.used_codes.add(code)

        if not self.is_token_valid():
            success, msg = self.refresh_token()
            if not success:
                return False, f"Lỗi Token: {msg}"

        try:
            with requests.Session() as s:
                res = s.post(BASE_URL + PARAMS, headers=self._headers(self.jwt),
                             json={"code": code.strip(), "captcha": ""}, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            try: data = res.json()
            except: data = {}

            if res.status_code == 401:
                success, _ = self.refresh_token()
                if success:
                    with self.lock: self.used_codes.discard(code)
                    return self.redeem_code(code, _retry=True)
                return False, "Lỗi xác thực"

            if res.status_code in [200, 201]:
                amount = data.get("money") or data.get("amount") or data.get("reward") or (data.get("data") or {}).get("money")
                return True, f"+{int(amount):,}k" if amount else "Thành công"
            elif res.status_code == 400:
                msg = data.get("message") if isinstance(data, dict) else "Thất bại"
                return False, msg
            elif res.status_code == 428:
                return False, "Yêu cầu CAPTCHA"
            elif res.status_code == 429:
                return False, "Quá giới hạn"
            else:
                return False, f"Lỗi {res.status_code}"
        except Exception:
            if _attempt < 2:
                return self.redeem_code(code, _retry=_retry, _attempt=_attempt+1)
            return False, "Lỗi mạng"

def process_new_code(code):
    for c in STATE["codes"]:
        if c["code"] == code: return
            
    logger.info(f"🔥 PHÁT HIỆN CODE MỚI: {code}")
    new_item = {"code": code, "status": "Pending", "msg": "Đang chờ..."}
    STATE["codes"].insert(0, new_item)
    if len(STATE["codes"]) > 10: STATE["codes"] = STATE["codes"][:10]
        
    if STATE["lc79_account"]:
        threading.Thread(target=redeem_thread, args=(new_item, STATE["lc79_account"])).start()
    else:
        notify_user(f"🔔 <b>Phát hiện code mới:</b> <code>{code}</code>\nVui lòng mở Mini App và đăng nhập LC79!")

def redeem_thread(item, acc):
    item["status"] = "Processing"
    item["msg"] = "Đang nhập..."
    success, msg = acc.redeem_code(item["code"])
    
    if success:
        item["status"] = "Success"
        item["msg"] = msg
        notify_user(f"✅ <b>NHẬP CODE THÀNH CÔNG</b>\n\n🎁 Mã: <code>{item['code']}</code>\n💰 Nhận được: <b>{msg}</b>")
    else:
        item["status"] = "Failed"
        item["msg"] = msg
        notify_user(f"❌ <b>NHẬP CODE THẤT BẠI</b>\n\n🎁 Mã: <code>{item['code']}</code>\n⚠️ Lý do: {msg}")

# ================================================================
#                    TELEGRAM BACKGROUND LISTENER
# ================================================================
def start_background_listener(session_str, api_id, api_hash):
    if STATE["listener_running"]: return
    STATE["listener_running"] = True
    
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash, loop=loop, connection_retries=None)
        
        async def main():
            await client.connect()
            try:
                channel = await client.get_entity('codelaucuagame')
                await client(functions.channels.JoinChannelRequest(channel=channel))
            except Exception as e: logger.warning(f"Join channel note: {e}")

            @client.on(events.NewMessage(chats='codelaucuagame'))
            async def handler(event):
                text = event.raw_text or ""
                match = re.search(r"LC79([A-Z0-9]+)", text)
                if match: process_new_code(match.group(1))

            await client.run_until_disconnected()

        loop.run_until_complete(main())

    threading.Thread(target=run, daemon=True).start()

# ================================================================
#                    API ENDPOINTS
# ================================================================
@flask_app.route("/api/send_code", methods=["POST"])
def api_send_code():
    data = request.json
    async def _send():
        try:
            client = TelegramClient(StringSession(), int(data.get("api_id")), data.get("api_hash"))
            await client.connect()
            sent = await client.send_code_request(data.get("phone"))
            session_str = client.session.save()
            await client.disconnect()
            return {"status": "ok", "phone_code_hash": sent.phone_code_hash, "session": session_str}
        except Exception as e: return {"status": "error", "message": str(e)}
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
                return {"status": "2fa", "session": new_session, "message": "Nhập mật khẩu 2FA."}
            except Exception:
                await client.disconnect()
                return {"status": "error", "message": "Mã OTP sai hoặc hết hạn."}
            
            new_session = client.session.save()
            await client.disconnect()
            start_background_listener(new_session, data.get("api_id"), data.get("api_hash"))
            return {"status": "ok", "session": new_session}
        except Exception as e: return {"status": "error", "message": str(e)}
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
                return {"status": "error", "message": "Mật khẩu 2FA sai."}
            
            new_session = client.session.save()
            await client.disconnect()
            start_background_listener(new_session, data.get("api_id"), data.get("api_hash"))
            return {"status": "ok", "session": new_session}
        except Exception as e: return {"status": "error", "message": str(e)}
    return jsonify(asyncio.run(_verify_2fa()))

@flask_app.route("/api/login_lc79", methods=["POST"])
def api_login_lc79():
    data = request.json
    un, pw = data.get("username"), data.get("password")
    user_id = data.get("user_id")
    
    if user_id: STATE["user_chat_id"] = user_id
    if not un or not pw: return jsonify({"status": "error", "message": "Thiếu thông tin."})
    
    md5_pw = hashlib.md5(pw.encode()).hexdigest()
    acc = Account(un, md5_pw, un)
    
    success, msg = acc.refresh_token()
    if success:
        STATE["lc79_account"] = acc
        for item in STATE["codes"]:
            if item["status"] == "Pending":
                threading.Thread(target=redeem_thread, args=(item, acc)).start()
        return jsonify({"status": "ok"})
    else:
        return jsonify({"status": "error", "message": msg})

@flask_app.route("/api/start_listener", methods=["POST"])
def api_start_listener():
    data = request.json
    if data.get("user_id"): STATE["user_chat_id"] = data.get("user_id")
    if data.get("session"): start_background_listener(data.get("session"), data.get("api_id"), data.get("api_hash"))
    return jsonify({"status": "ok"})

@flask_app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify({"codes": STATE["codes"], "has_lc79": STATE["lc79_account"] is not None})

# ================================================================
#                    MINI APP HTML
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<title>Auto Tool Center</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f1f5f9;--surface:#fff;--text:#0f172a;--text-muted:#64748b;--primary:#3b82f6;--success:#10b981;--danger:#ef4444;--border:#e2e8f0;}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:20px 16px;display:flex;flex-direction:column;align-items:center;}
.app-container{width:100%;max-width:480px;}.view{display:none;animation:fadeIn .3s ease;}.view.active{display:block;}
@keyframes fadeIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.card{background:var(--surface);border-radius:16px;padding:24px 20px;box-shadow:0 4px 6px -1px rgb(0 0 0 / 0.05);border:1px solid var(--border);margin-bottom:16px;}
.header{text-align:center;margin-bottom:24px;}.header h1{font-size:22px;font-weight:700;margin-bottom:6px;}.header p{color:var(--text-muted);font-size:14px;}
.input-group{margin-bottom:16px;}.input-group label{display:block;font-size:13px;font-weight:600;margin-bottom:6px;}
.input-group input{width:100%;padding:14px 16px;border-radius:12px;border:1px solid var(--border);font-size:15px;outline:none;transition:.2s;background:#f8fafc;}
.input-group input:focus{border-color:var(--primary);background:#fff;box-shadow:0 0 0 3px rgba(59,130,246,0.1);}
.btn{width:100%;padding:16px;border:none;border-radius:12px;background:var(--primary);color:#fff;font-size:16px;font-weight:600;cursor:pointer;transition:.2s;display:flex;justify-content:center;align-items:center;margin-top:8px;}
.btn:active{transform:scale(0.98);}.btn:disabled{opacity:0.6;pointer-events:none;}
.code-list{display:flex;flex-direction:column;gap:10px;margin-top:16px;}
.code-item{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-radius:12px;border:1px solid var(--border);background:#f8fafc;animation:slideIn 0.2s ease;}
@keyframes slideIn{from{opacity:0;transform:translateY(-5px);}to{opacity:1;transform:none;}}
.code-left{display:flex;flex-direction:column;gap:4px;}.code-text{font-family:monospace;font-size:16px;font-weight:700;color:var(--text);}.code-msg{font-size:12px;color:var(--text-muted);font-weight:500;}
.code-status{font-size:11px;font-weight:700;padding:4px 10px;border-radius:99px;}
.status-Pending{background:#fef3c7;color:#d97706;}.status-Processing{background:#e0f2fe;color:#0284c7;}.status-Success{background:#d1fae5;color:#059669;}.status-Failed{background:#fee2e2;color:#dc2626;}
.back-btn{background:none;border:none;color:var(--text-muted);font-size:14px;font-weight:500;cursor:pointer;margin-bottom:16px;display:inline-flex;align-items:center;gap:4px;}
</style>
</head>
<body>
<div class="app-container">
  <div id="view-tg-login" class="view">
    <div class="header"><h1>Kết Nối Telegram</h1><p>Nhập API để bắt đầu lấy code</p></div>
    <div class="card">
      <div class="input-group"><label>API ID</label><input type="number" id="apiId" placeholder="VD: 12345678"></div>
      <div class="input-group"><label>API HASH</label><input type="password" id="apiHash" placeholder="Nhập API HASH"></div>
      <div class="input-group"><label>Số điện thoại</label><input type="tel" id="phone" placeholder="VD: +84987654321"></div>
      <button class="btn" id="btn-send-code" onclick="sendCode()">Tiếp Tục</button>
    </div>
  </div>
  <div id="view-tg-otp" class="view">
    <button class="back-btn" onclick="showView('view-tg-login')">← Quay lại</button>
    <div class="header"><h1>Nhập Mã OTP</h1><p>Kiểm tra tin nhắn Telegram của bạn</p></div>
    <div class="card">
      <div class="input-group"><label>Mã OTP</label><input type="text" id="otpCode" placeholder="Nhập 5 chữ số" inputmode="numeric"></div>
      <button class="btn" id="btn-verify" onclick="verifyCode()">Xác Nhận</button>
    </div>
  </div>
  <div id="view-tg-2fa" class="view">
    <div class="header"><h1>Xác Thực 2 Lớp</h1><p>Tài khoản của bạn có bật 2FA</p></div>
    <div class="card">
      <div class="input-group"><label>Mật khẩu 2FA</label><input type="password" id="password2fa" placeholder="Nhập mật khẩu 2FA"></div>
      <button class="btn" id="btn-verify-2fa" onclick="verify2FA()">Đăng Nhập</button>
    </div>
  </div>
  <div id="view-lc79-login" class="view">
    <div class="header"><h1>Đăng Nhập LC79</h1><p>Tool sẽ tự động nhập code khi có mã mới</p></div>
    <div class="card">
      <div class="input-group"><label>Tài khoản LC79</label><input type="text" id="lc79User" placeholder="Nhập tên đăng nhập"></div>
      <div class="input-group"><label>Mật khẩu LC79</label><input type="password" id="lc79Pass" placeholder="Nhập mật khẩu (chưa mã hoá)"></div>
      <button class="btn" id="btn-login-lc79" onclick="loginLC79()">Kết Nối & Chạy Auto</button>
    </div>
  </div>
  <div id="view-dashboard" class="view">
    <div class="header"><h1>Hệ Thống Đang Chạy</h1><p>Đang chờ code mới từ kênh...</p></div>
    <div class="card" style="padding:16px;"><div class="code-list" id="code-list"></div></div>
  </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }
const tgUserId = tg?.initDataUnsafe?.user?.id;
let authData = { api_id: '', api_hash: '', phone: '', phone_code_hash: '', session: '' };
let pollInterval = null;

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
}

async function sendCode() {
  const api_id = document.getElementById('apiId').value.trim();
  const api_hash = document.getElementById('apiHash').value.trim();
  const phone = document.getElementById('phone').value.trim();
  if (!api_id || !api_hash || !phone) return alert("Nhập đủ thông tin!");
  const btn = document.getElementById('btn-send-code');
  btn.innerText = "Đang gửi..."; btn.disabled = true;
  try {
    const res = await fetch('/api/send_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ api_id, api_hash, phone }) });
    const data = await res.json();
    if (data.status === 'ok') {
      authData = { ...authData, api_id, api_hash, phone, phone_code_hash: data.phone_code_hash, session: data.session };
      showView('view-tg-otp');
    } else alert("Lỗi: " + data.message);
  } catch (e) { alert("Lỗi mạng!"); } finally { btn.innerText = "Tiếp Tục"; btn.disabled = false; }
}

async function verifyCode() {
  const code = document.getElementById('otpCode').value.trim();
  if (!code) return alert("Nhập OTP!");
  const btn = document.getElementById('btn-verify');
  btn.innerText = "Đang xác thực..."; btn.disabled = true;
  try {
    const res = await fetch('/api/verify_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...authData, code }) });
    const data = await res.json();
    if (data.status === 'ok') saveSessionAndStart(data.session);
    else if (data.status === '2fa') { authData.session = data.session; showView('view-tg-2fa'); } 
    else alert("Lỗi: " + data.message);
  } catch (e) { alert("Lỗi mạng!"); } finally { btn.innerText = "Xác Nhận"; btn.disabled = false; }
}

async function verify2FA() {
  const password = document.getElementById('password2fa').value.trim();
  if (!password) return alert("Nhập mật khẩu 2FA!");
  const btn = document.getElementById('btn-verify-2fa');
  btn.innerText = "Đang xác thực..."; btn.disabled = true;
  try {
    const res = await fetch('/api/verify_2fa', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...authData, password }) });
    const data = await res.json();
    if (data.status === 'ok') saveSessionAndStart(data.session);
    else alert("Lỗi: " + data.message);
  } catch (e) { alert("Lỗi mạng!"); } finally { btn.innerText = "Đăng Nhập"; btn.disabled = false; }
}

function saveSessionAndStart(session) {
  authData.session = session;
  localStorage.setItem('tg_session', session);
  localStorage.setItem('api_id', authData.api_id);
  localStorage.setItem('api_hash', authData.api_hash);
  fetch('/api/start_listener', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session, api_id: authData.api_id, api_hash: authData.api_hash, user_id: tgUserId }) });
  showView('view-lc79-login');
}

async function loginLC79() {
  const username = document.getElementById('lc79User').value.trim();
  const password = document.getElementById('lc79Pass').value.trim();
  if (!username || !password) return alert("Nhập đủ tài khoản/mật khẩu!");
  const btn = document.getElementById('btn-login-lc79');
  btn.innerText = "Đang kết nối..."; btn.disabled = true;
  try {
    const res = await fetch('/api/login_lc79', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password, user_id: tgUserId }) });
    const data = await res.json();
    if (data.status === 'ok') { showView('view-dashboard'); startPolling(); } 
    else alert("Lỗi đăng nhập: " + data.message);
  } catch (e) { alert("Lỗi mạng!"); } finally { btn.innerText = "Kết Nối & Chạy Auto"; btn.disabled = false; }
}

function startPolling() {
  if (pollInterval) clearInterval(pollInterval);
  pollInterval = setInterval(async () => {
    try { const res = await fetch('/api/state'); const data = await res.json(); renderCodes(data.codes); } catch (e) {}
  }, 100);
}

function renderCodes(codes) {
  const container = document.getElementById('code-list');
  if (!codes || codes.length === 0) { container.innerHTML = '<div style="text-align:center; color:var(--text-muted); padding:20px 0; font-size:14px;">Đang chờ code mới...</div>'; return; }
  let html = '';
  codes.forEach(item => {
    let statusClass = 'status-Pending';
    if (item.status === 'Success') statusClass = 'status-Success';
    if (item.status === 'Failed') statusClass = 'status-Failed';
    if (item.status === 'Processing') statusClass = 'status-Processing';
    html += `<div class="code-item"><div class="code-left"><div class="code-text">${item.code}</div><div class="code-msg">${item.msg || ''}</div></div><div class="code-status ${statusClass}">${item.status}</div></div>`;
  });
  container.innerHTML = html;
}

async function init() {
  const savedSession = localStorage.getItem('tg_session');
  const savedApiId = localStorage.getItem('api_id');
  const savedApiHash = localStorage.getItem('api_hash');
  if (savedSession && savedApiId && savedApiHash) {
    fetch('/api/start_listener', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session: savedSession, api_id: savedApiId, api_hash: savedApiHash, user_id: tgUserId }) });
    try {
      const res = await fetch('/api/state'); const data = await res.json();
      if (data.has_lc79) { showView('view-dashboard'); startPolling(); } 
      else showView('view-lc79-login');
    } catch (e) { showView('view-tg-login'); }
  } else showView('view-tg-login');
}
init();
</script>
</body>
</html>
"""

@flask_app.route("/")
def index(): return Response(MINIAPP_HTML, mimetype="text/html")
@flask_app.route("/health")
def health(): return Response("ok", mimetype="text/plain")

# ================================================================
#                    SELF-PING & BOT MAIN
# ================================================================
def keep_alive():
    if not WEBAPP_URL: return
    url = WEBAPP_URL.rstrip("/") + "/health"
    time.sleep(15)
    while True:
        try: requests.get(url, timeout=10)
        except: pass
        time.sleep(PING_INTERVAL)

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)

START_MESSAGE = """
<tg-emoji emoji-id="5368324170671202286">💎</tg-emoji> <b>AUTO TOOL VIP — LC79</b> <tg-emoji emoji-id="5368324170671202286">💎</tg-emoji>

<tg-emoji emoji-id="5447644880824181073">👑</tg-emoji> <b>Chào mừng</b> <i>{name}</i> đến với hệ thống!
<tg-emoji emoji-id="4960917714325121485">🔥</tg-emoji> Tốc độ ánh sáng — Tự động lấy & nhập code.

━━━━━━━━━━━━━━━━━━━━
<tg-emoji emoji-id="5276398495459370121">⚡</tg-emoji> Quét tin nhắn Real-time (0.1s).
<tg-emoji emoji-id="5386714776240519929">🤖</tg-emoji> Auto nhập code vào LC79.
<tg-emoji emoji-id="5278578979322940035">🛡️</tg-emoji> Bảo mật MD5 — Không lưu mật khẩu.

━━━━━━━━━━━━━━━━━━━━
🎯 <b>Bấm nút bên dưới để trải nghiệm ngay</b> 👇
"""

KEYBOARD = InlineKeyboardMarkup([[InlineKeyboardButton("🚀 MỞ MINI APP VIP", web_app=WebAppInfo(url=WEBAPP_URL or "https://example.com"))]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "bạn"
    await update.message.reply_text(START_MESSAGE.format(name=name), parse_mode="HTML", reply_markup=KEYBOARD, disable_web_page_preview=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        await asyncio.sleep(10)

async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "Khởi động Tool")])

def main():
    if not BOT_TOKEN: return
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()
    try: requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
    except: pass
    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_error_handler(error_handler)
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()