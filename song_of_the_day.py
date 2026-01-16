#!/usr/bin/env python3
"""
Song of the Day - Automatic daily playlist curation.

Maintains a "Songs of the Day" playlist where the song count matches the day of year:
- After Jan 1 → 1 song
- After Jan 3 → 3 songs
- After Dec 31 → 365 songs

Manual additions count toward the target. Deleted songs get replaced automatically.
Sends a nightly email report (if configured).

Usage:
    song_of_the_day.py --poll              # Record listening history (run every 1-5 min)
    song_of_the_day.py --finalize          # Nightly: ensure correct song count
    song_of_the_day.py --status            # Show playlist status and targets
    song_of_the_day.py --dry-run           # Test finalize without adding

Polling:
    The --poll mode captures listening from two sources:
    1. Recently played history (official Spotify API)
    2. Currently playing track (catches Spotify Jams, etc.)
    
    For best coverage (especially with Jams), run every 1-5 minutes via cron:
        * * * * * /path/to/python /path/to/song_of_the_day.py --poll -q

Configuration:
    Edit ~/.spotify-tools/config.json to customize playlist name, timezone, etc.
    Key settings:
    - day_boundary_hour: When a new day starts (default 4am, for night owls)
    - year_start_date: First day of playlist year (inferred from playlist name)
"""
from __future__ import annotations

import argparse
import json
import random
import smtplib
import ssl
import sys
import traceback
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import time

import pytz
import requests

from spotify_auth import (
    get_spotify_client, 
    get_state_dir, 
    get_current_user_display,
    set_profile,
    get_profile,
)


def retry_on_timeout(func, retries: int = 3, delay: float = 2.0):
    """
    Retry a function on transient network errors (timeouts, connection errors).
    
    Returns the function result, or raises the last exception if all retries fail.
    """
    last_exception = None
    for attempt in range(retries):
        try:
            return func()
        except (requests.exceptions.Timeout, 
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            last_exception = e
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))  # Exponential backoff
                continue
            raise
    raise last_exception


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_CONFIG = {
    "playlist_name": "Dave Songs of the Day 2026",
    "playlist_id": None,  # Populated on first run after lookup
    "timezone": "America/New_York",
    "cooldown_entries": 90,  # Songs can't repeat until 90 others added (0 = no cooldown)
    "min_duration_ms": 50_000,  # 50 seconds minimum
    "selection_mode": "strongly_weighted_random",  # "strongly_weighted_random", "weighted_random", or "most_played"
    # Prefer songs liked today: if True, songs added to Liked Songs today are
    # considered first before falling back to listening history
    "prefer_liked_songs": True,
    # Day boundary: hour at which new day starts (for night owls who stay up past midnight)
    # E.g., 4 means the script considers it "yesterday" until 4am
    "day_boundary_hour": 4,
    # Year start date: first day of the playlist year (inferred from playlist name if not set)
    # Format: "YYYY-MM-DD" e.g. "2026-01-01"
    "year_start_date": None,
    # Email settings (optional - notifications disabled if not configured)
    "email_enabled": False,
    "email_to": None,  # Recipient address
    "email_from": None,  # Sender address
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": None,  # Often same as email_from
    "smtp_pass": None,  # App password for Gmail, or regular password
}


def get_config_path() -> Path:
    """Return path to config file."""
    return get_state_dir() / "config.json"


def load_config() -> Dict[str, Any]:
    """Load config from file, creating with defaults if missing."""
    config_path = get_config_path()
    
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        # Merge with defaults for any missing keys
        for key, value in DEFAULT_CONFIG.items():
            if key not in config:
                config[key] = value
        return config
    else:
        # Create default config
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]) -> None:
    """Save config to file."""
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


# =============================================================================
# Target Song Count Calculation
# =============================================================================

import re


def get_year_start_date(config: Dict[str, Any]) -> date:
    """
    Get the date that corresponds to day 1 of the playlist.
    
    Priority:
    1. Explicit year_start_date in config
    2. Year parsed from playlist name (e.g., "Songs of the Day 2026" → Jan 1, 2026)
    3. Current year
    """
    # Check for explicit config
    if config.get("year_start_date"):
        return date.fromisoformat(config["year_start_date"])
    
    # Try to infer from playlist name (look for 4-digit year starting with 20)
    playlist_name = config.get("playlist_name", "")
    match = re.search(r'\b(20\d{2})\b', playlist_name)
    if match:
        year = int(match.group(1))
        return date(year, 1, 1)
    
    # Default to current year
    tz = pytz.timezone(config.get("timezone", "America/New_York"))
    return date(datetime.now(tz).year, 1, 1)


def get_effective_date(config: Dict[str, Any]) -> date:
    """
    Get the "effective date" for playlist targeting.
    
    If running before day_boundary_hour (default 4am), considers it
    still "yesterday" for targeting purposes. This handles night owls
    who stay up past midnight.
    """
    tz = pytz.timezone(config["timezone"])
    now = datetime.now(tz)
    
    day_boundary_hour = config.get("day_boundary_hour", 4)
    
    # If before boundary hour, treat as previous day
    if now.hour < day_boundary_hour:
        return now.date() - timedelta(days=1)
    else:
        return now.date()


def get_target_song_count(config: Dict[str, Any]) -> int:
    """
    Calculate target number of songs based on day of year.
    
    Returns the number of songs the playlist should have after the
    effective date. E.g., after Jan 3 → 3 songs.
    """
    effective_date = get_effective_date(config)
    year_start = get_year_start_date(config)
    
    # Calculate days since year start (1-indexed)
    # Jan 1 = day 1 → 1 song
    # Jan 2 = day 2 → 2 songs
    # etc.
    days_elapsed = (effective_date - year_start).days + 1
    
    return max(0, days_elapsed)


# =============================================================================
# Email Notifications
# =============================================================================

def send_email(
    config: Dict[str, Any],
    subject: str,
    body: str,
    html_body: Optional[str] = None,
    from_name: Optional[str] = None,
) -> bool:
    """
    Send an email notification.
    
    Args:
        from_name: Display name for the sender (e.g., "Song of the Day")
    
    Returns True on success, False on failure (logs error but doesn't raise).
    """
    if not config.get("email_enabled"):
        return False
    
    required = ["email_to", "email_from", "smtp_host", "smtp_user", "smtp_pass"]
    missing = [k for k in required if not config.get(k)]
    if missing:
        print(f"  Email not configured (missing: {', '.join(missing)})", file=sys.stderr)
        return False
    
    try:
        from email.utils import formataddr
        
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        
        # Set From with optional display name
        if from_name:
            msg["From"] = formataddr((from_name, config["email_from"]))
        else:
            msg["From"] = config["email_from"]
        
        msg["To"] = config["email_to"]
        
        # Plain text version
        msg.attach(MIMEText(body, "plain"))
        
        # HTML version (optional)
        if html_body:
            msg.attach(MIMEText(html_body, "html"))
        
        # Connect and send
        context = ssl.create_default_context()
        with smtplib.SMTP(config["smtp_host"], config.get("smtp_port", 587)) as server:
            server.starttls(context=context)
            server.login(config["smtp_user"], config["smtp_pass"])
            server.sendmail(config["email_from"], config["email_to"], msg.as_string())
        
        return True
    
    except Exception as e:
        print(f"  Failed to send email: {e}", file=sys.stderr)
        return False


def send_failure_email(config: Dict[str, Any], error: str, context: str = "") -> None:
    """Send an email notification about a script failure."""
    tz = pytz.timezone(config.get("timezone", "America/New_York"))
    now = datetime.now(tz)
    
    subject = f"🚨 Song of the Day Failed — {now.strftime('%Y-%m-%d')}"
    
    body = f"""Song of the Day script failed at {now.strftime('%Y-%m-%d %H:%M %Z')}

Context: {context or 'Unknown'}

Error:
{error}

Please check the logs and fix the issue.
"""
    
    send_email(config, subject, body)


