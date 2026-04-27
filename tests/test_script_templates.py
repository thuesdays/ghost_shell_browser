"""Sprint 9 — smoke tests for ghost_shell/scripts_templates/*.json.

Catches the most common regression: a template references an action
that was renamed or removed in the runner, leaving operators with a
"unknown action type" error at runtime.

Three checks per template:

1. **Parses as JSON.** Catches stray commas, missing quotes, etc.
   on every push.
2. **Top-level shape.** ``name`` and ``flow`` are required; ``flow``
   must be a list. ``description`` / ``category`` / ``tags`` are
   optional but typed when present.
3. **Every step's ``type`` resolves to a real handler.** Walks the
   recursive ``then_steps`` / ``else_steps`` / ``steps`` containers
   so nested ``if`` / ``loop`` / ``foreach_ad`` steps are checked
   too.

The list of valid action types is built dynamically by importing
the runner — no hand-maintained allow-list to drift out of sync.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TPL_DIR = PROJECT_ROOT / "ghost_shell" / "scripts_templates"

# Container keys that hold nested step lists. Mirrors what the runner's
# unified-flow executor and the script_update validator both walk.
NESTED_KEYS = ("steps", "then_steps", "else_steps", "post_ad_actions",
               "iterations")


def _all_templates():
    return sorted(TPL_DIR.glob("*.json"))


def _all_valid_action_types() -> set[str]:
    """Build the union of every known step type.

    Sources, in order of priority:

    1. ``ACTION_HANDLERS`` dict (ad-pipeline scope).
    2. ``LOOP_ACTION_HANDLERS`` dict (loop scope).
    3. Source-grep of ``_exec_single`` in runner.py for every
       ``act_type == "..."`` literal — covers the unified-flow
       dispatcher's if/elif chain, including types like
       ``catch_ads`` / ``search_query`` / ``save_var`` /
       ``foreach_ad`` / ``if`` etc. that aren't in either dict.
    4. Loop-control sentinels ``loop_break`` / ``loop_continue``
       handled inside loop bodies but not via a top-level act_type.

    Source-grep is the right call here over a hand-curated list:
    new unified-flow handlers added in future refactors get picked
    up automatically the next time the test runs."""
    import re as _re
    from pathlib import Path as _P
    from ghost_shell.actions import runner

    types = set()
    types.update(runner.ACTION_HANDLERS.keys())
    types.update(runner.LOOP_ACTION_HANDLERS.keys())

    # Source-derive every act_type literal from runner.py. Pattern
    # matches both `if act_type == "foo":` and the `t in (...)` tuples
    # used in ``_validate_step``.
    runner_src = _P(runner.__file__).read_text(encoding="utf-8")
    for m in _re.finditer(r'act_type\s*==\s*"([a-z_][\w]*)"', runner_src):
        types.add(m.group(1))
    # Tuple form: `t in ("search_query", "search_all_queries", ...)`
    for m in _re.finditer(r't\s+in\s+\(([^)]+)\)', runner_src):
        for piece in m.group(1).split(","):
            piece = piece.strip().strip('"').strip("'")
            if _re.fullmatch(r"[a-z_][\w]*", piece or ""):
                types.add(piece)

    # Loop-control sentinels — these are step shapes inside loop
    # bodies, not dispatched via act_type.
    types.update({"loop_break", "loop_continue"})
    return types


def _walk_steps(steps, path=""):
    """Yield (step_index_path, step_dict) for every step in a flow,
    recursing into nested containers."""
    if not isinstance(steps, list):
        return
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        yield (f"{path}[{i}]", step)
        for k in NESTED_KEYS:
            if isinstance(step.get(k), list):
                yield from _walk_steps(step[k], path=f"{path}[{i}].{k}")


# ──────────────────────────────────────────────────────────────
# Tests — parametrized over every template file
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", _all_templates(), ids=lambda p: p.name)
def test_template_parses_as_json(path):
    """Catches malformed JSON on every push. Cheap, runs fast."""
    json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", _all_templates(), ids=lambda p: p.name)
def test_template_has_required_top_level(path):
    """name + flow are required; types are checked."""
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name}: root is not an object"
    assert isinstance(data.get("name"), str) and data["name"], \
        f"{path.name}: missing or empty 'name'"
    assert isinstance(data.get("flow"), list), \
        f"{path.name}: 'flow' must be a list"
    if "description" in data:
        assert isinstance(data["description"], str)
    if "category" in data:
        assert isinstance(data["category"], str)
    if "tags" in data:
        assert isinstance(data["tags"], list)
        assert all(isinstance(t, str) for t in data["tags"])


@pytest.mark.parametrize("path", _all_templates(), ids=lambda p: p.name)
def test_template_every_step_has_type(path):
    """Every step (including nested ones) must declare a ``type``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for step_path, step in _walk_steps(data.get("flow") or []):
        assert "type" in step and isinstance(step["type"], str), \
            f"{path.name}: step at {step_path} has no 'type'"


@pytest.mark.parametrize("path", _all_templates(), ids=lambda p: p.name)
def test_template_every_step_type_is_known(path):
    """The runner must know how to handle every step's type. This
    is the regression guard: when an action gets renamed or
    removed, the templates that use it break loudly here instead
    of silently at the user's next run."""
    valid = _all_valid_action_types()
    data = json.loads(path.read_text(encoding="utf-8"))
    for step_path, step in _walk_steps(data.get("flow") or []):
        t = step.get("type")
        assert t in valid, \
            f"{path.name}: unknown action type {t!r} at {step_path}"


# ──────────────────────────────────────────────────────────────
# Single non-parametrized test for the API endpoint contract
# ──────────────────────────────────────────────────────────────

def test_endpoint_loads_all_templates():
    """`/api/scripts/templates` reads the same files. Mock-load via
    the same path glob the endpoint uses to confirm the directory
    layout is what the server expects (a templates/ subdir directly
    under ghost_shell/)."""
    assert TPL_DIR.is_dir(), f"templates dir missing: {TPL_DIR}"
    assert any(TPL_DIR.glob("*.json")), "no .json templates found"


# ──────────────────────────────────────────────────────────────
# Selector hygiene — TPL-01 from sprint-9-scripts-audit.md
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", _all_templates(), ids=lambda p: p.name)
def test_template_selectors_resolve(path):
    """Every selector field must be parseable by _resolve_selector
    without raising. This catches the Playwright-only ``:has-text()``
    syntax slipping in (we now translate it, but a brand-new
    Playwright-only construct would still surprise us)."""
    from ghost_shell.actions.runner import _resolve_selector
    data = json.loads(path.read_text(encoding="utf-8"))
    for step_path, step in _walk_steps(data.get("flow") or []):
        sel = step.get("selector")
        if not sel or not isinstance(sel, str):
            continue
        by, value = _resolve_selector(sel)
        assert by and value, \
            f"{path.name}: selector at {step_path} resolved to empty"
