import httpx
import pytest
import respx

from openaudible_mcp.client import OpenAudibleClient, OpenAudibleError
from openaudible_mcp.config import Settings

BASE = "http://oa.test:8000"
SPLIT = {
    "key": "Hilo 4/",
    "title": "Hilo Book 4",
    "directory_type": "audiobook",
    "files": [{"path": "api/books/Hilo%204%2F/files/01%20Intro.mp3", "kind": "audio", "type": "MP3"}],
}
BK = {"key": "BK_1", "title": "Hilo Book 4", "files": []}
OTHER = {"key": "BK_2", "title": "Something Else", "files": []}


def make_client() -> OpenAudibleClient:
    s = Settings(url=BASE, username="u", password="p")
    return OpenAudibleClient(s)


@respx.mock
async def test_resolve_prefers_single_file_book_over_split_copy():
    respx.get(f"{BASE}/api/library").respond(json={"audiobooks": [SPLIT, BK, OTHER]})
    c = make_client()
    assert (await c.resolve_book("hilo book 4"))["key"] == "BK_1"
    assert (await c.resolve_book("Hilo 4/"))["key"] == "Hilo 4/"
    with pytest.raises(OpenAudibleError, match="No book matches"):
        await c.resolve_book("zzz")


@respx.mock
async def test_ids_with_slashes_are_encoded():
    route = respx.get(f"{BASE}/admin/api/books/Hilo%204%2F/files").respond(
        json=[{"path": "Hilo 4/01 Intro.mp3", "size": 0, "modified": 0, "kind": "audio", "root": "BOOKS", "name": "01 Intro.mp3", "type": "MP3"}]
    )
    files = await make_client().files(SPLIT)
    assert route.called
    assert files[0]["missing"] is True
    assert files[0]["fallback_url"] == "/api/books/Hilo%204%2F/files/01%20Intro.mp3"


@respx.mock
async def test_listener_role_falls_back_to_public_files():
    respx.get(f"{BASE}/admin/api/books/Hilo%204%2F/files").respond(403, json={"title": "Forbidden"})
    files = await make_client().files(SPLIT)
    assert files == [
        {"name": "01 Intro.mp3", "type": "MP3", "kind": "audio", "size": None, "url": "/api/books/Hilo%204%2F/files/01%20Intro.mp3"}
    ]


@respx.mock
async def test_download_writes_file_and_falls_back(tmp_path):
    respx.get(f"{BASE}/admin/api/books/BK_1/file").respond(404)
    respx.get(f"{BASE}/api/books/BK_1/files/a.m4b").respond(content=b"x" * 1000)
    f = {"name": "a.m4b", "url": "/admin/api/books/BK_1/file", "params": {"root": "BOOKS", "path": "a.m4b"},
         "fallback_url": "/api/books/BK_1/files/a.m4b"}
    dest = tmp_path / "out" / "a.m4b"
    assert await make_client().download(f, dest) == 1000
    assert dest.read_bytes() == b"x" * 1000
    assert not (tmp_path / "out" / "a.m4b.part").exists()


@respx.mock
async def test_missing_file_error_mentions_refresh(tmp_path):
    respx.get(f"{BASE}/admin/api/books/BK_1/file").respond(404)
    f = {"name": "a.m4b", "url": "/admin/api/books/BK_1/file", "params": {"root": "BOOKS", "path": "a.m4b"}}
    with pytest.raises(OpenAudibleError, match="Refresh_Files"):
        await make_client().download(f, tmp_path / "a.m4b")


@respx.mock
async def test_auth_error_message():
    respx.get(f"{BASE}/admin/api/status").respond(401, text="Unauthorized")
    with pytest.raises(OpenAudibleError, match="authentication failed"):
        await make_client().status()


@respx.mock
async def test_unreachable_server():
    respx.get(f"{BASE}/api/library").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(OpenAudibleError, match="Cannot reach"):
        await make_client().books()
