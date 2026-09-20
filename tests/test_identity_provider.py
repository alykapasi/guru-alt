"""The seam between Guru and whoever proves who a person is (S21).

Two implementations are tested here and only here: the fake every other test signs in through,
and the real one's token check — which is the single piece of Clerk's SDK that can be exercised
offline, because a token signed by a key we generated verifies against that key's public half
without reaching anybody.

Fix round 1 (review of commit 9952ec2): the original mapping test modelled Clerk's user shape
with hand-written stub dataclasses that were shaped like the SDK but not typed like it — the
gap that let ``verified_emails`` come back empty for every real user. This file now builds its
doubles out of ``clerk_backend_api.models`` itself, so a test that models the wrong shape can no
longer stay green. Constructing a model validates a Python object locally; it never reaches
Clerk, so this stays as offline as the rest of the suite.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from clerk_backend_api import models
from clerk_backend_api.security.types import TokenPrefix, TokenVerificationError
from clerk_backend_api.security.types import TokenVerificationErrorReason as Reason
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import identity
from app.core.config import Settings

AZP = "http://localhost:5173"


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """A private PEM to sign test tokens with, and the public PEM the provider verifies against."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


def _token(private_pem: str, *, sub: str = "user_abc", azp: str = AZP, age: int = 0) -> str:
    now = datetime.now(UTC) - timedelta(minutes=age)
    return jwt.encode(
        {
            "sub": sub,
            "azp": azp,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=1)).timestamp()),
        },
        private_pem,
        algorithm="RS256",
    )


def _provider(public_pem: str | None) -> identity.ClerkIdentityProvider:
    return identity.ClerkIdentityProvider(
        secret_key="sk_test_not_used_offline",
        jwt_key=public_pem,
        authorized_parties=[AZP],
        sign_up_url=None,
    )


async def test_a_token_signed_by_the_instance_key_names_its_subject(
    keypair: tuple[str, str],
) -> None:
    private, public = keypair

    assert await _provider(public).verify(_token(private, sub="user_42")) == "user_42"


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda private: _token(private, azp="https://evil.example"), id="wrong-azp"),
        pytest.param(lambda private: _token(private, age=10), id="expired"),
        pytest.param(lambda _private: "not-a-token", id="malformed"),
    ],
)
async def test_a_token_that_does_not_belong_here_is_refused(keypair: tuple[str, str], make) -> None:
    private, public = keypair

    with pytest.raises(identity.InvalidToken):
        await _provider(public).verify(make(private))


async def test_a_token_signed_by_a_different_key_is_refused(keypair: tuple[str, str]) -> None:
    """The whole point of the public key: somebody else's signature is not this instance's."""
    _private, public = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    with pytest.raises(identity.InvalidToken):
        await _provider(public).verify(_token(other_pem))


# --- I1: a caller cannot route this call to Clerk's machine-token network path ------------------


@pytest.mark.parametrize("prefix", [p.value for p in TokenPrefix])
async def test_a_machine_token_prefix_is_refused_before_the_sdk_ever_sees_it(
    keypair: tuple[str, str], prefix: str
) -> None:
    """``verify_token_async`` dispatches on the token's *prefix* alone: ``ak_``/``oat_``/
    ``m2m_``/``mt_`` route to a call that POSTs to Clerk's API with our secret key. A token
    crafted to have both the prefix *and* a JWT's three-segment shape must still be refused —
    the shape check alone is not enough to close this, only the prefix check is.
    """
    private, public = keypair
    crafted = prefix + _token(private)

    with pytest.raises(identity.InvalidToken):
        await _provider(public).verify(crafted)


@pytest.mark.parametrize("prefix", [p.value for p in TokenPrefix])
async def test_a_bare_machine_token_is_also_refused(keypair: tuple[str, str], prefix: str) -> None:
    _private, public = keypair

    with pytest.raises(identity.InvalidToken):
        await _provider(public).verify(f"{prefix}opaque-secret-value")


