import asyncio
import logging
import secrets
import tempfile
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from . import main
from .db import clear_state
from .integrations import WooClient, build_variations

NAME, TYPE, REGULAR_PRICE, SALE_PRICE, STOCK, WEIGHT, CATEGORIES, DESCRIPTION, ATTRIBUTES, PHOTOS = range(10)
CANCEL_RE = r'^(?:❌ لغو|⬅️ منوی اصلی)$'
CATEGORY_PAGE_SIZE = 7
CTX_KEY = 'cutella_product_wizard'

log = logging.getLogger('orderscutella.product')


def _wizard(context):
    return context.user_data.setdefault(
        CTX_KEY,
        {
            'payload': {},
            'categories': [],
            'selected_categories': [],
        },
    )


def _reset(context):
    context.user_data.pop(CTX_KEY, None)


def _cancel_button():
    return main.cancel_menu()


def _type_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton('🟢 ساده', callback_data='ptype:simple'),
            InlineKeyboardButton('🟣 متغیر', callback_data='ptype:variable'),
        ]
    ])


def _skip_keyboard(callback_data, label):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback_data)]])


def _photos_keyboard(count=0):
    rows = []
    if count:
        rows.append([InlineKeyboardButton(f'✅ ثبت محصول با {count} عکس', callback_data='pphoto:done')])
    else:
        rows.append([InlineKeyboardButton('✅ ثبت محصول بدون عکس', callback_data='pphoto:done')])
    return InlineKeyboardMarkup(rows)


