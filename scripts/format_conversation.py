#!/usr/bin/env python3
"""
format_conversation.py
Converts a raw Claude Cowork conversation export into a formatted MD file
with proper speaker attribution (Bobby, Claude, ChatGPT, YouTube).

Usage:
    python3 ~/claude_memory/scripts/format_conversation.py conversation_002_raw.txt

Output:
    ~/claude_memory/conversations/conversation_002.md
"""

import os
import re
import sys
from pathlib import Path

CONV_DIR = os.path.expanduser("~/claude_memory/conversations/")

# Read USERNAME from .ember_config
def _read_username() -> str:
    config = Path.home() / "claude_memory" / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith("USERNAME=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"')
    return "user"

USERNAME = _read_username()
# Override for direct path resolution if expanduser doesn't resolve correctly
if not os.path.isdir(CONV_DIR):
    CONV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "conversations") + "/"

# Annotations that indicate Claude is about to speak or is doing tool work.
# These appear as duplicate consecutive lines in the raw export.
CLAUDE_ANNOTATIONS = [
    "Thought process",
    "Used a tool",
    "Created a file",
    "Updated todo list",
    "Inspect all tables and data in memory.db",
    "Show more",
]

# Patterns that match tool use annotations (variable text like "Read X files")
TOOL_PATTERNS = [
    r"^Read \d+ files?",
    r"^Ran \d+ commands?",
    r"^Read a file",
    r"^Wrote a file",
    r"^Edited a file",
    r"^Ran a command",
    r"^Used \d+ tools?",
]

# Markers that suggest ChatGPT content was pasted in
CHATGPT_MARKERS = [
    "🗒️ Answer",
    "MEMORY SYSTEM UPGRADE BRIEF",
    "ChatGPT",
]

# YouTube markers
YOUTUBE_MARKERS = [
    "https://youtu.be",
    "youtu.be",
]


def is_annotation(line):
    """Return True if this line is a system annotation to strip."""
    stripped = line.strip()
    if stripped in CLAUDE_ANNOTATIONS:
        return True
    for pattern in TOOL_PATTERNS:
        if re.match(pattern, stripped):
            return True
    return False


def is_image_line(line):
    stripped = line.strip()
    return stripped.startswith("Uploaded image") or stripped.endswith(".jpeg") or stripped.endswith(".png")


def looks_like_terminal(line):
    """Detect terminal output lines."""
    stripped = line.strip()
    return (
        bool(re.match(r'\w+@\w', stripped)) or
        stripped.startswith("(base)") or
        stripped.startswith("Traceback") or
        stripped.startswith("  File ") or
        stripped.startswith("Error") or
        stripped.startswith("Successfully") or
        stripped.startswith("Collecting") or
        stripped.startswith("Downloading") or
        stripped.startswith("Installing") or
        stripped.startswith("===") or
        stripped.startswith("Processing:") or
        stripped.startswith("Started:") or
        stripped.startswith("Finished:") or
        stripped.startswith("Conversation loaded") or
        stripped.startswith("Running extractions") or
        stripped.startswith("  Extract") or
        stripped.startswith("Database row") or
        stripped.startswith("  sessions") or
        stripped.startswith("  conversations") or
        stripped.startswith("  beliefs") or
        stripped.startswith("  epiphanies") or
        stripped.startswith("  questions") or
        stripped.startswith("  goals") or
        stripped.startswith("  entities") or
        stripped.startswith("  concepts") or
        stripped.startswith("  moods") or
        stripped.startswith("  gratitude") or
        stripped.startswith("  memory_provenance") or
        stripped.startswith("  patterns") or
        stripped.startswith("ollama run") or
        stripped.startswith("pip3 install") or
        stripped.startswith("python3") or
        stripped.startswith("sqlite3")
    )


def contains_chatgpt_marker(block):
    for marker in CHATGPT_MARKERS:
        if marker in block:
            return True
    return False


def contains_youtube_marker(block):
    for marker in YOUTUBE_MARKERS:
        if marker in block:
            return True
    return False


def detect_format(lines, raw):
    """Detect whether the file uses separate-line annotations or inline concatenated format."""
    for line in lines[:200]:
        stripped = line.strip()
        if stripped in CLAUDE_ANNOTATIONS:
            return "line_based"
    if "Thought processThought process" in raw:
        return "inline"
    return "line_based"


