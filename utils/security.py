import time
import logging
import functools
from typing import Callable, List, Set

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


class UserAuthorizer:
    """Manages user authorization via whitelist."""
    
    def __init__(self, authorized_user_ids: List[int]):
        self._authorized: Set[int] = set(authorized_user_ids)
        logger.info(f"Authorizer initialized with {len(self._authorized)} authorized users")
    
    def is_authorized(self, user_id: int) -> bool:
        return user_id in self._authorized
    
    def add_user(self, user_id: int) -> None:
        self._authorized.add(user_id)
        logger.info(f"User {user_id} added to authorized list")
    
    def remove_user(self, user_id: int) -> None:
        self._authorized.discard(user_id)
        logger.info(f"User {user_id} removed from authorized list")


class RateLimiter:
    """Simple token bucket rate limiter per user."""
    
    def __init__(self, max_requests: int = 10, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[int, list[float]] = {}
    
    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        if user_id not in self._requests:
            self._requests[user_id] = []
        
        # Remove expired timestamps
        self._requests[user_id] = [
            t for t in self._requests[user_id]
            if now - t < self.window_seconds
        ]
        
        if len(self._requests[user_id]) >= self.max_requests:
            return False
        
        self._requests[user_id].append(now)
        return True
    
    def time_until_allowed(self, user_id: int) -> float:
        if user_id not in self._requests or not self._requests[user_id]:
            return 0.0
        oldest = min(self._requests[user_id])
        return max(0.0, self.window_seconds - (time.time() - oldest))


def sanitize_input(text: str) -> str:
    """Basic input sanitization.
    
    We don't need heavy sanitization since OpenCode handles its own security,
    but we strip some obvious problematic patterns.
    """
    if not text:
        return ""
    
    # Strip leading/trailing whitespace
    text = text.strip()
    
    # Limit length (prevent abuse)
    max_input_length = 10000
    if len(text) > max_input_length:
        text = text[:max_input_length] + "\n[Input truncated]"
    
    return text


def authorized(authorizer: UserAuthorizer, rate_limiter: RateLimiter | None = None):
    """Decorator to restrict handler to authorized users with optional rate limiting."""
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if not user:
                return
            
            user_id = user.id
            
            # Check authorization
            if not authorizer.is_authorized(user_id):
                logger.warning(f"Unauthorized access attempt from user {user_id} ({user.username})")
                await update.message.reply_text(
                    "🚫 You are not authorized to use this bot.\n"
                    f"Your user ID is: <code>{user_id}</code>",
                    parse_mode="HTML",
                )
                return
            
            # Check rate limit
            if rate_limiter and not rate_limiter.is_allowed(user_id):
                wait_time = rate_limiter.time_until_allowed(user_id)
                await update.message.reply_text(
                    f"⏳ Rate limited. Please wait {wait_time:.0f} seconds.",
                    parse_mode="HTML",
                )
                return
            
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator
