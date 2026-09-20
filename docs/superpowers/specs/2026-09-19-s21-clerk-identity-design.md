# S21 — Hosted identity with Clerk — Design

**Status:** Approved design (2026-09-19). Feeds an implementation plan under
`docs/superpowers/plans/`. Second of three V0 workstream-1 slices, after
[S25a visibility](2026-09-19-s25-visibility-sweep-design.md) and before
[S25b publication](2026-09-19-s25-reviewed-publication-design.md).

## Context and goal

Guru authenticates with its own password system: Argon2id hashes, server-side session rows
(`learner_sessions`), reset tokens, a database-backed sign-in throttle, and a mail seam whose only
transport writes to the log. Registration is open, so V01's invite-only alpha is not enforced, and
there is no working recovery (S21).

Decision: **do not finish building authentication; delegate it.** Clerk owns credentials, sign-up,
email verification, recovery, social sign-in and the mail that goes with them. Guru keeps
everything that is *authorization*: who may enroll, the admin tier, audited sudo (P10), suspension,
and its own revocable session.

## Decisions

- **D1 — Clerk, Hobby plan.** Free to 50,000 monthly retained users. It includes invitation-only
  sign-up, verification/reset email delivery, a custom domain, and up to 3 social connections.
  It excludes banning users, MFA, and custom email templates, and caps Clerk's own impersonation at
  5 per month. Guru therefore keeps its own sudo and implements suspension itself.
- **D2 — Sign-in exchange, not per-request provider tokens.** The browser signs in with Clerk, then
  exchanges a Clerk session token once for the existing `guru_session` httpOnly cookie. Everything
  downstream is unchanged: `get_authenticated`, admin visits, SSE, `logout-all`, the `sign_in` test
  fixture. Accepted cost: two session layers. Signing out ends both, and a sign-out performed only
  on Clerk's side does not end a Guru session until Guru logout or expiry. Suspension is Guru-side
  and immediate regardless.
- **D3 — Retire Guru's password system.** Remove register, login, password change, email change,
  reset, the throttle, and the log-only mailer, with their tables and columns. Dev-login stays for
  local and test use; production already refuses to start with it on.
- **D4 — Guru issues and enforces invitations.** Admins invite from Guru's portal. Guru records the
  invitation and asks Clerk to send it. The exchange refuses any new identity without an open Guru
  invitation, so Guru stays invite-only even if Clerk's sign-up mode is misconfigured.
- **D5 — Social sign-in: Google, Meta (Facebook), X.** All three are free on both sides and fill the
  Hobby cap of 3. Apple is excluded: production needs an Apple Developer Program membership
  ($99/year), and it would be a fourth connection. No Guru code is provider-specific.
- **D6 — Existing accounts keep their passwords.** Clerk imports `argon2id` digests, which is
  Guru's stored format, so an import sets `password_digest` and `external_id = learner.id`.
  Re-invitation by email is the fallback link.

## Architecture

### Provider seam — `app/core/identity.py`

The only module that imports `clerk_backend_api` (pinned `clerk-backend-api==7.0.0`).

```python
@dataclass(frozen=True)
class ProviderUser:
    subject: str                    # Clerk user id
    external_id: str | None
    verified_emails: list[str]      # normalised; only addresses whose verification status is "verified"
    display_name: str | None

class IdentityProvider(Protocol):
    async def verify(self, token: str) -> str: ...          # returns the subject, or raises InvalidToken
    async def get_user(self, subject: str) -> ProviderUser: ...
    async def invite(self, email: str) -> str: ...           # returns the provider invitation id
    async def revoke_invitation(self, invitation_id: str) -> None: ...
    async def find_users_by_email(self, email: str) -> list[ProviderUser]: ...
    async def import_user(self, *, email: str, password_digest: str | None, external_id: str) -> ProviderUser: ...
```

- `ClerkIdentityProvider` verifies with `verify_token_async` and `VerifyTokenOptions(
  authorized_parties=..., jwt_key=settings.clerk_jwt_key or None, secret_key=...)`. It is
  networkless when the PEM is configured, and otherwise uses the SDK's JWKS path. Everything else
  uses the SDK's `*_async` methods. Invitations pass `redirect_url = settings.clerk_sign_up_url`.
- `FakeIdentityProvider` (tests) holds users and invitations in memory and accepts tokens it
  minted.
- `get_identity_provider` is a FastAPI dependency, overridden in tests like `get_llm_client`. When
  `clerk_secret_key` is unset, it returns `None`, and the exchange/invite routes answer
  `503 "sign-in is not configured"`.

**Settings:**

- `clerk_secret_key: SecretStr | None`
- `clerk_jwt_key: str | None` (PEM)
- `clerk_authorized_parties: list[str]` (default: `cors_origins`)
- `clerk_sign_up_url: str | None`

**Removed settings:** `sign_in_*`, `password_reset_*`.

