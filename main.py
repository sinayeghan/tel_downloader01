import asyncio
import atexit
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import imageio_ffmpeg
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==============================================================================
# Logging Configuration
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==============================================================================
# Environment & Runtime Configuration
# ==============================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", secrets.token_urlsafe(32))

PROXY_URL = os.getenv("PROXY_URL")
YTDLP_VERBOSE = os.getenv("YTDLP_VERBOSE", "0") == "1"

# Optional cookies path (not required; kept only for private/age-gated fallback)
COOKIE_FILE = os.getenv("COOKIE_FILE", "/etc/secrets/youtube_cookies.txt")

# Optional Cobalt API fallback.
# IMPORTANT: point this at a Cobalt instance that runs on a NON-datacenter IP
# (e.g. a home PC exposed through a Cloudflare Tunnel), or at an instance whose
# owner gave you access. A Cobalt instance hosted on Render has the same
# YouTube IP block as this bot. The public api.cobalt.tools does not allow
# use from other projects without permission.
COBALT_API_URL = os.getenv("COBALT_API_URL", "").strip().rstrip("/")
COBALT_API_KEY = os.getenv("COBALT_API_KEY", "").strip()
COBALT_AUTH_SCHEME = os.getenv("COBALT_AUTH_SCHEME", "Api-Key").strip()  # or "Bearer"

# PO Token (bgutil) HTTP Server configuration
POT_SERVER_HOST = "127.0.0.1"
POT_SERVER_PORT = int(os.getenv("POT_SERVER_PORT", "4416"))
POT_SERVER_URL = os.getenv(
    "POT_SERVER_URL", f"http://{POT_SERVER_HOST}:{POT_SERVER_PORT}"
)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
MAX_FILE_SIZE = 49 * 1024 * 1024  # 49 MB Telegram Bot API limit

BASE_DIR = Path(__file__).resolve().parent
BGUTIL_SERVER_DIR = BASE_DIR / "bgutil-server" / "server"

_pot_process: subprocess.Popen | None = None

# After a YouTube bot-check failure, go straight to the fallback for this long
# instead of making the user wait for yt-dlp to fail again.
BLOCK_TTL_SECONDS = 600
_youtube_blocked_until: float = 0.0

