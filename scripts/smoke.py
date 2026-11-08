"""Live end-to-end check against a real OpenAudible server, over MCP stdio.

    uv run python scripts/smoke.py [DEST_DIR]

Read-only apart from downloading the small built-in 'Welcome to OpenAudible' book
(as M4B, as a locally transcoded MP3, and as Yoto tracks) into DEST_DIR (default: a
temp dir). Settings come from the environment or ./.env, as for the server.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

from mcp import Client, StdioServerParameters


def show(label: str, result) -> dict | None:
    text = "".join(getattr(c, "text", "") for c in result.content)
    status = "ERROR" if result.is_error else "ok"
    print(f"\n== {label} [{status}]")
    print(text[:1200])
    try:
        return json.loads(text)
    except ValueError:
        return None


async def main() -> int:
    dest = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="oa-smoke-")
    params = StdioServerParameters(
        command="uv", args=["run", "openaudible-mcp"], env={**os.environ}, cwd=os.path.dirname(os.path.dirname(__file__))
    )
    failures = 0

    async def progress(p: float, total: float | None, msg: str | None) -> None:
        print(f"   progress {p:.0f}/{total or '?'} {msg or ''}")

    async with Client(params, read_timeout_seconds=600) as c:
        tools = await c.list_tools()
        print("tools:", ", ".join(t.name for t in tools.tools))

        r = show("server_status", await c.call_tool("server_status", {}))
        failures += r is None
        r = show("search 'hilo'", await c.call_tool("search_library", {"query": "hilo", "limit": 3}))
        failures += not (r and r["total_matches"] > 0)
        r = show("search ready m4b", await c.call_tool("search_library", {"status": "ready", "audio_format": "m4b"}))
        failures += not (r and r["total_matches"] > 0)
        r = show("get_book by title", await c.call_tool("get_book", {"book": "1-2-3 Magic", "include_description": False}))
        failures += not (r and r["state"] == "not_downloaded")
        r = show("get_chapters", await c.call_tool("get_chapters", {"book": "BK_LILI_004883"}))
        failures += not (r and len(r["chapters"]) > 3)

        res = await c.call_tool("download_audio", {"book": "1-2-3 Magic", "dest_dir": dest})
        show("download not-downloaded book (expect error)", res)
        failures += not res.is_error

        res = await c.call_tool("get_book", {"book": "Hilo"})
        show("ambiguous title (expect error)", res)
        failures += not res.is_error

        r = show(
            "download welcome m4b",
            await c.call_tool("download_audio", {"book": "welcome", "dest_dir": dest}, progress_callback=progress),
        )
        failures += not (r and r["files"])
        r = show(
            "download welcome mp3 (transcode)",
            await c.call_tool(
                "download_audio", {"book": "welcome", "dest_dir": dest, "format": "mp3"}, progress_callback=progress
            ),
        )
        failures += not (r and any(f["type"] == "MP3" for f in r["files"]))
        r = show(
            "welcome for yoto",
            await c.call_tool(
                "download_for_yoto", {"book": "welcome", "dest_dir": dest, "overwrite": True}, progress_callback=progress
            ),
        )
        failures += not (r and r["card_count"] == 1 and r["cards"][0]["track_count"] == 9)
        r = show("search 'big secret'", await c.call_tool("search_library", {"query": "big secret"}))
        failures += not (r and r["total_matches"] >= 1)

        # Split chapter-file entries may be listed after their files were deleted.
        res = await c.call_tool(
            "download_audio", {"book": "Hilo Book 8- Gina and the Big Secret/", "dest_dir": dest}
        )
        show("split-file book (ok if present on disk, else Refresh_Files hint)", res)
        text = "".join(getattr(x, "text", "") for x in res.content)
        failures += res.is_error and "Refresh_Files" not in text

    print(f"\nfiles in {dest}")
    for root, _, names in os.walk(dest):
        for n in sorted(names):
            p = os.path.join(root, n)
            print(f"  {os.path.relpath(p, dest)}  {os.path.getsize(p) / 1_048_576:.1f} MB")
    print("\nFAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
