"""Async HTTP client for the OpenAudible embedded web server.

Endpoints come from the server's own spec at /admin/openapi.json (v5.0.3).
The public /api/* routes work for any user; /admin/api/* routes need the Admin role.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, unquote

import httpx

from .config import Settings
from . import library

ProgressFn = Callable[[int, int | None], Awaitable[None]]

BOOK_ACTIONS = ("download", "convert", "pdf", "transcribe", "ignore", "unignore")
LIBRARY_TTL_SECONDS = 60


class OpenAudibleError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _seg(value: str) -> str:
    """Encode one path segment. Book ids can contain '/' (split-file books)."""
    return quote(value, safe="")


class OpenAudibleClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or Settings()
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._library: dict[str, Any] | None = None
        self._library_at = 0.0
        self._is_admin: bool | None = None

    # -- plumbing -------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            creds = self.settings.credentials()
            self._http = httpx.AsyncClient(
                base_url=self.settings.url,
                auth=httpx.BasicAuth(*creds) if creds else None,
                timeout=httpx.Timeout(30.0, read=300.0),
                transport=self._transport,
                follow_redirects=True,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self._client().request(method, path, **kw)
        except httpx.HTTPError as e:
            raise OpenAudibleError(
                f"Cannot reach OpenAudible at {self.settings.url}: {e.__class__.__name__}: {e}"
            ) from e
        if r.status_code >= 400:
            detail = r.text.strip()
            try:
                body = r.json()
                detail = body.get("title") or body.get("message") or body.get("error") or detail
            except ValueError:
                pass
            if r.status_code == 401:
                detail = (
                    "authentication failed (check OPENAUDIBLE_USERNAME / OPENAUDIBLE_PASSWORD)"
                    if self.settings.username
                    else "the server requires a login; set OPENAUDIBLE_USERNAME and OPENAUDIBLE_PASSWORD"
                )
            raise OpenAudibleError(f"{method} {path} -> HTTP {r.status_code}: {detail[:300]}", r.status_code)
        return r

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._request(method, path, **kw)
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    # -- library --------------------------------------------------------------

    async def library(self, refresh: bool = False) -> dict[str, Any]:
        """The full library: {'librarySettings': {...}, 'audiobooks': [...]}. Cached briefly."""
        if refresh or self._library is None or time.monotonic() - self._library_at > LIBRARY_TTL_SECONDS:
            self._library = await self._json("GET", "/api/library") or {}
            self._library_at = time.monotonic()
        return self._library or {}

    async def books(self, refresh: bool = False) -> list[dict[str, Any]]:
        return (await self.library(refresh)).get("audiobooks", [])

    async def book(self, book_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/books/{_seg(book_id)}")

    async def resolve_book(self, id_or_title: str) -> dict[str, Any]:
        """Find a book by exact id, ASIN, or an unambiguous title search."""
        books = await self.books()
        needle = id_or_title.strip()
        for b in books:
            if needle in (b.get("key"), b.get("asin"), b.get("product_id")):
                return b
        # Narrowest match first: exact title, then title phrase, then word search.
        # A split chapter-file copy shares its book's title, so prefer the single-file
        # book when that is the only ambiguity.
        def single(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
            if len(candidates) > 1:
                candidates = [b for b in candidates if not b.get("directory_type")] or candidates
            return candidates[0] if len(candidates) == 1 else None

        for level in ("exact", "phrase"):
            found = single([b for b in books if library.title_matches(b, needle) == level])
            if found:
                return found
        matches = library.search(books, query=needle)
        found = single(matches)
        if found:
            return found
        if not matches:
            raise OpenAudibleError(f"No book matches {needle!r}", 404)
        options = "; ".join(f"{b['key']} = {b.get('title')}" for b in matches[:8])
        raise OpenAudibleError(
            f"{len(matches)} books match {needle!r}; pass one of these ids: {options}", 409
        )

    async def chapters_flat(self, book_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/api/books/{_seg(book_id)}/chapters/flat")

    async def export(self, fmt: str, include_files: bool) -> bytes:
        params = {"format": fmt, "files": str(include_files).lower()}
        r = await self._request("GET", "/api/library/export", params=params)
        return r.content

    # -- files ----------------------------------------------------------------

    async def files(self, book: dict[str, Any]) -> list[dict[str, Any]]:
        """Every file of a book, normalized to {name, type, kind, size?, url, params?}.

        Uses the admin listing when allowed (it has sizes and the AAX source),
        otherwise the public file paths in the library record.
        """
        book_id = book["key"]
        if self._is_admin is not False:
            try:
                raw = await self._json("GET", f"/admin/api/books/{_seg(book_id)}/files")
                self._is_admin = True
                public = {f["name"]: f["url"] for f in self._public_files(book)}
                return [
                    {
                        "name": name,
                        "type": f.get("type"),
                        "kind": f.get("kind"),
                        "size": f.get("size") or None,
                        # The server still lists files deleted from disk, with size and mtime 0.
                        **({"missing": True} if not f.get("size") and not f.get("modified") else {}),
                        "url": f"/admin/api/books/{_seg(book_id)}/file",
                        "params": {"root": f["root"], "path": f["path"]},
                        # Split chapter-file books are listed here but only served publicly.
                        **({"fallback_url": public[name]} if name in public else {}),
                    }
                    for f in raw
                    for name in [f.get("name") or Path(f["path"]).name]
                ]
            except OpenAudibleError as e:
                if e.status not in (401, 403):
                    raise
                self._is_admin = False
        return self._public_files(book)

    @staticmethod
    def _public_files(book: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": unquote(f["path"].rsplit("/", 1)[-1]),
                "type": f.get("type"),
                "kind": f.get("kind"),
                "size": None,
                "url": "/" + f["path"].lstrip("/"),
            }
            for f in book.get("files", [])
            if f.get("type") != "THUMB"
        ]

    def public_url(self, file: dict[str, Any]) -> str:
        """An absolute URL for a file (needs HTTP Basic auth to open)."""
        url = self.settings.url + file["url"]
        if file.get("params"):
            url += "?" + "&".join(f"{k}={quote(v, safe='')}" for k, v in file["params"].items())
        return url

    async def download(self, file: dict[str, Any], dest: Path, progress: ProgressFn | None = None) -> int:
        """Stream one file to dest (via a .part file). Returns bytes written."""
        try:
            return await self._download(file["url"], file.get("params"), file, dest, progress)
        except OpenAudibleError as e:
            if e.status != 404:
                raise
            if file.get("fallback_url"):
                try:
                    return await self._download(file["fallback_url"], None, file, dest, progress)
                except OpenAudibleError as e2:
                    if e2.status != 404:
                        raise
            raise OpenAudibleError(
                f"The server lists {file['name']!r} but it is not on disk. Run the "
                "Refresh_Files command so OpenAudible rescans its folders.",
                404,
            ) from e

    async def _download(
        self,
        url: str,
        params: dict[str, str] | None,
        file: dict[str, Any],
        dest: Path,
        progress: ProgressFn | None,
    ) -> int:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        written = 0
        try:
            async with self._client().stream("GET", url, params=params) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise OpenAudibleError(f"GET {url} -> HTTP {r.status_code}", r.status_code)
                total = int(r.headers["content-length"]) if "content-length" in r.headers else file.get("size")
                last = 0.0
                with tmp.open("wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 20):
                        fh.write(chunk)
                        written += len(chunk)
                        if progress and time.monotonic() - last > 0.5:
                            last = time.monotonic()
                            await progress(written, total)
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise OpenAudibleError(f"Download of {file['name']} failed: {e}") from e
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)
        return written

    # -- admin: status and commands ------------------------------------------

    async def status(self) -> dict[str, Any]:
        return await self._json("GET", "/admin/api/status")

    async def commands(self) -> list[str]:
        return await self._json("GET", "/admin/api/commands")

    async def run_command(self, name: str) -> Any:
        return await self._json("POST", f"/admin/api/command/{_seg(name)}")

    async def book_action(self, book_id: str, action: str) -> Any:
        if action not in BOOK_ACTIONS:
            raise OpenAudibleError(f"Unknown action {action!r}; use one of {', '.join(BOOK_ACTIONS)}", 400)
        return await self._json("POST", f"/admin/api/books/{_seg(book_id)}/{action}")

    async def import_file(self, path: Path) -> Any:
        async def chunks():
            with path.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    yield chunk

        return await self._json(
            "POST",
            "/admin/api/import",
            params={"filename": path.name},
            content=chunks(),
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(path.stat().st_size),
            },
            timeout=httpx.Timeout(30.0, read=1800.0, write=600.0),
        )
