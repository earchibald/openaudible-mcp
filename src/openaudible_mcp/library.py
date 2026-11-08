"""Pure functions over OpenAudible book records (as served by /api/library)."""

from __future__ import annotations

import re
from typing import Any, Iterable, Literal

AUDIO_TYPES = ("M4B", "MP3", "M4A")
SOURCE_TYPES = ("AAX", "AAXC", "AA")

Status = Literal["any", "ready", "no_audio"]
SortKey = Literal["relevance", "title", "author", "purchase_date", "release_date", "duration"]


def duration_seconds(duration: str | None) -> int:
    """'HH:MM:SS' -> seconds. Returns 0 for anything unparseable."""
    if not duration:
        return 0
    try:
        parts = [int(p) for p in duration.split(":")]
    except ValueError:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def audio_files(book: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in book.get("files", []) if f.get("kind") == "audio"]


def audio_formats(book: dict[str, Any]) -> list[str]:
    return sorted({f["type"] for f in audio_files(book) if f.get("type") in AUDIO_TYPES})


def series_label(book: dict[str, Any]) -> str | None:
    name = book.get("series_name")
    if not name:
        return None
    seq = book.get("series_sequence")
    return f"{name} #{seq}" if seq else name


def summarize(book: dict[str, Any]) -> dict[str, Any]:
    """A compact view of a book for search results."""
    formats = audio_formats(book)
    out: dict[str, Any] = {
        "id": book["key"],
        "title": book.get("title"),
        "author": book.get("author"),
        "narrator": book.get("narrated_by"),
        "series": series_label(book),
        "duration": book.get("duration"),
        "purchase_date": book.get("purchase_date"),
        "audio_formats": formats,
        "audio_file_count": len(audio_files(book)),
        "state": "ready" if formats else "no_audio",
    }
    if book.get("directory_type"):
        # A folder of chapter files (e.g. a split MP3 book), listed separately
        # from the single-file book it was made from.
        out["split_into_chapter_files"] = True
    return {k: v for k, v in out.items() if v not in (None, "")}


_WORD = re.compile(r"\w+", re.UNICODE)


def _words(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _haystack(book: dict[str, Any]) -> str:
    fields = ("title", "title_short", "author", "narrated_by", "series_name", "asin", "key", "publisher")
    return " ".join(str(book.get(f) or "") for f in fields).casefold()


def _has_word_prefix(words: list[str], token: str) -> bool:
    return any(w.startswith(token) for w in words)


def _score(book: dict[str, Any], tokens: list[str]) -> int:
    """0 = no match. Every token must start some word of the searchable fields."""
    hay = _words(_haystack(book))
    if not all(_has_word_prefix(hay, t) for t in tokens):
        return 0
    title = _words(book.get("title") or "")
    phrase = " ".join(tokens)
    score = 1 + sum(2 for t in tokens if _has_word_prefix(title, t))
    if phrase in " ".join(title):
        score += 5
    if phrase in (" ".join(title), " ".join(_words(book.get("title_short") or ""))):
        score += 10
    return score


def title_matches(book: dict[str, Any], needle: str) -> Literal["exact", "phrase", None]:
    """How a free-text title relates to a book: exact title, or contained as a phrase."""
    want = " ".join(_words(needle))
    if not want:
        return None
    names = [" ".join(_words(book.get(f) or "")) for f in ("title", "title_short", "filename")]
    if want in names:
        return "exact"
    if any(f" {want} " in f" {n} " for n in names):
        return "phrase"
    return None


def search(
    books: Iterable[dict[str, Any]],
    query: str = "",
    author: str = "",
    narrator: str = "",
    series: str = "",
    status: Status = "any",
    audio_format: str = "",
    sort: SortKey = "relevance",
) -> list[dict[str, Any]]:
    """Filter and order raw book records. Returns the raw records."""
    tokens = _words(query)
    fmt = audio_format.upper()
    scored: list[tuple[int, dict[str, Any]]] = []
    for b in books:
        if author and author.casefold() not in (b.get("author") or "").casefold():
            continue
        if narrator and narrator.casefold() not in (b.get("narrated_by") or "").casefold():
            continue
        if series and series.casefold() not in (b.get("series_name") or "").casefold():
            continue
        formats = audio_formats(b)
        if status == "ready" and not formats:
            continue
        if status == "no_audio" and formats:
            continue
        if fmt and fmt not in formats:
            continue
        score = _score(b, tokens) if tokens else 1
        if score:
            scored.append((score, b))

    def title_key(b: dict[str, Any]) -> str:
        return (b.get("title") or "").casefold()

    if sort == "relevance":
        scored.sort(key=lambda sb: (-sb[0], title_key(sb[1])))
        return [b for _, b in scored]
    result = [b for _, b in scored]
    if sort == "title":
        result.sort(key=title_key)
    elif sort == "author":
        result.sort(key=lambda b: ((b.get("author") or "").casefold(), title_key(b)))
    elif sort == "duration":
        result.sort(key=lambda b: duration_seconds(b.get("duration")), reverse=True)
    elif sort in ("purchase_date", "release_date"):
        result.sort(key=lambda b: b.get(sort) or "", reverse=True)
    return result


def derive_state(files: list[dict[str, Any]]) -> str:
    """Book state from the admin file listing (which includes AAX sources)."""
    types = {f.get("type") for f in files if not f.get("missing")}
    if types & set(AUDIO_TYPES):
        return "ready"
    if types & set(SOURCE_TYPES):
        return "downloaded_not_converted"
    return "not_downloaded"


def pick_audio(files: list[dict[str, Any]], want: str) -> tuple[str, list[dict[str, Any]]] | None:
    """Choose which server audio files to fetch.

    want: 'best' (M4B, then MP3, then M4A), or a specific type.
    Returns (type, files sorted by path) or None.
    """
    by_type: dict[str, list[dict[str, Any]]] = {}
    for f in files:
        if f.get("kind") == "audio" and f.get("type") in AUDIO_TYPES and not f.get("missing"):
            by_type.setdefault(f["type"], []).append(f)
    order = AUDIO_TYPES if want == "best" else (want.upper(),)
    for t in order:
        if t in by_type:
            return t, sorted(by_type[t], key=lambda f: f.get("path") or f.get("name") or "")
    return None


_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(name: str, max_len: int = 150) -> str:
    cleaned = _UNSAFE.sub(" - ", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"( - )+", " - ", cleaned)
    return (cleaned[:max_len].rstrip(" .-")) or "untitled"
