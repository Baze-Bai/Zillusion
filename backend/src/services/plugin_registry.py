"""Plugin/Skill registration system — ported from Claude Code's directory-based discovery.

Features:
- Directory-based plugin discovery with frontmatter parsing
- User-configurable enable/disable per plugin
- Skill loading from multiple sources: bundled, user, managed
- Search hints for ToolSearch-based deferred loading
- Hot-reload support for development

Plugin structure:
  plugins/
    my-plugin/
      SKILL.md          # Frontmatter + instructions
      __init__.py       # Python entry point (optional)
      config.yaml       # Plugin configuration (optional)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


@dataclass
class PluginMetadata:
    """Metadata parsed from skill/plugin frontmatter."""
    name: str
    description: str = ""
    version: str = "0.0.1"
    author: str = ""
    tags: list[str] = field(default_factory=list)
    search_hint: str = ""  # 3-10 words for keyword matching
    should_defer: bool = False  # If True, only loaded when ToolSearch finds it
    always_load: bool = False  # If True, always visible to model
    domains: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)  # Regex patterns to auto-trigger


@dataclass
class Plugin:
    """A registered plugin with metadata and execution logic."""
    metadata: PluginMetadata
    source: str = "bundled"  # bundled / user / managed / mcp
    enabled: bool = True
    path: Path | None = None
    execute: Callable[..., Coroutine] | None = None
    tools: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from markdown content.

    Format:
    ---
    name: my-plugin
    description: Does something useful
    tags: [search, data]
    ---

    Plugin instructions here...
    """
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", content, re.DOTALL)
    if not match:
        return {}, content

    frontmatter_text = match.group(1)
    body = match.group(2)

    # Simple YAML-like parsing (no full YAML dependency needed)
    metadata: dict[str, Any] = {}
    for line in frontmatter_text.strip().split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()

            # Handle list values [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                items = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
                metadata[key] = [i for i in items if i]
            # Handle boolean
            elif value.lower() in ("true", "false"):
                metadata[key] = value.lower() == "true"
            else:
                metadata[key] = value.strip("'\"")

    return metadata, body


class PluginRegistry:
    """Central registry for all plugins and skills.

    Supports:
    - Programmatic registration (built-in plugins)
    - Directory-based discovery (user plugins)
    - Deferred loading via search hints
    - Enable/disable per plugin
    """

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._user_preferences: dict[str, bool] = {}  # plugin_name -> enabled

    def register(self, plugin: Plugin) -> None:
        """Register a plugin instance."""
        self._plugins[plugin.metadata.name] = plugin
        logger.debug("Registered plugin: %s (source=%s)", plugin.metadata.name, plugin.source)

    def register_from_metadata(
        self,
        metadata: PluginMetadata,
        execute: Callable | None = None,
        source: str = "bundled",
    ) -> Plugin:
        """Create and register a plugin from metadata."""
        plugin = Plugin(
            metadata=metadata,
            source=source,
            execute=execute,
        )
        self.register(plugin)
        return plugin

    def discover_from_directory(self, directory: Path, source: str = "user") -> int:
        """Scan a directory for plugins with SKILL.md frontmatter.

        Returns number of plugins discovered.
        """
        count = 0
        failures = 0
        if not directory.exists():
            logger.info(
                "plugin_registry.discover_skip",
                extra={
                    "event": "plugin_registry.discover_skip",
                    "reason": "directory_missing",
                    "directory": str(directory),
                    "source": source,
                },
            )
            return 0

        logger.info(
            "plugin_registry.discover_start",
            extra={
                "event": "plugin_registry.discover_start",
                "directory": str(directory),
                "source": source,
            },
        )

        for skill_file in directory.glob("*/SKILL.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(content)

                metadata = PluginMetadata(
                    name=fm.get("name", skill_file.parent.name),
                    description=fm.get("description", ""),
                    version=fm.get("version", "0.0.1"),
                    tags=fm.get("tags", []),
                    search_hint=fm.get("search_hint", ""),
                    should_defer=fm.get("should_defer", False),
                    always_load=fm.get("always_load", False),
                    domains=fm.get("domains", []),
                    triggers=fm.get("triggers", []),
                )

                plugin = Plugin(
                    metadata=metadata,
                    source=source,
                    path=skill_file.parent,
                    enabled=self._user_preferences.get(metadata.name, True),
                )
                self.register(plugin)
                count += 1
                logger.info(
                    "plugin_registry.loaded",
                    extra={
                        "event": "plugin_registry.loaded",
                        "name": metadata.name,
                        "version": metadata.version,
                        "source": source,
                        "path": str(skill_file.parent),
                    },
                )

            except Exception as e:
                failures += 1
                logger.warning(
                    "plugin_registry.load_failed",
                    extra={
                        "event": "plugin_registry.load_failed",
                        "path": str(skill_file),
                        "error_type": type(e).__name__,
                        "error": str(e),
                    },
                )

        logger.info(
            "plugin_registry.discover_complete",
            extra={
                "event": "plugin_registry.discover_complete",
                "directory": str(directory),
                "source": source,
                "loaded": count,
                "failed": failures,
            },
        )
        return count

    def get(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def get_all(self, enabled_only: bool = True) -> list[Plugin]:
        plugins = list(self._plugins.values())
        if enabled_only:
            plugins = [p for p in plugins if p.enabled]
        return plugins

    def get_always_loaded(self) -> list[Plugin]:
        """Get plugins that should always be visible to the model."""
        return [
            p for p in self._plugins.values()
            if p.enabled and p.metadata.always_load
        ]

    def get_deferred(self) -> list[Plugin]:
        """Get plugins that should be deferred (loaded on demand)."""
        return [
            p for p in self._plugins.values()
            if p.enabled and p.metadata.should_defer
        ]

    def search(self, query: str) -> list[Plugin]:
        """Search plugins by keyword matching against search_hint, name, tags.

        Ported from Claude Code's ToolSearch pattern.
        """
        query_lower = query.lower()
        results = []

        for plugin in self._plugins.values():
            if not plugin.enabled:
                continue

            # Match against search hint
            if plugin.metadata.search_hint and query_lower in plugin.metadata.search_hint.lower():
                results.append(plugin)
                continue

            # Match against name
            if query_lower in plugin.metadata.name.lower():
                results.append(plugin)
                continue

            # Match against tags
            if any(query_lower in tag.lower() for tag in plugin.metadata.tags):
                results.append(plugin)
                continue

            # Match against description
            if plugin.metadata.description and query_lower in plugin.metadata.description.lower():
                results.append(plugin)
                continue

        return results

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Enable or disable a plugin."""
        plugin = self._plugins.get(name)
        if plugin:
            plugin.enabled = enabled
            self._user_preferences[name] = enabled
            return True
        return False

    def get_trigger_matches(self, text: str) -> list[Plugin]:
        """Find plugins whose trigger patterns match the given text."""
        matches = []
        for plugin in self.get_all():
            for pattern in plugin.metadata.triggers:
                try:
                    if re.search(pattern, text, re.IGNORECASE):
                        matches.append(plugin)
                        break
                except re.error:
                    pass
        return matches


# Singleton
plugin_registry = PluginRegistry()
