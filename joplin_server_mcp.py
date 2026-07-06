"""
Joplin Server MCP - MCP server for Joplin Server (self-hosted sync server).

This server uses the joppy library to interact with Joplin Server's API.
Unlike the Desktop Web Clipper API, this connects directly to Joplin Server.

Requirements:
    - Joplin Server running (e.g., via Docker)
    - Valid user credentials (email/password)
    - joppy library installed

Environment variables:
    - JOPLIN_SERVER_URL: Server URL (default: http://localhost:22300)
    - JOPLIN_SERVER_EMAIL: User email
    - JOPLIN_SERVER_PASSWORD: User password
    - JOPLIN_E2EE_PASSWORD: Encryption master password (only needed if the
      account uses end-to-end encryption)
"""

import json
import os
import sys
import time
import uuid
from dataclasses import fields as dc_fields
from enum import Enum
from typing import Optional
from datetime import datetime
from contextlib import contextmanager
from urllib.parse import urlparse

import requests
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

from joppy.server_api import ServerApi, LockError
from joppy import data_types as dt

# Monkey-patch joppy to handle newer Joplin Server fields, NaN timestamps,
# and fix boolean serialization (Joplin expects 0/1, not False/True).
# Written against joppy 1.0.4 internals - keep the version pinned in
# requirements.txt in sync when updating.

def _filter_kwargs(dataclass_type, kwargs):
    """Filter kwargs to only include known fields and fix NaN timestamps."""
    known_fields = {f.name for f in dc_fields(dataclass_type)}
    filtered = {}
    for k, v in kwargs.items():
        if k not in known_fields:
            continue
        # Handle NaN timestamp values
        if v == "NaN" or v == "nan":
            v = None
        filtered[k] = v
    return filtered


def _make_patched_init(dataclass_type, original_init):
    def _patched_init(self, **kwargs):
        original_init(self, **_filter_kwargs(dataclass_type, kwargs))
    return _patched_init


for _dataclass in (dt.ResourceData, dt.NoteData, dt.NotebookData, dt.TagData):
    _dataclass.__init__ = _make_patched_init(_dataclass, _dataclass.__init__)


# Fix NoteData serialization to output 0/1 for boolean fields instead of False/True
def _patched_note_serialize(self):
    """Patched serialize that converts booleans to 0/1."""
    # Boolean fields that need 0/1 instead of False/True
    bool_fields = {'is_todo', 'is_conflict', 'encryption_applied', 'is_shared'}

    lines = ["" if self.title is None else self.title, ""]
    if self.body is not None:
        lines.extend([self.body, ""])

    for field_ in dc_fields(self):
        if field_.name == "id":
            # joppy's add_note reads note.id after serialize() (and asserts
            # it is set), so a generated id must be stored on the object.
            if self.id is None:
                self.id = uuid.uuid4().hex
            lines.append(f"{field_.name}: {self.id}")
        elif field_.name == "markup_language":
            markup = (
                self.markup_language
                if self.markup_language is not None
                else dt.MarkupLanguage.MARKDOWN
            )
            lines.append(f"{field_.name}: {int(markup)}")
        elif field_.name == "source_application":
            lines.append(f"{field_.name}: {self.source_application or 'joppy'}")
        elif field_.name in ("title", "body"):
            pass  # handled before
        elif field_.name == "type_":
            lines.append(f"{field_.name}: {int(dt.ItemType.NOTE)}")
        elif field_.name == "updated_time":
            value_raw = getattr(self, field_.name)
            value = "" if value_raw is None else value_raw
            lines.append(f"{field_.name}: {value}")
        elif field_.name in bool_fields:
            value_raw = getattr(self, field_.name)
            if value_raw is not None:
                # Convert bool to 0/1
                lines.append(f"{field_.name}: {1 if value_raw else 0}")
        else:
            value_raw = getattr(self, field_.name)
            if value_raw is not None:
                lines.append(f"{field_.name}: {value_raw}")
    return "\n".join(lines)

dt.NoteData.serialize = _patched_note_serialize

# =============================================================================
# Configuration
# =============================================================================

