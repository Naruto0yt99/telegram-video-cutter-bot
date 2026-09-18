import bot as legacy_bot
from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from library_nav import library_command, library_callback
from clip_handler import clip_command as source_clip_command


# Keep every existing bot feature from bot.py, but replace the old flat
# /library view with the hierarchical navigator.
legacy_bot.library_command = library_command


def main():
    legacy_bot.validate_bot_config()

    application = (
        Application.builder()
        .token(legacy_bot.BOT_TOKEN)
        .post_init(legacy_bot.post_init)
        .post_shutdown(legacy_bot.post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", legacy_bot.start_command))
    application.add_handler(CommandHandler("help", legacy_bot.help_command))
    application.add_handler(CommandHandler("library", library_command))
    application.add_handler(CallbackQueryHandler(library_callback, pattern=r"^l[abs]:|^lb$"))
    application.add_handler(CommandHandler("save", legacy_bot.save_command))
    application.add_handler(CommandHandler("edit", legacy_bot.edit_handler))
    application.add_handler(CommandHandler("find", legacy_bot.find_command))
    application.add_handler(CommandHandler("clip", source_clip_command))
    application.add_handler(CommandHandler("clips", source_clip_command))
    application.add_handler(CommandHandler("split", legacy_bot.split_command))
    application.add_handler(CommandHandler("next", legacy_bot.next_command))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, legacy_bot.text_handler)
    )
    application.add_handler(
        MessageHandler(filters.VIDEO | filters.Document.VIDEO, legacy_bot.receive_video)
    )
    application.add_error_handler(legacy_bot.error_handler)

    legacy_bot.logger.info("Bot starting with hierarchical library...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