# --- C3: a config/availability problem is not reported as a bad token ---------------------------


_THE_TOKEN_IS_BAD = (
    Reason.TOKEN_EXPIRED,
    Reason.TOKEN_INVALID,
    Reason.TOKEN_INVALID_AUTHORIZED_PARTIES,
    Reason.TOKEN_INVALID_AUDIENCE,
    Reason.TOKEN_IAT_IN_THE_FUTURE,
    Reason.TOKEN_NOT_ACTIVE_YET,
    Reason.TOKEN_INVALID_SIGNATURE,
)

_WE_ARE_BROKEN = (
    Reason.JWK_FAILED_TO_LOAD,
    Reason.JWK_REMOTE_INVALID,
    Reason.JWK_FAILED_TO_RESOLVE,
    Reason.JWK_KID_MISMATCH,
    Reason.SECRET_KEY_MISSING,
    Reason.SERVER_ERROR,
    Reason.INVALID_TOKEN_TYPE,
)


@pytest.mark.parametrize("reason", _THE_TOKEN_IS_BAD)
def test_a_problem_with_the_presented_token_is_invalid_token(reason: Reason) -> None:
    classified = identity._classify_verification_error(TokenVerificationError(reason))
    assert isinstance(classified, identity.InvalidToken)


@pytest.mark.parametrize("reason", _WE_ARE_BROKEN)
def test_a_config_or_availability_problem_is_provider_error(reason: Reason) -> None:
    classified = identity._classify_verification_error(TokenVerificationError(reason))
    assert isinstance(classified, identity.ProviderError)


def test_every_reason_the_sdk_can_raise_has_been_classified_on_purpose() -> None:
    """An unclassified reason defaults to ``InvalidToken``, which is the dangerous direction.

    A Clerk SDK upgrade that adds a reason would otherwise land silently on that default, and
    if the new reason described *our* breakage it would arrive as every learner's 401 — the
    failure this classification exists to prevent. Fail here instead, at the upgrade.
    """
    assert set(Reason) == set(_THE_TOKEN_IS_BAD) | set(_WE_ARE_BROKEN)


async def test_a_misconfigured_public_key_is_reported_as_a_provider_error(
    keypair: tuple[str, str],
) -> None:
    """The standard Clerk-dashboard mistake: an operator pastes the JWK JSON instead of the
    PEM. That is Guru's configuration being wrong, not the token — and unlike a bad token it
    affects every sign-in at once, so it must not look the same as one bad token does.
    """
    private, _public = keypair
    misconfigured = _provider("not a pem, this is the wrong paste")

    with pytest.raises(identity.ProviderError):
        await misconfigured.verify(_token(private))


# --- I2: a programming error in `_call` is not reported as a provider outage --------------------


def _call_provider() -> identity.ClerkIdentityProvider:
    return identity.ClerkIdentityProvider(
        secret_key="sk_test_not_used_offline", jwt_key=None, authorized_parties=[], sign_up_url=None
    )


async def test_call_lets_an_unknown_method_raise_as_the_bug_it_is() -> None:
    with pytest.raises(AttributeError):
        await _call_provider()._call("users.not_a_real_method")


async def test_call_lets_a_bad_keyword_raise_as_the_bug_it_is() -> None:
    """The exact shape of C2: calling ``list_async`` with ``email_address`` as a call-level
    keyword, instead of as a field on its ``request`` object, is a ``TypeError`` against the
    real SDK — and must surface as one, not as ``ProviderError``."""
    with pytest.raises(TypeError):
        await _call_provider()._call("users.list_async", email_address=["x"])


# --- C1: only verified addresses reach the application, built on the SDK's own model ------------


