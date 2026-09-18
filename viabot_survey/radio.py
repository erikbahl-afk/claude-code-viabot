"""Turn the modem's four radio numbers into one score, and say what limits it.

The modem reports RSRP, RSRQ, SINR and RSSI. Read individually they are hard to
act on, and read as an average they are actively misleading — so this reduces
them to a single 0-100 score plus the one fact that makes the score useful:
**which of the two independent problems is the one holding it down.**

Why two, and not four
---------------------

The four numbers carry two degrees of freedom, not four:

* **RSRP** — how much of *this cell's* signal arrives. Distance and concrete.
* **SINR** — how much of what arrives is the signal rather than noise and other
  transmitters. Interference and congestion.
* **RSSI** — everything the radio receives, signal and interference together.
* **RSRQ** — essentially the ratio of the first to the third.

So RSSI and RSRQ are largely a restatement of the relationship between RSRP and
SINR, which is why they are not in the score. They stay in the table underneath
it, where they are useful as a cross-check rather than as independent evidence.

Why the *minimum*, and not the mean
-----------------------------------

A link fails from either end, and the two failures need different remedies:

* Weak but clean (low RSRP, decent SINR) is a **coverage** problem — further
  from the cell than the concrete allows. Better antennas or a repeater help.
* Strong but dirty (decent RSRP, low SINR) is an **interference or congestion**
  problem. It is the "full bars, nothing works" case, and no antenna fixes it.

Averaging lets a strong signal hide a filthy one, which is exactly the case a
survey exists to find. Taking the worse of the two cannot: a good score means
both are good, and a bad score names which one to chase.

These bands are conventions, not requirements
---------------------------------------------

The cut points below are the ones the industry generally uses for LTE. Nothing
here has been checked against what *this* robot needs — like the dead-zone
thresholds, they are a starting point. The survey measures usability directly
(ping, and the load test), so a real walk can finally answer the question these
bands only assume: does the radio score actually predict whether teleop works?
"""

from __future__ import annotations

from typing import Any

#: Anchors for the linear maps, in the units the modem reports.
#:
#: RSRP: -120 dBm is the edge of usable LTE, -70 is as good as a garage gets.
#: SINR: below 0 dB the signal is weaker than the noise; 25 dB is excellent.
#: RSRQ: only used when the modem does not report SINR. -20 dB is unusable,
#:       -3 is excellent.
RSRP_FLOOR, RSRP_CEILING = -120.0, -70.0
SINR_FLOOR, SINR_CEILING = -5.0, 25.0
RSRQ_FLOOR, RSRQ_CEILING = -20.0, -3.0

#: What a score means in words. Lower bound of each band.
BANDS = ((75.0, "excellent"), (50.0, "good"), (25.0, "fair"), (0.0, "poor"))

STRENGTH, QUALITY = "strength", "quality"


def _scale(value: float | None, floor: float, ceiling: float) -> float | None:
    """Linear 0-100 between two anchors, clamped at both ends."""
    if value is None:
        return None
    try:
        fraction = (float(value) - floor) / (ceiling - floor)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return round(max(0.0, min(1.0, fraction)) * 100.0, 1)


def strength_score(rsrp: float | None) -> float | None:
    """How much of this cell's signal is arriving."""
    return _scale(rsrp, RSRP_FLOOR, RSRP_CEILING)


def quality_score(sinr: float | None, rsrq: float | None = None) -> float | None:
    """How much of what arrives is signal rather than noise.

    SINR is the better measure and most firmwares report it. RSRQ is the
    fallback for the ones that do not — it answers a similar question less
    directly, and a report should not simply go blank because of a firmware.
    """
    score = _scale(sinr, SINR_FLOOR, SINR_CEILING)
    if score is None:
        score = _scale(rsrq, RSRQ_FLOOR, RSRQ_CEILING)
    return score


def band(score: float | None) -> str:
    if score is None:
        return "unknown"
    for lower, name in BANDS:
        if score >= lower:
            return name
    return "poor"


def rate(sample: dict[str, Any]) -> dict[str, Any]:
    """Score one sample, and name what is holding it down.

    Returns the two component scores as well, because the combined number is
    only actionable alongside the reason for it.
    """
    strength = strength_score(sample.get("rsrp"))
    quality = quality_score(sample.get("sinr"), sample.get("rsrq"))

    present = [s for s in (strength, quality) if s is not None]
    if not present:
        return {"score": None, "strength": None, "quality": None,
                "limited_by": None, "band": "unknown"}

    score = min(present)
    # Only call something the limiting factor when both were measured and one
    # is genuinely worse. With a single reading there is nothing to compare.
    if strength is None or quality is None:
        limited_by = STRENGTH if quality is None else QUALITY
    else:
        limited_by = STRENGTH if strength <= quality else QUALITY

    return {"score": score, "strength": strength, "quality": quality,
            "limited_by": limited_by, "band": band(score)}


#: Quality expressed the way the chart shows it: four named steps, not a smooth
#: gradient. The reason is legibility, not taste — a continuous green-to-red
#: ramp carries its meaning in hue alone, and red against green is the single
#: most common colour-vision failure. Four named steps get a legend with the dB
#: ranges printed on it, so the colour is a shortcut to something also written
#: down rather than the only way to read the chart.
#:
#: Lower bound of each band, in dB of SINR.
QUALITY_BANDS: tuple[tuple[float, str], ...] = (
    (20.0, "excellent"),
    (13.0, "good"),
    (0.0, "fair"),
    (float("-inf"), "poor"),
)

#: The reserved status colours, plus a stroke width that widens as things get
#: worse. The width is the part that still works photocopied, printed in
#: greyscale, or read by someone who cannot tell red from green.
QUALITY_STYLE: dict[str, tuple[str, float]] = {
    "excellent": ("#0ca30c", 1.6),
    "good": ("#0ca30c", 1.6),
    "fair": ("#fab219", 2.3),
    "poor": ("#d03b3b", 3.2),
    "unknown": ("#646b7a", 1.4),
}

#: What each band means in the units the modem reports, for the legend. Colour
#: never travels without this.
QUALITY_LEGEND: tuple[tuple[str, str], ...] = (
    ("good", "SINR 13 dB and up — clean"),
    ("fair", "SINR 0 to 13 dB — usable, degrading"),
    ("poor", "SINR below 0 dB — noise louder than signal"),
)


def quality_band(sinr: float | None) -> str:
    """Which named step a SINR reading falls in."""
    if sinr is None:
        return "unknown"
    try:
        value = float(sinr)
    except (TypeError, ValueError):
        return "unknown"
    for lower, name in QUALITY_BANDS:
        if value >= lower:
            return name
    return "poor"
