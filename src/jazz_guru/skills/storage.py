"""Skill storage on disk.

A skill is a directory ``workspace/skills/<category>/<name>/`` containing a
required ``SKILL.md`` and zero or more adjunct files in
``references/`` / ``templates/`` / ``scripts/`` / ``assets/``.

``SKILL.md`` opens with a YAML frontmatter block (``---`` delimited) holding
machine-readable metadata; the rest is freeform markdown the agent will use
as instruction.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jazz_guru.actions.dynamic import ToolValidationError, validate_name
from jazz_guru.config import get_settings

SKILLS_DIR_NAME = "skills"
SKILL_FILE = "SKILL.md"
ADJUNCT_DIRS: tuple[str, ...] = ("references", "templates", "scripts", "assets")

VALID_CATEGORY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<fm>.*?\n)---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)


class SkillsError(ValueError):
    """Raised when a skill operation is rejected (bad path / metadata / size)."""


@dataclass
class SkillFrontmatter:
    name: str
    description: str = ""
    version: str = "1.0.0"
    category: str = "general"
    tags: list[str] = field(default_factory=list)
    requires_tools: list[str] = field(default_factory=list)
    fallback_when_tools: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    def render(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "category": self.category,
        }
        if self.tags:
            out["tags"] = list(self.tags)
        if self.requires_tools:
            out["requires_tools"] = list(self.requires_tools)
        if self.fallback_when_tools:
            out["fallback_when_tools"] = list(self.fallback_when_tools)
        for k, v in self.extras.items():
            if k not in out:
                out[k] = v
        return out


@dataclass
class Skill:
    frontmatter: SkillFrontmatter
    body: str
    sha256: str
    path: Path  # path to SKILL.md

    @property
    def name(self) -> str:
        return self.frontmatter.name

    @property
    def category(self) -> str:
        return self.frontmatter.category

    @property
    def directory(self) -> Path:
        return self.path.parent

    def metadata(self) -> dict[str, Any]:
        return {
            **self.frontmatter.render(),
            "path": str(self.path),
            "sha256": self.sha256,
        }


def skills_root() -> Path:
    p = get_settings().jg_workspace_dir / SKILLS_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _safe_skill_dir(category: str, name: str) -> Path:
    if not VALID_CATEGORY.match(category):
        raise SkillsError(
            f"invalid category {category!r}: must match {VALID_CATEGORY.pattern}"
        )
    try:
        nm = validate_name(name)
    except ToolValidationError as e:
        # Reuse the same name validator the dynamic-tool system uses so the
        # rules are consistent across the codebase.
        raise SkillsError(f"invalid skill name: {e}") from e
    base = skills_root().resolve()
    target = (base / category / nm).resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise SkillsError(f"skill path {target} escapes {base}") from e
    return target


def _safe_subpath(skill_dir: Path, relpath: str) -> Path:
    """Resolve a relative path under ``skill_dir`` (for adjunct files)."""
    if not relpath or relpath.strip() in ("", "."):
        raise SkillsError("relpath must be non-empty")
    target = (skill_dir / relpath).resolve()
    base = skill_dir.resolve()
    try:
        target.relative_to(base)
    except ValueError as e:
        raise SkillsError(f"path {relpath!r} escapes the skill directory") from e
    return target


def parse_skill_md(text: str) -> tuple[SkillFrontmatter, str]:
    """Parse YAML frontmatter + body out of a SKILL.md string."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillsError("SKILL.md must begin with a YAML frontmatter block (---)")
    try:
        data = yaml.safe_load(m.group("fm")) or {}
    except yaml.YAMLError as e:
        raise SkillsError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(data, dict):
        raise SkillsError("frontmatter must be a YAML mapping")
    name = str(data.get("name") or "").strip()
    if not name:
        raise SkillsError("frontmatter must include 'name'")
    try:
        validate_name(name)
    except ToolValidationError as e:
        raise SkillsError(f"invalid skill name in frontmatter: {e}") from e
    known = {
        "name",
        "description",
        "version",
        "category",
        "tags",
        "requires_tools",
        "fallback_when_tools",
    }
    extras = {k: v for k, v in data.items() if k not in known}

    def _as_str_list(key: str) -> list[str]:
        raw = data.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise SkillsError(
                f"frontmatter {key!r} must be a YAML list (got {type(raw).__name__})"
            )
        return [str(x).strip() for x in raw if str(x).strip()]

    fm = SkillFrontmatter(
        name=name,
        description=str(data.get("description") or "").strip(),
        version=str(data.get("version") or "1.0.0"),
        category=str(data.get("category") or "general").strip(),
        tags=_as_str_list("tags"),
        requires_tools=_as_str_list("requires_tools"),
        fallback_when_tools=_as_str_list("fallback_when_tools"),
        extras=extras,
    )
    if not VALID_CATEGORY.match(fm.category):
        raise SkillsError(
            f"invalid category {fm.category!r}: must match {VALID_CATEGORY.pattern}"
        )
    return fm, m.group("body").strip("\n") + ("\n" if m.group("body").strip() else "")