def send_nightly_email(
    config: Dict[str, Any],
    effective_date: date,
    playlist_count_before: int,
    target_count: int,
    songs_added: List[Dict[str, Any]],
    playlist_count_after: int,
    recent_tracks: List[Dict[str, Any]],
    dry_run: bool = False,
    error_message: Optional[str] = None,
    profile_name: Optional[str] = None,
    liked_today_candidates: Optional[List[Dict[str, Any]]] = None,
    listened_candidates: Optional[List[Dict[str, Any]]] = None,
    play_counts: Optional[Dict[str, int]] = None,
    all_listened_songs: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """
    Send a nightly summary email after finalize runs.
    
    Subject format: "SotD [<profile>]: Added <song> by <artist>"
    Body shows last 5 songs with 🤖/👤 icons, candidates considered, and all listened songs.
    
    Args:
        recent_tracks: Last N tracks from the playlist (most recent last)
        error_message: If set, indicates an error occurred
        profile_name: Profile name (omit from subject if "default")
        liked_today_candidates: Songs liked today that were eligible candidates
        listened_candidates: Songs from listening history that were eligible candidates
        play_counts: Dict of track_id -> play count
        all_listened_songs: All songs listened to that day
    """
    playlist_name = config.get('playlist_name', 'Song of the Day')
    year_start = get_year_start_date(config)
    day_number = (effective_date - year_start).days + 1
    
    # Format profile prefix for subject
    if profile_name and profile_name != "default":
        profile_prefix = f"SotD [{profile_name}]:"
    else:
        profile_prefix = "SotD:"
    
    # Determine the most recent song and whether it was auto-added
    is_error = error_message or (playlist_count_after < target_count and not songs_added)
    
    if is_error:
        # Error case
        subject = f"🚨 {profile_prefix} Error on Day {day_number}"
    elif songs_added:
        # Script added song(s) - show the last one added
        last_added = songs_added[-1]
        subject = f"🎵 {profile_prefix} Added {last_added['track_name']} by {last_added['artist']}"
    elif recent_tracks:
        # User added song(s) - show the most recent track in playlist
        last_track = recent_tracks[-1]
        subject = f"🎵 {profile_prefix} Added {last_track['track_name']} by {last_track['artist']}"
    else:
        subject = f"🎵 {profile_prefix} Day {day_number}"
    
    # Initialize defaults
    if play_counts is None:
        play_counts = {}
    if liked_today_candidates is None:
        liked_today_candidates = []
    if listened_candidates is None:
        listened_candidates = []
    
    # Load additions log to determine auto vs user-added for recent songs
    additions_log = load_additions_log()
    auto_added_ids = {a["track_id"] for a in additions_log if a.get("source") == "auto"}
    
    # Build plain text body
    lines = [
        f"Song of the Day - Nightly Report",
        f"",
        f"Date: {effective_date.strftime('%A, %B %d, %Y')} (Day {day_number})",
        f"Playlist: {playlist_name}",
        f"",
    ]
    
    if dry_run:
        lines.append("🔍 DRY RUN — No changes made")
        lines.append("")
    
    # Show last 5 songs with icons
    if recent_tracks:
        lines.append(f"{'─' * 50}")
        lines.append("Recent songs:")
        
        # Get last 5 tracks
        display_tracks = recent_tracks[-5:]
        
        for track in display_tracks:
            if track["track_id"] in auto_added_ids:
                icon = "🤖"  # Auto-added
            else:
                icon = "👤"  # User-added
            lines.append(f"  {icon} {track['track_name']} — {track['artist']}")
        
        lines.append(f"{'─' * 50}")
    
    # Show candidates considered - separated by category
    total_candidates = len(liked_today_candidates) + len(listened_candidates)
    if total_candidates > 0 or all_listened_songs:
        lines.append("")
        lines.append(f"{'─' * 50}")
        lines.append(f"Candidates considered ({total_candidates}):")
        
        # Show liked today first
        if liked_today_candidates:
            lines.append(f"\n  ❤️ Liked today ({len(liked_today_candidates)}):")
            # Sort by play count descending
            sorted_liked = sorted(liked_today_candidates, 
                                  key=lambda x: play_counts.get(x["track_id"], 0), reverse=True)
            for track in sorted_liked:
                count = play_counts.get(track["track_id"], 0)
                lines.append(f"    • {track['track_name']} — {track['artist']} ({count})")
        
        # Show listened candidates
        lines.append(f"\n  🎧 Listened today ({len(listened_candidates)}):")
        if listened_candidates:
            # Sort by play count descending
            sorted_listened = sorted(listened_candidates, 
                                     key=lambda x: play_counts.get(x["track_id"], 0), reverse=True)
            for track in sorted_listened[:20]:  # Limit to top 20
                count = play_counts.get(track["track_id"], 0)
                lines.append(f"    • {track['track_name']} — {track['artist']} ({count})")
            if len(sorted_listened) > 20:
                lines.append(f"    ... and {len(sorted_listened) - 20} more")
        else:
            # Explain why there are no listened candidates
            if all_listened_songs:
                lines.append(f"    (none — all filtered by cooldown)")
            else:
                lines.append(f"    (none)")
        
        lines.append(f"{'─' * 50}")
    
    # Show all listened songs (excluding candidates to avoid duplication)
    if all_listened_songs:
        candidate_ids = {c["track_id"] for c in liked_today_candidates}
        candidate_ids.update(c["track_id"] for c in listened_candidates)
        other_songs = [s for s in all_listened_songs if s["track_id"] not in candidate_ids]
        
        if other_songs:
            lines.append("")
            lines.append(f"{'─' * 50}")
            lines.append(f"Other songs listened to today ({len(other_songs)}):")
            # Sort by play count descending
            sorted_songs = sorted(other_songs, key=lambda x: play_counts.get(x["track_id"], 0), reverse=True)
            for track in sorted_songs[:15]:  # Limit to top 15
                count = play_counts.get(track["track_id"], 0)
                lines.append(f"  • {track['track_name']} — {track['artist']} — {count} play{'s' if count != 1 else ''}")
            if len(sorted_songs) > 15:
                lines.append(f"  ... and {len(sorted_songs) - 15} more")
            lines.append(f"{'─' * 50}")
    
    # Only show error info if something went wrong
    if is_error:
        lines.append("")
        if error_message:
            lines.append(f"❌ Error: {error_message}")
        else:
            diff = target_count - playlist_count_after
            lines.append(f"⚠️ Playlist is {diff} song(s) behind schedule (Day {day_number} = {target_count} songs)")
            lines.append(f"   Current count: {playlist_count_after}")
    
    body = "\n".join(lines)
    
    # Build HTML body
    html_lines = [
        "<html><body style='font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;'>",
        f"<h2>🎵 Song of the Day - Nightly Report</h2>",
        f"<p><strong>Date:</strong> {effective_date.strftime('%A, %B %d, %Y')} (Day {day_number})</p>",
        f"<p><strong>Playlist:</strong> {playlist_name}</p>",
    ]
    
    if dry_run:
        html_lines.append("<p><em>🔍 DRY RUN — No changes made</em></p>")
    
    # Show last 5 songs
    if recent_tracks:
        html_lines.append("<hr>")
        html_lines.append("<p><strong>Recent songs:</strong></p>")
        html_lines.append("<table style='border-collapse: collapse;'>")
        
        display_tracks = recent_tracks[-5:]
        
        for track in display_tracks:
            if track["track_id"] in auto_added_ids:
                icon = "🤖"
            else:
                icon = "👤"
            html_lines.append(
                f"<tr><td style='padding: 4px;'>{icon}</td>"
                f"<td style='padding: 4px;'><strong>{track['track_name']}</strong> — {track['artist']}</td></tr>"
            )
        
        html_lines.append("</table>")
        html_lines.append("<hr>")
    
    # Show candidates in HTML - two-column table format
    if total_candidates > 0 or all_listened_songs:
        html_lines.append(f"<p><strong>Candidates considered ({total_candidates}):</strong></p>")
        html_lines.append("<table style='border-collapse: collapse; width: 100%;'>")
        
        # Create two columns: liked today | listened today
        html_lines.append("<tr style='vertical-align: top;'>")
        
        # Left column: Liked today
        html_lines.append("<td style='padding: 8px; width: 50%; border-right: 1px solid #ddd;'>")
        html_lines.append(f"<strong>❤️ Liked today ({len(liked_today_candidates)})</strong><br>")
        if liked_today_candidates:
            sorted_liked = sorted(liked_today_candidates, 
                                  key=lambda x: play_counts.get(x["track_id"], 0), reverse=True)
            for track in sorted_liked:
                count = play_counts.get(track["track_id"], 0)
                html_lines.append(f"• {track['track_name']} — {track['artist']} <span style='color:#666;'>({count})</span><br>")
        else:
            html_lines.append("<span style='color:#999;'>(none)</span>")
        html_lines.append("</td>")
        
        # Right column: Listened today
        html_lines.append("<td style='padding: 8px; width: 50%;'>")
        html_lines.append(f"<strong>🎧 Listened today ({len(listened_candidates)})</strong><br>")
        if listened_candidates:
            sorted_listened = sorted(listened_candidates, 
                                     key=lambda x: play_counts.get(x["track_id"], 0), reverse=True)
            for track in sorted_listened[:20]:
                count = play_counts.get(track["track_id"], 0)
                html_lines.append(f"• {track['track_name']} — {track['artist']} <span style='color:#666;'>({count})</span><br>")
            if len(sorted_listened) > 20:
                html_lines.append(f"<span style='color:#666;'>... and {len(sorted_listened) - 20} more</span>")
        else:
            # Explain why there are no listened candidates
            if all_listened_songs:
                html_lines.append("<span style='color:#999;'>(none — all filtered by cooldown)</span>")
            else:
                html_lines.append("<span style='color:#999;'>(none)</span>")
        html_lines.append("</td>")
        
        html_lines.append("</tr></table>")
        html_lines.append("<hr>")
    
    # Show other listened songs in HTML
    if all_listened_songs:
        candidate_ids = {c["track_id"] for c in liked_today_candidates}
        candidate_ids.update(c["track_id"] for c in listened_candidates)
        other_songs = [s for s in all_listened_songs if s["track_id"] not in candidate_ids]
        
        if other_songs:
            html_lines.append(f"<p><strong>Other songs listened to today ({len(other_songs)}):</strong></p>")
            html_lines.append("<table style='border-collapse: collapse;'>")
            sorted_songs = sorted(other_songs, key=lambda x: play_counts.get(x["track_id"], 0), reverse=True)
            for track in sorted_songs[:15]:
                count = play_counts.get(track["track_id"], 0)
                html_lines.append(
                    f"<tr><td style='padding: 2px;'>•</td>"
                    f"<td style='padding: 2px;'>{track['track_name']} — {track['artist']} "
                    f"<span style='color: #666;'>({count})</span></td></tr>"
                )
            if len(sorted_songs) > 15:
                html_lines.append(f"<tr><td></td><td style='padding: 2px; color: #666;'>... and {len(sorted_songs) - 15} more</td></tr>")
            html_lines.append("</table>")
            html_lines.append("<hr>")
    
    # Error info if needed
    if is_error:
        if error_message:
            html_lines.append(f"<p style='color: red;'>❌ Error: {error_message}</p>")
        else:
            diff = target_count - playlist_count_after
            html_lines.append(f"<p style='color: orange;'>⚠️ Playlist is {diff} song(s) behind schedule</p>")
            html_lines.append(f"<p>Target for Day {day_number}: {target_count} songs<br>Current count: {playlist_count_after}</p>")
    
    html_lines.append("</body></html>")
    html_body = "\n".join(html_lines)
    
    send_email(config, subject, body, html_body, from_name="Song of the Day")


# =============================================================================
# Additions Log (tracks user vs auto-added songs)
# =============================================================================

def get_additions_log_path() -> Path:
    """Return path to the additions log file."""
    return get_state_dir() / "additions.json"


def load_additions_log() -> List[Dict[str, Any]]:
    """Load the additions log, or empty list if not exists."""
    log_path = get_additions_log_path()
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_additions_log(log: List[Dict[str, Any]]) -> None:
    """Save the additions log."""
    log_path = get_additions_log_path()
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


def record_addition(
    track_id: str,
    track_name: str,
    artist: str,
    source: str,  # "user" or "auto"
    date_added: date
) -> None:
    """Record a song addition to the log."""
    log = load_additions_log()
    
    # Avoid duplicates for the same date
    existing = next(
        (e for e in log if e["date"] == date_added.isoformat() and e["track_id"] == track_id),
        None
    )
    if existing:
        return  # Already recorded
    
    log.append({
        "date": date_added.isoformat(),
        "track_id": track_id,
        "track_name": track_name,
        "artist": artist,
        "source": source,
        "recorded_at": datetime.now(pytz.UTC).isoformat(),
    })
    
    save_additions_log(log)


def get_additions_for_period(start_date: date, end_date: date) -> List[Dict[str, Any]]:
    """Get all additions within a date range (inclusive)."""
    log = load_additions_log()
    return [
        e for e in log
        if start_date <= date.fromisoformat(e["date"]) <= end_date
    ]


# =============================================================================
# Daily Listening Log
# =============================================================================

def get_daily_dir() -> Path:
    """Return path to daily logs directory."""
    daily_dir = get_state_dir() / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir


def get_daily_log_path(day: date) -> Path:
    """Return path to the daily log file for a given date."""
    return get_daily_dir() / f"{day.isoformat()}.json"


def load_daily_log(day: date) -> Dict[str, Any]:
    """Load the daily log for a given date, or create empty structure."""
    log_path = get_daily_log_path(day)
    
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            return json.load(f)
    
    return {
        "date": day.isoformat(),
        "last_poll": None,
        "last_current_track_id": None,  # For currently-playing dedup
        "plays": [],
        "play_counts": {},
    }


def save_daily_log(day: date, log: Dict[str, Any]) -> None:
    """Save the daily log for a given date."""
    log_path = get_daily_log_path(day)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)


