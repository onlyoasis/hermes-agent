"""Skill-catalog compilation for the jev-skill-router plugin.

Compiles the candidate set the router may consider from the ACTIVE
profile's loadable skills — the same sources, filters, and precedence
``tools/skills_tool._find_all_skills`` uses (local ``HERMES_HOME/skills``
first, then configured ``skills.external_dirs``), minus everything the
host would refuse to load:

* skills disabled globally or for the resolved platform
  (``skills.disabled`` / ``skills.platform_disabled``),
* skills whose ``platforms`` frontmatter excludes this OS,
* skills whose runtime environment doesn't match.

Deliberately imports only :mod:`agent.skill_utils` (documented as
import-light — no tool registry, no CLI config chain) so registering the
plugin never pulls heavy modules into every host process.

Outbound minimisation (design doc §3.2): the vendor may only ever see
``skill_id`` / ``name`` / ``description`` for loadable skills, and only
for those in the configured outbound catalog subset. Skill BODIES are
never sent unless the skill id is explicitly listed in
``outbound_body_skills`` — private skill bodies do not leave the machine
without explicit allowance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set

from agent.skill_utils import (
    get_disabled_skill_names,
    get_external_skills_dirs,
    iter_skill_index_files,
    parse_frontmatter,
    skill_matches_environment,
    skill_matches_platform,
)

logger = logging.getLogger(__name__)

# Same metadata budget the host's skill scan reads. Enough for frontmatter
# plus the description-fallback first lines, without shipping whole bodies
# into memory on every turn.
_METADATA_READ_CHARS = 4000

# Frontmatter field caps mirrored from tools/skills_tool so candidate ids
# and descriptions match what skills_list would show the main agent.
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024


@dataclass(frozen=True)
class CatalogEntry:
    """One outbound-safe candidate. ``skill_md`` never leaves the host."""

    skill_id: str
    name: str
    description: str
    category: Optional[str]
    skill_md: Path

    def detail_text(self, body_excerpt: str = "") -> str:
        """Phase-2 evidence text: full description, plus body excerpt when
        the caller (which enforces the outbound-body allowlist) provides
        one. The excerpt is already truncated by the caller."""
        if body_excerpt:
            joined = " — ".join(p for p in (self.description, body_excerpt) if p)
            return joined
        return self.description


@dataclass
class CompiledCatalog:
    """Result of one catalog compilation."""

    # Ordered by (category, skill_id) for deterministic Choice criteria.
    entries: List[CatalogEntry] = field(default_factory=list)
    # False when any scan root failed mid-walk. An incomplete catalog can
    # never back an "abstain" conclusion (a missing dir might hold the
    # perfect skill), so the router must report ``unavailable`` instead.
    complete: bool = True
    # Human-readable note for the audit record when incomplete.
    scan_error: str = ""

    def ids(self) -> List[str]:
        return [e.skill_id for e in self.entries]

    def find(self, skill_id: str) -> Optional[CatalogEntry]:
        for e in self.entries:
            if e.skill_id == skill_id:
                return e
        return None


def _category_for(skill_md: Path, scan_roots: List[Path]) -> Optional[str]:
    """Mirror skills_tool._get_category_from_path for the dirs we scanned."""
    for root in scan_roots:
        try:
            parts = skill_md.relative_to(root).parts
        except ValueError:
            continue
        if len(parts) >= 3:
            return parts[0]
    return None


def _parse_entry(skill_md: Path, disabled: Set[str], scan_roots: List[Path]) -> Optional[CatalogEntry]:
    """Parse one SKILL.md into a CatalogEntry, applying loadability filters.

    Returns None when the skill should not be a candidate (unreadable,
    platform/env mismatch, disabled, or a duplicate id the caller filters).
    """
    try:
        content = skill_md.read_text(encoding="utf-8")[:_METADATA_READ_CHARS]
    except (UnicodeDecodeError, PermissionError, OSError) as exc:
        logger.debug("jev-skill-router: unreadable skill file %s: %s", skill_md, exc)
        return None

    try:
        frontmatter, body = parse_frontmatter(content)
    except Exception as exc:
        logger.debug("jev-skill-router: unparseable frontmatter %s: %s", skill_md, exc)
        return None

    if not skill_matches_platform(frontmatter):
        return None
    if not skill_matches_environment(frontmatter):
        return None

    name = str(frontmatter.get("name", skill_md.parent.name))[:MAX_NAME_LENGTH]
    if not name or name in disabled:
        return None

    description = str(frontmatter.get("description", ""))
    if not description:
        for line in body.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                description = line
                break
    if len(description) > MAX_DESCRIPTION_LENGTH:
        description = description[: MAX_DESCRIPTION_LENGTH - 3] + "..."

    return CatalogEntry(
        skill_id=name,
        name=name,
        description=description,
        category=_category_for(skill_md, scan_roots),
        skill_md=skill_md,
    )


def compile_catalog(
    outbound_catalog: Optional[Set[str]],
    scan_roots: Optional[List[Path]] = None,
) -> CompiledCatalog:
    """Compile the candidate catalog for the active profile.

    ``outbound_catalog``: ``None`` sends metadata for every loadable skill;
    a set restricts outbound metadata to that subset of loadable skills
    (plan §3.2 — the outbound allowlist is a SUBSET of the loadable set).
    Skills filtered out here simply stay invisible to the vendor; the main
    agent's own skill listing is untouched.
    """
    if scan_roots is None:
        scan_roots = _resolve_scan_roots()

    catalog = CompiledCatalog()
    try:
        disabled = get_disabled_skill_names()
    except Exception as exc:  # config unreadable -> treat as incomplete
        catalog.complete = False
        catalog.scan_error = f"disabled-set unreadable: {exc}"
        return catalog

    seen: Set[str] = set()
    for root in scan_roots:
        try:
            skill_files = list(iter_skill_index_files(root, "SKILL.md"))
        except Exception as exc:
            catalog.complete = False
            catalog.scan_error = f"scan failed under {root}: {exc}"
            continue
        for skill_md in skill_files:
            entry = _parse_entry(skill_md, disabled, scan_roots)
            if entry is None or entry.skill_id in seen:
                continue
            if outbound_catalog is not None and entry.skill_id not in outbound_catalog:
                # Not approved for outbound — stays available to the main
                # agent, just never sent to the vendor.
                continue
            seen.add(entry.skill_id)
            catalog.entries.append(entry)

    catalog.entries.sort(key=lambda e: (e.category or "", e.skill_id))
    return catalog


def _resolve_scan_roots() -> List[Path]:
    """Local profile skills dir first (precedence), then external dirs."""
    from hermes_constants import get_skills_dir

    roots: List[Path] = []
    local = get_skills_dir()
    if local.exists():
        roots.append(local)
    try:
        roots.extend(get_external_skills_dirs())
    except Exception:
        logger.debug("jev-skill-router: external skills dirs unavailable", exc_info=True)
    return roots


def body_excerpt(skill_md: Path, limit_chars: int) -> str:
    """Read a bounded body excerpt (text after the frontmatter).

    Callers must only invoke this for skills listed in
    ``outbound_body_skills`` — the excerpt is vendor-bound. Reads at most
    frontmatter + ``limit_chars`` from disk. Returns "" on any failure.
    """
    if limit_chars <= 0:
        return ""
    try:
        content = skill_md.read_text(encoding="utf-8")[: _METADATA_READ_CHARS + limit_chars]
    except (UnicodeDecodeError, PermissionError, OSError):
        return ""
    try:
        _, body = parse_frontmatter(content)
    except Exception:
        return ""
    text = body.strip()
    if len(text) > limit_chars:
        text = text[:limit_chars] + "…"
    return text


def entry_still_available(entry: CatalogEntry) -> bool:
    """Final pre-injection recheck (plan §3.2).

    Guards against a candidate that was disabled, edited, or made
    platform-incompatible between catalog compile and advice emission —
    a stale suggestion must never be applied. Re-resolves the disabled
    set from config so an in-session ``hermes skills`` toggle applies
    immediately. Fails CLOSED: an unreadable disabled-set means we
    cannot prove the skill is still loadable, so the suggestion drops.
    """
    if not entry.skill_md.exists():
        return False
    try:
        disabled = get_disabled_skill_names()
    except Exception:
        return False
    parsed = _parse_entry(entry.skill_md, disabled, _resolve_scan_roots())
    return parsed is not None and parsed.skill_id == entry.skill_id
