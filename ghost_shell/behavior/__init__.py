"""
ghost_shell.behavior — Per-profile behavioral fingerprint (Phase E,
Apr 2026).

Most antidetect tools focus on FP/JA3/cookies — the static identity
layer. They ignore the *behavioral* layer: how a user moves the
mouse, how fast they scroll, how long they dwell before clicking.
Google, Facebook, Cloudflare etc. all run anti-fraud models that
score this dimension. Bots show up because they all have the SAME
behavioral signature: identical inter-click intervals, perfectly
straight mouse paths, no idle micro-movements.

This module gives each profile a deterministic-but-distinct
behavioral persona. Same profile → same persona → consistent runs.
Different profiles → different personas → look like different
humans.

Persona dimensions:
    mouse_jitter        amplitude of micro-jitter on hover paths
                        (real users tremor; bots are rigid)
    mouse_curve         bezier control-point distance — high =
                        sweeping arcs, low = tight near-straight
    scroll_velocity     px/ms during scroll — real users vary
                        between 0.5 and 3.0 depending on reading
                        intent
    pre_click_dwell     ms hover-before-click (real: 200-1500;
                        bots: 0)
    inter_action_idle   ms between unrelated actions (real: gaussian
                        around a profile-specific mean)
    typing_wpm          words-per-minute when filling forms
                        (real: 30-90; bots: instantaneous paste)
    reading_speed       characters-per-second during dwell
                        (varies with content density)

Public API:
    get_persona(profile_name)        → cached persona dict
    profile_pre_click_dwell(persona) → sample one delay
    profile_mouse_curve(persona, p1, p2) → list of waypoints
    profile_scroll_steps(persona, total_px) → list of (delta, sleep)
    profile_typing_intervals(persona, text) → list of per-char ms
"""

from .profile import (
    get_persona,
    profile_pre_click_dwell,
    profile_mouse_curve,
    profile_scroll_steps,
    profile_typing_intervals,
    profile_idle_jitter,
)

__all__ = [
    "get_persona",
    "profile_pre_click_dwell",
    "profile_mouse_curve",
    "profile_scroll_steps",
    "profile_typing_intervals",
    "profile_idle_jitter",
]
