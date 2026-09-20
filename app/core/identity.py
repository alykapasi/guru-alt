"""Who a person is, as a hosted identity provider says (S21).

Guru does not store passwords. Clerk proves identity — sign-up, verification, recovery, social
sign-in and the mail that carries them — and this module is the only place that knows it. What
crosses this seam is deliberately tiny: a subject, the addresses the provider has *verified*,
and an external id. Everything Guru decides from that — whether the person was invited, whether
their account is suspended, what they may do — stays on Guru's side of it, because those are
authorization questions and a provider that answered them would be making product decisions.

Two implementations: the real one and a fake the tests sign in through. The fake is here rather
than in the test tree for the same reason ``FakeProvider`` lives in ``app.llm``: the seam and its
stand-in are one design, and a stand-in that drifts from the interface it doubles is worse than
none.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx
import structlog

from app.core.config import Settings

log = structlog.get_logger(__name__)

# A session token is a three-segment JWT (base64url header, payload, signature) and nothing
# else. ``verify_token_async`` dispatches on the token's *prefix* alone — an ``ak_``/``oat_``/
# ``m2m_``/``mt_`` prefix (or a JWT-shaped string wearing one) routes to a *different* SDK path
# that POSTs to Clerk's API with our secret key on every single call. Refusing anything that is
# not shaped like a session token, and anything carrying one of those prefixes, before the SDK
# ever sees it, is what keeps this call networkless for *any* input, not just well-behaved ones.
_SESSION_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class IdentityError(Exception):
    """Anything this seam refuses or cannot do."""


class InvalidToken(IdentityError):
    """The token does not verify: unknown, expired, tampered with, or for another audience.

    One exception for every reason, on the same argument as ``auth.resolve_session``: the caller
    can do exactly one thing about all of them, and telling them apart is free reconnaissance.
    """


class ProviderError(IdentityError):
    """The provider could not be reached, or refused a call Guru expected to succeed."""


@dataclass(frozen=True)
class ProviderUser:
    """One account at the provider, in the only terms Guru needs.

    ``verified_emails`` carries *verified* addresses only, primary first, spelled as the provider
    spells them. An unverified address is a claim by whoever typed it, and Guru links accounts by
    address — so accepting one would let anybody claim an invitation issued to somebody else.
    """

    subject: str
    external_id: str | None
    verified_emails: tuple[str, ...]
    display_name: str | None


@runtime_checkable
class IdentityProvider(Protocol):
    """What Guru asks of an identity provider. Deliberately six calls and no session concept."""

    async def verify(self, token: str) -> str: ...

    async def get_user(self, subject: str) -> ProviderUser: ...

    async def invite(self, email: str) -> str: ...

    async def revoke_invitation(self, invitation_id: str) -> None: ...

    async def find_users_by_email(self, email: str) -> list[ProviderUser]: ...

    async def import_user(
        self, *, email: str, password_digest: str | None, external_id: str
    ) -> ProviderUser: ...


def _classify_verification_error(exc: object) -> InvalidToken | ProviderError:
    """Sort Clerk's one verification-error class into the two Guru actually has.

    Only a genuine problem with the *presented* token becomes ``InvalidToken``: expired,
    not-yet-valid, wrong signature, wrong audience/authorized-party, malformed. Everything else
    this single SDK exception covers is about *our* configuration or Clerk's own availability —
    a JWKS fetch that failed, a public key that would not resolve, a missing secret key, an
    unexpected server error — and reporting one of those as a bad token would make a Clerk
    outage look like every learner's session going bad at once, with nothing for an alert to
    fire on and no way to tell the config mistake from an attack.
    """
    from clerk_backend_api.security.types import TokenVerificationErrorReason

    configuration_or_availability = {
        TokenVerificationErrorReason.JWK_FAILED_TO_LOAD,
        TokenVerificationErrorReason.JWK_REMOTE_INVALID,
        TokenVerificationErrorReason.JWK_FAILED_TO_RESOLVE,
        # JWK_KID_MISMATCH is deliberately *not* here, though it reads like a sibling of the
        # three above. Those mean we could not obtain the JWKS; this one is only reachable
        # once we have, and says the token names a signing key the set does not contain — a
        # token from somewhere else, which is the caller's problem and a 401. Probing a live
        # instance showed the cost of the other reading: any unauthenticated request carrying
        # a random `kid` made /auth/exchange report a provider outage, so the one signal that
        # is supposed to separate an attack from our breakage could be produced at will by
        # the attacker. Rotation is the case for the other reading — a token signed by a key
        # Clerk has since dropped arrives here — but the SDK refetches the set on a miss, so
        # what survives that refetch is a token this instance never issued.
        TokenVerificationErrorReason.SECRET_KEY_MISSING,
        TokenVerificationErrorReason.SERVER_ERROR,
        # Unreachable today: the SDK raises this only for a token carrying one of its
        # machine-token prefixes, and ``verify`` rejects everything that is not a three-segment
        # JWT before the SDK sees it. Listed anyway, because the only way it could ever fire is
        # the SDK disagreeing with that guard about what a session token looks like — our
        # mismatch to fix, not the caller's token to reject, and the whole point of this
        # function is that our breakage must never arrive as everyone's 401.
        TokenVerificationErrorReason.INVALID_TOKEN_TYPE,
    }
    if getattr(exc, "reason", None) in configuration_or_availability:
        return ProviderError(str(exc))
    return InvalidToken(str(exc))


def _to_provider_user(user: object) -> ProviderUser:
    """Map Clerk's user to ours, keeping only verified addresses, primary first.

    ``status`` is a ``(str, Enum)`` member on a real Clerk response (``VerificationStatus.
    VERIFIED``), not a plain ``str`` — comparing it to ``"verified"`` directly relies on that
    enum's ``str`` base for the equality, which holds for both the enum member and a plain
    string. Wrapping it in ``str()`` first breaks this: ``str(VerificationStatus.VERIFIED)`` is
    ``"VerificationStatus.VERIFIED"``, not ``"verified"``, so every address would silently drop.
    """
    emails = list(getattr(user, "email_addresses", None) or [])
    primary_id = getattr(user, "primary_email_address_id", None)
    verified = [
        address
        for address in emails
        if getattr(getattr(address, "verification", None), "status", None) == "verified"
        and getattr(address, "email_address", None)
    ]
    verified.sort(key=lambda address: getattr(address, "id", None) != primary_id)
    names = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
    display_name = " ".join(part for part in names if part) or None
    return ProviderUser(
        subject=str(getattr(user, "id", "")),
        external_id=getattr(user, "external_id", None),
        verified_emails=tuple(address.email_address for address in verified),
        display_name=display_name,
    )


class ClerkIdentityProvider:
    """Clerk, reached through its official SDK.

    ``jwt_key`` is the instance's public key. With it, verifying a token is arithmetic on this
    process — no network call on the hot path of signing in, and no dependency on Clerk being
    reachable to check a token it already issued. Without it the SDK fetches the JWKS instead,
    which works and is one more thing that can be slow at the wrong moment.
    """

    def __init__(
        self,
        *,
        secret_key: str,
        jwt_key: str | None,
        authorized_parties: list[str],
        sign_up_url: str | None,
    ) -> None:
        self._secret_key = secret_key
        self._jwt_key = jwt_key
        self.authorized_parties = authorized_parties
        self._sign_up_url = sign_up_url

    async def verify(self, token: str) -> str:
        from clerk_backend_api.security.types import (
            TokenPrefix,
            TokenVerificationError,
            VerifyTokenOptions,
        )
        from clerk_backend_api.security.verifytoken import verify_token_async

        if not _SESSION_TOKEN_SHAPE.match(token) or token.startswith(
            tuple(prefix.value for prefix in TokenPrefix)
        ):
            raise InvalidToken("not a session token")

        try:
            claims = await verify_token_async(
                token,
                VerifyTokenOptions(
                    secret_key=self._secret_key,
                    jwt_key=self._jwt_key,
                    authorized_parties=self.authorized_parties,
                ),
            )
        except TokenVerificationError as exc:
            raise _classify_verification_error(exc) from exc
        except Exception as exc:  # the SDK reaches the network when no jwt_key is configured
            raise ProviderError(f"could not verify the token: {exc}") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise InvalidToken("the token names no subject")
        return subject

    async def _call(self, method: str, **kwargs: object) -> object:
        """One Clerk call. A bad resource/method name or keyword argument is a programming
        error and is left to raise on its own terms; only a failure that is actually about
        reaching Clerk, or a response it sent back, becomes ``ProviderError``.
        """
        from clerk_backend_api import Clerk
        from clerk_backend_api.models import ClerkBaseError

        resource, _, name = method.partition(".")
        async with Clerk(bearer_auth=self._secret_key) as clerk:
            call = getattr(getattr(clerk, resource), name)
            try:
                return await call(**kwargs)
            except ClerkBaseError as exc:
                raise ProviderError(f"{method} failed: {exc}") from exc
            except httpx.TransportError as exc:
                raise ProviderError(f"{method} could not be completed: {exc}") from exc

    async def get_user(self, subject: str) -> ProviderUser:
        return _to_provider_user(await self._call("users.get_async", user_id=subject))

    async def invite(self, email: str) -> str:
        request: dict[str, object] = {"email_address": email, "notify": True}
        if self._sign_up_url:
            request["redirect_url"] = self._sign_up_url
        invitation = await self._call("invitations.create_async", request=request)
        invitation_id = getattr(invitation, "id", None)
        if not invitation_id:
            raise ProviderError("the provider returned an invitation with no id")
        return str(invitation_id)

    async def revoke_invitation(self, invitation_id: str) -> None:
        await self._call("invitations.revoke_async", invitation_id=invitation_id)

    async def find_users_by_email(self, email: str) -> list[ProviderUser]:
        from clerk_backend_api.models import GetUserListRequest

        # ``list_async`` is keyword-only and takes one ``request`` object; ``email_address``
        # is a field *on* that request, not a parameter of the call itself.
        found = await self._call(
            "users.list_async", request=GetUserListRequest(email_address=[email])
        )
        users = found if isinstance(found, list) else []
        return [_to_provider_user(user) for user in users]

    async def import_user(
        self, *, email: str, password_digest: str | None, external_id: str
    ) -> ProviderUser:
        request: dict[str, object] = {"email_address": [email], "external_id": external_id}
        if password_digest:
            # Guru's stored format (``app.core.security``). Clerk accepts the digest as-is, so
            # an imported learner keeps the password they already have.
            request["password_digest"] = password_digest
            request["password_hasher"] = "argon2id"
        else:
            request["skip_password_requirement"] = True
        return _to_provider_user(await self._call("users.create_async", **request))


class FakeIdentityProvider:
    """A provider that answers from memory. Tests and offline development sign in through it."""

    def __init__(self) -> None:
        self.users: dict[str, ProviderUser] = {}
        self.invitations: dict[str, str] = {}
        self.revoked: set[str] = set()
        self.imported: dict[str, str | None] = {}
        self.fail_next = False

    def add_user(
        self,
        *,
        emails: Sequence[str] = (),
        external_id: str | None = None,
        display_name: str | None = None,
        subject: str | None = None,
    ) -> ProviderUser:
        user = ProviderUser(
            subject=subject or f"user_{uuid.uuid4().hex[:12]}",
            external_id=external_id,
            verified_emails=tuple(emails),
            display_name=display_name,
        )
        self.users[user.subject] = user
        return user

    def token_for(self, subject: str) -> str:
        return f"fake:{subject}"

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ProviderError("the provider is unreachable")

    async def verify(self, token: str) -> str:
        self._maybe_fail()
        subject = token.removeprefix("fake:")
        if not token.startswith("fake:") or subject not in self.users:
            raise InvalidToken("unknown token")
        return subject

    async def get_user(self, subject: str) -> ProviderUser:
        self._maybe_fail()
        user = self.users.get(subject)
        if user is None:
            raise ProviderError(f"no user {subject}")
        return user

    async def invite(self, email: str) -> str:
        self._maybe_fail()
        # Mirrors Clerk: a second pending invitation to an address that already has one open is
        # refused (422) unless the caller passes ``ignore_existing``, which ``invite`` does not.
        if any(
            address == email
            for invitation_id, address in self.invitations.items()
            if invitation_id not in self.revoked
        ):
            raise ProviderError(f"an invitation to {email} is already pending")
        invitation_id = f"inv_{uuid.uuid4().hex[:12]}"
        self.invitations[invitation_id] = email
        return invitation_id

    async def revoke_invitation(self, invitation_id: str) -> None:
        self._maybe_fail()
        self.revoked.add(invitation_id)

    async def find_users_by_email(self, email: str) -> list[ProviderUser]:
        self._maybe_fail()
        # Exact match, mirroring Clerk's ``email_address`` filter: normalising an address (case,
        # whitespace) is the caller's job, not this seam's, so the fake does not do it either.
        return [user for user in self.users.values() if email in user.verified_emails]

    async def import_user(
        self, *, email: str, password_digest: str | None, external_id: str
    ) -> ProviderUser:
        self._maybe_fail()
        # Mirrors Clerk: creating a user for an address that already exists is refused (422).
        if any(email in user.verified_emails for user in self.users.values()):
            raise ProviderError(f"a user with address {email} already exists")
        user = self.add_user(emails=[email], external_id=external_id)
        self.imported[user.subject] = password_digest
        return user


def build_identity_provider(settings: Settings) -> IdentityProvider | None:
    """The provider this deployment has, or ``None`` when sign-in is not configured.

    ``None`` is a real state rather than an error: a checkout with no Clerk keys still runs, its
    tests still pass, and ``/auth/dev-login`` is still how a developer gets in. What it must not
    do is quietly admit people, so the routes that need a provider answer 503 and say so.
    """
    if not settings.clerk_secret_key:
        return None
    return ClerkIdentityProvider(
        secret_key=settings.clerk_secret_key,
        jwt_key=settings.clerk_jwt_key,
        authorized_parties=settings.clerk_authorized_parties or list(settings.cors_origins),
        sign_up_url=settings.clerk_sign_up_url,
    )
