import re
import html
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Telegram's hard limit is 4096 chars; use 4000 for safety
DEFAULT_MAX_LENGTH = 4000


def format_opencode_response(text: str) -> str:
    """Convert OpenCode output to Telegram-safe HTML.
    
    Handles:
    - Code blocks with syntax highlighting
    - Inline code
    - Bold/italic markdown to HTML
    - Escaping special chars outside code blocks
    """
    if not text:
        return "<i>No response received.</i>"
    
    # Split into code blocks and non-code segments
    segments = _split_code_blocks(text)
    formatted_parts = []
    
    for is_code, content, lang in segments:
        if is_code:
            # Code blocks — minimal escaping (only HTML entities)
            escaped = html.escape(content)
            if lang:
                formatted_parts.append(f'<pre><code class="{html.escape(lang)}">{escaped}</code></pre>')
            else:
                formatted_parts.append(f'<pre>{escaped}</pre>')
        else:
            # Regular text — convert markdown to HTML
            formatted_parts.append(_markdown_to_html(content))
    
    return "\n".join(formatted_parts)


def _split_code_blocks(text: str) -> List[Tuple[bool, str, str]]:
    """Split text into segments: (is_code, content, language)."""
    segments = []
    # Match ```language\n...\n``` blocks
    pattern = re.compile(r'```(\w*)\n?(.*?)```', re.DOTALL)
    
    last_end = 0
    for match in pattern.finditer(text):
        # Text before this code block
        before = text[last_end:match.start()]
        if before.strip():
            segments.append((False, before, ""))
        
        lang = match.group(1)
        code = match.group(2).strip()
        segments.append((True, code, lang))
        last_end = match.end()
    
    # Remaining text after last code block
    after = text[last_end:]
    if after.strip():
        segments.append((False, after, ""))
    
    # If no code blocks found, return the whole text as non-code
    if not segments:
        segments.append((False, text, ""))
    
    return segments


def _markdown_to_html(text: str) -> str:
    """Convert common markdown to Telegram HTML."""
    # Escape HTML first
    text = html.escape(text)
    
    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    
    # Italic: *text* or _text_
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'(?<!\w)_(.+?)_(?!\w)', r'<i>\1</i>', text)
    
    # Inline code: `code`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    
    # Headers: # Header → bold
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    
    # Strikethrough: ~~text~~
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # Links: [text](url)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    
    return text


def split_message(text: str, max_length: int = DEFAULT_MAX_LENGTH) -> List[str]:
    """Split a long message into chunks that fit Telegram's limit.
    
    Splits intelligently:
    - At newlines when possible
    - Never in the middle of a code block
    - Adds page indicators for multi-part messages
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    remaining = text
    
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        
        # Find a good split point
        split_at = _find_split_point(remaining, max_length)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    
    # Add page indicators if multiple chunks
    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk}\n\n📄 <i>{i+1}/{total}</i>" for i, chunk in enumerate(chunks)]
    
    return chunks


def _find_split_point(text: str, max_length: int) -> int:
    """Find the best point to split text."""
    # Check if we're inside a code block
    pre_open = text[:max_length].rfind('<pre')
    pre_close = text[:max_length].rfind('</pre>')
    
    if pre_open > pre_close:
        # We're inside a <pre> block — split before it
        split_at = pre_open
        if split_at > 0:
            return split_at
    
    # Try to split at double newline (paragraph break)
    search_region = text[:max_length]
    double_nl = search_region.rfind('\n\n')
    if double_nl > max_length // 2:  # Only use if it's in the second half
        return double_nl + 1
    
    # Try single newline
    single_nl = search_region.rfind('\n')
    if single_nl > max_length // 3:
        return single_nl + 1
    
    # Last resort: split at space
    space = search_region.rfind(' ')
    if space > max_length // 3:
        return space + 1
    
    # Absolute last resort: hard split
    return max_length


def format_session_info(session: dict) -> str:
    """Format session info for display."""
    active = "🟢 Active" if session.get("is_active") else "⚪ Archived"
    session_id = session.get('session_id', 'unknown')
    short_id = session_id[:8] if len(session_id) > 8 else session_id
    model = session.get('model', 'default') or 'default'
    mode = session.get('mode', 'build')
    msgs = session.get('message_count', 0)
    created = session.get('created_at', 'unknown')[:16]  # Trim to datetime
    
    return (
        f"{active} <code>{short_id}</code>\n"
        f"   Model: {html.escape(model)} | Mode: {mode} | Messages: {msgs}\n"
        f"   Created: {created}"
    )


def format_error(error_msg: str) -> str:
    """Format an error message for Telegram."""
    return f"⚠️ <b>Error</b>\n\n<pre>{html.escape(str(error_msg))}</pre>"


def format_status(
    opencode_available: bool,
    session_info: dict | None,
    model: str,
) -> str:
    """Format bot status for display."""
    oc_status = "🟢 Connected" if opencode_available else "🔴 Disconnected"
    
    lines = [
        "<b>📊 Bot Status</b>",
        "",
        f"OpenCode Server: {oc_status}",
        f"Default Model: <code>{html.escape(model)}</code>",
    ]
    
    if session_info:
        sid = session_info.get('session_id', 'none')[:8]
        lines.extend([
            "",
            "<b>Current Session:</b>",
            f"  ID: <code>{sid}</code>",
            f"  Mode: {session_info.get('mode', 'build')}",
            f"  Messages: {session_info.get('message_count', 0)}",
            f"  Model: <code>{html.escape(session_info.get('model', 'default') or 'default')}</code>",
        ])
    else:
        lines.append("\nNo active session. Send a message to start one.")
    
    return "\n".join(lines)
