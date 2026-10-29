"""
Tech BD Income — Telegram Bot  (bot.py)  — একটাই ফাইল

২৪ ঘন্টা চলে: Withdraw নোটিফিকেশন, Refer, Broadcast, DM নোটিফিকেশন, Telegram লিংক, চ্যানেল/গ্রুপ জয়েন যাচাই।
অ্যাডমিন প্যানেল খোলা রাখতে হয় না।

─── Render.com এ চালানো (ফ্রি) ───────────────────────────────────────────
 1. GitHub এ একটা PRIVATE repo বানান। শুধু এই একটা ফাইল দিন:  bot.py
    ⚠ serviceAccountKey.json / Token কখনো GitHub এ দেবেন না (দিলে Google চাবি নিজেই বন্ধ করে দেয়)।
 2. Render → New → Web Service → repo বাছুন →  Build Command:  pip install firebase-admin requests
                                                Start Command:  python bot.py
 3. Environment → Add Environment Variable:
       FIREBASE_KEY_JSON  =  Firebase চাবির .json ফাইলের পুরো লেখা (একদম সব)
       (ঐচ্ছিক)  BOT_TOKEN, CHANNEL_ID  — অ্যাডমিন প্যানেলে সেভ করা থাকলে লাগবে না
 4. Deploy শেষে "Live" হলে Render এর URL কপি করুন। ফ্রি প্ল্যানে ১৫ মিনিট কোনো ভিজিট না এলে ঘুমিয়ে যায়,
    তাই uptimerobot.com এ একটা HTTP monitor বানান (প্রতি ৫ মিনিট, ওই URL)। বট নিজেও নিজেকে পিং করে।
 5. অ্যাডমিন প্যানেল → Settings → Telegram Bot → Token/Channel সেভ → "bot.py Mode চালু করুন"।

─── নিজের কম্পিউটার/VPS এ চালানো ──────────────────────────────────────────
    serviceAccountKey.json এই ফাইলের পাশে রাখুন, তারপর:   python3 bot.py     (Windows: py bot.py)
    প্রথমবার লাইব্রেরি নিজে ইনস্টল হয়। ব্যাকগ্রাউন্ডে রাখতে:  nohup python3 bot.py > bot.log 2>&1 &

─── অন্যান্য ───────────────────────────────────────────────────────────────
  • বটকে আপনার চ্যানেলের Admin বানান (মেসেজ পোস্ট ও এডিট)।
  • Firebase চাবির জায়গা (যেটা আগে পায় সেটা নেয়): FIREBASE_KEY_JSON (লেখা বা base64) →
    GOOGLE_APPLICATION_CREDENTIALS (ফাইলের পথ) → ./serviceAccountKey.json → /etc/secrets/serviceAccountKey.json
  • ঐচ্ছিক ফাইল bot_config.json:  {"token": "...", "channelId": "-100...", "pyMode": true}
    অ্যাডমিন প্যানেলে যে মান ফাঁকা সেটা এখান/Environment থেকে ভরা হয়; প্যানেলের মান কখনো বদলায় না।
"""
import subprocess
import sys


def _ensure_deps():
    try:
        import requests, firebase_admin  # noqa: F401
        return
    except ImportError:
        pass
    print("📦 দরকারি লাইব্রেরি ইনস্টল হচ্ছে (firebase-admin, requests)... একটু অপেক্ষা করুন", flush=True)
    base = [sys.executable, "-m", "pip", "install", "-q", "firebase-admin", "requests"]
    try:
        subprocess.check_call(base)
    except subprocess.CalledProcessError:
        subprocess.check_call(base + ["--break-system-packages"])      # newer Linux / Termux
    import os
    os.execv(sys.executable, [sys.executable] + sys.argv)


_ensure_deps()

import base64
import hashlib
import hmac
import html
import json
import os
import re
import signal
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore

VERSION = "1.4.1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILES = [p for p in (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), os.path.join(BASE_DIR, "serviceAccountKey.json"), "/etc/secrets/serviceAccountKey.json") if p]
OFFSET_FILE = os.path.join(BASE_DIR, ".bot_offset")
CONFIG_FILE = os.path.join(BASE_DIR, "bot_config.json")
PING_MAX_AGE = 300          # seconds: older "new message" alerts are dropped
CFG_TTL = 30                # seconds between config re-reads
SEND_GAP = 0.04             # ≈ 25 messages / second (Telegram limit is 30)

try:                         # new Firestore clients prefer FieldFilter
    from google.cloud.firestore_v1.base_query import FieldFilter

    def where(q, field, op, value):
        return q.where(filter=FieldFilter(field, op, value))
except Exception:            # older clients / test doubles
    def where(q, field, op, value):
        return q.where(field, op, value)


HEALTH = {"ok": False, "state": "starting", "note": "", "version": VERSION}
_FB_READY = [False]


class BotSetupError(Exception):
    """A problem the owner must fix (bad key, missing key ...). The message is shown in plain words."""


def _fix_key(d: dict) -> dict:
    pk = d.get("private_key") or ""
    if "\\n" in pk and "\n" not in pk:           # key pasted with literal backslash-n characters
        d["private_key"] = pk.replace("\\n", "\n")
    return d


