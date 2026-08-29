import asyncio
import logging
import os
import re
import secrets
import tempfile
import threading
from difflib import SequenceMatcher
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .db import (
    add_user,
    claim_owner,
    clear_state,
    get_config,
    get_state,
    is_allowed,
    list_users,
    set_config,
    set_state,
)
from .excel import normalize_phone, normalize_text, parse_tracking_file
from .integrations import (
    ConfigurationError,
    ShopinoClient,
    WooClient,
    build_variations,
    shopino_name,
    shopino_phone,
    woo_name,
    woo_phone,
)

TOKEN = os.environ['BOTTOKEN']
PORT = int(os.getenv('PORT', '8095'))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('orderscutella')


def main_menu():
    return ReplyKeyboardMarkup([
        ['🛍 ثبت محصول', '📋 محصولات اخیر'],
        ['📦 رهگیری شاپینو', '🚚 رهگیری سایت Cutella'],
        ['⚙️ تنظیمات اتصال‌ها', '📊 وضعیت'],
    ], resize_keyboard=True)


def settings_menu():
    return ReplyKeyboardMarkup([
        ['🔌 تنظیم ووکامرس', '🔐 تنظیم شاپینو'],
        ['⬅️ منوی اصلی'],
    ], resize_keyboard=True)


def cancel_menu():
    return ReplyKeyboardMarkup([['❌ لغو', '⬅️ منوی اصلی']], resize_keyboard=True)


async def guard(update):
    uid = update.effective_user.id
    if is_allowed(uid):
        return True
    if claim_owner(uid):
        await update.effective_message.reply_text(
            '✅ این اکانت به‌عنوان مالک ربات Cutella ثبت شد.',
            reply_markup=main_menu(),
        )
        return True
    await update.effective_message.reply_text('⛔️ دسترسی ندارید.')
    return False


