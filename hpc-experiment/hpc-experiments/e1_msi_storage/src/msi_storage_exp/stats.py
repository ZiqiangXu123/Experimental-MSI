from __future__ import annotations
from dataclasses import dataclass
from math import sqrt
from typing import Sequence

@dataclass(frozen=True)
class LinearFit:
    intercept: float
    slope: float
    r_squared: float
    rmse: float
    max_abs_residual: float
    n: int

def linear_fit(x: Sequence[float], y: Sequence[float]) -> LinearFit:
    if len(x) != len(y) or len(x) < 2:
        raise ValueError('linear_fit requires equal-length sequences with at least two values')
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum(((v - mean_x) ** 2 for v in x))
    if sxx == 0:
        raise ValueError('x has zero variance')
    slope = sum(((a - mean_x) * (b - mean_y) for a, b in zip(x, y))) / sxx
    intercept = mean_y - slope * mean_x
    residuals = [b - (intercept + slope * a) for a, b in zip(x, y)]
    sse = sum((r * r for r in residuals))
    sst = sum(((b - mean_y) ** 2 for b in y))
    r_squared = 1.0 if sst == 0 and sse == 0 else 1.0 - sse / sst if sst else 0.0
    return LinearFit(intercept=intercept, slope=slope, r_squared=r_squared, rmse=sqrt(sse / n), max_abs_residual=max((abs(r) for r in residuals)), n=n)
