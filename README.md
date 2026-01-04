# Spotify Tools

Personal scripts for managing Spotify playlists automatically.

## Scripts

### `song_of_the_day.py`

Maintains a "Song of the Day" playlist by adding one song per day based on your listening history. If you manually add a song, it respects that choice. If you forget, it auto-selects from what you listened to that day.

**Features:**
- Polls listening history every minute (catches Spotify Jams too)
- Auto-selects songs using weighted random from your most-played tracks
- Configurable cooldown prevents repeats (default 90 songs, or disable)
- Multiple profiles for different playlists/users/rules
- Auto-creates playlists if they don't exist
- Tracks whether songs were user-added or auto-picked
- Optional email notifications (failures + weekly summary)

### `liked_songs_by_country.py`

Sorts your Liked Songs into country-specific playlists based on artist nationality (e.g., "Liked Songs - Japan", "Liked Songs - Germany").

**Features:**
- Creates one playlist per country automatically
- Uses MusicBrainz for artist country lookup (free, no API key)
- Falls back to OpenAI for artists not in MusicBrainz
- Runs incrementally — only processes new liked songs
- Handles collaborations: songs with artists from multiple countries go into multiple playlists
- Caches artist lookups to minimize API calls

---

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Create Spotify App

1. Go to https://developer.spotify.com/dashboard
2. Create an app
3. Add redirect URI: `http://localhost:8080/callback`
4. Note your Client ID and Client Secret
5. **For other users**: Add their Spotify email under Settings → User Management

### 3. Create `.env` File

   ```bash
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   SPOTIFY_REDIRECT_URI=http://localhost:8080/callback

# Optional: for liked_songs_by_country.py OpenAI fallback
OPENAI_API_KEY=sk-...
```

### 4. First Run (Authenticate)

```bash
python song_of_the_day.py --status
```

This opens a browser for Spotify login. After authenticating, your token is cached at `~/.spotify-tools/.cache`.

---

## Commands

| Command | Purpose |
|---------|---------|
| `--poll` | Record listening history (run every minute via cron) |
| `--status` | Show today's stats, playlist state, listening history |
| `--status --no-poll` | Show cached stats without refreshing |
| `--finalize` | Add a song if none added today (run nightly via cron) |
| `--dry-run` | Test finalize without actually adding to playlist |
| `--weekly-summary` | Generate/email summary of the week's songs |
| `--profile NAME` | Use a specific profile (see Profiles section) |
| `-q, --quiet` | Suppress non-essential output |

### Examples

```bash
# Check current status (polls first, then shows stats)
python song_of_the_day.py --status

# See what would be auto-picked without adding it
python song_of_the_day.py --dry-run

# Generate weekly summary (prints to console, emails if configured)
python song_of_the_day.py --weekly-summary
```

---

## Profiles

Profiles let you run multiple variations with different settings, playlists, or even different Spotify accounts.

```
~/.spotify-tools/
├── config.json          # "default" profile (backwards compatible)
├── .cache               # default profile's OAuth token
├── dave-auto/           # another profile
│   ├── config.json
│   └── .cache           # can be same or different Spotify account
└── uncle-bob/           # relative's profile
    ├── config.json
    └── .cache           # their Spotify OAuth token
```

### Usage

```bash
# Default profile (no flag needed)
python3 song_of_the_day.py --poll

# Specific profile
python3 song_of_the_day.py --profile dave-auto --poll
python3 song_of_the_day.py --profile uncle-bob --finalize
```

### Setting Up a New Profile

```bash
# Create the profile (just run any command)
python3 song_of_the_day.py --profile uncle-bob --status

# Edit the config
nano ~/.spotify-tools/uncle-bob/config.json
```

