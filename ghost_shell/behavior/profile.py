"""
behavior/profile.py — deterministic behavioral persona per profile
(Phase E, Apr 2026).

Each profile gets a unique RNG seed derived from its name, so:
    - profile "alice" always produces the same persona on every run
    - profile "bob"   produces a DIFFERENT, also-deterministic persona

Personas drift consistently within human bounds — no profile becomes
implausibly slow / fast / jittery. Bounds derived from observed
real-user telemetry datasets (UI Events Webroot, ChromeMetrics).

Persona is a frozen dict — no mutation. Helper functions read from
it and compute concrete numbers per call (with their own controlled
randomness, also seeded from the persona).
"""
import hashlib
import math
import random
from typing import Tuple

# Bounds for each persona dimension. Each profile's value is sampled
# from a uniform distribution within these bounds. The result is
# clamped — no profile gets an impossible value.

_BOUNDS = {
    # Pixels of jitter added during a mouse-move waypoint. A real
    # user shows 1-4px of involuntary tremor; a bot shows 0.
    "mouse_jitter":      (1.0, 4.0),

    # How "curvy" the mouse path is from origin → target. Bezier
    # control point offset as a fraction of straight-line distance.
    "mouse_curve":       (0.05, 0.35),

    # Pixels per millisecond during a scroll burst. Slow readers
    # ~0.5, fast scanners ~3.0.
    "scroll_velocity":   (0.6, 2.4),

    # ms of hover-on-target BEFORE a click event fires. Median real
    # users: 300-700ms. Bots: 0. Set per-profile within this band.
    "pre_click_dwell_min": (150.0, 400.0),
    "pre_click_dwell_max": (600.0, 1500.0),

    # ms between unrelated actions ("now I'll do the next thing").
    # Higher mean = more deliberate user.
    "inter_action_idle_mu":    (1500.0, 5500.0),
    "inter_action_idle_sigma": (400.0, 1500.0),

    # WPM when typing into a form field. 30-50 = casual typist,
    # 60-90 = professional.
    "typing_wpm":        (30.0, 90.0),

    # Characters per second when reading. Used for dwell computation
    # over text-heavy pages. Real users 10-25 cps.
    "reading_speed_cps": (12.0, 24.0),
}


_PERSONA_CACHE: dict = {}


def get_persona(profile_name: str) -> dict:
    """Return a deterministic persona dict for the given profile name.
    Cached in-process — repeated calls cost nothing.

    The persona derives from a SHA256 of the profile name, then uses
    that as a Python `random.Random` seed. Each dimension is sampled
    independently so changing one bound's range doesn't affect others.
    """
    if profile_name in _PERSONA_CACHE:
        return _PERSONA_CACHE[profile_name]

    seed_bytes = hashlib.sha256(
        f"behavioral_v1_{profile_name}".encode("utf-8")
    ).digest()
    seed_int = int.from_bytes(seed_bytes[:8], "big")
    rnd = random.Random(seed_int)

    persona = {"profile_name": profile_name}
    for key, (lo, hi) in _BOUNDS.items():
        persona[key] = round(rnd.uniform(lo, hi), 3)

    # Derived: ensure dwell_max > dwell_min by at least 200ms
    if persona["pre_click_dwell_max"] < persona["pre_click_dwell_min"] + 200:
        persona["pre_click_dwell_max"] = persona["pre_click_dwell_min"] + 200

    # Profile fingerprint for the persona (so logs / dashboards can
    # show "this profile is a slow_reader / fast_clicker / etc")
    persona["persona_summary"] = _classify(persona)
    persona["seed"] = seed_int
    _PERSONA_CACHE[profile_name] = persona
    return persona


def _classify(persona: dict) -> str:
    """Human-readable label like 'fast_clicker / smooth_scroller'.
    Used by the dashboard Persona card. Pure visual hint."""
    parts = []
    # Click speed
    median_dwell = (persona["pre_click_dwell_min"]
                    + persona["pre_click_dwell_max"]) / 2.0
    if median_dwell < 500:
        parts.append("fast_clicker")
    elif median_dwell > 900:
        parts.append("deliberate_clicker")
    else:
        parts.append("avg_clicker")
    # Scroll style
    if persona["scroll_velocity"] < 1.0:
        parts.append("slow_reader")
    elif persona["scroll_velocity"] > 2.0:
        parts.append("scanner")
    else:
        parts.append("balanced_scroller")
    # Tremor
    if persona["mouse_jitter"] > 3.0:
        parts.append("shaky_hand")
    return " / ".join(parts)


# ──────────────────────────────────────────────────────────────
# Per-call samplers (pure functions of persona + RNG)
# ──────────────────────────────────────────────────────────────

def _persona_rng(persona: dict, suffix: str) -> random.Random:
    """Per-action RNG so two consecutive samples don't return
    identical values, but still derive from the persona seed."""
    seed = (int(persona.get("seed", 0)) ^
            int.from_bytes(hashlib.sha256(
                suffix.encode("utf-8")).digest()[:8], "big"))
    return random.Random(seed ^ random.randint(0, 2**32))


def profile_pre_click_dwell(persona: dict) -> float:
    """Sample one pre-click hover duration, in seconds. Profile-shaped:
    a "fast_clicker" persona returns short waits, a "deliberate" one
    longer, but each sample is still randomised within its bounds."""
    rng = _persona_rng(persona, "preclick")
    ms = rng.uniform(persona["pre_click_dwell_min"],
                     persona["pre_click_dwell_max"])
    return ms / 1000.0