def load_service_account():
    """Return a path (str) or a dict for firebase_admin.credentials.Certificate()."""
    raw = (os.environ.get("FIREBASE_KEY_JSON") or "").strip()
    if raw:
        for attempt in (raw, None):
            try:
                txt = raw if attempt else base64.b64decode(raw).decode("utf-8")
                d = json.loads(txt)
                if isinstance(d, dict) and d.get("private_key") and d.get("client_email"):
                    return _fix_key(d)
            except Exception:
                continue
        raise BotSetupError("FIREBASE_KEY_JSON পড়া যাচ্ছে না। .json ফাইলের পুরো লেখা (শুরুর { থেকে শেষের } পর্যন্ত) হুবহু বসান।")
    for p in KEY_FILES:
        if os.path.exists(p):
            return p
    raise BotSetupError("Firebase চাবি পাওয়া যায়নি। Render এ Environment Variable FIREBASE_KEY_JSON দিন "
                        "(অথবা নিজের কম্পিউটারে serviceAccountKey.json এই ফাইলের পাশে রাখুন)। "
                        "চাবি পাবেন: Firebase Console → Project settings → Service accounts → Generate new private key")


def key_info(src):
    """(where the key came from, first 8 characters of its id) - safe to print, helps to spot an old key."""
    try:
        if isinstance(src, dict):
            return "FIREBASE_KEY_JSON", str(src.get("private_key_id") or "?")[:8]
        with open(src, encoding="utf-8") as f:
            return "ফাইল " + os.path.basename(str(src)), str(json.load(f).get("private_key_id") or "?")[:8]
    except Exception:
        return "?", "?"


