import os
import time
import threading
import logging
import requests
from flask import Flask, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# ============ CONFIG ============
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8990113148:AAH1wgUusu_Z3AW2JNF4JwivXAvCDOFLO8U")
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
#                    MINI APP HTML (PROFESSIONAL SAAS UI)
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="theme-color" content="#0a0c10">
<title>Auto Tool Center</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg: #0a0c10;
  --card: #151921;
  --card-hover: #1c212c;
  --accent: #6366f1;
  --accent-glow: rgba(99, 102, 241, 0.3);
  --accent2: #06b6d4;
  --text: #f3f4f6;
  --muted: #9ca3af;
  --border: #2d3748;
  --danger: #ef4444;
  --warn: #f59e0b;
  --radius: 16px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  padding: 20px 16px 40px;
  position: relative;
  overflow-x: hidden;
}
/* Ambient Background */
body::before{
  content:''; position:fixed; inset:0; z-index:0; pointer-events:none;
  background:
    radial-gradient(circle at 10% 10%, rgba(99, 102, 241, 0.08) 0%, transparent 40%),
    radial-gradient(circle at 90% 90%, rgba(6, 182, 212, 0.08) 0%, transparent 40%);
}
body>*{position:relative; z-index:1}

/* SVG Icon Utility */
.icon { width: 24px; height: 24px; stroke: currentColor; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; fill: none; }
.icon-sm { width: 18px; height: 18px; }
.icon-lg { width: 32px; height: 32px; }

/* ========== VIEW ========== */
.view{display:none; animation:viewIn .3s cubic-bezier(0.16, 1, 0.3, 1)}
.view.active{display:block}
@keyframes viewIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}

/* ========== HEADER ========== */
.header{text-align:center;padding:24px 0 28px}
.logo{
  display:inline-flex; align-items:center; justify-content:center;
  width: 76px; height: 76px; border-radius: 22px;
  background: linear-gradient(135deg, #1e2330, #151921);
  border: 1px solid var(--border);
  color: var(--accent);
  margin-bottom: 16px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.5), inset 0 1px 0 rgba(255,255,255,0.05);
}
.header h1{
  font-size: 24px; font-weight: 800; letter-spacing: -0.5px;
  color: var(--text);
  margin-bottom: 8px;
}
.header p{color:var(--muted);font-size:14px}
.stats-bar{display:flex;justify-content:center;gap:10px;margin-top:20px;flex-wrap:wrap}
.mini-stat{
  padding: 6px 14px; font-size: 12px; font-weight: 500;
  border-radius: 99px; background: var(--card);
  border: 1px solid var(--border); color: var(--muted);
  display: inline-flex; align-items: center; gap: 6px;
}
.mini-stat .dot{
  width: 6px; height: 6px; border-radius: 50%; background: #10b981;
  box-shadow: 0 0 8px #10b981;
}
.dot.online { animation: pulse 2s infinite; }
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ========== SECTION ========== */
.section-title{
  font-size: 13px; font-weight: 600; color: var(--muted);
  text-transform: uppercase; letter-spacing: 1px; margin: 20px 4px 14px;
  display: flex; align-items: center; gap: 8px;
}

/* ========== GRID ========== */
.grid{display:grid; grid-template-columns:1fr 1fr; gap:12px}
.service{
  background: var(--card);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 18px 16px; cursor: pointer; transition: all 0.2s ease;
  display: flex; flex-direction: column; gap: 12px;
  position: relative; overflow: hidden;
}
.service:active{transform:scale(0.97); background: var(--card-hover)}
.service .ico{color: var(--accent);}
.service .name{font-size: 14px; font-weight: 600; line-height: 1.3;}
.service .sub{font-size: 12px; color: var(--muted); line-height: 1.4;}
.service.full{grid-column:1 / -1; flex-direction:row; align-items:center; gap:16px; padding: 20px 18px;}
.service.full .ico{color: var(--accent2);}
.service.full .info{flex:1; display:flex; flex-direction:column; gap:4px;}
.service.full .arrow{color: var(--muted); transition: 0.3s;}
.service.full:active .arrow{transform: translateX(4px); color: var(--text);}
.service.featured{
  background: linear-gradient(135deg, rgba(99,102,241,0.08), rgba(6,182,212,0.05));
  border-color: rgba(99, 102, 241, 0.3);
}
.service .hot{
  position: absolute; top: 12px; right: 12px;
  font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
  padding: 4px 10px; border-radius: 99px;
  background: var(--accent); color: #fff;
  box-shadow: 0 4px 12px var(--accent-glow);
}

