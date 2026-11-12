import os
import re
import time
import json
import hmac
import hashlib
import base64
import queue
import asyncio
import logging
import threading
from collections import deque
from urllib.parse import parse_qsl

import requests
from flask import Flask, Response, request, jsonify
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIG — set these as environment variables on your host.
# Never put real tokens/passwords in source code.
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")  # Create locally; do not collect it in the Mini App.
TARGET_CHAT = os.getenv("TARGET_CHAT", "codelaucuagame")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "10000"))

# Optional comma-separated Telegram numeric IDs. Empty means any Telegram
# account that opens the genuine bot Mini App can use it.
_allowed = os.getenv("ALLOWED_TG_IDS", "").strip()
ALLOWED_TG_IDS = {x.strip() for x in _allowed.split(",") if x.strip()}

API_GET_ACCESS = "https://apifo88daigia.tele68.com/api"
LOGIN_URL = "https://wlb.tele68.com/v1/lobby/auth/login"
BASE_URL = "https://gameapi.tele68.com"
REDEEM_PATH = "/v1/giftcode/redemption?cp=R&cl=R&pf=web&at="

POLL_INTERVAL = 0.1  # Polls the local queue, not Telegram's servers.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
MAX_RETRY = 2
MAX_RECENT_CODES = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("auto-lc79")

app = Flask(__name__)
code_queue = queue.Queue()
state_lock = threading.RLock()
account_lock = threading.RLock()
recent_codes = deque(maxlen=MAX_RECENT_CODES)
seen_codes = set()
results = deque(maxlen=100)
lc79_account = None
telegram_status = "Chưa kết nối Telegram"
target_chat_id = None
worker_started = False


def md5_password(plain: str) -> str:
    # MD5 is used only because the supplied LC79 API appears to expect it.
    # It is not suitable for storing passwords securely.
    return hashlib.md5(plain.encode("utf-8")).hexdigest()


def verify_telegram_init_data(init_data: str):
    """Verify Telegram Mini App initData using the bot token."""
    if not BOT_TOKEN or not init_data:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(
            b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
            ).digest()
        calculated = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            return None

        user_data = json.loads(parsed.get("user", "{}"))
        user_id = str(user_data.get("id", ""))
        if ALLOWED_TG_IDS and user_id not in ALLOWED_TG_IDS:
            return None
        return user_data
    except Exception:
        return None


@app.before_request
def protect_api():
    if request.path.startswith("/api/"):
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        user = verify_telegram_init_data(init_data)
        if not user:
            return jsonify({
                "ok": False,
                "message": "Không xác thực được Telegram Mini App. Hãy mở từ bot."
            }), 401