### Data — migration `0054_hosted_identity`

- `learners.auth_subject` (text, unique, nullable): the Clerk user id.
- `learners.suspended_at` (timestamptz, nullable).
- `invitations`:
  - `id`, `email` (normalised), `created_at`;
  - `invited_by_id` (FK learners, SET NULL) and `invited_by_handle` (text);
  - `provider_invitation_id`;
  - `accepted_at`, `accepted_learner_id` (SET NULL);
  - `revoked_at`, `revoked_by_id` (SET NULL).
  - A partial unique index allows at most one *open* invitation per email.
- `account_actions`: the audit of administrative account operations.
  - `actor_id` (SET NULL) and `actor_handle` (text).
  - `learner_id` (SET NULL) and `learner_handle` (text, nullable).
  - `action` ∈ `invite`, `revoke_invitation`, `suspend`, `reinstate`.
  - `email` (nullable), `reason` (nullable), `created_at`.
  - Handles are stored as text so the record outlives both accounts, matching `impersonations`.

### Data — migration `0055_retire_passwords` (after the import has run)

Drops `learners.password_hash` with `ck_learners_password_requires_email`, and the
`password_reset_tokens` and `sign_in_attempts` tables. Downgrade recreates them **empty**; the
dropped hashes are not recoverable, and the RUNBOOK orders `poe identity-import --apply` first.
`argon2-cffi` leaves the dependencies if nothing else uses it.

### The exchange — `POST /api/v1/auth/exchange`

The Clerk token arrives as `Authorization: Bearer`. The route does not go through
`get_authenticated`.

1. `verify(token)`. On failure: 401 `"not authenticated"` (one message for every reason).
2. If a learner has `auth_subject == subject`, use it. Otherwise call `get_user(subject)` and try,
   in order:
   - a. `external_id` names an existing learner with a NULL `auth_subject` → link.
   - b. Any verified email equals the `email` of a learner with a NULL `auth_subject` → link.
     If verified emails match **two different** learners → 409, logged for an admin; never guess.
   - c. Any verified email has an **open** invitation → create the learner and mark the invitation
     accepted, in one transaction. The handle comes from the email via the existing
     `_handle_for` / `_unique_handle`, and `display_name` from Clerk.
   - d. Otherwise → 403 `"Guru is invite-only. Ask an administrator for an invitation."` No row
     is created.
3. If `suspended_at` is set → 403 `"This account is suspended."`
4. Sync `email` to the primary verified address when no other learner holds it.
5. `auth.issue(...)` sets the `guru_session` cookie and returns `LearnerRead`.

A 403 from step 2d or 3 is not a credential problem, so the text says what to do.

### Suspension

- `POST /admin/learners/{id}/suspend {reason}` requires a reason of at least 8 characters
  (`impersonation.MIN_REASON_LENGTH`), and refuses suspending yourself.
  - It stamps `suspended_at`, writes an `account_actions` row, and revokes the learner's own
    sessions immediately (rows with `impersonated_by_id IS NULL`).
  - Admin visits onto the account survive, so support can continue.
- `POST /admin/learners/{id}/reinstate {reason?}` clears `suspended_at` and audits.
- `resolve_session` refuses a suspended learner's own sessions, and any visit whose
  `impersonated_by` admin is suspended.
- `get_current_admin` and `require_operator` refuse suspended admins.

### Invitations (admin)

- `POST /admin/invitations {email}`:
  - 409 if the email already belongs to a learner or has an open invitation;
  - otherwise it calls `provider.invite` inside the transaction, then inserts the row and the
    audit, then commits;
  - on a provider error it returns 502 and nothing is recorded.
- `GET /admin/invitations` lists open, accepted and revoked invitations.
- `POST /admin/invitations/{id}/revoke` revokes at the provider first (a best-effort call whose
  failure is logged), then stamps and audits.
- All of these are `CurrentAdmin`, so impersonated sessions are refused as today.
- `poe invite <email>` bootstraps an empty deployment. `poe grant-admin` already exists.

### Import — `poe identity-import [--apply]`

Dry-run by default: it prints what it would do. For each learner with an email and no
`auth_subject`:

- If Clerk already has exactly one user with that email, link it.
- If Clerk has more than one, report it and skip.
- Otherwise create the user with:
  - `email_address=[email]` and `external_id=str(learner.id)`;
  - `password_digest=learner.password_hash` and `password_hasher="argon2id"`, when a hash exists;
  - `skip_password_requirement=True` when there is no hash.

It is idempotent. Learners without an email (e.g. `dev`) are skipped and listed.

### Retired code

- **Auth routes:** `register`, `login`, `password`, `email`, `password-reset`,
  `password-reset/confirm`.
