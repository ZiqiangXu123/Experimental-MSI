from __future__ import annotations
import ast
import os
import sys
from importlib import resources
from dataclasses import dataclass
from typing import Any
from .constants import FIELD_ANCHOR, FIELD_PAYLOAD, FIELD_WITNESS, FUZZ_SURFACES, GUARD_IDS, MAX_FRAME_BYTES, STAGE_CRASH
from .fixture import build_fixture, honest_frame
from .crypto import Ed25519OpenSSL
from .pipeline import verify_frame
from .protocol import ProtocolError, decode_fields, encode_fields
from .util import SplitMix64, get_peak_rss_kib, system_info
TARGET_MODULES = {'protocol.py', 'pipeline.py', 'encoding.py', 'merkle.py', 'accumulator.py'}

@dataclass
class LineCoverage:
    lines: set[tuple[str, int]]

    def tracer(self, frame: Any, event: str, arg: Any) -> Any:
        if event == 'line':
            filename = os.path.basename(frame.f_code.co_filename)
            if filename in TARGET_MODULES:
                self.lines.add((filename, int(frame.f_lineno)))
        return self.tracer

def executable_lines() -> set[tuple[str, int]]:
    package_root = resources.files(__package__)
    lines: set[tuple[str, int]] = set()
    for name in TARGET_MODULES:
        source = package_root.joinpath(name).read_text(encoding='utf-8')
        tree = ast.parse(source, filename=name)
        for node in ast.walk(tree):
            lineno = getattr(node, 'lineno', None)
            if lineno is not None and isinstance(node, (ast.stmt, ast.ExceptHandler)):
                lines.add((name, int(lineno)))
    return lines

def _mutate_bytes(data: bytes, rng: SplitMix64, dictionary: tuple[bytes, ...]) -> bytes:
    if not data:
        data = b'\x00'
    operation = rng.randbelow(8)
    mutable = bytearray(data)
    if operation == 0:
        index = rng.randbelow(len(mutable))
        mutable[index] ^= 1 << rng.randbelow(8)
    elif operation == 1:
        start = rng.randbelow(len(mutable))
        end = min(len(mutable), start + 1 + rng.randbelow(min(32, len(mutable) - start)))
        del mutable[start:end]
    elif operation == 2:
        index = rng.randbelow(len(mutable) + 1)
        token = dictionary[rng.randbelow(len(dictionary))]
        mutable[index:index] = token
    elif operation == 3:
        start = rng.randbelow(len(mutable))
        width = 1 + rng.randbelow(min(32, len(mutable) - start))
        index = rng.randbelow(len(mutable) + 1)
        mutable[index:index] = mutable[start:start + width]
    elif operation == 4:
        index = rng.randbelow(len(mutable))
        width = min(4, len(mutable) - index)
        mutable[index:index + width] = b'\xff' * width
    elif operation == 5:
        index = rng.randbelow(len(mutable) + 1)
        width = 1 + rng.randbelow(16)
        mutable[index:index] = bytes((rng.randbelow(256) for _ in range(width)))
    elif operation == 6:
        if len(mutable) > 1:
            cut = rng.randbelow(len(mutable) - 1) + 1
            mutable = mutable[:cut]
    else:
        mutable.reverse()
    if len(mutable) > MAX_FRAME_BYTES + 1:
        mutable = mutable[:MAX_FRAME_BYTES + 1]
    return bytes(mutable)

def mutate_surface(frame: bytes, surface: str, rng: SplitMix64) -> bytes:
    dictionary = (b'MSI7', b'P7LD', b'W7TN', b'A7NC', b'\x00', b'\xff', b'raw-v1', b'zlib-v1', b'per_block', b'epoch_packed')
    if surface == 'frame':
        return _mutate_bytes(frame, rng, dictionary)
    try:
        fields = list(decode_fields(frame))
    except ProtocolError:
        return _mutate_bytes(frame, rng, dictionary)
    target_id = {'payload': FIELD_PAYLOAD, 'witness': FIELD_WITNESS, 'anchor': FIELD_ANCHOR}[surface]
    output: list[tuple[int, bytes]] = []
    for field_id, data in fields:
        output.append((field_id, _mutate_bytes(data, rng, dictionary) if field_id == target_id else data))
    return encode_fields(output)

