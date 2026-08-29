import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from . import bridge_setup, main


async def status_command(update, context):
    if not await main.guard(update):
        return
    await update.message.reply_text(await main.status_text(), reply_markup=main.main_menu())


def run():
    main.start_health_server()
    app = Application.builder().token(main.TOKEN).build()
    app.add_handler(CommandHandler('start', main.start))
    app.add_handler(CommandHandler('cancel', main.cancel))
    app.add_handler(CommandHandler('allow', main.allow_user))
    app.add_handler(CommandHandler('users', main.users))
    app.add_handler(CommandHandler('status', status_command))
    # Must run before the broad text handler so Woo setup never asks for CK/CS.
    app.add_handler(bridge_setup.handler())
    app.add_handler(MessageHandler(filters.Document.ALL, main.document_handler))
    app.add_handler(MessageHandler(filters.PHOTO, main.photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main.text_handler))
    logging.getLogger('orderscutella').info('OrdersCutella bot started on health port %s', main.PORT)
    app.run_polling(drop_pending_updates=False)


if __name__ == '__main__':
    run()
