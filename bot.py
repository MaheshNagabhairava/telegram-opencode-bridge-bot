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
import os

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from telegram.error import TimedOut, NetworkError
from utils.formatting import format_error

from config import config
from opencode.client import OpenCodeClient
from sessions.manager import SessionManager
from utils.security import UserAuthorizer, RateLimiter, authorized
from handlers.commands import (
    start_command,
    help_command,
    new_command,
    sessions_command,
    plan_command,
    build_command,
    share_command,
    status_command,
    id_command,
    models_command,
    stop_command,
    project_command,
    enable_command,
    disable_command,
    set_bot_commands,
    callback_handler,
)
from handlers.messages import handle_message, handle_document

# ── Logging Setup ──────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("opencode-telegram-bot")


class RetryingHTTPXRequest(HTTPXRequest):
    """Custom HTTPXRequest that automatically retries failed requests on connection timeouts/errors."""
    async def do_request(self, *args, **kwargs) -> tuple[int, bytes]:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                return await super().do_request(*args, **kwargs)
            except (TimedOut, NetworkError) as e:
                # Do not retry on Pool timeout errors
                if "Pool timeout" in str(e) or attempt == max_retries - 1:
                    raise
                
                logger.warning(
                    f"⚠️ Telegram request failed (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {1.0 * (attempt + 1)}s..."
                )
                await asyncio.sleep(1.0 * (attempt + 1))


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
    async def _plan(update, context):
        await plan_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _build(update, context):
        await build_command(update, context)

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
    async def _project(update, context):
        await project_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _enable(update, context):
        await enable_command(update, context)

    @authorized(authorizer, rate_limiter)
    async def _disable(update, context):
        await disable_command(update, context)

    @authorized(authorizer)
    async def _callback(update, context):
        await callback_handler(update, context)

    @authorized(authorizer, rate_limiter)
    async def _message(update, context):
        await handle_message(update, context)

    @authorized(authorizer, rate_limiter)
    async def _document(update, context):
        await handle_document(update, context)

    return {
        "start": _start,
        "help": _help,
        "new": _new,
        "sessions": _sessions,
        "models": _models,
        "stop": _stop,
        "project": _project,
        "enable": _enable,
        "disable": _disable,
        "plan": _plan,
        "build": _build,
        "share": _share,
        "status": _status,
        "id": _id,
        "callback": _callback,
        "message": _message,
        "document": _document,
    }


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a Telegram message to notify the user."""
    # Log the error with traceback
    logger.error("Exception while handling an update:", exc_info=context.error)

    # Notify the user if the update is a Telegram Update with a message
    if isinstance(update, Update) and update.effective_message:
        try:
            error_message = f"An unexpected error occurred: {context.error}"
            # Reply to the user with the formatted error
            await update.effective_message.reply_text(
                format_error(error_message),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to send error notification message: {e}")


async def post_init(application) -> None:
    """Run after the bot application is initialized."""
    # Register bot commands in Telegram's menu
    await set_bot_commands(application)

    # Initialize session manager
    session_mgr = application.bot_data["session_manager"]
    await session_mgr.initialize()

    logger.info("🤖 Bot is ready! (OpenCode serve will start lazily on the first message)")


async def post_shutdown(application) -> None:
    """Clean up resources on shutdown."""
    logger.info("Shutting down...")

    # Stop the background opencode server process if running
    try:
        from opencode.server import stop_server
        await stop_server()
    except Exception as e:
        logger.warning(f"Failed to stop background OpenCode server: {e}")

    # Close session manager DB
    session_mgr = application.bot_data.get("session_manager")
    if session_mgr:
        await session_mgr.close()

    # Close HTTP client
    oc_client = application.bot_data.get("opencode_client")
    if oc_client:
        await oc_client.close()

    logger.info("Goodbye!")


def run_firstrun_setup():
    """Interactively prompts the user for configuration values and writes them to .env."""
    print("=" * 60)
    print("🚀 OpenCode Telegram Bot — Interactive First-Run Setup")
    print("=" * 60)
    print("This utility will help you configure your .env file.\n")
    
    # 1. Read existing .env.example values for base defaults, then override with .env if it exists
    current_values = {}
    
    # Read .env.example first
    if os.path.exists(".env.example"):
        try:
            with open(".env.example", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        # Strip quotes if present
                        v = v.strip().strip("'\"")
                        # Skip placeholder token/IDs
                        if not v.startswith("your_") and v != "123456789,987654321":
                            current_values[k.strip()] = v
        except Exception:
            pass

    # Override with actual .env values if they exist
    if os.path.exists(".env"):
        try:
            with open(".env", "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        # Strip quotes if present
                        v = v.strip().strip("'\"")
                        current_values[k.strip()] = v
        except Exception:
            pass

    # Helper function to ask with default
    def ask(key, prompt_text, default_val=""):
        default = current_values.get(key, default_val)
        default_display = f" [{default}]" if default else ""
        
        try:
            val = input(f"{prompt_text}{default_display}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nSetup aborted.")
            sys.exit(1)
            
        if not val:
            return default
        return val

    # 2. Gather inputs
    token = ask(
        "TELEGRAM_BOT_TOKEN", 
        "1. Enter your TELEGRAM_BOT_TOKEN (from @BotFather)"
    )
    while not token:
        print("❌ TELEGRAM_BOT_TOKEN is required to run the bot!")
        token = ask("TELEGRAM_BOT_TOKEN", "1. Enter your TELEGRAM_BOT_TOKEN")

    users = ask(
        "AUTHORIZED_USERS", 
        "2. Enter AUTHORIZED_USERS (comma-separated Telegram User IDs, e.g. 846469353)"
    )
    while not users:
        print("❌ AUTHORIZED_USERS is required to restrict access to the bot!")
        users = ask("AUTHORIZED_USERS", "2. Enter AUTHORIZED_USERS")

    server_url = ask(
        "OPENCODE_SERVER_URL", 
        "3. Enter OPENCODE_SERVER_URL", 
        "http://localhost:4096"
    )
    
    username = ask(
        "OPENCODE_SERVER_USERNAME", 
        "4. Enter OPENCODE_SERVER_USERNAME (leave blank if no authentication)"
    )
    
    password = ask(
        "OPENCODE_SERVER_PASSWORD", 
        "5. Enter OPENCODE_SERVER_PASSWORD (leave blank if no authentication)"
    )
    
    model = ask(
        "OPENCODE_MODEL", 
        "6. Enter OPENCODE_MODEL", 
        "anthropic/claude-sonnet-4"
    )
    
    work_dir = ask(
        "OPENCODE_WORK_DIR", 
        "7. Enter OPENCODE_WORK_DIR (full path to your parent workspace)", 
        os.path.abspath(".")
    )

    # 3. Format and write .env
    env_content = f"""# Telegram Bot Configuration