def profile_idle_jitter(persona: dict) -> float:
    """Sample one inter-action idle delay, in seconds.
    Gaussian with persona-shaped mean + sigma, clamped to [0.4s, 30s]."""
    rng = _persona_rng(persona, "idle")
    ms = rng.gauss(persona["inter_action_idle_mu"],
                   persona["inter_action_idle_sigma"])
    return max(0.4, min(30.0, ms / 1000.0))


def profile_mouse_curve(persona: dict,
                        x1: float, y1: float,
                        x2: float, y2: float,
                        steps: int = None) -> list:
    """Generate waypoints from (x1,y1) to (x2,y2) along a humanish
    Bezier curve with persona-shaped jitter.

    Returns: list of (x, y, dt_ms) tuples. dt_ms is the suggested
    delay before this waypoint — for selenium ActionChains use the
    cumulative timing.
    """
    rng = _persona_rng(persona, "curve")
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy) or 1.0
    if steps is None:
        # Step density: ~12 waypoints per 100px, but at least 6
        steps = max(6, int(dist / 8))

    # Control points perpendicular to the straight line, offset by
    # persona's curve fraction × distance, with a random direction.
    cp_offset = persona["mouse_curve"] * dist
    nx, ny = -dy / dist, dx / dist     # perpendicular unit vector
    side = 1 if rng.random() > 0.5 else -1
    cp1x = x1 + dx * 0.33 + side * nx * cp_offset
    cp1y = y1 + dy * 0.33 + side * ny * cp_offset
    cp2x = x1 + dx * 0.66 + side * nx * cp_offset * 0.6
    cp2y = y1 + dy * 0.66 + side * ny * cp_offset * 0.6

    jitter_amp = persona["mouse_jitter"]

    def bezier(t):
        u = 1 - t
        bx = (u**3 * x1 + 3 * u**2 * t * cp1x +
              3 * u * t**2 * cp2x + t**3 * x2)
        by = (u**3 * y1 + 3 * u**2 * t * cp1y +
              3 * u * t**2 * cp2y + t**3 * y2)
        return bx, by

    points = []
    for i in range(steps + 1):
        t = i / steps
        bx, by = bezier(t)
        # Add jitter (real users tremor)
        bx += rng.uniform(-jitter_amp, jitter_amp)
        by += rng.uniform(-jitter_amp, jitter_amp)
        # Inter-waypoint timing — hyperbolic ease-in-out so the
        # mouse accelerates from rest, sails through middle, decel.
        if i == 0:
            dt = 0
        else:
            base = 12 + rng.uniform(0, 4)
            ease = math.sin(math.pi * t)   # 0..1..0 envelope
            dt = base * (1.0 + 0.5 * (1 - ease))
        points.append((round(bx, 1), round(by, 1), round(dt, 1)))
    return points


def profile_scroll_steps(persona: dict,
                         total_px: int,
                         direction: int = 1) -> list:
    """Plan a scroll of total_px (always positive — direction = +1
    down, -1 up). Returns list of (delta_px, sleep_ms) tuples.

    Persona's scroll_velocity dictates px/ms; we add micro-pauses
    so the scroll pulses (real users don't smooth-scroll the whole
    page in one blast).
    """
    rng = _persona_rng(persona, "scroll")
    velocity = persona["scroll_velocity"]   # px/ms
    if total_px <= 0:
        return []
    out = []
    remaining = int(total_px)
    while remaining > 0:
        # Each burst is 80-220px depending on persona velocity
        burst_px = min(remaining, int(rng.uniform(80, 220)))
        # Time to scroll the burst at persona velocity, with ±20%
        burst_ms = max(60, int((burst_px / max(0.4, velocity))
                               * rng.uniform(0.8, 1.2)))
        # Reading pause AFTER a burst — proportional to burst size
        pause_ms = int(rng.uniform(120, 600)
                       + burst_px * 0.4)
        out.append((burst_px * direction, burst_ms))
        out.append((0, pause_ms))   # zero-delta = pure idle pause
        remaining -= burst_px
        # Occasionally scroll back a bit (re-reading)
        if rng.random() < 0.15 and remaining > 0:
            back_px = int(rng.uniform(20, 80))
            out.append((-back_px * direction,
                        int(back_px / max(0.4, velocity))))
            out.append((0, int(rng.uniform(180, 500))))
    return out


def profile_typing_intervals(persona: dict, text: str) -> list:
    """For each character in `text` return the ms-delay BEFORE that
    keystroke. Realistic typing has gaussian-distributed inter-key
    delays clustered around the persona's WPM.

    Common typo simulation is NOT included here — too risky to
    replay (form might reject mid-word). Add later via a separate
    helper if needed.
    """
    rng = _persona_rng(persona, "type")
    wpm = persona["typing_wpm"]
    # Average char-per-minute = WPM * 5 (avg word length 5 chars).
    # ms-per-char = 60_000 / cpm
    base_ms = 60_000.0 / max(10.0, wpm * 5.0)
    sigma = base_ms * 0.30           # 30% gaussian spread
    out = []
    for ch in text:
        # Spaces / punctuation slightly slower (word break thinking)
        mean = base_ms * (1.6 if ch in " .,!?;:" else 1.0)
        ms = max(20.0, rng.gauss(mean, sigma))
        out.append(round(ms, 1))
    return out
