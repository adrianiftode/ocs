# ocs — opencode session toolbox

`ocs` is a small, dependency-free CLI for [opencode](https://opencode.ai)
sessions. It lets you search, inspect, relocate and rename them from any folder,
without having to remember where each session was started.

## The problem

opencode binds every session to the directory you launched it from, and its
session list only shows sessions for the current directory. So a session you
recorded in `~/code/app` is invisible when you later run opencode in
`~/code/other`. Over time your sessions get scattered across many folders, and
there is no built-in way to:

- search across **all** sessions,
- see which folder a session belongs to,
- move a session to the project it actually belongs to,
- give a session a meaningful title (opencode's `session` command has no rename),
- find sessions that still have a meaningless auto-generated title.

`ocs` reads opencode's SQLite database directly and solves all of the above.

## Requirements

- **Python 3.8 or newer** (standard library only — no dependencies).
- The `opencode` CLI on `PATH` **only for `ocs move`, `ocs export` and `ocs import`**; every
  other command talks to the database directly.

## Installation

Run it straight from a clone (no install):

```sh
git clone https://github.com/adrianiftode/ocs.git
cd ocs
python ocs.py --help
```

Or install the `ocs` command into your environment:

```sh
pipx install .        # or: pip install .
ocs --help
```

### Launchers

The repository ships three thin launchers next to `ocs.py`, so you can call
`ocs` without typing `python ocs.py` every time:

| Platform                     | File       | Call it with        |
| ---------------------------- | ---------- | ------------------- |
| Linux / macOS / Git Bash / WSL | `ocs`    | `./ocs <args>`      |
| Windows (cmd.exe)            | `ocs.cmd`  | `ocs <args>`        |
| Windows (PowerShell)         | `ocs.ps1`  | `.\ocs.ps1 <args>`  |

Each launcher locates `ocs.py` next to itself and forwards all arguments. Set
`$PYTHON` to force a specific interpreter.

## Finding the database

`ocs` resolves `opencode.db` in this order:

1. `--db PATH` (global flag),
2. the `OPENCODE_DB` environment variable,
3. the default `~/.local/share/opencode/opencode.db`,
4. `opencode db path` as a last resort.

Run `ocs db` to print the path that will be used:

```sh
ocs db
```

## Commands

| Command    | What it does |
| ---------- | ------------ |
| `db`       | Print the resolved database path. |
| `projects` | List projects/directories and their session counts. |
| `list`     | List sessions across **all** folders (newest first). |
| `search`   | Full-text search inside message text, across all sessions. |
| `show`     | Print a session transcript. |
| `context`  | Compact digest of a session, for AI title generation. |
| `untitled` | List sessions whose title is still a placeholder. |
| `stats`    | Database statistics. |
| `rename`   | Set session titles (single session or bulk from JSON). |
| `export`   | Export a session to JSON via the opencode CLI. |
| `move`     | Relocate a session to another project directory. |
| `backup`   | Make a consistent copy of `opencode.db`. |

Every command accepts the global `--db PATH` flag, and most accept `--json` for
machine-readable output.

### Inspecting

```sh
ocs projects                      # all folders + session counts
ocs list -n 20                    # 20 most recently updated sessions
ocs list --sort created           # order by creation time
ocs list --untitled               # only sessions with placeholder titles
ocs list -d ~/code/app --json     # one folder, as JSON
ocs list --nested                 # include subagent/nested sessions
```

`list` prints the session id, title, and the directory it belongs to.

### Searching

Search runs a SQL `LIKE` prefilter followed by an exact match in Python, so it
is fast even on large databases and still matches non-ASCII text (accented
characters, emoji).

```sh
ocs search "connection refused"                 # anywhere, any folder
ocs search "NullReferenceException" -d ~/code/app
ocs search "MaximumReadableBytes" --types all   # include reasoning text
ocs search "sub.?titr" --regex                  # regular expression
ocs search "warning" --case-sensitive
ocs search "TODO" -C 120 --max-hits 5 -n 50     # wider context, more hits
ocs search "migration" --role user --json       # only user messages, as JSON
```

Options: `-d/--dir`, `-p/--project`, `--nested`, `-r/--regex`,
`--case-sensitive`, `--types {text,reasoning,all}`, `--role {user,assistant,both}`,
`-C/--context` (chars of context around a hit), `--max-hits` (per session),
`-n/--limit` (number of sessions).

### Reading a session

```sh
ocs show ses_0123456789abcdef          # full transcript (text parts)
ocs show ses_0123456789abcdef -n 6     # only the last 6 messages
ocs show ses_0123456789abcdef --tools  # include tool calls + input/output
ocs show ses_0123456789abcdef --reasoning --role assistant
```

### Renaming

```sh
# one session
ocs rename ses_0123456789abcdef "Fix flaky integration test"

# preview without writing anything
ocs rename ses_0123456789abcdef "Fix flaky integration test" --dry-run

# bulk, from a JSON file
ocs rename --file titles.json
```

`--file` accepts any of these shapes:

```json
{ "ses_0123456789abcdef": "Fix flaky integration test" }
```
```json
[ { "id": "ses_0123456789abcdef", "title": "Fix flaky integration test" } ]
```

A `BOM` (as written by some Windows editors) is tolerated.

By default `rename` writes a fresh, consistent backup of `opencode.db` to your
temp directory first (pass `--no-backup` to skip, `--backup-dir DIR` to choose
where it goes).

> Restart opencode (or switch to another session and back) to see new titles —
> the TUI caches the session list.

### Finding and fixing untitled sessions

opencode sometimes leaves a session with a generic title (or none). List them,
grab a compact digest of each, then apply a title:

```sh
ocs untitled --json
ocs context ses_0123456789abcdef          # human-readable summary
ocs context ses_0123456789abcdef --json   # same, for an AI agent
ocs rename ses_0123456789abcdef "Add CSV export to reports"
```

### Relocating sessions

`ocs move` exports the session and re-imports it from the target folder.
opencode's importer upserts by session id and rewrites the project/directory
binding, so this **moves** the session (it does not duplicate it).

```sh
ocs move ses_0123456789abcdef --to ~/code/app
ocs move ses_0123456789abcdef --to ~/code/app --yes   # skip confirmation
```

The target directory must already exist. Restart opencode afterwards.

### Backups, export and import

```sh
ocs backup                        # -> <temp>/ocs-backups/opencode-<timestamp>.db
ocs backup -o ~/backups/opencode.db
ocs export ses_0123456789abcdef                 # -> ses_0123456789abcdef.json
ocs export ses_0123456789abcdef --stdout        # pipe it somewhere
ocs import exported-session.json                # import from current directory
ocs import exported-session.json --to ~/code/app
```

`backup` uses SQLite's online backup API, so it is safe while opencode is
running.

## Sharing a `.db` with someone else

`ocs` can also point at a copied database, which is handy for sharing an
anonymised or archived session set:

```sh
ocs --db ~/shared/opencode.db list
ocs --db ~/shared/opencode.db search "error"
```

## Notes on the database format

`ocs` reads two tables that hold JSON:

- `session` — one row per session (`id`, `title`, `directory`, `project_id`, …),
- `message` + `part` — the conversation; `part.data` is compact JSON such as
  `{"type":"text","text":"…"}`.

All read commands open the database **read-only** (`file:…?mode=ro`). Only
`rename` and `move` write, and `rename` backs up first.

## License

MIT © 2026 Adrian Iftode. See [LICENSE](LICENSE).