def remove_inline_annotations(text):
    """Remove doubled tool-use annotations that appear inside Claude content."""
    patterns = [
        r"Updated todo list(?:, [\w\s]+)?Updated todo list",
        r"Read a fileRead a file",
        r"Read \d+ files?Read \d+ files?",
        r"Ran a commandRan a command",
        r"Ran \d+ commands?(?:[^R\n]{0,60})Ran \d+ commands?(?:[^R\n]{0,60})?",
        r"Created a fileCreated a file",
        r"Edited a fileEdited a file",
        r"Edited \d+ files?[^E\n]{0,80}Edited \d+ files?[^E\n]{0,80}",
        r"Searched codeSearched code",
        r"Inspect [^\n]{0,80}Inspect [^\n]{0,80}",
        r"Check [^\n]{0,80}Check [^\n]{0,80}",
        r"Wrote a fileWrote a file",
        r"Written to:[^\n]*",
    ]
    for p in patterns:
        text = re.sub(p, " ", text, flags=re.IGNORECASE)
    return text


def extract_bobby_tail(text):
    """
    Try to separate Claude's content from Bobby's tail message within a chunk.
    Returns (claude_part, bobby_part). If no clean split found, returns (text, '').
    """
    # Case 1: Terminal prompt marks Bobby's activity
    _terminal_re = re.compile(r'\n\w+@\w[\w-]* \S+ %')
    m = _terminal_re.search(text)
    terminal_idx = m.start() if m and m.start() > len(text) * 0.2 else -1
    if terminal_idx == -1:
        m2 = re.search(r'\w+@\w[\w-]* \S+ %', text)
        if m2 and m2.start() > len(text) * 0.4:
            terminal_idx = m2.start()
            return text[:terminal_idx].strip(), text[terminal_idx:].strip()
    elif terminal_idx > len(text) * 0.2:
        return text[:terminal_idx].strip(), text[terminal_idx:].strip()

    # Case 2: Find the Bobby/Claude boundary in the last paragraph
    # The last paragraph (after the final newline) often contains both
    last_nl = text.rfind("\n")
    if last_nl != -1:
        last_para = text[last_nl + 1:]
        # Look for sentence end followed immediately by new sentence (no space or newline)
        # This is the inline Bobby continuation pattern: "claude sentence?Bobby response"
        m = re.search(r'([.?!]["\']?)([A-Z][a-z]|[a-z])', last_para)
        if m:
            # Find the transition closest to the END of the paragraph
            all_matches = list(re.finditer(r'([.?!]["\']?)([A-Za-z])', last_para))
            if all_matches:
                # Use the LAST match as the Bobby start (nearest the end)
                best = all_matches[-1]
                split_in_para = best.end(1)
                full_split = last_nl + 1 + split_in_para
                claude_tail = text[:full_split].strip()
                bobby_part = text[full_split:].strip()
                # Only split if Bobby's part is non-trivial
                if bobby_part and len(bobby_part) > 10:
                    return claude_tail, bobby_part

    return text, ""


def split_inline_format(raw):
    """
    Handle the inline annotation format used in session 3+.
    Splits on 'Thought processThought process' (the Claude turn marker) and
    attempts to separate Bobby's tail messages from each Claude chunk.
    Returns list of (speaker_hint, text) tuples.
    """
    # Normalize Show more UI elements
    text = raw.replace("Show more", "\n")

    MARKER = "Thought processThought process"
    chunks = text.split(MARKER)

    blocks = []

    # Chunk 0: Bobby's opening message or empty (file usually starts with Claude marker)
    if chunks[0].strip():
        blocks.append(("human", chunks[0].strip()))

    for chunk in chunks[1:]:
        if not chunk.strip():
            continue

        # Remove inline tool-use annotations
        cleaned = remove_inline_annotations(chunk)

        # Try to extract Bobby's tail
        claude_part, bobby_part = extract_bobby_tail(cleaned)

        if claude_part.strip():
            blocks.append(("claude", claude_part.strip()))
        if bobby_part.strip():
            blocks.append(("human", bobby_part.strip()))

    return blocks


def split_into_raw_blocks(lines, raw=""):
    """
    Split raw lines into alternating Bobby/Claude blocks.
    Handles both line-based (session 2) and inline (session 3+) formats.
    Returns list of (speaker_hint, text) tuples.
    """
    fmt = detect_format(lines, raw)
    if fmt == "inline":
        return split_inline_format(raw)

    # ---- Original line-based parser ----
    blocks = []
    current_lines = []
    current_is_claude = False
    i = 0

    while i < len(lines):
        line = lines[i]

        # Skip image upload lines
        if is_image_line(line):
            i += 1
            continue

        # Check for duplicate annotation (signals start of Claude turn)
        if is_annotation(line):
            # Check if next non-blank line is the same annotation (duplicate pattern)
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and is_annotation(lines[j]):
                # This is the annotation pair. Save current block, start Claude block.
                if current_lines:
                    block_text = "\n".join(current_lines).strip()
                    if block_text:
                        blocks.append(("claude" if current_is_claude else "human", block_text))
                current_lines = []
                current_is_claude = True
                # Skip past the duplicate annotation
                i = j + 1
                continue

        current_lines.append(line)
        i += 1

    # Flush last block
    if current_lines:
        block_text = "\n".join(current_lines).strip()
        if block_text:
            blocks.append(("claude" if current_is_claude else "human", block_text))

    return blocks


