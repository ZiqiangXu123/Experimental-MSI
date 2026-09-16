from __future__ import annotations
import html
import math
from pathlib import Path
from typing import Iterable
_PALETTE = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#17becf']

def _esc(value: object) -> str:
    return html.escape(str(value))

def line_chart(path: Path, *, title: str, x_label: str, y_label: str, series: dict[str, list[tuple[float, float]]], x_tick_labels: list[tuple[float, str]] | None=None, log_y: bool=False) -> None:
    width, height = (1000, 620)
    left, right, top, bottom = (105, 35, 70, 90)
    plot_w = width - left - right
    plot_h = height - top - bottom
    all_points = [p for points in series.values() for p in points]
    if not all_points:
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='200'><text x='20' y='40'>No data</text></svg>", encoding='utf-8')
        return
    xs = [x for x, _ in all_points]
    ys_raw = [y for _, y in all_points if y > 0]
    ys = [math.log10(y) for y in ys_raw] if log_y else ys_raw
    x_min, x_max = (min(xs), max(xs))
    y_min, y_max = (min(ys), max(ys))
    if x_min == x_max:
        x_min -= 0.5
        x_max += 0.5
    if y_min == y_max:
        y_min *= 0.9
        y_max *= 1.1
        if y_min == y_max:
            y_max = y_min + 1
    y_pad = (y_max - y_min) * 0.08
    y_min -= y_pad
    y_max += y_pad

    def xp(x: float) -> float:
        return left + (x - x_min) / (x_max - x_min) * plot_w

    def yp(y: float) -> float:
        value = math.log10(y) if log_y else y
        return top + (y_max - value) / (y_max - y_min) * plot_h
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>", "<rect width='100%' height='100%' fill='white'/>", f"<text x='{width / 2}' y='34' text-anchor='middle' font-family='sans-serif' font-size='22' font-weight='bold'>{_esc(title)}</text>"]
    for i in range(6):
        frac = i / 5
        value = y_min + frac * (y_max - y_min)
        y = top + (1 - frac) * plot_h
        label_value = 10 ** value if log_y else value
        out.append(f"<line x1='{left}' y1='{y:.2f}' x2='{width - right}' y2='{y:.2f}' stroke='#dddddd'/>")
        out.append(f"<text x='{left - 12}' y='{y + 5:.2f}' text-anchor='end' font-family='sans-serif' font-size='13'>{label_value:.3g}</text>")
    out.append(f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height - bottom}' stroke='black'/>")
    out.append(f"<line x1='{left}' y1='{height - bottom}' x2='{width - right}' y2='{height - bottom}' stroke='black'/>")
    if x_tick_labels is None:
        x_tick_labels = [(x_min + i * (x_max - x_min) / 5, f'{x_min + i * (x_max - x_min) / 5:.3g}') for i in range(6)]
    for value, label in x_tick_labels:
        if value < x_min - 1e-12 or value > x_max + 1e-12:
            continue
        x = xp(value)
        out.append(f"<line x1='{x:.2f}' y1='{height - bottom}' x2='{x:.2f}' y2='{height - bottom + 6}' stroke='black'/>")
        out.append(f"<text x='{x:.2f}' y='{height - bottom + 25}' text-anchor='middle' font-family='sans-serif' font-size='12'>{_esc(label)}</text>")
    for idx, (name, points) in enumerate(series.items()):
        colour = _PALETTE[idx % len(_PALETTE)]
        ordered = sorted(points)
        coords = ' '.join((f'{xp(x):.2f},{yp(y):.2f}' for x, y in ordered if y > 0))
        out.append(f"<polyline points='{coords}' fill='none' stroke='{colour}' stroke-width='2.5'/>")
        for x, y in ordered:
            if y > 0:
                out.append(f"<circle cx='{xp(x):.2f}' cy='{yp(y):.2f}' r='3.5' fill='{colour}'/>")
        lx = width - right - 180
        ly = top + 18 * idx
        out.append(f"<line x1='{lx}' y1='{ly}' x2='{lx + 22}' y2='{ly}' stroke='{colour}' stroke-width='3'/>")
        out.append(f"<text x='{lx + 28}' y='{ly + 5}' font-family='sans-serif' font-size='13'>{_esc(name)}</text>")
    out.append(f"<text x='{left + plot_w / 2}' y='{height - 24}' text-anchor='middle' font-family='sans-serif' font-size='16'>{_esc(x_label)}</text>")
    out.append(f"<text transform='translate(27 {top + plot_h / 2}) rotate(-90)' text-anchor='middle' font-family='sans-serif' font-size='16'>{_esc(y_label)}</text>")
    out.append('</svg>')
    path.write_text('\n'.join(out), encoding='utf-8')

def stacked_bar_chart(path: Path, *, title: str, y_label: str, categories: list[str], phase_values: dict[str, list[float]]) -> None:
    width, height = (1000, 620)
    left, right, top, bottom = (110, 40, 70, 100)
    plot_w = width - left - right
    plot_h = height - top - bottom
    totals = [sum((phase_values[p][i] for p in phase_values)) for i in range(len(categories))]
    y_max = max(totals) * 1.1 if totals and max(totals) > 0 else 1.0
    bar_slot = plot_w / max(1, len(categories))
    bar_w = bar_slot * 0.55
    out = [f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' viewBox='0 0 {width} {height}'>", "<rect width='100%' height='100%' fill='white'/>", f"<text x='{width / 2}' y='34' text-anchor='middle' font-family='sans-serif' font-size='22' font-weight='bold'>{_esc(title)}</text>"]
    for i in range(6):
        value = y_max * i / 5
        y = top + plot_h - value / y_max * plot_h
        out.append(f"<line x1='{left}' y1='{y:.2f}' x2='{width - right}' y2='{y:.2f}' stroke='#dddddd'/>")
        out.append(f"<text x='{left - 12}' y='{y + 5:.2f}' text-anchor='end' font-family='sans-serif' font-size='13'>{value:.3g}</text>")
    for i, category in enumerate(categories):
        x = left + i * bar_slot + (bar_slot - bar_w) / 2
        cumulative = 0.0
        for pidx, (phase, values) in enumerate(phase_values.items()):
            value = values[i]
            h = value / y_max * plot_h
            y = top + plot_h - (cumulative + value) / y_max * plot_h
            colour = _PALETTE[pidx % len(_PALETTE)]
            out.append(f"<rect x='{x:.2f}' y='{y:.2f}' width='{bar_w:.2f}' height='{h:.2f}' fill='{colour}'/>")
            cumulative += value
        out.append(f"<text x='{x + bar_w / 2:.2f}' y='{height - bottom + 28}' text-anchor='middle' font-family='sans-serif' font-size='13'>{_esc(category)}</text>")
    for pidx, phase in enumerate(phase_values):
        colour = _PALETTE[pidx % len(_PALETTE)]
        lx = left + pidx * 150
        ly = height - 38
        out.append(f"<rect x='{lx}' y='{ly - 11}' width='14' height='14' fill='{colour}'/>")
        out.append(f"<text x='{lx + 20}' y='{ly + 1}' font-family='sans-serif' font-size='12'>{_esc(phase)}</text>")
    out.append(f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height - bottom}' stroke='black'/>")
    out.append(f"<line x1='{left}' y1='{height - bottom}' x2='{width - right}' y2='{height - bottom}' stroke='black'/>")
    out.append(f"<text transform='translate(27 {top + plot_h / 2}) rotate(-90)' text-anchor='middle' font-family='sans-serif' font-size='16'>{_esc(y_label)}</text>")
    out.append('</svg>')
    path.write_text('\n'.join(out), encoding='utf-8')