JOPLIN_SERVER_URL = os.getenv("JOPLIN_SERVER_URL", "http://localhost:22300")
JOPLIN_SERVER_EMAIL = os.getenv("JOPLIN_SERVER_EMAIL", "")
JOPLIN_SERVER_PASSWORD = os.getenv("JOPLIN_SERVER_PASSWORD", "")
# Master password for end-to-end encrypted accounts. Empty = E2EE not in use.
JOPLIN_E2EE_PASSWORD = os.getenv("JOPLIN_E2EE_PASSWORD", "")

_LOCAL_HOSTNAMES = ("localhost", "127.0.0.1", "::1")

# Global API instance (lazy initialized)
_api: Optional[ServerApi] = None

# Cache for get_all_notes() to avoid re-downloading every note (including
# bodies) on each list/search call. Invalidated on any write that can
# change notes.
_NOTES_CACHE_TTL_SECONDS = 30
_notes_cache = {"timestamp": 0.0, "notes": None}

# =============================================================================
# Initialize MCP Server
# =============================================================================

mcp = FastMCP("joplin_server_mcp")

# =============================================================================
# API Connection Helper
# =============================================================================

def _get_api() -> ServerApi:
    """Get or create the Joplin Server API connection."""
    global _api
    if _api is None:
        if not JOPLIN_SERVER_EMAIL or not JOPLIN_SERVER_PASSWORD:
            raise ValueError(
                "JOPLIN_SERVER_EMAIL and JOPLIN_SERVER_PASSWORD must be set. "
                "These are your Joplin Server login credentials."
            )
        _api = ServerApi(
            user=JOPLIN_SERVER_EMAIL,
            password=JOPLIN_SERVER_PASSWORD,
            url=JOPLIN_SERVER_URL
        )
    return _api


# =============================================================================
# End-to-end encryption (E2EE)
# =============================================================================
#
# joppy has no encryption support: its deserialize() raises "Encryption is not
# supported" on encrypted items, and its serialize() only ever emits plaintext.
# When JOPLIN_E2EE_PASSWORD is set we transparently decrypt items before joppy
# parses them (reads) and encrypt items before joppy uploads them (writes), so
# every tool works against E2EE accounts. Writes are only encrypted when the
# account actually has E2EE enabled - see _e2ee_active(). Crypto lives in
# joplin_crypto.

import joplin_crypto as _jc
from joppy import server_api as _joppy_server_api

# Decrypted master keys (id -> key), fetched and unwrapped once per process.
_master_key_store: Optional[dict] = None
# Cached sync target info (info.json): E2EE flag + active master key id.
_sync_info: Optional[dict] = None


class E2EEError(Exception):
    """Encrypted data was found but could not be de/encrypted (config problem)."""


def _get_sync_info() -> dict:
    """Fetch and cache the sync target's info.json (lock-exempt in joppy)."""
    global _sync_info
    if _sync_info is None:
        _sync_info = _get_api().get(
            "/api/items/root:/info.json:/content"
        ).json()
    return _sync_info


def _e2ee_active() -> bool:
    """Whether the account has E2EE enabled with an active master key."""
    info = _get_sync_info()
    return bool(info.get("e2ee", {}).get("value")) and bool(
        info.get("activeMasterKeyId", {}).get("value")
    )


def _active_master_key_id() -> Optional[str]:
    return _get_sync_info().get("activeMasterKeyId", {}).get("value")


def _get_master_key_store() -> dict:
    """Fetch the server's master keys and decrypt them with the master password.

    Cached after the first successful build. Raises E2EEError (surfaced to the
    user) when the password is missing or wrong.
    """
    global _master_key_store
    if _master_key_store is not None:
        return _master_key_store
    if not JOPLIN_E2EE_PASSWORD:
        raise E2EEError(
            "This data is end-to-end encrypted. Set JOPLIN_E2EE_PASSWORD to "
            "your Joplin encryption master password to read or write it."
        )
    store = _jc.build_master_key_store(
        _get_sync_info().get("masterKeys", []), JOPLIN_E2EE_PASSWORD
    )
    if not store:
        raise E2EEError(
            "Could not decrypt any master key - is JOPLIN_E2EE_PASSWORD correct?"
        )
    _master_key_store = store
    return store


