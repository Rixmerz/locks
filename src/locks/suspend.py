"""How long this machine has been suspended since it booted, read from two clocks: one that
counts a suspend and one that does not (Linux: `CLOCK_BOOTTIME` and `CLOCK_MONOTONIC`; macOS:
`CLOCK_MONOTONIC` and `CLOCK_UPTIME_RAW`). Two readings apart say how much of the time between
them the machine slept: a laptop closed over a weekend with an attempt open was read as days a
dead worker went unswept. None where neither pair exists. Stdlib, imports nothing of locks."""
from __future__ import annotations

import time

#: A difference under this between the two clocks is their own drift, not a suspend.
NOISE_S = 1.0


def suspended() -> float | None:
    """The difference of a clock that counts a suspend and one that does not, or None where neither pair
    exists: its growth between two readings of one boot is the time slept between them (`slept`)."""
    pairs = (("CLOCK_BOOTTIME", "CLOCK_MONOTONIC"), ("CLOCK_MONOTONIC", "CLOCK_UPTIME_RAW"))
    for with_sleep, without in pairs:
        a, b = getattr(time, with_sleep, None), getattr(time, without, None)
        if a is None or b is None:
            continue
        try:
            # The two clocks may start apart (on macOS `CLOCK_MONOTONIC` reads below `CLOCK_UPTIME_RAW`, #208):
            # the reading is their difference, and only how it grows between two readings is a sleep.
            return round(time.clock_gettime(a) - time.clock_gettime(b), 2)
        except OSError:
            continue
    return None


def slept(before: object, after: object) -> float | None:
    """Seconds slept between two `suspended()` readings of one boot, or None when one is missing or
    they are not of one boot (a smaller later reading)."""
    if not isinstance(before, int | float) or not isinstance(after, int | float) or isinstance(before, bool) \
            or isinstance(after, bool) or after < before - NOISE_S:
        return None
    gone = after - before
    return round(gone, 2) if gone >= NOISE_S else 0.0
