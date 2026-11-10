import base64
import io
import json
import threading
import time
import random
import websocket
import requests
import asyncio
from collections import defaultdict
from datetime import datetime
from PIL import Image
from thuattoan import getSunwinAIPrediction
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)



# =========================================================
# CONFIGURATION
# =========================================================
BOT_TOKEN = "8990113148:AAH1wgUusu_Z3AW2JNF4JwivXAvCDOFLO8U"

BASE = "https://bodergatez.dsrcgoms.net"
SITE = "https://v.hitclub.pl"
FG_ID = "2c2502aa39699bb6ff3e39c06f28fbce"

WS_URL = "wss://mynisketgw.hytsocesk.com/websocket"

USERNAME, PASSWORD, CAPTCHA, CHOOSE_GAME_TYPE, CHOOSE_BET_AMOUNT = range(5)

# =========================================================
# USER DATA MANAGER
# =========================================================
class UserSession:
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.session = requests.Session()
        self.ws = None
        self.ws_running = False
        self.betting_active = False
        self.stop_betting = False
        self.current_game_type = None
        self.current_session_id = None
        self.current_bet_amount = 1000
        self.user_balance = 0
        self.user_fullname = ""
        self.user_access_token = ""
        self.user_session_id = ""
        self.user_username = ""
        self.user_level = ""
        self.user_id = ""
        self.last_bet_side = None
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.bet_history = []
        self.martingale_enabled = True
        self.martingale_multiplier = 2.0
        self.martingale_max_step = 5
        self.current_martingale_step = 0
        self.ai_enabled = True
        self.last_results = []
        self.total_bet = 0
        self.total_win = 0
        self.total_loss = 0
        self.last_ai_reason = ""

user_sessions = {}
session_lock = threading.Lock()

def get_user_session(chat_id):
    with session_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = UserSession(chat_id)
        return user_sessions[chat_id]

# =========================================================
# HEADERS
# =========================================================
HEADERS = {
    "Origin": SITE,
    "Referer": f"{SITE}/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "x-fg-id": FG_ID,
}

# =========================================================
# TELEGRAM THREAD SAFE
# =========================================================
def send_telegram_alert(chat_id, text):
    global tg_loop, tg_bot
    if tg_loop and tg_bot:
        try:
            asyncio.run_coroutine_threadsafe(
                tg_bot.send_message(chat_id=chat_id, text=text),
                tg_loop
            )
        except Exception as e:
            print("SEND TG ERROR:", e)

# =========================================================
# CAPTCHA & LOGIN
# =========================================================
def get_captcha(session_obj):
    url = f"{BASE}/verify/index.aspx"
    payload = {"token": "", "fg_id": FG_ID}
    r = session_obj.post(url, data=payload, headers=HEADERS, timeout=15)
    try:
        data = r.json()
    except Exception as e:
        raise Exception(f"Captcha JSON fail: {e}")
    c = data.get("c", {})
    img_b64 = c.get("b64")
    token = c.get("token")
    msg = data.get("message") or c.get("message") or "Nhập captcha"
    if not img_b64 or not token:
        raise Exception("Missing captcha")
    return img_b64, token, msg

async def normal_login(user_session, username, password, captcha_text, captcha_token):
    try:
        user_session.session.get(SITE, headers=HEADERS, timeout=15)
        user_session.session.get(f"{BASE}/gwms/v1/collect-device.aspx?fg={FG_ID}", headers=HEADERS, timeout=15)

        payload = {
            "username": username, "password": password, "captcha": captcha_text,
            "token": captcha_token, "app_id": "bc114103", "browser": "chrome",
            "device": "Computer", "os": "Windows", "fg": FG_ID, "version": "3.12.2",
            "aff_id": "hit", "csrf": "", "bunleid": "",
        }

        response = user_session.session.post(
            f"{BASE}/user/login.aspx",
            json=payload,
            headers={**HEADERS, "Content-Type": "application/json"},
            timeout=20
        )
        login_data = response.json()

        if login_data.get("code") != 200:
            return {"login": login_data}

        data_list = login_data.get("data", [])
        if not data_list:
            return {"error": "Missing user data"}

        user_data = data_list[0]
        user_session.user_access_token = user_data.get("token", "")
        user_session.user_fullname = user_data.get("fullname", "Unknown")
        user_session.user_username = user_data.get("username", "Unknown")
        user_session.user_level = user_data.get("level", "Unknown")
        user_session.user_id = str(user_data.get("id", "Unknown"))
        user_session.user_balance = int(user_data.get("main_balance", 0))

        return {"success": True, "login": login_data}
    except Exception as e:
        return {"error": str(e)}

