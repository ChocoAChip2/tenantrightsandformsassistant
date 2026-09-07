"""Tests for crypto_service.py -- application-level encryption of user
content, and specifically its ability to be CHANGED later.

The round-trip is the easy part. The tests that matter here are the ones
proving the scheme can move: that a key can be rotated without stranding
data written under the old one, that the algorithm version can be bumped
without a migration, that rows written before encryption was switched on
keep working, and that a tampered value is rejected rather than silently
decrypting to something wrong.
"""

import base64
import os
import unittest
from unittest import mock

import crypto_service


def _key(seed: int) -> str:
    return base64.b64encode(bytes([seed]) * 32).decode()


KEY_A, KEY_B = _key(1), _key(2)


def _configured(keys: str, active: str | None = None):
    """Point crypto_service at a given key set for the duration of a with-block."""
    env = {"DATA_ENCRYPTION_KEYS": keys}
    if active is not None:
        env["DATA_ENCRYPTION_ACTIVE_KEY_ID"] = active
    return mock.patch.dict(os.environ, env, clear=False)


class CryptoTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(crypto_service.reload_keys)


class DisabledByDefaultTests(CryptoTestCase):
    def test_with_no_keys_configured_everything_is_a_passthrough(self):
        """Deploying this ahead of any key must not change behavior -- the
        live app has plaintext rows and no key set, and must keep working
        exactly as before."""
        with mock.patch.dict(os.environ, {"DATA_ENCRYPTION_KEYS": "", "DATA_ENCRYPTION_ACTIVE_KEY_ID": ""}):
            crypto_service.reload_keys()
            self.assertFalse(crypto_service.is_enabled())
            self.assertEqual(crypto_service.encrypt("my landlord won't fix the heat"), "my landlord won't fix the heat")
            self.assertEqual(crypto_service.decrypt("my landlord won't fix the heat"), "my landlord won't fix the heat")
            self.assertFalse(crypto_service.needs_rewrap("anything"))


class RoundTripTests(CryptoTestCase):
    def test_encrypts_and_decrypts(self):
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            secret = "My landlord has not fixed the heat since November."
            sealed = crypto_service.encrypt(secret)

            self.assertNotIn("landlord", sealed)
            self.assertTrue(sealed.startswith("enc:v1:k1:"))
            self.assertEqual(crypto_service.decrypt(sealed), secret)

    def test_same_plaintext_encrypts_differently_each_time(self):
        """A fresh nonce per message. Without this, identical messages
        would be visibly identical in the database, which leaks more than
        it looks like it does across a whole table."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            self.assertNotEqual(crypto_service.encrypt("same text"), crypto_service.encrypt("same text"))

    def test_empty_and_none_pass_through_untouched(self):
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            self.assertIsNone(crypto_service.encrypt(None))
            self.assertEqual(crypto_service.encrypt(""), "")
            self.assertIsNone(crypto_service.decrypt(None))

    def test_already_encrypted_values_are_not_double_wrapped(self):
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            once = crypto_service.encrypt("text")
            self.assertEqual(crypto_service.encrypt(once), once)


class PlaintextCoexistenceTests(CryptoTestCase):
    def test_plaintext_rows_still_read_after_encryption_is_switched_on(self):
        """The rows already in the live database have no envelope. Turning
        encryption on must not orphan them."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            self.assertEqual(crypto_service.decrypt("an old plaintext message"), "an old plaintext message")

    def test_plaintext_is_flagged_for_rewrap_once_encryption_is_on(self):
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            self.assertTrue(crypto_service.needs_rewrap("an old plaintext message"))


