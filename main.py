import os
import time
import threading
import logging
import requests
import re
import asyncio
from flask import Flask, Response, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import Conflict

# Telethon Imports
from telethon import TelegramClient, functions
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

    result = asyncio.run(_send())
    return jsonify(result)

@flask_app.route("/api/verify_code", methods=["POST"])
def api_verify_code():
    data = request.json
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    phone = data.get("phone")
    code = data.get("code")
    phone_code_hash = data.get("phone_code_hash")
    session_str = data.get("session")

    async def _verify():
        try:
            client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
            await client.connect()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                new_session = client.session.save()
                await client.disconnect()
                return {"status": "2fa", "session": new_session, "message": "Vui lòng nhập mật khẩu 2FA."}
            except Exception as e:
                await client.disconnect()
                return {"status": "error", "message": "Mã OTP không đúng hoặc đã hết hạn."}
            
            new_session = client.session.save()
            await client.disconnect()
            return {"status": "ok", "session": new_session}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    result = asyncio.run(_verify())
    return jsonify(result)

@flask_app.route("/api/verify_2fa", methods=["POST"])
def api_verify_2fa():
    data = request.json
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    password = data.get("password")
    session_str = data.get("session")

    async def _verify_2fa():
        try:
            client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
            await client.connect()
            try:
                await client.sign_in(password=password)
            except Exception as e:
                await client.disconnect()
                return {"status": "error", "message": "Mật khẩu 2FA không chính xác."}
            
            new_session = client.session.save()
            await client.disconnect()
            return {"status": "ok", "session": new_session}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    result = asyncio.run(_verify_2fa())
    return jsonify(result)

