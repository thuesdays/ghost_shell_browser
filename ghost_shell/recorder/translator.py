"""
recorder/translator.py — convert recorded CDP/DOM event log into a
unified-flow JSON script (Phase C, Apr 2026).

Input:  list of {kind, ts, url, ...} events from cdp_recorder.Recorder
Output: list of unified-flow steps usable by actions/runner.py:
        [{"type": "open_url", "url": "..."},
         {"type": "click_selector", "selector": "..."},
         {"type": "type", "selector": "...", "text": "..."},
         {"type": "scroll", "px": 300},
         {"type": "dwell", "min": 2, "max": 5}]

Heuristics:
    - Consecutive "keydown" events on the same target are coalesced
      into ONE "type" step with the joined text (matches how a real
      script step would issue them).
    - "scroll" events within 1000ms are coalesced into a single
      step with the cumulative delta.
    - Inter-event idle time → "dwell" step with fuzzed min/max
      around the observed delay (so replays vary slightly run-to-run).
    - Initial "nav" → "open_url" step (the first one); subsequent
      "nav" events become bare "wait_for_url" markers (script can
      auto-wait) rather than re-driving navigation.

Selector synthesis:
    The recorder captures target_id, target_class, target_tag and
    target_text. We prefer:
      1. #id          if available + reasonable
      2. tag.classA.classB  for multi-class buttons
      3. tag:contains(text) — supplemented by a click position
                              comment for human review
    The translator never invents selectors — when it can't build a
    confident one it emits a comment like
    {"type": "click_selector", "selector": "?", "_note": "..."} so
    the user can fix manually.
"""
import logging
import re
from typing import Any

logger = logging.getLogger("recorder.translator")


def translate_events_to_flow(events: list,
                             *,
                             coalesce_typing: bool = True,
                             coalesce_scroll: bool = True,
                             dwell_jitter_pct: float = 0.20) -> list:
    """Convert a recorded event log to a unified-flow step list.

    Args:
        events            chronological list of dicts from Recorder
        coalesce_typing   bundle consecutive keystrokes into one
                          "type" step (default True)
        coalesce_scroll   merge scroll bursts into one step (True)
        dwell_jitter_pct  ± fraction added to "dwell" min/max bounds
                          so replays vary (0.20 = ±20%)

    Returns:
        list of unified-flow step dicts ready to be saved as a Script.
    """
    if not events:
        return []

    steps = []
    i = 0
    n = len(events)
    last_ts = events[0].get("ts") or 0

    while i < n:
        ev = events[i]
        kind = ev.get("kind")
        ts = ev.get("ts") or last_ts

        # Idle → "dwell" step. Only emit if the gap is meaningful
        # (> 800ms) — sub-second gaps are normal click→nav latency.
        gap_ms = max(0, ts - last_ts)
        if gap_ms > 800:
            sec = gap_ms / 1000.0
            jitter = sec * dwell_jitter_pct
            steps.append({
                "type":     "dwell",
                "min":      round(max(0.5, sec - jitter), 1),
                "max":      round(sec + jitter, 1),
                "_recorded_ms": gap_ms,
            })

        if kind == "nav":
            url = ev.get("url") or ""
            # First nav = open_url; subsequent = wait_for_url
            if not steps or not any(
                s.get("type") == "open_url" for s in steps
            ):
                steps.append({"type": "open_url", "url": url})
            else:
                # Skip — runner auto-waits after click_selector;
                # adding wait_for_url is redundant and brittle.
                pass

        elif kind == "click":
            selector, note = _build_selector(ev)
            step = {
                "type":     "click_selector",
                "selector": selector,
            }
            if note:
                step["_note"] = note
            steps.append(step)

        elif kind == "keydown":
            if coalesce_typing:
                # Look ahead — collect all keydown events with the
                # same target_tag until a non-keydown event or a
                # different target. Drop modifier-only / nav keys.
                buf = []
                j = i
                last_target = ev.get("target_tag")
                while j < n and events[j].get("kind") == "keydown" \
                        and events[j].get("target_tag") == last_target:
                    k = events[j].get("key") or ""
                    if len(k) == 1:           # printable char
                        buf.append(k)
                    elif k == "Backspace" and buf:
                        buf.pop()
                    elif k == " " or k == "Spacebar":
                        buf.append(" ")
                    else:
                        # Special key (Enter, Tab, Escape) — emit
                        # whatever we've buffered, then a press_key.
                        if buf:
                            steps.append({
                                "type": "type",
                                "selector": last_target.lower() if last_target else "input",
                                "text": "".join(buf),
                            })
                            buf = []
                        steps.append({
                            "type": "press_key",
                            "key":  k,
                        })
                    j += 1
                if buf:
                    steps.append({
                        "type": "type",
                        "selector": last_target.lower() if last_target else "input",
                        "text": "".join(buf),
                    })
                i = j
                last_ts = events[j - 1].get("ts") or ts
                continue
            # Coalesce off — one step per keystroke (rarely useful)
            steps.append({
                "type": "press_key",
                "key":  ev.get("key") or "",
            })

        elif kind == "scroll":
            if coalesce_scroll:
                # Coalesce scroll events within 1s of each other
                cumulative_y = ev.get("scrollY") or 0
                start_y = cumulative_y
                j = i + 1
                while j < n and events[j].get("kind") == "scroll" \
                        and (events[j].get("ts", ts) - ts) < 1000:
                    cumulative_y = events[j].get("scrollY") or cumulative_y
                    j += 1
                delta = cumulative_y - start_y
                if abs(delta) >= 30:
                    steps.append({
                        "type": "scroll",
                        "px":   int(delta),
                    })
                i = max(j, i + 1)
                last_ts = events[max(j, i) - 1].get("ts") or ts
                continue
            # Single scroll
            steps.append({
                "type": "scroll",
                "px":   100 if (ev.get("scrollY") or 0) > 0 else -100,
            })

        else:
            # Unknown kind — preserve as a comment so the human can
            # decide. Keeps the round-trip lossless.
            steps.append({
                "type": "_unknown",
                "kind": kind,
                "raw":  {k: v for k, v in ev.items() if k != "raw"},
            })

        last_ts = ts
        i += 1

    return _post_process(steps)