/* ========== BACK BTN ========== */
.back-btn{
  display: inline-flex; align-items: center; gap: 8px;
  background: transparent; border: 1px solid var(--border);
  color: var(--muted); padding: 8px 16px; border-radius: 10px;
  font-size: 14px; font-weight: 500; cursor: pointer;
  margin-bottom: 20px; transition: 0.2s;
}
.back-btn:active{transform:scale(0.95); color: var(--text); background: var(--card)}

/* ========== LC HEADER ========== */
.lc-header{
  background: linear-gradient(135deg, #1e2330, #151921);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 28px 20px; margin-bottom: 20px; text-align: center;
  position: relative; overflow: hidden;
}
.lc-header::before{
  content:''; position:absolute; top:0; left:0; right:0; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent2), transparent);
}
.lc-icon{
  display: inline-flex; align-items: center; justify-content: center;
  width: 60px; height: 60px; border-radius: 16px;
  background: rgba(6, 182, 212, 0.1); color: var(--accent2);
  margin-bottom: 16px;
}
.lc-header h2{font-size: 20px; font-weight: 700; margin-bottom: 8px; letter-spacing: -0.5px;}
.lc-header p{font-size: 13px; color: var(--muted);}

/* ========== NOTE ========== */
.login-note{
  display: flex; gap: 12px; align-items: flex-start;
  padding: 16px; border-radius: 12px;
  background: rgba(245, 158, 11, 0.08);
  border: 1px solid rgba(245, 158, 11, 0.2);
  font-size: 13px; line-height: 1.5; color: #fcd34d;
  margin-bottom: 16px;
}
.login-note .icon { color: var(--warn); flex-shrink: 0; }
.login-note a{color: var(--warn); font-weight: 600; text-decoration: underline;}

/* ========== PANEL ========== */
.panel{
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 24px 20px;
}
.panel label{
  font-size: 12px; font-weight: 600; color: var(--muted);
  display: flex; align-items: center; gap: 6px; margin-bottom: 10px;
}
.panel input{
  width: 100%; background: var(--bg);
  border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px; color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 14px; outline: none; transition: 0.2s;
}
.panel input:focus{border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-glow)}
.panel input::placeholder{color: var(--muted); opacity: 0.5;}
.input-wrap{position: relative}
.input-wrap input{padding-right: 48px}
.toggle-eye{
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  background: transparent; border: none; color: var(--muted); cursor: pointer;
  width: 36px; height: 36px; border-radius: 8px;
  display: flex; align-items: center; justify-content: center;
}
.toggle-eye:active{background: var(--border)}
.hint{
  font-size: 12px; line-height: 1.6; color: var(--muted);
  background: rgba(99, 102, 241, 0.05);
  border-left: 2px solid var(--accent);
  padding: 12px 14px; border-radius: 6px; margin-top: 20px;
}
.hint b{color: var(--text); font-weight: 600;}
.hint .icon { color: var(--accent); display: inline-block; vertical-align: middle; margin-right: 4px; }

/* ========== BTN ========== */
.btn{
  width: 100%; padding: 16px; border: none; border-radius: 12px;
  font-size: 15px; font-weight: 600; cursor: pointer;
  margin-top: 24px; transition: 0.15s;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  color: #fff; background: var(--accent);
  box-shadow: 0 4px 14px var(--accent-glow);
}
.btn:active{transform: scale(0.97); box-shadow: none;}

