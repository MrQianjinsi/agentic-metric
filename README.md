# Agentic Metric

[中文文档](README-CN.md)

A local-only monitoring tool for AI coding agents. Track token usage and costs across Claude Code, Cursor, and more — with a TUI dashboard, system tray, and CLI.

**All data stays on your machine. No network requests, no telemetry, no data leaves your computer.** The tool only reads local agent data files (e.g. `~/.claude/`) and process info.

## Features

- **Live monitoring** — Detect running agent processes, incremental JSONL session parsing
- **Cost estimation** — Per-model pricing table, calculates API-equivalent costs
- **Today overview** — Sessions, token usage, and cost summary for the current day
- **Historical trends** — 30-day daily token/cost trends
- **TUI dashboard** — Terminal UI with 1-second live refresh, stacked token charts, and trend lines
- **System tray** — Sits in your system tray with a quick-access menu
- **Multi-agent** — Plugin architecture, supports Claude Code and Cursor, extensible

## Data Sources

| Agent | Path | Data |
|-------|------|------|
| Claude Code | `~/.claude/projects/` | JSONL sessions, token usage, model, branch |
| Claude Code | `~/.claude/stats-cache.json` | Daily activity stats |
| Cursor | Process detection | Running status, working directory |

All aggregated data is stored locally in `~/.local/share/agentic_metric/data.db` (SQLite).

## Installation

```bash
pip install agentic-metric
```

### Optional Dependencies

Linux system tray menu requires AppIndicator (pre-installed on most desktop distros):

```bash
# Ubuntu/Debian
sudo apt install gir1.2-ayatanaappindicator3-0.1

# Fedora
sudo dnf install libayatana-appindicator-gtk3
```

## Usage

```bash
agentic-metric status          # Show currently active agents
agentic-metric today           # Today's usage overview
agentic-metric history         # Historical trends (default 30 days)
agentic-metric history -d 7    # Last 7 days
agentic-metric sync            # Force sync data to local database
agentic-metric tui             # Launch TUI dashboard
agentic-metric tray            # Launch system tray icon
```

### TUI Keybindings

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Refresh data |
| `Tab` | Switch Dashboard / History tab |

## Privacy

- **Fully offline** — no network requests, no data sent anywhere
- **Read-only** — never modifies agent config or data files
- All stats stored in a local SQLite database
- Delete `~/.local/share/agentic_metric/` at any time to remove all data
