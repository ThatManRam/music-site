import json
import os
import re
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, send_file, session, url_for, g
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash
import yt_dlp

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


# Load a local .env when running the app directly. Systemd deployments can
# continue to provide the same values through EnvironmentFile.
if load_dotenv is not None:
    load_dotenv(Path(__file__).resolve().parent / ".env")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("MUSIC_STATE_DIR", BASE_DIR / "state")).resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)

DOWNLOAD_ARCHIVE = STATE_DIR / "downloads.txt"
NOTICE_DIR = STATE_DIR / "notices"
NOTICE_DIR.mkdir(parents=True, exist_ok=True)

MUSIC_LOCATIONS = [
    Path("/run/media/ram/CENMATE_250GB/music").resolve(),
    Path("/run/media/ram/CENMATE_640GB/music").resolve(),
]

SINGLES_FOLDER = "Singles"

# Security/resource limits. Adjust deliberately; do not remove them casually.
MAX_URL_LENGTH = 2048
MAX_PLAYLIST_NAME_LENGTH = 64
MAX_FILENAME_LENGTH = 180
MAX_JSON_BYTES = 16 * 1024
MAX_REQUEST_BYTES = 32 * 1024
MAX_AUDIO_FILE_BYTES = 500 * 1024 * 1024
MAX_PLAYLIST_ITEMS = max(1, min(5000, int(os.environ.get("MUSIC_MAX_PLAYLIST_ITEMS", "5000"))))
PLAYLIST_RECOVERY_BATCH_SIZE = max(50, min(500, int(os.environ.get("MUSIC_PLAYLIST_RECOVERY_BATCH_SIZE", "250"))))
MAX_PLAYLIST_RECOVERY_REQUESTS = max(1, min(20, int(os.environ.get("MUSIC_MAX_PLAYLIST_RECOVERY_REQUESTS", "20"))))
MAX_PARALLEL_DOWNLOADS = max(1, min(8, int(os.environ.get("MUSIC_MAX_PARALLEL_DOWNLOADS", "3"))))
MAX_PLAYLIST_NAME_COUNT = 200
MIN_FREE_SPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 30 * 60
MAX_TRACK_DURATION_SECONDS = 30 * 60
MAX_DOWNLOADS_PER_WINDOW = 3
DOWNLOAD_WINDOW_SECONDS = 10 * 60

ALLOWED_DOWNLOAD_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "soundcloud.com",
    "www.soundcloud.com",
}

ALLOWED_QUALITIES = {"128", "192", "256", "320"}

# The password must be supplied as a Werkzeug password hash.
# Generate one with:
# python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('YOUR_PASSWORD'))"
APP_USERNAME = os.environ.get("MUSIC_APP_USERNAME", "").strip()
APP_PASSWORD_HASH = os.environ.get("MUSIC_APP_PASSWORD_HASH", "").strip()
SECRET_KEY = os.environ.get("MUSIC_APP_SECRET_KEY", "").strip()

if not APP_USERNAME or not APP_PASSWORD_HASH or not SECRET_KEY:
    raise RuntimeError(
        "Set MUSIC_APP_USERNAME, MUSIC_APP_PASSWORD_HASH, and "
        "MUSIC_APP_SECRET_KEY before starting the server."
    )

COOKIE_SECURE = os.environ.get("MUSIC_COOKIE_SECURE", "0") == "1"


def resolve_config_path(value):
    """Resolve an optional config path, including legacy $PWD values."""
    if not value:
        return None

    expanded = os.path.expandvars(os.path.expanduser(value.strip()))
    path = Path(expanded)
    if not path.is_absolute():
        path = BASE_DIR / path

    return path.resolve()


YOUTUBE_COOKIE_FILE = resolve_config_path(
    os.environ.get("MUSIC_YOUTUBE_COOKIE_FILE", "cookies.txt")
)

if YOUTUBE_COOKIE_FILE and not YOUTUBE_COOKIE_FILE.is_file():
    # Missing optional cookies should not prevent normal public downloads.
    YOUTUBE_COOKIE_FILE = None


def store_download_notice(message, category):
    """Store a potentially large download result on disk, not in the session cookie."""
    notice_id = secrets.token_urlsafe(24)
    path = NOTICE_DIR / f"{notice_id}.json"
    payload = {"message": str(message), "category": category}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)
    return notice_id