HTTP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ==============================================================================
# Runtime Discovery & bgutil PO Token Server Management
# ==============================================================================
def find_deno() -> str | None:
    """Locate the Deno binary across common local and Render paths."""
    candidates = [
        os.getenv("DENO_BIN"),
        shutil.which("deno"),
        str(BASE_DIR / ".deno" / "bin" / "deno"),
        str(Path.home() / ".deno" / "bin" / "deno"),
        "/opt/render/project/src/.deno/bin/deno",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


def find_node() -> str | None:
    """Locate the Node.js binary."""
    return os.getenv("NODE_BIN") or shutil.which("node")


DENO_PATH = find_deno()
NODE_PATH = find_node()


def is_pot_server_alive(timeout: float = 1.5) -> bool:
    """Check if the bgutil HTTP server responds to /ping."""
    try:
        with urllib.request.urlopen(f"{POT_SERVER_URL}/ping", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def start_pot_server() -> bool:
    """Start the bgutil HTTP server on 127.0.0.1:4416 (prefers prebuilt Node.js)."""
    global _pot_process

    if is_pot_server_alive():
        return True

    node_build_js = BGUTIL_SERVER_DIR / "build" / "main.js"
    deno_src_ts = BGUTIL_SERVER_DIR / "src" / "main.ts"
    node_modules_dir = BGUTIL_SERVER_DIR / "node_modules"

    cmd: list[str] | None = None
    cwd: str = str(BGUTIL_SERVER_DIR)

    if NODE_PATH and node_build_js.is_file():
        cmd = [
            NODE_PATH,
            str(node_build_js),
            "--host",
            POT_SERVER_HOST,
            "--port",
            str(POT_SERVER_PORT),
        ]
    elif DENO_PATH and node_modules_dir.is_dir() and deno_src_ts.is_file():
        cwd = str(node_modules_dir)
        cmd = [
            DENO_PATH,
            "run",
            "--allow-env",
            "--allow-net",
            "--allow-ffi=.",
            "--allow-read=.",
            "../src/main.ts",
            "--host",
            POT_SERVER_HOST,
            "--port",
            str(POT_SERVER_PORT),
        ]

    if not cmd:
        logger.warning(
            "bgutil-server not built or runtime missing (node=%s, deno=%s, dir=%s).",
            NODE_PATH,
            DENO_PATH,
            BGUTIL_SERVER_DIR,
        )
        return False

    if _pot_process is not None and _pot_process.poll() is None:
        _pot_process.terminate()
        try:
            _pot_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _pot_process.kill()

    env = os.environ.copy()
    if PROXY_URL:
        # Ensure bgutil generates PO Tokens from the same outbound IP as yt-dlp
        env["HTTP_PROXY"] = PROXY_URL
        env["HTTPS_PROXY"] = PROXY_URL
        env["ALL_PROXY"] = PROXY_URL

    logger.info("Starting bgutil PO Token HTTP server (%s)...", cmd[0])
    _pot_process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )

    for _ in range(40):
        if is_pot_server_alive(timeout=1.0):
            logger.info("bgutil PO Token HTTP server is ready on %s.", POT_SERVER_URL)
            return True
        if _pot_process.poll() is not None:
            logger.error(
                "bgutil PO Token server exited prematurely with code %s.",
                _pot_process.returncode,
            )
            return False
        time.sleep(0.5)

    logger.warning("Timed out waiting for bgutil PO Token server /ping.")
    return False


def stop_pot_server() -> None:
    """Terminate the bgutil background server on exit."""
    global _pot_process
    if _pot_process is not None and _pot_process.poll() is None:
        _pot_process.terminate()
        try:
            _pot_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _pot_process.kill()
    _pot_process = None


atexit.register(stop_pot_server)


# ==============================================================================
# Error classification helpers
# ==============================================================================
class CobaltError(Exception):
    """Raised when the optional Cobalt fallback cannot deliver a file."""


class FileTooLargeError(RuntimeError):
    """Raised when the result exceeds Telegram's 49 MB bot limit."""


def is_bot_block(error: Exception | None) -> bool:
    if error is None:
        return False
    lowered = str(error).lower()
    return "sign in to confirm" in lowered or "not a bot" in lowered


def is_size_error(error: Exception | None) -> bool:
    if error is None:
        return False
    if isinstance(error, FileTooLargeError):
        return True
    lowered = str(error).lower()
    return "max-filesize" in lowered or "larger than" in lowered


def is_private_error(error: Exception | None) -> bool:
    if error is None:
        return False
    lowered = str(error).lower()
    return "private video" in lowered or "members-only" in lowered


def should_try_cobalt(error: Exception | None) -> bool:
    """Use the fallback for anything except cases it cannot improve."""
    return cobalt_enabled() and not is_size_error(error) and not is_private_error(error)


def is_youtube_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return (
        host == "youtu.be"
        or host.endswith(".youtu.be")
        or host == "youtube.com"
        or host.endswith(".youtube.com")
        or host == "youtube-nocookie.com"
        or host.endswith(".youtube-nocookie.com")
    )


def mark_youtube_blocked() -> None:
    global _youtube_blocked_until
    _youtube_blocked_until = time.time() + BLOCK_TTL_SECONDS


def youtube_recently_blocked() -> bool:
    return time.time() < _youtube_blocked_until


# ==============================================================================
# Optional Cobalt API fallback
# ==============================================================================
def cobalt_enabled() -> bool:
    return bool(COBALT_API_URL)


def _cobalt_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": HTTP_USER_AGENT,
    }
    if COBALT_API_KEY:
        headers["Authorization"] = f"{COBALT_AUTH_SCHEME} {COBALT_API_KEY}"
    return headers


