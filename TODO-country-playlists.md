# Liked Songs by Country — Implementation Plan

## Overview

A script that sorts a user's Liked Songs into country-specific playlists based on artist nationality. Runs incrementally (daily/weekly) to catch new additions.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│              liked_songs_by_country.py                      │
├─────────────────────────────────────────────────────────────┤
│  1. Fetch all Liked Songs from Spotify                      │
│  2. Filter to songs not yet processed                       │
│  3. For each new song's artist(s):                          │
│     a. Check local cache                                    │
│     b. Query MusicBrainz by Spotify ID or name              │
│     c. If not found → OpenAI fallback                       │
│     d. Cache result                                         │
│  4. Determine country playlist(s) for each song             │
│  5. Create playlists if needed ("Liked Songs - Japan")      │
│  6. Add songs to appropriate playlists                      │
│  7. Mark songs as processed                                 │
└─────────────────────────────────────────────────────────────┘
```

---

## State Files

All stored in `~/.spotify-tools/country-playlists/`:

**`artist-countries.json`** — Cached artist → country mappings
```json
{
  "spotify_artist_id_123": {
    "name": "Hikaru Utada",
    "country": "Japan",
    "source": "musicbrainz",
    "musicbrainz_id": "uuid-here",
    "cached_at": "2026-01-01T12:00:00Z"
  }
}
```

**`processed-songs.json`** — Track IDs already sorted into playlists
```json
{
  "last_run": "2026-01-01T12:00:00Z",
  "processed": ["track_id_1", "track_id_2", ...]
}
```

**`playlist-ids.json`** — Country → playlist ID mapping
```json
{
  "Japan": "spotify_playlist_id_abc",
  "United States": "spotify_playlist_id_def"
}
```

---

## Artist Country Resolution

### Step 1: Check Cache
- If artist_id in `artist-countries.json`, use cached value
- Cache entries don't expire (artist nationality doesn't change)

### Step 2: MusicBrainz Lookup
1. Search MusicBrainz for artist by name
2. If multiple results, try to match by:
   - Exact name match
   - Spotify external link (if available)
3. Extract "area" → "name" for country
4. Rate limit: 1 request/second (MusicBrainz requirement)

### Step 3: OpenAI Fallback
- For artists not found in MusicBrainz
- Prompt: "What country is the musical artist [Name] from? Reply with just the country name."
- Simple, deterministic prompt to minimize token usage

### Step 4: Manual Override (Future)
- Config file for manual corrections
- e.g., `{"artist_id": "override_country"}`

---

## Song → Playlist Assignment

**Conservative approach:**
1. Get country for each artist on the track
2. If all artists from same country → single playlist
3. If artists from different countries (e.g., collab, remix) → multiple playlists
4. "Unknown" country → skip or add to "Liked Songs - Unknown" playlist

**Example:**
- "Track by Japanese Artist" → Japan playlist only
- "Track by Japanese Artist ft. American Artist" → Japan + United States playlists
- "Track by Japanese Artist (Korean Artist Remix)" → Japan + South Korea playlists

---

## CLI Interface

```bash
# Full sync (process all unprocessed Liked Songs)
python liked_songs_by_country.py

# Dry run (show what would happen)
python liked_songs_by_country.py --dry-run

# Status (show stats)
python liked_songs_by_country.py --status

# Force reprocess a specific track
python liked_songs_by_country.py --reprocess TRACK_ID

# Lookup artist country (for debugging)
python liked_songs_by_country.py --lookup-artist "Artist Name"
```

---

## API Rate Limits

| API | Limit | Strategy |
|-----|-------|----------|
| Spotify | ~100 req/min | Pagination handles this naturally |
| MusicBrainz | 1 req/sec | Sleep between requests |
| OpenAI | Varies by tier | Batch requests, cache aggressively |

---

## Dependencies

```
spotipy>=2.23.0         # Spotify API
python-dotenv           # Environment variables  
requests                # MusicBrainz API calls
openai>=1.0.0           # OpenAI API for fallback
```

---

## Environment Variables

```bash
# Existing
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=...

# New
OPENAI_API_KEY=...
```

---

## Implementation Order

### Phase 1: Core Infrastructure
- [ ] Create state directory and file management
- [ ] Implement Liked Songs fetching (paginated)
- [ ] Implement processed songs tracking

### Phase 2: Artist Country Resolution  
- [ ] Implement MusicBrainz lookup (with rate limiting)
- [ ] Implement OpenAI fallback
- [ ] Implement caching layer

### Phase 3: Playlist Management
- [ ] Implement playlist creation ("Liked Songs - {Country}")
- [ ] Implement song → playlist assignment logic
- [ ] Implement incremental playlist updates

### Phase 4: CLI & Polish
- [ ] Add --dry-run mode
- [ ] Add --status mode
- [ ] Add --lookup-artist debugging
- [ ] Error handling and logging

---

## Open Questions

1. **What about "Unknown" artists?** — Create "Liked Songs - Unknown" playlist, or skip?
2. **Country name normalization** — "USA" vs "United States" vs "US"?
3. **Profile support** — Reuse song_of_the_day.py profile system?