def consume_download_notice():
    """Read and remove the one-time download result referenced by the session."""
    notice_id = session.pop("download_notice_id", None)
    if not isinstance(notice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", notice_id):
        return None

    path = NOTICE_DIR / f"{notice_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        try:
            path.unlink()
        except OSError:
            pass

    message = payload.get("message")
    category = payload.get("category")
    if not isinstance(message, str) or category not in {"success", "error"}:
        return None
    return category, message


def yt_dlp_common_options(use_cookies=True, youtube_client_mode="authenticated"):
    """Build yt-dlp options for either authenticated or public YouTube access.

    Authenticated YouTube access is tried first when cookies are configured.
    Public YouTube retries intentionally omit cookies because a valid account
    session can make an otherwise public video return UNPLAYABLE for that
    session/client combination.
    """
    options = {
        "remote_components": ["ejs:github"],
    }

    deno_path = Path.home() / ".deno" / "bin" / "deno"
    if deno_path.is_file():
        options["js_runtimes"] = {"deno": {"path": str(deno_path)}}

    if use_cookies and YOUTUBE_COOKIE_FILE:
        options["cookiefile"] = str(YOUTUBE_COOKIE_FILE)

    if youtube_client_mode == "authenticated":
        # This client combination is useful for authenticated/age-restricted
        # videos and is retained for the cookie-backed first attempt.
        options["extractor_args"] = {
            "youtube": {
                "player_client": ["default", "web_embedded"],
            }
        }
    elif youtube_client_mode == "public":
        # Deliberately do not force web_embedded here. Let the current yt-dlp
        # release select its normal public clients.
        pass
    else:
        raise ValueError("invalid YouTube client mode")

    return options


def is_youtube_url(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }


def should_retry_youtube_without_cookies(error):
    """Return True for failures that may be caused by an authenticated client.

    Do not retry private, removed, or authentication-required videos without
    cookies because the public attempt cannot legitimately make those available.
    """
    text = str(error).casefold()

    if "private video" in text:
        return False
    if "removed for violating" in text or "video has been removed" in text:
        return False
    if "sign in to confirm your age" in text:
        return False
    if "requires authentication" in text:
        return False
    if "members-only" in text or "members only" in text:
        return False

    return (
        "video unavailable" in text
        or "this video is not available" in text
        or "http error 403" in text
        or "403 forbidden" in text
        or "playability status" in text
    )


def cleanup_temp_downloads(destination, temp_prefix):
    for path in destination.glob(f".{temp_prefix}.*"):
        try:
            path.unlink()
        except OSError:
            pass


def download_with_youtube_fallback(options, url, destination, temp_prefix, start):
    """Run a download with cookies first, then retry public YouTube once.

    Returns None only when the first attempt should be retried. Any final
    yt-dlp DownloadError is allowed to propagate to the caller.
    """
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])
        return
    except yt_dlp.utils.DownloadError as error:
        if not (YOUTUBE_COOKIE_FILE and is_youtube_url(url) and
                should_retry_youtube_without_cookies(error)):
            raise

        app.logger.warning(
            "Authenticated YouTube download failed for %s; retrying without cookies",
            url,
        )
        cleanup_temp_downloads(destination, temp_prefix)

        public_options = dict(options)
        public_options.pop("cookiefile", None)
        # Remove the authenticated client selection and let yt-dlp use its
        # normal public clients for the fallback.
        public_options.pop("extractor_args", None)

        with yt_dlp.YoutubeDL(public_options) as ydl:
            ydl.download([url])


def extract_metadata_with_youtube_fallback(url, options):
    """Extract metadata with cookies first and a public retry when appropriate."""
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as error:
        if not (YOUTUBE_COOKIE_FILE and is_youtube_url(url) and
                should_retry_youtube_without_cookies(error)):
            raise

        app.logger.warning(
            "Authenticated YouTube metadata extraction failed for %s; retrying without cookies",
            url,
        )
        public_options = dict(options)
        public_options.pop("cookiefile", None)
        public_options.pop("extractor_args", None)

        with yt_dlp.YoutubeDL(public_options) as ydl:
            return ydl.extract_info(url, download=False)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(
    MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=COOKIE_SECURE,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 8,
)

# Only one downloader at a time. This limits CPU/network/disk abuse.
download_lock = threading.Lock()

# Small in-process rate limiter. Keep the app at one worker unless you replace
# this with a shared limiter such as Redis.
rate_lock = threading.Lock()
rate_buckets = defaultdict(deque)


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def client_ip():
    # Do NOT trust X-Forwarded-For because this server is not configured to
    # trust a specific reverse proxy.
    return request.remote_addr or "unknown"


def rate_limit(bucket_name, maximum, window_seconds):
    now = time.monotonic()
    key = (bucket_name, client_ip())

    with rate_lock:
        bucket = rate_buckets[key]

        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()

        if len(bucket) >= maximum:
            return False

        bucket.append(now)
        return True


# ---------------------------------------------------------------------------
# Authentication / CSRF
# ---------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify(error="authentication required"), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_security_helpers():
    return {"csrf_token": csrf_token(), "csp_nonce": g.csp_nonce}


def require_csrf():
    supplied = request.headers.get("X-CSRFToken")

    if not supplied:
        supplied = request.form.get("csrf_token")

    expected = session.get("csrf_token")

    if not expected or not supplied or not secrets.compare_digest(
        supplied, expected
    ):
        abort(403, description="Invalid CSRF token.")


@app.before_request
def security_checks():
    g.csp_nonce = secrets.token_urlsafe(16)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.content_length and request.content_length > MAX_REQUEST_BYTES:
            abort(413)

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.endpoint not in {"login"}:
            if session.get("authenticated"):
                require_csrf()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

PLAYLIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _()'&.-]{0,63}$")
FILENAME_RE = re.compile(r"^[^/\\\x00]{1,180}\.mp3$", re.IGNORECASE)


def validate_playlist_name(value):
    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value or len(value) > MAX_PLAYLIST_NAME_LENGTH:
        return None

    if value in {".", ".."}:
        return None

    if not PLAYLIST_RE.fullmatch(value):
        return None

    # Windows device names are harmless on Linux but rejecting them keeps the
    # application portable and avoids surprising filesystem semantics.
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"}:
        return None

    return value


def validate_quality(value):
    if not isinstance(value, str) or value not in ALLOWED_QUALITIES:
        return None
    return value


