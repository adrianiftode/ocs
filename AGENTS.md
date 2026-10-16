# AGENTS.md

Guidance for AI coding agents working in this repository, and for agents that
*use* the `ocs` CLI to tidy up opencode sessions.

## What this project is

`ocs` is a single-file, dependency-free Python CLI (`ocs.py`) that reads
opencode's SQLite database to search, inspect, export, relocate and rename
opencode sessions. Optional thin launchers (`ocs`, `ocs.cmd`, `ocs.ps1`) forward
to it.

## Repository layout

```
ocs.py          the entire CLI (standard library only)
ocs             POSIX launcher        (bash)
ocs.cmd         Windows launcher      (cmd.exe)
ocs.ps1         Windows launcher      (PowerShell)
pyproject.toml  packaging (console_script: ocs = ocs:main)
README.md       user documentation
LICENSE         MIT
```

Keep `ocs.py` dependency-free and Python 3.8 compatible. Do not introduce
third-party imports. Match the existing code style: type hints as strings
(`"str|None"`), `snake_case`, small focused functions, and the docstring on
every public function convention already used.

## Conventions

- **No hardcoded machine-specific paths.** The database path is always resolved
  via `resolve_db()` (`--db` → `$OPENCODE_DB` → default → `opencode db path`).
- **Read-only by default.** All read commands open the database with
  `file:…?mode=ro`. Only `rename` and `move` write, and `rename` makes a backup
  first. Preserve this.
- **Console is UTF-8.** `_utf8_console()` runs first in `main()` so message
  content with emoji, arrows or diacritics never crashes on Windows (cp1252).
- **JSON I/O tolerates a BOM** (`utf-8-sig`) — see `_load_title_map`.
- **`--json`** should be offered on any command that produces a list or a
  summary, and the JSON must be stable (keys are part of the interface).
- Bump `__version__` in `ocs.py` and `version` in `pyproject.toml` together.

## Database schema (read this before touching SQL)

opencode stores everything in `opencode.db` (WAL mode). Relevant tables:

| Table               | Key columns |
| ------------------- | ----------- |
| `project`           | `id`, `worktree`, `name` |
| `project_directory` | `project_id`, `directory` |
| `session`           | `id`, `project_id`, `workspace_id`, `parent_id`, `slug`, `directory`, `path`, `title`, `time_created`, `time_updated`, `time_archived` |
| `message`           | `id`, `session_id`, `time_created`, `data` (JSON, has `role`) |
| `part`              | `id`, `message_id`, `session_id`, `data` (JSON) |

- Timestamps are **milliseconds** since the epoch.
- `part.data` is **compact JSON**: one of `{"type":"text","text":…}`,
  `{"type":"reasoning","text":…}`, `{"type":"tool","tool":…,"state":{…}}`,
  `{"type":"step-start"}`, `{"type":"step-finish"}`. Text is the `text` field;
  reasoning is also `text`.
- A session is **nested** (a subagent) when `parent_id` is set. Default listings
  hide nested sessions and `--nested` shows them.
- A session is **untitled** when its title is empty or a placeholder
  (`is_placeholder_title`).

### How `move` works (important)

There is no rename-or-move SQL. `ocs move` runs `opencode export <id>`, then
`opencode import <file>` **from the target directory**. The importer upserts by
session id and overwrites `project_id` / `directory` / `path` with the current
directory's context — that is what makes it a *move* rather than a duplicate.
Never "move" by editing `session.directory` directly; opencode derived that
value and would recreate the old binding.

## Using `ocs` to (re)title sessions — agent workflow

When asked to give sessions meaningful titles:

1. **Back up first** (automatic on `rename`, but be aware):
   `ocs backup -o ~/opencode-backup.db` (or rely on the temp backup).
2. **Find the candidates**
   ```sh
   ocs untitled --json
   ```
   Returns `[{ "id", "title", "directory", ... }]`. Do **not** retitle nested
   sessions unless explicitly asked.
3. **Read a compact digest** of each candidate:
   ```sh
   ocs context <id> --json
   ```
   This includes the first user message, a numbered list of user requests,
   a preview of the last assistant answer, and the project directory.
4. **Propose a title.** Keep it short (≤ ~60 chars), specific, and in the
   **same language the user used** for that session (English for English
   threads, Romanian for Romanian ones). Prefer the shape
   `Area: action & outcome`, e.g. `Plex: fix missing movie metadata`,
   `fidelis-react: add CI + coverage`.
5. **Apply, then verify**
   ```sh
   ocs rename <id> "Chosen title" --dry-run   # preview
   ocs rename <id> "Chosen title"             # write (backs up automatically)
   ```

   For many sessions at once, write a JSON map and batch it:
   ```sh
   ocs rename --file titles.json              # {id: title} or [{id,title},...]
   ```
6. Remind the user that **opencode must be restarted** (or the session list
   refreshed) before the new titles appear — the TUI caches them.

### Safety rules for agents

- **Never** retitle or move nested/subagent sessions unless the user asked.
- **Never** delete the database or use `opencode session delete` on the user's
  behalf without explicit instruction.
- Prefer `--dry-run` and show the proposed titles before writing them.
- Titles are visible to the user in their session list: keep them accurate and
  free of speculation. Do not invent project names the session did not mention.

## Verifying changes

There is no test suite yet. Manual smoke test after editing `ocs.py`:

```sh
python ocs.py db
python ocs.py projects
python ocs.py list -n 5
python ocs.py list --untitled --json
python ocs.py search <term> -n 5
python ocs.py show <id> -n 3
python ocs.py context <id> --json
python ocs.py rename <id> "test title" --dry-run
python ocs.py --help
```

If you add a test suite, wire it into `pyproject.toml` and document the command
here.