- **Service:** the matching `app/services/auth.py` functions.
- **Security helpers:** `security.hash_password` / `verify_password`.
- **Mail:** `app/core/mail.py` and `release.py`'s mailer check.
- **Worker:** the reset and attempt purge jobs.
- **Schemas, tests and frontend:**
  - schemas: `RegisterRequest`, `LoginRequest` and the rest;
  - tests: `tests/test_auth_recovery.py` and the password parts of `tests/test_auth.py`;
  - frontend: `useLogin`, `useRegister` and the password form.

**Kept:** `logout`, `logout-all`, `me`, `sessions`, `dev-login` and all of P10.

`app/core/release.py` gains one check: production requires `clerk_secret_key` and non-empty
`clerk_authorized_parties`.

### Frontend

- **Package and provider.** Add `@clerk/react@6.16.1` (compatible with React 19.2, Node ≥ 20.9).
  `ClerkProvider` in `main.tsx` takes `VITE_CLERK_PUBLISHABLE_KEY`.
- **Routes.** `/sign-in` renders `<SignIn/>` (email, password, and the three social buttons). A new
  `/sign-up` renders `<SignUp/>`; it is reachable from invitation links only.
- **Exchange.** `RequireLearner` performs the exchange when Clerk `isSignedIn` and `ME` is null:
  `getToken()` → `POST /auth/exchange` → set `ME`.
  - A 403 shows the server's message and calls Clerk `signOut()`.
- **Signing out.** Sign-out calls Guru `logout` and then Clerk `signOut()`. If Clerk loads signed
  out while `ME` is set, Guru `logout` is called too.
- **Account management.** `<UserButton/>` in the nav covers email, password, connected accounts and
  Clerk's session list.
- **Admin portal.** It gains an Invitations panel and Suspend/Reinstate on learner rows, with the
  reason required.
- **One sign-in mode per build.** With a publishable key, only Clerk. Without one (vitest, CI
  Playwright, local without Clerk), only the existing dev-login button. vitest mocks
  `@clerk/react`.

### Provisioning and configuration (not code)

Run `npx -y clerk@latest init` in `frontend/`. It creates a claimable Clerk application with
temporary development keys; review its edits, and keep keys only in git-ignored env files. Then
configure Clerk:

- Sign-up mode **Restricted**, with email as a required identifier.
- Social connections: Google, Facebook, X. Development instances use Clerk's shared credentials.

The owner claims the app (`clerk auth login`). Production needs the owner's own OAuth app for each
provider and a domain; both are deferred operating choices recorded under S60 in `docs/RUNBOOK.md`.

## Verification

**API tests with `FakeIdentityProvider`:**

- Linking: link by `external_id`; link by any verified email; a verified email matching two
  learners → 409 with no change.
- Enrollment: create via open invitation, which marks it accepted and is single-use. Refused cases
  create **no** learner row:
  - uninvited;
  - unverified-email-only;
  - revoked invitation;
  - suspended account;
  - invalid token → 401.
- Email sync, which skips a taken address.
- Admin invite/revoke/suspend/reinstate:
  - non-admin and impersonated sessions → 403;
  - suspending yourself is refused;
  - a missing reason → 422;
  - suspension ends the learner's own sessions but keeps admin visits;
  - a suspended admin loses portal and ops access;
  - every action writes `account_actions`.

**Other checks:**

- **Real verification offline.** Generate an RSA key pair, sign an RS256 token with
  `sub`/`azp`/`exp`, and verify it through `ClerkIdentityProvider` with `clerk_jwt_key` set.
  Assert that a wrong `azp`, an expired token and a wrong key are all rejected.
- **Import** against the fake: dry run changes nothing, and it is idempotent. It passes through the
  digest, hasher and `external_id`, links existing users, and skips ambiguous ones.
- **Migrations.** `0054`/`0055` round-trip with data; `0055` removes the password artifacts. Check
  the release guard.
- **Frontend (vitest):**
  - the exchange on sign-in;
  - the 403 path, which signs Clerk out;
  - sign-out ends both sessions;
  - the dev-login-only mode when no key is set;
  - the admin invitation and suspension panels.

**Manual, recorded in the tracker:** one real browser sign-in against the Clerk development
instance, covering:

- invitation email → sign-up → exchange;
- an uninvited Google sign-in refused;
- sign-out.

CI does not prove this.

## Delivery

One commit per step, each tagged `[S21]`, in this order:

1. the seam and settings;
2. migration `0054`;
3. the exchange;
4. invitations;
5. suspension;
6. the import CLI;
7. the frontend;
8. retiring the password system with migration `0055`;
9. the RUNBOOK and tracker.

Gates at every commit:

- `poe check`, `poe format-check` and `poe api-contract`;
- `poe db-check`;
- the frontend build, lint and vitest.

## What this does not establish

That production sign-in works: it needs claimed keys, a domain and provider apps (S60). That
admins are protected by MFA: that needs Clerk Pro, an operating decision for S60. Or that Clerk's
availability is acceptable for the alpha. If Clerk is down, nobody new can sign in, though
existing Guru sessions keep working.
