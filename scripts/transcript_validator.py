#!/usr/bin/env python3
"""
transcript_validator.py
-----------------------
Validates that a conversation transcript contains verbatim exchanges rather
than summarized or compressed entries.

Called automatically by ingest.py before Qwen extraction. Can also be run
manually at any time to check a transcript file.

USAGE
-----
Check a specific file:
    python3 ~/claude_memory/scripts/transcript_validator.py USERNAME_2026_05_01_001.md

Check all unprocessed files:
    python3 ~/claude_memory/scripts/transcript_validator.py --all

Exit codes:
    0 = file looks verbatim (ok to proceed)
    1 = warnings found (suspicious entries, prompt user to confirm)
    2 = fatal problem (file unreadable, no exchanges found)

WHY THIS EXISTS
---------------
Claude instinctively compresses transcript entries when appending mid-session,
especially when the exchange was long. This has occurred in multiple sessions.
Summarized transcripts produce fewer useful Qwen extractions, defeating the
purpose of the memory system.

WHAT IT CHECKS
--------------
For each **Claude:** block:
  - Length < 300 chars flags as likely summary (real responses are typically
    much longer in this context).
  - Matches compression signature patterns: entries that open with a
    past-tense action verb followed by a colon or comma
    (e.g. "Flagged:", "Built:", "Presented:", "Confirmed:", "Noted that").

For each **Bobby:** block:
  - Contains [bracketed placeholder text] indicating a stand-in description
    rather than actual message content (e.g. "[ran ingest.py output]").

A file with zero Claude blocks is also flagged -- it may be an empty stub
that was ingested too early in the session.
"""

import re
import sys
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

_BASE    = Path.home() / "claude_memory"
CONV_DIR = _BASE / "conversations"

# ── Thresholds ─────────────────────────────────────────────────────────────────

# Claude responses shorter than this are flagged as possible summaries.
# Genuine short responses (one-liners) are possible but rare in this context.
CLAUDE_MIN_CHARS = 300

# Minimum number of Claude blocks expected in a real session transcript.
# A file with fewer than this is likely a stub.
MIN_CLAUDE_BLOCKS = 3

# ── Compression signature patterns ────────────────────────────────────────────

# These patterns match the opening of summary-style entries where Claude
# compressed a long response into a one-line recap.
SUMMARY_OPENERS = re.compile(
    r"^("
    r"Flagged[:\s]|"
    r"Built[:\s]|"
    r"Presented[:\s]|"
    r"Confirmed[:\s]|"
    r"Agreed[.\s]|"
    r"Explained[:\s]|"
    r"Guided[:\s]|"
    r"Designed[:\s]|"
    r"Recommended[:\s]|"
    r"Checked[:\s]|"
    r"Updated[:\s]|"
    r"Moved to[:\s]|"
    r"Proceeded to[:\s]|"
    r"Ran[:\s]|"
    r"\[session open\]|"
    r"\[DRY RUN\]"
    r")",
    re.IGNORECASE
)
# Removed from SUMMARY_OPENERS:
#   "Noted"   -- too common as a genuine verbatim response opener
#   "Created" -- same issue; Claude legitimately says "Created X" when building files

# Bobby messages that contain [bracketed descriptions] instead of actual text.
# Deliberately excludes:
#   - ISO timestamps:  [2026-04-24 12:52:12]
#   - Numeric IDs:     [42]  [v2.3]
#   - Ingest log tags: [2026-04-24 ...]
#   - Terminal output markers (see TERMINAL_OUTPUT_PATTERN below)
# Matches natural-language placeholders like: [ran the script on his Mac]
PLACEHOLDER_PATTERN = re.compile(
    r"(?<!\[)"                   # exclude if preceded by [ (inner bracket of [[ ... ]])
    r"\["
    r"(?!\[)"                    # exclude double-bracket [[ (Python list reprs, expressive text)
    r"(?!\d{4}-\d{2}-\d{2})"   # exclude ISO date prefix
    r"(?!\d+[\].])"             # exclude plain numbers / version strings
    r"(?![A-Z]{1,5}\])"        # exclude short allcaps codes like [Y/n]
    r"(?!s\.text\b)"            # exclude Python expressions like [s.text for s in t]
    r"[a-zA-Z][^\]]{14,}"      # must start with a letter, at least 15 chars total
    r"\]"
)

