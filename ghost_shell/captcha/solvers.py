"""
captcha/solvers.py — Provider abstraction for third-party captcha
solving services. Phase B (Apr 2026).

The actions runner detects a captcha gate (recaptcha iframe, hCaptcha
div, Cloudflare Turnstile widget) and calls `solve_on_page(driver)`
which:

  1. Loads provider credentials from DB config + Vault
  2. Detects sitekey + page URL via JS probe
  3. Detects captcha kind (recaptcha v2, recaptcha v3, hcaptcha,
     turnstile)
  4. Submits the task to the configured provider
  5. Polls for completion (typically 15-90s)
  6. Injects the resulting token into the page (g-recaptcha-response,
     h-captcha-response, cf-turnstile-response textareas)
  7. Triggers form-submit / re-validation

All provider interactions are HTTP-only; we never load the provider's
JS into the antidetect browser (would create a third-party fingerprint
contamination).

Per-profile or global credentials. If `vault_item_id` is set on the
provider config, the API key is fetched from the encrypted Vault at
solve time. If not, falls back to plaintext config.

Public API:
    solve_on_page(driver, *, kind=None, sitekey=None, timeout=120) -> str
        Detect (or accept overrides for) captcha info, solve via the
        configured provider, inject the token. Returns the token on
        success, raises ProviderError on failure.

    get_provider(name=None) -> BaseSolver
        Return the active provider instance, or a specific one by name.
        Raises ProviderError if no provider is configured.

    list_providers() -> list[str]
        Catalog of supported provider names.

Adding a new provider:
    1. Subclass BaseSolver
    2. Implement submit_task() and poll_result()
    3. Add a class entry in `PROVIDERS`

Provider docs (links for ops):
    2Captcha:   https://2captcha.com/2captcha-api
    AntiCaptcha: https://anti-captcha.com/apidoc
    CapSolver:  https://docs.capsolver.com/
"""
import json
import logging
import time
import typing as t
from abc import ABC, abstractmethod

import requests

logger = logging.getLogger("captcha.solvers")


class ProviderError(RuntimeError):
    """Solver-side failure. The .reason attribute carries a short
    machine-readable code so callers can branch on the cause without
    parsing the message:

        "no_credentials"   — provider not configured / API key missing
        "no_sitekey"       — could not detect sitekey on the page
        "submit_failed"    — provider rejected the task submission
        "timed_out"        — polling exceeded budget
        "solver_failed"    — provider returned ERROR
        "inject_failed"    — got a token but couldn't inject it
    """

    def __init__(self, message: str, reason: str = "unknown"):
        super().__init__(message)
        self.reason = reason


# ──────────────────────────────────────────────────────────────
# Sitekey detection (runs JS in the page to extract metadata)
# ──────────────────────────────────────────────────────────────

# A single JS probe that finds the dominant captcha widget on the page
# and returns its kind, sitekey, and any extra params (action, etc).
# Order matters — Cloudflare Turnstile is checked first because it
# can wrap reCAPTCHA, then hCaptcha, then reCAPTCHA. The first hit
# wins. Returns null if no widget is detected.
_DETECT_JS = r"""
(function() {
    function from_attr(sel, attr) {
        const el = document.querySelector(sel);
        return el ? (el.getAttribute(attr) || "") : "";
    }
    // Cloudflare Turnstile
    {
        const sk = from_attr('.cf-turnstile, [data-sitekey][class*="turnstile"]', 'data-sitekey');
        if (sk) {
            return {kind: "turnstile", sitekey: sk, url: location.href};
        }
    }
    // hCaptcha
    {
        const sk = from_attr('.h-captcha, [data-sitekey][class*="h-captcha"]', 'data-sitekey');
        if (sk) {
            return {kind: "hcaptcha", sitekey: sk, url: location.href};
        }
    }
    // reCAPTCHA v2 (visible checkbox or invisible)
    {
        const sk = from_attr('.g-recaptcha, [data-sitekey]', 'data-sitekey');
        if (sk) {
            // v3 is iframe-only with action= field — check for presence
            const v3iframe = document.querySelector('iframe[src*="recaptcha/api2/anchor"][src*="invisible"]');
            return {
                kind: v3iframe ? "recaptcha_v3" : "recaptcha_v2",
                sitekey: sk,
                url: location.href
            };
        }
    }
    // reCAPTCHA v2 fallback — iframe-only embed
    {
        const ifr = document.querySelector('iframe[src*="recaptcha/api2/anchor"]');
        if (ifr) {
            const m = ifr.src.match(/[?&]k=([^&]+)/);
            if (m) {
                return {
                    kind: "recaptcha_v2",
                    sitekey: decodeURIComponent(m[1]),
                    url: location.href
                };
            }
        }
    }
    return null;
})();
"""


