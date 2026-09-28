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

## 9. Live browser authentication and playlist progress

The application reads YouTube authentication cookies directly from the browser at
download time. The recommended setup is Firefox running as the same `kiosk` user
that runs the music service:

    MUSIC_YOUTUBE_BROWSER="firefox"
    MUSIC_YOUTUBE_BROWSER_PROFILE=""
    MUSIC_YOUTUBE_BROWSER_CONTAINER=""

Leaving the Firefox profile blank matches `--cookies-from-browser firefox`, so
yt-dlp uses the most recently accessed Firefox profile. No `cookies.txt` file is
created or stored by the application. yt-dlp documents `cookiesfrombrowser` as
the Python API equivalent of `--cookies-from-browser`, and its Firefox reader
works by copying the live cookies database before reading it.

Because browser cookies belong to the browser account, run the service as the
`kiosk` user if Firefox also runs as `kiosk`. The application should never run as
root. If Firefox is open, yt-dlp can read a temporary copy of the browser cookie
database rather than requiring the Firefox database to be unlocked.

Deno remains enabled for YouTube JavaScript challenge solving. The executable can
be configured with:

    MUSIC_DENO_PATH="/home/pi/.deno/bin/deno"

The app keeps the authenticated-first YouTube strategy. If an authenticated
request fails with a session/client-sensitive availability error, it retries
without browser cookies using yt-dlp's normal public client selection. Private,
members-only, removed, and authentication-required failures are not incorrectly
converted into anonymous retries.

The web downloader now runs as a background job. The browser polls a protected
status endpoint and displays both overall playlist progress (`completed / total`)
and the current track's yt-dlp byte progress. This keeps the page responsive
while the existing bounded parallel playlist workers continue processing entries.

## 10. Music locations

Configure the music roots only through `MUSIC_PATHS`; do not hard-code drive
paths in `app.py`:

    MUSIC_PATHS="/mnt/CENMATE_250GB/music:/mnt/CENMATE_640GB/music"

The application loads them as:

    MUSIC_LOCATIONS = [
        Path(path).expanduser().resolve()
        for path in os.environ.get("MUSIC_PATHS", "").split(":")
        if path.strip()
    ]

The colon-separated form allows additional mounted music locations to be added
without changing application code.

Some YouTube formats may still be unavailable because they require a GVS PO Token.
That warning does not necessarily prevent a download; yt-dlp can select another
usable format. The application does not hard-code or store PO Tokens.

Download results are stored server-side under `state/notices/`; the Flask session
cookie only stores a short notice ID, preventing large playlist results from
overflowing browser cookie limits.
