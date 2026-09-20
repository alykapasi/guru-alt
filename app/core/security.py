"""Session-token handling (S21).

A session token is 256 bits straight from the system CSPRNG, so there are no candidates to
enumerate — the only property the stored value needs is that reading the table does not hand
over a usable credential. SHA-256 gives exactly that, and a deliberately slow hash would be
actively wrong here, because it would sit on the path of *every authenticated request*.

The token is therefore stored as a fingerprint and never in full: a database dump, a backup
(S60) or a log line cannot be replayed as a login.

Password hashing used to live here too, with Argon2id and a dummy-verify to keep the
failed-login path a constant shape. Clerk owns credentials now and Guru stores none, so the
whole apparatus — and the enumeration oracle it existed to close — is gone rather than
maintained unused.
"""

import hashlib
import hmac
import secrets

_TOKEN_BYTES = 32  # 256 bits.


def new_session_token() -> str:
    """A fresh opaque session token. Returned once, to the client, and never stored."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """The stored form of a session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    """Constant-time comparison, for the places a fingerprint is compared rather than queried."""
    return hmac.compare_digest(left, right)
