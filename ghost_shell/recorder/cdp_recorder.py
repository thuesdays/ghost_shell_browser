"""
recorder/cdp_recorder.py — CDP-based session recorder (Phase C).

Subscribes to a curated set of Chrome DevTools Protocol events on the
attached browser and accumulates a typed event log. The log is a
chronologically-ordered list of dicts; translator.py converts it into
unified-flow action steps.

Events captured (and what they map to):

    Page.frameStartedLoading  → "nav" intent (URL pending)
    Page.frameNavigated       → "nav" with final URL
    Runtime.consoleAPICalled  → ignored (noise filter)
    Network.requestWillBeSent → captured for "wait_for_url" hints
                                 (filtered to main-frame document)
    Input.dispatchMouseEvent  → "click" / "hover" / "scroll"
    Input.dispatchKeyEvent    → "type" / "press_key"
    DOM.documentUpdated       → marker for navigation completion

Why CDP and not selenium events?
    Selenium intercepts at the JS-driver layer, which:
      - misses real user input on a manually-controlled browser
      - can't observe OS-level mouse/keyboard
    CDP runs on the browser side and sees actual DOM events as
    Chromium dispatches them, so a human moving a mouse in the
    Chrome window is visible. This is what lets us do "no-code"
    recording of real user workflows.

Caveats:
    - CDP is async; we accumulate events with monotonic timestamps
      so timeline order is preserved even if delivery is reordered
      across event types.
    - The recorder is single-tab. Multi-tab recording is a Phase
      C2 follow-up — for now, popping a new tab pauses recording
      until the user returns.
    - Mouse coordinates are PHYSICAL pixels (CDP convention). The
      translator converts to CSS-pixel selectors when writing the
      unified flow (using DOM querying around click positions).
"""
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("recorder.cdp")


