#!/usr/bin/env python3
"""Git-backed persistent memory CLI for OpenCode agents."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml
from cyclopts import App
from pydantic import BaseModel, ConfigDict, Field, validate_call

app = App(help="Git-backed persistent memory manager.")

type MemoryRecord = dict[str, object]
type JsonDict = dict[str, object]


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    filename: str
    distance: float


class SearchEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[SearchHit] = Field(default_factory=list)


class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="Memory content in markdown form.")
    project: str | None = Field(default=None)
    cwd: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    tag: list[str] | None = Field(default=None)
    memory_root: str | None = Field(default=None)


class ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str
    memory_root: str | None = Field(default=None)


class ListFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(default=None)
    cwd: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    tag: str | None = Field(default=None)
    limit: int = Field(default=50, ge=1)
    memory_root: str | None = Field(default=None)


class RecallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    project: str | None = Field(default=None)
    cwd: str | None = Field(default=None)
    session_id: str | None = Field(default=None)
    limit: int = Field(default=5, ge=1)
    memory_root: str | None = Field(default=None)


class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(alias="id")
    memory_root: str | None = Field(default=None)


class DoctorArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str | None = Field(default=None)
    memory_root: str | None = Field(default=None)


def emit(payload: JsonDict) -> None:
    print(json.dumps(payload, default=str))


def detect_git_root(cwd: str | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd if cwd else os.getcwd(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def slug_from_path(path: str) -> str:
    parts = [p for p in Path(path).parts if p not in ("", "/")]
    if len(parts) >= 2 and parts[0] == "home":
        parts = parts[2:]
    relevant = parts[-3:] if len(parts) >= 3 else parts
    slug = "-".join(relevant)
    slug = re.sub(r"[^a-z0-9]", "-", slug.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "unknown"


def resolve_memory_root(memory_root_override: str | None = None) -> Path:
    if memory_root_override:
        return Path(memory_root_override).expanduser()
    env_root = os.environ.get("OPENCODE_MEMORY_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    xdg_data = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(xdg_data) / "opencode-memory"


def validate_project_name(name: str) -> str | None:
    project_path = Path(name)
    if project_path.is_absolute():
        return f"project name must not be an absolute path: {name!r}"
    parts = project_path.parts
    if len(parts) != 1:
        return f"project name must be a single path component (no slashes or '..'): {name!r}"
    if parts[0] in (".", ".."):
        return f"project name must not be '.' or '..': {name!r}"
    return None


def resolve_project(project: str | None, cwd: str | None) -> str:
    if project:
        return project
    git_root = detect_git_root(cwd)
    if git_root:
        return slug_from_path(git_root)
    return "global"


def project_dir(root: Path, project: str) -> Path:
    result = (root / project).resolve()
    root_resolved = root.resolve()
    if not str(result).startswith(str(root_resolved) + os.sep) and result != root_resolved:
        raise ValueError(f"project path escapes memory root: {result}")
    return result


def gen_id() -> str:
    return "mem_" + secrets.token_urlsafe(8)


def make_filename(memory_id: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{memory_id}-{ts}.md"


def write_memory_file(path: Path, frontmatter: JsonDict, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=True)
    body = f"---\n{yaml_text}---\n{content}\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def parse_memory_file(path: Path) -> MemoryRecord | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    frontmatter_text = text[4:end]
    content = text[end + 5 :]
    try:
        frontmatter = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError:
        return None
    if not isinstance(frontmatter, dict):
        return None
    return {**frontmatter, "content": content.strip(), "path": str(path)}


def all_memory_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*.md") if not path.name.startswith(".")]


def scoped_memory_files(
    root: Path, project: str | None, session_id: str | None = None
) -> list[Path]:
    files = (
        list((root / project).glob("*.md"))
        if project and (root / project).exists()
        else all_memory_files(root)
        if not project
        else []
    )
    if session_id is None:
        return files
    filtered: list[Path] = []
    for file_path in files:
        memory = parse_memory_file(file_path)
        if memory and memory.get("session_id") == session_id:
            filtered.append(file_path)
    return filtered


def ensure_memory_repo(root: Path) -> str | None:
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").exists():
        return None
    init = subprocess.run(
        ["git", "init", "--quiet"], cwd=root, capture_output=True, text=True, check=False
    )
    if init.returncode != 0:
        return f"git init failed (exit {init.returncode}): {init.stderr.strip() or '(no output)'}"
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*.tmp\n", encoding="utf-8")
    return _git_commit(root, "chore: initialize memory store", allow_empty=True)


def _git_commit(root: Path, message: str, allow_empty: bool = False) -> str | None:
    try:
        add = subprocess.run(
            ["git", "add", "-A"], cwd=root, capture_output=True, text=True, timeout=10
        )
        if add.returncode != 0:
            return f"git add failed (exit {add.returncode}): {add.stderr.strip() or '(no output)'}"
        cmd = [
            "git",
            "-c",
            "user.email=memory@opencode",
            "-c",
            "user.name=opencode-memory",
            "commit",
            "-m",
            message,
        ]
        if allow_empty:
            cmd.append("--allow-empty")
        commit = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=10)
        if commit.returncode != 0:
            combined = (commit.stdout + "\n" + commit.stderr).strip()
            if "nothing to commit" in combined:
                return None
            return f"git commit failed (exit {commit.returncode}): {combined or '(no output)'}"
        return None
    except FileNotFoundError:
        return "git not found — install git to enable memory version control"
    except subprocess.TimeoutExpired:
        return "git operation timed out after 10s"
    except Exception as exc:  # pragma: no cover
        return f"git error: {exc}"


def run_semtools_search(query: str, files: list[str], top_k: int) -> list[SearchHit]:
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
                check=False,
            )
        except FileNotFoundError:
            continue
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"semtools exited {result.returncode}: {stderr or '(no stderr)'}")
        try:
            payload = SearchEnvelope.model_validate_json(result.stdout)
            return payload.results
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"semtools returned non-JSON: {result.stdout[:200]}") from exc
    raise RuntimeError(
        "semtools not found. Install with: cargo install semtools or "
        "npm install -g @llamaindex/semtools"
    )


@app.command
@validate_call
def remember(
    content: str,
    project: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    tag: list[str] | None = None,
    memory_root: str | None = None,
) -> None:
    args = RememberArgs(
        content=content,
        project=project,
        cwd=cwd,
        session_id=session_id,
        tag=tag,
        memory_root=memory_root,
    )
    root = resolve_memory_root(args.memory_root)
    git_error = ensure_memory_repo(root)

    if args.project:
        error = validate_project_name(args.project)
        if error:
            emit({"ok": False, "stage": "configuration", "message": error})
            raise SystemExit(1)

    project_name = resolve_project(args.project, args.cwd)
    memory_path = project_dir(root, project_name) / make_filename(gen_id())
    memory_id = memory_path.name.split("-", 1)[0]

    frontmatter: JsonDict = {
        "id": memory_id,
        "project": project_name,
        "session_id": args.session_id,
        "tags": args.tag or [],
    }

    try:
        write_memory_file(memory_path, frontmatter, args.content)
    except OSError as exc:
        emit({"ok": False, "stage": "write_file", "message": str(exc), "path": str(memory_path)})
        raise SystemExit(1) from exc

    git_error = git_error or _git_commit(root, f"remember: add memory {memory_id}")
    emit(
        {
            "ok": True,
            "kind": "remember",
            "id": memory_id,
            "path": str(memory_path),
            "project": project_name,
            "git_error": git_error,
        }
    )


@app.command(name="list")
@validate_call
def list_memories(sql: str, memory_root: str | None = None) -> None:
    args = ListArgs(sql=sql, memory_root=memory_root)
    root = resolve_memory_root(args.memory_root)
    files = all_memory_files(root)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE memories (
            id         TEXT,
            path       TEXT,
            project    TEXT,
            session_id TEXT,
            tags       TEXT,
            mtime      TEXT
        )
        """
    )
    rows: list[tuple[object | None, str, object | None, object | None, str, str]] = []
    for file_path in files:
        memory = parse_memory_file(file_path)
        if memory:
            mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC).isoformat()
            rows.append(
                (
                    memory.get("id"),
                    str(file_path),
                    memory.get("project"),
                    memory.get("session_id"),
                    json.dumps(memory.get("tags") or []),
                    mtime,
                )
            )
    if rows:
        conn.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.execute("PRAGMA query_only = ON")
    try:
        cursor = conn.execute(args.sql)
        if cursor.description is None:
            emit(
                {
                    "ok": False,
                    "stage": "sql",
                    "message": "SQL statement produced no result set — use a SELECT query",
                }
            )
            raise SystemExit(1)
        columns = [item[0] for item in cursor.description]
        result_rows: list[JsonDict] = [
            dict(zip(columns, row, strict=False)) for row in cursor.fetchall()
        ]
    except SystemExit:
        raise
    except Exception as exc:
        emit({"ok": False, "stage": "sql", "message": str(exc)})
        raise SystemExit(1) from exc
    emit({"ok": True, "kind": "list", "results": result_rows, "count": len(result_rows)})