def _clerk_email(*, id: str, email_address: str, verified: bool) -> models.EmailAddress:
    status = (
        models.VerificationStatus.VERIFIED if verified else models.VerificationStatus.UNVERIFIED
    )
    return models.EmailAddress(
        object=models.EmailAddressObject.EMAIL_ADDRESS,
        email_address=email_address,
        reserved=False,
        verification=models.Otp(
            status=status,
            strategy=models.Strategy.EMAIL_CODE,
            attempts=None,
            expire_at=None,
            object=models.VerificationObject.VERIFICATION_OTP,
        ),
        linked_to=[],
        created_at=0,
        updated_at=0,
        id=id,
    )


def _clerk_user(
    *,
    id: str,
    email_addresses: list[models.EmailAddress],
    external_id: str | None = None,
    primary_email_address_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> models.User:
    """A real ``models.User`` with every other field filled to a bland default. Building the
    whole thing rather than a hand-shaped stand-in is what C1 requires: a stub shaped like the
    SDK but typed unlike it (a plain ``str`` status instead of Clerk's ``(str, Enum)``) is
    exactly what let ``verified_emails`` come back empty for every real user while this test
    stayed green.
    """
    return models.User(
        id=id,
        object=models.UserObject.USER,
        external_id=external_id,
        primary_email_address_id=primary_email_address_id,
        primary_phone_number_id=None,
        primary_web3_wallet_id=None,
        username=None,
        first_name=first_name,
        last_name=last_name,
        has_image=False,
        public_metadata={},
        email_addresses=email_addresses,
        phone_numbers=[],
        web3_wallets=[],
        passkeys=[],
        password_enabled=True,
        two_factor_enabled=False,
        totp_enabled=False,
        backup_code_enabled=False,
        mfa_enabled_at=None,
        mfa_disabled_at=None,
        external_accounts=[],
        saml_accounts=[],
        enterprise_accounts=[],
        last_sign_in_at=None,
        banned=False,
        locked=False,
        lockout_expires_in_seconds=None,
        verification_attempts_remaining=None,
        updated_at=0,
        created_at=0,
        delete_self_enabled=True,
        create_organization_enabled=True,
        last_active_at=None,
        legal_accepted_at=None,
    )


def test_only_verified_addresses_reach_the_application() -> None:
    """An unverified address is a claim, not a fact, and Guru links accounts by address."""
    user = identity._to_provider_user(
        _clerk_user(
            id="user_1",
            external_id="ext-1",
            primary_email_address_id="e1",
            email_addresses=[
                _clerk_email(id="e1", email_address="Primary@Example.com", verified=True),
                _clerk_email(id="e2", email_address="unverified@example.com", verified=False),
                _clerk_email(id="e3", email_address="second@example.com", verified=True),
            ],
            first_name="Ada",
            last_name="Lovelace",
        )
    )

    assert user.subject == "user_1"
    assert user.external_id == "ext-1"
    # Primary first: it is the address the learner's account is named by.
    assert user.verified_emails == ("Primary@Example.com", "second@example.com")
    assert user.display_name == "Ada Lovelace"


# --- C2: find_users_by_email sends the request shape list_async actually takes ------------------


async def test_find_users_by_email_sends_a_request_object_not_a_call_level_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Users.list_async`` is keyword-only and takes one ``request`` object; ``email_address``
    is a field *on* that request, not a parameter of the call itself. The original code passed
    ``email_address=[email]`` straight to ``_call``, which is a ``TypeError`` against the real
    SDK on every single call (see ``test_call_lets_a_bad_keyword_raise_as_the_bug_it_is``)."""
    provider = _call_provider()
    captured: dict[str, object] = {}

    async def fake_call(method: str, **kwargs: object) -> object:
        captured["method"] = method
        captured.update(kwargs)
        return []

    monkeypatch.setattr(provider, "_call", fake_call)

    await provider.find_users_by_email("ada@example.com")

    assert captured["method"] == "users.list_async"
    request = captured.get("request")
    assert isinstance(request, models.GetUserListRequest)
    assert request.email_address == ["ada@example.com"]


async def test_the_fake_signs_in_exactly_the_people_it_was_given() -> None:
    fake = identity.FakeIdentityProvider()
    user = fake.add_user(emails=["someone@example.com"], display_name="Someone")

    assert await fake.verify(fake.token_for(user.subject)) == user.subject
    assert await fake.get_user(user.subject) == user
    assert await fake.find_users_by_email("someone@example.com") == [user]
    with pytest.raises(identity.InvalidToken):
        await fake.verify("fake:nobody")


# --- I4: the fake matches addresses exactly, like Clerk's filter does ---------------------------


async def test_the_fake_matches_addresses_exactly_like_clerks_filter_does() -> None:
    """Normalising an address (case, whitespace) is the caller's job, not this seam's — the
    real ``find_users_by_email`` passes the caller's spelling straight to Clerk's exact-match
    filter, and a fake that folded case would pass a test that then 404s in production."""
    fake = identity.FakeIdentityProvider()
    user = fake.add_user(emails=["Ada@Example.com"])

    assert await fake.find_users_by_email("Ada@Example.com") == [user]
    assert await fake.find_users_by_email("ADA@EXAMPLE.COM") == []
    assert await fake.find_users_by_email("ada@example.com") == []


# --- I3: the fake can fail during verify, like the real one can ---------------------------------


async def test_the_fake_can_fail_during_verify_like_an_unreachable_provider() -> None:
    fake = identity.FakeIdentityProvider()
    user = fake.add_user(emails=["someone@example.com"])
    fake.fail_next = True

    with pytest.raises(identity.ProviderError):
        await fake.verify(fake.token_for(user.subject))


async def test_the_fake_records_invitations_and_imports() -> None:
    fake = identity.FakeIdentityProvider()

    invitation = await fake.invite("invitee@example.com")
    assert fake.invitations[invitation] == "invitee@example.com"
    await fake.revoke_invitation(invitation)
    assert invitation in fake.revoked

    imported = await fake.import_user(
        email="old@example.com", password_digest="$argon2id$v=19$digest", external_id="ext-9"
    )
    assert imported.external_id == "ext-9"
    assert fake.imported[imported.subject] == "$argon2id$v=19$digest"


async def test_the_fake_can_be_made_to_fail_like_an_unreachable_provider() -> None:
    fake = identity.FakeIdentityProvider()
    fake.fail_next = True

    with pytest.raises(identity.ProviderError):
        await fake.invite("invitee@example.com")


# --- I5: the fake refuses duplicates Clerk refuses, too ------------------------------------------


async def test_the_fake_refuses_a_second_open_invitation_to_the_same_address() -> None:
    fake = identity.FakeIdentityProvider()
    await fake.invite("invitee@example.com")

    with pytest.raises(identity.ProviderError):
        await fake.invite("invitee@example.com")


async def test_the_fake_allows_reinviting_once_the_first_invitation_is_revoked() -> None:
    fake = identity.FakeIdentityProvider()
    first = await fake.invite("invitee@example.com")
    await fake.revoke_invitation(first)

    second = await fake.invite("invitee@example.com")

    assert second != first


async def test_the_fake_refuses_to_import_an_address_that_already_has_an_account() -> None:
    fake = identity.FakeIdentityProvider()
    fake.add_user(emails=["ada@example.com"])

    with pytest.raises(identity.ProviderError):
        await fake.import_user(email="ada@example.com", password_digest=None, external_id="ext-2")


def test_no_provider_is_built_without_a_secret_key() -> None:
    assert identity.build_identity_provider(Settings(clerk_secret_key=None)) is None
    built = identity.build_identity_provider(
        Settings(clerk_secret_key="sk_test_x", cors_origins=["http://localhost:5173"])
    )
    assert isinstance(built, identity.ClerkIdentityProvider)
    # Falls back to the origins the API already trusts, rather than accepting any caller.
    assert built.authorized_parties == ["http://localhost:5173"]