# =============================================================================
# Polling Logic
# =============================================================================

def parse_played_at(played_at_str: str) -> datetime:
    """Parse Spotify's played_at timestamp (ISO 8601 UTC)."""
    # Spotify returns: "2024-01-15T14:32:00.123Z" or "2024-01-15T14:32:00Z"
    # Handle both with and without milliseconds
    if "." in played_at_str:
        return datetime.strptime(played_at_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=pytz.UTC)
    else:
        return datetime.strptime(played_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=pytz.UTC)


def get_today(tz: pytz.BaseTzInfo) -> date:
    """Get today's date in the specified timezone."""
    return datetime.now(tz).date()


def has_recent_play(log: Dict[str, Any], track_id: str, within_seconds: int = 300) -> bool:
    """
    Check if we've recorded a play of this track within the last N seconds.
    Used to avoid double-counting from both recently-played and currently-playing.
    """
    if not log["plays"]:
        return False
    
    now_utc = datetime.now(pytz.UTC)
    for play in reversed(log["plays"]):  # Check most recent first
        try:
            play_time = parse_played_at(play["played_at"])
            age_seconds = (now_utc - play_time).total_seconds()
            if age_seconds > within_seconds:
                break  # Plays are roughly chronological, no need to check older
            if play["track_id"] == track_id:
                return True
        except (ValueError, KeyError):
            continue
    return False


def poll_currently_playing(sp, config: Dict[str, Any], log: Dict[str, Any], verbose: bool = True) -> int:
    """
    Check what's currently playing and record it if it's a new track.
    
    This captures plays that don't show up in recently-played (e.g., Spotify Jams).
    
    Returns number of new plays added.
    """
    tz = pytz.timezone(config["timezone"])
    now = datetime.now(tz)
    now_utc = datetime.now(pytz.UTC)
    
    try:
        current = retry_on_timeout(lambda: sp.current_playback())
    except Exception as e:
        if verbose:
            print(f"  Warning: Could not fetch current playback: {e}")
        return 0
    
    # Nothing playing or paused
    if not current or not current.get("is_playing"):
        # Clear last track so we record it again if it resumes
        if log.get("last_current_track_id"):
            log["last_current_track_id"] = None
            if verbose:
                print(f"  Currently: Nothing playing")
        return 0
    
    # Get track info
    item = current.get("item")
    if not item:
        return 0
    
    # Skip podcasts/episodes
    if item.get("type") == "episode":
        if verbose:
            print(f"  Currently: Podcast (skipped)")
        return 0
    
    track_id = item.get("id")
    if not track_id:
        return 0  # Local file
    
    track_name = item.get("name", "Unknown")
    artist = ", ".join(a.get("name", "?") for a in item.get("artists", []))
    
    # Check if this is the same track as last poll
    last_track_id = log.get("last_current_track_id")
    if track_id == last_track_id:
        if verbose:
            print(f"  Currently: {track_name} — {artist} (still playing)")
        return 0
    
    # New track! But check if we already have a recent play of it
    # (to avoid double-counting from recently-played endpoint)
    if has_recent_play(log, track_id, within_seconds=300):
        if verbose:
            print(f"  Currently: {track_name} — {artist} (already recorded)")
        log["last_current_track_id"] = track_id
        return 0
    
    # Record the play
    played_at_str = now_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    play_record = {
        "track_id": track_id,
        "track_name": track_name,
        "artist": artist,
        "played_at": played_at_str,
        "duration_ms": item.get("duration_ms", 0),
        "type": item.get("type", "track"),
        "context_type": current.get("context", {}).get("type") if current.get("context") else None,
        "source": "current_playback",  # Mark source for debugging
    }
    
    log["plays"].append(play_record)
    log["last_current_track_id"] = track_id
    
    if verbose:
        print(f"  Currently: {track_name} — {artist} (NEW - recorded)")
    
    return 1


