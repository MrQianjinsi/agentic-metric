"""Configuration constants and paths."""

from pathlib import Path

# Claude Code data paths
CLAUDE_HOME = Path.home() / ".claude"
STATS_CACHE = CLAUDE_HOME / "stats-cache.json"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Codex CLI data paths
CODEX_HOME = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Cursor data paths
CURSOR_TRACKING_DB = Path.home() / ".cursor" / "ai-tracking" / "ai-code-tracking.db"
CURSOR_STATE_DB = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"

# VS Code (Copilot Chat) data paths
VSCODE_STORAGE_DIR = Path.home() / ".config" / "Code" / "User" / "workspaceStorage"
VSCODE_GLOBAL_CHAT_DIR = Path.home() / ".config" / "Code" / "User" / "globalStorage" / "emptyWindowChatSessions"

# Application data
DATA_DIR = Path.home() / ".local" / "share" / "agentic_metric"
DB_PATH = DATA_DIR / "data.db"

# Refresh intervals (seconds)
LIVE_REFRESH_INTERVAL = 1  # running sessions
DATA_SYNC_INTERVAL = 300  # history sync to sqlite