def _extract_cipher_text(body: str) -> Optional[str]:
    """Pull the encryption_cipher_text value out of a serialized item body."""
    for line in body.split("\n"):
        if line.startswith("encryption_cipher_text: "):
            return line[len("encryption_cipher_text: "):] or None
    return None


_original_deserialize = _joppy_server_api.deserialize


def _patched_deserialize(body):
    """Decrypt E2EE items before handing them to joppy's deserialize.

    An encrypted item's cleartext body carries `encryption_applied: 1` and an
    `encryption_cipher_text: JED01...` blob holding the real serialized item. We
    decrypt the blob and let joppy parse the plaintext. Items that can't be
    turned into a known type (e.g. revision diffs) return None, mirroring
    joppy's own handling of revisions, so a single odd item never aborts a list.
    """
    if body and "encryption_applied: 1" in body:
        cipher = _extract_cipher_text(body)
        if cipher and cipher.startswith("JED"):
            store = _get_master_key_store()  # E2EEError bubbles up on misconfig
            try:
                body = _jc.decrypt_cipher_text(cipher, store)
            except _jc.JoplinCryptoError:
                # One undecryptable item shouldn't break a whole listing.
                return None
    try:
        return _original_deserialize(body)
    except (KeyError, ValueError):
        return None


_joppy_server_api.deserialize = _patched_deserialize


def _maybe_encrypt_item(plaintext: str) -> str:
    """Encrypt a serialized item before upload, if the account uses E2EE.

    When E2EE is not active the plaintext is returned unchanged (normal write).
    When it is active, the item is encrypted with the active master key so we
    never write plaintext into an encrypted store; a missing/wrong password
    raises E2EEError instead of silently leaking plaintext.
    """
    if not _e2ee_active():
        return plaintext
    store = _get_master_key_store()  # E2EEError on missing/wrong password
    mkid = _active_master_key_id()
    key = store.get(mkid)
    if key is None:
        raise E2EEError(
            f"Active master key {mkid} could not be decrypted; check "
            f"JOPLIN_E2EE_PASSWORD."
        )
    return _jc.build_encrypted_item(plaintext, mkid, key)


def _wrap_serialize_with_e2ee(dataclass_type) -> None:
    """Make a dataclass's serialize() encrypt its output when E2EE is active."""
    original_serialize = dataclass_type.serialize

    def _serialize(self):
        return _maybe_encrypt_item(original_serialize(self))

    dataclass_type.serialize = _serialize


# Wrap the writable text item types. NoteData.serialize is already patched
# above; wrapping it here layers encryption on top of that plaintext output.
# Resources are intentionally excluded (no resource-creation tool, and file
# content uses a different encryption method).
for _dataclass in (dt.NoteData, dt.NotebookData, dt.TagData, dt.NoteTagData):
    _wrap_serialize_with_e2ee(_dataclass)


@contextmanager
def _with_lock():
    """Context manager to acquire the Joplin Server sync lock.

    joppy's ServerApi requires an active sync lock for *every* request except
    login/lock/info (see server_api.py _request). That includes read-only
    operations like get_all_notebooks, so both read and write tools must run
    inside this context. The lock is released on exit, including on error.
    """
    api = _get_api()
    with api.sync_lock():
        yield api


def _sanitize_error_message(e: Exception) -> str:
    """Return an error message with credentials scrubbed."""
    message = str(e)
    if JOPLIN_SERVER_PASSWORD:
        message = message.replace(JOPLIN_SERVER_PASSWORD, "***")
    return message


def _tool_error(e: Exception) -> str:
    """Format an exception as a JSON error response for tool output."""
    if isinstance(e, LockError):
        return json.dumps({
            "error": "Could not acquire the Joplin Server sync lock. Another "
                     "client is probably syncing right now - try again shortly."
        }, indent=2)
    return json.dumps({"error": _sanitize_error_message(e)}, indent=2)