def label_block(hint, text):
    """
    Determine the actual speaker label for a block.
    Bobby blocks may contain pasted ChatGPT or YouTube content.
    """
    if hint == "claude":
        return "Claude"

    # Bobby block: check if it's mostly ChatGPT pasted content
    if contains_chatgpt_marker(text):
        return f"{USERNAME} + ChatGPT"

    if contains_youtube_marker(text):
        return f"{USERNAME} (YouTube transcript)"

    return USERNAME


def format_block(speaker, text):
    """Format a block with speaker label and clean up internal whitespace."""
    # Clean up excess blank lines
    lines = text.split("\n")
    cleaned = []
    prev_blank = False
    for line in lines:
        is_blank = line.strip() == ""
        if is_blank and prev_blank:
            continue  # collapse multiple blank lines
        cleaned.append(line)
        prev_blank = is_blank

    body = "\n".join(cleaned).strip()

    # Wrap terminal output in code blocks if not already
    if any(looks_like_terminal(l) for l in body.split("\n")):
        # Check if it's already in a code block
        if "```" not in body:
            terminal_lines = []
            non_terminal_lines = []
            in_terminal = False
            result_parts = []
            for line in body.split("\n"):
                if looks_like_terminal(line):
                    if not in_terminal:
                        if non_terminal_lines:
                            result_parts.append("\n".join(non_terminal_lines))
                            non_terminal_lines = []
                        result_parts.append("```")
                        in_terminal = True
                    result_parts.append(line)
                else:
                    if in_terminal:
                        result_parts.append("```")
                        in_terminal = False
                    non_terminal_lines.append(line)
            if in_terminal:
                result_parts.append("```")
            if non_terminal_lines:
                result_parts.append("\n".join(non_terminal_lines))
            body = "\n".join(result_parts).strip()

    return f"**{speaker}:** {body}\n"


def process_file(input_filename):
    input_path = os.path.join(CONV_DIR, input_filename)
    if not os.path.exists(input_path):
        print(f"ERROR: File not found: {input_path}")
        sys.exit(1)

    output_filename = input_filename.replace("_raw.txt", ".md").replace("_raw", "")
    if output_filename == input_filename:
        output_filename = input_filename.replace(".txt", "_formatted.md")
    output_path = os.path.join(CONV_DIR, output_filename)

    print(f"Reading: {input_path}")
    with open(input_path, "r", encoding="utf-8") as f:
        raw = f.read()

    lines = raw.split("\n")
    print(f"Lines: {len(lines):,}")

    # Determine date: prefer filename encoding, then explicit **Date:** header,
    # then fall back to a prompt rather than grabbing an arbitrary date from content.
    fn_match = re.search(r'(\d{4})_(\d{2})_(\d{2})', input_filename)
    if fn_match:
        date_str = f"{fn_match.group(1)}-{fn_match.group(2)}-{fn_match.group(3)}"
    else:
        header_match = re.search(r'\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})', raw[:2000])
        if header_match:
            date_str = header_match.group(1)
        else:
            date_str = input(f"  Enter session date for {input_filename} (YYYY-MM-DD): ").strip() or "unknown"

    print("Splitting into speaker blocks...")
    blocks = split_into_raw_blocks(lines, raw)
    fmt = detect_format(lines, raw)
    print(f"Format detected: {fmt}")
    print(f"Blocks found: {len(blocks)}")

    # Derive conversation label from output filename (e.g. conversation_003.md -> 003)
    conv_label = output_filename.replace(".md", "").replace("conversation_", "").replace("_formatted", "")

    output_lines = [
        f"# Conversation {conv_label}",
        f"**Date:** {date_str}",
        f"**Participants:** {USERNAME}, Claude",
        f"**Source:** {input_filename}",
        f"",
        f"---",
        f"",
    ]

    bobby_count = 0
    claude_count = 0

    for hint, text in blocks:
        if not text.strip():
            continue
        speaker = label_block(hint, text)
        if USERNAME in speaker:
            bobby_count += 1
        else:
            claude_count += 1
        output_lines.append(format_block(speaker, text))
        output_lines.append("")

    output_lines.append("---")
    output_lines.append(f"*Bobby turns: {bobby_count} | Claude turns: {claude_count}*")

    output = "\n".join(output_lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output)

    print(f"\nWritten to: {output_path}")
    print(f"Bobby turns: {bobby_count}")
    print(f"Claude turns: {claude_count}")
    print(f"Output size: {len(output):,} characters")
    print("\nDone. Review conversation_002.md and correct any misattributions before processing.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        filename = sys.argv[1]
    else:
        filename = input("Enter raw conversation filename: ").strip()

    process_file(filename)
