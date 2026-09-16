from __future__ import annotations
import html
from pathlib import Path
from typing import Iterable, Sequence
from .util import atomic_write_text
PALETTE = ('#3b82f6', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6', '#06b6d4', '#ec4899')

def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)

def _document(width: int, height: int, body: str, title: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">\n<style>\ntext {{ font-family: Arial, Helvetica, sans-serif; fill: #111827; }}\n.axis {{ stroke: #374151; stroke-width: 1; }}\n.grid {{ stroke: #e5e7eb; stroke-width: 1; }}\n.label {{ font-size: 12px; }}\n.small {{ font-size: 10px; }}\n.title {{ font-size: 17px; font-weight: 700; }}\n.legend {{ font-size: 11px; }}\n</style>\n<rect width="100%" height="100%" fill="white"/>\n<text x="{width / 2}" y="24" text-anchor="middle" class="title">{_esc(title)}</text>\n{body}\n</svg>\n'

def line_chart(path: Path, *, title: str, x_label: str, y_label: str, series: Sequence[tuple[str, Sequence[tuple[float, float]]]], width: int=900, height: int=520, y_min: float | None=None, y_max: float | None=None) -> None:
    left, right, top, bottom = (86, 24, 52, 76)
    plot_w, plot_h = (width - left - right, height - top - bottom)
    points = [(x, y) for _, rows in series for x, y in rows]
    if not points:
        atomic_write_text(path, _document(width, height, '<text x="40" y="80">No data</text>', title))
        return
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    xmin, xmax = (min(xs), max(xs))
    ymin = min(ys) if y_min is None else y_min
    ymax = max(ys) if y_max is None else y_max
    if xmax == xmin:
        xmax = xmin + 1.0
    if ymax == ymin:
        ymax = ymin + 1.0
    padx = (xmax - xmin) * 0.02
    pady = (ymax - ymin) * 0.08
    xmin -= padx
    xmax += padx
    if y_min is None:
        ymin -= pady
    if y_max is None:
        ymax += pady

    def sx(x: float) -> float:
        return left + (x - xmin) / (xmax - xmin) * plot_w

    def sy(y: float) -> float:
        return top + plot_h - (y - ymin) / (ymax - ymin) * plot_h
    body: list[str] = []
    for tick in range(6):
        fraction = tick / 5
        y = top + plot_h * (1 - fraction)
        value = ymin + (ymax - ymin) * fraction
        body.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" class="grid"/>')
        body.append(f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" class="small">{value:.3g}</text>')
    for tick in range(6):
        fraction = tick / 5
        x = left + plot_w * fraction
        value = xmin + (xmax - xmin) * fraction
        body.append(f'<line x1="{x:.2f}" y1="{top}" x2="{x:.2f}" y2="{top + plot_h}" class="grid"/>')
        body.append(f'<text x="{x:.2f}" y="{top + plot_h + 18}" text-anchor="middle" class="small">{value:.3g}</text>')
    body.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" class="axis"/>')
    body.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" class="axis"/>')
    for index, (name, rows) in enumerate(series):
        colour = PALETTE[index % len(PALETTE)]
        ordered = sorted(rows)
        poly = ' '.join((f'{sx(x):.2f},{sy(y):.2f}' for x, y in ordered))
        body.append(f'<polyline points="{poly}" fill="none" stroke="{colour}" stroke-width="2.2"/>')
        for x, y in ordered:
            body.append(f'<circle cx="{sx(x):.2f}" cy="{sy(y):.2f}" r="3" fill="{colour}"/>')
        lx = left + 8 + index % 4 * 185
        ly = 42 + index // 4 * 15
        body.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 22}" y2="{ly}" stroke="{colour}" stroke-width="3"/>')
        body.append(f'<text x="{lx + 27}" y="{ly + 4}" class="legend">{_esc(name)}</text>')
    body.append(f'<text x="{left + plot_w / 2}" y="{height - 22}" text-anchor="middle" class="label">{_esc(x_label)}</text>')
    body.append(f'<text x="20" y="{top + plot_h / 2}" text-anchor="middle" class="label" transform="rotate(-90 20 {top + plot_h / 2})">{_esc(y_label)}</text>')
    atomic_write_text(path, _document(width, height, '\n'.join(body), title))

def bar_chart(path: Path, *, title: str, categories: Sequence[str], series: Sequence[tuple[str, Sequence[float]]], y_label: str, width: int=1000, height: int=560, percent: bool=False) -> None:
    left, right, top, bottom = (88, 28, 58, 125)
    plot_w, plot_h = (width - left - right, height - top - bottom)
    values = [v for _, rows in series for v in rows]
    ymax = max(values) if values else 1.0
    ymax = max(1.0 if percent else 0.0, ymax) * 1.12 or 1.0
    group_w = plot_w / max(1, len(categories))
    bar_w = group_w * 0.78 / max(1, len(series))
    body: list[str] = []
    for tick in range(6):
        fraction = tick / 5
        y = top + plot_h * (1 - fraction)
        value = ymax * fraction
        label = f'{value * 100:.0f}%' if percent else f'{value:.3g}'
        body.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" class="grid"/>')
        body.append(f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" class="small">{label}</text>')
    for ci, category in enumerate(categories):
        group_x = left + ci * group_w
        for si, (_name, rows) in enumerate(series):
            value = float(rows[ci]) if ci < len(rows) else 0.0
            h = value / ymax * plot_h
            x = group_x + group_w * 0.11 + si * bar_w
            y = top + plot_h - h
            colour = PALETTE[si % len(PALETTE)]
            body.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w * 0.9:.2f}" height="{h:.2f}" fill="{colour}"/>')
        tx = group_x + group_w / 2
        ty = top + plot_h + 14
        body.append(f'<text x="{tx:.2f}" y="{ty}" text-anchor="end" class="small" transform="rotate(-42 {tx:.2f} {ty})">{_esc(category)}</text>')
    body.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" class="axis"/>')
    body.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" class="axis"/>')
    for si, (name, _rows) in enumerate(series):
        colour = PALETTE[si % len(PALETTE)]
        x = left + 10 + si % 4 * 205
        y = 44 + si // 4 * 16
        body.append(f'<rect x="{x}" y="{y - 9}" width="13" height="13" fill="{colour}"/>')
        body.append(f'<text x="{x + 18}" y="{y + 2}" class="legend">{_esc(name)}</text>')
    body.append(f'<text x="20" y="{top + plot_h / 2}" text-anchor="middle" class="label" transform="rotate(-90 20 {top + plot_h / 2})">{_esc(y_label)}</text>')
    atomic_write_text(path, _document(width, height, '\n'.join(body), title))

def stacked_bar(path: Path, *, title: str, categories: Sequence[str], stacks: Sequence[tuple[str, Sequence[float]]], y_label: str, width: int=1050, height: int=590) -> None:
    left, right, top, bottom = (90, 28, 62, 145)
    plot_w, plot_h = (width - left - right, height - top - bottom)
    totals = [sum((float(rows[i]) for _name, rows in stacks)) for i in range(len(categories))]
    ymax = max(totals) * 1.08 if totals and max(totals) > 0 else 1.0
    group_w = plot_w / max(1, len(categories))
    bar_w = group_w * 0.62
    body: list[str] = []
    for tick in range(6):
        fraction = tick / 5
        y = top + plot_h * (1 - fraction)
        value = ymax * fraction
        body.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" class="grid"/>')
        body.append(f'<text x="{left - 8}" y="{y + 4:.2f}" text-anchor="end" class="small">{value:.3g}</text>')
    for ci, category in enumerate(categories):
        x = left + ci * group_w + (group_w - bar_w) / 2
        cumulative = 0.0
        for si, (_name, rows) in enumerate(stacks):
            value = float(rows[ci]) if ci < len(rows) else 0.0
            h = value / ymax * plot_h
            y = top + plot_h - (cumulative + value) / ymax * plot_h
            body.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h:.2f}" fill="{PALETTE[si % len(PALETTE)]}"/>')
            cumulative += value
        tx = x + bar_w / 2
        ty = top + plot_h + 15
        body.append(f'<text x="{tx:.2f}" y="{ty}" text-anchor="end" class="small" transform="rotate(-45 {tx:.2f} {ty})">{_esc(category)}</text>')
    body.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" class="axis"/>')
    body.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" class="axis"/>')
    for si, (name, _rows) in enumerate(stacks):
        x = left + 10 + si % 4 * 210
        y = 46 + si // 4 * 16
        body.append(f'<rect x="{x}" y="{y - 9}" width="13" height="13" fill="{PALETTE[si % len(PALETTE)]}"/>')
        body.append(f'<text x="{x + 18}" y="{y + 2}" class="legend">{_esc(name)}</text>')
    body.append(f'<text x="20" y="{top + plot_h / 2}" text-anchor="middle" class="label" transform="rotate(-90 20 {top + plot_h / 2})">{_esc(y_label)}</text>')
    atomic_write_text(path, _document(width, height, '\n'.join(body), title))
