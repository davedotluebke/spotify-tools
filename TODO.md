# Songs of the Day 2026 — Implementation Plan

## Overview

A daily script that ensures exactly one song is added to the "Dave Songs of the Day 2026" playlist each day. The script:

1. **Polls listening history** every minute throughout the day (cron) — captures both recently-played and currently-playing to catch Spotify Jams
2. **Runs a nightly check** just before midnight Eastern to see if a song was manually added that day
3. **Auto-selects a song** if none was added, based on listening data and eligibility rules

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CRON JOBS                               │
├─────────────────────────────────────────────────────────────────┤
│  Every min: song_of_the_day.py --poll                           │
│  Nightly:   song_of_the_day.py --finalize  (11:55 PM Eastern)   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      LOCAL STATE (JSON)                         │
├─────────────────────────────────────────────────────────────────┤
│  ~/.spotify-tools/                                              │
│    ├── .cache                  # OAuth token                    │
│    ├── config.json             # playlist ID, preferences       │
│    ├── playlist-snapshot.json  # last known playlist state      │
│    ├── additions.json          # tracks user vs auto additions  │
│    └── daily/                                                   │
│          └── 2026-01-01.json   # all plays for that day         │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     SPOTIFY API (source of truth)               │
├─────────────────────────────────────────────────────────────────┤
│  GET  /me/player/recently-played   (last 50 tracks)             │
│  GET  /me/player                   (currently playing - Jams)   │
│  GET  /playlists/{id}/tracks       (current playlist state)     │
│  POST /playlists/{id}/tracks       (add selected song)          │
│  GET  /me/tracks                   (Liked Songs fallback)       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Components

### 1. `song_of_the_day.py` — Main Script

**Modes:**

| Flag | When | Purpose |
|------|------|---------|
| `--poll` | Every minute via cron | Fetch recently-played + currently-playing, append to today's log |
| `--finalize` | 11:55 PM Eastern | Check if song was added; if not, pick and add one |
| `--status` | Manual | Poll first, then show today's listening stats and playlist state |
| `--status --no-poll` | Manual | Show cached stats without polling (faster, for debugging) |
| `--dry-run` | Manual | Run finalize logic but don't actually add to playlist |
| `--weekly-summary` | Weekly via cron | Generate and email summary of week's songs (user vs auto) |

### 2. Local State Files

**`~/.spotify-tools/daily/YYYY-MM-DD.json`**

One file per day, accumulating all plays throughout the day via minute-by-minute polls.
Typically contains a few dozen entries (one per song play, not per poll).

```json
{
  "date": "2026-01-01",
  "last_poll": "2026-01-01T22:00:00-05:00",
  "last_current_track_id": "abc123",
  "plays": [
    {
      "track_id": "abc123",
      "track_name": "Song Title",
      "artist": "Artist Name",
      "played_at": "2026-01-01T14:32:00Z",
      "duration_ms": 215000,
      "type": "track",
      "context_type": "playlist",
      "source": "recently_played"
    }
  ],
  "play_counts": {
    "abc123": 5,
    "def456": 2
  }
}
```

**Note:** `source` is either `"recently_played"` (from Spotify history API) or `"current_playback"` (captured via currently-playing, e.g., during Jams).

**`~/.spotify-tools/playlist-snapshot.json`**
```json
{
  "playlist_id": "spotify:playlist:xxxxx",
  "playlist_name": "Dave Songs of the Day 2026",
  "last_checked": "2026-01-01T23:55:00-05:00",
  "track_count": 1,
  "tracks": [
    {
      "track_id": "abc123",
      "track_name": "Song Title",
      "artist": "Artist Name",
      "added_at": "2026-01-01",  // date only, inferred from position
      "position": 0
    }
  ]
}
```

**`~/.spotify-tools/config.json`**
```json
{
  "playlist_name": "Dave Songs of the Day 2026",
  "playlist_id": null,
  "timezone": "America/New_York",
  "cooldown_entries": 90,
  "min_duration_ms": 50000,
  "email_enabled": false,
  "email_to": "you@example.com",
  "email_from": "sender@example.com",
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "smtp_user": "sender@example.com",
  "smtp_pass": "your-app-password"
}
```

**`~/.spotify-tools/additions.json`** (tracks user vs auto additions)
```json
[
  {
    "date": "2026-01-01",
    "track_id": "abc123",
    "track_name": "Song Title",
    "artist": "Artist Name",
    "source": "user",
    "recorded_at": "2026-01-01T23:55:00+00:00"
  }
]
```
`source` is either `"user"` (manually added) or `"auto"` (script-added).

