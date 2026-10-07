import json
import os
import subprocess
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
from urllib.request import Request, urlopen

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
RESET_SCRIPT = BASE_DIR / "reset.sh"
RESET_LOG = STATE_DIR / "reset.log"
NOTICE_DIR = STATE_DIR / "notices"
NOTICE_DIR.mkdir(parents=True, exist_ok=True)

MUSIC_LOCATIONS = [
    Path(path).expanduser().resolve()
    for path in os.environ.get("MUSIC_PATHS", "").split(":")
    if path.strip()
]

if not MUSIC_LOCATIONS:
    raise RuntimeError(
        "Set MUSIC_PATHS to one or more colon-separated music directories."
    )

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
    "on.soundcloud.com",
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


YOUTUBE_BROWSER = os.environ.get("MUSIC_YOUTUBE_BROWSER", "firefox").strip().lower()
YOUTUBE_BROWSER_PROFILE = os.environ.get("MUSIC_YOUTUBE_BROWSER_PROFILE", "").strip() or None
YOUTUBE_BROWSER_CONTAINER = os.environ.get("MUSIC_YOUTUBE_BROWSER_CONTAINER", "").strip() or None

if YOUTUBE_BROWSER not in {"firefox", "chrome", "chromium", "brave", "edge", "opera", "vivaldi", "whale", "safari"}:
    raise RuntimeError("Unsupported MUSIC_YOUTUBE_BROWSER value.")


def browser_cookie_options():
    """Return yt-dlp options that read the live browser cookie database."""
    # yt-dlp accepts (browser, profile, keyring, container) in its Python API.
    # Leaving profile unset makes Firefox use the most recently accessed profile,
    # matching: --cookies-from-browser firefox
    return {
        "cookiesfrombrowser": (
            YOUTUBE_BROWSER,
            YOUTUBE_BROWSER_PROFILE,
            None,
            YOUTUBE_BROWSER_CONTAINER,
        ),
    }



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


YTDLP_VERBOSE = os.environ.get("MUSIC_YTDLP_VERBOSE", "1") == "1"


def yt_dlp_logging_options():
    """Return consistent yt-dlp logging settings for server diagnostics."""
    if YTDLP_VERBOSE:
        return {
            "verbose": True,
            "quiet": False,
            "no_warnings": False,
        }

    return {
        "quiet": True,
        "no_warnings": True,
    }


def yt_dlp_common_options(use_cookies=True, youtube_client_mode="authenticated"):
    """Build yt-dlp options for either authenticated or public YouTube access.

    Authenticated YouTube access is tried first when cookies are configured.
    Public YouTube retries intentionally omit cookies because a valid account
    session can make an otherwise public video return UNPLAYABLE for that
    session/client combination.
    """
    options = {
        "remote_components": ["ejs:github"],
        **yt_dlp_logging_options(),
    }

    deno_path = Path(os.environ.get("MUSIC_DENO_PATH", str(Path.home() / ".deno" / "bin" / "deno"))).expanduser()
    if deno_path.is_file():
        options["js_runtimes"] = {"deno": {"path": str(deno_path)}}

    if use_cookies:
        options.update(browser_cookie_options())

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
        if not (is_youtube_url(url) and
                should_retry_youtube_without_cookies(error)):
            raise

        app.logger.warning(
            "Authenticated YouTube download failed for %s; retrying without cookies",
            url,
        )
        cleanup_temp_downloads(destination, temp_prefix)

        public_options = dict(options)
        public_options.pop("cookiesfrombrowser", None)
        # Remove the authenticated client selection and let yt-dlp use its
        # normal public clients for the fallback.
        public_options.pop("extractor_args", None)

        with yt_dlp.YoutubeDL(public_options) as ydl:
            ydl.download([url])


