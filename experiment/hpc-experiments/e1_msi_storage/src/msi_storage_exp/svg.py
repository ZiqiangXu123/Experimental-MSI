from __future__ import annotations
import html
import math
from pathlib import Path
from typing import Iterable
GRAY = ['#202020', '#606060', '#a0a0a0', '#d0d0d0', '#f0f0f0']

def _esc(text: object) -> str:
    return html.escape(str(text))

def _nice_scale(max_value: float) -> tuple[float, str]:
    if max_value >= 1024 ** 4:
        return (1024 ** 4, 'TiB')
    if max_value >= 1024 ** 3:
        return (1024 ** 3, 'GiB')
    if max_value >= 1024 ** 2:
        return (1024 ** 2, 'MiB')
    if max_value >= 1024:
        return (1024, 'KiB')
    return (1.0, 'B')

def _format_tick(value: float, step: float) -> str:
    if not math.isfinite(value):
        return str(value)
    magnitude = abs(step)
    if magnitude == 0:
        decimals = 3
    elif magnitude >= 1:
        decimals = 0 if magnitude >= 2 else 1
    else:
        decimals = min(6, max(1, int(math.ceil(-math.log10(magnitude))) + 1))
    text = f'{value:.{decimals}f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text

def line_chart(path: Path, *, title: str, x_label: str, y_label: str, series: list[tuple[str, list[tuple[float, float]]]], log_x: bool=False, y_bytes: bool=False, width: int=1000, height: int=620) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    left, right, top, bottom = (105, 40, 70, 95)
    plot_w, plot_h = (width - left - right, height - top - bottom)
    all_points = [p for _, points in series for p in points]
    if not all_points:
        raise ValueError('line chart requires data')
    transformed_x = [math.log10(max(x, 1e-12)) if log_x else x for x, _ in all_points]
    ys = [y for _, y in all_points]
    xmin, xmax = (min(transformed_x), max(transformed_x))
    ymin, ymax = (min(ys), max(ys))
    if xmin == xmax:
        xmin -= 0.5
        xmax += 0.5
    if ymin == ymax:
        pad = max(abs(ymin) * 0.05, 1.0)
        ymin -= pad
        ymax += pad
    else:
        pad = (ymax - ymin) * 0.08
        ymin = max(0.0, ymin - pad)
        ymax += pad
    scale, unit = _nice_scale(ymax) if y_bytes else (1.0, '')
    ymin_s, ymax_s = (ymin / scale, ymax / scale)

    def px(x: float) -> float:
        tx = math.log10(max(x, 1e-12)) if log_x else x
        return left + (tx - xmin) / (xmax - xmin) * plot_w

    def py(y: float) -> float:
        return top + (1 - (y / scale - ymin_s) / (ymax_s - ymin_s)) * plot_h
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="white"/>', '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#111}.title{font-size:22px;font-weight:700}.axis{font-size:15px}.tick{font-size:13px}.legend{font-size:14px}</style>', f'<text x="{width / 2}" y="35" text-anchor="middle" class="title">{_esc(title)}</text>', f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#111" stroke-width="1.5"/>', f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#111" stroke-width="1.5"/>']
    y_step = (ymax_s - ymin_s) / 5
    for i in range(6):
        value = ymin_s + y_step * i
        y = top + plot_h - plot_h * i / 5
        out.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e0e0e0"/>')
        out.append(f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="tick">{_format_tick(value, y_step)}</text>')
    unique_x = sorted(set((x for x, _ in all_points)))
    if log_x:
        if len(unique_x) <= 12:
            xticks = unique_x
        else:
            xticks = [x for x in unique_x if x > 0 and abs(math.log2(x) - round(math.log2(x))) < 1e-12]
            if unique_x[0] not in xticks:
                xticks.insert(0, unique_x[0])
            if xticks and unique_x[-1] / xticks[-1] >= 1.35:
                xticks.append(unique_x[-1])
            if len(xticks) > 10:
                indices = sorted(set((round(i * (len(xticks) - 1) / 8) for i in range(9))))
                xticks = [xticks[i] for i in indices]
    elif len(unique_x) <= 12:
        xticks = unique_x
    else:
        indices = sorted(set((round(i * (len(unique_x) - 1) / 7) for i in range(8))))
        xticks = [unique_x[i] for i in indices]
    for x in xticks:
        xx = px(x)
        label = f'{x:g}'
        out.append(f'<line x1="{xx:.2f}" y1="{top + plot_h}" x2="{xx:.2f}" y2="{top + plot_h + 6}" stroke="#111"/>')
        out.append(f'<text x="{xx:.2f}" y="{top + plot_h + 24}" text-anchor="middle" class="tick">{_esc(label)}</text>')
    dash_patterns = ['', '8,5', '3,4', '12,4,3,4']
    markers = ['circle', 'square', 'diamond', 'triangle']
    for idx, (name, points) in enumerate(series):
        points = sorted(points)
        coords = ' '.join((f'{px(x):.2f},{py(y):.2f}' for x, y in points))
        dash = dash_patterns[idx % len(dash_patterns)]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
        out.append(f'<polyline points="{coords}" fill="none" stroke="{GRAY[idx % 3]}" stroke-width="2.5"{dash_attr}/>')
        for x, y in points:
            xx, yy = (px(x), py(y))
            if markers[idx % len(markers)] == 'circle':
                out.append(f'<circle cx="{xx:.2f}" cy="{yy:.2f}" r="4" fill="white" stroke="{GRAY[idx % 3]}" stroke-width="2"/>')
            elif markers[idx % len(markers)] == 'square':
                out.append(f'<rect x="{xx - 4:.2f}" y="{yy - 4:.2f}" width="8" height="8" fill="white" stroke="{GRAY[idx % 3]}" stroke-width="2"/>')
            elif markers[idx % len(markers)] == 'diamond':
                out.append(f'<path d="M {xx:.2f} {yy - 5:.2f} L {xx + 5:.2f} {yy:.2f} L {xx:.2f} {yy + 5:.2f} L {xx - 5:.2f} {yy:.2f} Z" fill="white" stroke="{GRAY[idx % 3]}" stroke-width="2"/>')
            else:
                out.append(f'<path d="M {xx:.2f} {yy - 5:.2f} L {xx + 5:.2f} {yy + 4:.2f} L {xx - 5:.2f} {yy + 4:.2f} Z" fill="white" stroke="{GRAY[idx % 3]}" stroke-width="2"/>')
    out.append(f'<text x="{left + plot_w / 2}" y="{height - 24}" text-anchor="middle" class="axis">{_esc(x_label)}</text>')
    ylabel = y_label + (f' ({unit})' if unit else '')
    out.append(f'<text x="28" y="{top + plot_h / 2}" text-anchor="middle" transform="rotate(-90 28 {top + plot_h / 2})" class="axis">{_esc(ylabel)}</text>')
    legend_x = left + 12
    legend_y = top + 18
    for idx, (name, _) in enumerate(series):
        y = legend_y + idx * 23
        dash = dash_patterns[idx % len(dash_patterns)]
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ''
        out.append(f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 32}" y2="{y}" stroke="{GRAY[idx % 3]}" stroke-width="2.5"{dash_attr}/>')
        out.append(f'<text x="{legend_x + 40}" y="{y + 5}" class="legend">{_esc(name)}</text>')
    out.append('</svg>')
    path.write_text('\n'.join(out), encoding='utf-8')

def normalized_stacked_bar(path: Path, *, title: str, categories: list[str], stacks: list[tuple[str, list[float]]], totals: list[float], width: int=1100, height: int=650) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    left, right, top, bottom = (95, 35, 75, 135)
    plot_w, plot_h = (width - left - right, height - top - bottom)
    n = len(categories)
    if n == 0:
        raise ValueError('bar chart requires categories')
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="white"/>', '<style>text{font-family:Arial,Helvetica,sans-serif;fill:#111}.title{font-size:22px;font-weight:700}.axis{font-size:15px}.tick{font-size:13px}.legend{font-size:14px}.total{font-size:12px}</style>', f'<text x="{width / 2}" y="36" text-anchor="middle" class="title">{_esc(title)}</text>', f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#111" stroke-width="1.5"/>', f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#111" stroke-width="1.5"/>']
    for pct in range(0, 101, 20):
        y = top + plot_h * (1 - pct / 100)
        out.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e0e0e0"/>')
        out.append(f'<text x="{left - 10}" y="{y + 4:.2f}" text-anchor="end" class="tick">{pct}%</text>')
    slot = plot_w / n
    bar_w = slot * 0.55
    patterns = [None, '6,3', '2,3']
    for i, cat in enumerate(categories):
        x = left + slot * (i + 0.5) - bar_w / 2
        cumulative = 0.0
        total = totals[i] if totals[i] else 1.0
        for j, (name, values) in enumerate(stacks):
            frac = values[i] / total
            h = plot_h * frac
            y = top + plot_h - cumulative - h
            out.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h:.2f}" fill="{GRAY[j + 1]}" stroke="#111" stroke-width="0.8"/>')
            if patterns[j] and h > 2:
                step = 8 if j == 1 else 6
                yy = y + step
                while yy < y + h:
                    out.append(f'<line x1="{x:.2f}" y1="{yy:.2f}" x2="{x + bar_w:.2f}" y2="{yy:.2f}" stroke="#777" stroke-width="0.6"/>')
                    yy += step
            cumulative += h
        out.append(f'<text x="{x + bar_w / 2:.2f}" y="{top + plot_h + 24}" text-anchor="middle" class="tick" transform="rotate(25 {x + bar_w / 2:.2f} {top + plot_h + 24})">{_esc(cat)}</text>')
        scale, unit = _nice_scale(totals[i])
        out.append(f'<text x="{x + bar_w / 2:.2f}" y="{top - 8}" text-anchor="middle" class="total">{totals[i] / scale:.2f} {unit}</text>')
    out.append(f'<text x="28" y="{top + plot_h / 2}" text-anchor="middle" transform="rotate(-90 28 {top + plot_h / 2})" class="axis">Share of total persistent bytes</text>')
    legend_x = left + 10
    legend_y = height - 35
    for j, (name, _) in enumerate(stacks):
        x = legend_x + j * 245
        out.append(f'<rect x="{x}" y="{legend_y - 13}" width="24" height="14" fill="{GRAY[j + 1]}" stroke="#111"/>')
        out.append(f'<text x="{x + 32}" y="{legend_y}" class="legend">{_esc(name)}</text>')
    out.append('</svg>')
    path.write_text('\n'.join(out), encoding='utf-8')
