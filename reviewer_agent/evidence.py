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

    def __init__(self, allowed_roots: list[str], max_diff_bytes=300_000,
                 max_document_chars=20_000, max_command_output_chars=50_000):
        self.roots = [Path(v).resolve(strict=True) for v in allowed_roots]
        self.max_diff_bytes = max_diff_bytes
        self.max_document_chars = max(1_000, int(max_document_chars))
        self.max_command_output_chars = max(1_000, int(max_command_output_chars))

    def root(self, value: str) -> Path:
        path = Path(value).resolve(strict=True)
        if not any(path == r or path.is_relative_to(r) for r in self.roots) or not (path / ".git").exists():
            raise EvidenceError("repository outside allowed roots or not Git")
        return path

    @staticmethod
    def _bounded_diff(value: str, limit: int) -> str:
        """Keep the diff within its byte budget, including the marker."""
        limit = max(1, int(limit))
        marker = "\n[TRUNCATED]"
        raw = value.encode("utf-8", errors="replace")
        if len(raw) <= limit:
            return value
        marker_bytes = marker.encode()
        if len(marker_bytes) >= limit:
            return marker_bytes[:limit].decode("utf-8", errors="ignore")
        head = raw[: limit - len(marker_bytes)].decode("utf-8", errors="ignore")
        return head + marker

    @staticmethod
    def _run(root: Path, argv: list[str], timeout=120, output_limit=50_000):
        started = time.monotonic()
        done = subprocess.run(argv, cwd=root, text=True, capture_output=True,
                              timeout=timeout, shell=False)
        return {"command": argv, "exit_code": done.returncode,
                "stdout": done.stdout[:output_limit], "stderr": done.stderr[:output_limit],
                "duration_seconds": time.monotonic()-started}

    def collect(self, repository: str, starting_commit: str, files: list[str], commands: list[list[str]]):
        root = self.root(repository)
        safe_files = []
        for value in files:
            resolved = (root / value).resolve()
            if resolved != root and not resolved.is_relative_to(root): raise EvidenceError("changed path escapes repo")
            safe_files.append(value)
        diff_result = self._run(root, ["git", "diff", "--no-ext-diff", starting_commit, "--", *safe_files],
                                output_limit=self.max_command_output_chars)
        diff = diff_result["stdout"]
        diff = self._bounded_diff(diff, self.max_diff_bytes)
        checks = {"diff_check": self._run(root, ["git", "diff", "--check", starting_commit, "--", *safe_files],
                                           output_limit=self.max_command_output_chars)}
        allowed = {"pytest", "ruff", "mypy", "npm", "pnpm", "yarn", "go", "cargo"}
        checks["verification"] = [self._run(root, cmd, output_limit=self.max_command_output_chars)
                                   for cmd in commands if cmd and cmd[0] in allowed]
        secret_hits = [p.pattern for p in self.SECRET_PATTERNS if p.search(diff)]
        docs = {name: (root/name).read_text(errors="replace")[:self.max_document_chars]
                for name in self.DOCS if (root/name).is_file()}
        flags = {"dependency_change": any(Path(f).name in {"pyproject.toml","package.json","requirements.txt","package-lock.json"} for f in files),
                 "migration_change": any("migration" in f.lower() for f in files),
                 "financial_change": any(word in diff.lower() for word in ("decimal", "cashflow", "rounding", "money")),
                 "tests_deleted": any(line.startswith("-") and ("assert" in line or "test_" in line) for line in diff.splitlines()),
                 "large_diff": diff.endswith("[TRUNCATED]")}
        return {"diff": diff, "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
                "checks": checks, "secret_hits": secret_hits, "documents": docs, "risk_flags": flags}
