#!/usr/bin/env python3
"""ocs - opencode session toolbox.

Search, inspect, relocate and rename opencode sessions from any folder,
without having to remember which project directory a session was started in.

opencode binds every session to the directory it was launched from, and its
session list only shows sessions for the current directory. So a session you
recorded in one project is invisible when you launch opencode somewhere else.
``ocs`` reads opencode's SQLite database directly and lets you query *all*
sessions regardless of folder.

Run ``ocs --help`` for the command list, or read README.md.

This module is intentionally dependency-free (standard library only) and works
on Windows, macOS and Linux with Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

DEFAULT_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

# Titles that opencode (or a default) generated and that carry no meaning.
_PLACEHOLDER_EXACT = {"", "hello", "hi", "hey", "greeting", "test", "new session"}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def die(msg: str, code: int = 1) -> "None":
    print(f"ocs: error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _utf8_console() -> None:
    """Force UTF-8 output so message content (emoji, arrows, diacritics) survives."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def resolve_db(explicit: "str|None") -> Path:
    """Locate opencode's SQLite database.

    Order: ``--db`` flag, ``$OPENCODE_DB``, the well-known default path, then
    ``opencode db path`` as a last resort.
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            die(f"database not found: {p}")
        return p

    env = os.environ.get("OPENCODE_DB")
    if env:
        p = Path(env).expanduser()
        if not p.exists():
            die(f"$OPENCODE_DB does not exist: {p}")
        return p

    if DEFAULT_DB.exists():
        return DEFAULT_DB

    # Ask the opencode CLI where its database lives.
    exe = shutil.which("opencode")
    if exe:
        try:
            out = subprocess.run(
                _opencode_argv(exe, ["db", "path"]),
                capture_output=True,
                text=True,
                timeout=30,
            )
            candidate = (out.stdout or "").strip().splitlines()
            if candidate:
                p = Path(candidate[-1]).expanduser()
                if p.exists():
                    return p
        except Exception:
            pass

    die(
        "could not find opencode.db. Pass --db PATH or set $OPENCODE_DB.\n"
        f"  looked for: {DEFAULT_DB}"
    )
    raise AssertionError("unreachable")


def connect(db: Path, ro: bool = True) -> sqlite3.Connection:
    if ro:
        uri = f"file:{db.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    else:
        con = sqlite3.connect(str(db), timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def jloads(text: "str|None") -> dict:
    if not text:
        return {}
    try:
        val = json.loads(text)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def fmt_ms(ms: "int|float|None") -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def one_line(text: str) -> str:
    return " ".join((text or "").split())


def strip_reminders(text: str) -> str:
    """Drop ``<system-reminder>...</system-reminder>`` noise from a message."""
    cleaned = re.sub(r"<system-reminder>.*?</system-reminder>", "", text or "", flags=re.S)
    return cleaned.strip()


def truncate(text: str, width: int) -> str:
    text = text or ""
    if width <= 0 or len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "\u2026"


def norm_dir(p: "str|Path|None") -> str:
    if not p:
        return ""
    s = str(p).replace("\\", "/").rstrip("/")
    return s.lower()


def dir_matches(directory: str, target: str) -> bool:
    d, t = norm_dir(directory), norm_dir(target)
    if not d or not t:
        return False
    return d == t or d.startswith(t + "/")


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def is_placeholder_title(title: "str|None") -> bool:
    t = one_line(title or "")
    if t.lower() in _PLACEHOLDER_EXACT:
        return True
    return bool(re.match(r"^new session\b", t, re.I))


def print_table(headers, rows, right=()):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line.rstrip())
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            cells.append(c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
        print("  ".join(cells).rstrip())


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #


def all_projects(con: sqlite3.Connection) -> dict:
    out = {}
    for r in con.execute("select id, worktree, name from project"):
        out[r["id"]] = {"id": r["id"], "worktree": r["worktree"], "name": r["name"]}
    return out


def session_rows(
    con: sqlite3.Connection,
    directory: "str|None" = None,
    project: "str|None" = None,
    nested: bool = False,
    limit: "int|None" = None,
    order: str = "updated",
) -> list:
    """Return session rows (sqlite3.Row) as plain dicts, newest first."""
    where, params = [], []
    if not nested:
        where.append("s.parent_id is null")
    if project:
        where.append("s.project_id = ?")
        params.append(project)
    sql = (
        "select s.id, s.project_id, s.parent_id, s.directory, s.path, s.title, s.slug, "
        "s.time_created, s.time_updated, s.time_archived, p.worktree "
        "from session s left join project p on p.id = s.project_id"
    )
    if where:
        sql += " where " + " and ".join(where)
    col = "s.time_created" if order == "created" else "s.time_updated"
    sql += f" order by {col} desc"

    out = []
    for r in con.execute(sql, params):
        d = dict(r)
        if directory and not dir_matches(d["directory"], directory):
            continue
        out.append(d)
        if limit and len(out) >= limit:
            break
    return out


def message_rows(con: sqlite3.Connection, session_id: str) -> list:
    """Yield parsed messages for a session in chronological order."""
    out = []
    for r in con.execute(
        "select id, time_created, data from message where session_id=? order by time_created, id",
        (session_id,),
    ):
        md = jloads(r["data"])
        md["_id"] = r["id"]
        if not md.get("time"):
            md["time"] = {"created": r["time_created"]}
        out.append(md)
    return out


def part_rows(con: sqlite3.Connection, message_id: str) -> list:
    out = []
    for r in con.execute(
        "select id, time_created, data from part where message_id=? order by time_created, id",
        (message_id,),
    ):
        pd = jloads(r["data"])
        pd["_id"] = r["id"]
        out.append(pd)
    return out


def role_of(message: dict) -> str:
    return message.get("role") or "?"


def text_parts(parts: list, include_reasoning: bool = False) -> list:
    out = []
    for p in parts:
        t = p.get("type")
        if t == "text":
            out.append(p)
        elif t == "reasoning" and include_reasoning:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# opencode CLI bridge (used by `export` and `move`)
# --------------------------------------------------------------------------- #


def _opencode_argv(exe: str, args: list) -> list:
    """Wrap the opencode executable so it can be spawned on any platform."""
    low = exe.lower()
    if low.endswith(".ps1"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            exe,
            *args,
        ]
    if low.endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", exe, *args]
    return [exe, *args]


def require_opencode() -> str:
    exe = shutil.which("opencode")
    if not exe:
        die(
            "the 'opencode' executable was not found on PATH. "
            "`export` and `move` need it; other commands read the database directly."
        )
    return exe


def opencode_export_bytes(exe: str, session_id: str) -> bytes:
    res = subprocess.run(
        _opencode_argv(exe, ["export", session_id]),
        capture_output=True,
    )
    if res.returncode != 0 or not res.stdout.strip():
        err = (res.stderr or b"").decode("utf-8", "replace").strip()
        die(f"`opencode export {session_id}` failed: {err or res.returncode}")
    return res.stdout


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_db(args, db: Path, con):
    print(str(db))


def cmd_projects(args, db: Path, con):
    projects = all_projects(con)
    counts = {}
    for r in con.execute(
        "select project_id, count(*) c from session where parent_id is null group by project_id"
    ):
        counts[r["project_id"]] = r["c"]
    rows = []
    for pid, p in sorted(projects.items(), key=lambda kv: norm_dir(kv[1]["worktree"])):
        rows.append([pid, counts.get(pid, 0), p["worktree"]])
    if args.json:
        print(json.dumps(rows and [
            {"id": r[0], "sessions": r[1], "worktree": r[2]} for r in rows
        ], indent=2, ensure_ascii=False))
        return
    print_table(["PROJECT ID", "SESSIONS", "WORKTREE"], rows, right={1})


def cmd_list(args, db: Path, con):
    rows = session_rows(
        con,
        directory=args.dir,
        project=args.project,
        nested=args.nested,
        limit=args.limit,
        order=args.sort,
    )
    if args.untitled:
        rows = [r for r in rows if is_placeholder_title(r["title"])]

    if args.json:
        out = [
            {
                "id": r["id"],
                "title": r["title"],
                "directory": r["directory"],
                "project": r["worktree"],
                "project_id": r["project_id"],
                "created": fmt_ms(r["time_created"]),
                "updated": fmt_ms(r["time_updated"]),
                "nested": bool(r["parent_id"]),
                "untitled": is_placeholder_title(r["title"]),
            }
            for r in rows
        ]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    if not rows:
        print("(no sessions)")
        return

    table = []
    for r in rows:
        table.append(
            [
                fmt_ms(r["time_updated"]),
                r["id"],
                truncate(one_line(r["title"]), args.width),
                r["directory"] or r["worktree"] or "",
            ]
        )
    print_table(["UPDATED", "SESSION ID", "TITLE", "DIRECTORY"], table)


def _snippet(text: str, query: str, ctx: int, regex: bool, flags: int):
    """Return (snippet, matched_bool) around the first match of query in text."""
    if regex:
        m = re.search(query, text, flags)
        if not m:
            return None, False
        start, end = m.start(), m.end()
    else:
        if flags & re.I:
            idx = text.lower().find(query.lower())
        else:
            idx = text.find(query)
        if idx < 0:
            return None, False
        start, end = idx, idx + len(query)
    lo = max(0, start - ctx)
    hi = min(len(text), end + ctx)
    piece = one_line(text[lo:hi])
    prefix = "\u2026" if lo > 0 else ""
    suffix = "\u2026" if hi < len(text) else ""
    return f"{prefix}{piece}{suffix}", True


def cmd_search(args, db: Path, con):
    query = args.query
    flags = 0 if args.case_sensitive else re.I
    regex = args.regex
    if regex:
        try:
            re.compile(query, flags)
        except re.error as e:
            die(f"invalid regex: {e}")

    wanted_types = {"text"}
    if args.types == "reasoning":
        wanted_types = {"reasoning"}
    elif args.types == "all":
        wanted_types = {"text", "reasoning"}

    wanted_roles = (
        {"user", "assistant"} if args.role == "both" else {args.role}
    )

    # Restrict to a set of sessions when filtering; otherwise all top-level.
    allowed = None
    if args.dir or args.project or not args.nested:
        allowed = {
            r["id"]
            for r in session_rows(
                con,
                directory=args.dir,
                project=args.project,
                nested=args.nested,
                order="updated",
            )
        }
        if not allowed:
            print("(no matching sessions)")
            return

    # Prefer a cheap SQL LIKE prefilter; fall back to a type-scoped scan.
    sql = (
        "select pa.session_id as sid, pa.message_id as mid, pa.data as pdata, "
        "m.data as mdata from part pa join message m on m.id = pa.message_id "
    )
    params: list = []
    if not regex and is_ascii(query):
        sql += "where pa.data like ? escape '\\'"
        params.append("%" + escape_like(query) + "%")
    else:
        sql += "where pa.data like ?"
        params.append('%"type":"text"%')

    hits_by_session: dict = {}
    role_cache: dict = {}
    total_hits = 0

    for row in con.execute(sql, params):
        sid, mid = row["sid"], row["mid"]
        if allowed is not None and sid not in allowed:
            continue
        part = jloads(row["pdata"])
        if part.get("type") not in wanted_types:
            continue
        text = part.get("text") or ""
        if not text:
            continue
        if mid not in role_cache:
            role_cache[mid] = role_of(jloads(row["mdata"]))
        role = role_cache[mid]
        if role not in wanted_roles:
            continue
        snip, ok = _snippet(text, query, args.context, regex, flags)
        if not ok:
            continue
        bucket = hits_by_session.setdefault(sid, [])
        if len(bucket) >= args.max_hits:
            continue
        bucket.append({"role": role, "match": snip, "message_id": mid})
        total_hits += 1

    # Resolve session metadata for the sessions that matched.
    meta = {}
    if hits_by_session:
        qmarks = ",".join("?" * len(hits_by_session))
        for r in con.execute(
            f"select s.id, s.title, s.directory, s.time_updated, p.worktree "
            f"from session s left join project p on p.id = s.project_id "
            f"where s.id in ({qmarks})",
            list(hits_by_session),
        ):
            meta[r["id"]] = dict(r)

    ordered = sorted(
        hits_by_session,
        key=lambda s: meta.get(s, {}).get("time_updated") or 0,
        reverse=True,
    )[: args.limit]

    if args.json:
        out = []
        for sid in ordered:
            m = meta.get(sid, {})
            out.append(
                {
                    "id": sid,
                    "title": m.get("title"),
                    "directory": m.get("directory") or m.get("worktree"),
                    "hits": hits_by_session[sid],
                }
            )
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    if not ordered:
        print(f"no matches for {query!r}")
        return

    for sid in ordered:
        m = meta.get(sid, {})
        print(f"\n\033[1m{sid}\033[0m  {one_line(m.get('title') or '')}")
        print(f"  {m.get('directory') or m.get('worktree') or ''}")
        for h in hits_by_session[sid]:
            print(f"  [{h['role']}] {h['match']}")
    print(f"\n{total_hits} hit(s) in {len(ordered)} session(s)")


def cmd_show(args, db: Path, con):
    sess = con.execute(
        "select id, title, directory, time_created, time_updated from session where id=?",
        (args.session,),
    ).fetchone()
    if not sess:
        die(f"no such session: {args.session}")
    messages = message_rows(con, args.session)
    if args.limit and args.limit > 0:
        messages = messages[-args.limit :]

    print(f"# {args.session}")
    print(f"title:     {sess['title']}")
    print(f"directory: {sess['directory']}")
    print(f"created:   {fmt_ms(sess['time_created'])}")
    print(f"updated:   {fmt_ms(sess['time_updated'])}")
    print(f"messages:  {len(messages)}")
    print()

    for msg in messages:
        role = role_of(msg)
        if args.role != "both" and role != args.role:
            continue
        created = (msg.get("time") or {}).get("created")

        lines = []
        for p in part_rows(con, msg["_id"]):
            t = p.get("type")
            if t == "text":
                body = (p.get("text") or "").rstrip()
                if body:
                    lines.append(body)
            elif t == "reasoning" and args.reasoning:
                body = one_line(p.get("text", ""))
                if body:
                    lines.append(f"[reasoning] {body}")
            elif t == "tool" and args.tools:
                name = p.get("tool", "?")
                entry = [f"[tool: {name}]"]
                state = p.get("state") or {}
                inp = state.get("input")
                if inp is not None:
                    entry.append(f"  input: {one_line(json.dumps(inp, ensure_ascii=False))[:500]}")
                out = state.get("output")
                if out is not None:
                    entry.append(f"  output: {one_line(str(out))[:500]}")
                lines.append("\n".join(entry))

        if not lines:
            continue
        print(f"\033[1m--- {role} ({fmt_ms(created)}) ---\033[0m")
        print("\n\n".join(lines))
        print()


def cmd_context(args, db: Path, con):
    """Compact digest of a session, meant for an AI agent generating a title."""
    sess = con.execute(
        "select id, title, directory, project_id, time_created, time_updated from session where id=?",
        (args.session,),
    ).fetchone()
    if not sess:
        die(f"no such session: {args.session}")
    messages = message_rows(con, args.session)

    user_msgs, assistant_msgs = [], []
    for msg in messages:
        parts = part_rows(con, msg["_id"])
        texts = [p.get("text", "") for p in text_parts(parts)]
        joined = "\n".join(t for t in texts if t)
        if not joined:
            continue
        if role_of(msg) == "user":
            user_msgs.append(strip_reminders(joined) or joined)
        elif role_of(msg) == "assistant":
            assistant_msgs.append(joined)

    first_user = next((u for u in user_msgs if u.strip()), "")
    last_assistant = assistant_msgs[-1] if assistant_msgs else ""

    if args.json:
        print(
            json.dumps(
                {
                    "id": sess["id"],
                    "title": sess["title"],
                    "directory": sess["directory"],
                    "created": fmt_ms(sess["time_created"]),
                    "updated": fmt_ms(sess["time_updated"]),
                    "messages": len(messages),
                    "user_messages": len(user_msgs),
                    "assistant_messages": len(assistant_msgs),
                    "first_user_message": truncate(first_user, args.width),
                    "user_requests": [
                        truncate(u, args.width) for u in user_msgs[: args.max_users]
                    ],
                    "last_assistant_message": truncate(last_assistant, args.answer),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    print(f"id:        {sess['id']}")
    print(f"title:     {sess['title']}")
    print(f"directory: {sess['directory']}")
    print(f"created:   {fmt_ms(sess['time_created'])}")
    print(f"updated:   {fmt_ms(sess['time_updated'])}")
    print(f"messages:  {len(messages)} ({len(user_msgs)} user / {len(assistant_msgs)} assistant)")
    print("\n--- FIRST USER MESSAGE ---")
    print(truncate(first_user, args.width))
    if user_msgs:
        print("\n--- USER REQUESTS ---")
        for i, u in enumerate(user_msgs[: args.max_users], 1):
            print(f"{i}. {truncate(one_line(u), args.width)}")
    print("\n--- LAST ASSISTANT ANSWER ---")
    print(truncate(one_line(last_assistant), args.answer))


def cmd_untitled(args, db: Path, con):
    rows = session_rows(con, directory=args.dir, project=args.project,
                        nested=args.nested, limit=args.limit)
    rows = [r for r in rows if is_placeholder_title(r["title"])]
    if args.json:
        print(json.dumps([
            {
                "id": r["id"],
                "title": r["title"],
                "directory": r["directory"],
                "created": fmt_ms(r["time_created"]),
                "updated": fmt_ms(r["time_updated"]),
            }
            for r in rows
        ], indent=2, ensure_ascii=False))
        return
    if not rows:
        print("(every session already has a meaningful title)")
        return
    print_table(
        ["UPDATED", "SESSION ID", "TITLE", "DIRECTORY"],
        [[fmt_ms(r["time_updated"]), r["id"], truncate(one_line(r["title"]), args.width),
          r["directory"] or r["worktree"] or ""] for r in rows],
    )


def cmd_stats(args, db: Path, con):
    stats = {
        "database": str(db),
        "projects": con.execute("select count(*) from project").fetchone()[0],
        "sessions": con.execute("select count(*) from session").fetchone()[0],
        "sessions_top_level": con.execute(
            "select count(*) from session where parent_id is null"
        ).fetchone()[0],
        "sessions_untitled": sum(
            1
            for (t,) in con.execute("select title from session where parent_id is null")
            if is_placeholder_title(t)
        ),
        "messages": con.execute("select count(*) from message").fetchone()[0],
        "parts": con.execute("select count(*) from part").fetchone()[0],
    }
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return
    for k, v in stats.items():
        print(f"{k:>20}: {v}")


def backup_db(db: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(db))
    out = sqlite3.connect(str(dest))
    try:
        with out:
            src.backup(out)
    finally:
        src.close()
        out.close()
    return dest


def cmd_backup(args, db: Path, con):
    dest = Path(args.out).expanduser() if args.out else (
        Path(tempfile.gettempdir()) / "ocs-backups" / f"opencode-{now_stamp()}.db"
    )
    backup_db(db, dest)
    print(f"backup written: {dest}")


def _load_title_map(source: str) -> dict:
    """Accept either {id: title} or [{"id":..,"title":..}] or [{"session_id":..,"title":..}]."""
    # utf-8-sig tolerates the BOM that Windows editors/PowerShell add.
    data = json.loads(Path(source).read_text(encoding="utf-8-sig"))
    mapping = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, str):
                mapping[k] = v
            elif isinstance(v, dict):
                mapping[k] = v.get("title") or v.get("new_title")
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            sid = item.get("id") or item.get("session_id") or item.get("sessionID")
            title = item.get("title") or item.get("new_title")
            if sid and title:
                mapping[sid] = title
    mapping = {k: v for k, v in mapping.items() if v}
    if not mapping:
        die(f"no id->title pairs found in {source}")
    return mapping


def cmd_rename(args, db: Path, con):
    # Build the id -> title mapping.
    if args.file:
        mapping = _load_title_map(args.file)
    elif args.session and args.title is not None:
        mapping = {args.session: args.title}
    else:
        die("provide either SESSION_ID TITLE or --file titles.json")

    if args.dry_run:
        print(f"would rename {len(mapping)} session(s):")
        for sid, title in mapping.items():
            row = con.execute("select title from session where id=?", (sid,)).fetchone()
            old = row["title"] if row else "<not found>"
            print(f"  {sid}\n    old: {old}\n    new: {title}")
        return

    rw = connect(db, ro=False)
    if not args.no_backup:
        dest = Path(args.backup_dir).expanduser() / f"opencode-{now_stamp()}.db" \
            if args.backup_dir else Path(tempfile.gettempdir()) / "ocs-backups" / f"opencode-{now_stamp()}.db"
        backup_db(db, dest)
        print(f"backup written: {dest}")

    updated, missing = 0, []
    try:
        cur = rw.cursor()
        for sid, title in mapping.items():
            cur.execute("UPDATE session SET title=? WHERE id=?", (title, sid))
            if cur.rowcount == 0:
                missing.append(sid)
            else:
                updated += 1
        rw.commit()
    finally:
        rw.close()

    print(f"renamed {updated}/{len(mapping)} session(s)")
    if missing:
        print("not found:")
        for sid in missing:
            print(f"  {sid}")
    if updated:
        print("restart opencode (or switch sessions) to see the new titles.")


def cmd_export(args, db: Path, con):
    exe = require_opencode()
    raw = opencode_export_bytes(exe, args.session)
    if args.stdout:
        sys.stdout.buffer.write(raw)
        return
    dest = Path(args.out).expanduser() if args.out else Path(f"{args.session}.json")
    dest.write_bytes(raw)
    print(f"exported {args.session} -> {dest} ({len(raw)} bytes)")


def cmd_import(args, db: Path, con):
    exe = require_opencode()
    src = Path(args.file).expanduser()
    if not src.exists():
        die(f"file not found: {src}")
    target = Path(args.to).expanduser() if getattr(args, "to", None) else Path.cwd()
    if not target.exists():
        die(f"target directory does not exist: {target}")
    res = subprocess.run(
        _opencode_argv(exe, ["import", str(src)]),
        cwd=str(target),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        die(f"`opencode import` failed: {(res.stderr or '').strip() or res.returncode}")
    out = (res.stdout or "").strip()
    if out:
        print(out)
    print("restart opencode to refresh its session list.")


def cmd_move(args, db: Path, con):
    exe = require_opencode()
    target = Path(args.to).expanduser()
    if not target.exists():
        die(f"target directory does not exist: {target}")

    sess = con.execute(
        "select id, directory, title from session where id=?", (args.session,)
    ).fetchone()
    if not sess:
        die(f"no such session: {args.session}")

    print(f"moving {args.session}")
    print(f"  from: {sess['directory']}")
    print(f"  to:   {target.resolve()}")

    if not args.yes:
        reply = input("proceed? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("aborted.")
            return

    raw = opencode_export_bytes(exe, args.session)
    tmp = Path(tempfile.gettempdir()) / f"ocs-{args.session}.json"
    tmp.write_bytes(raw)
    try:
        res = subprocess.run(
            _opencode_argv(exe, ["import", str(tmp)]),
            cwd=str(target),
            capture_output=True,
            text=True,
        )
    finally:
        if not args.keep:
            tmp.unlink(missing_ok=True)

    if res.returncode != 0:
        die(f"`opencode import` failed: {(res.stderr or '').strip() or res.returncode}")
    print((res.stdout or "").strip())

    # Verify against the database.
    ver = sqlite3.connect(str(db))
    ver.row_factory = sqlite3.Row
    try:
        row = ver.execute(
            "select directory, project_id from session where id=?", (args.session,)
        ).fetchone()
    finally:
        ver.close()
    if row:
        print(f"  now:  {row['directory']}  (project {row['project_id']})")
    print("restart opencode to refresh its session list.")


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ocs",
        description="Search, inspect, relocate and rename opencode sessions from any folder.",
        epilog=(
            "examples:\n"
            "  ocs search 'connection refused'          search every session, every folder\n"
            "  ocs list -n 20                           recent sessions across all folders\n"
            "  ocs show <session-id>                    print a transcript\n"
            "  ocs context <session-id> --json          digest for an AI agent\n"
            "  ocs move <session-id> --to ~/code/app    relocate a session to a project\n"
            "  ocs rename <session-id> 'Fix login bug'  give a session a meaningful title\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"ocs {__version__}")
    p.add_argument("--db", help="path to opencode.db (default: auto-detected)")
    sub = p.add_subparsers(dest="command", required=True)

    # db -----------------------------------------------------------------
    sd = sub.add_parser("db", help="print the resolved database path")
    sd.set_defaults(func=cmd_db)

    # projects -----------------------------------------------------------
    sp = sub.add_parser("projects", help="list projects/directories and their session counts")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_projects)

    # list ---------------------------------------------------------------
    sl = sub.add_parser("list", help="list sessions across all folders (newest first)")
    sl.add_argument("-d", "--dir", help="only sessions under this directory")
    sl.add_argument("-p", "--project", help="only sessions with this project id")
    sl.add_argument("-n", "--limit", type=int, default=0, help="max sessions (0 = all)")
    sl.add_argument("--nested", action="store_true", help="include subagent/nested sessions")
    sl.add_argument("--untitled", action="store_true", help="only sessions with placeholder titles")
    sl.add_argument("--sort", choices=["updated", "created"], default="updated")
    sl.add_argument("--width", type=int, default=56, help="title column width")
    sl.add_argument("--json", action="store_true")
    sl.set_defaults(func=cmd_list)

    # search -------------------------------------------------------------
    ss = sub.add_parser("search", help="full-text search inside message text")
    ss.add_argument("query")
    ss.add_argument("-d", "--dir", help="only sessions under this directory")
    ss.add_argument("-p", "--project", help="only sessions with this project id")
    ss.add_argument("--nested", action="store_true", help="include subagent/nested sessions")
    ss.add_argument("-r", "--regex", action="store_true", help="treat query as a regular expression")
    ss.add_argument("--case-sensitive", action="store_true")
    ss.add_argument("--types", choices=["text", "reasoning", "all"], default="text")
    ss.add_argument("--role", choices=["user", "assistant", "both"], default="both")
    ss.add_argument("-C", "--context", type=int, default=70, help="characters of context around a hit")
    ss.add_argument("--max-hits", type=int, default=3, help="max hits shown per session")
    ss.add_argument("-n", "--limit", type=int, default=20, help="max sessions to show")
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_search)

    # show ---------------------------------------------------------------
    sh = sub.add_parser("show", help="print a session transcript")
    sh.add_argument("session")
    sh.add_argument("-n", "--limit", type=int, default=0, help="only the last N messages")
    sh.add_argument("--role", choices=["user", "assistant", "both"], default="both")
    sh.add_argument("--reasoning", action="store_true", help="include reasoning parts")
    sh.add_argument("--tools", action="store_true", help="include tool calls")
    sh.set_defaults(func=cmd_show)

    # context ------------------------------------------------------------
    sc = sub.add_parser("context", help="compact digest of a session (for AI title generation)")
    sc.add_argument("session")
    sc.add_argument("--max-users", type=int, default=8)
    sc.add_argument("--width", type=int, default=240, help="chars per user message")
    sc.add_argument("--answer", type=int, default=1200, help="chars of the last answer")
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_context)

    # untitled -----------------------------------------------------------
    su = sub.add_parser("untitled", help="list sessions whose title is still a placeholder")
    su.add_argument("-d", "--dir")
    su.add_argument("-p", "--project")
    su.add_argument("-n", "--limit", type=int, default=0)
    su.add_argument("--nested", action="store_true")
    su.add_argument("--width", type=int, default=56)
    su.add_argument("--json", action="store_true")
    su.set_defaults(func=cmd_untitled)

    # stats --------------------------------------------------------------
    st = sub.add_parser("stats", help="database statistics")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    # rename -------------------------------------------------------------
    sr = sub.add_parser("rename", help="set session titles (single or bulk from JSON)")
    sr.add_argument("session", nargs="?", help="session id (with TITLE)")
    sr.add_argument("title", nargs="?", help="new title (with SESSION_ID)")
    sr.add_argument("--file", help="JSON file: {id: title} or [{id,title}, ...]")
    sr.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    sr.add_argument("--no-backup", action="store_true", help="skip the automatic DB backup")
    sr.add_argument("--backup-dir", help="directory for the backup file")
    sr.set_defaults(func=cmd_rename)

    # export -------------------------------------------------------------
    se = sub.add_parser("export", help="export a session to JSON via the opencode CLI")
    se.add_argument("session")
    se.add_argument("-o", "--out", help="output file (default: <session-id>.json)")
    se.add_argument("--stdout", action="store_true", help="write to stdout instead")
    se.set_defaults(func=cmd_export)

    # import -------------------------------------------------------------
    si = sub.add_parser("import", help="import a session from JSON via the opencode CLI")
    si.add_argument("file", help="JSON file exported by opencode/ocs")
    si.add_argument("--to", help="target project directory (default: current directory)")
    si.set_defaults(func=cmd_import)

    # move ---------------------------------------------------------------
    sm = sub.add_parser("move", help="relocate a session to another project directory")
    sm.add_argument("session")
    sm.add_argument("--to", required=True, help="target project directory")
    sm.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sm.add_argument("--keep", action="store_true", help="keep the intermediate export file")
    sm.set_defaults(func=cmd_move)

    # backup -------------------------------------------------------------
    sb = sub.add_parser("backup", help="make a consistent copy of opencode.db")
    sb.add_argument("-o", "--out", help="destination file")
    sb.set_defaults(func=cmd_backup)

    return p


def main(argv: "list|None" = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    db = resolve_db(args.db)

    # Read-only connection for every command (rename also opens its own RW one).
    con = connect(db, ro=True)
    try:
        args.func(args, db, con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
