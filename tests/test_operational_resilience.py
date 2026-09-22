from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_systemd_templates_use_journal_limits_and_private_backup_permissions():
    for name in (
        "agent-orchestrator.service.example",
        "agent-control-center.service.example",
        "homelab-routing-agent.service.example",
    ):
        text = (ROOT / "deploy" / name).read_text()
        assert "StandardOutput=journal" in text
        assert "StandardError=journal" in text
        assert "LogRateLimitIntervalSec=" in text
        assert "LogRateLimitBurst=" in text

    journal = (ROOT / "deploy" / "journald-agent-team.conf.example").read_text()
    assert "SystemMaxUse=" in journal
    assert "MaxRetentionSec=" in journal
    backup = (ROOT / "deploy" / "agent-db-backup.service.example").read_text()
    assert "UMask=0077" in backup
    assert "PGSERVICE=" in backup
    assert "DATABASE_URL" not in backup
    assert (ROOT / "deploy" / "agent-db-backup.timer.example").exists()
    routing = (ROOT / "deploy" / "homelab-routing-agent.service.example").read_text()
    assert "--log-config" in routing
    assert (ROOT / "deploy" / "uvicorn-logging.json.example").exists()


def test_disaster_recovery_runbook_defines_order_and_non_destructive_worktree_policy():
    text = (ROOT / "docs" / "DISASTER_RECOVERY.md").read_text().lower()
    positions = [text.index(term) for term in (
        "1. contain", "2. postgresql", "3. git", "4. configuration",
        "5. systemd", "6. workflow",
    )]
    assert positions == sorted(positions)
    for phrase in (
        "agent-db-backup verify",
        "empty target database",
        "diagnose-worktree",
        "never runs git reset",
        "do not commit",
    ):
        assert phrase in text


def test_development_database_uses_an_external_secret_file():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "POSTGRES_PASSWORD_FILE" in compose
    assert "POSTGRES_PASSWORD:" not in compose
    assert "agent_postgres_password" in compose