For a **different Spotify account** (relatives), they need to authenticate once:
1. Run `--status` with their profile on a machine with a browser
2. They log in to their Spotify account (use incognito if you're logged into yours)
3. Copy the `.cache` file to the server if needed

---

## Configuration

Configuration is stored at `~/.spotify-tools/config.json`. Created automatically on first run.

```json
{
  "playlist_name": "Dave Songs of the Day 2026",
  "playlist_id": null,
  "timezone": "America/New_York",
  "day_boundary_hour": 4,
  "year_start_date": null,
  "cooldown_entries": 90,
  "min_duration_ms": 50000,
  "selection_mode": "weighted_random",
  "email_enabled": false,
  "email_to": "you@example.com",
  "email_from": "sender@example.com",
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "smtp_user": "sender@example.com",
  "smtp_pass": "your-app-password"
}
```

| Setting | Description |
|---------|-------------|
| `playlist_name` | Name of your playlist (auto-created if doesn't exist). If it contains a year like "2026", that's used for day counting. |
| `playlist_id` | Auto-populated after first lookup |
| `timezone` | Your timezone for day calculations |
| `day_boundary_hour` | Hour when a new day starts (default 4 = 4am). For night owls who stay up past midnight. |
| `year_start_date` | First day of the playlist year (default: Jan 1 of year in playlist name, e.g. `"2026-01-01"`) |
| `cooldown_entries` | Songs can't repeat until this many others added (0 = no cooldown) |
| `min_duration_ms` | Minimum track length (filters intros/skits) |
| `selection_mode` | `"weighted_random"` (default) or `"most_played"` (always top track) |
| `email_enabled` | Set `true` to enable email notifications |
| `email_to` | Recipient email address |
| `smtp_*` | SMTP server settings (see Email Setup below) |

### Example Configs

**Auto-only playlist (no manual intervention):**
```json
{
  "playlist_name": "Dave Songs of the Day 2026 - Auto",
  "cooldown_entries": 90,
  "selection_mode": "weighted_random"
}
```

**Night owl who stays up late (day doesn't end until 3am):**
```json
{
  "playlist_name": "Songs of the Day 2026",
  "day_boundary_hour": 3,
  "timezone": "America/New_York"
}
```

**Most-played, no cooldown (for a relative):**
```json
{
  "playlist_name": "Bob Songs of the Day 2026",
  "cooldown_entries": 0,
  "selection_mode": "most_played"
}
```

---

## Cron Setup (Server Deployment)

Add to crontab (`crontab -e`):

```bash
CRON_TZ=America/New_York

# === Default profile ===
* * * * * /usr/bin/python3 /path/to/song_of_the_day.py --poll -q >> ~/logs/poll.log 2>&1
55 23 * * * /usr/bin/python3 /path/to/song_of_the_day.py --finalize >> ~/logs/finalize.log 2>&1
0 9 * * 0 /usr/bin/python3 /path/to/song_of_the_day.py --weekly-summary >> ~/logs/summary.log 2>&1

# === Additional profiles ===
* * * * * /usr/bin/python3 /path/to/song_of_the_day.py --profile dave-auto --poll -q >> ~/logs/dave-auto.log 2>&1
55 23 * * * /usr/bin/python3 /path/to/song_of_the_day.py --profile dave-auto --finalize >> ~/logs/dave-auto.log 2>&1
```

### Headless Server (EC2) Setup

The first authentication requires a browser. To set up on a headless server:

1. Run locally first to complete OAuth flow
2. Copy `~/.spotify-tools/.cache` (or `~/.spotify-tools/PROFILE/.cache`) to the server
3. Token auto-refreshes as long as `.cache` is writable

**For different Spotify accounts**, use incognito mode when authenticating to avoid picking up your own login cookies.

---

## Email Setup (Optional)

Email notifications are sent for:
- **Failures**: Any unhandled error during script execution
- **Weekly Summary**: Recap of songs added (user vs auto-picked)

### Gmail Setup

1. Enable 2-factor authentication on your Google account
2. Generate an [App Password](https://support.google.com/accounts/answer/185833)
3. Configure in `config.json`:

```json
{
  "email_enabled": true,
  "email_to": "you@example.com",
  "email_from": "youraddress@gmail.com",
  "smtp_host": "smtp.gmail.com",
  "smtp_port": 587,
  "smtp_user": "youraddress@gmail.com",
  "smtp_pass": "xxxx xxxx xxxx xxxx"
}
```

### AWS SES Setup

1. **Verify your domain or email** in SES console
2. **Create SMTP credentials** in SES → SMTP Settings → Create credentials
3. **Request production access** if still in sandbox mode (or add recipient emails to verified list)

```json
{
  "email_enabled": true,
  "email_to": "you@example.com",
  "email_from": "verified@yourdomain.com",
  "smtp_host": "email-smtp.us-east-1.amazonaws.com",
  "smtp_port": 587,
  "smtp_user": "YOUR_SES_SMTP_USER",
  "smtp_pass": "YOUR_SES_SMTP_PASSWORD"
}
```

---

## How It Works

### Count-Based Logic

The script ensures **playlist count = day of year**:
- After Jan 1 → 1 song
- After Jan 3 → 3 songs  
- After Feb 1 → 32 songs
- After Dec 31 → 365 (or 366) songs

This means:
- **Manual additions count** — if you add a song, it counts toward the target
- **Deleted songs get replaced** — if a song is removed, the script adds another
- **Catch-up mode** — if behind (script didn't run, songs deleted), it adds multiple songs

### Day Boundary

By default, a new "day" starts at 4am (`day_boundary_hour`). This handles night owls who stay up past midnight. If you run finalize at 2am on Jan 4, it still targets Jan 3's count.

### Selection Algorithm

When songs need to be added:

1. **Build candidate pool** from today's listening history
2. **Apply eligibility filters**:
   - Not in last N playlist entries (cooldown, default 90)
   - Not a podcast episode
   - At least 50 seconds long
3. **Rank by play count** (most-played first)
4. **Select** based on `selection_mode`:
   - `weighted_random`: Random from top 5, weighted by play count
   - `most_played`: Always picks the top track
5. **Fallback cascade** if no eligible songs:
   - Try last 2 days → 3 days → week → Liked Songs

### Nightly Email

After each finalize run, an email is sent (if configured) reporting:
- Playlist count before/after
- Target count for the day
- Songs added (or why none were added)
- Whether playlist is on track, ahead, or behind

### Why Minute-by-Minute Polling?

Spotify Jams (collaborative listening sessions) don't appear in the recently-played API. By polling the currently-playing endpoint every minute, we catch these plays too.

---

## State Files

All state is stored in `~/.spotify-tools/` (or `~/.spotify-tools/PROFILE/` for named profiles):

| File | Purpose |
|------|---------|
| `.cache` | OAuth token (auto-refreshes) |
| `config.json` | Configuration settings |
| `playlist-snapshot.json` | Last known playlist state |
| `additions.json` | Log of all additions (user vs auto) |
| `daily/YYYY-MM-DD.json` | Listening history per day |

---

## Scopes Required

The script requests these Spotify OAuth scopes:

- `user-read-recently-played` — listening history
- `user-read-playback-state` — currently playing (for Jams)
- `playlist-read-private` — read playlist contents
- `playlist-modify-private` — add tracks to playlist
- `playlist-modify-public` — add tracks (if playlist is public)
- `user-library-read` — Liked Songs fallback

---

## Troubleshooting

### "Permissions missing" error
- Delete `~/.spotify-tools/.cache` and re-authenticate
- New scopes may have been added since your last login

### Songs not being captured
- Ensure `--poll` is running every minute
- Check `--status` to see what's been recorded
- Spotify Jams require frequent polling to catch

### Email not sending
- Verify `email_enabled: true` in config
- For Gmail, ensure you're using an App Password, not your regular password
- Check SMTP settings match your provider

### 403 "user may not be registered" error
- Go to developer.spotify.com/dashboard → your app → Settings → User Management
- Add the user's Spotify email address

---

---

## Liked Songs by Country

### Setup

Add your OpenAI API key to `.env` (for fallback lookups):

```bash
OPENAI_API_KEY=sk-...
```

### Commands

```bash
# Process all new liked songs
python liked_songs_by_country.py

# Dry run (show what would happen)
python liked_songs_by_country.py --dry-run

# Show statistics
python liked_songs_by_country.py --status

# Debug: look up a single artist
python liked_songs_by_country.py --lookup-artist "Hikaru Utada"

# Skip OpenAI fallback (MusicBrainz only)
python liked_songs_by_country.py --no-openai

# Use only OpenAI (skip MusicBrainz)
python liked_songs_by_country.py --openai-only

# Generate markdown report of songs by country
python liked_songs_by_country.py --report

# Generate report to custom file
python liked_songs_by_country.py --report my-report.md

# Clear all country playlists (before re-processing)
python liked_songs_by_country.py --clear-playlists

# Clear artist cache (force re-lookups)
python liked_songs_by_country.py --clear-cache

# Verbose output
python liked_songs_by_country.py -v
```

### How It Works

1. Fetches all your Liked Songs from Spotify
2. Filters to songs not yet processed
3. For each artist, looks up their country:
   - **Cache**: Previously looked-up artists are cached permanently
   - **MusicBrainz**: Free music database (only used when artist has direct country data)
   - **OpenAI**: Handles artists MusicBrainz can't resolve (cities, regions, or missing data)
4. Creates playlists like "Liked Songs - Japan" as needed
5. Adds songs to appropriate country playlists
6. Marks songs as processed (won't be re-processed next run)

**Note**: First run may take a while due to MusicBrainz rate limits (1.5 sec between requests). Subsequent runs are fast since artist data is cached.

### Collaboration Handling

- **Single artist**: Goes to that artist's country playlist
- **Multiple artists, same country**: Goes to one playlist
- **Multiple artists, different countries**: Goes to multiple playlists
  - e.g., a US artist ft. Korean artist → both "Liked Songs - United States" and "Liked Songs - South Korea"

### State Files

Stored in `~/.spotify-tools/country-playlists/`:

| File | Purpose |
|------|---------|
| `artist-countries.json` | Cached artist → country lookups |
| `processed-songs.json` | Track IDs already sorted |
| `playlist-ids.json` | Country → playlist ID mapping |

### Cron Setup (Optional)

Run weekly to catch new liked songs:

```bash
0 3 * * 0 /usr/bin/python3 /path/to/liked_songs_by_country.py >> ~/logs/country-playlists.log 2>&1
```

---

## License

MIT License - see [LICENSE](LICENSE) file.
