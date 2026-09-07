"""Application-level encryption for the content users write.

READ THE THREAT MODEL BEFORE ASSUMING WHAT THIS PROTECTS
-------------------------------------------------------
What it protects against: anyone who gets at the *stored data* without
getting at the app process. A leaked or stolen database dump, a
compromised Supabase project, a misconfigured RLS policy, an support
engineer or future collaborator with console/SQL access to the tables --
all of them see ciphertext instead of tenants' housing situations.

What it does NOT protect against: a compromised app server. The key lives
in that process's environment, so anything that can run code there can
decrypt. This is deliberate, not an oversight, because the alternative is
not available to this product:

    True end-to-end encryption -- where only the user's own password can
    unlock their messages and the server genuinely cannot read them -- is
    incompatible with what this app does. Gemini has to read the
    conversation server-side to answer it, and the RA-81 PDF is filled in
    from the same text. Encrypting so the server can't read it would mean
    the assistant can't read it either, and there would be no product
    left. It would also mean a forgotten password destroys every
    conversation permanently, since there would be nothing left that
    could re-derive the key.

So the honest description is "encrypted at rest, under a key the
application holds" -- a real and worthwhile boundary, but not "only the
user can ever read this".

DESIGNED TO BE CHANGED LATER
----------------------------
Encryption schemes age. This one is built so the cipher and the key can
both be replaced without a migration, a downtime window, or touching rows
that were written years earlier. Every ciphertext carries its own
envelope saying how to read it:

    enc:v<algorithm version>:<key id>:<base64 payload>

- The ALGORITHM VERSION selects a class from _ALGORITHMS. Adding
  AES-SIV, XChaCha20-Poly1305, or an AAD-binding variant later means
  registering it as version 2 and pointing CURRENT_VERSION at it. Old
  rows keep decrypting through version 1 forever.
- The KEY ID selects a key from the configured set. Rotating means adding
  a new key, pointing the active id at it, and keeping the old one listed
  so existing rows still open. Nothing has to be re-encrypted at once.
- needs_rewrap() reports whether a value is behind the current scheme, so
  callers can quietly upgrade rows as they touch them -- the migration
  happens in the background, spread over normal use, instead of as a
  single risky batch job.

Configuration (all optional -- unset means encryption is OFF and every
function here is a pass-through, so deploying this cannot break a running
app that has no keys configured yet):

    DATA_ENCRYPTION_KEYS      "k1:<base64 32 bytes>,k2:<base64 32 bytes>"
    DATA_ENCRYPTION_ACTIVE_KEY_ID   which of those to encrypt new data with

Generate a key with:
    python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
"""

import base64
import logging
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

ENVELOPE_PREFIX = "enc:"


class DecryptionError(Exception):
    """Raised when a value carries an envelope we cannot open.

    Callers should let this surface rather than swallowing it: silently
    returning ciphertext (or an empty string) as if it were the user's
    message would corrupt what they see and, worse, could get written
    back over the real data.
    """


class AesGcmV1:
    """AES-256-GCM. Authenticated, so tampering fails loudly rather than
    decrypting to garbage. 96-bit nonce, freshly random per message, which
    is the size the GCM spec is defined around."""

    version = 1
    key_bytes = 32
    nonce_bytes = 12

    @classmethod
    def encrypt(cls, key: bytes, plaintext: str) -> bytes:
        nonce = os.urandom(cls.nonce_bytes)
        ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return nonce + ciphertext

    @classmethod
    def decrypt(cls, key: bytes, payload: bytes) -> str:
        nonce, ciphertext = payload[: cls.nonce_bytes], payload[cls.nonce_bytes :]
        return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


# Version -> implementation. Add new schemes here; never remove an old one
# while any row might still be encrypted with it.
_ALGORITHMS = {AesGcmV1.version: AesGcmV1}

# What new writes use. Bump this (after registering the new class above)
# to start writing under a stronger scheme; reads keep working throughout.
CURRENT_VERSION = AesGcmV1.version


