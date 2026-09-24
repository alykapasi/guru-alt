"""Every number the teaching loop runs on that nobody has measured (S18).

S18 has sat at *Accepted · needs data* through sixteen passes while the thing it describes grew:
each pass that closed a loop added another threshold, and each one admitted in its own comment
that it was a taste parameter. Those admissions are scattered across nine modules, which means
the honest answer to "what uncalibrated numbers is the product running on?" has been "read the
codebase".

This is that answer in one place, and it is deliberately **not** a copy of the constants. Each
entry records the value as inventoried; ``live()`` reads what the code actually holds now. They
are asserted equal in the test suite, so changing one of these numbers fails a test naming it —
which is the point. A knob nobody has calibrated should not be quietly re-guessed; re-guessing
it is a decision, and this makes the decision visible.

It is an inventory, not a calibration. Nothing here sets a value or recommends one. What it
gives is the denominator: how many numbers a reading would have to settle, and which reading
would settle each.
"""

from __future__ import annotations

from pydantic import BaseModel


class Knob(BaseModel):
    """One uncalibrated constant: where it lives, what it decides, what would settle it."""

    id: str
    where: str
    value: float
    governs: str
    # The specific reading that would replace the guess. Not "more data" — the design that
    # produces the number, so the list doubles as a brief for what S59 has to be able to
    # answer before any of this stops being taste.
    settled_by: str


KNOBS: list[Knob] = [
    Knob(
        id="detour.failure_threshold",
        where="app.core.config.Settings.detour_failure_threshold",
        value=0.5,
        governs="what counts as having failed a component, for detours and for review format",
        settled_by="the score below which a learner does not go on to succeed unaided (S59)",
    ),
    Knob(
        id="detour.min_failures",
        where="app.core.config.Settings.detour_min_failures",
        value=2.0,
        governs="how many failures before a prerequisite detour is offered",
        settled_by="whether detouring at 1, 2 or 3 failures changes later unassisted success",
    ),
    Knob(
        id="detour.max_repeats",
        where="app.core.config.Settings.detour_max_repeats",
        value=2.0,
        governs="how often one prerequisite may be retried before the planner stops offering it",
        settled_by="the trip count past which returning to the same prerequisite stops helping",
    ),
    Knob(
        id="review.diagnose_min_failures",
        where="app.core.config.Settings.review_diagnose_min_failures",
        value=2.0,
        governs="when a due review stops being self-rated and becomes a diagnosable open question",
        settled_by="whether the costlier format recovers components the cheap one does not",
    ),
    Knob(
        id="practice.target_success_rate",
        where="app.core.config.Settings.practice_target_success_rate",
        value=0.75,
        governs="the difficulty practice aims at — how often the learner should succeed",
        settled_by="a within-learner comparison of success targets on later retention (S59)",
    ),
    Knob(
        id="retention.min_days",
        where="app.core.config.Settings.retention_min_days",
        value=1.0,
        governs="the gap after which a re-answer counts as retention rather than repetition",
        settled_by="the delayed-unassisted-probe design S59 defers, which is what defines it",
    ),
    Knob(
        id="mastery.conservative_bar",
        where="app.core.config.Settings.mastery_conservative_bar",
        value=0.5,
        governs="the lower confidence bound at which a component is treated as mastered",
        settled_by="the bound above which unassisted transfer tasks are actually passed",
    ),
    Knob(
        id="mastery.conservative_k",
        where="app.learning.tracer.CONSERVATIVE_K",
        value=1.0,
        governs="how far below the point estimate that mastery claim is made, in SDs",
        settled_by="the SD margin that best predicts unaided success at the next delayed check",
    ),
    Knob(
        id="profile.help_seeking_low",
        where="app.learning.lesson_plan.HELP_SEEKING_LOW",
        value=0.5,
        governs="below this, a learner is treated as rarely asking for help",
        settled_by="whether scaffolding chosen on this signal changes outcomes at all",
    ),
    Knob(
        id="profile.help_seeking_high",
        where="app.learning.lesson_plan.HELP_SEEKING_HIGH",
        value=1.5,
        governs="above this, a learner is treated as leaning on help",
        settled_by="whether scaffolding chosen on this signal changes outcomes at all",
    ),
    Knob(
        id="profile.persistence_high",
        where="app.learning.lesson_plan.PERSISTENCE_HIGH",
        value=0.6,
        governs="above this, a learner is treated as persisting through difficulty",
        settled_by="whether persistence-adjusted pacing changes completion or retention",
    ),
    Knob(
        id="format.difficulty_band",
        where="app.learning.lesson_plan.FORMAT_DIFFICULTY_BAND",
        value=0.1,
        governs="how close two formats' difficulties must be to count as interchangeable",
        settled_by="the difficulty difference learners actually notice between formats",
    ),
    Knob(
        id="format.score_margin",
        where="app.learning.lesson_plan.FORMAT_SCORE_MARGIN",
        value=0.1,
        governs="how much better one format must score before it is preferred",
        settled_by="the margin at which a format difference stops being sampling noise",
    ),
    Knob(
        id="activity.momentum_up_ratio",
        where="app.learning.activity.MOMENTUM_UP_RATIO",
        value=1.2,
        governs="the 7-day activity ratio read as accelerating",
        settled_by="whether the momentum reading predicts anything about the next week",
    ),
    Knob(
        id="activity.momentum_down_ratio",
        where="app.learning.activity.MOMENTUM_DOWN_RATIO",
        value=0.8,
        governs="the 7-day activity ratio read as slowing",
        settled_by="whether the momentum reading predicts anything about the next week",
    ),
    Knob(
        id="placement.some.ability",
        where='app.services.placement._ESTIMATE_BY_LEVEL["some"].ability',
        value=0.75,
        governs="where a learner claiming some background is seeded before any evidence",
        settled_by="the observed ability of learners who described themselves that way",
    ),
    Knob(
        id="placement.strong.ability",
        where='app.services.placement._ESTIMATE_BY_LEVEL["strong"].ability',
        value=1.75,
        governs="where a learner claiming strong background is seeded before any evidence",
        settled_by="the observed ability of learners who described themselves that way",
    ),
    Knob(
        id="ingest.ocr_min_text_chars",
        where="app.rag.adapters.pdf._MIN_TEXT_CHARS",
        value=16.0,
        governs="how little text a PDF page may yield before it is treated as scanned and OCR'd",
        settled_by=(
            "the character count below which a page's own text layer is worse than OCR of it, "
            "measured on pages where both are available (S27)"
        ),
    ),
]


