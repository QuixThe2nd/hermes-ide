"""Bot Desktop display RPCs: the ``display.*`` surface ``methods_display.py`` serves.

Shapes are typed from ``tools/bot_desktop/runtime.py::DesktopStatus.as_dict`` (flat snapshot),
``tools/bot_desktop/lease.py::Lease.as_dict`` and the handlers themselves. The install flow's
server→client request (``display.install.sudo``) lives in ``server_requests.py`` with the other
masked prompts; its ``display.install.log`` / ``display.install.done`` events stream here.
"""

from __future__ import annotations

from pydantic import Field

from .base import Params, Payload, Result, WireEnum
from .common import ProfileParams
from .registry import event, method


class DisplayLeaseHolder(WireEnum):
    """``tools/bot_desktop/lease.py``: the agent by default, exactly one human viewer after Take over."""

    agent = "agent"
    human = "human"


class DisplayLease(Result):
    """``lease.py::Lease.as_dict`` — who may drive this profile's screen right now."""

    holder: DisplayLeaseHolder
    viewer_id: str | None = None
    since: float
    reason: str = ""
    pending_handoff: str | None = None


class DesktopRuntimeStatus(Result):
    """``tools/bot_desktop/runtime.py::DesktopStatus.as_dict`` — the process state itself."""

    profile: str
    supported: bool
    installed: bool
    missing: list[str]
    running: bool
    pid: int | None = None
    display: str | None = None
    socket: str | None = None
    geometry: str
    install_command: str | None = None


class DisplayStatusResult(DesktopRuntimeStatus):
    """``methods_display.py::_display_snapshot``: the runtime status flattened, plus the lease and
    the profile key the snapshot was resolved for (multiplexed gateways answer per bot)."""

    profile_key: str
    lease: DisplayLease


method("display.status", params=ProfileParams, result=DisplayStatusResult,
       doc="Runtime + lease snapshot of a profile's Bot Desktop (the pane's state authority).")
method("display.start", params=ProfileParams, result=DisplayStatusResult,
       doc="Start this profile's headless Xvnc/Xfce session; returns the fresh snapshot.")


class DisplayStopResult(DisplayStatusResult):
    stopped: bool


method("display.stop", params=ProfileParams, result=DisplayStopResult,
       doc="Stop the headless session (releasing any lease first); ``stopped`` is the teardown verdict.")


class DisplayObserveParams(ProfileParams):
    viewer_id: str | None = None


class DisplayObserveResult(DisplayStatusResult):
    ticket: str
    path: str
    viewer_id: str


method("display.observe", params=DisplayObserveParams, result=DisplayObserveResult,
       doc="Mint the single-use, 30 s RFB ticket the viewer redeems on ``/api/display/ws``.")


class DisplayInstallParams(ProfileParams):
    """The desktop's Install card: profile from the routed call, ``session_id`` only so the sudo
    card can name the session the install was started from (absent for sessionless installs)."""

    session_id: str | None = None


class DisplayInstallResult(Result):
    started: bool
    command: str
    profile_key: str


method("display.install", params=DisplayInstallParams, result=DisplayInstallResult,
       doc="Start the one-click distro package install in the background (sudo card + log/done events).")


class DisplayLeaseAcquireParams(ProfileParams):
    viewer_id: str
    reason: str = ""


class DisplayLeaseResult(Result):
    lease: DisplayLease


method("display.lease.acquire", params=DisplayLeaseAcquireParams, result=DisplayLeaseResult,
       doc="Take over: hand control of the screen to one human viewer.")
method("display.lease.release", params=DisplayObserveParams, result=DisplayLeaseResult,
       doc="Hand back (or release the current holder when no ``viewer_id`` is given).")


# ── events ────────────────────────────────────────────────────────────────────────────────────


class DisplayLeasePayload(Payload):
    profile_key: str
    lease: DisplayLease


event("display.lease", DisplayLeasePayload,
      doc="A lease transition every connected client repaints from (bot-row badge, viewer border).")


class DisplayInstallLogPayload(Payload):
    profile_key: str
    line: str


event("display.install.log", DisplayInstallLogPayload,
      doc="One streamed stdout line of the running package install.")


class DisplayInstallDonePayload(Payload):
    profile_key: str
    code: int
    status: DesktopRuntimeStatus = Field(..., description="fresh runtime snapshot without the lease")


event("display.install.done", DisplayInstallDonePayload,
      doc="The install run ended (``code``); carries the fresh status snapshot for the repaint.")
