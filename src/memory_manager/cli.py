#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "pyyaml",
#   "typer",
# ]
# ///
"""opencode-file-memory: YAML-headered markdown memory store for OpenCode agents.

Commands:
  remember    Save a memory to the file store
  list        Query memory metadata with SQL; returns matching paths
  list-files  Output memory file paths for piping to other tools
  recall      Search memories semantically (via semtools) — standalone only
  forget      Delete a memory by ID
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml

app = typer.Typer(no_args_is_help=True, add_completion=False)

BUG_REPORTING_URL = "https://github.com/dzackgarza/opencode-memory-plugin/issues/new?labels=bug"

# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def detect_git_root(cwd: Optional[str] = None) -> Optional[str]:
    """Return the absolute git root path of cwd, or None if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd if cwd else os.getcwd(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def slug_from_path(path: str) -> str:
    """Convert an absolute path to a filesystem-safe lowercase slug.

    Takes the last 3 meaningful path components (skipping 'home' and usernames
    to avoid leaking PII into directory names when the path is under /home).
    """
    parts = [p for p in Path(path).parts if p not in ("", "/")]
    # Drop leading 'home' + one-component username to get meaningful parts
    if len(parts) >= 2 and parts[0] == "home":
        parts = parts[2:]
    relevant = parts[-3:] if len(parts) >= 3 else parts
    slug = "-".join(relevant)
    slug = re.sub(r"[^a-z0-9]", "-", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown"


def ensure_memory_repo(root: Path) -> Optional[str]:
    """Initialize root as a git repo if it isn't already one.

    Returns an error message string on failure, None on success.
    A failure here means the store will not be version-controlled — a significant
    integrity problem that callers must surface to the agent.
    """
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        init = subprocess.run(
            ["git", "init", "--quiet"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            return f"git init failed (exit {init.returncode}): {init.stderr.strip() or '(no output)'}"
        gitignore = root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*.tmp\n")
        return _git_commit(root, "chore: initialize memory store", allow_empty=True)
    return None


def _git_commit(root: Path, message: str, allow_empty: bool = False) -> Optional[str]:
    """Stage all changes and commit. Returns an error string on failure, None on success.

    Callers must surface non-None results to the agent. Version control is not
    optional — it is the primary integrity mechanism for the memory store.
    """
    try:
        add = subprocess.run(
            ["git", "add", "-A"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if add.returncode != 0:
            return f"git add failed (exit {add.returncode}): {add.stderr.strip() or '(no output)'}"

        cmd = [
            "git",
            "-c", "user.email=memory@opencode",
            "-c", "user.name=opencode-memory",
            "commit",
            "-m", message,
        ]
        if allow_empty:
            cmd.append("--allow-empty")
        commit = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=10)

        if commit.returncode != 0:
            combined = (commit.stdout + "\n" + commit.stderr).strip()
            # "nothing to commit" is exit 1 but benign — the file write already landed
            if "nothing to commit" in combined:
                return None
            return f"git commit failed (exit {commit.returncode}): {combined or '(no output)'}"
        return None
    except FileNotFoundError:
        return "git not found — install git to enable memory version control"
    except subprocess.TimeoutExpired:
        return "git operation timed out after 10s"
    except Exception as exc:
        return f"git error: {exc}"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def resolve_memory_root(memory_root_override: Optional[str] = None) -> Path:
    """Return the memory repo root directory.

    Priority: --memory-root arg > OPENCODE_MEMORY_ROOT env > XDG default.
    The default is ~/.local/share/opencode-memory (shared across all projects).
    """
    if memory_root_override:
        return Path(memory_root_override).expanduser()
    env_root = os.environ.get("OPENCODE_MEMORY_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg_data) / "opencode-memory"


def validate_project_name(name: str) -> Optional[str]:
    """Return an error string if name is not a safe single-component directory name."""
    p = Path(name)
    if p.is_absolute():
        return f"project name must not be an absolute path: {name!r}"
    parts = p.parts
    if len(parts) != 1:
        return f"project name must be a single path component (no slashes or '..'): {name!r}"
    if parts[0] in (".", ".."):
        return f"project name must not be '.' or '..': {name!r}"
    return None


def resolve_project(
    project: Optional[str],
    cwd: Optional[str],
) -> str:
    """Return the project name for directory routing.

    Priority: explicit --project > git root detection from --cwd > "global".
    "global" is not a special scope — it is just the default project name when
    no git context is available or explicitly requested.
    """
    if project:
        return project
    git_root = detect_git_root(cwd)
    if git_root:
        return slug_from_path(git_root)
    return "global"


def project_dir(root: Path, project: str) -> Path:
    """Return the directory for a given project name, confined to root."""
    result = (root / project).resolve()
    root_resolved = root.resolve()
    if not str(result).startswith(str(root_resolved) + os.sep) and result != root_resolved:
        raise ValueError(f"project path escapes memory root: {result}")
    return result


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def gen_id() -> str:
    return "mem_" + secrets.token_urlsafe(8)


def make_filename(memory_id: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{memory_id}-{ts}.md"


def write_memory_file(path: Path, frontmatter: dict, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=True
    )
    body = f"---\n{yaml_text}---\n{content}\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)  # atomic on same filesystem (POSIX rename)


def parse_memory_file(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except (IOError, OSError):
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    frontmatter_text = text[4:end]
    content = text[end + 5:]
    try:
        fm = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    return {**fm, "content": content.strip(), "path": str(path)}


def all_memory_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.rglob("*.md") if not p.name.startswith(".")]


def scoped_memory_files(
    root: Path,
    project: Optional[str],
    session_id: Optional[str] = None,
) -> list[Path]:
    """Return memory files matching a project filter.

    - project set: only root/{project}/*.md
    - project=None: all files under root
    """
    if project:
        d = root / project
        files: list[Path] = list(d.glob("*.md")) if d.exists() else []
    else:
        files = all_memory_files(root)

    if session_id:
        filtered = []
        for f in files:
            m = parse_memory_file(f)
            if m and m.get("session_id") == session_id:
                filtered.append(f)
        return filtered
    return files


# ---------------------------------------------------------------------------
# Semtools integration
# ---------------------------------------------------------------------------


def run_semtools_search(query: str, files: list[str], top_k: int) -> list[dict]:
    """Run semtools search, trying direct binary first then npx fallback."""
    for cmd_prefix in (
        ["semtools"],
        ["npx", "--yes", "--package=@llamaindex/semtools", "semtools"],
    ):
        try:
            result = subprocess.run(
                [*cmd_prefix, "search", "--json", "--top-k", str(top_k), query, *files],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except FileNotFoundError:
            continue
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(
                f"semtools exited {result.returncode}: {stderr or '(no stderr)'}"
            )
        try:
            return json.loads(result.stdout).get("results", [])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"semtools returned non-JSON: {result.stdout[:200]}") from exc

    raise RuntimeError(
        "semtools not found. Install with: cargo install semtools  or  npm install -g @llamaindex/semtools"
    )


# ---------------------------------------------------------------------------
# Output helper
# ---------------------------------------------------------------------------


def emit(payload: dict) -> None:
    print(json.dumps(payload, default=str))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def remember(
    content: Annotated[str, typer.Option("--content", help="Memory content (markdown)")],
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="Project name. 'global' for cross-project storage. Omit to auto-detect from --cwd."),
    ] = None,
    cwd: Annotated[
        Optional[str],
        typer.Option("--cwd", help="Working directory for git root detection (default: process cwd)"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Session ID stored as provenance metadata"),
    ] = None,
    tag: Annotated[
        Optional[list[str]],
        typer.Option("--tag", help="Tag (repeatable: --tag foo --tag bar)"),
    ] = None,
    memory_root: Annotated[
        Optional[str],
        typer.Option("--memory-root", help="Override the memory root directory"),
    ] = None,
) -> None:
    """Write a new memory to the file store."""
    root = resolve_memory_root(memory_root)
    git_error = ensure_memory_repo(root)

    if project:
        err = validate_project_name(project)
        if err:
            emit({"ok": False, "stage": "configuration", "message": err})
            raise typer.Exit(1)

    proj = resolve_project(project, cwd)
    pdir = project_dir(root, proj)

    memory_id = gen_id()
    path = pdir / make_filename(memory_id)

    frontmatter = {
        "id": memory_id,
        "project": proj,
        "session_id": session_id,
        "tags": tag or [],
    }

    try:
        write_memory_file(path, frontmatter, content)
    except OSError as exc:
        emit({"ok": False, "stage": "write_file", "message": str(exc), "path": str(path)})
        raise typer.Exit(1)

    git_error = git_error or _git_commit(root, f"remember: add memory {memory_id}")

    emit({
        "ok": True,
        "kind": "remember",
        "id": memory_id,
        "path": str(path),
        "project": proj,
        "git_error": git_error,
    })


@app.command(name="list")
def list_memories(
    sql: Annotated[str, typer.Option("--sql", help="SQL SELECT query against the memories table")],
    memory_root: Annotated[
        Optional[str],
        typer.Option("--memory-root", help="Override the memory root directory"),
    ] = None,
) -> None:
    """Query memory metadata with SQL. Returns rows from an in-memory SQLite table.

    Schema:
      CREATE TABLE memories (
        id         TEXT,
        path       TEXT,
        project    TEXT,
        session_id TEXT,
        tags       TEXT,    -- JSON array, e.g. '["deploy","ops"]'
        mtime      TEXT     -- ISO 8601 from filesystem mtime
      )
    """
    root = resolve_memory_root(memory_root)
    files = all_memory_files(root)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE memories (
            id         TEXT,
            path       TEXT,
            project    TEXT,
            session_id TEXT,
            tags       TEXT,    -- JSON array, e.g. '["deploy","ops"]'
            mtime      TEXT     -- ISO 8601 from filesystem mtime
        )
    """)
    rows = []
    for f in files:
        m = parse_memory_file(f)
        if m:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat()
            rows.append((
                m.get("id"),
                str(f),
                m.get("project"),
                m.get("session_id"),
                json.dumps(m.get("tags") or []),
                mtime,
            ))
    if rows:
        conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    # Prevent write operations — agents may only SELECT
    conn.execute("PRAGMA query_only = ON")

    try:
        cursor = conn.execute(sql)
        if cursor.description is None:
            emit({"ok": False, "stage": "sql", "message": "SQL statement produced no result set — use a SELECT query"})
            raise typer.Exit(1)
        cols = [d[0] for d in cursor.description]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    except typer.Exit:
        raise
    except Exception as exc:
        emit({"ok": False, "stage": "sql", "message": str(exc)})
        raise typer.Exit(1)

    emit({"ok": True, "kind": "list", "results": rows, "count": len(rows)})


@app.command(name="list-files")
def list_files(
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="Restrict to a project name (e.g. 'global')"),
    ] = None,
    cwd: Annotated[
        Optional[str],
        typer.Option("--cwd", help="Working directory for git root detection"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Filter by session ID metadata"),
    ] = None,
    tag: Annotated[
        Optional[str],
        typer.Option("--tag", help="Filter by tag"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum results")] = 50,
    memory_root: Annotated[
        Optional[str],
        typer.Option("--memory-root", help="Override the memory root directory"),
    ] = None,
) -> None:
    """Output memory file paths one per line for piping to other tools.

    Example: list-files --project global | xargs grep 'nginx'
    Example: list-files --cwd . | head -20 | xargs cat
    """
    root = resolve_memory_root(memory_root)
    proj = resolve_project(project, cwd) if (project or cwd) else None
    files = scoped_memory_files(root, proj, session_id)

    count = 0
    for path in sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if tag:
            memory = parse_memory_file(path)
            if not memory or tag not in (memory.get("tags") or []):
                continue
        print(path)
        count += 1
        if count >= limit:
            break


@app.command()
def recall(
    query: Annotated[str, typer.Argument(help="Search query")],
    project: Annotated[
        Optional[str],
        typer.Option("--project", help="Restrict to a project name. Omit to search all."),
    ] = None,
    cwd: Annotated[
        Optional[str],
        typer.Option("--cwd", help="Working directory for git root detection"),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option("--session-id", help="Filter to memories with this session ID"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum number of results")] = 5,
    memory_root: Annotated[
        Optional[str],
        typer.Option("--memory-root", help="Override the memory root directory"),
    ] = None,
) -> None:
    """Search memories semantically using semtools (standalone tool, not a plugin tool)."""
    root = resolve_memory_root(memory_root)
    proj = resolve_project(project, cwd) if (project or cwd) else None
    files = scoped_memory_files(root, proj, session_id)

    if not files:
        emit({"ok": True, "kind": "recall", "results": [], "count": 0})
        return

    file_strs = [str(f) for f in files]

    try:
        hits = run_semtools_search(query, file_strs, top_k=limit * 3)
    except RuntimeError as exc:
        emit({"ok": False, "stage": "semtools", "message": str(exc)})
        raise typer.Exit(1)

    best_distance: dict[str, float] = {}
    for hit in hits:
        fname = hit["filename"]
        dist = float(hit["distance"])
        if fname not in best_distance or dist < best_distance[fname]:
            best_distance[fname] = dist

    ranked = sorted(best_distance.items(), key=lambda kv: kv[1])[:limit]

    results = []
    for fname, distance in ranked:
        memory = parse_memory_file(Path(fname))
        if memory:
            results.append({**memory, "distance": distance})

    emit({"ok": True, "kind": "recall", "results": results, "count": len(results)})


@app.command()
def forget(
    memory_id: Annotated[str, typer.Option("--id", help="Memory ID to delete (mem_xxx)")],
    memory_root: Annotated[
        Optional[str],
        typer.Option("--memory-root", help="Override the memory root directory"),
    ] = None,
) -> None:
    """Delete a memory by ID. Scans all memories — no project flag needed."""
    root = resolve_memory_root(memory_root)

    for path in all_memory_files(root):
        memory = parse_memory_file(path)
        if memory and memory.get("id") == memory_id:
            try:
                path.unlink()
            except OSError as exc:
                emit({
                    "ok": False,
                    "stage": "delete_file",
                    "message": str(exc),
                    "id": memory_id,
                })
                raise typer.Exit(1)
            git_error = _git_commit(root, f"forget: delete memory {memory_id}")
            emit({
                "ok": True,
                "kind": "forget",
                "id": memory_id,
                "path": str(path),
                "message": f"Deleted {memory_id}",
                "git_error": git_error,
            })
            return

    emit({
        "ok": False,
        "stage": "not_found",
        "message": f"No memory found with id {memory_id!r}",
        "id": memory_id,
    })
    raise typer.Exit(1)





if __name__ == "__main__":
    app()
