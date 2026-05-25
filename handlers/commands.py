"""
Telegram command handlers for the OpenCode bot.

Handles all slash commands: /start, /help, /new, /sessions, /switch,
/model, /share, /status, /mode.
"""

import html
import logging

from telegram import Update, BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
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
        "/project depth &lt;1-5&gt; — Configure recursive subfolder scan depth\n"
        "/enable — Enable live tool call & progress streaming\n"
        "/disable — Disable live progress streaming\n"
        "/sessions — List your recent sessions\n"
        "/switch <code>&lt;id&gt;</code> — Switch to a different session\n"
        "/model <code>&lt;name&gt;</code> — Change AI model\n"
        "/models — List all available models\n"
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
    """List recent sessions for this user in their active workspace.

    Uses the OpenCode server as the source of truth.  Sessions are filtered
    by matching the server's `directory` field to the user's currently
    active workspace, exactly like the OpenCode GUI does.
    """
    import os
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

    # -- 1. Fetch ALL sessions from the server ---------
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

    # -- 2. Prune local DB: delete any sessions not on the server --
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

    # -- 3. Filter server sessions to the current workspace --
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

    # -- 4. Build the display list ----------------------
    workspace_sessions.sort(
        key=lambda s: s.get("time", {}).get("updated", 0),
        reverse=True,
    )

    # Lookup locally-tracked data (message counts, model, etc.)
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

        # Update local DB title in background
        if s_title and s_id in local_map:
            try:
                await session_mgr._db.execute(
                    "UPDATE sessions SET name = ? WHERE opencode_session_id = ?",
                    (s_title, s_id)
                )
                await session_mgr._db.commit()
            except Exception:
                pass

    lines.append("\n<i>Use /switch &lt;id&gt; to switch sessions</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")



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
    
    # 1. Resolve short ID prefix to full ID if necessary
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

    # Fallback to local DB if server query failed or returned no matches
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

    # 2. Check if the resolved session exists on the server (if server is online)
    if server_sessions:
        server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
        if resolved_id not in server_session_ids:
            # Delete it from local DB as it was deleted on the server!
            try:
                await session_mgr._db.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                    (user_id, resolved_id)
                )
                await session_mgr._db.commit()
            except Exception:
                pass
            
            # Clean active sessions cache if needed
            if user_id in session_mgr._active_sessions and session_mgr._active_sessions[user_id]["session_id"] == resolved_id:
                del session_mgr._active_sessions[user_id]
                
            await update.message.reply_text(
                f"❌ Session <code>{html.escape(resolved_id[:8])}</code> has been deleted on the server.\n"
                f"Use /sessions to see your current sessions.",
                parse_mode="HTML",
            )
            return

    # 3. Switch to the session in local database
    success = await session_mgr.switch_session(user_id, resolved_id)

    # 4. If not found locally but exists on the server, dynamically register it in local DB and switch!
    if not success and server_sessions:
        server_session_ids = {s.get("id") for s in server_sessions if s.get("id")}
        if resolved_id in server_session_ids:
            # Find the session object
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
                
                # Insert into local DB
                try:
                    await session_mgr._db.execute(
                        "INSERT INTO sessions (user_id, opencode_session_id, work_dir, name, created_at, last_active, is_active, message_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
                        (user_id, resolved_id, s_dir, s_title, now_iso, now_iso)
                    )
                    await session_mgr._db.commit()
                    # Switch again
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
# Command: /project
# ──────────────────────────────────────────────
async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List project directories or switch to one by number or name."""
    import os
    user_id = update.effective_user.id
    session_mgr = context.bot_data["session_manager"]
    config = context.bot_data["config"]
    oc_client = context.bot_data["opencode_client"]

    def norm(p):
        if not p:
            return ""
        return os.path.normcase(os.path.normpath(os.path.abspath(p)))

    # 1. Base directory is the workspace parent folder configured in .env
    base_dir = os.path.abspath(config.opencode_work_dir)
    
    # 2. Get user's current working directory and scan settings
    current_dir = await session_mgr.get_user_work_dir(user_id, base_dir)
    current_dir = os.path.abspath(current_dir)
    
    user_depth = await session_mgr.get_user_scan_depth(user_id, config.project_scan_depth)

    # 3. Check if the user is changing the scan depth setting
    if context.args and context.args[0].lower() in ("depth", "level", "levels"):
        if len(context.args) > 1 and context.args[1].isdigit():
            new_depth = int(context.args[1])
            if 1 <= new_depth <= 5: # Limit depth between 1 and 5 for safety and speed
                await session_mgr.set_user_scan_depth(user_id, new_depth)
                await update.message.reply_text(
                    f"⚙️ <b>Scan depth updated successfully!</b>\n"
                    f"Projects will now be scanned up to <b>{new_depth}</b> level(s) deep inside your parent workspace.\n\n"
                    f"Type /project to see the updated list!",
                    parse_mode="HTML"
                )
                return
            else:
                await update.message.reply_text(
                    "⚠️ Invalid depth level. Please choose a depth level between 1 and 5.",
                    parse_mode="HTML"
                )
                return
        else:
            await update.message.reply_text(
                "⚠️ Usage: <code>/project depth &lt;number&gt;</code>\n"
                "Example: <code>/project depth 2</code> (scans up to 2 levels deep)",
                parse_mode="HTML"
            )
            return

    # 4. Pruned recursive scanning function to list project subfolders
    def scan_projects_recursive(current_dir, parent_dir, max_depth=3, current_depth=1):
        if current_depth > max_depth:
            return []
        
        ignore_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".gemini", ".idea", ".vscode", "build", "dist", ".next"}
        found_projects = []
        try:
            for name in sorted(os.listdir(current_dir)):
                if name.startswith(".") or name in ignore_dirs:
                    continue
                full_path = os.path.join(current_dir, name)
                if os.path.isdir(full_path):
                    # Relpath from parent_dir
                    rel_path = os.path.relpath(full_path, parent_dir)
                    found_projects.append(rel_path)
                    # Recurse down
                    found_projects.extend(scan_projects_recursive(full_path, parent_dir, max_depth, current_depth + 1))
        except Exception:
            pass
        return found_projects

    try:
        if not os.path.exists(base_dir) or not os.path.isdir(base_dir):
            await update.message.reply_text(
                f"❌ <b>Workspace directory does not exist:</b>\n<code>{html.escape(base_dir)}</code>",
                parse_mode="HTML"
            )
            return

        # Scan subfolders recursively (up to user-configured depth)
        projects = scan_projects_recursive(base_dir, base_dir, max_depth=user_depth)
    except Exception as e:
        logger.error(f"Failed to scan workspace directory {base_dir}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ <b>Error scanning projects folder:</b>\n<code>{html.escape(str(e))}</code>",
            parse_mode="HTML"
        )
        return

    # 5. Check if we received an argument to switch
    if context.args:
        arg = " ".join(context.args).strip()
        target_folder = None
        target_path = None

        # Check if the user specified the root workspace (0, 'root', or the base dir name)
        base_name = os.path.basename(base_dir)
        if arg == "0" or arg.lower() in ("root", "root workspace", "root workspace root", base_name.lower()):
            target_folder = f"[Root] {base_name}"
            target_path = base_dir
        # Check if the argument is a valid absolute path on the system
        elif os.path.isabs(arg) and os.path.isdir(arg):
            target_path = os.path.abspath(arg)
            target_folder = os.path.basename(target_path) or "Root"
        # Check if argument is a number from the projects list
        elif arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(projects):
                target_folder = projects[idx].replace('\\', '/')
                target_path = os.path.abspath(os.path.join(base_dir, projects[idx]))
            else:
                await update.message.reply_text(
                    f"⚠️ Invalid project number. Choose a number between 1 and {len(projects)}, or 0 for Root Workspace.",
                    parse_mode="HTML"
                )
                return
        else:
            # Case-insensitive name match inside subfolders (checks full relative path or leaf folder name)
            for p in projects:
                folder_name = os.path.basename(p)
                p_display = p.replace('\\', '/')
                if p.lower() == arg.lower() or p_display.lower() == arg.lower() or folder_name.lower() == arg.lower():
                    target_folder = p_display
                    target_path = os.path.abspath(os.path.join(base_dir, p))
                    break
            
            # If no exact name match, try a partial match inside subfolders
            if not target_path:
                for p in projects:
                    folder_name = os.path.basename(p)
                    p_display = p.replace('\\', '/')
                    if arg.lower() in p.lower() or arg.lower() in folder_name.lower():
                        target_folder = p_display
                        target_path = os.path.abspath(os.path.join(base_dir, p))
                        break

        if not target_path:
            await update.message.reply_text(
                f"⚠️ Project folder <code>{html.escape(arg)}</code> not found inside your workspace directory.\n\n"
                f"Type /project to see the list of available projects.",
                parse_mode="HTML"
            )
            return

        # Save to database
        await session_mgr.set_user_work_dir(user_id, target_path)

        # 6. Dynamic Restart & Isolation Logic:
        # Inform the user that we are switching the project and restarting the OpenCode server to mount the directory.
        status_msg = await update.message.reply_text(
            f"⏳ <b>Switching project to:</b> <code>{html.escape(target_folder)}</code>...\n"
            f"Restarting OpenCode serve backend to physically isolate execution environment...",
            parse_mode="HTML"
        )

        async def update_status(text):
            try:
                await status_msg.edit_text(text, parse_mode="HTML")
            except Exception:
                await update.message.reply_text(text, parse_mode="HTML")

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

        # Clear the old session from SQLite and active cache
        await session_mgr.clear_session(user_id)

        # Create a fresh session inside the restarted backend scoped to the target directory
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

            await session_mgr.set_active_session(user_id, session_id, config.opencode_model, work_dir=target_path)

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
        return

    # 6. Show the list of available projects (no arguments provided)
    lines = [
        "<b>📁 Project Workspace Root:</b>",
        f"<code>{html.escape(base_dir)}</code>\n",
        f"📍 <b>Currently Active Folder:</b>",
        f"<code>{html.escape(current_dir)}</code>\n",
        "<b>📂 Available Projects:</b>"
    ]

    # Always render Option 0 (the root parent workspace itself!)
    is_root_active = (current_dir == base_dir)
    root_marker = "🔹" if is_root_active else "🏠"
    lines.append(f"  0. {root_marker} <code>[Root Workspace Root]</code>")

    if projects:
        for i, p in enumerate(projects):
            p_display = p.replace('\\', '/')
            is_active_marker = "🔹" if os.path.abspath(os.path.join(base_dir, p)) == current_dir else "📁"
            lines.append(f"  {i+1}. {is_active_marker} <code>{html.escape(p_display)}</code>")
        
        lines.append("")
        lines.append(f"⚙️ <i>Recursive Depth:</i> <code>{user_depth} level(s)</code> (Type <code>/project depth &lt;1-5&gt;</code> to change!)")
        lines.append("👉 <i>To switch, type:</i> <code>/project &lt;number&gt;</code> or <code>/project &lt;name&gt;</code>")
    else:
        lines.append("  <i>(No subdirectories found in the workspace root)</i>")
        lines.append(f"\n⚙️ <i>Recursive Depth:</i> <code>{user_depth} level(s)</code> (Type <code>/project depth &lt;1-5&gt;</code> to change!)")
        lines.append("\n💡 <i>Create directories inside your workspace root folder to manage multiple projects!</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


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
        BotCommand("enable", "Enable live progress streaming"),
        BotCommand("disable", "Disable live progress streaming"),
        BotCommand("sessions", "List your sessions"),
        BotCommand("switch", "Switch to another session"),
        BotCommand("model", "Change AI model"),
        BotCommand("models", "List all available models"),
        BotCommand("mode", "Toggle plan/build mode"),
        BotCommand("share", "Share current session"),
        BotCommand("status", "Bot & connection status"),
        BotCommand("id", "Show your Telegram user ID"),
    ]
    # 1. Set default command list globally
    await app.bot.set_my_commands(commands)
    
    # 2. Set explicitly for all private chats (ensures visibility in DMs)
    try:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    except Exception as e:
        logger.warning(f"Could not set commands for private chats scope: {e}")
        
    # 3. Set explicitly for all group chats (ensures visibility in group discussions)
    try:
        await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logger.warning(f"Could not set commands for group chats scope: {e}")
