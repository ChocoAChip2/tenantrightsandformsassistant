"""In-memory brute-force lockout for the login route.

Flask-Limiter (see rate_limit.py) already caps /login at 10 POSTs per
minute, which slows a scripted attack down but resets every minute --
fine for "don't let this endpoint fall over," not enough on its own for
"stop someone from grinding through a password list." This module adds a
second, much longer-memory guard on top: after MAX_FAILED_ATTEMPTS wrong
passwords from the same key (the same rate_limit_key() used elsewhere --
the signed-in user_id if there is one, otherwise the caller's IP, which
for a pre-login attempt is always the IP), further attempts are refused
outright for a while, and each time that happens again after the lock
expires, the wait roughly doubles.

Like rate_limit.py, storage is a single in-memory dict -- a deliberate,
documented limitation, not an oversight: it does not survive a restart
and is not shared across multiple gunicorn workers or dynos, so on a
scaled-up deployment a determined attacker gets roughly
(threshold x worker count) attempts rather than exactly ten. Moving this
to a shared store (Redis, the same one that would fix rate_limit.py's
equivalent gap) would close it; this app has no shared cache in its
deployment today, so that's a bigger infrastructure change than "add a
login lockout" calls for. A successful login clears the failure streak
(record_success) but deliberately does NOT reset lockout_count -- the
whole point of the exponential growth is that a repeat offender who
eventually guesses right, waits out the lock, and starts again faces a
longer wait next time, not a fresh ten free tries.
"""

import math
import time

MAX_FAILED_ATTEMPTS = 10
BASE_LOCKOUT_SECONDS = 60 * 60  # 1 hour
# Doubling forever would let one address accumulate a de facto permanent
# ban after a handful of lockout cycles; capping it keeps the penalty
# severe (a full day) without that edge case.
MAX_LOCKOUT_SECONDS = 24 * 60 * 60  # 1 day

# key -> {"failures": int, "locked_until": epoch seconds, "lockout_count": int}
_STORE: dict[str, dict[str, float]] = {}


def seconds_until_unlocked(key: str) -> int:
    """How much longer `key` is locked out, rounded up to a whole second.

    0 means not currently locked (either never failed enough, or the lock
    already expired -- expiry is checked lazily here rather than with a
    background sweep, since this store is small and short-lived).
    """
    entry = _STORE.get(key)
    if not entry:
        return 0
    remaining = entry["locked_until"] - time.time()
    return max(0, math.ceil(remaining))


def record_failure(key: str) -> None:
    """Count one more failed login attempt for `key`.

    A no-op while already locked -- there's no reason to extend an
    already-running lock just because more attempts kept arriving during
    it (those attempts are also the ones seconds_until_unlocked() is
    already telling the caller to refuse before this ever runs).
    """
    entry = _STORE.setdefault(
        key, {"failures": 0, "locked_until": 0.0, "lockout_count": 0}
    )
    if entry["locked_until"] > time.time():
        return

    entry["failures"] += 1
    if entry["failures"] >= MAX_FAILED_ATTEMPTS:
        entry["lockout_count"] += 1
        duration = min(
            BASE_LOCKOUT_SECONDS * (2 ** (entry["lockout_count"] - 1)),
            MAX_LOCKOUT_SECONDS,
        )
        entry["locked_until"] = time.time() + duration
        entry["failures"] = 0


def record_success(key: str) -> None:
    """Clear the failure streak and any active lock for `key`.

    lockout_count is intentionally left alone -- see the module docstring.
    """
    entry = _STORE.get(key)
    if not entry:
        return
    entry["failures"] = 0
    entry["locked_until"] = 0.0


def format_duration(seconds: int) -> str:
    """A short, human-readable wait time for the lockout flash message."""
    if seconds < 60:
        return "less than a minute"
    minutes = math.ceil(seconds / 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = math.ceil(minutes / 60)
    return f"{hours} hour{'s' if hours != 1 else ''}"


def reset_all() -> None:
    """Test-only: clear every tracked key.

    _STORE is a module-level singleton shared across every create_app()
    call in this process (same tradeoff as rate_limit.limiter), so tests
    that exercise /login need to reset it in setUp or an earlier test's
    failures can silently lock out a later, unrelated test.
    """
    _STORE.clear()