def poll_listening_history(sp, config: Dict[str, Any], verbose: bool = True) -> Dict[str, Any]:
    """
    Fetch recently played tracks AND currently playing, merge into today's log.
    
    Returns the updated daily log.
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    now = datetime.now(tz)
    
    if verbose:
        print(f"Polling listening history for {today} ({config['timezone']})")
    
    # Load existing log
    log = load_daily_log(today)
    
    # Ensure last_current_track_id exists (for older log files)
    if "last_current_track_id" not in log:
        log["last_current_track_id"] = None
    
    existing_played_at = {p["played_at"] for p in log["plays"]}
    
    # === Part 1: Recently played (official history) ===
    # Use retry logic for transient network errors
    results = retry_on_timeout(lambda: sp.current_user_recently_played(limit=50))
    items = results.get("items", [])
    
    if verbose:
        print(f"  Recently played: Fetched {len(items)} tracks from Spotify")
    
    new_from_history = 0
    for item in items:
        played_at_str = item.get("played_at")
        if not played_at_str:
            continue
        
        # Parse and check if this play is from today
        played_at_utc = parse_played_at(played_at_str)
        played_at_local = played_at_utc.astimezone(tz)
        
        if played_at_local.date() != today:
            continue  # Not today's play
        
        if played_at_str in existing_played_at:
            continue  # Already recorded
        
        # Extract track info
        track = item.get("track", {})
        if not track:
            continue
        
        track_id = track.get("id")
        if not track_id:
            continue  # Skip local files
        
        # Build play record
        play_record = {
            "track_id": track_id,
            "track_name": track.get("name", "Unknown"),
            "artist": ", ".join(a.get("name", "?") for a in track.get("artists", [])),
            "played_at": played_at_str,
            "duration_ms": track.get("duration_ms", 0),
            "type": track.get("type", "track"),
            "context_type": item.get("context", {}).get("type") if item.get("context") else None,
            "source": "recently_played",
        }
        
        log["plays"].append(play_record)
        existing_played_at.add(played_at_str)
        new_from_history += 1
    
    if verbose:
        print(f"  Recently played: Added {new_from_history} new plays")
    
    # === Part 2: Currently playing (catches Jams, etc.) ===
    new_from_current = poll_currently_playing(sp, config, log, verbose=verbose)
    
    # === Finalize ===
    # Recompute play counts from all plays
    play_counts: Dict[str, int] = {}
    for play in log["plays"]:
        tid = play["track_id"]
        play_counts[tid] = play_counts.get(tid, 0) + 1
    log["play_counts"] = play_counts
    
    # Update last poll time
    log["last_poll"] = now.isoformat()
    
    # Save
    save_daily_log(today, log)
    
    total_new = new_from_history + new_from_current
    if verbose:
        print(f"  Total: {total_new} new plays (total: {len(log['plays'])} plays today)")
        if play_counts:
            top_tracks = sorted(play_counts.items(), key=lambda x: x[1], reverse=True)[:5]
            print(f"  Top tracks by play count:")
            for tid, count in top_tracks:
                # Find track name
                name = next((p["track_name"] for p in log["plays"] if p["track_id"] == tid), tid)
                artist = next((p["artist"] for p in log["plays"] if p["track_id"] == tid), "")
                print(f"    {count}x - {name} — {artist}")
    
    return log


# =============================================================================
# Playlist Operations
# =============================================================================

def get_snapshot_path() -> Path:
    """Return path to playlist snapshot file."""
    return get_state_dir() / "playlist-snapshot.json"


def find_playlist_by_name(sp, name: str) -> Optional[Dict[str, Any]]:
    """
    Find a playlist by name in the current user's playlists.
    
    Returns the playlist object if found, None otherwise.
    Prefers exact case-insensitive match; falls back to partial match.
    """
    name_lower = name.lower().strip()
    candidate = None
    
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items", []):
            pl_name = pl.get("name", "").lower().strip()
            if pl_name == name_lower:
                return pl  # Exact match
            if candidate is None and name_lower in pl_name:
                candidate = pl  # Partial match fallback
        
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    
    return candidate


def get_playlist_id(sp, config: Dict[str, Any], create_if_missing: bool = True) -> Optional[str]:
    """
    Get the playlist ID, looking it up by name if not cached.
    
    Caches the ID in config.json for future runs.
    If create_if_missing is True and playlist doesn't exist, creates it.
    Returns None if playlist not found and create_if_missing is False.
    """
    # Use cached ID if available
    if config.get("playlist_id"):
        return config["playlist_id"]
    
    # Look up by name
    playlist_name = config.get("playlist_name", "")
    if not playlist_name:
        return None
    
    playlist = find_playlist_by_name(sp, playlist_name)
    
    # Create playlist if it doesn't exist
    if not playlist and create_if_missing:
        print(f"📝 Playlist '{playlist_name}' not found, creating it...")
        user_id = sp.current_user()["id"]
        new_playlist = sp.user_playlist_create(
            user_id,
            playlist_name,
            public=False,
            description="Daily song picks - auto-managed by song_of_the_day.py"
        )
        playlist = new_playlist
        print(f"✅ Created playlist: {playlist_name}")
    
    if not playlist:
        return None
    
    # Cache the ID
    config["playlist_id"] = playlist["id"]
    save_config(config)
    
    return playlist["id"]


def fetch_playlist_tracks(sp, playlist_id: str) -> List[Dict[str, Any]]:
    """
    Fetch all tracks from a playlist (handles pagination).
    
    Returns list of track info dicts with: track_id, track_name, artist, added_at, position
    """
    tracks = []
    position = 0
    
    results = sp.playlist_items(
        playlist_id,
        additional_types=("track",),
        fields="items(added_at,track(id,name,artists(name),duration_ms,type)),next",
        limit=100,
    )
    
    while results:
        for item in results.get("items", []):
            track = item.get("track")
            if not track or not track.get("id"):
                position += 1
                continue  # Skip local files or unavailable tracks
            
            tracks.append({
                "track_id": track["id"],
                "track_name": track.get("name", "Unknown"),
                "artist": ", ".join(a.get("name", "?") for a in track.get("artists", [])),
                "added_at": item.get("added_at", ""),
                "duration_ms": track.get("duration_ms", 0),
                "position": position,
            })
            position += 1
        
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    
    return tracks


def load_playlist_snapshot() -> Optional[Dict[str, Any]]:
    """Load the playlist snapshot from disk, or None if not exists."""
    snapshot_path = get_snapshot_path()
    if snapshot_path.exists():
        with open(snapshot_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_playlist_snapshot(snapshot: Dict[str, Any]) -> None:
    """Save the playlist snapshot to disk."""
    snapshot_path = get_snapshot_path()
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def take_playlist_snapshot(sp, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Fetch current playlist state and save as snapshot.
    
    Returns the snapshot dict, or None if playlist not found.
    """
    playlist_id = get_playlist_id(sp, config)
    if not playlist_id:
        return None
    
    tz = pytz.timezone(config["timezone"])
    now = datetime.now(tz)
    
    tracks = fetch_playlist_tracks(sp, playlist_id)
    
    snapshot = {
        "playlist_id": playlist_id,
        "playlist_name": config.get("playlist_name", ""),
        "last_checked": now.isoformat(),
        "track_count": len(tracks),
        "tracks": tracks,
    }
    
    save_playlist_snapshot(snapshot)
    return snapshot