class LC79Account:
    def __init__(self, username: str, password_md5: str, nickname: str):
        self.username = username
        self.password_md5 = password_md5
        self.nickname = nickname
        self.jwt = None
        self.jwt_exp = 0
        self.session = requests.Session()
        self.lock = threading.RLock()

    @staticmethod
    def _headers(token=None):
        headers = {
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
            "origin": "https://lc79.to",
            "referer": "https://lc79.to/",
            "accept": "application/json, text/plain, */*",
        }
        if token:
            headers["authorization"] = f"Bearer {token}"
        return headers

    @staticmethod
    def _jwt_exp(token):
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload))
            return int(data.get("exp", 0))
        except Exception:
            return 0

    def refresh_token(self):
        with self.lock:
            try:
                r = self.session.get(
                    API_GET_ACCESS,
                    params={
                        "c": "3",
                        "un": self.username,
                        "pw": self.password_md5,
                        "cp": "R",
                        "cl": "R",
                        "pf": "web",
                        "at": ""
                    },
                    headers=self._headers(),
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
                data = r.json()
                access_token = data.get("accessToken") if data.get("success") else None
                if not access_token:
                    log.warning("LC79 access-token request was rejected.")
                    return False

                login_headers = {
                    "content-type": "application/json",
                    "origin": "https://lc79b.bet",
                    "referer": "https://lc79b.bet/",
                    "user-agent": "Mozilla/5.0",
                }
                r = self.session.post(
                    LOGIN_URL,
                    headers=login_headers,
                    json={
                        "nickName": self.nickname,
                        "accessToken": access_token
                    },
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
                data = r.json()
                token = data.get("token")
                if not token:
                    log.warning("LC79 login did not return a token.")
                    return False

                self.jwt = token
                self.jwt_exp = self._jwt_exp(token)
                return True
            except Exception as exc:
                log.warning("LC79 login error: %s", type(exc).__name__)
                return False

    def ensure_token(self):
        if self.jwt and (not self.jwt_exp or time.time() < self.jwt_exp - 60):
            return True
        return self.refresh_token()

    def redeem(self, code):
        for attempt in range(MAX_RETRY + 1):
            if not self.ensure_token():
                return {"status": "login_error", "message": "Không đăng nhập được LC79."}
            try:
                r = self.session.post(
                    BASE_URL + REDEEM_PATH,
                    headers=self._headers(self.jwt),
                    json={"code": code.strip(), "captcha": ""},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
                )
                try:
                    data = r.json()
                except Exception:
                    data = {}

                if r.status_code == 401 and attempt < MAX_RETRY:
                    self.jwt = None
                    continue

                if r.status_code in (200, 201):
                    amount = (
                        data.get("money") or data.get("amount")
                        or data.get("reward")
                        or (data.get("data") or {}).get("money")
                    )
                    return {
                        "status": "success",
                        "message": "Đổi mã thành công",
                        "amount": amount
                    }

                message = data.get("message", "Yêu cầu bị từ chối") if isinstance(data, dict) else "Yêu cầu bị từ chối"
                return {
                    "status": "failed",
                    "message": str(message)[:180],
                    "http_status": r.status_code
                }
            except requests.RequestException as exc:
                if attempt >= MAX_RETRY:
                    return {"status": "error", "message": type(exc).__name__}
                time.sleep(0.4 * (attempt + 1))

        return {"status": "error", "message": "Không thể đổi mã."}


def add_code(code):
    code = (code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4,40}", code):
        return False

    with state_lock:
        if code in seen_codes:
            return False
        seen_codes.add(code)
        recent_codes.appendleft({
            "code": code,
            "received_at": time.strftime("%H:%M:%S"),
            "status": "Đang chờ"
        })

    code_queue.put(code)
    log.info("Received a new code.")
    return True


def set_code_status(code, status, message="", amount=None):
    item = {
        "code": code,
        "status": status,
        "message": message,
        "amount": amount,
        "updated_at": time.strftime("%H:%M:%S")
    }
    with state_lock:
        for entry in recent_codes:
            if entry["code"] == code:
                entry.update(item)
                break
        results.appendleft(item)


def redemption_worker():
    log.info("Redemption worker started; local queue poll = 0.1s.")
    while True:
        try:
            code = code_queue.get(timeout=POLL_INTERVAL)
        except queue.Empty:
            continue

        try:
            with account_lock:
                account = lc79_account

            if account is None:
                set_code_status(code, "Chưa đăng nhập LC79", "Hãy đăng nhập LC79 trên Mini App.")
                continue

            set_code_status(code, "Đang nhập")
            outcome = account.redeem(code)
            status = outcome.get("status", "error")
            if status == "success":
                set_code_status(code, "Thành công", outcome.get("message", ""), outcome.get("amount"))
            else:
                set_code_status(code, "Thất bại", outcome.get("message", "Không rõ lỗi"))
        except Exception:
            log.exception("Unexpected redemption worker error.")
            set_code_status(code, "Lỗi", "Lỗi nội bộ.")
        finally:
            code_queue.task_done()


# ============================================================
# TELETHON — use a session created locally by the account owner.
# No OTP, 2FA password, or session string is requested by the web page.
# ============================================================
async def telegram_listener():
    global target_chat_id, telegram_status

    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_SESSION):
        telegram_status = "Thiếu cấu hình Telegram session"
        log.warning("Set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_SESSION.")
        return

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        TELEGRAM_API_ID,
        TELEGRAM_API_HASH,
        connection_retries=5,
        retry_delay=3,
        auto_reconnect=True,
        request_retries=5,
        flood_sleep_threshold=60
    )

    await client.start()
    username = TARGET_CHAT.rstrip("/").split("/")[-1].lstrip("@")
    entity = await client.get_entity(username)
    target_chat_id = utils.get_peer_id(entity)
    telegram_status = f"Đã kết nối @{username}"
    log.info("Telegram listener connected.")

    @client.on(events.NewMessage(chats=entity))
    async def on_new_message(event):
        text = event.raw_text or ""
        # Supports both the channel's labelled format and LC79-prefixed codes.
        matches = re.findall(r"Mã code là:\s*([A-Z0-9]+)|\b(LC79[A-Z0-9]+)\b", text, re.I)
        for first, second in matches:
            found = (first or second).upper()
            if found.startswith("LC79"):
                found = found[4:]
            if found:
                add_code(found)

    await client.run_until_disconnected()


