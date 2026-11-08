"""Plan MP3 tracks for Yoto "Make Your Own" cards.

Limits, from OpenAudible's "Split for Yoto" mode and its Yoto tutorial: a card holds at
most 100 tracks and 500 MB, and each track must be under 60 minutes and 100 MB. Like
OpenAudible, each chapter becomes a track, and chapters that are too long are cut into
equal parts named "<chapter> (1 of 3)".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

MB = 1_048_576
MAX_TRACKS = 100
MAX_CARD_BYTES = 500 * MB
MAX_TRACK_BYTES = 100 * MB
# OpenAudible keeps a 30 s margin below Yoto's 60-minute track limit.
MAX_TRACK_MS = 60 * 60_000 - 30_000
MIN_CHAPTER_MS = 500
# Card fills to this fraction of 500 MB, leaving room for tags and rounding.
CARD_FILL = 0.97
BITRATES = (128, 112, 96, 80, 64, 56, 48, 40, 32)
MONO_AT_OR_BELOW = 64


@dataclass(frozen=True)
class Track:
    title: str
    start_ms: int
    length_ms: int


@dataclass(frozen=True)
class Plan:
    kbps: int
    cards: list[list[Track]]

    @property
    def tracks(self) -> list[Track]:
        return [t for card in self.cards for t in card]


def estimated_bytes(length_ms: int, kbps: int) -> int:
    """Size of a constant-bitrate MP3, with a little room for frame and tag overhead."""
    return int(length_ms / 1000 * kbps * 125 * 1.01) + 8192


def max_track_ms(kbps: int) -> int:
    by_size = int((MAX_TRACK_BYTES * 0.97 - 8192) / (kbps * 125 * 1.01) * 1000)
    return min(MAX_TRACK_MS, by_size)


def chapter_spans(chapters: Iterable[dict[str, Any]], total_ms: int) -> list[Track]:
    """Turn OpenAudible flat chapters into gap-free spans covering the whole book.

    Each chapter runs to the start of the next, so no audio between chapters is lost.
    Books without chapters become one span.
    """
    rows = sorted(
        ((int(c.get("start_offset_ms") or 0), str(c.get("title") or "").strip()) for c in chapters),
        key=lambda r: r[0],
    )
    if not rows or total_ms <= 0:
        return [Track("Part", 0, total_ms)] if total_ms > 0 else []
    if rows[0][0] > 0:
        rows[0] = (0, rows[0][1])
    spans: list[Track] = []
    for i, (start, title) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else total_ms
        end = min(end, total_ms)
        if end - start < MIN_CHAPTER_MS:
            continue
        spans.append(Track(title or f"Chapter {len(spans) + 1}", start, end - start))
    return spans


def split_long(spans: list[Track], limit_ms: int) -> list[Track]:
    out: list[Track] = []
    for s in spans:
        parts = max(1, math.ceil(s.length_ms / limit_ms))
        if parts == 1:
            out.append(s)
            continue
        step = math.ceil(s.length_ms / parts)
        for n in range(parts):
            start = s.start_ms + n * step
            length = min(step, s.start_ms + s.length_ms - start)
            out.append(Track(f"{s.title} ({n + 1} of {parts})", start, length))
    return out


def pack_cards(tracks: list[Track], kbps: int) -> list[list[Track]]:
    """Fill cards in order, starting a new one at 100 tracks or ~500 MB."""
    budget = MAX_CARD_BYTES * CARD_FILL
    cards: list[list[Track]] = [[]]
    used = 0
    for t in tracks:
        size = estimated_bytes(t.length_ms, kbps)
        if cards[-1] and (len(cards[-1]) >= MAX_TRACKS or used + size > budget):
            cards.append([])
            used = 0
        cards[-1].append(t)
        used += size
    return cards if cards[0] else []


def plan(
    chapters: Iterable[dict[str, Any]],
    total_ms: int,
    max_kbps: int = 96,
    kbps: int | None = None,
) -> Plan:
    """Choose a bitrate and lay tracks out on cards.

    With kbps fixed, use it. Otherwise take the highest bitrate up to max_kbps that
    still needs the fewest cards possible, so a book fits one card whenever it can.
    """
    spans = chapter_spans(list(chapters), total_ms)
    if kbps:
        return Plan(kbps, pack_cards(split_long(spans, max_track_ms(kbps)), kbps))
    ladder = [b for b in BITRATES if b <= max_kbps] or [BITRATES[-1]]
    options = [Plan(b, pack_cards(split_long(spans, max_track_ms(b)), b)) for b in ladder]
    fewest = min(len(p.cards) for p in options)
    return next(p for p in options if len(p.cards) == fewest)


def parse_kbps(bitrate: str | int | None) -> int | None:
    """'96k' / '96' / 96 -> 96."""
    if bitrate in (None, ""):
        return None
    text = str(bitrate).strip().lower().removesuffix("bps").removesuffix("k")
    try:
        value = int(float(text))
    except ValueError:
        return None
    return value // 1000 if value >= 1000 else value