def detect_daily_addition(
    sp, 
    config: Dict[str, Any], 
    verbose: bool = True
) -> Dict[str, Any]:
    """
    Detect if a song was added to the playlist today.
    
    Checks both:
    1. Tracks added since the last snapshot (new tracks)
    2. Any tracks in the playlist with today's added_at date
    
    Returns dict with:
        - added_today: bool - whether a new song was added today
        - todays_tracks: list - all tracks added today
        - needs_song: bool - whether we need to auto-add a song
        - current_snapshot: dict - the current playlist state
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    
    # Take fresh snapshot
    new_snapshot = take_playlist_snapshot(sp, config)
    if not new_snapshot:
        return {
            "added_today": False,
            "todays_tracks": [],
            "needs_song": True,
            "error": "Playlist not found",
            "current_snapshot": None,
        }
    
    # Check ALL tracks for today's date (not just new ones since last snapshot)
    todays_tracks = []
    for track in new_snapshot["tracks"]:
        added_at = track.get("added_at", "")
        if added_at:
            try:
                # Parse added_at (ISO 8601)
                added_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
                added_local = added_dt.astimezone(tz)
                if added_local.date() == today:
                    todays_tracks.append(track)
                    if verbose:
                        print(f"  Found song added today: {track['track_name']} — {track['artist']}")
            except (ValueError, TypeError):
                pass
    
    added_today = len(todays_tracks) > 0
    needs_song = not added_today
    
    if verbose:
        old_snapshot = load_playlist_snapshot()
        if old_snapshot:
            old_count = old_snapshot.get("track_count", 0)
            new_count = new_snapshot["track_count"]
            if new_count > old_count:
                print(f"  Playlist grew: {old_count} → {new_count} tracks")
            elif new_count < old_count:
                print(f"  Playlist shrunk: {old_count} → {new_count} tracks")
    
    return {
        "added_today": added_today,
        "todays_tracks": todays_tracks,
        "needs_song": needs_song,
        "current_snapshot": new_snapshot,
    }


# =============================================================================
# Song Selection Algorithm
# =============================================================================

def get_cooldown_track_ids(snapshot: Dict[str, Any], cooldown: int) -> set:
    """
    Get track IDs that are in the cooldown period (last N entries).
    
    These tracks are not eligible for selection.
    """
    tracks = snapshot.get("tracks", [])
    # Get last N tracks by position
    cooldown_tracks = tracks[-cooldown:] if len(tracks) >= cooldown else tracks
    return {t["track_id"] for t in cooldown_tracks}


def is_eligible(
    track: Dict[str, Any], 
    cooldown_ids: set, 
    min_duration_ms: int
) -> Tuple[bool, str]:
    """
    Check if a track is eligible for selection.
    
    Returns (is_eligible, reason) tuple.
    """
    track_id = track["track_id"]
    
    # Rule 1: Not in cooldown period
    if track_id in cooldown_ids:
        return False, "in cooldown (recently added to playlist)"
    
    # Rule 2: Not a podcast episode
    if track.get("type") == "episode":
        return False, "podcast episode"
    
    # Rule 3: Meets minimum duration
    duration = track.get("duration_ms", 0)
    if duration < min_duration_ms:
        return False, f"too short ({duration // 1000}s < {min_duration_ms // 1000}s)"
    
    return True, "eligible"


def get_candidates_from_days(
    days: List[date], 
    cooldown_ids: set, 
    min_duration_ms: int,
    verbose: bool = True
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Build candidate pool from listening history over multiple days.
    
    Returns (candidates, play_counts) where:
    - candidates: list of unique eligible tracks
    - play_counts: dict of track_id -> total play count across all days
    """
    seen_tracks: Dict[str, Dict[str, Any]] = {}  # track_id -> track info
    total_play_counts: Dict[str, int] = {}
    
    for day in days:
        log = load_daily_log(day)
        
        for play in log.get("plays", []):
            track_id = play["track_id"]
            
            # Accumulate play counts
            total_play_counts[track_id] = total_play_counts.get(track_id, 0) + 1
            
            # Store track info (use most recent play's info)
            if track_id not in seen_tracks:
                seen_tracks[track_id] = play
    
    # Filter to eligible tracks
    candidates = []
    for track_id, track in seen_tracks.items():
        eligible, reason = is_eligible(track, cooldown_ids, min_duration_ms)
        if eligible:
            candidates.append(track)
        elif verbose:
            print(f"    Skipping {track['track_name']}: {reason}")
    
    # Only include play counts for eligible candidates
    eligible_counts = {
        tid: count 
        for tid, count in total_play_counts.items() 
        if tid in {c["track_id"] for c in candidates}
    }
    
    return candidates, eligible_counts


def select_song_from_candidates(
    candidates: List[Dict[str, Any]], 
    play_counts: Dict[str, int],
    selection_mode: str = "weighted_random",
    top_n: int = 5
) -> Optional[Dict[str, Any]]:
    """
    Select a song from candidates based on selection mode.
    
    Modes:
        - "weighted_random": Top N by play count, weighted random selection
        - "strongly_weighted_random": Like weighted_random but squares play counts,
          heavily favoring songs with more plays
        - "most_played": Always pick the single most-played track
    
    1. Rank by play count (descending), then by most recent play
    2. For weighted modes: take top N, select randomly weighted by play count
    3. For most_played: just return the top one
    """
    if not candidates:
        return None
    
    # Sort by play count descending, then by played_at descending (most recent first)
    def sort_key(track):
        count = play_counts.get(track["track_id"], 1)
        played_at = track.get("played_at", "")
        return (count, played_at)
    
    ranked = sorted(candidates, key=sort_key, reverse=True)
    
    # For "most_played" mode, just return the top track
    if selection_mode == "most_played":
        return ranked[0]
    
    # For weighted random modes
    # Take top N
    top_candidates = ranked[:top_n]
    
    if len(top_candidates) == 1:
        return top_candidates[0]
    
    # Calculate weights based on mode
    weights = [play_counts.get(t["track_id"], 1) for t in top_candidates]
    
    # For strongly_weighted_random, square the weights to heavily favor more-played songs
    if selection_mode == "strongly_weighted_random":
        weights = [w ** 2 for w in weights]
    
    selected = random.choices(top_candidates, weights=weights, k=1)[0]
    
    return selected


def fetch_todays_liked_songs(
    sp, 
    config: Dict[str, Any],
    verbose: bool = True
) -> List[Dict[str, Any]]:
    """
    Fetch songs that were added to Liked Songs today.
    
    Returns tracks liked on the effective date (respects day_boundary_hour).
    Since Liked Songs are sorted newest first, we can stop once we hit
    songs from previous days.
    """
    tz = pytz.timezone(config["timezone"])
    effective_date = get_effective_date(config)
    
    tracks = []
    results = retry_on_timeout(lambda: sp.current_user_saved_tracks(limit=50))
    
    while results:
        found_older = False
        for item in results.get("items", []):
            added_at = item.get("added_at", "")
            if not added_at:
                continue
            
            # Parse the added_at timestamp
            try:
                added_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
                added_local = added_dt.astimezone(tz)
                added_date = added_local.date()
            except (ValueError, TypeError):
                continue
            
            # Check if this was liked today (on the effective date)
            if added_date < effective_date:
                # Songs are sorted newest first, so we can stop here
                found_older = True
                break
            
            if added_date == effective_date:
                track = item.get("track")
                if not track or not track.get("id"):
                    continue
                
                tracks.append({
                    "track_id": track["id"],
                    "track_name": track.get("name", "Unknown"),
                    "artist": ", ".join(a.get("name", "?") for a in track.get("artists", [])),
                    "duration_ms": track.get("duration_ms", 0),
                    "type": track.get("type", "track"),
                    "added_at": added_at,
                    "played_at": added_at,  # For compatibility with play_counts lookup
                })
        
        if found_older:
            break
        
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    
    if verbose and tracks:
        print(f"  Found {len(tracks)} song(s) liked today")
    
    return tracks


def fetch_liked_songs_sample(sp, limit: int = 200) -> List[Dict[str, Any]]:
    """
    Fetch a sample of the user's Liked Songs for fallback selection.
    """
    tracks = []
    results = sp.current_user_saved_tracks(limit=50)
    
    while results and len(tracks) < limit:
        for item in results.get("items", []):
            track = item.get("track")
            if not track or not track.get("id"):
                continue
            
            tracks.append({
                "track_id": track["id"],
                "track_name": track.get("name", "Unknown"),
                "artist": ", ".join(a.get("name", "?") for a in track.get("artists", [])),
                "duration_ms": track.get("duration_ms", 0),
                "type": track.get("type", "track"),
                "played_at": item.get("added_at", ""),  # Use liked date as proxy
            })
        
        if results.get("next") and len(tracks) < limit:
            results = sp.next(results)
        else:
            break
    
    return tracks


