"""NLA inference and residual-steering primitives for released model families.

Runtime symbols are loaded lazily so pure taxonomy/analysis tools do not need
to import torch and transformers just to classify archived metadata.
"""

from __future__ import annotations

from . import probes

__all__ = ["NLASession", "LAYER", "MODEL_DIRS", "models_dir", "probes"]


def __getattr__(name: str):
    if name in {"NLASession", "LAYER", "MODEL_DIRS", "models_dir"}:
        from . import session
        return getattr(session, name)
    raise AttributeError(name)
