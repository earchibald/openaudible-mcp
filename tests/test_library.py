from openaudible_mcp import library


def book(key, title, files=(), **kw):
    return {"key": key, "title": title, "files": list(files), **kw}


M4B = {"path": "api/books/a/files/A.m4b", "kind": "audio", "type": "M4B"}
ART = {"path": "api/books/a/files/A.jpg", "kind": "image", "type": "ART"}

BOOKS = [
    book("BK_1", "1-2-3 Magic - Effective Discipline", [ART], title_short="1-2-3 Magic", author="Thomas Phelan"),
    book("BK_2", "Ship of Magic", [ART], series_name="The Liveship Traders", series_sequence="1", author="Robin Hobb"),
    book("BK_3", "Magic Tree House #16: Olympics", [ART, M4B], author="Mary Pope Osborne", duration="01:02:03"),
    book("Split/", "Magic Tree House #16: Olympics", [M4B | {"type": "MP3"}], directory_type="audiobook"),
]


def test_duration_seconds():
    assert library.duration_seconds("07:01:00") == 25260
    assert library.duration_seconds("") == 0
    assert library.duration_seconds("bad") == 0


def test_tokens_match_word_prefixes_not_substrings():
    # '1' must not match '16' in the Magic Tree House title.
    hits = library.search(BOOKS, "1-2-3 magic")
    assert [b["key"] for b in hits] == ["BK_1"]


def test_relevance_prefers_title_hits():
    hits = library.search(BOOKS, "magic")
    assert hits[0]["key"] in {"BK_2", "BK_1", "BK_3"}
    assert len(hits) == 4


def test_filters():
    assert [b["key"] for b in library.search(BOOKS, status="ready")] == ["BK_3", "Split/"]
    assert [b["key"] for b in library.search(BOOKS, audio_format="mp3")] == ["Split/"]
    assert [b["key"] for b in library.search(BOOKS, series="liveship")] == ["BK_2"]
    assert [b["key"] for b in library.search(BOOKS, author="hobb")] == ["BK_2"]


def test_sort_duration():
    assert library.search(BOOKS, sort="duration")[0]["key"] == "BK_3"


def test_title_matches():
    assert library.title_matches(BOOKS[0], "1-2-3 magic") == "exact"  # title_short
    assert library.title_matches(BOOKS[0], "effective discipline") == "phrase"
    assert library.title_matches(BOOKS[0], "discipline effective") is None


def test_summarize():
    s = library.summarize(BOOKS[1])
    assert s["series"] == "The Liveship Traders #1"
    assert s["state"] == "no_audio"
    assert library.summarize(BOOKS[3])["split_into_chapter_files"] is True


def test_derive_state_and_missing_files():
    assert library.derive_state([{"type": "AAX"}]) == "downloaded_not_converted"
    assert library.derive_state([{"type": "ART"}]) == "not_downloaded"
    assert library.derive_state([{"type": "M4B"}]) == "ready"
    assert library.derive_state([{"type": "MP3", "missing": True}]) == "not_downloaded"


def test_pick_audio():
    files = [
        {"type": "MP3", "kind": "audio", "path": "b/02.mp3"},
        {"type": "MP3", "kind": "audio", "path": "b/01.mp3"},
        {"type": "M4B", "kind": "audio", "path": "b.m4b"},
        {"type": "AAX", "kind": "audio", "path": "b.aax"},
    ]
    assert library.pick_audio(files, "best")[0] == "M4B"
    kind, chosen = library.pick_audio(files, "mp3")
    assert kind == "MP3" and [f["path"] for f in chosen] == ["b/01.mp3", "b/02.mp3"]
    assert library.pick_audio(files, "m4a") is None
    assert library.pick_audio([{"type": "M4B", "kind": "audio", "missing": True}], "best") is None


def test_safe_filename():
    assert library.safe_filename("Gina: The Girl/Who") == "Gina - The Girl - Who"
    assert library.safe_filename("  ...  ") == "untitled"