TELEGRAM_BOT_TOKEN={token}
AUTHORIZED_USERS={users}

# OpenCode Configuration
OPENCODE_SERVER_URL={server_url}
OPENCODE_SERVER_USERNAME={username}
OPENCODE_SERVER_PASSWORD={password}
OPENCODE_MODEL={model}
OPENCODE_WORK_DIR="{work_dir}"

# Limits
MAX_MESSAGE_LENGTH=4000
RESPONSE_TIMEOUT=0  # Set to 0 to disable request timeouts entirely

# Database
DB_PATH=sessions.db
"""

    try:
        with open(".env", "w", encoding="utf-8") as f:
            f.write(env_content)
        print("\n✅ Success! Configuration written to .env file.")
        print("=" * 60)
        print("You can now start the bot using:")
        print("  python bot.py")
        print("=" * 60 + "\n")
    except Exception as e:
        print(f"\n❌ Error writing to .env file: {e}")


def main():
    """Build and run the Telegram bot."""

    # If --env CLI flag is passed, or if .env does not exist, run setup
    if "--env" in sys.argv:
        run_firstrun_setup()
        sys.exit(0)

    if not os.path.exists(".env"):
        print("⚠️ .env file not found!")
        try:
            choice = input("Would you like to run the interactive setup now? (y/n): ").strip().lower()
            if choice in ("y", "yes"):
                run_firstrun_setup()
                print("\nSetup complete! Starting the bot...")
            else:
                print("Please copy .env.example to .env and configure it before starting.")
                sys.exit(1)
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled.")
            sys.exit(1)

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
    request = RetryingHTTPXRequest(
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=5.0,
        connection_pool_size=512,
    )

    application = (
        ApplicationBuilder()
        .token(config.telegram_bot_token)
        .request(request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ── Register global error handler ─────────────────────
    application.add_error_handler(error_handler)

    # Store components in bot_data for access in handlers
    application.bot_data["config"] = config
    application.bot_data["opencode_client"] = oc_client
    application.bot_data["session_manager"] = session_mgr
    application.bot_data["server_started"] = False

    # ── Build auth-wrapped handlers ───────────────────────
    handlers = build_authorized_handlers(authorizer, rate_limiter)

    # ── Register command handlers ─────────────────────────
    application.add_handler(CommandHandler("start", handlers["start"], block=False))
    application.add_handler(CommandHandler("help", handlers["help"], block=False))
    application.add_handler(CommandHandler("new", handlers["new"], block=False))
    application.add_handler(CommandHandler("sessions", handlers["sessions"], block=False))
    application.add_handler(CommandHandler("models", handlers["models"], block=False))
    application.add_handler(CommandHandler("stop", handlers["stop"], block=False))
    application.add_handler(CommandHandler("project", handlers["project"], block=False))
    application.add_handler(CommandHandler("enable", handlers["enable"], block=False))
    application.add_handler(CommandHandler("disable", handlers["disable"], block=False))
    application.add_handler(CommandHandler("plan", handlers["plan"], block=False))
    application.add_handler(CommandHandler("build", handlers["build"], block=False))
    application.add_handler(CommandHandler("share", handlers["share"], block=False))
    application.add_handler(CommandHandler("status", handlers["status"], block=False))
    application.add_handler(CommandHandler("id", handlers["id"], block=False))

    # ── Register callback query handler ───────────────────
    application.add_handler(CallbackQueryHandler(handlers["callback"], block=False))

    # ── Register message handler (catches all text) ───────
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handlers["message"],
            block=False,
        )
    )

    # ── Register document handler (catches file uploads) ──
    application.add_handler(
        MessageHandler(
            (filters.Document.ALL | filters.PHOTO) & ~filters.COMMAND,
            handlers["document"],
            block=False,
        )
    )

    # ── Start polling ─────────────────────────────────────
    logger.info("Starting bot with long polling...")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
