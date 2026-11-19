"""Turn-liveness activity tracking for ``AIAgent``.

The runtime method bodies live on ``AIAgent``; this mixin retains the composed class shape.
"""


class ActivityTrackingMixin:
    """Liveness timestamps/labels and rate-limited session activity persistence."""