def extract_metadata_with_youtube_fallback(url, options):
    """Extract metadata with cookies first and a public retry when appropriate.

    Do not globally set ``allowed_extractors`` here. Normal YouTube URLs must
    reach yt-dlp's YouTube extractor, and canonical SoundCloud URLs must reach
    the SoundCloud extractor. yt-dlp can use its generic extractor automatically
    for short redirect URLs such as on.soundcloud.com.
    """
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as error:
        if not (is_youtube_url(url) and
                should_retry_youtube_without_cookies(error)):
            raise

        app.logger.warning(
            "Authenticated YouTube metadata extraction failed for %s; retrying without cookies",
            url,
        )
        public_options = dict(options)
        public_options.pop("cookiesfrombrowser", None)
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

# Background download jobs. The app intentionally runs with one Gunicorn
# worker, so this in-process state is shared by the request and polling calls.
download_jobs = {}
download_jobs_lock = threading.Lock()
DOWNLOAD_JOB_TTL_SECONDS = 60 * 60


def create_download_job():
    job_id = secrets.token_urlsafe(24)
    with download_jobs_lock:
        download_jobs[job_id] = {
            "status": "queued",
            "phase": "Queued",
            "total": 0,
            "completed": 0,
            "current_index": 0,
            "current_title": "",
            "current_percent": 0,
            "current_downloaded": 0,
            "current_total": 0,
            "message": "",
            "created": time.monotonic(),
            "updated": time.monotonic(),
            "tracks": {},
        }
    return job_id


def update_download_job(job_id, **changes):
    now = time.monotonic()
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return
        job.update(changes)
        job["updated"] = now


def update_download_track(job_id, index, **changes):
    now = time.monotonic()
    with download_jobs_lock:
        job = download_jobs.get(job_id)
        if not job:
            return
        track = dict(job["tracks"].get(str(index), {}))
        track.update(changes)
        job["tracks"][str(index)] = track
        job["updated"] = now


def get_download_job(job_id):
    now = time.monotonic()
    with download_jobs_lock:
        # Expire old completed jobs to avoid unbounded memory growth.
        for key, job in list(download_jobs.items()):
            if now - job["updated"] > DOWNLOAD_JOB_TTL_SECONDS:
                download_jobs.pop(key, None)

        job = download_jobs.get(job_id)
        if not job:
            return None

        # Do not expose internal timing data or the per-track worker state.
        return {k: v for k, v in job.items() if k not in {"created", "updated", "tracks"}}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def client_ip():
    # Do NOT trust X-Forwarded-For because this server is not configured to
    # trust a specific reverse proxy.
    return request.remote_addr or "unknown"


def rate_limit(bucket_name, maximum, window_seconds, ip_address=None):
    now = time.monotonic()
    if ip_address is None:
        ip_address = client_ip()
    key = (bucket_name, ip_address)

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


def resolve_soundcloud_short_url(value):
    """Resolve an on.soundcloud.com share link to its canonical SoundCloud URL.

    SoundCloud share links return an HTTP redirect to soundcloud.com. Resolving
    that redirect before calling yt-dlp lets yt-dlp select its native
    SoundCloud extractor instead of remaining in the generic extractor.
    Only the known SoundCloud short-link host is resolved, and the final URL
    must pass the normal download URL allowlist.
    """
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower().rstrip(".")

    if hostname != "on.soundcloud.com":
        return value

    try:
        request = Request(
            value,
            method="HEAD",
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MusicDownloader/1.0)",
            },
        )
        with urlopen(request, timeout=10) as response:
            resolved_url = response.geturl()
    except Exception as error:
        app.logger.warning(
            "Could not resolve SoundCloud short URL %s: %s",
            value,
            error,
        )
        return None

    resolved_url = validate_download_url(resolved_url)
    if not resolved_url:
        app.logger.warning(
            "SoundCloud short URL %s resolved to a rejected URL",
            value,
        )
        return value

    app.logger.info(
        "Resolved SoundCloud short URL %s -> %s",
        value,
        resolved_url,
    )
    return resolved_url


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


