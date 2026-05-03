#!/usr/bin/env python3
"""
parse_raw_transcript.py
-----------------------
Converts the raw Cowork session export into a clean **USERNAME:** / **Claude:**
transcript suitable for the ember-engine ingest pipeline.

Raw format observations:
  - User messages: "You said: [first line]\n[full message]"
    (first occurrence of the session is the raw paste with no prefix)
  - Claude responses: "Claude responded: [brief note]\n[tool call lines]\n[actual text]"
  - Tool call lines: appear as CONSECUTIVE DUPLICATE lines
    e.g.  "Read 3 files\nRead 3 files"  or  "Ran a command\nRan a command"
  - Noise: "Thought process", "Show more", blank duplicate lines
"""

import sys
from pathlib import Path


_DEFAULT_RAW = Path.home() / "claude_memory/conversations"
_DEFAULT_OUT = Path.home() / "claude_memory/conversations"


def _read_username() -> str:
    config = Path.home() / "claude_memory" / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith("USERNAME=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"')
    return "user"

USERNAME = _read_username()


# ---------------------------------------------------------------------------

def remove_tool_noise(text: str) -> str:
    """
    Strip tool-call lines from a block of Claude text.
    Tool calls always appear as two consecutive identical non-empty lines.
    Also removes lone 'Thought process' and 'Show more' lines.
    """
    lines = text.split("\n")
    cleaned = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Consecutive duplicate = tool call description; skip both
        if (i + 1 < len(lines)
                and line.strip()
                and line.strip() == lines[i + 1].strip()):
            i += 2
            continue
        # Single noise markers
        if line.strip() in ("Thought process", "Show more"):
            i += 1
            continue
        cleaned.append(line)
        i += 1

    # Collapse runs of 3+ blank lines down to 1
    result = []
    blank_run = 0
    for line in cleaned:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append(line)
        else:
            blank_run = 0
            result.append(line)

    return "\n".join(result).strip()


def parse_raw(raw_text: str):
    """
    Returns a list of (speaker, text) tuples.
    speaker = USERNAME or "Claude"
    """
    turns = []
    lines = raw_text.splitlines()

    # First block is the user's session-open paste (no "You said:" prefix)
    # It runs until the first "Claude responded:" line.
    first_claude = next(
        (i for i, l in enumerate(lines) if l.startswith("Claude responded:")),
        None,
    )
    if first_claude is None:
        return turns

    opener = "\n".join(lines[:first_claude]).strip()
    if opener:
        turns.append((USERNAME, opener))

    i = first_claude
    while i < len(lines):
        line = lines[i]

        # ── Claude block ───────────────────────────────────────────────────
        if line.startswith("Claude responded:"):
            # Collect everything until the next "You said:" or end of file
            block_lines = [line[len("Claude responded:"):].strip()]
            i += 1
            while i < len(lines) and not lines[i].startswith("You said:"):
                block_lines.append(lines[i])
                i += 1
            brief = block_lines[0] if block_lines else ""
            raw_block = "\n".join(block_lines)
            cleaned = remove_tool_noise(raw_block)
            # Remove duplicated intro: "Intro.\n\nIntro. full response..."
            if brief and cleaned:
                paras = cleaned.split("\n\n")
                if len(paras) >= 2 and paras[1].strip().startswith(paras[0].strip()):
                    cleaned = "\n\n".join(paras[1:]).strip()
            if cleaned:
                turns.append(("Claude", cleaned))

        # ── Bobby block ────────────────────────────────────────────────────
        elif line.startswith("You said:"):
            # The first line is "You said: [preview]"
            # The NEXT line is the full message (may be same or longer)
            preview = line[len("You said:"):].strip()
            i += 1
            # Collect full message lines until next Claude/Bobby block
            msg_lines = []
            while i < len(lines) and not lines[i].startswith("Claude responded:") \
                    and not lines[i].startswith("You said:"):
                msg_lines.append(lines[i])
                i += 1
            full_msg = "\n".join(msg_lines).strip()
            # Use whichever is longer (full_msg usually contains the full text)
            text = full_msg if len(full_msg) >= len(preview) else preview
            if not text:
                text = preview
            if text:
                turns.append((USERNAME, text))

        else:
            i += 1

    return turns


def write_transcript(turns, out_path: Path):
    parts = []
    for speaker, text in turns:
        parts.append(f"**{speaker}:**\n\n{text}")
        parts.append("")
        parts.append("---")
        parts.append("")

    out_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Written: {out_path}  ({len(turns)} turns)")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="Convert a raw Cowork window export into a verbatim transcript."
    )
    p.add_argument("raw_file", help="Path to the raw .txt export from the Cowork window")
    p.add_argument("--out", default=None,
                   help="Output path for the transcript .md file. "
                        "Default: ~/claude_memory/conversations/<raw_stem>.md")
    p.add_argument("--preview", action="store_true",
                   help="Print parsed turns without writing a file")
    args = p.parse_args()

    raw_path = Path(args.raw_file).expanduser()
    if not raw_path.exists():
        print(f"ERROR: file not found: {raw_path}")
        raise SystemExit(1)

    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        stem = raw_path.stem.replace("Raw-", "").replace("raw-", "")
        out_path = _DEFAULT_OUT / f"{stem}.md"

    raw_text = raw_path.read_text(encoding="utf-8")
    turns = parse_raw(raw_text)
    print(f"Parsed {len(turns)} turns")
    for spk, txt in turns:
        snippet = txt[:80].replace("\n", " ")
        print(f"  [{spk:6s}] {snippet!r}")

    if args.preview:
        print("\n-- preview mode: no file written --")
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_transcript(turns, out_path)
        print("Done.")