def live() -> dict[str, float]:
    """Read what the code actually holds right now.

    Imported inside the function, not at module scope: this package is imported by a report
    that must run without the application's settings being loadable, and an inventory that
    cannot be printed unless the app starts is no use during exactly the incident where you
    want to know what numbers are in force.
    """
    from app.core.config import get_settings
    from app.learning import activity, tracer
    from app.learning import lesson_plan as learning_plan
    from app.rag.adapters import pdf
    from app.services import placement

    s = get_settings()
    return {
        "detour.failure_threshold": float(s.detour_failure_threshold),
        "detour.min_failures": float(s.detour_min_failures),
        "detour.max_repeats": float(s.detour_max_repeats),
        "review.diagnose_min_failures": float(s.review_diagnose_min_failures),
        "practice.target_success_rate": float(s.practice_target_success_rate),
        "retention.min_days": float(s.retention_min_days),
        "mastery.conservative_bar": float(s.mastery_conservative_bar),
        "mastery.conservative_k": float(tracer.CONSERVATIVE_K),
        "profile.help_seeking_low": float(learning_plan.HELP_SEEKING_LOW),
        "profile.help_seeking_high": float(learning_plan.HELP_SEEKING_HIGH),
        "profile.persistence_high": float(learning_plan.PERSISTENCE_HIGH),
        "format.difficulty_band": float(learning_plan.FORMAT_DIFFICULTY_BAND),
        "format.score_margin": float(learning_plan.FORMAT_SCORE_MARGIN),
        "activity.momentum_up_ratio": float(activity.MOMENTUM_UP_RATIO),
        "activity.momentum_down_ratio": float(activity.MOMENTUM_DOWN_RATIO),
        "placement.some.ability": float(placement._ESTIMATE_BY_LEVEL["some"].ability),
        "placement.strong.ability": float(placement._ESTIMATE_BY_LEVEL["strong"].ability),
        "ingest.ocr_min_text_chars": float(pdf._MIN_TEXT_CHARS),
    }


def drifted() -> list[tuple[Knob, float | None]]:
    """Entries whose inventoried value no longer matches the code. Empty is the healthy state.

    A non-empty result is not a bug in the product — it means somebody changed an uncalibrated
    number and the inventory has not been told. Either the new value is as arbitrary as the old
    one, in which case the entry needs updating, or it came from a reading, in which case the
    entry should stop being here at all.
    """
    current = live()
    out: list[tuple[Knob, float | None]] = []
    for knob in KNOBS:
        now = current.get(knob.id)
        if now is None or now != knob.value:
            out.append((knob, now))
    return out
