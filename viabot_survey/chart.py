"""Draw a walk's throughput as an SVG the report can carry inside itself.

The report has to open from a laptop with no network, off a USB stick, and
years from now — so there is no charting library to reach for. This draws the
plot as plain SVG elements with no script behind them, which the report then
embeds directly in the page.

What is plotted needs saying plainly, because it is easy to misread as a speed
test. The rig does not measure how fast the link *can* go: it holds a UDP
stream open at the bitrate a teleoperation session really uses, and records
what arrived each second. So the line's ceiling is the offered rate, not the
link's capacity, and the interesting shape is where it falls *below* that
dashed line — those are the seconds a real session would have lost video.

A stretch with no line at all is the severe case rather than missing data.
iperf3's control channel is TCP, so a link that fails badly enough takes the
test down with it and nothing is reported until it comes back.
"""

from __future__ import annotations

import html
from typing import Any, Sequence

#: Distinguishable without colour as well as with it, because these get
#: printed, photocopied and looked at by people who do not see red and green
#: apart. Uplink is solid, downlink dashed.
UPLINK_COLOR = "#1b4d8f"
DOWNLINK_COLOR = "#b26a00"

_MARGIN_LEFT = 54
_MARGIN_RIGHT = 16
_MARGIN_TOP = 22
_MARGIN_BOTTOM = 36

#: Step sizes for the walked-time axis, in seconds. Chosen so labels land on
#: numbers a person reads as times rather than as arbitrary offsets.
_TIME_STEPS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _nice_ceiling(value: float) -> float:
    """The next round number at or above ``value``, for an axis top."""
    if value <= 0:
        return 1.0
    step = 1.0
    while step < value:
        step *= 10
    while step > value:
        step /= 10
    for factor in (1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if step * factor >= value:
            return step * factor
    return step * 10


def _time_step(span_s: float, wanted: int = 6) -> int:
    for step in _TIME_STEPS:
        if span_s / step <= wanted:
            return step
    return _TIME_STEPS[-1]


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _runs(values: Sequence[float | None]) -> list[tuple[int, list[float]]]:
    """Contiguous stretches of real readings, with where each one starts.

    Gaps are never bridged. Joining the two ends of a stretch where the stream
    was down would draw a line through the worst moment of the walk.
    """
    out: list[tuple[int, list[float]]] = []
    start: int | None = None
    run: list[float] = []
    for index, value in enumerate(values):
        if value is None:
            if run and start is not None:
                out.append((start, run))
            start, run = None, []
            continue
        if start is None:
            start = index
        run.append(float(value))
    if run and start is not None:
        out.append((start, run))
    return out


def _series_paths(series: dict, columns: int, x_of, y_of,
                  color: str, dashed: bool) -> str:
    """One direction: a min-to-max band with the median drawn through it."""
    lo, mid, hi = series.get("lo") or [], series.get("mid") or [], series.get("hi") or []
    dash = ' stroke-dasharray="6 3"' if dashed else ""
    parts: list[str] = []
    for start, run in _runs(mid):
        indices = range(start, start + len(run))
        # The band only means something where a column covered more than one
        # second; with one reading per column min, median and max coincide.
        if any(lo[i] is not None and hi[i] is not None and hi[i] > lo[i]
               for i in indices):
            top = " ".join(f"{x_of(i):.1f},{y_of(hi[i]):.1f}" for i in indices)
            bottom = " ".join(f"{x_of(i):.1f},{y_of(lo[i]):.1f}"
                              for i in reversed(indices))
            parts.append(f'<polygon points="{top} {bottom}" fill="{color}" '
                         'fill-opacity="0.16" stroke="none"/>')
        points = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}"
                          for i, v in zip(indices, run))
        if len(run) == 1:
            parts.append(f'<circle cx="{x_of(start):.1f}" cy="{y_of(run[0]):.1f}" '
                         f'r="2" fill="{color}"/>')
        else:
            parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" '
                         f'stroke-width="1.8" stroke-linejoin="round"{dash}/>')
    return "".join(parts)


