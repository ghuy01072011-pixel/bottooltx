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
from flask import Flask, Response, jsonify
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, ContextTypes, filters
)
from telegram.error import Conflict

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

# ============ CONFIG ============
BOT_TOKEN     = os.getenv("BOT_TOKEN", "8584201446:AAHAINg4IALEX1sJ-dmiinLsPLZ-u_S0nTc")
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

# ============ STATES ============
(ASK_API_ID, ASK_API_HASH, ASK_PHONE, ASK_OTP, ASK_2FA,
 ASK_GAME_USER, ASK_GAME_PASS) = range(7)

# ============ HELPERS ============
def fmt_num(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


async def send_code_tg(api_id, api_hash, phone):
    client = None
    try:
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.connect()
        sent = await client.send_code_request(phone)
        session_str = client.session.save()
        await client.disconnect()
        return {"status": "ok", "phone_code_hash": sent.phone_code_hash, "session": session_str}
    except Exception as e:
        if client and client.is_connected():
            try: await client.disconnect()
            except Exception: pass
        return {"status": "error", "message": str(e)}


async def verify_code_tg(api_id, api_hash, phone, code, phone_code_hash, session_str):
    client = None
    try:
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            new_session = client.session.save()
            await client.disconnect()
            return {"status": "2fa", "session": new_session}
        except Exception:
            await client.disconnect()
            return {"status": "error", "message": "Mã OTP không đúng hoặc đã hết hạn."}
        new_session = client.session.save()
        await client.disconnect()
        return {"status": "ok", "session": new_session}
    except Exception as e:
        if client and client.is_connected():
            try: await client.disconnect()
            except Exception: pass
        return {"status": "error", "message": str(e)}


async def verify_2fa_tg(api_id, api_hash, password, session_str):
    client = None
    try:
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        await client.connect()
        try:
            await client.sign_in(password=password)
        except Exception:
            await client.disconnect()
            return {"status": "error", "message": "Mật khẩu 2FA không chính xác."}
        new_session = client.session.save()
        await client.disconnect()
        return {"status": "ok", "session": new_session}
    except Exception as e:
        if client and client.is_connected():
            try: await client.disconnect()
            except Exception: pass
        return {"status": "error", "message": str(e)}


async def get_codes_tg(api_id, api_hash, session_str):
    client = None
    try:
        client = TelegramClient(StringSession(session_str), int(api_id), api_hash)
        await client.connect()

        channel_entity = None
        try:
            async for dialog in client.iter_dialogs(limit=200):
                if dialog.id == -1002237214360 or (dialog.entity and getattr(dialog.entity, 'username', '') == 'codelaucuagame'):
                    channel_entity = dialog.entity
                    break
        except Exception:
            pass

        if not channel_entity:
            try: channel_entity = await client.get_entity('codelaucuagame')
            except Exception: pass

        if not channel_entity:
            try: channel_entity = await client.get_entity(-1002237214360)
            except Exception: pass

        if not channel_entity:
            await client.disconnect()
            return {"status": "error", "message": "Không tìm thấy kênh LC79. Tài khoản có thể chưa join."}

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

        await client.disconnect()

        if not codes:
            return {"status": "error", "message": "Không tìm thấy code LC79 nào trong 100 tin gần nhất."}
        return {"status": "ok", "codes": codes}
    except Exception as e:
        if client and client.is_connected():
            try: await client.disconnect()
            except Exception: pass
        logger.error(f"get_codes_tg error: {e}")
        return {"status": "error", "message": str(e)}


def login_game_sync(username, password):
    """Login game LC79 (sync, chạy trong thread)"""
    try:
        pw_hash = hashlib.md5(password.encode("utf-8")).hexdigest()
        params = {"c": 3, "un": username, "pw": pw_hash, "cp": "R", "cl": "R", "pf": "web", "at": ""}
        resp = requests.get(GAME_API_URL, params=params, timeout=15)
        result = resp.json()
        if not result.get("success"):
            return {"status": "error", "message": "Sai tài khoản hoặc mật khẩu game!"}
        session_key = result.get("sessionKey", "")
        user_info = {}
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


# ============ BOT HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "💎 <b>AUTO TOOL VIP</b> 💎\n\n"
        "👋 Chào mừng bạn đến với tool!\n"
        "🔐 Tool sẽ đăng nhập Telegram + game LC79 để tự động lấy code.\n\n"
        "📝 <b>Bước 1:</b> Nhập <b>API ID</b> Telegram:\n"
        "<i>(Lấy tại my.telegram.org)</i>\n\n"
        "Gõ /cancel để hủy bất cứ lúc nào.",
        parse_mode="HTML"
    )
    return ASK_API_ID


async def ask_api_hash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ API ID phải là số. Nhập lại:")
        return ASK_API_ID
    context.user_data["api_id"] = text
    await update.message.reply_text("🔑 Nhập <b>API HASH</b>:", parse_mode="HTML")
    return ASK_API_HASH


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["api_hash"] = update.message.text.strip()
    await update.message.reply_text(
        "📱 Nhập <b>số điện thoại</b> Telegram:\n<i>(VD: +84987654321)</i>",
        parse_mode="HTML"
    )
    return ASK_PHONE


