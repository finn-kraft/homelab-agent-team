"""Explicit plugin registration and discovery for optional agent features."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any


@dataclass(frozen=True, slots=True)
class PluginInfo:
    name: str
    version: str = "unknown"
    plugin: Any = None


class PluginManager:
    """Manage optional plugins without coupling them to workflow state."""

    def __init__(self, *, group: str = "homelab_agent_team.plugins") -> None:
        self.group = group
        self._plugins: dict[str, PluginInfo] = {}

    def register(self, plugin: Any, *, name: str | None = None,
                 version: str = "unknown", replace: bool = False) -> PluginInfo:
        resolved = name or str(getattr(plugin, "name", plugin.__class__.__name__)).strip()
        if not resolved:
            raise ValueError("plugin name must not be empty")
        if resolved in self._plugins and not replace:
            raise ValueError(f"plugin already registered: {resolved}")
        info = PluginInfo(resolved, version, plugin)
        self._plugins[resolved] = info
        return info

    def discover(self) -> tuple[PluginInfo, ...]:
        """Load installed entry points in the configured plugin group."""
        discovered: list[PluginInfo] = []
        points = entry_points()
        selected = points.select(group=self.group) if hasattr(points, "select") else points.get(self.group, ())
        for point in selected:
            loaded = point.load()
            plugin = loaded() if isinstance(loaded, type) else loaded
            discovered.append(self.register(plugin, name=point.name, replace=True))
        return tuple(discovered)

    def get(self, name: str) -> Any:
        try:
            return self._plugins[name].plugin
        except KeyError as exc:
            raise KeyError(f"plugin not registered: {name}") from exc

    def infos(self) -> tuple[PluginInfo, ...]:
        return tuple(self._plugins[name] for name in sorted(self._plugins))

    def start_all(self) -> None:
        for info in self.infos():
            start = getattr(info.plugin, "start", None)
            if callable(start):
                start()

    def stop_all(self) -> None:
        for info in reversed(self.infos()):
            stop = getattr(info.plugin, "stop", None)
            if callable(stop):
                stop()


__all__ = ["PluginInfo", "PluginManager"]