def detect_captcha_on_page(driver) -> t.Optional[dict]:
    """Run the JS probe and return {kind, sitekey, url} or None.

    Returns None on detection failure (no widget present, JS error,
    or driver disconnected). Never raises — captcha detection is a
    soft check.
    """
    try:
        return driver.execute_script(_DETECT_JS)
    except Exception as e:
        logger.debug(f"detect_captcha_on_page: JS probe failed: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# Token injection (universal across kinds)
# ──────────────────────────────────────────────────────────────

# After getting a token from the provider, set the matching textarea/
# input value AND fire a callback if the page registered one. Each
# captcha library uses its own field name:
#   reCAPTCHA → g-recaptcha-response (also a callback in window)
#   hCaptcha  → h-captcha-response
#   Turnstile → cf-turnstile-response
_INJECT_JS = r"""
(function(token, kind) {
    const fieldMap = {
        recaptcha_v2: 'g-recaptcha-response',
        recaptcha_v3: 'g-recaptcha-response',
        hcaptcha:     'h-captcha-response',
        turnstile:    'cf-turnstile-response',
    };
    const field = fieldMap[kind];
    if (!field) return {ok: false, reason: 'unknown_kind'};

    // Set ALL textareas/inputs with this name (sometimes the page has
    // multiple instances — invisible recaptcha + a backup field).
    const els = document.getElementsByName(field);
    let n = 0;
    for (const el of els) {
        if (el && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT')) {
            el.value = token;
            // Some pages listen for input events instead of polling.
            try { el.dispatchEvent(new Event('input', {bubbles: true})); } catch(e){}
            try { el.dispatchEvent(new Event('change', {bubbles: true})); } catch(e){}
            n++;
        }
    }

    // Try to invoke the page-registered callback. reCAPTCHA / hCaptcha
    // both look up a global named function via data-callback attribute
    // on the widget div.
    try {
        const widget = document.querySelector(
            kind.startsWith('recaptcha') ? '.g-recaptcha[data-callback]' :
            kind === 'hcaptcha' ? '.h-captcha[data-callback]' :
            '.cf-turnstile[data-callback]'
        );
        if (widget) {
            const cb = widget.getAttribute('data-callback');
            if (cb && typeof window[cb] === 'function') {
                window[cb](token);
            }
        }
    } catch(e) { /* callback not required for most pages */ }

    return {ok: n > 0, fields_set: n};
})(arguments[0], arguments[1]);
"""


def inject_token(driver, kind: str, token: str) -> bool:
    """Inject the solved token into the page. Returns True on success.
    Caller should usually click submit / refresh / wait for re-validate
    after this returns."""
    try:
        result = driver.execute_script(_INJECT_JS, token, kind) or {}
        ok = bool(result.get("ok"))
        if not ok:
            logger.warning(f"inject_token: ok=False reason={result.get('reason')}")
        return ok
    except Exception as e:
        logger.warning(f"inject_token: JS execution failed: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# Provider base + credentials resolution
# ──────────────────────────────────────────────────────────────

def _load_provider_config() -> dict:
    """Pull captcha provider config from the DB.

    Config keys (under captcha.*):
        captcha.provider              str — active provider name
        captcha.api_key               str — plaintext API key (DEV only)
        captcha.vault_item_id         int — encrypted Vault entry id
                                            (preferred — config_get
                                            returns plain string if set;
                                            we resolve via Vault here)
        captcha.timeout_sec           int — polling budget (default 120)
        captcha.poll_interval_sec     int — initial poll delay (default 5)

    Returns a dict the providers can read. Falls back gracefully if
    the DB layer isn't available (e.g. unit-testing the solver).
    """
    cfg = {
        "provider":          None,
        "api_key":           None,
        "timeout_sec":       120,
        "poll_interval_sec": 5,
    }
    try:
        from ghost_shell.db.database import get_db
        db = get_db()
        cfg["provider"] = db.config_get("captcha.provider")
        cfg["api_key"]  = db.config_get("captcha.api_key")
        cfg["timeout_sec"] = int(
            db.config_get("captcha.timeout_sec", 120) or 120
        )
        cfg["poll_interval_sec"] = int(
            db.config_get("captcha.poll_interval_sec", 5) or 5
        )
        # Vault override — preferred for prod
        vid = db.config_get("captcha.vault_item_id")
        if vid:
            try:
                from ghost_shell.accounts import get_decrypted_secret
                v = get_decrypted_secret(int(vid), "api_key")
                if v:
                    cfg["api_key"] = v
            except Exception as ve:
                logger.debug(f"vault api_key resolve failed: {ve}")
    except Exception as e:
        logger.debug(f"_load_provider_config: db unreachable: {e}")
    return cfg


class BaseSolver(ABC):
    """Abstract base. Every provider sub-classes this and implements
    `submit_task` + `poll_result`. The `solve()` method does the high-
    level orchestration (timeout, retries, error mapping)."""

    name: str = "base"

    def __init__(self, api_key: str, *, timeout_sec: int = 120,
                 poll_interval_sec: int = 5):
        if not api_key:
            raise ProviderError("API key not configured", "no_credentials")
        self.api_key = api_key
        self.timeout_sec = max(30, int(timeout_sec))
        self.poll_interval_sec = max(2, int(poll_interval_sec))
        # Tunable per provider — first poll is always the user's
        # poll_interval_sec; later polls back off. 2Captcha typically
        # needs 15-30s for reCAPTCHA v2; CapSolver is faster (5-15s).
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": f"GhostShell-Captcha/1.0 ({self.name})"
        })

    # ── abstract ─────────────────────────────────────────────
    @abstractmethod
    def submit_task(self, kind: str, sitekey: str, url: str) -> str:
        """Submit a captcha task to the provider. Return a task id
        the poll method will use. Raise ProviderError on failure."""
        ...

    @abstractmethod
    def poll_result(self, task_id: str) -> t.Optional[str]:
        """Poll the provider for the task result. Return the token
        string when ready, None when not-ready-yet (caller will wait
        and retry). Raise ProviderError on permanent failure."""
        ...

    # ── orchestration ───────────────────────────────────────
    def solve(self, kind: str, sitekey: str, url: str) -> str:
        """Submit + poll-with-timeout. Returns token. Raises
        ProviderError(reason='timed_out') on budget exceeded."""
        logger.info(
            f"[captcha:{self.name}] submitting kind={kind} "
            f"sitekey={sitekey[:12]}… url={url[:80]}…"
        )
        task_id = self.submit_task(kind, sitekey, url)
        logger.info(f"[captcha:{self.name}] task_id={task_id} "
                    f"polling up to {self.timeout_sec}s")
        deadline = time.time() + self.timeout_sec
        # First sleep before first poll — most providers can't return
        # a result in <5s anyway. Reduces "not ready" noise.
        time.sleep(self.poll_interval_sec)
        while time.time() < deadline:
            try:
                token = self.poll_result(task_id)
            except ProviderError:
                raise
            except Exception as e:
                logger.warning(
                    f"[captcha:{self.name}] poll exception: {e} "
                    f"— retrying"
                )
                time.sleep(self.poll_interval_sec)
                continue
            if token:
                logger.info(
                    f"[captcha:{self.name}] solved in "
                    f"{int(self.timeout_sec - (deadline - time.time()))}s"
                )
                return token
            time.sleep(self.poll_interval_sec)
        raise ProviderError(
            f"polling exceeded {self.timeout_sec}s",
            reason="timed_out",
        )


