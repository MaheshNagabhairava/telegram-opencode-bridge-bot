"""
Telegram command handlers for the OpenCode bot.

Handles all slash commands: /start, /help, /new, /sessions,
/share, /status, /mode.
"""

import html
import logging
import os

from telegram import Update, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats, InlineKeyboardButton, InlineKeyboardMarkup
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
        "/project — View & switch active project directories\n"
        "/create_project — Create a new workspace folder\n"
        "/delete_project — Delete a workspace folder\n"
        "/enable — Enable live tool call & progress streaming\n"
        "/disable — Disable live progress streaming\n"
        "/sessions — List your recent sessions (tap to switch)\n"
        "/history [count] — Show active session history\n"
        "/delete — Permanently delete a session\n"
        "/models — List all available models (tap to change)\n"
        "/mode — Select agent mode (TUI dropdown equivalents)\n"
        "/plan — Switch to plan mode (read-only)\n"
        "/build — Switch to build mode (read, write, execute)\n"
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
    """List recent sessions for this user in their active workspace."""
    from utils.formatting import format_session_info

    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]
    config = context.bot_data["config"]

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

    # Resolve the user's current workspace directory
    base_dir = os.path.abspath(config.opencode_work_dir)
    current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
    current_dir = os.path.abspath(current_dir)

    def norm(p):
        if not p:
            return ""
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))

    current_dir_norm = norm(current_dir)

    # Fetch ALL sessions from the server
    server_sessions = []
    server_session_ids = set()
    try:
        server_sessions = await oc_client.list_sessions()
        server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
    except Exception as e:
        logger.warning(f"Could not fetch sessions from server: {e}")
        await update.message.reply_text(
            "⚠️ Could not reach the OpenCode server to list sessions.\n"
            "Make sure <code>opencode serve</code> is running.",
            parse_mode="HTML",
        )
        return

    # Prune local DB: delete any sessions not on the server
    local_sessions = await session_mgr.list_user_sessions(user_id)
    local_ids = [s.get("session_id") for s in local_sessions]
    stale_ids = [sid for sid in local_ids if sid not in server_session_ids]
    if stale_ids:
        for sid in stale_ids:
            try:
                await session_mgr._db.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                    (user_id, sid)
                )
            except Exception as e:
                logger.error(f"Error pruning session {sid}: {e}")
        await session_mgr._db.commit()

        # Clear active session cache if it was pruned
        active_sid = await session_mgr.get_active_session(user_id)
        if active_sid in stale_ids:
            if user_id in session_mgr._active_sessions:
                del session_mgr._active_sessions[user_id]

    # Filter server sessions to the current workspace
    workspace_sessions = []
    for s in server_sessions:
        s_dir = s.get("directory", "")
        if norm(s_dir) == current_dir_norm:
            workspace_sessions.append(s)

    if not workspace_sessions:
        folder_name = os.path.basename(current_dir) or "Root"
        await update.message.reply_text(
            f"📭 No sessions found in project <b>{html.escape(folder_name)}</b>.\n"
            f"Send a message to start one!",
            parse_mode="HTML",
        )
        return

    workspace_sessions.sort(
        key=lambda s: s.get("time", {}).get("updated", 0),
        reverse=True,
    )

    # Lookup locally-tracked data
    refreshed_local = await session_mgr.list_user_sessions(user_id)
    local_map = {ls.get("session_id"): ls for ls in refreshed_local}
    active_sid = await session_mgr.get_active_session(user_id)

    folder_name = os.path.basename(current_dir) or "Root"
    lines = [f"<b>📋 Sessions in {html.escape(folder_name)}</b>\n"]

    for s in workspace_sessions:
        s_id = s.get("id", "")
        s_title = s.get("title", "")
        local_info = local_map.get(s_id, {})

        display = {
            "session_id": s_id,
            "name": s_title,
            "is_active": (s_id == active_sid),
            "message_count": local_info.get("message_count", 0),
            "created_at": "",
            "last_active": "",
            "model": local_info.get("model", ""),
            "mode": local_info.get("mode", "build"),
        }

        time_obj = s.get("time", {})
        if time_obj.get("created"):
            from datetime import datetime, timezone
            try:
                display["created_at"] = datetime.fromtimestamp(
                    time_obj["created"] / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass
        if time_obj.get("updated"):
            from datetime import datetime, timezone
            try:
                display["last_active"] = datetime.fromtimestamp(
                    time_obj["updated"] / 1000, tz=timezone.utc
                ).isoformat()
            except Exception:
                pass

        lines.append(format_session_info(display))
        lines.append("")

        if s_title and s_id in local_map:
            try:
                await session_mgr._db.execute(
                    "UPDATE sessions SET name = ? WHERE opencode_session_id = ?",
                    (s_title, s_id)
                )
                await session_mgr._db.commit()
            except Exception:
                pass

    keyboard = []
    for s in workspace_sessions:
        s_id = s.get("id", "")
        s_title = s.get("title", "") or s_id[:8]
        is_active = (s_id == active_sid)
        marker = "🔹" if is_active else "📄"
        button_text = f"{marker} {s_title}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"sess:{s_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

    if keyboard:
        lines.append("\n👉 <b>Tap a session below to instantly switch to it:</b>")
    else:
        lines.append("\n<i>Send a message to start a conversation!</i>")

    await update.message.reply_text("\n".join(lines), reply_markup=reply_markup, parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /delete
# ──────────────────────────────────────────────
async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List recent sessions for this user in their active workspace for deletion."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]
    config = context.bot_data["config"]

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

    # Resolve workspace
    base_dir = os.path.abspath(config.opencode_work_dir)
    current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
    current_dir = os.path.abspath(current_dir)

    def norm(p):
        if not p:
            return ""
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))

    current_dir_norm = norm(current_dir)

    # Fetch server sessions
    server_sessions = []
    try:
        server_sessions = await oc_client.list_sessions()
    except Exception as e:
        logger.warning(f"Could not fetch sessions from server during delete call: {e}")
        await update.message.reply_text(
            "⚠️ Could not reach the OpenCode server to list sessions.\n"
            "Make sure <code>opencode serve</code> is running.",
            parse_mode="HTML",
        )
        return

    # Filter to current workspace
    workspace_sessions = []
    for s in server_sessions:
        s_dir = s.get("directory", "")
        if norm(s_dir) == current_dir_norm:
            workspace_sessions.append(s)

    if not workspace_sessions:
        folder_name = os.path.basename(current_dir) or "Root"
        await update.message.reply_text(
            f"📭 No sessions found in project <b>{html.escape(folder_name)}</b> to delete.",
            parse_mode="HTML",
        )
        return

    workspace_sessions.sort(
        key=lambda s: s.get("time", {}).get("updated", 0),
        reverse=True,
    )

    active_sid = await session_mgr.get_active_session(user_id)

    folder_name = os.path.basename(current_dir) or "Root"
    text = (
        f"🗑️ <b>Delete Session (Workspace: {html.escape(folder_name)})</b>\n\n"
        f"Choose a session below to permanently delete it from your local machine and the server.\n\n"
        f"⚠️ <b>WARNING:</b> This cannot be undone!"
    )

    keyboard = []
    for s in workspace_sessions:
        s_id = s.get("id", "")
        s_title = s.get("title", "") or s_id[:8]
        is_active = (s_id == active_sid)
        marker = "🔹 (Current)" if is_active else "📄"
        button_text = f"❌ Delete {s_title} {marker}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delsess:{s_id}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /switch <session_id>
# ──────────────────────────────────────────────
async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch to a different session."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

    if not context.args:
        await update.message.reply_text(
            "⚠️ Usage: /switch <code>&lt;session_id&gt;</code>\n\n"
            "Use /sessions to see available session IDs.",
            parse_mode="HTML",
        )
        return

    target_id = context.args[0].strip()
    resolved_id = None
    server_sessions = []
    
    try:
        server_sessions = await oc_client.list_sessions()
        for s in server_sessions:
            s_id = s.get("id", "")
            if s_id.lower().startswith(target_id.lower()):
                resolved_id = s_id
                break
    except Exception as e:
        logger.warning(f"Could not verify session list on server during switch resolution: {e}")

    if not resolved_id:
        try:
            local_sessions = await session_mgr.list_user_sessions(user_id)
            for s in local_sessions:
                s_id = s.get("session_id", "")
                if s_id.lower().startswith(target_id.lower()):
                    resolved_id = s_id
                    break
        except Exception as e:
            logger.warning(f"Could not fetch local sessions during switch resolution: {e}")

    if not resolved_id:
        resolved_id = target_id

    if server_sessions:
        server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
        if resolved_id not in server_session_ids:
            try:
                await session_mgr._db.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                    (user_id, resolved_id)
                )
                await session_mgr._db.commit()
            except Exception:
                pass
            
            if user_id in session_mgr._active_sessions and session_mgr._active_sessions[user_id]["session_id"] == resolved_id:
                del session_mgr._active_sessions[user_id]
                
            await update.message.reply_text(
                f"❌ Session <code>{html.escape(resolved_id[:8])}</code> has been deleted on the server.\n"
                f"Use /sessions to see your current sessions.",
                parse_mode="HTML",
            )
            return

    success = await session_mgr.switch_session(user_id, resolved_id)

    if not success and server_sessions:
        server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
        if resolved_id in server_session_ids:
            s_obj = None
            for s in server_sessions:
                if s.get("id") == resolved_id:
                    s_obj = s
                    break
            
            if s_obj:
                s_dir = s_obj.get("directory", "")
                s_title = s_obj.get("title", "")
                
                from datetime import datetime
                now_iso = datetime.utcnow().isoformat()
                
                try:
                    await session_mgr._db.execute(
                        "INSERT INTO sessions (user_id, opencode_session_id, work_dir, name, created_at, last_active, is_active, message_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                        (user_id, resolved_id, s_dir, s_title, now_iso, now_iso)
                    )
                    await session_mgr._db.commit()
                    success = await session_mgr.switch_session(user_id, resolved_id)
                except Exception:
                    pass

    if success:
        await update.message.reply_text(
            f"✅ Switched to session <code>{html.escape(resolved_id[:8])}</code>",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(
            f"❌ Session <code>{html.escape(resolved_id[:8])}</code> not found.\n"
            f"Use /sessions to see your sessions.",
            parse_mode="HTML",
        )


# ──────────────────────────────────────────────
# Commands: /plan and /build
# ──────────────────────────────────────────────
async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the current session to plan mode (read-only)."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    await session_mgr.set_mode(user_id, "plan")
    await update.message.reply_text(
        "📋 Mode changed to <b>plan</b>\n\n"
        "<i>OpenCode is now in read-only analysis mode (no file modifications).</i>",
        parse_mode="HTML",
    )


async def build_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Switch the current session to build mode (read, write, execute)."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    await session_mgr.set_mode(user_id, "build")
    await update.message.reply_text(
        "🔨 Mode changed to <b>build</b>\n\n"
        "<i>OpenCode can now read, write files, and execute commands.</i>",
        parse_mode="HTML",
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all available agent modes on the OpenCode server."""
    user_id = update.effective_user.id
    bot_data = context.bot_data
    oc_client = bot_data["opencode_client"]
    session_mgr = bot_data["session_manager"]

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        agents = await oc_client.get_available_agents()
        if not agents:
            await update.message.reply_text("📭 No agent modes found on the server.", parse_mode="HTML")
            return

        session_info = await session_mgr.get_session_info(user_id)
        current_mode = (session_info or {}).get("mode", "build") or "build"

        # Emojis mapping for premium design
        mode_emojis = {
            "build": "🔨",
            "plan": "📋",
            "pentester": "🕵️",
        }

        keyboard = []
        for agent in agents:
            if agent.get("hidden"):
                continue
            name = agent.get("name", "")
            if not name:
                continue

            emoji = mode_emojis.get(name.lower(), "🤖")
            is_active = (name.lower() == current_mode.lower())
            
            # Label
            label = f"{emoji} {name.upper()}"
            if is_active:
                label += " 🔹 (Current)"
            
            keyboard.append([InlineKeyboardButton(label, callback_data=f"mode:{name}")])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        
        await update.message.reply_text(
            "<b>🤖 Select Agent Mode</b>\n\n"
            "Choose which AI agent configuration and permissions to use for this session:",
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"Failed to fetch agents: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Failed to retrieve agents from OpenCode server: {html.escape(str(e))}",
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

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

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
    """Show the user's Telegram ID."""
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
    """List all available models on the OpenCode server."""
    user_id = update.effective_user.id
    bot_data = context.bot_data
    oc_client = bot_data["opencode_client"]

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        data = await oc_client.get_available_models()
        all_providers = data.get("all", [])
        connected = data.get("connected", [])

        if not all_providers:
            await update.message.reply_text("📭 No models found on the server.", parse_mode="HTML")
            return

        connected_lower = {c.lower() for c in connected if c}

        lines = [
            "<b>🤖 Choose AI Model Provider</b>\n",
            "Select an active connected provider below to view and switch models for your current session:"
        ]

        # Provider-to-emoji mapping for high visual contrast and distinct colors
        provider_emojis = {
            "anthropic": "🟧",
            "openai": "🟩",
            "google": "🔵",
            "ollama": "🟪",
            "chutes": "🟥",
            "opencode": "🟡",
        }

        keyboard = []
        for p in all_providers:
            p_id = p.get("id", "")
            p_id_lower = p_id.lower() if p_id else ""
            if p_id_lower not in connected_lower:
                continue
            models = p.get("models", {})
            if not models:
                continue
            
            p_name = p.get("name", p_id)
            emoji = provider_emojis.get(p_id_lower, "⚪")
            keyboard.append([InlineKeyboardButton(f"───【 {emoji} {p_name.upper()} 】───", callback_data=f"prov:{p_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None

        response_text = "\n".join(lines)
        from utils.formatting import split_message
        chunks = split_message(response_text, 4000)
        for i, chunk in enumerate(chunks):
            markup = reply_markup if i == len(chunks) - 1 else None
            await update.message.reply_text(chunk, reply_markup=markup, parse_mode="HTML")

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

    # Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

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
# Command: /project dynamic explorer helpers & commands
# ──────────────────────────────────────────────

def get_subdirectories(parent_dir: str) -> list[str]:
    import os
    ignore_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gemini", ".idea", ".vscode", "build", "dist", ".next"}
    subdirs = []
    try:
        if os.path.exists(parent_dir) and os.path.isdir(parent_dir):
            for name in sorted(os.listdir(parent_dir)):
                if name.startswith(".") or name in ignore_dirs:
                    continue
                if os.path.isdir(os.path.join(parent_dir, name)):
                    subdirs.append(name)
    except Exception:
        pass
    return subdirs


def render_project_explorer(browsing_dir: str, base_dir: str, current_active_dir: str) -> tuple[str, InlineKeyboardMarkup]:
    import os
    import html
    
    subdirs = get_subdirectories(browsing_dir)
    
    # Normalize paths for clean display
    norm_base = os.path.normpath(base_dir)
    norm_browsing = os.path.normpath(browsing_dir)
    norm_active = os.path.normpath(current_active_dir)
    
    # Determine relation to base_dir
    rel_path = os.path.relpath(norm_browsing, norm_base)
    display_rel = "Root" if rel_path == "." else rel_path.replace('\\', '/')
    
    lines = [
        "<b>📁 Workspace File Explorer</b>\n",
        f"🏠 <b>Workspace Root Parent:</b>",
        f"<code>{html.escape(norm_base)}</code>\n",
        f"🔍 <b>Currently Browsing:</b>",
        f"<code>{html.escape(display_rel)}</code>",
        f"<pre>{html.escape(norm_browsing)}</pre>\n",
        f"📍 <b>Currently Active Workspace:</b>",
        f"<code>{html.escape(os.path.basename(norm_active) or 'Root')}</code>",
        f"<pre>{html.escape(norm_active)}</pre>\n",
        "ℹ️ <i>Explore subfolders below. Tap <b>SELECT THIS WORKSPACE</b> to set the currently browsing folder as your active environment.</i>"
    ]
    
    # Build inline keyboard
    keyboard = []
    
    # Control row: Parent Folder (if not at base_dir root) and Select Workspace
    control_row = []
    
    is_at_root = (os.path.normcase(norm_browsing) == os.path.normcase(norm_base))
    if not is_at_root:
        control_row.append(InlineKeyboardButton("⬅️ Parent Folder", callback_data="proj_nav:parent"))
        
    control_row.append(InlineKeyboardButton("✅ SELECT THIS WORKSPACE", callback_data="proj_nav:select"))
    keyboard.append(control_row)
    
    # Subdirectories
    if subdirs:
        subdir_buttons = []
        for idx, name in enumerate(subdirs):
            subdir_buttons.append(InlineKeyboardButton(f"📁 {name}/", callback_data=f"proj_nav:sub:{idx}"))
        
        for j in range(0, len(subdir_buttons), 2):
            keyboard.append(subdir_buttons[j:j+2])
    else:
        lines.append("\n📭 <i>(No subdirectories inside this folder)</i>")
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    return "\n".join(lines), reply_markup


async def execute_project_switch(update_or_query, context: ContextTypes.DEFAULT_TYPE, user_id: int, target_path: str) -> None:
    """Helper to perform the actual project switch, server restart, and session creation."""
    import os
    import html
    session_mgr = context.bot_data["session_manager"]
    oc_client = context.bot_data["opencode_client"]
    config = context.bot_data["config"]

    # Save to database
    await session_mgr.set_user_work_dir(user_id, target_path)
    
    base_dir = os.path.abspath(config.opencode_work_dir)
    target_folder = os.path.relpath(target_path, base_dir)
    if target_folder == ".":
        target_folder = "[Root] " + os.path.basename(base_dir)
    else:
        target_folder = target_folder.replace('\\', '/')

    # Inform user/edit message
    status_text = (
        f"⏳ <b>Switching project to:</b> <code>{html.escape(target_folder)}</code>...\n"
        f"Restarting OpenCode serve backend to physically isolate execution environment..."
    )
    
    is_query = hasattr(update_or_query, "edit_message_text")
    if is_query:
        status_msg = await update_or_query.edit_message_text(status_text, parse_mode="HTML")
    else:
        status_msg = await update_or_query.message.reply_text(status_text, parse_mode="HTML")

    async def update_status(text):
        try:
            await status_msg.edit_text(text, parse_mode="HTML")
        except Exception:
            if is_query:
                await update_or_query.message.reply_text(text, parse_mode="HTML")
            else:
                await update_or_query.reply_text(text, parse_mode="HTML")

    from urllib.parse import urlparse
    try:
        url_parsed = urlparse(config.opencode_server_url)
        hostname = url_parsed.hostname or "127.0.0.1"
        port = url_parsed.port or 8080
    except Exception:
        hostname = "127.0.0.1"
        port = 8080

    from opencode.server import restart_server
    restart_success = await restart_server(target_path, port=port, hostname=hostname)

    if not restart_success:
        await update_status(
            f"❌ <b>Failed to restart OpenCode server inside the new project directory.</b>\n\n"
            f"📍 <i>Target Path: {html.escape(target_path)}</i>\n\n"
            f"Please ensure <code>opencode serve</code> can run on port {port} or check bot logs.",
        )
        return

    context.bot_data["server_started"] = True

    # Check if there is an existing last active session for this workspace
    last_sess_id = await session_mgr.get_last_session_in_workspace(user_id, target_path)
    resumed = False

    if last_sess_id:
        try:
            # Query server to verify the session still exists there
            server_sessions = await oc_client.list_sessions()
            server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
            if last_sess_id in server_session_ids:
                await session_mgr.switch_session(user_id, last_sess_id)
                resumed = True
                await update_status(
                    f"🔄 <b>Switched project to:</b> <code>{html.escape(target_folder)}</code>\n"
                    f"🚀 <b>OpenCode server restarted & last session resumed!</b>\n\n"
                    f"📍 <i>Path: {html.escape(target_path)}</i>\n"
                    f"🆔 <i>Session ID: {html.escape(last_sess_id[:8])}</i>\n\n"
                    f"Resumed previous workspace conversation. Send your next message to continue coding!",
                )
        except Exception as e:
            logger.warning(f"Failed to verify and switch to last active session: {e}")

    if not resumed:
        await session_mgr.clear_session(user_id)
        try:
            result = await oc_client.create_session(directory=target_path)
            if not isinstance(result, dict):
                raise ValueError("Invalid session response from OpenCode server.")
            
            session_id = (
                result.get("id")
                or result.get("session_id")
                or result.get("sessionId")
            )
            if not session_id:
                raise ValueError("Response did not contain a session ID.")

            # Fetch user preferred model and mode
            preferred_model = await session_mgr.get_user_preferred_model(user_id, config.opencode_model)
            preferred_mode = await session_mgr.get_user_preferred_mode(user_id, "build")
            await session_mgr.set_active_session(
                user_id, session_id, preferred_model, work_dir=target_path, mode=preferred_mode
            )

            await update_status(
                f"🔄 <b>Switched project to:</b> <code>{html.escape(target_folder)}</code>\n"
                f"🚀 <b>OpenCode server restarted & fresh session prepared!</b>\n\n"
                f"📍 <i>Path: {html.escape(target_path)}</i>\n\n"
                f"Deterministic isolation active. Send your next message to start coding!",
            )
        except Exception as e:
            logger.error(f"Failed to create fresh session after server restart: {e}", exc_info=True)
            await update_status(
                f"🔄 <b>Switched project to:</b> <code>{html.escape(target_folder)}</code>\n"
                f"⚠️ Server restarted successfully, but failed to create a new session: <code>{html.escape(str(e))}</code>\n\n"
                f"📍 <i>Path: {html.escape(target_path)}</i>\n\n"
                f"Try sending a message; the bot will attempt to auto-heal and create a new session.",
            )


async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List project directories or switch to one by number or name."""
    import os
    import html
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    config = context.bot_data["config"]

    # Base directory is the workspace parent folder configured in .env
    base_dir = os.path.abspath(config.opencode_work_dir)
    
    current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
    current_dir = os.path.abspath(current_dir)

    # Check for manual switch
    if context.args:
        arg = " ".join(context.args).strip()
        target_path = None
        
        base_name = os.path.basename(base_dir)
        if arg == "0" or arg.lower() in ("root", "root workspace", "root workspace root", base_name.lower()):
            target_path = base_dir
        elif os.path.isabs(arg) and os.path.isdir(arg):
            target_path = os.path.abspath(arg)
        else:
            def find_dir_recursive(parent, target_name):
                ignore_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gemini", ".idea", ".vscode", "build", "dist", ".next"}
                try:
                    for name in os.listdir(parent):
                        if name.startswith(".") or name in ignore_dirs:
                            continue
                        full_path = os.path.join(parent, name)
                        if os.path.isdir(full_path):
                            if name.lower() == target_name.lower():
                                return full_path
                            found = find_dir_recursive(full_path, target_name)
                            if found:
                                return found
                except Exception:
                    pass
                return None
            target_path = find_dir_recursive(base_dir, arg)

        if not target_path:
            await update.message.reply_text(
                f"⚠️ Project folder <code>{html.escape(arg)}</code> not found inside your workspace directory.\n\n"
                f"Type /project to see the file explorer.",
                parse_mode="HTML"
            )
            return

        await execute_project_switch(update, context, user_id, target_path)
        return

    context.user_data["browsing_dir"] = current_dir
    text, reply_markup = render_project_explorer(current_dir, base_dir, current_dir)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


# ──────────────────────────────────────────────
# Command: /enable
# ──────────────────────────────────────────────
async def enable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable real-time tool execution streaming on Telegram."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]

    await session_mgr.set_user_streaming(user_id, 1)

    await update.message.reply_text(
        "🚀 <b>Real-time Streaming Enabled!</b>\n\n"
        "I will now show you what OpenCode is doing (like tool calls, shell commands, and file edits) live on Telegram as it executes!\n\n"
        "Type /disable at any time to turn it off.",
        parse_mode="HTML"
    )


# ──────────────────────────────────────────────
# Command: /disable
# ──────────────────────────────────────────────
async def disable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable real-time tool execution streaming on Telegram."""
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]

    await session_mgr.set_user_streaming(user_id, 0)

    await update.message.reply_text(
        "⏸️ <b>Real-time Streaming Disabled.</b>\n\n"
        "I will only show you the final outputs from OpenCode.\n\n"
        "Type /enable to turn it back on.",
        parse_mode="HTML"
    )


# ──────────────────────────────────────────────
# Command: /history [count]
# ──────────────────────────────────────────────
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch and display conversation history of the currently active session."""
    import asyncio
    import html
    from utils.formatting import format_opencode_response, split_message, format_error

    user_id = update.effective_user.id
    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]
    oc_client = bot_data["opencode_client"]
    config = bot_data["config"]

    # 1. Ensure OpenCode server is running dynamically
    from handlers.messages import ensure_server_running
    if not await ensure_server_running(update, context, user_id):
        return

    # 2. Get active session ID
    session_id = await session_mgr.get_active_session(user_id)
    if not session_id:
        await update.message.reply_text(
            "📭 <b>No active session found.</b>\n\n"
            "Send a message first to start a conversation or use /sessions to switch/select a session.",
            parse_mode="HTML"
        )
        return

    # 3. Parse optional [count] argument
    count = None
    if context.args:
        try:
            count = int(context.args[0])
            if count <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text(
                "⚠️ <b>Invalid argument!</b>\n\n"
                "Please provide a positive integer, e.g., <code>/history 5</code>.",
                parse_mode="HTML"
            )
            return

    # 4. Fetch messages from server
    try:
        messages = await oc_client.list_messages(session_id)
    except Exception as e:
        logger.error(f"Failed to fetch conversation history for session {session_id[:8]}...: {e}", exc_info=True)
        await update.message.reply_text(
            format_error(f"Failed to fetch conversation history: {e}"),
            parse_mode="HTML"
        )
        return

    # 5. Helper function to extract text
    def extract_conversational_text(message: dict) -> str:
        parts = message.get("parts", [])
        content_text = ""
        if isinstance(parts, list):
            # Join all text parts, ignoring reasoning, steps, tool calls etc.
            text_parts = [
                p.get("text", "")
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content_text = "".join(text_parts)
        
        # Fallback to content or text fields if parts are missing or empty
        if not content_text.strip():
            content_text = message.get("content", message.get("text", ""))
            
        return content_text.strip()

    # 6. Parse and format messages chronologically
    formatted_messages = []
    for msg in messages:
        content_text = extract_conversational_text(msg)
        if not content_text:
            continue
        
        # Extract role
        role = msg.get("info", {}).get("role") or msg.get("role", "")
        formatted_messages.append((role, content_text))

    # Slice the messages to count if specified
    if count is not None:
        formatted_messages = formatted_messages[-count:]

    if not formatted_messages:
        await update.message.reply_text(
            "📭 <b>No conversational history found in this session yet.</b>",
            parse_mode="HTML"
        )
        return

    # Build history blocks
    history_parts = []
    for role, text in formatted_messages:
        formatted_text = format_opencode_response(text)
        if role == "user":
            history_parts.append(f"👤 <b>User:</b>\n{formatted_text}")
        else:
            # assistant
            history_parts.append(f"🤖 <b>OpenCode:</b>\n{formatted_text}")

    history_text = "\n\n───────────────────\n\n".join(history_parts)

    # 7. Check if the active session is running a task and add header
    header = ""
    if session_mgr.is_session_running(user_id):
        header = (
            "⚡ <b>(Active Running Task)</b>\n"
            "<i>OpenCode is currently busy running a prompt. The history below may not reflect the latest pending response.</i>\n\n"
            "───────────────────\n\n"
        )

    full_text = header + history_text

    # 8. Split and send sequentially
    chunks = split_message(full_text, 4000)
    for i, chunk in enumerate(chunks):
        try:
            await update.message.reply_text(
                chunk,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        except Exception as e:
            logger.warning(f"HTML parsing failed for history chunk {i+1}, falling back to plain text: {e}")
            try:
                import re
                plain = re.sub(r'<[^>]+>', '', chunk)
                await update.message.reply_text(
                    plain,
                    disable_web_page_preview=True
                )
            except Exception as e2:
                logger.error(f"Failed to send history chunk {i+1} as plain text: {e2}")

        if i < len(chunks) - 1:
            await asyncio.sleep(0.5)


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
        BotCommand("project", "View & switch project folders"),
        BotCommand("create_project", "Create a new workspace folder"),
        BotCommand("delete_project", "Delete an existing workspace folder"),
        BotCommand("enable", "Enable live progress streaming"),
        BotCommand("disable", "Disable live progress streaming"),
        BotCommand("sessions", "List your sessions"),
        BotCommand("history", "Show active session history"),
        BotCommand("delete", "Permanently delete a session"),
        BotCommand("models", "List all available models"),
        BotCommand("mode", "Select agent mode (TUI dropdown equivalents)"),
        BotCommand("plan", "Switch to plan mode (read-only)"),
        BotCommand("build", "Switch to build mode (read, write, execute)"),
        BotCommand("share", "Share current session"),
        BotCommand("status", "Bot & connection status"),
        BotCommand("id", "Show your Telegram user ID"),
    ]
    await app.bot.set_my_commands(commands)
    
    try:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning(f"Could not set commands for private chats scope: {e}")
        
    try:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning(f"Could not set commands for group chats scope: {e}")


# ──────────────────────────────────────────────
# Central Callback Query Handler
# ──────────────────────────────────────────────
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Centralized handler for all inline button tap actions (projects, sessions, models)."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data
    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]
    oc_client = bot_data["opencode_client"]
    config = bot_data["config"]

    if not data or data == "noop":
        return

    import os
    logger.info(f"Callback query received from user {user_id}: {data}")

    # 1. Switch Session tap
    if data.startswith("sess:"):
        target_id = data[len("sess:"):]
        
        from handlers.messages import ensure_server_running
        if not await ensure_server_running(update, context, user_id):
            return

        resolved_id = None
        server_sessions = []
        try:
            server_sessions = await oc_client.list_sessions()
            for s in server_sessions:
                s_id = s.get("id", "")
                if s_id.lower().startswith(target_id.lower()):
                    resolved_id = s_id
                    break
        except Exception as e:
            logger.warning(f"Could not verify session list on server during callback: {e}")

        if not resolved_id:
            try:
                local_sessions = await session_mgr.list_user_sessions(user_id)
                for s in local_sessions:
                    s_id = s.get("session_id", "")
                    if s_id.lower().startswith(target_id.lower()):
                        resolved_id = s_id
                        break
            except Exception as e:
                logger.warning(f"Could not fetch local sessions during callback: {e}")

        if not resolved_id:
            resolved_id = target_id

        if server_sessions:
            server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
            if resolved_id not in server_session_ids:
                try:
                    await session_mgr._db.execute(
                        "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                        (user_id, resolved_id)
                    )
                    await session_mgr._db.commit()
                except Exception:
                    pass
                
                if user_id in session_mgr._active_sessions and session_mgr._active_sessions[user_id]["session_id"] == resolved_id:
                    del session_mgr._active_sessions[user_id]
                    
                await query.edit_message_text(
                    f"❌ Session <code>{html.escape(resolved_id[:8])}</code> has been deleted on the server.",
                    parse_mode="HTML",
                )
                return

        success = await session_mgr.switch_session(user_id, resolved_id)

        if not success and server_sessions:
            server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
            if resolved_id in server_session_ids:
                s_obj = None
                for s in server_sessions:
                    if s.get("id") == resolved_id:
                        s_obj = s
                        break
                
                if s_obj:
                    s_dir = s_obj.get("directory", "")
                    s_title = s_obj.get("title", "")
                    from datetime import datetime
                    now_iso = datetime.utcnow().isoformat()
                    try:
                        await session_mgr._db.execute(
                            "INSERT INTO sessions (user_id, opencode_session_id, work_dir, name, created_at, last_active, is_active, message_count) "
                            "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                            (user_id, resolved_id, s_dir, s_title, now_iso, now_iso)
                        )
                        await session_mgr._db.commit()
                        success = await session_mgr.switch_session(user_id, resolved_id)
                    except Exception:
                        pass

        if success:
            await query.edit_message_text(
                f"✅ Switched to session <code>{html.escape(resolved_id[:8])}</code>",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text(
                f"❌ Session <code>{html.escape(resolved_id[:8])}</code> not found.",
                parse_mode="HTML",
            )

    # 1.3 Delete Session tap
    elif data.startswith("delsess:"):
        target_id = data[len("delsess:"):]
        
        from handlers.messages import ensure_server_running
        if not await ensure_server_running(update, context, user_id):
            return

        resolved_id = None
        server_sessions = []
        try:
            server_sessions = await oc_client.list_sessions()
            for s in server_sessions:
                s_id = s.get("id", "")
                if s_id.lower().startswith(target_id.lower()):
                    resolved_id = s_id
                    break
        except Exception as e:
            logger.warning(f"Could not verify session list on server during delete callback: {e}")

        if not resolved_id:
            resolved_id = target_id

        try:
            # 1. Delete on OpenCode Server
            await oc_client.delete_session(resolved_id)
        except Exception as e:
            logger.warning(f"Failed to delete session {resolved_id} on server, proceeding locally: {e}")

        # 2. Delete locally in SQLite DB
        try:
            await session_mgr._db.execute(
                "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                (user_id, resolved_id)
            )
            await session_mgr._db.commit()
        except Exception as e:
            logger.error(f"Failed to delete session {resolved_id} in local database: {e}")

        # 3. If active session was deleted, clear active cache and reset
        active_sid = await session_mgr.get_active_session(user_id)
        if active_sid == resolved_id:
            if user_id in session_mgr._active_sessions:
                del session_mgr._active_sessions[user_id]
            # Try to auto-resolve first remaining session or let the bot create a new one lazily
            await session_mgr.clear_session(user_id)

        await query.edit_message_text(
            f"🗑️ <b>Deleted:</b> Session <code>{html.escape(resolved_id[:8])}</code> has been permanently removed.",
            parse_mode="HTML",
        )

    # 1.5 Handle Sensitive Operations / Tool Permissions
    elif data.startswith("perm:"):
        parts = data.split(":")
        if len(parts) == 3:
            action = parts[1]      # "allow" or "deny"
            short_key = parts[2]   # 8-char lookup key
            
            pending_perms = bot_data.get("pending_permissions", {})
            pending = pending_perms.get(short_key)
            
            if not pending:
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n⚠️ <b>Request Expired:</b> This permission prompt is no longer valid or the bot was restarted.",
                    parse_mode="HTML"
                )
                return
                
            session_id = pending["session_id"]
            permission_id = pending["permission_id"]
            
            response_value = "once" if action == "allow" else "reject"
            
            try:
                # Call our client's respond_to_permission method
                success = await oc_client.respond_to_permission(
                    session_id=session_id,
                    permission_id=permission_id,
                    response=response_value,
                    remember=False
                )
                
                # Delete from registry
                pending_perms.pop(short_key, None)
                
                # Format final notification status
                if response_value == "once":
                    status_text = "✅ <b>Approved:</b> The agent was allowed to perform this operation."
                else:
                    status_text = "❌ <b>Rejected:</b> The agent was denied permission to perform this operation."
                
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n{status_text}",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error responding to permission {permission_id} in callback: {e}", exc_info=True)
                await query.edit_message_text(
                    text=f"{query.message.text}\n\n⚠️ <b>Error:</b> Failed to submit decision to OpenCode server: {e}",
                    parse_mode="HTML"
                )

    # 2. Switch Model tap
    elif data.startswith("model:"):
        new_model = data[len("model:"):]
        await session_mgr.set_model(user_id, new_model)
        await query.edit_message_text(
            f"✅ Model changed to <code>{html.escape(new_model)}</code>\n\n"
            f"<i>This applies to your current session.</i>",
            parse_mode="HTML",
        )

    # 2.3 Switch Mode/Agent tap
    elif data.startswith("mode:"):
        new_mode = data[len("mode:"):]
        await session_mgr.set_mode(user_id, new_mode)
        
        mode_emojis = {
            "build": "🔨",
            "plan": "📋",
            "pentester": "🕵️",
        }
        emoji = mode_emojis.get(new_mode.lower(), "🤖")
        
        await query.edit_message_text(
            f"{emoji} Mode changed to <b>{html.escape(new_mode)}</b>\n\n"
            f"<i>OpenCode will now use the '{html.escape(new_mode)}' agent configuration.</i>",
            parse_mode="HTML",
        )

    # 2.5 Drill Down Provider Tapped
    elif data.startswith("prov:"):
        provider_id = data[len("prov:"):]
        
        provider_emojis = {
            "anthropic": "🟧",
            "openai": "🟩",
            "google": "🔵",
            "ollama": "🟪",
            "chutes": "🟥",
            "opencode": "🟡",
        }

        try:
            models_data = await oc_client.get_available_models()
            all_providers = models_data.get("all", [])
            connected = models_data.get("connected", [])
            
            target_provider = None
            for p in all_providers:
                p_id = p.get("id", "")
                if p_id.lower() == provider_id.lower():
                    target_provider = p
                    break
            
            if not target_provider:
                await query.edit_message_text("❌ No models found for this provider on the server.")
                return
                
            p_id = target_provider.get("id", "")
            p_name = target_provider.get("name", p_id)
            emoji = provider_emojis.get(p_id.lower(), "⚪")
            models = target_provider.get("models", {})
            
            sub_keyboard = []
            sub_keyboard.append([InlineKeyboardButton(f"───【 {emoji} {p_name.upper()} MODELS 】───", callback_data="noop")])
            
            model_buttons = []
            for m_id, m in models.items():
                m_name = m.get("name", m_id)
                path = f"{p_id}/{m_id}"
                
                display_name = m_name[:15] + "..." if len(m_name) > 18 else m_name
                model_buttons.append(InlineKeyboardButton(f"🤖 {display_name}", callback_data=f"model:{path}"))
            
            for j in range(0, len(model_buttons), 2):
                sub_keyboard.append(model_buttons[j:j+2])
                
            sub_keyboard.append([InlineKeyboardButton(f"« ⬅️ Back to Providers", callback_data="prov_back")])
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(sub_keyboard))
            
        except Exception as e:
            logger.error(f"Failed to drill down to provider {provider_id}: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Failed to query models for {provider_id}: {e}")

    # 2.6 Back to Provider Menu Tapped
    elif data == "prov_back":
        provider_emojis = {
            "anthropic": "🟧",
            "openai": "🟩",
            "google": "🔵",
            "ollama": "🟪",
            "chutes": "🟥",
            "opencode": "🟡",
        }

        try:
            models_data = await oc_client.get_available_models()
            all_providers = models_data.get("all", [])
            connected = models_data.get("connected", [])
            connected_lower = {c.lower() for c in connected if c}
            
            main_keyboard = []
            for p in all_providers:
                p_id = p.get("id", "")
                p_id_lower = p_id.lower() if p_id else ""
                if p_id_lower not in connected_lower:
                    continue
                models = p.get("models", {})
                if not models:
                    continue
                
                p_name = p.get("name", p_id)
                emoji = provider_emojis.get(p_id_lower, "⚪")
                main_keyboard.append([InlineKeyboardButton(f"───【 {emoji} {p_name.upper()} 】───", callback_data=f"prov:{p_id}")])
                
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(main_keyboard))
            
        except Exception as e:
            logger.error(f"Failed to return to main provider menu: {e}", exc_info=True)
            await query.edit_message_text(f"❌ Failed to load main provider menu: {e}")

    # 3. Dynamic File Explorer Navigation
    elif data.startswith("proj_nav:"):
        base_dir = os.path.abspath(config.opencode_work_dir)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        
        browsing_dir = context.user_data.get("browsing_dir")
        if not browsing_dir:
            browsing_dir = current_dir
            context.user_data["browsing_dir"] = browsing_dir
            
        browsing_dir = os.path.abspath(browsing_dir)
        action = data[len("proj_nav:"):]
        
        if action == "parent":
            parent_dir = os.path.abspath(os.path.dirname(browsing_dir))
            if os.path.normcase(browsing_dir) != os.path.normcase(base_dir):
                if os.path.normcase(parent_dir).startswith(os.path.normcase(base_dir)):
                    browsing_dir = parent_dir
                    context.user_data["browsing_dir"] = browsing_dir
            
            text, reply_markup = render_project_explorer(browsing_dir, base_dir, current_dir)
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
            
        elif action == "select":
            await execute_project_switch(query, context, user_id, browsing_dir)
            
        elif action.startswith("sub:"):
            try:
                sub_idx = int(action[len("sub:"):])
                subdirs = get_subdirectories(browsing_dir)
                if 0 <= sub_idx < len(subdirs):
                    target_dir = os.path.abspath(os.path.join(browsing_dir, subdirs[sub_idx]))
                    if os.path.normcase(target_dir).startswith(os.path.normcase(base_dir)):
                        browsing_dir = target_dir
                        context.user_data["browsing_dir"] = browsing_dir
                
                text, reply_markup = render_project_explorer(browsing_dir, base_dir, current_dir)
                await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error navigating subfolder: {e}", exc_info=True)
                await query.edit_message_text(f"❌ Navigation error: {e}")

    # 4. Workspace Deletion callback query flows
    elif data.startswith("delproj_ask:"):
        target_folder = data[len("delproj_ask:"):]
        
        # Verify it's not active
        base_dir = os.path.abspath(config.opencode_work_dir)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        
        target_path = os.path.abspath(os.path.join(base_dir, target_folder))
        
        if os.path.normcase(target_path) == os.path.normcase(current_dir):
            await query.edit_message_text(
                f"❌ <b>Cannot Delete Active Workspace:</b>\n"
                f"You cannot delete your currently active workspace directory <code>{html.escape(target_folder)}</code>.\n"
                f"Please switch to another workspace folder first using /project, then attempt deletion.",
                parse_mode="HTML"
            )
            return

        warning_text = (
            f"⚠️ <b>WARNING: Permanent Deletion!</b>\n\n"
            f"Are you absolutely sure you want to permanently delete the folder <code>{html.escape(target_folder)}</code>?\n\n"
            f"This will physically **erase all source files and directories** inside this workspace on your computer!\n\n"
            f"🚨 <b>THIS ACTION CANNOT BE UNDONE!</b>"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("💥 Yes, Erase Folder", callback_data=f"delproj_confirm:{target_folder}"),
                InlineKeyboardButton("↩️ Cancel", callback_data="delproj_cancel")
            ]
        ]
        
        await query.edit_message_text(warning_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("delproj_confirm:"):
        target_folder = data[len("delproj_confirm:"):]
        base_dir = os.path.abspath(config.opencode_work_dir)
        target_path = os.path.abspath(os.path.join(base_dir, target_folder))
        
        # 1. Security Check: Path traversal exploit guard
        if not os.path.normcase(target_path).startswith(os.path.normcase(base_dir)) or target_path == base_dir:
            await query.edit_message_text(
                "❌ <b>Security Violation:</b> Deletion path lies outside your parent workspace directory.",
                parse_mode="HTML"
            )
            return
            
        # 2. Check if currently active (double check)
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        if os.path.normcase(target_path) == os.path.normcase(current_dir):
            await query.edit_message_text(
                "❌ <b>Cannot Delete Active Workspace:</b> Please switch to another workspace first.",
                parse_mode="HTML"
            )
            return

        # 3. Perform physical deletion & SQLite sync
        import shutil
        try:
            if os.path.exists(target_path):
                # Clean up local SQLite DB session history inside the workspace
                # Let's delete all sessions associated with this path
                # Since workspace paths are normalized, let's normalize this target path
                norm_target_path = os.path.normcase(target_path)
                
                # Fetch all sessions in the DB to match work_dir
                cursor = await session_mgr._db.execute("SELECT opencode_session_id, work_dir FROM sessions WHERE user_id = ?", (user_id,))
                rows = await cursor.fetchall()
                stale_sids = []
                
                def norm(p):
                    if not p:
                        return ""
                    return os.path.normcase(os.path.normpath(os.path.abspath(p)))

                for row in rows:
                    sid, wd = row
                    if norm(wd) == norm_target_path:
                        stale_sids.append(sid)

                if stale_sids:
                    for sid in stale_sids:
                        await session_mgr._db.execute(
                            "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                            (user_id, sid)
                        )
                    await session_mgr._db.commit()

                    # Clear session mgr active cache if active
                    active_sid = await session_mgr.get_active_session(user_id)
                    if active_sid in stale_sids:
                        if user_id in session_mgr._active_sessions:
                            del session_mgr._active_sessions[user_id]
                
                # Physical rmtree
                shutil.rmtree(target_path)
                
                await query.edit_message_text(
                    f"🗑️ <b>Folder Deleted Successfully!</b>\n\n"
                    f"The workspace folder <code>{html.escape(target_folder)}</code> and all its files have been permanently erased from your hard drive, "
                    f"and all session logs are cleared.",
                    parse_mode="HTML"
                )
            else:
                await query.edit_message_text(
                    f"❌ <b>Folder not found:</b> The folder <code>{html.escape(target_folder)}</code> no longer exists on disk.",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Failed to delete folder {target_path}: {e}", exc_info=True)
            await query.edit_message_text(
                f"❌ <b>Deletion Failed:</b> An error occurred while erasing the directory:\n"
                f"<code>{html.escape(str(e))}</code>",
                parse_mode="HTML"
            )

    elif data == "delproj_cancel":
        await query.edit_message_text(
            "↩️ <b>Deletion Cancelled.</b>\n\n"
            "Your workspace folder and code remain completely untouched.",
            parse_mode="HTML"
        )


# ──────────────────────────────────────────────
# Commands: /create_project and /delete_project
# ──────────────────────────────────────────────

async def create_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a new workspace folder inside the base directory."""
    import os
    import html
    
    user_id = update.effective_user.id
    config = context.bot_data["config"]
    
    if not context.args:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> /create_project <code>&lt;new_folder_name&gt;</code>\n\n"
            "Example: <code>/create_project backend-api</code>",
            parse_mode="HTML"
        )
        return

    name = " ".join(context.args).strip()
    
    # Strict filename sanitization (keep alphanumeric, space, dash, underscore)
    cleaned_name = "".join(c for c in name if c.isalnum() or c in ("-", "_", " ")).strip()
    
    if not cleaned_name:
        await update.message.reply_text(
            "⚠️ <b>Invalid folder name.</b> Only alphanumeric characters, dashes, underscores, and spaces are allowed.",
            parse_mode="HTML"
        )
        return

    base_dir = os.path.abspath(config.opencode_work_dir)
    target_path = os.path.abspath(os.path.join(base_dir, cleaned_name))
    
    # Security traversal check
    if not os.path.normcase(target_path).startswith(os.path.normcase(base_dir)):
        await update.message.reply_text(
            "❌ <b>Security Violation:</b> Target path lies outside your parent workspace directory.",
            parse_mode="HTML"
        )
        return

    # Check existence
    if os.path.exists(target_path):
        if os.path.isdir(target_path):
            await update.message.reply_text(
                f"ℹ️ <b>Folder already exists.</b> Switching you to <code>{html.escape(cleaned_name)}</code>...",
                parse_mode="HTML"
            )
            await execute_project_switch(update, context, user_id, target_path)
        else:
            await update.message.reply_text(
                f"❌ <b>File conflict:</b> A file named <code>{html.escape(cleaned_name)}</code> already exists at that path.",
                parse_mode="HTML"
            )
        return

    # Physical directory creation
    try:
        os.makedirs(target_path, exist_ok=True)
        await execute_project_switch(update, context, user_id, target_path)
    except Exception as e:
        logger.error(f"Failed to create workspace folder {target_path}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ <b>Folder Creation Failed:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )


async def delete_project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List workspaces in base directory for secure deletion."""
    import os
    import html
    
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    config = context.bot_data["config"]
    
    base_dir = os.path.abspath(config.opencode_work_dir)
    
    # If the user passed a direct folder name in context.args
    if context.args:
        arg_folder = " ".join(context.args).strip()
        cleaned_arg = "".join(c for c in arg_folder if c.isalnum() or c in ("-", "_", " ")).strip()
        
        target_path = os.path.abspath(os.path.join(base_dir, cleaned_arg))
        if not os.path.normcase(target_path).startswith(os.path.normcase(base_dir)) or target_path == base_dir:
            await update.message.reply_text(
                "❌ <b>Security Violation:</b> Target path lies outside your parent workspace directory.",
                parse_mode="HTML"
            )
            return
            
        if not os.path.exists(target_path) or not os.path.isdir(target_path):
            await update.message.reply_text(
                f"❌ <b>Workspace not found:</b> The folder <code>{html.escape(cleaned_arg)}</code> does not exist.",
                parse_mode="HTML"
            )
            return
            
        # Verify it's not active
        current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
        current_dir = os.path.abspath(current_dir)
        if os.path.normcase(target_path) == os.path.normcase(current_dir):
            await update.message.reply_text(
                f"❌ <b>Cannot Delete Active Workspace:</b>\n"
                f"You cannot delete your active workspace. Please switch to another workspace first using /project.",
                parse_mode="HTML"
            )
            return

        warning_text = (
            f"⚠️ <b>WARNING: Permanent Deletion!</b>\n\n"
            f"Are you absolutely sure you want to permanently delete the folder <code>{html.escape(cleaned_arg)}</code>?\n\n"
            f"This will physically **erase all source files and directories** inside this workspace on your computer!\n\n"
            f"🚨 <b>THIS ACTION CANNOT BE UNDONE!</b>"
        )
        
        keyboard = [
            [
                InlineKeyboardButton("💥 Yes, Erase Folder", callback_data=f"delproj_confirm:{cleaned_arg}"),
                InlineKeyboardButton("↩️ Cancel", callback_data="delproj_cancel")
            ]
        ]
        
        await update.message.reply_text(warning_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        return

    # Otherwise list all subfolders for interactive selection
    subdirs = get_subdirectories(base_dir)
    
    if not subdirs:
        await update.message.reply_text(
            "📭 <b>No workspace subdirectories found to delete.</b>",
            parse_mode="HTML"
        )
        return

    current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
    current_dir = os.path.abspath(current_dir)
    
    text = (
        "🗑️ <b>Delete Workspace Folder</b>\n\n"
        "Select a folder below to permanently delete it from your local machine. "
        "Stale session histories will be synced automatically.\n\n"
        "⚠️ <b>WARNING:</b> This erases physical code files!"
    )
    
    keyboard = []
    for name in subdirs:
        target_path = os.path.abspath(os.path.join(base_dir, name))
        is_active = (os.path.normcase(target_path) == os.path.normcase(current_dir))
        
        # Don't show delete button for the currently active workspace
        if is_active:
            continue
            
        button_text = f"❌ Delete {name}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"delproj_ask:{name}")])
        
    if not keyboard:
        await update.message.reply_text(
            "📭 <b>No other workspace folders found to delete.</b>\n"
            "<i>(You cannot delete your currently active workspace)</i>",
            parse_mode="HTML"
        )
        return
        
    keyboard.append([InlineKeyboardButton("↩️ Cancel", callback_data="delproj_cancel")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

