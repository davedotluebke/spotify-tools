#!/usr/bin/env python3
"""
Sort Liked Songs into country-specific playlists based on artist nationality.

Uses MusicBrainz for artist country lookup, with OpenAI fallback for unknown artists.
Runs incrementally - only processes songs not yet sorted into playlists.

Usage:
    python liked_songs_by_country.py              # Process new liked songs
    python liked_songs_by_country.py --dry-run    # Show what would happen
    python liked_songs_by_country.py --status     # Show statistics
    python liked_songs_by_country.py --lookup-artist "Artist Name"  # Debug lookup
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# Import shared auth from spotify_auth.py
from spotify_auth import get_spotify_client, load_env, get_state_dir

# Optional OpenAI import
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


# =============================================================================
# Configuration
# =============================================================================

MUSICBRAINZ_USER_AGENT = "SpotifyCountryPlaylists/1.0 (https://github.com/davedotluebke/spotify-tools)"
MUSICBRAINZ_API_BASE = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_RATE_LIMIT = 1.0  # seconds between requests

# Country name normalization map (for variations, not cities)
COUNTRY_ALIASES = {
    "United States of America": "United States",
    "USA": "United States",
    "US": "United States",
    "U.S.A.": "United States",
    "UK": "United Kingdom",
    "U.K.": "United Kingdom",
    "Great Britain": "United Kingdom",
    "England": "United Kingdom",
    "Scotland": "United Kingdom",
    "Wales": "United Kingdom",
    "Northern Ireland": "United Kingdom",
    "Republic of Korea": "South Korea",
    "Korea": "South Korea",
    "ROK": "South Korea",
    "DPRK": "North Korea",
    "PRC": "China",
    "People's Republic of China": "China",
    "ROC": "Taiwan",
    "Republic of China": "Taiwan",
    "Russian Federation": "Russia",
    "Deutschland": "Germany",
    "Nippon": "Japan",
    "Brasil": "Brazil",
    "España": "Spain",
    "Italia": "Italy",
}



# =============================================================================
# State Management
# =============================================================================

def get_country_state_dir() -> Path:
    """Get the state directory for country playlists."""
    base_dir = get_state_dir()
    country_dir = base_dir / "country-playlists"
    country_dir.mkdir(parents=True, exist_ok=True)
    return country_dir


def get_artist_cache_path() -> Path:
    return get_country_state_dir() / "artist-countries.json"


def get_processed_songs_path() -> Path:
    return get_country_state_dir() / "processed-songs.json"


def get_playlist_ids_path() -> Path:
    return get_country_state_dir() / "playlist-ids.json"


def load_artist_cache() -> Dict[str, Dict[str, Any]]:
    """Load cached artist → country mappings."""
    path = get_artist_cache_path()
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_artist_cache(cache: Dict[str, Dict[str, Any]]) -> None:
    """Save artist → country cache."""
    path = get_artist_cache_path()
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


def load_processed_songs() -> Dict[str, Any]:
    """Load set of already-processed song IDs."""
    path = get_processed_songs_path()
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {"last_run": None, "processed": []}


def save_processed_songs(data: Dict[str, Any]) -> None:
    """Save processed songs data."""
    path = get_processed_songs_path()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_playlist_ids() -> Dict[str, str]:
    """Load country → playlist ID mappings."""
    path = get_playlist_ids_path()
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_playlist_ids(data: Dict[str, str]) -> None:
    """Save playlist ID mappings."""
    path = get_playlist_ids_path()
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# =============================================================================
# Spotify API Helpers
# =============================================================================

def fetch_all_liked_songs(sp) -> List[Dict[str, Any]]:
    """Fetch all liked songs from Spotify (handles pagination)."""
    songs = []
    offset = 0
    limit = 50
    
    print("📚 Fetching Liked Songs...")
    
    while True:
        results = sp.current_user_saved_tracks(limit=limit, offset=offset)
        items = results.get("items", [])
        
        if not items:
            break
            
        for item in items:
            track = item.get("track")
            if track and track.get("id"):
                album = track.get("album", {})
                songs.append({
                    "track_id": track["id"],
                    "track_name": track["name"],
                    "album_name": album.get("name"),
                    "artists": [
                        {"id": a["id"], "name": a["name"]}
                        for a in track.get("artists", [])
                    ],
                    "added_at": item.get("added_at"),
                })
        
        offset += limit
        if len(items) < limit:
            break
            
        # Progress indicator
        if offset % 200 == 0:
            print(f"   ...fetched {offset} songs")
    
    print(f"   ✓ Found {len(songs)} total Liked Songs")
    return songs


def get_or_create_playlist(sp, country: str, playlist_ids: Dict[str, str]) -> str:
    """Get existing playlist ID or create new one for a country."""
    if country in playlist_ids:
        return playlist_ids[country]
    
    playlist_name = f"Liked Songs - {country}"
    print(f"📝 Creating playlist: {playlist_name}")
    
    user_id = sp.current_user()["id"]
    playlist = sp.user_playlist_create(
        user_id,
        playlist_name,
        public=False,
        description=f"Liked songs by artists from {country}. Auto-managed by liked_songs_by_country.py"
    )
    
    playlist_ids[country] = playlist["id"]
    save_playlist_ids(playlist_ids)
    
    return playlist["id"]


def get_playlist_track_ids(sp, playlist_id: str) -> Set[str]:
    """Get all track IDs currently in a playlist."""
    track_ids = set()
    offset = 0
    limit = 100
    
    while True:
        results = sp.playlist_items(
            playlist_id,
            limit=limit,
            offset=offset,
            fields="items(track(id)),next"
        )
        items = results.get("items", [])
        
        if not items:
            break
            
        for item in items:
            track = item.get("track")
            if track and track.get("id"):
                track_ids.add(track["id"])
        
        offset += limit
        if len(items) < limit:
            break
    
    return track_ids


def add_tracks_to_playlist(sp, playlist_id: str, track_ids: List[str]) -> None:
    """Add tracks to a playlist (handles batching)."""
    # Spotify allows max 100 tracks per request
    for i in range(0, len(track_ids), 100):
        batch = track_ids[i:i+100]
        uris = [f"spotify:track:{tid}" for tid in batch]
        sp.playlist_add_items(playlist_id, uris)


def clear_country_playlists(sp) -> None:
    """Remove all tracks from all country playlists."""
    playlist_ids = load_playlist_ids()
    
    if not playlist_ids:
        print("No country playlists found")
        return
    
    print(f"🗑️  Clearing {len(playlist_ids)} country playlists...")
    
    for country, playlist_id in sorted(playlist_ids.items()):
        # Get all tracks in playlist
        track_ids = list(get_playlist_track_ids(sp, playlist_id))
        
        if not track_ids:
            print(f"   ✓ {country}: already empty")
            continue
        
        # Remove tracks in batches of 100
        for i in range(0, len(track_ids), 100):
            batch = track_ids[i:i+100]
            uris = [f"spotify:track:{tid}" for tid in batch]
            sp.playlist_remove_all_occurrences_of_items(playlist_id, uris)
        
        print(f"   ✓ {country}: removed {len(track_ids)} tracks")
    
    # Clear processed songs so they'll be re-added
    processed_path = get_processed_songs_path()
    if processed_path.exists():
        processed_path.unlink()
        print("   ✓ Cleared processed songs list")
    
    print("\n✅ All playlists cleared. Run again to re-populate.")


# =============================================================================
# MusicBrainz API
# =============================================================================

_last_musicbrainz_request = 0.0


def musicbrainz_request(endpoint: str, params: Dict[str, str] = None) -> Optional[Dict]:
    """Make a rate-limited request to MusicBrainz API. Returns None on any error."""
    global _last_musicbrainz_request
    
    # Rate limiting - be conservative
    elapsed = time.time() - _last_musicbrainz_request
    min_wait = 1.5
    if elapsed < min_wait:
        time.sleep(min_wait - elapsed)
    
    if params is None:
        params = {}
    params["fmt"] = "json"
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
    
    try:
        url = f"{MUSICBRAINZ_API_BASE}/{endpoint}"
        response = requests.get(url, params=params, headers=headers, timeout=10)
        _last_musicbrainz_request = time.time()
        
        if response.status_code == 200:
            return response.json()
        else:
            # Any error - just return None and let OpenAI handle it
            return None
    except requests.RequestException:
        # Connection error, timeout, etc. - let OpenAI handle it
        return None



def lookup_artist_musicbrainz(artist_name: str, spotify_id: str = None) -> Optional[str]:
    """
    Look up an artist's country via MusicBrainz.
    
    Returns normalized country name or None if not found.
    Only returns a result if MusicBrainz has a Country-type area.
    For cities/regions, returns None to let OpenAI handle it (avoids extra API calls).
    """
    # Search for artist by name (simple search, not strict field match)
    params = {"query": artist_name, "limit": "10"}
    result = musicbrainz_request("artist", params)
    
    if not result or "artists" not in result:
        return None
    
    artists = result["artists"]
    if not artists:
        return None
    
    # Try to find best match with a Country-type area
    best_match = None
    artist_name_lower = artist_name.lower()
    
    for artist in artists:
        name = artist.get("name", "").lower()
        aliases = [a.get("name", "").lower() for a in artist.get("aliases", [])]
        area = artist.get("area", {})
        
        # Only consider artists with Country-type areas
        if area.get("type") != "Country":
            continue
        
        # Exact name match preferred
        if name == artist_name_lower or artist_name_lower in aliases:
            best_match = artist
            break
        
        # Otherwise take first result with a country
        if not best_match:
            best_match = artist
    
    if not best_match:
        # No artist with a Country-type area found
        # Return None to let OpenAI handle it
        return None
    
    # Extract country from area
    area = best_match.get("area", {})
    area_name = area.get("name")
    
    if not area_name:
        return None
    
    return normalize_country(area_name)


def normalize_country(country: str) -> str:
    """Normalize country name to a standard form."""
    if not country:
        return "Unknown"
    
    # Check aliases
    if country in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[country]
    
    # Check case-insensitive aliases
    for alias, normalized in COUNTRY_ALIASES.items():
        if alias.lower() == country.lower():
            return normalized
    
    return country


# =============================================================================
# OpenAI Fallback
# =============================================================================

def lookup_artist_openai(artist_name: str, song_name: str = None, album_name: str = None) -> Optional[str]:
    """
    Look up an artist's country via OpenAI API.
    
    Optionally includes song/album context for better disambiguation.
    Returns normalized country name or None if lookup fails.
    """
    if not OPENAI_AVAILABLE:
        print("   ⚠️  OpenAI not available (pip install openai)")
        return None
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("   ⚠️  OPENAI_API_KEY not set")
        return None
    
    try:
        client = OpenAI(api_key=api_key)
        
        # Build the query with optional context
        query = f"What country is the musical artist '{artist_name}' from?"
        if song_name or album_name:
            context_parts = []
            if song_name:
                context_parts.append(f"Song: \"{song_name}\"")
            if album_name:
                context_parts.append(f"Album: \"{album_name}\"")
            query += f" Context: {', '.join(context_parts)}"
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a music expert. When asked about an artist's country of origin, respond with ONLY the country name, nothing else. If the artist is from multiple countries or you're unsure, respond with the primary country they're associated with. If you don't know, respond with 'Unknown'."
                },
                {
                    "role": "user", 
                    "content": query
                }
            ],
            max_tokens=50,
            temperature=0
        )
        
        country = response.choices[0].message.content.strip()
        return normalize_country(country)
        
    except Exception as e:
        print(f"   ⚠️  OpenAI error: {e}")
        return None


# =============================================================================
# Artist Country Resolution
# =============================================================================

def get_artist_country(
    artist_id: str,
    artist_name: str,
    cache: Dict[str, Dict[str, Any]],
    use_openai: bool = True,
    openai_only: bool = False,
    song_name: str = None,
    album_name: str = None,
    verbose: bool = False
) -> Tuple[str, str]:
    """
    Get an artist's country, using cache → MusicBrainz → OpenAI fallback.
    
    If openai_only is True, skips MusicBrainz and goes straight to OpenAI.
    song_name and album_name provide context for better OpenAI disambiguation.
    
    Returns (country, source) tuple.
    """
    # Check cache first
    if artist_id in cache:
        cached = cache[artist_id]
        return cached["country"], cached["source"]
    
    if verbose:
        print(f"   🔍 Looking up: {artist_name}")
    
    country = None
    source = "unknown"
    
    # Try MusicBrainz first (unless openai_only)
    if not openai_only:
        country = lookup_artist_musicbrainz(artist_name, artist_id)
        if country and country != "Unknown":
            source = "musicbrainz"
            if verbose:
                print(f"      → {country} (MusicBrainz)")
    
    # Try OpenAI if MusicBrainz didn't find it (or if openai_only)
    if (not country or country == "Unknown") and use_openai:
        country = lookup_artist_openai(artist_name, song_name=song_name, album_name=album_name)
        if country and country != "Unknown":
            source = "openai"
            if verbose:
                print(f"      → {country} (OpenAI)")
        else:
            country = "Unknown"
            source = "unknown"
            if verbose:
                print(f"      → Unknown")
    elif not country:
        country = "Unknown"
        if verbose:
            print(f"      → Unknown (MusicBrainz only)")
    
    # Cache the result
    cache[artist_id] = {
        "name": artist_name,
        "country": country or "Unknown",
        "source": source,
        "cached_at": datetime.now(timezone.utc).isoformat()
    }
    save_artist_cache(cache)
    
    return country or "Unknown", source


# =============================================================================
# Main Processing Logic
# =============================================================================

def determine_countries_for_track(
    track: Dict[str, Any],
    cache: Dict[str, Dict[str, Any]],
    use_openai: bool = True,
    openai_only: bool = False,
    verbose: bool = False
) -> Set[str]:
    """
    Determine which country playlist(s) a track belongs to.
    
    Returns set of country names.
    """
    countries = set()
    
    # Get song/album context for better OpenAI disambiguation
    song_name = track.get("track_name")
    album_name = track.get("album_name")
    
    for artist in track["artists"]:
        country, _ = get_artist_country(
            artist["id"],
            artist["name"],
            cache,
            use_openai=use_openai,
            openai_only=openai_only,
            song_name=song_name,
            album_name=album_name,
            verbose=verbose
        )
        if country and country != "Unknown":
            countries.add(country)
    
    return countries


def process_liked_songs(
    sp,
    dry_run: bool = False,
    use_openai: bool = True,
    openai_only: bool = False,
    verbose: bool = False
) -> Dict[str, int]:
    """
    Process liked songs and add to country playlists.
    
    If openai_only is True, skips MusicBrainz and uses OpenAI for all lookups.
    
    Returns dict of {country: num_songs_added}.
    """
    # Load state
    artist_cache = load_artist_cache()
    processed_data = load_processed_songs()
    processed_set = set(processed_data.get("processed", []))
    playlist_ids = load_playlist_ids()
    
    # Fetch all liked songs
    all_songs = fetch_all_liked_songs(sp)
    
    # Filter to unprocessed songs
    new_songs = [s for s in all_songs if s["track_id"] not in processed_set]
    
    if not new_songs:
        print("✅ No new songs to process")
        return {}
    
    print(f"\n🎵 Processing {len(new_songs)} new songs...")
    if openai_only:
        print("   (Using OpenAI only - skipping MusicBrainz)")
    
    # Group songs by country
    country_to_tracks: Dict[str, List[str]] = {}
    
    for i, song in enumerate(new_songs, 1):
        if verbose or i % 50 == 0:
            print(f"   [{i}/{len(new_songs)}] {song['track_name']}")
        
        countries = determine_countries_for_track(
            song, 
            artist_cache, 
            use_openai=use_openai,
            openai_only=openai_only,
            verbose=verbose
        )
        
        for country in countries:
            if country not in country_to_tracks:
                country_to_tracks[country] = []
            country_to_tracks[country].append(song["track_id"])
        
        # Mark as processed (even if Unknown)
        processed_set.add(song["track_id"])
    
    # Add tracks to playlists
    results = {}
    
    if dry_run:
        print("\n🧪 DRY RUN - Would add:")
        for country, track_ids in sorted(country_to_tracks.items()):
            print(f"   {country}: {len(track_ids)} songs")
            results[country] = len(track_ids)
    else:
        print("\n📝 Updating playlists...")
        for country, track_ids in sorted(country_to_tracks.items()):
            if country == "Unknown":
                print(f"   ⏭️  Skipping {len(track_ids)} songs with unknown artist countries")
                continue
            
            # Get or create playlist
            playlist_id = get_or_create_playlist(sp, country, playlist_ids)
            
            # Get existing tracks to avoid duplicates
            existing = get_playlist_track_ids(sp, playlist_id)
            new_track_ids = [tid for tid in track_ids if tid not in existing]
            
            if new_track_ids:
                add_tracks_to_playlist(sp, playlist_id, new_track_ids)
                print(f"   ✓ {country}: added {len(new_track_ids)} songs")
                results[country] = len(new_track_ids)
            else:
                print(f"   ✓ {country}: no new songs (already in playlist)")
        
        # Save processed songs
        processed_data["processed"] = list(processed_set)
        processed_data["last_run"] = datetime.now(timezone.utc).isoformat()
        save_processed_songs(processed_data)
    
    return results


# Country to flag emoji mapping  
COUNTRY_FLAGS = {
    'United States': '🇺🇸', 'United Kingdom': '🇬🇧', 'Canada': '🇨🇦',
    'Germany': '🇩🇪', 'France': '🇫🇷', 'Australia': '🇦🇺', 'Sweden': '🇸🇪',
    'Japan': '🇯🇵', 'Ireland': '🇮🇪', 'Netherlands': '🇳🇱', 'Belgium': '🇧🇪',
    'Italy': '🇮🇹', 'New Zealand': '🇳🇿', 'Austria': '🇦🇹', 'South Africa': '🇿🇦',
    'Norway': '🇳🇴', 'Finland': '🇫🇮', 'Poland': '🇵🇱', 'Iceland': '🇮🇸',
    'Jamaica': '🇯🇲', 'Mexico': '🇲🇽', 'Mali': '🇲🇱', 'Russia': '🇷🇺',
    'Denmark': '🇩🇰', 'Ukraine': '🇺🇦', 'Spain': '🇪🇸', 'Colombia': '🇨🇴',
    'Argentina': '🇦🇷', 'Algeria': '🇩🇿', 'Mongolia': '🇲🇳', 'Chile': '🇨🇱',
    'Palestine': '🇵🇸', 'Cuba': '🇨🇺', 'Moldova': '🇲🇩', 'Estonia': '🇪🇪',
    'Switzerland': '🇨🇭', 'Bulgaria': '🇧🇬', 'Romania': '🇷🇴', 'Czechia': '🇨🇿',
    'Haiti': '🇭🇹', 'Portugal': '🇵🇹', 'India': '🇮🇳', 'Brazil': '🇧🇷',
    'China': '🇨🇳', 'South Korea': '🇰🇷', 'Taiwan': '🇹🇼', 'Singapore': '🇸🇬',
    'Thailand': '🇹🇭', 'Indonesia': '🇮🇩', 'Philippines': '🇵🇭', 'Vietnam': '🇻🇳',
    'Malaysia': '🇲🇾', 'Greece': '🇬🇷', 'Turkey': '🇹🇷', 'Israel': '🇮🇱',
    'Egypt': '🇪🇬', 'Nigeria': '🇳🇬', 'Kenya': '🇰🇪', 'Morocco': '🇲🇦',
    'Unknown': '❓'
}


def format_duration(ms: int) -> str:
    """Format milliseconds as Xh Ym or Ym."""
    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours > 0:
        return f'{hours}h {minutes}m'
    else:
        return f'{minutes}m'


def generate_country_report(sp, output_file: str) -> None:
    """Generate a markdown report of liked songs by country."""
    from collections import defaultdict
    
    print("📊 Generating country report...")
    
    # Load artist cache
    artist_cache = load_artist_cache()
    if not artist_cache:
        print("❌ No artist data cached. Run the script first to populate.")
        return
    
    # Fetch all liked songs with duration
    print("   Fetching liked songs...")
    songs = []
    offset = 0
    while True:
        results = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = results.get('items', [])
        if not items:
            break
        for item in items:
            track = item.get('track')
            if track and track.get('id'):
                songs.append({
                    'track_id': track['id'],
                    'track_name': track['name'],
                    'duration_ms': track.get('duration_ms', 0),
                    'artists': [{'id': a['id'], 'name': a['name']} for a in track.get('artists', [])]
                })
        offset += 50
        if len(items) < 50:
            break
    
    print(f"   Found {len(songs)} songs")
    
    # Calculate stats per country
    country_stats = defaultdict(lambda: {'artists': set(), 'songs': 0, 'duration_ms': 0})
    
    for song in songs:
        song_countries = set()
        for artist in song['artists']:
            artist_data = artist_cache.get(artist['id'])
            if artist_data:
                country = artist_data['country']
                if country and country != 'Unknown':
                    song_countries.add(country)
                    country_stats[country]['artists'].add(artist['id'])
        
        # Add song to each country (for collabs)
        for country in song_countries:
            country_stats[country]['songs'] += 1
            country_stats[country]['duration_ms'] += song['duration_ms']
    
    # Sort by duration (most listened)
    sorted_countries = sorted(
        [(c, s) for c, s in country_stats.items()],
        key=lambda x: x[1]['duration_ms'],
        reverse=True
    )
    
    # Generate markdown
    lines = []
    lines.append("# Liked Songs by Country")
    lines.append("")
    lines.append("*My Liked Songs library, sorted by artist country of origin.*")
    lines.append("")
    lines.append("| Flag | Country | Artists | Songs | Duration |")
    lines.append("|:----:|---------|--------:|------:|---------:|")
    
    for country, stats in sorted_countries:
        flag = COUNTRY_FLAGS.get(country, '🏳️')
        artists = len(stats['artists'])
        songs_count = stats['songs']
        duration = format_duration(stats['duration_ms'])
        lines.append(f"| {flag} | {country} | {artists} | {songs_count} | {duration} |")
    
    # Calculate totals
    total_artists = len(set(aid for s in country_stats.values() for aid in s['artists']))
    total_songs = len(songs)
    total_duration = sum(s['duration_ms'] for s in country_stats.values())
    unknown_count = sum(1 for a in artist_cache.values() if a.get('country') == 'Unknown')
    
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"**Total:** {total_songs:,} songs • {total_artists:,} artists • {len(country_stats)} countries • {format_duration(total_duration)}")
    lines.append("")
    lines.append(f"*{unknown_count} artists with unknown origin not included above*")
    lines.append("")
    
    # Write to file
    with open(output_file, 'w') as f:
        f.write('\n'.join(lines))
    
    print(f"✅ Report written to {output_file}")


def show_status(sp) -> None:
    """Show current status and statistics."""
    artist_cache = load_artist_cache()
    processed_data = load_processed_songs()
    playlist_ids = load_playlist_ids()
    
    print("📊 Status")
    print("=" * 50)
    
    # Last run
    last_run = processed_data.get("last_run", "Never")
    print(f"Last run: {last_run}")
    
    # Processed songs
    processed_count = len(processed_data.get("processed", []))
    print(f"Processed songs: {processed_count}")
    
    # Cached artists
    print(f"Cached artists: {len(artist_cache)}")
    
    # Country breakdown
    by_source = {"musicbrainz": 0, "openai": 0, "unknown": 0}
    by_country: Dict[str, int] = {}
    for artist_data in artist_cache.values():
        source = artist_data.get("source", "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        
        country = artist_data.get("country", "Unknown")
        by_country[country] = by_country.get(country, 0) + 1
    
    print(f"\nArtist lookups by source:")
    for source, count in sorted(by_source.items()):
        print(f"   {source}: {count}")
    
    print(f"\nArtists by country (top 20):")
    for country, count in sorted(by_country.items(), key=lambda x: -x[1])[:20]:
        print(f"   {country}: {count}")
    
    # Playlists
    print(f"\nCountry playlists: {len(playlist_ids)}")
    for country in sorted(playlist_ids.keys()):
        print(f"   • Liked Songs - {country}")
    
    # Check for new songs
    all_songs = fetch_all_liked_songs(sp)
    processed_set = set(processed_data.get("processed", []))
    new_count = sum(1 for s in all_songs if s["track_id"] not in processed_set)
    print(f"\nNew songs to process: {new_count}")


def lookup_artist_cli(artist_name: str, use_openai: bool = True) -> None:
    """Debug command to look up a single artist."""
    print(f"🔍 Looking up: {artist_name}")
    print("-" * 40)
    
    # Try MusicBrainz
    print("MusicBrainz:")
    country = lookup_artist_musicbrainz(artist_name)
    if country:
        print(f"   → {country}")
    else:
        print("   → Not found")
    
    # Try OpenAI
    if use_openai:
        print("\nOpenAI:")
        country = lookup_artist_openai(artist_name)
        if country:
            print(f"   → {country}")
        else:
            print("   → Not found / Error")


def fix_cache(use_openai: bool = True, verbose: bool = False) -> None:
    """
    Re-lookup artists in cache that have non-country values (cities, etc).
    
    This fixes entries that were cached before the city→country lookup was added.
    """
    cache = load_artist_cache()
    
    # Known country names to skip (these are already correct)
    known_countries = {
        "United States", "United Kingdom", "Canada", "Australia", "Germany",
        "France", "Japan", "South Korea", "Italy", "Spain", "Brazil", "Mexico",
        "Sweden", "Norway", "Denmark", "Finland", "Netherlands", "Belgium",
        "Austria", "Switzerland", "Ireland", "New Zealand", "Russia", "China",
        "Taiwan", "India", "Argentina", "Portugal", "Poland", "Czech Republic",
        "Hungary", "Greece", "Turkey", "South Africa", "Israel", "Jamaica",
        "Cuba", "Colombia", "Chile", "Peru", "Venezuela", "Philippines",
        "Indonesia", "Thailand", "Vietnam", "Malaysia", "Singapore",
        "Nigeria", "Kenya", "Egypt", "Morocco", "Iceland", "Croatia",
        "Romania", "Ukraine", "Puerto Rico", "North Korea", "Unknown"
    }
    
    # Find entries that need fixing
    needs_fix = []
    for artist_id, data in cache.items():
        country = data.get("country", "")
        if country and country not in known_countries:
            needs_fix.append((artist_id, data))
    
    if not needs_fix:
        print("✅ Cache looks good - no city entries found")
        return
    
    print(f"🔧 Found {len(needs_fix)} entries that may be cities (not countries):")
    for _, data in needs_fix[:10]:
        print(f"   • {data.get('name')}: {data.get('country')}")
    if len(needs_fix) > 10:
        print(f"   ... and {len(needs_fix) - 10} more")
    
    print(f"\n🔍 Re-looking up {len(needs_fix)} artists...")
    
    fixed = 0
    for i, (artist_id, data) in enumerate(needs_fix, 1):
        artist_name = data.get("name", "")
        old_country = data.get("country", "")
        
        if verbose:
            print(f"   [{i}/{len(needs_fix)}] {artist_name} (was: {old_country})")
        
        # Try MusicBrainz again (now with city→country lookup)
        new_country = lookup_artist_musicbrainz(artist_name)
        
        # If still not found and OpenAI enabled, try that
        if (not new_country or new_country == old_country) and use_openai:
            new_country = lookup_artist_openai(artist_name)
        
        if new_country and new_country != old_country:
            cache[artist_id]["country"] = new_country
            cache[artist_id]["source"] = "musicbrainz" if new_country != "Unknown" else "unknown"
            cache[artist_id]["cached_at"] = datetime.now(timezone.utc).isoformat()
            fixed += 1
            if verbose:
                print(f"      → {new_country}")
    
    save_artist_cache(cache)
    print(f"\n✅ Fixed {fixed} entries")


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sort Liked Songs into country-specific playlists"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current status and statistics"
    )
    parser.add_argument(
        "--lookup-artist",
        type=str,
        metavar="NAME",
        help="Look up a single artist's country (for debugging)"
    )
    parser.add_argument(
        "--fix-cache",
        action="store_true",
        help="Re-lookup cached entries that are cities instead of countries"
    )
    parser.add_argument(
        "--no-openai",
        action="store_true",
        help="Disable OpenAI fallback (use MusicBrainz only)"
    )
    parser.add_argument(
        "--openai-only",
        action="store_true",
        help="Skip MusicBrainz, use OpenAI for all lookups (more accurate but costs ~$0.02)"
    )
    parser.add_argument(
        "--clear-playlists",
        action="store_true",
        help="Remove all tracks from country playlists (use before re-processing)"
    )
    parser.add_argument(
        "--report",
        type=str,
        nargs="?",
        const="LIKED_SONGS_BY_COUNTRY.md",
        metavar="FILE",
        help="Generate markdown report of songs by country (default: LIKED_SONGS_BY_COUNTRY.md)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed progress"
    )
    
    args = parser.parse_args()
    
    # Load environment
    load_env()
    
    # Handle --lookup-artist (doesn't need Spotify auth)
    if args.lookup_artist:
        lookup_artist_cli(args.lookup_artist, use_openai=not args.no_openai)
        return 0
    
    # Handle --fix-cache (doesn't need Spotify auth)
    if args.fix_cache:
        fix_cache(use_openai=not args.no_openai, verbose=args.verbose)
        return 0
    
    # Get Spotify client (needed for remaining commands)
    sp = get_spotify_client()
    if not sp:
        print("❌ Failed to authenticate with Spotify")
        return 1
    
    # Show current user
    try:
        user = sp.current_user()
        print(f"👤 Logged in as: {user['display_name']} ({user['id']})")
    except Exception as e:
        print(f"❌ Failed to get user info: {e}")
        return 1
    
    # Handle --clear-playlists
    if args.clear_playlists:
        clear_country_playlists(sp)
        return 0
    
    # Handle --report
    if args.report:
        generate_country_report(sp, args.report)
        return 0
    
    # Handle modes
    if args.status:
        show_status(sp)
        return 0
    
    # Default: process songs
    try:
        results = process_liked_songs(
            sp,
            dry_run=args.dry_run,
            use_openai=not args.no_openai,
            openai_only=args.openai_only,
            verbose=args.verbose
        )
        
        if results:
            total = sum(results.values())
            print(f"\n✅ Done! Added {total} songs to {len(results)} playlists")
        
        return 0
        
    except Exception as e:
        print(f"❌ Error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