# =========================================================
# GET BET DECISION FROM AI
# =========================================================
def get_bet_decision_ai(user_session):
    if not user_session.ai_enabled or len(user_session.last_results) < 3:
        return random.choice([1, 2]), "🎲 Ngẫu nhiên"
    
    # Gọi AI prediction từ file thuattoan
    result = getSunwinAIPrediction(user_session.last_results)
    user_session.last_ai_reason = result.get('reason', '')
    
    if result['prediction'] == 1:
        return 1, f"🤖 AI: TÀI (độ tin cậy {result['confidence']}%) - {result.get('reason', '')}"
    else:
        return 2, f"🤖 AI: XỈU (độ tin cậy {result['confidence']}%) - {result.get('reason', '')}"

def calculate_martingale_bet(user_session, base_bet):
    if not user_session.martingale_enabled or user_session.consecutive_losses == 0:
        return base_bet
    step = min(user_session.consecutive_losses, user_session.martingale_max_step)
    multiplier = user_session.martingale_multiplier ** step
    bet_amount = int(base_bet * multiplier)
    max_bet = int(user_session.user_balance * 0.7)
    return min(bet_amount, max_bet, 50000000)

# =========================================================
# WEBSOCKET FUNCTIONS
# =========================================================
def on_websocket_open(ws_instance, user_session):
    print(f"✅ WS CONNECTED for user {user_session.chat_id}")
    login_packet = [1, "MiniGame", "", "", {
        "agentId": "1",
        "accessToken": user_session.user_access_token,
        "reconnect": False
    }]
    ws_instance.send(json.dumps(login_packet))

