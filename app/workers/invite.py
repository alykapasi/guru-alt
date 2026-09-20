"""Invite the first person into an empty deployment — ``uv run poe invite <email> [--no-send]``
(S21).

The bootstrap has to live outside the API, for the same reason ``grant_admin`` does: nobody yet
holds an administrator session to reach ``POST /admin/invitations``, and the only authority that
exists before there is one is whoever holds the database. So the first invitation — the one that
eventually gets sent to the first administrator — is issued from here.

``--no-send``, or a deployment with no Clerk keys configured, records the invitation on Guru's
side without asking the provider to deliver anything: useful for a dry run, or for handing
somebody a sign-up link out of band. It never invents a session for the invite to be attributed
to — the row is written with ``invited_by_handle="operator (cli)"`` and no learner id, which is
what ``app.services.accounts.Actor`` being ``Learner | str`` exists for.
"""

import argparse
import asyncio
import sys

from app.core.config import get_settings
from app.core.db import SessionFactory
from app.core.identity import ProviderError, build_identity_provider
from app.services import accounts

CLI_HANDLE = "operator (cli)"


async def run(email: str, *, send: bool) -> int:
    provider = build_identity_provider(get_settings())
    notify = send and provider is not None

    async with SessionFactory() as session:
        try:
            invitation = await accounts.invite(
                session, provider, actor=CLI_HANDLE, email=email, notify=notify
            )
        except accounts.AlreadyEnrolled:
            print(f"{email} already has an account.", file=sys.stderr)
            return 1
        except accounts.AlreadyInvited:
            print(f"{email} already has an open invitation.", file=sys.stderr)
            return 1
        except ProviderError as exc:
            print(f"the sign-in provider could not be reached: {exc}", file=sys.stderr)
            return 1

    if notify:
        print(f"Invited {email}; the sign-in provider was asked to send it.")
    elif not send:
        print(f"Invited {email}; --no-send, so nothing was sent.")
    else:
        print(f"Invited {email}; no sign-in provider is configured, so nothing was sent.")
    print(f"invitation id: {invitation.id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Invite an address to enroll in Guru.")
    parser.add_argument("email", help="the address to invite")
    parser.add_argument(
        "--no-send",
        action="store_true",
        help="record the invitation without asking the provider to deliver it",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.email, send=not args.no_send)))


if __name__ == "__main__":
    main()
