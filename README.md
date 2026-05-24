# 🤖 Telegram → OpenCode Bridge Bot

A lightweight Python bot that bridges your Telegram messages directly to [OpenCode](https://opencode.ai) — an AI coding agent running on your machine. Think of it as having Claude/GPT-powered coding assistance right in your pocket via Telegram.

## ✨ Features

- **Direct OpenCode integration** — routes your messages to OpenCode's HTTP API
- **Persistent sessions** — conversations maintain context across messages
- **Session management** — create, switch, list, and share sessions
- **Model switching** — change AI models on the fly (`/model`)
- **Plan/Build modes** — toggle between read-only analysis and full execution
- **Smart formatting** — code blocks with syntax highlighting in Telegram
- **Auto message splitting** — handles responses longer than Telegram's 4096 char limit
- **Security** — whitelist-based access control + rate limiting
- **Subprocess fallback** — works even without `opencode serve` via CLI

## 📋 Prerequisites

1. **Python 3.10+**
2. **OpenCode CLI** — install via:
   ```bash
   npm install -g opencode-ai
   # or
   curl -fsSL https://opencode.ai/install | bash
   ```
3. **Telegram Bot Token** — get one from [@BotFather](https://t.me/BotFather)
4. **Your Telegram User ID** — send `/id` to the bot after setup, or use [@userinfobot](https://t.me/userinfobot)

## 🚀 Quick Start

### 1. Clone & Install

```bash
cd telegram-opencode-bot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
AUTHORIZED_USERS=your_telegram_user_id
OPENCODE_MODEL=anthropic/claude-sonnet-4
```

> **💡 Tip:** Don't know your Telegram user ID? Start the bot with `AUTHORIZED_USERS=0` temporarily, send `/id` to the bot, then update the `.env` with your real ID.

### 3. Start OpenCode Server

In a separate terminal:

```bash
opencode serve --port 4444 --hostname 127.0.0.1
```

This starts the OpenCode HTTP API on `localhost:4096`.

### 4. Run the Bot

```bash
python bot.py
```

### 5. Chat!

Open Telegram, find your bot, and start coding! 🎉

## 📱 Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message & connection check |
| `/help` | Show all commands |
| `/new` | Start a fresh conversation |
| `/sessions` | List your recent sessions |
| `/switch <id>` | Switch to a different session |
| `/model <name>` | Change AI model(ex: /model opencode/deepseek-v4-flash-free) |
| `/mode <plan\|build>` | Toggle plan/build mode |
| `/share` | Share current session (public URL) |
| `/status` | Check connection & session details |
| `/id` | Show your Telegram user ID |
| `/stop` | Abort active model processing |
| `/models` | List all available models |

## 🏗️ Architecture

```
Telegram User
    │
    ▼
Telegram Bot (Python)
    │
    ├──► OpenCode HTTP API (localhost:4096)  ← primary
            │
            ├──► LLM Provider (Claude/GPT/Gemini)
            └──► Local Filesystem & Shell
```

## ⚙️ Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather | **Required** |
| `AUTHORIZED_USERS` | Comma-separated Telegram user IDs | **Required** |
| `OPENCODE_SERVER_URL` | OpenCode HTTP API URL | `http://localhost:4096` |
| `OPENCODE_SERVER_USERNAME` | OpenCode auth username | *(empty)* |
| `OPENCODE_SERVER_PASSWORD` | OpenCode auth password | *(empty)* |
| `OPENCODE_MODEL` | Default AI model | `anthropic/claude-sonnet-4` |
| `OPENCODE_WORK_DIR` | Working directory for OpenCode | `.` |
| `MAX_MESSAGE_LENGTH` | Max Telegram message length | `4000` |
| `RESPONSE_TIMEOUT` | Max wait for response (seconds) | `300` |
| `DB_PATH` | SQLite database path | `sessions.db` |

## 🔒 Security

- **User whitelist** — only Telegram user IDs in `AUTHORIZED_USERS` can use the bot
- **Rate limiting** — 20 requests per minute per user (configurable)
- **Input sanitization** — inputs are trimmed and length-limited
- **No public exposure** — designed to run on your local machine

> ⚠️ **Warning:** This bot executes AI-driven code on your machine. Only authorize trusted users.

## 📁 Project Structure

```
telegram-opencode-bot/
├── bot.py                  # Main entry point
├── config.py               # Environment configuration
├── handlers/
│   ├── commands.py         # Slash command handlers
│   └── messages.py         # Text message → OpenCode bridge
├── opencode/
│   ├── client.py           # OpenCode HTTP API client
│   └── subprocess.py       # Fallback CLI executor
├── sessions/
│   └── manager.py          # Per-user session tracking (SQLite)
├── utils/
│   ├── formatting.py       # Telegram message formatting
│   └── security.py         # Auth, rate limiting, sanitization
├── .env.example            # Environment template
├── requirements.txt        # Python dependencies
└── README.md               # This file
```

