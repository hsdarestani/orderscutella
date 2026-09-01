import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from . import bridge_setup, main, product_flow
# Adds browse-all category pages while preserving product_flow's fuzzy search.
from . import category_browse  # noqa: F401
from .db import del_config
from .vestaland_order_sync import VestalandOrderSyncHTTP


async def status_command(update, context):
    if not await main.guard(update):
        return
    await update.message.reply_text(await main.status_text(), reply_markup=main.main_menu())


def purge_legacy_woo_credentials():
    for key in ('woo_ck', 'woo_cs', 'wp_user', 'wp_app_password'):
        del_config(key)


def run():
    purge_legacy_woo_credentials()
    # Keep the existing health endpoint while adding verified Vestaland order sync.
    main.HealthHandler = VestalandOrderSyncHTTP
    main.start_health_server()
    app = Application.builder().token(main.TOKEN).build()
    app.add_handler(CommandHandler('start', main.start))
    app.add_handler(CommandHandler('cancel', main.cancel))
    app.add_handler(CommandHandler('allow', main.allow_user))
    app.add_handler(CommandHandler('users', main.users))
    app.add_handler(CommandHandler('status', status_command))
    # Must run before the broad text handler so Woo setup never asks for CK/CS.
    app.add_handler(bridge_setup.handler())
    # Product wizard uses native Telegram buttons and must run before legacy
    # text/photo handlers so it owns the entire product creation conversation.
    app.add_handler(product_flow.handler())
    app.add_handler(MessageHandler(filters.Document.ALL, main.document_handler))
    app.add_handler(MessageHandler(filters.PHOTO, main.photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main.text_handler))
    logging.getLogger('orderscutella').info('OrdersCutella bot started on health port %s', main.PORT)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    run()
