from __future__ import annotations
import html
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence
PALETTE = ('#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#17becf')

def _escape(value: object) -> str:
    return html.escape(str(value))

def _finite_points(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in points if math.isfinite(float(x)) and math.isfinite(float(y))]

def line_chart(path: str | Path, *, title: str, x_label: str, y_label: str, series: Mapping[str, Sequence[tuple[float, float]]], x_log2: bool=False, x_tick_labels: Sequence[tuple[float, str]] | None=None) -> None:
    path = Path(path)
    width, height = (1080, 650)
    left, right, top, bottom = (110, 45, 75, 95)
    plot_w = width - left - right
    plot_h = height - top - bottom
    clean: dict[str, list[tuple[float, float]]] = {name: _finite_points(points) for name, points in series.items()}
    all_points = [point for points in clean.values() for point in points]
    if not all_points:
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='1080' height='220'><text x='30' y='60' font-family='sans-serif'>No data available</text></svg>", encoding='utf-8')
        return

    def transform_x(value: float) -> float:
        if x_log2:
            if value <= 0:
                raise ValueError('log2 x-axis requires positive values')
            return math.log2(value)
        return value
    transformed = [(transform_x(x), y) for x, y in all_points]
    xs = [x for x, _ in transformed]
    ys = [y for _, y in transformed]
    x_min, x_max = (min(xs), max(xs))
    y_min, y_max = (min(ys), max(ys))
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5
    if y_min == y_max:
        pad = abs(y_min) * 0.1 or 1.0
        y_min -= pad
        y_max += pad
    else:
        pad = (y_max - y_min) * 0.08
        y_min -= pad
        y_max += pad

    def xp(value: float) -> float:
        x = transform_x(value)
        return left + (x - x_min) / (x_max - x_min) * plot_w

    def yp(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_h
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>", "<rect width='100%' height='100%' fill='white'/>", f"<text x='{width / 2:.1f}' y='36' text-anchor='middle' font-family='sans-serif' font-size='22' font-weight='bold'>{_escape(title)}</text>"]
    for index in range(6):
        fraction = index / 5
        value = y_min + fraction * (y_max - y_min)
        y = top + (1.0 - fraction) * plot_h
        out.append(f"<line x1='{left}' y1='{y:.2f}' x2='{width - right}' y2='{y:.2f}' stroke='#dddddd'/>")
        out.append(f"<text x='{left - 12}' y='{y + 5:.2f}' text-anchor='end' font-family='sans-serif' font-size='13'>{value:.4g}</text>")
    out.extend([f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height - bottom}' stroke='black'/>", f"<line x1='{left}' y1='{height - bottom}' x2='{width - right}' y2='{height - bottom}' stroke='black'/>"])
    if x_tick_labels is None:
        raw_xs = sorted({x for x, _ in all_points})
        if len(raw_xs) <= 12:
            x_tick_labels = [(value, f'{value:g}') for value in raw_xs]
        else:
            x_tick_labels = []
            for index in range(6):
                transformed_value = x_min + index * (x_max - x_min) / 5
                raw_value = 2 ** transformed_value if x_log2 else transformed_value
                x_tick_labels.append((raw_value, f'{raw_value:.4g}'))
    for value, label in x_tick_labels:
        x = xp(float(value))
        out.append(f"<line x1='{x:.2f}' y1='{height - bottom}' x2='{x:.2f}' y2='{height - bottom + 6}' stroke='black'/>")
        out.append(f"<text x='{x:.2f}' y='{height - bottom + 27}' text-anchor='middle' font-family='sans-serif' font-size='12'>{_escape(label)}</text>")
    for index, (name, points) in enumerate(clean.items()):
        colour = PALETTE[index % len(PALETTE)]
        ordered = sorted(points, key=lambda pair: pair[0])
        coordinates = ' '.join((f'{xp(x):.2f},{yp(y):.2f}' for x, y in ordered))
        out.append(f"<polyline points='{coordinates}' fill='none' stroke='{colour}' stroke-width='2.6'/>")
        for x, y in ordered:
            out.append(f"<circle cx='{xp(x):.2f}' cy='{yp(y):.2f}' r='3.7' fill='{colour}'/>")
        legend_x = width - right - 230
        legend_y = top + 20 * index
        out.append(f"<line x1='{legend_x}' y1='{legend_y}' x2='{legend_x + 24}' y2='{legend_y}' stroke='{colour}' stroke-width='3'/>")
        out.append(f"<text x='{legend_x + 31}' y='{legend_y + 5}' font-family='sans-serif' font-size='13'>{_escape(name)}</text>")
    out.extend([f"<text x='{left + plot_w / 2:.1f}' y='{height - 24}' text-anchor='middle' font-family='sans-serif' font-size='16'>{_escape(x_label)}</text>", f"<text transform='translate(28 {top + plot_h / 2:.1f}) rotate(-90)' text-anchor='middle' font-family='sans-serif' font-size='16'>{_escape(y_label)}</text>", '</svg>'])
    path.write_text('\n'.join(out), encoding='utf-8')
