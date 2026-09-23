"""Pure statistics helpers for eval reporting — stdlib math only (no scipy).

Used by scripts/eval-peek.py (Wilson intervals next to pass rates, the
--compare McNemar pairing) and unit-tested directly. Kept tiny on purpose:
bakeoff decisions need exactly two numbers — how uncertain a single pass
rate is, and whether two brains actually differ on the SAME cases.
"""

from __future__ import annotations

from math import comb, sqrt


def wilson_interval(passes: int, n: int,
                    z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for a binomial pass rate (default 95%).

    Unlike the normal approximation it stays sane at the pass rates eval
    suites actually produce (0/10, 18/32) and never leaves [0, 1]. None when
    there are no runs.
    """
    if n <= 0:
        return None
    p = passes / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value over discordant pairs.

    b = cases passing only under brain A, c = only under brain B. Under the
    null (no difference) each discordant pair is a fair coin, so the p-value
    is the binomial tail at min(b, c) of n=b+c trials, doubled. 1.0 when
    there are no discordant pairs.
    """
    n = b + c
    if n <= 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2.0 * tail)