# ──────────────────────────────────────────────────────────────
# Concrete providers
# ──────────────────────────────────────────────────────────────

class TwoCaptchaSolver(BaseSolver):
    """2Captcha (api.2captcha.com). Uses the legacy /in.php /res.php
    endpoints — still the official + most reliable interface.
    """
    name = "2captcha"
    BASE = "https://2captcha.com"

    def submit_task(self, kind: str, sitekey: str, url: str) -> str:
        # Map our kinds to 2captcha 'method' parameter
        method_map = {
            "recaptcha_v2": "userrecaptcha",
            "recaptcha_v3": "userrecaptcha",
            "hcaptcha":     "hcaptcha",
            "turnstile":    "turnstile",
        }
        method = method_map.get(kind)
        if not method:
            raise ProviderError(f"unsupported kind for 2Captcha: {kind}",
                                reason="submit_failed")
        params = {
            "key":     self.api_key,
            "method":  method,
            "sitekey": sitekey,
            "pageurl": url,
            "json":    1,
        }
        if kind == "recaptcha_v3":
            params["version"] = "v3"
            params["min_score"] = 0.7
        try:
            r = self._http.get(f"{self.BASE}/in.php", params=params,
                               timeout=15)
            data = r.json()
        except Exception as e:
            raise ProviderError(f"2captcha submit failed: {e}",
                                reason="submit_failed")
        if data.get("status") != 1:
            raise ProviderError(
                f"2captcha rejected: {data.get('request')}",
                reason="submit_failed",
            )
        return str(data["request"])  # task id

    def poll_result(self, task_id: str) -> t.Optional[str]:
        params = {
            "key":    self.api_key,
            "action": "get",
            "id":     task_id,
            "json":   1,
        }
        r = self._http.get(f"{self.BASE}/res.php", params=params,
                           timeout=15)
        data = r.json()
        if data.get("status") == 1:
            return str(data["request"])
        # Not ready yet
        if data.get("request") == "CAPCHA_NOT_READY":
            return None
        # Permanent error
        raise ProviderError(
            f"2captcha solver error: {data.get('request')}",
            reason="solver_failed",
        )


