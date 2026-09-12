"""Conversation CRUD + the plain tutor turn.

Mutators here ``flush`` (so ids are assigned) but do not ``commit`` — ``create_conversation``
and ``run_tutor_turn`` own their own commit boundaries around streaming.
"""

import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent.tutor import TutorState, build_tutor_graph
from app.core.config import get_settings
from app.learning import conversation_evidence, feedback, mastery
from app.learning.conversation_evidence import TurnIntent
from app.learning.diagnosis import FailureKind
from app.learning.grading import GradeResult, InvalidResponse
from app.llm.registry import LLMClient
from app.llm.types import ChatMessage, ChatRole, ModelRole, Usage
from app.models.assessment import Item
from app.models.chat import Conversation, ConversationPhase, ConversationSource, Message
from app.models.knowledge import KC
from app.models.learning import LearnerKCState
from app.rag.retrieval import retrieve
from app.schemas.assessment import AnswerSubmit
from app.schemas.chat import CheckResultRead
from app.services import assessment as assessment_svc
from app.services import knowledge as knowledge_svc
from app.services import learner_context
from app.services import session_runner as session_runner_svc
from app.services.assessment import item_to_read
from app.services.lesson_plan import PlanGroundingContext
from app.services.llm_log import log_llm_call
from app.services.turn_common import (
    TurnEvent,
    add_message,
    build_check_result,
    extract_citations,
    format_grounding,
    to_chat_messages,
)

log = structlog.get_logger(__name__)

TUTOR_SYSTEM_PROMPT = (
    "You are Guru, a patient and encouraging tutor. Explain ideas clearly and "
    "concisely, check the learner's understanding with questions, and prefer worked "
    "examples over lecturing. Adapt to the learner's level."
)


@dataclass(frozen=True)
class CheckOutcome:
    """A conversational attempt that was graded.

    Carries what the tutor is told before it replies *and* what the learner is told after: the
    same grade serves both, and they must not be allowed to drift apart. ``priors`` is each
    component's ability before the update, read before grading, so the movement can be reported
    as a movement rather than as a number the learner has no baseline for.
    """

    item: Item
    result: GradeResult
    priors: Mapping[uuid.UUID, mastery.Estimate]
    states: Sequence[LearnerKCState]
    # How often each failure kind has already come up per component, read before this attempt
    # was recorded (S09). A slip and a settled wrong idea are the same shape on one attempt,
    # and the response they need is opposite.
    prior_kinds: Mapping[uuid.UUID, Mapping[FailureKind, int]]


def _check_note(item: Item) -> str:
    """Tell the tutor which question is actually on the table.

    Until now the tutor never knew. The practice item was resolved, handed to the client as a
    widget, and left out of the prompt entirely — so the tutor's prose routinely asked
    something else, and the learner saw two different questions at once with only one of them
    graded.
    """
    return (
        "You have already put this practice question to the learner and they have not answered "
        "it yet. Keep it in play: restate it in your own words if that helps, but do not swap "
        "in a different question, because their next answer is graded against this one.\n"
        f"Practice question: {item.stem}"
    )


def _check_result(outcome: CheckOutcome, kcs: Sequence[KC]) -> CheckResultRead:
    """The learner-facing report for a conversational check.

    Built by the same function guided practice uses (``turn_common.build_check_result``), from
    the same ``CheckOutcome`` the tutor's own instruction is built from — the reply the learner
    reads and the record they check it against must not be able to disagree.
    """
    return build_check_result(
        item_id=outcome.item.id,
        result=outcome.result,
        priors=outcome.priors,
        states=outcome.states,
        kcs=kcs,
        prior_kinds=outcome.prior_kinds,
    )


def _feedback_note(outcome: CheckOutcome, kc_names: Mapping[uuid.UUID, str]) -> str:
    """The tutor's instruction after grading a conversational check.

    The evidence is read by ``app.learning.feedback``, shared with guided practice, so the two
    flows cannot reach different conclusions about the same mistake. Only the framing is local:
    a conversational check must not be followed by another question in the same turn.
    """
    return feedback.teaching_note(
        outcome.result,
        kc_names,
        prior_kinds=outcome.prior_kinds,
        opening="The learner has just attempted that question.",
        closing=(
            "Respond to what they actually wrote, then follow the guidance above. Do not pose "
            "another practice question this turn."
        ),
    )


