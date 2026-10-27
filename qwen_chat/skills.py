"""Reusable, per-user Agent Skills packages; loading never executes their code."""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat
from typing import Callable

from . import config

BUNDLED_SKILLS = Path(__file__).with_name("data") / "skills"
MAX_SKILL_BYTES = 16_000
MAX_PACKAGE_BYTES = 10 * 1024 * 1024
MAX_PACKAGE_FILES = 64
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SLASH = re.compile(r"(?<!\S)/([A-Za-z0-9-]*)\Z")


class SkillError(ValueError):
    pass


def valid_name(name) -> bool:
    return isinstance(name, str) and 1 <= len(name) <= 64 and bool(_NAME.fullmatch(name))


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    instructions: str
    builtin: bool = False
    compatibility: str = ""


@dataclass(frozen=True)
class SkillPackage:
    skill: Skill
    files: dict[str, bytes]

    @property
    def digest(self) -> str:
        digest = hashlib.sha256()
        for name, data in sorted(self.files.items()):
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(data).digest())
        return digest.hexdigest()


def parse_skill(raw: bytes, directory: Path, builtin: bool = False) -> Skill:
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillError(f"SKILL.md is too long ({MAX_SKILL_BYTES:,} bytes maximum). "
                         "Move detailed material into references/.")
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise SkillError("SKILL.md must be UTF-8 text.") from None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md needs YAML frontmatter with name and description.")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise SkillError("The YAML frontmatter needs a closing --- line.")
    try:
        import yaml
    except ImportError:
        raise SkillError("Skill metadata needs PyYAML (Debian package python3-yaml).") from None
    try:
        metadata = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise SkillError(f"Invalid skill metadata: {exc}") from None
    if not isinstance(metadata, dict):
        raise SkillError("Skill metadata must contain name and description fields.")
    name, description = metadata.get("name"), metadata.get("description")
    if not valid_name(name) or directory.name != name:
        raise SkillError("The skill name must match its folder: 1-64 lowercase letters, "
                         "numbers and single hyphens, without a leading or trailing hyphen.")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
        raise SkillError("The skill description must be 1-1024 characters and say when to use it.")
    compatibility = metadata.get("compatibility", "")
    if not isinstance(compatibility, str) or len(compatibility) > 500:
        raise SkillError("compatibility must be a short string (at most 500 characters).")
    instructions = "".join(lines[end + 1:]).strip()
    if not instructions:
        raise SkillError("Add reusable instructions below the frontmatter.")
    return Skill(name, " ".join(description.split()), directory, instructions,
                 builtin, compatibility)


