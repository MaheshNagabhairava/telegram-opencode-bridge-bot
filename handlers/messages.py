"""
Core message handler — bridges Telegram text messages to OpenCode.

This is the heart of the bot. Every non-command text message is routed
through here to OpenCode's HTTP API (or subprocess fallback).
"""

import logging
import asyncio

from telegram import Update
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
    startup_notice = await update.message.reply_text(
        "⏳ <b>Initializing OpenCode server...</b>\n"
        "This happens once on first startup to physically mount your workspace.",
        parse_mode="HTML"
    )

    async def update_startup_status(text):
        try:
            await startup_notice.edit_text(text, parse_mode="HTML")
        except Exception:
            await update.message.reply_text(text, parse_mode="HTML")

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
    
    sse_task = None
    if is_streaming == 1:
        sse_task = asyncio.create_task(
            _listen_and_stream_events(update, session_id, config.opencode_server_url)
        )

    typing_task = asyncio.create_task(
        _keep_typing(update, config.response_timeout)
    )
    try:
        session_info = await session_mgr.get_session_info(user_id)
        session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model

        try:
            response_text = await _send_to_opencode(
                oc_client=oc_client,
                session_id=session_id,
                prompt=message_text,
                model=session_model,
            )

            if response_text is None:
                # Session expired or was deleted/lost on the OpenCode server (e.g. server restart)
                logger.warning(f"Session {session_id[:8]}... not found on server (returned null). Creating a new session and retrying...")
                session_id = await _create_session(oc_client, user_id, session_mgr, config)
                # Re-fetch model for safe retry
                session_info = await session_mgr.get_session_info(user_id)
                session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model
                response_text = await _send_to_opencode(
                    oc_client=oc_client,
                    session_id=session_id,
                    prompt=message_text,
                    model=session_model,
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
                
                response_text = await _send_to_opencode(
                    oc_client=oc_client,
                    session_id=session_id,
                    prompt=message_text,
                    model=session_model,
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
        if typing_task:
            typing_task.cancel()
        if sse_task:
            sse_task.cancel()

    # ── 4. Track the message ──────────────────────────────
    await session_mgr.increment_message_count(user_id, prompt=message_text)

    # ── 5. Format and send response ───────────────────────
    if not response_text or response_text == "ABORTED":
        # Silent return for aborted, cancelled, or empty responses
        return

    # Format OpenCode output for Telegram
    formatted = format_opencode_response(response_text)

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

    await session_mgr.set_active_session(user_id, session_id, config.opencode_model, work_dir=work_dir)
    return session_id


async def _send_to_opencode(oc_client, session_id, prompt, model):
    """Send a prompt to OpenCode HTTP API.

    Returns:
        The response text from OpenCode, or None if the session does not exist.
    """
    logger.info(f"Sending to OpenCode API: session={session_id[:8]}... model={model}")
    response = await oc_client.send_message(session_id, prompt, model=model)
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


async def _listen_and_stream_events(update: Update, session_id: str, server_url: str):
    """Listens to global OpenCode events via SSE and posts tool-call progress live on Telegram."""
    import aiohttp
    import json
    import html

    url = f"{server_url.rstrip('/')}/global/event"
    notified_calls = set()
    completed_calls = set()

    def truncate(text, max_len=500):
        if not text:
            return ""
        text = str(text)
        if len(text) > max_len:
            return text[:max_len] + "\n... (truncated)"
        return text

    async with aiohttp.ClientSession() as sse_session:
        try:
            async with sse_session.get(url, headers={"Accept": "text/event-stream"}) as resp:
                async for line in resp.content:
                    line_str = line.decode('utf-8').strip()
                    if not line_str or not line_str.startswith("data:"):
                        continue
                    
                    data_content = line_str[5:].strip()
                    try:
                        event_obj = json.loads(data_content)
                        # Check if sessionID matches
                        payload = event_obj.get("payload", {})
                        if not isinstance(payload, dict):
                            continue
                        
                        properties = payload.get("properties", {})
                        if not isinstance(properties, dict):
                            continue
                        
                        event_session_id = properties.get("sessionID", "")
                        if event_session_id != session_id:
                            continue

                        event_type = payload.get("type", "")
                        if event_type == "message.part.updated":
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
            pass
        except Exception as e:
            logger.warning(f"Error in SSE streaming task listener: {e}")
