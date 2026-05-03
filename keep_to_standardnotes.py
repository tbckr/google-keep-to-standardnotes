#!/usr/bin/env python3
"""
keep_to_standardnotes.py
========================

Converts a Google Keep Takeout into the Standard Notes backup format
(version 004) — as losslessly as the Standard Notes import schema allows.

Mapping
-------
title                                   -> content.title
                                           (fallback: first non-empty line of
                                            text, otherwise "Untitled")
textContent                             -> content.text
listContent                             -> content.text as Markdown checkboxes
                                           ("- [ ] " / "- [x] "), checked
                                           status preserved
isPinned / isArchived / isTrashed       -> content.pinned / .archived / .trashed
createdTimestampUsec                    -> created_at (ISO 8601, UTC)
userEditedTimestampUsec                 -> updated_at + appData.client_updated_at
labels[].name                           -> dedicated Tag item with references[]
                                           pointing at each note's UUID
color                                   -> appData (custom namespace)
annotations[]                           -> appended to body as a Markdown link
                                           list AND preserved fully in appData
attachments[]                           -> appended to body as a file list AND
                                           preserved fully in appData
sharees[]                               -> appended to body as a list AND
                                           preserved in appData
the entire original JSON                -> appData.<keep-namespace>.original

This keeps anything Standard Notes doesn't natively map intact under appData,
recoverable later via plugins or scripts.

Usage
-----
    python3 keep_to_standardnotes.py <KEEP_DIR> <OUTPUT_FILE> [--include-trashed]

Example:
    python3 keep_to_standardnotes.py \\
        ~/Takeout/Keep \\
        "Standard Notes Backup and Import File.txt"

The output file can then be imported into Standard Notes via
    Settings -> Backups -> Import Backup

Attachments (images/audio from Keep) are NOT embedded in the SN file —
Standard Notes' import format does not support binary data. The filenames
are appended to the note as a list so the original files can be matched
back manually from the Takeout directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Namespace for anything Standard Notes doesn't natively understand.
# Original Keep data is parked here and survives the import.
KEEP_NS = "de.tbckr.google_keep_import"
SN_NS = "org.standardnotes.sn"

# Standard Notes Super editor (Lexical-based rich text).
SUPER_NOTE_TYPE = "super"
SUPER_EDITOR_ID = "com.standardnotes.super-editor"


# ---------- Helpers ---------------------------------------------------------

def usec_to_iso(usec: int | None) -> str | None:
    """Keep timestamps are microseconds since the Unix epoch."""
    if usec is None:
        return None
    return (
        datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def list_content_to_markdown(list_content: list[dict[str, Any]]) -> str:
    """Keep list -> Markdown checkboxes; checked status is preserved."""
    lines = []
    for item in list_content:
        text = (item.get("text") or "").rstrip()
        checked = bool(item.get("isChecked", False))
        prefix = "- [x] " if checked else "- [ ] "
        lines.append(prefix + text)
    return "\n".join(lines)


_CHECKBOX_PREFIX_RE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*")


def derive_title(note: dict[str, Any], body: str) -> str:
    """Title = Keep title, else first non-empty body line, else 'Untitled'."""
    raw = (note.get("title") or "").strip()
    if raw:
        return raw
    for raw_line in body.splitlines():
        line = _CHECKBOX_PREFIX_RE.sub("", raw_line).strip()
        if line:
            # 80-char cap so list-derived titles don't run away
            return line[:80]
    return "Untitled"


def render_annotations(annotations: list[dict[str, Any]]) -> str:
    out = ["", "---", "**Links from Google Keep:**", ""]
    for a in annotations:
        url = a.get("url", "")
        title = (a.get("title") or "").strip() or url
        desc = (a.get("description") or "").strip()
        if not url:
            continue
        if desc:
            out.append(f"- [{title}]({url}) — {desc}")
        else:
            out.append(f"- [{title}]({url})")
    return "\n".join(out)


def render_attachments(attachments: list[dict[str, Any]]) -> str:
    out = [
        "",
        "---",
        "**Attachments from Google Keep (kept separately in the Takeout directory):**",
        "",
    ]
    for a in attachments:
        filepath = a.get("filePath", "")
        mimetype = a.get("mimetype", "")
        out.append(f"- `{filepath}` ({mimetype})")
    return "\n".join(out)


def render_sharees(sharees: list[dict[str, Any]]) -> str:
    out = ["", "---", "**Shared with (Google Keep):**", ""]
    for s in sharees:
        email = s.get("email", "")
        is_owner = bool(s.get("isOwner", False))
        out.append(f"- {email}{' (owner)' if is_owner else ''}")
    return "\n".join(out)


# ---------- Lexical (Super editor) helpers ----------------------------------
#
# Standard Notes' Super editor stores its document as a JSON-stringified
# Lexical EditorState in `content.text`. These builders return plain dicts
# matching Lexical's serialization shape — see lexical.dev/docs/concepts/
# serialization. Every element node carries `direction` / `format` / `indent`
# / `version`; values are explicit so the JSON shape stays auditable.

def lex_text(text: str, format_bits: int = 0) -> dict[str, Any]:
    return {
        "type": "text",
        "detail": 0,
        "format": format_bits,
        "mode": "normal",
        "style": "",
        "text": text,
        "version": 1,
    }


def lex_paragraph(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "paragraph",
        "children": children,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1,
        "textFormat": 0,
        "textStyle": "",
    }


def lex_heading(level: int, text: str) -> dict[str, Any]:
    return {
        "type": "heading",
        "tag": f"h{level}",
        "children": [lex_text(text)],
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1,
    }


def lex_list(list_type: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "list",
        "listType": list_type,
        "tag": "ol" if list_type == "number" else "ul",
        "start": 1,
        "children": items,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1,
    }


def lex_listitem(
    children: list[dict[str, Any]],
    value: int,
    checked: bool | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "type": "listitem",
        "children": children,
        "value": value,
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1,
    }
    # `checked` is required for check lists, omitted otherwise.
    if checked is not None:
        item["checked"] = checked
    return item


def lex_link(url: str, text: str) -> dict[str, Any]:
    return {
        "type": "link",
        "url": url,
        "target": None,
        "rel": None,
        "children": [lex_text(text)],
        "direction": "ltr",
        "format": "",
        "indent": 0,
        "version": 1,
    }


def lex_doc(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "root": {
            "type": "root",
            "children": children,
            "direction": "ltr",
            "format": "",
            "indent": 0,
            "version": 1,
        }
    }


def build_super_body(note: dict[str, Any]) -> dict[str, Any]:
    """Build a Lexical EditorState for the Super editor (not yet stringified)."""
    children: list[dict[str, Any]] = []

    text_content = note.get("textContent")
    if text_content:
        for line in text_content.split("\n"):
            if line:
                children.append(lex_paragraph([lex_text(line)]))
            else:
                children.append(lex_paragraph([]))

    list_content = note.get("listContent") or []
    if list_content:
        items = []
        for i, item in enumerate(list_content, start=1):
            text = (item.get("text") or "").rstrip()
            checked = bool(item.get("isChecked", False))
            items.append(
                lex_listitem([lex_text(text)], value=i, checked=checked)
            )
        children.append(lex_list("check", items))

    annotations = note.get("annotations") or []
    if annotations:
        children.append(lex_heading(3, "Links from Google Keep"))
        items = []
        i = 0
        for a in annotations:
            url = a.get("url") or ""
            if not url:
                continue
            i += 1
            title = (a.get("title") or "").strip() or url
            desc = (a.get("description") or "").strip()
            inlines: list[dict[str, Any]] = [lex_link(url, title)]
            if desc:
                inlines.append(lex_text(f" — {desc}"))
            items.append(lex_listitem(inlines, value=i))
        if items:
            children.append(lex_list("bullet", items))

    attachments = note.get("attachments") or []
    if attachments:
        children.append(lex_heading(3, "Attachments from Google Keep"))
        items = []
        for i, a in enumerate(attachments, start=1):
            filepath = a.get("filePath") or ""
            mimetype = a.get("mimetype") or ""
            # 16 = code formatting (Lexical text-format bitmask).
            inlines = [lex_text(filepath, format_bits=16), lex_text(f" ({mimetype})")]
            items.append(lex_listitem(inlines, value=i))
        children.append(lex_list("bullet", items))

    sharees = note.get("sharees") or []
    if sharees:
        children.append(lex_heading(3, "Shared with (Google Keep)"))
        items = []
        for i, s in enumerate(sharees, start=1):
            email = s.get("email") or ""
            is_owner = bool(s.get("isOwner", False))
            text = email + (" (owner)" if is_owner else "")
            items.append(lex_listitem([lex_text(text)], value=i))
        children.append(lex_list("bullet", items))

    if not children:
        children = [lex_paragraph([])]

    return lex_doc(children)


def build_body(note: dict[str, Any]) -> str:
    """Build the note body from textContent / listContent plus trailers."""
    parts: list[str] = []

    text_content = note.get("textContent")
    if text_content:
        parts.append(text_content)

    list_content = note.get("listContent")
    if list_content:
        parts.append(list_content_to_markdown(list_content))

    annotations = note.get("annotations") or []
    if annotations:
        parts.append(render_annotations(annotations))

    attachments = note.get("attachments") or []
    if attachments:
        parts.append(render_attachments(attachments))

    sharees = note.get("sharees") or []
    if sharees:
        parts.append(render_sharees(sharees))

    return "\n\n".join(p for p in parts if p)


# ---------- Conversion ------------------------------------------------------

def convert_note(
    note: dict[str, Any],
    source_filename: str,
    super_mode: bool = False,
) -> tuple[dict[str, Any], str]:
    note_uuid = str(uuid.uuid4())

    created_iso = usec_to_iso(note.get("createdTimestampUsec")) or now_iso()
    updated_iso = usec_to_iso(note.get("userEditedTimestampUsec")) or created_iso

    # Plaintext body always built — used for title and (in super mode) preview.
    body_plain = build_body(note)
    title = derive_title(note, body_plain)

    if super_mode:
        lexical_doc = build_super_body(note)
        content_text = json.dumps(
            lexical_doc, ensure_ascii=False, separators=(",", ":")
        )
    else:
        content_text = body_plain

    content: dict[str, Any] = {
        "title": title,
        "text": content_text,
        "references": [],
        "trashed": bool(note.get("isTrashed", False)),
        "pinned": bool(note.get("isPinned", False)),
        "archived": bool(note.get("isArchived", False)),
        "appData": {
            SN_NS: {
                "client_updated_at": updated_iso,
            },
            KEEP_NS: {
                "source_file": source_filename,
                "color": note.get("color", "DEFAULT"),
                # Full original for later recovery / inspection.
                # Standard Notes passes arbitrary appData keys through.
                "original": note,
            },
        },
    }

    if super_mode:
        # Without preview_plain, the SN note list shows the raw Lexical JSON
        # blob until the app re-renders the note. Cap the excerpt for sanity.
        content["noteType"] = SUPER_NOTE_TYPE
        content["editorIdentifier"] = SUPER_EDITOR_ID
        content["spellcheck"] = True
        content["preview_plain"] = body_plain[:160]

    item: dict[str, Any] = {
        "uuid": note_uuid,
        "content_type": "Note",
        "created_at": created_iso,
        "updated_at": updated_iso,
        "content": content,
    }
    return item, note_uuid


def build_tag_items(tag_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for name in sorted(tag_map.keys()):
        data = tag_map[name]
        items.append(
            {
                "uuid": data["uuid"],
                "content_type": "Tag",
                "created_at": data["created_at"],
                "updated_at": data["updated_at"],
                "content": {
                    "title": name,
                    "references": data["references"],
                    "appData": {
                        SN_NS: {
                            "client_updated_at": data["updated_at"],
                        },
                    },
                },
            }
        )
    return items


# ---------- Main ------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Google Keep Takeout into a Standard Notes backup (version 004).",
    )
    parser.add_argument(
        "input_dir",
        help="Path to the Keep directory inside the Takeout (contains the *.json files).",
    )
    parser.add_argument(
        "output_file",
        help='Output path, typically: "Standard Notes Backup and Import File.txt".',
    )
    parser.add_argument(
        "--include-trashed",
        action="store_true",
        help="Also import notes trashed in Keep (default: skip them).",
    )
    parser.add_argument(
        "--super",
        action="store_true",
        help="Import notes as Super (Lexical rich-text) notes instead of plaintext.",
    )
    args = parser.parse_args()

    input_path = Path(args.input_dir)
    if not input_path.is_dir():
        print(f"Input directory not found: {input_path}", file=sys.stderr)
        return 1

    json_files = sorted(input_path.glob("*.json"))
    if not json_files:
        print(f"No *.json files found in {input_path}.", file=sys.stderr)
        return 1

    items: list[dict[str, Any]] = []
    tag_map: dict[str, dict[str, Any]] = {}

    counts = {
        "total": 0,
        "converted": 0,
        "skipped_trashed": 0,
        "errors": 0,
    }

    for json_file in json_files:
        counts["total"] += 1
        try:
            with json_file.open("r", encoding="utf-8") as f:
                note = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR reading {json_file.name}: {exc}", file=sys.stderr)
            counts["errors"] += 1
            continue

        if note.get("isTrashed", False) and not args.include_trashed:
            counts["skipped_trashed"] += 1
            continue

        try:
            note_item, note_uuid = convert_note(
                note, json_file.name, super_mode=args.super
            )
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR converting {json_file.name}: {exc}", file=sys.stderr)
            counts["errors"] += 1
            continue

        items.append(note_item)
        counts["converted"] += 1

        for label in note.get("labels") or []:
            name = (label.get("name") or "").strip()
            if not name:
                continue
            entry = tag_map.setdefault(
                name,
                {
                    "uuid": str(uuid.uuid4()),
                    "created_at": note_item["created_at"],
                    "updated_at": note_item["updated_at"],
                    "references": [],
                },
            )
            # latest update timestamp among the referenced notes wins
            if note_item["updated_at"] > entry["updated_at"]:
                entry["updated_at"] = note_item["updated_at"]
            if note_item["created_at"] < entry["created_at"]:
                entry["created_at"] = note_item["created_at"]
            entry["references"].append(
                {"content_type": "Note", "uuid": note_uuid}
            )

    items.extend(build_tag_items(tag_map))

    backup = {"version": "004", "items": items}

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)

    print(f"Files read:             {counts['total']}", file=sys.stderr)
    print(f"Notes converted:        {counts['converted']}", file=sys.stderr)
    print(f"Skipped (trashed):      {counts['skipped_trashed']}", file=sys.stderr)
    print(f"Errors:                 {counts['errors']}", file=sys.stderr)
    print(f"Tags created:           {len(tag_map)}", file=sys.stderr)
    print(f"Output:                 {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