def on_websocket_message(ws_instance, message, user_session):
    try:
        data = json.loads(message)
        if not isinstance(data, list) or len(data) == 0:
            return

        # Login response
        if data[0] == 1:
            if len(data) >= 2 and data[1] is not True:
                send_telegram_alert(user_session.chat_id, "❌ Kết nối thất bại!")
                ws_instance.close()
                return
            send_telegram_alert(user_session.chat_id, "✅ Kết nối thành công!")
            ws_instance.send(json.dumps([6, "MiniGame", "lobbyPlugin", {"cmd": 10001}]))
            time.sleep(0.5)
            if user_session.current_game_type == "md5":
                ws_instance.send(json.dumps([7, "MiniGame", "1", 4]))
                time.sleep(0.5)
                ws_instance.send(json.dumps([6, "MiniGame", "taixiuKCBPlugin", {"cmd": 2000}]))
            else:
                ws_instance.send(json.dumps([7, "MiniGame", "1", 6]))
                time.sleep(0.5)
                ws_instance.send(json.dumps([6, "MiniGame", "taixiuPlugin", {"cmd": 1005}]))
            return

        # Extract payload
        payload = None
        if data[0] == 5 and len(data) >= 2:
            payload = data[1]
        elif len(data) >= 4:
            payload = data[3]
        if not isinstance(payload, dict):
            return

        packet_cmd = payload.get("cmd")

        # Update balance
        if packet_cmd == 100:
            try:
                user_session.user_balance = payload["As"]["gold"]
            except:
                pass
            return

        # New session detected
        if (user_session.current_game_type == "md5" and packet_cmd == 2005) or (user_session.current_game_type == "taixiu" and packet_cmd == 1002):
            sid = payload.get("sid")
            if sid and sid != user_session.current_session_id:   
                user_session.current_session_id = sid
                if user_session.betting_active:
                    place_websocket_bet(ws_instance, user_session, sid)
            return

        # Result
        if (user_session.current_game_type == "md5" and packet_cmd == 2006) or (user_session.current_game_type == "taixiu" and packet_cmd == 1004):
            d1 = payload.get("d1", 0)
            d2 = payload.get("d2", 0)
            d3 = payload.get("d3", 0)
            total = d1 + d2 + d3
            if total == 0: return
            win_side = 1 if total >= 11 else 2
            result_str = "TÀI" if win_side == 1 else "XỈU"
            
            user_session.last_results.append(win_side)
            if len(user_session.last_results) > 50:
                user_session.last_results.pop(0)

            bet_status = "⚪ Không cược"
            if user_session.last_bet_side is not None:
                if user_session.last_bet_side == win_side:
                    rate = 1.98 if user_session.current_game_type == "md5" else 1.97
                    total_return = int(user_session.current_bet_amount * rate)
                    user_session.user_balance += total_return
                    profit = total_return - user_session.current_bet_amount
                    bet_status = f"🎉 THẮNG (+{profit:,}đ)"
                    user_session.total_win += profit
                    user_session.consecutive_losses = 0
                    user_session.consecutive_wins += 1
                else:
                    bet_status = f"❌ THUA (-{user_session.current_bet_amount:,}đ)"
                    user_session.total_loss += user_session.current_bet_amount
                    user_session.consecutive_losses += 1
                    user_session.consecutive_wins = 0
            
            user_session.total_bet += user_session.current_bet_amount
            
            send_telegram_alert(
                user_session.chat_id,
                f"🏁 KẾT QUẢ #{user_session.current_session_id}\n"
                f"🎲 {d1} - {d2} - {d3} = {total} | {result_str}\n"
                f"📊 {bet_status}\n"
                f"💰 Dư: {user_session.user_balance:,}đ\n"
                f"📈 Tổng cược: {user_session.total_bet:,}đ | Lời: {user_session.total_win - user_session.total_loss:,}đ"
            )
            user_session.last_bet_side = None 
            return

    except Exception as error:
        print("WS ERROR:", error)

def place_websocket_bet(ws_instance, user_session, session_id):
    if not user_session.betting_active or user_session.stop_betting:
        return

    final_bet_amount = calculate_martingale_bet(user_session, user_session.current_bet_amount)

    if user_session.user_balance < final_bet_amount:
        send_telegram_alert(user_session.chat_id, f"⚠️ Hết tiền! Dư: {user_session.user_balance:,}đ")
        stop_websocket_betting(user_session)
        return

    # Lấy quyết định từ AI
    eid, ai_reason = get_bet_decision_ai(user_session)
    user_session.last_bet_side = eid         
    bet_side = "TÀI" if eid == 1 else "XỈU"

    user_session.user_balance -= final_bet_amount
    user_session.current_bet_amount = final_bet_amount

    strategy_text = ""
    if user_session.martingale_enabled and user_session.consecutive_losses > 0:
        strategy_text = f" [Gấp thếp x{user_session.martingale_multiplier ** user_session.consecutive_losses:.1f}]"

    send_telegram_alert(
        user_session.chat_id,
        f"🎲 CƯỢC #{session_id}{strategy_text}\n"
        f"📌 {bet_side}\n"
        f"🤖 {ai_reason}\n"
        f"💰 Tiền: {final_bet_amount:,}đ\n"
        f"💳 Dư: {user_session.user_balance:,}đ"
    )

    if user_session.current_game_type == "md5":
        packet = [6, "MiniGame", "taixiuKCBPlugin", {
            "cmd": 2002, "b": final_bet_amount, "sid": session_id,
            "aid": 1, "eid": eid, "sqe": True, "a": False
        }]
    else:
        packet = [6, "MiniGame", "taixiuPlugin", {
            "cmd": 1000, "b": final_bet_amount, "sid": session_id,
            "aid": 1, "eid": eid, "sqe": True, "a": False
        }]

    try:
        ws_instance.send(json.dumps(packet))
    except Exception as error:
        print("BET ERROR:", error)

