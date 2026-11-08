"""MCP server exposing an OpenAudible library: search, inspect, and fetch MP3/M4B audio."""

from __future__ import annotations

import asyncio
import functools
import html
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, ParamSpec, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import library, yoto
from .client import BOOK_ACTIONS, OpenAudibleClient, OpenAudibleError
from .config import ConfigError, Settings
from .transcode import TranscodeError, mp3_track, to_mp3

INSTRUCTIONS = """\
Tools for a personal OpenAudible audiobook server.

Typical flow: search_library -> get_book -> download_audio.
- Book arguments accept a book id (e.g. BK_TANT_006018), an ASIN, or a title that
  matches one book.
- A book is 'ready' when the server has converted audio (M4B/MP3). Most purchased
  books are 'not_downloaded' until prepared: prepare_book queues the server-side
  Audible download + conversion, or download_audio(prepare_if_missing=true) does it
  and waits.
- The server converts to one format (usually M4B). download_audio(format="mp3")
  transcodes locally with ffmpeg when the server has no MP3.
- For a Yoto player or Yoto card, always use download_for_yoto. It makes numbered
  MP3 tracks that fit Yoto's limits (100 tracks / 500 MB per card, tracks under 60 min).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

mcp = MCPServer("openaudible", instructions=INSTRUCTIONS)
settings = Settings()
client = OpenAudibleClient(settings)

P = ParamSpec("P")
R = TypeVar("R")


def reported(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Turn expected failures into ToolErrors so their text reaches the client."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except (OpenAudibleError, TranscodeError, ConfigError) as e:
            raise ToolError(str(e)) from e

    return wrapper


def _plain(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _size(n: int) -> str:
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{n / 1024:.0f} KB"


def _file_view(f: dict[str, Any]) -> dict[str, Any]:
    out = {"name": f["name"], "type": f["type"], "url": client.public_url(f)}
    if f.get("size"):
        out["size"] = _size(f["size"])
    if f.get("missing"):
        out["missing_on_disk"] = True
    return out


# -- browsing -----------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@reported
async def search_library(
    query: str = "",
    author: str = "",
    narrator: str = "",
    series: str = "",
    status: library.Status = "any",
    audio_format: Literal["", "mp3", "m4b", "m4a"] = "",
    sort: library.SortKey = "relevance",
    limit: int = 20,
    offset: int = 0,
    refresh: bool = False,
) -> dict[str, Any]:
    """Search the OpenAudible library.

    query matches words in title, author, narrator, series, publisher, ASIN or id.
    author/narrator/series are substring filters. status: 'ready' = has audio on the
    server, 'no_audio' = not yet downloaded/converted. sort by relevance, title,
    author, purchase_date or release_date (newest first), or duration (longest first).
    """
    books = await client.books(refresh=refresh)
    hits = library.search(books, query, author, narrator, series, status, audio_format, sort)
    limit = max(1, min(limit, 200))
    page = hits[offset : offset + limit]
    return {
        "total_matches": len(hits),
        "library_size": len(books),
        "offset": offset,
        "results": [library.summarize(b) for b in page],
    }


@mcp.tool(annotations=READ_ONLY)
@reported
async def get_book(book: str, include_description: bool = True) -> dict[str, Any]:
    """Details for one book: metadata, state, files on the server (with streaming URLs), chapter count."""
    record = await client.resolve_book(book)
    files = await client.files(record)
    detail = library.summarize(record)
    detail.pop("audio_file_count", None)
    detail["state"] = library.derive_state(files)
    detail.update(
        {
            k: record.get(k)
            for k in ("asin", "publisher", "release_date", "genre", "language", "info_link", "finished")
            if record.get(k) not in (None, "")
        }
    )
    if record.get("rating_average"):
        detail["rating"] = f"{float(record['rating_average']):.2f} ({record.get('rating_count', '?')})"
    detail["chapter_count"] = len(record.get("chapters") or [])
    if include_description:
        detail["summary"] = _plain(record.get("summary") or record.get("description"), 1500)
    detail["files"] = [_file_view(f) for f in files]
    detail["streaming_note"] = "File URLs need the same HTTP Basic credentials as this server."
    return {k: v for k, v in detail.items() if v not in (None, "")}


@mcp.tool(annotations=READ_ONLY)
@reported
async def get_chapters(book: str) -> dict[str, Any]:
    """Chapter list for a book with start times (HH:MM:SS) and lengths."""
    record = await client.resolve_book(book)
    data = await client.chapters_flat(record["key"])

    def hms(ms: int) -> str:
        s = int(ms // 1000)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    chapters = [
        {
            "index": c.get("index", i),
            "title": c.get("title"),
            "start": hms(c.get("start_offset_ms", 0)),
            "length": hms(c.get("length_ms", 0)),
            **({"level": c["level"]} if c.get("level") else {}),
        }
        for i, c in enumerate(data.get("chapters", []))
    ]
    return {"id": record["key"], "title": record.get("title"), "chapters": chapters}


# -- getting audio -------------------------------------------------------------


async def _trigger_prepare(book_id: str, state: str) -> str:
    action = {"not_downloaded": "download", "downloaded_not_converted": "convert"}.get(state)
    if action is None:
        return "none (already ready)"
    try:
        await client.book_action(book_id, action)
        return action
    except OpenAudibleError as e:
        if e.status == 409:  # already queued or running
            return f"{action} (already in progress)"
        raise


async def _wait_until_ready(
    record: dict[str, Any], timeout_s: float, ctx: Context | None
) -> list[dict[str, Any]]:
    """Queue server-side download/convert as needed and poll until audio exists."""
    book_id = record["key"]
    deadline = time.monotonic() + timeout_s
    convert_requested = False
    stalled_polls = 0
    files = await client.files(record)
    state = library.derive_state(files)
    await _trigger_prepare(book_id, state)
    while state != "ready":
        if time.monotonic() > deadline:
            raise OpenAudibleError(
                f"'{record.get('title')}' is still {state} after {timeout_s / 60:.0f} min; "
                "the server keeps working on it - call download_audio again later.",
                504,
            )
        if ctx:
            await ctx.info(f"Waiting for server: {state}")
        await asyncio.sleep(15)
        files = await client.files(record)
        state = library.derive_state(files)
        if state == "downloaded_not_converted" and not convert_requested:
            # The server only converts automatically when 'auto convert' is on.
            stalled_polls += 1
            if stalled_polls >= 2:
                await _trigger_prepare(book_id, state)
                convert_requested = True
    await client.library(refresh=True)
    return files


async def _ensure_audio(
    record: dict[str, Any],
    files: list[dict[str, Any]],
    choose: Callable[[list[dict[str, Any]]], tuple[str, list[dict[str, Any]]] | None],
    want: str,
    prepare_if_missing: bool,
    wait_timeout_minutes: int,
    ctx: Context,
) -> tuple[list[dict[str, Any]], tuple[str, list[dict[str, Any]]]]:
    """Return (files, chosen audio), preparing the book on the server first if asked."""
    title = record.get("title") or record["key"]
    chosen = choose(files)
    if chosen is not None:
        return files, chosen
    state = library.derive_state(files)
    if any(f.get("missing") for f in files) and state != "ready":
        raise OpenAudibleError(
            f"The server lists audio for '{title}' but the files are not on disk. "
            "Run run_command('Refresh_Files') so OpenAudible rescans, or download the original book."
        )
    if state == "ready":
        have = ", ".join(sorted({f["type"] for f in files if f["kind"] == "audio"}))
        raise OpenAudibleError(f"'{title}' has no {want.upper()} on the server (it has: {have}). Try format='best'.")
    if not prepare_if_missing:
        raise OpenAudibleError(
            f"'{title}' has no audio on the server yet (state: {state}). "
            "Call prepare_book, or call this tool again with prepare_if_missing=true."
        )
    await ctx.info(f"Preparing '{title}' on the server ({state})")
    files = await _wait_until_ready(record, wait_timeout_minutes * 60, ctx)
    chosen = choose(files)
    if chosen is None:
        raise OpenAudibleError(f"'{title}' is ready but has no {want.upper()} audio")
    return files, chosen


async def _save_files(
    items: list[tuple[dict[str, Any], Path]], ctx: Context, overwrite: bool
) -> tuple[list[dict[str, Any]], list[str], set[str]]:
    """Download (server file, local path) pairs. Returns results, skipped names, fetched paths."""
    total_bytes = sum(f.get("size") or 0 for f, _ in items) or None
    done_bytes = 0
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    fetched: set[str] = set()
    for i, (f, dest) in enumerate(items):
        if dest.exists() and not overwrite and (not f.get("size") or dest.stat().st_size == f["size"]):
            skipped.append(dest.name)
            done_bytes += dest.stat().st_size
            results.append({"path": str(dest), "type": f["type"], "size": _size(dest.stat().st_size)})
            continue
        base = done_bytes

        async def on_progress(n: int, total: int | None, base: int = base, i: int = i) -> None:
            msg = f"Downloading {i + 1}/{len(items)}: {n / 1_048_576:.0f} MB"
            await ctx.report_progress(base + n, total_bytes or total, msg)

        size = await client.download(f, dest, on_progress)
        done_bytes += size
        fetched.add(str(dest))
        results.append({"path": str(dest), "type": f["type"], "size": _size(size)})
    return results, skipped, fetched


def _book_folder(title: str, dest_dir: str | None) -> Path:
    folder = Path(dest_dir).expanduser() if dest_dir else settings.download_dir
    return folder / library.safe_filename(title)


def _cover(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in files if f["type"] == "ART"][:1]


@mcp.tool(annotations=ACTION)
@reported
async def download_audio(
    book: str,
    ctx: Context,
    format: Literal["best", "m4b", "mp3", "m4a"] = "best",
    dest_dir: str | None = None,
    prepare_if_missing: bool = False,
    wait_timeout_minutes: int = 30,
    include_cover: bool = True,
    overwrite: bool = False,
    mp3_bitrate: str | None = None,
    keep_source: bool = False,
) -> dict[str, Any]:
    """Download a book's audio from the server to this computer.

    format: 'best' takes what the server has (M4B, else MP3, else M4A). 'mp3' uses the
    server's MP3s if any, otherwise downloads the M4B and transcodes it locally with
    ffmpeg, keeping chapters, tags and cover (mp3_bitrate, default from
    OPENAUDIBLE_MP3_BITRATE or 96k; keep_source keeps the M4B too).
    Files go to dest_dir/<title>/ (default OPENAUDIBLE_DOWNLOAD_DIR, ~/Downloads/OpenAudible).
    prepare_if_missing: if the server has no audio yet, queue the Audible download and
    conversion on the server and wait up to wait_timeout_minutes before downloading.
    Existing files of the same size are skipped unless overwrite is true.
    For a Yoto player or card, use download_for_yoto instead.
    """
    record = await client.resolve_book(book)
    title = record.get("title") or record["key"]
    want = format

    def choose(fs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]] | None:
        if want == "mp3":
            return library.pick_audio(fs, "MP3") or library.pick_audio(fs, "best")
        return library.pick_audio(fs, want)

    files, (server_type, audio) = await _ensure_audio(
        record, await client.files(record), choose, want, prepare_if_missing, wait_timeout_minutes, ctx
    )
    folder = _book_folder(title, dest_dir)
    single = len(audio) == 1

    def local_name(f: dict[str, Any]) -> str:
        ext = Path(f["name"]).suffix.lower()
        return library.safe_filename(title) + ext if single else library.safe_filename(Path(f["name"]).stem) + ext

    items = [(f, folder / local_name(f)) for f in audio]
    if include_cover:
        items += [(f, folder / ("cover" + Path(f["name"]).suffix.lower())) for f in _cover(files)]
    results, skipped, fetched = await _save_files(items, ctx, overwrite)

    source = f"server {server_type}"
    if want == "mp3" and server_type != "MP3":
        src_entry = next(r for r in results if r["type"] == server_type)
        src = Path(src_entry["path"])
        mp3 = src.with_suffix(".mp3")
        if mp3.exists() and not overwrite:
            skipped.append(mp3.name)
        else:
            total_s = float(library.duration_seconds(record.get("duration")) or 0) or None

            async def on_transcode(done_s: float, total: float | None) -> None:
                await ctx.report_progress(done_s, total, f"Transcoding to MP3: {done_s / 60:.0f} min")

            await ctx.info(f"Transcoding {src.name} to MP3 locally")
            await to_mp3(src, mp3, mp3_bitrate or settings.mp3_bitrate, total_s, on_transcode)
        results.append({"path": str(mp3), "type": "MP3", "size": _size(mp3.stat().st_size)})
        # Drop the intermediate M4B only if this call fetched it; keep one the user already had.
        if not keep_source and str(src) in fetched:
            src.unlink(missing_ok=True)
            results.remove(src_entry)
        source = f"server {server_type}, transcoded locally to MP3"

    return {
        "id": record["key"],
        "title": title,
        "folder": str(folder),
        "source": source,
        "files": results,
        **({"skipped_existing": skipped} if skipped else {}),
    }


YOTO_TRACK_FILE = re.compile(r"^\d{2,3} - .+\.mp3$")


def _hms(ms: int) -> str:
    s = int(ms // 1000)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"


@mcp.tool(annotations=ACTION)
@reported
async def download_for_yoto(
    book: str,
    ctx: Context,
    dest_dir: str | None = None,
    prepare_if_missing: bool = False,
    wait_timeout_minutes: int = 30,
    bitrate: str | None = None,
    max_bitrate: str | None = None,
    overwrite: bool = False,
    keep_source: bool = False,
) -> dict[str, Any]:
    """Make a book ready for a Yoto player: numbered MP3 tracks for a Yoto "Make Your Own"
    card playlist. Use this whenever the user wants audio for a Yoto player or card.

    Follows OpenAudible's "Split for Yoto" rules: one track per chapter, chapters over
    60 minutes cut into equal parts, each track under 100 MB, at most 100 tracks and
    500 MB per card. Long books are spread over several cards ("Card 1 of 2", ...).
    The bitrate is chosen automatically: the highest up to max_bitrate (default
    OPENAUDIBLE_MP3_BITRATE or 96k) that keeps the book on as few cards as possible;
    64k and below are encoded in mono. Pass bitrate (e.g. '64k') to force one.
    Tracks go to dest_dir/<title>/Yoto/ with cover.jpg for the card art. The server's
    M4B is fetched and cut up locally with ffmpeg; it is deleted afterwards unless
    keep_source is true or it was already there. prepare_if_missing works as in
    download_audio. Existing tracks are kept unless overwrite is true.
    """
    record = await client.resolve_book(book)
    title = record.get("title") or record["key"]
    author = record.get("author") or ""
    fixed = yoto.parse_kbps(bitrate)
    if bitrate and not fixed:
        raise OpenAudibleError(f"Unrecognized bitrate {bitrate!r}; use a value like '64k'")
    ceiling = yoto.parse_kbps(max_bitrate) or yoto.parse_kbps(settings.mp3_bitrate) or 96

    def choose(fs: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]] | None:
        picked = library.pick_audio(fs, "best")
        return picked if picked and len(picked[1]) == 1 else None

    files = await client.files(record)
    if (picked := library.pick_audio(files, "best")) and len(picked[1]) > 1:
        raise OpenAudibleError(
            f"'{title}' is stored as {len(picked[1])} separate files. Use the single-file copy "
            "of this book (search_library marks split copies with split_into_chapter_files)."
        )
    files, (server_type, audio) = await _ensure_audio(
        record, files, choose, "single-file", prepare_if_missing, wait_timeout_minutes, ctx
    )

    chapters = (await client.chapters_flat(record["key"])).get("chapters", [])
    ends = [int(c.get("start_offset_ms") or 0) + int(c.get("length_ms") or 0) for c in chapters]
    total_ms = max([library.duration_seconds(record.get("duration")) * 1000, *ends])
    if total_ms <= 0:
        raise OpenAudibleError(f"'{title}' has no duration or chapter information to split by")
    plan = yoto.plan(chapters, total_ms, max_kbps=ceiling, kbps=fixed)
    cards = plan.cards
    if not cards:
        raise OpenAudibleError(f"'{title}' has no audio to split")

    book_dir = _book_folder(title, dest_dir)
    yoto_dir = book_dir / "Yoto"
    card_dirs = [yoto_dir] if len(cards) == 1 else [yoto_dir / f"Card {i + 1} of {len(cards)}" for i in range(len(cards))]
    planned: list[tuple[Path, int, int, yoto.Track]] = []
    for c, (card, cdir) in enumerate(zip(cards, card_dirs)):
        width = max(2, len(str(len(card))))
        for n, t in enumerate(card, 1):
            planned.append((cdir / f"{n:0{width}d} - {library.safe_filename(t.title)}.mp3", c, n, t))

    # The layout file records how existing tracks were made, so a changed bitrate or
    # chapter plan is never mixed with old tracks.
    layout_file = yoto_dir / ".yoto-layout.json"
    layout = {"kbps": plan.kbps, "mono": plan.kbps <= yoto.MONO_AT_OR_BELOW, "tracks": [str(p.relative_to(yoto_dir)) for p, *_ in planned]}
    existing = {
        p for d in [yoto_dir, *yoto_dir.glob("Card * of *")] if d.is_dir() for p in d.iterdir() if YOTO_TRACK_FILE.match(p.name)
    }
    try:
        previous = json.loads(layout_file.read_text())
    except (OSError, ValueError):
        previous = None
    if existing and previous != layout and not overwrite:
        was = f" at {previous['kbps']}k" if isinstance(previous, dict) and "kbps" in previous else ""
        raise OpenAudibleError(
            f"{yoto_dir} already has {len(existing)} Yoto track(s){was} from a different layout than this "
            f"one ({len(planned)} tracks at {plan.kbps}k). Call again with overwrite=true to rebuild it."
        )
    if overwrite or previous != layout:
        for p in existing:
            p.unlink()
        for d in yoto_dir.glob("Card * of *"):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        layout_file.unlink(missing_ok=True)

    src_file = audio[0]
    src_path = book_dir / (library.safe_filename(title) + Path(src_file["name"]).suffix.lower())
    items = [(src_file, src_path)] + [(f, yoto_dir / ("cover" + Path(f["name"]).suffix.lower())) for f in _cover(files)]
    saved, _, fetched = await _save_files(items, ctx, overwrite=False)

    todo = [(p, c, n, t) for p, c, n, t in planned if not p.exists()]
    total_s = sum(t.length_ms for *_, t in todo) / 1000 or None
    done: dict[Path, float] = {}
    kbps = f"{plan.kbps}k"
    # Low bitrates sound much better in mono, and the Yoto speaker is mono anyway.
    mono = plan.kbps <= yoto.MONO_AT_OR_BELOW
    gate = asyncio.Semaphore(max(1, min(4, (os.cpu_count() or 2) // 2)))

    async def encode(path: Path, c: int, n: int, t: yoto.Track) -> None:
        async with gate:
            path.parent.mkdir(parents=True, exist_ok=True)
            album = title if len(cards) == 1 else f"{title} (Card {c + 1} of {len(cards)})"
            tags = {
                "title": t.title, "album": album, "artist": author, "album_artist": author,
                "track": f"{n}/{len(cards[c])}", "genre": "Audiobook",
            }
            last = t.start_ms + t.length_ms >= total_ms
            # The last track runs to the end of the file, in case the listed duration is short.
            length_s = t.length_ms / 1000 + (60 if last else 0)

            async def on_progress(s: float, _total: float | None) -> None:
                done[path] = min(s, t.length_ms / 1000)
                await ctx.report_progress(sum(done.values()), total_s, f"Encoding Yoto tracks ({len(cards[c])} on card {c + 1})")

            await mp3_track(src_path, path, t.start_ms / 1000, length_s, kbps, tags, on_progress, mono)

    if todo:
        await ctx.info(f"Encoding {len(todo)} Yoto tracks at {kbps}")
        await asyncio.gather(*(encode(*row) for row in todo))
    yoto_dir.mkdir(parents=True, exist_ok=True)
    layout_file.write_text(json.dumps(layout, indent=1))

    if not keep_source and str(src_path) in fetched:
        src_path.unlink(missing_ok=True)

    warnings: list[str] = []
    out_cards = []
    for c, cdir in enumerate(card_dirs):
        rows = [(p, t) for p, cc, _, t in planned if cc == c]
        sizes = [p.stat().st_size for p, _ in rows]
        for (p, t), sz in zip(rows, sizes):
            if sz > yoto.MAX_TRACK_BYTES:
                warnings.append(f"{p.name} is {_size(sz)}, over Yoto's 100 MB track limit")
        if sum(sizes) > yoto.MAX_CARD_BYTES:
            warnings.append(f"{cdir.name} totals {_size(sum(sizes))}, over Yoto's 500 MB card limit; use a lower bitrate")
        out_cards.append(
            {
                "folder": str(cdir),
                "track_count": len(rows),
                "total_size": _size(sum(sizes)),
                "total_length": _hms(sum(t.length_ms for _, t in rows)),
                "tracks": [f"{p.name} ({_hms(t.length_ms)})" for p, t in rows],
            }
        )
    divided = [t.title.rsplit(" (1 of ", 1)[0] for *_, t in planned if " (1 of " in t.title]
    return {
        "id": record["key"],
        "title": title,
        "source": f"server {server_type}, split locally",
        "bitrate": kbps + (" mono" if mono else ""),
        "card_count": len(cards),
        "cards": out_cards,
        **({"cover": str(p)} if (p := next((Path(r["path"]) for r in saved if r["type"] == "ART"), None)) else {}),
        **({"chapters_divided": divided} if divided else {}),
        **({"warnings": warnings} if warnings else {}),
        "kept_existing_tracks": len(planned) - len(todo),
        "next_steps": (
            "At my.yotoplay.com/my-cards choose 'Create a playlist', upload the tracks of one card "
            "folder in name order (optionally cover.jpg as the card art) and save. Then in the Yoto "
            "app open the playlist, tap 'Link to a card' and hold a blank Make Your Own card to the phone."
        ),
    }


@mcp.tool(annotations=ACTION)
@reported
async def prepare_book(book: str) -> dict[str, Any]:
    """Queue whatever the server needs to make a book's audio available.

    Not downloaded -> queues the Audible download (the server then converts it when
    auto-convert is on). Downloaded but not converted -> queues conversion. Returns
    immediately; check progress with get_book or server_status.
    """
    record = await client.resolve_book(book)
    state = library.derive_state(await client.files(record))
    action = await _trigger_prepare(record["key"], state)
    return {"id": record["key"], "title": record.get("title"), "state_before": state, "queued": action}


@mcp.tool(annotations=ACTION)
@reported
async def book_action(
    book: str, action: Literal["download", "convert", "pdf", "transcribe", "ignore", "unignore"]
) -> dict[str, Any]:
    """Run one server-side action on a book: download (AAX from Audible), convert (to the
    server's configured format), pdf (fetch companion PDF), transcribe, ignore, unignore."""
    assert action in BOOK_ACTIONS
    record = await client.resolve_book(book)
    result = await client.book_action(record["key"], action)
    return {"id": record["key"], "title": record.get("title"), "action": action, "accepted": True, "response": result}


# -- server --------------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
@reported
async def server_status() -> dict[str, Any]:
    """OpenAudible status: library counters (books, to download, MP3/M4B files, ...) and active job queues."""
    data = await client.status()
    queues = {
        name: {k: q[k] for k in ("jobs", "queue", "jobsCompleted", "fail", "errors") if k in q}
        for name, q in (data.get("queues") or {}).items()
        if q.get("jobs") or q.get("queue") or q.get("fail") or q.get("errors")
    }
    counters = {k: v for k, v in (data.get("status") or {}).items() if v not in ("", None)}
    return {"version": data.get("version"), "counters": counters, "active_queues": queues or "idle"}


@mcp.tool(annotations=ACTION)
@reported
async def run_command(
    command: Literal[
        "Sync_Quick", "Sync_Full", "Refresh_Files", "Download_All", "Convert_All", "Cancel_All_Jobs", "Check_For_Update"
    ],
) -> dict[str, Any]:
    """Run a global OpenAudible command. Sync_Quick/Sync_Full pull new purchases from Audible;
    Refresh_Files rescans the library folders; Download_All/Convert_All queue every pending
    book (can be many hours of work); Cancel_All_Jobs stops queued work."""
    result = await client.run_command(command)
    return {"command": command, "accepted": True, "response": result}


@mcp.tool(annotations=ACTION)
@reported
async def export_library(
    format: Literal["csv", "tsv", "json", "xlsx"] = "csv",
    dest_path: str | None = None,
    include_file_paths: bool = False,
) -> dict[str, Any]:
    """Save the library list as csv/tsv/json/xlsx (default to the download folder)."""
    data = await client.export(format, include_file_paths)
    dest = Path(dest_path).expanduser() if dest_path else settings.download_dir / f"openaudible-library.{format}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"path": str(dest), "size_kb": round(len(data) / 1024, 1)}


@mcp.tool(annotations=ACTION)
@reported
async def import_audio(file_path: str) -> dict[str, Any]:
    """Upload a local audiobook file (mp3, m4a, m4b, mp4, aa, aax, zip, pdf, ...) into the OpenAudible library."""
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise OpenAudibleError(f"No such file: {path}")
    result = await client.import_file(path)
    await client.library(refresh=True)
    return {"file": str(path), "imported": result}


def main(argv: list[str] | None = None) -> None:
    settings.apply_args(argv)
    mcp.run()


if __name__ == "__main__":
    main()
