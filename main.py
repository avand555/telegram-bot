import os
import re
import gc
import math
import time
import stat
import base64
import asyncio
import ctypes
import datetime as dt
import html
import json
import mimetypes
import secrets
import shutil
import uuid
import zipfile
import threading
import subprocess
import logging
import hmac
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

# ============================================
# --- 0. RUNTIME / LOGGING ---
# ============================================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("telegram-r2-bot")

mimetypes.add_type("application/vnd.apple.mpegurl", ".m3u8")
mimetypes.add_type("video/mp2t", ".ts")

# ============================================
# --- 1. THIRD-PARTY IMPORTS ---
# ============================================
from telethon import TelegramClient, events, Button
from telethon.network import ConnectionTcpFull
from aiohttp import web, ClientSession, ClientTimeout, ClientResponseError
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import gdown
import yt_dlp
import nest_asyncio

nest_asyncio.apply()

# ============================================
# --- 2. CONFIGURATION ---
# ============================================

def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer.") from exc


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be a number.") from exc


API_ID = env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
def parse_admin_ids(value: str) -> set[int]:
    ids: set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ids.add(int(raw))
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid ADMIN_IDS value: {raw!r}. "
                "Use comma-separated numeric Telegram user IDs."
            ) from exc
    return ids


ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", ""))

R2_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME", "").strip()
R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "").strip().rstrip("/")

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin").strip()
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "").strip()

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
KOYEB_PUBLIC_URL = os.getenv("KOYEB_PUBLIC_URL", "").strip().rstrip("/")
KOYEB_APP_NAME = os.getenv("KOYEB_APP_NAME", "").strip()

PORT = env_int("PORT", 8000)
MAX_CONCURRENT_JOBS = max(1, env_int("MAX_CONCURRENT_JOBS", 4))
R2_UPLOAD_WORKERS = max(1, env_int("R2_UPLOAD_WORKERS", 8))
R2_MULTIPART_CONCURRENCY = max(1, env_int("R2_MULTIPART_CONCURRENCY", 4))
R2_MULTIPART_CHUNK_MB = max(5, env_int("R2_MULTIPART_CHUNK_MB", 8))
DIRECT_LINK_TTL = max(300, env_int("DIRECT_LINK_TTL", 86400))
ACTION_TTL = max(3600, env_int("ACTION_TTL", 30 * 86400))
PROGRESS_UPDATE_SECONDS = max(2, env_int("PROGRESS_UPDATE_SECONDS", 4))
DOWNLOAD_CHUNK_MB = max(1, env_int("DOWNLOAD_CHUNK_MB", 1))
TELEGRAM_STREAM_CHUNK_KB = max(128, env_int("TELEGRAM_STREAM_CHUNK_KB", 512))
MAX_ZIP_FILES = max(1, env_int("MAX_ZIP_FILES", 50000))
MAX_ZIP_UNCOMPRESSED_GB = max(1, env_int("MAX_ZIP_UNCOMPRESSED_GB", 20))
HTTP_TIMEOUT_SECONDS = max(30, env_int("HTTP_TIMEOUT_SECONDS", 120))

PUBLIC_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce,"
    "http://tracker.openbittorrent.com:80/announce,"
    "udp://opentracker.i2p.rocks:6969/announce"
)

# Shared application state. Direct Telegram links are intentionally in-memory;
# they expire automatically and do not survive a container restart.
global_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
routes = web.RouteTableDef()
link_storage: dict[str, dict] = {}
active_tasks: dict[str, dict] = {}
pending_torrent_selections: dict[tuple[int, int], dict] = {}

client = TelegramClient(
    "bot_session",
    API_ID,
    API_HASH,
    connection=ConnectionTcpFull,
)

# ============================================
# --- 3. VALIDATION / HELPERS ---
# ============================================

def validate_startup_config() -> None:
    missing = []
    if API_ID <= 0:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_IDS:
        missing.append("ADMIN_IDS")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    if not DASHBOARD_PASS:
        logger.warning(
            "DASHBOARD_PASS is not set. The R2 dashboard will remain locked."
        )


def validate_r2_config() -> None:
    missing = []
    if not R2_ACCOUNT_ID:
        missing.append("R2_ACCOUNT_ID")
    if not R2_ACCESS_KEY_ID:
        missing.append("R2_ACCESS_KEY_ID")
    if not R2_SECRET_ACCESS_KEY:
        missing.append("R2_SECRET_ACCESS_KEY")
    if not R2_BUCKET_NAME:
        missing.append("R2_BUCKET_NAME")

    if missing:
        raise RuntimeError(
            "Cloudflare R2 is not configured. Missing: " + ", ".join(missing)
        )


