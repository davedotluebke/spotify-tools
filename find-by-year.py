#!/usr/bin/env python3
"""
List songs in one of your Spotify playlists whose *album* was released in a given year.

Quick start:
1) pip install spotipy python-dotenv
2) Create a Spotify App at https://developer.spotify.com/dashboard, add a Redirect URI (e.g. http://localhost:8080/callback)
3) Create a .env file alongside this script with:
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   SPOTIFY_REDIRECT_URI=http://localhost:8080/callback
4) Run:  python find-by-year.py "My Playlist Name" --year 2025 --csv results.csv
   Or:   python find-by-year.py "My Playlist Name" (will prompt for year)

Notes:
- Scope needed: playlist-read-private playlist-read-collaborative user-library-read
- The first run opens a browser for login; a token cache ( .cache ) is stored locally.
- Matches tracks whose album.release_date is in the specified year respecting release_date_precision (year/month/day).
"""
from __future__ import annotations
import argparse
import os
import sys
from typing import Dict, List, Optional

from dotenv import load_dotenv
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
]


def load_env() -> None:
    # Load .env if present
    load_dotenv()
    missing = [k for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI") if not os.getenv(k)]
    if missing:
        print(
            "Missing env vars: " + ", ".join(missing) +
            "\nCreate a Spotify app and set them in a .env file or your environment.",
            file=sys.stderr,
        )
        # Don't hard-exit; Spotipy will still error with a clearer message. This is just a heads-up.


def auth_client() -> Spotify:
    oauth = SpotifyOAuth(
        scope=SCOPES,
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        open_browser=True,
        cache_path=os.getenv("SPOTIFY_TOKEN_CACHE", ".cache"),
    )
    return Spotify(auth_manager=oauth)


def find_playlist_by_name(sp: Spotify, name: str) -> Optional[Dict]:
    """Return the best-matching playlist object for the current user by name.
    Prefers case-insensitive exact match; otherwise first partial match.
    """
    name_lower = name.lower().strip()
    next_url = None
    while True:
        page = sp.current_user_playlists(limit=50) if not next_url else sp.next(page)
        for pl in page["items"]:
            if pl.get("name", "").lower().strip() == name_lower:
                return pl
        # If we didn't find an exact match on this page, keep track for partials
        partial = next((pl for pl in page["items"] if name_lower in pl.get("name", "").lower()), None)
        if partial:
            # Keep as a fallback, but continue in case a later page has an exact match
            candidate = partial
        else:
            candidate = None
        if page.get("next"):
            next_url = page["next"]
            continue
        return candidate


def playlist_tracks(sp: Spotify, playlist_id: str) -> List[Dict]:
    """Yield all track items in a playlist (handles pagination)."""
    items: List[Dict] = []
    results = sp.playlist_items(
        playlist_id,
        additional_types=("track",),
        fields=(
            "items(track(id,name,is_local,artists(name),album(name,release_date,release_date_precision))),"
            "next"
        ),
        limit=100,
    )
    while True:
        items.extend(results.get("items", []))
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    return items


def is_album_released_in_year(release_date: Optional[str], precision: Optional[str], year: str) -> bool:
    if not release_date:
        return False
    if precision == "year":
        result = release_date == year
        print(f"    [DEBUG] Year precision: {release_date} == {year} ? {result}")
        return result
    # For month/day precision, the string starts with YYYY-MM or YYYY-MM-DD
    result = release_date.startswith(f"{year}-")
    print(f"    [DEBUG] Date precision: {release_date}.startswith('{year}-') ? {result}")
    return result


def format_artists(artists: List[Dict]) -> str:
    return ", ".join(a.get("name", "?") for a in artists or [])


def build_row(item: Dict, year: str) -> Optional[Dict[str, str]]:
    track = item.get("track") or {}
    if not track or track.get("is_local"):
        return None
    album = track.get("album") or {}
    rd = album.get("release_date")
    rdp = album.get("release_date_precision")
    if not is_album_released_in_year(rd, rdp, year):
        return None
    return {
        "track": track.get("name", "?"),
        "artists": format_artists(track.get("artists", [])),
        "album": album.get("name", "?"),
        "album_release_date": rd or "",
        "release_precision": rdp or "",
        "track_id": track.get("id") or "",
    }


def main():
    parser = argparse.ArgumentParser(description="List tracks in a playlist whose album released in a given year.")
    parser.add_argument("playlist_name", help="Playlist name to search in your account")
    parser.add_argument("--year", dest="year", help="Year to filter albums by (e.g., 2025). If not provided, you will be prompted.")
    parser.add_argument("--csv", dest="csv_path", help="Optional path to write CSV output")
    args = parser.parse_args()

    # Get year from args or prompt user
    year = args.year
    if not year:
        try:
            year = input("Enter the year to search for (e.g., 2025): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNo year provided. Exiting.", file=sys.stderr)
            sys.exit(1)
    
    # Validate year
    if not year.isdigit() or len(year) != 4:
        print(f"Invalid year: '{year}'. Please provide a 4-digit year.", file=sys.stderr)
        sys.exit(1)

    load_env()
    sp = auth_client()

    user = sp.current_user()
    print(f"Authenticated as: {user.get('display_name') or user.get('id')}")

    if args.playlist_name.lower().strip() in ("liked songs", "liked", "favorites"):
        print("Scanning your Liked Songs...")
        results = sp.current_user_saved_tracks(limit=50)
        items = []
        while results:
            items.extend(results["items"])
            if results.get("next"):
                results = sp.next(results)
            else:
                break
        playlist_name = "Liked Songs"
    else:
        playlist = find_playlist_by_name(sp, args.playlist_name)
        if not playlist:
            print(f"No playlist found matching '{args.playlist_name}'.", file=sys.stderr)
            sys.exit(2)
        items = playlist_tracks(sp, playlist["id"])
        playlist_name = playlist['name']

    print(f"Filtering for albums released in {year}...")
    print(f"\n--- DEBUG: All tracks in playlist ---")

    rows: List[Dict[str, str]] = []
    for item in items:
        # Debug output
        track = item.get("track") or {}
        if track and not track.get("is_local"):
            album = track.get("album") or {}
            rd = album.get("release_date", "NO DATE")
            rdp = album.get("release_date_precision", "NO PRECISION")
            track_name = track.get("name", "?")
            artists = format_artists(track.get("artists", []))
            album_name = album.get("name", "?")
            print(f"  {track_name} — {artists} | {album_name} | Release: {rd} (precision: {rdp})")
        
        row = build_row(item, year)
        if row:
            rows.append(row)
    
    print(f"--- END DEBUG ---\n")

    # Deduplicate by track_id to be tidy in case of duplicates within the playlist
    seen = set()
    uniq_rows = []
    for r in rows:
        tid = r.get("track_id")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        uniq_rows.append(r)

    if not uniq_rows:
        print(f"No tracks found with albums released in {year}.")
        return

    print(f"\nFound {len(uniq_rows)} track(s) with albums released in {year}:\n")
    for r in uniq_rows:
        print(f"• {r['track']} — {r['artists']} | {r['album']} | {r['album_release_date']} ({r['release_precision']})")

    if args.csv_path:
        import csv
        fieldnames = ["track", "artists", "album", "album_release_date", "release_precision", "track_id"]
        with open(args.csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in uniq_rows:
                writer.writerow(r)
        print(f"\nCSV written to {args.csv_path}")


if __name__ == "__main__":
    import atexit
    import gc
    
    def cleanup():
        """Force garbage collection to avoid spotipy cleanup errors on exit."""
        gc.collect()
    
    atexit.register(cleanup)
    
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted by user.")
