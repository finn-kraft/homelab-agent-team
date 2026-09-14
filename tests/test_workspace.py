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


def test_delete_needs_justification(tmp_path):
    root = repo(tmp_path)
    (root / "old.txt").write_text("old")
    workspace = Workspace(root, [tmp_path])
    with pytest.raises(WorkspaceViolation):
        workspace.delete("old.txt", "cleanup")