@app.command(name="list-files")
@validate_call
def list_files(
    project: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    tag: str | None = None,
    limit: int = 50,
    memory_root: str | None = None,
) -> None:
    args = ListFilesArgs(
        project=project,
        cwd=cwd,
        session_id=session_id,
        tag=tag,
        limit=limit,
        memory_root=memory_root,
    )
    root = resolve_memory_root(args.memory_root)
    project_name = resolve_project(args.project, args.cwd) if (args.project or args.cwd) else None
    files = scoped_memory_files(root, project_name, args.session_id)
    count = 0
    for file_path in sorted(
        files, key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True
    ):
        if args.tag:
            memory = parse_memory_file(file_path)
            tags = memory.get("tags") if memory else None
            if not memory or not isinstance(tags, list) or args.tag not in tags:
                continue
        print(file_path)
        count += 1
        if count >= args.limit:
            break


@app.command
@validate_call
def recall(
    query: str,
    project: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    limit: int = 5,
    memory_root: str | None = None,
) -> None:
    args = RecallArgs(
        query=query,
        project=project,
        cwd=cwd,
        session_id=session_id,
        limit=limit,
        memory_root=memory_root,
    )
    root = resolve_memory_root(args.memory_root)
    project_name = resolve_project(args.project, args.cwd) if (args.project or args.cwd) else None
    files = scoped_memory_files(root, project_name, args.session_id)
    if not files:
        emit({"ok": True, "kind": "recall", "results": [], "count": 0})
        return
    try:
        hits = run_semtools_search(args.query, [str(item) for item in files], top_k=args.limit * 3)
    except RuntimeError as exc:
        emit({"ok": False, "stage": "semtools", "message": str(exc)})
        raise SystemExit(1) from exc

    best_distance: dict[str, float] = {}
    for hit in hits:
        filename = hit.filename
        distance = hit.distance
        if filename not in best_distance or distance < best_distance[filename]:
            best_distance[filename] = distance

    ranked = sorted(best_distance.items(), key=lambda item: item[1])[: args.limit]
    results: list[MemoryRecord] = []
    for filename, distance in ranked:
        memory = parse_memory_file(Path(filename))
        if memory:
            results.append({**memory, "distance": distance})
    emit({"ok": True, "kind": "recall", "results": results, "count": len(results)})