def throughput_svg(timeline: dict, *, width: int = 900, height: int = 260) -> str:
    """The whole plot, or "" when there is nothing worth drawing.

    ``timeline`` is what :func:`viabot_survey.report.throughput_timeline`
    produces: already bucketed into columns, so the page stays a sensible size
    however long the walk was.
    """
    columns = int(timeline.get("columns") or 0)
    if columns <= 0:
        return ""

    directions = [
        ("uplink", "Uplink (robot's video out)", UPLINK_COLOR, False),
        ("downlink", "Downlink (operator's commands in)", DOWNLINK_COLOR, True),
    ]
    present = [d for d in directions if (timeline.get(d[0]) or {}).get("mid")]
    if not present:
        return ""

    ceiling = 0.0
    for name, _label, _color, _dashed in present:
        series = timeline[name]
        ceiling = max(ceiling, *[v for v in series.get("hi") or [] if v is not None],
                      0.0)
        offered = series.get("offered_mbps")
        if offered:
            ceiling = max(ceiling, float(offered))
    y_max = _nice_ceiling(ceiling * 1.12)

    plot_w = width - _MARGIN_LEFT - _MARGIN_RIGHT
    plot_h = height - _MARGIN_TOP - _MARGIN_BOTTOM
    per_column = float(timeline.get("seconds_per_column") or 1.0)
    walked_s = float(timeline.get("walked_s") or columns * per_column)

    def x_of(column: float) -> float:
        return _MARGIN_LEFT + (column + 0.5) * plot_w / columns

    def x_edge(column: float) -> float:
        return _MARGIN_LEFT + column * plot_w / columns

    def y_of(value: float) -> float:
        return _MARGIN_TOP + plot_h * (1.0 - min(value, y_max) / y_max)

    # -- behind everything: where the walk was dead, and where it was paused --
    bands = []
    for start, end in timeline.get("no_stream") or []:
        left = x_edge(start)
        right = max(x_edge(end + 1), left + 1.5)
        bands.append(f'<rect x="{left:.1f}" y="{_MARGIN_TOP}" '
                     f'width="{right - left:.1f}" height="{plot_h}" '
                     'fill="#646b7a" fill-opacity="0.16"/>')
    for start, end in timeline.get("dead_zones") or []:
        left = x_edge(start)
        right = max(x_edge(end + 1), left + 1.5)
        bands.append(f'<rect x="{left:.1f}" y="{_MARGIN_TOP}" '
                     f'width="{right - left:.1f}" height="{plot_h}" '
                     'fill="#b3261e" fill-opacity="0.10"/>')
    for column in timeline.get("pauses") or []:
        bands.append(f'<line x1="{x_edge(column):.1f}" y1="{_MARGIN_TOP}" '
                     f'x2="{x_edge(column):.1f}" y2="{_MARGIN_TOP + plot_h}" '
                     'stroke="#646b7a" stroke-width="1" stroke-dasharray="2 3"/>')

    # -- axes ----------------------------------------------------------------
    grid = []
    tick = _nice_ceiling(y_max / 4.0)
    value = 0.0
    while value <= y_max + 1e-9:
        y = y_of(value)
        grid.append(f'<line x1="{_MARGIN_LEFT}" y1="{y:.1f}" '
                    f'x2="{width - _MARGIN_RIGHT}" y2="{y:.1f}" '
                    'stroke="#e3e6ec" stroke-width="1"/>')
        grid.append(f'<text x="{_MARGIN_LEFT - 8}" y="{y + 4:.1f}" '
                    'text-anchor="end" font-size="11" fill="#646b7a">'
                    f'{value:g}</text>')
        value += tick

    step = _time_step(walked_s)
    at = 0.0
    while at <= walked_s + 1e-9:
        x = x_edge(at / per_column)
        grid.append(f'<text x="{x:.1f}" y="{_MARGIN_TOP + plot_h + 18:.1f}" '
                    'text-anchor="middle" font-size="11" fill="#646b7a">'
                    f'{_clock(at)}</text>')
        at += step

    # -- the offered rate, which is the ceiling the readings are measured against
    references = []
    for name, label, color, _dashed in present:
        offered = (timeline.get(name) or {}).get("offered_mbps")
        if not offered:
            continue
        y = y_of(float(offered))
        references.append(
            f'<line x1="{_MARGIN_LEFT}" y1="{y:.1f}" x2="{width - _MARGIN_RIGHT}" '
            f'y2="{y:.1f}" stroke="{color}" stroke-width="1" stroke-dasharray="4 4" '
            'stroke-opacity="0.65"/>')
        references.append(
            f'<text x="{width - _MARGIN_RIGHT - 4}" y="{y - 4:.1f}" '
            f'text-anchor="end" font-size="10" fill="{color}">'
            f'sent {offered:g} Mbit/s</text>')

    lines = "".join(
        _series_paths(timeline[name], columns, x_of, y_of, color, dashed)
        for name, _label, color, dashed in present)

    legend = []
    for index, (_name, label, color, dashed) in enumerate(present):
        x = _MARGIN_LEFT + index * 250
        dash = ' stroke-dasharray="6 3"' if dashed else ""
        legend.append(
            f'<line x1="{x}" y1="{height - 6}" x2="{x + 22}" y2="{height - 6}" '
            f'stroke="{color}" stroke-width="1.8"{dash}/>'
            f'<text x="{x + 28}" y="{height - 2}" font-size="11" fill="#16181d">'
            f'{_e(label)}</text>')

    described = " and ".join(label for _n, label, _c, _d in present)
    return (
        f'<svg class="chart" viewBox="0 0 {width} {height + 12}" '
        f'role="img" width="100%" height="auto" '
        'xmlns="http://www.w3.org/2000/svg" '
        f'aria-label="Throughput delivered over {_clock(walked_s)} of walking">'
        f"<title>Throughput delivered over the walk</title>"
        f"<desc>{_e(described)}, in megabits per second, against walked time. "
        "Dashed horizontal lines are the rates the rig sent at; grey columns "
        "are seconds when nothing arrived at all; red columns are dead "
        "zones.</desc>"
        f'<rect x="{_MARGIN_LEFT}" y="{_MARGIN_TOP}" width="{plot_w}" '
        f'height="{plot_h}" fill="#ffffff"/>'
        + "".join(bands) + "".join(grid) + "".join(references) + lines
        + f'<line x1="{_MARGIN_LEFT}" y1="{_MARGIN_TOP + plot_h}" '
          f'x2="{width - _MARGIN_RIGHT}" y2="{_MARGIN_TOP + plot_h}" '
          'stroke="#646b7a" stroke-width="1"/>'
        + f'<text x="{_MARGIN_LEFT}" y="{_MARGIN_TOP - 5}" text-anchor="start" '
          'font-size="10" fill="#646b7a">Mbit/s delivered</text>'
        + "".join(legend) + "</svg>")