def validate_download_url(value):
    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value or len(value) > MAX_URL_LENGTH:
        return None

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    if parsed.scheme.lower() != "https":
        return None

    if not parsed.hostname:
        return None

    if parsed.username or parsed.password:
        return None

    hostname = parsed.hostname.lower().rstrip(".")

    if hostname not in ALLOWED_DOWNLOAD_HOSTS:
        return None

    # Do not accept explicit nonstandard ports.
    if parsed.port is not None and parsed.port != 443:
        return None

    return value


def validate_drive_index(value):
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None

    if index < 0 or index >= len(MUSIC_LOCATIONS):
        return None

    return index


def safe_path_under(root, relative_path):
    """
    Resolve a user-controlled path and prove that it remains below root.
    Returns None if traversal/symlink escape is detected.
    """
    root = root.resolve()

    try:
        candidate = (root / relative_path).resolve()
    except (OSError, RuntimeError):
        return None

    try:
        candidate.relative_to(root)
    except ValueError:
        return None

    return candidate


def validate_song_path(relative_path):
    if not isinstance(relative_path, str):
        return None

    if len(relative_path) > 500:
        return None

    # Reject absolute paths and obvious alternate separators before resolve().
    if relative_path.startswith(("/", "\\")):
        return None

    if "\x00" in relative_path:
        return None

    parts = Path(relative_path).parts

    if any(part in {"", ".", ".."} for part in parts):
        return None

    filename = parts[-1]

    if len(filename) > MAX_FILENAME_LENGTH:
        return None

    if not FILENAME_RE.fullmatch(filename):
        return None

    return relative_path


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def ensure_music_locations():
    for root in MUSIC_LOCATIONS:
        root.mkdir(parents=True, exist_ok=True)
        (root / SINGLES_FOLDER).mkdir(parents=True, exist_ok=True)


def format_bytes(value):
    value = float(value)

    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024

    return f"{value:.1f} PB"


def get_storage_info():
    result = []

    for index, root in enumerate(MUSIC_LOCATIONS):
        try:
            usage = shutil.disk_usage(root)
            result.append({
                "index": index,
                "path": str(root),
                "total": format_bytes(usage.total),
                "used": format_bytes(usage.used),
                "free": format_bytes(usage.free),
                "free_bytes": usage.free,
            })
        except OSError:
            result.append({
                "index": index,
                "path": str(root),
                "total": "Unavailable",
                "used": "Unavailable",
                "free": "Unavailable",
                "free_bytes": 0,
            })

    return result


def get_download_location():
    candidates = []

    for index, root in enumerate(MUSIC_LOCATIONS):
        try:
            free = shutil.disk_usage(root).free
            if free >= MIN_FREE_SPACE_BYTES:
                candidates.append((free, index))
        except OSError:
            continue

    if not candidates:
        raise RuntimeError("No music drive has enough free space.")

    return max(candidates)[1]


def playlist_path(drive_index, playlist_name):
    playlist_name = validate_playlist_name(playlist_name)

    if not playlist_name:
        return None

    root = MUSIC_LOCATIONS[drive_index]
    return safe_path_under(root, playlist_name)


def get_playlist_names():
    names = set()

    for root in MUSIC_LOCATIONS:
        try:
            for item in root.iterdir():
                if item.is_dir() and validate_playlist_name(item.name):
                    names.add(item.name)
        except OSError:
            continue

    names.add(SINGLES_FOLDER)
    return sorted(names, key=str.casefold)


def playlist_exists(name):
    name = validate_playlist_name(name)

    if not name:
        return False

    for root in MUSIC_LOCATIONS:
        directory = safe_path_under(root, name)

        if directory and directory.is_dir():
            return True

    return False


def create_playlist(name):
    name = validate_playlist_name(name)

    if not name:
        return False, "Invalid playlist name."

    if name.casefold() == SINGLES_FOLDER.casefold():
        return False, "That name is reserved."

    if playlist_exists(name):
        return False, "That playlist already exists."

    drive_index = get_download_location()
    directory = playlist_path(drive_index, name)

    if not directory:
        return False, "Invalid playlist path."

    directory.mkdir(parents=False, exist_ok=False)

    return True, f"Created playlist '{name}'."


def delete_playlist(name):
    name = validate_playlist_name(name)

    if not name:
        return False, "Invalid playlist name."

    if name.casefold() == SINGLES_FOLDER.casefold():
        return False, "Singles cannot be deleted."

    found = False

    for root in MUSIC_LOCATIONS:
        directory = safe_path_under(root, name)

        if not directory or not directory.is_dir():
            continue

        found = True

        # Refuse symlink directories even though resolve() protects traversal.
        if directory.is_symlink():
            return False, "Refusing to delete a symbolic link."

        shutil.rmtree(directory)

    if not found:
        return False, "Playlist does not exist."

    return True, f"Deleted playlist '{name}'."