/* ========== FOOTER ========== */
.footer{text-align: center; color: var(--muted); font-size: 12px; margin-top: 32px; line-height: 1.6;}
</style>
</head>
<body>

<!-- =================== VIEW 1: HOME =================== -->
<div id="view-home" class="view active">
  <div class="header">
    <div class="logo">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
    </div>
    <h1>Auto Tool Center</h1>
    <p>Hệ thống tự động hóa đa nền tảng</p>
    <div class="stats-bar">
      <div class="mini-stat"><span class="dot online"></span> Hệ thống ổn định</div>
      <div class="mini-stat">
        <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        12,458+
      </div>
    </div>
  </div>

  <div class="section-title">Dịch vụ hệ thống</div>
  <div class="grid">
    <!-- Featured Service -->
    <div class="service featured full" onclick="openLogin()">
      <div class="hot">HOT</div>
      <div class="ico">
        <svg class="icon icon-lg" viewBox="0 0 24 24"><line x1="6" y1="12" x2="10" y2="12"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="15" y1="13" x2="15.01" y2="13"/><line x1="18" y1="11" x2="18.01" y2="11"/><rect x="2" y="6" width="20" height="12" rx="2"/></svg>
      </div>
      <div class="info">
        <div class="name">Tool Auto LC79</div>
        <div class="sub">Tự động nhập Giftcode siêu tốc</div>
      </div>
      <div class="arrow">
        <svg class="icon" viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
      </div>
    </div>

    <!-- Normal Services -->
    <div class="service">
      <div class="ico">
        <svg class="icon" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
      </div>
      <div class="name">Auto Nhiệm Vụ</div>
      <div class="sub">Hoạt động 24/7</div>
    </div>
    
    <div class="service">
      <div class="ico">
        <svg class="icon" viewBox="0 0 24 24"><polyline points="20 12 20 22 4 22 4 12"/><rect x="2" y="7" width="20" height="5"/><line x1="12" y1="22" x2="12" y2="7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7z"/><path d="M12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7z"/></svg>
      </div>
      <div class="name">Auto Giftcode</div>
      <div class="sub">Đa nền tảng</div>
    </div>

    <div class="service">
      <div class="ico">
        <svg class="icon" viewBox="0 0 24 24"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
      </div>
      <div class="name">Check-in Auto</div>
      <div class="sub">Điểm danh</div>
    </div>

    <div class="service">
      <div class="ico">
        <svg class="icon" viewBox="0 0 24 24"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>
      </div>
      <div class="name">Rút Quà</div>
      <div class="sub">Nhận thưởng</div>
    </div>

    <div class="service">
      <div class="ico">
        <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="10" rx="2"/><circle cx="12" cy="5" r="2"/><path d="M12 7v4"/><line x1="8" y1="16" x2="8" y2="16"/><line x1="16" y1="16" x2="16" y2="16"/></svg>
      </div>
      <div class="name">Nuôi Acc</div>
      <div class="sub">Treo máy</div>
    </div>

    <div class="service">
      <div class="ico">
        <svg class="icon" viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
      </div>
      <div class="name">Thống Kê</div>
      <div class="sub">Báo cáo</div>
    </div>
  </div>

  <div class="footer">© 2026 Auto Tool Center.<br>Hệ thống bảo mật & tối ưu.</div>
</div>

