#!/usr/bin/env python3
"""
Shared Spotify authentication module.

Handles OAuth flow with token caching. Tokens are stored in ~/.spotify-tools/.cache
by default, ensuring they persist regardless of working directory.

First-time setup:
1. Create a Spotify App at https://developer.spotify.com/dashboard
2. Add redirect URI: http://localhost:8080/callback
3. Set environment variables (or .env file):
   - SPOTIFY_CLIENT_ID
   - SPOTIFY_CLIENT_SECRET
   - SPOTIFY_REDIRECT_URI=http://localhost:8080/callback

For headless servers (EC2):
1. Run any script locally first to complete the OAuth browser flow
2. Copy ~/.spotify-tools/.cache to the server
3. The token will auto-refresh as long as .cache is writable
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth


# Default scopes for song-of-the-day functionality
DEFAULT_SCOPES = [
    "user-read-recently-played",   # for listening history
    "user-read-playback-state",    # for currently-playing (catches Jams)
    "playlist-read-private",       # to read playlist contents
    "playlist-modify-private",     # to add tracks (if playlist is private)
    "playlist-modify-public",      # to add tracks (if playlist is public)
    "user-library-read",           # for Liked Songs fallback
]

# Default paths
DEFAULT_BASE_DIR = Path.home() / ".spotify-tools"

# Module-level state for current profile
_current_profile: Optional[str] = None


def set_profile(profile: Optional[str]) -> None:
    """Set the current profile. None means 'default'."""
    global _current_profile
    _current_profile = profile


def get_profile() -> str:
    """Get the current profile name."""
    return _current_profile or "default"


def get_state_dir(profile: Optional[str] = None) -> Path:
    """
    Return the state directory for a profile, creating it if needed.
    
    If profile is None, uses the current profile set via set_profile().
    
    For backwards compatibility:
    - "default" profile uses ~/.spotify-tools/ if config.json exists there
    - Otherwise uses ~/.spotify-tools/{profile}/
    """
    if profile is None:
        profile = get_profile()
    
    # Check for env override first
    env_override = os.getenv("SPOTIFY_STATE_DIR")
    if env_override:
        state_dir = Path(env_override)
    elif profile == "default":
        # Backwards compatibility: use base dir if config exists there
        legacy_config = DEFAULT_BASE_DIR / "config.json"
        if legacy_config.exists():
            state_dir = DEFAULT_BASE_DIR
        else:
            state_dir = DEFAULT_BASE_DIR / profile
    else:
        state_dir = DEFAULT_BASE_DIR / profile
    
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_cache_path(profile: Optional[str] = None) -> Path:
    """Return the token cache path for a profile."""
    return get_state_dir(profile) / ".cache"


def load_env() -> None:
    """Load environment variables from .env file if present."""
    # Try .env in current directory first, then in the script's directory
    load_dotenv()
    
    script_dir = Path(__file__).parent
    env_file = script_dir / ".env"
    if env_file.exists():
        load_dotenv(env_file)


def check_env() -> List[str]:
    """Check for required environment variables. Returns list of missing vars."""
    required = ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"]
    return [k for k in required if not os.getenv(k)]


def get_spotify_client(
    scopes: Optional[List[str]] = None,
    cache_path: Optional[Path] = None,
    open_browser: bool = True,
) -> Spotify:
    """
    Create an authenticated Spotify client.
    
    Args:
        scopes: OAuth scopes to request. Defaults to DEFAULT_SCOPES.
        cache_path: Path to store OAuth token. Defaults to ~/.spotify-tools/.cache
        open_browser: Whether to open browser for OAuth flow. Set False for headless.
    
    Returns:
        Authenticated Spotify client.
    
    Raises:
        SystemExit: If required environment variables are missing.
    """
    load_env()
    
    missing = check_env()
    if missing:
        print(
            f"Missing environment variables: {', '.join(missing)}\n"
            "Create a Spotify app at https://developer.spotify.com/dashboard\n"
            "and set them in a .env file or your environment.",
            file=sys.stderr,
        )
        sys.exit(1)
    
    if scopes is None:
        scopes = DEFAULT_SCOPES
    
    if cache_path is None:
        cache_path = get_cache_path()
    
    # Ensure parent directory exists
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    oauth = SpotifyOAuth(
        scope=scopes,
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI"),
        client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
        open_browser=open_browser,
        cache_path=str(cache_path),
    )
    
    return Spotify(auth_manager=oauth)


def get_current_user_display(sp: Spotify) -> str:
    """Return a display string for the current authenticated user."""
    user = sp.current_user()
    return user.get("display_name") or user.get("id") or "Unknown"

