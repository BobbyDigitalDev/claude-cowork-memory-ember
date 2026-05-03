#!/usr/bin/env python3
"""
populate_channel_ids.py
-----------------------
One-time script to populate trusted_sources.channel_id for all active
YouTube channels where channel_id is NULL.

YouTube's oEmbed API no longer returns UC... channel IDs for @handle URLs —
it now returns the @handle itself, making oEmbed useless for this purpose.

This script tries three resolution methods in order:

  Method 1 — yt-dlp (preferred, no API key needed):
    yt-dlp extracts channel metadata including the UC... channel_id from
    any YouTube URL. Run: pip install yt-dlp
    Install: pip install yt-dlp (or: brew install yt-dlp)

  Method 2 — YouTube Data API v3:
    Set YOUTUBE_API_KEY in ~/claude_memory/.env.
    Free tier: 10,000 units/day. Channels.list costs 1 unit per call.
    Register at: https://console.developers.google.com/

  Method 3 — Manual entry:
    Script falls back to prompting for each unresolved channel.
    Paste the UC... ID (visible in the channel URL after clicking About,
    or from https://commentpicker.com/youtube-channel-id.php).

USAGE
-----
    python3 ~/claude_memory/scripts/populate_channel_ids.py
    python3 ~/claude_memory/scripts/populate_channel_ids.py --dry-run
    python3 ~/claude_memory/scripts/populate_channel_ids.py --manual-only
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_BASE   = Path.home() / "claude_memory"
DB_PATH = _BASE / "memory.db"
ENV_PATH = _BASE / ".env"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def resolve_via_ytdlp(channel_url: str) -> str:
    """Use yt-dlp to extract channel_id from a channel URL."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-single-json", "--no-download",
             "--playlist-items", "0", channel_url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        # yt-dlp returns channel_id directly
        ch_id = data.get("channel_id") or data.get("uploader_id", "")
        if ch_id and ch_id.startswith("UC"):
            return ch_id
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        pass
    return None


def resolve_via_youtube_api(handle_or_url: str, api_key: str) -> str:
    """Use YouTube Data API v3 to resolve @handle or channel URL to channel_id."""
    # Extract handle from URL if needed
    handle = handle_or_url
    m = re.search(r"@([\w.-]+)", handle_or_url)
    if m:
        handle = "@" + m.group(1)

    # forHandle parameter (strips leading @)
    for_handle = handle.lstrip("@")
    url = (f"https://www.googleapis.com/youtube/v3/channels"
           f"?part=id&forHandle={urllib.parse.quote(for_handle)}&key={api_key}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ClaudeMemoryScout/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        items = data.get("items", [])
        if items:
            return items[0]["id"]
    except Exception as e:
        print(f"    API error for {handle}: {e}")
    return None


def check_ytdlp() -> bool:
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run(args):
    env = load_env()
    youtube_api_key = env.get("YOUTUBE_API_KEY")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT id, channel_name, channel_url, channel_id
        FROM trusted_sources
        WHERE is_active = 1
          AND source_type = 'youtube_channel'
          AND (channel_id IS NULL OR channel_id = '')
        ORDER BY id
    """).fetchall()

    if not rows:
        print("All trusted YouTube channels already have channel_ids. Nothing to do.")
        conn.close()
        return

    print(f"\nChannels needing channel_id: {len(rows)}")
    print("=" * 60)

    ytdlp_available = check_ytdlp()
    if ytdlp_available:
        print("yt-dlp: available (method 1)")
    elif youtube_api_key:
        print("yt-dlp: not found | YouTube API key: loaded (method 2)")
    else:
        print("yt-dlp: not found | YouTube API key: not set")
        print("Falling back to manual entry (method 3)")
        print("You can also install yt-dlp: pip install yt-dlp")
        print("Or add YOUTUBE_API_KEY to ~/claude_memory/.env\n")

    resolved = 0

    for row in rows:
        ch_name = row["channel_name"] or "Unknown"
        ch_url  = row["channel_url"] or ""
        ch_db_id = row["id"]

        print(f"\n[{ch_name}]  {ch_url}")

        ch_id = None

        # Method 1: yt-dlp
        if ytdlp_available and not args.manual_only:
            print(f"  Trying yt-dlp...")
            ch_id = resolve_via_ytdlp(ch_url)
            if ch_id:
                print(f"  yt-dlp resolved: {ch_id}")

        # Method 2: YouTube Data API
        if not ch_id and youtube_api_key and not args.manual_only:
            print(f"  Trying YouTube Data API...")
            ch_id = resolve_via_youtube_api(ch_url, youtube_api_key)
            if ch_id:
                print(f"  API resolved: {ch_id}")

        # Method 3: Manual entry
        if not ch_id:
            print(f"  Could not auto-resolve.")
            print(f"  Find the UC... ID at: https://commentpicker.com/youtube-channel-id.php")
            print(f"  Or open {ch_url} in Chrome → right-click → View Page Source → search 'externalId'")
            try:
                user_input = input(f"  Enter channel_id for {ch_name} (or Enter to skip): ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nAborted.")
                break
            if user_input.startswith("UC"):
                ch_id = user_input
                print(f"  Manual: {ch_id}")
            else:
                print(f"  Skipped.")
                continue

        if ch_id and not args.dry_run:
            conn.execute(
                "UPDATE trusted_sources SET channel_id = ? WHERE id = ?",
                (ch_id, ch_db_id)
            )
            conn.commit()
            resolved += 1
            print(f"  Saved to DB.")
        elif ch_id and args.dry_run:
            print(f"  [DRY RUN] Would save: {ch_id}")
            resolved += 1

    conn.close()
    print(f"\n{'='*60}")
    print(f"Done. {resolved}/{len(rows)} channel_ids resolved.")
    if resolved > 0 and not args.dry_run:
        print("Scout will use these on the next run — no more resolution step needed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Populate trusted_sources.channel_id for YouTube channels")
    parser.add_argument("--dry-run",     action="store_true", help="Resolve but don't write to DB")
    parser.add_argument("--manual-only", action="store_true", help="Skip auto-resolution, go straight to manual entry")
    args = parser.parse_args()
    run(args)
