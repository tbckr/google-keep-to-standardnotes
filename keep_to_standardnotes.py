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

def convert_note(note: dict[str, Any], source_filename: str) -> tuple[dict[str, Any], str]:
    note_uuid = str(uuid.uuid4())

    created_iso = usec_to_iso(note.get("createdTimestampUsec")) or now_iso()
    updated_iso = usec_to_iso(note.get("userEditedTimestampUsec")) or created_iso

    body = build_body(note)
    title = derive_title(note, body)

    item: dict[str, Any] = {
        "uuid": note_uuid,
        "content_type": "Note",
        "created_at": created_iso,
        "updated_at": updated_iso,
        "content": {
            "title": title,
            "text": body,
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
        },
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
            note_item, note_uuid = convert_note(note, json_file.name)
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
