# opencode-recall

A TUI (Terminal User Interface) to full-text search your [OpenCode](https://opencode.ai) conversations and resume them.

Built with Python + [Textual](https://textual.textualize.io/).

## Features

- **Full-text search** across all your OpenCode sessions and message content
- **Split-pane layout** — session list on the left, conversation preview on the right
- **Scope toggle** — search everywhere or filter to current directory
- **Debounced search** — stays responsive on large databases
- **Resume sessions** — press Enter to launch `opencode --session <id>`
- **Copy session ID** — Ctrl+Y copies to clipboard

## Install

Requires [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/alvaropma/opencode-recall.git
cd opencode-recall
uv sync
```

## Usage

```bash
uv run python recall.py
```

Or create a shell alias:

```bash
alias recall='cd /path/to/opencode-recall && uv run python recall.py'
```

## Keybindings

| Key | Action |
|---|---|
| Type anything | Search conversations |
| ↑↓ | Navigate session list |
| Enter | Resume selected session |
| Ctrl+Y | Copy session ID |
| Ctrl+E | Toggle scope (everywhere / current dir) |
| Esc | Quit |

## How it works

OpenCode stores all session data in a SQLite database at `~/.local/share/opencode/opencode.db`. This tool reads that database (read-only) and provides a searchable interface over your conversation history.