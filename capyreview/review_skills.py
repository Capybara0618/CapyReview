"""Formal Agent Skills discovery, validation and domain-scoped discovery."""
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, Iterable, Tuple

import yaml


SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class ReviewSkillMetadata:
    name: str
    description: str
    domains: Tuple[str, ...]
    version: int = 1


@dataclass(frozen=True)
class ActivatedReviewSkill:
    metadata: ReviewSkillMetadata
    body: str
    references: Tuple[str, ...]

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def version(self) -> int:
        return self.metadata.version


class ReviewSkillRegistry:
    """Load metadata eagerly and Skill bodies/resources only after activation."""

    def __init__(self, root, packages=()):
        self.root = Path(root).resolve()
        self.packages: Dict[str, dict] = {}
        for value in packages:
            package = self._normalize_package(value)
            self.packages[package["name"]] = package

    def discover(self) -> list[ReviewSkillMetadata]:
        values = []
        if self.root.is_dir():
            for package in sorted(self.root.iterdir()):
                if package.is_dir() and not package.name.startswith("."):
                    metadata, _body = self._parse(package)
                    values.append(metadata)
        by_name = {item.name: item for item in values}
        for package in self.packages.values():
            metadata, _body = self._parse_text(
                package["skill_md"], package["name"], package["version"]
            )
            by_name[metadata.name] = metadata
        return [by_name[name] for name in sorted(by_name)]

    def activate(self, name: str) -> ActivatedReviewSkill:
        if name in self.packages:
            package = self.packages[name]
            metadata, body = self._parse_text(
                package["skill_md"], name, package["version"]
            )
            references = self._declared_references(body)
            for relative in references:
                if relative not in package["references"]:
                    raise ValueError(
                        "skill reference does not exist inside the package: %s"
                        % relative
                    )
            return ActivatedReviewSkill(metadata, body, references)
        package = self._package(name)
        metadata, body = self._parse(package)
        references = self._declared_references(body)
        for relative in references:
            path = (package / relative).resolve()
            if not path.is_file() or package not in path.parents:
                raise ValueError(
                    "skill reference does not exist inside the package: %s" % relative
                )
        return ActivatedReviewSkill(metadata, body, references)

    def export_package(self, name: str) -> dict:
        """Return one complete formal package for versioning or evolution."""
        if name in self.packages:
            package = self.packages[name]
            return {
                "name": package["name"],
                "skill_md": package["skill_md"],
                "references": dict(package["references"]),
            }
        directory = self._package(name)
        activated = self.activate(name)
        return {
            "name": name,
            "skill_md": (directory / "SKILL.md").read_text(encoding="utf-8-sig"),
            "references": {
                relative: (directory / relative).read_text(encoding="utf-8")
                for relative in activated.references
            },
        }

    def read_reference(self, name: str, relative_path: str) -> str:
        activated = self.activate(name)
        relative = self._safe_reference(relative_path)
        normalized = relative.as_posix()
        if normalized not in activated.references:
            raise ValueError("reference is not declared by the activated skill")
        if name in self.packages:
            return self.packages[name]["references"][normalized]
        return (self._package(name) / relative).read_text(encoding="utf-8")

    def _package(self, name: str) -> Path:
        value = str(name).strip()
        if not self._valid_name(value):
            raise ValueError("invalid skill name")
        package = (self.root / value).resolve()
        if package.parent != self.root or not package.is_dir():
            raise ValueError("skill package was not found")
        return package

    def _parse(self, package: Path) -> tuple[ReviewSkillMetadata, str]:
        path = package / "SKILL.md"
        if not path.is_file():
            raise ValueError("skill package must contain SKILL.md")
        text = path.read_text(encoding="utf-8-sig")
        return self._parse_text(text, package.name, 1)

    def _parse_text(
        self, text: str, expected_name: str, version: int,
    ) -> tuple[ReviewSkillMetadata, str]:
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise ValueError("SKILL.md must contain YAML frontmatter")
        frontmatter, body = text[4:].split("\n---\n", 1)
        data = yaml.safe_load(frontmatter)
        if not isinstance(data, dict):
            raise ValueError("SKILL.md frontmatter must be an object")
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        if not self._valid_name(name):
            raise ValueError("invalid SKILL.md name")
        if name != expected_name:
            raise ValueError("SKILL.md name must match its directory")
        if not description or len(description) > 1024:
            raise ValueError("SKILL.md description must contain 1 to 1024 characters")
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in metadata.items()
        ):
            raise ValueError("SKILL.md metadata values must be strings")
        domains = self._words(metadata.get("capyreview-domains", ""))
        if not domains:
            raise ValueError("review skills require domain metadata")
        return ReviewSkillMetadata(
            name, description, domains, int(version)
        ), body.strip()

    def _normalize_package(self, value: dict) -> dict:
        if not isinstance(value, dict) or not isinstance(value.get("skill_md"), str):
            raise ValueError("persisted skill package requires skill_md")
        name = str(value.get("name", "")).strip()
        try:
            version = int(value.get("version", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError("persisted skill package version must be an integer") from exc
        references = value.get("references") or {}
        if not self._valid_name(name) or version < 1 or not isinstance(references, dict):
            raise ValueError("invalid persisted skill package")
        normalized_references = {}
        for path, content in references.items():
            relative = self._safe_reference(str(path)).as_posix()
            if not isinstance(content, str):
                raise ValueError("skill reference content must be text")
            normalized_references[relative] = content
        self._parse_text(value["skill_md"], name, version)
        return {
            "name": name, "version": version,
            "skill_md": value["skill_md"],
            "references": normalized_references,
        }

    def _declared_references(self, body: str) -> Tuple[str, ...]:
        references = []
        for target in MARKDOWN_LINK.findall(body):
            target = target.strip().split("#", 1)[0]
            if not target or "://" in target:
                continue
            references.append(self._safe_reference(target).as_posix())
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _valid_name(name: str) -> bool:
        return bool(1 <= len(name) <= 64 and SKILL_NAME.fullmatch(name))

    @staticmethod
    def _words(value: str) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(
            item.lower() for item in re.split(r"[\s,]+", str(value).strip()) if item
        ))

    @staticmethod
    def _safe_reference(value: str) -> Path:
        normalized = str(value).replace("\\", "/").strip()
        parts = Path(normalized).parts
        if (
            not normalized.startswith("references/")
            or not parts or any(part in {"", ".", ".."} for part in parts)
            or Path(normalized).is_absolute()
        ):
            raise ValueError("skill reference must stay inside references/")
        return Path(normalized)


class ReviewSkillSelector:
    """Expose the skills allowed for a reviewer domain.

    Semantic selection belongs to the reviewer agent.  The host keeps only the
    trusted domain boundary here so a reviewer cannot load an unrelated Skill.
    """

    def select(
        self, skills: Iterable[ReviewSkillMetadata], domains=(), paths=(), diff: str = "",
    ) -> list[ReviewSkillMetadata]:
        selected_domains = {str(item).strip().lower() for item in domains}
        return sorted(
            (
                skill for skill in skills
                if selected_domains.intersection(skill.domains)
            ),
            key=lambda skill: skill.name,
        )
