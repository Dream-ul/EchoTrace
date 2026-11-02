"""Agent-readable Skill discovery and progressive disclosure."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from echotrace.domain.agent_roles import AgentRole


_AGENT_ROLES = frozenset(role.value for role in AgentRole)


@dataclass(frozen=True, slots=True)
class AgentSkillSpec:
    name: str
    description: str
    roles: frozenset[str]
    path: Path

    @property
    def version(self) -> str:
        """Return a content-addressed version for audit/replay purposes."""

        return self.content_hash[:16]

    @property
    def content_hash(self) -> str:
        return sha256((self.path / "SKILL.md").read_bytes()).hexdigest()

    def index_entry(self) -> dict[str, str]:
        return {"name": self.name, "description": self.description}


class AgentSkillCatalog:
    """Expose only metadata until the model activates one professional Skill."""

    def __init__(self, root: str | Path, *, max_skill_bytes: int = 64 * 1024) -> None:
        self.root = Path(root).resolve()
        self.max_skill_bytes = max_skill_bytes
        self._skills: dict[str, AgentSkillSpec] = {}

    def discover(self) -> list[AgentSkillSpec]:
        self._skills.clear()
        if not self.root.is_dir():
            return []
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            skill_file = directory / "SKILL.md"
            if not directory.is_dir() or not skill_file.is_file():
                continue
            metadata = _read_frontmatter(skill_file, max_bytes=self.max_skill_bytes)
            name = str(metadata.get("name", "")).strip()
            description = str(metadata.get("description", "")).strip()
            roles = metadata.get("roles")
            if (
                name != directory.name
                or not description
                or not isinstance(roles, list)
                or not roles
                or any(not isinstance(role, str) or not role.strip() for role in roles)
                or any(role.strip() not in _AGENT_ROLES for role in roles)
            ):
                raise ValueError(f"invalid Agent Skill metadata: {directory}")
            self._skills[name] = AgentSkillSpec(
                name, description, frozenset(role.strip() for role in roles), directory.resolve()
            )
        return list(self._skills.values())

    def index(self, *, role: str | None = None) -> list[dict[str, str]]:
        if not self._skills:
            self.discover()
        return [
            self._skills[name].index_entry()
            for name in sorted(self._skills)
            if role is None or role in self._skills[name].roles
        ]

    def activate(self, name: str, *, role: str | None = None) -> str:
        spec = self._get(name)
        if role is not None and role not in spec.roles:
            raise ValueError(f"Agent Skill is not available to role {role}: {name}")
        path = spec.path / "SKILL.md"
        if path.stat().st_size > self.max_skill_bytes:
            raise ValueError(f"Agent Skill instructions exceed limit: {name}")
        return path.read_text(encoding="utf-8")

    def activation_metadata(self, name: str) -> dict[str, str]:
        """Return non-procedural metadata for an activation audit record."""

        spec = self._get(name)
        return {
            "name": spec.name,
            "description": spec.description,
            "version": spec.version,
            "content_hash": spec.content_hash,
            "package_path": str(spec.path),
        }

    def read_reference(self, skill_name: str, reference: str) -> str:
        spec = self._get(skill_name)
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Skill reference must stay inside references/")
        path = (spec.path / "references" / relative).resolve()
        try:
            path.relative_to((spec.path / "references").resolve())
        except ValueError as exc:
            raise ValueError("Skill reference escapes package") from exc
        if not path.is_file() or path.stat().st_size > self.max_skill_bytes:
            raise ValueError(f"Skill reference unavailable: {reference}")
        return path.read_text(encoding="utf-8")

    def _get(self, name: str) -> AgentSkillSpec:
        if not self._skills:
            self.discover()
        try:
            return self._skills[name]
        except KeyError as exc:
            raise KeyError(f"unknown Agent Skill: {name}") from exc


def _read_frontmatter(path: Path, *, max_bytes: int) -> dict[str, Any]:
    lines: list[str] = []
    bytes_read = 0
    closing_delimiter_found = False
    with path.open("rb") as handle:
        for raw_line in handle:
            bytes_read += len(raw_line)
            if bytes_read > max_bytes:
                raise ValueError(f"SKILL.md frontmatter exceeds limit: {path}")
            line = raw_line.decode("utf-8")
            lines.append(line)
            if line.strip() == "---" and len(lines) > 1:
                closing_delimiter_found = True
                break
    if not closing_delimiter_found:
        raise ValueError(f"SKILL.md frontmatter is required: {path}")
    text = "".join(lines)
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"SKILL.md frontmatter is required: {path}")
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for Agent Skill discovery") from exc
    value = yaml.safe_load(match.group(1)) or {}
    if not isinstance(value, dict):
        raise ValueError(f"SKILL.md frontmatter must be an object: {path}")
    return value
