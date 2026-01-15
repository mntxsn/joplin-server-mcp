# Joplin Server MCP for Claude Code

A Model Context Protocol (MCP) server that enables Claude Code to interact with self-hosted Joplin Server.

## Features

- List, create, read, update, and delete notes
- Manage notebooks (folders)
- Manage tags
- Search notes (client-side)
- Full integration with Claude Code

## Prerequisites

- Python 3.10 or higher
- A Joplin Server account (self-hosted)
- Claude Code installed

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/erickt23/joplin-server-mcp.git
cd joplin-server-mcp
```

### 2. Create virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Claude Code

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

**Important:** Replace paths and credentials with your actual values.

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

### 4. Restart Claude Code

Close and reopen Claude Code for the MCP to load.

## Usage

Once configured, you can ask Claude to interact with your Joplin notes:

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
| `joplin_list_notes` | List all notes or filter by notebook |
| `joplin_list_folders` | List all notebooks |
| `joplin_list_tags` | List all tags |
| `joplin_get_note` | Read a specific note |
| `joplin_create_note` | Create a new note |
| `joplin_update_note` | Update an existing note |
| `joplin_delete_note` | Delete a note |
| `joplin_search_notes` | Search notes by title/body |
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

## Security

- Your password is stored in plain text in the Claude Code config
- Ensure your config file has appropriate permissions: `chmod 600 ~/.claude/claude_code_config.json`
- Do not commit your config file to version control

## License

MIT License

## Author

Erick Toussaint (@erickt23)
