"""Lifecycle and health registry shared by local agent components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Component(Protocol):
    """Minimum lifecycle contract for an agent-team service component."""

    name: str

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def health(self) -> Any: ...


@dataclass(slots=True)
class ComponentSnapshot:
    name: str
    status: str
    detail: Any = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "updated_at": self.updated_at.isoformat()}


class ComponentRegistry:
    """Thread-safe registry whose explicit lifecycle controls worker startup."""

    def __init__(self) -> None:
        self._components: dict[str, Component] = {}
        self._lock = RLock()

    def register(self, component: Component, *, replace: bool = False) -> None:
        name = str(component.name).strip()
        if not name:
            raise ValueError("component name must not be empty")
        if not isinstance(component, Component):
            raise TypeError("component must implement the Component protocol")
        with self._lock:
            if name in self._components and not replace:
                raise ValueError(f"component already registered: {name}")
            self._components[name] = component

    def unregister(self, name: str) -> Component:
        with self._lock:
            try:
                return self._components.pop(name)
            except KeyError as exc:
                raise KeyError(f"component not registered: {name}") from exc

    def get(self, name: str) -> Component:
        with self._lock:
            try:
                return self._components[name]
            except KeyError as exc:
                raise KeyError(f"component not registered: {name}") from exc

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._components))

    def _ordered(self) -> list[Component]:
        with self._lock:
            return [self._components[name] for name in sorted(self._components)]

    def start_all(self) -> None:
        for component in self._ordered():
            component.start()

    def stop_all(self) -> None:
        for component in reversed(self._ordered()):
            component.stop()

    def snapshots(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for component in self._ordered():
            try:
                health = component.health()
                status = "online" if health is True or health is None else "degraded"
                detail = None if health is True else health
            except Exception as exc:  # health must never break the dashboard
                status, detail = "offline", str(exc)
            result.append(ComponentSnapshot(component.name, status, detail).as_dict())
        return result


__all__ = ["Component", "ComponentRegistry", "ComponentSnapshot"]