class AntiCaptchaSolver(BaseSolver):
    """Anti-Captcha (api.anti-captcha.com). Modern JSON API."""
    name = "anticaptcha"
    BASE = "https://api.anti-captcha.com"

    def submit_task(self, kind: str, sitekey: str, url: str) -> str:
        type_map = {
            "recaptcha_v2": "RecaptchaV2TaskProxyless",
            "recaptcha_v3": "RecaptchaV3TaskProxyless",
            "hcaptcha":     "HCaptchaTaskProxyless",
            "turnstile":    "TurnstileTaskProxyless",
        }
        task_type = type_map.get(kind)
        if not task_type:
            raise ProviderError(f"unsupported kind: {kind}",
                                reason="submit_failed")
        body = {
            "clientKey": self.api_key,
            "task": {
                "type":       task_type,
                "websiteURL": url,
                "websiteKey": sitekey,
            },
        }
        if kind == "recaptcha_v3":
            body["task"]["minScore"] = 0.7
            body["task"]["pageAction"] = "verify"
        try:
            r = self._http.post(f"{self.BASE}/createTask", json=body,
                                timeout=15)
            data = r.json()
        except Exception as e:
            raise ProviderError(f"anticaptcha submit failed: {e}",
                                reason="submit_failed")
        if data.get("errorId") != 0:
            raise ProviderError(
                f"anticaptcha rejected: {data.get('errorDescription')}",
                reason="submit_failed",
            )
        return str(data["taskId"])

    def poll_result(self, task_id: str) -> t.Optional[str]:
        body = {"clientKey": self.api_key, "taskId": int(task_id)}
        r = self._http.post(f"{self.BASE}/getTaskResult", json=body,
                            timeout=15)
        data = r.json()
        if data.get("errorId") != 0:
            raise ProviderError(
                f"anticaptcha poll error: {data.get('errorDescription')}",
                reason="solver_failed",
            )
        if data.get("status") == "ready":
            sol = data.get("solution") or {}
            # reCAPTCHA / hCaptcha return gRecaptchaResponse;
            # Turnstile returns token directly
            return (sol.get("gRecaptchaResponse")
                    or sol.get("token")
                    or sol.get("captcha"))
        return None  # processing