def _build_selector(ev: dict) -> tuple:
    """Build a CSS selector from a recorded click target.
    Returns (selector_string, optional_note). The note is set when
    the selector is heuristic and the user should review it."""
    tid = (ev.get("target_id") or "").strip()
    tag = (ev.get("target_tag") or "").lower().strip() or "*"
    cls = (ev.get("target_class") or "").strip()
    text = (ev.get("target_text") or "").strip()

    # 1. ID is the strongest signal — single CSS-token if it looks
    #    syntactically valid (alphanumeric + _ - only).
    if tid and re.match(r"^[A-Za-z][\w\-:]*$", tid):
        return f"#{tid}", None

    # 2. Tag + reasonable class chain. CSS class names with spaces
    #    are split; weird unicode classes get filtered.
    if cls:
        # Take first 2 classes that look like real CSS identifiers
        good = []
        for c in cls.split():
            if re.match(r"^[A-Za-z][\w\-:]*$", c):
                good.append(c)
            if len(good) >= 2:
                break
        if good:
            return f"{tag}.{'.'.join(good)}", None

    # 3. Tag-only fallback with text-based note for human review.
    if text:
        return tag, f"target text was: {text[:60]}"
    return tag, "no usable id/class/text — user should refine"


def _post_process(steps: list) -> list:
    """Final passes:
      - drop trailing _unknown steps (they're usually noise from
        document-end scroll events)
      - merge adjacent dwell steps
      - cap each step's _note length so the JSON stays human-readable
    """
    # Drop trailing unknowns
    while steps and steps[-1].get("type") == "_unknown":
        steps.pop()
    # Merge adjacent dwells
    out = []
    for s in steps:
        if (out and s.get("type") == "dwell"
                and out[-1].get("type") == "dwell"):
            out[-1]["min"] = round(out[-1]["min"] + s["min"], 1)
            out[-1]["max"] = round(out[-1]["max"] + s["max"], 1)
        else:
            out.append(s)
    # Note length cap
    for s in out:
        n = s.get("_note")
        if n and len(n) > 120:
            s["_note"] = n[:117] + "…"
    return out
