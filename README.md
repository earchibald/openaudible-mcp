# openaudible-mcp

An MCP server for a personal [OpenAudible](https://openaudible.org) library. Use it to
search your audiobooks, see what's ready, have the server download and convert new ones,
and pull M4B or MP3 audio onto this machine.

It talks only to OpenAudible's built-in web server (File → Preferences → Web Server). The
endpoints come from the server's own spec at `/admin/openapi.json` (OpenAudible 5.0.3).

## Tools

| Tool | What it does |
|---|---|
| `search_library` | Search by words, author, narrator or series. Filter by state (`ready` / `no_audio`) or format. Sort by relevance, title, dates or duration. |
| `get_book` | Metadata, state (`ready`, `downloaded_not_converted`, `not_downloaded`) and server files with sizes and streaming URLs. |
| `get_chapters` | Chapter titles with start times and lengths. |
| `download_audio` | Saves audio to `~/Downloads/OpenAudible/<title>/`. `format="mp3"` transcodes the server's M4B locally with ffmpeg, keeping chapters, tags and cover. `prepare_if_missing=true` has the server download and convert first, then waits. |
| `download_for_yoto` | Builds numbered MP3 tracks for a Yoto "Make Your Own" card in `<title>/Yoto/`, with `cover.jpg` for the card art. See [Yoto](#yoto). |
| `prepare_book` | Queues the server-side Audible download or conversion and returns at once. |
| `book_action` | Runs one raw action: `download`, `convert`, `pdf`, `transcribe`, `ignore` or `unignore`. |
| `server_status` | Library counters and active job queues. |
| `run_command` | `Sync_Quick`, `Sync_Full`, `Refresh_Files`, `Download_All`, `Convert_All`, `Cancel_All_Jobs`, `Check_For_Update`. |
| `export_library` | Saves the book list as csv, tsv, json or xlsx. |
| `import_audio` | Uploads a local audio file into the library. |

Every `book` argument takes a book id (`BK_TANT_006018`), an ASIN, or a title that matches
a single book.

Example prompts:
- "What Brandon Sanderson books do I have that are ready to listen to?"
- "Get me *Lola Goes to School* as an MP3."
- "Put *World War Z* on a Yoto card for me."
- "Sync my Audible library and tell me what's new."

## Yoto

`download_for_yoto` follows the rules of OpenAudible's own "Split for Yoto" mode (see the
[Yoto tutorial](https://openaudible.org/tutorials/yoto) and
[Split and Join](https://openaudible.org/tutorials/split-join)):

- Each chapter becomes one MP3 track, named `01 - <chapter>.mp3` so the files sort in order.
- A chapter of an hour or more is cut into equal parts, such as `Chapter 7 (1 of 2)`. Every
  track is under 60 minutes and 100 MB.
- A card holds at most 100 tracks and 500 MB. The bitrate is the highest one up to
  `OPENAUDIBLE_MP3_BITRATE` (96k by default) that keeps the book on the fewest cards.
  A 6-hour book fits on one card at 96k. A 55-hour collection needs 3 cards at 56k.
  Bitrates of 64k and below are encoded in mono, which sounds better for speech, and the
  Yoto speaker is mono anyway.
- A book that needs several cards gets folders named `Card 1 of 3`, `Card 2 of 3` and so on.
  Pass `bitrate="64k"` to force a bitrate.

OpenAudible's "Split for Yoto" is a desktop-only action that the web API can't trigger.
So the server sends its M4B, and the tracks are cut on this computer with ffmpeg. The
M4B is deleted afterwards, unless you were already keeping it.

To make the card, go to [my.yotoplay.com/my-cards](https://my.yotoplay.com/my-cards) and
choose **Create a playlist**. Upload one card folder's tracks in name order, then save.
Then in the Yoto app, open the playlist, tap **Link to a card**, and hold a blank card to
your phone.

## Configuration

| Setting | Variable | Flag | Default |
|---|---|---|---|
| Server URL | `OPENAUDIBLE_URL` | `--url` | `http://localhost:8000` |
| Username | `OPENAUDIBLE_USERNAME` | `--username` | none (no login) |
| Password | `OPENAUDIBLE_PASSWORD` | `--password`, `--password-file` | none |
| Download folder | `OPENAUDIBLE_DOWNLOAD_DIR` | `--download-dir` | `~/Downloads/OpenAudible` |
| MP3 / max Yoto bitrate | `OPENAUDIBLE_MP3_BITRATE` | `--mp3-bitrate` | `96k` |
| Settings file | `OPENAUDIBLE_ENV_FILE` | `--env-file` | `./.env` if it exists |

Flags win over environment variables, and environment variables win over the `.env`
file. Empty values count as unset. Copy `.env.example` to `.env`, which git ignores. Use
`--password-file` or a variable rather than `--password`, because other local users can
see command-line arguments.

The server URL is the one shown under File → Preferences → Web Server in OpenAudible. Use
an Admin account. A Listener account can still search and download, but it can't see file
sizes or AAX state, prepare books, or run commands.

## Install

Requires [uv](https://docs.astral.sh/uv/). MP3 and Yoto output also need `ffmpeg`
(`brew install ffmpeg`).

### Claude Code plugin (recommended)

The repo is a plugin marketplace with one plugin. Claude Code asks for the URL, username,
password, download folder and bitrate when you install it, and keeps the password in your
system keychain.

```
/plugin marketplace add /path/to/openaudible-mcp
/plugin install openaudible@openaudible-mcp
```

To change the settings later, run `/plugin configure openaudible@openaudible-mcp`. The
shell command `claude plugin install openaudible@openaudible-mcp` doesn't ask for
settings, so pass them as `--config url=http://my-mac.local:8000` and so on, or run
`/plugin configure` afterwards.

### Any MCP client

Run the server with `uv` from a checkout, or with `uvx` without one:

```sh
uv run --directory /path/to/openaudible-mcp openaudible-mcp            # reads /path/to/openaudible-mcp/.env
uvx --from /path/to/openaudible-mcp openaudible-mcp --url http://my-mac.local:8000
```

Claude Code without the plugin:

```sh
claude mcp add -s user openaudible \
  -e OPENAUDIBLE_URL=http://my-mac.local:8000 -e OPENAUDIBLE_USERNAME=admin -e OPENAUDIBLE_PASSWORD=... \
  -- uv run --directory /path/to/openaudible-mcp openaudible-mcp
```

Claude Desktop and similar clients:

```json
{
  "mcpServers": {
    "openaudible": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/openaudible-mcp", "openaudible-mcp"],
      "env": {
        "OPENAUDIBLE_URL": "http://my-mac.local:8000",
        "OPENAUDIBLE_USERNAME": "admin",
        "OPENAUDIBLE_PASSWORD": "..."
      }
    }
  }
}
```

## How it behaves

- The server converts to one format for the whole library (usually M4B), and the API
  can't override that per book. So MP3s and Yoto tracks are made on this machine.
- `prepare_book` and `prepare_if_missing` queue the download. If auto-convert is off on
  the server, the wait loop queues conversion after the download lands.
- OpenAudible keeps listing split chapter-file books after their folder is deleted. Such
  files show as `missing_on_disk`, and downloading them suggests `Refresh_Files`.
- Downloads write to a `.part` file first, then rename it. A file that already exists
  locally at the same size is skipped unless `overwrite=true`.

## Development

```sh
uv run pytest                     # offline unit tests
uv run python scripts/smoke.py    # live end-to-end run over MCP stdio, using ./.env
claude plugin validate .          # check the plugin and marketplace manifests
```

`scripts/smoke.py` only reads, apart from downloading the 9 MB built-in "Welcome to
OpenAudible" book into a temp folder as M4B, MP3 and Yoto tracks.
