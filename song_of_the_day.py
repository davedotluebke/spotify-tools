#!/usr/bin/env python3
"""
Song of the Day - Automatic daily playlist curation.

Ensures exactly one song is added to a "Songs of the Day" playlist each day.
Polls listening history throughout the day, then auto-selects a song at night
if none was manually added.

Usage:
    song_of_the_day.py --poll              # Record listening history (run every 1-5 min)
    song_of_the_day.py --finalize          # Nightly: add song if needed
    song_of_the_day.py --status            # Show today's stats
    song_of_the_day.py --dry-run           # Test finalize without adding

Polling:
    The --poll mode captures listening from two sources:
    1. Recently played history (official Spotify API)
    2. Currently playing track (catches Spotify Jams, etc.)
    
    For best coverage (especially with Jams), run every 1-5 minutes via cron:
        * * * * * /path/to/python /path/to/song_of_the_day.py --poll -q

Configuration:
    Edit ~/.spotify-tools/config.json to customize playlist name, timezone, etc.
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

import pytz

from spotify_auth import (
    get_spotify_client, 
    get_state_dir, 
    get_current_user_display,
    set_profile,
    get_profile,
)


# =============================================================================
# Configuration
# =============================================================================

DEFAULT_CONFIG = {
    "playlist_name": "Dave Songs of the Day 2026",
    "playlist_id": None,  # Populated on first run after lookup
    "timezone": "America/New_York",
    "cooldown_entries": 90,  # Songs can't repeat until 90 others added (0 = no cooldown)
    "min_duration_ms": 50_000,  # 50 seconds minimum
    "selection_mode": "weighted_random",  # "weighted_random" or "most_played"
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
# Email Notifications
# =============================================================================

def send_email(
    config: Dict[str, Any],
    subject: str,
    body: str,
    html_body: Optional[str] = None
) -> bool:
    """
    Send an email notification.
    
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
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
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
        current = sp.current_playback()
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
    results = sp.current_user_recently_played(limit=50)
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


