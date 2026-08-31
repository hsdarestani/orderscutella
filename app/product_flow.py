import asyncio
import base64
import hashlib
import logging
import re
import secrets
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, filters

from . import main
from .db import clear_state, get_state, set_state
from .integrations import WooClient

log = logging.getLogger('orderscutella.product')

# The visible product wizard mirrors OrdersVesta's current production UX.
ACTIVE = 1
CTX_KEY = 'cutella_vesta_product'
GALLERY_BATCHES = {}
GALLERY_DEBOUNCE_SECONDS = 1.15
GALLERY_IMAGE_CONCURRENCY = 2
MEDIA_CHUNK_SIZE = 5120

CATEGORY_PHRASE_ALIASES = (
    (re.compile(r'\bپاک\s*کننده(?:\s*ها)?\b'), 'شوینده'),
    (re.compile(r'\bفیس\s*واش\b'), 'شوینده'),
    (re.compile(r'\bاسکین\s*کر\b'), 'مراقبت پوست'),
)
CATEGORY_TOKEN_ALIASES = {
    'شستشو': 'شوینده',
    'شستشوی': 'شوینده',
    'پاککننده': 'شوینده',
    'کلینزر': 'شوینده',
    'پوستی': 'پوست',
    'مراقبتی': 'مراقبت',
    'آرایشی': 'آرایش',
    'بهداشتی': 'بهداشت',
    'موها': 'مو',
}
CATEGORY_STOPWORDS = {
    'از', 'با', 'برای', 'به', 'بندی', 'در', 'دسته', 'محصول', 'محصولات',
    'لوازم', 'انواع', 'و',
}


def _state(uid):
    state = get_state(uid)
    if state and state.get('flow') == 'woo_product':
        return state
    return None


def _save(uid, step, data):
    set_state(uid, 'woo_product', step, data)


def _b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def _fast_upload_media(path, filename):
    """Same signed chunk strategy used by OrdersVesta product UX."""
    client = WooClient()
    path = Path(path)
    data = path.read_bytes()
    if not data:
        raise RuntimeError('فایل تصویر خالی است.')

    upload_id = secrets.token_hex(16)
    digest = hashlib.sha256(data).hexdigest()
    safe_name = Path(filename or path.name).name or 'cutella-product.jpg'

    begin = client._signed_get('media_begin', {
        'upload_id': upload_id,
        'filename': safe_name,
        'size': len(data),
        'sha256': digest,
    })
    if begin.get('already_finished') and isinstance(begin.get('result'), dict):
        return begin['result']

    chunks = []
    for offset in range(0, len(data), MEDIA_CHUNK_SIZE):
        chunk = data[offset:offset + MEDIA_CHUNK_SIZE]
        chunks.append((offset, _b64url(chunk)))

    def send_chunk(item):
        offset, encoded = item
        return client._signed_get('media_chunk', {
            'upload_id': upload_id,
            'offset': offset,
            'data': encoded,
        })

    workers = max(1, min(6, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(send_chunk, item) for item in chunks]
        for future in as_completed(futures):
            future.result()

    return client._signed_get('media_finish', {'upload_id': upload_id})


def type_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('📦 ثابت (Simple)', callback_data='woo:type:simple'),
        InlineKeyboardButton('🎛 متغیر (Variable)', callback_data='woo:type:variable'),
    ]])


def gallery_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('⏭ بدون گالری', callback_data='woo:gallery:skip')],
        [InlineKeyboardButton('❌ لغو', callback_data='woo:cancel')],
    ])


def sale_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('بدون تخفیف', callback_data='woo:sale:skip'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def weight_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('⏭ بدون وزن', callback_data='woo:weight:skip'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def price_mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('💰 قیمت همه یکسان', callback_data='woo:var:price:same'),
        InlineKeyboardButton('🧾 قیمت جداگانه', callback_data='woo:var:price:separate'),
    ]])


def stock_mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('📦 موجودی همه یکسان', callback_data='woo:var:stock:same'),
        InlineKeyboardButton('🔢 موجودی جداگانه', callback_data='woo:var:stock:separate'),
    ]])