@app.command
@validate_call
def forget(id: str, memory_root: str | None = None) -> None:  # noqa: A002
    args = ForgetArgs(id=id, memory_root=memory_root)
    root = resolve_memory_root(args.memory_root)
    for file_path in all_memory_files(root):
        memory = parse_memory_file(file_path)
        if memory and memory.get("id") == args.memory_id:
            try:
                file_path.unlink()
            except OSError as exc:
                emit(
                    {"ok": False, "stage": "delete_file", "message": str(exc), "id": args.memory_id}
                )
                raise SystemExit(1) from exc
            git_error = _git_commit(root, f"forget: delete memory {args.memory_id}")
            emit(
                {
                    "ok": True,
                    "kind": "forget",
                    "id": args.memory_id,
                    "path": str(file_path),
                    "message": f"Deleted {args.memory_id}",
                    "git_error": git_error,
                }
            )
            return
    emit(
        {
            "ok": False,
            "stage": "not_found",
            "message": f"No memory found with id {args.memory_id!r}",
            "id": args.memory_id,
        }
    )
    raise SystemExit(1)


@app.command
@validate_call
def doctor(cwd: str | None = None, memory_root: str | None = None) -> None:
    args = DoctorArgs(cwd=cwd, memory_root=memory_root)
    root = resolve_memory_root(args.memory_root)
    git_root = detect_git_root(args.cwd)
    emit(
        {
            "ok": True,
            "kind": "doctor",
            "memory_root": str(root),
            "memory_root_exists": root.exists(),
            "memory_repo_initialized": (root / ".git").exists(),
            "cwd_git_root": git_root,
            "resolved_project": resolve_project(None, args.cwd),
            "semtools_available": shutil_which_any(["semtools", "npx"]),
        }
    )


def shutil_which_any(commands: list[str]) -> bool:
    for command in commands:
        result = subprocess.run(
            ["which", command], capture_output=True, text=True, timeout=5, check=False
        )
        if result.returncode == 0:
            return True
    return False


def main() -> None:
    app()


if __name__ == "__main__":
    main()