def _sanitize_title(title: str) -> str:
    """Collapse newlines in titles.

    Joplin's raw item format is line-based with the title on the first line;
    embedded newlines would corrupt the serialized item.
    """
    return " ".join(title.splitlines())


def _get_all_notes_cached(api) -> list:
    """Return all notes, using a short-lived cache to avoid full re-downloads."""
    now = time.monotonic()
    if (
        _notes_cache["notes"] is None
        or now - _notes_cache["timestamp"] > _NOTES_CACHE_TTL_SECONDS
    ):
        _notes_cache["notes"] = api.get_all_notes()
        _notes_cache["timestamp"] = now
    return _notes_cache["notes"]


def _invalidate_notes_cache() -> None:
    _notes_cache["notes"] = None


def _format_timestamp(dt_obj: Optional[datetime]) -> str:
    """Format datetime to readable string."""
    if not dt_obj:
        return "N/A"
    return dt_obj.strftime("%Y-%m-%d %H:%M:%S")


def _note_to_dict(note: dt.NoteData) -> dict:
    """Convert NoteData to dictionary."""
    return {
        "id": note.id,
        "parent_id": note.parent_id,
        "title": note.title,
        "body": note.body,
        "created_time": _format_timestamp(note.created_time),
        "updated_time": _format_timestamp(note.updated_time),
        "is_todo": note.is_todo,
        "todo_due": _format_timestamp(note.todo_due),
        "todo_completed": _format_timestamp(note.todo_completed),
    }


def _notebook_to_dict(notebook: dt.NotebookData) -> dict:
    """Convert NotebookData to dictionary."""
    return {
        "id": notebook.id,
        "title": notebook.title,
        "parent_id": notebook.parent_id,
        "created_time": _format_timestamp(notebook.created_time),
        "updated_time": _format_timestamp(notebook.updated_time),
    }


def _tag_to_dict(tag: dt.TagData) -> dict:
    """Convert TagData to dictionary."""
    return {
        "id": tag.id,
        "title": tag.title,
        "created_time": _format_timestamp(tag.created_time),
        "updated_time": _format_timestamp(tag.updated_time),
    }


# =============================================================================
# Enums and Input Models
# =============================================================================

class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class CreateNoteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="The note title", min_length=1)
    body: str = Field(default="", description="The note body in Markdown format")
    parent_id: str = Field(..., description="ID of the notebook to create the note in (required)")
    is_todo: Optional[bool] = Field(default=False, description="Whether this note is a todo item")


class UpdateNoteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to update")
    title: Optional[str] = Field(default=None, description="New title for the note")
    body: Optional[str] = Field(default=None, description="New body content in Markdown")
    parent_id: Optional[str] = Field(default=None, description="Move note to this notebook ID")


class GetNoteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to retrieve")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class DeleteNoteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    note_id: str = Field(..., description="The ID of the note to delete")


class ListNotesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_id: Optional[str] = Field(default=None, description="Filter notes by notebook ID")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of notes to return")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class SearchNotesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query (searches in title and body)", min_length=1)
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of results to return")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class CreateFolderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="The notebook title", min_length=1)
    parent_id: Optional[str] = Field(default=None, description="Parent notebook ID")


class ListFoldersInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class DeleteFolderInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    folder_id: str = Field(..., description="The ID of the notebook to delete")


class CreateTagInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="The tag name", min_length=1)


class ListTagsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class AddTagToNoteInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    tag_id: str = Field(..., description="The tag ID")
    note_id: str = Field(..., description="The note ID to tag")


# =============================================================================
# Note Tools
# =============================================================================

