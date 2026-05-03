# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-purpose CLI that converts a Google Keep Takeout export into a Standard Notes backup file (`version 004`). The entire converter is one file: `keep_to_standardnotes.py`. It uses only the Python standard library — there are no runtime dependencies.

## Dev environment

The toolchain is layered:

- **Nix flake** (`flake.nix`) provides `uv` and `python3.12` in a `devShell`. It sets `UV_PYTHON_DOWNLOADS=never` and `UV_PYTHON=…/python3.12`, so uv uses the Nix-provided interpreter rather than downloading its own.
- **direnv** (`.envrc` = `use flake`) auto-activates the dev shell on `cd`.
- **uv** manages the project venv (`.venv/`) and lockfile (`uv.lock`, committed).

Do **not** lift `UV_PYTHON_DOWNLOADS=never` without also bumping the Nix-pinned interpreter — uv must keep matching the Nix Python so the dev shell and the venv don't drift.

## Common commands

```bash
# Run the converter
uv run python keep_to_standardnotes.py <KEEP_DIR> <OUTPUT_FILE> [--include-trashed]

# Sync venv from pyproject.toml + uv.lock (e.g. after pulling)
uv sync

# Dependencies
uv add <pkg>          # runtime
uv add --dev <pkg>    # dev only
```

There are no tests, lint config, or build steps yet.

## Conversion architecture

The script targets the Standard Notes backup schema `version 004`:

```json
{ "version": "004", "items": [ /* Note items, then Tag items */ ] }
```

Two namespace constants govern `appData`:

- `SN_NS = "org.standardnotes.sn"` — Standard Notes' own keys (e.g. `client_updated_at`).
- `KEEP_NS = "de.tbckr.google_keep_import"` — everything from Keep that SN doesn't natively map. **The full original Keep note JSON is stashed under `appData[KEEP_NS]["original"]`**, so nothing is lost on import — plugins/scripts can recover it later.

### Mapping (full table in the script's module docstring)

| Keep field | Standard Notes target |
|---|---|
| `title` / first non-empty body line / `"Untitled"` | `content.title` (derived in `derive_title`, strips Markdown checkbox prefixes from the first-line fallback) |
| `textContent` | `content.text` |
| `listContent` | Markdown `- [ ]` / `- [x]` checkboxes (checked status preserved) |
| `isPinned` / `isArchived` / `isTrashed` | `content.pinned` / `.archived` / `.trashed` |
| `createdTimestampUsec` / `userEditedTimestampUsec` | `created_at` / `updated_at` — Keep uses µs-since-epoch, SN uses ISO 8601 UTC; see `usec_to_iso` |
| `labels[].name` | A separate `Tag` item per label, with `references[]` pointing to each note's UUID |
| `annotations[]` / `attachments[]` / `sharees[]` | Rendered as Markdown trailer sections **and** stored verbatim in `appData[KEEP_NS]` |

### Build pipeline (in `main`)

1. Iterate `*.json` in `input_dir`. Trashed notes are skipped unless `--include-trashed`.
2. `convert_note` builds one Note item per file and assigns a fresh UUID.
3. While converting, `tag_map[name]` accumulates one entry per label. Each entry keeps the earliest `created_at` and latest `updated_at` across referencing notes, plus a `references[]` list of `{content_type: "Note", uuid}`.
4. `build_tag_items` emits one Tag item per accumulated label (sorted by name); these are appended after the Note items.
5. The whole `{version, items}` blob is dumped to `output_file` as UTF-8 JSON (`ensure_ascii=False`).

## Intentional design choices (don't "clean up")

- **Trailer text duplicates `appData`.** Annotations/attachments/sharees are rendered into the note body in Markdown *and* preserved verbatim under `appData[KEEP_NS]`. The body copy is for human readability inside Standard Notes; the appData copy is for lossless round-tripping. Keep both.
- **Attachments are never embedded.** Standard Notes' import format has no binary slot. `render_attachments` writes only the file path + mimetype list; the actual files stay in the Takeout directory and must be reattached manually.
- **`from __future__ import annotations`** is in effect. The codebase uses PEP 604 (`int | None`) and PEP 585 (`dict[str, Any]`) syntax. The project pins Python 3.12 via `requires-python` and the flake.
