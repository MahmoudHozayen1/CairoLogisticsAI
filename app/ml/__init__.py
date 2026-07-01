"""SwiftRoute predictive ML layer.

Public entry point is :func:`get_service`, which lazily loads (and, on first use,
trains) the drop-off/pickup/late-risk predictors and the demand/cost forecaster.
Kept import-light so pulling in the web app never drags in training code.
"""
from .service import get_service


def get_assistant():
    """Lazy accessor for the admin operations assistant (chatbot)."""
    from .assistant import get_assistant as _get
    return _get()


def get_router():
    """Lazy accessor for the learning-to-route pointer policy (Slice 4)."""
    from .neural_router import get_router as _get
    return _get()


def get_behavior_model():
    """Lazy accessor for the courier behaviour model (Slice 5)."""
    from .behavior import get_behavior_model as _get
    return _get()


__all__ = ["get_service", "get_assistant", "get_router", "get_behavior_model"]