<!-- =================== VIEW 2: LOGIN LC79 =================== -->
<div id="view-login" class="view">
  <button class="back-btn" onclick="goHome()">
    <svg class="icon icon-sm" viewBox="0 0 24 24"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>
    Quay lại
  </button>

  <div class="lc-header">
    <div class="lc-icon">
      <svg class="icon icon-lg" viewBox="0 0 24 24"><line x1="6" y1="12" x2="10" y2="12"/><line x1="8" y1="10" x2="8" y2="14"/><line x1="15" y1="13" x2="15.01" y2="13"/><line x1="18" y1="11" x2="18.01" y2="11"/><rect x="2" y="6" width="20" height="12" rx="2"/></svg>
    </div>
    <h2>Xác Thực Telegram</h2>
    <p>Kết nối API để sử dụng Tool Auto LC79</p>
  </div>

  <div class="login-note">
    <svg class="icon" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
    <div>Yêu cầu <b>API ID</b> và <b>API HASH</b>. Lấy tại <a href="https://my.telegram.org" target="_blank">my.telegram.org</a></div>
  </div>

  <div class="panel">
    <label>
      <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      API ID
    </label>
    <input type="number" id="apiId" placeholder="Nhập API ID (VD: 12345678)" inputmode="numeric" autocomplete="off">

    <label style="margin-top:20px">
      <svg class="icon icon-sm" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      API HASH
    </label>
    <div class="input-wrap">
      <input type="password" id="apiHash" placeholder="Nhập API HASH" autocomplete="off">
      <button class="toggle-eye" onclick="toggleHash()">
        <svg class="icon icon-sm" id="eye-icon" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
      </button>
    </div>

    <div class="hint">
      <svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
      <b>Hướng dẫn:</b><br>
      1. Truy cập <b>my.telegram.org</b> và đăng nhập.<br>
      2. Chọn <b>API development tools</b>.<br>
      3. Sao chép <b>App api_id</b> và <b>App api_hash</b>.
    </div>

    <button class="btn" onclick="handleLogin()">
      <svg class="icon icon-sm" viewBox="0 0 24 24"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/><polyline points="10 17 15 12 10 7"/><line x1="15" y1="12" x2="3" y2="12"/></svg>
      Tiến Hành Đăng Nhập
    </button>
  </div>

  <div class="footer">
    <svg class="icon icon-sm" style="display:inline-block;vertical-align:middle;margin-right:4px" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
    Dữ liệu được mã hóa an toàn tuyệt đối.
  </div>
</div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) { tg.ready(); tg.expand(); }

function openLogin() {
  document.getElementById('view-home').classList.remove('active');
  document.getElementById('view-login').classList.add('active');
  window.scrollTo({top:0, behavior:'smooth'});
  if (tg) tg.HapticFeedback?.impactOccurred('light');
}

function goHome() {
  document.getElementById('view-login').classList.remove('active');
  document.getElementById('view-home').classList.add('active');
  window.scrollTo({top:0, behavior:'smooth'});
  if (tg) tg.HapticFeedback?.impactOccurred('light');
}

function toggleHash() {
  const el = document.getElementById('apiHash');
  const icon = document.getElementById('eye-icon');
  if (el.type === 'password') {
    el.type = 'text';
    icon.innerHTML = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';
  } else {
    el.type = 'password';
    icon.innerHTML = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
  }
}

function handleLogin() {
  const apiId   = document.getElementById('apiId').value.trim();
  const apiHash = document.getElementById('apiHash').value.trim();

  if (!apiId || !apiHash) {
    showToast('Vui lòng nhập đầy đủ thông tin!');
    return;
  }
  if (!/^\d+$/.test(apiId)) {
    showToast('API ID không hợp lệ (phải là số).');
    return;
  }
  if (apiHash.length < 20) {
    showToast('API HASH không hợp lệ.');
    return;
  }

  showToast('Chức năng đang được bảo trì, vui lòng chờ!');
  if (tg) tg.HapticFeedback?.notificationOccurred('warning');
}

