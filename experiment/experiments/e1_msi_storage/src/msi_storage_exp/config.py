from __future__ import annotations
import itertools
import json
from pathlib import Path
from typing import Any
from .constants import CODEC_IDS, LAYOUT_IDS
from .util import canonical_json, point_id
REQUIRED_AXES = ('shards', 'historical_epochs', 'epoch_lengths', 'block_bytes', 'hot_windows', 'layouts', 'codecs', 'seeds')

def load_config(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        cfg = json.load(fh)
    if cfg.get('schema_version') != 1:
        raise ValueError('config schema_version must be 1')
    if not isinstance(cfg.get('sweeps'), list) or not cfg['sweeps']:
        raise ValueError('config must contain a non-empty sweeps list')
    return cfg

def _validate_point(point: dict[str, Any]) -> None:
    if point['shards'] < 1:
        raise ValueError('shards must be >= 1')
    if point['historical_epochs'] < 1:
        raise ValueError('historical_epochs must be >= 1')
    if point['epoch_length'] < 1:
        raise ValueError('epoch_length must be >= 1')
    if point['block_bytes'] < 34:
        raise ValueError('block_bytes must be >= 34')
    if point['hot_window'] < 0:
        raise ValueError('hot_window must be >= 0')
    if point['layout'] not in LAYOUT_IDS:
        raise ValueError(f"unsupported layout: {point['layout']}")
    if point['codec'] not in CODEC_IDS:
        raise ValueError(f"unsupported codec: {point['codec']}")
    allowed = {'msi', 'anchor', 'root_index', 'payload', 'aux', 'hot'}
    unknown = set(point['materialize']) - allowed
    if unknown:
        raise ValueError(f'unknown materialization components: {sorted(unknown)}')
    if point['codec'] != 'raw-v1' and 'payload' not in point['materialize']:
        raise ValueError('compressed payload sizes require payload materialization')

def expand_config(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    defaults = cfg.get('defaults', {})
    points: list[dict[str, Any]] = []
    for sweep in cfg['sweeps']:
        missing = [axis for axis in REQUIRED_AXES if axis not in sweep]
        if missing:
            raise ValueError(f"sweep {sweep.get('name')} missing axes: {missing}")
        axes = [sweep[name] for name in REQUIRED_AXES]
        for values in itertools.product(*axes):
            shards, epochs, lengths, block_bytes, hot, layout, codec, seed = values
            point = {'experiment': 'E1', 'sweep': sweep['name'], 'shards': int(shards), 'historical_epochs': int(epochs), 'epoch_length': int(lengths), 'block_bytes': int(block_bytes), 'hot_window': int(hot), 'layout': str(layout), 'codec': str(codec), 'seed': int(seed), 'materialize': sorted(set(sweep.get('materialize', defaults.get('materialize', [])))), 'max_materialized_bytes': int(sweep.get('max_materialized_bytes', defaults.get('max_materialized_bytes', 2 ** 30))), 'notes': sweep.get('notes', '')}
            _validate_point(point)
            point['point_id'] = point_id(point)
            points.append(point)
    points.sort(key=lambda p: (p['sweep'], p['shards'], p['historical_epochs'], p['epoch_length'], p['block_bytes'], p['hot_window'], p['layout'], p['codec'], p['seed']))
    ids = [p['point_id'] for p in points]
    if len(ids) != len(set(ids)):
        raise ValueError('configuration expanded to duplicate points')
    return points

def write_plan(points: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as fh:
        for point in points:
            fh.write(canonical_json(point) + '\n')

def read_plan_point(path: Path, index: int) -> dict[str, Any]:
    if index < 0:
        raise IndexError('plan index must be non-negative')
    with path.open('r', encoding='utf-8') as fh:
        for i, line in enumerate(fh):
            if i == index:
                return json.loads(line)
    raise IndexError(f'plan index {index} is out of range')

def count_plan(path: Path) -> int:
    with path.open('r', encoding='utf-8') as fh:
        return sum((1 for line in fh if line.strip()))
