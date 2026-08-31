from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ConversationHandler

from . import main
from . import product_flow as pf
from .integrations import WooClient


PER_PAGE = 7
ORIG_CALLBACK = pf.callback
ORIG_SEARCH_KEYBOARD = pf.category_search_keyboard


def _browse_keyboard(data, page=0):
    categories = list(data.get('category_cache') or [])
    selected = set(int(x) for x in data.get('categories', []))
    pages = max(1, (len(categories) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(int(page), pages - 1))

    start = page * PER_PAGE
    rows = []
    for cat in categories[start:start + PER_PAGE]:
        cid = int(cat['id'])
        mark = '✅' if cid in selected else '▫️'
        rows.append([
            InlineKeyboardButton(
                f'{mark} {cat["name"]}',
                callback_data=f'woo:catbrowse:{cid}:{page}',
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('◀️ قبلی', callback_data=f'woo:catpage:{page - 1}'))
    nav.append(InlineKeyboardButton(f'{page + 1}/{pages}', callback_data='woo:catnoop'))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton('بعدی ▶️', callback_data=f'woo:catpage:{page + 1}'))
    rows.append(nav)

    rows.append([
        InlineKeyboardButton(
            f'✅ تأیید دسته‌بندی‌ها ({len(selected)})',
            callback_data='woo:catdone',
        )
    ])
    if selected:
        rows.append([
            InlineKeyboardButton('🧹 پاک کردن انتخاب‌ها', callback_data='woo:catbrowseclear')
        ])
    return InlineKeyboardMarkup(rows)


def category_search_keyboard(results, selected):
    """Keep Vesta fuzzy search, with an easy way back to the full category list."""
    original = ORIG_SEARCH_KEYBOARD(results, selected)
    rows = [list(row) for row in original.inline_keyboard]
    rows.append([
        InlineKeyboardButton('📚 نمایش همه دسته‌ها', callback_data='woo:catbrowse:show')
    ])
    return InlineKeyboardMarkup(rows)


async def send_categories(chat, uid):
    state = pf._state(uid)
    if not state:
        return
    data = state['payload']

    if not data.get('category_cache'):
        try:
            cats = await pf.asyncio.to_thread(WooClient().categories)
        except Exception as exc:
            return await chat.send_message(f'❌ دریافت دسته‌بندی‌ها ناموفق بود: {exc}')
        data['category_cache'] = [
            {
                'id': int(x['id']),
                'name': x['name'],
                'parent': int(x.get('parent') or 0),
            }
            for x in cats if x.get('id')
        ]

    # Stable alphabetical browsing makes the list easier to scan while search
    # continues to rank fuzzy/related names separately.
    data['category_cache'].sort(key=lambda x: (str(x.get('name') or '').casefold(), int(x['id'])))
    data['category_page'] = 0
    pf._save(uid, 'categories', data)

    chosen = pf.selected_category_names(data)
    chosen_text = f'\n\nانتخاب‌شده: {"، ".join(chosen)}' if chosen else ''
    await chat.send_message(
        '📚 دسته‌بندی محصول را انتخاب کنید.\n\n'
        'دسته‌های واقعی سایت همین پایین نمایش داده شده‌اند؛ با «قبلی / بعدی» بین صفحه‌ها بروید و هر تعداد لازم است تیک بزنید.\n\n'
        '🔎 اگر پیدا کردنش سخت بود، فقط یک کلمه مثل «پوست»، «مو» یا «رژ» تایپ کنید تا دسته‌های مشابه و مرتبط پیشنهاد شوند.'
        + chosen_text,
        reply_markup=_browse_keyboard(data, 0),
    )


async def _render_browse(q, uid, data, page):
    pages = max(1, (len(data.get('category_cache') or []) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(int(page), pages - 1))
    data['category_page'] = page
    pf._save(uid, 'categories', data)
    chosen = pf.selected_category_names(data)
    chosen_text = '، '.join(chosen) if chosen else 'هیچ‌کدام'
    await q.edit_message_text(
        '📚 همه دسته‌بندی‌ها\n'
        f'انتخاب‌شده: {chosen_text}\n\n'
        'روی دسته‌ها بزنید تا تیک بخورند. همچنین می‌توانید هر کلمه‌ای تایپ کنید تا دسته‌های مشابه نمایش داده شوند.',
        reply_markup=_browse_keyboard(data, page),
    )


async def callback(update, context):
    q = update.callback_query
    cb = q.data or ''
    uid = q.from_user.id

    if not (
        cb == 'woo:catbrowse:show'
        or cb.startswith('woo:catbrowse:')
        or cb.startswith('woo:catpage:')
        or cb in {'woo:catnoop', 'woo:catbrowseclear'}
    ):
        return await ORIG_CALLBACK(update, context)

    if not await main.guard(update):
        return ConversationHandler.END

    state = pf._state(uid)
    if not state or state.get('step') != 'categories':
        await q.answer('این مرحله منقضی شده است.', show_alert=True)
        return pf.ACTIVE

    data = state['payload']

    if cb == 'woo:catnoop':
        await q.answer()
        return pf.ACTIVE

    if cb == 'woo:catbrowse:show':
        await q.answer()
        await _render_browse(q, uid, data, data.get('category_page', 0))
        return pf.ACTIVE

    if cb == 'woo:catbrowseclear':
        await q.answer()
        data['categories'] = []
        await _render_browse(q, uid, data, data.get('category_page', 0))
        return pf.ACTIVE

    if cb.startswith('woo:catpage:'):
        await q.answer()
        page = int(cb.rsplit(':', 1)[-1])
        await _render_browse(q, uid, data, page)
        return pf.ACTIVE

    if cb.startswith('woo:catbrowse:'):
        await q.answer()
        _, _, sid, spage = cb.split(':')
        cid = int(sid)
        page = int(spage)
        selected = set(int(x) for x in data.get('categories', []))
        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)
        data['categories'] = sorted(selected)
        await _render_browse(q, uid, data, page)
        return pf.ACTIVE

    return pf.ACTIVE


# Patch the already-built Vesta-style wizard rather than duplicating its flow.
# Global lookups inside product_flow use these replacements at runtime.
pf.send_categories = send_categories
pf.category_search_keyboard = category_search_keyboard
pf.callback = callback