@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    data = request.json
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    session_str = data.get("session")

    async def _get_codes():
        client = None
        try:
            client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
            await client.connect()

            channel_entity = None

            # 1. Quét qua danh sách Dialogs (Cuộc trò chuyện) để tìm kênh
            try:
                async for dialog in client.iter_dialogs(limit=200):
                    if dialog.id == -1002237214360 or (dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame'):
                        channel_entity = dialog.entity
                        break
            except Exception as e:
                logger.warning(f"Lỗi quét dialogs: {e}")

            # 2. Nếu chưa có, thử get_entity bằng username
            if not channel_entity:
                try:
                    channel_entity = await client.get_entity('codelaucuagame')
                except Exception:
                    pass

            # 3. Thử get_entity bằng link
            if not channel_entity:
                try:
                    channel_entity = await client.get_entity('https://t.me/codelaucuagame')
                except Exception:
                    pass

            # 4. Thử get_entity bằng ID
            if not channel_entity:
                try:
                    channel_entity = await client.get_entity(-1002237214360)
                except Exception:
                    pass

            if not channel_entity:
                return {"status": "error", "message": "Không thể tìm thấy kênh. Tài khoản có thể chưa join hoặc kênh bị ẩn."}

            # Thực hiện join nếu chưa tham gia
            try:
                await client(functions.channels.JoinChannelRequest(channel=channel_entity))
                logger.info("Đã join kênh thành công.")
            except Exception as e:
                logger.info(f"Join channel note (có thể đã join rồi): {e}")

            # Lấy 100 tin nhắn gần nhất
            codes = []
            pattern = re.compile(r'LC79[A-Z0-9]+')
            logger.info("Bắt đầu quét tin nhắn tìm code...")

            async for msg in client.iter_messages(channel_entity, limit=100):
                # FIX LỖI TẠI ĐÂY: Dùng getattr để tránh lỗi MessageService object has no attribute 'caption'
                # Telethon dùng .message để lấy text hoặc caption của media an toàn nhất
                raw_text = getattr(msg, 'message', '')
                if not raw_text:
                    raw_text = getattr(msg, 'text', '')
                
                if not raw_text:
                    continue
                    
                matches = pattern.findall(raw_text)
                for match in matches:
                    stripped_code = match[4:].strip()
                    if stripped_code and stripped_code not in codes:
                        codes.append(stripped_code)
                        logger.info(f"Tìm thấy code: {stripped_code}")
                    
                    if len(codes) >= 10:
                        break
                        
                if len(codes) >= 10:
                    break

            if not codes:
                return {"status": "error", "message": "Không tìm thấy code LC79 nào trong 100 tin nhắn gần nhất."}

            return {"status": "ok", "codes": codes}
            
        except Exception as e:
            logger.error(f"Lỗi get_codes: {e}")
            return {"status": "error", "message": f"Lỗi: {str(e)}"}
        finally:
            if client and client.is_connected():
                await client.disconnect()

    result = asyncio.run(_get_codes())
    return jsonify(result)


# ================================================================
#                    MINI APP HTML (VIP GLASSMORPHISM)
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#05050a">
<title>Auto Tool Center - VIP</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');

:root{
  --bg: #05050a;
  --card: rgba(20, 24, 39, 0.6);
  --card-hover: rgba(30, 35, 55, 0.8);
  --accent: #8b5cf6;
  --accent-glow: rgba(139, 92, 246, 0.4);
  --accent2: #06b6d4;
  --text: #f8fafc;
  --muted: #94a3b8;
  --border: rgba(255, 255, 255, 0.08);
  --danger: #ef4444;
  --success: #10b981;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html, body { touch-action: pan-x pan-y; height: 100%; }
body{
  font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  padding: 20px 16px 40px; overflow-x: hidden;
  background-image: 
    radial-gradient(circle at 0% 0%, rgba(139, 92, 246, 0.15) 0%, transparent 50%),
    radial-gradient(circle at 100% 100%, rgba(6, 182, 212, 0.15) 0%, transparent 50%);
  background-attachment: fixed;
}
body>*{position:relative; z-index:1}

.icon { width: 24px; height: 24px; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; fill: none; }
.icon-sm { width: 18px; height: 18px; }
.icon-lg { width: 32px; height: 32px; }

.view{display:none; animation:viewIn .4s cubic-bezier(0.16, 1, 0.3, 1)}
.view.active{display:block}
@keyframes viewIn{from{opacity:0;transform:translateY(15px) scale(0.98)}to{opacity:1;transform:none}}

.header{text-align:center;padding:30px 0 30px}
.logo{
  display:inline-flex; align-items:center; justify-content:center;
  width: 80px; height: 80px; border-radius: 24px;
  background: linear-gradient(135deg, rgba(139, 92, 246, 0.2), rgba(6, 182, 212, 0.2));
  border: 1px solid rgba(139, 92, 246, 0.4);
  color: var(--accent); margin-bottom: 20px; 
  box-shadow: 0 10px 40px rgba(139, 92, 246, 0.2), inset 0 0 20px rgba(139, 92, 246, 0.1);
  backdrop-filter: blur(10px);
}
.header h1{font-size: 26px; font-weight: 800; letter-spacing: -0.8px; margin-bottom: 8px;}
.header h1 span { background: linear-gradient(to right, #a78bfa, #22d3ee); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header p{color:var(--muted);font-size:14px; font-weight: 500;}

.section-title{font-size: 13px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 1.5px; margin: 24px 4px 16px; display: flex; align-items: center; gap: 8px;}
.section-title::after { content: ''; flex: 1; height: 1px; background: linear-gradient(90deg, var(--border), transparent); }

.grid{display:grid; grid-template-columns:1fr 1fr; gap:14px}
.service{
  background: var(--card); border: 1px solid var(--border); border-radius: 20px;
  padding: 20px 16px; cursor: pointer; transition: all 0.3s ease;
  display: flex; flex-direction: column; gap: 14px; position: relative;
  backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
}
.service:active{transform:scale(0.96); background: var(--card-hover); border-color: rgba(255,255,255,0.15);}
.service .ico{color: var(--accent);}
.service .name{font-size: 15px; font-weight: 700;}
.service .sub{font-size: 12px; color: var(--muted); font-weight: 500;}
.service.full{grid-column:1 / -1; flex-direction:row; align-items:center; gap:16px; padding: 22px 20px;}
.service.full .ico{color: var(--accent2);}
.service.full .info{flex:1; display:flex; flex-direction:column; gap:4px;}
.service.full .arrow{color: var(--muted); transition: 0.3s;}
.service.full:active .arrow{transform: translateX(6px); color: var(--text);}
.service.featured{background: linear-gradient(135deg, rgba(139,92,246,0.1), rgba(6,182,212,0.05)); border-color: rgba(139, 92, 246, 0.3);}
.service .hot{position: absolute; top: 12px; right: 12px; font-size: 9px; font-weight: 800; letter-spacing: 0.5px; padding: 4px 10px; border-radius: 99px; background: linear-gradient(135deg, #ec4899, #8b5cf6); color: #fff; box-shadow: 0 4px 12px rgba(236, 72, 153, 0.4);}

.back-btn{display: inline-flex; align-items: center; gap: 8px; background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); padding: 10px 18px; border-radius: 12px; font-size: 14px; font-weight: 600; cursor: pointer; margin-bottom: 20px; transition: 0.2s;}
.back-btn:active{transform:scale(0.95); background: rgba(255,255,255,0.1);}

.lc-header{background: linear-gradient(135deg, rgba(30, 35, 55, 0.8), rgba(20, 24, 39, 0.6)); border: 1px solid var(--border); border-radius: 24px; padding: 32px 20px; margin-bottom: 20px; text-align: center; backdrop-filter: blur(12px); position: relative; overflow: hidden;}
.lc-header::before { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 2px; background: linear-gradient(90deg, transparent, var(--accent), transparent); }
.lc-icon{display: inline-flex; align-items: center; justify-content: center; width: 64px; height: 64px; border-radius: 20px; background: rgba(139, 92, 246, 0.15); color: var(--accent); margin-bottom: 16px; border: 1px solid rgba(139, 92, 246, 0.3);}
.lc-header h2{font-size: 22px; font-weight: 800; margin-bottom: 8px; letter-spacing: -0.5px;}
.lc-header p{font-size: 13px; color: var(--muted); font-weight: 500;}

.panel{background: var(--card); border: 1px solid var(--border); border-radius: 24px; padding: 24px 20px; backdrop-filter: blur(12px);}
.panel label{font-size: 12px; font-weight: 700; color: var(--muted); display: flex; align-items: center; gap: 6px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px;}
.panel input{width: 100%; background: rgba(0,0,0,0.3); border: 1px solid var(--border); border-radius: 14px; padding: 16px; color: var(--text); font-size: 15px; font-weight: 500; outline: none; transition: 0.3s; margin-bottom: 20px;}
.panel input:focus{border-color: var(--accent); box-shadow: 0 0 0 4px rgba(139, 92, 246, 0.15); background: rgba(0,0,0,0.5);}
.panel input::placeholder { color: rgba(148, 163, 184, 0.5); }

.btn{width: 100%; padding: 16px; border: none; border-radius: 14px; font-size: 16px; font-weight: 700; cursor: pointer; transition: 0.2s; display: flex; align-items: center; justify-content: center; gap: 8px; color: #fff; background: linear-gradient(135deg, #8b5cf6, #6366f1); box-shadow: 0 8px 24px var(--accent-glow);}
.btn:active{transform: scale(0.97); box-shadow: none;}
.btn:disabled {opacity: 0.6; pointer-events: none;}

.code-box { background: rgba(0,0,0,0.3); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; font-family: 'Courier New', monospace; font-size: 16px; color: #34d399; font-weight: 800; letter-spacing: 1.5px;}
.copy-btn { background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); color: #34d399; padding: 8px 14px; border-radius: 8px; cursor: pointer; font-weight: 700; font-size: 12px; transition: 0.2s;}
.copy-btn:active { background: #10b981; color: #000; }

.footer{text-align: center; color: var(--muted); font-size: 12px; margin-top: 40px; font-weight: 500;}
</style>
</head>
<body>

<!-- =================== VIEW 1: HOME =================== -->
<div id="view-home" class="view active">
  <div class="header">
    <div class="logo"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg></div>
    <h1>Auto Tool <span>VIP</span></h1>
    <p>Hệ thống tự động hóa thế hệ mới</p>
  </div>
  <div class="section-title">Dịch vụ cao cấp</div>
  <div class="grid">
    <div class="service featured full" onclick="openLogin()">
      <div class="hot">HOT VIP</div>
      <div class="ico"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="2" y="6" width="20" height="12" rx="2"/><line x1="6" y1="12" x2="10" y2="12"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="15" y1="13" x2="15.01" y2="13"/><line x1="18" y1="11" x2="18.01" y2="11"/></svg></div>
      <div class="info">
        <div class="name">Tool Auto LC79</div>
        <div class="sub">Tự động lấy code từ kênh Telegram</div>
      </div>
      <div class="arrow"><svg class="icon" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg></div>
    </div>
  </div>
</div>

<!-- =================== VIEW 2: LOGIN (API) =================== -->
<div id="view-login" class="view">
  <button class="back-btn" onclick="goHome()"><svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg> Quay lại</button>
  <div class="lc-header">
    <div class="lc-icon"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
    <h2>Xác Thực API</h2>
    <p>Kết nối Telegram để sử dụng Tool</p>
  </div>
  <div class="panel">
    <label>API ID</label>
    <input type="number" id="apiId" placeholder="VD: 12345678" autocomplete="off">
    
    <label>API HASH</label>
    <input type="password" id="apiHash" placeholder="Nhập API HASH" autocomplete="off">
    
    <label>Số điện thoại</label>
    <input type="tel" id="phone" placeholder="VD: +84987654321" autocomplete="off">
    
    <button class="btn" id="btn-send-code" onclick="sendCode()">Tiếp Tục <svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg></button>
  </div>
</div>

<!-- =================== VIEW 3: OTP VERIFY =================== -->
<div id="view-otp" class="view">
  <button class="back-btn" onclick="showView('view-login')"><svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg> Quay lại</button>
  <div class="lc-header">
    <div class="lc-icon" style="color: var(--accent2); background: rgba(6,182,212,0.1); border-color: rgba(6,182,212,0.3);"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg></div>
    <h2>Nhập Mã OTP</h2>
    <p>Mã xác thực đã được gửi tới Telegram</p>
  </div>
  <div class="panel">
    <label>Mã OTP</label>
    <input type="text" id="otpCode" placeholder="Nhập 5 chữ số" inputmode="numeric" autocomplete="one-time-code">
    <button class="btn" id="btn-verify" onclick="verifyCode()">Xác Nhận OTP</button>
  </div>
</div>

<!-- =================== VIEW 4: 2FA PASSWORD =================== -->
<div id="view-2fa" class="view">
  <button class="back-btn" onclick="showView('view-otp')"><svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg> Quay lại</button>
  <div class="lc-header" style="border-color: rgba(239, 68, 68, 0.3);">
    <div class="lc-icon" style="color: var(--danger); background: rgba(239, 68, 68, 0.1); border-color: rgba(239, 68, 68, 0.3);"><svg class="icon icon-lg" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div>
    <h2>Xác Thực 2 Lớp</h2>
    <p>Tài khoản của bạn được bảo vệ bằng 2FA</p>
  </div>
  <div class="panel">
    <label>Mật khẩu 2FA</label>
    <input type="password" id="password2fa" placeholder="Nhập mật khẩu 2FA của bạn" autocomplete="off">
    <button class="btn" id="btn-verify-2fa" onclick="verify2FA()">Đăng Nhập Ngay</button>
  </div>
</div>

<!-- =================== VIEW 5: RESULT CODES =================== -->
<div id="view-result" class="view">
  <div class="lc-header" style="padding: 24px; border-color: rgba(16, 185, 129, 0.3);">
    <div class="lc-icon" style="background: rgba(16, 185, 129, 0.1); color: var(--success); border-color: rgba(16, 185, 129, 0.3);"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div>
    <h2>Đăng Nhập VIP</h2>
    <p>10 Mã Code mới nhất từ kênh LC79</p>
  </div>
  
  <div class="panel" id="codes-container" style="padding: 16px;">
    <!-- Codes will be injected here -->
  </div>
  
  <button class="btn" style="margin-top: 20px; background: rgba(255,255,255,0.05); border: 1px solid var(--border); box-shadow: none;" onclick="goHome()">Về Trang Chủ</button>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

let authData = { api_id: '', api_hash: '', phone: '', phone_code_hash: '', session: '', password: '' };

function showView(id) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  window.scrollTo({top:0, behavior:'smooth'});
}

function openLogin() { showView('view-login'); }
function goHome() { showView('view-home'); }

async function sendCode() {
  const api_id = document.getElementById('apiId').value.trim();
  const api_hash = document.getElementById('apiHash').value.trim();
  const phone = document.getElementById('phone').value.trim();

  if (!api_id || !api_hash || !phone) {
    alert("Vui lòng nhập đầy đủ thông tin!");
    return;
  }

  const btn = document.getElementById('btn-send-code');
  btn.innerText = "Đang gửi OTP..."; btn.disabled = true;

  try {
    const res = await fetch('/api/send_code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_id, api_hash, phone })
    });
    const data = await res.json();

    if (data.status === 'ok') {
      authData = { ...authData, api_id, api_hash, phone, phone_code_hash: data.phone_code_hash, session: data.session };
      showView('view-otp');
    } else {
      alert("Lỗi: " + data.message);
    }
  } catch (e) {
    alert("Lỗi kết nối Server!");
  } finally {
    btn.innerText = "Tiếp Tục"; btn.disabled = false;
  }
}

async function verifyCode() {
  const code = document.getElementById('otpCode').value.trim();
  if (!code) { alert("Vui lòng nhập OTP!"); return; }

  const btn = document.getElementById('btn-verify');
  btn.innerText = "Đang xác thực..."; btn.disabled = true;

  try {
    const res = await fetch('/api/verify_code', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...authData, code })
    });
    const data = await res.json();

    if (data.status === 'ok') {
      authData.session = data.session;
      await fetchCodes(); 
    } else if (data.status === '2fa') {
      authData.session = data.session;
      showView('view-2fa');
    } else {
      alert("Lỗi: " + data.message);
      btn.innerText = "Xác Nhận"; btn.disabled = false;
    }
  } catch (e) {
    alert("Lỗi xác thực!");
    btn.innerText = "Xác Nhận"; btn.disabled = false;
  }
}

async function verify2FA() {
  const password = document.getElementById('password2fa').value.trim();
  if (!password) { alert("Vui lòng nhập mật khẩu 2FA!"); return; }

  const btn = document.getElementById('btn-verify-2fa');
  btn.innerText = "Đang xác thực..."; btn.disabled = true;

  try {
    const res = await fetch('/api/verify_2fa', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...authData, password })
    });
    const data = await res.json();

    if (data.status === 'ok') {
      authData.session = data.session;
      await fetchCodes();
    } else {
      alert("Lỗi: " + data.message);
      btn.innerText = "Đăng Nhập Ngay"; btn.disabled = false;
    }
  } catch (e) {
    alert("Lỗi xác thực 2FA!");
    btn.innerText = "Đăng Nhập Ngay"; btn.disabled = false;
  }
}

