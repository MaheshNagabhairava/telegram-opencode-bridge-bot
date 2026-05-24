"""
Telegram command handlers for the OpenCode bot.

Handles all slash commands: /start, /help, /new, /sessions, /switch,
/model, /share, /status, /mode.
"""

import html
import logging

from telegram import Update, BotCommand
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Command: /start
# ──────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and initial session creation."""
    user = update.effective_user
    bot_data = context.bot_data

    session_mgr = bot_data["session_manager"]
    oc_client = bot_data["opencode_client"]

    # Check if OpenCode is reachable
    oc_available = await oc_client.is_available()
    status_icon = "🟢" if oc_available else "🔴"

    welcome = (
        f"👋 <b>Welcome, {html.escape(user.first_name)}!</b>\n\n"
        f"I'm your bridge to <b>OpenCode</b> — an AI coding agent "
        f"running on your machine.\n\n"
        f"OpenCode Server: {status_icon} {'Connected' if oc_available else 'Disconnected (make sure `opencode serve` is running)'}\n\n"
        f"<b>How to use:</b>\n"
        f"Just send me any coding question or instruction, and I'll "
        f"route it to OpenCode. Your conversation is persistent — "
        f"follow-up messages keep context.\n\n"
        f"Type /help to see all commands."
    )
    await update.message.reply_text(welcome, parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /help
# ──────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all available commands."""
    help_text = (
        "/start — Welcome message & connection check\n"
        "/help — Show this help message\n"
        "/new — Start a fresh conversation (clears current session)\n"
        "/stop — Stop/abort the current active task\n"
        "/sessions — List your recent sessions\n"
        "/switch <code>&lt;id&gt;</code> — Switch to a different session\n"
        "/model <code>&lt;name&gt;</code> — Change AI model\n"
        "/mode <code>&lt;plan|build&gt;</code> — Toggle plan/build mode\n"
        "/share — Share current session (get public URL)\n"
        "/status — Show bot & connection status\n"
        "/id — Show your Telegram user ID\n\n"
        "<b>💡 Tips:</b>\n"
        "• Just type normally to chat with OpenCode\n"
        "• Use <code>@filename</code> in prompts to reference files\n"
        "• Sessions persist — follow-up messages keep context\n"
        "• Use /new to start fresh when switching topics"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /new
# ──────────────────────────────────────────────
async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear current session and start fresh."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]

    await session_mgr.clear_session(user_id)

    await update.message.reply_text(
        "🔄 <b>Session cleared!</b>\n\n"
        "Send your next message to start a fresh conversation.",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Command: /sessions
# ──────────────────────────────────────────────
async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent sessions for this user."""
    from utils.formatting import format_session_info

    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]

    sessions = await session_mgr.list_user_sessions(user_id)

    if not sessions:
        await update.message.reply_text(
            "📭 No sessions found. Send a message to start one!",
            parse_mode="HTML",
        )
        return

    lines = ["<b>📋 Your Sessions</b>\n"]
    for s in sessions:
        lines.append(format_session_info(s))
        lines.append("")

    lines.append("\n<i>Use /switch &lt;id&gt; to switch sessions</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /switch <session_id>
# ──────────────────────────────────────────────
async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to a different session."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: /switch <code>&lt;session_id&gt;</code>\n\n"
            "Use /sessions to see available session IDs.",
            parse_mode="HTML",
        )
        return

    target_id = context.args[0]
    success = await session_mgr.switch_session(user_id, target_id)

    if success:
        await update.message.reply_text(
            f"✅ Switched to session <code>{html.escape(target_id[:8])}</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"❌ Session <code>{html.escape(target_id[:8])}</code> not found.\n"
            f"Use /sessions to see your sessions.",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# Command: /model <model_name>
# ──────────────────────────────────────────────
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change the AI model for the current session."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    config = context.bot_data["config"]

    if not context.args:
        current_info = await session_mgr.get_session_info(user_id)
        current_model = (current_info or {}).get("model", config.opencode_model) or config.opencode_model

        await update.message.reply_text(
            f"🤖 <b>Current model:</b> <code>{html.escape(current_model)}</code>\n\n"
            f"<b>Usage:</b> /model <code>&lt;provider/model&gt;</code>\n\n"
            f"<b>Examples:</b>\n"
            f"  /model anthropic/claude-sonnet-4\n"
            f"  /model google/gemini-2.5-pro\n"
            f"  /model openai/gpt-4o\n"
            f"  /model ollama/llama3",
            parse_mode="HTML",
        )
        return

    new_model = " ".join(context.args)
    await session_mgr.set_model(user_id, new_model)

    await update.message.reply_text(
        f"✅ Model changed to <code>{html.escape(new_model)}</code>\n\n"
        f"<i>This applies to your current session.</i>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Command: /mode <plan|build>
# ──────────────────────────────────────────────
async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch between plan and build mode."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]

    if not context.args:
        current_info = await session_mgr.get_session_info(user_id)
        current_mode = (current_info or {}).get("mode", "build")

        await update.message.reply_text(
            f"⚙️ <b>Current mode:</b> {current_mode}\n\n"
            f"<b>Usage:</b> /mode <code>&lt;plan|build&gt;</code>\n\n"
            f"• <b>build</b> — OpenCode can read, write, and execute\n"
            f"• <b>plan</b> — Read-only analysis, no file modifications",
            parse_mode="HTML",
        )
        return

    mode = context.args[0].lower()
    if mode not in ("plan", "build"):
        await update.message.reply_text(
            "❌ Invalid mode. Use <code>plan</code> or <code>build</code>.",
            parse_mode="HTML",
        )
        return

    await session_mgr.set_mode(user_id, mode)
    emoji = "📋" if mode == "plan" else "🔨"

    await update.message.reply_text(
        f"{emoji} Mode changed to <b>{mode}</b>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Command: /share
# ──────────────────────────────────────────────
async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Share the current session and get a public URL."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]

    session_id = await session_mgr.get_active_session(user_id)
    if not session_id:
        await update.message.reply_text(
            "📭 No active session to share. Send a message first!",
            parse_mode="HTML",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        share_url = await oc_client.share_session(session_id)
        await update.message.reply_text(
            f"🔗 <b>Session shared!</b>\n\n{html.escape(str(share_url))}",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Failed to share session: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to share session: {html.escape(str(e))}",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# Command: /status
# ──────────────────────────────────────────────
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot status, connection info, and current session."""
    from utils.formatting import format_status

    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]
    config = context.bot_data["config"]

    oc_available = await oc_client.is_available()
    session_info = await session_mgr.get_session_info(user_id)

    status_text = format_status(oc_available, session_info, config.opencode_model)
    await update.message.reply_text(status_text, parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /id
# ──────────────────────────────────────────────
async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's Telegram ID (useful for adding to AUTHORIZED_USERS)."""
    user = update.effective_user
    await update.message.reply_text(
        f"👤 <b>Your Telegram User ID:</b>\n<code>{user.id}</code>\n\n"
        f"<i>Add this to AUTHORIZED_USERS in your .env file to authorize yourself.</i>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Command: /models
# ──────────────────────────────────────────────
async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all available (connected/active) models on the OpenCode server."""
    bot_data = context.bot_data
    oc_client = bot_data["opencode_client"]

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        data = await oc_client.get_available_models()
        all_providers = data.get("all", [])
        connected = data.get("connected", [])

        if not all_providers:
            await update.message.reply_text("📭 No models found on the server.", parse_mode="HTML")
            return

        lines = ["<b>🤖 Available Models on OpenCode</b>\n"]
        connected_count = 0
        
        for p in all_providers:
            p_id = p.get("id")
            # Only display models for connected/active providers (e.g. opencode free models, or ones with active API keys like chutes)
            if p_id not in connected:
                continue
                
            p_name = p.get("name", p_id)
            models = p.get("models", {})
            
            if not models:
                continue
                
            connected_count += 1
            lines.append(f"🔌 <b>{html.escape(p_name)}</b> (<code>{html.escape(p_id)}</code>):")
            for m_id, m in models.items():
                m_name = m.get("name", m_id)
                # Provider programmatic path is providerID/modelID
                path = f"{p_id}/{m_id}"
                lines.append(f"  • {html.escape(m_name)}\n    → <code>/model {html.escape(path)}</code>")
            lines.append("")

        if connected_count == 0:
            lines.append("<i>No active/connected providers found.</i>\n")

        lines.append("💡 <b>Tip:</b> To enable additional providers (like <code>chutes</code>, <code>openai</code>, <code>anthropic</code>, etc.), configure their respective API keys in your environment or on the OpenCode server.")

        response_text = "\n".join(lines)
        from utils.formatting import split_message
        chunks = split_message(response_text, 4000)
        for chunk in chunks:
            await update.message.reply_text(chunk, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Failed to fetch models: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to retrieve models from OpenCode server: {html.escape(str(e))}",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# Command: /stop
# ──────────────────────────────────────────────
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop/abort the active session's running process."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]

    session_id = await session_mgr.get_active_session(user_id)
    if not session_id:
        await update.message.reply_text("📭 No active session is running to stop.", parse_mode="HTML")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        success = await oc_client.abort_session(session_id)
        if success:
            await update.message.reply_text("🛑 <b>Execution aborted successfully!</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ No active task was running, or session is already idle.", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to stop session: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to stop session: {html.escape(str(e))}",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# Register bot commands for Telegram menu
# ──────────────────────────────────────────────
async def set_bot_commands(app) -> None:
    """Register commands with Telegram so they appear in the bot menu."""
    commands = [
        BotCommand("start", "Welcome & connection check"),
        BotCommand("help", "Show all commands"),
        BotCommand("new", "Start a fresh conversation"),
        BotCommand("stop", "Stop/abort the current active task"),
        BotCommand("sessions", "List your sessions"),
        BotCommand("switch", "Switch to another session"),
        BotCommand("model", "Change AI model"),
        BotCommand("models", "List all available models"),
        BotCommand("mode", "Toggle plan/build mode"),
        BotCommand("share", "Share current session"),
        BotCommand("status", "Bot & connection status"),
        BotCommand("id", "Show your Telegram user ID"),
    ]
    await app.bot.set_my_commands(commands)
