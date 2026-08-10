# Local setup and debugging

This guide covers installing cabineteer on macOS and Windows, registering it with your AI client, and diagnosing the most common connection problems. The commands below are macOS unless a step is marked **Windows**; see the [Windows](#windows) section for the shell and config-path differences.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| macOS 12 Monterey or later | Intel and Apple Silicon both work |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | The only hard dependency you need to install manually |
| ~2 GB free disk | For the full install with CadQuery; lite mode needs < 100 MB |

Install uv if you don't have it:

```bash
brew install uv
# or without Homebrew:
curl -LsSf https://astral.sh/uv/install.sh | sh
```

uv manages Python automatically — you do not need a separate Python install.

---

## Install

Clone the repo and install in one step:

```bash
git clone https://github.com/chemrich/cabineteer.git
cd cabineteer
uv sync          # full install: CadQuery + rectpack + dev tools
```

Smoke-test the install:

```bash
uv run cabineteer --help
```

You should see the argparse help block listing `--http`, `--port`, `--host`, and `--max-port-attempts`. If that works, the server binary and all dependencies are present.

> **Lite mode** — if you hit CadQuery build errors (see [CadQuery won't install](#cadquery-wont-install) below), you can run without it. Parametric design, evaluation, cutlist BOM, and the full MCP server all work; 3D geometry and the HTML viewer are disabled.
>
> ```bash
> uv run --no-group full cabineteer --help
> ```

---

## Windows

Everything works on Windows; only the shell and the config-file paths differ.

**Install uv** (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then `git clone` and `uv sync` exactly as in [Install](#install) above.

**Register with Claude Code.** The macOS one-liner ends in `$(pwd)`, which only expands in a Unix shell — on Windows it produces a broken path. Either run the command from **Git Bash** (bundled with Git for Windows, where `$(pwd)` works), or pass the folder's absolute path instead:

```
claude mcp add cabineteer -- uv --directory C:\Users\yourname\cabineteer run cabineteer
```

Confirm it registered with `claude mcp list` — you should see `cabineteer`.

**Claude Desktop config** lives at `%APPDATA%\Claude\claude_desktop_config.json`. As on macOS, GUI apps don't inherit your terminal PATH, so use the absolute path to `uv.exe` (find it with `where uv`) and escape backslashes in JSON:

```json
{
  "mcpServers": {
    "cabineteer": {
      "command": "C:\\Users\\yourname\\.local\\bin\\uv.exe",
      "args": ["--directory", "C:\\Users\\yourname\\cabineteer", "run", "cabineteer"]
    }
  }
}
```

Fully quit and relaunch Claude Desktop after editing the config. Its MCP logs are at `%APPDATA%\Claude\Logs\mcp-server-cabineteer.log`.

---

## Launch modes

### stdio — Claude Code or Claude Desktop (recommended)

stdio is the default transport. The AI client launches `cabineteer` as a child process and communicates over stdin/stdout. No port is involved, so there are no port conflicts and no firewall rules to worry about.

**Claude Code** — register once at user scope so it's available in every project:

```bash
claude mcp add cabineteer -- uv --directory /absolute/path/to/cabineteer run cabineteer
```

Verify it registered:

```bash
claude mcp list
# cabineteer: uv --directory /…/cabineteer run cabineteer
```

Inside any Claude Code session, `/mcp` lists connected servers and confirms the thirty cabineteer tools are visible.

**Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "cabineteer": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/cabineteer", "run", "cabineteer"]
    }
  }
}
```

Replace `/absolute/path/to/cabineteer` with the real path (no `~` shorthand — Claude Desktop does not expand tildes). Restart Claude Desktop after saving. The hammer icon in the toolbar should show the cabinet tools.

### HTTP/SSE — persistent server or multi-client

Run a long-lived server when you want to keep the process running and connect to it from Gemini CLI, a browser client, or multiple sessions at once.

```bash
# Default port 3749, auto-increments if occupied
uv run cabineteer --http

# Specific port
uv run cabineteer --http --port 4200

# Bind all interfaces (e.g. for access from another machine on your LAN)
uv run cabineteer --http --host 0.0.0.0 --port 4200
```

The resolved port is printed to stderr and written to `/tmp/cabineteer.port`:

```bash
# Read the port without parsing log output
PORT=$(cat /tmp/cabineteer.port)
echo "Server is on port $PORT"
```

Confirm the SSE endpoint is reachable:

```bash
curl -N "http://127.0.0.1:${PORT}/sse"
# You should see the SSE stream open (event: endpoint …)
# Press Ctrl-C to close
```

Configure Gemini CLI (`~/.gemini/settings.json`):

```json
{
  "mcp": {
    "servers": {
      "cabineteer": { "url": "http://127.0.0.1:3749/sse" }
    }
  }
}
```

---

## Debugging connection problems

### "command not found: cabineteer"

The `cabineteer` script only exists inside the uv environment. Always launch via `uv run`:

```bash
# Wrong — only works after a global pip install, which is not recommended
cabineteer --http

# Right
uv run cabineteer --http

# Or activate the environment first
source .venv/bin/activate
cabineteer --http
```

When registering with Claude Code, the `uv --directory … run cabineteer` form handles this automatically.

### Claude Desktop: tools not appearing

Claude Desktop launches MCP servers using the PATH it inherits from launchd, which is **not** the same as your interactive terminal PATH. `uv` installed via Homebrew at `/opt/homebrew/bin/uv` may be invisible to GUI apps.

Fix: use the full absolute path to the `uv` binary in your config:

```bash
which uv   # e.g. /opt/homebrew/bin/uv
```

```json
{
  "mcpServers": {
    "cabineteer": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/Users/yourname/cabineteer", "run", "cabineteer"]
    }
  }
}
```

Also double-check:
- The path in `args` is absolute and the directory exists.
- You fully quit and relaunched Claude Desktop after editing the config (Cmd-Q, not just closing the window).
- There are no JSON syntax errors in the config file — a trailing comma or missing brace silently breaks the whole file.

Validate your JSON before saving:

```bash
python3 -m json.tool ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

### Claude Desktop: reading the MCP logs

Claude Desktop writes MCP server output to log files:

```
~/Library/Logs/Claude/mcp-server-cabineteer.log   # server stdout/stderr
~/Library/Logs/Claude/mcp.log                       # MCP host-side messages
```

Tail them while restarting Claude Desktop to see exactly what's failing:

```bash
tail -f ~/Library/Logs/Claude/mcp-server-cabineteer.log
tail -f ~/Library/Logs/Claude/mcp.log
```

### Port conflict in HTTP mode

If port 3749 is already in use, the server auto-increments through up to 20 ports by default. If all are occupied it exits. Widen the search or pick a different starting port:

```bash
uv run cabineteer --http --port 5000 --max-port-attempts 40
```

Find what's holding a port:

```bash
lsof -i :3749
```

Check whether a previous server is still running:

```bash
cat /tmp/cabineteer.port    # shows the port of the last server that wrote this file
```

### "No module named 'cabineteer'" after a fresh sync

If the package itself isn't importable even after `uv sync`, the venv's editable install is in a bad state (this can happen when uv or pip leaves behind stale dist-info). The reliable fix is a clean rebuild:

```bash
rm -rf .venv
uv sync
uv run cabineteer --help
```

If the error persists after a clean venv, confirm that `src/cabineteer/` exists and that `uv sync` completed without errors before trying anything else.

### CadQuery won't install

CadQuery has a large native dependency tree (OCCT). If the build fails or takes too long, switch to lite mode — everything except 3D geometry works:

```bash
# Install without CadQuery
uv sync --no-group full

# Launch in lite mode
uv run --no-group full cabineteer
```

The server will start and all thirty tools are available; the visualize tools return a "CadQuery not installed" error instead of geometry, `evaluate_cabinet` skips interference checks, PDF outputs are skipped (HTML/CSV/JSON still generate), and sheet layouts fall back to the pure-Python strip optimizer.

### Smoke-testing the stdio protocol manually

You can drive the server directly without a client to confirm basic health:

```bash
# Send an MCP initialize request and read the response
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}},"id":1}' \
  | uv run cabineteer
```

A healthy server returns a JSON object with `serverInfo` and a `capabilities` block. Any Python traceback here means a dependency or import problem — check the output for the specific error.

### Stale port file

If the server crashed without cleaning up, `/tmp/cabineteer.port` may contain a stale port number that confuses scripts reading it:

```bash
rm -f /tmp/cabineteer.port
```

### Apple Silicon / Rosetta

CadQuery's OCCT binaries are native ARM64 on Apple Silicon — do not run `uv` under Rosetta (x86_64 emulation). Check which architecture your terminal is using:

```bash
arch   # should print "arm64" on Apple Silicon, not "i386"
```

If it prints `i386`, open a new terminal that is not running under Rosetta, or run `arch -arm64 zsh` to get a native shell.

---

## Quick-reference

```bash
# Install
uv sync

# Smoke test
uv run cabineteer --help

# Register with Claude Code
claude mcp add cabineteer -- uv --directory $(pwd) run cabineteer
claude mcp list

# HTTP server
uv run cabineteer --http
PORT=$(cat /tmp/cabineteer.port) && curl -N "http://127.0.0.1:${PORT}/sse"

# Lite mode (no CadQuery)
uv run --no-group full cabineteer

# Manual stdio test
echo '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}},"id":1}' \
  | uv run cabineteer

# Claude Desktop logs
tail -f ~/Library/Logs/Claude/mcp-server-cabineteer.log
tail -f ~/Library/Logs/Claude/mcp.log
```
