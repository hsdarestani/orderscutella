import asyncio
import os

from telegram.ext import CommandHandler, ConversationHandler, MessageHandler, filters

from .db import clear_state, del_config, set_config
from .integrations import WooClient

URL, TOKEN = range(2)
DEFAULT_RELAY = os.getenv('BRIDGE_RELAY_URL', 'https://cutella-bridge-relay.hsdf7rb.workers.dev').rstrip('/')


async def begin(update, context):
    if not await __import__('app.main', fromlist=['guard']).guard(update):
        return ConversationHandler.END
    clear_state(update.effective_user.id)
    await update.message.reply_text(
        '🔐 اتصال امن Cutella با Bot Bridge\n\n'
        'دیگه CK/CS و Application Password لازم نیست.\n'
        'اول آدرس سایت را بفرست:\nhttps://cutellashop.ir'
    )
    return URL


async def url_step(update, context):
    value = (update.message.text or '').strip().rstrip('/')
    if not value.startswith('https://'):
        await update.message.reply_text('آدرس باید با https:// شروع شود.')
        return URL
    context.user_data['bridge_url'] = value
    await update.message.reply_text(
        'حالا Bridge Token را بفرست.\n\n'
        'داخل وردپرس: WooCommerce → Cutella Bot Bridge → Bridge Token'
    )
    return TOKEN


async def token_step(update, context):
    value = (update.message.text or '').strip()
    if len(value) < 24:
        await update.message.reply_text('Bridge Token معتبر نیست؛ دوباره کپی کن.')
        return TOKEN

    set_config('woo_url', context.user_data['bridge_url'])
    set_config('woo_bridge_token', value)
    set_config('woo_relay_url', DEFAULT_RELAY)

    for key in ('woo_ck', 'woo_cs', 'wp_user', 'wp_app_password'):
        del_config(key)

    try:
        await update.message.delete()
    except Exception:
        pass

    status = await update.effective_chat.send_message('⏳ اتصال امن از مسیر Cloudflare در حال بررسی است…')
    try:
        total = await asyncio.to_thread(WooClient().probe)
        await status.edit_text(
            f'✅ Cutella Bot Bridge متصل شد — {total} محصول\n'
            f'Transport: Cloudflare Relay + Signed HMAC'
        )
    except Exception as exc:
        await status.edit_text(f'⚠️ تنظیمات ذخیره شد ولی Relay هنوز پاسخ نداد:\n{exc}')
    finally:
        context.user_data.pop('bridge_url', None)
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text('لغو شد.')
    return ConversationHandler.END


def handler():
    return ConversationHandler(
        entry_points=[
            CommandHandler('bridge', begin),
            MessageHandler(filters.Regex(r'^🔌 تنظیم ووکامرس$'), begin),
        ],
        states={
            URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, url_step)],
            TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, token_step)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )
