# S21 Hosted Identity with Clerk Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clerk owns credentials, sign-up, verification, recovery and social sign-in; Guru keeps
authorization — invite-controlled enrollment, the admin tier, audited sudo, and account suspension
— and retires its own password system.

**Architecture:** A provider seam (`app/core/identity.py`) is the only module importing the Clerk
SDK. The browser signs in with Clerk, then exchanges a Clerk token once at
`POST /api/v1/auth/exchange` for the existing `guru_session` cookie, so every downstream
mechanism (sessions, admin visits, SSE, tests) is unchanged. Invitations and suspension live in
Guru with their own audit table. The password system is removed last, after the frontend no longer
calls it, and its hashes are preserved in a transitional table for the Clerk import.

**Tech Stack:** FastAPI, async SQLAlchemy 2, Alembic, pytest-asyncio, httpx; React 19 + Vite +
vitest + Playwright; `clerk-backend-api==7.0.0`, `@clerk/react@6.16.1`.

**Spec:** `docs/superpowers/specs/2026-09-19-s21-clerk-identity-design.md`

## Global Constraints

- Python 3.13; ruff line length 100; Node ≥ 20.9; run backend commands via `uv run`.
- Every commit leaves green: `uv run poe check`, `uv run poe format-check`, `uv run poe api-contract`,
  `uv run poe db-check`. When a commit changes the API surface, regenerate types with
  `uv run poe api-types` and commit `frontend/src/api/schema.d.ts` in the same commit.
- Every commit that touches `frontend/` also leaves green: `npm run build`, `npm run lint`,
  `npm test` (run from `frontend/`).
- Commit subjects end with `[S21]`. Commit bodies end with a `Co-Authored-By:` trailer naming the
  model that wrote the commit, exactly as your own harness attribution instruction gives it.
- Stage files **by explicit path only**. Never `git add -A` / `git add .` / `git commit -a`.
- Never stage `docs/guru-suggestions-tracker.md` (the owner's uncommitted rewrite) or
  `.claude/settings.json`. After each commit `git status --short` must show only
  ` M docs/guru-suggestions-tracker.md`.
- Never `git reset`, `git commit --amend`, `git rebase`, `git stash`, `git push`, or force anything.
- Never call a real Clerk endpoint. No test may require network access. Clerk keys are never
  committed; they belong in git-ignored env files only.
- Guru decides access: a valid Clerk token is not by itself permission to use Guru.
- Known intermittent failures, acceptable only if they are the *only* failure and pass on a
  re-run you report: `tests/eval/test_eval.py::test_retrieval_eval_gate` (S76) and
  `tests/test_mastery.py::test_recent_attempts_counts_sittings_not_rows` (a diagnosed clock-skew
  flake: it compares a Postgres `now()` timestamp against the app's clock).

## File map

| File | Change |
| --- | --- |
| `app/core/identity.py` (new) | `IdentityProvider` protocol, `ProviderUser`, `InvalidToken`, `ProviderError`, `ClerkIdentityProvider`, `FakeIdentityProvider`, `build_identity_provider` |
| `app/core/config.py` | `clerk_*` settings; later, removal of `sign_in_*` / `password_reset_*` |
| `app/api/deps.py` | `get_identity_provider`, `IdentityProviderDep` |
| `app/models/learner.py` | `auth_subject`, `suspended_at`; later, `password_hash` removed |
| `app/models/auth.py` | `Invitation`, `AccountAction`, `AccountActionKind`, `LegacyPasswordDigest`; later, `PasswordResetToken` / `SignInAttempt` removed |
| `app/services/identity.py` (new) | `enroll` — verify → link/create → suspension gate |
| `app/services/accounts.py` (new) | invitations and suspension, each writing an `AccountAction` |
| `app/api/v1/auth.py` | `/auth/exchange`; dev-login takes an optional email; password routes removed last |
| `app/api/v1/admin.py`, `app/schemas/admin.py` | invitation and suspension routes and schemas |
| `app/services/admin.py` | roster carries `suspended_at` |
| `app/services/auth.py` | `resolve_session` refuses suspended accounts; password machinery removed last |
| `app/services/retention.py` | dispositions for the new tables |
| `app/workers/invite.py`, `app/workers/identity_import.py` (new) | `poe invite`, `poe identity-import` |
| `db/migrations/versions/0054_hosted_identity.py`, `0055_retire_passwords.py` (new) | schema |
| `frontend/src/auth/*` (new), `main.tsx`, `App.tsx`, `pages/SignIn.tsx`, `components/NavBar.tsx`, `api/auth.ts`, `api/admin.ts`, `pages/Admin.tsx` | Clerk mode, exchange, sign-out, portal panels |
| `frontend/e2e/journey.ts` and specs | sign in through dev-login instead of the password form |
| `docs/RUNBOOK.md`, `.env.example` | provisioning, configuration, import ordering |

---

### Task 1: The provider seam

**Files:**
- Create: `app/core/identity.py`
- Modify: `app/core/config.py` (new settings beside the auth block, around line 129)
- Modify: `app/api/deps.py` (beside `get_llm_client`, around line 60)
- Modify: `pyproject.toml` (dependency), `uv.lock`
- Create: `tests/test_identity_provider.py`

**Interfaces:**
- Produces:
  - `ProviderUser(subject: str, external_id: str | None, verified_emails: tuple[str, ...], display_name: str | None)` — `verified_emails` holds only addresses the provider reports verified, primary first, exactly as the provider spells them.
  - `class InvalidToken(Exception)`, `class ProviderError(Exception)`.
  - `IdentityProvider` protocol: `verify(token) -> str`, `get_user(subject) -> ProviderUser`,
    `invite(email) -> str`, `revoke_invitation(invitation_id) -> None`,
    `find_users_by_email(email) -> list[ProviderUser]`,
    `import_user(*, email, password_digest, external_id) -> ProviderUser`.
  - `ClerkIdentityProvider`, `FakeIdentityProvider`, `build_identity_provider(settings) -> IdentityProvider | None`.
  - `app.api.deps.get_identity_provider` and `IdentityProviderDep = Annotated[IdentityProvider | None, Depends(get_identity_provider)]`.

- [ ] **Step 1: Add the dependency**

```bash
uv add 'clerk-backend-api==7.0.0'
```

Expected: `pyproject.toml` gains the pin and `uv.lock` updates. It brings `pyjwt` and
`cryptography`, which Step 2's test uses.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_identity_provider.py`:

```python
"""The seam between Guru and whoever proves who a person is (S21).

Two implementations are tested here and only here: the fake every other test signs in through,
and the real one's token check — which is the single piece of Clerk's SDK that can be exercised
offline, because a token signed by a key we generated verifies against that key's public half
without reaching anybody.
"""

import uuid
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
async def test_a_token_that_does_not_belong_here_is_refused(
    keypair: tuple[str, str], make
) -> None:
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
```

Add the two stub dataclasses the mapping test uses at the bottom of the same file:

```python
from dataclasses import dataclass, field


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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_identity_provider.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.identity'`.

- [ ] **Step 4: Write `app/core/identity.py`**

```python
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

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
import uuid

import structlog

from app.core.config import Settings

log = structlog.get_logger(__name__)


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


def _to_provider_user(user: object) -> ProviderUser:
    """Map Clerk's user to ours, keeping only verified addresses, primary first."""
    emails = list(getattr(user, "email_addresses", None) or [])
    primary_id = getattr(user, "primary_email_address_id", None)
    verified = [
        address
        for address in emails
        if str(getattr(getattr(address, "verification", None), "status", "")) == "verified"
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
        from clerk_backend_api.security.types import TokenVerificationError, VerifyTokenOptions
        from clerk_backend_api.security.verifytoken import verify_token_async

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
            raise InvalidToken(str(exc)) from exc
        except Exception as exc:  # the SDK reaches the network when no jwt_key is configured
            raise ProviderError(f"could not verify the token: {exc}") from exc
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise InvalidToken("the token names no subject")
        return subject

    async def _call(self, method: str, **kwargs: object) -> object:
        """One Clerk call, with every failure mode collapsed into ``ProviderError``."""
        from clerk_backend_api import Clerk
        from clerk_backend_api.models import ClerkBaseError

        resource, _, name = method.partition(".")
        try:
            async with Clerk(bearer_auth=self._secret_key) as clerk:
                call = getattr(getattr(clerk, resource), name)
                return await call(**kwargs)
        except ClerkBaseError as exc:
            raise ProviderError(f"{method} failed: {exc}") from exc
        except Exception as exc:
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
        found = await self._call("users.list_async", email_address=[email])
        return [_to_provider_user(user) for user in (found or [])]

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
        invitation_id = f"inv_{uuid.uuid4().hex[:12]}"
        self.invitations[invitation_id] = email
        return invitation_id

    async def revoke_invitation(self, invitation_id: str) -> None:
        self._maybe_fail()
        self.revoked.add(invitation_id)

    async def find_users_by_email(self, email: str) -> list[ProviderUser]:
        self._maybe_fail()
        wanted = email.strip().lower()
        return [
            user
            for user in self.users.values()
            if any(address.strip().lower() == wanted for address in user.verified_emails)
        ]

    async def import_user(
        self, *, email: str, password_digest: str | None, external_id: str
    ) -> ProviderUser:
        self._maybe_fail()
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
```

- [ ] **Step 5: Add the settings**

In `app/core/config.py`, directly after the auth block (the `dev_auto_login` field is around
line 217; put these next to the session settings around line 129):

```python
    # Hosted identity (S21). Clerk proves who a person is; Guru still decides what they may do.
    # No secret key means no provider: /auth/exchange and the invitation routes answer 503, and
    # /auth/dev-login is the only way in — which production refuses to start with.
    clerk_secret_key: str | None = None
    # The instance's public key (Clerk dashboard → API keys → JWKS public key), PEM-encoded.
    # With it, verifying a session token touches no network.
    clerk_jwt_key: str | None = None
    # Which origins' tokens are accepted, checked against the token's `azp`. Empty means the
    # origins CORS already trusts.
    clerk_authorized_parties: list[str] = []
    # Where an invitation link lands: the frontend's sign-up route.
    clerk_sign_up_url: str | None = None
```

- [ ] **Step 6: Expose it as a dependency**

In `app/api/deps.py`, beside `get_llm_client`:

```python
@lru_cache
def _identity_provider() -> IdentityProvider | None:
    return build_identity_provider(get_settings())


def get_identity_provider() -> IdentityProvider | None:
    """The hosted identity provider, or None when none is configured. Overridden in tests."""
    return _identity_provider()


IdentityProviderDep = Annotated[IdentityProvider | None, Depends(get_identity_provider)]
```

with `from app.core.identity import IdentityProvider, build_identity_provider` at the top.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_identity_provider.py -q`
Expected: all pass. If `verify_token_async` rejects a token whose `jwt_key` is supplied without a
reachable instance, read the SDK at
`.venv/lib/python3.13/site-packages/clerk_backend_api/security/verifytoken.py` and adjust the
call — not the assertions. If the SDK genuinely cannot verify offline, report BLOCKED with the
traceback: the spec's offline-verification claim would be wrong and that is the controller's call.

- [ ] **Step 8: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract && uv run poe db-check`

```bash
git add app/core/identity.py app/core/config.py app/api/deps.py pyproject.toml uv.lock \
  tests/test_identity_provider.py
git commit -m "feat(identity): add the hosted identity seam and its fake [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 2: Schema for linking, invitations and the account audit

**Files:**
- Modify: `app/models/learner.py`, `app/models/auth.py`
- Create: `db/migrations/versions/0054_hosted_identity.py`
- Modify: `app/services/retention.py` (the `RETENTION` tuple, and the erase path around line 275)
- Create: `tests/test_hosted_identity_schema.py`

**Interfaces:**
- Produces: `Learner.auth_subject: str | None` (unique), `Learner.suspended_at: datetime | None`;
  models `Invitation`, `AccountAction`, enum `AccountActionKind`
  (`INVITE`, `REVOKE_INVITATION`, `SUSPEND`, `REINSTATE`).
- Later tasks read: `Invitation.email`, `.accepted_at`, `.accepted_learner_id`, `.revoked_at`,
  `.provider_invitation_id`, `.invited_by_learner_id`, `.invited_by_handle`,
  `.revoked_by_learner_id`; `AccountAction.actor_learner_id`, `.actor_handle`, `.learner_id`,
  `.learner_handle`, `.action`, `.email`, `.reason`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_hosted_identity_schema.py`:

```python
"""The rows that make enrollment invite-controlled and administrative acts reviewable (S21)."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AccountAction, AccountActionKind, Invitation
from app.models.learner import Learner
from app.services import retention as retention_svc


async def _learner(session: AsyncSession, **kwargs: object) -> Learner:
    learner = Learner(handle=f"l-{uuid.uuid4().hex[:8]}", **kwargs)
    session.add(learner)
    await session.flush()
    return learner


async def test_one_provider_identity_belongs_to_one_learner(db_session: AsyncSession) -> None:
    """Two learners sharing a subject would mean one sign-in resolving to either of them."""
    await _learner(db_session, auth_subject="user_1")
    await _learner(db_session, auth_subject="user_1")

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_an_address_can_have_only_one_open_invitation(db_session: AsyncSession) -> None:
    admin = await _learner(db_session)
    for _ in range(2):
        db_session.add(
            Invitation(
                email="invitee@example.com",
                invited_by_learner_id=admin.id,
                invited_by_handle=admin.handle,
            )
        )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_spent_invitation_does_not_block_a_new_one(db_session: AsyncSession) -> None:
    """Re-inviting somebody whose invitation was accepted or withdrawn is ordinary."""
    admin = await _learner(db_session)
    learner = await _learner(db_session)
    db_session.add(
        Invitation(
            email="invitee@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
            accepted_at=datetime.now(UTC),
            accepted_learner_id=learner.id,
        )
    )
    db_session.add(
        Invitation(
            email="invitee@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
            revoked_at=datetime.now(UTC),
            revoked_by_learner_id=admin.id,
        )
    )
    await db_session.flush()

    db_session.add(
        Invitation(
            email="invitee@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
        )
    )
    await db_session.flush()  # no error


async def test_the_audit_outlives_both_accounts(db_session: AsyncSession) -> None:
    """An audit a subject can erase by closing their account is not an audit of anything."""
    admin = await _learner(db_session)
    learner = await _learner(db_session)
    action = AccountAction(
        actor_learner_id=admin.id,
        actor_handle=admin.handle,
        learner_id=learner.id,
        learner_handle=learner.handle,
        action=AccountActionKind.SUSPEND,
        reason="abusing the tutor",
    )
    db_session.add(action)
    await db_session.flush()

    await db_session.delete(learner)
    await db_session.delete(admin)
    await db_session.flush()
    await db_session.refresh(action)

    assert action.learner_id is None and action.actor_learner_id is None
    assert action.actor_handle == admin.handle
    assert action.action == AccountActionKind.SUSPEND
    assert action.reason == "abusing the tutor"


async def test_closing_an_account_takes_its_invitation_and_unnames_it_in_the_audit(
    db_session: AsyncSession,
) -> None:
    """Their address is theirs: it goes with the account. Who acted, and that they acted, stays."""
    admin = await _learner(db_session)
    learner = await _learner(db_session, email="gone@example.com")
    db_session.add(
        Invitation(
            email="gone@example.com",
            invited_by_learner_id=admin.id,
            invited_by_handle=admin.handle,
            accepted_at=datetime.now(UTC),
            accepted_learner_id=learner.id,
        )
    )
    action = AccountAction(
        actor_learner_id=admin.id,
        actor_handle=admin.handle,
        learner_id=learner.id,
        learner_handle=learner.handle,
        action=AccountActionKind.INVITE,
        email="gone@example.com",
    )
    db_session.add(action)
    await db_session.commit()

    await retention_svc.erase_learner(db_session, learner.id)

    await db_session.refresh(action)
    assert action.learner_handle is None and action.email is None
    assert action.actor_handle == admin.handle
    remaining = await db_session.scalar(
        Invitation.__table__.select().where(Invitation.email == "gone@example.com")
    )
    assert remaining is None


def test_the_new_stores_have_a_stated_disposition() -> None:
    named = set(retention_svc.retention_tables())

    assert {"invitations", "account_actions"} <= named
```

Before writing the models, read `app/services/retention.py` and find the erase entry point's real
name (the test calls it `erase_learner`; use whatever the service actually exposes — the function
the API's immediate-erasure path calls) and adjust the test accordingly. Keep the assertions.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_hosted_identity_schema.py -q`
Expected: FAIL — `ImportError: cannot import name 'Invitation' from 'app.models.auth'`.

- [ ] **Step 3: Add the columns and models**

In `app/models/learner.py`, inside `Learner`:

```python
    # The provider's id for this person (S21). NULL for a learner nobody has signed in as yet:
    # an account created by an import before its owner arrives, or the dev learner. Unique,
    # because one provider identity resolving to two learners is one sign-in with two answers.
    auth_subject: Mapped[str | None] = mapped_column(unique=True, index=True, default=None)
    # Set by an administrator (S21). Access stops immediately and the reason is in
    # ``account_actions``; the learner's work is untouched, because suspension is not deletion.
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
```

In `app/models/auth.py`:

```python
class Invitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Permission for one address to enroll (S21).

    Guru is invite-only for the alpha, and this row — not the provider's setting — is what
    enforces it: the exchange refuses an identity whose addresses have no open invitation. The
    provider is asked to *deliver* the invitation, so one misconfigured dashboard toggle cannot
    turn the alpha into open registration.

    Open means accepted and revoked are both NULL, and the partial unique index says an address
    has at most one of those at a time. A spent or withdrawn invitation stays as history.
    """

    __tablename__ = "invitations"
    __table_args__ = (
        Index(
            "uq_invitations_open_email",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    email: Mapped[str] = mapped_column(index=True)
    invited_by_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    # Beside the id, for the same reason as ``impersonations``: an id whose row is gone names
    # nobody, and "who let this person in" has to survive the inviter closing their account.
    invited_by_handle: Mapped[str] = mapped_column()
    provider_invitation_id: Mapped[str | None] = mapped_column(default=None)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    accepted_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_by_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), default=None
    )


class AccountActionKind(StrEnum):
    """What an administrator did to an account."""

    INVITE = "invite"
    REVOKE_INVITATION = "revoke_invitation"
    SUSPEND = "suspend"
    REINSTATE = "reinstate"


class AccountAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One administrative act on accounts, recorded (S21, V13).

    ``admin_actions`` records what a *visit* touched; this records the acts that need no visit —
    letting somebody in, and stopping them. Written in the same transaction as the act, so there
    is no path that suspends an account without leaving a record of who did it and why.
    """

    __tablename__ = "account_actions"

    actor_learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    actor_handle: Mapped[str] = mapped_column()
    learner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("learners.id", ondelete="SET NULL"), index=True, default=None
    )
    learner_handle: Mapped[str | None] = mapped_column(default=None)
    action: Mapped[str] = mapped_column(index=True)
    # The address an invitation was issued to. Cleared when that person's account is erased.
    email: Mapped[str | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(default=None)
```

Add the imports these need (`Index`, `text`, `StrEnum`).

- [ ] **Step 4: Write migration `0054_hosted_identity`**

Follow the house style of `db/migrations/versions/0053_note_exact_authorship.py`: a docstring that
says *why*, `revision = "0054_hosted_identity"`, `down_revision = "0053_note_exact_authorship"`.
`upgrade()` adds the two learner columns (with the unique index on `auth_subject`), creates
`invitations` (with the partial unique index) and `account_actions`. `downgrade()` reverses it.
Generate a first draft with `uv run alembic revision --autogenerate -m "hosted identity"` if you
like, but rename the file and revision to the house pattern and write the docstring yourself.

- [ ] **Step 5: Record the retention dispositions**

In `app/services/retention.py`, add to `RETENTION`:

```python
    StoreRetention(
        "invitations",
        "partly deleted",
        "Permission to enroll (S21). The invitation this learner accepted goes with the "
        "account, because it holds their address. Invitations *they* issued as an "
        "administrator are retained with the rest of the administrative record — who let "
        "somebody in is not the invitee's to erase.",
    ),
    StoreRetention(
        "account_actions",
        "partly deleted",
        "Administrative acts on accounts (S21): invitations, suspensions, reinstatements. The "
        "same split as impersonations — the subject's id and handle and the address go, so "
        "nothing left names them; that an administrator acted, when, and why is retained.",
    ),
```

In the erase path, immediately before the `Impersonation` handle-clearing update:

```python
    # Their address is theirs, so the invitation they accepted goes with the account; the ones
    # they issued as an administrator stay with the rest of the administrative record.
    await session.execute(
        delete(Invitation).where(Invitation.accepted_learner_id == learner_id)
    )
    await session.execute(
        update(AccountAction)
        .where(AccountAction.learner_id == learner_id)
        .values(learner_handle=None, email=None)
    )
```

- [ ] **Step 6: Run the tests and the schema gates**

Run: `uv run pytest tests/test_hosted_identity_schema.py tests/test_retention.py -q`
Then: `uv run poe db-upgrade && uv run poe db-check`
Expected: tests pass; `alembic check` reports no new operations (models and migration agree).

Also run the migration round-trip the repo already has a harness for:
`uv run pytest tests/test_migrations_with_data.py -q`. If that file parametrises revisions, add
`0054_hosted_identity` to it in the same style; if it does not, add one test there that upgrades to
`0054`, inserts a learner with `auth_subject`, downgrades to `0053`, and upgrades again.

- [ ] **Step 7: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract && uv run poe db-check`

```bash
git add app/models/learner.py app/models/auth.py app/services/retention.py \
  db/migrations/versions/0054_hosted_identity.py tests/test_hosted_identity_schema.py \
  tests/test_migrations_with_data.py
git commit -m "feat(identity): schema for provider links, invitations and account audit [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 3: The exchange

**Files:**
- Create: `app/services/identity.py`
- Modify: `app/services/auth.py` (make `_handle_for` / `_unique_handle` public)
- Modify: `app/api/v1/auth.py` (the new route), `frontend/src/api/schema.d.ts` (regenerated)
- Create: `tests/test_identity_exchange.py`
- Modify: `tests/test_cross_connection.py` (add the concurrent-enrollment test)

**Interfaces:**
- Consumes: `IdentityProviderDep`, `FakeIdentityProvider` (Task 1); `Invitation` (Task 2).
- Produces:
  - `app.services.auth.handle_for(email) -> str` and `unique_handle(session, base) -> str`
    (renamed from the underscore versions; `register` keeps working through the new names).
  - `app.services.identity.enroll(session, provider, subject) -> Learner`, raising `NotInvited`,
    `AccountSuspended`, `AmbiguousIdentity`.
  - `POST /api/v1/auth/exchange` → 200 `LearnerRead` and the `guru_session` cookie.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_identity_exchange.py`:

```python
"""Signing in with a hosted identity, and who that lets in (S21).

Clerk says *who* somebody is. Every question about whether they may use Guru at all — invited,
suspended, already known — is answered here, which is why these tests drive the real route with
a fake provider rather than testing a helper.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_identity_provider
from app.core.identity import FakeIdentityProvider
from app.main import app
from app.models.auth import Invitation
from app.models.learner import Learner

API = "/api/v1"


@pytest.fixture
def provider() -> Iterator[FakeIdentityProvider]:
    fake = FakeIdentityProvider()
    app.dependency_overrides[get_identity_provider] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_identity_provider, None)


async def _invitation(session: AsyncSession, email: str, **kwargs: object) -> Invitation:
    invitation = Invitation(
        email=email, invited_by_learner_id=None, invited_by_handle="operator", **kwargs
    )
    session.add(invitation)
    await session.flush()
    return invitation


async def _exchange(client: AsyncClient, token: str):
    return await client.post(f"{API}/auth/exchange", headers={"Authorization": f"Bearer {token}"})


async def test_an_invited_address_enrolls_and_is_signed_in(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    user = provider.add_user(emails=["Invitee@example.com"], display_name="Ada Lovelace")

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "invitee@example.com"
    assert body["display_name"] == "Ada Lovelace"
    learner = await db_session.scalar(select(Learner).where(Learner.auth_subject == user.subject))
    assert learner is not None
    invitation = await db_session.scalar(select(Invitation))
    assert invitation.accepted_at is not None and invitation.accepted_learner_id == learner.id
    # Signed in: the session cookie is set, and it authenticates the same learner.
    me = await anon_client.get(f"{API}/auth/me")
    assert me.status_code == 200 and me.json()["id"] == str(learner.id)


async def test_an_uninvited_identity_is_refused_and_creates_nothing(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    user = provider.add_user(emails=["stranger@example.com"])
    before = await db_session.scalar(select(Learner.id).limit(1))

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 403
    assert "invite" in r.json()["detail"].lower()
    after = await db_session.scalars(select(Learner.id))
    assert (before is None) == (len(list(after)) == 0)


async def test_an_unverified_address_does_not_claim_an_invitation(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """The provider only hands us verified addresses; an identity with none is nobody here."""
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    user = provider.add_user(emails=[])

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 403
    invitation = await db_session.scalar(select(Invitation))
    assert invitation.accepted_at is None


async def test_a_revoked_invitation_does_not_let_anybody_in(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    await _invitation(
        db_session, "invitee@example.com", revoked_at=datetime.now(UTC)
    )
    await db_session.commit()
    user = provider.add_user(emails=["invitee@example.com"])

    assert (await _exchange(anon_client, provider.token_for(user.subject))).status_code == 403


async def test_an_invitation_is_spent_once(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    first = provider.add_user(emails=["invitee@example.com"])
    second = provider.add_user(emails=["invitee@example.com"])

    assert (await _exchange(anon_client, provider.token_for(first.subject))).status_code == 200
    # A second identity with the same address is not the invited person arriving twice.
    assert (await _exchange(anon_client, provider.token_for(second.subject))).status_code == 403


async def test_an_existing_account_is_linked_by_its_verified_address(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """The re-invitation path: the learner's work is already here and must stay theirs."""
    learner = Learner(handle="ada", email="ada@example.com", display_name="Ada")
    db_session.add(learner)
    await db_session.commit()
    user = provider.add_user(emails=["ADA@example.com"])

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 200 and r.json()["id"] == str(learner.id)
    await db_session.refresh(learner)
    assert learner.auth_subject == user.subject


async def test_an_imported_account_is_linked_by_its_external_id(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """What `poe identity-import` sets up: the provider carries the learner's own id."""
    learner = Learner(handle="ada2", email="ada2@example.com")
    db_session.add(learner)
    await db_session.commit()
    user = provider.add_user(emails=["somethingelse@example.com"], external_id=str(learner.id))

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 200 and r.json()["id"] == str(learner.id)


async def test_an_identity_matching_two_accounts_is_refused_rather_than_guessed(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Picking one would hand somebody another learner's work."""
    db_session.add(Learner(handle="one", email="one@example.com"))
    db_session.add(Learner(handle="two", email="two@example.com"))
    await db_session.commit()
    user = provider.add_user(emails=["one@example.com", "two@example.com"])

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 409
    assert (
        await db_session.scalar(select(Learner).where(Learner.auth_subject == user.subject))
    ) is None


async def test_a_suspended_account_cannot_sign_in(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    learner = Learner(
        handle="susp", email="susp@example.com", suspended_at=datetime.now(UTC)
    )
    db_session.add(learner)
    await db_session.commit()
    user = provider.add_user(emails=["susp@example.com"], external_id=str(learner.id))

    r = await _exchange(anon_client, provider.token_for(user.subject))

    assert r.status_code == 403 and "suspended" in r.json()["detail"].lower()
    assert (await anon_client.get(f"{API}/auth/me")).status_code == 401


async def test_an_address_change_at_the_provider_follows_the_learner(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    learner = Learner(handle="moved", email="old@example.com", auth_subject="user_moved")
    db_session.add(learner)
    await db_session.commit()
    provider.add_user(emails=["new@example.com"], subject="user_moved")

    assert (await _exchange(anon_client, provider.token_for("user_moved"))).status_code == 200

    await db_session.refresh(learner)
    assert learner.email == "new@example.com"


async def test_an_address_another_learner_holds_is_not_taken(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """Syncing must never make two accounts collide on one address."""
    db_session.add(Learner(handle="holder", email="taken@example.com"))
    learner = Learner(handle="mover", email="mine@example.com", auth_subject="user_x")
    db_session.add(learner)
    await db_session.commit()
    provider.add_user(emails=["taken@example.com"], subject="user_x")

    assert (await _exchange(anon_client, provider.token_for("user_x"))).status_code == 200

    await db_session.refresh(learner)
    assert learner.email == "mine@example.com"


async def test_a_bad_token_is_one_answer(
    anon_client: AsyncClient, provider: FakeIdentityProvider
) -> None:
    assert (await _exchange(anon_client, "fake:nobody")).status_code == 401
    assert (await anon_client.post(f"{API}/auth/exchange")).status_code == 401


async def test_an_unreachable_provider_is_not_a_refusal(
    anon_client: AsyncClient, db_session: AsyncSession, provider: FakeIdentityProvider
) -> None:
    """502, not 403: "we could not ask" and "you may not" are different things to a learner."""
    user = provider.add_user(emails=["invitee@example.com"])
    await _invitation(db_session, "invitee@example.com")
    await db_session.commit()
    provider.fail_next = True

    assert (await _exchange(anon_client, provider.token_for(user.subject))).status_code == 502


async def test_without_a_provider_the_route_says_so(anon_client: AsyncClient) -> None:
    """No Clerk keys configured: a checkout that still runs, and does not quietly admit anybody."""
    app.dependency_overrides[get_identity_provider] = lambda: None
    try:
        r = await anon_client.post(
            f"{API}/auth/exchange", headers={"Authorization": "Bearer whatever"}
        )
    finally:
        app.dependency_overrides.pop(get_identity_provider, None)

    assert r.status_code == 503
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_identity_exchange.py -q`
Expected: FAIL — every case 404s, because `/auth/exchange` does not exist.

- [ ] **Step 3: Rename the handle helpers**

In `app/services/auth.py`, rename `_handle_for` → `handle_for` and `_unique_handle` →
`unique_handle` (keeping both docstrings), and update `register`'s call. They are about to have a
second caller, and a private name with two callers in two modules is a lie about its scope.

- [ ] **Step 4: Write `app/services/identity.py`**

```python
"""Turning a proven identity into a Guru learner (S21).

Clerk answers "who is this?". This module answers the three questions Clerk cannot: is this
somebody we already know, are they allowed in at all, and is their account still open. That
split is the whole design — a provider that decided who may use Guru would be making the
product's access policy, and swapping providers would then change who can sign in.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.identity import IdentityProvider, ProviderUser
from app.models.auth import Invitation
from app.models.learner import Learner
from app.services.auth import handle_for, normalise_email, unique_handle


class EnrollmentError(Exception):
    """This identity does not become a session."""


class NotInvited(EnrollmentError):
    """Nobody has invited any address this identity holds."""


class AccountSuspended(EnrollmentError):
    """The account exists and an administrator has stopped it."""


class AmbiguousIdentity(EnrollmentError):
    """The identity's verified addresses name more than one learner. Never guessed."""


async def enroll(
    session: AsyncSession, provider: IdentityProvider, subject: str
) -> Learner:
    """The learner this provider identity is, creating the account if they were invited.

    Ordered by how *certain* each match is: the provider id we stored ourselves, then the id we
    asked the provider to carry for us, then a verified address, then an invitation. A weaker
    signal never overrides a stronger one, and no signal at all is a refusal rather than a
    guess.
    """
    learner = await session.scalar(select(Learner).where(Learner.auth_subject == subject))
    user = await provider.get_user(subject)
    if learner is None:
        learner = await _link(session, user) or await _create(session, user)
    if learner.suspended_at is not None:
        raise AccountSuspended(str(learner.id))
    await _sync_email(session, learner, user)
    await session.commit()
    await session.refresh(learner)
    return learner


def _addresses(user: ProviderUser) -> list[str]:
    return [normalise_email(address) for address in user.verified_emails]


async def _link(session: AsyncSession, user: ProviderUser) -> Learner | None:
    """An account this identity already belongs to, by external id or by verified address."""
    if user.external_id:
        try:
            learner_id = uuid.UUID(user.external_id)
        except ValueError:
            learner_id = None
        if learner_id is not None:
            candidate = await session.get(Learner, learner_id)
            if candidate is not None and candidate.auth_subject is None:
                candidate.auth_subject = user.subject
                return candidate

    addresses = _addresses(user)
    if not addresses:
        return None
    candidates = list(
        await session.scalars(
            select(Learner).where(
                Learner.email.in_(addresses), Learner.auth_subject.is_(None)
            )
        )
    )
    if len(candidates) > 1:
        raise AmbiguousIdentity(", ".join(sorted(c.handle for c in candidates)))
    if not candidates:
        return None
    candidates[0].auth_subject = user.subject
    return candidates[0]


async def _create(session: AsyncSession, user: ProviderUser) -> Learner:
    """Enroll an invited address, spending its invitation in the same transaction."""
    addresses = _addresses(user)
    invitation = await session.scalar(
        select(Invitation)
        .where(
            Invitation.email.in_(addresses),
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .order_by(Invitation.created_at)
        .with_for_update()
    )
    if invitation is None:
        raise NotInvited(user.subject)

    learner = Learner(
        handle=await unique_handle(session, handle_for(invitation.email)),
        display_name=user.display_name,
        email=invitation.email,
        auth_subject=user.subject,
    )
    session.add(learner)
    try:
        await session.flush()
    except IntegrityError:
        # Two sign-ins for one new identity at once: the unique constraint on `auth_subject`
        # decides, and the loser reads the winner's row rather than failing the person.
        await session.rollback()
        existing = await session.scalar(
            select(Learner).where(Learner.auth_subject == user.subject)
        )
        if existing is None:
            raise
        return existing
    invitation.accepted_at = datetime.now(UTC)
    invitation.accepted_learner_id = learner.id
    return learner


async def _sync_email(session: AsyncSession, learner: Learner, user: ProviderUser) -> None:
    """Follow an address change at the provider, unless another learner holds that address."""
    addresses = _addresses(user)
    if not addresses or learner.email == addresses[0]:
        return
    taken = await session.scalar(
        select(Learner.id).where(Learner.email == addresses[0], Learner.id != learner.id)
    )
    if taken is None:
        learner.email = addresses[0]
```

- [ ] **Step 5: Add the route**

In `app/api/v1/auth.py`:

```python
@router.post("/exchange", response_model=LearnerRead)
async def exchange(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    provider: IdentityProviderDep,
):
    """Trade a proven identity for a Guru session (S21).

    The provider's token arrives in ``Authorization`` and is spent here, once. What the browser
    keeps is the same httpOnly cookie every other route already takes — which is why nothing
    downstream of this line knows Clerk exists, and why signing out, "log out everywhere" and
    suspension keep working exactly as they did.
    """
    if provider is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "sign-in is not configured on this server"
        )
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")

    try:
        subject = await provider.verify(token)
        learner = await identity.enroll(session, provider, subject)
    except InvalidToken as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated") from exc
    except identity.NotInvited as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Guru is invite-only. Ask an administrator for an invitation.",
        ) from exc
    except identity.AccountSuspended as exc:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This account is suspended. An administrator can reinstate it.",
        ) from exc
    except identity.AmbiguousIdentity as exc:
        log.warning("identity.ambiguous", subject=subject, learners=str(exc))
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This sign-in matches more than one Guru account; an administrator has to resolve it.",
        ) from exc
    except ProviderError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "the sign-in provider could not be reached; try again",
        ) from exc

    issued = await svc.issue(session, learner, ttl=timedelta(hours=settings.session_ttl_hours))
    _set_session_cookie(response, issued.token, settings)
    return learner
```

Add the imports it needs (`IdentityProviderDep`, `InvalidToken`, `ProviderError`,
`app.services.identity as identity`, and a `structlog` logger if the module has none).

- [ ] **Step 6: Add the concurrency test**

In `tests/test_cross_connection.py`, beside the existing registration-race test, add one in the
same style (the file's `live_client` fixture drives real connections):

```python
async def test_one_new_identity_signing_in_twice_at_once_makes_one_account(...):
    """Two tabs, one new person. The unique constraint on `auth_subject` decides; nobody 500s."""
```

Both requests must return 200, and exactly one `learners` row may exist for that subject.

- [ ] **Step 7: Run everything this touches**

Run: `uv run pytest tests/test_identity_exchange.py tests/test_cross_connection.py tests/test_auth.py -q`
Then regenerate the contract: `uv run poe api-types`
Expected: tests pass; `frontend/src/api/schema.d.ts` gains the `/auth/exchange` path.

- [ ] **Step 8: Full gate, then commit**

Run: `uv run poe check && uv run poe format-check && uv run poe api-contract && uv run poe db-check`
and, because `schema.d.ts` changed, from `frontend/`: `npm run build && npm run lint && npm test`

```bash
git add app/services/identity.py app/services/auth.py app/api/v1/auth.py \
  frontend/src/api/schema.d.ts tests/test_identity_exchange.py tests/test_cross_connection.py
git commit -m "feat(auth): exchange a proven identity for a Guru session [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 4: Invitations

**Files:**
- Create: `app/services/accounts.py`
- Modify: `app/api/v1/admin.py`, `app/schemas/admin.py`, `frontend/src/api/schema.d.ts`
- Create: `app/workers/invite.py`; modify `pyproject.toml` (`invite` task)
- Create: `tests/test_invitations.py`

**Interfaces:**
- Consumes: `Invitation`, `AccountAction`, `AccountActionKind` (Task 2); `IdentityProviderDep`,
  `ProviderError` (Task 1).
- Produces, in `app.services.accounts`:
  - `invite(session, provider, *, actor, email) -> Invitation`
  - `list_invitations(session) -> list[Invitation]`
  - `revoke_invitation(session, provider, *, actor, invitation_id) -> Invitation`
  - exceptions `AlreadyEnrolled`, `AlreadyInvited`, `NoSuchInvitation`, `NotOpen`
  - `actor` is a `Learner` for the portal, or the string handle `"operator (cli)"` for the CLI —
    model it as `Actor = Learner | str` and record `actor_learner_id=None` for the string form.
- Produces routes: `POST /admin/invitations`, `GET /admin/invitations`,
  `POST /admin/invitations/{invitation_id}/revoke`; schemas `InvitationCreate`, `InvitationRead`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_invitations.py`, driving the real routes with `admin_client` (an administrator)
and `api_client` (an ordinary learner), with the `provider` fixture from Task 3 copied in. Cover:

```python
async def test_inviting_records_the_invitation_and_asks_the_provider_to_send_it(...)
    # POST /admin/invitations {"email": "New@Example.com"} → 201; the row stores the normalised
    # address, the admin's id and handle, and the provider's invitation id; provider.invitations
    # holds exactly that address; an AccountAction of kind "invite" names the admin and address.

async def test_an_address_that_already_has_an_account_is_refused(...)        # 409, nothing recorded
async def test_a_second_open_invitation_for_one_address_is_refused(...)      # 409, one row
async def test_re_inviting_an_address_whose_invitation_was_revoked_is_allowed(...)  # 201

async def test_nothing_is_recorded_when_the_provider_cannot_be_reached(...)
    # provider.fail_next = True → 502, and no invitation row and no audit row exist afterwards.

async def test_revoking_closes_the_invitation_at_both_ends(...)
    # → 200; row has revoked_at and revoked_by; provider.revoked holds the id; audit kind
    # "revoke_invitation".

async def test_revoking_an_invitation_that_is_not_open_is_refused(...)       # 409 on accepted
async def test_revoking_survives_a_provider_that_will_not_answer(...)
    # provider.fail_next = True → the local revocation still succeeds (200) because refusing to
    # record it would leave an invitation Guru believes is open; the failure is logged.

async def test_listing_shows_open_accepted_and_revoked(...)                  # GET, three rows
async def test_an_ordinary_learner_cannot_invite_or_list(...)                # 403 on all three
async def test_an_impersonated_administrator_cannot_invite(...)
    # Borrowed identity is not an administrator: reuse the pattern in tests/test_admin_sudo.py.
async def test_without_a_provider_inviting_says_so(...)                      # 503
```

Write these out fully in the repo's style: real requests, real assertions on rows, no mocks
beyond the fake provider.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_invitations.py -q` → FAIL (routes do not exist).

- [ ] **Step 3: Write `app/services/accounts.py`**

Module docstring: this is where an administrator's acts on *accounts* live — letting somebody in,
and stopping them — each writing its `AccountAction` in the same transaction as the act, so there
is no path that does one without the other. `invite` calls the provider **before** inserting, so a
provider failure records nothing; `revoke_invitation` records locally even when the provider call
fails, because an invitation Guru believes is open is the worse of the two errors — log the
failure.

- [ ] **Step 4: Add the routes and schemas**

`InvitationCreate {email: EmailStr}`; `InvitationRead` exposing id, email, invited_by_handle,
created_at, accepted_at, revoked_at, and a derived `status` of `open` / `accepted` / `revoked`.
Routes take `CurrentAdmin` (which already refuses impersonated sessions) and `IdentityProviderDep`,
mapping: `AlreadyEnrolled`/`AlreadyInvited`/`NotOpen` → 409, `NoSuchInvitation` → 404,
`ProviderError` → 502, provider `None` → 503.

Update the admin router's module docstring: it is no longer read-only.

- [ ] **Step 5: Add the CLI**

`app/workers/invite.py`, modelled on `app/workers/grant_admin.py`:
`uv run poe invite <email> [--no-send]`. It records the invitation with
`invited_by_handle="operator (cli)"`, and calls the provider unless `--no-send` or no provider is
configured, printing what it did. This is how the first person is invited to an empty deployment.
Register it in `pyproject.toml`: `invite = "python -m app.workers.invite"`.

- [ ] **Step 6: Run, regenerate types, full gate, commit**

Run: `uv run pytest tests/test_invitations.py tests/test_admin_sudo.py -q`, then
`uv run poe api-types`, then the full gate and the frontend gate (schema changed).

```bash
git add app/services/accounts.py app/api/v1/admin.py app/schemas/admin.py app/workers/invite.py \
  pyproject.toml frontend/src/api/schema.d.ts tests/test_invitations.py
git commit -m "feat(admin): invitation-controlled enrollment, recorded at both ends [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 5: Suspension

**Files:**
- Modify: `app/services/accounts.py`, `app/services/auth.py` (`resolve_session`),
  `app/api/deps.py` (`get_current_admin`, `require_operator`), `app/api/v1/admin.py`,
  `app/schemas/admin.py`, `app/services/admin.py` (roster), `frontend/src/api/schema.d.ts`
- Create: `tests/test_suspension.py`

**Interfaces:**
- Produces: `accounts.suspend(session, *, actor, learner_id, reason) -> Learner`,
  `accounts.reinstate(session, *, actor, learner_id, reason=None) -> Learner`;
  exceptions `NoSuchLearner`, `CannotSuspendSelf`, `ReasonRequired` (reuse
  `impersonation.MIN_REASON_LENGTH`).
- Produces routes `POST /admin/learners/{learner_id}/suspend`,
  `POST /admin/learners/{learner_id}/reinstate`; `LearnerUsage` gains `suspended_at`.

- [ ] **Step 1: Write the failing tests** (`tests/test_suspension.py`)

```python
async def test_suspending_ends_the_learners_sessions_immediately(...)
    # A signed-in learner's next request is a 401 the moment they are suspended.
async def test_an_administrators_visit_to_a_suspended_account_still_works(...)
    # Support has to be able to look at exactly the account that is in trouble (P10/V13).
async def test_a_suspended_learner_cannot_exchange_a_new_identity_for_a_session(...)   # 403
async def test_a_suspended_administrator_loses_the_portal_and_the_operational_reads(...)
async def test_reinstating_lets_them_sign_in_again(...)
async def test_suspension_requires_a_reason(...)                     # 422 under 8 characters
async def test_an_administrator_cannot_suspend_themselves(...)       # 409: locking yourself out
async def test_every_suspension_and_reinstatement_is_recorded(...)   # AccountAction rows
async def test_an_ordinary_learner_cannot_suspend_anybody(...)       # 403
async def test_the_roster_shows_who_is_suspended(...)                # GET /admin/learners
async def test_suspension_touches_no_learner_work(...)
    # Their conversations, notes and mastery rows are all still there afterwards.
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

- `accounts.suspend`: reason ≥ `MIN_REASON_LENGTH`; refuse self; stamp `suspended_at`; write the
  audit row; revoke the learner's **own** sessions only:

```python
    await session.execute(
        update(LearnerSession)
        .where(
            LearnerSession.learner_id == learner.id,
            LearnerSession.impersonated_by_id.is_(None),
            LearnerSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
```

  A visit is an administrator's credential, not the learner's, and support has to keep working on
  exactly the account that is in trouble.
- `resolve_session` (`app/services/auth.py`): after loading the learner, refuse an ordinary
  session whose learner is suspended, and refuse a visit whose *administrator* is suspended.
  Comment why: suspension has to bite on a session that already exists, not only at sign-in.
- `get_current_admin` and `require_operator` (`app/api/deps.py`): a suspended administrator is not
  an administrator.
- `LearnerUsage` and the roster query gain `suspended_at`.

- [ ] **Step 4: Run, regenerate types, full gate, commit**

Run: `uv run pytest tests/test_suspension.py tests/test_auth.py tests/test_admin_sudo.py tests/test_identity_exchange.py -q`,
then `uv run poe api-types`, the full gate, and the frontend gate.

```bash
git add app/services/accounts.py app/services/auth.py app/api/deps.py app/api/v1/admin.py \
  app/schemas/admin.py app/services/admin.py frontend/src/api/schema.d.ts tests/test_suspension.py
git commit -m "feat(admin): suspend and reinstate an account, with the reason recorded [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 6: A development sign-in the journeys can use

**Files:**
- Modify: `app/api/v1/auth.py` (dev-login), `app/schemas/auth.py`, `frontend/src/api/schema.d.ts`
- Modify: `tests/test_auth.py` (two new cases beside the existing dev-login tests)
- Modify: `frontend/e2e/journey.ts` and every spec that calls `register`

**Interfaces:**
- Produces: `POST /auth/dev-login` accepts an optional body `{"email": "..."}`. With an address it
  signs in as the learner with that address, creating one if needed; without it, the dev learner
  as before. Still 404 unless `dev_auto_login`.
- Produces: `frontend/e2e/journey.ts` exports `signIn(page) -> {email}` replacing `register`.

- [ ] **Step 1: Write the failing tests** (in `tests/test_auth.py`)

```python
async def test_dev_login_can_name_the_account_it_signs_in_as(anon_client) -> None:
    """The journeys need a fresh account per run, and the password form is going away."""
    # POST /auth/dev-login {"email": "journey-1@example.com"} → 200, a learner with that email
    # exists, and /auth/me is that learner. A second call with the same address reuses it.

async def test_dev_login_still_refuses_when_it_is_turned_off(anon_client) -> None:
    # With dev_auto_login off, the body makes no difference: 404.
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement**

`DevLoginRequest(BaseModel): email: EmailStr | None = None`, and the route takes
`body: DevLoginRequest | None = None`. With an address: normalise it, find the learner or create
one (handle via `unique_handle(session, handle_for(address))`). Extend the route's docstring: this
is also how the browser journeys sign in, because the production build has no password form and
Clerk's hosted UI cannot be driven offline.

- [ ] **Step 4: Move the journeys onto it**

In `frontend/e2e/journey.ts`, replace `register` with:

```typescript
/** Sign in as a fresh account, through the development sign-in (S21).
 *
 * Not through the UI: Clerk owns the sign-in form now, and its hosted UI cannot be driven in a
 * CI browser with no network. What these journeys exist to prove is the product behind the
 * sign-in — the event stream, the cookie, the credentialed cross-origin fetch — so they take
 * the one door that works offline and exercise everything after it.
 *
 * `page.request` shares the browser context's cookie jar, so the page is signed in too.
 */
export async function signIn(page: Page): Promise<{ email: string }> {
  const email = `journey-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
  const response = await page.request.post(`${API_BASE}/api/v1/auth/dev-login`, {
    data: { email },
  });
  expect(response.ok(), `dev-login → ${response.status()}`).toBeTruthy();
  return { email };
}
```

Update `newAccount` (drop the password) and every caller: `chat.spec.ts`, `admin.spec.ts`,
`curriculum.spec.ts`, `library.spec.ts`, `practice.spec.ts`. A journey that asserted on the
registration form itself (the "signed-out browser is sent to sign in" case) keeps asserting that
the signed-out browser lands on `/signin` with the "Sign in" heading. After signing in through the
API, navigate to the page the journey needs (`await page.goto("/app/chat")`).

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_auth.py -q`, `uv run poe api-types`, then from `frontend/`:
`npm run build && npm run lint && npm test`, then the journeys: `npm run e2e`.
If the e2e run cannot start (no database, Redis, or browser binaries), say so explicitly in your
report with the error — do not claim it passed.

- [ ] **Step 6: Full gate, then commit**

```bash
git add app/api/v1/auth.py app/schemas/auth.py frontend/src/api/schema.d.ts tests/test_auth.py \
  frontend/e2e/journey.ts frontend/e2e/chat.spec.ts frontend/e2e/admin.spec.ts \
  frontend/e2e/curriculum.spec.ts frontend/e2e/library.spec.ts frontend/e2e/practice.spec.ts
git commit -m "test(e2e): sign the browser journeys in without the password form [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 7: Clerk in the browser

**Files:**
- Modify: `frontend/package.json`, `package-lock.json` (add `@clerk/react@6.16.1`)
- Create: `frontend/src/auth/mode.ts`, `frontend/src/auth/ClerkPanels.tsx`,
  `frontend/src/auth/session.ts`
- Modify: `frontend/src/main.tsx`, `App.tsx`, `pages/SignIn.tsx`, `components/NavBar.tsx`,
  `components/RequireLearner.tsx`, `api/auth.ts`
- Create: `frontend/src/auth/ClerkPanels.test.tsx`; modify `frontend/src/components/RequireLearner.test.tsx`
- Modify: `frontend/.env.example` (or create) — document `VITE_CLERK_PUBLISHABLE_KEY`

**Interfaces:**
- Consumes: `POST /api/v1/auth/exchange` (Task 3).
- Produces: `clerkEnabled: boolean` and `CLERK_PUBLISHABLE_KEY` in `src/auth/mode.ts`;
  `useExchange()` in `src/api/auth.ts`; `useSignOutEverywhere()` in `src/auth/session.ts`.

**Why the mode is a module constant:** `ClerkProvider` throws when it has no publishable key, and
Clerk's hooks only work inside it. A build with no key (vitest, CI, the Playwright journeys, a
checkout with no Clerk account) therefore must not render the provider at all, and must not call a
Clerk hook. Deciding once at module load — not per render — keeps every hook call unconditional.

- [ ] **Step 1: Install**

```bash
cd frontend && npm install @clerk/react@6.16.1
```

- [ ] **Step 2: Write the failing tests** (`src/auth/ClerkPanels.test.tsx`)

Mock `@clerk/react` (vitest `vi.mock`) with a controllable `useAuth` returning
`{isLoaded, isSignedIn, getToken}` and stub `SignIn`/`SignUp`/`UserButton` components, and mock
`./mode` so `clerkEnabled` is true. Cover:

- when Clerk is signed in and Guru is not, the exchange is called once with the Clerk token, and
  the learner query is refreshed;
- a 403 from the exchange shows the server's message and calls Clerk's `signOut`;
- a 502 shows a retry-ish message and does **not** sign out (the person is who they said they are;
  the server could not ask);
- signing out calls Guru's logout and Clerk's `signOut`;
- when Clerk reports signed out while Guru still has a session, Guru's logout is called.

And in `RequireLearner.test.tsx`, a case proving the keyless build still renders its children for a
signed-in learner without touching Clerk at all.

- [ ] **Step 3: Run to verify they fail.**

- [ ] **Step 4: Implement**

`src/auth/mode.ts`:

```typescript
/** Which sign-in this build has (S21).
 *
 * Decided once, at module load, because `ClerkProvider` throws without a key and Clerk's hooks
 * only work inside it — so a keyless build must never render the provider or call a hook. One
 * constant keeps every hook call unconditional and the two modes honest about which is running.
 */
export const CLERK_PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as
  | string
  | undefined;

export const clerkEnabled = Boolean(CLERK_PUBLISHABLE_KEY);
```

`main.tsx`: wrap `<App/>` in `<ClerkProvider publishableKey={CLERK_PUBLISHABLE_KEY!} afterSignOutUrl="/signin">`
only when `clerkEnabled`.

`src/api/auth.ts`: add

```typescript
/** Trade the Clerk session token for Guru's own session (S21). */
export function useExchange() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (token: string) => {
      const { data, error, response } = await api.POST("/api/v1/auth/exchange", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (error || !data) throw new ExchangeFailed(response.status, await detailOf(response));
      return data as CurrentLearner;
    },
    onSuccess: () => queryClient.clear(),
  });
}
```

with `class ExchangeFailed extends Error { constructor(readonly status: number, message: string) }`
and a small `detailOf(response)` helper that reads `{detail}` and falls back to a generic message.
Keep `useCurrentLearner`, `useLogout`, `useDevLogin`; `useLogin`/`useRegister` stay until Task 9.

`src/auth/ClerkPanels.tsx`: `ClerkSignInPanel` (renders `<SignIn routing="path" path="/signin"
signUpUrl="/sign-up" />` plus the exchange effect and any error banner), `ClerkSignUpPanel`
(`<SignUp routing="path" path="/sign-up" signInUrl="/signin" />`), `ClerkSessionWatcher` (calls
Guru logout when Clerk is loaded and signed out while `ME` exists), `ClerkUserButton`.

`src/auth/session.ts`: `useSignOutEverywhere` selected at module load —
`export const useSignOutEverywhere = clerkEnabled ? useClerkAndGuruSignOut : useGuruSignOut;`.

`pages/SignIn.tsx`: when `clerkEnabled`, render the heading "Sign in" and `<ClerkSignInPanel/>`;
otherwise keep today's page but with the password form removed in Task 9 — for now, leave the form
in place and add the Clerk branch above it, so this commit does not break the keyless build.

`App.tsx`: routes become `/signin/*` and, when `clerkEnabled`, `/sign-up/*` rendering
`<ClerkSignUpPanel/>`; `RequireLearner` mounts `<ClerkSessionWatcher/>` when `clerkEnabled`.

`NavBar.tsx`: use `useSignOutEverywhere()`; render `<ClerkUserButton/>` when `clerkEnabled`.

- [ ] **Step 5: Run and commit**

From `frontend/`: `npm test && npm run build && npm run lint`. Backend gate unchanged but run
`uv run poe api-contract` to be sure `schema.d.ts` still matches.

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/auth frontend/src/main.tsx \
  frontend/src/App.tsx frontend/src/pages/SignIn.tsx frontend/src/components/NavBar.tsx \
  frontend/src/components/RequireLearner.tsx frontend/src/components/RequireLearner.test.tsx \
  frontend/src/api/auth.ts frontend/.env.example
git commit -m "feat(frontend): sign in with Clerk and exchange for a Guru session [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 8: The portal's account controls

**Files:**
- Modify: `frontend/src/api/admin.ts`, `frontend/src/pages/Admin.tsx`,
  `frontend/src/pages/Admin.test.tsx`

**Interfaces:**
- Consumes: the routes from Tasks 4 and 5.
- Produces: `useInvitations`, `useInvite`, `useRevokeInvitation`, `useSuspend`, `useReinstate`.

- [ ] **Step 1: Write the failing tests** (`Admin.test.tsx`, following its existing mocking style)

- the Invitations panel lists open, accepted and revoked invitations with their status;
- inviting posts the address and shows the new invitation; a 409 shows the server's message;
- revoking asks for confirmation, posts, and updates the row;
- a roster row for a suspended learner shows a "Suspended" badge and offers Reinstate;
- Suspend requires a reason before the button is enabled, and posts reason and learner id;
- a failed call shows the server's `detail` rather than a generic failure.

- [ ] **Step 2: Run to verify they fail. Step 3: Implement. Step 4: `npm test && npm run build && npm run lint`.**

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/admin.ts frontend/src/pages/Admin.tsx frontend/src/pages/Admin.test.tsx
git commit -m "feat(admin): invite, suspend and reinstate from the portal [S21]

Co-Authored-By: <your model> <noreply@anthropic.com>"
git status --short
```

---

### Task 9: Retire the password system

**Files:**
- Modify: `app/api/v1/auth.py`, `app/services/auth.py`, `app/core/security.py`,
  `app/core/config.py`, `app/core/release.py`, `app/workers/tasks.py`, `app/schemas/auth.py`,
  `app/models/auth.py`, `app/models/learner.py`, `app/services/retention.py`, `pyproject.toml`
- Delete: `app/core/mail.py`, `tests/test_auth_recovery.py`
- Create: `db/migrations/versions/0055_retire_passwords.py`
- Modify: `tests/test_auth.py`, `tests/test_cross_connection.py`, `tests/test_migrations_with_data.py`
- Modify: `frontend/src/api/auth.ts`, `frontend/src/pages/SignIn.tsx`, `frontend/src/api/schema.d.ts`

**Interfaces:**
- Produces: model `LegacyPasswordDigest` (`learner_id` primary key, `digest`, `created_at`) — the
  holding table migration `0055` moves hashes into, and `poe identity-import` empties.
- Removes: `/auth/register`, `/auth/login`, `/auth/password`, `/auth/email`,
  `/auth/password-reset`, `/auth/password-reset/confirm`; `auth.register`, `authenticate`,
  `check_sign_in_allowed`, `record_failed_sign_in`, `purge_sign_in_attempts`,
  `begin_password_reset`, `complete_password_reset`, `purge_password_resets`, `change_password`,
  `change_email`, and their exceptions; `security.hash_password` / `verify_password`;
  `app.core.mail`; `PasswordResetToken`, `SignInAttempt`, `Learner.password_hash`; the
  `sign_in_*` and `password_reset_*` settings; `useLogin`, `useRegister`.

**Why the holding table:** the import needs the hashes, and dropping the column in the same change
that ships the import would destroy them before an operator could run it. `0055` moves them, the
import deletes each row as it succeeds, and nothing depends on deploy ordering.

- [ ] **Step 1: Write the failing tests first**

In `tests/test_auth.py`, replace the password cases with the boundary that must hold afterwards:

```python
async def test_the_password_routes_are_gone(anon_client: AsyncClient) -> None:
    """Not disabled, gone: an endpoint that answers 401 is an endpoint somebody can attack."""
    for path, body in [
        ("/auth/register", {"email": "a@example.com", "password": "x" * 12}),
        ("/auth/login", {"email": "a@example.com", "password": "x" * 12}),
        ("/auth/password", {"current_password": "x" * 12, "new_password": "y" * 12}),
        ("/auth/email", {"email": "b@example.com", "password": "x" * 12}),
        ("/auth/password-reset", {"email": "a@example.com"}),
        ("/auth/password-reset/confirm", {"token": "t", "new_password": "y" * 12}),
    ]:
        assert (await anon_client.post(f"{API}{path}", json=body)).status_code == 404


def test_the_openapi_document_offers_no_password_route() -> None:
    paths = app.openapi()["paths"]
    assert not [p for p in paths if "password" in p or p.endswith("/auth/register")]
```

In a new `tests/test_password_retirement.py`, test the migration's data move with the repo's
migration harness: a learner with a hash at `0054` has their digest in `legacy_password_digests`
and no `password_hash` column at `0055`; downgrading restores the column with the digest in it.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Remove the code**, in this order, letting `ruff check` find the orphans:

routes → service functions → schemas → security helpers → `mail.py` and its release check →
worker purges → settings → models. Then update:

- `app/core/release.py`: drop the mailer check; add
  `if not settings.clerk_secret_key: problems.append("GURU_CLERK_SECRET_KEY is unset — nobody could sign in")`
  and a check that `clerk_authorized_parties or cors_origins` is non-empty.
- `app/services/retention.py`: replace the `password_reset_tokens` entry with one for
  `legacy_password_digests` ("deleted — cascades from the learner; a digest for an account that no
  longer exists is a credential for nobody").
- `pyproject.toml`: drop `argon2-cffi` **only if** nothing imports it any more (`rg argon2`).

- [ ] **Step 4: Write migration `0055_retire_passwords`**

`upgrade()`: create `legacy_password_digests`; `INSERT INTO legacy_password_digests (learner_id,
digest) SELECT id, password_hash FROM learners WHERE password_hash IS NOT NULL`; drop the check
constraint `ck_learners_password_requires_email` and the `password_hash` column; drop
`password_reset_tokens` and `sign_in_attempts`. `downgrade()`: re-add the column and constraint,
copy the digests back, recreate the two tables empty, drop the holding table. Say all of that in
the docstring, including that the recreated tables are empty by construction.

- [ ] **Step 5: Frontend**

Remove `useLogin`, `useRegister`, `AuthFailed`, and the password form from `SignIn.tsx`; the page
becomes: Clerk panel when enabled, otherwise "Sign-in is not configured for this build." plus the
development button in dev. Regenerate `schema.d.ts`.

- [ ] **Step 6: Run everything**

`uv run pytest tests/test_auth.py tests/test_password_retirement.py tests/test_cross_connection.py tests/test_retention.py tests/test_migrations_with_data.py -q`,
`uv run poe api-types`, the full backend gate, `uv run poe db-check`, and from `frontend/`:
`npm test && npm run build && npm run lint`.

- [ ] **Step 7: Commit** (stage every modified path explicitly; include the deletions with
`git rm app/core/mail.py tests/test_auth_recovery.py`)

```
refactor(auth): retire Guru's password system [S21]
```

---

### Task 10: Import existing accounts into Clerk

**Files:**
- Create: `app/workers/identity_import.py`, `tests/test_identity_import.py`
- Modify: `pyproject.toml` (`identity-import = "python -m app.workers.identity_import"`)

**Interfaces:**
- Consumes: `IdentityProvider` (Task 1), `LegacyPasswordDigest` (Task 9).
- Produces: `plan(session, provider) -> list[ImportStep]` and `apply(session, provider, steps)`;
  `ImportStep(learner_id, handle, email, action: "create" | "link" | "skip", why: str)`.

- [ ] **Step 1: Write the failing tests** (`tests/test_identity_import.py`, fake provider)

- a learner with an address and a stored digest is created at the provider with that digest, the
  hasher `argon2id`, and `external_id` set to their learner id; `auth_subject` is stored; the
  digest row is deleted;
- a learner with no digest is created with no password requirement, and can then be linked;
- a learner whose address already exists once at the provider is linked, not created;
- an address matching **two** provider users is skipped and reported;
- a learner with no address is skipped and reported (the dev learner);
- a dry run performs nothing: no provider calls, no rows changed;
- running twice changes nothing the second time;
- a provider failure mid-run leaves the learners it already imported linked (each learner is its
  own transaction).

- [ ] **Step 2: Run to verify they fail. Step 3: Implement. Step 4: Run.**

The CLI prints one line per learner and a summary; `--apply` performs it, the default is a dry run.

- [ ] **Step 5: Full gate, then commit**

```
feat(identity): import existing accounts into the provider, passwords intact [S21]
```

---

### Task 11: Operator documentation

**Files:** `docs/RUNBOOK.md`, `.env.example`, `frontend/.env.example`

- [ ] **Step 1: Write it**

A new RUNBOOK section, "Identity (S21)", covering:

- provisioning: `npx -y clerk@latest init` in `frontend/`, claiming the app with
  `npx -y clerk@latest auth login`, and that temporary keys are not production-ready;
- dashboard configuration: sign-up mode **Restricted**, email as a required identifier, and the
  three social connections (Google, Meta, X) — with the note that development instances use
  Clerk's shared OAuth credentials while production needs your own app per provider, and that
  Apple is excluded because it needs a paid Apple Developer Program membership;
- every `GURU_CLERK_*` setting and where its value comes from;
- the bootstrap sequence for an empty deployment: `poe invite <email>` → the person signs up →
  `poe grant-admin <email>`;
- the import: run `poe identity-import` (dry run), read it, then `--apply`; the holding table it
  empties; what happens to a learner it skips;
- what Guru still enforces itself, and why: invitations, suspension, audited visits;
- the open operating decisions: administrators have no MFA on Clerk's free plan (Pro is
  $25/month), and production needs a domain.

`.env.example`: the `GURU_CLERK_*` block, with the retired `GURU_SIGN_IN_*` /
`GURU_PASSWORD_RESET_*` entries removed. `frontend/.env.example`: `VITE_CLERK_PUBLISHABLE_KEY`,
and that leaving it unset gives the development sign-in build.

- [ ] **Step 2: Check the links and commit**

Run `npx -y markdownlint-cli2 docs/RUNBOOK.md` (line-length warnings match the rest of the repo and
are fine) and confirm every file path and command you named exists.

```
docs: how to operate hosted identity [S21]
```
