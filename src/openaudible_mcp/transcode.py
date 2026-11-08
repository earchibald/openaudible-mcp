"""Local M4B/M4A -> MP3 conversion with ffmpeg: whole books (keeping tags, chapters and
cover) and single tracks cut from a book."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Awaitable, Callable

ProgressFn = Callable[[float, float | None], Awaitable[None]]


class TranscodeError(RuntimeError):
    pass


def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise TranscodeError("ffmpeg is not installed (brew install ffmpeg); it is needed to make MP3s locally")
    return path


def mp3_command(ffmpeg: str, src: Path, dest: Path, bitrate: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(src),
        "-map", "0:a:0", "-map", "0:v?",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        "-c:v", "copy", "-disposition:v", "attached_pic",
        "-map_metadata", "0", "-map_chapters", "0",
        "-id3v2_version", "3",
        "-progress", "pipe:1", "-nostats",
        str(dest),
    ]


def track_command(
    ffmpeg: str,
    src: Path,
    dest: Path,
    start_s: float,
    length_s: float,
    bitrate: str,
    tags: dict[str, str],
    mono: bool = False,
) -> list[str]:
    """One MP3 track cut from src. Seeking before -i is frame-accurate when re-encoding."""
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{start_s:.3f}", "-t", f"{length_s:.3f}",
        "-i", str(src),
        "-map", "0:a:0", "-vn",
        "-c:a", "libmp3lame", "-b:a", bitrate,
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-id3v2_version", "3",
    ]
    if mono:
        cmd += ["-ac", "1"]
    for k, v in tags.items():
        cmd += ["-metadata", f"{k}={v}"]
    return cmd + ["-progress", "pipe:1", "-nostats", str(dest)]


async def to_mp3(
    src: Path,
    dest: Path,
    bitrate: str,
    total_seconds: float | None = None,
    progress: ProgressFn | None = None,
) -> Path:
    return await _run(mp3_command(ffmpeg_path(), src, _part(dest), bitrate), dest, total_seconds, progress)


async def mp3_track(
    src: Path,
    dest: Path,
    start_s: float,
    length_s: float,
    bitrate: str,
    tags: dict[str, str],
    progress: ProgressFn | None = None,
    mono: bool = False,
) -> Path:
    cmd = track_command(ffmpeg_path(), src, _part(dest), start_s, length_s, bitrate, tags, mono)
    return await _run(cmd, dest, length_s, progress)


def _part(dest: Path) -> Path:
    return dest.with_name(dest.stem + ".part.mp3")


async def _run(cmd: list[str], dest: Path, total_seconds: float | None, progress: ProgressFn | None) -> Path:
    tmp = Path(cmd[-1])
    # argv list, no shell: paths with spaces or quotes are passed through untouched.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stderr_task = asyncio.create_task(proc.stderr.read())
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if progress and line.startswith("out_time_us="):
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                await progress(done, total_seconds)
        code = await proc.wait()
    except BaseException:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise
    err = (await stderr_task).decode(errors="replace").strip()
    if code != 0:
        tmp.unlink(missing_ok=True)
        raise TranscodeError(f"ffmpeg failed ({code}): {err[-500:]}")
    tmp.replace(dest)
    return dest