async def send_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    d = context.user_data
    msg = await update.message.reply_text("⏳ Đang gửi OTP...")
    result = await send_code_tg(d["api_id"], d["api_hash"], phone)
    if result["status"] != "ok":
        await msg.edit_text(f"❌ Lỗi: {result['message']}\n\nNhập lại số điện thoại:")
        return ASK_PHONE
    d["phone"] = phone
    d["phone_code_hash"] = result["phone_code_hash"]
    d["session"] = result["session"]
    await msg.edit_text("✅ Đã gửi OTP!\n\n🔑 Nhập <b>mã OTP</b>:", parse_mode="HTML")
    return ASK_OTP


async def verify_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    d = context.user_data
    msg = await update.message.reply_text("⏳ Đang xác thực...")
    result = await verify_code_tg(d["api_id"], d["api_hash"], d["phone"], code, d["phone_code_hash"], d["session"])
    if result["status"] == "ok":
        d["session"] = result["session"]
        await msg.edit_text(
            "✅ Đăng nhập Telegram thành công!\n\n"
            "🎮 Nhập <b>username game LC79</b>:",
            parse_mode="HTML"
        )
        return ASK_GAME_USER
    elif result["status"] == "2fa":
        d["session"] = result["session"]
        await msg.edit_text(
            "🔐 Tài khoản có 2FA.\n\n"
            "Nhập <b>mật khẩu 2FA</b>:",
            parse_mode="HTML"
        )
        return ASK_2FA
    else:
        await msg.edit_text(f"❌ {result['message']}\n\nNhập lại OTP:")
        return ASK_OTP


async def verify_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    d = context.user_data
    try: await update.message.delete()
    except Exception: pass
    msg = await context.bot.send_message(update.effective_chat.id, "⏳ Đang xác thực 2FA...")
    result = await verify_2fa_tg(d["api_id"], d["api_hash"], password, d["session"])
    if result["status"] == "ok":
        d["session"] = result["session"]
        await msg.edit_text(
            "✅ Đăng nhập Telegram thành công!\n\n"
            "🎮 Nhập <b>username game LC79</b>:",
            parse_mode="HTML"
        )
        return ASK_GAME_USER
    else:
        await msg.edit_text(f"❌ {result['message']}\n\nNhập lại mật khẩu 2FA:")
        return ASK_2FA


async def ask_game_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["game_user"] = update.message.text.strip()
    await update.message.reply_text(
        "🔒 Nhập <b>mật khẩu game LC79</b>:\n\n"
        "⚠️ Tin nhắn sẽ được xoá tự động vì lý do bảo mật.",
        parse_mode="HTML"
    )
    return ASK_GAME_PASS


async def process_game_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    try: await update.message.delete()
    except Exception: pass

    d = context.user_data
    game_user = d["game_user"]
    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(chat_id, "⏳ Đang đăng nhập game LC79...")

    game_result = await asyncio.to_thread(login_game_sync, game_user, password)

    if game_result["status"] != "ok":
        await msg.edit_text(
            f"❌ {game_result['message']}\n\nNhập lại <b>username game LC79</b>:",
            parse_mode="HTML"
        )
        d.pop("game_user", None)
        return ASK_GAME_USER

    await msg.edit_text(
        "✅ Đăng nhập game thành công!\n\n⏳ Đang quét kênh lấy code LC79...",
        parse_mode="HTML"
    )
    codes_result = await get_codes_tg(d["api_id"], d["api_hash"], d["session"])

    info_block = (
        f"✅ <b>ĐĂNG NHẬP THÀNH CÔNG</b>\n\n"
        f"👤 Tài khoản: <b>{game_result['username']}</b>\n"
        f"💰 Số dư Vin: <b>{fmt_num(game_result['balance'])}</b>\n"
        f"🎯 Xu: <b>{fmt_num(game_result['xuTotal'])}</b>\n\n"
    )

    if codes_result["status"] == "ok":
        codes = codes_result["codes"]
        codes_lines = "\n".join([f"{i+1}. <code>LC79{c}</code>" for i, c in enumerate(codes)])
        final_text = (
            info_block
            + f"📋 <b>10 CODE MỚI NHẤT:</b>\n\n{codes_lines}\n\n"
            + "💡 Chạm vào code để copy.\n\n"
            + "🔄 Gõ /start để bắt đầu lại."
        )
    else:
        final_text = (
            info_block
            + f"⚠️ Không lấy được code: {codes_result['message']}\n\n"
            + "🔄 Gõ /start để bắt đầu lại."
        )

    await msg.edit_text(final_text, parse_mode="HTML")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Đã hủy.\n\nGõ /start để bắt đầu lại.")
    return ConversationHandler.END


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = int(time.time() - SERVER_START_TIME)
    h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
    await update.message.reply_text(
        f"🚀 <b>SERVER STATUS</b>\n\n"
        f"🟢 Bot: <b>Online</b>\n"
        f"⏱ Uptime: <b>{h:02d}:{m:02d}:{s:02d}</b>\n"
        f"🌐 Web health: <code>/health</code>",
        parse_mode="HTML"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.warning("⚠️ Conflict — chờ 10s rồi tiếp tục")
        await asyncio.sleep(10)
    else:
        logger.error(f"Lỗi: {context.error}")


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "Bắt đầu tool"),
        BotCommand("cancel", "Hủy thao tác hiện tại"),
        BotCommand("status", "Xem trạng thái server"),
    ])