def get_playlist_id(sp, config: Dict[str, Any]) -> Optional[str]:
    """
    Get the playlist ID, looking it up by name if not cached.
    
    Caches the ID in config.json for future runs.
    Returns None if playlist not found.
    """
    # Use cached ID if available
    if config.get("playlist_id"):
        return config["playlist_id"]
    
    # Look up by name
    playlist_name = config.get("playlist_name", "")
    if not playlist_name:
        return None
    
    playlist = find_playlist_by_name(sp, playlist_name)
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
        - "most_played": Always pick the single most-played track
    
    1. Rank by play count (descending), then by most recent play
    2. For weighted_random: take top N, select randomly weighted by play count
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
    
    # For "weighted_random" mode (default)
    # Take top N
    top_candidates = ranked[:top_n]
    
    if len(top_candidates) == 1:
        return top_candidates[0]
    
    # Weighted random selection
    weights = [play_counts.get(t["track_id"], 1) for t in top_candidates]
    selected = random.choices(top_candidates, weights=weights, k=1)[0]
    
    return selected


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
    verbose: bool = True
) -> Optional[Dict[str, Any]]:
    """
    Select a song to add to the playlist.
    
    Uses fallback cascade:
    1. Today's listening
    2. Last 2 days
    3. Last 3 days
    4. Last week
    5. Liked Songs
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    
    cooldown = config.get("cooldown_entries", 90)
    min_duration = config.get("min_duration_ms", 50_000)
    selection_mode = config.get("selection_mode", "weighted_random")
    
    cooldown_ids = get_cooldown_track_ids(snapshot, cooldown)
    
    if verbose:
        if cooldown > 0:
            print(f"  Cooldown: {len(cooldown_ids)} tracks in last {cooldown} entries")
        else:
            print(f"  Cooldown: disabled (repeats allowed)")
        print(f"  Selection mode: {selection_mode}")
    
    # Fallback cascade
    fallback_levels = [
        ("today", [today]),
        ("last 2 days", [today, today - timedelta(days=1)]),
        ("last 3 days", [today - timedelta(days=i) for i in range(3)]),
        ("last week", [today - timedelta(days=i) for i in range(7)]),
    ]
    
    for level_name, days in fallback_levels:
        if verbose:
            print(f"\n  Trying {level_name}...")
        
        candidates, play_counts = get_candidates_from_days(
            days, cooldown_ids, min_duration, verbose=False
        )
        
        if verbose:
            print(f"    Found {len(candidates)} eligible candidates")
        
        if candidates:
            selected = select_song_from_candidates(
                candidates, play_counts, selection_mode=selection_mode
            )
            if selected:
                count = play_counts.get(selected["track_id"], 1)
                if verbose:
                    print(f"    Selected: {selected['track_name']} — {selected['artist']} ({count} plays)")
                return selected
    
    # Final fallback: Liked Songs
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
        return selected
    
    if verbose:
        print(f"  ❌ No eligible songs found anywhere!")
    
    return None


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
    Finalize the day: check if a song was added, and auto-add if not.
    
    Returns exit code: 0 for success, 1 for error.
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    now = datetime.now(tz)
    
    if verbose:
        print(f"Finalizing {today} at {now.strftime('%H:%M')} {config['timezone']}")
        if dry_run:
            print("  (DRY RUN - no changes will be made)\n")
        else:
            print()
    
    # Check playlist state
    playlist_id = get_playlist_id(sp, config)
    if not playlist_id:
        print(f"❌ Playlist '{config['playlist_name']}' not found!", file=sys.stderr)
        return 1
    
    if verbose:
        print(f"Playlist: {config['playlist_name']}")
        print(f"Playlist ID: {playlist_id}")
    
    # Detect if song was already added today
    detection = detect_daily_addition(sp, config, verbose=verbose)
    
    if detection.get("error"):
        print(f"❌ Error: {detection['error']}", file=sys.stderr)
        return 1
    
    if detection["added_today"]:
        if verbose:
            print(f"\n✅ Song already added today:")
            for t in detection["todays_tracks"]:
                print(f"   → {t['track_name']} — {t['artist']}")
            print(f"\nNo action needed.")
        
        # Record user additions (if not already recorded)
        for t in detection["todays_tracks"]:
            record_addition(
                track_id=t["track_id"],
                track_name=t["track_name"],
                artist=t["artist"],
                source="user",
                date_added=today,
            )
        
        return 0
    
    # Need to select and add a song
    if verbose:
        print(f"\n❌ No song added today. Selecting one...")
    
    snapshot = detection["current_snapshot"]
    selected = select_song(sp, config, snapshot, verbose=verbose)
    
    if not selected:
        print(f"\n❌ Could not find any eligible song to add!", file=sys.stderr)
        return 1
    
    if verbose:
        print(f"\n🎵 Selected: {selected['track_name']} — {selected['artist']}")
    
    if dry_run:
        print(f"\n(DRY RUN) Would add: {selected['track_name']} — {selected['artist']}")
        return 0
    
    # Actually add the track
    if verbose:
        print(f"  Adding to playlist...")
    
    success = add_track_to_playlist(sp, playlist_id, selected["track_id"])
    
    if success:
        if verbose:
            print(f"\n✅ Successfully added: {selected['track_name']} — {selected['artist']}")
        
        # Record this auto-addition
        record_addition(
            track_id=selected["track_id"],
            track_name=selected["track_name"],
            artist=selected["artist"],
            source="auto",
            date_added=today,
        )
        
        # Update snapshot to reflect the new track
        take_playlist_snapshot(sp, config)
        
        return 0
    else:
        return 1


# =============================================================================
# Status Display
# =============================================================================

def show_status(sp, config: Dict[str, Any]) -> None:
    """Show current status: today's listening, playlist state, etc."""
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    now = datetime.now(tz)
    
    print(f"\n{'='*60}")
    print(f"Song of the Day Status — {today} {now.strftime('%H:%M')} {config['timezone']}")
    print(f"{'='*60}\n")
    
    # Config info
    print(f"Playlist: {config['playlist_name']}")
    if config.get("playlist_id"):
        print(f"Playlist ID: {config['playlist_id']}")
    print(f"Cooldown: {config['cooldown_entries']} entries")
    print(f"Min duration: {config['min_duration_ms'] // 1000}s")
    
    # === Playlist state ===
    print(f"\n--- Playlist State ---")
    playlist_id = get_playlist_id(sp, config)
    if not playlist_id:
        print(f"⚠️  Playlist '{config['playlist_name']}' not found!")
        print(f"   Create the playlist in Spotify first.")
    else:
        # Check for additions
        detection = detect_daily_addition(sp, config, verbose=False)
        snapshot = detection.get("current_snapshot")
        
        if snapshot:
            print(f"Total tracks: {snapshot['track_count']}")
            
            if detection["added_today"]:
                print(f"✅ Song added today: Yes ({len(detection['todays_tracks'])} track(s))")
                for t in detection["todays_tracks"]:
                    print(f"   → {t['track_name']} — {t['artist']}")
            else:
                print(f"❌ Song added today: No")
                print(f"   (Will auto-add at finalize time)")
            
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

def generate_weekly_summary(config: Dict[str, Any], verbose: bool = True) -> Tuple[str, str]:
    """
    Generate a weekly summary of songs added.
    
    Returns (plain_text, html) versions of the summary.
    """
    tz = pytz.timezone(config["timezone"])
    today = get_today(tz)
    
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
        "<html><body>",
        "<h2>🎵 Song of the Day — Weekly Summary</h2>",
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
    
    if verbose:
        print("Generating weekly summary...")
    
    plain_text, html_text = generate_weekly_summary(config, verbose=verbose)
    
    if verbose:
        print("\n" + plain_text + "\n")
    
    if not config.get("email_enabled"):
        if verbose:
            print("Email not enabled. To send this summary, configure email settings in config.json")
        return 0
    
    subject = f"🎵 Song of the Day — Weekly Summary ({today.strftime('%b %d')})"
    
    if verbose:
        print(f"Sending email to {config.get('email_to')}...")
    
    success = send_email(config, subject, plain_text, html_text)
    
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