---

## Spotify API Scopes Required

```python
SCOPES = [
    "user-read-recently-played",   # for listening history
    "user-read-playback-state",    # for currently-playing (catches Jams)
    "playlist-read-private",       # to read playlist contents
    "playlist-modify-private",     # to add tracks (if playlist is private)
    "playlist-modify-public",      # to add tracks (if playlist is public)
    "user-library-read",           # for Liked Songs fallback
]
```

---

## Algorithm: Song Selection

### Step 1: Detect if user already added a song today

Compare current playlist state to yesterday's snapshot:
- If playlist has one more track than yesterday, and that track wasn't auto-added by us → user added it manually → **done, no action needed**
- If playlist length unchanged → need to auto-add

**Edge case:** User swapped out yesterday's auto-pick for a different song
- Playlist length same, but last track differs from our record
- Treat as "user made a choice" → update snapshot, consider today still needing a song

### Step 2: Build candidate pool from today's listening

```
candidates = all unique tracks from today's listening history
```

### Step 3: Apply eligibility filters

```python
def is_eligible(track, playlist_tracks):
    track_id = track["track_id"]
    
    # Rule 1: Not in the last 90 entries of the playlist
    recent_90 = [t["track_id"] for t in playlist_tracks[-90:]]
    if track_id in recent_90:
        return False
    
    # Rule 2: Not a podcast episode
    # (Spotify returns type="episode" for podcasts vs type="track" for music)
    if track.get("type") == "episode":
        return False
    
    # Rule 3: Track must be at least 50 seconds long
    # (filters out intros, interludes, skits, sound effects)
    duration_ms = track.get("duration_ms", 0)
    if duration_ms < 50_000:  # 50 seconds in milliseconds
        return False
    
    return True

eligible = [t for t in candidates if is_eligible(t, playlist_tracks)]
```

### Step 4: Rank and select via weighted random

```python
def select_song(eligible, play_counts):
    # Sort by play count descending, then by most recent play
    ranked = sorted(eligible, key=lambda t: (play_counts[t], last_played[t]), reverse=True)
    
    # Take top 5 (or fewer if not enough eligible)
    top_n = ranked[:5]
    
    # Weighted random selection: weight = play_count
    # e.g., song with 5 plays is 5x more likely than song with 1 play
    weights = [play_counts[t] for t in top_n]
    selected = random.choices(top_n, weights=weights, k=1)[0]
    return selected
```

This adds variety while still favoring heavily-played tracks.

### Step 5: Fallback cascade (if no eligible songs today)

| Fallback Level | Source | Condition |
|----------------|--------|-----------|
| 1 | Past 2 days | Expand to listening history from yesterday + today |
| 2 | Past 3 days | Expand further |
| 3 | Past week | Expand to last 7 days |
| 4 | Liked Songs | Random pick from user's Liked Songs, still respecting 90-entry cooldown |

### Step 6: Add to playlist

```python
sp.playlist_add_items(playlist_id, [selected_track_uri])
```

Update local snapshot with new track.

---

## Polling Logic (`--poll`)

### Source 1: Recently Played
1. Fetch `/me/player/recently-played` (limit=50)
2. Filter to plays where `played_at` is today (Eastern time)
3. Load today's file from `~/.spotify-tools/daily/YYYY-MM-DD.json` (or create if first poll of day)
4. **Merge new plays with existing plays:**
   - Each play has a unique `played_at` ISO timestamp
   - Use `played_at` as the dedup key to avoid double-counting
   - Only append plays whose `played_at` is not already in the file
   - This handles overlapping windows between polls
5. Mark with `source: "recently_played"`

### Source 2: Currently Playing
6. Fetch `/me/player` (current playback state)
7. If a track is playing and differs from `last_current_track_id`:
   - Check if we've already recorded this track within last 5 minutes (dedup)
   - If not, record as new play with current timestamp
   - Mark with `source: "current_playback"`
   - Update `last_current_track_id`

### Finalize
8. Recompute `play_counts` from the merged `plays` array
9. Update `last_poll` timestamp
10. Save file

**Why two sources?** Spotify Jams (collaborative listening sessions) don't appear in recently-played history. The currently-playing endpoint catches these plays.

**Dedup logic:** To avoid double-counting when a song appears in both sources, we check if we've already recorded that track_id within the last 5 minutes before adding a currently-playing entry.

**Note:** 1-minute polling is recommended to avoid missing plays during Jams (songs can be as short as 2-3 minutes).

---

## Cron Setup (EC2)