def _read_regular(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise SkillError(f"Only regular files are allowed in skills: {path.name}.")
    with path.open("rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise SkillError(f"Skill file is too large: {path.name}.")
    return data


def package_files(directory: Path) -> dict[str, bytes]:
    """Take a bounded snapshot so approval applies to exactly the bytes installed."""
    if directory.is_symlink() or not directory.is_dir():
        raise SkillError("A skill must be a self-contained folder, not a file or symlink.")
    files: dict[str, bytes] = {}
    total = 0

    def on_error(error):
        raise error

    for top, dirs, names in os.walk(directory, followlinks=False, onerror=on_error):
        for name in sorted(dirs + names):
            path = Path(top) / name
            if name.startswith(".") or name == "__pycache__" or path.is_symlink():
                raise SkillError(f"Do not package hidden files, caches or symlinks: "
                                 f"{path.relative_to(directory)}.")
            if path.is_dir():
                continue
            if len(files) >= MAX_PACKAGE_FILES:
                raise SkillError(f"A skill may contain at most {MAX_PACKAGE_FILES} files.")
            data = _read_regular(path, MAX_PACKAGE_BYTES - total)
            total += len(data)
            files[path.relative_to(directory).as_posix()] = data
    if "SKILL.md" not in files:
        raise SkillError("The skill folder needs a SKILL.md file.")
    return files


def write_package(files: dict[str, bytes], destination: Path) -> None:
    destination.mkdir(mode=0o700)
    for relative, data in files.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as fh:
            fh.write(data)
        path.chmod(0o600)


def library_root() -> Path:
    """The shared library, kept apart from the chats. Moves skills saved beside the chats
    by older versions; anything that can't be moved stays where it was."""
    root, old = config.HARNESS_DIR / "skills", config.DATA_DIR / "skills"
    if old.is_dir() and not old.is_symlink():
        with suppress(OSError):
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for entry in old.iterdir():
                if not entry.name.startswith(".") and not os.path.lexists(root / entry.name):
                    with suppress(OSError):
                        os.rename(entry, root / entry.name)
            old.rmdir()
    return root


class SkillRegistry:
    def __init__(self, user_root: Path | None = None, bundled_root: Path | None = None):
        self.user_root = (Path(user_root) if user_root is not None else library_root()).resolve()
        self.bundled_root = Path(bundled_root) if bundled_root is not None else BUNDLED_SKILLS

    @staticmethod
    def _read(directory: Path, builtin: bool) -> Skill:
        if directory.is_symlink() or not directory.is_dir():
            raise SkillError("A skill must be a folder, not a file or symlink.")
        return parse_skill(_read_regular(directory / "SKILL.md", MAX_SKILL_BYTES), directory, builtin)

    def catalog(self, query: str = "") -> tuple[list[Skill], list[str]]:
        found: dict[str, Skill] = {}
        errors = []
        for root, builtin in ((self.bundled_root, True), (self.user_root, False)):
            if not root.exists():
                continue
            try:
                entries = sorted(root.iterdir())
            except OSError as exc:
                errors.append(f"Cannot read the skill library: {exc}")
                continue
            for directory in entries:
                if directory.name.startswith("."):
                    continue
                try:
                    skill = self._read(directory, builtin)
                    if skill.name in found:
                        raise SkillError("This name belongs to a built-in skill; use another name.")
                    found[skill.name] = skill
                except (OSError, SkillError) as exc:
                    errors.append(f"{directory.name}: {exc}")
        query = query.casefold().strip()
        skills = [s for s in found.values() if query in (s.name + " " + s.description).casefold()]
        skills.sort(key=lambda s: (not s.name.startswith(query), s.name))
        return skills, errors

    def get(self, name: str) -> Skill:
        if not valid_name(name):
            raise SkillError("Unknown skill name. Use list_skills to see the available skills.")
        for root, builtin in ((self.bundled_root, True), (self.user_root, False)):
            path = root / name
            if os.path.lexists(path):
                return self._read(path, builtin)
        raise SkillError(f"Unknown skill '{name}'. Use list_skills to discover others.")

    def package(self, directory: Path, builtin: bool = False) -> SkillPackage:
        files = package_files(directory)
        return SkillPackage(parse_skill(files["SKILL.md"], directory, builtin), files)

    def destination(self, name: str) -> Path:
        if not valid_name(name):
            raise SkillError("Invalid skill name.")
        if os.path.lexists(self.bundled_root / name):
            raise SkillError("Built-in skills cannot be replaced. Give this skill a new name.")
        path = self.user_root / name
        if path.is_symlink():
            raise SkillError("The saved skill folder cannot be a symlink.")
        return path

    def version(self, name: str) -> str | None:
        path = self.destination(name)
        return self.package(path).digest if path.exists() else None

    def install(self, package: SkillPackage, previous: str | None,
                backup: Callable[[Path], Path]) -> Path | None:
        import tempfile
        target = self.destination(package.skill.name)
        self.user_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix=".saving-", dir=self.user_root) as scratch:
            staged = Path(scratch) / package.skill.name
            write_package(package.files, staged)
            if self.version(package.skill.name) != previous:
                raise SkillError("The saved skill changed while you were reviewing it. "
                                 "Nothing was replaced; review it again.")
            old = backup(target) if target.exists() else None
            try:
                os.replace(staged, target)
            except OSError:
                # The backup stays recoverable even if a filesystem failure prevents rollback.
                if old is not None and not target.exists():
                    try:
                        import shutil
                        shutil.copytree(old, target)
                    except OSError as restore_error:
                        raise SkillError(f"Saving failed and the old skill is in the Trash at {old}; "
                                         f"restoring it failed: {restore_error}") from None
                raise
        return old


def slash_query(text: str, cursor: int) -> tuple[int, int, str] | None:
    """A completion token at the caret, not a URL, file path, or fenced code."""
    prefix = text[:cursor]
    if prefix.count("```") % 2:
        return None
    match = _SLASH.search(prefix)
    if not match or len(match[1]) > 64:
        return None
    rest = re.match(r"[A-Za-z0-9-]*", text[cursor:])[0]
    end = cursor + len(rest)
    if text[end:end + 1] in ("/", ".", ":"):
        return None
    return match.start(), end, match[1].casefold()
