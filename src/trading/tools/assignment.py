"""Per-role tool assignment resolver.

Agents declare their toolsets in settings.yaml (`agents.tools`). Registry tools
are validated against TOOL_SCHEMAS; `web_search` is a server-side Anthropic
pseudo-tool handled by the runner. `propose_order` is only ever granted to the
strategy role, regardless of what the YAML says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Pseudo-tool name for Anthropic's server-side web search (not in TOOL_SCHEMAS).
WEB_SEARCH = "web_search"

KNOWN_ROLES = ("strategy", "risk", "redteam", "scoring", "intel")


@dataclass(frozen=True)
class ResolvedTools:
    """Tools available to one agent role after config resolution."""

    role: str
    registry: tuple[str, ...]   # names passed to ToolRegistry
    web_search: bool = False
    web_search_max_uses: int = 0

    def has_registry_tools(self) -> bool:
        return bool(self.registry)


def _default_spec(role: str) -> list[Any]:
    """Fallback when agents.tools is absent/empty for a role."""
    if role == "strategy":
        return ["all"]
    if role == "intel":
        return []
    if role == "scoring":
        return [
            "read_journal", "read_memory", "read_playbook",
            "get_bars", "get_market_context", "recall_similar",
        ]
    # risk / redteam / unknown -> all read-only
    return ["all_readonly"]


def _expand_entry(entry: Any, *, role: str, tools_cfg: dict, visiting: set[str]) -> list[str]:
    """Expand one list entry into concrete tool names (may include web_search)."""
    if isinstance(entry, str):
        key = entry.strip().lower()
        if key == "all":
            from .registry import TOOL_SCHEMAS
            return sorted(TOOL_SCHEMAS)
        if key == "all_readonly":
            from .registry import READ_ONLY_TOOLS
            return list(READ_ONLY_TOOLS)
        if key.startswith("same_as:"):
            return _resolve_raw(key.split(":", 1)[1].strip(), tools_cfg, visiting)
        return [entry.strip()]

    if isinstance(entry, dict):
        if "same_as" in entry:
            return _resolve_raw(str(entry["same_as"]).strip(), tools_cfg, visiting)
        raise ValueError(f"unknown tools entry for {role}: {entry}")

    raise ValueError(f"invalid tools entry for {role}: {entry!r}")


def _resolve_raw(role: str, tools_cfg: dict, visiting: set[str]) -> list[str]:
    if role in visiting:
        raise ValueError(f"circular same_as involving '{role}'")
    visiting.add(role)

    raw = tools_cfg.get(role)
    if raw is None:
        spec = _default_spec(role)
    elif isinstance(raw, dict) and "same_as" in raw:
        return _resolve_raw(str(raw["same_as"]).strip(), tools_cfg, visiting)
    elif isinstance(raw, str):
        spec = [raw]
    elif isinstance(raw, list):
        spec = raw
    else:
        raise ValueError(f"agents.tools.{role} must be a list, got {type(raw).__name__}")

    names: list[str] = []
    for entry in spec:
        names.extend(_expand_entry(entry, role=role, tools_cfg=tools_cfg, visiting=visiting))
    visiting.discard(role)
    return names


def resolve_tools_for(agents_settings, role: str) -> ResolvedTools:
    """Resolve the toolset for `role` from AgentSettings.

    Raises ValueError on unknown tool names, circular same_as, or invalid specs.
    """
    role = (role or "").strip().lower()
    if not role:
        raise ValueError("role is required")

    tools_cfg = dict(getattr(agents_settings, "tools", None) or {})
    max_uses_cfg = dict(getattr(agents_settings, "web_search_max_uses", None) or {})

    raw_names = _resolve_raw(role, tools_cfg, set())

    from .registry import TOOL_SCHEMAS

    registry: list[str] = []
    want_web = False
    unknown: list[str] = []
    for name in raw_names:
        if name == WEB_SEARCH:
            want_web = True
            continue
        if name not in TOOL_SCHEMAS:
            unknown.append(name)
            continue
        if name not in registry:
            registry.append(name)

    if unknown:
        raise ValueError(f"unknown tools for role '{role}': {unknown}")

    # Safety invariant: only strategy may propose orders, regardless of YAML.
    if role != "strategy":
        registry = [n for n in registry if n not in ("propose_order", "propose_vertical")]

    max_uses = int(max_uses_cfg.get(role, 5 if role == "intel" else 3 if role == "strategy" else 0))
    if max_uses <= 0:
        want_web = False

    return ResolvedTools(
        role=role,
        registry=tuple(sorted(registry)),
        web_search=want_web,
        web_search_max_uses=max_uses if want_web else 0,
    )


def web_search_tool_schema(max_uses: int) -> dict:
    """Anthropic server-side web_search tool entry for messages.create."""
    return {
        "type": "web_search_20250305",
        "name": WEB_SEARCH,
        "max_uses": max(1, int(max_uses)),
    }