def on_websocket_error(ws_instance, error, user_session):
    send_telegram_alert(user_session.chat_id, f"⚠️ Lỗi: {error}")

def on_websocket_close(ws_instance, code, msg, user_session):
    send_telegram_alert(user_session.chat_id, "🔌 Mất kết nối! Đang kết nối lại...")
    if user_session.betting_active:
        time.sleep(5)
        threading.Thread(target=start_websocket_betting, args=(user_session,), daemon=True).start()

def start_websocket_betting(user_session):
    user_session.betting_active = True
    user_session.stop_betting = False
    websocket.enableTrace(False)
    user_session.ws = websocket.WebSocketApp(
        WS_URL,
        header=[f"Origin: {SITE}", f"User-Agent: {HEADERS['User-Agent']}"],
        on_open=lambda ws: on_websocket_open(ws, user_session),
        on_message=lambda ws, msg: on_websocket_message(ws, msg, user_session),
        on_error=lambda ws, err: on_websocket_error(ws, err, user_session),
        on_close=lambda ws, code, msg: on_websocket_close(ws, code, msg, user_session),
    )
    thread = threading.Thread(target=lambda: user_session.ws.run_forever(ping_interval=15, ping_timeout=5))
    thread.daemon = True
    thread.start()
    return True

def stop_websocket_betting(user_session):
    user_session.betting_active = False
    user_session.stop_betting = True
    if user_session.ws:
        user_session.ws.close()

# =========================================================
# TELEGRAM BOT HANDLERS
# =========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global tg_loop, tg_bot
    tg_loop = asyncio.get_running_loop()
    tg_bot = context.application.bot
    chat_id = update.effective_chat.id
    get_user_session(chat_id)

    await update.message.reply_text(
        "🤖 HIT CLUB AI BET BOT 🤖\n━━━━━━━━━━━━━━━━━━━━━\n"
        "🧠 15+ thuật toán AI: Markov, Fibonacci, RSI, KNN, Bayes, GARCH, LSTM...\n"
        "📈 Gấp thếp thông minh\n"
        "⚡ Auto cược sau mỗi phiên\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n🔐 Nhập tài khoản:"
    )
    return USERNAME

async def get_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["username"] = update.message.text
    await update.message.reply_text("🔒 Nhập mật khẩu:")
    return PASSWORD

async def get_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["password"] = update.message.text
    chat_id = update.effective_chat.id
    user_session = get_user_session(chat_id)
    try:
        img_b64, token, msg = get_captcha(user_session.session)
        context.user_data["captcha_token"] = token
        img_bytes = base64.b64decode(img_b64)
        img = Image.open(io.BytesIO(img_bytes))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        await update.effective_message.reply_photo(photo=buf, caption=f"🔐 CAPTCHA\n{msg}\n\n📝 Nhập mã:")
        return CAPTCHA
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {e}")
        return ConversationHandler.END

