#!/usr/bin/env python3
"""
Telegram → OpenCode Bridge Bot

Main entry point. Wires together all components:
- Telegram bot (python-telegram-bot)
- OpenCode HTTP client + subprocess fallback
- Session manager (SQLite-backed)
- Security (auth whitelist + rate limiting)

Usage:
    1. Copy .env.example → .env and fill in your values
    2. Start OpenCode server:  opencode serve
    3. Run the bot:  python bot.py
"""

import asyncio
import logging
import sys

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from config import config
from opencode.client import OpenCodeClient
from sessions.manager import SessionManager
from utils.security import UserAuthorizer, RateLimiter, authorized
from handlers.commands import (
    start_command,
    help_command,
    new_command,
    sessions_command,
    switch_command,
    model_command,
    mode_command,
    share_command,
    status_command,
    id_command,
    models_command,
    stop_command,
    set_bot_commands,
)
from handlers.messages import handle_message

# ── Logging Setup ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("opencode-telegram-bot")


# ── Wrap handlers with auth decorator ──────────────────────

def build_authorized_handlers(authorizer: UserAuthorizer, rate_limiter: RateLimiter):
    """Wrap all handlers with authorization and rate limiting."""

    @authorized(authorizer, rate_limiter)
    async def _start(update, context):
        await start_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _help(update, context):
        await help_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _new(update, context):
        await new_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _sessions(update, context):
        await sessions_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _switch(update, context):
        await switch_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _model(update, context):
        await model_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _mode(update, context):
        await mode_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _share(update, context):
        await share_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _status(update, context):
        await status_command(update, context)

    @authorized(authorizer)
    async def _id(update, context):
        await id_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _models(update, context):
        await models_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _stop(update, context):
        await stop_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _message(update, context):
        await handle_message(update, context)

    return {
        "start": _start,
        "help": _help,
        "new": _new,
        "sessions": _sessions,
        "switch": _switch,
        "model": _model,
        "models": _models,
        "stop": _stop,
        "mode": _mode,
        "share": _share,
        "status": _status,
        "id": _id,
        "message": _message,
    }


async def post_init(application) -> None:
    """Run after the bot application is initialized."""
    # Register bot commands in Telegram's menu
    await set_bot_commands(application)

    # Initialize session manager
    session_mgr = application.bot_data["session_manager"]
    await session_mgr.initialize()

    # Check OpenCode availability
    oc_client = application.bot_data["opencode_client"]
    if await oc_client.is_available():
        logger.info("✅ OpenCode HTTP API is reachable")
    else:
        logger.error("❌ OpenCode HTTP API is unreachable! Make sure OpenCode is running:")
        logger.error("   Start server: opencode serve")

    logger.info("🤖 Bot is ready!")


async def post_shutdown(application) -> None:
    """Clean up resources on shutdown."""
    logger.info("Shutting down...")

    # Close session manager DB
    session_mgr = application.bot_data.get("session_manager")
    if session_mgr:
        await session_mgr.close()

    # Close HTTP client
    oc_client = application.bot_data.get("opencode_client")
    if oc_client:
        await oc_client.close()

    logger.info("Goodbye!")


def main():
    """Build and run the Telegram bot."""

    # ── Validate config ───────────────────────────────────
    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        logger.error("Copy .env.example → .env and fill in your values.")
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("  OpenCode Telegram Bot")
    logger.info("=" * 50)
    logger.info(f"  OpenCode Server: {config.opencode_server_url}")
    logger.info(f"  Default Model:   {config.opencode_model}")
    logger.info(f"  Work Directory:  {config.opencode_work_dir}")
    logger.info(f"  Authorized Users: {len(config.authorized_users)}")
    logger.info("=" * 50)

    # ── Initialize components ─────────────────────────────
    authorizer = UserAuthorizer(config.authorized_users)
    rate_limiter = RateLimiter(max_requests=20, window_seconds=60)

    oc_client = OpenCodeClient(
        server_url=config.opencode_server_url,
        username=config.opencode_server_username,
        password=config.opencode_server_password,
        timeout=config.response_timeout,
    )

    session_mgr = SessionManager(db_path=config.db_path)

    # ── Build Telegram application ────────────────────────
    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Store components in bot_data for access in handlers
    application.bot_data["config"] = config
    application.bot_data["opencode_client"] = oc_client
    application.bot_data["session_manager"] = session_mgr

    # ── Build auth-wrapped handlers ───────────────────────
    handlers = build_authorized_handlers(authorizer, rate_limiter)

    # ── Register command handlers ─────────────────────────
    application.add_handler(CommandHandler("start", handlers["start"], block=False))
    application.add_handler(CommandHandler("help", handlers["help"], block=False))
    application.add_handler(CommandHandler("new", handlers["new"], block=False))
    application.add_handler(CommandHandler("sessions", handlers["sessions"], block=False))
    application.add_handler(CommandHandler("switch", handlers["switch"], block=False))
    application.add_handler(CommandHandler("model", handlers["model"], block=False))
    application.add_handler(CommandHandler("models", handlers["models"], block=False))
    application.add_handler(CommandHandler("stop", handlers["stop"], block=False))
    application.add_handler(CommandHandler("mode", handlers["mode"], block=False))
    application.add_handler(CommandHandler("share", handlers["share"], block=False))
    application.add_handler(CommandHandler("status", handlers["status"], block=False))
    application.add_handler(CommandHandler("id", handlers["id"], block=False))

    # ── Register message handler (catches all text) ───────
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handlers["message"],
            block=False,
        )
    )

    # ── Start polling ─────────────────────────────────────
    logger.info("Starting bot with long polling...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message"],
    )


if __name__ == "__main__":
    main()
