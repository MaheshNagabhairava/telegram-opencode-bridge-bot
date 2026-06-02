"""
Core message handler — bridges Telegram text messages to OpenCode.

This is the heart of the bot. Every non-command text message is routed
through here to OpenCode's HTTP API (or subprocess fallback).
"""

import logging
import asyncio
import os

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ChatAction

from utils.formatting import format_opencode_response, split_message, format_error
from utils.security import sanitize_input
from opencode.client import OpenCodeAPIError, OpenCodeConnectionError

logger = logging.getLogger(__name__)


async def ensure_server_running(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Ensure the OpenCode serve process is running in the correct directory.

    Uses an in-memory flag inside bot_data to avoid redundant local HTTP pings.
    If the server is offline, it dynamically boots it scoped to the correct directory.

    Returns True if the server is running, False otherwise.
    """
    bot_data = context.bot_data
    config = bot_data["config"]
    oc_client = bot_data["opencode_client"]
    session_mgr = bot_data["session_manager"]

    # 1. Check in-memory flag
    if bot_data.get("server_started"):
        return True

    # 2. If flag is False, check if the server is already reachable (e.g. started externally)
    if await oc_client.is_available():
        bot_data["server_started"] = True
        return True

    # 3. Server is offline - lazy launch it scoped to the user's active folder
    import html
    startup_notice = await update.effective_message.reply_text(
        "⏳ <b>Initializing OpenCode server...</b>\n"
        "This happens once on first startup to physically mount your workspace.",
        parse_mode="HTML"
    )

    async def update_startup_status(text):
        try:
            await startup_notice.edit_text(text, parse_mode="HTML")
        except Exception:
            await update.effective_message.reply_text(text, parse_mode="HTML")

    # Resolve last active directory for this user, falling back to default OPENCODE_WORK_DIR
    work_dir = await session_mgr.get_user_work_dir(user_id, config.opencode_work_dir)

    from urllib.parse import urlparse
    try:
        url_parsed = urlparse(config.opencode_server_url)
        hostname = url_parsed.hostname or "127.0.0.1"
        port = url_parsed.port or 8080
    except Exception:
        hostname = "127.0.0.1"
        port = 8080

    from opencode.server import restart_server
    logger.info(f"Lazy launching OpenCode server inside: {work_dir} on port {port}")

    started = await restart_server(work_dir, port=port, hostname=hostname)

    if not started:
        await update_startup_status(
            "❌ <b>Failed to start OpenCode server automatically.</b>\n\n"
            "Please make sure <code>opencode</code> is installed on your system or check the bot logs."
        )
        return False

    try:
        await startup_notice.delete()
    except Exception:
        pass

    bot_data["server_started"] = True
    return True


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages by routing them to OpenCode.

    Flow:
        1. Ensure the OpenCode server is running dynamically
        2. Get or create an OpenCode session for this user
        3. Send typing indicator
        4. Forward prompt to OpenCode (HTTP API → subprocess fallback)
        5. Format and split the response
        6. Send back to Telegram
    """
    user = update.effective_user
    user_id = user.id
    message_text = sanitize_input(update.message.text or "")

    if not message_text or not message_text.strip():
        return

    status_msg = None

    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]
    oc_client = bot_data["opencode_client"]
    config = bot_data["config"]

    # ── 1. Ensure OpenCode server is running ────────────────
    if not await ensure_server_running(update, context, user_id):
        return

    # ── 2. Send typing indicator ──────────────────────────
    await update.message.chat.send_action(ChatAction.TYPING)

    # ── 3. Get or create session ──────────────────────────
    session_id = await session_mgr.get_active_session(user_id)

    if not session_id:
        # Create a new OpenCode session
        try:
            session_id = await _create_session(oc_client, user_id, session_mgr, config)
        except Exception as e:
            logger.error(f"Failed to create session: {e}", exc_info=True)
            await update.message.reply_text(
                format_error(f"Failed to create session: {e}"),
                parse_mode="HTML",
            )
            return

    # ── 4. Send prompt to OpenCode ────────────────────────
    # Check if streaming is enabled
    is_streaming = await session_mgr.get_user_streaming(user_id, 0)
    
    # Send a premium dynamic phase status message to keep user informed in real-time
    status_msg = await update.message.reply_text(
        "🧠 <b>Thinking...</b>\n<i>Analyzing request and preparing a plan...</i>",
        parse_mode="HTML"
    )
    
    status_msg_holder = [status_msg]
    
    # Always spawn the SSE event stream listener so we can handle interactive permission prompts
    # (e.g. for sensitive files like .env) even if the user has disabled regular tool-call progress.
    sse_task = asyncio.create_task(
        _listen_and_stream_events(
            update=update,
            context=context,
            session_id=session_id,
            server_url=config.opencode_server_url,
            is_streaming=bool(is_streaming == 1),
            status_msg_holder=status_msg_holder
        )
    )

    typing_task = asyncio.create_task(
        _keep_typing(update, config.response_timeout)
    )
    before_ids = set()
    sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
    sent_message_ids.clear()
    session_mgr.set_session_running(user_id, True)
    try:
        # Fetch message IDs before sending the prompt
        try:
            before_messages = await oc_client.list_messages(session_id)
            before_ids = {m.get("info", {}).get("id") for m in before_messages if m.get("info", {}).get("id")}
        except Exception as e:
            logger.warning(f"Failed to fetch messages before prompt: {e}")

        session_info = await session_mgr.get_session_info(user_id)
        session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model
        session_mode = (session_info or {}).get("mode", "build") or "build"

        try:
            response_text = await _send_to_opencode(
                oc_client=oc_client,
                session_id=session_id,
                prompt=message_text,
                model=session_model,
                agent=session_mode,
            )

            if response_text is None:
                # Session expired or was deleted/lost on the OpenCode server (e.g. server restart)
                logger.warning(f"Session {session_id[:8]}... not found on server (returned null). Creating a new session and retrying...")
                session_id = await _create_session(oc_client, user_id, session_mgr, config)
                # Re-fetch model and mode for safe retry
                session_info = await session_mgr.get_session_info(user_id)
                session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model
                session_mode = (session_info or {}).get("mode", "build") or "build"
                response_text = await _send_to_opencode(
                    oc_client=oc_client,
                    session_id=session_id,
                    prompt=message_text,
                    model=session_model,
                    agent=session_mode,
                )
        except OpenCodeConnectionError as conn_err:
            # Connection crashed/failed - Reset flag and self-heal!
            logger.warning(f"Connection lost to OpenCode server: {conn_err}. Attempting to recover...")
            bot_data["server_started"] = False
            
            await update.message.reply_text(
                "⚠️ <i>Connection to OpenCode server was lost. Attempting to restart server and retry...</i>",
                parse_mode="HTML",
            )
            
            if await ensure_server_running(update, context, user_id):
                # Server is back up - recreate session and retry message!
                session_id = await _create_session(oc_client, user_id, session_mgr, config)
                session_info = await session_mgr.get_session_info(user_id)
                session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model
                session_mode = (session_info or {}).get("mode", "build") or "build"
                
                response_text = await _send_to_opencode(
                    oc_client=oc_client,
                    session_id=session_id,
                    prompt=message_text,
                    model=session_model,
                    agent=session_mode,
                )
            else:
                raise conn_err
        except OpenCodeAPIError as e:
            # Check if the session is missing on the server (404)
            if e.status == 404:
                logger.warning(f"Session {session_id[:8]}... not found on server (HTTP 404). Starting a new one...")
                # Delete the deleted session from DB
                try:
                    await session_mgr._db.execute(
                        "DELETE FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
                        (user_id, session_id)
                    )
                    await session_mgr._db.commit()
                except Exception:
                    pass
                
                # Clear cache
                if user_id in session_mgr._active_sessions:
                    del session_mgr._active_sessions[user_id]
                    
                # Create a brand new session and retry
                session_id = await _create_session(oc_client, user_id, session_mgr, config)
                session_info = await session_mgr.get_session_info(user_id)
                session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model
                session_mode = (session_info or {}).get("mode", "build") or "build"
                
                await update.message.reply_text(
                    "⚠️ <i>Active session was deleted or expired on the server. Starting a fresh session...</i>",
                    parse_mode="HTML",
                )
                
                # Retry sending
                response_text = await _send_to_opencode(
                    oc_client=oc_client,
                    session_id=session_id,
                    prompt=message_text,
                    model=session_model,
                    agent=session_mode,
                )
            else:
                raise

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⏰ <b>Request timed out.</b>\n\n"
            "OpenCode took too long to respond. Try a simpler prompt or check the server.",
            parse_mode="HTML",
        )
        return
    except OpenCodeAPIError as e:
        logger.error(f"OpenCode API error: {e}", exc_info=True)
        await update.message.reply_text(
            format_error(str(e)),
            parse_mode="HTML",
        )
        return
    except Exception as e:
        logger.error(f"OpenCode error: {e}", exc_info=True)
        await update.message.reply_text(
            format_error(str(e)),
            parse_mode="HTML",
        )
        return
    finally:
        session_mgr.set_session_running(user_id, False)
        if typing_task:
            typing_task.cancel()
        if sse_task:
            sse_task.cancel()
        # Clean up by deleting the temporary live phase status message
        if status_msg_holder and status_msg_holder[0]:
            try:
                await status_msg_holder[0].delete()
            except Exception:
                pass

    # ── 4. Track the message ──────────────────────────────
    await session_mgr.increment_message_count(user_id, prompt=message_text)

    # ── 5. Format and send response ───────────────────────
    # Fetch messages after prompt completes to get all multi-step assistant messages
    response_texts = []
    try:
        after_messages = await oc_client.list_messages(session_id)
        new_messages = [
            m for m in after_messages
            if m.get("info", {}).get("id") not in before_ids 
            and m.get("info", {}).get("id") not in sent_message_ids
            and m.get("info", {}).get("role") == "assistant"
        ]
        for m in new_messages:
            parts = m.get("parts", [])
            content_text = ""
            if isinstance(parts, list):
                text_parts = [
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content_text = "".join(text_parts)
            if content_text.strip():
                response_texts.append(content_text)
    except Exception as e:
        logger.warning(f"Failed to fetch messages after prompt: {e}")

    # Fallback to standard response if no intermediate texts were retrieved
    all_responses = response_texts if response_texts else ([response_text] if response_text else [])

    if not all_responses:
        if response_text == "ABORTED":
            return
        # Send a user-friendly status message to prevent getting stuck silently
        await update.message.reply_text(
            "ℹ️ <b>OpenCode finished execution.</b>\n<i>(No conversational text response was returned)</i>",
            parse_mode="HTML"
        )
        return

    for resp in all_responses:
        if not resp or resp == "ABORTED":
            continue

        # Format OpenCode output for Telegram
        formatted = format_opencode_response(resp)

        # Split into chunks if too long
        chunks = split_message(formatted, config.max_message_length)

        for i, chunk in enumerate(chunks):
            try:
                await update.message.reply_text(
                    chunk,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                # If HTML parsing fails, try sending as plain text
                logger.warning(f"HTML parse failed for chunk {i+1}, falling back to plain text: {e}")
                try:
                    # Strip HTML tags for plain text fallback
                    import re
                    plain = re.sub(r'<[^>]+>', '', chunk)
                    await update.message.reply_text(
                        plain,
                        disable_web_page_preview=True,
                    )
                except Exception as e2:
                    logger.error(f"Failed to send chunk {i+1} even as plain text: {e2}")

            # Small delay between chunks to respect rate limits
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)


async def _create_session(oc_client, user_id, session_mgr, config):
    """Create a new OpenCode session and register it."""
    # Fetch preferred user workspace directory, falling back to base configuration path
    work_dir = await session_mgr.get_user_work_dir(user_id, config.opencode_work_dir)
    
    result = await oc_client.create_session(directory=work_dir)
    if not isinstance(result, dict):
        raise ValueError(f"Invalid session response from OpenCode server: {result}")
    
    session_id = (
        result.get("id")
        or result.get("session_id")
        or result.get("sessionId")
    )
    if not session_id:
        raise ValueError(f"OpenCode server response did not contain a session ID: {result}")

    # Fetch user preferred model and mode, falling back to defaults
    preferred_model = await session_mgr.get_user_preferred_model(user_id, config.opencode_model)
    preferred_mode = await session_mgr.get_user_preferred_mode(user_id, "build")

    await session_mgr.set_active_session(
        user_id, session_id, preferred_model, work_dir=work_dir, mode=preferred_mode
    )
    return session_id


async def _send_to_opencode(oc_client, session_id, prompt, model, agent):
    """Send a prompt to OpenCode HTTP API.

    Returns:
        The response text from OpenCode, or None if the session does not exist.
    """
    logger.info(f"Sending to OpenCode API: session={session_id[:8]}... model={model} agent={agent}")
    response = await oc_client.send_message(session_id, prompt, model=model, agent=agent)
    if response is None:
        return None
    return response.content


async def _keep_typing(update: Update, max_seconds: int = 3600) -> None:
    """Keep sending typing indicators while we wait for OpenCode.

    Telegram typing indicator expires after ~5 seconds, so we
    refresh it every 4 seconds.
    """
    # If max_seconds is 0 or less, default to 1 hour (3600 seconds)
    limit = max_seconds if max_seconds and max_seconds > 0 else 3600
    try:
        elapsed = 0
        while elapsed < limit:
            await update.message.chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(4)
            elapsed += 4
    except asyncio.CancelledError:
        pass  # Expected when response arrives
    except Exception:
        pass  # Don't crash on typing indicator failures


async def _listen_and_stream_events(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: str,
    server_url: str,
    is_streaming: bool,
    status_msg_holder = None
):
    """Listens to global OpenCode events via SSE and handles tool progress/permission requests.
    Includes an automatic reconnect loop with exponential back-off to prevent getting stuck.
    """
    import aiohttp
    import json
    import html
    import uuid
    import time
    import os

    url = f"{server_url.rstrip('/')}/global/event"
    notified_calls = set()
    completed_calls = set()
    last_update_time = [0.0]
    last_status_text = ["🧠 <b>Thinking...</b>\n<i>Analyzing request and preparing a plan...</i>"]

    def truncate(text, max_len=500):
        if not text:
            return ""
        text = str(text)
        if len(text) > max_len:
            return text[:max_len] + "\n... (truncated)"
        return text

    async def update_status(text: str):
        now = time.time()
        last_status_text[0] = text
        # Throttling to respect Telegram API rate limits (minimum 1.5 seconds between message edits)
        if status_msg_holder and status_msg_holder[0] and (now - last_update_time[0] >= 1.5):
            try:
                await status_msg_holder[0].edit_text(text, parse_mode="HTML")
                last_update_time[0] = now
            except Exception as e:
                logger.debug(f"Failed to update status message: {e}")

    retry_delay = 1.0
    while True:
        try:
            async with aiohttp.ClientSession(read_bufsize=100 * 1024 * 1024) as sse_session:
                async with sse_session.get(url, headers={"Accept": "text/event-stream"}) as resp:
                    # Connection successful, reset retry delay
                    retry_delay = 1.0
                    
                    async for line in resp.content:
                        line_str = line.decode('utf-8').strip()
                        if not line_str or not line_str.startswith("data:"):
                            continue
                        
                        data_content = line_str[5:].strip()
                        try:
                            event_obj = json.loads(data_content)
                            payload = event_obj.get("payload", {})
                            if not isinstance(payload, dict):
                                continue
                            
                            properties = payload.get("properties", {})
                            if not isinstance(properties, dict):
                                continue
                            
                            event_session_id = (
                                properties.get("sessionID")
                                or properties.get("sessionId")
                                or properties.get("session_id")
                                or payload.get("sessionID")
                                or payload.get("sessionId")
                                or payload.get("session_id")
                                or ""
                            )
                            if event_session_id != session_id:
                                continue

                            event_type = payload.get("type", "")

                            # A. Handle Intermediate Assistant Message Completion (Real-time Streaming)
                            if event_type == "message.updated":
                                info = properties.get("info", {})
                                msg_id = info.get("id")
                                role = info.get("role")
                                completed = info.get("time", {}).get("completed")
                                
                                if role == "assistant" and completed:
                                    sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
                                    if msg_id not in sent_message_ids:
                                        sent_message_ids.add(msg_id)
                                        try:
                                            oc_client = context.bot_data["opencode_client"]
                                            messages = await oc_client.list_messages(session_id)
                                            target_msg = next((m for m in messages if m.get("info", {}).get("id") == msg_id), None)
                                            if target_msg:
                                                parts = target_msg.get("parts", [])
                                                content_text = ""
                                                if isinstance(parts, list):
                                                    text_parts = [
                                                        p.get("text", "")
                                                        for p in parts
                                                        if isinstance(p, dict) and p.get("type") == "text"
                                                    ]
                                                    content_text = "".join(text_parts)
                                                
                                                if content_text.strip():
                                                    # Delete the old status message at the top
                                                    if status_msg_holder and status_msg_holder[0]:
                                                        try:
                                                            await status_msg_holder[0].delete()
                                                        except Exception:
                                                            pass
                                                        status_msg_holder[0] = None

                                                    from utils.formatting import format_opencode_response, split_message
                                                    formatted = format_opencode_response(content_text)
                                                    chunks = split_message(formatted, context.bot_data["config"].max_message_length)
                                                    for i, chunk in enumerate(chunks):
                                                        await update.message.reply_text(
                                                            chunk,
                                                            parse_mode="HTML",
                                                            disable_web_page_preview=True,
                                                        )
                                                        if i < len(chunks) - 1:
                                                            await asyncio.sleep(0.5)

                                                    # Recreate the status indicator at the very bottom
                                                    if status_msg_holder:
                                                        try:
                                                            status_msg_holder[0] = await update.message.reply_text(
                                                                last_status_text[0],
                                                                parse_mode="HTML"
                                                            )
                                                            last_update_time[0] = time.time()
                                                        except Exception as e:
                                                            logger.warning(f"Failed to recreate status message at bottom: {e}")
                                        except Exception as e:
                                            logger.warning(f"Failed to stream intermediate message {msg_id}: {e}")

                            # B. Handle Permission Requested Popup (Always Enabled)
                            elif event_type == "permission.asked":
                                perm_id = properties.get("id") or properties.get("permissionID") or payload.get("id")
                                perm_type = properties.get("permission") or properties.get("type") or "execute"
                                patterns = properties.get("patterns", [])

                                if not perm_id:
                                    logger.warning("Received permission.asked event but no permission ID was found.")
                                    continue

                                # Register pending permission in-memory lookup to avoid Telegram 64-char callback limit
                                if "pending_permissions" not in context.bot_data:
                                    context.bot_data["pending_permissions"] = {}

                                short_key = uuid.uuid4().hex[:8]
                                context.bot_data["pending_permissions"][short_key] = {
                                    "session_id": session_id,
                                    "permission_id": perm_id
                                }

                                patterns_text = ""
                                if patterns:
                                    pat_list = "\n".join([f"• <code>{html.escape(str(p))}</code>" for p in patterns])
                                    patterns_text = f"\n<b>Target Resource(s):</b>\n{pat_list}"

                                tool_name = ""
                                tool_info = properties.get("tool", {})
                                if isinstance(tool_info, dict):
                                    tool_name = tool_info.get("name", "")
                                if not tool_name:
                                    tool_name = perm_type

                                msg = (
                                    f"🛡️ <b>OpenCode Permission Requested</b>\n\n"
                                    f"The agent is asking for confirmation to use the tool <code>{html.escape(tool_name)}</code>.\n"
                                    f"{patterns_text}\n\n"
                                    f"Do you want to allow this operation?"
                                )

                                keyboard = [
                                    [
                                        InlineKeyboardButton("✅ Yes, Allow", callback_data=f"perm:allow:{short_key}"),
                                        InlineKeyboardButton("❌ No, Deny", callback_data=f"perm:deny:{short_key}")
                                    ]
                                ]

                                await update.message.reply_text(
                                    msg,
                                    parse_mode="HTML",
                                    reply_markup=InlineKeyboardMarkup(keyboard)
                                )

                            # B. Handle Tool Execution Progress
                            elif event_type == "message.part.updated":
                                part = properties.get("part", {})
                                if not isinstance(part, dict):
                                    continue
                                
                                part_type = part.get("type", "")
                                if part_type == "tool":
                                    tool_name = part.get("tool", "unknown")
                                    call_id = part.get("callID", "unknown")
                                    state = part.get("state", {})
                                    if not isinstance(state, dict):
                                        continue
                                    
                                    status = state.get("status", "")
                                    input_data = state.get("input", {})
                                    output_data = state.get("output", "")
                                    metadata = state.get("metadata", {})
                                    if not isinstance(metadata, dict):
                                        metadata = {}

                                    # ── 1. Update In-Place Status Message (Always Active) ──
                                    if status in ("pending", "running") and status_msg_holder and status_msg_holder[0]:
                                        status_text = ""
                                        if tool_name == "bash":
                                            cmd = input_data.get("command") or input_data.get("content") or ""
                                            cmd_truncated = truncate(cmd, 60)
                                            status_text = f"💻 <b>Running shell command...</b>\n<code>{html.escape(cmd_truncated)}</code>"
                                        elif tool_name in ("edit", "write", "save"):
                                            path = input_data.get("path") or input_data.get("target") or input_data.get("filepath") or ""
                                            path_truncated = truncate(os.path.basename(path) if path else "", 60)
                                            status_text = f"📝 <b>Modifying file...</b>\n<code>{html.escape(path_truncated)}</code>"
                                        elif tool_name in ("read", "view", "show"):
                                            path = input_data.get("path") or input_data.get("target") or input_data.get("filepath") or ""
                                            path_truncated = truncate(os.path.basename(path) if path else "", 60)
                                            status_text = f"🔍 <b>Reading file...</b>\n<code>{html.escape(path_truncated)}</code>"
                                        elif tool_name in ("webfetch", "websearch", "search"):
                                            query = input_data.get("query") or input_data.get("url") or ""
                                            query_truncated = truncate(query, 60)
                                            status_text = f"🌐 <b>Searching web...</b>\n<code>{html.escape(query_truncated)}</code>"
                                        else:
                                            status_text = f"⚙️ <b>Executing tool <code>{html.escape(tool_name)}</code>...</b>"
                                        
                                        await update_status(status_text)

                                    # ── 2. Stream Full Tool Logs (Only if is_streaming is True) ──
                                    if is_streaming:
                                        # 1. Tool Call Started / Running
                                        if status in ("pending", "running") and call_id not in notified_calls:
                                            notified_calls.add(call_id)
                                            
                                            desc = input_data.get("description", "") if isinstance(input_data, dict) else ""
                                            desc_text = f" — <i>\"{html.escape(desc)}\"</i>" if desc else ""
                                            
                                            # Format arguments
                                            arg_lines = []
                                            if isinstance(input_data, dict):
                                                for k, v in input_data.items():
                                                    if k not in ("description", "content"):
                                                        arg_lines.append(f"<b>{html.escape(str(k))}:</b> {html.escape(truncate(str(v)))}")
                                            args_text = "\n".join(arg_lines)
                                            
                                            msg = (
                                                f"🛠️ <b>Calling Tool <code>{html.escape(tool_name)}</code></b>{desc_text}\n"
                                            )
                                            if args_text:
                                                msg += f"{args_text}\n"
                                                
                                            await update.message.reply_text(msg, parse_mode="HTML")

                                        # 2. Tool Completed
                                        elif status == "completed" and call_id not in completed_calls:
                                            completed_calls.add(call_id)
                                            
                                            exit_code = metadata.get("exit", 0)
                                            output_cleaned = truncate(str(output_data))
                                            
                                            msg = (
                                                f"✅ <b>Tool <code>{html.escape(tool_name)}</code> Completed</b> (Exit <code>{exit_code}</code>)\n"
                                            )
                                            if output_cleaned.strip():
                                                msg += f"<pre>{html.escape(output_cleaned)}</pre>"
                                            else:
                                                msg += f"<i>(No output returned)</i>"
                                                
                                            await update.message.reply_text(msg, parse_mode="HTML")

                                        # 3. Tool Failed
                                        elif status in ("failed", "error") and call_id not in completed_calls:
                                            completed_calls.add(call_id)
                                            
                                            output_cleaned = truncate(str(output_data))
                                            
                                            msg = (
                                                f"❌ <b>Tool <code>{html.escape(tool_name)}</code> Failed</b>\n"
                                            )
                                            if output_cleaned.strip():
                                                msg += f"<pre>{html.escape(output_cleaned)}</pre>"
                                            else:
                                                msg += f"<i>(No error description returned)</i>"
                                                
                                            await update.message.reply_text(msg, parse_mode="HTML")

                        except Exception as e:
                            logger.debug(f"Error parsing SSE event in listener: {e}")

        except asyncio.CancelledError:
            logger.debug("SSE streaming task listener cancelled by parent task.")
            break
        except Exception as e:
            err_name = e or type(e).__name__
            logger.warning(f"Error in SSE streaming task listener: {err_name}. Reconnecting in {retry_delay}s...")
            try:
                await asyncio.sleep(retry_delay)
            except asyncio.CancelledError:
                break
            retry_delay = min(retry_delay * 2, 10.0)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming file uploads (documents and gallery photos) from Telegram,
    download them to the active workspace, and trigger the OpenCode agent for analysis.
    """
    user = update.effective_user
    user_id = user.id
    
    document = update.message.document
    photo_list = update.message.photo

    if not document and not photo_list:
        return

    bot_data = context.bot_data
    session_mgr = bot_data["session_manager"]
    oc_client = bot_data["opencode_client"]
    config = bot_data["config"]

    import uuid
    import time

    # 1. Extract file metadata
    if document:
        filename = document.file_name or "uploaded_file"
        file_id = document.file_id
        file_size = document.file_size
    else:
        # It's a photo from gallery
        photo = photo_list[-1]  # Get largest resolution photo
        filename = f"photo_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
        file_id = photo.file_id
        file_size = photo.file_size

    # 2. Path Traversal & Security Sanitization
    sanitized_filename = os.path.basename(filename)

    # 3. Get active workspace directory
    work_dir = await session_mgr.get_user_work_dir(user_id, config.opencode_work_dir)
    os.makedirs(work_dir, exist_ok=True)
    destination_path = os.path.join(work_dir, sanitized_filename)

    # 4. Check 20MB Telegram Bot API download limit
    max_size = 20 * 1024 * 1024  # 20MB in bytes
    if file_size > max_size:
        await update.message.reply_text(
            f"⚠️ <b>File Too Large!</b>\n\n"
            f"Telegram standard bots are restricted to downloads under 20MB. "
            f"Your file size is <code>{file_size / (1024*1024):.2f}MB</code>.\n\n"
            f"Please copy the file manually to your local project folder:\n"
            f"<code>{html.escape(work_dir)}</code>",
            parse_mode="HTML"
        )
        return

    # 5. Overwrite Protection (Backup existing files)
    if os.path.exists(destination_path):
        backup_path = destination_path + ".bak"
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)  # Delete old backup if present
            os.rename(destination_path, backup_path)
            logger.info(f"Backed up existing file {sanitized_filename} to {sanitized_filename}.bak")
        except Exception as e:
            logger.warning(f"Failed to backup existing file {sanitized_filename}: {e}")

    import html

    # 6. Send progress / typing indicator
    status_notice = await update.message.reply_text(
        f"📥 <b>Downloading file...</b>\n"
        f"Saving <code>{html.escape(sanitized_filename)}</code> directly to your local project workspace.",
        parse_mode="HTML"
    )

    try:
        # Download file via Telegram API
        file_obj = await context.bot.get_file(file_id)
        await file_obj.download_to_drive(custom_path=destination_path)
        
        await status_notice.delete()
    except Exception as e:
        logger.error(f"Failed to download file from Telegram: {e}", exc_info=True)
        try:
            await status_notice.edit_text(
                f"❌ <b>Download Failed</b>\n\n"
                f"Failed to fetch file from Telegram: <code>{html.escape(str(e))}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # 7. Retrieve Caption / Prompt
    caption = sanitize_input(update.message.caption or "")

    if caption and caption.strip():
        # User uploaded a file AND wrote a caption/instruction (e.g. "Explain this code")
        # Format a unified prompt for the OpenCode agent
        prompt_text = (
            f"[System Notification: The user uploaded the file '{sanitized_filename}' "
            f"successfully into the active workspace directory. Please analyze it based on their prompt below.]\n\n"
            f"{caption}"
        )
        
        # Route to standard message pipeline!
        # First, ensure OpenCode server is running
        if not await ensure_server_running(update, context, user_id):
            return

        await update.message.chat.send_action(ChatAction.TYPING)

        # Get or create active session
        session_id = await session_mgr.get_active_session(user_id)
        if not session_id:
            try:
                session_id = await _create_session(oc_client, user_id, session_mgr, config)
            except Exception as se:
                logger.error(f"Failed to create session during document upload: {se}", exc_info=True)
                await update.message.reply_text(
                    format_error(f"Failed to create session: {se}"),
                    parse_mode="HTML"
                )
                return

        # Start dynamic phase status indicator and streaming
        is_streaming = await session_mgr.get_user_streaming(user_id, 0)
        status_msg = await update.message.reply_text(
            "🧠 <b>Thinking...</b>\n<i>Analyzing request and preparing a plan...</i>",
            parse_mode="HTML"
        )
        
        status_msg_holder = [status_msg]

        sse_task = asyncio.create_task(
            _listen_and_stream_events(
                update=update,
                context=context,
                session_id=session_id,
                server_url=config.opencode_server_url,
                is_streaming=bool(is_streaming == 1),
                status_msg_holder=status_msg_holder
            )
        )

        typing_task = asyncio.create_task(
            _keep_typing(update, config.response_timeout)
        )

        before_ids = set()
        sent_message_ids = context.user_data.setdefault("sent_message_ids", set())
        sent_message_ids.clear()
        session_mgr.set_session_running(user_id, True)
        try:
            session_info = await session_mgr.get_session_info(user_id)
            session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model
            session_mode = (session_info or {}).get("mode", "build") or "build"

            # Fetch message IDs before sending the prompt
            try:
                before_messages = await oc_client.list_messages(session_id)
                before_ids = {m.get("info", {}).get("id") for m in before_messages if m.get("info", {}).get("id")}
            except Exception as e:
                logger.warning(f"Failed to fetch messages before document prompt: {e}")

            response_text = await _send_to_opencode(
                oc_client=oc_client,
                session_id=session_id,
                prompt=prompt_text,
                model=session_model,
                agent=session_mode,
            )

            # Increment count
            await session_mgr.increment_message_count(user_id, prompt=caption)

            # Send response back to user
            # Fetch messages after prompt completes to get all multi-step assistant messages
            response_texts = []
            try:
                after_messages = await oc_client.list_messages(session_id)
                new_messages = [
                    m for m in after_messages
                    if m.get("info", {}).get("id") not in before_ids 
                    and m.get("info", {}).get("id") not in sent_message_ids
                    and m.get("info", {}).get("role") == "assistant"
                ]
                for m in new_messages:
                    parts = m.get("parts", [])
                    content_text = ""
                    if isinstance(parts, list):
                        text_parts = [
                            p.get("text", "")
                            for p in parts
                            if isinstance(p, dict) and p.get("type") == "text"
                        ]
                        content_text = "".join(text_parts)
                    if content_text.strip():
                        response_texts.append(content_text)
            except Exception as e:
                logger.warning(f"Failed to fetch messages after document prompt: {e}")

            # Fallback to standard response if no intermediate texts were retrieved
            all_responses = response_texts if response_texts else ([response_text] if response_text else [])

            if not all_responses:
                if response_text == "ABORTED":
                    return
                # Send a user-friendly status message to prevent getting stuck silently
                await update.message.reply_text(
                    "ℹ️ <b>OpenCode finished execution.</b>\n<i>(No conversational text response was returned)</i>",
                    parse_mode="HTML"
                )
                return

            for resp in all_responses:
                if not resp or resp == "ABORTED":
                    continue

                # Format OpenCode output for Telegram
                formatted = format_opencode_response(resp)

                # Split into chunks if too long
                chunks = split_message(formatted, config.max_message_length)

                for i, chunk in enumerate(chunks):
                    try:
                        await update.message.reply_text(
                            chunk,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as he:
                        import re
                        plain = re.sub(r'<[^>]+>', '', chunk)
                        await update.message.reply_text(
                            plain,
                            disable_web_page_preview=True,
                        )
                    if i < len(chunks) - 1:
                        await asyncio.sleep(0.5)

        except Exception as e:
            logger.error(f"Error analyzing uploaded file: {e}", exc_info=True)
            await update.message.reply_text(
                format_error(str(e)),
                parse_mode="HTML"
            )
        finally:
            session_mgr.set_session_running(user_id, False)
            if typing_task:
                typing_task.cancel()
            if sse_task:
                sse_task.cancel()
            if status_msg_holder and status_msg_holder[0]:
                try:
                    await status_msg_holder[0].delete()
                except Exception:
                    pass

    else:
        # File uploaded with NO caption/prompt.
        # Just send a high-fidelity confirmation card!
        size_display = f"{file_size / 1024:.2f} KB" if file_size < 1024*1024 else f"{file_size / (1024*1024):.2f} MB"
        
        confirmation = (
            f"📥 <b>File Saved Successfully!</b>\n\n"
            f"• <b>Filename:</b> <code>{html.escape(sanitized_filename)}</code>\n"
            f"• <b>Size:</b> <code>{size_display}</code>\n"
            f"• <b>Destination:</b> <code>{html.escape(work_dir)}</code>\n\n"
            f"<i>OpenCode can now read and access this file locally. Ask me anything about it!</i>"
        )
        
        await update.message.reply_text(confirmation, parse_mode="HTML")
