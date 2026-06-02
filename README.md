# 🤖 Telegram ➔ OpenCode Bridge Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![OpenCode: v0.1+](https://img.shields.io/badge/OpenCode-Compatible-brightgreen.svg)](https://opencode.ai)

A premium, feature-rich Python bot that bridges your Telegram client directly to [OpenCode](https://opencode.ai) — a powerful AI coding agent running on your local machine. Authorize your personal user account and get professional, agentic software engineering assistance right in your pocket.

---

## ✨ Features

- **Direct OpenCode Integration** — Routes your chat prompts directly to the OpenCode HTTP API or local server processes.
- **Dynamic Project Isolation** — Effortlessly view, create, delete, and switch workspace folders on your local machine using an interactive Telegram file explorer interface.
- **Mobile File-Upload Uploads** — Send files, code logs, or images directly from Telegram to upload them into your active workspace folder.
- **Robust Session Registry** — Seamlessly list, switch, delete, and share conversations, automatically synced with the backend SQLite database.
- **Conversational History View (`/history`)** — Chronologically fetches the active conversation, filtered of long execution logs, reasoning traces, or step metadata. Messages are sent sequentially with rate-limit respect and support pagination controls.
- **Live Tool Execution Streaming** — Watch the agent interact with your filesystem, write code, run bash commands, and search the web in real-time (`/enable` / `/disable`).
- **Flexible Model & Agent Mode Selectors** — Swap underlying LLM models on the fly (`/models`) and switch agent personas (e.g. Build, Plan, or Webtester) using clean interactive menus (`/mode`).
- **Secure by Default** — Enforces a strict user whitelist check on every message and callback action to prevent unauthorized access.
- **Robustness & Windows Stability** — Configured with automated API retries, suppressed verbose network noise, and Selector Event Loops for clean subprocess termination on Windows systems.

---

## 📋 Prerequisites

1. **Python 3.10+** installed on your system.
2. **OpenCode CLI** installed and initialized:
   ```bash
   npm install -g opencode-ai
   # or
   curl -fsSL https://opencode.ai/install | bash
   ```
3. **Telegram Bot Token** — Obtain one instantly via [@BotFather](https://t.me/BotFather).
4. **Your Telegram User ID** — Retrieve it using [@userinfobot](https://t.me/userinfobot), or start the bot and send `/id`.

---

## 🚀 Installation & Setup

### Option A: Standard PyPI Installation
Install and run the bot wrapper directly from the terminal:
```bash
pip install telegram-opencode-bridge-bot==0.1.15
telegram-opencode-bot
```
> **💡 Tip:** Use the `--env` flag anytime to reconfigure your variables:
> `telegram-opencode-bot --env`

---

### Option B: Manual Git Installation

1. **Clone the repository** and install dependencies:
   ```bash
   git clone https://github.com/MaheshNagabhairava/telegram-opencode-bridge-bot.git
   cd telegram-opencode-bot
   pip install -r requirements.txt
   ```

2. **Start the bot**:
   ```bash
   python bot.py
   ```
> **💡 Tip:** Use the `--env` flag anytime to reconfigure your variables:
> `python bot.py --env`
---

## 📱 Command Reference

| Command | Parameter | Description |
| :--- | :--- | :--- |
| **`/start`** | — | Prints the welcome card and verifies connectivity to the OpenCode server. |
| **`/help`** | — | Displays a complete guide of all available commands and tips. |
| **`/new`** | — | Clears the current active session cache to start a fresh topic. |
| **`/stop`** | — | Aborts any active running task or model generation process. |
| **`/status`** | — | Prints detailed statistics about the server connection and active session settings. |
| **`/id`** | — | Displays your Telegram User ID (useful for whitelisting). |
| **`/project`** | `[name]` | Opens the directory explorer. Optionally switches directly to the specified folder. |
| **`/create_project`** | `<name>` | Creates a new isolated folder workspace inside your primary workspace path. |
| **`/delete_project`** | `[name]` | Erases a workspace directory from disk (requires inline button verification). |
| **`/sessions`** | — | Lists your recent conversations in the current workspace to allow switching. |
| **`/history`** | `[count]` | Displays the chronological history of the active session, split into text batches. |
| **`/delete`** | — | Prompts an inline keyboard to permanently delete a session. |
| **`/models`** | — | Lists all models available on the OpenCode server to easily select a default. |
| **`/mode`** | — | Opens a menu to select agent personas (e.g. Build, Plan, custom testers). |
| **`/plan`** | — | Quick switch shortcut to Plan Mode (read-only analysis). |
| **`/build`** | — | Quick switch shortcut to Build Mode (read, write, execute permissions). |
| **`/enable`** | — | Enables real-time streaming of tool calls, shell executions, and file edits. |
| **`/disable`** | — | Disables streaming; the bot will only report the final LLM response. |
| **`/share`** | — | Fetches a public, shareable web preview URL of the active conversation. |
---

## ⚙️ Configuration Variables

The following parameters can be defined in your `.env` configuration file:

| Variable | Type | Description | Default |
| :--- | :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | *String* | Secret token issued by @BotFather. | **Required** |
| `AUTHORIZED_USERS` | *List* | Comma-separated list of whitelisted Telegram User IDs. | **Required** |
| `OPENCODE_SERVER_URL` | *String* | The target URL hosting the OpenCode endpoint. | `http://localhost:4096` |
| `OPENCODE_SERVER_USERNAME`| *String* | Optional Basic Auth username for server login. | *(empty)* |
| `OPENCODE_SERVER_PASSWORD`| *String* | Optional Basic Auth password for server login. | *(empty)* |
| `OPENCODE_MODEL` | *String* | The default LLM provider/model key configured. | `opencode/deepseek-v4-flash-free` |
| `OPENCODE_WORK_DIR` | *String* | Local parent path containing your projects. | `.` |
| `PROJECT_SCAN_DEPTH` | *Integer*| Max recursion depth used to search project directories. | `2` |
| `MAX_MESSAGE_LENGTH` | *Integer*| Maximum character limit before chunking Telegram texts. | `4000` |
| `RESPONSE_TIMEOUT` | *Integer*| Request timeout in seconds (set `0` to disable timeouts). | `300` |
| `DB_PATH` | *String* | Storage path for the SQLite session manager. | `sessions.db` |

---

## 🏗️ Architecture Layout

```
    👤 Telegram Client (Mobile / Desktop)
                   │
                   ▼ (HTTPS Long Polling / Webhook)
        🤖 Telegram Bridge Bot (Python)
                   │
                   ▼ (HTTP Local API / SSE Events)
       💻 OpenCode Server (localhost:4096)
                   │
          ┌────────┴────────┐
          ▼                 ▼
   🤖 LLM Provider    📁 Local Workspace
(Claude/DeepSeek/etc.)  (Shell / Filesystem)
```

---

## 🔒 Security Policy

> [!WARNING]
> This bot allows remote execution of shell commands, filesystem operations, and internet fetching on the host machine.
>
> 1. Keep `AUTHORIZED_USERS` configured strictly with **your own** Telegram User ID.
> 2. Avoid exposing your Telegram Bot Token or sharing public URLs if your host machine handles sensitive information.
> 3. Verify tool permissions in the console log if executing arbitrary files.

---

## 📁 Project Structure

```
telegram-opencode-bot/
├── bot.py                  # Main entry point, loops, and setup
├── config.py               # validated configuration loader
├── sessions.db             # Persistent SQL database (generated)
├── requirements.txt        # python dependencies
├── handlers/
│   ├── commands.py         # Slash command handlers (/start, /history, etc.)
│   └── messages.py         # Text & file message routing logic
├── opencode/
│   ├── client.py           # Async HTTP client wrapper for OpenCode API
│   └── server.py           # Background subprocess lifecycle management
├── sessions/
│   └── manager.py          # SQL persistence and state registry
└── utils/
    ├── formatting.py       # Conversions, splits, and custom table layouts
    └── security.py         # Rate-limiting, whitelist guards, and checks
```

---

## 📄 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more details.
