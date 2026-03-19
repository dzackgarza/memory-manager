from pathlib import Path

from memory_manager.cli import (
    parse_memory_file,
    resolve_memory_root,
    resolve_project,
    slug_from_path,
    validate_project_name,
    write_memory_file,
)


def test_slug_from_path_skips_home_and_username() -> None:
    assert (
        slug_from_path("/home/dzack/opencode-plugins/memory-manager")
        == "opencode-plugins-memory-manager"
    )


def test_validate_project_name_rejects_nested_paths() -> None:
    assert validate_project_name("foo/bar") is not None
    assert validate_project_name("../bad") is not None
    assert validate_project_name("global") is None


def test_resolve_project_falls_back_to_global_without_git_root() -> None:
    assert resolve_project(None, "/definitely/not/a/repo") == "global"


def test_write_and_parse_memory_file(tmp_path: Path) -> None:
    target = tmp_path / "global" / "mem_test-20260319T000000Z.md"
    write_memory_file(
        target,
        {
            "id": "mem_test",
            "project": "global",
            "session_id": "ses_test",
            "tags": ["alpha", "beta"],
        },
        "hello world",
    )

    parsed = parse_memory_file(target)
    assert parsed is not None
    assert parsed["id"] == "mem_test"
    assert parsed["project"] == "global"
    assert parsed["session_id"] == "ses_test"
    assert parsed["tags"] == ["alpha", "beta"]
    assert parsed["content"] == "hello world"


def test_resolve_memory_root_prefers_override(tmp_path: Path) -> None:
    assert resolve_memory_root(str(tmp_path)) == tmp_path
