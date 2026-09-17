import subprocess
import sys
from pathlib import Path

import pytest

from coder_agent.commands import CommandPolicy, CommandRejected, CommandRunner
from coder_agent.workspace import Workspace


def make_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return Workspace(root, [tmp_path])


def test_rejects_shell_and_destructive_git(tmp_path):
    policy = CommandPolicy()
    with pytest.raises(CommandRejected):
        policy.validate(["sh", "-c", "anything"])
    with pytest.raises(CommandRejected):
        policy.validate(["git", "reset", "--hard"])
    with pytest.raises(CommandRejected):
        policy.validate(["git", "push"])


def test_captures_command_truth(tmp_path):
    runner = CommandRunner(make_workspace(tmp_path))
    result = runner.run(["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"])
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert result.exit_code == 7
    assert result.duration_seconds >= 0


def test_python_tools_can_use_the_service_interpreter():
    resolved = CommandRunner._resolve_python_tool(["pytest", "-q"], {"PATH": ""})
    assert resolved[:3] == [sys.executable, "-m", "pytest"]