def _load_keys() -> tuple[dict[str, bytes], str | None]:
    """Parse the configured keys. Returns ({key_id: key}, active_key_id).

    A malformed entry is skipped with a warning rather than crashing the
    app: losing the ability to encrypt new rows is bad, but taking the
    whole service down -- including for users whose data is plaintext and
    perfectly readable -- is worse.
    """
    raw = os.environ.get("DATA_ENCRYPTION_KEYS", "").strip()
    if not raw:
        return {}, None

    keys: dict[str, bytes] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            logger.warning("Ignoring malformed DATA_ENCRYPTION_KEYS entry (expected 'id:base64key').")
            continue
        key_id, _, encoded = entry.partition(":")
        key_id = key_id.strip()
        try:
            key = base64.b64decode(encoded.strip(), validate=True)
        except Exception:
            logger.warning("Ignoring DATA_ENCRYPTION_KEYS entry %r: not valid base64.", key_id)
            continue
        if len(key) != AesGcmV1.key_bytes:
            logger.warning(
                "Ignoring DATA_ENCRYPTION_KEYS entry %r: key is %d bytes, expected %d.",
                key_id, len(key), AesGcmV1.key_bytes,
            )
            continue
        keys[key_id] = key

    active = os.environ.get("DATA_ENCRYPTION_ACTIVE_KEY_ID", "").strip() or None
    if active is None and len(keys) == 1:
        # One key configured and no explicit choice: there's only one thing
        # it could mean, so don't make that a configuration error.
        active = next(iter(keys))
    if active is not None and active not in keys:
        logger.warning(
            "DATA_ENCRYPTION_ACTIVE_KEY_ID=%r is not among the configured keys; "
            "new data will be written unencrypted.", active,
        )
        active = None
    return keys, active


_KEYS, _ACTIVE_KEY_ID = _load_keys()


def reload_keys() -> None:
    """Re-read the environment. Used by tests, and usable from a shell to
    pick up a rotation without a full restart."""
    global _KEYS, _ACTIVE_KEY_ID
    _KEYS, _ACTIVE_KEY_ID = _load_keys()


def is_enabled() -> bool:
    """Whether new writes will actually be encrypted."""
    return _ACTIVE_KEY_ID is not None


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def encrypt(plaintext: str | None) -> str | None:
    """Wrap a value for storage. Returns it unchanged when encryption is
    not configured, so this is safe to deploy ahead of any key."""
    if plaintext is None or plaintext == "" or not is_enabled():
        return plaintext
    if is_encrypted(plaintext):
        return plaintext  # already wrapped; never double-encrypt

    algorithm = _ALGORITHMS[CURRENT_VERSION]
    payload = algorithm.encrypt(_KEYS[_ACTIVE_KEY_ID], plaintext)
    return f"{ENVELOPE_PREFIX}v{CURRENT_VERSION}:{_ACTIVE_KEY_ID}:{base64.b64encode(payload).decode('ascii')}"


def decrypt(value: str | None) -> str | None:
    """Unwrap a stored value.

    Anything without an envelope is returned as-is -- that's every row
    written before encryption was turned on, and it is why this can be
    enabled on a live database with existing data and no migration.
    """
    if not is_encrypted(value):
        return value

    try:
        _, version_part, key_id, encoded = value.split(":", 3)
        version = int(version_part.lstrip("v"))
    except (ValueError, AttributeError) as exc:
        raise DecryptionError(f"Malformed ciphertext envelope: {exc}") from exc

    algorithm = _ALGORITHMS.get(version)
    if algorithm is None:
        raise DecryptionError(
            f"Ciphertext was written with algorithm version {version}, which this "
            "build does not know how to read. This means a rollback past the "
            "version that wrote it -- deploy the newer build again."
        )
    key = _KEYS.get(key_id)
    if key is None:
        raise DecryptionError(
            f"Ciphertext was encrypted with key {key_id!r}, which is not in "
            "DATA_ENCRYPTION_KEYS. A retired key must stay listed for as long "
            "as any row still uses it."
        )

    try:
        return algorithm.decrypt(key, base64.b64decode(encoded))
    except (InvalidTag, ValueError) as exc:
        raise DecryptionError(
            "Ciphertext failed authentication -- it was modified, truncated, or "
            "encrypted under a different key with the same id."
        ) from exc


def needs_rewrap(value: str | None) -> bool:
    """Whether this value is behind the current scheme.

    True for plaintext that could now be encrypted, and for ciphertext
    written under an older algorithm version or a non-active key. Callers
    use this to upgrade rows opportunistically as they read them, so a
    rotation or an algorithm change migrates itself over normal use
    instead of needing a big-bang re-encryption.
    """
    if not is_enabled():
        return False
    if not is_encrypted(value):
        return bool(value)
    try:
        _, version_part, key_id, _ = value.split(":", 3)
        return int(version_part.lstrip("v")) != CURRENT_VERSION or key_id != _ACTIVE_KEY_ID
    except ValueError:
        return False


def describe() -> dict:
    """Non-secret summary, for logs and a future admin/health view."""
    return {
        "enabled": is_enabled(),
        "current_version": CURRENT_VERSION,
        "known_versions": sorted(_ALGORITHMS),
        "active_key_id": _ACTIVE_KEY_ID,
        "loaded_key_ids": sorted(_KEYS),
    }
