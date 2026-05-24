import aiosqlite
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages OpenCode sessions per Telegram user with SQLite persistence."""
    
    def __init__(self, db_path: str = "sessions.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        # In-memory cache for fast lookups
        self._active_sessions: Dict[int, Dict[str, Any]] = {}
    
    async def initialize(self) -> None:
        """Initialize the database and create tables if needed."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("""   
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                opencode_session_id TEXT NOT NULL,
                model TEXT DEFAULT '',
                mode TEXT DEFAULT 'build',
                message_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_active TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        """)
        await self._db.execute("""  
            CREATE INDEX IF NOT EXISTS idx_user_active 
            ON sessions(user_id, is_active)
        """)
        await self._db.commit()
        
        # Load active sessions into memory
        async with self._db.execute(
            "SELECT user_id, opencode_session_id, model, mode, message_count, created_at, last_active "
            "FROM sessions WHERE is_active = 1"
        ) as cursor:
            async for row in cursor:
                self._active_sessions[row[0]] = {
                    "session_id": row[1],
                    "model": row[2],
                    "mode": row[3],
                    "message_count": row[4],
                    "created_at": row[5],
                    "last_active": row[6],
                }
        
        logger.info(f"Session manager initialized. {len(self._active_sessions)} active sessions loaded.")
    
    async def get_active_session(self, user_id: int) -> Optional[str]:
        """Get the active OpenCode session ID for a user, or None."""
        session = self._active_sessions.get(user_id)
        return session["session_id"] if session else None
    
    async def get_session_info(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get full session info for a user."""
        return self._active_sessions.get(user_id)
    
    async def set_active_session(
        self, user_id: int, opencode_session_id: str, model: str = ""
    ) -> None:
        """Set or create an active session for a user."""
        now = datetime.utcnow().isoformat()
        
        # Deactivate any existing active session
        if user_id in self._active_sessions:
            await self._db.execute(
                "UPDATE sessions SET is_active = 0, last_active = ? WHERE user_id = ? AND is_active = 1",
                (now, user_id)
            )
        
        # Insert new active session
        await self._db.execute(
            "INSERT INTO sessions (user_id, opencode_session_id, model, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
            (user_id, opencode_session_id, model, now, now)
        )
        await self._db.commit()
        
        # Update cache
        self._active_sessions[user_id] = {
            "session_id": opencode_session_id,
            "model": model,
            "mode": "build",
            "message_count": 0,
            "created_at": now,
            "last_active": now,
        }
        
        logger.info(f"New session for user {user_id}: {opencode_session_id}")
    
    async def increment_message_count(self, user_id: int) -> None:
        """Increment the message count for a user's active session."""
        if user_id in self._active_sessions:
            self._active_sessions[user_id]["message_count"] += 1
            now = datetime.utcnow().isoformat()
            self._active_sessions[user_id]["last_active"] = now
            await self._db.execute(
                "UPDATE sessions SET message_count = message_count + 1, last_active = ? "
                "WHERE user_id = ? AND is_active = 1",
                (now, user_id)
            )
            await self._db.commit()
    
    async def set_mode(self, user_id: int, mode: str) -> None:
        """Set the mode (build/plan) for a user's active session."""
        if user_id in self._active_sessions:
            self._active_sessions[user_id]["mode"] = mode
            await self._db.execute(
                "UPDATE sessions SET mode = ? WHERE user_id = ? AND is_active = 1",
                (mode, user_id)
            )
            await self._db.commit()
    
    async def set_model(self, user_id: int, model: str) -> None:
        """Set the model for a user's active session."""
        if user_id in self._active_sessions:
            self._active_sessions[user_id]["model"] = model
            await self._db.execute(
                "UPDATE sessions SET model = ? WHERE user_id = ? AND is_active = 1",
                (model, user_id)
            )
            await self._db.commit()
    
    async def clear_session(self, user_id: int) -> None:
        """Deactivate the current session for a user (they'll get a new one on next message)."""
        if user_id in self._active_sessions:
            now = datetime.utcnow().isoformat()
            await self._db.execute(
                "UPDATE sessions SET is_active = 0, last_active = ? WHERE user_id = ? AND is_active = 1",
                (now, user_id)
            )
            await self._db.commit()
            del self._active_sessions[user_id]
            logger.info(f"Session cleared for user {user_id}")
    
    async def list_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        """List all sessions (active and archived) for a user."""
        sessions = []
        async with self._db.execute(
            "SELECT opencode_session_id, model, mode, message_count, created_at, last_active, is_active "
            "FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (user_id,)
        ) as cursor:
            async for row in cursor:
                sessions.append({
                    "session_id": row[0],
                    "model": row[1],
                    "mode": row[2],
                    "message_count": row[3],
                    "created_at": row[4],
                    "last_active": row[5],
                    "is_active": bool(row[6]),
                })
        return sessions
    
    async def switch_session(self, user_id: int, session_id: str) -> bool:
        """Switch a user to a different existing session."""
        # Check if session exists for this user
        async with self._db.execute(
            "SELECT opencode_session_id, model, mode, message_count, created_at "
            "FROM sessions WHERE user_id = ? AND opencode_session_id = ?",
            (user_id, session_id)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
        
        now = datetime.utcnow().isoformat()
        
        # Deactivate current
        await self._db.execute(
            "UPDATE sessions SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )
        
        # Activate target
        await self._db.execute(
            "UPDATE sessions SET is_active = 1, last_active = ? WHERE user_id = ? AND opencode_session_id = ?",
            (now, user_id, session_id)
        )
        await self._db.commit()
        
        # Update cache
        self._active_sessions[user_id] = {
            "session_id": row[0],
            "model": row[1],
            "mode": row[2],
            "message_count": row[3],
            "created_at": row[4],
            "last_active": now,
        }
        
        logger.info(f"User {user_id} switched to session {session_id}")
        return True
    
    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            logger.info("Session manager closed.")
