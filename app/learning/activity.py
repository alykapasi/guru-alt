"""Pure policy for the dashboard's activity summary — streak + momentum.

Split from ``app.services.analytics`` the same way ``lesson_plan.py``/``tracer.py`` split
policy from I/O: these take plain values (no session), so they're trivial to unit test.
"""

from datetime import date, timedelta

MOMENTUM_UP_RATIO = 1.2
MOMENTUM_DOWN_RATIO = 0.8
"""v1-arbitrary thresholds on the 7-day observation-count ratio — same spirit as the lesson
plan's MASTERY_*_THRESHOLD, not calibrated against real outcome data."""


def streak_days(active_days: set[date], today: date) -> int:
    """Consecutive practice days ending today or yesterday — a missed day resets it, but
    today not yet having activity doesn't erase yesterday's streak mid-day."""
    if today in active_days:
        start = today
    elif today - timedelta(days=1) in active_days:
        start = today - timedelta(days=1)
    else:
        return 0

    streak = 0
    day = start
    while day in active_days:
        streak += 1
        day -= timedelta(days=1)
    return streak


def momentum_trend(last_7d: int, prior_7d: int) -> str:
    """Practice-volume trend over the trailing two 7-day windows — "momentum" tied to actual
    graded activity, not app opens (see MASTERPLAN's gamification decision)."""
    if last_7d == 0 and prior_7d == 0:
        return "none"
    if prior_7d == 0:
        return "up"
    ratio = last_7d / prior_7d
    if ratio >= MOMENTUM_UP_RATIO:
        return "up"
    if ratio <= MOMENTUM_DOWN_RATIO:
        return "down"
    return "steady"