# ================================================================
#                    ALIVE HTML
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
  background:rgba(20,24,39,0.6);backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
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
h1{
  font-size:28px;font-weight:800;margin-bottom:8px;letter-spacing:-0.5px;
  background:linear-gradient(to right,#34d399,#22d3ee);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.status{
  display:inline-flex;align-items:center;gap:8px;
  color:#34d399;font-size:13px;font-weight:600;margin-bottom:24px;
}
.dot{
  width:10px;height:10px;border-radius:50%;background:#10b981;
  box-shadow:0 0 12px #10b981;
  animation:pulse 1.5s ease-in-out infinite;
}
@keyframes pulse{
  0%,100%{transform:scale(1);opacity:1}
  50%{transform:scale(1.4);opacity:.6}
}
.stat{
  background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.08);
  border-radius:12px;padding:14px 18px;margin-top:12px;
  display:flex;justify-content:space-between;align-items:center;
}
.stat-label{color:#94a3b8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px}
.stat-value{color:#34d399;font-size:15px;font-weight:800;font-family:'Courier New',monospace}
.footer{margin-top:24px;color:#64748b;font-size:11px;font-weight:500}
</style>
</head>
<body>
<div class="card">
  <div class="icon-wrap">🚀</div>
  <h1>SERVER ALIVE</h1>
  <div class="status"><span class="dot"></span>Bot đang hoạt động ổn định</div>
  <div class="stat">
    <span class="stat-label">Uptime</span>
    <span class="stat-value" id="uptime">--:--:--</span>
  </div>
  <div class="stat">
    <span class="stat-label">Ping</span>
    <span class="stat-value" id="ping">--</span>
  </div>
  <div class="stat">
    <span class="stat-label">Last Update</span>
    <span class="stat-value" id="lastUpdate">--</span>
  </div>
  <div class="footer">Auto refresh every 10 seconds</div>
</div>
<script>
function pad(n){return n.toString().padStart(2,'0')}
function fmt(s){
  var h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60;
  return pad(h)+':'+pad(m)+':'+pad(sec);
}
async function tick(){
  try{
    var t0 = performance.now();
    var res = await fetch('/alive', {cache:'no-store'});
    var t1 = performance.now();
    var data = await res.json();
    document.getElementById('uptime').textContent = fmt(data.uptime);
    document.getElementById('ping').textContent = Math.round(t1-t0) + ' ms';
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString('vi-VN');
  }catch(e){
    document.getElementById('ping').textContent = 'ERR';
  }
}
setInterval(tick, 10000);
tick();
</script>
</body>
</html>
"""


# ============ FLASK ROUTES ============
@flask_app.route("/")
def index():
    return Response(ALIVE_HTML, mimetype="text/html")


@flask_app.route("/health")
def health():
    return Response("ok", mimetype="text/plain")


@flask_app.route("/alive")
def alive():
    return jsonify({
        "status": "alive",
        "uptime": int(time.time() - SERVER_START_TIME),
        "timestamp": int(time.time()),
    })


# ============ SELF-PING & FLASK RUN ============
def keep_alive():
    if not WEBAPP_URL:
        logger.warning("WEBAPP_URL/RENDER_EXTERNAL_URL chưa set — tự ping bị tắt.")
        return
    url = WEBAPP_URL.rstrip("/") + "/health"
    time.sleep(15)
    while True:
        try:
            requests.get(url, timeout=10)
            logger.debug(f"Self-ping OK: {url}")
        except Exception as e:
            logger.debug(f"Self-ping fail: {e}")
        time.sleep(PING_INTERVAL)


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)


# ============ MAIN ============
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN chưa set!")
        return

    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=run_flask, daemon=True).start()

    logger.info("Khởi động bot...")

    try:
        requests.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook?drop_pending_updates=true",
            timeout=5
        )
        logger.info("Đã xoá webhook cũ.")
    except Exception as e:
        logger.warning(f"Không xoá được webhook: {e}")

    bot_app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_API_ID:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_api_hash)],
            ASK_API_HASH:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_PHONE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, send_otp)],
            ASK_OTP:       [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp)],
            ASK_2FA:       [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_2fa)],
            ASK_GAME_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_game_pass)],
            ASK_GAME_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_game_login)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    bot_app.add_handler(conv_handler)
    bot_app.add_handler(CommandHandler("status", status_cmd))
    bot_app.add_error_handler(error_handler)

    logger.info("Bot đang polling...")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()