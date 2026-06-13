"""`bmad-auto init`: make a target project orchestratable.

- copies the hook relay script to <project>/.automator/bmad_auto_hook.py
- idempotently merges hook registrations into each selected CLI's hook config
  (dialect + native->canonical event map come from the CLI profile)
- installs the bundled bmad-auto-* skills into each selected CLI's skill tree
  (.claude/skills for claude, .agents/skills for codex/gemini)
- writes .automator/policy.toml from the template (if missing)
- gitignores .automator/runs/

Every dialect registers the same relay script under the CLI's native event
names while passing the canonical event name as the script argument, so the
orchestrator's signal watcher is CLI-agnostic.
"""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

from .adapters.profile import ALIASES, CLIProfile, ProfileError, load_profiles
from .policy import POLICY_TEMPLATE

HOOK_SCRIPT_REL = ".automator/bmad_auto_hook.py"
HOOK_MARKER = "bmad_auto_hook.py"
GEMINI_HOOK_TIMEOUT_MS = 60_000

# The bmad-auto-* skills bundled in the wheel (automator/data/skills/) that
# `bmad-auto init` lays down. They must be installed together — bmad-auto-review
# references bmad-auto-dev/deferred-work-format.md as a sibling.
MODULE_SKILLS = (
    "bmad-auto-dev",
    "bmad-auto-review",
    "bmad-auto-resolve",
    "bmad-auto-sweep",
    "bmad-auto-setup",
)


def _hook_command(project: Path, profile: CLIProfile, canonical_event: str) -> str:
    if profile.hooks.dialect == "claude-settings-json":
        return f'python3 "$CLAUDE_PROJECT_DIR"/{HOOK_SCRIPT_REL} {canonical_event}'
    # Codex/Gemini expose no $CLAUDE_PROJECT_DIR equivalent to hook commands;
    # bake the absolute path at init time.
    return f"python3 {shlex.quote(str(project / HOOK_SCRIPT_REL))} {canonical_event}"


def _hook_entry(dialect: str, command: str) -> dict:
    handler: dict = {"type": "command", "command": command}
    if dialect == "gemini-settings-json":
        handler["timeout"] = GEMINI_HOOK_TIMEOUT_MS  # Gemini timeouts are milliseconds
        return {"matcher": "", "hooks": [handler]}
    # claude-settings-json and codex-hooks-json share the schema
    return {"hooks": [handler]}


def merge_hooks(config: dict, registrations: dict[str, str], dialect: str) -> tuple[dict, bool]:
    """Add relay registrations (native event -> command) to a hook config dict."""
    changed = False
    hooks = config.setdefault("hooks", {})
    for native_event, command in registrations.items():
        matchers = hooks.setdefault(native_event, [])
        already = any(
            HOOK_MARKER in handler.get("command", "")
            for matcher in matchers
            if isinstance(matcher, dict)
            for handler in matcher.get("hooks", [])
            if isinstance(handler, dict)
        )
        if not already:
            matchers.append(_hook_entry(dialect, command))
            changed = True
    return config, changed


def _register_hooks(project: Path, profile: CLIProfile) -> int:
    config_path = project / profile.hooks.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config: dict = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"FAIL: {config_path} is not valid JSON; fix it and re-run init")
            return 1
    registrations = {
        native: _hook_command(project, profile, canonical)
        for native, canonical in profile.hooks.events.items()
    }
    config, changed = merge_hooks(config, registrations, profile.hooks.dialect)
    if changed:
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"  hooks registered ({profile.name}): {config_path}")
    else:
        print(f"  hooks already registered ({profile.name})")
    return 0


def _copy_traversable(src, dst: Path) -> None:
    """Recursively copy a packaged resource tree to a filesystem path.

    Walks via the Traversable API (.iterdir/.read_bytes) rather than resolving a
    filesystem path, so it works even when the package is zip-imported.
    """
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            _copy_traversable(child, dst / child.name)
    else:
        dst.write_bytes(src.read_bytes())


def _copy_skills(project: Path, trees: Sequence[str], force: bool) -> bool:
    """Install the bundled bmad-auto-* skills into each project skill tree.

    A skill directory that already exists is skipped unless ``force`` (so the
    BMAD installer's copy or local edits are never clobbered silently). Returns
    True if any skill was skipped because it already existed.
    """
    skills_root = resources.files("automator.data").joinpath("skills")
    skipped_any = False
    for tree in trees:
        tree_dir = project / tree
        installed: list[str] = []
        skipped: list[str] = []
        for skill in MODULE_SKILLS:
            dst = tree_dir / skill
            if dst.exists() and not force:
                skipped.append(skill)
                continue
            if dst.exists():
                shutil.rmtree(dst)
            _copy_traversable(skills_root.joinpath(skill), dst)
            installed.append(skill)
        parts: list[str] = []
        if installed:
            parts.append(f"installed {', '.join(installed)}")
        if skipped:
            parts.append(f"skipped {', '.join(skipped)} (exist)")
            skipped_any = True
        print(f"  skills -> {tree}/: {'; '.join(parts) if parts else 'nothing to do'}")
    return skipped_any


def install_into(
    project: Path,
    clis: Sequence[str] = ("claude",),
    *,
    skills: bool = True,
    force_skills: bool = False,
) -> int:
    project = project.resolve()
    try:
        available = load_profiles(project)
        profiles = []
        for name in clis:
            key = ALIASES.get(name, name)
            if key not in available:
                raise ProfileError(
                    f"unknown CLI profile: {name!r} (available: {sorted(available)})"
                )
            profiles.append(available[key])
    except ProfileError as e:
        print(f"FAIL: {e}")
        return 1

    automator_dir = project / ".automator"
    automator_dir.mkdir(parents=True, exist_ok=True)

    # 1. hook relay script (shared by all CLIs)
    script_target = project / HOOK_SCRIPT_REL
    script_source = resources.files("automator.data").joinpath("bmad_auto_hook.py")
    script_target.write_text(script_source.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"  hook script: {script_target}")

    # 2. per-CLI hook registration
    for profile in profiles:
        if _register_hooks(project, profile) != 0:
            return 1

    # 3. bundled skills into each CLI's skill tree (deduped: codex+gemini share
    #    .agents/skills)
    skills_skipped = False
    if skills:
        trees = list(dict.fromkeys(p.skill_tree for p in profiles))
        skills_skipped = _copy_skills(project, trees, force_skills)

    # 4. policy template
    policy_path = automator_dir / "policy.toml"
    if policy_path.is_file():
        print("  policy exists, leaving untouched")
    else:
        policy_path.write_text(POLICY_TEMPLATE, encoding="utf-8")
        print(f"  policy written: {policy_path}")

    # 5. gitignore runs dir
    gitignore = project / ".gitignore"
    ignore_line = ".automator/runs/"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    if ignore_line not in existing.splitlines():
        with gitignore.open("a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(ignore_line + "\n")
        print(f"  gitignored: {ignore_line}")

    if skills_skipped:
        print("  some skills already present; re-run with --force-skills to overwrite")

    print(
        "init complete. One-time setup before `bmad-auto run` — spawned "
        "sessions cannot answer first-run dialogs, and a pending dialog reads "
        "as a session timeout:"
    )
    for profile in profiles:
        if profile.first_run_note:
            print(f"  {profile.name}: {profile.first_run_note}")
    return 0
