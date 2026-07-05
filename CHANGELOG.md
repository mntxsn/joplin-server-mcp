# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- End-to-end encryption (E2EE) support for reading. When `JOPLIN_E2EE_PASSWORD`
  is set, encrypted items are transparently decrypted before use, so all read
  tools work against encrypted accounts. Implements Joplin's modern AES-256-GCM
  methods (KeyV1/FileV1/StringV1) in `joplin_crypto.py`. Clear errors are
  returned when the password is missing or wrong.

### Fixed

- Read tools (`joplin_get_note`, `joplin_list_notes`, `joplin_search_notes`,
  `joplin_list_folders`, `joplin_list_tags`) now acquire the sync lock again.
  joppy's `ServerApi` requires an active lock for every request, so the 0.2.0
  change that dropped the lock from reads made them always fail with a
  misleading "another client is syncing" `LockError`.

### Known limitations

- Writing to E2EE accounts is not encrypted: created/updated notes are stored
  unencrypted (a real Joplin client re-encrypts them on next sync). Read-only
  use is unaffected.

## [0.2.0] - 2026-07-05

### Added

- `limit` parameter (default 100, max 1000) for `joplin_list_notes` and
  `joplin_search_notes`; responses report totals and note when results are
  truncated.
- Short-lived (30 s) in-memory cache for `get_all_notes()`, used by note
  listing and search, invalidated by any write that can change notes.
- Startup warning when `JOPLIN_SERVER_URL` uses plain HTTP to a non-local
  host (credentials would travel unencrypted).
- Friendly error message when the Joplin sync lock is held by another
  client (`LockError`), instead of a raw exception string.
- README sections for connecting the server to Continue.dev and opencode.
- This changelog.

### Changed

- Read-only operations (`joplin_get_note`, `joplin_list_notes`,
  `joplin_search_notes`, `joplin_list_folders`, `joplin_list_tags`) no
  longer acquire the Joplin sync lock — faster and no collisions with
  syncing Joplin clients. Writes still take the lock.
- Note, notebook, and tag titles are sanitized: embedded newlines are
  collapsed to spaces, since Joplin's line-based raw item format would
  otherwise be corrupted.
- Error messages in tool output are scrubbed so the account password can
  never leak into responses.
- `joplin_ping` no longer includes the account email in its output.
- The patched `NoteData.serialize` no longer mutates `markup_language` and
  `source_application` on the object; only the `id` assignment remains,
  which joppy's `add_note` contract requires.
- The four dataclass `__init__` monkey-patches are generated in a loop
  instead of duplicated; `uuid`, `dataclasses.fields`, and `requests` are
  now module-level imports.
- `requirements.txt`: joppy pinned to `1.0.4` (the monkey-patches depend on
  its internals and were verified against this version); `requests` added
  as an explicit dependency.
- README generalized from Claude Code-only to any MCP client.

### Fixed

- `joplin_ping` now distinguishes connectivity from authentication: a login
  failure is reported as `connected: true, authenticated: false` instead of
  being mislabeled as a connection failure.
- Removed unused imports (`List`, `Any`); `LockError` is now actually
  handled instead of merely imported.

## [0.1.0] - 2026-01-14

### Added

- Initial release: stdio MCP server for self-hosted Joplin Server using
  [joppy](https://github.com/marph91/joppy).
- Tools for notes (create, get, update, delete, list, search), notebooks
  (create, list, delete), tags (create, list, add to note), and a
  connection check (`joplin_ping`).
- Markdown and JSON response formats; Pydantic input validation.
- joppy monkey-patches for newer Joplin Server fields, NaN timestamps, and
  0/1 boolean serialization.