def telegram_thread():
    global telegram_status
    try:
        asyncio.run(telegram_listener())
    except Exception as exc:
        telegram_status = "Mất kết nối Telegram"
        log.exception("Telegram listener stopped: %s", type(exc).__name__)


# ============================================================
# API
# ============================================================
@app.get("/health")
def health():
    return "ok", 200


@app.get("/api/status")
def api_status():
    with account_lock:
        account_ready = lc79_account is not None
    with state_lock:
        codes = list(recent_codes)
        history = list(results)[:20]
    return jsonify({
        "ok": True,
        "telegram": telegram_status,
        "lc79_logged_in": account_ready,
        "queue_size": code_queue.qsize(),
        "codes": codes,
        "history": history
    })


@app.post("/api/lc79/login")
def api_lc79_login():
    global lc79_account
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    nickname = str(data.get("nickname", "")).strip()

    if not username or not password or not nickname:
        return jsonify({"ok": False, "message": "Nhập đủ username, mật khẩu và nickname."}), 400
    if len(username) > 100 or len(password) > 200 or len(nickname) > 100:
        return jsonify({"ok": False, "message": "Thông tin đăng nhập quá dài."}), 400

    account = LC79Account(username, md5_password(password), nickname)
    if not account.refresh_token():
        return jsonify({"ok": False, "message": "Đăng nhập LC79 thất bại. Kiểm tra thông tin."}), 401

    with account_lock:
        old = lc79_account
        lc79_account = account
    if old:
        old.session.close()

    return jsonify({"ok": True, "message": "Đăng nhập LC79 thành công."})


@app.post("/api/codes/add")
def api_add_code():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", ""))
    if not add_code(code):
        return jsonify({"ok": False, "message": "Mã không hợp lệ hoặc đã được nhận."}), 409
    return jsonify({"ok": True})