# Terminal output markers are bracketed Bobby messages that summarize what a
# script printed rather than quoting Bobby's actual words. These are legitimate —
# we agreed not to paste full terminal output into transcripts. A marker is
# recognized as terminal output (not a placeholder) if it contains:
#   - a .py or .sh script filename, OR
#   - command-line flags (--flag), OR
#   - a numeric result pattern (N chunks, N processed, N passed, etc.)
TERMINAL_OUTPUT_PATTERN = re.compile(
    r"\.py\b|\.sh\b"            # script filename
    r"|--[a-z]"                 # CLI flag
    r"|\d+\s+(?:chunk|embed|process|skip|fail|pass|belief|error|warning|result|seed|hit)"
    r"|session resumed"         # context compaction marker
    r"|context compaction"      # explicit compaction note
    r"|continuing from \w",     # continuation marker
    re.IGNORECASE
)

# A Claude block that contains a code fence is legitimately short — the code IS
# the content. Don't apply the length threshold to these blocks.
CODE_FENCE_PATTERN = re.compile(r"```")


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_blocks(text):
    """
    Parse a transcript into a list of (speaker, content, line_number) tuples.
    Speaker is 'User' (any **Name:** that is not Claude) or 'Claude'.
    Detects any **Word:** pattern — works with any USERNAME, not just 'Bobby'.
    """
    blocks = []
    current_speaker = None
    current_lines = []
    current_start = 0

    for i, line in enumerate(text.splitlines(), 1):
        claude_match = re.match(r"^\*\*Claude:\*\*\s*(.*)", line)
        user_match   = re.match(r"^\*\*(?!Claude:)\w[\w ]*:\*\*\s*(.*)", line) if not claude_match else None

        if claude_match or user_match:
            # Save the previous block
            if current_speaker:
                blocks.append((
                    current_speaker,
                    "\n".join(current_lines).strip(),
                    current_start
                ))
            # Start new block
            current_speaker = "Claude" if claude_match else "User"
            first_line = (claude_match or user_match).group(1)
            current_lines = [first_line] if first_line.strip() else []
            current_start = i
        elif current_speaker:
            current_lines.append(line)

    # Save final block
    if current_speaker:
        blocks.append((
            current_speaker,
            "\n".join(current_lines).strip(),
            current_start
        ))

    return blocks


# ── Checks ────────────────────────────────────────────────────────────────────

