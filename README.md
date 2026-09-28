# Hardened LAN Music Server

## 1. Install dependencies

sudo apt update
sudo apt install ffmpeg python3-venv

cd /path/to/music-downloader
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

## 2. Create a password hash

Run:

python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash(input('Password: '), method='scrypt'))"

Copy the resulting hash into MUSIC_APP_PASSWORD_HASH.

## 3. Create a random Flask secret

Run:

python3 -c "import secrets; print(secrets.token_urlsafe(48))"

Copy the result into MUSIC_APP_SECRET_KEY.

## 4. Export the configuration

Do NOT commit the real environment file.

export MUSIC_APP_USERNAME="ram"
export MUSIC_APP_PASSWORD_HASH='YOUR_HASH'
export MUSIC_APP_SECRET_KEY='YOUR_RANDOM_SECRET'
export MUSIC_BIND="0.0.0.0"
export MUSIC_PORT="5000"
export MUSIC_COOKIE_SECURE="0"
export MUSIC_STATE_DIR="$PWD/state"

mkdir -p "$MUSIC_STATE_DIR"

## 5. Start for LAN testing

source venv/bin/activate
python3 app.py

Find your LAN IP:

hostname -I

Then from another device on the same LAN:

http://YOUR_LAN_IP:5000

## 6. Firewall

Example for a 192.168.1.0/24 LAN:

sudo ufw allow from 192.168.1.0/24 to any port 5000 proto tcp

Do not port-forward TCP 5000 from the internet.

## 7. Production-style server

Do not use Flask's development server as the long-term deployment.

With the virtual environment active:

gunicorn \
  --workers 1 \
  --threads 4 \
  --bind 0.0.0.0:5000 \
  --access-logfile - \
  --error-logfile - \
  app:app

Keep workers at 1 because the built-in rate limiter and download lock are process-local.

## 8. HTTPS

For real password protection over Wi-Fi, use HTTPS. Otherwise login credentials and session traffic travel over plain HTTP.

When HTTPS is configured:

export MUSIC_COOKIE_SECURE=1

Do not enable this while accessing the server over HTTP, or the login cookie will not be sent.

## Security notes

- Only HTTPS YouTube/SoundCloud hosts are accepted by the downloader.
- User-controlled paths are resolved and checked against the configured music roots.
- Symlinks are rejected for served/moved files and playlist directories.
- Playlist names are allow-listed.
- Only MP3 files are served/moved.
- Request bodies are size-limited.
- Downloads are rate-limited. Single-track downloads remain sequential; playlist entries are processed concurrently with a bounded worker pool (default 3).
- Download size and time are bounded.
- CSRF protection is required for state-changing authenticated requests.
- Session cookies are HttpOnly and SameSite=Strict.
- Security response headers are set globally.
- Never run the server as root.
- Never expose port 5000 directly to the internet.
- Do not commit .env or real password hashes/secrets to Git.

## Download limits

- Tracks must be shorter than 30 minutes. Tracks that are 30:00 or longer are skipped before the audio download begins.

## 9. Optional YouTube authentication cookies

To allow yt-dlp to access videos that your own YouTube account is permitted to
watch, export the account cookies in Netscape/Mozilla cookie format and save
them as `cookies.txt` beside `.env`. Do not share or commit this file.

Set:

    MUSIC_YOUTUBE_COOKIE_FILE="cookies.txt"
    MUSIC_MAX_PARALLEL_DOWNLOADS="3"
    MUSIC_MAX_PLAYLIST_ITEMS="5000"

Large YouTube playlists receive additional extraction handling. The app compares
yt-dlp's returned entry count with YouTube's reported `playlist_count`. If the
playlist is incomplete, it retries using `youtubetab:skip=webpage` and then makes
bounded playlist-index range requests to recover entries that were not exposed by
the initial extraction. Recovery is capped by `MUSIC_MAX_PLAYLIST_RECOVERY_REQUESTS`
and `MUSIC_PLAYLIST_RECOVERY_BATCH_SIZE` so a broken YouTube pagination response
does not cause unbounded requests. If entries are still missing, the final result
explicitly reports how many YouTube said existed versus how many yt-dlp exposed.

The application uses the cookie file for playlist extraction, per-video
metadata extraction, and the actual MP3 download. It also enables yt-dlp's
EJS components from GitHub and automatically uses Deno from ~/.deno/bin/deno
when present. This matches the tested command-line configuration:
`--cookies cookies.txt --remote-components ejs:github`.

Some YouTube formats may still be unavailable because they require a GVS PO
Token. That warning does not necessarily prevent a download; yt-dlp can select
another usable format. The application does not hard-code or store PO Tokens.
If an authenticated video still cannot be accessed, that entry is skipped and
playlist processing continues.

The cookie file should be readable only by the service account, for example:

    chmod 600 cookies.txt

Never paste the contents of `cookies.txt` into chat, Git, logs, or the web UI.

Download results are stored server-side under `state/notices/`; the Flask session cookie only stores a short notice ID, preventing large playlist results from overflowing browser cookie limits.

### YouTube authenticated/public fallback

The downloader now uses a two-stage YouTube strategy when `cookies.txt` is
configured:

1. Try authenticated extraction/download first, preserving access to videos the
   configured account is allowed to watch.
2. If that authenticated attempt reports a client/session-sensitive availability
   failure such as `Video unavailable` or HTTP 403, retry once without cookies
   using yt-dlp's normal public client selection.
3. Private, removed, members-only, and authentication/age-verification failures
   are not incorrectly converted into anonymous retries.

The same fallback is applied to single-track metadata extraction, playlist-entry
metadata/download workers, and playlist extraction. Cookie contents are never
logged or returned to the browser.
