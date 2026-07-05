# Joplin Server MCP

A Model Context Protocol (MCP) server for interacting with a self-hosted Joplin Server. Works with any MCP-capable client — configuration examples are included for Claude Code, Continue.dev, and opencode.

## Features

- List, create, read, update, and delete notes
- Manage notebooks (folders)
- Manage tags
- Search notes (client-side)
- Read end-to-end encrypted (E2EE) accounts by setting `JOPLIN_E2EE_PASSWORD`
- Standard stdio MCP server — works with Claude Code, Continue.dev, opencode, and other MCP clients

## Prerequisites

- Python 3.10 or higher
- A Joplin Server account (self-hosted)
- An MCP-capable client (e.g. Claude Code, Continue.dev, opencode)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/mntxsn/joplin-server-mcp.git
cd joplin-server-mcp
```

### 2. Create virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure your MCP client

The server communicates over stdio and reads its settings from environment variables, so any MCP client can launch it. Pick the section for your client below. In all examples, replace paths and credentials with your actual values.

#### Claude Code

Create or edit `~/.claude/claude_code_config.json`:

```json
{
  "mcpServers": {
    "joplin": {
      "command": "/full/path/to/joplin-server-mcp/venv/bin/python",
      "args": ["/full/path/to/joplin-server-mcp/joplin_server_mcp.py"],
      "env": {
        "JOPLIN_SERVER_URL": "https://your-joplin-server.com",
        "JOPLIN_SERVER_EMAIL": "your-email@example.com",
        "JOPLIN_SERVER_PASSWORD": "your-password"
      }
    }
  }
}
```

#### Continue.dev

Add the server to `~/.continue/config.yaml` (or as a block file under `~/.continue/mcpServers/`):

```yaml
mcpServers:
  - name: joplin
    command: /full/path/to/joplin-server-mcp/venv/bin/python
    args:
      - /full/path/to/joplin-server-mcp/joplin_server_mcp.py
    env:
      JOPLIN_SERVER_URL: https://your-joplin-server.com
      JOPLIN_SERVER_EMAIL: your-email@example.com
      JOPLIN_SERVER_PASSWORD: your-password