def check_file(filepath):
    """
    Validate a transcript file. Returns (warnings, errors) as lists of strings.
    warnings = suspicious but not fatal
    errors   = definite problems that should block ingestion
    """
    warnings = []
    errors   = []

    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as e:
        errors.append(f"Could not read file: {e}")
        return warnings, errors

    blocks = parse_blocks(text)
    claude_blocks = [(content, lineno) for spk, content, lineno in blocks if spk == "Claude"]
    user_blocks   = [(content, lineno) for spk, content, lineno in blocks if spk == "User"]

    # Fatal: no exchanges at all
    if not blocks:
        errors.append("No **[Name]:** or **Claude:** blocks found. File may be empty or malformatted.")
        return warnings, errors

    # Warning: very few Claude blocks (probable stub ingested too early)
    if len(claude_blocks) < MIN_CLAUDE_BLOCKS:
        warnings.append(
            f"Only {len(claude_blocks)} Claude block(s) found. "
            f"Expected at least {MIN_CLAUDE_BLOCKS} for a real session transcript. "
            f"This file may be a session stub ingested before the session was complete. "
            f"Consider waiting until the session is done before ingesting."
        )

    # Check each Claude block
    for content, lineno in claude_blocks:
        issues = []
        has_code_block = bool(CODE_FENCE_PATTERN.search(content))

        # Length check — exempt blocks that contain code fences. A one-liner
        # followed by a code block is legitimately short; the code IS the content.
        if len(content) < CLAUDE_MIN_CHARS and not has_code_block:
            issues.append(
                f"length {len(content)} chars (threshold: {CLAUDE_MIN_CHARS}) -- "
                f"likely a summary, not verbatim text"
            )

        # Compression signature check
        if SUMMARY_OPENERS.match(content.strip()):
            snippet = content.strip()[:80].replace("\n", " ")
            issues.append(
                f'opens with summary-style phrase: "{snippet}..."'
            )

        if issues:
            for issue in issues:
                warnings.append(f"Line {lineno} (Claude block): {issue}")

    # Check user blocks for placeholders
    for content, lineno in user_blocks:
        matches = PLACEHOLDER_PATTERN.findall(content)
        if matches:
            for m in matches:
                # Terminal output markers are intentional — they summarize script
                # output that we agreed not to paste verbatim. Skip them.
                if TERMINAL_OUTPUT_PATTERN.search(m):
                    continue
                warnings.append(
                    f"Line {lineno} (user block): contains placeholder text: [{m}] -- "
                    f"replace with actual message content"
                )

    return warnings, errors


# ── Report ────────────────────────────────────────────────────────────────────

def report(filepath, warnings, errors, quiet=False):
    """Print a human-readable validation report. Returns exit code."""
    name = filepath.name

    if errors:
        print(f"  FATAL  {name}")
        for e in errors:
            print(f"         ERROR: {e}")
        return 2

    if warnings:
        print(f"  WARN   {name}  ({len(warnings)} issue(s))")
        if not quiet:
            for w in warnings:
                print(f"         ! {w}")
        return 1

    print(f"  OK     {name}")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────

def validate_one(filename, quiet=False):
    """Validate a single file. Returns (exit_code, warnings, errors)."""
    filepath = CONV_DIR / filename
    if not filepath.exists():
        # Try as absolute path
        filepath = Path(filename)
    if not filepath.exists():
        print(f"  ERROR: file not found: {filename}")
        return 2, [], [f"File not found: {filename}"]

    warnings, errors = check_file(filepath)
    code = report(filepath, warnings, errors, quiet=quiet)
    return code, warnings, errors


def main():
    import argparse
    p = argparse.ArgumentParser(description="Validate transcript verbatim quality")
    p.add_argument("filename", nargs="?", help="Transcript filename (in conversations/)")
    p.add_argument("--all",   action="store_true", help="Check all .md files in conversations/")
    p.add_argument("--quiet", action="store_true", help="Suppress per-warning detail")
    args = p.parse_args()

    print()
    print("Transcript Validator")
    print("=" * 50)

    if args.all:
        files = sorted(CONV_DIR.glob("*.md"))
        if not files:
            print("No .md files found in conversations/")
            sys.exit(0)
        worst = 0
        for f in files:
            w, e = check_file(f)
            code = report(f, w, e, quiet=args.quiet)
            worst = max(worst, code)
        print()
        if worst == 0:
            print("All files look verbatim.")
        elif worst == 1:
            print("Some files have suspicious entries. Review warnings above.")
        else:
            print("Fatal problems found. See errors above.")
        sys.exit(worst)

    elif args.filename:
        code, warnings, errors = validate_one(args.filename, quiet=args.quiet)
        print()
        if code == 0:
            print("File looks verbatim. OK to ingest.")
        elif code == 1:
            print(f"{len(warnings)} warning(s). Review before ingesting.")
            print("To ingest anyway: python3 ~/claude_memory/scripts/ingest.py "
                  f"{args.filename}")
        else:
            print("Fatal problem. Fix before ingesting.")
        sys.exit(code)

    else:
        p.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
