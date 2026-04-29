"""
ghost_shell.recorder — Session Recording & Replay (Phase C, Apr 2026).

Records a manual browsing session via CDP events and converts the
event stream into a unified-flow JSON script that the action runner
can replay on N other profiles.

Workflow:
    1. User opens a profile and clicks "Record" in the dashboard.
    2. dashboard/server.py marks the session recording=True in DB.
    3. Inside main.py, after launch, the recorder attaches CDP
       listeners (Page.frameNavigated, Input.dispatchMouseEvent etc).
    4. User performs the workflow manually in the live browser.
    5. User clicks "Stop" — recorder serialises the buffered events
       to ./recordings/<profile>/<timestamp>.events.json
    6. translator.py converts events → unified-flow JSON. The user
       is prompted to save as a Script in the library.

Public API:
    from ghost_shell.recorder import (
        Recorder, translate_events_to_flow,
    )
"""

from .cdp_recorder import Recorder
from .translator import translate_events_to_flow

__all__ = ["Recorder", "translate_events_to_flow"]