def clean_filename(value):
    value = str(value)

    # Remove characters that are problematic in filenames.
    value = re.sub(r"[\x00-\x1f\x7f]", "", value)
    value = re.sub(r'[<>:"/\\|?*]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")

    if not value:
        value = "Unknown"

    return value[:MAX_FILENAME_LENGTH - 4]


def normalize_title(value):
    value = re.sub(r"\s+", " ", str(value)).strip().casefold()
    return value


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def get_library():
    playlists = {}

    for drive_index, root in enumerate(MUSIC_LOCATIONS):
        try:
            entries = list(root.iterdir())
        except OSError:
            continue

        for directory in entries:
            if not directory.is_dir() or directory.is_symlink():
                continue

            playlist = validate_playlist_name(directory.name)

            if not playlist:
                continue

            playlists.setdefault(playlist, [])

            try:
                songs = list(directory.iterdir())
            except OSError:
                continue

            for song in songs:
                if not song.is_file() or song.is_symlink():
                    continue

                if song.suffix.casefold() != ".mp3":
                    continue

                try:
                    size = song.stat().st_size
                except OSError:
                    continue

                if size > MAX_AUDIO_FILE_BYTES:
                    continue

                relative = f"{playlist}/{song.name}"

                playlists[playlist].append({
                    "title": song.stem,
                    "filename": song.name,
                    "path": relative,
                    "drive_index": drive_index,
                    "size": format_bytes(size),
                    "size_bytes": size,
                    "url": url_for(
                        "serve_music",
                        drive_index=drive_index,
                        filename=relative,
                    ),
                })

    for songs in playlists.values():
        songs.sort(key=lambda item: item["title"].casefold())

    return dict(sorted(playlists.items(), key=lambda item: item[0].casefold()))


def find_existing_song(title, playlist_name):
    wanted = normalize_title(title)

    for drive_index, root in enumerate(MUSIC_LOCATIONS):
        directory = playlist_path(drive_index, playlist_name)

        if not directory or not directory.is_dir():
            continue

        try:
            for song in directory.iterdir():
                if song.is_file() and song.suffix.casefold() == ".mp3":
                    if normalize_title(song.stem) == wanted:
                        return True
        except OSError:
            continue

    return False


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

class DownloadTimeout(Exception):
    pass


def make_progress_hook(deadline):
    def hook(data):
        if time.monotonic() > deadline:
            raise DownloadTimeout()

        if data.get("status") == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            if downloaded > MAX_AUDIO_FILE_BYTES:
                raise ValueError("download exceeded size limit")

    return hook


def progress_hook(data):
    status = data.get("status")

    if status == "downloading":
        # yt-dlp controls the network operation. We do not trust its filename
        # or path as an application path; output templates below are controlled.
        return

    if status == "finished":
        return


def download_single(info, playlist_name, quality):
    title = clean_filename(info.get("title") or "Unknown")

    # Never start an audio download for a track that is 30 minutes or longer.
    # A missing duration is also rejected here; callers should provide the
    # metadata from yt-dlp before invoking the actual download.
    duration = info.get("duration")
    if not isinstance(duration, (int, float)):
        return False, f"Skipped: {title} (duration unavailable)"

    if duration >= MAX_TRACK_DURATION_SECONDS:
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        return False, (
            f"Skipped: {title} ({minutes}:{seconds:02d}) is 30 minutes or longer."
        )

    if find_existing_song(title, playlist_name):
        return False, f"Already exists: {title}"

    drive_index = get_download_location()
    destination = playlist_path(drive_index, playlist_name)

    if not destination:
        return False, "Invalid destination."

    destination.mkdir(parents=False, exist_ok=True)

    # Use a UUID-like token in the temporary name so a malicious/odd title
    # cannot control the path.
    temp_prefix = secrets.token_hex(12)

    output_template = str(
        destination / f".{temp_prefix}.%(ext)s"
    )

    start = time.monotonic()

    options = {
        **yt_dlp_common_options(),
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": False,
        "overwrites": False,
        "restrictfilenames": True,
        "progress_hooks": [make_progress_hook(start + MAX_DOWNLOAD_SECONDS)],
        "max_filesize": MAX_AUDIO_FILE_BYTES,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }],
        # Only these extractors are useful to this application.
        "allowed_extractors": [
            "youtube",
            "youtube:tab",
            "soundcloud",
        ],
    }

    # Do not allow the extractor to process arbitrary URLs here.
    url = validate_download_url(info["webpage_url"])

    if not url:
        return False, "Rejected download URL."

    try:
        download_with_youtube_fallback(
            options, url, destination, temp_prefix, start
        )

        if time.monotonic() - start > MAX_DOWNLOAD_SECONDS:
            return False, "Download exceeded the time limit."

        # Find the newly generated MP3 by our controlled temporary prefix.
        generated = list(destination.glob(f".{temp_prefix}.*"))

        mp3_candidates = [
            path for path in generated
            if path.is_file() and path.suffix.casefold() == ".mp3"
        ]

        if len(mp3_candidates) != 1:
            return False, "Download did not produce exactly one MP3."

        source = mp3_candidates[0]

        if source.stat().st_size > MAX_AUDIO_FILE_BYTES:
            source.unlink(missing_ok=True)
            return False, "Downloaded file exceeds the size limit."

        final_name = clean_filename(title) + ".mp3"
        final_path = safe_path_under(destination, final_name)

        if not final_path or final_path.exists():
            source.unlink(missing_ok=True)
            return False, "A file with that name already exists."

        source.rename(final_path)

        return True, f"Downloaded: {title}"

    except yt_dlp.utils.DownloadError:
        # Expected yt-dlp failures are re-raised so playlist processing can
        # classify the failure and continue with the next entry.
        for path in destination.glob(f".{temp_prefix}.*"):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    except Exception:
        # Clean up temporary artifacts belonging to this request.
        for path in destination.glob(f".{temp_prefix}.*"):
            try:
                path.unlink()
            except OSError:
                pass

        # Do not return internal exception details to the browser.
        app.logger.exception("Download failed")
        return False, "Download failed."

    finally:
        # Best-effort cleanup if a postprocessor leaves a temporary file.
        for path in destination.glob(f".{temp_prefix}.*"):
            try:
                path.unlink()
            except OSError:
                pass


