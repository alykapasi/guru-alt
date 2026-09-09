"""Fingerprints for diagnostics, so a log line can identify content without retaining it (S61).

Several failure paths logged the model's whole reply, or an exception whose message embeds the
input that failed validation. Those replies are generated from a learner's goal, uploads, and
answers, so a parse bug meant learner content sitting in log storage — with no retention
policy of its own — in order to answer a question ("is this the same failure as before?") that
a fingerprint answers just as well.
"""

import hashlib


def fingerprint(text: str) -> str:
    """``len=<n> sha256=<12 hex>`` — enough to tell two failures apart, and nothing else.

    Same text gives the same fingerprint across processes and restarts, so repeated failures
    still group; no amount of it reconstructs the content.
    """
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"len={len(text)} sha256={digest}"