def cobalt_resolve(url: str, quality: str) -> tuple[str, str | None]:
    """Ask the Cobalt API for a direct download URL. Returns (url, filename)."""
    payload: dict[str, str] = {"url": url, "filenameStyle": "basic"}
    if quality == "mp3":
        payload.update(
            {"downloadMode": "audio", "audioFormat": "mp3", "audioBitrate": "128"}
        )
    else:
        payload.update(
            {
                "downloadMode": "auto",
                "videoQuality": quality,
                "youtubeVideoCodec": "h264",
            }
        )

    request = urllib.request.Request(
        f"{COBALT_API_URL}/",
        data=json.dumps(payload).encode("utf-8"),
        headers=_cobalt_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise CobaltError(f"Cobalt HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise CobaltError(f"Cobalt unreachable: {exc}") from exc

    status = data.get("status")
    if status in ("tunnel", "redirect"):
        return data["url"], data.get("filename")
    if status == "picker":
        items = data.get("picker") or []
        chosen = next((i for i in items if i.get("type") == "video"), None) or (
            items[0] if items else None
        )
        if chosen and chosen.get("url"):
            return chosen["url"], None
        raise CobaltError("Cobalt returned an empty picker.")
    if status == "error":
        code = (data.get("error") or {}).get("code", "unknown")
        raise CobaltError(f"Cobalt error: {code}")
    raise CobaltError(f"Cobalt returned unsupported status: {status}")


def cobalt_download(url: str, quality: str, folder: str) -> str:
    """Download a file through Cobalt into folder, enforcing the 49 MB limit."""
    file_url, filename = cobalt_resolve(url, quality)

    ext = Path(filename).suffix if filename else ""
    if not ext:
        ext = ".mp3" if quality == "mp3" else ".mp4"
    target = Path(folder) / f"media{ext}"

    request = urllib.request.Request(
        file_url, headers={"User-Agent": HTTP_USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_FILE_SIZE:
                raise FileTooLargeError("File is larger than max-filesize (49MB)")

            written = 0
            with open(target, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_FILE_SIZE:
                        raise FileTooLargeError("File is larger than max-filesize (49MB)")
                    out.write(chunk)
    except FileTooLargeError:
        target.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise CobaltError(f"Cobalt download failed: {exc}") from exc

    if not target.is_file() or target.stat().st_size == 0:
        raise CobaltError("Cobalt returned an empty file.")
    return str(target)


# ==============================================================================
# yt-dlp Options & Extraction Logic
# ==============================================================================
PRIMARY_CLIENTS = ["mweb", "tv_simply", "android_vr", "web"]
FALLBACK_CLIENTS = ["android_vr", "web_embedded", "web_safari"]


def load_optional_cookies(options: dict, folder: str) -> None:
    """Attach cookies only if an optional cookie file exists and is non-empty."""
    cookie_src = Path(COOKIE_FILE)
    if not cookie_src.is_file() or cookie_src.stat().st_size == 0:
        return
    temp_cookie = str(Path(folder) / "cookies.txt")
    shutil.copyfile(cookie_src, temp_cookie)
    options["cookiefile"] = temp_cookie


def base_options(folder: str, player_clients: list[str]) -> dict:
    """Build yt-dlp options configured for cookie-less PO Token extraction."""
    options = {
        "noplaylist": True,
        "socket_timeout": 45,
        "retries": 3,
        "sleep_requests": 1.5,
        "quiet": not YTDLP_VERBOSE,
        "no_warnings": not YTDLP_VERBOSE,
        "verbose": YTDLP_VERBOSE,
        "ffmpeg_location": FFMPEG_PATH,
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
            },
            "youtubepot-bgutilhttp": {
                "base_url": [POT_SERVER_URL],
            },
        },
    }

    if DENO_PATH:
        options["js_runtimes"] = {"deno": {"path": DENO_PATH}}

    if PROXY_URL:
        options["proxy"] = PROXY_URL

    load_optional_cookies(options, folder)
    return options


def ensure_pot_ready() -> None:
    """Ensure the local bgutil server is running before calling yt-dlp."""
    if not is_pot_server_alive(timeout=1.0):
        start_pot_server()


def friendly_error(error: Exception) -> str:
    """Convert raw exceptions into concise Persian user messages."""
    text = str(error)
    lowered = text.lower()

    if is_bot_block(error):
        hint = (
            "برای رفع دائمی، یکی از این دو متغیر را در Render تنظیم کنید:\n"
            "• COBALT_API_URL (و COBALT_API_KEY) برای یک سرور Cobalt روی IP غیر دیتاسنتری\n"
            "• PROXY_URL برای یک پروکسی مسکونی (Residential)"
        )
        return (
            "🚫 یوتیوب درخواست این IP سرور را به عنوان ربات تشخیص داده است.\n"
            f"{hint}\n\n"
            f"جزئیات: {text[:300]}"
        )
    if isinstance(error, CobaltError):
        return (
            "❌ دانلود از یوتیوب با روش اصلی و روش جایگزین (Cobalt) هر دو ناموفق بود.\n\n"
            f"جزئیات: {text[:400]}"
        )
    if "requested format is not available" in lowered:
        return (
            "⚠️ کیفیت انتخاب‌شده برای این ویدیو در دسترس نیست. لطفاً کیفیت دیگری را امتحان کنید.\n\n"
            f"جزئیات: {text[:300]}"
        )
    if is_size_error(error):
        return (
            "📦 حجم این فایل از محدودیت ۴۹ مگابایتی ربات تلگرام بیشتر است.\n"
            "لطفاً کیفیت پایین‌تر (مثل 480p) یا گزینه صوتی MP3 را انتخاب کنید."
        )
    if is_private_error(error):
        return "🔒 این ویدیو خصوصی یا مخصوص اعضای ویژه است و بدون دسترسی قابل دانلود نیست."

    return f"❌ خطا در پردازش لینک:\n{text[:600]}"


def extract_info(url: str) -> dict:
    """Extract media metadata with primary + fallback client strategies.

    If yt-dlp fails and a Cobalt fallback is configured, returns a stub dict
    (without title) so the user can still pick a quality and download via Cobalt.
    """
    youtube = is_youtube_url(url)

    # Known-blocked YouTube IP: don't make the user wait for yt-dlp to fail again.
    if youtube and cobalt_enabled() and youtube_recently_blocked():
        logger.info("YouTube recently blocked; skipping yt-dlp metadata, using Cobalt path.")
        return {"title": None, "_via": "cobalt"}

    ensure_pot_ready()
    last_error: Exception | None = None

    for clients in (PRIMARY_CLIENTS, FALLBACK_CLIENTS):
        try:
            with tempfile.TemporaryDirectory() as folder:
                opts = base_options(folder, clients)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and info.get("formats"):
                    logger.info(
                        "Extracted '%s' (%d formats) using clients=%s",
                        info.get("title", "unknown"),
                        len(info["formats"]),
                        clients,
                    )
                    return info
        except Exception as exc:
            last_error = exc
            logger.warning("extract_info failed with clients=%s: %s", clients, exc)

    if youtube and is_bot_block(last_error):
        mark_youtube_blocked()

    if should_try_cobalt(last_error):
        logger.info("yt-dlp metadata failed; continuing with Cobalt fallback.")
        return {"title": None, "_via": "cobalt"}

    raise last_error or RuntimeError("اطلاعاتی برای این لینک یافت نشد.")


def _download_with_ytdlp(url: str, quality: str, folder: str) -> str:
    """Download with yt-dlp (primary + fallback client sets)."""
    ensure_pot_ready()
    last_error: Exception | None = None

    for clients in (PRIMARY_CLIENTS, FALLBACK_CLIENTS):
        try:
            opts = base_options(folder, clients)
            opts["outtmpl"] = str(Path(folder) / "media.%(ext)s")
            opts["max_filesize"] = MAX_FILE_SIZE

            if quality == "mp3":
                opts["format"] = "bestaudio/best"
                opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ]
            else:
                opts["format"] = (
                    f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/"
                    f"bestvideo[height<={quality}]+bestaudio/"
                    f"best[height<={quality}]/best"
                )
                opts["merge_output_format"] = "mp4"

            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)

            files = [
                f
                for f in Path(folder).iterdir()
                if f.is_file()
                and not f.name.endswith((".part", ".ytdl"))
                and f.name != "cookies.txt"
            ]
            if not files:
                raise RuntimeError("فایل دانلودشده یافت نشد (احتمالاً حجم بیش از ۴۹ مگابایت بوده است).")

            downloaded = max(files, key=lambda p: p.stat().st_size)
            if downloaded.stat().st_size > MAX_FILE_SIZE:
                raise FileTooLargeError("File is larger than max-filesize (49MB)")

            return str(downloaded)
        except Exception as exc:
            last_error = exc
            for leftover in Path(folder).iterdir():
                if leftover.is_file() and leftover.name != "cookies.txt":
                    leftover.unlink(missing_ok=True)
            logger.warning("download_media failed with clients=%s: %s", clients, exc)

    raise last_error or RuntimeError("دانلود فایل ناموفق بود.")


def download_media(url: str, quality: str, folder: str) -> str:
    """Download video or audio into folder and enforce the 49 MB Telegram limit."""
    youtube = is_youtube_url(url)

    # Known-blocked YouTube IP: go straight to Cobalt.
    if youtube and cobalt_enabled() and youtube_recently_blocked():
        logger.info("YouTube recently blocked; downloading via Cobalt.")
        return cobalt_download(url, quality, folder)

    try:
        return _download_with_ytdlp(url, quality, folder)
    except Exception as ytdlp_error:
        if youtube and is_bot_block(ytdlp_error):
            mark_youtube_blocked()

        if not should_try_cobalt(ytdlp_error):
            raise

        logger.info("yt-dlp download failed; trying Cobalt fallback.")
        try:
            return cobalt_download(url, quality, folder)
        except CobaltError as cobalt_error:
            logger.warning("Cobalt fallback failed: %s", cobalt_error)
            # Keep the original reason visible (e.g. the bot-check message).
            raise CobaltError(
                f"yt-dlp: {str(ytdlp_error)[:200]} | Cobalt: {cobalt_error}"
            ) from cobalt_error


# ==============================================================================
# Telegram Bot Handlers
# ==============================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "🚀 به ربات **Trendify Media Downloader** خوش آمدید!\n\n"
        "لینک ویدیو یا موسیقی خود را از پلتفرم‌های زیر ارسال کنید:\n"
        "• YouTube | Instagram | TikTok | Facebook | X\n\n"
        "✨ نسخه بدون نیاز به کوکی (مجهز به PO Token خودکار)",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()
    if not (url.startswith("https://") or url.startswith("http://")):
        await update.message.reply_text("❌ لطفاً یک لینک معتبر (شروع‌شده با http یا https) ارسال کنید.")
        return

    status_msg = await update.message.reply_text("⏳ در حال بررسی لینک و دریافت کیفیت‌های موجود...")

    try:
        info = await asyncio.to_thread(extract_info, url)
        title = info.get("title") or "ویدیو (عنوان در دسترس نیست)"
        context.user_data["url"] = url

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎬 1080p", callback_data="1080"),
                    InlineKeyboardButton("🎬 720p", callback_data="720"),
                    InlineKeyboardButton("🎬 480p", callback_data="480"),
                ],
                [
                    InlineKeyboardButton("🎵 دانلود صوتی (MP3)", callback_data="mp3"),
                ],
            ]
        )

        await status_msg.edit_text(
            f"📌 {title}\n\nکیفیت مورد نظر خود را انتخاب کنید:",
            reply_markup=keyboard,
        )
    except Exception as exc:
        await status_msg.edit_text(friendly_error(exc))


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    choice = query.data
    if choice not in {"1080", "720", "480", "mp3"}:
        return

    url = context.user_data.get("url")
    if not url:
        await query.edit_message_text("⚠️ لینک منقضی شده است. لطفاً لینک را دوباره ارسال کنید.")
        return

    label = "MP3" if choice == "mp3" else f"{choice}p"
    await query.edit_message_text(f"⏳ در حال دانلود با کیفیت {label} و آماده‌سازی برای ارسال...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            file_path = await asyncio.to_thread(download_media, url, choice, folder)
            chat_id = query.message.chat_id

            with open(file_path, "rb") as media_file:
                if choice == "mp3":
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=media_file,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
                elif file_path.lower().endswith(".mp4"):
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=media_file,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
                else:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=media_file,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=30,
                        pool_timeout=30,
                    )

        await query.edit_message_text(f"✅ فایل با کیفیت {label} با موفقیت ارسال شد!")
    except Exception as exc:
        await query.edit_message_text(friendly_error(exc)[:700])


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)


# ==============================================================================
# Application Entrypoint
# ==============================================================================
def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    if not HOSTNAME:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME environment variable is required.")

    pot_ready = start_pot_server()

    logger.info("yt-dlp version: %s", yt_dlp.version.__version__)
    logger.info("Node path: %s", NODE_PATH)
    logger.info("Deno path: %s", DENO_PATH)
    logger.info("bgutil PO Token server ready: %s (%s)", pot_ready, POT_SERVER_URL)
    logger.info("Proxy configured: %s", bool(PROXY_URL))
    logger.info("Cobalt fallback configured: %s", cobalt_enabled())
    logger.info("Optional cookie file present: %s", Path(COOKIE_FILE).is_file())

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    webhook_url = f"https://{HOSTNAME}/{WEBHOOK_PATH}"
    logger.info("Starting webhook on 0.0.0.0:%s -> %s", PORT, webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