```bash
# Ensure EC2 timezone or use explicit TZ in cron
# Option 1: Set TZ in crontab
CRON_TZ=America/New_York

# Poll listening history every minute (captures Spotify Jams)
# Use -q (quiet) to reduce log noise
* * * * * /path/to/venv/bin/python /path/to/song_of_the_day.py --poll -q >> /var/log/spotify-tools/poll.log 2>&1

# Nightly finalize at 11:55 PM Eastern
55 23 * * * /path/to/venv/bin/python /path/to/song_of_the_day.py --finalize >> /var/log/spotify-tools/finalize.log 2>&1

# Weekly summary email on Sundays at 9 AM Eastern
0 9 * * 0 /path/to/venv/bin/python /path/to/song_of_the_day.py --weekly-summary >> /var/log/spotify-tools/summary.log 2>&1
```

**Note:** 1-minute polling is needed to capture Spotify Jams, which don't show up in the recently-played API. This makes ~1,440 API calls/day, well within Spotify's limits.

**Note:** Email notifications require `email_enabled: true` and SMTP settings in config.json. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833).

**Alternative:** systemd timers (more robust, better logging)

---

## Edge Cases to Handle

| Scenario | Handling |
|----------|----------|
| Playlist doesn't exist yet | Exit with clear error message; don't auto-create |
| First run of the year (no history) | Use Liked Songs fallback |
| User deleted a song from playlist | Detect length decrease, update snapshot, don't re-add |
| User reordered playlist | Irrelevant; we only care about last 90 by position |
| Duplicate track IDs in history | Dedupe in play_counts |
| Token expired | Spotipy handles refresh; ensure `.cache` is writable |
| Network failure | Log error, exit non-zero; cron will retry next minute |
| Multiple songs added in one day | Take no action; user is in control |
| Script runs twice in same minute | Idempotent; check playlist state before adding |

---

## File Structure

```
spotify-tools/
├── find-by-year.py              # existing script
├── song_of_the_day.py           # new main script
├── spotify_auth.py              # shared auth logic (refactored from find-by-year.py)
├── requirements.txt             # spotipy, python-dotenv, pytz
├── .env                         # SPOTIFY_CLIENT_ID, etc.
├── TODO.md                      # this file
└── README.md                    # usage instructions
```

---

## Implementation Order

### Phase 1: Core Infrastructure ✅
- [x] Extract shared auth code into `spotify_auth.py`
- [x] Create config/state directory structure (`~/.spotify-tools/`)
- [x] Implement `--poll` mode: fetch and store listening history
- [x] Add currently-playing capture (for Spotify Jams)
- [x] Test polling to verify data capture

### Phase 2: Playlist Integration ✅
- [x] Implement playlist lookup by name
- [x] Implement playlist snapshot (read current tracks)
- [x] Implement "did user add a song today?" detection
- [x] Implement `--status` mode for debugging

### Phase 3: Selection Algorithm ✅
- [x] Implement eligibility filter (90-entry cooldown, duration, type)
- [x] Implement ranking (play count + recency)
- [x] Implement weighted random selection from top 5
- [x] Implement fallback cascade (2 days → 3 days → week → Liked Songs)
- [x] Implement `--dry-run` mode

### Phase 4: Finalize & Deploy ✅
- [x] Implement `--finalize` mode (full flow + add to playlist)
- [ ] Add logging (file-based, with rotation)
- [ ] Write deployment instructions for EC2 cron
- [x] Test end-to-end with real playlist

### Phase 5: Polish ✅
- [ ] Add `--backfill YYYY-MM-DD` mode to manually trigger for a past date
- [x] Add email notification on failure (auto-sends if email configured)
- [x] Add weekly summary email of songs added (`--weekly-summary`)
- [x] Track user vs auto-added songs in additions.json

---

## Open Questions / Future Considerations

1. **What if I want different rules?** — Consider a simple config file or CLI flags for things like:
   - Cooldown period (default 90)
   - Prefer/avoid certain genres
   - Exclude explicit tracks
   - Weight by time of day (evening listens matter more?)

2. **Year rollover** — Script is hardcoded to "2026" playlist. Next year, either:
   - Parameterize playlist name
   - Create new script/config for 2027

3. **Historical backfill** — If you start this mid-January, do you want to backfill Jan 1-15? Would require stored listening history (or Last.fm integration).

4. **Observability** — Consider a simple SQLite database instead of JSON files for easier querying/debugging.

---

## Dependencies

```
spotipy>=2.23.0
python-dotenv>=1.0.0
pytz>=2024.1
```


