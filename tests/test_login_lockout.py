"""Unit tests for login_lockout.py, exercised directly against the module
rather than through HTTP -- the login route itself is limited to 10 POSTs
per minute by Flask-Limiter (see rate_limit.py and routes.py), so driving
the lockout threshold (also 10) through real requests would hit that
separate rate limit first and never actually reach the lockout logic.
tests/test_security_hardening.py covers the route-level wiring (the flash
message, the request never reaching Supabase) using this module's
functions directly to arrange the "already locked out" state instead of
sending ten real requests.
"""

import time
import unittest

import login_lockout as lockout


class LoginLockoutTestCase(unittest.TestCase):
    def setUp(self):
        lockout.reset_all()


class RecordFailureTests(LoginLockoutTestCase):
    def test_not_locked_before_reaching_the_threshold(self):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS - 1):
            lockout.record_failure("1.2.3.4")

        self.assertEqual(lockout.seconds_until_unlocked("1.2.3.4"), 0)

    def test_locks_out_once_the_threshold_is_reached(self):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure("1.2.3.4")

        remaining = lockout.seconds_until_unlocked("1.2.3.4")
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, lockout.BASE_LOCKOUT_SECONDS)

    def test_keys_are_tracked_independently(self):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure("1.2.3.4")

        self.assertEqual(lockout.seconds_until_unlocked("5.6.7.8"), 0)

    def test_failures_while_already_locked_do_not_extend_the_lock(self):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure("1.2.3.4")
        first_remaining = lockout.seconds_until_unlocked("1.2.3.4")

        lockout.record_failure("1.2.3.4")
        second_remaining = lockout.seconds_until_unlocked("1.2.3.4")

        # Allow a hair of clock drift between the two reads rather than
        # requiring bit-for-bit equality.
        self.assertLessEqual(second_remaining, first_remaining)


class ExponentialBackoffTests(LoginLockoutTestCase):
    def _lock_once(self, key):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure(key)

    def test_a_second_lockout_after_the_first_expires_lasts_twice_as_long(self):
        key = "1.2.3.4"
        self._lock_once(key)
        first_duration = lockout.seconds_until_unlocked(key)

        # Simulate the first lock having already expired, then trip a
        # second one -- record_failure only checks `locked_until`, so
        # backdating it has the same effect as time actually passing.
        lockout._STORE[key]["locked_until"] = time.time() - 1
        self._lock_once(key)
        second_duration = lockout.seconds_until_unlocked(key)

        # +/- a couple of seconds for the wall-clock time this test itself
        # takes to run between the two measurements.
        self.assertGreater(second_duration, first_duration * 1.9)

    def test_lockout_duration_is_capped(self):
        key = "1.2.3.4"
        for cycle in range(10):
            self._lock_once(key)
            lockout._STORE[key]["locked_until"] = time.time() - 1

        # One more cycle to leave it actually locked, then check the cap.
        self._lock_once(key)
        self.assertLessEqual(
            lockout.seconds_until_unlocked(key), lockout.MAX_LOCKOUT_SECONDS
        )


class RecordSuccessTests(LoginLockoutTestCase):
    def test_success_clears_an_in_progress_failure_streak(self):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS - 1):
            lockout.record_failure("1.2.3.4")

        lockout.record_success("1.2.3.4")

        # If the streak weren't cleared, one more failure would tip it over
        # the threshold and lock the key out.
        lockout.record_failure("1.2.3.4")
        self.assertEqual(lockout.seconds_until_unlocked("1.2.3.4"), 0)

    def test_success_clears_an_active_lock(self):
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure("1.2.3.4")
        self.assertGreater(lockout.seconds_until_unlocked("1.2.3.4"), 0)

        lockout.record_success("1.2.3.4")

        self.assertEqual(lockout.seconds_until_unlocked("1.2.3.4"), 0)

    def test_success_does_not_reset_the_exponential_backoff_counter(self):
        """A success after a lockout clears the *current* lock so the user
        can log in right away, but deliberately leaves lockout_count alone
        -- a repeat offender who eventually gets in doesn't earn a fresh
        set of ten free tries with the base one-hour penalty; their next
        lockout still escalates."""
        key = "1.2.3.4"
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure(key)
        first_duration = lockout.seconds_until_unlocked(key)

        lockout.record_success(key)
        for _ in range(lockout.MAX_FAILED_ATTEMPTS):
            lockout.record_failure(key)
        second_duration = lockout.seconds_until_unlocked(key)

        self.assertGreater(second_duration, first_duration * 1.9)


class FormatDurationTests(unittest.TestCase):
    def test_under_a_minute(self):
        self.assertEqual(lockout.format_duration(30), "less than a minute")

    def test_singular_minute(self):
        self.assertEqual(lockout.format_duration(60), "1 minute")

    def test_plural_minutes(self):
        self.assertEqual(lockout.format_duration(300), "5 minutes")

    def test_singular_hour(self):
        self.assertEqual(lockout.format_duration(3600), "1 hour")

    def test_plural_hours(self):
        self.assertEqual(lockout.format_duration(7200), "2 hours")


if __name__ == "__main__":
    unittest.main()
