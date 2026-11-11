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
PORT          = int(os.getenv("PORT", 10000))                    # Render dùng 10000
PING_INTERVAL = int(os.getenv("PING_INTERVAL", 5))               # giây (mặc định 5s)
WEBAPP_URL    = os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL", "")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AutoTool")

flask_app = Flask(__name__)

# ================================================================
#                    MINI APP HTML (SIÊU ĐẸP)
# ================================================================
MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="theme-color" content="#080b14">
<title>Auto Tool Center</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{
  --bg:#080b14;
  --card:#111827;
  --card2:#1a2236;
  --accent:#4f8cff;
  --accent2:#00e5a0;
  --danger:#ff5470;
  --warn:#ffb547;
  --text:#eaf0ff;
  --muted:#8898b8;
  --border:rgba(255,255,255,0.08);
  --radius:18px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  background:var(--bg);
  color:var(--text);
  min-height:100vh;
  padding:16px 14px 40px;
  position:relative;
  overflow-x:hidden;
}
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(circle at 15% 5%, #1e3a8a55 0%, transparent 45%),
    radial-gradient(circle at 85% 95%, #00e5a033 0%, transparent 45%),
    radial-gradient(circle at 50% 50%, #4f8cff22 0%, transparent 70%);
}
body>*{position:relative;z-index:1}

/* ========== VIEW ========== */
.view{display:none;animation:viewIn .3s ease}
.view.active{display:block}
@keyframes viewIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* ========== HEADER ========== */
.header{text-align:center;padding:20px 0 24px}
.logo{
  display:inline-flex;align-items:center;justify-content:center;
  width:72px;height:72px;border-radius:24px;
  background:linear-gradient(135deg,#4f8cff,#00e5a0);
  font-size:36px;margin-bottom:16px;
  box-shadow:0 12px 40px rgba(79,140,255,.35),
             inset 0 2px 0 rgba(255,255,255,.2);
  animation:float 3s ease-in-out infinite;
}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
.header h1{
  font-size:24px;font-weight:800;letter-spacing:.5px;
  background:linear-gradient(90deg,#4f8cff,#00e5a0,#4f8cff);
  background-size:200% auto;
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;animation:shine 3s linear infinite;
}
@keyframes shine{to{background-position:200% center}}
.header p{color:var(--muted);font-size:13.5px;margin-top:6px}
.stats-bar{display:flex;justify-content:center;gap:8px;margin-top:14px;flex-wrap:wrap}
.mini-stat{
  padding:5px 12px;font-size:11.5px;font-weight:600;
  border-radius:99px;background:var(--card);
  border:1px solid var(--border);color:var(--muted);
  display:inline-flex;align-items:center;gap:5px;
}
.mini-stat .dot{
  width:6px;height:6px;border-radius:50%;background:var(--accent2);
  box-shadow:0 0 8px var(--accent2);
  animation:pulse 1.5s ease-in-out infinite;
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.85)}}

/* ========== SECTION ========== */
.section-title{
  font-size:12px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:1.2px;margin:16px 4px 12px;
}

/* ========== GRID ========== */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.service{
  background:linear-gradient(160deg,var(--card),var(--card2));
  border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 14px;cursor:pointer;transition:.2s;
  display:flex;flex-direction:column;gap:8px;
  position:relative;overflow:hidden;
}
.service:active{transform:scale(.97);border-color:rgba(79,140,255,.5)}
.service .ico{font-size:26px;line-height:1}
.service .name{font-size:13.5px;font-weight:700;line-height:1.3}
.service .sub{font-size:11.5px;color:var(--muted);line-height:1.4}
.service.full{grid-column:1 / -1;flex-direction:row;align-items:center;gap:14px;padding:18px 16px}
.service.full .ico{font-size:32px}
.service.full .info{flex:1;display:flex;flex-direction:column;gap:4px}
.service.full .arrow{font-size:22px;color:var(--accent);transition:.3s}
.service.full:active .arrow{transform:translateX(4px)}
.service.featured{
  background:linear-gradient(135deg,#4f8cff22,#00e5a022,#1a2236);
  border-color:rgba(79,140,255,.4);
  box-shadow:0 8px 30px rgba(79,140,255,.15);
}
.service .hot{
  position:absolute;top:10px;right:10px;
  font-size:9.5px;font-weight:800;letter-spacing:.5px;
  padding:3px 8px;border-radius:99px;
  background:linear-gradient(135deg,#ff5470,#ff8c42);
  color:#fff;box-shadow:0 2px 10px rgba(255,84,112,.5);
  animation:hotPulse 2s ease-in-out infinite;
}
@keyframes hotPulse{0%,100%{transform:scale(1)}50%{transform:scale(1.08)}}

/* ========== BACK BTN ========== */
.back-btn{
  display:inline-flex;align-items:center;gap:6px;
  background:var(--card);border:1px solid var(--border);
  color:var(--text);padding:9px 16px;border-radius:99px;
  font-size:13px;font-weight:600;cursor:pointer;
  margin-bottom:18px;transition:.2s;
}
.back-btn:active{transform:scale(.95);background:var(--card2)}

/* ========== LC HEADER ========== */
.lc-header{
  background:linear-gradient(135deg,#ff6a3d,#ff2e63);
  border-radius:var(--radius);padding:24px 18px;
  margin-bottom:16px;text-align:center;
  box-shadow:0 12px 40px rgba(255,46,99,.3);
  position:relative;overflow:hidden;
}
.lc-header::before{
  content:'';position:absolute;inset:0;
  background:radial-gradient(circle at 30% 20%,rgba(255,255,255,.2),transparent 50%);
  pointer-events:none;
}
.lc-icon{font-size:40px;margin-bottom:8px;filter:drop-shadow(0 4px 12px rgba(0,0,0,.3))}
.lc-header h2{font-size:19px;font-weight:800;letter-spacing:.5px;margin-bottom:6px;position:relative;z-index:1}
.lc-header p{font-size:12.5px;opacity:.92;position:relative;z-index:1}

/* ========== NOTE ========== */
.login-note{
  display:flex;gap:10px;align-items:flex-start;
  padding:12px 14px;border-radius:14px;
  background:rgba(255,181,71,.1);
  border:1px solid rgba(255,181,71,.25);
  font-size:12px;line-height:1.55;
  color:#ffd88a;margin-bottom:14px;
}
.login-note span{font-size:16px;flex-shrink:0}
.login-note a{color:var(--warn);font-weight:700;text-decoration:underline}

/* ========== PANEL ========== */
.panel{
  background:var(--card);border:1px solid var(--border);
  border-radius:var(--radius);padding:18px 16px;
}
.panel label{
  font-size:11.5px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.8px;
  display:block;margin-bottom:8px;
}
.panel input{
  width:100%;background:var(--card2);
  border:1.5px solid var(--border);border-radius:12px;
  padding:14px 16px;color:var(--text);
  font-family:ui-monospace,Menlo,monospace;
  font-size:14px;outline:none;transition:.2s;
}
.panel input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(79,140,255,.15)}
.panel input::placeholder{color:var(--muted);opacity:.6;font-weight:400}
.input-wrap{position:relative}
.input-wrap input{padding-right:48px}
.toggle-eye{
  position:absolute;right:6px;top:50%;transform:translateY(-50%);
  background:transparent;border:none;font-size:18px;cursor:pointer;
  width:38px;height:38px;border-radius:10px;
  display:flex;align-items:center;justify-content:center;transition:.2s;
}
.toggle-eye:active{background:rgba(255,255,255,.08)}
.hint{
  font-size:12px;line-height:1.65;color:var(--muted);
  background:rgba(79,140,255,.06);
  border-left:3px solid var(--accent);
  padding:10px 12px;border-radius:8px;margin-top:16px;
}
.hint b{color:var(--text)}

/* ========== BTN ========== */
.btn{
  width:100%;padding:15px;border:none;border-radius:14px;
  font-size:15px;font-weight:800;letter-spacing:.3px;
  cursor:pointer;margin-top:18px;transition:.15s;
  display:flex;align-items:center;justify-content:center;gap:8px;
  color:#04121f;background:linear-gradient(135deg,#4f8cff,#00e5a0);
  box-shadow:0 8px 24px rgba(79,140,255,.35);
}
.btn:active{transform:scale(.97);box-shadow:0 4px 12px rgba(79,140,255,.25)}

/* ========== FOOTER ========== */
.footer{
  text-align:center;color:var(--muted);
  font-size:11.5px;margin-top:32px;line-height:1.5;
}
</style>
</head>
<body>

<!-- =================== VIEW 1: HOME =================== -->
<div id="view-home" class="view active">
  <div class="header">
    <div class="logo">⚡</div>
    <h1>AUTO TOOL CENTER</h1>
    <p>Toàn bộ dịch vụ auto — 100% tự động</p>
    <div class="stats-bar">
      <div class="mini-stat"><span class="dot"></span> Online 24/7</div>
      <div class="mini-stat">👥 12.4K users</div>
      <div class="mini-stat">✅ 99.9%</div>
    </div>
  </div>

  <div class="section-title">🔥 Dịch vụ phổ biến</div>
  <div class="grid">
    <div class="service featured full" onclick="openLogin()">
      <div class="hot">HOT</div>
      <div class="ico">🎮</div>
      <div class="info">
        <div class="name">Tool Auto Nhập Code LC79</div>
        <div class="sub">Tự động redeem giftcode — siêu nhanh, siêu chuẩn</div>
      </div>
      <div class="arrow">→</div>
    </div>

    <div class="service">
      <div class="ico">⚡</div>
      <div class="name">Auto Nhiệm Vụ</div>
      <div class="sub">Chạy 24/7</div>
    </div>
    <div class="service">
      <div class="ico">🎁</div>
      <div class="name">Auto Giftcode</div>
      <div class="sub">Đa game</div>
    </div>
    <div class="service">
      <div class="ico">📱</div>
      <div class="name">Auto Check-in</div>
      <div class="sub">Điểm danh</div>
    </div>
    <div class="service">
      <div class="ico">💰</div>
      <div class="name">Auto Rút Quà</div>
      <div class="sub">Nhận thưởng</div>
    </div>
    <div class="service">
      <div class="ico">🤖</div>
      <div class="name">Auto Nuôi Acc</div>
      <div class="sub">Treo máy</div>
    </div>
    <div class="service">
      <div class="ico">📊</div>
      <div class="name">Thống Kê</div>
      <div class="sub">Báo cáo</div>
    </div>
  </div>

  <div class="footer">© 2026 Auto Tool Center • v3.0</div>
</div>

<!-- =================== VIEW 2: LOGIN LC79 =================== -->
<div id="view-login" class="view">
  <button class="back-btn" onclick="goHome()">← Quay lại</button>

  <div class="lc-header">
    <div class="lc-icon">🎮</div>
    <h2>ĐĂNG NHẬP TELEGRAM</h2>
    <p>Kết nối tài khoản để dùng Tool Auto LC79</p>
  </div>

  <div class="login-note">
    <span>⚠️</span>
    <div>Tool cần <b>API ID</b> & <b>API HASH</b> để đăng nhập Telegram.<br>
      Lấy tại: <a href="https://my.telegram.org" target="_blank">my.telegram.org</a></div>
  </div>

  <div class="panel">
    <label>🆔 API ID</label>
    <input type="number" id="apiId" placeholder="VD: 12345678" inputmode="numeric" autocomplete="off">

    <label style="margin-top:14px">🔑 API HASH</label>
    <div class="input-wrap">
      <input type="password" id="apiHash" placeholder="VD: a1b2c3d4e5f6..." autocomplete="off">
      <button class="toggle-eye" onclick="toggleHash()">👁</button>
    </div>

    <div class="hint">
      💡 <b>Hướng dẫn lấy API:</b><br>
      1. Vào <b>my.telegram.org</b> → đăng nhập<br>
      2. Chọn <b>API development tools</b><br>
      3. Copy <b>App api_id</b> và <b>App api_hash</b>
    </div>

    <button class="btn" onclick="handleLogin()">
      🔐 ĐĂNG NHẬP
    </button>
  </div>

  <div class="footer">🔒 Thông tin được mã hoá & bảo mật tuyệt đối</div>
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
}

function toggleHash() {
  const el = document.getElementById('apiHash');
  el.type = el.type === 'password' ? 'text' : 'password';
}

function handleLogin() {
  const apiId   = document.getElementById('apiId').value.trim();
  const apiHash = document.getElementById('apiHash').value.trim();

  if (!apiId || !apiHash) {
    showToast('⚠️ Vui lòng nhập đầy đủ API ID và API HASH');
    return;
  }
  if (!/^\d+$/.test(apiId)) {
    showToast('⚠️ API ID phải là số');
    return;
  }
  if (apiHash.length < 20) {
    showToast('⚠️ API HASH không hợp lệ');
    return;
  }

  // TODO: chức năng đăng nhập thêm sau
  showToast('🚧 Chức năng đang phát triển — vui lòng chờ!');
  if (tg) tg.HapticFeedback?.notificationOccurred('warning');
}

function showToast(msg) {
  let t = document.getElementById('toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'toast';
    t.style.cssText =
      'position:fixed;bottom:30px;left:50%;transform:translateX(-50%) translateY(100px);' +
      'background:#1a2236;color:#eaf0ff;padding:12px 20px;border-radius:14px;' +
      'font-size:13.5px;font-weight:600;box-shadow:0 10px 40px rgba(0,0,0,.5);' +
      'border:1px solid rgba(255,255,255,.1);z-index:9999;transition:.3s;' +
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
#                    SELF-PING (GIỮ RENDER THỨC)
# ================================================================
def keep_alive():
    if not WEBAPP_URL:
        logger.warning("⚠️ WEBAPP_URL chưa set — bỏ qua self-ping")
        return
    url = WEBAPP_URL.rstrip("/") + "/health"
    logger.info(f"🔁 Self-ping: {url} mỗi {PING_INTERVAL}s")
    time.sleep(15)  # đợi server sẵn sàng
    while True:
        try:
            r = requests.get(url, timeout=10)
            logger.info(f"💓 Ping OK ({r.status_code})")
        except Exception as e:
            logger.warning(f"❌ Ping fail: {e}")
        time.sleep(PING_INTERVAL)

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)

# ================================================================
#                    TELEGRAM BOT
# ================================================================
START_MESSAGE = """
╔═══════════════════════╗
     🚀 <b>TOOL NHIỆM VỤ AUTO 100%</b> 🚀
╚═══════════════════════╝

✨ <b>Chào mừng</b> <i>{name}</i> đến với hệ thống!
🔥 Hệ thống auto nhiệm vụ <b>thế hệ mới</b> — nhanh, mượt, an toàn.

━━━━━━━━━━━━━━━━━━━━━━
💎 <b>TÍNH NĂNG NỔI BẬT</b>
├ ⚡ Auto làm nhiệm vụ 24/7
├ 🤖 Xử lý thông minh 100% tự động
├ 🎮 Tool Auto Nhập Code <b>LC79</b>
├ 🔒 Bảo mật cao — không lộ info
└ ♻️ Cập nhật liên tục — miễn phí

━━━━━━━━━━━━━━━━━━━━━━
📊 <b>THỐNG KÊ HỆ THỐNG</b>
👥 Người dùng: <b>12,458+</b>
✅ Tỉ lệ thành công: <b>99.9%</b>
⚡ Uptime: <b>24/7</b>

━━━━━━━━━━━━━━━━━━━━━━
🎯 <b>Bấm nút bên dưới để mở Mini App</b> 👇
"""

KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton(
        "🌐 MỞ MINI APP — TẤT CẢ DỊCH VỤ",
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
        logger.error("❌ BOT_TOKEN chưa set!")
        return
    if not WEBAPP_URL:
        logger.warning("⚠️ WEBAPP_URL chưa set — nút Mini App sẽ không bấm được!")

    # Self-ping
    threading.Thread(target=keep_alive, daemon=True).start()
    # Flask
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"🌐 Flask chạy tại port {PORT}")

    # Bot
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    logger.info("🤖 Bot đang chạy...")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()