@mcp.tool(
    name="joplin_create_note",
    annotations={
        "title": "Create a Joplin Note",
        "readOnlyHint": False,
        "destructiveHint": False,
    }
)
def joplin_create_note(params: CreateNoteInput) -> str:
    """Create a new note in Joplin Server.

    Args:
        params: CreateNoteInput with title, body, parent_id (required), is_todo

    Returns:
        JSON with created note ID and details
    """
    try:
        title = _sanitize_title(params.title)
        with _with_lock() as api:
            # Convert boolean to int for Joplin compatibility
            note_id = api.add_note(
                parent_id=params.parent_id,
                title=title,
                body=params.body,
                is_todo=1 if params.is_todo else 0,
            )
            _invalidate_notes_cache()
            return json.dumps({
                "success": True,
                "note_id": note_id,
                "title": title,
                "message": f"Note '{title}' created successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_get_note",
    annotations={
        "title": "Get a Joplin Note",
        "readOnlyHint": True,
    }
)
def joplin_get_note(params: GetNoteInput) -> str:
    """Retrieve a specific note by ID.

    Args:
        params: GetNoteInput with note_id and response_format

    Returns:
        Note content in requested format
    """
    try:
        with _with_lock() as api:
            note = api.get_note(params.note_id)

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(_note_to_dict(note), indent=2)

        # Markdown format
        lines = [
            f"# {note.title or 'Untitled'}",
            "",
            f"**ID:** `{note.id}`",
            f"**Created:** {_format_timestamp(note.created_time)}",
            f"**Updated:** {_format_timestamp(note.updated_time)}",
        ]

        if note.is_todo:
            status = "Completed" if note.todo_completed else "Pending"
            lines.append(f"**Todo Status:** {status}")

        if note.parent_id:
            lines.append(f"**Notebook ID:** `{note.parent_id}`")

        lines.extend(["", "---", "", note.body or ""])
        return "\n".join(lines)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_update_note",
    annotations={
        "title": "Update a Joplin Note",
        "readOnlyHint": False,
    }
)
def joplin_update_note(params: UpdateNoteInput) -> str:
    """Update an existing note.

    Args:
        params: UpdateNoteInput with note_id and fields to update

    Returns:
        JSON confirmation or error
    """
    try:
        update_data = {}
        if params.title is not None:
            update_data["title"] = _sanitize_title(params.title)
        if params.body is not None:
            update_data["body"] = params.body
        if params.parent_id is not None:
            update_data["parent_id"] = params.parent_id

        if not update_data:
            return json.dumps({"error": "No update fields specified"}, indent=2)

        with _with_lock() as api:
            api.modify_note(params.note_id, **update_data)
            _invalidate_notes_cache()
            return json.dumps({
                "success": True,
                "note_id": params.note_id,
                "message": "Note updated successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_delete_note",
    annotations={
        "title": "Delete a Joplin Note",
        "readOnlyHint": False,
        "destructiveHint": True,
    }
)
def joplin_delete_note(params: DeleteNoteInput) -> str:
    """Delete a note from Joplin Server.

    Args:
        params: DeleteNoteInput with note_id

    Returns:
        JSON confirmation or error
    """
    try:
        with _with_lock() as api:
            api.delete_note(params.note_id)
            _invalidate_notes_cache()
            return json.dumps({
                "success": True,
                "message": "Note deleted successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_list_notes",
    annotations={
        "title": "List Joplin Notes",
        "readOnlyHint": True,
    }
)
def joplin_list_notes(params: ListNotesInput) -> str:
    """List notes, optionally filtered by notebook.

    Args:
        params: ListNotesInput with optional folder_id, limit and response_format

    Returns:
        List of notes in requested format
    """
    try:
        with _with_lock() as api:
            all_notes = _get_all_notes_cached(api)

        # Filter by folder if specified
        if params.folder_id:
            all_notes = [n for n in all_notes if n.parent_id == params.folder_id]

        total = len(all_notes)
        notes = all_notes[:params.limit]

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({
                "items": [_note_to_dict(n) for n in notes],
                "count": len(notes),
                "total": total,
            }, indent=2)

        # Markdown format
        lines = ["# Notes", ""]

        if not notes:
            lines.append("*No notes found*")
        else:
            for note in notes:
                prefix = ""
                if note.is_todo:
                    prefix = "[x] " if note.todo_completed else "[ ] "
                lines.append(f"- {prefix}**{note.title or 'Untitled'}**")
                lines.append(f"  - ID: `{note.id}`")
                lines.append(f"  - Updated: {_format_timestamp(note.updated_time)}")

        if total > len(notes):
            lines.append(f"\n*Showing {len(notes)} of {total} notes (increase `limit` for more)*")
        else:
            lines.append(f"\n*Total: {total} notes*")
        return "\n".join(lines)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_search_notes",
    annotations={
        "title": "Search Joplin Notes",
        "readOnlyHint": True,
    }
)
def joplin_search_notes(params: SearchNotesInput) -> str:
    """Search notes by title or body content.

    Note: This performs client-side filtering as Joplin Server doesn't have
    a native search API.

    Args:
        params: SearchNotesInput with query, limit and response_format

    Returns:
        Matching notes in requested format
    """
    try:
        with _with_lock() as api:
            all_notes = _get_all_notes_cached(api)
        query_lower = params.query.lower()

        # Client-side search
        matches = []
        for note in all_notes:
            title_match = note.title and query_lower in note.title.lower()
            body_match = note.body and query_lower in note.body.lower()
            if title_match or body_match:
                matches.append(note)

        total = len(matches)
        matches = matches[:params.limit]

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({
                "query": params.query,
                "items": [_note_to_dict(n) for n in matches],
                "count": len(matches),
                "total": total,
            }, indent=2)

        # Markdown format
        lines = [f"# Search Results for '{params.query}'", ""]

        if not matches:
            lines.append("*No results found*")
        else:
            for note in matches:
                lines.append(f"- **{note.title or 'Untitled'}**")
                lines.append(f"  - ID: `{note.id}`")

        if total > len(matches):
            lines.append(f"\n*Showing {len(matches)} of {total} matching notes (increase `limit` for more)*")
        else:
            lines.append(f"\n*Found {total} matching notes*")
        return "\n".join(lines)
    except Exception as e:
        return _tool_error(e)


# =============================================================================
# Folder/Notebook Tools
# =============================================================================

@mcp.tool(
    name="joplin_create_folder",
    annotations={
        "title": "Create a Joplin Notebook",
        "readOnlyHint": False,
    }
)
def joplin_create_folder(params: CreateFolderInput) -> str:
    """Create a new notebook.

    Args:
        params: CreateFolderInput with title and optional parent_id

    Returns:
        JSON with created notebook details
    """
    try:
        title = _sanitize_title(params.title)
        with _with_lock() as api:
            kwargs = {"title": title}
            if params.parent_id:
                kwargs["parent_id"] = params.parent_id

            folder_id = api.add_notebook(**kwargs)
            return json.dumps({
                "success": True,
                "folder_id": folder_id,
                "title": title,
                "message": f"Notebook '{title}' created successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_list_folders",
    annotations={
        "title": "List Joplin Notebooks",
        "readOnlyHint": True,
    }
)
def joplin_list_folders(params: ListFoldersInput) -> str:
    """List all notebooks.

    Args:
        params: ListFoldersInput with response_format

    Returns:
        List of notebooks in requested format
    """
    try:
        with _with_lock() as api:
            all_folders = api.get_all_notebooks()

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({
                "folders": [_notebook_to_dict(f) for f in all_folders]
            }, indent=2)

        # Markdown format
        lines = ["# Notebooks", ""]

        if not all_folders:
            lines.append("*No notebooks found*")
        else:
            for folder in all_folders:
                lines.append(f"- **{folder.title or 'Untitled'}**")
                lines.append(f"  - ID: `{folder.id}`")

        return "\n".join(lines)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_delete_folder",
    annotations={
        "title": "Delete a Joplin Notebook",
        "readOnlyHint": False,
        "destructiveHint": True,
    }
)
def joplin_delete_folder(params: DeleteFolderInput) -> str:
    """Delete a notebook.

    Args:
        params: DeleteFolderInput with folder_id

    Returns:
        JSON confirmation or error
    """
    try:
        with _with_lock() as api:
            api.delete_notebook(params.folder_id)
            _invalidate_notes_cache()
            return json.dumps({
                "success": True,
                "message": "Notebook deleted successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


# =============================================================================
# Tag Tools
# =============================================================================

@mcp.tool(
    name="joplin_create_tag",
    annotations={
        "title": "Create a Joplin Tag",
        "readOnlyHint": False,
    }
)
def joplin_create_tag(params: CreateTagInput) -> str:
    """Create a new tag.

    Args:
        params: CreateTagInput with title

    Returns:
        JSON with created tag details
    """
    try:
        title = _sanitize_title(params.title)
        with _with_lock() as api:
            tag_id = api.add_tag(title=title)
            return json.dumps({
                "success": True,
                "tag_id": tag_id,
                "title": title,
                "message": f"Tag '{title}' created successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_list_tags",
    annotations={
        "title": "List Joplin Tags",
        "readOnlyHint": True,
    }
)
def joplin_list_tags(params: ListTagsInput) -> str:
    """List all tags.

    Args:
        params: ListTagsInput with response_format

    Returns:
        List of tags in requested format
    """
    try:
        with _with_lock() as api:
            all_tags = api.get_all_tags()

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({
                "tags": [_tag_to_dict(t) for t in all_tags]
            }, indent=2)

        # Markdown format
        lines = ["# Tags", ""]

        if not all_tags:
            lines.append("*No tags found*")
        else:
            for tag in all_tags:
                lines.append(f"- **{tag.title or 'Untitled'}** (`{tag.id}`)")

        return "\n".join(lines)
    except Exception as e:
        return _tool_error(e)


@mcp.tool(
    name="joplin_add_tag_to_note",
    annotations={
        "title": "Add Tag to Note",
        "readOnlyHint": False,
    }
)
def joplin_add_tag_to_note(params: AddTagToNoteInput) -> str:
    """Add a tag to a note.

    Args:
        params: AddTagToNoteInput with tag_id and note_id

    Returns:
        JSON confirmation or error
    """
    try:
        with _with_lock() as api:
            api.add_tag_to_note(tag_id=params.tag_id, note_id=params.note_id)
            return json.dumps({
                "success": True,
                "message": "Tag added to note successfully"
            }, indent=2)
    except Exception as e:
        return _tool_error(e)


# =============================================================================
# Utility Tools
# =============================================================================

@mcp.tool(
    name="joplin_ping",
    annotations={
        "title": "Check Joplin Server Connection",
        "readOnlyHint": True,
    }
)
def joplin_ping() -> str:
    """Check if Joplin Server is accessible.

    Returns:
        JSON with connection status
    """
    try:
        # Direct ping without going through joppy (avoids lock requirement)
        response = requests.get(f"{JOPLIN_SERVER_URL}/api/ping", timeout=10)
        if response.status_code != 200:
            return json.dumps({
                "connected": False,
                "authenticated": False,
                "error": f"Server responded with status {response.status_code}"
            }, indent=2)
    except Exception as e:
        return json.dumps({
            "connected": False,
            "authenticated": False,
            "error": str(e)
        }, indent=2)

    # Server is reachable - verify authentication separately so login
    # failures aren't reported as connection failures.
    try:
        _get_api()
    except Exception as e:
        return json.dumps({
            "connected": True,
            "authenticated": False,
            "url": JOPLIN_SERVER_URL,
            "error": _sanitize_error_message(e)
        }, indent=2)

    return json.dumps({
        "connected": True,
        "authenticated": True,
        "url": JOPLIN_SERVER_URL,
        "message": "Joplin Server is accessible and authenticated"
    }, indent=2)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    if not JOPLIN_SERVER_EMAIL or not JOPLIN_SERVER_PASSWORD:
        print(
            "WARNING: JOPLIN_SERVER_EMAIL and JOPLIN_SERVER_PASSWORD not set.\n"
            "Set them with:\n"
            "  export JOPLIN_SERVER_EMAIL='your-email'\n"
            "  export JOPLIN_SERVER_PASSWORD='your-password'\n",
            file=sys.stderr
        )

    _parsed_url = urlparse(JOPLIN_SERVER_URL)
    if _parsed_url.scheme == "http" and _parsed_url.hostname not in _LOCAL_HOSTNAMES:
        print(
            f"WARNING: JOPLIN_SERVER_URL ({JOPLIN_SERVER_URL}) uses plain HTTP "
            "to a non-local host. Credentials and note contents will be sent "
            "unencrypted - use https:// for remote servers.\n",
            file=sys.stderr
        )

    mcp.run()
