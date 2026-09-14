from __future__ import annotations

import re

from .commands import CommandRunner


class GitError(RuntimeError):
    pass


class GitRepository:
    def __init__(self, runner: CommandRunner):
        self.runner = runner

    def _git(self, *args: str, check: bool = True):
        result = self.runner.run(["git", *args])
        if check and result.exit_code:
            raise GitError(result.stderr.strip() or "git command failed")
        return result

    def branch(self) -> str:
        return self._git("branch", "--show-current").stdout.strip()

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").stdout.strip()

    def status(self) -> str:
        return self._git("status", "--short").stdout

    def diff(self) -> str:
        return self._git("diff", "--no-ext-diff").stdout

    def changed_files(self) -> list[str]:
        out = self._git("status", "--porcelain=v1", "-uall").stdout
        files: list[str] = []
        for line in out.splitlines():
            if len(line) <= 3:
                continue
            path = line[3:]
            # A rename is represented as "old -> new"; the new path is task-owned.
            files.append(path.rsplit(" -> ", 1)[-1])
        return files

    def commit(self, files: list[str], message: str) -> str:
        if not files:
            raise GitError("refusing to commit without explicit files")
        if not re.fullmatch(r"[\w ./@+-]+", message) or len(message) < 8:
            raise GitError("invalid commit message")
        self._git("add", "--", *files)
        self._git("commit", "-m", message)
        return self.head()
