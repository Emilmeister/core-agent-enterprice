from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import CoreError


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str | None = None
    digest: str | None = None


class SkillResolver:
    def __init__(self, declarations):
        self._declarations = {item["name"]: dict(item) for item in declarations}
        self._snapshots = {}
        self._loaded = []

    @property
    def loaded_resources(self):
        return tuple(self._loaded)

    def _root(self, name):
        try:
            source = self._declarations[name]["source"]
        except KeyError:
            raise CoreError("SKILL_INVALID") from None
        parsed = urlparse(source)
        if parsed.scheme != "file":
            raise CoreError("SKILL_INVALID")
        return Path(unquote(parsed.path)).resolve()

    def _parse(self, name):
        path = self._root(name) / "SKILL.md"
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except OSError:
            raise CoreError("SKILL_INVALID") from None
        expected_digest = self._declarations[name].get("digest")
        actual_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if expected_digest and expected_digest != actual_digest:
            raise CoreError("SKILL_INVALID")
        lines = content.splitlines()
        if len(lines) < 4 or lines[0] != "---" or "---" not in lines[1:]:
            raise CoreError("SKILL_INVALID")
        end = lines[1:].index("---") + 1
        metadata = {}
        for line in lines[1:end]:
            if ":" not in line:
                raise CoreError("SKILL_INVALID")
            key, value = line.split(":", 1)
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                value = [
                    part.strip() for part in value[1:-1].split(",") if part.strip()
                ]
            metadata[key.strip()] = value
        if metadata.get("name") != name or not metadata.get("description"):
            raise CoreError("SKILL_INVALID")
        instructions = "\n".join(lines[end + 1 :]).lstrip() + (
            "\n" if content.endswith("\n") else ""
        )
        return metadata, instructions, content

    def discover(self):
        result = []
        for name in self._declarations:
            metadata, _, _ = self._parse(name)
            result.append(Skill(name, metadata["description"]))
        return result

    def activate(self, name):
        if name not in self._snapshots:
            metadata, instructions, content = self._parse(name)
            self._snapshots[name] = Skill(
                name,
                metadata["description"],
                instructions,
                hashlib.sha256(content.encode()).hexdigest(),
            )
            self._loaded.append(f"{name}/SKILL.md")
        return self._snapshots[name]

    def read_resource(self, name, relative):
        root = self._root(name)
        path = (root / relative).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            raise CoreError("POLICY_DENIED") from None
        try:
            value = path.read_text(encoding="utf-8")
        except OSError:
            raise CoreError("SKILL_INVALID") from None
        resource = f"{name}/{relative}"
        if resource not in self._loaded:
            self._loaded.append(resource)
        return value

    def for_child(self, allowed):
        return SkillResolver(
            [self._declarations[name] for name in self._declarations if name in allowed]
        )

    def resolve_lock(self):
        graph = {}
        for name in self._declarations:
            metadata, _, content = self._parse(name)
            graph[name] = metadata.get("dependencies", [])
        visiting = set()
        visited = set()

        def visit(name):
            if name in visiting:
                raise CoreError("SKILL_INVALID")
            if name in visited:
                return
            visiting.add(name)
            for dependency in graph.get(name, []):
                if dependency not in graph:
                    raise CoreError("SKILL_INVALID")
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in graph:
            visit(name)
        return tuple(sorted(visited))
