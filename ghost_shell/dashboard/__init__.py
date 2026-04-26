"""Flask dashboard server.

Intentionally does NOT eagerly `from .server import app` — that side
effect imports server.py as `ghost_shell.dashboard.server`, which then
clashes with `runpy.run_module("ghost_shell.dashboard.server",
run_name="__main__")` in `ghost_shell/__main__.py` and produces:

    RuntimeWarning: 'ghost_shell.dashboard.server' found in
    sys.modules after import of package 'ghost_shell.dashboard',
    but prior to execution of 'ghost_shell.dashboard.server';
    this may result in unpredictable behaviour

Anyone who needs the Flask app object should import it explicitly:

    from ghost_shell.dashboard.server import app
"""

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"