# ============================================================
# MINI APP — light theme, mobile-first
# ============================================================
HTML = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#f7f8fc">
<title>Auto LC79</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#f6f7fb;--card:#fff;--ink:#172033;--muted:#8791a5;--line:#e9edf4;--purple:#7157e8;--green:#13a879;--red:#e05262;--blue:#3985e8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:18px 15px 36px}
.wrap{max-width:560px;margin:auto}.top{padding:17px 3px 22px}.eyebrow{font-size:11px;letter-spacing:1.5px;font-weight:800;color:var(--purple)}h1{font-size:27px;margin:7px 0 5px;letter-spacing:-.8px}.sub{font-size:13px;color:var(--muted)}
.card{background:var(--card);border:1px solid var(--line);border-radius:19px;padding:17px;margin-bottom:13px;box-shadow:0 5px 20px #26365a08}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px}.section{font-weight:800;font-size:14px}.muted{color:var(--muted);font-size:12px}
.pill{font-size:11px;padding:6px 9px;border-radius:20px;background:#f0edff;color:var(--purple);font-weight:700}.pill.ok{background:#e8f8f1;color:var(--green)}.pill.bad{background:#fff0f1;color:var(--red)}
input{width:100%;border:1px solid var(--line);background:#fafbfe;border-radius:12px;padding:13px;margin-top:7px;margin-bottom:12px;font-size:14px;outline:none;color:var(--ink)}
input:focus{border-color:#a99af5}label{font-size:11px;color:#667085;font-weight:750}
button{border:0;border-radius:12px;padding:12px 15px;background:var(--purple);color:white;font-weight:750;font-size:13px;cursor:pointer}
button.secondary{background:#f0edff;color:var(--purple)}button:disabled{opacity:.55}
.full{width:100%}.code{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:12px 0;border-bottom:1px solid var(--line)}.code:last-child{border:0}.code strong{font-family:ui-monospace,monospace;letter-spacing:1px;font-size:14px}.status{font-size:11px;color:var(--muted);margin-top:4px}.empty{text-align:center;padding:23px;color:var(--muted);font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.stat{background:#fafbfe;border:1px solid var(--line);border-radius:13px;padding:12px}.stat b{font-size:17px;display:block;margin-top:5px}.hidden{display:none}
.note{font-size:11px;line-height:1.5;color:var(--muted);margin-top:10px}
</style>
</head>
<body><div class="wrap">
<header class="top"><div class="eyebrow">AUTO TOOL CENTER · VIP</div><h1>LC79 Dashboard</h1><div class="sub">Theo dõi mã và trạng thái đổi thưởng theo thời gian thực</div></header>

<section class="card">
 <div class="row"><div class="section">Trạng thái hệ thống</div><span class="pill" id="overall">Đang kiểm tra</span></div>
 <div class="grid" style="margin-top:14px">
  <div class="stat"><div class="muted">Telegram</div><b id="tgState" style="font-size:13px">Đang kiểm tra</b></div>
  <div class="stat"><div class="muted">LC79</div><b id="lcState" style="font-size:13px">Chưa đăng nhập</b></div>
  <div class="stat"><div class="muted">Mã đang chờ</div><b id="queueCount">0</b></div>
  <div class="stat"><div class="muted">Mã hiển thị</div><b id="codeCount">0</b></div>
 </div>
</section>

<section class="card">
 <div class="row"><div class="section">Đăng nhập LC79</div><span class="pill" id="loginBadge">Chưa đăng nhập</span></div>
 <form id="loginForm" style="margin-top:15px">
  <label>USERNAME</label><input id="username" autocomplete="username" placeholder="Tên đăng nhập LC79" required>
  <label>MẬT KHẨU</label><input id="password" type="password" autocomplete="current-password" placeholder="Mật khẩu LC79" required>
  <label>NICKNAME LC79</label><input id="nickname" placeholder="Nickname dùng cho API" required>
  <button class="full" id="loginBtn">Đăng nhập LC79</button>
 </form>
 <div class="note">Mật khẩu chỉ được gửi đến máy chủ của bạn qua kết nối HTTPS và chuyển MD5 ở server theo định dạng API đang sử dụng. Không ghi mật khẩu vào lịch sử.</div>
</section>

<section class="card">
 <div class="row"><div class="section">10 mã mới nhất</div><span class="pill">AUTO UPDATE</span></div>
 <div id="codes"><div class="empty">Đang chờ mã từ Telegram…</div></div>
</section>

<section class="card">
 <div class="row"><div class="section">Lịch sử đổi mã</div><span class="muted">20 hoạt động gần nhất</span></div>
 <div id="history"><div class="empty">Chưa có hoạt động</div></div>
</section>
<div class="note" style="text-align:center">AUTO LC79 · Mini App</div>
</div>
<script>
const tg = window.Telegram?.WebApp;
if(tg){tg.ready();tg.expand();}
const initData = tg?.initData || "";
const headers = {"Content-Type":"application/json","X-Telegram-Init-Data":initData};
async function api(url,opts={}){const r=await fetch(url,{...opts,headers:{...headers,...(opts.headers||{})}});const d=await r.json();if(!r.ok)throw new Error(d.message||"Yêu cầu thất bại");return d;}
function esc(s){return String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function renderCodes(codes){
 const box=document.getElementById("codes");
 document.getElementById("codeCount").textContent=codes.length;
 if(!codes.length){box.innerHTML='<div class="empty">Đang chờ mã từ Telegram…</div>';return;}
 box.innerHTML=codes.map(x=>`<div class="code"><div><strong>${esc(x.code)}</strong><div class="status">${esc(x.status||"")}</div></div><button class="secondary" onclick="copyCode('${esc(x.code)}')">COPY</button></div>`).join("");
}
function renderHistory(items){
 const box=document.getElementById("history");
 if(!items.length){box.innerHTML='<div class="empty">Chưa có hoạt động</div>';return;}
 box.innerHTML=items.map(x=>`<div class="code"><div><strong>${esc(x.code)}</strong><div class="status">${esc(x.status)} · ${esc(x.message||"")}</div></div><span class="pill ${x.status==="Thành công"?"ok":x.status==="Thất bại"?"bad":""}">${esc(x.updated_at||"")}</span></div>`).join("");
}
async function copyCode(code){try{await navigator.clipboard.writeText(code);if(tg)tg.HapticFeedback?.notificationOccurred("success");}catch(e){alert("Không copy được mã");}}
async function refresh(){
 try{
  const d=await api("/api/status");
  document.getElementById("tgState").textContent=d.telegram;
  document.getElementById("lcState").textContent=d.lc79_logged_in?"Đã đăng nhập":"Chưa đăng nhập";
  document.getElementById("loginBadge").textContent=d.lc79_logged_in?"Đã đăng nhập":"Chưa đăng nhập";
  document.getElementById("loginBadge").className="pill "+(d.lc79_logged_in?"ok":"");
  document.getElementById("queueCount").textContent=d.queue_size;
  document.getElementById("overall").textContent=d.telegram.includes("Đã kết nối")?"Đang hoạt động":"Kiểm tra Telegram";
  renderCodes(d.codes);renderHistory(d.history);
 }catch(e){document.getElementById("overall").textContent=e.message;}
}
document.getElementById("loginForm").addEventListener("submit",async e=>{
 e.preventDefault();const btn=document.getElementById("loginBtn");btn.disabled=true;btn.textContent="Đang đăng nhập…";
 try{
  await api("/api/lc79/login",{method:"POST",body:JSON.stringify({
   username:document.getElementById("username").value.trim(),
   password:document.getElementById("password").value,
   nickname:document.getElementById("nickname").value.trim()
  })});
  document.getElementById("password").value="";alert("Đăng nhập LC79 thành công.");await refresh();
 }catch(err){alert(err.message);}
 finally{btn.disabled=false;btn.textContent="Đăng nhập LC79";}
});
refresh();setInterval(refresh,1000);
</script></body></html>"""


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "bạn"
    if not WEBAPP_URL:
        await update.message.reply_text("Mini App chưa được cấu hình. Hãy đặt WEBAPP_URL.")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 MỞ AUTO LC79", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text(
        f"Chào {name}! Mở Mini App để đăng nhập LC79 và theo dõi mã.",
        reply_markup=keyboard
    )


async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "Mở Auto LC79 Mini App")
    ])


def main():
    global worker_started
    if not BOT_TOKEN:
        raise RuntimeError("Thiếu BOT_TOKEN.")
    if not WEBAPP_URL.startswith("https://"):
        raise RuntimeError("WEBAPP_URL phải là URL HTTPS công khai.")

    if not worker_started:
        threading.Thread(target=redemption_worker, daemon=True).start()
        worker_started = True

    threading.Thread(target=telegram_thread, daemon=True).start()

    bot = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    bot.add_handler(CommandHandler("start", start_command))

    # Flask is served separately; use a production WSGI server in deployment.
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False),
        daemon=True
    ).start()

    log.info("Starting Telegram bot polling.")
    bot.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
