from dataclasses import dataclass

import pytest

from components import ComponentRegistry
from plugins import PluginManager
from transaction_manager import TransactionManager


class Connection:
    def __init__(self):
        self.committed = self.rolled_back = self.closed = False

    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True
    def close(self): self.closed = True


def test_transaction_manager_commits_and_closes():
    connection = Connection()
    assert TransactionManager(lambda: connection).run(lambda db: "ok") == "ok"
    assert connection.committed and connection.closed and not connection.rolled_back


def test_transaction_manager_rolls_back_on_failure():
    connection = Connection()
    with pytest.raises(RuntimeError):
        TransactionManager(lambda: connection).run(lambda _db: (_ for _ in ()).throw(RuntimeError("bad")))
    assert connection.rolled_back and connection.closed and not connection.committed


@dataclass
class Service:
    name: str
    healthy: bool = True
    started: bool = False

    def start(self): self.started = True
    def stop(self): self.started = False
    def health(self): return self.healthy


def test_component_registry_lifecycle_and_health():
    service = Service("telemetry")
    registry = ComponentRegistry()
    registry.register(service)
    registry.start_all()
    assert service.started
    assert registry.snapshots()[0]["status"] == "online"
    registry.stop_all()
    assert not service.started


def test_plugin_manager_registers_and_starts_plugins():
    plugin = Service("gpu")
    manager = PluginManager()
    manager.register(plugin)
    manager.start_all()
    assert manager.get("gpu") is plugin and plugin.started