async def get_captcha_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    captcha_text = update.message.text.strip()
    username = context.user_data["username"]
    password = context.user_data["password"]
    captcha_token = context.user_data["captcha_token"]
    chat_id = update.effective_chat.id
    user_session = get_user_session(chat_id)

    await update.message.reply_text("⏳ Đang đăng nhập...")
    result = await normal_login(user_session, username, password, captcha_text, captcha_token)

    if result.get("error"):
        await update.message.reply_text(f"❌ Lỗi: {result['error']}")
        return ConversationHandler.END

    login_data = result.get("login")
    if login_data.get("code") != 200:
        if "captcha" in str(login_data.get("message", "")).lower():
            await update.message.reply_text("❌ Sai captcha!")
            return await get_password(update, context)
        await update.message.reply_text(f"❌ Login fail: {login_data}")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("🎲 TÀI XỈU THƯỜNG 🟢", callback_data="taixiu")],
        [InlineKeyboardButton("🎲 TÀI XỈU MD5 🔴", callback_data="md5")],
    ]
    await update.message.reply_text(
        f"✅ ĐĂNG NHẬP THÀNH CÔNG ✅\n\n"
        f"👤 {user_session.user_fullname}\n"
        f"💰 Dư: {user_session.user_balance:,}đ\n\n"
        f"🎮 CHỌN BÀN:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CHOOSE_GAME_TYPE

async def choose_game_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    game_type = query.data
    context.user_data["game_type"] = game_type
    chat_id = update.effective_chat.id    
    user_session = get_user_session(chat_id)
    user_session.current_game_type = game_type

    await query.edit_message_text(
        f"✅ Đã chọn: {'TÀI XỈU THƯỜNG' if game_type == 'taixiu' else 'TÀI XỈU MD5'}\n\n"
        f"💰 Dư: {user_session.user_balance:,}đ\n"
        f"⚡ Tối thiểu: 1,000đ\n\n"
        f"📝 Nhập tiền cược mỗi ván:"
    )
    return CHOOSE_BET_AMOUNT

async def get_bet_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_session = get_user_session(chat_id)
    try:
        bet_amount = int(update.message.text.replace(",", "").replace(".", "").strip())
    except:
        await update.message.reply_text("❌ Nhập số hợp lệ!")
        return CHOOSE_BET_AMOUNT

    if bet_amount < 1000:
        await update.message.reply_text("❌ Tối thiểu 1,000đ!")
        return CHOOSE_BET_AMOUNT
    if bet_amount > user_session.user_balance:
        await update.message.reply_text(f"❌ Không đủ tiền! Dư: {user_session.user_balance:,}đ")
        return CHOOSE_BET_AMOUNT

    user_session.current_bet_amount = bet_amount
    user_session.total_bet = 0
    user_session.total_win = 0
    user_session.total_loss = 0
    user_session.consecutive_losses = 0
    user_session.last_results = []

    await update.message.reply_text(
        f"🚀 KHỞI ĐỘNG AI BET 🚀\n\n"
        f"🎲 Bàn: {'TÀI XỈU THƯỜNG' if user_session.current_game_type == 'taixiu' else 'TÀI XỈU MD5'}\n"
        f"💰 Vốn: {user_session.user_balance:,}đ\n"
        f"💵 Mỗi ván: {bet_amount:,}đ\n"
        f"🧠 AI: 15+ thuật toán\n"
        f"📈 Gấp thếp: {'BẬT' if user_session.martingale_enabled else 'TẮT'}\n\n"
        f"⏳ Đang kết nối..."
    )
    
    start_websocket_betting(user_session)
    await update.message.reply_text("✅ AI BET ĐÃ CHẠY\n🛑 Dùng /stopbet để dừng")
    return ConversationHandler.END

async def stop_bet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_session = get_user_session(chat_id)
    stop_websocket_betting(user_session)
    await update.message.reply_text(
        f"🛑 ĐÃ DỪNG 🛑\n\n"
        f"💰 Dư cuối: {user_session.user_balance:,}đ\n"
        f"📈 Tổng cược: {user_session.total_bet:,}đ\n"
        f"🎉 Thắng: {user_session.total_win:,}đ\n"
        f"❌ Thua: {user_session.total_loss:,}đ\n"
        f"📊 Lợi nhuận: {user_session.total_win - user_session.total_loss:,}đ"
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Đã hủy.")
    return ConversationHandler.END

# =========================================================
# MAIN
# =========================================================
tg_loop = None
tg_bot = None

def main():
    global tg_loop, tg_bot
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_bot = app.bot
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_username)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
            CAPTCHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_captcha_text)],
            CHOOSE_GAME_TYPE: [CallbackQueryHandler(choose_game_type)],
            CHOOSE_BET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_bet_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)]
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("stopbet", stop_bet_command))

    print("🤖 AI BET BOT ĐANG CHẠY...")
    app.run_polling()

if __name__ == "__main__":
    main()