from openaudible_mcp import yoto

MIN = 60_000


def chapters(*minutes):
    out, start = [], 0
    for i, m in enumerate(minutes):
        out.append({"title": f"Chapter {i + 1}", "start_offset_ms": start, "length_ms": m * MIN})
        start += m * MIN
    return out, start


def test_spans_are_gap_free_and_cover_the_book():
    ch, total = chapters(10, 20, 30)
    ch[1]["length_ms"] -= 5000  # a gap before chapter 3 belongs to chapter 2
    spans = yoto.chapter_spans(ch, total)
    assert [s.length_ms for s in spans] == [10 * MIN, 20 * MIN, 30 * MIN]
    assert sum(s.length_ms for s in spans) == total


def test_long_chapters_are_divided_under_an_hour():
    ch, total = chapters(30, 150)
    p = yoto.plan(ch, total, max_kbps=96)
    titles = [t.title for t in p.tracks]
    assert titles == ["Chapter 1", "Chapter 2 (1 of 3)", "Chapter 2 (2 of 3)", "Chapter 2 (3 of 3)"]
    assert all(t.length_ms <= yoto.MAX_TRACK_MS for t in p.tracks)
    assert sum(t.length_ms for t in p.tracks) == total
    assert p.tracks[-1].start_ms + p.tracks[-1].length_ms == total


def test_short_book_keeps_max_bitrate_on_one_card():
    ch, total = chapters(*[20] * 9)
    p = yoto.plan(ch, total, max_kbps=96)
    assert p.kbps == 96 and len(p.cards) == 1


def test_long_book_lowers_bitrate_to_fit_one_card():
    ch, total = chapters(*[30] * 30)  # 15 h: ~650 MB at 96k, fits at 64k
    p = yoto.plan(ch, total, max_kbps=96)
    assert len(p.cards) == 1 and p.kbps <= 64
    size = sum(yoto.estimated_bytes(t.length_ms, p.kbps) for t in p.tracks)
    assert size <= yoto.MAX_CARD_BYTES


def test_more_than_100_chapters_spans_cards():
    ch, total = chapters(*[5] * 130)
    p = yoto.plan(ch, total, max_kbps=96)
    assert [len(c) for c in p.cards] == [100, 30]
    assert p.kbps == 96


def test_fixed_bitrate_and_no_chapters():
    p = yoto.plan([], 90 * MIN, kbps=128)
    assert p.kbps == 128 and [t.title for t in p.tracks] == ["Part (1 of 2)", "Part (2 of 2)"]


def test_parse_kbps():
    assert yoto.parse_kbps("64k") == 64
    assert yoto.parse_kbps("128kbps") == 128
    assert yoto.parse_kbps(96000) == 96
    assert yoto.parse_kbps("") is None and yoto.parse_kbps("fast") is None
