"""Reliability before validity — S59's first slice (O04, answered 2026-09-13).

Two questions, in this order, and neither of them is "does the product teach":

1. ``metrics`` — is the estimator's published probability true, *and* is it informative? A
   forecaster that always predicts the base rate is perfectly calibrated and worthless, so
   calibration is never reported without the skill score beside it.
2. ``agreement`` — do two independent graders land in the same place? The production grader
   supplies the partial credit every estimate is built from, and it has never been checked
   against anything but ten human labels and itself.

Why first: every outcome measurement is expressed in these numbers, so a miscalibrated
estimator makes them uninterpretable rather than merely noisy — and it fails silently. This
also works at the cohort size O01 implies; the designs that establish *effect* do not.
"""
