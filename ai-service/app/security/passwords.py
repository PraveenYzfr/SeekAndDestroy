"""Password hashing and verification.

scrypt from the standard library - no new dependency, and a memory-hard KDF
rather than a bare digest. SHA-256 of a password is not password hashing: it
is fast, which is exactly the property an attacker with a stolen hash wants.

The stored value is self-describing::

    scrypt$n=16384,r=8,p=1$<base64 salt>$<base64 derived key>

Parameters travel with each hash rather than being read from today's
constants, so :data:`DEFAULT_N` can be raised later without invalidating
existing rows - old hashes keep verifying under the parameters they were
created with, and :func:`needs_rehash` reports which ones are now below
policy so they can be upgraded on next successful sign-in.

Nothing here ever logs, returns or stores a plaintext password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

ALGORITHM = "scrypt"
DEFAULT_N = 16_384  # CPU/memory cost - must be a power of two
DEFAULT_R = 8       # block size
DEFAULT_P = 1       # parallelisation
SALT_BYTES = 16
KEY_BYTES = 32

#: scrypt needs roughly 128 * N * r bytes. At N=16384, r=8 that is ~16 MB;
#: hashlib's default maxmem of 0 means "use the OpenSSL default", which is
#: lower than that, so it has to be raised explicitly or scrypt raises.
_MAXMEM = 64 * 1024 * 1024


class InvalidPasswordHash(ValueError):
    """The stored value is not a hash this module can read."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def hash_password(password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P) -> str:
    """Derive a storable hash. A fresh random salt every call, so the same
    password hashed twice never produces the same string.
    """
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES, maxmem=_MAXMEM)
    return f"{ALGORITHM}$n={n},r={r},p={p}${_b64e(salt)}${_b64e(key)}"


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes]:
    try:
        algorithm, params, salt_b64, key_b64 = stored.split("$")
    except ValueError as exc:
        raise InvalidPasswordHash("malformed password hash") from exc
    if algorithm != ALGORITHM:
        raise InvalidPasswordHash(f"unsupported password hash algorithm: {algorithm!r}")
    try:
        parsed = dict(part.split("=", 1) for part in params.split(","))
        n, r, p = int(parsed["n"]), int(parsed["r"]), int(parsed["p"])
    except (ValueError, KeyError) as exc:
        raise InvalidPasswordHash("malformed scrypt parameters") from exc
    return n, r, p, _b64d(salt_b64), _b64d(key_b64)


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time verification. Returns False - never raises - for an
    absent or unreadable hash, so a caller cannot accidentally distinguish
    "no password set" from "wrong password" by catching an exception.
    """
    if not stored or not password:
        return False
    try:
        n, r, p, salt, expected = _parse(stored)
    except InvalidPasswordHash:
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected), maxmem=_MAXMEM
        )
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(candidate, expected)


def needs_rehash(stored: str | None, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P) -> bool:
    """True when a stored hash is below current policy and should be upgraded
    after the next successful sign-in (when the plaintext is briefly in hand).
    """
    if not stored:
        return False
    try:
        stored_n, stored_r, stored_p, _salt, _key = _parse(stored)
    except InvalidPasswordHash:
        return True
    return (stored_n, stored_r, stored_p) != (n, r, p)