def free_memory() -> None:
    """Run Python GC and trim glibc malloc heap when available."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def human_size(bytes_val: int | float | None) -> str:
    if not bytes_val:
        return "0 B"

    value = float(bytes_val)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def get_status_text(
    action: str,
    filename: str,
    current: int,
    total: int,
    start_time: float,
) -> str:
    diff = max(time.time() - start_time, 0.001)
    percent = (current / total) * 100 if total > 0 else 0.0
    percent = min(percent, 100.0)
    speed = current / diff
    blocks = min(10, max(0, int(percent // 10)))
    progress_bar = "■" * blocks + "□" * (10 - blocks)

    return (
        f"🚀 **{action}**\n"
        f"📦 `{filename}`\n\n"
        f"🌀 **Progress:** `[{progress_bar}] {percent:.2f}%`\n"
        f"⚡ **Speed:** `{human_size(speed)}/s`\n"
        f"📂 **Size:** `{human_size(current)} / {human_size(total)}`"
    )


def get_readable_time(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def sanitize_filename(filename: str | None, fallback: str = "downloaded_file") -> str:
    filename = os.path.basename(filename or "").strip()
    filename = filename.replace("\x00", "")
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = re.sub(r"\s+", " ", filename).strip(" .")
    return filename or fallback


def sanitize_prefix(prefix: str | None, fallback: str = "uploads") -> str:
    prefix = (prefix or "").replace("\\", "/").strip()
    parts = []
    for part in prefix.split("/"):
        part = re.sub(r"[\x00:*?\"<>|]", "", part).strip(" .")
        if part and part not in {".", ".."}:
            parts.append(part)
    return "/".join(parts) or fallback


def clean_double_extension(filename: str) -> str:
    filename = sanitize_filename(filename)
    while filename.lower().endswith((".mp4.mp4", ".mkv.mkv", ".zip.zip", ".ts.ts")):
        filename = filename[:-4]
    return filename


def get_unique_filename(filepath: str) -> str:
    filepath = clean_double_extension(filepath)
    if not os.path.exists(filepath):
        return filepath

    base, ext = os.path.splitext(filepath)
    counter = 1
    candidate = filepath
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def get_largest_file(folder_path: str) -> str | None:
    largest = None
    max_size = 0

    for root, _, files in os.walk(folder_path):
        for name in files:
            path = os.path.join(root, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > max_size:
                max_size = size
                largest = path
    return largest


def build_public_base_url() -> str:
    base = PUBLIC_BASE_URL or KOYEB_PUBLIC_URL
    if not base and KOYEB_APP_NAME:
        base = f"https://{KOYEB_APP_NAME}.koyeb.app"
    return base.rstrip("/")


def build_r2_public_url(s3_key: str) -> str:
    if not R2_PUBLIC_URL:
        return s3_key
    return f"{R2_PUBLIC_URL}/{quote(s3_key, safe='/')}"


def schedule_message_edit(
    loop: asyncio.AbstractEventLoop,
    msg,
    text: str,
    *,
    buttons=None,
) -> None:
    try:
        future = asyncio.run_coroutine_threadsafe(
            msg.edit(text, buttons=buttons),
            loop,
        )
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
    except Exception:
        pass


def new_task(task_type: str) -> tuple[str, threading.Event]:
    code = secrets.token_urlsafe(8)
    cancel_event = threading.Event()
    active_tasks[code] = {
        "type": task_type,
        "cancel_event": cancel_event,
        "process": None,
        "created_at": time.time(),
    }
    return code, cancel_event


def remove_task(code: str | None) -> None:
    if code:
        active_tasks.pop(code, None)


def task_cancelled(code: str | None) -> bool:
    if not code:
        return False
    item = active_tasks.get(code)
    return bool(item and item["cancel_event"].is_set())


def purge_expired_link_storage() -> None:
    now = time.time()
    expired = []

    for code, item in link_storage.items():
        created = float(item.get("created_at", 0))
        kind = item.get("kind", "telegram")
        ttl = ACTION_TTL if kind == "r2" else DIRECT_LINK_TTL
        if now - created > ttl:
            expired.append(code)

    for code in expired:
        link_storage.pop(code, None)

    expired_tasks = []
    for code, item in active_tasks.items():
        if now - float(item.get("created_at", now)) > 86400:
            expired_tasks.append(code)
    for code in expired_tasks:
        active_tasks.pop(code, None)

    expired_pending = []
    for key, item in pending_torrent_selections.items():
        created_at = float(item.get("created_at", now))
        if now - created_at > 900:
            expired_pending.append(key)

    for key in expired_pending:
        item = pending_torrent_selections.pop(key, None)
        if item:
            shutil.rmtree(item.get("workdir", ""), ignore_errors=True)


# ============================================
# --- 4. CLOUDFLARE R2 ---
# ============================================

@lru_cache(maxsize=1)
def get_r2_client():
    validate_r2_config()

    clean_id = (
        R2_ACCOUNT_ID
        .replace("https://", "")
        .replace("http://", "")
        .split(".")[0]
        .strip("/")
    )
    endpoint = f"https://{clean_id}.r2.cloudflarestorage.com"

    r2_config = Config(
        region_name="auto",
        signature_version="s3v4",
        connect_timeout=30,
        read_timeout=120,
        retries={"max_attempts": 4, "mode": "standard"},
    )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=r2_config,
    )


def r2_extra_args(file_path: str) -> dict:
    filename = os.path.basename(file_path)
    ext = os.path.splitext(filename)[1].lower()
    mime_type, _ = mimetypes.guess_type(filename)
    mime_type = mime_type or "application/octet-stream"

    if ext == ".m3u8":
        cache_control = "no-cache, no-store, must-revalidate"
    elif ext in {".ts", ".mp4", ".mkv", ".webm", ".mov", ".mp3", ".aac", ".m4s"}:
        cache_control = "public, max-age=31536000, immutable"
    else:
        cache_control = "public, max-age=86400"

    return {
        "ContentType": mime_type,
        "ContentDisposition": "inline",
        "CacheControl": cache_control,
    }


def sync_get_smart_dashboard_data() -> dict:
    s3 = get_r2_client()
    paginator = s3.get_paginator("list_objects_v2")

    all_objects = []
    for page in paginator.paginate(Bucket=R2_BUCKET_NAME):
        all_objects.extend(page.get("Contents", []))

    hls_bases = set()
    for obj in all_objects:
        key = obj["Key"]
        if key.lower().endswith("/master.m3u8"):
            hls_bases.add(key.rsplit("/", 1)[0])

    hls_packages = {
        base: {
            "name": base,
            "size": 0,
            "date": None,
            "type": "HLS",
            "url_key": f"{base}/master.m3u8",
        }
        for base in hls_bases
    }

    standalone_files = []
    total_size = 0
    mp4_count = 0

    # Match each object against the deepest known HLS directory instead of
    # comparing against every HLS package. This is much cheaper for large buckets.
    sorted_bases = sorted(hls_bases, key=lambda value: value.count("/"), reverse=True)

    for obj in all_objects:
        key = obj["Key"]
        size = int(obj.get("Size", 0))
        date = obj.get("LastModified")
        total_size += size

        matched_base = None
        for base in sorted_bases:
            if key == base or key.startswith(base + "/"):
                matched_base = base
                break

        if matched_base:
            item = hls_packages[matched_base]
            item["size"] += size
            if item["date"] is None or (date and date > item["date"]):
                item["date"] = date
            continue

        if not key.endswith("/"):
            standalone_files.append(
                {
                    "name": key,
                    "size": size,
                    "date": date,
                    "type": "FILE",
                    "url_key": key,
                }
            )
            if key.lower().endswith(".mp4"):
                mp4_count += 1

    items = list(hls_packages.values()) + standalone_files
    items.sort(
        key=lambda item: item["date"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
        reverse=True,
    )

    return {
        "total_size": total_size,
        "mp4_count": mp4_count,
        "hls_count": len(hls_packages),
        "items": items,
    }


def sync_delete_r2_file(s3_key: str) -> None:
    if not s3_key or s3_key.startswith("/"):
        raise ValueError("Invalid R2 key.")
    get_r2_client().delete_object(Bucket=R2_BUCKET_NAME, Key=s3_key)


def sync_delete_r2_folder(prefix: str) -> None:
    prefix = sanitize_prefix(prefix, "")
    if not prefix:
        raise ValueError("Invalid R2 folder prefix.")

    s3 = get_r2_client()
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=R2_BUCKET_NAME, Prefix=prefix.rstrip("/") + "/"):
        objects = page.get("Contents", [])
        keys = [{"Key": obj["Key"]} for obj in objects]

        # S3 DeleteObjects accepts up to 1000 objects per request.
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            if batch:
                response = s3.delete_objects(
                    Bucket=R2_BUCKET_NAME,
                    Delete={"Objects": batch, "Quiet": True},
                )
                errors = response.get("Errors", [])
                if errors:
                    raise RuntimeError(f"R2 delete failed for {len(errors)} object(s).")


def sync_r2_upload(
    file_path: str,
    s3_key: str,
    loop: asyncio.AbstractEventLoop,
    msg,
    start_t: float,
) -> None:
    s3 = get_r2_client()
    file_size = os.path.getsize(file_path)
    filename = os.path.basename(file_path)

    class ProgressCallback:
        def __init__(self):
            self.seen = 0
            self.last_update = 0.0
            self.lock = threading.Lock()

        def __call__(self, bytes_amount: int):
            with self.lock:
                self.seen += bytes_amount
                now = time.time()
                if now - self.last_update < PROGRESS_UPDATE_SECONDS and self.seen < file_size:
                    return
                self.last_update = now

            schedule_message_edit(
                loop,
                msg,
                get_status_text(
                    "R2 Uploading",
                    filename,
                    min(self.seen, file_size),
                    file_size,
                    start_t,
                ),
            )

    config = TransferConfig(
        multipart_threshold=R2_MULTIPART_CHUNK_MB * 1024 * 1024,
        multipart_chunksize=R2_MULTIPART_CHUNK_MB * 1024 * 1024,
        max_concurrency=R2_MULTIPART_CONCURRENCY,
        use_threads=True,
    )

    s3.upload_file(
        file_path,
        R2_BUCKET_NAME,
        s3_key,
        Callback=ProgressCallback(),
        ExtraArgs=r2_extra_args(file_path),
        Config=config,
    )


def sync_r2_upload_folder(
    folder_path: str,
    s3_prefix: str,
    loop: asyncio.AbstractEventLoop,
    msg,
    start_t: float,
) -> None:
    s3 = get_r2_client()
    s3_prefix = sanitize_prefix(s3_prefix)

    all_files = []
    total_size = 0
    for root_dir, _, filenames in os.walk(folder_path):
        for filename in filenames:
            filepath = os.path.join(root_dir, filename)
            try:
                size = os.path.getsize(filepath)
            except OSError:
                continue
            all_files.append(filepath)
            total_size += size

    if not all_files:
        raise ValueError("The extracted HLS directory is empty.")

    class ProgressCallback:
        def __init__(self):
            self.seen = 0
            self.last_update = 0.0
            self.lock = threading.Lock()

        def __call__(self, bytes_amount: int):
            with self.lock:
                self.seen += bytes_amount
                now = time.time()
                if now - self.last_update < PROGRESS_UPDATE_SECONDS and self.seen < total_size:
                    return
                self.last_update = now
                seen = min(self.seen, total_size)

            schedule_message_edit(
                loop,
                msg,
                get_status_text(
                    "R2 HLS Upload",
                    s3_prefix,
                    seen,
                    total_size,
                    start_t,
                ),
            )

    progress = ProgressCallback()
    transfer_config = TransferConfig(
        multipart_threshold=R2_MULTIPART_CHUNK_MB * 1024 * 1024,
        multipart_chunksize=R2_MULTIPART_CHUNK_MB * 1024 * 1024,
        max_concurrency=R2_MULTIPART_CONCURRENCY,
        use_threads=True,
    )

    def upload_single_file(file_path: str):
        rel_path = os.path.relpath(file_path, folder_path)
        rel_path = rel_path.replace(os.sep, "/")
        # Keep S3 keys unencoded. URL encoding is applied only when building public URLs.
        s3_key = f"{s3_prefix}/{rel_path}"

        s3.upload_file(
            file_path,
            R2_BUCKET_NAME,
            s3_key,
            Callback=progress,
            ExtraArgs=r2_extra_args(file_path),
            Config=transfer_config,
        )

    with ThreadPoolExecutor(max_workers=R2_UPLOAD_WORKERS) as executor:
        futures = [executor.submit(upload_single_file, path) for path in all_files]
        for future in futures:
            future.result()


def find_master_playlist(folder_path: str) -> str | None:
    matches = []
    for root, _, files in os.walk(folder_path):
        for filename in files:
            if filename.lower() == "master.m3u8":
                matches.append(os.path.join(root, filename))
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning("Multiple master.m3u8 files found; using: %s", matches[0])
    return matches[0]


# ============================================
# --- 5. ZIP SAFETY / HLS ---
# ============================================

def safe_extract_zip(zip_path: str, destination: str) -> None:
    max_uncompressed = MAX_ZIP_UNCOMPRESSED_GB * 1024**3
    destination = os.path.abspath(destination)
    os.makedirs(destination, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as archive:
        infos = archive.infolist()
        files = [info for info in infos if not info.is_dir()]

        if len(files) > MAX_ZIP_FILES:
            raise ValueError(
                f"ZIP contains too many files ({len(files)}). Limit is {MAX_ZIP_FILES}."
            )

        total_uncompressed = sum(max(0, info.file_size) for info in files)
        if total_uncompressed > max_uncompressed:
            raise ValueError(
                f"ZIP uncompressed size exceeds {MAX_ZIP_UNCOMPRESSED_GB} GB."
            )

        root = os.path.realpath(destination)

        for info in infos:
            name = info.filename.replace("\\", "/")
            if not name:
                continue

            # Reject absolute paths, drive prefixes, traversal and symlinks.
            parts = [part for part in name.split("/") if part]
            if name.startswith("/") or (parts and ":" in parts[0]) or ".." in parts:
                raise ValueError(f"Unsafe ZIP path: {info.filename}")

            target = os.path.realpath(os.path.join(destination, *parts))
            if os.path.commonpath([root, target]) != root:
                raise ValueError(f"Unsafe ZIP extraction target: {info.filename}")

            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError(f"ZIP symlinks are not allowed: {info.filename}")

            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info, "r") as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest, length=1024 * 1024)


async def upload_zip_or_hls(
    zip_path: str,
    workspace: str,
    msg,
    target_folder: str | None,
    start_t: float,
) -> tuple[str, str | None]:
    """Extract a ZIP; if it contains master.m3u8, upload it as HLS, else upload the ZIP itself."""
    filename = os.path.basename(zip_path)
    extract_dir = os.path.join(workspace, "extracted")
    await msg.edit("📦 **Inspecting ZIP archive...**")

    await asyncio.to_thread(safe_extract_zip, zip_path, extract_dir)
    master = find_master_playlist(extract_dir)

    if not master:
        logger.info("ZIP has no master.m3u8; uploading the ZIP as a normal file.")
        return "FILE", None

    project_name = os.path.splitext(filename)[0]
    top_entries = [
        item for item in os.listdir(extract_dir)
        if item not in {".", ".."}
    ]

    if (
        len(top_entries) == 1
        and os.path.isdir(os.path.join(extract_dir, top_entries[0]))
    ):
        upload_source_dir = os.path.join(extract_dir, top_entries[0])
        default_prefix = sanitize_prefix(top_entries[0], project_name)
    else:
        upload_source_dir = extract_dir
        default_prefix = sanitize_prefix(project_name, "hls")

    s3_prefix = sanitize_prefix(target_folder, default_prefix) if target_folder else default_prefix

    await msg.edit(
        f"⬆️ **Uploading HLS to R2...**\n📂 `{s3_prefix}`"
    )
    await asyncio.to_thread(
        sync_r2_upload_folder,
        upload_source_dir,
        s3_prefix,
        asyncio.get_running_loop(),
        msg,
        start_t,
    )

    # Build the master key based on where the playlist lives inside the upload root.
    rel_master = os.path.relpath(master, upload_source_dir).replace(os.sep, "/")
    master_key = f"{s3_prefix}/{rel_master}"
    return "HLS", build_r2_public_url(master_key)


# ============================================
# --- 6. DOWNLOAD ENGINES ---
# ============================================

def extract_gdrive_id(url: str) -> str | None:
    """Extract a Google Drive file ID from common public share URL formats."""
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = parsed.netloc.lower().split(":", 1)[0]
    if not (host == "drive.google.com" or host.endswith(".drive.google.com")):
        return None

    patterns = (
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)

    query_id = re.search(r"(?:^|&)id=([a-zA-Z0-9_-]+)", parsed.query)
    if query_id:
        return query_id.group(1)

    return None


async def download_google_drive(
    url: str,
    workspace: str,
    msg,
    start_t: float,
    custom_name: str | None = None,
    task_code: str | None = None,
) -> str:
    """
    Download a public Google Drive file using gdown.

    gdown 6.1.0 accepts Drive share links directly and can resolve the real
    filename before downloading. See the current gdown documentation/release notes.
    """
    file_id = extract_gdrive_id(url)
    if not file_id:
        raise ValueError("Invalid or unsupported Google Drive file URL.")

    os.makedirs(workspace, exist_ok=True)

    await msg.edit(
        "☁️ **Google Drive detected**\n"
        "🔎 Resolving file information...",
        buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
    )

    detected_name = None
    try:
        info = await asyncio.to_thread(
            gdown.download,
            url=url,
            output=None,
            quiet=True,
            skip_download=True,
        )
        detected_name = getattr(info, "path", None) if info else None
    except Exception as exc:
        logger.warning("Google Drive filename resolution failed: %s", exc)

    if task_cancelled(task_code):
        raise asyncio.CancelledError()

    detected_name = sanitize_filename(detected_name, f"{file_id}.bin")

    if custom_name:
        requested = sanitize_filename(custom_name, detected_name)
        detected_ext = os.path.splitext(detected_name)[1]
        if "." not in requested and detected_ext:
            requested += detected_ext
        filename = requested
    else:
        filename = detected_name

    output_path = get_unique_filename(os.path.join(workspace, filename))

    await msg.edit(
        f"⬇️ **Downloading from Google Drive...**\n"
        f"🎬 `{os.path.basename(output_path)}`",
        buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
    )

    try:
        result = await asyncio.to_thread(
            gdown.download,
            url=url,
            output=output_path,
            quiet=True,
            resume=True,
        )
    except Exception as exc:
        raise ValueError(
            "Google Drive download failed. Make sure the file is shared as "
            "'Anyone with the link' and that Google has not throttled the file. "
            f"Details: {exc}"
        ) from exc

    if task_cancelled(task_code):
        raise asyncio.CancelledError()

    file_path = result or output_path
    if not os.path.isfile(file_path):
        # Defensive fallback in case gdown changes the returned path.
        candidates = [
            os.path.join(workspace, item)
            for item in os.listdir(workspace)
            if os.path.isfile(os.path.join(workspace, item))
        ]
        if not candidates:
            raise ValueError("Google Drive download produced no file.")
        file_path = max(candidates, key=os.path.getsize)

    if os.path.getsize(file_path) <= 0:
        raise ValueError("Google Drive returned an empty file.")

    await msg.edit(
        f"✅ **Google Drive Downloaded**\n"
        f"🎬 `{os.path.basename(file_path)}`\n"
        f"📦 `{human_size(os.path.getsize(file_path))}`"
    )
    return file_path


async def download_direct(
    url: str,
    workspace: str,
    msg,
    start_t: float,
    custom_name: str | None = None,
    task_code: str | None = None,
) -> str:
    timeout = ClientTimeout(
        total=None,
        connect=30,
        sock_read=HTTP_TIMEOUT_SECONDS,
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TelegramR2Bot/1.0)",
        "Accept": "*/*",
    }

    async with ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    raise ValueError(
                        "Direct link returned an HTML page instead of a downloadable file."
                    )

                total = int(response.headers.get("Content-Length", 0) or 0)

                header_name = response.headers.get("Content-Disposition", "")
                detected_name = ""
                filename_match = re.search(
                    r"filename\*=UTF-8''([^;]+)|filename=[\"']?([^\"';]+)",
                    header_name,
                    re.IGNORECASE,
                )
                if filename_match:
                    detected_name = unquote(
                        filename_match.group(1) or filename_match.group(2) or ""
                    )

                if not detected_name:
                    path_name = unquote(urlparse(str(response.url)).path.rsplit("/", 1)[-1])
                    detected_name = path_name

                filename = sanitize_filename(
                    custom_name or detected_name,
                    "downloaded_file.bin",
                )

                # If a server gives us a generic /view URL, infer MP4 for video responses.
                if filename.lower() in {"view", "download", "uc", ""}:
                    if content_type.startswith("video/"):
                        ext = mimetypes.guess_extension(content_type) or ".mp4"
                        filename = f"downloaded_file{ext}"

                file_path = get_unique_filename(os.path.join(workspace, filename))
                os.makedirs(workspace, exist_ok=True)

                await msg.edit(
                    f"⬇️ **Downloading...**\n🎬 `{os.path.basename(file_path)}`",
                    buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
                )

                downloaded = 0
                last_update = 0.0
                chunk_size = DOWNLOAD_CHUNK_MB * 1024 * 1024

                with open(file_path, "wb") as output:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        if task_cancelled(task_code):
                            raise asyncio.CancelledError()
                        output.write(chunk)
                        downloaded += len(chunk)

                        now = time.time()
                        if now - last_update >= PROGRESS_UPDATE_SECONDS or (
                            total and downloaded >= total
                        ):
                            last_update = now
                            try:
                                await msg.edit(
                                    get_status_text(
                                        "Downloading",
                                        os.path.basename(file_path),
                                        downloaded,
                                        total,
                                        start_t,
                                    ),
                                    buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
                                )
                            except Exception:
                                pass

        except ClientResponseError as exc:
            raise ValueError(
                f"HTTP {exc.status} while downloading the direct link."
            ) from exc

    if not os.path.exists(file_path) or os.path.getsize(file_path) <= 0:
        raise ValueError("Direct download produced no file.")

    return file_path


async def sync_yt_dlp_download(
    url: str,
    workspace: str,
    custom_name: str | None,
    msg,
    start_t: float,
    task_code: str | None = None,
) -> str:
    """Run yt-dlp in a worker thread so the aiohttp/Telethon event loop stays responsive."""
    os.makedirs(workspace, exist_ok=True)
    loop = asyncio.get_running_loop()
    last_update = {"time": 0.0}

    def progress_hook(data: dict):
        if task_cancelled(task_code):
            raise yt_dlp.utils.DownloadError("Cancelled by user")

        if data.get("status") != "downloading":
            return

        now = time.time()
        if now - last_update["time"] < PROGRESS_UPDATE_SECONDS:
            return
        last_update["time"] = now

        downloaded = int(data.get("downloaded_bytes", 0) or 0)
        total = int(
            data.get("total_bytes")
            or data.get("total_bytes_estimate")
            or 0
        )
        speed = float(data.get("speed") or 0)
        eta = data.get("eta")
        filename = os.path.basename(data.get("filename") or "media")
        percent = (downloaded / total * 100) if total else 0

        text = (
            f"🎬 **yt-dlp Download**\n"
            f"📦 `{filename}`\n\n"
            f"🌀 **Progress:** `{percent:.2f}%`\n"
            f"⚡ **Speed:** `{human_size(speed)}/s`\n"
            f"📂 **Size:** `{human_size(downloaded)} / {human_size(total)}`"
        )
        if eta is not None:
            text += f"\n⏱️ **ETA:** `{get_readable_time(eta)}`"

        schedule_message_edit(
            loop,
            msg,
            text,
            buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
        )

    def worker() -> str:
        output_template = os.path.join(workspace, "%(title)s.%(ext)s")
        options = {
            "outtmpl": output_template,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "continuedl": True,
            "overwrites": False,
            "merge_output_format": "mp4",
            "progress_hooks": [progress_hook],
            "socket_timeout": 30,
            "http_chunk_size": 10 * 1024 * 1024,
            "ffmpeg_location": "/usr/bin/ffmpeg",
        }

        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([url])

        if task_cancelled(task_code):
            raise yt_dlp.utils.DownloadError("Cancelled by user")

        result = get_largest_file(workspace)
        if not result or os.path.getsize(result) <= 0:
            raise ValueError("yt-dlp did not produce a usable media file.")

        return result

    try:
        result_path = await asyncio.to_thread(worker)
    except yt_dlp.utils.DownloadError as exc:
        if task_cancelled(task_code) or "Cancelled" in str(exc):
            raise asyncio.CancelledError() from exc
        raise ValueError(f"yt-dlp failed: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"yt-dlp failed: {exc}") from exc

    if custom_name:
        custom = sanitize_filename(custom_name)
        ext = os.path.splitext(result_path)[1]
        if "." not in custom:
            custom += ext
        target = get_unique_filename(os.path.join(workspace, custom))
        if os.path.abspath(result_path) != os.path.abspath(target):
            os.replace(result_path, target)
            result_path = target

    return result_path



# ============================================
# --- TORRENT METADATA / MANUAL FILE SELECTOR ---
# ============================================

def normalize_magnet(value: str) -> str:
    value = (value or "").strip()
    value = value.replace("\\&", "&")
    value = value.replace("\\:", ":")
    return value


def is_torrent_source(url: str) -> bool:
    lower = (url or "").lower().strip()
    return lower.startswith("magnet:?") or lower.endswith(".torrent")


def parse_aria2_torrent_info(torrent_path: str) -> tuple[str, list[dict]]:
    try:
        output = subprocess.check_output(
            ["aria2c", "-S", torrent_path],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=90,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"aria2c could not inspect the torrent: {exc.output[-1200:]}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Torrent metadata inspection timed out.") from exc

    name_match = re.search(r"(?im)^\s*Name:\s*(.+?)\s*$", output)
    torrent_name = (
        name_match.group(1).strip()
        if name_match else Path(torrent_path).stem
    )

    files = []
    current_idx = None
    current_path = None

    for raw_line in output.splitlines():
        line = raw_line.rstrip()

        match = re.match(r"^\s*(\d+)\|(.+?)\s*$", line)
        if match:
            current_idx = int(match.group(1))
            current_path = match.group(2).strip()
            continue

        if current_idx is not None and current_path is not None:
            size_match = re.match(r"^\s*\|(.+?)\s*$", line)
            if size_match:
                files.append({
                    "idx": current_idx,
                    "path": current_path,
                    "size": size_match.group(1).strip(),
                })
                current_idx = None
                current_path = None

    if not files:
        raise RuntimeError(
            "No files were detected in torrent metadata."
        )

    return torrent_name, files


def fetch_torrent_metadata_sync(source: str, workdir: str) -> str:
    os.makedirs(workdir, exist_ok=True)
    source = normalize_magnet(source)

    if source.lower().startswith("magnet:?"):
        cmd = [
            "aria2c",
            "--bt-metadata-only=true",
            "--bt-save-metadata=true",
            "--enable-dht=true",
            "--bt-enable-lpd=false",
            "--disable-ipv6=true",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--seed-time=0",
            "--dir", workdir,
            source,
        ]
    else:
        cmd = [
            "aria2c",
            "--dir", workdir,
            "--out", "source.torrent",
            "--max-tries=3",
            "--retry-wait=2",
            "--console-log-level=warn",
            source,
        ]

    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
    )

    if completed.returncode != 0:
        raise RuntimeError(
            "Could not fetch torrent metadata:\n"
            + completed.stdout[-1500:]
        )

    torrent_files = [
        os.path.join(workdir, name)
        for name in os.listdir(workdir)
        if name.lower().endswith(".torrent")
        and os.path.isfile(os.path.join(workdir, name))
    ]

    if not torrent_files:
        raise RuntimeError(
            "No .torrent metadata file was produced."
        )

    return max(torrent_files, key=os.path.getmtime)


def build_torrent_selection_messages(
    torrent_name: str,
    files: list[dict],
    max_chars: int = 3400,
) -> list[str]:
    header = (
        f"📦 **Torrent:** `{torrent_name}`\n\n"
        "**Files inside torrent:**\n\n"
    )
    footer = (
        "\n➡️ **Reply with:**\n"
        "`1,3,5` • `1-5` • `all` • `cancel`"
    )

    pages = []
    current = header

    for item in files:
        row = (
            f"**[{item['idx']}]** `{item['path']}`\n"
            f"    📦 `{item['size']}`\n"
        )

        if (
            len(current) + len(row) + len(footer) > max_chars
            and current != header
        ):
            pages.append(current + footer)
            current = header

        current += row

    pages.append(current + footer)
    return pages


def parse_torrent_selection(
    selection: str,
    valid_indices: set[int],
) -> list[int]:
    selection = selection.strip().lower()

    if selection == "all":
        return sorted(valid_indices)

    selected = set()

    for token in re.split(r"[,\s]+", selection):
        if not token:
            continue

        if re.fullmatch(r"\d+-\d+", token):
            left, right = map(
                int,
                token.split("-", 1),
            )

            if left > right:
                left, right = right, left

            if right - left > 5000:
                raise ValueError(
                    "Selection range is too large."
                )

            selected.update(range(left, right + 1))

        elif token.isdigit():
            selected.add(int(token))

        else:
            raise ValueError(
                f"Invalid selection token: `{token}`"
            )

    selected &= valid_indices

    if not selected:
        raise ValueError(
            "No valid torrent files were selected."
        )

    return sorted(selected)


def find_downloaded_selected_files(
    workspace: str,
    selected_items: list[dict],
) -> list[str]:
    local_files = []

    for root, _, names in os.walk(workspace):
        for name in names:
            path = os.path.join(root, name)
            if os.path.isfile(path):
                local_files.append(path)

    normalized = {}

    for path in local_files:
        rel = os.path.relpath(
            path,
            workspace,
        ).replace(os.sep, "/")
        normalized[rel] = path

    results = []

    for item in selected_items:
        target = (
            item["path"]
            .replace("\\", "/")
            .lstrip("./")
        )

        if target in normalized:
            results.append(normalized[target])
            continue

        matches = [
            path
            for rel, path in normalized.items()
            if rel == target
            or rel.endswith("/" + target)
        ]

        if matches:
            results.append(
                min(
                    matches,
                    key=lambda p: len(
                        os.path.relpath(p, workspace)
                    ),
                )
            )

    unique = []
    seen = set()

    for path in results:
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            unique.append(path)

    return unique


async def start_torrent_selection(
    event,
    source: str,
    target_folder: str | None,
) -> bool:
    source = normalize_magnet(source)

    if not is_torrent_source(source):
        return False

    key = (event.chat_id, event.sender_id)

    old = pending_torrent_selections.pop(key, None)
    if old:
        shutil.rmtree(
            old.get("workdir", ""),
            ignore_errors=True,
        )

    workdir = os.path.join(
        "/tmp",
        f"torrent_select_{uuid.uuid4().hex[:10]}",
    )
    os.makedirs(workdir, exist_ok=True)

    status = await event.reply(
        "🔎 **Reading torrent metadata...**\n"
        "⏳ Please wait."
    )

    try:
        torrent_path = await asyncio.to_thread(
            fetch_torrent_metadata_sync,
            source,
            workdir,
        )

        torrent_name, files = await asyncio.to_thread(
            parse_aria2_torrent_info,
            torrent_path,
        )

        pending_torrent_selections[key] = {
            "torrent_path": torrent_path,
            "workdir": workdir,
            "torrent_name": torrent_name,
            "files": files,
            "target_folder": target_folder,
            "source": source,
            "created_at": time.time(),
        }

        pages = build_torrent_selection_messages(
            torrent_name,
            files,
        )

        await status.delete()

        for page_index, page in enumerate(pages, start=1):
            if len(pages) > 1:
                page = (
                    f"📄 **Page {page_index}/{len(pages)}**\n\n"
                    + page
                )
            await event.reply(page)

        return True

    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)

        await status.edit(
            f"❌ **Torrent metadata error:**\n"
            f"`{str(exc)[:1800]}`"
        )
        return True


async def download_selected_torrent(
    torrent_path: str,
    selected_indices: list[int],
    workspace: str,
    msg,
    task_code: str,
) -> None:
    selection = ",".join(
        str(i) for i in selected_indices
    )

    cmd = [
        "aria2c",
        "--seed-time=0",
        "--bt-stop-timeout=300",
        "--summary-interval=2",
        "--console-log-level=warn",
        "--disable-ipv6=true",
        "--bt-enable-lpd=false",
        "--enable-dht=true",
        "--bt-max-peers=128",
        "--max-tries=5",
        "--retry-wait=3",
        "--file-allocation=none",
        "--continue=true",
        "--split=8",
        "--min-split-size=1M",
        f"--select-file={selection}",
        "--dir", workspace,
        "-T", torrent_path,
    ]

    await msg.edit(
        f"⬇️ **Downloading selected torrent files...**\n\n"
        f"📑 **Selected:** `{selection}`",
        buttons=task_cancel_button(task_code),
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    active_tasks[task_code]["process"] = process
    last_update = 0.0

    try:
        while True:
            if task_cancelled(task_code):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                raise asyncio.CancelledError()

            line = await process.stdout.readline()
            if not line:
                break

            now = time.time()

            if now - last_update >= PROGRESS_UPDATE_SECONDS:
                last_update = now

                line_text = line.decode(
                    "utf-8",
                    errors="replace",
                ).strip()

                if line_text:
                    try:
                        await msg.edit(
                            f"⬇️ **Torrent Download**\n\n"
                            f"📑 Selected: `{selection}`\n"
                            f"🌀 `{line_text[-1200:]}`",
                            buttons=task_cancel_button(
                                task_code
                            ),
                        )
                    except Exception:
                        pass

        return_code = await process.wait()

        if return_code != 0:
            raise RuntimeError(
                f"aria2c exited with code {return_code}."
            )

    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass


async def finish_selected_torrent_to_r2(
    event,
    pending: dict,
    selected_indices: list[int],
) -> None:
    task_code, _ = new_task(
        "torrent-selection"
    )

    workspace = os.path.join(
        "/tmp",
        f"torrent_dl_{uuid.uuid4().hex[:10]}",
    )
    os.makedirs(workspace, exist_ok=True)

    status = await event.reply(
        f"✅ **Selection received:** "
        f"`{','.join(map(str, selected_indices))}`\n\n"
        f"⬇️ **Starting selected-file download...**",
        buttons=task_cancel_button(task_code),
    )

    index_set = set(selected_indices)

    selected_items = [
        item
        for item in pending["files"]
        if int(item["idx"]) in index_set
    ]

    try:
        await download_selected_torrent(
            pending["torrent_path"],
            selected_indices,
            workspace,
            status,
            task_code,
        )

        downloaded_files = await asyncio.to_thread(
            find_downloaded_selected_files,
            workspace,
            selected_items,
        )

        if len(downloaded_files) != len(selected_items):
            raise RuntimeError(
                f"Only {len(downloaded_files)} of "
                f"{len(selected_items)} selected files "
                "were found after downloading."
            )

        await status.edit(
            f"✅ **Selected files downloaded.**\n\n"
            f"📑 Files: `{len(downloaded_files)}`\n"
            f"📤 **Uploading to Cloudflare R2...**"
        )

        uploaded = []

        for local_path in downloaded_files:
            if task_cancelled(task_code):
                raise asyncio.CancelledError()

            filename = sanitize_filename(
                os.path.basename(local_path),
                "downloaded_file",
            )

            rel_path = os.path.relpath(
                local_path,
                workspace,
            ).replace(os.sep, "/")

            base_folder = pending.get(
                "target_folder"
            )

            if base_folder:
                r2_folder = sanitize_prefix(
                    base_folder,
                    "uploads",
                )
            else:
                r2_folder = sanitize_prefix(
                    pending["torrent_name"],
                    "torrent",
                )

            s3_key = f"{r2_folder}/{rel_path}"

            await status.edit(
                f"📤 **Uploading to R2...**\n"
                f"🎬 `{filename}`"
            )

            await asyncio.to_thread(
                sync_r2_upload,
                local_path,
                s3_key,
                asyncio.get_running_loop(),
                status,
                time.time(),
            )

            uploaded.append(
                (filename, build_r2_public_url(s3_key), s3_key)
            )

        result = [
            "✅ **Torrent selection complete!**",
            "",
            f"📦 **Uploaded files:** `{len(uploaded)}`",
            "",
        ]

        buttons = []

        for number, (filename, url, s3_key) in enumerate(
            uploaded,
            start=1,
        ):
            result.append(
                f"**{number}.** `{filename}`\n"
                f"🔗 `{url}`"
            )

            code = secrets.token_urlsafe(10)
            link_storage[code] = {
                "kind": "r2",
                "s3_key": s3_key,
                "created_at": time.time(),
            }

            buttons.append([
                Button.inline(
                    f"🗑️ Delete {filename[:35]}",
                    data=f"delr2_{code}",
                )
            ])

        await status.edit(
            "\n".join(result)[:3900],
            buttons=buttons[:20],
            link_preview=False,
        )

    except asyncio.CancelledError:
        await status.edit(
            "🛑 **Torrent task cancelled.**"
        )

    except Exception as exc:
        logger.exception(
            "Selected torrent → R2 failed"
        )
        await status.edit(
            f"❌ **Torrent/R2 Error:**\n"
            f"`{str(exc)[:1800]}`"
        )

    finally:
        remove_task(task_code)
        shutil.rmtree(
            workspace,
            ignore_errors=True,
        )
        free_memory()


async def download_magnet(
    magnet: str,
    workspace: str,
    custom_name: str | None,
    msg,
    start_t: float,
    task_code: str | None = None,
) -> str:
    """Download a magnet URI using aria2c installed in the Docker image."""
    os.makedirs(workspace, exist_ok=True)

    cmd = [
        "aria2c",
        "--dir",
        workspace,
        "--continue=true",
        "--file-allocation=none",
        "--seed-time=0",
        "--bt-enable-lpd=true",
        "--bt-tracker-connect-timeout=10",
        "--bt-tracker-interval=30",
        "--summary-interval=5",
        "--console-log-level=warn",
        "--max-tries=5",
        "--retry-wait=3",
        "--split=8",
        "--min-split-size=1M",
        "--bt-tracker=" + PUBLIC_TRACKERS,
        magnet,
    ]

    await msg.edit(
        "🧲 **Starting torrent download...**",
        buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    if task_code in active_tasks:
        active_tasks[task_code]["process"] = process

    last_update = 0.0

    try:
        while True:
            if task_cancelled(task_code):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                raise asyncio.CancelledError()

            line = await process.stdout.readline()
            if not line:
                break

            text_line = line.decode("utf-8", errors="replace").strip()
            now = time.time()
            if text_line and now - last_update >= PROGRESS_UPDATE_SECONDS:
                last_update = now
                short_line = text_line[-800:]
                try:
                    await msg.edit(
                        f"🧲 **Torrent Download**\n`{short_line}`",
                        buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
                    )
                except Exception:
                    pass

        return_code = await process.wait()
        if return_code != 0:
            raise ValueError(f"aria2c exited with code {return_code}.")

    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    result = get_largest_file(workspace)
    if not result or os.path.getsize(result) <= 0:
        raise ValueError("Torrent download produced no usable file.")

    if custom_name:
        custom = sanitize_filename(custom_name)
        ext = os.path.splitext(result)[1]
        if "." not in custom and ext:
            custom += ext
        target = get_unique_filename(os.path.join(workspace, custom))
        if os.path.abspath(result) != os.path.abspath(target):
            os.replace(result, target)
            result = target

    return result


async def download_any_url(
    url: str,
    workspace: str,
    custom_name: str | None,
    msg,
    start_t: float,
    task_code: str | None = None,
) -> str:
    url = url.strip().strip(",")

    # 1. Magnet links.
    if url.lower().startswith("magnet:?"):
        return await download_magnet(
            url, workspace, custom_name, msg, start_t, task_code
        )

    # 2. Google Drive public file links.
    if extract_gdrive_id(url):
        return await download_google_drive(
            url, workspace, msg, start_t, custom_name, task_code
        )

    # 3. Obvious direct-file URLs should skip yt-dlp when possible.
    lower_path = urlparse(url).path.lower()
    direct_extensions = (
        ".zip", ".mp4", ".mkv", ".webm", ".mov", ".avi",
        ".mp3", ".m4a", ".aac", ".flac", ".ts", ".m3u8",
    )
    is_zip = bool(custom_name and custom_name.lower().endswith(".zip")) or lower_path.endswith(".zip")

    if not is_zip and not lower_path.endswith(direct_extensions):
        # 4. yt-dlp for supported media pages.
        try:
            await msg.edit(
                "🅿️ **Trying yt-dlp media extraction...**",
                buttons=[[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]] if task_code else None,
            )
            result = await sync_yt_dlp_download(
                url,
                workspace,
                custom_name,
                msg,
                start_t,
                task_code,
            )
            if result and os.path.exists(result) and os.path.getsize(result) > 0:
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("yt-dlp did not handle %s: %s", url[:120], exc)

    # 5. Standard HTTP/HTTPS direct download.
    return await download_direct(
        url,
        workspace,
        msg,
        start_t,
        custom_name,
        task_code,
    )


# ============================================
# --- 7. DASHBOARD ---
# ============================================

def check_dashboard_auth(request: web.Request) -> bool:
    if not DASHBOARD_PASS:
        return False

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False

    try:
        raw = base64.b64decode(auth_header[6:], validate=True).decode("utf-8")
        user, password = raw.split(":", 1)
    except Exception:
        return False

    return hmac.compare_digest(user, DASHBOARD_USER) and hmac.compare_digest(
        password,
        DASHBOARD_PASS,
    )


DASHBOARD_CSS = """
:root {
  --bg: #0b1120;
  --card: #111827;
  --card2: #172033;
  --text: #f8fafc;
  --muted: #94a3b8;
  --accent: #38bdf8;
  --green: #34d399;
  --red: #fb7185;
  --border: #263244;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 20px;
  font-family: Inter, Segoe UI, system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
}
.container { max-width: 1280px; margin: auto; }
.header-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 15px;
  margin-bottom: 20px;
}
h1 { font-size: 24px; margin: 0; }
.sub { color: var(--muted); font-size: 13px; margin-top: 5px; }
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 14px;
  margin-bottom: 18px;
}
.stat-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
}
.stat-title { color: var(--muted); font-size: 12px; text-transform: uppercase; font-weight: 700; }
.stat-val { color: var(--accent); font-size: 23px; font-weight: 800; margin-top: 6px; }
.controls { margin-bottom: 14px; }
.search-box {
  width: 100%;
  padding: 14px 16px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--card);
  color: var(--text);
  outline: none;
}
.search-box:focus { border-color: var(--accent); }
.table-wrapper {
  overflow-x: auto;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
}
table { width: 100%; border-collapse: collapse; min-width: 920px; }
th {
  background: var(--card2);
  color: var(--muted);
  padding: 14px;
  text-align: left;
  font-size: 12px;
}
td {
  padding: 14px;
  border-top: 1px solid var(--border);
  color: #cbd5e1;
  font-size: 14px;
  vertical-align: top;
}
tr:hover td { background: rgba(255,255,255,.02); }
.file-name { word-break: break-word; }
.actions { display: flex; flex-wrap: wrap; gap: 7px; }
.btn {
  border: 0;
  border-radius: 8px;
  padding: 8px 11px;
  cursor: pointer;
  text-decoration: none;
  font-weight: 700;
  font-size: 12px;
}
.btn-copy { background: rgba(56,189,248,.12); color: var(--accent); }
.btn-view { background: rgba(52,211,153,.12); color: var(--green); }
.btn-delete { background: rgba(251,113,133,.12); color: var(--red); }
.hls-row td { background: rgba(250,204,21,.035); }
.badge { color: #fbbf24; font-weight: 800; }
"""

DASHBOARD_JS = """
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    alert('✅ URL copied');
  } catch (e) {
    prompt('Copy URL:', text);
  }
}

