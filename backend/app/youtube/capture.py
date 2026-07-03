"""yt-dlp resolution + ffmpeg live-edge audio capture."""
import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

CHUNK_BYTES = 3200  # 100ms of 16kHz s16le mono


def _ydl(opts: dict[str, Any] | None = None):
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "noplaylist": True}
    return yt_dlp.YoutubeDL({**base, **(opts or {})})


async def resolve_channel(url: str) -> dict[str, str]:
    """Channel URL/@handle → {channel_id, title, handle}."""
    def run() -> dict[str, str]:
        with _ydl({"extract_flat": True, "playlist_items": "0"}) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
        return {
            "channel_id": info.get("channel_id") or info.get("id") or "",
            "title": info.get("channel") or info.get("title") or "",
            "handle": (info.get("uploader_id") or "").lstrip("@"),
        }

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def check_live(channel_id: str) -> dict[str, str] | None:
    """Is the channel live right now? → {video_id, title, audio_url} or None."""
    def run() -> dict[str, str] | None:
        import yt_dlp

        url = f"https://www.youtube.com/channel/{channel_id}/live"
        try:
            with _ydl({"format": "bestaudio/best"}) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            return None
        if not info or not info.get("is_live"):
            return None
        audio_url = info.get("url") or ""
        if not audio_url:
            for f in info.get("formats") or []:
                if f.get("acodec") not in (None, "none") and f.get("url"):
                    audio_url = f["url"]
                    break
        if not audio_url:
            return None
        return {
            "video_id": info.get("id", ""),
            "title": info.get("title", ""),
            "audio_url": audio_url,
        }

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def resolve_vod(url: str) -> dict[str, str]:
    """VOD URL → {video_id, title, channel_id}."""
    def run() -> dict[str, str]:
        with _ydl() as ydl:
            info = ydl.extract_info(url, download=False, process=False)
        return {
            "video_id": info.get("id", ""),
            "title": info.get("title", ""),
            "channel_id": info.get("channel_id", ""),
        }

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def download_vod_audio(url: str, out_dir: str) -> str:
    """Download a VOD's audio (m4a) for batch transcription. Returns file path."""
    def run() -> str:
        with _ydl({
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": f"{out_dir}/%(id)s.%(ext)s",
        }) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info)

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def stream_live_pcm(audio_url: str) -> AsyncIterator[bytes]:
    """ffmpeg pulls the live manifest at the live edge → 16kHz mono s16le PCM.

    `-fflags nobuffer -flags low_delay` + `-live_start_index -1` keep us on the
    newest segment; every 100ms chunk is yielded as soon as ffmpeg emits it.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-fflags", "nobuffer", "-flags", "low_delay",
        "-live_start_index", "-1",
        "-i", audio_url,
        "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        if proc.returncode not in (0, -9, None):
            stderr = b""
            if proc.stderr is not None:
                with contextlib.suppress(Exception):
                    stderr = await proc.stderr.read()
            log.warning("ffmpeg_capture_ended", code=proc.returncode,
                        stderr=stderr.decode(errors="replace")[-400:])
