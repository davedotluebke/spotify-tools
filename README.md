# Spotify Tools

Personal scripts for interacting with my Spotify account.

## Current Scripts

### `find-by-year.py`
List tracks in a playlist whose album was released in a given year.

```bash
python find-by-year.py "My Playlist" --year 2025
```

### `song_of_the_day.py` (in development)
Automated daily script that maintains a "Songs of the Day 2026" playlist by adding one song per day based on listening history.

See [TODO.md](TODO.md) for the implementation plan.

## Setup

1. Install dependencies:
   ```bash
   pip install spotipy python-dotenv pytz
   ```

2. Create a Spotify App at https://developer.spotify.com/dashboard
   - Add redirect URI: `http://localhost:8080/callback`

3. Create a `.env` file:
   ```
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   SPOTIFY_REDIRECT_URI=http://localhost:8080/callback
   ```

4. Run a script — first run will open browser for Spotify login.

## License

Personal project, not intended for distribution.