async def create_conversation(
    session: AsyncSession,
    learner_id: uuid.UUID,
    title: str | None = None,
    kind: str = "chat",
    subject_id: uuid.UUID | None = None,
    source_ids: Sequence[uuid.UUID] = (),
) -> Conversation:
    conversation = Conversation(
        learner_id=learner_id, title=title, kind=kind, subject_id=subject_id
    )
    session.add(conversation)
    await session.flush()
    for source_id in source_ids:
        session.add(ConversationSource(conversation_id=conversation.id, source_id=source_id))
    await session.commit()
    await session.refresh(conversation, attribute_names=["conversation_sources"])
    return conversation


async def get_conversation(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Conversation | None:
    # populate_existing=True: without it, session.get() silently ignores the eager-load option
    # whenever the object is already in the session's identity map (e.g. a caller fetched it
    # separately first) — conversation_sources would stay unloaded and .source_ids would try an
    # unsupported sync lazy-load outside of any awaited ORM operation.
    return await session.get(
        Conversation,
        conversation_id,
        options=[selectinload(Conversation.conversation_sources)],
        populate_existing=True,
    )


async def update_conversation_title(
    session: AsyncSession, conversation: Conversation, title: str
) -> Conversation:
    conversation.title = title
    await session.commit()
    await session.refresh(conversation)
    return conversation


async def delete_conversation(session: AsyncSession, conversation: Conversation) -> None:
    # Messages cascade (Conversation.messages: cascade="all, delete-orphan" + FK ondelete
    # CASCADE); LLMCall.conversation_id is ondelete SET NULL, so the cost/token audit log
    # survives deletion by design.
    await session.delete(conversation)
    await session.commit()


async def list_conversations(
    session: AsyncSession, learner_id: uuid.UUID
) -> Sequence[Conversation]:
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.learner_id == learner_id)
        .order_by(Conversation.created_at.desc())
        .options(selectinload(Conversation.conversation_sources))
    )
    return result.all()


async def list_messages(session: AsyncSession, conversation_id: uuid.UUID) -> Sequence[Message]:
    """Every message in a conversation, oldest first (the transcript the client renders).

    ``id`` breaks the tie because ``created_at`` is transaction-start time: two messages
    written in one transaction carry the *same* timestamp, and ordering on it alone left their
    order to the planner. Harmless while rendering a whole transcript; not harmless once a
    window is taken from one end of it.
    """
    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
    )
    return result.all()


async def recent_messages(
    session: AsyncSession, conversation_id: uuid.UUID, *, limit: int
) -> Sequence[Message]:
    """The last ``limit`` messages, oldest first — the history a *turn* carries.

    Distinct from :func:`list_messages`, which is what the client displays. A turn used to
    forward the entire conversation, so a long-running one grew its own cost and context
    without bound until a provider refused it. Durable facts outlive the window through
    ``app/memory/``; the raw transcript beyond it does not.
    """
    result = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    return list(reversed(result.all()))


async def _resolve_check(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    user_content: str,
) -> tuple[Item | None, CheckOutcome | None]:
    """What this message does about the check that is open: returns (still open, graded).

    Mastery used to be reachable from exactly one place — the guided-practice workflow calling
    ``answer_item`` — so a learner who answered a question in conversation had demonstrated
    nothing the system recorded. This is the second door, and it is deliberately narrow: only a
    message that an intent gate reads as a real attempt at a question the system itself posed
    becomes an observation (S15, :mod:`app.learning.conversation_evidence`).

    Every failure here leaves the check standing and records nothing. A grading call that
    errors must not consume the learner's answer, and must not be resolved by guessing a score.
    """
    if (
        conversation.phase != ConversationPhase.AWAITING_ANSWER
        or conversation.active_item_id is None
    ):
        return None, None
    item = await assessment_svc.get_item(session, conversation.active_item_id)
    if item is None:
        # Belt and braces: ``active_item_id`` is ON DELETE SET NULL, so a deleted item clears
        # the pointer and the check above already caught it. This covers the read losing a race
        # with that delete — and grading is not the place to find out.
        return None, None

    intent, usage = await conversation_evidence.classify_intent(
        llm, question=item.stem, message=user_content
    )
    if usage.input_tokens or usage.output_tokens:
        await log_llm_call(
            learner_id=learner_id,
            conversation_id=conversation.id,
            role=conversation_evidence.CHECK_ROLE.value,
            spec=llm.spec(conversation_evidence.CHECK_ROLE),
            usage=usage,
        )

    if intent is TurnIntent.WITHDRAWAL:
        # Declining a question is not failing it. The check is dropped and nothing reaches the
        # tracer — a learner who would rather move on must be able to, without the refusal
        # itself being recorded as evidence they could not do it.
        log.info("chat.check_withdrawn", conversation_id=str(conversation.id))
        return None, None
    if intent is TurnIntent.DEFERRAL:
        # They engaged without answering, so the question stands — and the reply they are about
        # to get is help they will have had before attempting it.
        conversation.active_item_scaffolds += 1
        return item, None

    # Read before grading: `answer_item` returns the posterior, and a posterior with nothing
    # to compare it against is not something a learner can act on.
    kc_ids = [link.kc_id for link in item.kc_links]
    priors = await mastery.estimate_kcs(session, learner_id, kc_ids)
    # Read here too, and for the same reason: once this attempt is recorded the count would
    # include it, and "this is the third time" has to mean three counting this one.
    prior_kinds = await mastery.prior_failure_kinds(session, learner_id, kc_ids)
    try:
        result, states = await assessment_svc.answer_item(
            session,
            learner_id,
            item,
            AnswerSubmit(
                response={"text": user_content},
                # Server-counted, like every other assistance signal: it is the conversation's
                # own history that decides whether this was an independent demonstration.
                hints_used=conversation.active_item_scaffolds,
            ),
            llm=llm,
        )
    except InvalidResponse:
        # The response does not fit the item type at all. Only SHORT items are ever posed as
        # conversational checks (see below), so this means the conversation is holding an item
        # from elsewhere — leave it open rather than scoring an answer nobody could give.
        log.warning("chat.check_not_answerable", item_id=str(item.id))
        return item, None
    except Exception as exc:
        log.error("chat.check_grading_failed", item_id=str(item.id), error=str(exc))
        return item, None
    return None, CheckOutcome(
        item=item, result=result, priors=priors, states=states, prior_kinds=prior_kinds
    )