def explain_firebase_error(e: Exception) -> str:
    m = str(e)
    if "invalid_grant" in m or "Invalid JWT" in m or "invalid_client" in m:
        return ("Firebase চাবি কাজ করছে না (Invalid JWT Signature)। চাবিটা বাতিল হয়েছে বা নষ্ট। "
                "GitHub এ চাবি আপলোড হলে Google নিজেই সেটা বন্ধ করে দেয়। সমাধান: Firebase Console → Project settings → "
                "Service accounts → Generate new private key দিয়ে নতুন চাবি বানান এবং শুধু Render এর FIREBASE_KEY_JSON এ বসান "
                "(GitHub এ রাখবেন না)। তারপর Render এ Manual Deploy দিন।")
    if "PERMISSION_DENIED" in m or "403" in m:
        return "Firebase চাবির অনুমতি নেই বা Firestore চালু নেই: " + m[:160]
    return "Firebase এর সাথে যুক্ত হওয়া যায়নি: " + m[:200]


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(HEALTH, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def start_health_server():
    """Render (Web Service) needs an open port, and uptime monitors need something to visit."""
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", int(port)), _Health)
    except OSError as e:
        print("health server:", e, flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("🌐 Health server চালু (port %s)" % port, flush=True)
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if url:                                          # ping ourselves so the free instance is not put to sleep
        def keep():
            while True:
                time.sleep(540)
                try:
                    requests.get(url, timeout=20)
                except Exception:
                    pass
        threading.Thread(target=keep, daemon=True).start()


BST = timezone(timedelta(hours=6))


def mask_name(n) -> str:
    n = str(n or "").strip()
    return (n[0] + "***" + n[-1] if len(n) > 2 else n[0] + "***") if n else "User"


def mask_acct(a) -> str:
    a = re.sub(r"\s+", "", str(a or ""))
    return a[:3] + "****" + a[-3:] if len(a) >= 8 else ("****" + a[-2:] if len(a) >= 3 else "****")


def fmt_when(ts) -> str:
    dt = ts if isinstance(ts, datetime) else datetime.now(timezone.utc)
    return dt.astimezone(BST).strftime("%d %b %Y, %I:%M %p") + " (BST)"


def esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=False)


def bdt(n) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        v = 0.0
    return "BDT=%.2f(💸)" % v


def num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class TgError(Exception):
    def __init__(self, description, code=None, retry=None):
        super().__init__(description)
        self.code, self.retry = code, retry


class Bot:
    def __init__(self):
        src = load_service_account()
        where, kid = key_info(src)
        print("🔑 Firebase চাবি: %s, ID: %s…" % (where, kid), flush=True)
        cred = credentials.Certificate(src)
        try:
            chk = getattr(cred, "get_access_token", None)
            if chk:
                chk()                      # fails at once (not after 5 minutes) if the key is revoked / damaged
            if not _FB_READY[0]:
                firebase_admin.initialize_app(cred)
                _FB_READY[0] = True
            self.db = firestore.client()
        except BotSetupError:
            raise
        except Exception as e:
            raise BotSetupError(explain_firebase_error(e) + "  [চাবির উৎস: %s, ID: %s…]" % (where, kid))
        self.http = requests.Session()
        self.cfg, self.cfg_t = {}, 0.0
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.unsubs, self.active = [], False
        self.admins = (0.0, set())
        self.logbuf, self.state, self.note = [], "starting", ""
        self.stats = {"withdrawNew": 0, "withdrawDone": 0, "refers": 0, "pings": 0, "links": 0, "broadcast": 0, "joinChecks": 0}
        self.send_lock, self.last_send = threading.Lock(), 0.0
        self.busy_bc = set()
        self.offset = 0
        try:
            self.bootstrap()
        except Exception as e:
            print("bootstrap:", e, flush=True)
        try:
            self.offset = int(open(OFFSET_FILE).read().strip() or 0)
        except Exception:
            pass
        try:                                   # survives restarts / redeploys on hosts without a permanent disk
            self.offset = max(self.offset, int((self.col("private").document("botState").get().to_dict() or {}).get("offset") or 0))
        except Exception:
            pass

    # ───────────────────────── helpers ─────────────────────────
    def file_cfg(self) -> dict:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def env_cfg(self) -> dict:
        d = {}
        if os.environ.get("BOT_TOKEN"):
            d["token"] = os.environ["BOT_TOKEN"].strip()
        if os.environ.get("CHANNEL_ID"):
            d["channelId"] = os.environ["CHANNEL_ID"].strip()
        if os.environ.get("ENABLE_PY_MODE", "").lower() in ("1", "true", "yes"):
            d["pyMode"] = True
        return d

    def bootstrap(self):
        """Copy Token / Channel / bot.py Mode from bot_config.json into the admin-panel settings (only where empty)."""
        f = {**self.file_cfg(), **self.env_cfg()}
        if not f:
            return
        ref = self.col("private").document("bot")
        cur = ref.get().to_dict() or {}
        upd = {}
        if f.get("token") and not cur.get("token"):
            upd["token"] = str(f["token"]).strip()
        if f.get("channelId") and not cur.get("channelId"):
            upd["channelId"] = str(f["channelId"]).strip()
        if f.get("pyMode") is True and "pyMode" not in cur:
            upd["pyMode"] = True
        if upd:
            ref.set(upd, merge=True)
            self.log("⚙ অ্যাডমিন প্যানেলের ফাঁকা সেটিং ভরা হয়েছে: %s" % ", ".join(sorted(upd)))

    def log(self, msg: str):
        msg = re.sub(r"bot\d{6,12}:[A-Za-z0-9_-]{30,}", "bot***", str(msg))     # never print the token into logs
        line = "%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        self.logbuf.append(line)
        del self.logbuf[:-40]

    def col(self, name):
        return self.db.collection(name)

    def load_cfg(self, force=False) -> dict:
        if not force and self.cfg and time.time() - self.cfg_t < CFG_TTL:
            return self.cfg
        c = self.col("private").document("bot").get().to_dict() or {}
        st = self.col("settings").document("app").get().to_dict() or {}
        fc = self.file_cfg()
        self.cfg = {
            "token": os.environ.get("BOT_TOKEN") or c.get("token") or fc.get("token") or "",
            "channel": str(os.environ.get("CHANNEL_ID") or c.get("channelId") or fc.get("channelId") or ""),
            "username": c.get("botUsername") or "",
            "py": c.get("pyMode") is True,
            "proof": str(c.get("proofChannel") or ""),
            "proof_pinned": c.get("proofPinned") is True,
            "app_url": st.get("appUrl") or "",
            "ref_reward": num(st.get("refReward")),
        }
        self.cfg_t = time.time()
        return self.cfg

    def tg(self, method: str, **params):
        if method != "getUpdates":
            with self.send_lock:
                wait = SEND_GAP - (time.time() - self.last_send)
                if wait > 0:
                    time.sleep(wait)
                self.last_send = time.time()
        try:
            r = self.http.post("https://api.telegram.org/bot%s/%s" % (self.cfg["token"], method), json=params, timeout=(10, 40))
        except requests.RequestException as e:    # no internet / DNS / timeout (message would contain the token, so keep only the type)
            raise TgError("network error: %s" % type(e).__name__)
        try:
            j = r.json()
        except ValueError:
            j = {}
        if not j.get("ok"):
            raise TgError(j.get("description") or "HTTP %s" % r.status_code, j.get("error_code"), (j.get("parameters") or {}).get("retry_after"))
        return j["result"]

    def try_tg(self, method: str, **params):
        try:
            return self.tg(method, **params)
        except TgError as e:
            self.log("⚠ %s: %s" % (method, e))
            return None

    def tg_of(self, uid):
        d = self.col("tgLinks").document(str(uid)).get()
        return d.to_dict() if d.exists else None

    def tg_dm(self, uid, link: dict, **payload) -> bool:
        try:
            self.tg("sendMessage", chat_id=link.get("tgId"), **payload)
            return True
        except TgError as e:
            self.log("❌ DM যায়নি (%s): %s" % (link.get("name") or link.get("tgName") or link.get("tgId"), e))
            if e.code in (403, 400):
                self.col("tgLinks").document(str(uid)).set({"started": False}, merge=True)
            return False

    def app_kb(self) -> dict:
        u = self.cfg.get("app_url") or ""
        if u.startswith("https://"):
            return {"reply_markup": {"inline_keyboard": [[{"text": "🚀 Open App", "web_app": {"url": u}}]]}}
        return {}

    def claim(self, ref, field: str) -> bool:
        """Take a flag exactly once, even if two copies of the bot are running."""
        @firestore.transactional
        def run(tx):
            s = ref.get(transaction=tx)
            if not s.exists or (s.to_dict() or {}).get(field):
                return False
            tx.update(ref, {field: True})
            return True
        return run(self.db.transaction())

    def safe(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:  # never let one bad document stop the bot
            self.log("❌ %s: %s" % (getattr(fn, "__name__", "task"), e))

    # ───────────────────────── balance ─────────────────────────
    def user_bal(self, uid, exclude_id=None) -> float:
        def total(col, field):
            return sum(num((d.to_dict() or {}).get(field)) for d in where(self.col(col), "uid", "==", uid).stream())
        earned = sum(total(c, "reward") for c in ("taskDone", "adViews", "bonuses", "promoUse", "gameWins"))
        earned += sum(total(c, "amount") for c in ("refBonuses", "adjustments"))
        spent = 0.0
        for d in where(self.col("withdrawals"), "uid", "==", uid).stream():
            x = d.to_dict() or {}
            if d.id != exclude_id and x.get("status") != "rejected":
                spent += num(x.get("amount"))
        return round(earned - spent, 2)

    # ───────────────────────── public payment proof ─────────────────────────
    def publish_payout(self, wid: str, w: dict):
        """Create the public record of an approved payout (once). Also posts it to the public proof channel."""
        ref = self.col("payoutsFeed").document(wid)
        data = {"name": mask_name(w.get("name")), "acct": mask_acct(w.get("address")), "method": str(w.get("method") or ""),
                "amount": num(w.get("amount")), "ref": wid[:6].upper(), "txid": str(w.get("txid") or "").strip()[:40],
                "ts": w.get("reviewedAt") if isinstance(w.get("reviewedAt"), datetime) else firestore.SERVER_TIMESTAMP}
        try:
            ref.create(data)
        except Exception as e:
            if type(e).__name__ != "AlreadyExists":
                raise
            upd = {"acct": data["acct"], "ref": data["ref"], "method": data["method"]}
            if data["txid"]:
                upd["txid"] = data["txid"]
            ref.set(upd, merge=True)
            return False
        self.col("settings").document("payStats").set({"totalAmount": firestore.Increment(data["amount"]), "count": firestore.Increment(1), "last": firestore.SERVER_TIMESTAMP}, merge=True)
        if self.cfg.get("proof"):
            self.try_tg("sendMessage", chat_id=self.cfg["proof"], parse_mode="HTML", disable_web_page_preview=True,
                        text="✅ <b>Payment Sent</b>\n👤 %s\n🏦 %s • %s\n💰 <b>BDT %.2f</b>\n🕒 %s\n🧾 Ref #%s%s"
                        % (esc(data["name"]), esc(data["method"]), esc(data["acct"]), data["amount"], fmt_when(data["ts"]), data["ref"],
                           (" • Txn: <code>%s</code>" % esc(data["txid"])) if data["txid"] else ""))
        return True

    def ensure_proof_intro(self):
        """Post and pin one explanation message in the public proof channel (only once)."""
        c = self.cfg
        if not c.get("proof") or c.get("proof_pinned"):
            return
        m = self.try_tg("sendMessage", chat_id=c["proof"], parse_mode="HTML",
                        text="✅ <b>Payment Proofs</b>\n\nEvery withdrawal paid by Tech BD Income is posted here automatically after it is sent: "
                             "masked name, payment method, masked account, amount, date, reference and transaction ID.")
        if m:
            self.try_tg("pinChatMessage", chat_id=c["proof"], message_id=m["message_id"], disable_notification=True)
            self.col("private").document("bot").set({"proofPinned": True}, merge=True)
            self.cfg["proof_pinned"] = True

    # ───────────────────────── withdrawals ─────────────────────────
    def tg_withdraw(self, snap):
        w, ref, cfg = snap.to_dict() or {}, snap.reference, self.cfg
        if w.get("status") == "pending" and not w.get("tgSent"):
            if not self.claim(ref, "tgSent"):
                return
            bal = self.user_bal(w.get("uid"), snap.id)
            flagged = num(w.get("amount")) > bal + 0.005
            link = self.tg_of(w.get("uid"))
            if not link:
                self.log("ℹ ইউজারের Telegram যুক্ত নেই (সে Telegram এর ভেতর থেকে অ্যাপ খোলেনি), তাই ইনবক্সে মেসেজ যাবে না: %s" % (w.get("name") or w.get("uid")))
            text = ("💸 <b>New Withdraw Request</b>\n\n👤 %s%s\n🆔 <code>%s</code>\n💰 Amount: <b>%s</b>\n🏦 Method: <b>%s</b>\n📮 Address: <code>%s</code>\n📊 Verified balance (আগে): %s"
                    % (esc(w.get("name") or "User"), (" (@%s)" % esc(link["tgName"])) if link and link.get("tgName") else "", esc(w.get("uid")),
                       bdt(w.get("amount")), esc(w.get("method")), esc(w.get("address")), bdt(bal)))
            if flagged:
                text += "\n\n⚠️ <b>WARNING:</b> চাওয়া পরিমাণ যাচাইকৃত ব্যালেন্সের চেয়ে বেশি!"
            sent = self.try_tg("sendMessage", chat_id=cfg["channel"], text=text, parse_mode="HTML",
                               reply_markup={"inline_keyboard": [[{"text": "✅ Approve", "callback_data": "wd:a:" + snap.id}, {"text": "❌ Reject", "callback_data": "wd:r:" + snap.id}]]})
            ref.update({"verifiedBalance": bal, "flagged": flagged, "chText": text, "chMsgId": sent["message_id"] if sent else None})
            if link:
                self.tg_dm(w.get("uid"), link, parse_mode="HTML", text="⏳ <b>আপনার Withdraw অনুরোধ জমা হয়েছে</b>\n\n💰 %s\n🏦 %s — <code>%s</code>\n\nঅ্যাডমিন যাচাই করে শীঘ্রই জানাবে।" % (bdt(w.get("amount")), esc(w.get("method")), esc(w.get("address"))))
            self.stats["withdrawNew"] += 1
            self.log("💸 Withdraw %s — %s" % (bdt(w.get("amount")), w.get("name") or w.get("uid")))
        if w.get("status") in ("approved", "rejected") and not w.get("tgDone"):
            if not self.claim(ref, "tgDone"):
                return
            ok = w.get("status") == "approved"
            link = self.tg_of(w.get("uid"))
            if link:
                if ok:
                    msg = "✅ <b>Withdraw Approved!</b>\n\n💰 %s\n🏦 %s — <code>%s</code>\n\nপেমেন্ট পাঠানো হয়েছে। ধন্যবাদ!" % (bdt(w.get("amount")), esc(w.get("method")), esc(w.get("address")))
                else:
                    msg = "❌ <b>Withdraw Rejected</b>\n\n💰 %s\n%s\nটাকা আপনার ব্যালেন্সে ফেরত এসেছে।" % (bdt(w.get("amount")), ("📝 কারণ: %s\n" % esc(w["note"])) if w.get("note") else "")
                self.tg_dm(w.get("uid"), link, parse_mode="HTML", text=msg)
            if ok:
                try:
                    self.publish_payout(snap.id, w)
                except Exception as e:
                    self.log("⚠ payment proof: %s" % e)
            fresh = ref.get().to_dict() or {}
            if fresh.get("chMsgId") and fresh.get("chText"):
                self.try_tg("editMessageText", chat_id=cfg["channel"], message_id=fresh["chMsgId"], parse_mode="HTML",
                            text=fresh["chText"] + "\n\n%s %s" % ("✅ <b>APPROVED</b>" if ok else "❌ <b>REJECTED</b>", esc(fresh.get("reviewedVia") or "(admin panel)")),
                            reply_markup={"inline_keyboard": []})
            self.stats["withdrawDone"] += 1
            self.log("%s Withdraw %s: %s" % ("✅" if ok else "❌", w.get("status"), w.get("name") or w.get("uid")))

    # ───────────────────────── pings / link requests ─────────────────────────
    def tg_ping(self, snap):
        p = snap.to_dict() or {}
        ts = p.get("ts")
        age = (datetime.now(timezone.utc) - ts).total_seconds() if isinstance(ts, datetime) else 0
        if age > PING_MAX_AGE:
            snap.reference.delete()
            return
        link = self.tg_of(p.get("to"))
        if link and link.get("started"):
            label = {"msg": "💬 নতুন মেসেজ এসেছে", "call": "📞 অডিও কল আসছে", "vcall": "🎥 ভিডিও কল আসছে"}.get(p.get("kind"), "🔔 নতুন নোটিফিকেশন")
            self.try_tg("sendMessage", chat_id=link.get("tgId"), parse_mode="HTML",
                        text="%s\n\n👤 %s থেকে।\nএখনই অ্যাপ খুলুন।" % (label, esc(p.get("fromName") or "একজন ইউজার")), **self.app_kb())
            self.stats["pings"] += 1
        snap.reference.delete()

    def verify_init(self, init_data):
        d = dict(urllib.parse.parse_qsl(str(init_data or ""), keep_blank_values=True))
        h = d.pop("hash", None)
        if not h:
            return None
        check = "\n".join("%s=%s" % (k, v) for k, v in sorted(d.items()))
        secret = hmac.new(b"WebAppData", self.cfg["token"].encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, h) or time.time() - num(d.get("auth_date")) > 7 * 86400:
            return None
        try:
            return json.loads(d.get("user") or "")
        except ValueError:
            return None

    def tg_link_req(self, snap):
        uid = snap.id
        u = self.verify_init((snap.to_dict() or {}).get("initData"))
        if u and u.get("id"):
            tg_id = str(u["id"])
            old = list(where(self.col("tgLinks"), "tgId", "==", tg_id).stream())
            started = False
            try:
                self.tg("sendChatAction", chat_id=tg_id, action="typing")
                started = True
            except TgError:
                pass
            b = self.db.batch()
            for d in old:
                if d.id != uid:
                    b.delete(d.reference)
            photo = u.get("photo_url") or ""
            b.set(self.col("tgLinks").document(uid), {
                "tgId": tg_id, "tgName": u.get("username") or "",
                "name": " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x),
                "photo": photo if str(photo).startswith("https://") else "", "started": started, "ts": firestore.SERVER_TIMESTAMP}, merge=True)
            b.commit()
            self.stats["links"] += 1
            self.log("🔗 Telegram linked: %s (%s) — ইনবক্সে মেসেজ %s" % (u.get("first_name") or "", tg_id, "যাবে ✅" if started else "যাবে না ❌ (বটে START দিতে হবে)"))
        else:
            self.log("⚠ Telegram যাচাই ব্যর্থ (Token ঠিক আছে? অ্যাপ Telegram এর ভেতর থেকে খোলা?)")
        snap.reference.delete()

    # ───────────────────────── join gate (channels / groups set in the admin panel) ─────────────────────────
    def join_check(self, snap):
        """The app asks 'did this user join everything?'. Answer is written to joinRes/<uid> (the app waits for it)."""
        uid, req = snap.id, snap.to_dict() or {}
        nonce = str(req.get("nonce") or "")
        if not nonce:
            return
        ts = req.get("ts")
        if isinstance(ts, datetime) and (datetime.now(timezone.utc) - ts).total_seconds() > 180:
            return                                              # an old request (bot was offline) - the app already gave up
        res_ref = self.col("joinRes").document(uid)
        old = res_ref.get().to_dict() or {}
        if old.get("nonce") == nonce:
            return                                              # already answered
        cfg = self.col("settings").document("join").get().to_dict() or {}
        items = [x for x in (cfg.get("items") or [])][:10]
        out = {"nonce": nonce, "ok": False, "missing": [], "unverifiable": [], "ver": cfg.get("ver") or 0, "ts": firestore.SERVER_TIMESTAMP}
        user = self.verify_init(req.get("initData")) if req.get("initData") else None
        tg_id = int(user["id"]) if user and user.get("id") else None
        if tg_id is None:
            link = self.tg_of(uid)
            if link and str(link.get("tgId") or "").isdigit() and not req.get("initData"):
                tg_id = int(link["tgId"])
        if tg_id is None:
            out["error"] = "no-telegram"
            res_ref.set(out)
            self.log("⚠ জয়েন যাচাই: Telegram পরিচয় পাওয়া যায়নি (%s…)" % uid[:8])
            return
        for i, it in enumerate(items):
            chat = str((it or {}).get("chat") or "").strip()
            if not chat:
                out["unverifiable"].append(i)                   # no @username / id given in the panel → cannot be checked
                continue
            try:
                m = self.tg("getChatMember", chat_id=int(chat) if re.fullmatch(r"-?\d+", chat) else chat, user_id=tg_id)
                st = m.get("status")
                joined = st in ("creator", "administrator", "member") or (st == "restricted" and m.get("is_member") is True)
                if not joined:
                    out["missing"].append(i)
            except TgError as e:
                out["unverifiable"].append(i)                   # e.g. the bot is not admin there → do not lock the user out
                self.log("⚠ জয়েন যাচাই %s: %s (বটকে ওই চ্যানেল/গ্রুপের Admin বানান)" % (chat, e))
        out["ok"] = not out["missing"]
        res_ref.set(out)
        self.stats["joinChecks"] += 1
        self.log("🔎 Join check %s…: %s" % (uid[:8], "✅ সব জয়েন করেছে" if out["ok"] else "❌ বাকি %s" % out["missing"]))

    # ───────────────────────── Telegram updates ─────────────────────────
    def is_chan_admin(self, uid) -> bool:
        t, ids = self.admins
        if time.time() - t > 300:
            ids = {m["user"]["id"] for m in self.tg("getChatAdministrators", chat_id=self.cfg["channel"])}
            self.admins = (time.time(), ids)
        return uid in ids

    def on_message(self, m: dict):
        chat, frm = m.get("chat") or {}, m.get("from") or {}
        if chat.get("type") != "private" or not frm or frm.get("is_bot"):
            return
        tg_id, text = str(frm.get("id")), str(m.get("text") or "")
        name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        ref = self.col("botUsers").document(tg_id)
        existed = ref.get().exists
        data = {"name": name, "username": frm.get("username") or "", "active": True, "lastStart": firestore.SERVER_TIMESTAMP}
        if not existed:
            data["joinedAt"] = firestore.SERVER_TIMESTAMP
        ref.set(data, merge=True)
        try:
            for d in where(self.col("tgLinks"), "tgId", "==", tg_id).stream():
                d.reference.set({"started": True}, merge=True)
        except Exception:
            pass
        if not re.match(r"^/start", text, re.I):
            self.try_tg("sendMessage", chat_id=chat["id"], text="অ্যাপ খুলতে নিচের মেনু বাটন ব্যবহার করুন। 👇")
            return
        self.load_cfg()
        parts = text.split()
        payload = parts[1].strip() if len(parts) > 1 else ""
        reward = self.cfg.get("ref_reward", 0)
        if not existed and re.fullmatch(r"[A-Za-z0-9]{10,40}", payload) and reward > 0:
            try:
                ru = self.col("users").document(payload).get()
                own = list(where(self.col("tgLinks"), "tgId", "==", tg_id).stream())
                done = self.col("refBonuses").document(tg_id).get()
                if ru.exists and not (ru.to_dict() or {}).get("banned") and not done.exists and not any(d.id == payload for d in own):
                    self.col("refBonuses").document(tg_id).set({"uid": payload, "amount": reward, "tgId": tg_id, "name": name, "ts": firestore.SERVER_TIMESTAMP})
                    self.stats["refers"] += 1
                    self.log("🎁 Refer: %s → %s… (%s)" % (name or tg_id, payload[:8], reward))
                    rl = self.tg_of(payload)
                    if rl:
                        self.tg_dm(payload, rl, parse_mode="HTML", text="🎉 <b>নতুন রেফার!</b>\n%s আপনার লিংক দিয়ে যোগ দিয়েছে।\n➕ %s আপনার ব্যালেন্সে যোগ হয়েছে।" % (esc(name or "একজন বন্ধু"), bdt(reward)))
            except Exception as e:
                self.log("⚠ referral: %s" % e)
        self.try_tg("sendMessage", chat_id=chat["id"], parse_mode="HTML",
                    text="👋 স্বাগতম, <b>%s</b>!\n\nTech BD Income এ কাজ করে ইনকাম করুন। শুরু করতে অ্যাপ খুলুন।" % esc(frm.get("first_name") or "বন্ধু"), **self.app_kb())

    def on_callback(self, q: dict):
        p = str(q.get("data") or "").split(":")
        if p[0] != "wd" or len(p) < 3 or p[1] not in ("a", "r") or not p[2]:
            self.try_tg("answerCallbackQuery", callback_query_id=q["id"])
            return
        ok = False
        try:
            ok = self.is_chan_admin((q.get("from") or {}).get("id"))
        except TgError as e:
            self.log("⚠ admins: %s" % e)
        if not ok:
            self.try_tg("answerCallbackQuery", callback_query_id=q["id"], text="⛔ আপনি অ্যাডমিন নন", show_alert=True)
            return
        ref = self.col("withdrawals").document(p[2])
        status = "approved" if p[1] == "a" else "rejected"
        frm = q.get("from") or {}

        @firestore.transactional
        def run(tx):
            s = ref.get(transaction=tx)
            if not s.exists or (s.to_dict() or {}).get("status") != "pending":
                return "done"
            tx.update(ref, {"status": status, "reviewedAt": firestore.SERVER_TIMESTAMP,
                            "reviewedVia": "telegram:" + ("@" + frm["username"] if frm.get("username") else str(frm.get("id"))),
                            "note": "Rejected by admin" if status == "rejected" else ""})
            return "ok"
        res = run(self.db.transaction())
        self.try_tg("answerCallbackQuery", callback_query_id=q["id"],
                    text=("✅ Approved" if status == "approved" else "❌ Rejected") if res == "ok" else "ইতিমধ্যে প্রসেস হয়েছে")

    def handle_update(self, u: dict):
        if u.get("message"):
            self.on_message(u["message"])
        elif u.get("callback_query"):
            self.on_callback(u["callback_query"])

    # ───────────────────────── broadcast (queued from the admin panel) ─────────────────────────
    def process_broadcast(self, doc_id: str):
        if doc_id in self.busy_bc:
            return
        self.busy_bc.add(doc_id)
        try:
            ref = self.col("broadcasts").document(doc_id)
            b = ref.get().to_dict() or {}
            if b.get("status") not in ("queued", "sending"):
                return
            ids = b.get("ids")
            if not isinstance(ids, list):
                s = set()
                if b.get("target") == "one" and b.get("tgId"):
                    s.add(str(b["tgId"]))
                else:
                    for d in self.col("botUsers").stream():
                        if (d.to_dict() or {}).get("active") is not False:
                            s.add(d.id)
                    for d in self.col("tgLinks").stream():
                        t = (d.to_dict() or {}).get("tgId")
                        if t:
                            s.add(str(t))
                ids = sorted(s)[:20000]
                ref.update({"status": "sending", "ids": ids, "pos": 0, "total": len(ids), "sent": int(b.get("sent") or 0), "failed": int(b.get("failed") or 0)})
                b = ref.get().to_dict() or {}
            pos, sent, failed = int(b.get("pos") or 0), int(b.get("sent") or 0), int(b.get("failed") or 0)
            text = "📢 " + str(b.get("text") or "")
            self.log("📢 broadcast শুরু: %d জন (%d থেকে)" % (len(ids), pos))
            since_save = 0
            while pos < len(ids) and not self.stop.is_set() and self.active:
                uid = str(ids[pos])
                try:
                    self.tg("sendMessage", chat_id=uid, text=text, disable_web_page_preview=True)
                    sent += 1
                    pos += 1
                except TgError as e:
                    if e.code == 429:
                        time.sleep((e.retry or 2) + 1)
                        continue
                    failed += 1
                    pos += 1
                    if e.code == 403:
                        self.col("botUsers").document(uid).set({"active": False}, merge=True)
                since_save += 1
                if since_save >= 25:
                    ref.update({"pos": pos, "sent": sent, "failed": failed})
                    since_save = 0
            done = pos >= len(ids)
            upd = {"pos": pos, "sent": sent, "failed": failed, "status": "done" if done else "sending"}
            if done:
                upd["doneAt"] = firestore.SERVER_TIMESTAMP
                self.stats["broadcast"] += 1
            ref.update(upd)
            self.log("📢 broadcast %s: পাঠানো %d, ব্যর্থ %d" % ("শেষ" if done else "থেমেছে", sent, failed))
        finally:
            self.busy_bc.discard(doc_id)

    # ───────────────────────── listeners + loops ─────────────────────────
    def activate(self):
        self._delete_webhook()          # getUpdates (long polling) only works without a webhook
        me = self.tg("getMe")
        self.log("✅ Bot চালু: @%s  (bot.py v%s)" % (me.get("username"), VERSION))
        if me.get("username") and self.cfg.get("username") != me["username"]:
            try:
                self.col("private").document("bot").set({"botUsername": me["username"]}, merge=True)
                self.cfg["username"] = me["username"]
            except Exception as e:
                self.log("⚠ botUsername: %s" % e)
        self.active = True              # set before the listeners start (their first callbacks run at once)
        try:
            self.ensure_proof_intro()
        except Exception as e:
            self.log("⚠ proof channel: %s" % e)
        sub = lambda q, cb: self.unsubs.append(q.on_snapshot(cb))
        sub(self.col("withdrawals").order_by("ts", direction=firestore.Query.DESCENDING).limit(150),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.tg_withdraw, c.document) for c in ch if c.type.name != "REMOVED"])
        sub(self.col("pings"),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.tg_ping, c.document) for c in ch if c.type.name == "ADDED"])
        sub(self.col("joinReq"),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.join_check, c.document) for c in ch if c.type.name in ("ADDED", "MODIFIED")])
        sub(self.col("tgReq"),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.tg_link_req, c.document) for c in ch if c.type.name != "REMOVED"])
        sub(where(self.col("broadcasts"), "status", "in", ["queued", "sending"]),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.process_broadcast, c.document.id) for c in ch if c.type.name != "REMOVED"])

    def _delete_webhook(self):
        try:
            self.tg("deleteWebhook", drop_pending_updates=False)
        except TgError as e:
            self.log("⚠ deleteWebhook: %s" % e)

    def deactivate(self):
        for u in self.unsubs:
            try:
                u.unsubscribe()
            except Exception:
                pass
        self.unsubs, self.active = [], False

    def poll_once(self):
        ups = self.tg("getUpdates", offset=self.offset, timeout=25, allowed_updates=["message", "callback_query"])
        for u in ups:
            self.offset = u["update_id"] + 1
            try:
                with open(OFFSET_FILE, "w") as f:
                    f.write(str(self.offset))
            except OSError:
                pass
            self.pool.submit(self.safe, self.handle_update, u)
        if ups:
            try:
                self.col("private").document("botState").set({"offset": self.offset}, merge=True)
            except Exception:
                pass

    def heartbeat(self):
        while not self.stop.is_set():
            HEALTH.update({"ok": self.state == "running", "state": self.state, "note": " | ".join(self.logbuf[-2:])[-300:], "stats": self.stats})
            try:
                self.col("private").document("botStatus").set({
                    "ts": firestore.SERVER_TIMESTAMP, "version": VERSION, "state": self.state, "engine": "bot.py",
                    "host": socket.gethostname(), "pid": os.getpid(), "note": " | ".join(self.logbuf[-3:])[-400:], "stats": self.stats}, merge=True)
            except Exception as e:
                print("heartbeat:", e, flush=True)
            self.stop.wait(30)

    def run(self):
        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(s, lambda *_: self.stop.set())
            except ValueError:
                pass
        threading.Thread(target=self.heartbeat, daemon=True).start()
        self.log("🤖 bot.py v%s শুরু হয়েছে" % VERSION)
        back = 2
        while not self.stop.is_set():
            try:
                cfg = self.load_cfg()
                if not cfg["token"]:
                    self.state, self.note = "waiting", "Bot token not saved in the admin panel"
                    self.deactivate()
                    self.stop.wait(15)
                    continue
                if not cfg["py"]:
                    if self.active or self.state != "idle":
                        self.log("⏸ অ্যাডমিন প্যানেলে 'bot.py Mode' বন্ধ — অপেক্ষা করছি")
                    self.deactivate()
                    self.state = "idle"
                    self.stop.wait(15)
                    continue
                if not self.active:
                    self.activate()
                self.state = "running"
                self.poll_once()
                back = 2
            except TgError as e:
                if e.code == 401:
                    self.log("❌ Token ভুল — অ্যাডমিন প্যানেলে ঠিক করুন")
                    self.state = "waiting"
                    self.deactivate()
                    self.stop.wait(30)
                elif e.code == 409:
                    self.log("⚠ অন্য কোথাও (পেজ-বট বা আরেকটা bot.py) একই বট চলছে")
                    self.stop.wait(20)
                else:
                    self.log("⚠ Telegram: %s" % e)
                    self.stop.wait(back)
                    back = min(back * 2, 30)
            except Exception as e:
                self.log("⚠ %s" % e)
                self.stop.wait(back)
                back = min(back * 2, 30)
        self.deactivate()
        self.state = "stopped"
        try:
            self.col("private").document("botStatus").set({"ts": firestore.SERVER_TIMESTAMP, "state": "stopped", "engine": "bot.py"}, merge=True)
        except Exception:
            pass
        self.pool.shutdown(wait=False)
        self.log("👋 বন্ধ হয়েছে")


def main():
    start_health_server()
    while True:
        try:
            bot = Bot()
            bot.run()
            if bot.stop.is_set():
                break                           # stopped on purpose (Ctrl+C / kill)
        except BotSetupError as e:
            HEALTH.update({"ok": False, "state": "error", "note": str(e)})
            print("❌ " + str(e), flush=True)
            time.sleep(60)                      # keep the web port open so you can read the message, then try again
            continue
        except SystemExit:
            raise
        except KeyboardInterrupt:
            break
        except Exception as e:                  # unexpected crash → start again
            print("⚠ বট বন্ধ হয়ে গিয়েছিল (%s) — ৫ সেকেন্ডে আবার চালু হচ্ছে" % e, flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