def variation_sale_keyboard(index=None, common=False):
    cb = 'woo:var:sale:common:skip' if common else f'woo:var:sale:{index}:skip'
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('بدون تخفیف', callback_data=cb),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def preview_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('🚀 ثبت در سایت', callback_data='woo:publish'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def _bar(done, total, width=10):
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    filled = round(width * done / total)
    pct = round(100 * done / total)
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


async def _safe_edit(message, text, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        pass


async def _upload_telegram_photo(bot, file_id, index=0):
    tg_file = await bot.get_file(file_id, read_timeout=60, connect_timeout=30)
    tmp = Path(tempfile.gettempdir()) / f'woo_{uuid.uuid4().hex}_{index}.jpg'
    try:
        await tg_file.download_to_drive(custom_path=str(tmp), read_timeout=120, connect_timeout=30)
        media = await asyncio.to_thread(_fast_upload_media, tmp, tmp.name)
        return {'id': int(media['id']), 'src': media.get('source_url', '')}
    finally:
        tmp.unlink(missing_ok=True)


def _parse_int(text):
    x = str(text or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    x = x.replace(',', '').replace('٬', '').strip()
    return int(x) if x.isdigit() else None


def _parse_weight(text):
    value = str(text or '').translate(str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789'))
    value = value.strip().replace(' ', '')
    if value.count(',') == 1 and '.' not in value:
        value = value.replace(',', '.')
    value = value.replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?', value):
        return None
    try:
        if float(value) <= 0:
            return None
    except Exception:
        return None
    return value


def _variations_summary(data):
    rows = []
    for v in data.get('variations', []):
        sale = v.get('sale_price') or '—'
        rows.append(
            f"• {v.get('option')}: موجودی {v.get('stock', 0)} | قیمت {v.get('regular_price', '—')} | تخفیف {sale}"
        )
    return '\n'.join(rows)


def _norm(value):
    return main.normalize_text(str(value or '')).strip().lower()


def _canonical_category_text(value):
    text = _norm(value)
    for pattern, replacement in CATEGORY_PHRASE_ALIASES:
        text = pattern.sub(replacement, text)
    tokens = [CATEGORY_TOKEN_ALIASES.get(token, token) for token in text.split()]
    return ' '.join(tokens)


def _meaningful_words(value):
    return [
        token for token in _canonical_category_text(value).split()
        if token and token not in CATEGORY_STOPWORDS
    ]


def _query_parts(text):
    raw = str(text or '').replace('\n', ',').replace('،', ',')
    return [part.strip() for part in raw.split(',') if part.strip()]


def _category_score(name, queries):
    n = _canonical_category_text(name)
    if not n:
        return 0
    nwords = _meaningful_words(n)
    ncompact = ''.join(nwords)
    ntokens = set(nwords)
    scores = []
    for query in queries:
        q = _canonical_category_text(query)
        if not q:
            continue
        qwords = _meaningful_words(q)
        qcompact = ''.join(qwords)
        qtokens = set(qwords)
        if ncompact == qcompact:
            score = 1200 + min(120, max(0, len(nwords) - 1) * 80)
        elif qcompact in ncompact:
            score = 900 - min(120, max(0, len(ncompact) - len(qcompact)))
        elif ncompact in qcompact:
            score = 820 + min(240, max(0, len(nwords) - 1) * 120)
        else:
            overlap = len(qtokens & ntokens)
            if overlap:
                query_coverage = overlap / max(1, len(qtokens))
                category_coverage = overlap / max(1, len(ntokens))
                score = round((query_coverage * 360) + (category_coverage * 300) + (overlap * 90))
            else:
                similarity = max(
                    (
                        SequenceMatcher(None, qtoken, ntoken).ratio()
                        for qtoken in qtokens if len(qtoken) >= 3
                        for ntoken in ntokens if len(ntoken) >= 3
                    ),
                    default=0,
                )
                score = round(similarity * 420) if similarity >= 0.72 else 0
        if score > 0:
            scores.append(score)
    if not scores:
        return 0
    scores.sort(reverse=True)
    return scores[0] + round(sum(scores[1:]) * 0.45)


def category_matches(cache, query, limit=14):
    parts = _query_parts(query)
    if not parts:
        return []
    scored = []
    for cat in cache:
        score = _category_score(cat.get('name', ''), parts)
        if score > 0:
            scored.append((score, len(str(cat.get('name', ''))), str(cat.get('name', '')), cat))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [row[3] for row in scored[:limit]]


def category_search_keyboard(results, selected):
    rows = []
    selected = set(int(x) for x in selected)
    for cat in results:
        cid = int(cat['id'])
        mark = '✅' if cid in selected else '▫️'
        rows.append([InlineKeyboardButton(f'{mark} {cat["name"]}', callback_data=f'woo:catsearch:{cid}')])
    rows.append([InlineKeyboardButton(f'✅ تأیید دسته‌بندی‌ها ({len(selected)})', callback_data='woo:catdone')])
    if selected:
        rows.append([InlineKeyboardButton('🧹 پاک کردن انتخاب‌ها', callback_data='woo:catclear')])
    return InlineKeyboardMarkup(rows)


def selected_category_names(data):
    by_id = {int(x['id']): x['name'] for x in data.get('category_cache', [])}
    return [by_id.get(int(cid), str(cid)) for cid in data.get('categories', [])]


async def send_categories(chat, uid):
    state = _state(uid)
    if not state:
        return
    data = state['payload']
    if not data.get('category_cache'):
        try:
            cats = await asyncio.to_thread(WooClient().categories)
        except Exception as exc:
            return await chat.send_message(f'❌ دریافت دسته‌بندی‌ها ناموفق بود: {exc}')
        data['category_cache'] = [
            {'id': int(x['id']), 'name': x['name'], 'parent': int(x.get('parent') or 0)}
            for x in cats if x.get('id')
        ]

    _save(uid, 'categories', data)
    chosen = selected_category_names(data)
    suffix = f'\n\nانتخاب‌شده: {"، ".join(chosen)}' if chosen else ''
    await chat.send_message(
        '🔎 دسته‌بندی محصول را جستجو کنید.\n\n'
        'اسم یک یا چند دسته را تایپ کنید؛ با ویرگول جدا کنید.\n'
        'مثال: «رژ لب، آرایش صورت»\n\n'
        'ربات نزدیک‌ترین دسته‌بندی‌های واقعی سایت را پیشنهاد می‌دهد و می‌توانید چند مورد را انتخاب کنید.'
        + suffix
    )


async def _ask_short_description(chat, uid, data, intro=None):
    _save(uid, 'short_description', data)
    text = '✍️ کپشن / توضیح کوتاه محصول را بفرستید.\nاین متن در فیلد «توضیح کوتاه محصول» ووکامرس ذخیره می‌شود.'
    if intro:
        text = intro + '\n\n' + text
    await chat.send_message(text)


async def _after_gallery(chat, uid, data):
    cover = data.get('cover')
    gallery = list(data.get('gallery') or [])
    data['images'] = ([cover] if cover else []) + gallery

    if data.get('type') == 'variable':
        _save(uid, 'var_attribute_name', data)
        await chat.send_message(
            f'✅ تصاویر کامل شد.\nکاور: 1\nگالری: {len(gallery)}\n\n'
            '🎛 محصول متغیر است. نام ویژگی Variation را بفرستید؛ مثلاً:\nرنگ\nحجم\nمدل'
        )
    else:
        _save(uid, 'stock', data)
        await chat.send_message(
            f'✅ تصاویر کامل شد.\nکاور: 1\nگالری: {len(gallery)}\n\n'
            '📦 موجودی محصول چند عدد است؟'
        )


async def _process_gallery_batch(uid, chat_id, group_id, bot):
    try:
        await asyncio.sleep(GALLERY_DEBOUNCE_SECONDS)
        batch = GALLERY_BATCHES.get(uid)
        if not batch or batch.get('group_id') != group_id:
            return
        file_ids = list(batch.get('file_ids') or [])
        GALLERY_BATCHES.pop(uid, None)

        state = _state(uid)
        if not state or state.get('step') != 'gallery':
            return
        data = state['payload']
        if not file_ids:
            return

        _save(uid, 'gallery_uploading', data)
        progress = await bot.send_message(chat_id, f'📤 در حال آپلود گالری…\n0/{len(file_ids)} عکس\n{_bar(0, len(file_ids))}')

        sem = asyncio.Semaphore(GALLERY_IMAGE_CONCURRENCY)

        async def worker(idx, fid):
            async with sem:
                media = await _upload_telegram_photo(bot, fid, idx)
                return idx, media

        tasks = [asyncio.create_task(worker(i, fid)) for i, fid in enumerate(file_ids)]
        results = {}
        failures = []
        completed = 0
        for future in asyncio.as_completed(tasks):
            try:
                idx, media = await future
                results[idx] = media
            except Exception as exc:
                failures.append(str(exc))
            completed += 1
            await _safe_edit(progress, f'📤 در حال آپلود گالری…\n{completed}/{len(file_ids)} عکس\n{_bar(completed, len(file_ids))}')

        uploaded = [results[i] for i in sorted(results)]
        data.setdefault('gallery', []).extend(uploaded)
        data['images'] = ([data['cover']] if data.get('cover') else []) + list(data.get('gallery') or [])

        if failures:
            _save(uid, 'gallery', data)
            detail = failures[0]
            if 'Unknown operation' in detail:
                detail = 'نسخه Cutella Bot Bridge نصب‌شده روی وردپرس قدیمی است و آپلود عکس را پشتیبانی نمی‌کند.'
            await _safe_edit(
                progress,
                f'⚠️ آپلود گالری کامل نشد.\nموفق: {len(uploaded)}/{len(file_ids)}\n{_bar(len(uploaded), len(file_ids))}\n\n{detail}\nعکس‌های ناموفق را دوباره یکجا بفرستید.'
            )
            return

        await _safe_edit(progress, f'✅ گالری آپلود شد.\n{len(uploaded)}/{len(file_ids)} عکس\n{_bar(len(file_ids), len(file_ids))}')
        await _after_gallery(await bot.get_chat(chat_id), uid, data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state = _state(uid)
        if state:
            _save(uid, 'gallery', state['payload'])
        try:
            text = str(exc)
            if 'Unknown operation' in text:
                text = 'نسخه Cutella Bot Bridge نصب‌شده روی وردپرس قدیمی است و آپلود عکس را پشتیبانی نمی‌کند.'
            await bot.send_message(chat_id, f'❌ آپلود گالری ناموفق بود: {text}\nدوباره آلبوم را بفرستید.')
        except Exception:
            pass


async def begin(update: Update, context):
    if not await main.guard(update):
        return ConversationHandler.END
    uid = update.effective_user.id
    clear_state(uid)
    context.user_data.pop(CTX_KEY, None)

    try:
        client = WooClient()
        if not client.base_url or not client.bridge_token:
            raise RuntimeError('اتصال ووکامرس هنوز تنظیم نشده است.')
    except Exception as exc:
        await update.effective_message.reply_text(
            f'❌ اتصال ووکامرس هنوز تنظیم نشده است. ابتدا «🔌 اتصال ووکامرس» را انجام دهید.\n{exc}',
            reply_markup=main.settings_menu(),
        )
        return ConversationHandler.END

    data = {'images': [], 'cover': None, 'gallery': [], 'categories': [], 'category_page': 0}
    _save(uid, 'name', data)
    await update.effective_message.reply_text('➕ ثبت محصول جدید\n\nاسم محصول را بفرستید.', reply_markup=main.cancel_menu())
    return ACTIVE


async def cancel(update: Update, context):
    uid = update.effective_user.id
    clear_state(uid)
    context.user_data.pop(CTX_KEY, None)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text('❌ عملیات لغو شد.')
        except Exception:
            pass
        await update.callback_query.message.reply_text('منوی اصلی:', reply_markup=main.main_menu())
    else:
        await update.effective_message.reply_text('منوی اصلی:', reply_markup=main.main_menu())
    return ConversationHandler.END


async def photo(update: Update, context):
    if not await main.guard(update):
        return
    uid = update.effective_user.id
    state = _state(uid)
    if not state:
        return await update.effective_message.reply_text('الان منتظر عکس محصول نیستم.', reply_markup=main.main_menu())

    step = state['step']
    data = state['payload']

    if step == 'cover':
        if update.message.media_group_id:
            return await update.message.reply_text('کاور باید فقط یک عکس باشد. لطفاً یک عکس جداگانه بفرستید.')
        status = await update.message.reply_text('📤 آپلود کاور…\n░░░░░░░░░░ 10%')
        try:
            p = update.message.photo[-1]
            tg_file = await p.get_file(read_timeout=60, connect_timeout=30)
            await _safe_edit(status, '📤 آپلود کاور…\n███░░░░░░░ 30%')
            tmp = Path(tempfile.gettempdir()) / f'woo_cover_{uuid.uuid4().hex}.jpg'
            try:
                await tg_file.download_to_drive(custom_path=str(tmp), read_timeout=120, connect_timeout=30)
                await _safe_edit(status, '📤 آپلود کاور…\n█████░░░░░ 50%')
                media = await asyncio.to_thread(_fast_upload_media, tmp, tmp.name)
            finally:
                tmp.unlink(missing_ok=True)

            cover = {'id': int(media['id']), 'src': media.get('source_url', '')}
            data['cover'] = cover
            data['gallery'] = []
            data['images'] = [cover]
            _save(uid, 'gallery', data)
            await _safe_edit(status, '✅ کاور آپلود شد.\n██████████ 100%')
            return await update.message.reply_text(
                '🖼 حالا عکس‌های گالری را **یکجا به صورت آلبوم** بفرستید.\n\n'
                'همه عکس‌ها را در تلگرام با هم انتخاب و Send کنید؛ ربات بعد از دریافت کل آلبوم آن‌ها را موازی آپلود می‌کند و Progress را نشان می‌دهد.\n\n'
                'اگر گالری ندارید «بدون گالری» را بزنید.',
                reply_markup=gallery_keyboard(),
                parse_mode='Markdown',
            )
        except Exception as exc:
            text = str(exc)
            if 'Unknown operation' in text:
                text = 'نسخه Cutella Bot Bridge نصب‌شده روی وردپرس قدیمی است و عملیات آپلود عکس را نمی‌شناسد. افزونه روی سایت باید با نسخه فعلی هماهنگ شود.'
            await _safe_edit(status, f'❌ آپلود کاور ناموفق بود: {text}')
            return

    if step == 'gallery':
        file_id = update.message.photo[-1].file_id
        group_id = str(update.message.media_group_id or f'single-{update.message.message_id}')
        batch = GALLERY_BATCHES.get(uid)
        if batch and batch.get('group_id') != group_id:
            return await update.message.reply_text('⏳ یک آلبوم در حال دریافت است؛ چند لحظه صبر کنید.')
        if not batch:
            batch = {'group_id': group_id, 'file_ids': [], 'task': None}
            GALLERY_BATCHES[uid] = batch
        if file_id not in batch['file_ids']:
            batch['file_ids'].append(file_id)
        old_task = batch.get('task')
        if old_task and not old_task.done():
            old_task.cancel()
        batch['task'] = asyncio.create_task(_process_gallery_batch(uid, update.effective_chat.id, group_id, context.bot))
        return

    if step == 'gallery_uploading':
        return await update.message.reply_text('⏳ گالری در حال آپلود است؛ Progress را بالا می‌بینید.')

    return await update.message.reply_text('برای ادامه از مرحله فعلی استفاده کنید.')


async def _ask_stock_mode(chat, uid, data):
    _save(uid, 'var_stock_mode', data)
    await chat.send_message(
        '📦 موجودی Variationها چطور است؟\n\nاگر همه یک تعداد دارند «موجودی همه یکسان» را بزنید؛ اگر فرق دارند «موجودی جداگانه» را انتخاب کنید.',
        reply_markup=stock_mode_keyboard(),
    )


async def _finish_variations(chat, uid, data):
    data['stock'] = sum(int(v.get('stock', 0)) for v in data.get('variations', []))
    await _ask_short_description(chat, uid, data, '✅ Variationها کامل شدند:\n\n' + _variations_summary(data))


async def product_preview(chat, uid):
    state = _state(uid)
    if not state:
        return
    data = state['payload']
    cats = {int(x['id']): x['name'] for x in data.get('category_cache', [])}
    selected = [cats.get(int(i), str(i)) for i in data.get('categories', [])]
    gallery_count = len(data.get('gallery') or [])
    caption = str(data.get('short_description') or '')
    if len(caption) > 220:
        caption = caption[:217] + '…'

    if data.get('type') == 'variable':
        pricing = f'🎛 ویژگی: {data.get("attribute_name", "—")}\n{_variations_summary(data)}'
        typ = 'متغیر'
    else:
        pricing = f'موجودی: {data.get("stock", 0)}\nقیمت اصلی: {data.get("regular_price", "—")}\nقیمت تخفیف: {data.get("sale_price") or "—"}'
        typ = 'ثابت'

    text = (
        '🧾 پیش‌نمایش محصول\n\n'
        f'نام: {data.get("name")}\nنوع: {typ}\n{pricing}\n'
        f'وزن: {data.get("weight", "—") or "—"}\n'
        f'کاور: {"✅" if data.get("cover") else "❌"}\nگالری: {gallery_count} عکس\n'
        f'دسته‌بندی: {"، ".join(selected) if selected else "بدون دسته"}\n\n'
        f'کپشن:\n{caption}\n\nاگر اطلاعات درست است ثبت نهایی را بزنید.'
    )
    _save(uid, 'preview', data)
    await chat.send_message(text, reply_markup=preview_keyboard())


async def _publish(chat, uid):
    state = _state(uid)
    if not state:
        return
    data = state['payload']
    client = WooClient()

    common = {
        'name': data['name'],
        'status': 'publish',
        'categories': [{'id': int(i)} for i in data.get('categories', [])],
        'images': [{'id': int(x['id'])} for x in data.get('images', [])],
        'short_description': str(data.get('short_description') or ''),
        'weight': str(data.get('weight') or ''),
    }

    if data.get('type') == 'variable':
        attr_name = data.get('attribute_name') or 'گزینه'
        values = [v['option'] for v in data.get('variations', [])]
        parent_payload = dict(common)
        parent_payload.update({
            'type': 'variable',
            'attributes': [{'name': attr_name, 'visible': True, 'variation': True, 'options': values}],
        })
        product = await asyncio.to_thread(client.create_product, parent_payload)
        pid = int(product['id'])
        created = []
        try:
            for v in data.get('variations', []):
                payload = {
                    'regular_price': str(v.get('regular_price') or ''),
                    'manage_stock': True,
                    'stock_quantity': int(v.get('stock') or 0),
                    'attributes': [{'name': attr_name, 'option': v['option']}],
                }
                if v.get('sale_price'):
                    payload['sale_price'] = str(v['sale_price'])
                child = await asyncio.to_thread(client.create_variation, pid, payload)
                created.append(child)
        except Exception as exc:
            raise RuntimeError(f'محصول مادر #{pid} ساخته شد ولی ساخت Variationها در {len(created)}/{len(values)} متوقف شد: {exc}')
        clear_state(uid)
        await chat.send_message(
            f'✅ محصول متغیر با موفقیت ثبت شد.\n\n#{pid} — {product.get("name")}\nVariationها: {len(created)}\n{product.get("permalink", "")}',
            reply_markup=main.main_menu(),
        )
        return product

    payload = dict(common)
    payload.update({
        'type': 'simple',
        'manage_stock': True,
        'stock_quantity': int(data.get('stock', 0)),
        'regular_price': str(data.get('regular_price', '')),
    })
    if data.get('sale_price'):
        payload['sale_price'] = str(data['sale_price'])
    product = await asyncio.to_thread(client.create_product, payload)
    clear_state(uid)
    await chat.send_message(
        f'✅ محصول ثبت شد.\n\n#{product.get("id")} — {product.get("name")}\n{product.get("permalink", "")}',
        reply_markup=main.main_menu(),
    )
    return product


async def text(update: Update, context):
    if not await main.guard(update):
        return ConversationHandler.END
    uid = update.effective_user.id
    value = (update.effective_message.text or '').strip()

    if value in {'❌ لغو عملیات', '❌ لغو', '⬅️ منوی اصلی'}:
        return await cancel(update, context)

    state = _state(uid)
    if not state:
        return ConversationHandler.END
    data = state['payload']
    step = state['step']

    if step == 'name':
        if not value:
            await update.message.reply_text('اسم محصول را بفرستید.')
            return ACTIVE
        data['name'] = value
        _save(uid, 'type', data)
        await update.message.reply_text('نوع محصول را انتخاب کنید:', reply_markup=type_keyboard())
        return ACTIVE

    if step == 'stock':
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('موجودی را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        data['stock'] = x
        _save(uid, 'regular_price', data)
        await update.message.reply_text('قیمت اصلی را به عدد بفرستید. مثال: 1250000')
        return ACTIVE

    if step == 'regular_price':
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('قیمت اصلی را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        data['regular_price'] = str(x)
        _save(uid, 'sale_price', data)
        await update.message.reply_text('قیمت تخفیف را بفرستید؛ اگر تخفیف ندارد «بدون تخفیف» را بزنید.', reply_markup=sale_keyboard())
        return ACTIVE

    if step == 'sale_price':
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        if x >= int(data.get('regular_price') or 0):
            await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
            return ACTIVE
        data['sale_price'] = str(x)
        await _ask_short_description(update.effective_chat, uid, data)
        return ACTIVE

    if step == 'var_attribute_name':
        if not value:
            await update.message.reply_text('نام ویژگی را وارد کنید؛ مثلاً «رنگ» یا «حجم».')
            return ACTIVE
        data['attribute_name'] = value
        _save(uid, 'var_attribute_values', data)
        await update.message.reply_text(f'مقادیر «{value}» را بفرستید.\nبا ویرگول یا هرکدام در یک خط. مثال:\nقرمز، صورتی، نود')
        return ACTIVE

    if step == 'var_attribute_values':
        raw = value.replace('\n', ',').replace('،', ',')
        values, seen = [], set()
        for part in raw.split(','):
            item = part.strip()
            key = _norm(item).replace(' ', '')
            if item and key and key not in seen:
                seen.add(key)
                values.append(item)
        if len(values) < 2:
            await update.message.reply_text('برای محصول متغیر حداقل دو مقدار بفرستید؛ مثلاً «قرمز، صورتی».')
            return ACTIVE
        data['attribute_values'] = values
        data['variations'] = [{'option': x, 'regular_price': '', 'sale_price': '', 'stock': 0} for x in values]
        _save(uid, 'var_price_mode', data)
        await update.message.reply_text(
            '💰 قیمت Variationها چطور است؟\n\nاگر قیمت همه یکی است یک بار می‌گیریم و روی همه اعمال می‌کنیم؛ اگر فرق دارد یکی‌یکی می‌پرسیم.',
            reply_markup=price_mode_keyboard(),
        )
        return ACTIVE

    if step == 'var_regular_common':
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('قیمت اصلی را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        data['var_common_regular'] = str(x)
        _save(uid, 'var_sale_common', data)
        await update.message.reply_text('قیمت تخفیف مشترک را بفرستید؛ اگر تخفیف ندارند «بدون تخفیف» را بزنید.', reply_markup=variation_sale_keyboard(common=True))
        return ACTIVE

    if step == 'var_sale_common':
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        if x >= int(data['var_common_regular']):
            await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
            return ACTIVE
        for variation in data.get('variations', []):
            variation['regular_price'] = data['var_common_regular']
            variation['sale_price'] = str(x)
        data.pop('var_common_regular', None)
        await _ask_stock_mode(update.effective_chat, uid, data)
        return ACTIVE

    if step == 'var_regular_each':
        idx = int(data.get('var_index', 0))
        variants = data.get('variations', [])
        if idx >= len(variants):
            await _ask_stock_mode(update.effective_chat, uid, data)
            return ACTIVE
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('قیمت اصلی را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        variants[idx]['regular_price'] = str(x)
        _save(uid, 'var_sale_each', data)
        await update.message.reply_text(f'قیمت تخفیف «{variants[idx]["option"]}» را بفرستید؛ یا «بدون تخفیف» را بزنید.', reply_markup=variation_sale_keyboard(index=idx))
        return ACTIVE

    if step == 'var_sale_each':
        idx = int(data.get('var_index', 0))
        variants = data.get('variations', [])
        if idx >= len(variants):
            await _ask_stock_mode(update.effective_chat, uid, data)
            return ACTIVE
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        if x >= int(variants[idx]['regular_price']):
            await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
            return ACTIVE
        variants[idx]['sale_price'] = str(x)
        idx += 1
        data['var_index'] = idx
        if idx >= len(variants):
            await _ask_stock_mode(update.effective_chat, uid, data)
            return ACTIVE
        _save(uid, 'var_regular_each', data)
        await update.message.reply_text(f'قیمت اصلی «{variants[idx]["option"]}» را بفرستید.')
        return ACTIVE

    if step == 'var_stock_common':
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('موجودی را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        for variation in data.get('variations', []):
            variation['stock'] = x
        await _finish_variations(update.effective_chat, uid, data)
        return ACTIVE

    if step == 'var_stock_each':
        idx = int(data.get('var_index', 0))
        variants = data.get('variations', [])
        x = _parse_int(value)
        if x is None:
            await update.message.reply_text('موجودی را فقط به صورت عدد وارد کنید.')
            return ACTIVE
        variants[idx]['stock'] = x
        idx += 1
        data['var_index'] = idx
        if idx >= len(variants):
            await _finish_variations(update.effective_chat, uid, data)
            return ACTIVE
        _save(uid, 'var_stock_each', data)
        await update.message.reply_text(f'موجودی «{variants[idx]["option"]}» چند عدد است؟')
        return ACTIVE

    if step == 'short_description':
        if not value:
            await update.message.reply_text('کپشن نمی‌تواند خالی باشد؛ توضیح کوتاه محصول را بفرستید.')
            return ACTIVE
        data['short_description'] = value
        _save(uid, 'weight', data)
        await update.message.reply_text(
            '⚖️ وزن محصول را عددی وارد کنید.\nعدد باید مطابق واحد وزن تنظیم‌شده در ووکامرس سایت باشد؛ مثال: 0.25 یا 250.\n\nاگر این محصول وزن ندارد، «بدون وزن» را بزنید.',
            reply_markup=weight_keyboard(),
        )
        return ACTIVE

    if step == 'weight':
        weight = _parse_weight(value)
        if weight is None:
            await update.message.reply_text('وزن را فقط به صورت عدد مثبت وارد کنید؛ مثال: 0.25 یا 250.')
            return ACTIVE
        data['weight'] = weight
        await send_categories(update.effective_chat, uid)
        return ACTIVE

    if step == 'categories':
        results = category_matches(data.get('category_cache', []), value)
        if not results:
            await update.message.reply_text('چیزی نزدیک به این عبارت پیدا نکردم. کوتاه‌تر بنویسید؛ مثلاً «رژ»، «پوست»، «مو»، «آرایش صورت».')
            return ACTIVE
        data['category_query'] = value
        _save(uid, 'categories', data)
        chosen = selected_category_names(data)
        chosen_text = f'\nانتخاب‌شده: {"، ".join(chosen)}' if chosen else ''
        await update.message.reply_text(
            f'پیشنهادهای نزدیک به «{value}»:{chosen_text}\n\nدسته‌های موردنظر را تیک بزنید. برای دسته‌های بیشتر دوباره تایپ کنید.',
            reply_markup=category_search_keyboard(results, data.get('categories', [])),
        )
        return ACTIVE

    await update.message.reply_text('برای ادامه از دکمه‌های همین مرحله استفاده کنید.')
    return ACTIVE


async def callback(update: Update, context):
    q = update.callback_query
    cb = q.data or ''
    uid = q.from_user.id
    if not await main.guard(update):
        return ConversationHandler.END

    state = _state(uid)
    if cb == 'woo:cancel':
        return await cancel(update, context)
    if not state:
        await q.answer('این عملیات منقضی شده است.', show_alert=True)
        return ConversationHandler.END

    data = state['payload']
    step = state['step']

    if cb.startswith('woo:type:') and step == 'type':
        await q.answer()
        typ = cb.split(':', 2)[-1]
        data['type'] = typ
        data['cover'], data['gallery'], data['images'] = None, [], []
        _save(uid, 'cover', data)
        await q.edit_message_text('✅ نوع محصول: ' + ('متغیر' if typ == 'variable' else 'ثابت'))
        await q.message.reply_text('🖼 اول **فقط عکس اصلی / کاور محصول** را بفرستید.\nگالری را در مرحله بعد جدا و یکجا می‌گیریم.', parse_mode='Markdown')
        return ACTIVE

    if cb == 'woo:gallery:skip' and step == 'gallery':
        await q.answer()
        data['gallery'] = []
        data['images'] = [data['cover']] if data.get('cover') else []
        await q.edit_message_text('⏭ محصول بدون گالری ادامه پیدا می‌کند.')
        await _after_gallery(q.message.chat, uid, data)
        return ACTIVE

    if cb == 'woo:sale:skip' and step == 'sale_price' and data.get('type') != 'variable':
        await q.answer()
        data['sale_price'] = ''
        await q.edit_message_text('بدون تخفیف ثبت شد.')
        await _ask_short_description(q.message.chat, uid, data)
        return ACTIVE

    if cb == 'woo:weight:skip' and step == 'weight':
        await q.answer()
        data['weight'] = ''
        await q.edit_message_text('⏭ وزن برای این محصول ثبت نمی‌شود.')
        await send_categories(q.message.chat, uid)
        return ACTIVE

    if cb == 'woo:var:price:same' and step == 'var_price_mode':
        await q.answer()
        _save(uid, 'var_regular_common', data)
        await q.edit_message_text('💰 قیمت همه Variationها یکسان است.')
        await q.message.reply_text('قیمت اصلی مشترک را بفرستید.')
        return ACTIVE

    if cb == 'woo:var:price:separate' and step == 'var_price_mode':
        await q.answer()
        data['var_index'] = 0
        _save(uid, 'var_regular_each', data)
        await q.edit_message_text('🧾 قیمت هر Variation جداگانه ثبت می‌شود.')
        await q.message.reply_text(f'قیمت اصلی «{data["variations"][0]["option"]}» را بفرستید.')
        return ACTIVE

    if cb == 'woo:var:sale:common:skip' and step == 'var_sale_common':
        await q.answer()
        regular = data.get('var_common_regular', '')
        for v in data.get('variations', []):
            v['regular_price'], v['sale_price'] = regular, ''
        data.pop('var_common_regular', None)
        await q.edit_message_text('بدون تخفیف برای همه Variationها ثبت شد.')
        await _ask_stock_mode(q.message.chat, uid, data)
        return ACTIVE

    if cb.startswith('woo:var:sale:') and cb.endswith(':skip') and step == 'var_sale_each':
        await q.answer()
        idx = int(cb.split(':')[3])
        variants = data.get('variations', [])
        if idx < len(variants):
            variants[idx]['sale_price'] = ''
        idx += 1
        data['var_index'] = idx
        await q.edit_message_text('بدون تخفیف ثبت شد.')
        if idx >= len(variants):
            await _ask_stock_mode(q.message.chat, uid, data)
            return ACTIVE
        _save(uid, 'var_regular_each', data)
        await q.message.reply_text(f'قیمت اصلی «{variants[idx]["option"]}» را بفرستید.')
        return ACTIVE

    if cb == 'woo:var:stock:same' and step == 'var_stock_mode':
        await q.answer()
        _save(uid, 'var_stock_common', data)
        await q.edit_message_text('📦 موجودی همه Variationها یکسان است.')
        await q.message.reply_text('موجودی مشترک هر Variation چند عدد است؟')
        return ACTIVE

    if cb == 'woo:var:stock:separate' and step == 'var_stock_mode':
        await q.answer()
        data['var_index'] = 0
        _save(uid, 'var_stock_each', data)
        await q.edit_message_text('🔢 موجودی هر Variation جداگانه ثبت می‌شود.')
        await q.message.reply_text(f'موجودی «{data["variations"][0]["option"]}» چند عدد است؟')
        return ACTIVE

    if cb.startswith('woo:catsearch:') or cb == 'woo:catclear':
        if step != 'categories':
            await q.answer('این مرحله منقضی شده است.', show_alert=True)
            return ACTIVE
        await q.answer()
        selected = set(int(x) for x in data.get('categories', []))
        if cb == 'woo:catclear':
            selected.clear()
        else:
            cid = int(cb.rsplit(':', 1)[-1])
            selected.remove(cid) if cid in selected else selected.add(cid)
        data['categories'] = sorted(selected)
        _save(uid, 'categories', data)
        query = data.get('category_query', '')
        results = category_matches(data.get('category_cache', []), query)
        chosen = selected_category_names(data)
        chosen_text = '، '.join(chosen) if chosen else 'هیچ‌کدام'
        await q.edit_message_text(
            f'پیشنهادهای نزدیک به «{query}»\nانتخاب‌شده: {chosen_text}\n\nبرای دسته‌های دیگر دوباره اسمشان را تایپ کنید.',
            reply_markup=category_search_keyboard(results, selected),
        )
        return ACTIVE

    if cb == 'woo:catdone' and step == 'categories':
        await q.answer()
        await q.edit_message_text('✅ دسته‌بندی‌ها انتخاب شدند.')
        await product_preview(q.message.chat, uid)
        return ACTIVE

    if cb == 'woo:publish' and step == 'preview':
        await q.answer()
        await q.edit_message_text('⏳ در حال ثبت محصول در ووکامرس…')
        try:
            await _publish(q.message.chat, uid)
            return ConversationHandler.END
        except Exception as exc:
            log.exception('publish failed')
            await q.message.reply_text(f'❌ ثبت محصول ناموفق بود: {exc}')
            return ACTIVE

    await q.answer()
    return ACTIVE


def handler():
    cancel_text = MessageHandler(filters.Regex(r'^(?:❌ لغو عملیات|❌ لغو|⬅️ منوی اصلی)$'), cancel)
    return ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r'^(?:🛍 ثبت محصول|➕ ثبت محصول جدید)$'), begin)],
        states={
            ACTIVE: [
                cancel_text,
                CallbackQueryHandler(callback, pattern=r'^woo:'),
                MessageHandler(filters.PHOTO, photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, text),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel), cancel_text],
        allow_reentry=True,
        per_message=False,
    )
