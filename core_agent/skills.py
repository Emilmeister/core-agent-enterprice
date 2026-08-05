from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from .errors import CoreError

MAX_SKILL_RESOURCE_BYTES = 64 * 1024
SKILL_ACTIVATE_TOOL = "core_skill_activate"
SKILL_RESOURCE_TOOL = "core_skill_read_resource"
SKILL_TOOLS = frozenset({SKILL_ACTIVATE_TOOL, SKILL_RESOURCE_TOOL})


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str | None = None
    digest: str | None = None
    resources: tuple[str, ...] = ()


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
        path = Path(unquote(parsed.path))
        try:
            if path.is_symlink() or not path.is_dir():
                raise CoreError("SKILL_INVALID")
            return path.resolve(strict=True)
        except OSError:
            raise CoreError("SKILL_INVALID") from None

    @staticmethod
    def _valid_digest(value):
        if not isinstance(value, str) or not value.startswith("sha256:"):
            return False
        digest = value.removeprefix("sha256:")
        return len(digest) == 64 and all(
            character in "0123456789abcdef" for character in digest
        )

    def verify_lock(self):
        for name, declaration in self._declarations.items():
            expected = declaration.get("resources")
            if not self._valid_digest(declaration.get("digest")) or not isinstance(
                expected, dict
            ):
                raise CoreError("SKILL_INVALID")
            root = self._root(name)
            actual = {}
            try:
                for path in root.rglob("*"):
                    if path.is_symlink():
                        raise CoreError("SKILL_INVALID")
                    if not path.is_file():
                        continue
                    relative = path.relative_to(root).as_posix()
                    if relative == "SKILL.md":
                        continue
                    with path.open("rb") as source:
                        actual[relative] = (
                            "sha256:"
                            + hashlib.file_digest(source, "sha256").hexdigest()
                        )
            except OSError:
                raise CoreError("SKILL_INVALID") from None
            if any(not self._valid_digest(value) for value in expected.values()):
                raise CoreError("SKILL_INVALID")
            if actual != expected:
                raise CoreError("SKILL_INVALID")
            self._parse(name)
        return tuple(sorted(self._declarations))

    def _parse(self, name):
        path = self._root(name) / "SKILL.md"
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
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
        if not instructions.strip():
            raise CoreError("SKILL_INVALID")
        return metadata, instructions, content

    def _resource_names(self, name):
        declared = self._declarations[name].get("resources")
        if declared is not None:
            return tuple(sorted(declared))
        root = self._root(name)
        resources = []
        try:
            paths = root.rglob("*")
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                if relative != "SKILL.md":
                    resources.append(relative)
        except OSError:
            raise CoreError("SKILL_INVALID") from None
        return tuple(sorted(resources))

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
                self._resource_names(name),
            )
            self._loaded.append(f"{name}/SKILL.md")
        return self._snapshots[name]

    def list_resources(self, name):
        try:
            return self._snapshots[name].resources
        except KeyError:
            raise CoreError("CAPABILITY_DISABLED") from None

    def read_resource(self, name, relative, *, max_bytes=MAX_SKILL_RESOURCE_BYTES):
        if name not in self._snapshots:
            raise CoreError("CAPABILITY_DISABLED")
        if (
            not isinstance(relative, str)
            or not relative
            or "\x00" in relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or relative == "SKILL.md"
        ):
            raise CoreError("POLICY_DENIED")
        root = self._root(name)
        candidate = root
        for part in Path(relative).parts:
            candidate /= part
            if candidate.is_symlink():
                raise CoreError("POLICY_DENIED")
        path = candidate.resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            raise CoreError("POLICY_DENIED") from None
        try:
            with path.open("rb") as source:
                raw = source.read(max_bytes + 1)
        except FileNotFoundError:
            raise CoreError("SKILL_RESOURCE_MISSING") from None
        except (IsADirectoryError, OSError):
            raise CoreError("SKILL_RESOURCE_INVALID") from None
        if len(raw) > max_bytes:
            raise CoreError("SKILL_RESOURCE_INVALID")
        expected = self._declarations[name].get("resources")
        if expected is not None:
            digest = "sha256:" + hashlib.sha256(raw).hexdigest()
            if expected.get(relative) != digest:
                raise CoreError("SKILL_INVALID")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CoreError("SKILL_RESOURCE_INVALID") from None
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
