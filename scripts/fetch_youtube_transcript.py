#!/usr/bin/env python3
"""
fetch_youtube_transcript.py
Fetches the transcript from a YouTube video and saves it as a plain text file.

Usage:
    python3 fetch_youtube_transcript.py <youtube_url>
    python3 fetch_youtube_transcript.py <youtube_url> --output ~/path/to/output.txt
    python3 fetch_youtube_transcript.py <youtube_url> --cookies ~/path/to/cookies.txt
    python3 fetch_youtube_transcript.py <youtube_url> --lang es
    python3 fetch_youtube_transcript.py <youtube_url> --raw        (keep timestamps)
    python3 fetch_youtube_transcript.py <youtube_url> --list       (list available languages)
    python3 fetch_youtube_transcript.py <youtube_url> --stdout     (print to terminal, no file)

Cookie note:
    If YouTube blocks the request with an IP/auth error, export your browser cookies
    to a Netscape-format .txt file and pass it with --cookies. In Chrome, use the
    "Get cookies.txt LOCALLY" extension. Export from youtube.com.

Output:
    Saves to ~/claude_memory/research/transcripts/ by default.
    Filename: <sanitized_title>_transcript.txt (falls back to <video_id>_transcript.txt if title unavailable)
"""

import argparse
import os
import re
import sys
from pathlib import Path
from datetime import datetime


def extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",          # youtube.com/watch?v=
        r"(?:youtu\.be/)([a-zA-Z0-9_-]{11})",   # youtu.be/
        r"(?:embed/)([a-zA-Z0-9_-]{11})",        # youtube.com/embed/
        r"(?:shorts/)([a-zA-Z0-9_-]{11})",       # youtube.com/shorts/
        r"^([a-zA-Z0-9_-]{11})$",               # bare video ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def build_api(cookies_path: str = None):
    """Build a YouTubeTranscriptApi instance, optionally with browser cookies."""
    from youtube_transcript_api import YouTubeTranscriptApi

    if cookies_path:
        import requests
        session = requests.Session()
        cookies_path = os.path.expanduser(cookies_path)
        if not os.path.exists(cookies_path):
            print(f"[error] Cookie file not found: {cookies_path}")
            sys.exit(1)
        # Load Netscape-format cookies
        with open(cookies_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    domain, _, path, secure, expires, name, value = parts[:7]
                    session.cookies.set(name, value, domain=domain.lstrip("."), path=path)
        print(f"[info] Loaded cookies from {cookies_path}")
        return YouTubeTranscriptApi(http_client=session)

    return YouTubeTranscriptApi()


def list_languages(api, video_id: str):
    """Print all available transcript languages for a video."""
    transcript_list = api.list(video_id)
    print(f"\nAvailable transcripts for {video_id}:")
    for t in transcript_list:
        generated = " [auto-generated]" if t.is_generated else ""
        translatable = " [translatable]" if t.is_translatable else ""
        print(f"  {t.language_code:10s}  {t.language}{generated}{translatable}")
    print()


def fetch_transcript(api, video_id: str, languages: list, raw: bool):
    """
    Fetch transcript and return as a formatted string.
    raw=True keeps timestamps. raw=False joins into clean flowing text.
    """
    fetched = api.fetch(video_id, languages=languages)

    if raw:
        lines = []
        for entry in fetched:
            ts = entry.start
            minutes = int(ts // 60)
            seconds = ts % 60
            lines.append(f"[{minutes:02d}:{seconds:05.2f}]  {entry.text}")
        return "\n".join(lines)
    else:
        # Join into clean paragraphs. Break every ~10 lines to add whitespace.
        chunks = [entry.text.strip() for entry in fetched]
        paragraphs = []
        group = []
        for i, chunk in enumerate(chunks):
            group.append(chunk)
            if (i + 1) % 10 == 0:
                paragraphs.append(" ".join(group))
                group = []
        if group:
            paragraphs.append(" ".join(group))
        return "\n\n".join(paragraphs)


def extract_channel_id_from_url(author_url) -> str:
    """Extract YouTube channel ID (UC...) from an author_url if present.

    YouTube oEmbed returns two URL formats:
      - https://www.youtube.com/channel/UCxxxxxxxxxx  (contains stable channel ID)
      - https://www.youtube.com/@handle               (no channel ID without API)

    Returns the channel ID string or None.
    """
    if not author_url:
        return None
    match = re.search(r"/channel/(UC[\w-]+)", author_url)
    return match.group(1) if match else None


def get_video_metadata(video_id: str) -> dict:
    """Fetch video metadata via oEmbed (no API key needed).

    Returns a dict with keys:
        title        : str  — video title (empty string on failure)
        channel_name : str  — channel display name (empty string on failure)
        channel_url  : str  — channel URL from oEmbed (empty string on failure)
        channel_id   : str or None — UC... ID if extractable from channel_url
    """
    empty = {"title": "", "channel_name": "", "channel_url": "", "channel_id": None}
    try:
        import urllib.request
        import json
        url = (f"https://www.youtube.com/oembed"
               f"?url=https://www.youtube.com/watch?v={video_id}&format=json")
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        channel_url = data.get("author_url", "")
        return {
            "title":        data.get("title", ""),
            "channel_name": data.get("author_name", ""),
            "channel_url":  channel_url,
            "channel_id":   extract_channel_id_from_url(channel_url),
        }
    except Exception:
        return empty


def get_video_title(video_id: str) -> str:
    """Backward-compatible wrapper — returns title only. Use get_video_metadata for full info."""
    return get_video_metadata(video_id)["title"]


def title_to_filename(title: str) -> str:
    """Convert a video title to a clean, readable filename."""
    # Lowercase
    name = title.lower()
    # Replace common separators and spaces with underscores
    name = re.sub(r"[\s\-–]+", "_", name)
    # Strip characters that are invalid or annoying in filenames
    name = re.sub(r"[^\w]", "", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    # Trim leading/trailing underscores
    name = name.strip("_")
    # Cap at 60 chars so paths stay readable
    return name[:60]


def build_transcript_filename(title: str, video_id: str, dt: datetime = None) -> str:
    """
    Build the output filename for a transcript.
    Format: YYYY_MM_DD_<slug>_transcript.txt
    The date prefix makes the transcripts folder scannable by ingestion date.
    Falls back to video_id if title is empty.
    """
    if dt is None:
        dt = datetime.now()
    date_prefix = dt.strftime("%Y_%m_%d")
    slug = title_to_filename(title) if title else video_id
    return f"{date_prefix}_{slug}_transcript.txt"


def main():
    parser = argparse.ArgumentParser(
        description="Fetch a YouTube video transcript and save it to a file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("url", help="YouTube video URL or video ID")
    parser.add_argument("--output", "-o", help="Output file path (default: ~/claude_memory/research/transcripts/<id>_transcript.txt)")
    parser.add_argument("--cookies", "-c", help="Path to Netscape-format cookies.txt file (use if YouTube blocks the request)")
    parser.add_argument("--lang", "-l", default="en", help="Language code (default: en). Pass multiple with commas: en,es")
    parser.add_argument("--raw", action="store_true", help="Keep timestamps in output instead of joining into clean text")
    parser.add_argument("--list", action="store_true", help="List available transcript languages and exit")
    parser.add_argument("--stdout", action="store_true", help="Print transcript to stdout instead of saving to file")
    args = parser.parse_args()

    # Resolve video ID
    video_id = extract_video_id(args.url)
    if not video_id:
        print(f"[error] Could not extract video ID from: {args.url}")
        sys.exit(1)
    print(f"[info] Video ID: {video_id}")

    # Build API
    api = build_api(args.cookies)

    # List mode
    if args.list:
        list_languages(api, video_id)
        return

    # Resolve language list
    languages = [lang.strip() for lang in args.lang.split(",")]

    # Fetch
    print(f"[info] Fetching transcript (languages: {languages})...")
    try:
        text = fetch_transcript(api, video_id, languages, args.raw)
    except Exception as e:
        err = str(e)
        print(f"\n[error] Transcript fetch failed: {err}\n")
        if any(x in err for x in ["IpBlocked", "RequestBlocked", "TooManyRequests", "429", "403"]):
            print("YouTube is blocking unauthenticated requests from this IP.")
            print("Fix: export your browser cookies from youtube.com and pass them with --cookies.")
            print("  In Chrome: install 'Get cookies.txt LOCALLY' extension, export from youtube.com")
            print(f"  Then run: python3 {sys.argv[0]} '{args.url}' --cookies ~/cookies.txt")
        elif "NoTranscriptFound" in err or "TranscriptsDisabled" in err:
            print("This video has no transcript available in the requested language.")
            print(f"Try: python3 {sys.argv[0]} '{args.url}' --list")
        sys.exit(1)

    # Stats
    word_count = len(text.split())
    read_minutes = round(word_count / 200)
    print(f"[info] Transcript fetched. {word_count:,} words (~{read_minutes} min read)")

    # Get metadata for header (title + channel info via oEmbed)
    meta = get_video_metadata(video_id)
    title = meta["title"]
    if title:
        print(f"[info] Title: {title}")
    if meta["channel_name"]:
        print(f"[info] Channel: {meta['channel_name']}")

    # Build header
    header_lines = [
        f"# YouTube Transcript",
        f"",
        f"**Video ID:** {video_id}",
        f"**URL:** https://www.youtube.com/watch?v={video_id}",
    ]
    if title:
        header_lines.insert(1, f"**Title:** {title}")
    if meta["channel_name"]:
        header_lines.append(f"**Channel:** {meta['channel_name']}")
    if meta["channel_url"]:
        header_lines.append(f"**Channel URL:** {meta['channel_url']}")
    if meta["channel_id"]:
        header_lines.append(f"**Channel ID:** {meta['channel_id']}")
    header_lines += [
        f"**Fetched:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**Words:** {word_count:,}",
        f"**Format:** {'timestamped' if args.raw else 'clean text'}",
        f"",
        f"---",
        f"",
    ]
    full_text = "\n".join(header_lines) + "\n" + text

    # Output
    if args.stdout:
        print("\n" + full_text)
        return

    # Resolve output path
    if args.output:
        output_path = Path(os.path.expanduser(args.output))
    else:
        base_dir = Path.home() / "claude_memory" / "research" / "transcripts"
        base_dir.mkdir(parents=True, exist_ok=True)
        filename = build_transcript_filename(title, video_id, datetime.now())
        output_path = base_dir / filename

    output_path.write_text(full_text, encoding="utf-8")
    print(f"[done] Saved to: {output_path}")


if __name__ == "__main__":
    main()