def render_skill_md(fm: SkillFrontmatter, body: str) -> str:
    """Render frontmatter + body back into SKILL.md text."""
    fm_yaml = yaml.safe_dump(fm.render(), sort_keys=False).strip()
    body_clean = body.strip("\n")
    return f"---\n{fm_yaml}\n---\n{body_clean}\n"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    fm, body = parse_skill_md(text)
    return Skill(frontmatter=fm, body=body, sha256=_hash(text), path=path)


def load_all_skills() -> list[Skill]:
    """Walk ``workspace/skills/`` and return every parseable skill."""
    out: list[Skill] = []
    root = skills_root()
    for skill_md in sorted(root.glob("*/*/" + SKILL_FILE)):
        try:
            out.append(load_skill(skill_md))
        except (SkillsError, OSError):
            # Best-effort: a malformed skill shouldn't break the whole list.
            continue
    return out


def list_skills_metadata(
    *,
    allowed_tools: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return metadata for active skills.

    If ``allowed_tools`` is provided, each skill's ``requires_tools`` and
    ``fallback_when_tools`` are evaluated against it to decide whether the
    skill is shown.
    """
    from jazz_guru.skills.conditional import is_skill_active  # local to avoid cycles

    items: list[dict[str, Any]] = []
    for skill in load_all_skills():
        if allowed_tools is not None and not is_skill_active(skill, allowed_tools):
            continue
        items.append(skill.metadata())
    return items


def skills_metadata_block(metadata: list[dict[str, Any]] | None = None) -> str:
    """Render a compact metadata block for the system prompt.

    Format is intentionally terse: ``- <category>/<name>: <description>``.
    The agent expands a skill it wants with ``skill_view(name)``.
    """
    if metadata is None:
        metadata = list_skills_metadata()
    if not metadata:
        return ""
    lines = ["\n---\n## Skills available (progressive disclosure)"]
    lines.append(
        "Use `skill_view(name)` to read the full content of any skill below. "
        "Use `skill_manage` to add or update skills based on what you learn."
    )
    for m in metadata:
        tags = m.get("tags") or []
        tag_str = f"  [tags: {', '.join(tags)}]" if tags else ""
        desc = (m.get("description") or "").strip() or "(no description)"
        lines.append(f"- {m['category']}/{m['name']}: {desc}{tag_str}")
    return "\n".join(lines)


def write_skill(
    category: str,
    name: str,
    *,
    description: str,
    body: str,
    version: str = "1.0.0",
    tags: list[str] | None = None,
    requires_tools: list[str] | None = None,
    fallback_when_tools: list[str] | None = None,
    extras: dict[str, Any] | None = None,
    overwrite: bool = True,
) -> Skill:
    """Create or replace a skill (full SKILL.md write)."""
    skill_dir = _safe_skill_dir(category, name)
    if skill_dir.exists() and not overwrite:
        raise SkillsError(f"skill {category}/{name} already exists")
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = SkillFrontmatter(
        name=name,
        description=description,
        version=version,
        category=category,
        tags=tags or [],
        requires_tools=requires_tools or [],
        fallback_when_tools=fallback_when_tools or [],
        extras=extras or {},
    )
    md = render_skill_md(fm, body)
    skill_md = skill_dir / SKILL_FILE
    skill_md.write_text(md, encoding="utf-8")
    return load_skill(skill_md)


def patch_skill_body(category: str, name: str, find: str, replace: str) -> Skill:
    """Apply an exact find-and-replace to a skill's body (frontmatter untouched)."""
    skill_dir = _safe_skill_dir(category, name)
    skill_md = skill_dir / SKILL_FILE
    if not skill_md.exists():
        raise SkillsError(f"skill {category}/{name} does not exist")
    text = skill_md.read_text(encoding="utf-8")
    fm, body = parse_skill_md(text)
    if not find:
        raise SkillsError("find string must not be empty")
    count = body.count(find)
    if count == 0:
        raise SkillsError("find string not present in skill body")
    if count > 1:
        raise SkillsError(
            f"find string occurs {count} times in skill body; make it unique"
        )
    new_body = body.replace(find, replace, 1)
    skill_md.write_text(render_skill_md(fm, new_body), encoding="utf-8")
    return load_skill(skill_md)


def delete_skill(category: str, name: str) -> bool:
    """Delete a skill directory recursively. Returns True if anything was removed."""
    skill_dir = _safe_skill_dir(category, name)
    if not skill_dir.exists():
        return False
    # Recursive rmtree, but only inside the workspace.
    import shutil

    shutil.rmtree(skill_dir)
    # Clean up the now-empty category dir to keep the tree tidy.
    parent = skill_dir.parent
    try:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return True


def _normalize_adjunct_relpath(relpath: str) -> str:
    """Restrict adjunct file operations to the standard subdirectories.

    Without this, ``read/remove`` could touch ``SKILL.md`` itself or anything
    else under the skill dir, breaking the skill's frontmatter contract via
    the adjunct API surface.
    """
    rel = relpath.strip().lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if len(parts) < 2:
        raise SkillsError(
            "relpath must include a filename under one of "
            f"{ADJUNCT_DIRS} (e.g. references/foo.md)"
        )
    top = parts[0]
    if top not in ADJUNCT_DIRS:
        raise SkillsError(
            f"relpath must start with one of {ADJUNCT_DIRS} (got {top!r})"
        )
    return "/".join(parts)


def write_skill_file(
    category: str, name: str, relpath: str, content: str
) -> dict[str, Any]:
    """Write an adjunct file under the skill's directory.

    ``relpath`` must point inside one of the standard subdirectories
    (references/, templates/, scripts/, assets/).
    """
    skill_dir = _safe_skill_dir(category, name)
    if not skill_dir.exists():
        raise SkillsError(f"skill {category}/{name} does not exist")
    rel = _normalize_adjunct_relpath(relpath)
    target = _safe_subpath(skill_dir, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": str(target), "bytes": len(content.encode("utf-8"))}


def remove_skill_file(category: str, name: str, relpath: str) -> bool:
    skill_dir = _safe_skill_dir(category, name)
    if not skill_dir.exists():
        return False
    rel = _normalize_adjunct_relpath(relpath)
    target = _safe_subpath(skill_dir, rel)
    if not target.exists():
        return False
    if target.is_dir():
        raise SkillsError("relpath points to a directory; refuse to recursive-delete")
    target.unlink()
    return True


def read_skill_file(category: str, name: str, relpath: str) -> str:
    skill_dir = _safe_skill_dir(category, name)
    if not skill_dir.exists():
        raise SkillsError(f"skill {category}/{name} does not exist")
    rel = _normalize_adjunct_relpath(relpath)
    target = _safe_subpath(skill_dir, rel)
    if not target.exists() or not target.is_file():
        raise SkillsError(f"file {relpath} not found in skill {category}/{name}")
    return target.read_text(encoding="utf-8")
