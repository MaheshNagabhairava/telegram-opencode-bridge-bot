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
            html_converted = _markdown_to_html(content)
            # Parse and convert markdown tables inside the HTML-converted text
            formatted_parts.append(_convert_markdown_tables(html_converted))
    
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


def _convert_markdown_tables(text: str) -> str:
    """Detect and convert Markdown tables to Telegram-friendly HTML cards or lists."""
    lines = text.split("\n")
    processed_lines = []
    
    in_table = False
    table_headers = []
    table_rows = []
    
    def flush_table():
        nonlocal in_table, table_headers, table_rows
        if not table_headers:
            in_table = False
            table_rows = []
            return []
        
        formatted_table = []
        num_cols = len(table_headers)
        
        if num_cols == 2:
            # 2 Columns (Checklist / Key-Value Card)
            header_left = table_headers[0].strip()
            header_right = table_headers[1].strip()
            
            # Strip tags for clean header title
            h_left_raw = re.sub(r'<[^>]+>', '', header_left).strip()
            formatted_table.append(f"<b>📋 {h_left_raw}</b>\n")
            
            for row in table_rows:
                if len(row) >= 2:
                    key = row[0].strip()
                    val = row[1].strip()
                    
                    # Strip any redundant bold tags from key for clean styling
                    clean_key = key
                    if clean_key.startswith("<b>") and clean_key.endswith("</b>"):
                        clean_key = clean_key[3:-4]
                        
                    formatted_table.append(f"• <b>{clean_key}:</b> {val}")
            formatted_table.append("") # Empty line after table
            
        else:
            # 3+ Columns (API Card Layout)
            method_idx = -1
            path_idx = -1
            other_idxs = []
            
            for idx, h in enumerate(table_headers):
                h_clean = h.strip().lower()
                h_raw = re.sub(r'<[^>]+>', '', h_clean)
                if "method" in h_raw:
                    method_idx = idx
                elif "path" in h_raw or "endpoint" in h_raw:
                    path_idx = idx
                else:
                    other_idxs.append(idx)
            
            for row in table_rows:
                method_val = row[method_idx].strip() if method_idx >= 0 and method_idx < len(row) else ""
                path_val = row[path_idx].strip() if path_idx >= 0 and path_idx < len(row) else ""
                
                # Clean method and path from any HTML tags
                method_raw = re.sub(r'<[^>]+>', '', method_val).upper().strip()
                path_raw = re.sub(r'<[^>]+>', '', path_val).strip()
                
                # Format method badge with colored emojis
                method_html = ""
                if method_raw:
                    emoji = "⚪"
                    if "GET" in method_raw and "POST" in method_raw:
                        emoji = "🟢/🔵"
                    elif "GET" in method_raw:
                        emoji = "🟢"
                    elif "POST" in method_raw:
                        emoji = "🔵"
                    elif "PUT" in method_raw:
                        emoji = "🟡"
                    elif "DELETE" in method_raw:
                        emoji = "🔴"
                    elif "PATCH" in method_raw:
                        emoji = "🟣"
                    
                    method_html = f"{emoji} <b>{method_raw}</b>"
                
                # Format path in monospaced code blocks
                path_html = ""
                if path_raw:
                    path_html = f" <code>{path_raw}</code>"
                
                card_title = f"{method_html}{path_html}".strip()
                if card_title:
                    formatted_table.append(card_title)
                
                # Add other columns as key-value bullets under this card
                for idx in other_idxs:
                    if idx < len(row):
                        col_name = table_headers[idx].strip()
                        col_val = row[idx].strip()
                        
                        col_name_raw = re.sub(r'<[^>]+>', '', col_name).strip()
                        
                        if col_val:
                            formatted_table.append(f"• <b>{col_name_raw}:</b> {col_val}")
                
                formatted_table.append("") # Empty line between cards
                
        # Reset state
        in_table = False
        table_headers = []
        table_rows = []
        
        return formatted_table

    i = 0
    while i < len(lines):
        line = lines[i]
        line_stripped = line.strip()
        
        if line_stripped.startswith("|"):
            cells = [c.strip() for c in line_stripped.split("|")[1:-1]]
            
            is_separator = False
            if cells:
                is_separator = all(re.match(r'^:?-+:?$', c) for c in cells)
                
            if is_separator:
                i += 1
                continue
                
            if not in_table:
                in_table = True
                table_headers = cells
            else:
                table_rows.append(cells)
        else:
            if in_table:
                processed_lines.extend(flush_table())
            processed_lines.append(line)
            
        i += 1
        
    if in_table:
        processed_lines.extend(flush_table())
        
    return "\n".join(processed_lines)



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
    name = session.get('name', '') or ''
    
    name_line = f"   <b>Conversation:</b> {html.escape(name)}\n" if name else ""
    return (
        f"{active} <code>{short_id}</code>\n"
        f"{name_line}"
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