def _category_keyboard(context, page=0):
    data = _wizard(context)
    categories = data.get('categories') or []
    selected = {int(x) for x in data.get('selected_categories') or []}
    pages = max(1, (len(categories) + CATEGORY_PAGE_SIZE - 1) // CATEGORY_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    start = page * CATEGORY_PAGE_SIZE
    batch = categories[start:start + CATEGORY_PAGE_SIZE]

    rows = []
    for category in batch:
        cid = int(category['id'])
        prefix = '✅' if cid in selected else '▫️'
        name = str(category.get('name') or f'#{cid}')
        rows.append([
            InlineKeyboardButton(
                f'{prefix} {name}',
                callback_data=f'pcat:toggle:{page}:{cid}',
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('⬅️ قبلی', callback_data=f'pcat:page:{page - 1}'))
    nav.append(InlineKeyboardButton(f'{page + 1}/{pages}', callback_data='pcat:noop'))
    if page < pages - 1:
        nav.append(InlineKeyboardButton('بعدی ➡️', callback_data=f'pcat:page:{page + 1}'))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(
            f'✅ تأیید ({len(selected)})',
            callback_data='pcat:done',
        ),
        InlineKeyboardButton('⏭ بدون دسته', callback_data='pcat:none'),
    ])
    return InlineKeyboardMarkup(rows), page


async def begin(update: Update, context):
    if not await main.guard(update):
        return ConversationHandler.END

    clear_state(update.effective_user.id)
    _reset(context)
    _wizard(context)

    status = await update.effective_message.reply_text('⏳ اتصال فروشگاه را بررسی می‌کنم…')
    try:
        await asyncio.to_thread(WooClient().probe)
    except Exception as exc:
        _reset(context)
        await status.edit_text(f'❌ اتصال WooCommerce آماده نیست:\n{exc}')
        await update.effective_message.reply_text('از تنظیمات اتصال را بررسی کن.', reply_markup=main.settings_menu())
        return ConversationHandler.END

    await status.edit_text('✅ فروشگاه متصل است.')
    await update.effective_message.reply_text(
        '1️⃣ نام محصول را بفرست.',
        reply_markup=_cancel_button(),
    )
    return NAME


async def cancel(update: Update, context):
    clear_state(update.effective_user.id)
    _reset(context)
    if update.callback_query:
        await update.callback_query.answer()
        message = update.callback_query.message
    else:
        message = update.effective_message
    await message.reply_text('لغو شد.', reply_markup=main.main_menu())
    return ConversationHandler.END


async def name_step(update: Update, context):
    text = (update.effective_message.text or '').strip()
    if not text:
        await update.effective_message.reply_text('نام محصول خالی نباشد.')
        return NAME
    _wizard(context)['payload']['name'] = text
    await update.effective_message.reply_text(
        '2️⃣ نوع محصول را انتخاب کن:',
        reply_markup=_type_keyboard(),
    )
    return TYPE


async def type_step(update: Update, context):
    query = update.callback_query
    await query.answer()
    product_type = query.data.split(':', 1)[1]
    if product_type not in {'simple', 'variable'}:
        return TYPE
    _wizard(context)['payload']['type'] = product_type
    label = 'ساده' if product_type == 'simple' else 'متغیر'
    await query.edit_message_text(f'2️⃣ نوع محصول: {label} ✅')
    await query.message.reply_text('3️⃣ قیمت اصلی را بفرست؛ فقط عدد.')
    return REGULAR_PRICE


async def regular_price_step(update: Update, context):
    try:
        value = main.clean_price(update.effective_message.text)
        if not value:
            raise ValueError('قیمت اصلی لازم است.')
    except Exception as exc:
        await update.effective_message.reply_text(f'❌ {exc}\nقیمت را دوباره بفرست.')
        return REGULAR_PRICE

    _wizard(context)['payload']['regular_price'] = value
    await update.effective_message.reply_text(
        '4️⃣ قیمت تخفیف را بفرست، یا دکمه «بدون تخفیف» را بزن.',
        reply_markup=_skip_keyboard('psale:none', '⏭ بدون تخفیف'),
    )
    return SALE_PRICE


def _sale_is_valid(payload, value):
    if not value:
        return
    try:
        if float(value) >= float(payload['regular_price']):
            raise ValueError('قیمت تخفیف باید کمتر از قیمت اصلی باشد.')
    except KeyError:
        return


async def sale_price_text(update: Update, context):
    payload = _wizard(context)['payload']
    try:
        value = main.clean_price(update.effective_message.text)
        _sale_is_valid(payload, value)
    except Exception as exc:
        await update.effective_message.reply_text(f'❌ {exc}\nقیمت تخفیف را دوباره بفرست یا «بدون تخفیف» را بزن.')
        return SALE_PRICE
    payload['sale_price'] = value
    await update.effective_message.reply_text('5️⃣ موجودی را بفرست؛ مثلاً 10.')
    return STOCK


async def sale_price_skip(update: Update, context):
    query = update.callback_query
    await query.answer('بدون تخفیف')
    _wizard(context)['payload']['sale_price'] = ''
    await query.edit_message_text('4️⃣ بدون تخفیف ✅')
    await query.message.reply_text('5️⃣ موجودی را بفرست؛ مثلاً 10.')
    return STOCK


async def stock_step(update: Update, context):
    text = (update.effective_message.text or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹', '0123456789')).strip()
    if not text.isdigit():
        await update.effective_message.reply_text('❌ موجودی باید عدد باشد؛ مثلاً 10.')
        return STOCK
    _wizard(context)['payload']['stock'] = int(text)
    await update.effective_message.reply_text(
        '6️⃣ وزن را بفرست، یا «بدون وزن» را بزن.',
        reply_markup=_skip_keyboard('pweight:none', '⏭ بدون وزن'),
    )
    return WEIGHT


async def _load_categories(context):
    data = _wizard(context)
    if data.get('categories'):
        return data['categories']
    categories = await asyncio.to_thread(WooClient().categories)
    categories = [
        {
            'id': int(item['id']),
            'name': str(item.get('name') or f"#{item['id']}"),
            'parent': int(item.get('parent') or 0),
        }
        for item in categories
        if item.get('id')
    ]
    categories.sort(key=lambda item: (item['parent'] != 0, item['name'].casefold()))
    data['categories'] = categories
    return categories


async def _ask_categories(message, context):
    categories = await _load_categories(context)
    if not categories:
        _wizard(context)['payload']['category_ids'] = []
        await message.reply_text('7️⃣ دسته‌بندی‌ای روی سایت پیدا نشد؛ رد شد.')
        return await _ask_description(message)

    keyboard, _ = _category_keyboard(context, 0)
    await message.reply_text(
        '7️⃣ دسته‌بندی‌ها را از لیست واقعی سایت انتخاب کن.\n'
        'می‌توانی چند مورد را تیک بزنی و بعد «تأیید» را بزنی.',
        reply_markup=keyboard,
    )
    return CATEGORIES


async def weight_text(update: Update, context):
    try:
        value = main.clean_price(update.effective_message.text)
    except Exception as exc:
        await update.effective_message.reply_text(f'❌ {exc}\nوزن را دوباره بفرست یا «بدون وزن» را بزن.')
        return WEIGHT
    _wizard(context)['payload']['weight'] = value
    try:
        return await _ask_categories(update.effective_message, context)
    except Exception as exc:
        log.exception('category load failed')
        await update.effective_message.reply_text(f'❌ دریافت دسته‌بندی‌ها ناموفق بود:\n{exc}')
        return WEIGHT


async def weight_skip(update: Update, context):
    query = update.callback_query
    await query.answer('بدون وزن')
    _wizard(context)['payload']['weight'] = ''
    await query.edit_message_text('6️⃣ بدون وزن ✅')
    try:
        return await _ask_categories(query.message, context)
    except Exception as exc:
        log.exception('category load failed')
        await query.message.reply_text(f'❌ دریافت دسته‌بندی‌ها ناموفق بود:\n{exc}')
        return WEIGHT


async def category_step(update: Update, context):
    query = update.callback_query
    data = _wizard(context)
    raw = query.data or ''

    if raw == 'pcat:noop':
        await query.answer()
        return CATEGORIES

    if raw == 'pcat:done':
        await query.answer('دسته‌بندی‌ها ثبت شد')
        selected = [int(x) for x in data.get('selected_categories') or []]
        data['payload']['category_ids'] = selected
        await query.edit_message_text(f'7️⃣ {len(selected)} دسته‌بندی انتخاب شد ✅')
        await _ask_description(query.message)
        return DESCRIPTION

    if raw == 'pcat:none':
        await query.answer('بدون دسته‌بندی')
        data['selected_categories'] = []
        data['payload']['category_ids'] = []
        await query.edit_message_text('7️⃣ بدون دسته‌بندی ✅')
        await _ask_description(query.message)
        return DESCRIPTION

    parts = raw.split(':')
    if len(parts) == 3 and parts[:2] == ['pcat', 'page']:
        page = int(parts[2])
        keyboard, _ = _category_keyboard(context, page)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return CATEGORIES

    if len(parts) == 4 and parts[:2] == ['pcat', 'toggle']:
        page = int(parts[2])
        cid = int(parts[3])
        selected = {int(x) for x in data.get('selected_categories') or []}
        if cid in selected:
            selected.remove(cid)
            action = 'برداشته شد'
        else:
            selected.add(cid)
            action = 'انتخاب شد'
        data['selected_categories'] = sorted(selected)
        keyboard, _ = _category_keyboard(context, page)
        name = next((c['name'] for c in data.get('categories') or [] if int(c['id']) == cid), str(cid))
        await query.answer(f'{name}: {action}')
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return CATEGORIES

    await query.answer()
    return CATEGORIES


async def _ask_description(message):
    await message.reply_text(
        '8️⃣ توضیحات محصول را بفرست، یا «بدون توضیحات» را بزن.',
        reply_markup=_skip_keyboard('pdesc:none', '⏭ بدون توضیحات'),
    )
    return DESCRIPTION


async def _after_description(message, context):
    payload = _wizard(context)['payload']
    if payload.get('type') == 'variable':
        await message.reply_text(
            '9️⃣ ویژگی‌ها را بفرست. هر ویژگی یک خط:\n'
            'رنگ: قرمز، آبی\n'
            'سایز: S, M, L'
        )
        return ATTRIBUTES
    return await _ask_photos(message, context)


async def description_text(update: Update, context):
    _wizard(context)['payload']['description'] = (update.effective_message.text or '').strip()
    return await _after_description(update.effective_message, context)


async def description_skip(update: Update, context):
    query = update.callback_query
    await query.answer('بدون توضیحات')
    _wizard(context)['payload']['description'] = ''
    await query.edit_message_text('8️⃣ بدون توضیحات ✅')
    return await _after_description(query.message, context)


async def attributes_step(update: Update, context):
    try:
        attrs = main.parse_attributes(update.effective_message.text)
    except Exception as exc:
        await update.effective_message.reply_text(f'❌ {exc}\nویژگی‌ها را دوباره بفرست.')
        return ATTRIBUTES
    _wizard(context)['payload']['attributes'] = attrs
    return await _ask_photos(update.effective_message, context)


async def _ask_photos(message, context):
    payload = _wizard(context)['payload']
    payload.setdefault('images', [])
    await message.reply_text(
        '📷 عکس‌های محصول را یکی‌یکی بفرست.\n'
        'اولین عکس = کاور محصول.\n'
        'وقتی تمام شد دکمه پایین را بزن.',
        reply_markup=_photos_keyboard(len(payload['images'])),
    )
    return PHOTOS


async def photo_step(update: Update, context):
    payload = _wizard(context)['payload']
    photo = update.effective_message.photo[-1]
    tg_file = await photo.get_file()
    progress = await update.effective_message.reply_text('⏳ در حال آپلود عکس روی Cutella…')

    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f'{secrets.token_hex(6)}.jpg'
            await tg_file.download_to_drive(custom_path=str(path))
            media = await asyncio.to_thread(WooClient().upload_media, path, path.name)
    except Exception as exc:
        log.exception('product media upload failed')
        text = str(exc)
        if 'Unknown operation' in text:
            text = (
                'نسخه Bot Bridge نصب‌شده روی وردپرس قدیمی است و عملیات آپلود عکس را نمی‌شناسد. '
                'کد بات درست است؛ افزونه Cutella Bot Bridge روی سایت باید با نسخه فعلی ریپو جایگزین شود.'
            )
        await progress.edit_text(f'❌ آپلود عکس ناموفق بود:\n{text}')
        await update.effective_message.reply_text(
            'می‌توانی بعد از اصلاح Bridge دوباره عکس بفرستی، یا فعلاً محصول را بدون عکس ثبت کنی.',
            reply_markup=_photos_keyboard(len(payload.get('images') or [])),
        )
        return PHOTOS

    payload.setdefault('images', []).append({'id': int(media['id'])})
    count = len(payload['images'])
    await progress.edit_text(f'✅ عکس {count} آپلود شد.')
    await update.effective_message.reply_text(
        'عکس بعدی را بفرست یا ثبت محصول را بزن.',
        reply_markup=_photos_keyboard(count),
    )
    return PHOTOS


async def _finalize(message, context, uid):
    payload = _wizard(context)['payload']
    client = WooClient()
    category_payload = [{'id': int(cid)} for cid in payload.get('category_ids') or []]
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

    progress = await message.reply_text('⏳ در حال ثبت محصول روی سایت…')
    try:
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
    except Exception as exc:
        log.exception('product finalize failed')
        await progress.edit_text(f'❌ ثبت محصول ناموفق بود:\n{exc}')
        await message.reply_text(
            'اطلاعات این محصول هنوز داخل ربات مانده؛ دوباره «ثبت محصول» را بزن یا لغو کن.',
            reply_markup=_photos_keyboard(len(payload.get('images') or [])),
        )
        return PHOTOS

    clear_state(uid)
    _reset(context)
    await progress.edit_text(
        f"✅ محصول ثبت شد\n#{product.get('id')} — {product.get('name')}\n{product.get('permalink', '')}"
    )
    await message.reply_text('آماده محصول بعدی.', reply_markup=main.main_menu())
    return ConversationHandler.END


async def photo_done(update: Update, context):
    query = update.callback_query
    await query.answer()
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    return await _finalize(query.message, context, update.effective_user.id)


def handler():
    cancel_text = MessageHandler(filters.Regex(CANCEL_RE), cancel)
    text = filters.TEXT & ~filters.COMMAND & ~filters.Regex(CANCEL_RE)

    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r'^🛍 ثبت محصول$'), begin),
        ],
        states={
            NAME: [cancel_text, MessageHandler(text, name_step)],
            TYPE: [CallbackQueryHandler(type_step, pattern=r'^ptype:(?:simple|variable)$')],
            REGULAR_PRICE: [cancel_text, MessageHandler(text, regular_price_step)],
            SALE_PRICE: [
                cancel_text,
                CallbackQueryHandler(sale_price_skip, pattern=r'^psale:none$'),
                MessageHandler(text, sale_price_text),
            ],
            STOCK: [cancel_text, MessageHandler(text, stock_step)],
            WEIGHT: [
                cancel_text,
                CallbackQueryHandler(weight_skip, pattern=r'^pweight:none$'),
                MessageHandler(text, weight_text),
            ],
            CATEGORIES: [
                cancel_text,
                CallbackQueryHandler(category_step, pattern=r'^pcat:'),
            ],
            DESCRIPTION: [
                cancel_text,
                CallbackQueryHandler(description_skip, pattern=r'^pdesc:none$'),
                MessageHandler(text, description_text),
            ],
            ATTRIBUTES: [cancel_text, MessageHandler(text, attributes_step)],
            PHOTOS: [
                cancel_text,
                CallbackQueryHandler(photo_done, pattern=r'^pphoto:done$'),
                MessageHandler(filters.PHOTO, photo_step),
            ],
        },
        fallbacks=[
            CommandHandler('cancel', cancel),
            MessageHandler(filters.Regex(CANCEL_RE), cancel),
        ],
        allow_reentry=True,
    )
