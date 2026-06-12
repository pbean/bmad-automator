# BMAD Automator module (`bauto`)

A BMAD module pairing the automation skills with the
[bmad-auto orchestrator tool](https://github.com/pbean/bmad-automator) (the
Python program that drives the loop). The skills are installed by the BMAD
installer; `bmad-auto-setup` then installs the `bmad-automator` package from its
Git repository, so installing this module gives you a working system — skills
plus the orchestrator that invokes them. Standard BMAD installs are never
modified; the skills are automator-owned forks maintained against their upstream
counterparts.

| Component          | Forked from          | Role                                                                                                                                             |
| ------------------ | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bmad-automator`   | — (this repo, Git)   | the orchestrator: ralph-loop, hooks, tmux adapters, TUI. CLI `bmad-auto`. Installed by `bmad-auto-setup` from Git.                               |
| `bmad-auto-dev`    | `bmad-quick-dev`     | unattended implementation: story key / feedback file / dw-bundle → spec + code + result.json                                                     |
| `bmad-auto-review` | `bmad-code-review`   | unattended adversarial review of a dev spec in a fresh context                                                                                   |
| `bmad-auto-sweep`  | — (automator-native) | read-only deferred-work ledger triage                                                                                                            |
| `bmad-auto-setup`  | — (scaffolded)       | registers the module in `_bmad/config.yaml` + `module-help.csv`, **installs the orchestrator tool from Git**, runs `bmad-auto init` + `validate` |

## Install into a project

```bash
cp -r bmad-auto-* /path/to/project/.claude/skills/
cp -r bmad-auto-* /path/to/project/.agents/skills/   # only if using codex/gemini
claude "/bmad-auto-setup accept all defaults"        # registers config AND installs the tool
```

`bmad-auto-setup` is one-shot: it merges config + help entries, then
`python3 -m pip install "<plugin>/tool[tui]"`, then `bmad-auto init`
(hooks/policy/gitignore) and `bmad-auto validate` (preflight). To register the
skills without touching the Python environment, tell it `skills only`.

The skills must be installed **together**: `bmad-auto-review` appends
deferred-work entries per `bmad-auto-dev/deferred-work-format.md` (sibling
skill directory). Requires the BMad Method (bmm) module
(`_bmad/bmm/config.yaml`) and a `sprint-status.yaml` from
`bmad-sprint-planning`.

`_bmad/custom/<skill-name>.toml` customization overrides are keyed by skill
directory name — duplicate any `bmad-quick-dev.toml` / `bmad-code-review.toml`
overrides as `bmad-auto-dev.toml` / `bmad-auto-review.toml`.

## Maintaining the forks

- This directory is **canonical** for the skills. The repo's `.claude/skills/`
  and `.agents/skills/` hold copies; `tests/test_module_skills_sync.py` fails if
  they drift. After editing here, re-copy the skill dirs into both trees.
- The orchestrator tool is **not** bundled here — the BMAD installer copies only
  the skill directories, so a sibling `tool/` would never reach an installed
  project. `bmad-auto-setup` installs the `bmad-automator` package from
  <https://github.com/pbean/bmad-automator> (`src/automator`, `pyproject.toml`
  are canonical at the repo root).
- The forks keep the upstream file structure. To pull upstream improvements:
  `diff -r <bmad-install>/bmad-quick-dev bmad-auto-dev`, merge manually.
- Do **not** rename the result.json `workflow` values (`"quick-dev"`,
  `"code-review"`, `"deferred-sweep-triage"`) or the `plan-code-review` route —
  they are machine contracts validated by the orchestrator, not skill names.

Validate after changes:

```bash
python3 ../.claude/skills/bmad-module-builder/scripts/validate-module.py .
```