def select_song(
    sp,
    config: Dict[str, Any],
    snapshot: Dict[str, Any],
    verbose: bool = True,
    extra_exclude_ids: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """
    Select a song to add to the playlist.
    
    Uses fallback cascade:
    0. Songs liked today (if prefer_liked_songs is enabled)
    1. Today's listening history
    2. Last 2 days
    3. Last 3 days
    4. Last week
    5. Liked Songs (random sample)
    
    Args:
        extra_exclude_ids: Additional track IDs to exclude (e.g., tracks already
                          added in this run when adding multiple songs)
    """
    selected, _, _ = select_song_with_candidates(sp, config, snapshot, verbose, extra_exclude_ids)
    return selected


def select_song_with_candidates(
    sp,
    config: Dict[str, Any],
    snapshot: Dict[str, Any],
    verbose: bool = True,
    extra_exclude_ids: Optional[set] = None,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Select a song to add to the playlist, returning both the selection and candidates.
    
    Uses fallback cascade:
    0. Songs liked today (if prefer_liked_songs is enabled)
    1. Today's listening history
    2. Last 2 days
    3. Last 3 days
    4. Last week
    5. Liked Songs (random sample)
    
    Args:
        extra_exclude_ids: Additional track IDs to exclude (e.g., tracks already
                          added in this run when adding multiple songs)
    
    Returns:
        Tuple of (selected_track, liked_today_candidates, listened_candidates)
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    
    cooldown = config.get("cooldown_entries", 90)
    min_duration = config.get("min_duration_ms", 50_000)
    selection_mode = config.get("selection_mode", "weighted_random")
    prefer_liked = config.get("prefer_liked_songs", True)
    
    cooldown_ids = get_cooldown_track_ids(snapshot, cooldown)
    
    # Add extra exclusions if provided
    if extra_exclude_ids:
        cooldown_ids = cooldown_ids | extra_exclude_ids
    
    if verbose:
        if cooldown > 0:
            print(f"  Cooldown: {len(cooldown_ids)} tracks in last {cooldown} entries")
        else:
            print(f"  Cooldown: disabled (repeats allowed)")
        print(f"  Selection mode: {selection_mode}")
        print(f"  Prefer liked songs: {prefer_liked}")
    
    # Track candidates from each source
    liked_today_candidates: List[Dict[str, Any]] = []
    listened_candidates: List[Dict[str, Any]] = []
    
    # === Priority 0: Songs liked today ===
    if prefer_liked:
        if verbose:
            print(f"\n  Trying songs liked today...")
        
        todays_liked = fetch_todays_liked_songs(sp, config, verbose=False)
        
        # Filter to eligible tracks
        for track in todays_liked:
            eligible, reason = is_eligible(track, cooldown_ids, min_duration)
            if eligible:
                liked_today_candidates.append(track)
        
        if verbose:
            print(f"    Found {len(todays_liked)} liked today, {len(liked_today_candidates)} eligible")
        
        if liked_today_candidates:
            # Get play counts from today's listening history for weighted selection
            log = load_daily_log(today)
            play_counts = log.get("play_counts", {})
            
            selected = select_song_from_candidates(
                liked_today_candidates, play_counts, selection_mode=selection_mode
            )
            if selected:
                count = play_counts.get(selected["track_id"], 0)
                if verbose:
                    plays_str = f"({count} plays)" if count > 0 else "(not played today)"
                    print(f"    Selected: {selected['track_name']} — {selected['artist']} {plays_str}")
                return selected, liked_today_candidates, listened_candidates
    
    # === Fallback cascade: Listening history ===
    # Only use today's listening for the candidates we report
    # (multi-day fallback is just for selection, not for email reporting)
    fallback_levels = [
        ("today's listening", [today], True),  # True = include in listened_candidates
        ("last 2 days", [today, today - timedelta(days=1)], False),
        ("last 3 days", [today - timedelta(days=i) for i in range(3)], False),
        ("last week", [today - timedelta(days=i) for i in range(7)], False),
    ]
    
    for level_name, days, include_in_report in fallback_levels:
        if verbose:
            print(f"\n  Trying {level_name}...")
        
        candidates, play_counts = get_candidates_from_days(
            days, cooldown_ids, min_duration, verbose=False
        )
        
        if verbose:
            print(f"    Found {len(candidates)} eligible candidates")
        
        # Always collect today's listening candidates for reporting
        if include_in_report:
            listened_candidates = candidates.copy()
        
        if candidates:
            selected = select_song_from_candidates(
                candidates, play_counts, selection_mode=selection_mode
            )
            if selected:
                count = play_counts.get(selected["track_id"], 1)
                if verbose:
                    print(f"    Selected: {selected['track_name']} — {selected['artist']} ({count} plays)")
                return selected, liked_today_candidates, listened_candidates
    
    # === Final fallback: Random from Liked Songs ===
    if verbose:
        print(f"\n  Trying Liked Songs fallback...")
    
    liked_songs = fetch_liked_songs_sample(sp, limit=200)
    candidates = []
    for track in liked_songs:
        eligible, _ = is_eligible(track, cooldown_ids, min_duration)
        if eligible:
            candidates.append(track)
    
    if verbose:
        print(f"    Found {len(candidates)} eligible from Liked Songs")
    
    if candidates:
        # Random selection from liked songs (no play count weighting)
        selected = random.choice(candidates)
        if verbose:
            print(f"    Selected: {selected['track_name']} — {selected['artist']}")
        return selected, liked_today_candidates, listened_candidates
    
    if verbose:
        print(f"  ❌ No eligible songs found anywhere!")
    
    return None, liked_today_candidates, listened_candidates


# =============================================================================
# Finalize (Add Song to Playlist)
# =============================================================================

def add_track_to_playlist(sp, playlist_id: str, track_id: str) -> bool:
    """
    Add a track to the end of the playlist.
    
    Returns True on success, False on failure.
    """
    try:
        track_uri = f"spotify:track:{track_id}"
        sp.playlist_add_items(playlist_id, [track_uri])
        return True
    except Exception as e:
        print(f"  ❌ Failed to add track: {e}", file=sys.stderr)
        return False


def finalize_day(
    sp,
    config: Dict[str, Any],
    dry_run: bool = False,
    verbose: bool = True
) -> int:
    """
    Finalize the day: ensure playlist has correct number of songs for day of year.
    
    Logic:
    - Calculate target count (day of year = song count)
    - If playlist count < target, add songs until we reach target
    - If playlist count >= target, do nothing
    
    This approach means:
    - Manual additions count toward the target
    - Deleted songs get replaced
    - If you're behind (e.g., script didn't run), it catches up
    
    Returns exit code: 0 for success, 1 for error.
    """
    tz = pytz.timezone(config["timezone"])
    now = datetime.now(tz)
    today = get_today(tz)
    effective_date = get_effective_date(config)
    target_count = get_target_song_count(config)
    year_start = get_year_start_date(config)
    day_number = (effective_date - year_start).days + 1
    profile_name = get_profile()
    
    if verbose:
        print(f"Finalizing for {effective_date} (Day {day_number}) at {now.strftime('%H:%M')} {config['timezone']}")
        if dry_run:
            print("  (DRY RUN - no changes will be made)\n")
        else:
            print()
    
    # Get playlist
    playlist_id = get_playlist_id(sp, config)
    if not playlist_id:
        print(f"❌ Playlist '{config['playlist_name']}' not found!", file=sys.stderr)
        return 1
    
    if verbose:
        print(f"Playlist: {config['playlist_name']}")
        print(f"Playlist ID: {playlist_id}")
    
    # Get current playlist state
    snapshot = take_playlist_snapshot(sp, config)
    if not snapshot:
        print(f"❌ Could not fetch playlist!", file=sys.stderr)
        return 1
    
    # Record any user-added songs (tracks added today that aren't in additions log)
    existing_additions = load_additions_log()
    existing_today_ids = {
        e["track_id"] for e in existing_additions 
        if e.get("date") == effective_date.isoformat()
    }
    
    for track in snapshot.get("tracks", []):
        added_at = track.get("added_at", "")
        if added_at:
            try:
                added_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
                added_local = added_dt.astimezone(tz)
                if added_local.date() == effective_date and track["track_id"] not in existing_today_ids:
                    # This track was added today but not recorded - it's a user addition
                    record_addition(
                        track_id=track["track_id"],
                        track_name=track["track_name"],
                        artist=track["artist"],
                        source="user",
                        date_added=effective_date,
                    )
                    if verbose:
                        print(f"  📝 Recorded user addition: {track['track_name']} — {track['artist']}")
            except (ValueError, TypeError):
                pass
    
    playlist_count_before = snapshot["track_count"]
    songs_needed = target_count - playlist_count_before
    
    if verbose:
        print(f"\nPlaylist count: {playlist_count_before}")
        print(f"Target count (Day {day_number}): {target_count}")
        print(f"Songs needed: {max(0, songs_needed)}")
    
    # Load today's listening history for email info
    daily_log = load_daily_log(today)
    play_counts = daily_log.get("play_counts", {})
    
    # Build list of all unique listened songs for email
    seen_tracks: Dict[str, Dict[str, Any]] = {}
    for play in daily_log.get("plays", []):
        track_id = play["track_id"]
        if track_id not in seen_tracks:
            seen_tracks[track_id] = play
    all_listened_songs = list(seen_tracks.values())
    
    # Track what we add and candidates considered
    songs_added: List[Dict[str, Any]] = []
    all_liked_candidates: List[Dict[str, Any]] = []
    all_listened_candidates: List[Dict[str, Any]] = []
    extra_exclude_ids: set = set()
    
    if songs_needed <= 0:
        if verbose:
            if songs_needed == 0:
                print(f"\n✅ Playlist is exactly on track!")
            else:
                print(f"\n✅ Playlist is {-songs_needed} song(s) ahead of schedule")
            print(f"No action needed.")
        
        # Even if no songs needed, still collect candidates for email reporting
        # (shows what would have been selected if needed)
        _, liked_candidates, listened_candidates = select_song_with_candidates(
            sp, config, snapshot, 
            verbose=False, 
            extra_exclude_ids=extra_exclude_ids
        )
        all_liked_candidates = liked_candidates
        all_listened_candidates = listened_candidates
    else:
        if verbose:
            print(f"\n🎵 Need to add {songs_needed} song(s)...")
        
        # Add songs one at a time until we reach target or run out of candidates
        for i in range(songs_needed):
            if verbose:
                print(f"\n--- Selecting song {i + 1} of {songs_needed} ---")
            
            # Re-fetch snapshot if we've added songs (to update cooldown correctly)
            if songs_added and not dry_run:
                snapshot = take_playlist_snapshot(sp, config)
            
            selected, liked_candidates, listened_candidates = select_song_with_candidates(
                sp, config, snapshot, 
                verbose=verbose, 
                extra_exclude_ids=extra_exclude_ids
            )
            
            # Collect liked candidates (avoid duplicates)
            existing_liked_ids = {c["track_id"] for c in all_liked_candidates}
            for c in liked_candidates:
                if c["track_id"] not in existing_liked_ids:
                    all_liked_candidates.append(c)
                    existing_liked_ids.add(c["track_id"])
            
            # Collect listened candidates (avoid duplicates)
            existing_listened_ids = {c["track_id"] for c in all_listened_candidates}
            for c in listened_candidates:
                if c["track_id"] not in existing_listened_ids:
                    all_listened_candidates.append(c)
                    existing_listened_ids.add(c["track_id"])
            
            if not selected:
                print(f"\n⚠️ Could not find eligible song #{i + 1}. Stopping.", file=sys.stderr)
                break
            
            if verbose:
                print(f"\n🎵 Selected: {selected['track_name']} — {selected['artist']}")
            
            if dry_run:
                print(f"  (DRY RUN) Would add: {selected['track_name']} — {selected['artist']}")
                songs_added.append(selected)
                extra_exclude_ids.add(selected["track_id"])
            else:
                if verbose:
                    print(f"  Adding to playlist...")
                
                success = add_track_to_playlist(sp, playlist_id, selected["track_id"])
                
                if success:
                    if verbose:
                        print(f"  ✅ Added: {selected['track_name']} — {selected['artist']}")
                    
                    songs_added.append(selected)
                    extra_exclude_ids.add(selected["track_id"])
                    
                    # Record this auto-addition
                    record_addition(
                        track_id=selected["track_id"],
                        track_name=selected["track_name"],
                        artist=selected["artist"],
                        source="auto",
                        date_added=effective_date,
                    )
                else:
                    print(f"  ❌ Failed to add track!", file=sys.stderr)
                    break
    
    # Get final snapshot (always, for email recent tracks)
    if not dry_run:
        final_snapshot = take_playlist_snapshot(sp, config)
        if final_snapshot:
            playlist_count_after = final_snapshot["track_count"]
            recent_tracks = final_snapshot.get("tracks", [])
        else:
            playlist_count_after = playlist_count_before + len(songs_added)
            recent_tracks = snapshot.get("tracks", [])
    else:
        playlist_count_after = playlist_count_before + len(songs_added)
        # For dry run, simulate the added songs in the track list
        recent_tracks = list(snapshot.get("tracks", []))
        for song in songs_added:
            recent_tracks.append({
                "track_id": song["track_id"],
                "track_name": song["track_name"],
                "artist": song["artist"],
            })
    
    # Summary
    if verbose:
        print(f"\n{'=' * 50}")
        print(f"Summary:")
        print(f"  Before: {playlist_count_before} songs")
        print(f"  Added: {len(songs_added)} song(s)")
        print(f"  After: {playlist_count_after} songs")
        print(f"  Target: {target_count} songs")
        
        if playlist_count_after >= target_count:
            diff = playlist_count_after - target_count
            if diff == 0:
                print(f"\n✅ Playlist is exactly on track!")
            else:
                print(f"\n✅ Playlist is {diff} song(s) ahead of schedule")
        else:
            diff = target_count - playlist_count_after
            print(f"\n⚠️ Playlist is still {diff} song(s) behind schedule")
    
    # Send nightly email
    send_nightly_email(
        config=config,
        effective_date=effective_date,
        playlist_count_before=playlist_count_before,
        target_count=target_count,
        songs_added=songs_added,
        playlist_count_after=playlist_count_after,
        recent_tracks=recent_tracks,
        dry_run=dry_run,
        profile_name=profile_name,
        liked_today_candidates=all_liked_candidates,
        listened_candidates=all_listened_candidates,
        play_counts=play_counts,
        all_listened_songs=all_listened_songs,
    )
    
    # Return success if we're at or above target, or if we added all we could
    if playlist_count_after >= target_count:
        return 0
    elif songs_added:
        # We added some but couldn't reach target - partial success
        return 0
    elif songs_needed > 0:
        # We needed songs but couldn't add any
        return 1
    else:
        return 0


# =============================================================================
# Status Display
# =============================================================================

def show_status(sp, config: Dict[str, Any]) -> None:
    """Show current status: today's listening, playlist state, etc."""
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    now = datetime.now(tz)
    effective_date = get_effective_date(config)
    target_count = get_target_song_count(config)
    year_start = get_year_start_date(config)
    day_number = (effective_date - year_start).days + 1
    
    print(f"\n{'='*60}")
    print(f"Song of the Day Status — {effective_date} (Day {day_number})")
    print(f"Current time: {now.strftime('%H:%M')} {config['timezone']}")
    print(f"{'='*60}\n")
    
    # Config info
    print(f"Playlist: {config['playlist_name']}")
    if config.get("playlist_id"):
        print(f"Playlist ID: {config['playlist_id']}")
    print(f"Year start: {year_start}")
    print(f"Day boundary: {config.get('day_boundary_hour', 4)}:00 (new day starts at this hour)")
    print(f"Cooldown: {config['cooldown_entries']} entries")
    print(f"Min duration: {config['min_duration_ms'] // 1000}s")
    
    # === Playlist state ===
    print(f"\n--- Playlist State ---")
    playlist_id = get_playlist_id(sp, config)
    if not playlist_id:
        print(f"⚠️  Playlist '{config['playlist_name']}' not found!")
        print(f"   Create the playlist in Spotify first.")
    else:
        # Get current snapshot
        snapshot = take_playlist_snapshot(sp, config)
        
        if snapshot:
            current_count = snapshot['track_count']
            songs_needed = target_count - current_count
            
            print(f"Current tracks: {current_count}")
            print(f"Target (Day {day_number}): {target_count}")
            
            if songs_needed <= 0:
                if songs_needed == 0:
                    print(f"✅ Status: Exactly on track!")
                else:
                    print(f"✅ Status: {-songs_needed} song(s) ahead of schedule")
            else:
                print(f"⚠️ Status: {songs_needed} song(s) behind — will add at finalize")
            
            # Show last few tracks in playlist
            if snapshot["tracks"]:
                print(f"\nLast 5 tracks in playlist:")
                for track in snapshot["tracks"][-5:]:
                    added = track.get("added_at", "")[:10]  # Just the date
                    print(f"  [{added}] {track['track_name']} — {track['artist']}")
    
    # === Today's listening ===
    log = load_daily_log(today)
    print(f"\n--- Today's Listening ---")
    print(f"Last poll: {log.get('last_poll', 'Never')}")
    print(f"Total plays: {len(log['plays'])}")
    print(f"Unique tracks: {len(log.get('play_counts', {}))}")
    
    if log.get("play_counts"):
        print(f"\nPlay counts:")
        sorted_counts = sorted(
            log["play_counts"].items(), 
            key=lambda x: x[1], 
            reverse=True
        )
        for tid, count in sorted_counts[:10]:
            name = next((p["track_name"] for p in log["plays"] if p["track_id"] == tid), tid)
            artist = next((p["artist"] for p in log["plays"] if p["track_id"] == tid), "")
            print(f"  {count}x - {name} — {artist}")
        if len(sorted_counts) > 10:
            print(f"  ... and {len(sorted_counts) - 10} more tracks")
    
    print()


# =============================================================================
# Weekly Summary
# =============================================================================

def generate_weekly_summary(
    config: Dict[str, Any], 
    verbose: bool = True,
    profile_name: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Generate a weekly summary of songs added.
    
    Args:
        profile_name: Profile name for display (omit from header if "default")
    
    Returns (plain_text, html) versions of the summary.
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    playlist_name = config.get('playlist_name', 'Song of the Day')
    
    # Get the last 7 days (including today)
    start_date = today - timedelta(days=6)
    end_date = today
    
    additions = get_additions_for_period(start_date, end_date)
    
    # Group by date
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for add in additions:
        d = add["date"]
        if d not in by_date:
            by_date[d] = []
        by_date[d].append(add)
    
    # Count by source
    user_count = sum(1 for a in additions if a.get("source") == "user")
    auto_count = sum(1 for a in additions if a.get("source") == "auto")
    
    # Build plain text
    lines = [
        f"🎵 Song of the Day — Weekly Summary",
        f"",
        f"Playlist: {playlist_name}",
        f"Week of {start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}",
        f"",
        f"Total songs: {len(additions)} ({user_count} manual, {auto_count} auto-picked)",
        f"",
        f"─" * 50,
    ]
    
    # List each day
    for i in range(7):
        day = start_date + timedelta(days=i)
        day_str = day.isoformat()
        day_name = day.strftime("%A, %b %d")
        
        day_additions = by_date.get(day_str, [])
        
        if day_additions:
            for add in day_additions:
                source_icon = "👤" if add.get("source") == "user" else "🤖"
                lines.append(f"{day_name}: {source_icon} {add['track_name']} — {add['artist']}")
        else:
            lines.append(f"{day_name}: (no song)")
    
    lines.append(f"─" * 50)
    lines.append(f"")
    lines.append(f"👤 = manually added, 🤖 = auto-picked")
    
    plain_text = "\n".join(lines)
    
    # Build HTML version
    html_lines = [
        "<html><body style='font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 600px;'>",
        "<h2>🎵 Song of the Day — Weekly Summary</h2>",
        f"<p><strong>Playlist:</strong> {playlist_name}</p>",
        f"<p><strong>Week of {start_date.strftime('%b %d')} – {end_date.strftime('%b %d, %Y')}</strong></p>",
        f"<p>Total songs: {len(additions)} ({user_count} manual, {auto_count} auto-picked)</p>",
        "<hr>",
        "<table style='border-collapse: collapse; width: 100%;'>",
    ]
    
    for i in range(7):
        day = start_date + timedelta(days=i)
        day_str = day.isoformat()
        day_name = day.strftime("%A, %b %d")
        
        day_additions = by_date.get(day_str, [])
        
        if day_additions:
            for add in day_additions:
                source_icon = "👤" if add.get("source") == "user" else "🤖"
                html_lines.append(
                    f"<tr><td style='padding: 4px;'>{day_name}</td>"
                    f"<td style='padding: 4px;'>{source_icon}</td>"
                    f"<td style='padding: 4px;'><strong>{add['track_name']}</strong> — {add['artist']}</td></tr>"
                )
        else:
            html_lines.append(
                f"<tr><td style='padding: 4px;'>{day_name}</td>"
                f"<td colspan='2' style='padding: 4px; color: #999;'>(no song)</td></tr>"
            )
    
    html_lines.extend([
        "</table>",
        "<hr>",
        "<p><small>👤 = manually added, 🤖 = auto-picked</small></p>",
        "</body></html>",
    ])
    
    html_text = "\n".join(html_lines)
    
    return plain_text, html_text


def send_weekly_summary(config: Dict[str, Any], verbose: bool = True) -> int:
    """
    Generate and send the weekly summary email.
    
    Returns exit code: 0 for success, 1 for error.
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    profile_name = get_profile()
    playlist_name = config.get('playlist_name', 'Song of the Day')
    
    if verbose:
        print("Generating weekly summary...")
    
    plain_text, html_text = generate_weekly_summary(config, verbose=verbose, profile_name=profile_name)
    
    if verbose:
        print("\n" + plain_text + "\n")
    
    if not config.get("email_enabled"):
        if verbose:
            print("Email not enabled. To send this summary, configure email settings in config.json")
        return 0
    
    # Format profile prefix for subject
    if profile_name and profile_name != "default":
        profile_prefix = f"SotD [{profile_name}]:"
    else:
        profile_prefix = "SotD:"
    
    subject = f"🎵 {profile_prefix} Weekly Summary {playlist_name}"
    
    if verbose:
        print(f"Sending email to {config.get('email_to')}...")
    
    success = send_email(config, subject, plain_text, html_text, from_name="Song of the Day")
    
    if success:
        if verbose:
            print("✅ Weekly summary email sent!")
        return 0
    else:
        print("❌ Failed to send weekly summary email", file=sys.stderr)
        return 1


# =============================================================================
# Main CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Song of the Day - Automatic daily playlist curation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --poll                       # Record listening history (run every minute)
  %(prog)s --status                     # Poll + show today's stats (always fresh)
  %(prog)s --finalize                   # Add song if none added today (run nightly)
  %(prog)s --dry-run                    # Test finalize without modifying playlist
  %(prog)s --weekly-summary             # Email summary of this week's songs
  %(prog)s --profile dave-auto --poll   # Use a different profile
        """,
    )
    
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--poll",
        action="store_true",
        help="Fetch and record recent listening history",
    )
    mode.add_argument(
        "--finalize",
        action="store_true",
        help="Check if song was added today; if not, auto-select one",
    )
    mode.add_argument(
        "--status",
        action="store_true",
        help="Show today's listening stats and playlist state",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Run finalize logic but don't actually add to playlist",
    )
    mode.add_argument(
        "--weekly-summary",
        action="store_true",
        help="Generate and email a summary of this week's songs",
    )
    
    parser.add_argument(
        "--profile", "-p",
        type=str,
        default=None,
        help="Profile name (default: 'default'). Each profile has its own config and data.",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress non-essential output",
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="For --status: skip implicit poll, show cached data only",
    )
    
    args = parser.parse_args()
    verbose = not args.quiet
    
    # Set profile before anything else
    set_profile(args.profile)
    
    # Initialize
    config = load_config()
    
    if verbose:
        profile_name = get_profile()
        print(f"Profile: {profile_name}")
        print(f"State directory: {get_state_dir()}")
        print(f"Config: {get_config_path()}")
    
    # Authenticate
    sp = get_spotify_client()
    
    if verbose:
        user = get_current_user_display(sp)
        print(f"Authenticated as: {user}\n")
    
    # Execute mode
    if args.poll:
        poll_listening_history(sp, config, verbose=verbose)
        return 0
    
    elif args.status:
        # Implicitly poll first to get fresh data (unless --no-poll)
        if not args.no_poll:
            if verbose:
                print("Refreshing listening data...\n")
            poll_listening_history(sp, config, verbose=False)
        show_status(sp, config)
        return 0
    
    elif args.finalize or args.dry_run:
        return finalize_day(sp, config, dry_run=args.dry_run, verbose=verbose)
    
    elif args.weekly_summary:
        return send_weekly_summary(config, verbose=verbose)
    
    return 0


if __name__ == "__main__":
    import atexit
    import gc
    
    def cleanup():
        """Force garbage collection to avoid spotipy cleanup errors on exit."""
        gc.collect()
    
    atexit.register(cleanup)
    
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)
    except Exception as e:
        # Send failure notification if possible
        error_msg = f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}"
        print(f"\n❌ Unexpected error: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        
        try:
            # Try to load config and send failure email
            config = load_config()
            context = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "unknown"
            send_failure_email(config, error_msg, context=f"Running: {context}")
        except Exception:
            pass  # Don't fail on failure to send email
        
        sys.exit(1)

