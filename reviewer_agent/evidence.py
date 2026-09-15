from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path


class EvidenceError(ValueError): pass


class EvidenceCollector:
    DOCS = ("WORKER.md", "AGENTS.md", "README.md", "docs/architecture.md",
            "docs/data-model.md", "docs/roadmap.md")
    SECRET_PATTERNS = (re.compile(r"-----BEGIN .*PRIVATE " + r"KEY-----"),
                       re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
                       re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"))

    def __init__(self, allowed_roots: list[str], max_diff_bytes=300_000):
        self.roots = [Path(v).resolve(strict=True) for v in allowed_roots]
        self.max_diff_bytes = max_diff_bytes

    def root(self, value: str) -> Path:
        path = Path(value).resolve(strict=True)
        if not any(path == r or path.is_relative_to(r) for r in self.roots) or not (path / ".git").exists():
            raise EvidenceError("repository outside allowed roots or not Git")
        return path

    @staticmethod
    def _run(root: Path, argv: list[str], timeout=120):
        started = time.monotonic()
        done = subprocess.run(argv, cwd=root, text=True, capture_output=True,
                              timeout=timeout, shell=False)
        return {"command": argv, "exit_code": done.returncode, "stdout": done.stdout[:100_000],
                "stderr": done.stderr[:100_000], "duration_seconds": time.monotonic()-started}

    def collect(self, repository: str, starting_commit: str, files: list[str], commands: list[list[str]]):
        root = self.root(repository)
        safe_files = []
        for value in files:
            resolved = (root / value).resolve()
            if resolved != root and not resolved.is_relative_to(root): raise EvidenceError("changed path escapes repo")
            safe_files.append(value)
        diff_result = self._run(root, ["git", "diff", "--no-ext-diff", starting_commit, "--", *safe_files])
        diff = diff_result["stdout"]
        if len(diff.encode()) > self.max_diff_bytes: diff = diff[:self.max_diff_bytes] + "\n[TRUNCATED]"
        checks = {"diff_check": self._run(root, ["git", "diff", "--check", starting_commit, "--", *safe_files])}
        allowed = {"pytest", "ruff", "mypy", "npm", "pnpm", "yarn", "go", "cargo"}
        checks["verification"] = [self._run(root, cmd) for cmd in commands if cmd and cmd[0] in allowed]
        secret_hits = [p.pattern for p in self.SECRET_PATTERNS if p.search(diff)]
        docs = {name: (root/name).read_text(errors="replace")[:50_000] for name in self.DOCS if (root/name).is_file()}
        flags = {"dependency_change": any(Path(f).name in {"pyproject.toml","package.json","requirements.txt","package-lock.json"} for f in files),
                 "migration_change": any("migration" in f.lower() for f in files),
                 "financial_change": any(word in diff.lower() for word in ("decimal", "cashflow", "rounding", "money")),
                 "tests_deleted": any(line.startswith("-") and ("assert" in line or "test_" in line) for line in diff.splitlines()),
                 "large_diff": diff.endswith("[TRUNCATED]")}
        return {"diff": diff, "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "checks": checks, "secret_hits": secret_hits, "documents": docs, "risk_flags": flags}
