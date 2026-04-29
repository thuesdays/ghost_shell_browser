"""
ghost_shell.captcha — CAPTCHA solver integration (Phase B, Apr 2026).

Provides a uniform interface to third-party captcha-solving services
(2Captcha, Anti-Captcha, CapSolver). When `actions/runner.py` or
`main.py` detects a captcha gate it delegates to `solve_on_page()`
which selects the configured provider, extracts the sitekey, submits
to the provider, polls for the token, and injects the result back
into the page.

Replacing the old "captcha → rotate IP" reflex with "captcha → solve →
continue" extends session lifetime by 5-10× — clicked-through ads on
warm cookies is more valuable than a fresh IP with no engagement
history.

Public API:
    from ghost_shell.captcha import (
        solve_on_page, get_provider, list_providers, ProviderError,
    )
"""

from .solvers import (
    solve_on_page,
    get_provider,
    list_providers,
    ProviderError,
    BaseSolver,
)

__all__ = [
    "solve_on_page",
    "get_provider",
    "list_providers",
    "ProviderError",
    "BaseSolver",
]