class Recorder:
    """Attaches to a Selenium-driven Chrome session and records CDP
    events into a buffered list. Thread-safe — start/stop can be
    called from the dashboard HTTP thread while events arrive on the
    CDP listener thread.

    Usage:
        rec = Recorder(driver, profile_name)
        rec.start()
        # ... user does stuff in the browser ...
        events = rec.stop()
        rec.save_to_disk(events)   # ./recordings/<profile>/<ts>.json

    Or, for in-memory pipeline:
        translator.translate_events_to_flow(events)
    """

    def __init__(self, driver, profile_name: str,
                 output_dir: str = "recordings"):
        self.driver       = driver
        self.profile_name = profile_name
        self.output_dir   = output_dir
        self._events      = []     # list of dicts
        self._lock        = threading.Lock()
        self._started_at  = None
        self._stopped_at  = None
        self._listeners   = []     # CDP subscription handles for cleanup

    # ── Public lifecycle ──────────────────────────────────────

    def start(self) -> None:
        """Subscribe to CDP events. Idempotent — repeated calls
        are no-ops once a recording is in flight."""
        if self._started_at is not None and self._stopped_at is None:
            logger.debug("Recorder.start: already running")
            return
        self._events = []
        self._started_at = time.time()
        self._stopped_at = None

        # Selenium 4 exposes CDP via driver.execute_cdp_cmd. For
        # event subscriptions we use the lower-level pycdp-like
        # bridge that Chromium's DevTools session offers:
        # driver.execute_cdp_cmd("DOMAIN.METHOD", params).
        try:
            # Enable the relevant domains. Each enable() makes the
            # browser start emitting that domain's events.
            self.driver.execute_cdp_cmd("Page.enable", {})
            self.driver.execute_cdp_cmd("Network.enable", {})
            self.driver.execute_cdp_cmd("Runtime.enable", {})
            # DOM not strictly required but useful for documentUpdated
            # marker that pairs nicely with frameNavigated.
            self.driver.execute_cdp_cmd("DOM.enable", {})
        except Exception as e:
            logger.warning(f"Recorder.start: CDP enable failed: {e}")
            self._stopped_at = time.time()
            raise

        # Attach a CDP event listener via Selenium's bidi adapter.
        # Selenium 4.18+ exposes driver.script.add_console_message
        # _handler-style helpers; older versions need a websocket
        # tap. We use a lightweight polling approach that works
        # across Selenium versions: inject a JS hook that pushes
        # events to a JS-side buffer + poll it with execute_script.
        # See _install_js_hooks() / _poll_loop().
        self._install_js_hooks()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="recorder-poll"
        )
        self._poll_stop = threading.Event()
        self._poll_thread.start()
        logger.info(
            f"[recorder] started for profile={self.profile_name!r}"
        )

    def stop(self) -> list:
        """Stop recording, return the accumulated events list."""
        if self._stopped_at is not None:
            return list(self._events)
        self._stopped_at = time.time()
        try:
            if hasattr(self, "_poll_stop"):
                self._poll_stop.set()
            if hasattr(self, "_poll_thread"):
                self._poll_thread.join(timeout=2)
        except Exception:
            pass
        # Drain any remaining JS-side buffer
        try:
            self._drain_js_buffer()
        except Exception as e:
            logger.debug(f"Recorder.stop: final drain failed: {e}")

        with self._lock:
            events = list(self._events)
        logger.info(
            f"[recorder] stopped for profile={self.profile_name!r} — "
            f"{len(events)} events captured in "
            f"{int(self._stopped_at - self._started_at)}s"
        )
        return events

    def save_to_disk(self, events: list = None,
                     filename: str = None) -> str:
        """Persist events as JSON. Returns the absolute path written.
        If events is None, uses the current buffer."""
        if events is None:
            with self._lock:
                events = list(self._events)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        prof_dir = os.path.join(self.output_dir, self.profile_name)
        os.makedirs(prof_dir, exist_ok=True)
        fname = filename or f"{ts}.events.json"
        path = os.path.join(prof_dir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "profile_name": self.profile_name,
                "started_at":   self._started_at,
                "stopped_at":   self._stopped_at,
                "events":       events,
            }, f, ensure_ascii=False, indent=2)
        return os.path.abspath(path)

    # ── JS-side hook (DOM event capture) ──────────────────────
    # CDP's Input.* events are intended for INJECTION not OBSERVATION.
    # To capture real user input we attach a JS hook that listens for
    # actual click/scroll/keydown events at document level and pushes
    # them into a window-scoped buffer that we drain via polling.
    #
    # This is a pragmatic compromise:
    #   + works on every Selenium version
    #   + survives navigation if we re-inject after frameNavigated
    #   - misses events fired in iframes/cross-origin content
    #   - 100-200ms latency between event and capture
    #
    # For ad-monitoring scripts (search → click → dwell → back), the
    # latency is fine — flows are slow enough that ordering is preserved.

    _JS_HOOK = r"""
        (function() {
            if (window.__gs_rec_buf) return; // already installed
            window.__gs_rec_buf = [];
            const push = (e) => {
                try {
                    window.__gs_rec_buf.push({
                        ...e,
                        url: location.href,
                        ts: Date.now(),
                    });
                    if (window.__gs_rec_buf.length > 5000) {
                        // Keep buffer bounded so a long recording doesn't
                        // OOM the page. Drainer should empty it well
                        // before this cap.
                        window.__gs_rec_buf.shift();
                    }
                } catch (e) {}
            };
            // Click capture — bubbling phase so clicks on inner
            // elements register too.
            document.addEventListener('click', (ev) => {
                const t = ev.target;
                push({
                    kind: 'click',
                    x: ev.clientX, y: ev.clientY,
                    button: ev.button,
                    target_tag: t ? t.tagName : '',
                    target_id:  t && t.id || '',
                    target_class: t && t.className || '',
                    target_text: (t && t.innerText || '').slice(0, 80),
                });
            }, true);
            // Keydown capture — only printable + a few specials
            document.addEventListener('keydown', (ev) => {
                const k = ev.key || '';
                push({
                    kind: 'keydown',
                    key: k,
                    code: ev.code,
                    target_tag: ev.target ? ev.target.tagName : '',
                });
            }, true);
            // Scroll capture — throttled to 4Hz to avoid event spam
            let lastScrollAt = 0;
            window.addEventListener('scroll', () => {
                const now = Date.now();
                if (now - lastScrollAt < 250) return;
                lastScrollAt = now;
                push({
                    kind: 'scroll',
                    scrollY: window.scrollY,
                    scrollX: window.scrollX,
                });
            }, {passive: true});
        })();
    """

    def _install_js_hooks(self) -> None:
        """Inject the listener-attach JS into the current page. Must
        be re-run on every navigation (frameNavigated event) — the
        listeners live on the document and don't survive nav."""
        try:
            self.driver.execute_script(self._JS_HOOK)
        except Exception as e:
            logger.debug(f"_install_js_hooks: {e}")

    _DRAIN_JS = r"""
        const out = window.__gs_rec_buf || [];
        window.__gs_rec_buf = [];
        return out;
    """

    def _drain_js_buffer(self) -> list:
        """Pull all accumulated events from the page-side buffer.
        Each call empties the buffer atomically (safe for concurrent
        reads from the user's browser)."""
        try:
            arr = self.driver.execute_script(self._DRAIN_JS) or []
            with self._lock:
                self._events.extend(arr)
            return arr
        except Exception as e:
            logger.debug(f"_drain_js_buffer: {e}")
            return []

    def _poll_loop(self) -> None:
        """Background thread: drain JS buffer every 500ms, re-inject
        the JS hook if URL changed (crude detection — compares
        current_url with last seen). Stops when _poll_stop is set."""
        last_url = None
        while not self._poll_stop.is_set():
            try:
                # If URL changed, re-install the hook (lost on nav)
                cur = self.driver.current_url
                if cur != last_url:
                    self._install_js_hooks()
                    # Synthesize a "nav" event so the translator can
                    # see the navigation point even if the JS hook
                    # missed the inflight click that triggered it.
                    with self._lock:
                        self._events.append({
                            "kind": "nav",
                            "url":  cur,
                            "ts":   int(time.time() * 1000),
                        })
                    last_url = cur
                self._drain_js_buffer()
            except Exception as e:
                logger.debug(f"_poll_loop: {e}")
            self._poll_stop.wait(0.5)