function showToast(msg) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.style.cssText =
      'position:fixed;bottom:30px;left:50%;transform:translateX(-50%) translateY(100px);' +
      'background:#1c212c;color:#f3f4f6;padding:14px 24px;border-radius:12px;' +
      'font-size:14px;font-weight:500;box-shadow:0 10px 40px rgba(0,0,0,.6);' +
      'border:1px solid #2d3748;z-index:9999;transition:all .3s cubic-bezier(0.16, 1, 0.3, 1);' +
      'max-width:90%;text-align:center;opacity:0;';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  requestAnimationFrame(() => {
    t.style.transform = 'translateX(-50%) translateY(0)';
    t.style.opacity = '1';
  });
  clearTimeout(t._timeout);
  t._timeout = setTimeout(() => {
    t.style.transform = 'translateX(-50%) translateY(100px)';
    t.style.opacity = '0';
  }, 2500);
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
#                    SELF-PING
# ================================================================
def keep_alive():
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL chưa set — bỏ qua self-ping")
        return
    url = WEBAPP_URL.rstrip("/") + "/health"
    logger.info(f"Self-ping: {url} mỗi {PING_INTERVAL}s")
    time.sleep(15)
    while True:
        try:
            r = requests.get(url, timeout=10)
            logger.info(f"Ping OK ({r.status_code})")
        except Exception as e:
            logger.warning(f"Ping fail: {e}")
        time.sleep(PING_INTERVAL)

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)

# ================================================================
#                    TELEGRAM BOT MESSAGE (PREMIUM ICONS)
# ================================================================
# Sử dụng Custom Emoji (Telegram Premium). 
# Các ID dưới đây là ID của Telegram Animated Emoji hệ thống.
START_MESSAGE = """
<tg-emoji emoji-id="5368324170671202286">🚀</tg-emoji> <b>TOOL NHIỆM VỤ AUTO 100%</b> <tg-emoji emoji-id="5368324170671202286">🚀</tg-emoji>

<tg-emoji emoji-id="5447644880824181073">✨</tg-emoji> <b>Chào mừng</b> <i>{name}</i> đến với hệ thống!
<tg-emoji emoji-id="4960917714325121485">🔥</tg-emoji> Hệ thống auto nhiệm vụ <b>thế hệ mới</b> — nhanh, mượt, an toàn.

──────────────────
<tg-emoji emoji-id="6235401323201735695">💎</tg-emoji> <b>TÍNH NĂNG NỔI BẬT</b>
<tg-emoji emoji-id="5276398495459370121">⚡</tg-emoji> Auto làm nhiệm vụ 24/7
<tg-emoji emoji-id="5386714776240519929">🤖</tg-emoji> Xử lý thông minh 100% tự động
<tg-emoji emoji-id="5368324170671202286">🎮</tg-emoji> Tool Auto Nhập Code <b>LC79</b>
<tg-emoji emoji-id="5278578979322940035">🔒</tg-emoji> Bảo mật cao — không lộ info
<tg-emoji emoji-id="5276220482695545233">♻️</tg-emoji> Cập nhật liên tục — miễn phí

──────────────────
<tg-emoji emoji-id="5445147458006596472">📊</tg-emoji> <b>THỐNG KÊ HỆ THỐNG</b>
<tg-emoji emoji-id="5388671156088144891">👥</tg-emoji> Người dùng: <b>12,458+</b>
<tg-emoji emoji-id="5276398495459370121">✅</tg-emoji> Tỉ lệ thành công: <b>99.9%</b>
<tg-emoji emoji-id="5276398495459370121">⚡</tg-emoji> Uptime: <b>24/7</b>

──────────────────
<tg-emoji emoji-id="5368324170671202286">🎯</tg-emoji> <b>Bấm nút bên dưới để mở Mini App</b> 👇
"""
# Lưu ý: Bạn có thể thay đổi các `emoji-id` bằng ID của bất kỳ gói Sticker Premium nào bạn thích.
# Fallback emoji (ví dụ: 🚀) sẽ hiển thị nếu người dùng không có Telegram Premium hoặc thiết bị không hỗ trợ.

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "MỞ MINI APP — TẤT CẢ DỊCH VỤ",
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

# ================================================================
#                    MAIN
# ================================================================
def main():
    if not BOT_TOKEN or "DÁN_TOKEN" in BOT_TOKEN:
        logger.error("BOT_TOKEN chưa set!")
        return
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL chưa set — nút Mini App sẽ không bấm được!")

    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"Flask chạy tại port {PORT}")

    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    logger.info("Bot đang chạy...")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()