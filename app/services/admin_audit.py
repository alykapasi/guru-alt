"""Central durable sudo intent and ASGI response completion audit."""

from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from app.models.auth import AdminAction, Impersonation
from app.services.auth import Authenticated


async def begin_action(request: Request, session: AsyncSession, who: Authenticated) -> None:
    visit_id = await session.scalar(
        select(Impersonation.id).where(Impersonation.session_id == who.session_id)
    )
    if visit_id is None:
        raise RuntimeError("sudo credential has no audit visit")
    route = request.scope["route"].path
    if not route.startswith("/api/v1/"):
        route = "/api/v1" + route
    row = AdminAction(
        impersonation_id=visit_id,
        method=request.method,
        route=route,
        resource_ids={key: str(value) for key, value in request.path_params.items()},
    )
    session.add(row)
    await session.commit()  # Must exist durably before any endpoint mutation.
    session.info["admin_actor_id"] = str(who.impersonated_by_id)
    session.info["admin_action_id"] = str(row.id)
    request.scope["admin_audit"] = (session.bind, row.id)


class AdminAuditMiddleware:
    """Complete only after the response stream finishes; incomplete rows remain inspectable."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        status_code = None
        terminal_body = False

        async def complete(code):
            audit = scope.get("admin_audit")
            if audit is not None:
                bind, action_id = audit
                async with AsyncSession(
                    bind=bind, join_transaction_mode="create_savepoint"
                ) as audit_session:
                    await audit_session.execute(
                        update(AdminAction)
                        .where(AdminAction.id == action_id)
                        .values(status_code=code, completed_at=datetime.now(UTC))
                    )
                    await audit_session.commit()

        async def audited_send(message):
            nonlocal status_code, terminal_body
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                terminal_body = True

        try:
            await self.app(scope, receive, audited_send)
            if terminal_body:
                await complete(status_code)
        except Exception:
            if status_code is None:
                await complete(500)
            raise
