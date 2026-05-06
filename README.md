# google-keep-to-standardnotes

Convert a [Google Keep](https://keep.google.com) Takeout export into a
[Standard Notes](https://standardnotes.com) backup file (`version 004`) — as
losslessly as the Standard Notes import schema allows.

A single-file Python script (`keep_to_standardnotes.py`) using only the standard
library. No runtime dependencies.

## Features

- One Standard Notes `Note` item per Keep note, one `Tag` item per Keep label
  (with `references[]` pointing to the notes that carry the label).
- Pinned, archived and trashed flags are preserved.
- Keep checklists become Markdown checkboxes (`- [ ]` / `- [x]`) by default,
  or a real interactive Lexical `check` list under `--super`.
- Annotations (link previews), attachment file lists, and sharee lists are
  rendered into the note body **and** stored verbatim under `appData`, so
  nothing from the original export is lost.
- Microsecond Keep timestamps are converted to ISO 8601 UTC.
- The full original Keep JSON is parked under
  `appData["de.tbckr.google_keep_import"].original` — recoverable later via
  plugins or scripts.
- Optional `--super` flag emits Standard Notes Super (Lexical rich-text) notes
  instead of plaintext.

## Quick start

1. Request a [Google Takeout](https://takeout.google.com) export of Keep and
   unpack it. The interesting directory is `Takeout/Keep/`, which contains one
   `*.json` file per note.
2. Run the converter:

   ```bash
   python3 keep_to_standardnotes.py \
       ~/Takeout/Keep \
       "Standard Notes Backup and Import File.txt"
   ```

3. In Standard Notes, go to **Settings → Backups → Import Backup** and select
   the generated file.

## Usage

```
python3 keep_to_standardnotes.py <KEEP_DIR> <OUTPUT_FILE> [--include-trashed] [--super]
```

| Flag | Effect |
|---|---|
| `--include-trashed` | Also import notes that are trashed in Keep (skipped by default). |
| `--super` | Emit notes for the Standard Notes Super editor (Lexical JSON). Default is plaintext/Markdown. |

Output statistics (files read, notes converted, skipped, errors, tags created)
are printed to stderr.

## Mapping

| Keep field | Standard Notes target |
|---|---|
| `title` (or first non-empty body line, else `"Untitled"`) | `content.title` |
| `textContent` | `content.text` (Markdown by default; Lexical JSON under `--super`) |
| `listContent` | Markdown `- [ ]` / `- [x]` checkboxes (default), or a Lexical `check` list under `--super` |
| `isPinned` / `isArchived` / `isTrashed` | `content.pinned` / `.archived` / `.trashed` |
| `createdTimestampUsec` | `created_at` (ISO 8601 UTC) |
| `userEditedTimestampUsec` | `updated_at` + `appData.client_updated_at` |
| `labels[].name` | One `Tag` item per label, with `references[]` to the note UUIDs |
| `color` | `appData["de.tbckr.google_keep_import"].color` |
| `annotations[]` | Markdown link list in body **and** preserved in `appData` |
| `attachments[]` | Markdown file list in body **and** preserved in `appData` |
| `sharees[]` | Markdown list in body **and** preserved in `appData` |
| *the entire original JSON* | `appData["de.tbckr.google_keep_import"].original` |

## Plaintext vs. `--super`

- **Plaintext (default)**: `content.text` is plain Markdown. Stable, byte-for-
  byte reproducible output.
- **`--super`**: `content.text` is a JSON-stringified Lexical EditorState.
  Notes carry `noteType: "super"` and `editorIdentifier:
  "com.standardnotes.super-editor"`. Checklists become real interactive
  Lexical check lists, annotation URLs become real link nodes, and a
  `preview_plain` excerpt is set so the SN note list shows readable text
  instead of raw JSON.

The flag is *all-or-nothing* per run — there is no per-note mix of editors.

## What gets dropped

- **Attachments are not embedded.** Standard Notes' import format has no
  binary slot. The file paths and mimetypes are listed in the note body and
  preserved under `appData`, but the actual image/audio files stay in the
  Takeout directory and have to be re-attached manually.

## Development

The toolchain is layered:

- **Nix flake** (`flake.nix`) provides `uv` and `python3.12` in a `devShell`.
- **direnv** (`.envrc` = `use flake`) auto-activates the dev shell on `cd`.
- **uv** manages the project venv (`.venv/`) and the committed lockfile.

```bash
# Run via uv
uv run python keep_to_standardnotes.py <KEEP_DIR> <OUTPUT_FILE>

# Sync venv after pulling
uv sync
```

The script targets Python 3.12 (`from __future__ import annotations`,
PEP 604 / PEP 585 syntax).

## License

[MIT](LICENSE.md).
