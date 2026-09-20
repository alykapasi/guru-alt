"""Move existing accounts into the sign-in provider — ``uv run poe identity-import`` (S21).

Guru's learners predate Clerk. Nobody should have to be told their account still exists but
their password does not, so this hands Clerk the Argon2 digest `0055` set aside, and they sign
in with the password they already had.

Three things can be true of a learner, and the run says which for every one of them:

- Clerk has never heard of the address → **create**, with the digest if there is one.
- Clerk already has exactly one user with that address → **link**; do not create a second.
- Anything else → **skip**, with the reason printed. A learner with no address (the dev
  learner) has nothing to import, and an address matching *two* provider users is a question
  only a human can answer — picking one would hand somebody another person's account.

A dry run is the default and performs nothing: no provider calls, no rows changed. `--apply`
does it for real.

**Each learner is its own transaction.** A provider failure halfway through leaves everyone
already imported linked and committed, so a re-run picks up where it stopped instead of
starting over against a provider that now holds half the accounts. That is also what makes the
command safe to run twice: a learner with `auth_subject` set is already done, and is skipped.
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.core.identity import IdentityProvider, ProviderError, build_identity_provider
from app.models.auth import LegacyPasswordDigest
from app.models.learner import Learner
from app.services.auth import normalise_email

Action = Literal["create", "link", "skip"]


@dataclass(frozen=True)
class ImportStep:
    """What the run intends to do about one learner, and why.

    ``why`` is printed for every step, not only the skipped ones: an operator reading a dry run
    needs to see the reasoning behind a *create* as much as behind a refusal, because that is
    the line that would have been wrong.
    """

    learner_id: uuid.UUID
    handle: str
    email: str | None
    action: Action
    why: str


async def plan(session: AsyncSession, provider: IdentityProvider) -> list[ImportStep]:
    """Decide what to do about every learner, without changing anything.

    Asks the provider — a read — so a dry run still reports accurately which addresses it
    already holds. Nothing here writes, to Guru or to the provider.
    """
    learners = list(await session.scalars(select(Learner).order_by(Learner.created_at, Learner.id)))
    steps: list[ImportStep] = []

    for learner in learners:
        if learner.auth_subject is not None:
            steps.append(
                ImportStep(learner.id, learner.handle, learner.email, "skip", "already linked")
            )
            continue
        if not learner.email:
            steps.append(
                ImportStep(
                    learner.id,
                    learner.handle,
                    None,
                    "skip",
                    "no address — nothing to import them as",
                )
            )
            continue

        address = normalise_email(learner.email)
        existing = await provider.find_users_by_email(address)
        if len(existing) == 1:
            steps.append(
                ImportStep(
                    learner.id,
                    learner.handle,
                    address,
                    "link",
                    "the provider already has this address",
                )
            )
        elif existing:
            steps.append(
                ImportStep(
                    learner.id,
                    learner.handle,
                    address,
                    "skip",
                    f"{len(existing)} provider users share this address — a human must decide",
                )
            )
        else:
            digest = await session.scalar(
                select(LegacyPasswordDigest.digest).where(
                    LegacyPasswordDigest.learner_id == learner.id
                )
            )
            steps.append(
                ImportStep(
                    learner.id,
                    learner.handle,
                    address,
                    "create",
                    "with their existing password" if digest else "with no password set",
                )
            )

    return steps


async def apply(session: AsyncSession, provider: IdentityProvider, steps: list[ImportStep]) -> int:
    """Perform the steps. Returns the number of learners changed.

    One transaction per learner, committed as it goes — see the module docstring.
    """
    changed = 0

    for step in steps:
        if step.action == "skip" or step.email is None:
            continue

        learner = await session.get(Learner, step.learner_id)
        if learner is None or learner.auth_subject is not None:
            # Deleted or linked since the plan was drawn. The plan is a read of a moving
            # database, so the write re-checks rather than trusting it.
            continue

        if step.action == "link":
            found = await provider.find_users_by_email(step.email)
            if len(found) != 1:
                # It was one when the plan was drawn and is not now. Refuse rather than guess.
                print(
                    f"  {step.handle}: skipped — the provider no longer has exactly one user "
                    f"for {step.email}",
                    file=sys.stderr,
                )
                continue
            learner.auth_subject = found[0].subject
        else:
            digest = await session.scalar(
                select(LegacyPasswordDigest).where(LegacyPasswordDigest.learner_id == learner.id)
            )
            user = await provider.import_user(
                email=step.email,
                password_digest=digest.digest if digest else None,
                external_id=str(learner.id),
            )
            learner.auth_subject = user.subject
            if digest is not None:
                # The digest has done its one job. Keeping it would leave a credential lying
                # around for an account whose credential now lives somewhere else entirely.
                await session.delete(digest)

        await session.commit()
        changed += 1

    return changed


def _report(steps: list[ImportStep]) -> None:
    for step in steps:
        print(f"  {step.action:<6} {step.handle:<24} {step.email or '—':<32} {step.why}")
    counts = {
        action: sum(1 for s in steps if s.action == action) for action in ("create", "link", "skip")
    }
    print(f"\n{counts['create']} to create, {counts['link']} to link, {counts['skip']} skipped.")


async def run(*, apply_changes: bool) -> int:
    provider = build_identity_provider(get_settings())
    if provider is None:
        print(
            "no sign-in provider is configured — set GURU_CLERK_SECRET_KEY before importing",
            file=sys.stderr,
        )
        return 1

    async with SessionFactory() as session:
        try:
            steps = await plan(session, provider)
            _report(steps)

            if not apply_changes:
                print("\nDry run — nothing was changed. Re-run with --apply to perform it.")
                return 0

            changed = await apply(session, provider, steps)
        except ProviderError as exc:
            # Whatever was committed before this stays committed, on purpose: a re-run resumes.
            print(f"\nthe sign-in provider could not be reached: {exc}", file=sys.stderr)
            print("anything already imported is linked; re-run to continue.", file=sys.stderr)
            return 1

    print(f"\nImported {changed} learner(s).")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import existing Guru accounts into the sign-in provider."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the import; without it this is a dry run that changes nothing",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(apply_changes=args.apply)))


if __name__ == "__main__":
    main()
