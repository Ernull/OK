import os
import json
import logging
import asyncio
import io
import uuid
import requests
import redis
from concurrent.futures import ThreadPoolExecutor
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler

# تنظیمات لاگینگ برای دیباگ در پنل Railway
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# اتصال به دیتابیس Redis از طریق متغیر محیطی Railway
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logging.info("✅ Successfully connected to Redis!")
except Exception as e:
    logging.error(f"❌ Failed to connect to Redis: {e}")

# تعریف وضعیت‌های مکالمه
PHONE, OTP, ASK_NAME = range(3)

def save_account_to_db(phone, access_token, refresh_token):
    """ذخیره توکن‌ها در ردیس به صورت Hash"""
    try:
        redis_client.hset(f"account:{phone}", mapping={
            "access_token": access_token,
            "refresh_token": refresh_token
        })
        logging.info(f"✅ Tokens for {phone} successfully saved to Redis.")
    except Exception as e:
        logging.error(f"❌ Redis Error: {e}")

# هدرهای پایه برای عبور از فایروال
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "Origin": "https://www.okala.com",
    "Referer": "https://www.okala.com/"
}

# اجرای درخواست‌های مسدودکننده در پس‌زمینه
executor = ThreadPoolExecutor(max_workers=10)

async def async_request(method, url, **kwargs):
    loop = asyncio.get_running_loop()
    if method.upper() == 'POST':
        return await loop.run_in_executor(executor, lambda: requests.post(url, **kwargs))
    elif method.upper() == 'PUT':
        return await loop.run_in_executor(executor, lambda: requests.put(url, **kwargs))
    return await loop.run_in_executor(executor, lambda: requests.get(url, **kwargs))

def get_user_headers(context: ContextTypes.DEFAULT_TYPE):
    """ساخت هدر اختصاصی برای هر کاربر با آیدی‌های ثابت در طول نشست"""
    if 'device_id' not in context.user_data:
        context.user_data['device_id'] = str(uuid.uuid4())
        context.user_data['session_id'] = str(uuid.uuid4())
    
    headers = BASE_HEADERS.copy()
    headers['X-User-Unique-Id'] = context.user_data['device_id']
    headers['session-id'] = context.user_data['session_id']
    return headers

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("سلام! لطفاً شماره موبایل خود را برای ورود به اکالا وارد کنید:")
    return PHONE

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.text.strip()
    context.user_data['phone'] = phone

    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {
        "mobile": phone,
        "deviceTypeCode": 7,
        "confirmTerms": True,
        "notRobot": False,
        "otpType": 0,
        "ValidationCodeCreateReason": 5,
        "OtpApp": 0,
        "IsAppOnly": False
    }

    try:
        response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
        if response.status_code == 200:
            await update.message.reply_text("✅ کد تایید پیامک شد. لطفاً آن را وارد کنید:")
            return OTP
        else:
            await update.message.reply_text(f"❌ خطا در ارسال کد. وضعیت سرور: {response.status_code}")
            return ConversationHandler.END
    except Exception as e:
        logging.error(f"Error in request_otp: {e}")
        await update.message.reply_text("❌ خطای ارتباطی با سرور. لطفاً مجدداً تلاش کنید.")
        return ConversationHandler.END

async def verify_otp_and_check_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp_code = update.message.text.strip()
    phone = context.user_data.get('phone')
    msg = await update.message.reply_text("⏳ در حال احراز هویت...")

    token_url = "https://apigateway.okala.com/api/v1/accounts/tokens"
    token_payload = {
        "mobile_number": phone,
        "otp_code": otp_code,
        "grant_type": "customer_grant_type",
        "client_id": "customer_client_id",
        "client_secret": "u_M{'57j!%LI21#",
        "client_name": "customer_client_name",
        "device_type_code": 7,
        "scope": "offline_access",
        "loginDuration": 4815
    }
    
    headers_urlencoded = get_user_headers(context)
    headers_urlencoded["Content-Type"] = "application/x-www-form-urlencoded"

    response = await async_request('POST', token_url, data=token_payload, headers=headers_urlencoded)

    if response.status_code == 200:
        auth_data = response.json()
        access_token = auth_data.get("access_token")
        refresh_token = auth_data.get("refresh_token")
        
        context.user_data['access_token'] = access_token
        context.user_data['auth_data'] = auth_data 
        
        # ذخیره توکن‌ها در دیتابیس Redis
        if access_token and refresh_token:
            save_account_to_db(phone, access_token, refresh_token)

        has_name = auth_data.get("UserInfo", {}).get("HasName", False)

        if not has_name:
            await msg.edit_text("⚠️ حساب شما فاقد نام است.\nلطفاً نام و نام خانوادگی خود را وارد کنید (مثال: علی احمدی):")
            return ASK_NAME
        else:
            await msg.edit_text("✅ اطلاعات هویتی کامل است. در حال دریافت لاگ‌ها...")
            return await fetch_and_send_logs(update, context, msg)
    else:
        await msg.edit_text("❌ کد وارد شده اشتباه است یا منقضی شده. با ارسال مجدد /start تلاش کنید.")
        return ConversationHandler.END