def clean_price(value):
    value = str(value or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789'))
    value = value.replace(',', '').replace('٬', '').strip()
    if value in {'-', 'ندارد', 'بدون', '0'}:
        return ''
    if not re.fullmatch(r'\d+(?:\.\d+)?', value):
        raise ValueError('عدد معتبر وارد کنید.')
    return value


def resolve_categories(client, raw):
    raw_items = [normalize_text(x).strip() for x in re.split(r'[,،\n]+', raw) if x.strip()]
    if not raw_items or raw_items == ['-']:
        return []
    categories = client.categories()
    resolved = []
    for item in raw_items:
        if item.isdigit():
            resolved.append({'id': int(item)})
            continue
        best = None
        best_score = 0
        for category in categories:
            name = normalize_text(category.get('name'))
            score = SequenceMatcher(None, item, name).ratio()
            if score > best_score:
                best = category
                best_score = score
        if best and best_score >= 0.72:
            resolved.append({'id': int(best['id'])})
        else:
            raise ValueError(f'دسته‌بندی «{item}» پیدا نشد.')
    return resolved


def parse_attributes(raw):
    result = {}
    for line in str(raw or '').splitlines():
        line = line.strip()
        if not line:
            continue
        if ':' not in line:
            raise ValueError('هر ویژگی را مثل «رنگ: قرمز، آبی» وارد کنید.')
        name, values = line.split(':', 1)
        options = [x.strip() for x in re.split(r'[,،]', values) if x.strip()]
        if not name.strip() or not options:
            raise ValueError('نام ویژگی و حداقل یک مقدار لازم است.')
        result[name.strip()] = options
    if not result:
        raise ValueError('حداقل یک ویژگی لازم است.')
    combinations = 1
    for options in result.values():
        combinations *= len(options)
    if combinations > 50:
        raise ValueError('تعداد ترکیب‌ها بیشتر از 50 است؛ ویژگی‌ها را محدودتر کنید.')
    return result


def score_candidate(row, name, phone=''):
    target_name = normalize_text(row.get('name'))
    candidate_name = normalize_text(name)
    if not target_name or not candidate_name:
        name_score = 0.0
    else:
        name_score = SequenceMatcher(None, target_name, candidate_name).ratio()
        if target_name in candidate_name or candidate_name in target_name:
            name_score = max(name_score, 0.92)
    target_phone = normalize_phone(row.get('phone'))
    candidate_phone = normalize_phone(phone)
    if target_phone and candidate_phone and target_phone == candidate_phone:
        return min(1.0, max(name_score, 0.90) + 0.08)
    return name_score


def choose_match(row, candidates, name_fn, phone_fn):
    ranked = sorted(
        ((score_candidate(row, name_fn(item), phone_fn(item)), item) for item in candidates),
        key=lambda x: x[0],
        reverse=True,
    )
    if not ranked:
        return None, 'none'
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0
    if best_score >= 0.90 and best_score - second_score >= 0.06:
        return best, 'strong'
    if best_score >= 0.97:
        return best, 'strong'
    return None, 'ambiguous'


async def start(update: Update, context):
    if not await guard(update):
        return
    clear_state(update.effective_user.id)
    await update.message.reply_text(
        '🤖 ربات مدیریت Cutella آماده است.\n\n'
        'محصول، Shopino و کدهای رهگیری سایت از همین‌جا مدیریت می‌شوند.',
        reply_markup=main_menu(),
    )


async def cancel(update: Update, context):
    if not await guard(update):
        return
    clear_state(update.effective_user.id)
    await update.message.reply_text('لغو شد.', reply_markup=main_menu())


async def allow_user(update: Update, context):
    if not await guard(update):
        return
    if get_config('owner') != str(update.effective_user.id):
        return await update.message.reply_text('فقط مالک ربات می‌تواند کاربر اضافه کند.')
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text('استفاده: /allow TELEGRAM_ID')
    add_user(int(context.args[0]))
    await update.message.reply_text(f'✅ {context.args[0]} اضافه شد.')


async def users(update: Update, context):
    if not await guard(update):
        return
    owner = get_config('owner') or '-'
    others = list_users()
    await update.message.reply_text(
        f'مالک: {owner}\nکاربران مجاز: ' + (', '.join(map(str, others)) if others else '—')
    )


async def status_text():
    lines = ['📊 وضعیت اتصال‌ها']
    try:
        woo_count = await asyncio.to_thread(WooClient().probe)
        lines.append(f'✅ WooCommerce: متصل — {woo_count} محصول')
    except Exception as exc:
        lines.append(f'❌ WooCommerce: {exc}')
    try:
        shopino_count = await asyncio.to_thread(ShopinoClient().probe)
        lines.append(f'✅ Shopino: متصل — {shopino_count} سفارش')
    except Exception as exc:
        lines.append(f'❌ Shopino: {exc}')
    return '\n'.join(lines)


async def finalize_product(update, state):
    payload = state['payload']
    client = WooClient()
    categories = await asyncio.to_thread(client.categories)
    # Resolve from already typed raw category names without a second network call.
    raw_items = [normalize_text(x).strip() for x in re.split(r'[,،\n]+', payload.get('categories', '')) if x.strip()]
    category_payload = []
    for item in raw_items:
        if item == '-':
            continue
        if item.isdigit():
            category_payload.append({'id': int(item)})
            continue
        scored = sorted(
            ((SequenceMatcher(None, item, normalize_text(cat.get('name'))).ratio(), cat) for cat in categories),
            key=lambda x: x[0], reverse=True,
        )
        if not scored or scored[0][0] < 0.72:
            raise ValueError(f'دسته‌بندی «{item}» پیدا نشد.')
        category_payload.append({'id': int(scored[0][1]['id'])})

    data = {
        'name': payload['name'],
        'type': payload['type'],
        'status': 'publish',
        'description': payload.get('description', ''),
        'categories': category_payload,
        'images': payload.get('images', []),
    }
    if payload.get('weight'):
        data['weight'] = payload['weight']

    if payload['type'] == 'simple':
        data.update({
            'regular_price': payload['regular_price'],
            'manage_stock': True,
            'stock_quantity': int(payload.get('stock') or 0),
        })
        if payload.get('sale_price'):
            data['sale_price'] = payload['sale_price']
        product = await asyncio.to_thread(client.create_product, data)
    else:
        attrs = payload['attributes']
        data['attributes'] = [
            {'name': name, 'visible': True, 'variation': True, 'options': values}
            for name, values in attrs.items()
        ]
        product = await asyncio.to_thread(client.create_product, data)
        variations = build_variations(attrs, payload['regular_price'], payload.get('sale_price'))
        for variation in variations:
            variation['manage_stock'] = True
            variation['stock_quantity'] = int(payload.get('stock') or 0)
            await asyncio.to_thread(client.create_variation, product['id'], variation)

    clear_state(update.effective_user.id)
    await update.message.reply_text(
        f"✅ محصول ثبت شد\n#{product.get('id')} — {product.get('name')}\n{product.get('permalink', '')}",
        reply_markup=main_menu(),
    )


async def handle_tracking_file(update, target):
    document = update.message.document
    if not document:
        return
    suffix = Path(document.file_name or '').suffix.lower()
    if suffix not in {'.xlsx', '.xlsm', '.csv'}:
        return await update.message.reply_text('فایل باید XLSX / XLSM / CSV باشد.')
    progress = await update.message.reply_text('⏳ فایل دریافت شد؛ در حال تطبیق سفارش‌ها…')
    tg_file = await document.get_file()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / (document.file_name or f'tracking{suffix}')
        await tg_file.download_to_drive(custom_path=str(path))
        rows = await asyncio.to_thread(parse_tracking_file, path)

        if target == 'shopino':
            client = ShopinoClient()
            candidates = await asyncio.to_thread(client.orders)
            done_count = skipped = ambiguous = 0
            for row in rows:
                match, quality = choose_match(row, candidates, shopino_name, shopino_phone)
                if not match:
                    ambiguous += 1
                    continue
                current = str(match.get('tracking_code') or '').strip()
                if current and current != row['code']:
                    skipped += 1
                    continue
                if current == row['code']:
                    done_count += 1
                    continue
                await asyncio.to_thread(client.set_tracking, match['id'], row['code'])
                done_count += 1
        else:
            client = WooClient()
            candidates = await asyncio.to_thread(client.orders)
            done_count = skipped = ambiguous = 0
            for row in rows:
                match, quality = choose_match(row, candidates, woo_name, woo_phone)
                if not match:
                    ambiguous += 1
                    continue
                try:
                    await asyncio.to_thread(client.update_order_tracking, match['id'], row['code'])
                    done_count += 1
                except RuntimeError as exc:
                    if 'overwrite' in str(exc):
                        skipped += 1
                    else:
                        raise

    clear_state(update.effective_user.id)
    await progress.edit_text(
        f'✅ پردازش تمام شد\n\n'
        f'کل ردیف‌ها: {len(rows)}\n'
        f'ثبت/تأیید شده: {done_count}\n'
        f'کد متفاوت و محافظت‌شده: {skipped}\n'
        f'نیازمند تطبیق دستی: {ambiguous}'
    )
    await update.message.reply_text('آماده عملیات بعدی.', reply_markup=main_menu())


async def document_handler(update: Update, context):
    if not await guard(update):
        return
    state = get_state(update.effective_user.id)
    if not state or state['step'] != 'waiting_file':
        return await update.message.reply_text('اول از منو نوع عملیات رهگیری را انتخاب کنید.', reply_markup=main_menu())
    if state['flow'] == 'tracking_shopino':
        return await handle_tracking_file(update, 'shopino')
    if state['flow'] == 'tracking_woo':
        return await handle_tracking_file(update, 'woo')


async def photo_handler(update: Update, context):
    if not await guard(update):
        return
    state = get_state(update.effective_user.id)
    if not state or state['flow'] != 'product' or state['step'] != 'photos':
        return await update.message.reply_text('الان منتظر عکس محصول نیستم.', reply_markup=main_menu())
    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    payload = state['payload']
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f'{secrets.token_hex(6)}.jpg'
        await tg_file.download_to_drive(custom_path=str(path))
        media = await asyncio.to_thread(WooClient().upload_media, path, path.name)
    payload.setdefault('images', []).append({'id': int(media['id'])})
    set_state(update.effective_user.id, 'product', 'photos', payload)
    await update.message.reply_text(
        f"✅ عکس {len(payload['images'])} آپلود شد. عکس بعدی را بفرست یا «تمام شد» بنویس."
    )


async def text_handler(update: Update, context):
    if not await guard(update):
        return
    uid = update.effective_user.id
    text = (update.message.text or '').strip()

    if text in {'⬅️ منوی اصلی', '❌ لغو'}:
        clear_state(uid)
        return await update.message.reply_text('منوی اصلی', reply_markup=main_menu())

    if text == '⚙️ تنظیمات اتصال‌ها':
        clear_state(uid)
        return await update.message.reply_text('تنظیم کدام اتصال؟', reply_markup=settings_menu())
    if text == '🔌 تنظیم ووکامرس':
        set_state(uid, 'woo_config', 'url', {})
        return await update.message.reply_text('آدرس سایت را بفرست:\nhttps://cutellashop.ir', reply_markup=cancel_menu())
    if text == '🔐 تنظیم شاپینو':
        set_state(uid, 'shopino_config', 'shop_id', {})
        return await update.message.reply_text('Shop ID فروشگاه Cutella در Shopino را بفرست.', reply_markup=cancel_menu())
    if text in {'📊 وضعیت', '/status'}:
        return await update.message.reply_text(await status_text(), reply_markup=main_menu())
    if text == '📋 محصولات اخیر':
        try:
            products = await asyncio.to_thread(WooClient().recent_products, 10)
            body = '\n'.join(
                f"• #{p.get('id')} — {p.get('name')} — {p.get('price') or 'بدون قیمت'}"
                for p in products
            ) or 'محصولی نیست.'
            return await update.message.reply_text(body, reply_markup=main_menu())
        except Exception as exc:
            return await update.message.reply_text(f'❌ {exc}', reply_markup=main_menu())
    if text == '📦 رهگیری شاپینو':
        set_state(uid, 'tracking_shopino', 'waiting_file', {})
        return await update.message.reply_text('فایل رهگیری XLSX/XLSM/CSV را بفرست.', reply_markup=cancel_menu())
    if text == '🚚 رهگیری سایت Cutella':
        set_state(uid, 'tracking_woo', 'waiting_file', {})
        return await update.message.reply_text('فایل رهگیری XLSX/XLSM/CSV را بفرست.', reply_markup=cancel_menu())
    if text == '🛍 ثبت محصول':
        try:
            await asyncio.to_thread(WooClient().probe)
        except Exception as exc:
            return await update.message.reply_text(f'اول اتصال WooCommerce را تنظیم کن:\n{exc}', reply_markup=settings_menu())
        set_state(uid, 'product', 'name', {})
        return await update.message.reply_text('نام محصول را بفرست.', reply_markup=cancel_menu())

    state = get_state(uid)
    if not state:
        return await update.message.reply_text('از منو انتخاب کن.', reply_markup=main_menu())
    flow, step, payload = state['flow'], state['step'], state['payload']

    try:
        if flow == 'woo_config':
            if step == 'url':
                if not re.match(r'^https?://', text):
                    raise ValueError('آدرس باید با http:// یا https:// شروع شود.')
                payload['url'] = text.rstrip('/')
                set_state(uid, flow, 'ck', payload)
                return await update.message.reply_text('WooCommerce Consumer Key را بفرست (ck_...).')
            if step == 'ck':
                payload['ck'] = text
                set_state(uid, flow, 'cs', payload)
                return await update.message.reply_text('WooCommerce Consumer Secret را بفرست (cs_...).')
            if step == 'cs':
                payload['cs'] = text
                set_state(uid, flow, 'wp_user', payload)
                return await update.message.reply_text('WordPress Username را بفرست. اگر فعلاً نداری «-» بفرست.')
            if step == 'wp_user':
                payload['wp_user'] = '' if text == '-' else text
                set_state(uid, flow, 'wp_pass', payload)
                return await update.message.reply_text('WordPress Application Password را بفرست. اگر فعلاً نداری «-» بفرست.')
            if step == 'wp_pass':
                set_config('woo_url', payload['url'])
                set_config('woo_ck', payload['ck'])
                set_config('woo_cs', payload['cs'])
                set_config('wp_user', payload.get('wp_user', ''))
                set_config('wp_app_password', '' if text == '-' else text.replace(' ', ''))
                clear_state(uid)
                count = await asyncio.to_thread(WooClient().probe)
                return await update.message.reply_text(f'✅ WooCommerce متصل شد — {count} محصول', reply_markup=main_menu())

        if flow == 'shopino_config':
            if step == 'shop_id':
                if not text.isdigit():
                    raise ValueError('Shop ID باید عدد باشد.')
                payload['shop_id'] = text
                set_state(uid, flow, 'session', payload)
                return await update.message.reply_text('مقدار sessionid مرورگر Shopino را بفرست.')
            if step == 'session':
                set_config('shopino_shop_id', payload['shop_id'])
                set_config('shopino_session', text)
                clear_state(uid)
                count = await asyncio.to_thread(ShopinoClient().probe)
                return await update.message.reply_text(f'✅ Shopino متصل شد — {count} سفارش', reply_markup=main_menu())

        if flow == 'product':
            if step == 'name':
                payload['name'] = text
                set_state(uid, flow, 'type', payload)
                return await update.message.reply_text('نوع محصول؟ «ساده» یا «متغیر»')
            if step == 'type':
                normalized = normalize_text(text)
                if normalized in {'ساده', 'simple'}:
                    payload['type'] = 'simple'
                elif normalized in {'متغیر', 'variable'}:
                    payload['type'] = 'variable'
                else:
                    raise ValueError('فقط «ساده» یا «متغیر» بنویس.')
                set_state(uid, flow, 'regular_price', payload)
                return await update.message.reply_text('قیمت اصلی را بفرست (فقط عدد).')
            if step == 'regular_price':
                payload['regular_price'] = clean_price(text)
                set_state(uid, flow, 'sale_price', payload)
                return await update.message.reply_text('قیمت تخفیف را بفرست یا «-».')
            if step == 'sale_price':
                payload['sale_price'] = clean_price(text)
                set_state(uid, flow, 'stock', payload)
                return await update.message.reply_text('موجودی را بفرست (مثلاً 10). برای محصول متغیر، موجودی هر variation همین عدد می‌شود.')
            if step == 'stock':
                if not text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹','0123456789')).isdigit():
                    raise ValueError('موجودی باید عدد باشد.')
                payload['stock'] = int(text.translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹','0123456789')))
                set_state(uid, flow, 'weight', payload)
                return await update.message.reply_text('وزن را بفرست یا «-».')
            if step == 'weight':
                payload['weight'] = '' if text == '-' else clean_price(text)
                set_state(uid, flow, 'categories', payload)
                return await update.message.reply_text('دسته‌بندی‌ها را با ویرگول بنویس؛ مثل «پوست، آرایشی». اگر نمی‌خواهی «-».')
            if step == 'categories':
                payload['categories'] = text
                set_state(uid, flow, 'description', payload)
                return await update.message.reply_text('توضیحات محصول را بفرست؛ اگر ندارد «-».')
            if step == 'description':
                payload['description'] = '' if text == '-' else text
                if payload['type'] == 'variable':
                    set_state(uid, flow, 'attributes', payload)
                    return await update.message.reply_text(
                        'ویژگی‌ها را هرکدام در یک خط بفرست:\nرنگ: قرمز، آبی\nسایز: S, M, L'
                    )
                payload['images'] = []
                set_state(uid, flow, 'photos', payload)
                return await update.message.reply_text('عکس‌ها را یکی‌یکی بفرست. وقتی تمام شد «تمام شد» بنویس.')
            if step == 'attributes':
                payload['attributes'] = parse_attributes(text)
                payload['images'] = []
                set_state(uid, flow, 'photos', payload)
                return await update.message.reply_text('عکس‌ها را یکی‌یکی بفرست. وقتی تمام شد «تمام شد» بنویس.')
            if step == 'photos':
                if normalize_text(text) not in {'تمام شد', 'تمامشد', 'done'}:
                    return await update.message.reply_text('عکس بفرست یا «تمام شد» بنویس.')
                return await finalize_product(update, state)

    except Exception as exc:
        log.exception('operation failed')
        return await update.message.reply_text(f'❌ {exc}\nدوباره مقدار درست را بفرست یا لغو کن.', reply_markup=cancel_menu())


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            body = b'{"ok":true,"service":"orderscutella"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        return


def start_health_server():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


def run():
    start_health_server()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('cancel', cancel))
    app.add_handler(CommandHandler('allow', allow_user))
    app.add_handler(CommandHandler('users', users))
    app.add_handler(CommandHandler('status', lambda u, c: u.message.reply_text(status_text())))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    log.info('OrdersCutella bot started on health port %s', PORT)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    run()
