#!/usr/bin/env python3
"""Sync the bundled orchestrator copy under module/tool/ from the canonical source.

The repo plays two roles: it is the development repo for the `bmad-automator`
Python package (root `src/`, `pyproject.toml`, `README.md`) AND the distributable
BMAD plugin under `module/`. The plugin ships an installable copy of the tool in
`module/tool/` because `bmad-auto-setup` runs `pip install "<skill-dir>/../tool"`.

Distribution is the raw git tree (root marketplace.json `source: "./module"`), so
the copy must physically exist under `module/tool/`. To stop it drifting from the canonical
source, it is *generated* by this script and verified in CI:

    python scripts/sync-tool.py           # regenerate generated copies from source
    python scripts/sync-tool.py --check   # fail (exit 1) if any copy is stale

It also generates a repo-root `module.yaml` mirror of the setup skill's
`assets/module.yaml` so the BMAD installer can locate the module descriptor
(its lookup checks <repo>/module.yaml but not the marketplace `source` subdir).

`src/` and `module/bmad-auto-setup/assets/module.yaml` are the single sources of
truth; never hand-edit module/tool/ or the repo-root module.yaml.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEST = REPO / "module" / "tool"

# Single files copied verbatim from repo root into module/tool/.
FILE_MAP = {
    "pyproject.toml": "pyproject.toml",
    "README.md": "README.md",
}

# Package tree copied from src/<pkg> into module/tool/src/<pkg>.
PKG = "automator"

# Repo-root mirror of the canonical module descriptor. bmad-method's installer
# locates a module's module.yaml through hardcoded roots (<repo>/skills, /src,
# /src/skills, and <repo>/module.yaml) and does NOT honor the marketplace
# `source: "./module"` subdir. Without a copy at the repo root the post-install
# steps (agent roster + config scoping in config.toml) warn that they "could not
# locate module.yaml for 'bauto'". The canonical file stays under the setup
# skill (read at runtime in installed projects); this root copy is generated.
ROOT_MIRRORS = {
    REPO / "module" / "bmad-auto-setup" / "assets" / "module.yaml": REPO / "module.yaml",
}

# Names excluded everywhere (build/runtime debris, never part of the package).
EXCLUDE_DIRS = {"__pycache__"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _iter_pkg_files(root: Path):
    """Yield package files under *root*, relative to *root*, skipping debris."""
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        yield rel


def _planned_pairs() -> list[tuple[Path, Path]]:
    """Return (source, dest) pairs the sync would produce."""
    pairs: list[tuple[Path, Path]] = []
    for src_rel, dst_rel in FILE_MAP.items():
        pairs.append((REPO / src_rel, DEST / dst_rel))
    src_pkg = REPO / "src" / PKG
    dst_pkg = DEST / "src" / PKG
    for rel in _iter_pkg_files(src_pkg):
        pairs.append((src_pkg / rel, dst_pkg / rel))
    for src, dst in ROOT_MIRRORS.items():
        pairs.append((src, dst))
    return pairs


def _stale_dest_files(planned_dests: set[Path]) -> list[Path]:
    """Dest files under the managed package tree that the source no longer has."""
    dst_pkg = DEST / "src" / PKG
    stale: list[Path] = []
    if not dst_pkg.exists():
        return stale
    for rel in _iter_pkg_files(dst_pkg):
        if (dst_pkg / rel) not in planned_dests:
            stale.append(dst_pkg / rel)
    return stale


def check() -> int:
    pairs = _planned_pairs()
    planned_dests = {dst for _, dst in pairs}
    problems: list[str] = []
    for src, dst in pairs:
        if not dst.exists():
            problems.append(f"missing:    {dst.relative_to(REPO)}")
        elif not filecmp.cmp(src, dst, shallow=False):
            problems.append(f"out-of-date: {dst.relative_to(REPO)}")
    for dst in _stale_dest_files(planned_dests):
        problems.append(f"stale:      {dst.relative_to(REPO)}")
    if problems:
        print("generated copies are out of sync with the canonical source:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("\nRun: python scripts/sync-tool.py", file=sys.stderr)
        return 1
    print("generated copies are in sync with the canonical source.")
    return 0


def sync() -> int:
    pairs = _planned_pairs()
    planned_dests = {dst for _, dst in pairs}
    changed = 0
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or not filecmp.cmp(src, dst, shallow=False):
            shutil.copy2(src, dst)
            changed += 1
    removed = 0
    for dst in _stale_dest_files(planned_dests):
        dst.unlink()
        removed += 1
    print(f"Synced generated copies from source: {changed} written, {removed} removed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify module/tool/ matches the source; exit 1 if stale (no writes)",
    )
    args = parser.parse_args()
    return check() if args.check else sync()


if __name__ == "__main__":
    raise SystemExit(main())