def title_with_fallback(info, fallback_id=None):
    """Return a usable filename/title even when yt-dlp provides no title."""
    if isinstance(info, dict):
        raw_title = info.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            return clean_filename(raw_title)

        video_id = info.get("id") or fallback_id
        if video_id:
            return clean_filename(f"YouTube - {video_id}")

    if fallback_id:
        return clean_filename(f"YouTube - {fallback_id}")

    return "Untitled"


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def get_library_playlist(playlist_name):
    """Load only one playlist from the music library.

    The old get_library() implementation scanned every playlist and every MP3
    whenever the library page was opened.  For large libraries that made the
    initial page unnecessarily expensive.  This function scans only the
    playlist the user selected, across all configured music drives.
    """
    playlist_name = validate_playlist_name(playlist_name)
    if not playlist_name:
        return []

    songs = []

    for drive_index, root in enumerate(MUSIC_LOCATIONS):
        directory = playlist_path(drive_index, playlist_name)

        if not directory or not directory.is_dir() or directory.is_symlink():
            continue

        try:
            entries = list(directory.iterdir())
        except OSError:
            continue

        for song in entries:
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

            relative = f"{playlist_name}/{song.name}"

            songs.append({
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

    songs.sort(key=lambda item: (item["title"].casefold(), item["drive_index"]))
    return songs


def get_library():
    """Compatibility helper for the player page.

    The player still needs the complete library because it is designed as a
    combined player view. The /library page deliberately does not call this.
    """
    return {
        name: get_library_playlist(name)
        for name in get_playlist_names()
    }


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


def make_progress_hook(deadline, progress_callback=None):
    def hook(data):
        if time.monotonic() > deadline:
            raise DownloadTimeout()

        status = data.get("status")
        downloaded = data.get("downloaded_bytes") or 0
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0

        if downloaded > MAX_AUDIO_FILE_BYTES:
            raise ValueError("download exceeded size limit")

        if progress_callback:
            percent = None
            if total:
                percent = max(0, min(100, int(downloaded * 100 / total)))
            progress_callback(status, downloaded, total, percent)

    return hook


def download_single(
    info,
    playlist_name,
    quality,
    progress_callback=None,
    download_url=None,
):
    title = title_with_fallback(info)

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
        **yt_dlp_logging_options(),
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "continuedl": False,
        "overwrites": False,
        "restrictfilenames": True,
        "progress_hooks": [make_progress_hook(start + MAX_DOWNLOAD_SECONDS, progress_callback)],
        "max_filesize": MAX_AUDIO_FILE_BYTES,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }],
    }

    # Always download from the original URL supplied by the user/caller.
    # Some extractors (notably SoundCloud) can put an internal/embed URL into
    # info["webpage_url"]. Feeding that derived URL back into yt-dlp can cause
    # errors such as "Unsupported URL: https://w.soundcloud.com/player/...".
    # The original on.soundcloud.com URL is exactly what yt-dlp successfully
    # resolves through its generic extractor.
    if download_url is None:
        download_url = info.get("webpage_url")

    url = validate_download_url(download_url)

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


def download_playlist_entry(entry, playlist_name, quality, progress_callback=None):
    """Extract one playlist entry's metadata and download it independently."""
    entry_id = entry.get("id")
    title = title_with_fallback(entry, entry_id)

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
        **yt_dlp_logging_options(),
        "socket_timeout": 20,
        "retries": 2,
    }

    try:
        # A separate YoutubeDL instance is required for each concurrent worker.
        metadata = extract_metadata_with_youtube_fallback(
            webpage_url, metadata_options
        )

        metadata_title = title_with_fallback(metadata, entry_id)
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
            progress_callback=progress_callback,
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
        **yt_dlp_logging_options(),
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
        **yt_dlp_logging_options(),
    }

    try:
        with yt_dlp.YoutubeDL(authenticated_options) as playlist_ydl:
            return extract_youtube_playlist(
                playlist_ydl, url, max_items
            )
    except yt_dlp.utils.DownloadError as error:
        if not (is_youtube_url(url) and
                should_retry_youtube_without_cookies(error)):
            raise

        app.logger.warning(
            "Authenticated YouTube playlist extraction failed for %s; retrying without cookies",
            url,
        )

        public_options = {
            **yt_dlp_common_options(use_cookies=False, youtube_client_mode="public"),
            **yt_dlp_logging_options(),
        }
        with yt_dlp.YoutubeDL(public_options) as playlist_ydl:
            return extract_youtube_playlist(
                playlist_ydl, url, max_items
            )


