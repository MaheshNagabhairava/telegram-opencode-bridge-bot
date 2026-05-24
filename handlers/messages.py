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

logger = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages by routing them to OpenCode.

    Flow:
        1. Get or create an OpenCode session for this user
        2. Send typing indicator
        3. Forward prompt to OpenCode (HTTP API → subprocess fallback)
        4. Format and split the response
        5. Send back to Telegram
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

    # ── 1. Send typing indicator ──────────────────────────
    await update.message.chat.send_action(ChatAction.TYPING)

    # ── 2. Get or create session ──────────────────────────
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

    # ── 3. Send prompt to OpenCode ────────────────────────
    try:
        # Keep typing indicator alive during long operations
        typing_task = asyncio.create_task(
            _keep_typing(update, config.response_timeout)
        )

        session_info = await session_mgr.get_session_info(user_id)
        session_model = (session_info or {}).get("model", config.opencode_model) or config.opencode_model

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

        typing_task.cancel()

    except asyncio.TimeoutError:
        await update.message.reply_text(
            "⏰ <b>Request timed out.</b>\n\n"
            "OpenCode took too long to respond. Try a simpler prompt or check the server.",
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
    result = await oc_client.create_session(directory=config.opencode_work_dir)
    if not isinstance(result, dict):
        raise ValueError(f"Invalid session response from OpenCode server: {result}")
    
    session_id = (
        result.get("id")
        or result.get("session_id")
        or result.get("sessionId")
    )
    if not session_id:
        raise ValueError(f"OpenCode server response did not contain a session ID: {result}")

    await session_mgr.set_active_session(user_id, session_id, config.opencode_model)
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