async function fetchCodes() {
  try {
    showView('view-result');
    const container = document.getElementById('codes-container');
    container.innerHTML = '<div style="text-align:center; color:var(--muted); padding: 20px;">Đang quét kênh và lấy code...</div>';

    const res = await fetch('/api/get_codes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(authData)
    });
    const data = await res.json();

    if (data.status === 'ok') {
      container.innerHTML = '';
      if (data.codes.length === 0) {
        container.innerHTML = '<div style="text-align:center; color:var(--muted); padding: 20px;">Không tìm thấy code nào mới.</div>';
      } else {
        data.codes.forEach((code, index) => {
          const div = document.createElement('div');
          div.className = 'code-box';
          div.innerHTML = `<span>${index + 1}. ${code}</span> <button class="copy-btn" onclick="copyText('${code}')">COPY</button>`;
          container.appendChild(div);
        });
      }
    } else {
      container.innerHTML = `<div style="color: var(--danger); text-align:center; padding: 20px;">Lỗi: ${data.message}</div>`;
    }
  } catch (e) {
    document.getElementById('codes-container').innerHTML = `<div style="color: var(--danger); text-align:center; padding: 20px;">Lỗi kết nối!</div>`;
  }
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    if (tg) tg.HapticFeedback?.notificationOccurred('success');
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:30px;left:50%;transform:translateX(-50%);background:linear-gradient(135deg, #10b981, #059669);color:white;padding:12px 24px;border-radius:12px;font-size:14px;font-weight:700;z-index:9999;transition:0.3s;box-shadow:0 10px 20px rgba(16,185,129,0.3)';
    t.innerText = "Đã copy: " + text;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(-50%) translateY(20px)'; setTimeout(() => t.remove(), 300); }, 2000);
  });
}
</script>
</body>
</html>
"""

# ================================================================
#                    FLASK ROUTES
# ================================================================
@flask_app.route("/")
def index():
    return Response(MINIAPP_HTML, mimetype="text/html")

@flask_app.route("/health")
def health():
    return Response("ok", mimetype="text/plain")

# ================================================================
#                    SELF-PING & BOT MAIN
# ================================================================
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

# Bot Telegram Premium Emojis (VIP Layout)
START_MESSAGE = """
<tg-emoji emoji-id="5368324170671202286">💎</tg-emoji> <b>AUTO TOOL CENTER — VIP</b> <tg-emoji emoji-id="5368324170671202286">💎</tg-emoji>

