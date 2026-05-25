"""
Configuration module for the Telegram-OpenCode bot.

Loads settings from environment variables (with .env file support)
and exposes them via a validated dataclass singleton.
"""

import os
from dataclasses import dataclass, field
from typing import List
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """Application configuration loaded from environment variables.

    Attributes:
        telegram_bot_token: Bot token obtained from @BotFather.
        authorized_users: List of Telegram user IDs permitted to interact with the bot.
        opencode_server_url: Base URL of the OpenCode HTTP server.
        opencode_server_username: Optional username for OpenCode server authentication.
        opencode_server_password: Optional password for OpenCode server authentication.
        opencode_model: LLM model identifier used by OpenCode.
        opencode_work_dir: Working directory OpenCode operates in.
        max_message_length: Maximum characters per Telegram message chunk.
        response_timeout: Seconds to wait for an OpenCode response before timing out.
        db_path: File path for the SQLite session database.
    """

    # Telegram
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv('TELEGRAM_BOT_TOKEN', '')
    )
    authorized_users: List[int] = field(
        default_factory=lambda: [
            int(uid.strip())
            for uid in os.getenv('AUTHORIZED_USERS', '').split(',')
            if uid.strip().isdigit()
        ]
    )

    # OpenCode
    opencode_server_url: str = field(
        default_factory=lambda: os.getenv('OPENCODE_SERVER_URL', 'http://localhost:4444')
    )
    opencode_server_username: str = field(
        default_factory=lambda: os.getenv('OPENCODE_SERVER_USERNAME', '')
    )
    opencode_server_password: str = field(
        default_factory=lambda: os.getenv('OPENCODE_SERVER_PASSWORD', '')
    )
    opencode_model: str = field(
        default_factory=lambda: os.getenv('OPENCODE_MODEL', 'OpenCode Zen/DeepSeek V4 Flash Free')
    )
    opencode_work_dir: str = field(
        default_factory=lambda: os.getenv('OPENCODE_WORK_DIR', '.')
    )
    project_scan_depth: int = field(
        default_factory=lambda: int(os.getenv('PROJECT_SCAN_DEPTH', '2'))
    )

    # Limits
    max_message_length: int = field(
        default_factory=lambda: int(os.getenv('MAX_MESSAGE_LENGTH', '4000'))
    )
    response_timeout: int = field(
        default_factory=lambda: int(os.getenv('RESPONSE_TIMEOUT', '300'))
    )

    # Database
    db_path: str = field(
        default_factory=lambda: os.getenv('DB_PATH', 'sessions.db')
    )

    def validate(self) -> None:
        """Validate that all required configuration values are present.

        Raises:
            ValueError: If a required configuration value is missing.
        """
        if not self.telegram_bot_token:
            raise ValueError('TELEGRAM_BOT_TOKEN is required')
        if not self.authorized_users:
            raise ValueError(
                'AUTHORIZED_USERS is required (comma-separated Telegram user IDs)'
            )


config = Config()
