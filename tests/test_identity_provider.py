"""The seam between Guru and whoever proves who a person is (S21).

Two implementations are tested here and only here: the fake every other test signs in through,
and the real one's token check — which is the single piece of Clerk's SDK that can be exercised
offline, because a token signed by a key we generated verifies against that key's public half
without reaching anybody.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
import pytest
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


def _provider(public_pem: str) -> identity.ClerkIdentityProvider:
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


def test_only_verified_addresses_reach_the_application() -> None:
    """An unverified address is a claim, not a fact, and Guru links accounts by address."""
    user = identity._to_provider_user(
        _ClerkUserStub(
            id="user_1",
            external_id="ext-1",
            primary_email_address_id="e1",
            email_addresses=[
                _EmailStub(id="e1", email_address="Primary@Example.com", status="verified"),
                _EmailStub(id="e2", email_address="unverified@example.com", status="unverified"),
                _EmailStub(id="e3", email_address="second@example.com", status="verified"),
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


async def test_the_fake_signs_in_exactly_the_people_it_was_given() -> None:
    fake = identity.FakeIdentityProvider()
    user = fake.add_user(emails=["someone@example.com"], display_name="Someone")

    assert await fake.verify(fake.token_for(user.subject)) == user.subject
    assert await fake.get_user(user.subject) == user
    assert await fake.find_users_by_email("SOMEONE@example.com") == [user]
    with pytest.raises(identity.InvalidToken):
        await fake.verify("fake:nobody")


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


def test_no_provider_is_built_without_a_secret_key() -> None:
    assert identity.build_identity_provider(Settings(clerk_secret_key=None)) is None
    built = identity.build_identity_provider(
        Settings(clerk_secret_key="sk_test_x", cors_origins=["http://localhost:5173"])
    )
    assert isinstance(built, identity.ClerkIdentityProvider)
    # Falls back to the origins the API already trusts, rather than accepting any caller.
    assert built.authorized_parties == ["http://localhost:5173"]


@dataclass
class _VerificationStub:
    status: str


@dataclass
class _EmailStub:
    id: str
    email_address: str
    status: str
    verification: _VerificationStub | None = None

    def __post_init__(self) -> None:
        self.verification = _VerificationStub(self.status)


@dataclass
class _ClerkUserStub:
    id: str
    external_id: str | None
    primary_email_address_id: str | None
    email_addresses: list[_EmailStub] = field(default_factory=list)
    first_name: str | None = None
    last_name: str | None = None
