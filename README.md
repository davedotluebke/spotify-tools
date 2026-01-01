# Spotify Tools

Personal scripts for interacting with Spotify.

## Scripts

### `song_of_the_day.py`

Automatically maintains a "Song of the Day" playlist by adding one song per day based on your listening history. If you manually add a song, it respects that choice. If you forget, it auto-selects from what you listened to that day.

**Features:**
- Polls listening history every minute (catches Spotify Jams too)
- Auto-selects songs using weighted random from your most-played tracks
- 90-song cooldown prevents repeats
- Tracks whether songs were user-added or auto-picked
- Optional email notifications (failures + weekly summary)

### `find-by-year.py`

List tracks in a playlist whose album was released in a given year.

```bash
python find-by-year.py "My Playlist" --year 2025
```

---

## Quick Start

### 1. Install Dependencies

```bash
pip install spotipy python-dotenv pytz
```

### 2. Create Spotify App

1. Go to https://developer.spotify.com/dashboard
2. Create an app
3. Add redirect URI: `http://localhost:8080/callback`
4. Note your Client ID and Client Secret

### 3. Create `.env` File

```bash
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFY_REDIRECT_URI=http://localhost:8080/callback
```

### 4. Create Your Playlist

Create a playlist in Spotify named exactly as configured (default: "Dave Songs of the Day 2026").

### 5. First Run (Authenticate)

```bash
python song_of_the_day.py --status
```

This opens a browser for Spotify login. After authenticating, your token is cached at `~/.spotify-tools/.cache`.

---

## Song of the Day Usage

### Commands

| Command | Purpose |
|---------|---------|
| `--poll` | Record listening history (run every minute via cron) |
| `--status` | Show today's stats, playlist state, listening history |
| `--status --no-poll` | Show cached stats without refreshing |
| `--finalize` | Add a song if none added today (run nightly via cron) |
| `--dry-run` | Test finalize without actually adding to playlist |
| `--weekly-summary` | Generate/email summary of the week's songs |
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

## Configuration

Configuration is stored at `~/.spotify-tools/config.json`. Created automatically on first run.

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

| Setting | Description |
|---------|-------------|
| `playlist_name` | Name of your playlist (must exist in Spotify) |
| `playlist_id` | Auto-populated after first lookup |
| `timezone` | Your timezone for "today" calculations |
| `cooldown_entries` | Songs can't repeat until this many others added |
| `min_duration_ms` | Minimum track length (filters intros/skits) |
| `email_enabled` | Set `true` to enable email notifications |
| `email_to` | Recipient email address |
| `smtp_*` | SMTP server settings (see Email Setup below) |

---

## Cron Setup (Server Deployment)

Add to crontab (`crontab -e`):

```bash
CRON_TZ=America/New_York

# Poll listening history every minute
* * * * * /path/to/venv/bin/python /path/to/song_of_the_day.py --poll -q >> /var/log/spotify-tools/poll.log 2>&1

# Finalize at 11:55 PM (add song if none added today)
55 23 * * * /path/to/venv/bin/python /path/to/song_of_the_day.py --finalize >> /var/log/spotify-tools/finalize.log 2>&1

# Weekly summary on Sundays at 9 AM
0 9 * * 0 /path/to/venv/bin/python /path/to/song_of_the_day.py --weekly-summary >> /var/log/spotify-tools/summary.log 2>&1
```

### Headless Server (EC2) Setup

The first authentication requires a browser. To set up on a headless server:

1. Run locally first to complete OAuth flow
2. Copy `~/.spotify-tools/.cache` to the server
3. Token auto-refreshes as long as `.cache` is writable

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

### Selection Algorithm

1. **Check if user added a song today** — if yes, done
2. **Build candidate pool** from today's listening history
3. **Apply eligibility filters**:
   - Not in last 90 playlist entries (cooldown)
   - Not a podcast episode
   - At least 50 seconds long
4. **Rank by play count** (most-played first)
5. **Weighted random selection** from top 5 (more plays = higher chance)
6. **Fallback cascade** if no eligible songs:
   - Try last 2 days → 3 days → week → Liked Songs

### Why Minute-by-Minute Polling?

Spotify Jams (collaborative listening sessions) don't appear in the recently-played API. By polling the currently-playing endpoint every minute, we catch these plays too.

---

## State Files

All state is stored in `~/.spotify-tools/`:

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

### "Playlist not found"
- Ensure the playlist exists in Spotify with the exact name in config
- The script doesn't auto-create playlists

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

---

## License

Personal project, not intended for distribution.
