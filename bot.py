#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tech BD Income — Telegram Bot  (bot.py)

Runs 24/7 on any always-on computer (VPS, home PC, Termux, Raspberry Pi, Railway, Replit ...).
The admin panel does NOT need to stay open.

What it does (same as the admin-panel bot, just always on):
  • Withdraw notifications   → channel message with Approve / Reject buttons + user DM + status updates
  • Refer                    → /start <uid> gives the referral bonus and notifies the referrer
  • Broadcast                → messages queued from the admin panel are sent in the background
  • DM notifications         → new chat message / call alerts
  • Telegram account linking → verifies the Mini-App login (initData) and saves the link

Setup (once):
  1.  pip install firebase-admin requests
  2.  Firebase Console → Project settings → Service accounts → "Generate new private key"
      Save the file next to this script as  serviceAccountKey.json
  3.  Admin panel → Settings → Telegram Bot: save the Bot Token / Channel ID, then turn ON "bot.py Mode"
  4.  python3 bot.py        (keep it running, e.g. with systemd / pm2 / screen / nohup)

Optional file  bot_config.json  (next to this script):
  {"token": "...", "channelId": "-100...", "pyMode": true}
  On start, any value that is still EMPTY in the admin panel (Firestore private/bot) is filled in from this file,
  so the panel shows the Token / Channel and bot.py Mode is already ON. Values saved in the panel are never overwritten.
  Keep this file and serviceAccountKey.json on the bot computer only - never upload them to your website.

Optional environment variables:
  GOOGLE_APPLICATION_CREDENTIALS   path of the service-account json (default: ./serviceAccountKey.json)
  BOT_TOKEN, CHANNEL_ID            override the values saved in the admin panel
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, firestore

VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or os.path.join(BASE_DIR, "serviceAccountKey.json")
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
        if not os.path.exists(KEY_FILE):
            sys.exit("❌ serviceAccountKey.json পাওয়া যায়নি: %s\n   Firebase Console → Project settings → Service accounts → Generate new private key" % KEY_FILE)
        firebase_admin.initialize_app(credentials.Certificate(KEY_FILE))
        self.db = firestore.client()
        self.http = requests.Session()
        self.cfg, self.cfg_t = {}, 0.0
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=6)
        self.unsubs, self.active = [], False
        self.admins = (0.0, set())
        self.logbuf, self.state, self.note = [], "starting", ""
        self.stats = {"withdrawNew": 0, "withdrawDone": 0, "refers": 0, "pings": 0, "links": 0, "broadcast": 0}
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

    # ───────────────────────── helpers ─────────────────────────
    def file_cfg(self) -> dict:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def bootstrap(self):
        """Copy Token / Channel / bot.py Mode from bot_config.json into the admin-panel settings (only where empty)."""
        f = self.file_cfg()
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
            self.log("⚙ অ্যাডমিন প্যানেলের সেটিং bot_config.json থেকে ভরা হয়েছে: %s" % ", ".join(sorted(upd)))

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
                name = (w.get("name") or "").strip()
                masked = (name[0] + "***" + name[-1] if len(name) > 2 else name[0] + "***") if name else "User"
                try:
                    self.col("payoutsFeed").add({"name": masked, "amount": num(w.get("amount")), "method": w.get("method"), "ts": firestore.SERVER_TIMESTAMP})
                except Exception as e:
                    self.log("⚠ payoutsFeed: %s" % e)
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
        sub = lambda q, cb: self.unsubs.append(q.on_snapshot(cb))
        sub(self.col("withdrawals").order_by("ts", direction=firestore.Query.DESCENDING).limit(150),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.tg_withdraw, c.document) for c in ch if c.type.name != "REMOVED"])
        sub(self.col("pings"),
            lambda docs, ch, t: [self.pool.submit(self.safe, self.tg_ping, c.document) for c in ch if c.type.name == "ADDED"])
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

    def heartbeat(self):
        while not self.stop.is_set():
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


if __name__ == "__main__":
    Bot().run()
