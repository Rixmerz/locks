"""locks: one core that routes each task to the right model, effort and capabilities."""
from __future__ import annotations

from locks import legacy as _legacy

__version__ = "0.97.0"

# Every variable of the old name a person or an older process set reads as its `LOCKS_*`, before anything
# else of the package looks at the environment.
_legacy.adopt_env()
