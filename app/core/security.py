"""Password hashing and session-token handling (S21).

Two different hashes for two different threat models, which is the only non-obvious thing
here. A **password** is low-entropy and chosen by a person, so it is hashed with Argon2id:
deliberately slow and memory-hard, so an attacker holding the table cannot enumerate
candidates faster than the parameters allow. A **session token** is 256 bits straight from
the system CSPRNG, so there are no candidates to enumerate — the only property the stored
value needs is that reading the table does not hand over a usable credential. SHA-256 gives
exactly that, and Argon2 would be actively wrong for it, because it would put a deliberately
slow hash on the path of *every authenticated request*.

The token is therefore stored as a fingerprint and never in full: a database dump, a backup
(S60) or a log line cannot be replayed as a login.
"""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# Long enough to be worth requiring, short enough not to push people towards reuse. A length
# floor rather than a character-class rule, which is what current guidance actually supports.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 1024  # Argon2 hashes the whole input; an unbounded one is a DoS vector.

_TOKEN_BYTES = 32  # 256 bits.

_hasher = PasswordHasher()

# Verifying against a real hash for an address that does not exist keeps the failed-login path
# the same shape whether or not the account is there. Without it, "unknown email" returns in
# microseconds and "wrong password" in ~50ms, and the difference is an account-enumeration
# oracle that needs no successful login to read.
_DUMMY_HASH = _hasher.hash("a password that is nobody's password")


def hash_password(password: str) -> str:
    """Argon2id hash of ``password``, salt and parameters embedded in the returned string."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Whether ``password`` matches ``password_hash``.

    A learner with no password hash — one created before S21, or one that will authenticate
    through some other identity source later — can never match, but still costs a verification,
    so absence is not distinguishable from a wrong password by timing either.
    """
    if password_hash is None:
        verify_dummy()
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_dummy() -> None:
    """Spend a verification against a throwaway hash, so a miss costs what a hit costs."""
    try:
        _hasher.verify(_DUMMY_HASH, "")
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass


def new_session_token() -> str:
    """A fresh opaque session token. Returned once, to the client, and never stored."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """The stored form of a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, for the places a fingerprint is compared rather than queried."""
    return hmac.compare_digest(left, right)