<tg-emoji emoji-id="5447644880824181073">👑</tg-emoji> <b>Chào mừng</b> <i>{name}</i> đến với hệ thống!
<tg-emoji emoji-id="4960917714325121485">🔥</tg-emoji> Trải nghiệm tự động hóa đỉnh cao thế hệ mới.

━━━━━━━━━━━━━━━━━━━━
<tg-emoji emoji-id="6235401323201735695">⚙️</tg-emoji> <b>TÍNH NĂNG VIP</b>
<tg-emoji emoji-id="5276398495459370121">⚡</tg-emoji> Đăng nhập Telegram cá nhân an toàn.
<tg-emoji emoji-id="5386714776240519929">🔐</tg-emoji> Hỗ trợ xác thực 2 lớp (2FA).
<tg-emoji emoji-id="5368324170671202286">🎮</tg-emoji> Auto Join kênh & Lấy 10 code mới nhất.
<tg-emoji emoji-id="5278578979322940035">🛡️</tg-emoji> Bảo mật 100% — Không lưu trữ dữ liệu.

━━━━━━━━━━━━━━━━━━━━
🎯 <b>Bấm nút bên dưới để trải nghiệm ngay</b> 👇
"""

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "🚀 MỞ MINI APP VIP",
        web_app=WebAppInfo(url=WEBAPP_URL or "https://example.com")
    )],
])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "bạn"
    await update.message.reply_text(
        START_MESSAGE.format(name=name),
        parse_mode="HTML",
        reply_markup=KEYBOARD,
        disable_web_page_preview=True,
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Xử lý lỗi Conflict không làm sập bot"""
    if isinstance(context.error, Conflict):
        logger.warning("⚠️ Phát hiện xung đột Bot (Conflict). Có thể có instance khác đang chạy. Đang chờ 10s...")
        await asyncio.sleep(10)
    else:
        logger.error(f"Lỗi không xác định: {context.error}")

async def post_init(application: Application):
    """Thiết lập Menu lệnh cho Bot"""
    await application.bot.set_my_commands([
        BotCommand("start", "Khởi động Bot và mở Mini App")
    ])

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN chưa set!")
        return

    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("Đang khởi động Bot VIP...")
    
    # Xóa Webhook cũ (nếu có) trước khi chạy Polling để tránh lỗi Conflict
    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=5)
        logger.info("Đã dọn dẹp Webhook cũ.")
    except Exception as e:
        logger.warning(f"Không thể dọn webhook: {e}")

    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot_app.add_handler(CommandHandler("start", start))
    
    # Thêm Error Handler để tránh crash bot khi có Conflict
    bot_app.add_error_handler(error_handler)
    
    logger.info("Bot đang chạy polling...")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()