async def _pose_check(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    plan_context: PlanGroundingContext,
) -> Item | None:
    """A question to leave with the learner for the plan's active step, or ``None``.

    SHORT only, and that is a correctness constraint rather than a preference: a conversational
    answer arrives as prose, and grading prose against an MCQ reads ``response["choice"]``,
    finds nothing, and scores every answer wrong (see ``short_answer_item_for_kc``). Structured
    item types stay on the session surface, where the client sends a structured answer.
    """
    kc = await session.get(KC, plan_context.kc_id)
    if kc is None:
        return None
    return await session_runner_svc.short_answer_item_for_kc(
        session, llm, learner_id=learner_id, kc=kc
    )


async def run_tutor_turn(
    session: AsyncSession,
    llm: LLMClient,
    *,
    learner_id: uuid.UUID,
    conversation: Conversation,
    history: Sequence[Message],
    user_content: str,
    max_tokens: int,
    source_ids: Sequence[uuid.UUID] = (),
    persist_user: bool = True,
) -> AsyncIterator[TurnEvent]:
    """Persist the user turn, stream the tutor's reply through the graph, then persist it.

    The conversation's committed goal from the refinement gate (if any) is folded into the
    system prompt so generation stays grounded in it. The learner's active lesson-plan step (if
    any) is folded in the same way, so the plan actually drives the conversation rather than
    sitting beside it — see ``lesson_plan.get_active_step_context``.

    ``conversation.subject_id`` makes that lookup exact instead of the cross-subject heuristic,
    and scopes retrieval-grounded citations (Phase 7) together with ``source_ids``, the
    conversation's explicit narrowing if any — a "general" (subject-less) conversation retrieves
    nothing, cites nothing, and poses no check.

    Learner-global memory (facts/preferences/summaries from past conversations — see
    ``app.memory.retrieval``) is folded in on every turn, not gated behind a subject; this is
    what "wires memory into sessions" — write-back is a separate, on-demand step (see
    ``app.services.memory.write_back``). System-prompt order is pinned: base prompt -> goal ->
    plan-grounding -> check or feedback -> retrieval-grounding -> memory-note.

    **The check (S15).** A subject-scoped conversation leaves one practice question with the
    learner and keeps it there until they answer it, decline it, or the item disappears. While
    it stands, the tutor is told what it is, so its prose and the client's widget ask the same
    thing. When the learner attempts it, the attempt is graded through the same
    ``answer_item`` path guided practice uses — tracer, per-component evidence, diagnosis, plan
    revision — and the grade comes back into this turn's prompt as instructions about what to
    teach. A turn that resolves a check does not pose the next one: the feedback is the turn.

    ``persist_user`` is False when the caller has already written the learner's message
    and linked it to a durable turn record (S51); the content is still carried into this
    turn's model context, it is simply not appended to the transcript a second time.
    """
    conversation_id = conversation.id
    subject_id = conversation.subject_id
    messages = to_chat_messages(history)
    messages.append(ChatMessage(role=ChatRole.USER, content=user_content))
    if persist_user:
        await add_message(session, conversation_id, ChatRole.USER.value, user_content)
        await session.commit()

    context = await learner_context.gather(
        session, llm, learner_id=learner_id, conversation=conversation, query=user_content
    )

    open_check, outcome = await _resolve_check(
        session, llm, learner_id=learner_id, conversation=conversation, user_content=user_content
    )
    notes: list[str] = []
    check_result: CheckResultRead | None = None
    if outcome is not None:
        kcs = await knowledge_svc.get_kcs(session, [link.kc_id for link in outcome.item.kc_links])
        notes.append(_feedback_note(outcome, {kc.id: kc.name for kc in kcs}))
        check_result = _check_result(outcome, kcs)
    elif open_check is None and context.plan is not None and subject_id is not None:
        # Subject-scoped only, and the asymmetry with plan *grounding* is deliberate. A
        # subject-less conversation still gets grounding from whichever plan the learner was
        # last on, because a soft hint aimed at the wrong subject costs a slightly odd
        # paragraph. A check is not a hint: answering it writes a mastery observation, and
        # writing one against a KC picked by a cross-subject heuristic is evidence about a
        # skill the conversation may have had nothing to do with.
        open_check = await _pose_check(
            session, llm, learner_id=learner_id, plan_context=context.plan
        )
        conversation.active_item_scaffolds = 0
    if open_check is not None:
        notes.append(_check_note(open_check))

    hits = []
    grounding = None
    if subject_id is not None:
        hits = await retrieve(
            session,
            llm,
            user_content,
            learner_id=learner_id,
            subject_id=subject_id,
            source_ids=source_ids or None,
            limit=get_settings().chat_grounding_limit,
        )
        grounding = format_grounding(hits)

    system = learner_context.compose(
        TUTOR_SYSTEM_PROMPT,
        context,
        extra=notes,
        grounding=grounding,
        # An open check is a fixed task: the tutor is told not to swap the question, so it must
        # not also be told what level to aim a new one at.
        task_fixed=open_check is not None,
    )

    spec = llm.spec(ModelRole.SMART)
    initial: TutorState = {
        "messages": messages,
        "system": system,
        "max_tokens": max_tokens,
        "reply": "",
        "usage": Usage(),
    }

    reply = ""
    usage = Usage()
    try:
        async for mode, payload in build_tutor_graph(llm).astream(
            initial, stream_mode=["custom", "values"]
        ):
            if mode == "custom":
                yield TurnEvent(type="token", text=payload["token"])  # ty: ignore[invalid-argument-type]
            elif mode == "values":
                reply = payload["reply"]  # ty: ignore[invalid-argument-type]
                usage = payload["usage"]  # ty: ignore[invalid-argument-type]
    except Exception as exc:
        log.error("tutor.stream_failed", error=str(exc), model=spec.model)
        yield TurnEvent(type="error", detail="generation failed")
        return

    citations = extract_citations(reply, hits)
    assistant = await add_message(
        session,
        conversation_id,
        ChatRole.ASSISTANT.value,
        reply,
        model=spec.model,
        citations=citations,
        check_result=check_result,
    )
    cost = await log_llm_call(
        learner_id=learner_id,
        conversation_id=conversation_id,
        role=ModelRole.SMART.value,
        spec=spec,
        usage=usage,
    )
    await session.commit()
    yield TurnEvent(
        type="done",
        message_id=str(assistant.id),
        usage=usage,
        cost_usd=cost,
        item=item_to_read(open_check) if open_check is not None else None,
        # "check" is what tells the router this item is a question still awaiting an answer,
        # rather than one the turn merely mentioned — see ``_phase_after``.
        detail="check" if open_check is not None else "",
        citations=citations,
        check_result=check_result,
    )


async def record_phase(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    phase: ConversationPhase,
    *,
    active_item_id: uuid.UUID | None = None,
) -> None:
    """Persist what the conversation is waiting for, and which item if it is waiting on one.

    Best-effort: a turn that has already streamed its whole reply must not fail because the
    bookkeeping write did. A phase that fails to land degrades to the frontend's old
    behaviour for that one conversation, which is worse than correct but far better than a
    broken stream.
    """
    try:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return
        conversation.phase = phase
        conversation.active_item_id = active_item_id
        await session.commit()
    except Exception:
        log.warning("chat.phase_not_recorded", conversation_id=str(conversation_id))