def run_fuzz_case(case: dict[str, Any]) -> dict[str, Any]:
    surface = str(case['surface'])
    if surface not in FUZZ_SURFACES:
        raise ValueError('unsupported fuzz surface')
    budget = int(case['fuzz_executions'])
    seed = int(case['fuzz_seed'])
    rng = SplitMix64(seed)
    fixture = build_fixture(case)
    backend = Ed25519OpenSSL()
    position = int(case['query_position'])
    query = (fixture.shard, fixture.epoch, position)
    honest = honest_frame(fixture, position)
    corpus: list[bytes] = [honest]
    known_signatures: set[tuple[tuple[str, ...], str, str]] = set()
    covered_guards: set[str] = set()
    line_coverage = LineCoverage(set())
    executable = executable_lines()
    false_accepts = 0
    false_rejects = 0
    crashes = 0
    accepted_mutations = 0
    new_coverage_events = 0
    stage_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    interesting: list[dict[str, Any]] = []
    max_input_bytes = len(honest)
    line_trace_interval = max(1, int(case.get('line_trace_interval', 100)))
    for index in range(budget):
        base = corpus[rng.randbelow(len(corpus))]
        mutated = mutate_surface(base, surface, rng)
        max_input_bytes = max(max_input_bytes, len(mutated))
        if index % line_trace_interval == 0:
            old_trace = sys.gettrace()
            sys.settrace(line_coverage.tracer)
            try:
                decision = verify_frame(mutated, query, fixture, openssl=backend)
            finally:
                sys.settrace(old_trace)
        else:
            decision = verify_frame(mutated, query, fixture, openssl=backend)
        control = verify_frame(honest, query, fixture, openssl=backend)
        if not control.accepted:
            false_rejects += 1
        if decision.stage == STAGE_CRASH:
            crashes += 1
        stage_counts[decision.stage] = stage_counts.get(decision.stage, 0) + 1
        reason_counts[decision.reason] = reason_counts.get(decision.reason, 0) + 1
        if decision.accepted:
            accepted_mutations += 1
            if decision.block != fixture.blocks[position] or decision.root_used != fixture.entry.root or decision.metadata_violation:
                false_accepts += 1
        signature = (tuple(sorted(decision.guards)), decision.stage, decision.reason)
        newly_covered = set(decision.guards) - covered_guards
        if signature not in known_signatures or newly_covered:
            known_signatures.add(signature)
            covered_guards.update(decision.guards)
            new_coverage_events += 1
            if len(corpus) < int(case.get('max_corpus', 4096)):
                corpus.append(mutated)
            if len(interesting) < 256:
                interesting.append({'iteration': index, 'input_bytes': len(mutated), 'stage': decision.stage, 'reason': decision.reason, 'new_guards': sorted(newly_covered), 'input_sha256': __import__('hashlib').sha256(mutated).hexdigest()})
    return {'schema_version': 1, 'experiment': 'E7', 'trial_kind': 'fuzz', 'case': case, 'fixture': fixture.setup, 'system': system_info(), 'metrics': {'fuzz_executions': budget, 'paired_honest_controls': budget, 'false_accepts': false_accepts, 'false_rejects': false_rejects, 'crashes': crashes, 'accepted_mutations': accepted_mutations, 'corpus_size': len(corpus), 'new_coverage_events': new_coverage_events, 'semantic_guards_covered': len(covered_guards), 'semantic_guards_total': len(GUARD_IDS), 'semantic_guard_coverage': len(covered_guards) / len(GUARD_IDS), 'line_points_covered': len(line_coverage.lines & executable), 'line_points_total': len(executable), 'sampled_line_coverage': len(line_coverage.lines & executable) / len(executable) if executable else 0.0, 'max_input_bytes': max_input_bytes, 'peak_rss_kib': get_peak_rss_kib()}, 'stage_counts': stage_counts, 'reason_counts': reason_counts, 'covered_guards': sorted(covered_guards), 'covered_line_points': [f'{name}:{line}' for name, line in sorted(line_coverage.lines & executable)], 'line_universe': [f'{name}:{line}' for name, line in sorted(executable)], 'interesting_inputs': interesting, 'checks': {'zero_false_accepts': false_accepts == 0, 'zero_false_rejects': false_rejects == 0, 'zero_crashes': crashes == 0, 'budget_complete': sum(stage_counts.values()) == budget}}