class CapSolverSolver(BaseSolver):
    """CapSolver (api.capsolver.com). Similar to AntiCaptcha shape."""
    name = "capsolver"
    BASE = "https://api.capsolver.com"

    def submit_task(self, kind: str, sitekey: str, url: str) -> str:
        type_map = {
            "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
            "recaptcha_v3": "ReCaptchaV3TaskProxyLess",
            "hcaptcha":     "HCaptchaTaskProxyLess",
            "turnstile":    "AntiTurnstileTaskProxyLess",
        }
        task_type = type_map.get(kind)
        if not task_type:
            raise ProviderError(f"unsupported kind: {kind}",
                                reason="submit_failed")
        body = {
            "clientKey": self.api_key,
            "task": {
                "type":       task_type,
                "websiteURL": url,
                "websiteKey": sitekey,
            },
        }
        if kind == "recaptcha_v3":
            body["task"]["pageAction"] = "verify"
            body["task"]["minScore"] = 0.7
        try:
            r = self._http.post(f"{self.BASE}/createTask", json=body,
                                timeout=15)
            data = r.json()
        except Exception as e:
            raise ProviderError(f"capsolver submit failed: {e}",
                                reason="submit_failed")
        if data.get("errorId") not in (0, None):
            raise ProviderError(
                f"capsolver rejected: {data.get('errorDescription')}",
                reason="submit_failed",
            )
        return str(data["taskId"])

    def poll_result(self, task_id: str) -> t.Optional[str]:
        body = {"clientKey": self.api_key, "taskId": task_id}
        r = self._http.post(f"{self.BASE}/getTaskResult", json=body,
                            timeout=15)
        data = r.json()
        if data.get("errorId") not in (0, None):
            raise ProviderError(
                f"capsolver poll error: {data.get('errorDescription')}",
                reason="solver_failed",
            )
        if data.get("status") == "ready":
            sol = data.get("solution") or {}
            return (sol.get("gRecaptchaResponse")
                    or sol.get("token")
                    or sol.get("captcha"))
        return None


PROVIDERS: dict = {
    "2captcha":    TwoCaptchaSolver,
    "anticaptcha": AntiCaptchaSolver,
    "capsolver":   CapSolverSolver,
}


def list_providers() -> list:
    """Catalog of supported provider names. UI uses this to populate
    the Settings → Captcha provider dropdown."""
    return sorted(PROVIDERS.keys())


def get_provider(name: str = None) -> BaseSolver:
    """Return a configured solver instance.

    If `name` is None, the active provider from config (`captcha.provider`)
    is used. Raises ProviderError(reason='no_credentials') if no provider
    is configured or its API key is missing.
    """
    cfg = _load_provider_config()
    name = name or cfg.get("provider")
    if not name:
        raise ProviderError("no captcha provider configured",
                            reason="no_credentials")
    cls = PROVIDERS.get(name)
    if not cls:
        raise ProviderError(f"unknown provider: {name}",
                            reason="no_credentials")
    return cls(
        cfg.get("api_key"),
        timeout_sec=cfg.get("timeout_sec", 120),
        poll_interval_sec=cfg.get("poll_interval_sec", 5),
    )


def solve_on_page(driver, *,
                  kind: str = None,
                  sitekey: str = None,
                  url: str = None,
                  timeout: int = None) -> str:
    """High-level entry point: detect → solve → inject. Returns the
    token on success. Caller should still trigger the form-submit /
    page-retry that uses the token.

    Overrides:
      kind / sitekey / url   — skip detection, use these values
      timeout                — override the configured budget
    """
    if not (kind and sitekey):
        info = detect_captcha_on_page(driver) or {}
        kind = kind or info.get("kind")
        sitekey = sitekey or info.get("sitekey")
        url = url or info.get("url")
    if not kind or not sitekey:
        raise ProviderError(
            "could not detect captcha on page",
            reason="no_sitekey",
        )
    if not url:
        try:
            url = driver.current_url
        except Exception:
            url = ""
    solver = get_provider()
    if timeout:
        solver.timeout_sec = max(30, int(timeout))
    token = solver.solve(kind, sitekey, url)
    if not inject_token(driver, kind, token):
        raise ProviderError("token injection failed",
                            reason="inject_failed")
    return token
