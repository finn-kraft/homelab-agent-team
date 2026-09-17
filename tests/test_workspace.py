import os
import subprocess
from pathlib import Path

import pytest

from coder_agent.workspace import Workspace, WorkspaceViolation


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_workspace_allows_normal_file(tmp_path):
    root = repo(tmp_path)
    workspace = Workspace(root, [tmp_path])
    workspace.write_text("src/example.py", "answer = 42\n")
    assert workspace.read_text("src/example.py") == "answer = 42\n"


def test_workspace_blocks_parent_escape(tmp_path):
    root = repo(tmp_path)
    workspace = Workspace(root, [tmp_path])
    with pytest.raises(WorkspaceViolation):
        workspace.write_text("../../escape", "bad")


def test_workspace_blocks_symlink_escape(tmp_path):
    root = repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, root / "link")
    workspace = Workspace(root, [tmp_path])
    with pytest.raises(WorkspaceViolation):
        workspace.write_text("link/escape", "bad")


def test_missing_read_path_explains_how_to_recover(tmp_path):
    workspace = Workspace(repo(tmp_path), [tmp_path])
    with pytest.raises(WorkspaceViolation, match="inspect the repository tree"):
        workspace.read_text("does-not-exist.txt")


def test_relative_path_canonicalizes_model_spellings(tmp_path):
    root = repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "example.py").write_text("answer = 42\n")
    workspace = Workspace(root, [tmp_path])

    assert workspace.relative_path("./src/../src/example.py") == "src/example.py"
    assert workspace.relative_path(str(root / "src/example.py")) == "src/example.py"


def test_workspace_lists_relative_files_without_git_metadata(tmp_path):
    root = repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("value = 1\n")
    workspace = Workspace(root, [tmp_path])
    assert "src/app.py" in workspace.list_files()
    assert all(".git" not in value for value in workspace.list_files())


def test_delete_needs_justification(tmp_path):
    root = repo(tmp_path)
    (root / "old.txt").write_text("old")
    workspace = Workspace(root, [tmp_path])
    with pytest.raises(WorkspaceViolation):
        workspace.delete("old.txt", "cleanup")