```

**Note:** Continue.dev only exposes MCP tools in **Agent mode** — they are not available in the regular Chat or Edit modes.

#### opencode

Add the server to `opencode.json` in your project (or globally in `~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "joplin": {
      "type": "local",
      "command": [
        "/full/path/to/joplin-server-mcp/venv/bin/python",
        "/full/path/to/joplin-server-mcp/joplin_server_mcp.py"
      ],
      "environment": {
        "JOPLIN_SERVER_URL": "https://your-joplin-server.com",
        "JOPLIN_SERVER_EMAIL": "your-email@example.com",
        "JOPLIN_SERVER_PASSWORD": "your-password",
        "JOPLIN_E2EE_PASSWORD": "your-encryption-master-password"
      },
      "enabled": true
    }
  }
}
```

**Tip:** Instead of putting credentials in every client config, you can set the three environment variables once in your shell profile and omit the `env`/`environment` blocks — the server reads them from its environment either way.

**End-to-end encryption:** If your Joplin account uses E2EE, also set `JOPLIN_E2EE_PASSWORD` to your encryption master password (this is separate from your login password). The server then transparently decrypts notes, notebooks, and tags for reading. Without it, read tools return a clear "this data is end-to-end encrypted" error. Note: writes are not re-encrypted — a real Joplin client re-encrypts newly created/updated notes on its next sync.

**Note on confirmations:** The tools declare MCP annotations (`readOnlyHint`, `destructiveHint`), but not every client honors them. Clients other than Claude Code may prompt differently — or not at all — before destructive operations like `joplin_delete_note` or `joplin_delete_folder`.

#### Path examples by OS:

**Linux:**
```json
"command": "/home/username/joplin-server-mcp/venv/bin/python",
"args": ["/home/username/joplin-server-mcp/joplin_server_mcp.py"]
```

**macOS:**
```json
"command": "/Users/username/joplin-server-mcp/venv/bin/python",
"args": ["/Users/username/joplin-server-mcp/joplin_server_mcp.py"]
```

**Windows:**
```json
"command": "C:\\Users\\username\\joplin-server-mcp\\venv\\Scripts\\python.exe",
"args": ["C:\\Users\\username\\joplin-server-mcp\\joplin_server_mcp.py"]
```

### 4. Restart your client

Close and reopen your MCP client for the server to load.

## Usage

Once configured, you can ask the assistant to interact with your Joplin notes:

| Action | Example Prompt |
|--------|----------------|
| List notebooks | "List my Joplin notebooks" |
| List notes | "Show notes in my Work notebook" |
| Search notes | "Search Joplin for meeting notes" |
| Read a note | "Read the note titled Project Plan" |
| Create a note | "Create a note called TODO in my Personal notebook" |
| Update a note | "Add a section to my TODO note" |
| Delete a note | "Delete the note called Old Draft" |

## Available Tools

| Tool | Description |
|------|-------------|
| `joplin_ping` | Test connection to Joplin Server |
| `joplin_list_notes` | List notes, optionally filtered by notebook (`limit`, default 100) |
| `joplin_list_folders` | List all notebooks |
| `joplin_list_tags` | List all tags |
| `joplin_get_note` | Read a specific note |
| `joplin_create_note` | Create a new note |
| `joplin_update_note` | Update an existing note |
| `joplin_delete_note` | Delete a note |
| `joplin_search_notes` | Search notes by title/body (`limit`, default 100) |
| `joplin_create_folder` | Create a new notebook |
| `joplin_delete_folder` | Delete a notebook |
| `joplin_create_tag` | Create a new tag |
| `joplin_add_tag_to_note` | Add a tag to a note |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `JOPLIN_SERVER_URL` | Your Joplin Server URL | `http://localhost:22300` |
| `JOPLIN_SERVER_EMAIL` | Your Joplin account email | (required) |
| `JOPLIN_SERVER_PASSWORD` | Your Joplin account password | (required) |

## Troubleshooting

### "Cannot connect to Joplin Server"
- Verify the server URL is correct
- Check your internet connection
- Ensure the server is running

### "Authentication failed"
- Double-check your email and password
- Ensure you have an active account on the Joplin Server

### "MCP not loading in Claude Code"
- Verify all paths in the config are absolute (not relative)
- Check that the Python executable exists
- Ensure the script file exists at the specified location

### "Module not found" errors
- Activate the virtual environment
- Reinstall dependencies: `pip install -r requirements.txt`

## Technical Notes

This MCP uses the [joppy](https://github.com/marph91/joppy) library to interact with Joplin Server's API. Some patches are applied to handle:

- Newer Joplin Server fields not in joppy
- NaN timestamp values
- Boolean serialization (Joplin expects 0/1, not False/True)

Because these patches touch joppy internals, the joppy version is pinned in `requirements.txt` (currently `1.0.4`). Re-check the patches before bumping it.

Other implementation details:

- The Joplin sync lock is only acquired for write operations; reads run without it, so they don't collide with syncing Joplin clients. If another client holds the lock during a write, the tool returns a clear "try again shortly" error.
- Note listings and searches use a short-lived (30 s) in-memory cache of all notes, invalidated by any write, to avoid re-downloading every note body on each call.

## Security

- Your password is stored in plain text in the MCP client config. Ensure the file has appropriate permissions (e.g. `chmod 600`) and never commit it to version control. Alternatively, set the environment variables in your shell profile instead of the client config.
- Use `https://` for remote servers — with plain HTTP, credentials and note contents travel unencrypted. The server prints a startup warning if `JOPLIN_SERVER_URL` uses HTTP to a non-local host.
- Error messages returned by the tools are scrubbed so the password can never leak into tool output.

## License

MIT License

## Author

Erick Toussaint (@erickt23)