async function deleteItem(key, isHLS) {
  const label = isHLS ? 'HLS package' : 'file';
  if (!confirm('Delete this ' + label + '?\\n\\n' + key)) return;

  const body = new URLSearchParams();
  body.set(isHLS ? 'prefix' : 'key', key);

  try {
    const response = await fetch(isHLS ? '/delete_folder' : '/delete_file', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest'
      },
      body
    });
    if (!response.ok) {
      alert('Delete failed: HTTP ' + response.status);
      return;
    }
    location.reload();
  } catch (e) {
    alert('Delete failed: ' + e);
  }
}

function filterTable() {
  const input = document.getElementById('searchInput').value.toLowerCase();
  document.querySelectorAll('tbody tr').forEach(row => {
    const name = row.querySelector('.file-name')?.innerText.toLowerCase() || '';
    row.style.display = name.includes(input) ? '' : 'none';
  });
}
"""


@routes.get("/health")
async def health_handler(request: web.Request):
    purge_expired_link_storage()
    return web.json_response({
        "status": "ok",
        "service": "telegram-r2-bot",
        "time": dt.datetime.now(dt.timezone.utc).isoformat(),
    })


@routes.get("/dashboard")
async def dashboard_handler(request: web.Request):
    if not check_dashboard_auth(request):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="R2 Dashboard"'},
            text="🔒 Access denied",
        )

    try:
        data = await asyncio.to_thread(sync_get_smart_dashboard_data)
        rows = []

        for item in data["items"]:
            name = item["name"]
            size_str = human_size(item["size"])
            date = item.get("date")
            date_str = date.strftime("%Y-%m-%d %H:%M") if date else "-"
            url = build_r2_public_url(item["url_key"])

            if item["type"] == "HLS":
                rows.append(
                    f"""
                    <tr class='hls-row'>
                      <td>📦 <span class='file-name'><b>{html.escape(name)}</b> <span class='badge'>(HLS)</span></span></td>
                      <td>{html.escape(size_str)}</td>
                      <td>{html.escape(date_str)}</td>
                      <td><div class='actions'>
                        <button class='btn btn-copy' onclick='copyText({json.dumps(url)})'>🔗 Copy Master</button>
                        <a class='btn btn-view' href='{html.escape(url, quote=True)}' target='_blank' rel='noopener'>▶️ Play</a>
                        <button class='btn btn-delete' onclick='deleteItem({json.dumps(name)}, true)'>🗑️ Delete</button>
                      </div></td>
                    </tr>
                    """
                )
            else:
                rows.append(
                    f"""
                    <tr>
                      <td>🎬 <span class='file-name'>{html.escape(name)}</span></td>
                      <td>{html.escape(size_str)}</td>
                      <td>{html.escape(date_str)}</td>
                      <td><div class='actions'>
                        <button class='btn btn-copy' onclick='copyText({json.dumps(url)})'>🔗 Copy</button>
                        <a class='btn btn-view' href='{html.escape(url, quote=True)}' target='_blank' rel='noopener'>▶️ Open</a>
                        <button class='btn btn-delete' onclick='deleteItem({json.dumps(name)}, false)'>🗑️ Delete</button>
                      </div></td>
                    </tr>
                    """
                )

        body_rows = "".join(rows) or (
            "<tr><td colspan='4' style='text-align:center;color:#94a3b8;'>No objects found.</td></tr>"
        )

        storage = human_size(data.get("total_size", 0))
        hls_count = data.get("hls_count", 0)
        mp4_count = data.get("mp4_count", 0)

        page = f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Cloudflare R2 Manager</title>
<style>{DASHBOARD_CSS}</style>
</head>
<body>
<div class='container'>
  <div class='header-bar'>
    <div>
      <h1>🛡️ Cloudflare R2 Manager</h1>
      <div class='sub'>Telegram downloader • R2 storage • HLS library</div>
    </div>
  </div>

  <div class='stats-grid'>
    <div class='stat-card'><div class='stat-title'>Total Storage</div><div class='stat-val'>{html.escape(storage)}</div></div>
    <div class='stat-card'><div class='stat-title'>HLS Packages</div><div class='stat-val'>{hls_count}</div></div>
    <div class='stat-card'><div class='stat-title'>MP4 Files</div><div class='stat-val'>{mp4_count}</div></div>
    <div class='stat-card'><div class='stat-title'>Objects</div><div class='stat-val'>{len(data.get('items', []))}</div></div>
  </div>

  <div class='controls'>
    <input id='searchInput' class='search-box' oninput='filterTable()' placeholder='🔍 Search files and HLS packages'>
  </div>

  <div class='table-wrapper'>
    <table>
      <thead><tr><th>Name</th><th>Size</th><th>Uploaded</th><th>Actions</th></tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </div>
</div>
<script>{DASHBOARD_JS}</script>
</body>
</html>"""

        return web.Response(
            text=page,
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    except Exception as exc:
        logger.exception("Dashboard error")
        page = (
            "<html><body style='font-family:sans-serif;background:#0b1120;color:#f8fafc;padding:40px;'>"
            "<h2>Dashboard error</h2>"
            f"<p>{html.escape(str(exc))}</p>"
            "</body></html>"
        )
        return web.Response(status=500, text=page, content_type="text/html")


async def require_dashboard_post(request: web.Request) -> bool:
    if not check_dashboard_auth(request):
        return False
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@routes.post("/delete_file")
async def web_delete_handler(request: web.Request):
    if not await require_dashboard_post(request):
        return web.Response(status=401, text="Unauthorized")

    key = (await request.post()).get("key", "")
    if not key:
        return web.Response(status=400, text="Missing key")

    try:
        await asyncio.to_thread(sync_delete_r2_file, key)
        return web.json_response({"ok": True})
    except Exception as exc:
        logger.exception("R2 file deletion failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@routes.post("/delete_folder")
async def web_delete_folder_handler(request: web.Request):
    if not await require_dashboard_post(request):
        return web.Response(status=401, text="Unauthorized")

    prefix = (await request.post()).get("prefix", "")
    if not prefix:
        return web.Response(status=400, text="Missing prefix")

    try:
        await asyncio.to_thread(sync_delete_r2_folder, prefix)
        return web.json_response({"ok": True})
    except Exception as exc:
        logger.exception("R2 folder deletion failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


@routes.get("/")
async def root_handler(request: web.Request):
    return web.Response(
        text=(
            "<html><body style='background:#0b1120;color:#38bdf8;"
            "text-align:center;padding-top:120px;font-family:sans-serif;'>"
            "<h1 style='font-size:42px;'>✅ System Online</h1>"
            "<p style='color:#94a3b8;'>Telegram + Cloudflare R2 service is running.</p>"
            "<a href='/dashboard' style='display:inline-block;margin-top:20px;padding:14px 24px;"
            "background:#38bdf8;color:#0b1120;text-decoration:none;border-radius:8px;font-weight:800;'>"
            "Open Dashboard</a></body></html>"
        ),
        content_type="text/html",
    )


# ============================================
# --- 8. DIRECT TELEGRAM STREAMING ---
# ============================================

def parse_range_header(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    if not range_header:
        return None

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        raise ValueError("Only a single byte range is supported.")

    start_str, end_str = match.groups()
    if not start_str and not end_str:
        raise ValueError("Invalid Range header.")

    if not start_str:
        length = int(end_str)
        if length <= 0:
            raise ValueError("Invalid suffix range.")
        length = min(length, file_size)
        return file_size - length, file_size - 1

    start = int(start_str)
    if start >= file_size:
        raise IndexError("Range starts past end of file.")

    end = int(end_str) if end_str else file_size - 1
    end = min(end, file_size - 1)
    if end < start:
        raise ValueError("Invalid byte range.")
    return start, end


@routes.get("/stream/{code}/{filename}")
async def stream_handler(request: web.Request):
    purge_expired_link_storage()

    code = request.match_info.get("code", "")
    item = link_storage.get(code)
    if not item or item.get("kind") != "telegram":
        return web.Response(text="Expired or invalid link.", status=410)

    if time.time() - float(item.get("created_at", 0)) > DIRECT_LINK_TTL:
        link_storage.pop(code, None)
        return web.Response(text="Expired.", status=410)

    chat_id = item.get("chat_id")
    message_id = item.get("message_id")
    file_name = sanitize_filename(
        unquote(request.match_info.get("filename", "video.mp4")),
        "video.mp4",
    )

    try:
        telegram_message = await client.get_messages(chat_id, ids=message_id)
    except Exception as exc:
        logger.exception("Telegram message lookup failed: %s", exc)
        return web.Response(text="Telegram file is unavailable.", status=404)

    if not telegram_message or not telegram_message.file or not telegram_message.media:
        return web.Response(text="Telegram file is unavailable.", status=404)

    file_size = int(telegram_message.file.size or 0)
    if file_size <= 0:
        return web.Response(text="Invalid file size.", status=500)

    mime_type = (
        getattr(telegram_message.file, "mime_type", None)
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )

    try:
        requested_range = parse_range_header(request.headers.get("Range"), file_size)
    except IndexError:
        return web.Response(
            status=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    except ValueError:
        return web.Response(
            status=416,
            headers={"Content-Range": f"bytes */{file_size}"},
            text="Invalid Range header.",
        )

    if requested_range:
        start, end = requested_range
        status = 206
    else:
        start, end = 0, file_size - 1
        status = 200

    content_length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Type": mime_type,
        "Content-Disposition": f'inline; filename="{file_name.replace(chr(34), "")}"',
        "Cache-Control": "private, no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    response = web.StreamResponse(status=status, headers=headers)
    await response.prepare(request)

    chunk_size = TELEGRAM_STREAM_CHUNK_KB * 1024
    limit = math.ceil(content_length / chunk_size)
    sent = 0

    try:
        async for chunk in client.iter_download(
            telegram_message.media,
            offset=start,
            request_size=chunk_size,
            chunk_size=chunk_size,
            limit=limit,
            file_size=file_size,
        ):
            if sent >= content_length:
                break

            remaining = content_length - sent
            data = bytes(chunk[:remaining])
            if not data:
                break

            await response.write(data)
            sent += len(data)

            if sent >= content_length:
                break

    except (ConnectionResetError, asyncio.CancelledError):
        pass
    except Exception:
        logger.exception("Telegram stream failed")

    try:
        await response.write_eof()
    except Exception:
        pass

    return response


# ============================================
# --- 9. TELEGRAM HANDLERS ---
# ============================================

def parse_admin_input(input_text: str) -> tuple[list[str], str | None, str | None]:
    text = input_text.strip()

    name_match = re.search(
        r"(?:^|\s)-n\s+(.+?)(?=\s+-f\s+|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    folder_match = re.search(
        r"(?:^|\s)-f\s+(.+)$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    custom_name = sanitize_filename(name_match.group(1).strip()) if name_match else None
    target_folder = sanitize_prefix(folder_match.group(1).strip(), "") if folder_match else None

    clean = text
    if name_match:
        clean = clean.replace(name_match.group(0), " ")
    if folder_match:
        clean = clean.replace(folder_match.group(0), " ")

    urls = re.findall(
        r"https?://[^\s,]+|magnet:\?[^\s,]+",
        clean,
        flags=re.IGNORECASE,
    )
    urls = [url.strip().rstrip(",") for url in urls]

    return urls, custom_name, target_folder


def task_cancel_button(task_code: str):
    return [[Button.inline("🛑 Cancel", data=f"cancel_{task_code}")]]


@client.on(
    events.NewMessage(
        incoming=True,
        func=lambda e: e.sender_id in ADMIN_IDS,
    )
)
async def master_handler(event):
    purge_expired_link_storage()

    pending_key = (
        event.chat_id,
        event.sender_id,
    )

    # =========================================================
    # Pending torrent selection response
    # =========================================================
    pending = pending_torrent_selections.get(
        pending_key
    )

    if pending and event.text:
        selection_text = event.text.strip()

        if selection_text.lower() == "cancel":
            pending_torrent_selections.pop(
                pending_key,
                None,
            )

            shutil.rmtree(
                pending.get("workdir", ""),
                ignore_errors=True,
            )

            await event.reply(
                "🛑 **Torrent selection cancelled.**"
            )
            return

        valid_indices = {
            int(item["idx"])
            for item in pending["files"]
        }

        try:
            selected = parse_torrent_selection(
                selection_text,
                valid_indices,
            )
        except ValueError as exc:
            await event.reply(
                f"❌ `{str(exc)}`\n\n"
                "**Examples:**\n"
                "`1,3,5`\n"
                "`1-5`\n"
                "`all`\n"
                "`cancel`"
            )
            return

        pending_torrent_selections.pop(
            pending_key,
            None,
        )

        await finish_selected_torrent_to_r2(
            event,
            pending,
            selected,
        )

        shutil.rmtree(
            pending.get("workdir", ""),
            ignore_errors=True,
        )

        return

    # =========================================================
    # Telegram file
    # =========================================================
    if event.file:
        filename = sanitize_filename(
            event.file.name,
            "file.bin",
        )

        await event.reply(
            f"📂 **File Detected:** `{filename}`",
            buttons=[
                [
                    Button.inline(
                        "🔗 Generate Direct Link",
                        data=f"link_{event.id}",
                    )
                ],
                [
                    Button.inline(
                        "🛡️ Upload to Cloudflare R2",
                        data=f"r2_{event.id}",
                    )
                ],
            ],
        )
        return

    if not event.text:
        return

    text_value = event.text.strip()

    if not (
        text_value.lower().startswith("http")
        or text_value.lower().startswith("magnet:?")
    ):
        return

    urls, custom_name, target_folder = parse_admin_input(
        text_value
    )

    if not urls:
        await event.reply(
            "❌ **No supported URL found.**"
        )
        return

    # Manual torrent selection is one torrent at a time.
    torrent_urls = [
        url
        for url in urls
        if is_torrent_source(url)
    ]

    if torrent_urls:
        if len(urls) != 1:
            await event.reply(
                "⚠️ **Send one torrent URL at a time "
                "when using manual file selection.**"
            )
            return

        if await start_torrent_selection(
            event,
            torrent_urls[0],
            target_folder,
        ):
            return

    # =========================================================
    # Existing non-torrent URL pipeline
    # =========================================================
    async with global_semaphore:
        for url in urls:
            task_code, _ = new_task("url")

            msg = await event.reply(
                f"🔗 **Processing Link:**\n"
                f"`{url[:120]}`",
                buttons=task_cancel_button(
                    task_code
                ),
            )

            workspace = (
                f"dl_{uuid.uuid4().hex[:10]}"
            )
            os.makedirs(
                workspace,
                exist_ok=True,
            )

            start_t = time.time()

            try:
                final_path = await download_any_url(
                    url,
                    workspace,
                    custom_name,
                    msg,
                    start_t,
                    task_code,
                )

                if task_cancelled(task_code):
                    raise asyncio.CancelledError()

                if (
                    not final_path
                    or not os.path.isfile(final_path)
                ):
                    raise ValueError(
                        "Downloader returned no usable file."
                    )

                filename = os.path.basename(
                    final_path
                )

                if filename.lower().endswith(".zip"):
                    kind, hls_url = await upload_zip_or_hls(
                        final_path,
                        workspace,
                        msg,
                        target_folder,
                        time.time(),
                    )

                    if kind == "HLS":
                        await msg.edit(
                            f"✅ **HLS Uploaded to R2!**\n\n"
                            f"📦 `{filename}`\n"
                            f"📺 **Master Stream:**\n"
                            f"`{hls_url}`",
                            link_preview=False,
                        )
                    else:
                        r2_url, code = await upload_to_r2(
                            final_path,
                            msg,
                            target_folder,
                        )

                        await msg.edit(
                            f"✅ **ZIP Uploaded to R2!**\n\n"
                            f"📦 `{filename}`\n"
                            f"🔗 `{r2_url}`",
                            buttons=[
                                [
                                    Button.inline(
                                        "🗑️ Delete from R2",
                                        data=f"delr2_{code}",
                                    )
                                ]
                            ],
                            link_preview=False,
                        )
                else:
                    r2_url, code = await upload_to_r2(
                        final_path,
                        msg,
                        target_folder,
                    )

                    await msg.edit(
                        f"✅ **Downloaded & Uploaded!**\n\n"
                        f"🎬 `{filename}`\n"
                        f"🔗 `{r2_url}`",
                        buttons=[
                            [
                                Button.inline(
                                    "🗑️ Delete from R2",
                                    data=f"delr2_{code}",
                                )
                            ]
                        ],
                        link_preview=False,
                    )

            except asyncio.CancelledError:
                try:
                    await msg.edit(
                        "🛑 **Task cancelled.**"
                    )
                except Exception:
                    pass

            except Exception as exc:
                logger.exception(
                    "URL processing failed: %s",
                    url,
                )

                try:
                    await msg.edit(
                        f"❌ **Error**\n"
                        f"`{url[:120]}`\n\n"
                        f"**Reason:** "
                        f"`{str(exc)[:1500]}`"
                    )
                except Exception:
                    pass

            finally:
                remove_task(task_code)

                shutil.rmtree(
                    workspace,
                    ignore_errors=True,
                )

                free_memory()


async def download_telegram_to_file(
    tg_msg,
    workspace: str,
    status,
    start_t: float,
    task_code: str,
) -> str:
    filename = clean_double_extension(
        sanitize_filename(tg_msg.file.name, "video.mp4")
    )
    file_path = os.path.join(workspace, filename)

    total = int(tg_msg.file.size or 0)
    downloaded = 0
    last_update = 0.0
    chunk_size = DOWNLOAD_CHUNK_MB * 1024 * 1024

    with open(file_path, "wb") as output:
        async for chunk in client.iter_download(
            tg_msg.media,
            request_size=chunk_size,
            file_size=total,
        ):
            if task_cancelled(task_code):
                raise asyncio.CancelledError()

            output.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if now - last_update >= PROGRESS_UPDATE_SECONDS or (total and downloaded >= total):
                last_update = now
                try:
                    await status.edit(
                        get_status_text(
                            "Telegram Download",
                            filename,
                            downloaded,
                            total,
                            start_t,
                        ),
                        buttons=task_cancel_button(task_code),
                    )
                except Exception:
                    pass

    if not os.path.isfile(file_path) or os.path.getsize(file_path) <= 0:
        raise ValueError("Telegram download produced an empty file.")

    return file_path


@client.on(events.CallbackQuery)
async def on_callback(event):
    if event.sender_id not in ADMIN_IDS:
        return

    purge_expired_link_storage()
    data = event.data.decode("utf-8", errors="ignore")

    if data.startswith("cancel_"):
        code = data.split("_", 1)[1]
        item = active_tasks.get(code)
        if not item:
            await event.answer("Task already finished or expired.", alert=True)
            return

        item["cancel_event"].set()
        process = item.get("process")
        if process and process.returncode is None:
            try:
                process.terminate()
            except Exception:
                pass

        await event.answer("Cancellation requested.", alert=False)
        try:
            await event.edit("🛑 **Cancellation requested...**")
        except Exception:
            pass
        return

    if data.startswith("delr2_"):
        code = data.split("_", 1)[1]
        item = link_storage.get(code)
        if not item or item.get("kind") != "r2":
            await event.answer("Delete action expired.", alert=True)
            return

        await event.answer("Deleting...", alert=False)
        try:
            await asyncio.to_thread(sync_delete_r2_file, item["s3_key"])
            link_storage.pop(code, None)
            await event.edit(
                f"🗑️ **File Deleted from R2**\nKey: `{item['s3_key']}`"
            )
        except Exception as exc:
            logger.exception("R2 callback deletion failed")
            await event.edit(f"❌ **Delete Error:** `{str(exc)[:1000]}`")
        return

    if data.startswith("link_"):
        try:
            msg_id = int(data.split("_", 1)[1])
        except ValueError:
            return await event.answer("Invalid file reference.", alert=True)

        await event.answer("Generating direct link...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file:
            return await event.respond("❌ File not found.")

        base = build_public_base_url()
        if not base:
            return await event.respond(
                "❌ Set PUBLIC_BASE_URL or KOYEB_PUBLIC_URL/KOYEB_APP_NAME first."
            )

        purge_expired_link_storage()
        code = secrets.token_urlsafe(10)
        filename = clean_double_extension(
            sanitize_filename(tg_msg.file.name, "video.mp4")
        )
        link_storage[code] = {
            "kind": "telegram",
            "chat_id": event.chat_id,
            "message_id": msg_id,
            "created_at": time.time(),
        }

        stream_link = (
            f"{base}/stream/{code}/{quote(filename, safe='')}"
        )
        expires_at = dt.datetime.fromtimestamp(
            time.time() + DIRECT_LINK_TTL,
            dt.timezone.utc,
        ).strftime("%Y-%m-%d %H:%M UTC")

        await event.respond(
            f"🚀 **Direct Link:**\n`{stream_link}`\n\n"
            f"⏳ Expires: `{expires_at}`"
        )
        return

    if data.startswith("r2_"):
        try:
            msg_id = int(data.split("_", 1)[1])
        except ValueError:
            return await event.answer("Invalid file reference.", alert=True)

        await event.answer("Uploading to R2...", alert=False)
        tg_msg = await client.get_messages(event.chat_id, ids=msg_id)
        if not tg_msg or not tg_msg.file:
            return await event.respond("❌ Telegram file not found.")

        async with global_semaphore:
            task_code, _ = new_task("telegram-r2")
            workspace = f"dl_{uuid.uuid4().hex[:10]}"
            os.makedirs(workspace, exist_ok=True)
            status = await event.respond(
                "⬇️ **Downloading from Telegram...**",
                buttons=task_cancel_button(task_code),
            )
            start_t = time.time()

            try:
                file_path = await download_telegram_to_file(
                    tg_msg,
                    workspace,
                    status,
                    start_t,
                    task_code,
                )
                filename = os.path.basename(file_path)

                if filename.lower().endswith(".zip"):
                    kind, hls_url = await upload_zip_or_hls(
                        file_path,
                        workspace,
                        status,
                        None,
                        time.time(),
                    )
                    if kind == "HLS":
                        await status.edit(
                            f"✅ **HLS Uploaded!**\n\n"
                            f"📦 `{filename}`\n"
                            f"📺 **Master Stream:**\n`{hls_url}`",
                            link_preview=False,
                        )
                    else:
                        r2_url, code = await upload_to_r2(file_path, status)
                        await status.edit(
                            f"✅ **R2 Upload Complete!**\n🎬 `{filename}`\n🔗 `{r2_url}`",
                            buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]],
                            link_preview=False,
                        )
                else:
                    r2_url, code = await upload_to_r2(file_path, status)
                    await status.edit(
                        f"✅ **R2 Upload Complete!**\n🎬 `{filename}`\n🔗 `{r2_url}`",
                        buttons=[[Button.inline("🗑️ Delete from R2", data=f"delr2_{code}")]],
                        link_preview=False,
                    )

            except asyncio.CancelledError:
                await status.edit("🛑 **Task cancelled.**")
            except Exception as exc:
                logger.exception("Telegram → R2 failed")
                await status.edit(f"❌ **Error:** `{str(exc)[:1500]}`")
            finally:
                remove_task(task_code)
                shutil.rmtree(workspace, ignore_errors=True)
                free_memory()


# ============================================
# --- 10. R2 UPLOAD ENTRY POINT ---
# ============================================

async def upload_to_r2(
    file_path: str,
    msg,
    target_folder: str | None = None,
) -> tuple[str, str]:
    start_t = time.time()
    loop = asyncio.get_running_loop()
    filename = sanitize_filename(os.path.basename(file_path), "downloaded_file")

    now = dt.datetime.now(dt.timezone.utc)
    if target_folder:
        prefix = sanitize_prefix(target_folder, "uploads")
    else:
        prefix = f"{now.year}/{now.month:02d}/{now.day:02d}"

    s3_key = f"{prefix}/{filename}"

    await msg.edit(
        f"⬆️ **Uploading to Cloudflare R2...**\n🎬 `{filename}`"
    )

    await asyncio.to_thread(
        sync_r2_upload,
        file_path,
        s3_key,
        loop,
        msg,
        start_t,
    )

    purge_expired_link_storage()
    code = secrets.token_urlsafe(10)
    link_storage[code] = {
        "kind": "r2",
        "s3_key": s3_key,
        "created_at": time.time(),
    }

    return build_r2_public_url(s3_key), code


# ============================================
# --- 11. STARTUP ---
# ============================================

async def main():
    validate_startup_config()

    app = web.Application(client_max_size=0)
    app.add_routes(routes)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("HTTP server started on port %s", PORT)

    try:
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Telegram client started.")
        await client.run_until_disconnected()
    finally:
        await runner.cleanup()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