async def save_name_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.message.text.strip()
    
    if not full_name:
        await update.message.reply_text("⚠️ لطفاً یک نام معتبر وارد کنید:")
        return ASK_NAME

    parts = full_name.split(maxsplit=1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    access_token = context.user_data.get('access_token')
    msg = await update.message.reply_text("⏳ در حال ثبت اطلاعات هویتی شما در سیستم اکالا...")

    update_profile_url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/UpdateCustomer" 
    
    auth_headers = get_user_headers(context)
    auth_headers["Authorization"] = f"Bearer {access_token}"
    auth_headers["Content-Type"] = "application/json"

    payload = {
        "birthDate": "",
        "birthDateEpoch": 700086600,
        "customerType": 0,
        "firstName": first_name,
        "genderCode": 1,
        "genderTitle": "مذکر",
        "lastName": last_name,
        "gender": "male"
    }

    try:
        response = await async_request('POST', update_profile_url, json=payload, headers=auth_headers)
        if response.status_code == 200 and response.json().get("success"):
            await msg.edit_text("✅ نام شما با موفقیت ثبت شد. در حال استخراج داده‌ها...")
        else:
            await msg.edit_text("⚠️ خطا در ثبت نام. در حال استخراج داده‌های فعلی...")
    except Exception as e:
        logging.error(f"Error in save_name_and_continue: {e}")
        await msg.edit_text("⚠️ خطا در ارتباط. در حال دریافت داده‌ها...")

    return await fetch_and_send_logs(update, context, msg)

async def fetch_and_send_logs(update: Update, context: ContextTypes.DEFAULT_TYPE, status_msg) -> int:
    access_token = context.user_data.get('access_token')
    phone = context.user_data.get('phone')

    auth_headers = get_user_headers(context)
    auth_headers["Authorization"] = f"Bearer {access_token}"

    all_results_log = {
        "authentication_info": context.user_data.get('auth_data')
    }

    orders_url = "https://apigateway.okala.com/api/v1/AppHomePage/Hippo/get-orders-state"
    orders_response = await async_request('GET', orders_url, headers=auth_headers)
    if orders_response.status_code == 200:
        try:
            all_results_log["orders_state"] = orders_response.json()
        except:
            all_results_log["orders_state"] = orders_response.text

    bff_url = "https://apigateway.okala.com/api/bff/v1/stores?fragments=userMe&fragments=address"
    user_response = await async_request('GET', bff_url, headers=auth_headers)
    if user_response.status_code == 200:
        try:
            all_results_log["user_profile_and_address"] = user_response.json()
        except:
            pass

    formatted_log = json.dumps(all_results_log, indent=4, ensure_ascii=False)
    await status_msg.edit_text("✅ اطلاعات استخراج و در دیتابیس (Redis) ذخیره شد!")

    if len(formatted_log) > 4000:
        log_file = io.BytesIO(formatted_log.encode('utf-8'))
        log_file.name = f"okala_results_{phone}.json"
        
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=log_file,
            caption="📄 فایل کامل اطلاعات استخراج‌شده"
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"<pre><code class='language-json'>{formatted_log}</code></pre>",
            parse_mode='HTML'
        )

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

def main():
    # دریافت توکن ربات از محیط
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logging.error("❌ BOT_TOKEN is not set in environment variables!")
        return

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
    
    logging.info("🚀 Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