def is_youtube_playlist_url(url):
    """Return True only for the explicit YouTube playlist URL form."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    if host not in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        return False

    return parsed.path.rstrip("/").lower() == "/playlist" and bool(
        parsed.query and "list=" in parsed.query
    )


def playlist_name_from_title(title):
    """Convert a remote playlist title into a safe local playlist name."""
    value = clean_filename(title or "YouTube Playlist")

    # Keep the local playlist naming rules while making ordinary YouTube
    # titles such as "Rock: 2026" safe for the filesystem.
    value = value.replace("_", " ")
    value = re.sub(r"[^A-Za-z0-9 _()'&.-]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = value[:MAX_PLAYLIST_NAME_LENGTH].strip(" .")

    if not value or not value[0].isalnum():
        value = "YouTube Playlist " + value

    if value.casefold() == SINGLES_FOLDER.casefold():
        value = "YouTube Playlist"

    return validate_playlist_name(value) or "YouTube Playlist"


def classify_ytdlp_error(error, title):
    """Turn an expected yt-dlp failure into a user-safe playlist status."""
    text = str(error).casefold()

    if "private video" in text:
        return f"Skipped: {title} (private video)"
    if "sign in to confirm your age" in text or "age-restricted" in text or "requires authentication" in text:
        return f"Skipped: {title} (requires authentication)"
    if "http error 403" in text or "403 forbidden" in text:
        return f"Skipped: {title} (HTTP 403)"
    if "video unavailable" in text or "this video is not available" in text:
        return f"Skipped: {title} (video unavailable)"

    return f"Skipped: {title} (download failed)"


def download_playlist_entry(entry, playlist_name, quality):
    """Extract one playlist entry's metadata and download it independently."""
    entry_id = entry.get("id")
    title = clean_filename(entry.get("title") or "Unknown")

    webpage_url = (
        entry.get("webpage_url")
        or entry.get("original_url")
        or entry.get("url")
    )

    extractor = str(entry.get("extractor_key") or "").casefold()
    if extractor.startswith("youtube") and entry_id:
        webpage_url = f"https://www.youtube.com/watch?v={entry_id}"

    webpage_url = validate_download_url(webpage_url)
    if not webpage_url:
        return False, f"Skipped: {title} (invalid URL)"

    if find_existing_song(title, playlist_name):
        return False, f"Already exists: {title}"

    metadata_options = {
        **yt_dlp_common_options(),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "allowed_extractors": ["youtube", "soundcloud"],
    }

    try:
        # A separate YoutubeDL instance is required for each concurrent worker.
        metadata = extract_metadata_with_youtube_fallback(
            webpage_url, metadata_options
        )

        metadata_title = clean_filename(metadata.get("title") or title)
        duration = metadata.get("duration")

        if not isinstance(duration, (int, float)):
            return False, f"Skipped: {metadata_title} (duration unavailable)"

        if duration >= MAX_TRACK_DURATION_SECONDS:
            minutes = int(duration // 60)
            seconds = int(duration % 60)
            return False, f"Skipped: {metadata_title} ({minutes}:{seconds:02d}, over 30 minute limit)"

        return download_single(
            {"webpage_url": webpage_url, "title": metadata_title, "duration": duration},
            playlist_name,
            quality,
        )

    except yt_dlp.utils.DownloadError as error:
        return False, classify_ytdlp_error(error, title)
    except Exception:
        app.logger.exception("Playlist entry failed: %s", entry_id or title)
        return False, f"Skipped: {title} (download failed)"


def normalize_playlist_entries(info, max_items):
    """Return usable flat-playlist entries without silently truncating them."""
    entries = info.get("entries")
    if not entries:
        return []

    result = []
    for entry in entries:
        if not entry:
            continue
        result.append(entry)
        if len(result) >= max_items:
            break

    return result


def playlist_entry_key(entry):
    """Return a stable key for de-duplicating playlist entries."""
    if not isinstance(entry, dict):
        return None

    entry_id = entry.get("id")
    if entry_id:
        return f"id:{entry_id}"

    webpage_url = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if webpage_url:
        return f"url:{webpage_url}"

    index = entry.get("playlist_index")
    if isinstance(index, int):
        return f"index:{index}"

    return None


def merge_playlist_entries(existing, additional, max_items):
    """Merge playlist results while preserving playlist order and avoiding duplicates."""
    merged = {}
    no_key = []

    for entry in list(existing) + list(additional):
        key = playlist_entry_key(entry)
        if key is None:
            no_key.append(entry)
            continue
        merged.setdefault(key, entry)

    values = list(merged.values()) + no_key

    def sort_key(entry):
        index = entry.get("playlist_index") if isinstance(entry, dict) else None
        if isinstance(index, int):
            return (0, index)
        if isinstance(index, str) and index.isdigit():
            return (0, int(index))
        return (1, 0)

    values.sort(key=sort_key)
    return values[:max_items]


def playlist_count_from_info(info):
    """Return YouTube's reported playlist count when it is trustworthy."""
    value = info.get("playlist_count") if isinstance(info, dict) else None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None

    if value < 0:
        return None
    return value


def extract_youtube_playlist(ydl, url, max_items):
    """Extract as much of a YouTube playlist as yt-dlp/YouTube currently expose.

    YouTube has recently returned incomplete playlist pagination to yt-dlp.  The
    fallback skips the initial webpage request, and targeted playlist-index
    ranges are then attempted when the reported playlist count is still larger
    than the entries already received.
    """
    base_options = {
        "extract_flat": True,
        "noplaylist": False,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 2,
        "playlistend": max_items,
        "allowed_extractors": ["youtube", "youtube:tab"],
    }

    with yt_dlp.YoutubeDL({**ydl.params, **base_options}) as extractor:
        info = extractor.extract_info(url, download=False)

    entries = normalize_playlist_entries(info, max_items)
    reported_count = playlist_count_from_info(info)
    target_count = min(reported_count, max_items) if reported_count else max_items

    if not reported_count or len(entries) >= target_count:
        return info, entries, reported_count

    # Known YouTube workaround: skipping the initial playlist webpage can make
    # yt-dlp use a different API path and expose additional playlist entries.
    fallback_options = {
        **base_options,
        "extractor_args": {"youtubetab": {"skip": ["webpage"]}},
    }

    try:
        with yt_dlp.YoutubeDL({**ydl.params, **fallback_options}) as extractor:
            fallback_info = extractor.extract_info(url, download=False)
        fallback_entries = normalize_playlist_entries(fallback_info, max_items)
        entries = merge_playlist_entries(entries, fallback_entries, max_items)

        if len(fallback_entries) > len(normalize_playlist_entries(info, max_items)):
            info = fallback_info
            reported_count = playlist_count_from_info(fallback_info) or reported_count
            target_count = min(reported_count, max_items) if reported_count else target_count
    except yt_dlp.utils.DownloadError:
        app.logger.warning("YouTube playlist fallback extraction failed", exc_info=True)

    if len(entries) >= target_count:
        return info, entries, reported_count

    # Try bounded index ranges.  Some YouTube sessions expose later entries
    # when playlist-items is supplied even though the initial full enumeration
    # stops early.  Stop when requests stop producing new entries.
    batch_size = PLAYLIST_RECOVERY_BATCH_SIZE
    recovery_requests = 0
    no_progress = 0

    start = max(1, len(entries) + 1)
    while start <= target_count and recovery_requests < MAX_PLAYLIST_RECOVERY_REQUESTS:
        stop = min(target_count, start + batch_size - 1)
        range_spec = f"{start}:{stop}"

        range_options = {
            **base_options,
            "playlist_items": range_spec,
            "extractor_args": {"youtubetab": {"skip": ["webpage"]}},
        }

        before = len(entries)
        recovery_requests += 1

        try:
            with yt_dlp.YoutubeDL({**ydl.params, **range_options}) as extractor:
                range_info = extractor.extract_info(url, download=False)
            range_entries = normalize_playlist_entries(range_info, max_items)
            entries = merge_playlist_entries(entries, range_entries, max_items)
        except yt_dlp.utils.DownloadError:
            app.logger.warning(
                "YouTube playlist range recovery failed for %s-%s",
                start,
                stop,
                exc_info=True,
            )

        if len(entries) == before:
            no_progress += 1
        else:
            no_progress = 0

        if len(entries) >= target_count:
            break

        # If YouTube returns the same first page repeatedly, further range
        # requests are unlikely to recover anything and would just add load.
        if no_progress >= 2:
            break

        start = stop + 1

    return info, entries, reported_count


def describe_playlist_extraction(reported_count, entries, max_items):
    """Return a human-readable extraction status for the final result."""
    if not reported_count:
        return None

    target_count = min(reported_count, max_items)
    if len(entries) >= target_count:
        return None

    if reported_count > max_items:
        return (
            f"YouTube reports {reported_count} playlist entries, but this app is configured "
            f"to process at most {max_items}."
        )

    return (
        f"YouTube reports {reported_count} playlist entries, but yt-dlp only exposed "
        f"{len(entries)} after recovery attempts. {reported_count - len(entries)} "
        "playlist entries were not exposed by YouTube and could not be downloaded."
    )


def extract_youtube_playlist_with_fallback(url, max_items):
    """Extract a YouTube playlist with authenticated access, then public access.

    Public retry is only used for availability/playability-style failures.
    Private/authentication-required playlists remain cookie-backed and are not
    retried as anonymous requests.
    """
    authenticated_options = {
        **yt_dlp_common_options(),
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(authenticated_options) as playlist_ydl:
            return extract_youtube_playlist(
                playlist_ydl, url, max_items
            )
    except yt_dlp.utils.DownloadError as error:
        if not (YOUTUBE_COOKIE_FILE and is_youtube_url(url) and
                should_retry_youtube_without_cookies(error)):
            raise

        app.logger.warning(
            "Authenticated YouTube playlist extraction failed for %s; retrying without cookies",
            url,
        )

        public_options = {
            **yt_dlp_common_options(use_cookies=False, youtube_client_mode="public"),
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(public_options) as playlist_ydl:
            return extract_youtube_playlist(
                playlist_ydl, url, max_items
            )


def download_music(url, playlist_name, quality):
    validated_url = validate_download_url(url)
    quality = validate_quality(quality)

    if not validated_url:
        return False, "Invalid or unsupported URL."

    if not quality:
        return False, "Invalid MP3 quality."

    is_playlist = is_youtube_playlist_url(validated_url)
    playlist_name = validate_playlist_name(playlist_name) if playlist_name else None

    # A YouTube playlist determines its own local destination. Single videos
    # and SoundCloud URLs still require the user to choose a local playlist.
    if not is_playlist and not playlist_name:
        return False, "Select a playlist for a single-track download."

    if playlist_name and not playlist_exists(playlist_name):
        return False, "Playlist does not exist."

    if not rate_limit("download", MAX_DOWNLOADS_PER_WINDOW, DOWNLOAD_WINDOW_SECONDS):
        return False, "Too many downloads. Try again later."

    if not download_lock.acquire(blocking=False):
        return False, "A download is already in progress."

    try:
        max_items = MAX_PLAYLIST_ITEMS if is_playlist else 1

        extraction_warning = None

        if is_playlist:
            # Playlist extraction gets special handling because YouTube can
            # report a large playlist while exposing only the first page to
            # yt-dlp.  The helper retries through alternate extraction paths.
            info, entries, reported_count = extract_youtube_playlist_with_fallback(
                validated_url, max_items
            )
            extraction_warning = describe_playlist_extraction(
                reported_count, entries, max_items
            )
        else:
            options = {
                **yt_dlp_common_options(),
                "extract_flat": True,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 2,
                "allowed_extractors": ["youtube", "youtube:tab", "soundcloud"],
            }

            info = extract_metadata_with_youtube_fallback(
                validated_url, options
            )
            entries = [info]

        if is_playlist:
            remote_title = info.get("title") or info.get("playlist_title")
            automatic_name = playlist_name_from_title(remote_title)

            if not playlist_exists(automatic_name):
                created, create_message = create_playlist(automatic_name)
                if not created and not playlist_exists(automatic_name):
                    return False, f"Could not create playlist: {create_message}"

            playlist_name = automatic_name

        if not playlist_name:
            return False, "Invalid playlist."

        if is_playlist and not entries:
            return False, "No playlist entries were exposed by YouTube."

        if is_playlist and len(entries) > MAX_PLAYLIST_ITEMS:
            return False, "Playlist is too large."

        # Single-video behavior remains sequential and otherwise unchanged.
        if not is_playlist:
            ok, message = download_single(info, playlist_name, quality)
            return ok, message

        # Playlist entries are independent. Process them concurrently while
        # keeping the worker count bounded to avoid exhausting CPU/network/disk.
        messages = [None] * len(entries)
        success_count = 0

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS) as executor:
            futures = {
                executor.submit(download_playlist_entry, entry, playlist_name, quality): index
                for index, entry in enumerate(entries)
            }

            for future in as_completed(futures):
                index = futures[future]
                try:
                    ok, message = future.result()
                except Exception:
                    app.logger.exception("Unhandled playlist worker failure")
                    ok, message = False, "Skipped: playlist entry (download failed)"

                messages[index] = message
                if ok:
                    success_count += 1

        messages = [message for message in messages if message]

        result_messages = []
        if extraction_warning:
            result_messages.append(extraction_warning)
        result_messages.extend(messages)

        if success_count:
            return True, f"Playlist '{playlist_name}': " + " | ".join(result_messages)

        return False, " | ".join(result_messages) or "Nothing was downloaded."

    except yt_dlp.utils.DownloadError as error:
        return False, classify_ytdlp_error(error, "Playlist")
    except Exception:
        app.logger.exception("Download operation failed")
        return False, "Download failed."
    finally:
        download_lock.release()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    if not session.get("authenticated"):
        return redirect(url_for("login"))

    download_notice = consume_download_notice()

    return render_template(
        "index.html",
        playlists=get_playlist_names(),
        storage=get_storage_info(),
        download_notice=download_notice,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    if request.method == "POST":
        if not rate_limit("login", 5, 15 * 60):
            flash("Too many login attempts. Try again later.", "error")
            return render_template("login.html"), 429

        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            isinstance(username, str)
            and isinstance(password, str)
            and len(username) <= 128
            and len(password) <= 1024
            and secrets.compare_digest(username, APP_USERNAME)
            and check_password_hash(APP_PASSWORD_HASH, password)
        ):
            session.clear()
            session["authenticated"] = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True

            next_url = request.args.get("next", "")

            # Prevent open redirects.
            if (
                next_url.startswith("/")
                and not next_url.startswith("//")
            ):
                return redirect(next_url)

            return redirect(url_for("index"))

        # Same response regardless of whether username or password was wrong.
        flash("Invalid username or password.", "error")

    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.post("/download")
@login_required
def start_download():
    if not rate_limit("download-request", 10, 60):
        flash("Too many download requests.", "error")
        return redirect(url_for("index"))

    url = request.form.get("url", "")
    quality = request.form.get("quality", "")
    playlist = request.form.get("playlist", "")

    # Don't echo arbitrary user input into HTML.
    if len(url) > MAX_URL_LENGTH:
        flash("URL is too long.", "error")
        return redirect(url_for("index"))

    ok, message = download_music(url, playlist, quality)

    # Playlist results can be tens of kilobytes. Flask's default session is
    # stored in the browser cookie, so never put the full result there.
    # Store it server-side and keep only a short opaque ID in the session.
    session.pop("_flashes", None)
    session["download_notice_id"] = store_download_notice(
        message, "success" if ok else "error"
    )

    return redirect(url_for("index"))


@app.get("/player")
@login_required
def player():
    return render_template(
        "player.html",
        playlists=get_library(),
    )


@app.get("/library")
@login_required
def library():
    return render_template(
        "library.html",
        playlists=get_library(),
        storage=get_storage_info(),
    )


@app.get("/music/<int:drive_index>/<path:filename>")
@login_required
def serve_music(drive_index, filename):
    drive_index = validate_drive_index(drive_index)

    if drive_index is None:
        abort(404)

    filename = validate_song_path(filename)

    if not filename:
        abort(404)

    root = MUSIC_LOCATIONS[drive_index]
    path = safe_path_under(root, filename)

    if not path or not path.is_file() or path.is_symlink():
        abort(404)

    if path.suffix.casefold() != ".mp3":
        abort(404)

    try:
        size = path.stat().st_size
    except OSError:
        abort(404)

    if size > MAX_AUDIO_FILE_BYTES:
        abort(404)

    # send_file supports conditional/range requests through Werkzeug, which
    # allows seeking in the browser's audio player.
    response = send_file(
        path,
        mimetype="audio/mpeg",
        conditional=True,
        etag=True,
        max_age=0,
    )

    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"

    return response


@app.post("/create-playlist")
@login_required
def create_playlist_route():
    if not rate_limit("playlist-create", 20, 60):
        return jsonify(error="Too many requests."), 429

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(error="JSON object required."), 400

    if len(request.data) > MAX_JSON_BYTES:
        return jsonify(error="Request too large."), 413

    name = validate_playlist_name(data.get("name"))

    if not name:
        return jsonify(error="Invalid playlist name."), 400

    try:
        ok, message = create_playlist(name)
    except Exception:
        app.logger.exception("Playlist creation failed")
        return jsonify(error="Could not create playlist."), 500

    return jsonify(message=message), 200 if ok else 400


@app.post("/delete-playlist")
@login_required
def delete_playlist_route():
    if not rate_limit("playlist-delete", 10, 60):
        return jsonify(error="Too many requests."), 429

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(error="JSON object required."), 400

    name = validate_playlist_name(data.get("name"))

    if not name:
        return jsonify(error="Invalid playlist name."), 400

    if name.casefold() == SINGLES_FOLDER.casefold():
        return jsonify(error="Singles cannot be deleted."), 400

    try:
        ok, message = delete_playlist(name)
    except Exception:
        app.logger.exception("Playlist deletion failed")
        return jsonify(error="Could not delete playlist."), 500

    return jsonify(message=message), 200 if ok else 400


@app.post("/move-song")
@login_required
def move_song():
    if not rate_limit("move-song", 60, 60):
        return jsonify(error="Too many requests."), 429

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(error="JSON object required."), 400

    source_drive = validate_drive_index(data.get("source_drive"))
    source_path = validate_song_path(data.get("source_path"))
    destination_playlist = validate_playlist_name(
        data.get("destination_playlist")
    )

    if source_drive is None:
        return jsonify(error="Invalid source drive."), 400

    if not source_path:
        return jsonify(error="Invalid source path."), 400

    if not destination_playlist:
        return jsonify(error="Invalid destination playlist."), 400

    if not playlist_exists(destination_playlist):
        return jsonify(error="Destination playlist does not exist."), 400

    source_root = MUSIC_LOCATIONS[source_drive]
    source = safe_path_under(source_root, source_path)

    if not source or not source.is_file() or source.is_symlink():
        return jsonify(error="Source file does not exist."), 404

    if source.suffix.casefold() != ".mp3":
        return jsonify(error="Only MP3 files can be moved."), 400

    try:
        if source.stat().st_size > MAX_AUDIO_FILE_BYTES:
            return jsonify(error="File exceeds allowed size."), 400
    except OSError:
        return jsonify(error="Could not inspect source file."), 400

    filename = source.name

    # Prefer an existing destination playlist directory.
    destination = None

    for root in MUSIC_LOCATIONS:
        candidate = playlist_path(
            MUSIC_LOCATIONS.index(root),
            destination_playlist,
        )

        if candidate and candidate.is_dir():
            destination = candidate
            break

    if destination is None:
        return jsonify(error="Destination directory unavailable."), 400

    destination_file = safe_path_under(destination, filename)

    if not destination_file:
        return jsonify(error="Invalid destination path."), 400

    if destination_file.exists():
        return jsonify(error="A file with that name already exists."), 409

    try:
        shutil.move(str(source), str(destination_file))

        # Remove an empty non-Singles source playlist directory.
        source_parent = source.parent

        if (
            source_parent.name.casefold()
            != SINGLES_FOLDER.casefold()
            and source_parent.is_dir()
        ):
            try:
                next(source_parent.iterdir())
            except StopIteration:
                source_parent.rmdir()
            except OSError:
                pass

        return jsonify(message="Song moved successfully.")

    except Exception:
        app.logger.exception("Song move failed")
        return jsonify(error="Could not move song."), 500


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

@app.errorhandler(HTTPException)
def handle_http_error(error):
    if request.path.startswith("/api/"):
        return jsonify(error=error.description), error.code

    return render_template(
        "error.html",
        code=error.code,
        message=error.description,
    ), error.code


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception("Unhandled application error")

    if request.path.startswith("/api/"):
        return jsonify(error="Internal server error."), 500

    return render_template(
        "error.html",
        code=500,
        message="Internal server error.",
    ), 500


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), usb=()"
    )

    # This application uses no third-party JavaScript.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        f"script-src 'self' 'nonce-{g.csp_nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "media-src 'self'; "
        "connect-src 'self';"
    )

    # HSTS should only be enabled when HTTPS is actually configured.
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )

    return response


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ensure_music_locations()

    # Development server is acceptable for initial LAN testing only.
    # For the hardened deployment, use gunicorn/systemd as described below.
    app.run(
        host=os.environ.get("MUSIC_BIND", "127.0.0.1"),
        port=int(os.environ.get("MUSIC_PORT", "5000")),
        debug=False,
    )