class KeyRotationTests(CryptoTestCase):
    def test_data_written_under_a_retired_key_still_decrypts(self):
        """The whole point of rotation support: after moving to a new key,
        every row written under the old one must keep opening, with no
        migration required first."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            old_ciphertext = crypto_service.encrypt("written under the first key")

        with _configured(f"k1:{KEY_A},k2:{KEY_B}", "k2"):
            crypto_service.reload_keys()
            self.assertEqual(crypto_service.decrypt(old_ciphertext), "written under the first key")

    def test_new_writes_use_the_new_key(self):
        with _configured(f"k1:{KEY_A},k2:{KEY_B}", "k2"):
            crypto_service.reload_keys()
            self.assertTrue(crypto_service.encrypt("fresh").startswith("enc:v1:k2:"))

    def test_data_under_a_retired_key_is_flagged_for_rewrap(self):
        """This is what drives the background migration: a row sealed with
        a non-active key is rewritten under the current one as it is read."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            old_ciphertext = crypto_service.encrypt("old")

        with _configured(f"k1:{KEY_A},k2:{KEY_B}", "k2"):
            crypto_service.reload_keys()
            self.assertTrue(crypto_service.needs_rewrap(old_ciphertext))
            rewrapped = crypto_service.encrypt(crypto_service.decrypt(old_ciphertext))
            self.assertTrue(rewrapped.startswith("enc:v1:k2:"))
            self.assertFalse(crypto_service.needs_rewrap(rewrapped))

    def test_dropping_a_key_that_is_still_in_use_fails_loudly(self):
        """Removing an old key too early must raise, not return garbage or
        an empty string -- silently losing a user's messages while
        appearing to work is the worst possible outcome here."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            old_ciphertext = crypto_service.encrypt("still needs k1")

        with _configured(f"k2:{KEY_B}", "k2"):
            crypto_service.reload_keys()
            with self.assertRaises(crypto_service.DecryptionError) as caught:
                crypto_service.decrypt(old_ciphertext)
            self.assertIn("k1", str(caught.exception))


class AlgorithmUpgradeTests(CryptoTestCase):
    def test_a_newer_algorithm_can_be_registered_and_old_data_still_reads(self):
        """Simulates the future upgrade path: register a version 2, point
        CURRENT_VERSION at it, and confirm version 1 rows still open while
        new writes use version 2."""

        class FakeV2(crypto_service.AesGcmV1):
            version = 2

        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            v1_ciphertext = crypto_service.encrypt("written under v1")
            self.assertTrue(v1_ciphertext.startswith("enc:v1:"))

            with mock.patch.dict(crypto_service._ALGORITHMS, {2: FakeV2}), \
                 mock.patch.object(crypto_service, "CURRENT_VERSION", 2):
                self.assertEqual(crypto_service.decrypt(v1_ciphertext), "written under v1")
                self.assertTrue(crypto_service.encrypt("new").startswith("enc:v2:"))
                self.assertTrue(crypto_service.needs_rewrap(v1_ciphertext))

    def test_ciphertext_from_an_unknown_future_version_fails_loudly(self):
        """A rollback to a build that predates the algorithm that wrote a
        row. Better a clear error than a wrong answer."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            with self.assertRaises(crypto_service.DecryptionError) as caught:
                crypto_service.decrypt("enc:v99:k1:" + base64.b64encode(b"x" * 40).decode())
            self.assertIn("99", str(caught.exception))


class TamperDetectionTests(CryptoTestCase):
    def test_modified_ciphertext_is_rejected(self):
        """AES-GCM is authenticated, so someone with write access to the
        database cannot flip bits in a stored message and have it decrypt
        to something different."""
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            sealed = crypto_service.encrypt("the rent was paid on time")
            prefix, payload = sealed.rsplit(":", 1)
            raw = bytearray(base64.b64decode(payload))
            raw[-1] ^= 0x01
            tampered = f"{prefix}:{base64.b64encode(bytes(raw)).decode()}"

            with self.assertRaises(crypto_service.DecryptionError):
                crypto_service.decrypt(tampered)

    def test_a_malformed_envelope_is_rejected(self):
        with _configured(f"k1:{KEY_A}", "k1"):
            crypto_service.reload_keys()
            with self.assertRaises(crypto_service.DecryptionError):
                crypto_service.decrypt("enc:garbage")


class KeyLoadingTests(CryptoTestCase):
    def test_a_wrong_length_key_is_ignored_rather_than_crashing_the_app(self):
        short = base64.b64encode(b"tooshort").decode()
        with _configured(f"k1:{short}", "k1"):
            crypto_service.reload_keys()
            self.assertFalse(crypto_service.is_enabled())

    def test_a_single_key_needs_no_explicit_active_id(self):
        with mock.patch.dict(os.environ, {"DATA_ENCRYPTION_KEYS": f"only:{KEY_A}", "DATA_ENCRYPTION_ACTIVE_KEY_ID": ""}):
            crypto_service.reload_keys()
            self.assertTrue(crypto_service.is_enabled())
            self.assertTrue(crypto_service.encrypt("x").startswith("enc:v1:only:"))

    def test_an_active_id_that_is_not_configured_disables_encryption(self):
        """Fail safe: write plaintext (readable, recoverable) rather than
        encrypting under a key nobody has."""
        with _configured(f"k1:{KEY_A}", "missing"):
            crypto_service.reload_keys()
            self.assertFalse(crypto_service.is_enabled())

    def test_describe_reports_state_without_leaking_key_material(self):
        with _configured(f"k1:{KEY_A},k2:{KEY_B}", "k2"):
            crypto_service.reload_keys()
            described = crypto_service.describe()
            self.assertEqual(described["active_key_id"], "k2")
            self.assertEqual(described["loaded_key_ids"], ["k1", "k2"])
            self.assertNotIn(KEY_A, str(described))
            self.assertNotIn(KEY_B, str(described))


if __name__ == "__main__":
    unittest.main()