def download_music(url, playlist_name, quality, job_id=None, ip_address=None):
    validated_url = validate_download_url(url)
    quality = validate_quality(quality)

    if not validated_url:
        return False, "Invalid or unsupported URL."

    # Resolve SoundCloud's share-link redirect before metadata extraction.
    # This makes yt-dlp receive the canonical soundcloud.com URL and use its
    # native SoundCloud extractor instead of getting stuck at [generic].
    resolved_url = resolve_soundcloud_short_url(validated_url)
    if resolved_url is None:
        return False, "Could not resolve the SoundCloud share link. Try the full soundcloud.com URL."
    validated_url = resolved_url

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

    if not rate_limit(
        "download", MAX_DOWNLOADS_PER_WINDOW, DOWNLOAD_WINDOW_SECONDS, ip_address
    ):
        return False, "Too many downloads. Try again later."

    if not download_lock.acquire(blocking=False):
        return False, "A download is already in progress."

    try:
        max_items = MAX_PLAYLIST_ITEMS if is_playlist else 1

        if job_id:
            update_download_job(
                job_id, status="running",
                phase="Extracting playlist" if is_playlist else "Extracting track",
            )

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
                **yt_dlp_logging_options(),
                "socket_timeout": 20,
                "retries": 2,
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

        if job_id:
            update_download_job(
                job_id,
                status="running",
                phase="Downloading playlist" if is_playlist else "Downloading track",
                total=len(entries),
                completed=0,
                current_percent=0,
                current_downloaded=0,
                current_total=0,
            )

        # Single-video behavior remains sequential and otherwise unchanged.
        if not is_playlist:
            def single_progress(status, downloaded, total, percent):
                if job_id:
                    update_download_job(
                        job_id,
                        phase="Downloading track",
                        current_percent=percent if percent is not None else 0,
                        current_downloaded=downloaded,
                        current_total=total,
                        current_title=title_with_fallback(info),
                    )

            ok, message = download_single(
                info,
                playlist_name,
                quality,
                progress_callback=single_progress,
                download_url=validated_url,
            )
            if job_id:
                update_download_job(
                    job_id, completed=1, total=1, current_percent=100 if ok else 0,
                    phase="Complete" if ok else "Finished with errors",
                )
            return ok, message

        # Playlist entries are independent. Process them concurrently while
        # keeping the worker count bounded to avoid exhausting CPU/network/disk.
        messages = [None] * len(entries)
        success_count = 0

        def make_track_progress(index, title):
            def callback(status, downloaded, total, percent):
                if not job_id:
                    return
                track_status = "downloading" if status == "downloading" else status
                update_download_track(
                    job_id, index, title=title, status=track_status,
                    percent=percent if percent is not None else 0,
                    downloaded=downloaded, total=total,
                )
                update_download_job(
                    job_id, current_index=index + 1, current_title=title,
                    current_percent=percent if percent is not None else 0,
                    current_downloaded=downloaded, current_total=total,
                )
            return callback

        for index, entry in enumerate(entries):
            if job_id:
                update_download_track(
                    job_id, index, title=title_with_fallback(entry, entry.get("id")),
                    status="queued", percent=0, downloaded=0, total=0,
                )

        with ThreadPoolExecutor(max_workers=MAX_PARALLEL_DOWNLOADS) as executor:
            futures = {
                executor.submit(
                    download_playlist_entry, entry, playlist_name, quality,
                    make_track_progress(index, title_with_fallback(entry, entry.get("id"))),
                ): index
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
                update_download_track(
                    job_id, index, status="completed" if ok else "failed",
                    percent=100 if ok else 0,
                ) if job_id else None
                if ok:
                    success_count += 1

                if job_id:
                    update_download_job(
                        job_id, completed=sum(1 for m in messages if m is not None),
                        current_percent=100 if ok else 0,
                    )

        messages = [message for message in messages if message]

        result_messages = []
        if extraction_warning:
            result_messages.append(extraction_warning)
        result_messages.extend(messages)

        if success_count:
            message = f"Playlist '{playlist_name}': " + " | ".join(result_messages)
            if job_id:
                update_download_job(
                    job_id, status="completed", phase="Complete",
                    completed=len(entries), total=len(entries), message=message,
                    current_percent=100,
                )
            return True, message

        message = " | ".join(result_messages) or "Nothing was downloaded."
        if job_id:
            update_download_job(
                job_id, status="completed", phase="Finished with errors",
                completed=len(entries), total=len(entries), message=message,
            )
        return False, message

    except yt_dlp.utils.DownloadError as error:
        message = classify_ytdlp_error(error, "Playlist")
        if job_id:
            update_download_job(job_id, status="completed", phase="Finished with errors", message=message)
        return False, message
    except Exception:
        app.logger.exception("Download operation failed")
        message = "Download failed."
        if job_id:
            update_download_job(job_id, status="error", phase="Failed", message=message)
        return False, message
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


@app.post("/api/reset")
@login_required
def reset_application():
    """Run the project-local reset.sh script outside the request lifetime."""
    if not rate_limit("reset", 1, 5 * 60):
        return jsonify(error="Reset was recently started. Try again later."), 429

    if not RESET_SCRIPT.is_file():
        return jsonify(error="reset.sh was not found in the application directory."), 500

    if not os.access(RESET_SCRIPT, os.R_OK):
        return jsonify(error="reset.sh is not readable by the music service."), 500

    try:
        RESET_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_handle = RESET_LOG.open("ab", buffering=0)
        log_handle.write(("\n\n===== reset started %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S %z")).encode())

        # reset.sh is intentionally launched as a detached process because the
        # script normally ends by restarting this service. Waiting for it here
        # would make the HTTP request race with the service shutdown.
        process = subprocess.Popen(
            ["/bin/bash", str(RESET_SCRIPT)],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        log_handle.close()
    except OSError as exc:
        try:
            log_handle.close()
        except Exception:
            pass
        app.logger.exception("Could not start reset script")
        return jsonify(error=f"Could not start reset.sh: {exc}"), 500

    return jsonify(message="Reset started. The server may restart shortly.", pid=process.pid), 202


@app.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.post("/download")
@login_required
def start_download():
    if not rate_limit("download-request", 10, 60):
        return jsonify(error="Too many download requests."), 429

    url = request.form.get("url", "")
    quality = request.form.get("quality", "")
    playlist = request.form.get("playlist", "")

    if len(url) > MAX_URL_LENGTH:
        return jsonify(error="URL is too long."), 400

    job_id = create_download_job()
    # Capture request data before the background thread starts. Flask's
    # request context ends when this HTTP handler returns, so the worker
    # must never call request.remote_addr itself.
    ip_address = client_ip()

    def worker():
        ok, message = download_music(
            url, playlist, quality, job_id=job_id, ip_address=ip_address
        )
        update_download_job(
            job_id,
            status="completed" if ok else "error",
            phase="Complete" if ok else "Failed",
            message=message,
        )

    threading.Thread(target=worker, name=f"download-{job_id[:8]}", daemon=True).start()

    return jsonify(job_id=job_id), 202


@app.get("/api/download-status/<job_id>")
@login_required
def download_status(job_id):
    if not isinstance(job_id, str) or len(job_id) > 128:
        return jsonify(error="Invalid job."), 400

    job = get_download_job(job_id)
    if not job:
        return jsonify(error="Download job not found."), 404

    return jsonify(job)


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
    # Do not scan every MP3 here. The browser loads one selected playlist
    # through /api/library/<playlist_name> after the user chooses it.
    return render_template(
        "library.html",
        playlist_names=get_playlist_names(),
        storage=get_storage_info(),
    )


@app.get("/api/playlists")
@login_required
def playlists_api():
    """Return playlist names for persistent-player controls."""
    return jsonify(playlists=get_playlist_names())


@app.get("/api/library/<path:playlist_name>")
@login_required
def library_playlist_api(playlist_name):
    """Return songs for exactly one selected playlist."""
    playlist_name = validate_playlist_name(playlist_name)

    if not playlist_name:
        return jsonify(error="Invalid playlist name."), 400

    if not playlist_exists(playlist_name):
        return jsonify(error="Playlist does not exist."), 404

    try:
        songs = get_library_playlist(playlist_name)
    except Exception:
        app.logger.exception("Library playlist loading failed: %s", playlist_name)
        return jsonify(error="Could not load playlist."), 500

    return jsonify(playlist=playlist_name, songs=songs)


@app.post("/api/library/song/delete")
@login_required
def delete_library_song():
    """Delete one MP3 after validating its drive and library-relative path."""
    if not rate_limit("library-song-delete", 30, 60):
        return jsonify(error="Too many requests."), 429

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON object required."), 400

    drive_index = validate_drive_index(data.get("drive_index"))
    relative_path = validate_song_path(data.get("path"))

    if drive_index is None:
        return jsonify(error="Invalid drive."), 400
    if not relative_path:
        return jsonify(error="Invalid song path."), 400

    root = MUSIC_LOCATIONS[drive_index]
    song = safe_path_under(root, relative_path)

    if not song or not song.is_file() or song.is_symlink():
        return jsonify(error="Song does not exist."), 404

    if song.suffix.casefold() != ".mp3":
        return jsonify(error="Only MP3 files can be deleted."), 400

    try:
        song.unlink()
    except OSError:
        app.logger.exception("Song deletion failed: %s", relative_path)
        return jsonify(error="Could not delete song."), 500

    return jsonify(message="Song deleted successfully.")


@app.post("/api/library/song/rename")
@login_required
def rename_library_song():
    """Rename one MP3 without allowing the destination to leave its playlist."""
    if not rate_limit("library-song-rename", 30, 60):
        return jsonify(error="Too many requests."), 429

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="JSON object required."), 400

    drive_index = validate_drive_index(data.get("drive_index"))
    relative_path = validate_song_path(data.get("path"))
    requested_name = data.get("name")

    if drive_index is None:
        return jsonify(error="Invalid drive."), 400
    if not relative_path:
        return jsonify(error="Invalid song path."), 400
    if not isinstance(requested_name, str):
        return jsonify(error="Song name must be text."), 400

    requested_name = requested_name.strip()
    if requested_name.lower().endswith(".mp3"):
        requested_name = requested_name[:-4]

    # Reuse the filename sanitizer, then require a non-empty result. The
    # extension is always controlled by the server.
    new_stem = clean_filename(requested_name)
    if not new_stem or new_stem.casefold() in {".", ".."}:
        return jsonify(error="Invalid song name."), 400

    root = MUSIC_LOCATIONS[drive_index]
    song = safe_path_under(root, relative_path)

    if not song or not song.is_file() or song.is_symlink():
        return jsonify(error="Song does not exist."), 404

    if song.suffix.casefold() != ".mp3":
        return jsonify(error="Only MP3 files can be renamed."), 400

    destination = safe_path_under(song.parent, new_stem + ".mp3")
    if not destination:
        return jsonify(error="Invalid destination name."), 400

    if destination == song:
        return jsonify(
            message="Song name is unchanged.",
            title=song.stem,
            filename=song.name,
            path=relative_path,
        )

    if destination.exists():
        return jsonify(error="A song with that name already exists."), 409

    try:
        song.rename(destination)
    except OSError:
        app.logger.exception("Song rename failed: %s", relative_path)
        return jsonify(error="Could not rename song."), 500

    new_relative_path = str(Path(relative_path).parent / destination.name)
    if new_relative_path.startswith("."):
        new_relative_path = destination.name

    return jsonify(
        message="Song renamed successfully.",
        title=destination.stem,
        filename=destination.name,
        path=new_relative_path,
        url=url_for(
            "serve_music",
            drive_index=drive_index,
            filename=new_relative_path,
        ),
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

    # Do not let the browser treat local MP3 responses as reusable HTTP cache
    # entries. The media element may still buffer data while a track is active,
    # but the server response itself should not become a persistent cache item.
    response.headers["Cache-Control"] = "private, no-store, max-age=0, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
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
