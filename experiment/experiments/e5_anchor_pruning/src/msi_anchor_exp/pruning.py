from __future__ import annotations
import heapq
import statistics
import time
from dataclasses import dataclass, field
from typing import Any
from .accumulator import AggregateAnchor, DirectAnchor, DynamicAccumulator, HashCounter, RootStatement, aggregate_anchor_bytes, checkpoint_object_bytes, direct_anchor_bytes, make_aggregate_evidence, make_checkpoint_anchor, make_direct_evidence, verify_aggregate_evidence, verify_direct_evidence
from .crypto import Ed25519Signer, Ed25519Verifier, deterministic_seed
from .epoch import commit_epoch
from .util import stable_seed, summarise
SAFE_FAULTS = {'none', 'not_final', 'local_root_mismatch', 'missing_certificate', 'wrong_position', 'stale_prefix_root', 'invalid_signature', 'uncovered_prefix'}

@dataclass
class EpochState:
    epoch: int
    position: int
    arrival_ms: float
    finality_time_ms: float
    payload_bytes: int
    retained: bool = True
    finality_ok: bool = False
    local_root_match: bool = False
    anchor_valid: bool = False
    local_root: bytes | None = None
    published_root: bytes | None = None
    statement: RootStatement | None = None
    pruned_at_ms: float | None = None
    fault_selected: bool = False
    fault_attempted: bool = False
    anchor_attempts: int = 0
    anchor_rejections: int = 0

@dataclass(order=True)
class Event:
    time_ms: float
    sequence: int
    kind: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)

def _flip_last_byte(value: bytes) -> bytes:
    if not value:
        return value
    return value[:-1] + bytes([value[-1] ^ 1])

def _selected(index: int, fraction: float, seed: int, namespace: str) -> bool:
    if fraction <= 0:
        return False
    period = max(1, int(round(1.0 / fraction)))
    if namespace == 'checkpoint':
        offset = 0
    else:
        offset = stable_seed(seed, namespace) % period
    return (index - 1) % period == offset

def _downsample(points: list[dict[str, float | int]], limit: int) -> list[dict[str, float | int]]:
    if len(points) <= limit:
        return points
    if limit < 2:
        return [points[-1]]
    step = (len(points) - 1) / (limit - 1)
    indices = sorted({round(index * step) for index in range(limit)})
    return [points[index] for index in indices]

def simulate_pruning(case: dict[str, Any]) -> dict[str, Any]:
    anchor_mode = str(case['anchor_mode'])
    if anchor_mode not in {'direct', 'aggregate'}:
        raise ValueError(f'unsupported anchor_mode: {anchor_mode}')
    policy = str(case.get('policy', 'safe'))
    if policy not in {'safe', 'unsafe_delete_after_upload'}:
        raise ValueError(f'unsupported pruning policy: {policy}')
    fault = str(case.get('fault', 'none'))
    if fault not in SAFE_FAULTS:
        raise ValueError(f'unsupported fault: {fault}')
    if anchor_mode == 'direct' and fault in {'stale_prefix_root', 'uncovered_prefix'}:
        raise ValueError(f'fault {fault} is aggregate-only')
    seed = int(case['seed'])
    shard = int(case.get('shard', 1))
    total_epochs = int(case['total_epochs'])
    block_count = int(case['blocks_per_epoch'])
    block_size = int(case['block_size'])
    arrival_rate = float(case['arrival_rate_eps'])
    finality_delay_ms = float(case['finality_delay_ms'])
    certificate_delay_ms = float(case['certificate_delay_ms'])
    retry_delay_ms = float(case['retry_delay_ms'])
    checkpoint_interval = int(case.get('checkpoint_interval', 1))
    hot_window = int(case['hot_window_epochs'])
    fault_fraction = float(case.get('fault_fraction', 0.05))
    timeline_limit = int(case.get('timeline_sample_limit', 2000))
    if total_epochs <= 0 or block_count <= 0 or block_size <= 0:
        raise ValueError('epoch and block sizes must be positive')
    if arrival_rate <= 0 or checkpoint_interval <= 0 or hot_window <= 0:
        raise ValueError('arrival rate, checkpoint interval, and hot window must be positive')
    payload_bytes_per_epoch = block_count * block_size
    signer = Ed25519Signer(deterministic_seed('checkpoint', int(case['anchor_key_seed'])))
    verifier = Ed25519Verifier(signer.public_key)
    direct_anchor = DirectAnchor(shard=shard, public_key=signer.public_key)
    accumulator = DynamicAccumulator()
    events: list[Event] = []
    sequence = 0

    def schedule(time_ms: float, kind: str, **payload: Any) -> None:
        nonlocal sequence
        sequence += 1
        heapq.heappush(events, Event(float(time_ms), sequence, kind, payload))
    for epoch in range(1, total_epochs + 1):
        arrival_ms = (epoch - 1) * 1000.0 / arrival_rate
        schedule(arrival_ms, 'upload', epoch=epoch)
        schedule(arrival_ms + finality_delay_ms, 'finalise', epoch=epoch)
    states: dict[int, EpochState] = {}
    active_aggregate_anchor: AggregateAnchor | None = None
    previous_valid_anchor: AggregateAnchor | None = None
    checkpoint_index = 0
    retained_count = 0
    max_retained_count = 0
    max_retained_bytes = 0
    max_overflow = 0
    backlog_area_epoch_ms = 0.0
    timeline: list[dict[str, float | int]] = []
    last_event_time = 0.0
    root_recompute_ns: list[int] = []
    root_recompute_hashes = 0
    root_recompute_bytes = 0
    append_update_ns: list[int] = []
    checkpoint_sign_ns: list[int] = []
    verify_anchor_ns: list[int] = []
    proof_bytes: list[int] = []
    time_to_prune_from_arrival_ms: list[float] = []
    time_to_prune_from_finality_ms: list[float] = []
    direct_cert_bytes = 0
    checkpoint_bytes = 0
    checkpoint_count = 0
    checkpoint_dropped = 0
    fault_injections = 0
    fault_rejections = 0
    anchor_attempts = 0
    anchor_accepts = 0
    anchor_rejects = 0
    pruned_without_finality = 0
    pruned_without_root_match = 0
    pruned_without_valid_anchor = 0
    unsafe_prunes = 0

    def record_backlog(now: float, reason: str) -> None:
        nonlocal max_retained_count, max_retained_bytes, max_overflow
        retained_bytes = retained_count * payload_bytes_per_epoch
        max_retained_count = max(max_retained_count, retained_count)
        max_retained_bytes = max(max_retained_bytes, retained_bytes)
        max_overflow = max(max_overflow, max(0, retained_count - hot_window))
        timeline.append({'time_ms': now, 'retained_epochs': retained_count, 'retained_payload_bytes': retained_bytes, 'hot_window_overflow_epochs': max(0, retained_count - hot_window), 'reason_code': {'upload': 1, 'finalise': 2, 'certificate': 3, 'checkpoint': 4, 'recovery': 5, 'prune': 6}.get(reason, 0)})

    def perform_prune(state: EpochState, now: float, anchor_ok: bool) -> None:
        nonlocal retained_count
        nonlocal pruned_without_finality, pruned_without_root_match
        nonlocal pruned_without_valid_anchor, unsafe_prunes
        if not state.retained:
            return
        allowed = policy == 'unsafe_delete_after_upload' or (state.finality_ok and state.local_root_match and anchor_ok)
        if not allowed:
            return
        if not state.finality_ok:
            pruned_without_finality += 1
        if not state.local_root_match:
            pruned_without_root_match += 1
        if not anchor_ok:
            pruned_without_valid_anchor += 1
        if policy == 'unsafe_delete_after_upload':
            unsafe_prunes += 1
        state.retained = False
        state.pruned_at_ms = now
        retained_count -= 1
        if state.finality_time_ms <= now:
            time_to_prune_from_finality_ms.append(now - state.finality_time_ms)
        time_to_prune_from_arrival_ms.append(now - state.arrival_ms)
        record_backlog(now, 'prune')

    def verify_direct(state: EpochState, evidence: bytes, now: float, recovery: bool) -> None:
        nonlocal anchor_attempts, anchor_accepts, anchor_rejects, fault_rejections
        state.anchor_attempts += 1
        anchor_attempts += 1
        started = time.perf_counter_ns()
        valid = verify_direct_evidence(state.statement, evidence, direct_anchor, verifier)
        verify_anchor_ns.append(time.perf_counter_ns() - started)
        state.anchor_valid = valid
        if valid:
            anchor_accepts += 1
        else:
            state.anchor_rejections += 1
            anchor_rejects += 1
            if state.fault_selected and (not recovery):
                fault_rejections += 1
        perform_prune(state, now, valid)
        record_backlog(now, 'recovery' if recovery else 'certificate')

    def make_valid_checkpoint(prefix: int) -> AggregateAnchor:
        nonlocal checkpoint_bytes, checkpoint_count
        root_started = time.perf_counter_ns()
        root = accumulator.root(prefix)
        root_elapsed = time.perf_counter_ns() - root_started
        append_update_ns.append(root_elapsed)
        sign_started = time.perf_counter_ns()
        anchor = make_checkpoint_anchor(shard, prefix, root, signer)
        checkpoint_sign_ns.append(time.perf_counter_ns() - sign_started)
        checkpoint_count += 1
        checkpoint_bytes += checkpoint_object_bytes()
        return anchor

    def verify_aggregate_pending(now: float, anchor: AggregateAnchor, *, recovery: bool, event_fault: str, valid_current_anchor: AggregateAnchor, old_anchor: AggregateAnchor | None) -> None:
        nonlocal active_aggregate_anchor, previous_valid_anchor
        nonlocal anchor_attempts, anchor_accepts, anchor_rejects, fault_rejections
        nonlocal fault_injections
        if recovery or event_fault in {'none', 'wrong_position', 'uncovered_prefix'}:
            previous_valid_anchor = active_aggregate_anchor
            active_aggregate_anchor = valid_current_anchor
        for epoch in sorted(states):
            state = states[epoch]
            if not state.retained or state.statement is None:
                continue
            if state.position > valid_current_anchor.prefix_count:
                continue
            selected_fault = state.fault_selected and (not state.fault_attempted) and (not recovery)
            used_anchor = anchor
            statement_for_evidence = state.statement
            proof_prefix = used_anchor.prefix_count
            if event_fault == 'uncovered_prefix' and selected_fault and (old_anchor is not None):
                used_anchor = old_anchor
                proof_prefix = old_anchor.prefix_count
                if state.position <= proof_prefix:
                    selected_fault = False
            if state.position > proof_prefix:
                state.anchor_attempts += 1
                anchor_attempts += 1
                state.anchor_rejections += 1
                anchor_rejects += 1
                state.fault_attempted = True
                fault_rejections += 1
                continue
            if event_fault == 'wrong_position' and selected_fault:
                wrong_position = state.position + 1 if state.position < proof_prefix else state.position - 1
                if wrong_position >= 1 and wrong_position in states and (states[wrong_position].statement is not None):
                    statement_for_evidence = states[wrong_position].statement or statement_for_evidence
                else:
                    statement_for_evidence = RootStatement(state.statement.shard, state.statement.epoch, state.statement.position, _flip_last_byte(state.statement.root))
                fault_injections += 1
            proof = accumulator.proof(statement_for_evidence.position, proof_prefix)
            evidence = make_aggregate_evidence(statement_for_evidence, proof)
            proof_bytes.append(len(evidence))
            counter = HashCounter()
            state.anchor_attempts += 1
            anchor_attempts += 1
            started = time.perf_counter_ns()
            valid = verify_aggregate_evidence(state.statement, evidence, used_anchor, verifier, counter)
            verify_anchor_ns.append(time.perf_counter_ns() - started)
            if selected_fault:
                state.fault_attempted = True
            state.anchor_valid = valid
            if valid:
                anchor_accepts += 1
            else:
                state.anchor_rejections += 1
                anchor_rejects += 1
                if selected_fault or event_fault in {'invalid_signature', 'stale_prefix_root'}:
                    fault_rejections += 1
            perform_prune(state, now, valid)
        record_backlog(now, 'recovery' if recovery else 'checkpoint')
    try:
        while events:
            event = heapq.heappop(events)
            now = event.time_ms
            backlog_area_epoch_ms += retained_count * max(0.0, now - last_event_time)
            last_event_time = now
            if event.kind == 'upload':
                epoch = int(event.payload['epoch'])
                arrival_ms = now
                state = EpochState(epoch=epoch, position=epoch, arrival_ms=arrival_ms, finality_time_ms=arrival_ms + finality_delay_ms, payload_bytes=payload_bytes_per_epoch, fault_selected=fault != 'none' and _selected(epoch, fault_fraction, seed, 'epoch'))
                states[epoch] = state
                retained_count += 1
                record_backlog(now, 'upload')
                if policy == 'unsafe_delete_after_upload':
                    perform_prune(state, now, anchor_ok=False)
                continue
            if event.kind == 'finalise':
                epoch = int(event.payload['epoch'])
                state = states[epoch]
                started = time.perf_counter_ns()
                local_root, metrics = commit_epoch(seed, shard, epoch, block_count, block_size)
                root_recompute_ns.append(time.perf_counter_ns() - started)
                root_recompute_hashes += metrics.hash_calls
                root_recompute_bytes += metrics.canonical_bytes
                state.local_root = local_root
                state.finality_ok = not (fault == 'not_final' and state.fault_selected)
                published_root = local_root
                if fault == 'local_root_mismatch' and state.fault_selected:
                    published_root = _flip_last_byte(local_root)
                    fault_injections += 1
                if fault == 'not_final' and state.fault_selected:
                    fault_injections += 1
                state.published_root = published_root
                state.local_root_match = local_root == published_root
                state.statement = RootStatement(shard, epoch, state.position, published_root)
                if anchor_mode == 'direct':
                    evidence_started = time.perf_counter_ns()
                    evidence = make_direct_evidence(state.statement, signer)
                    checkpoint_sign_ns.append(time.perf_counter_ns() - evidence_started)
                    direct_cert_bytes += len(evidence)
                    if fault == 'missing_certificate' and state.fault_selected:
                        fault_injections += 1
                    else:
                        delivered = evidence
                        if fault == 'wrong_position' and state.fault_selected:
                            wrong = RootStatement(shard, epoch, state.position + 1, published_root)
                            delivered = make_direct_evidence(wrong, signer)
                            fault_injections += 1
                            schedule(now + certificate_delay_ms + retry_delay_ms, 'direct_certificate', epoch=epoch, evidence=evidence, recovery=True)
                        elif fault == 'invalid_signature' and state.fault_selected:
                            delivered = _flip_last_byte(evidence)
                            fault_injections += 1
                            schedule(now + certificate_delay_ms + retry_delay_ms, 'direct_certificate', epoch=epoch, evidence=evidence, recovery=True)
                        schedule(now + certificate_delay_ms, 'direct_certificate', epoch=epoch, evidence=delivered, recovery=False)
                else:
                    append_started = time.perf_counter_ns()
                    accumulator.append_statement(state.statement)
                    append_update_ns.append(time.perf_counter_ns() - append_started)
                    should_checkpoint = accumulator.count % checkpoint_interval == 0 or epoch == total_epochs
                    if should_checkpoint:
                        checkpoint_index += 1
                        valid_anchor = make_valid_checkpoint(accumulator.count)
                        selected_checkpoint = fault != 'none' and _selected(checkpoint_index, fault_fraction, seed, 'checkpoint')
                        if fault == 'missing_certificate' and selected_checkpoint:
                            checkpoint_dropped += 1
                            fault_injections += 1
                        else:
                            delivered_anchor = valid_anchor
                            old_anchor: AggregateAnchor | None = None
                            event_fault = 'none'
                            needs_recovery = False
                            if fault == 'invalid_signature' and selected_checkpoint:
                                delivered_anchor = AggregateAnchor(shard=valid_anchor.shard, prefix_count=valid_anchor.prefix_count, accumulator_root=valid_anchor.accumulator_root, signature=_flip_last_byte(valid_anchor.signature), public_key=valid_anchor.public_key)
                                event_fault = fault
                                needs_recovery = True
                                fault_injections += 1
                            elif fault == 'stale_prefix_root' and selected_checkpoint:
                                old_prefix = max(1, valid_anchor.prefix_count - checkpoint_interval)
                                old_root = accumulator.root(old_prefix)
                                old_valid = make_checkpoint_anchor(shard, old_prefix, old_root, signer)
                                delivered_anchor = AggregateAnchor(shard=shard, prefix_count=valid_anchor.prefix_count, accumulator_root=old_valid.accumulator_root, signature=old_valid.signature, public_key=old_valid.public_key)
                                event_fault = fault
                                needs_recovery = True
                                fault_injections += 1
                            elif fault == 'uncovered_prefix' and selected_checkpoint:
                                old_prefix = max(1, valid_anchor.prefix_count - checkpoint_interval)
                                old_root = accumulator.root(old_prefix)
                                old_anchor = make_checkpoint_anchor(shard, old_prefix, old_root, signer)
                                event_fault = fault
                                needs_recovery = True
                                fault_injections += 1
                            elif fault == 'wrong_position':
                                event_fault = fault
                            schedule(now + certificate_delay_ms, 'aggregate_checkpoint', delivered_anchor=delivered_anchor, valid_anchor=valid_anchor, old_anchor=old_anchor, event_fault=event_fault, recovery=False)
                            if needs_recovery:
                                schedule(now + certificate_delay_ms + retry_delay_ms, 'aggregate_checkpoint', delivered_anchor=valid_anchor, valid_anchor=valid_anchor, old_anchor=None, event_fault='none', recovery=True)
                record_backlog(now, 'finalise')
                continue
            if event.kind == 'direct_certificate':
                state = states[int(event.payload['epoch'])]
                verify_direct(state, event.payload['evidence'], now, bool(event.payload.get('recovery', False)))
                continue
            if event.kind == 'aggregate_checkpoint':
                verify_aggregate_pending(now, event.payload['delivered_anchor'], recovery=bool(event.payload.get('recovery', False)), event_fault=str(event.payload.get('event_fault', 'none')), valid_current_anchor=event.payload['valid_anchor'], old_anchor=event.payload.get('old_anchor'))
                continue
            raise RuntimeError(f'unknown event kind: {event.kind}')
    finally:
        signer.close()
        verifier.close()
    end_time_ms = last_event_time
    pending_durations_ms = [max(0.0, end_time_ms - state.finality_time_ms) for state in states.values() if state.retained]
    pruned_epochs = sum((1 for state in states.values() if not state.retained))
    pending_epochs = total_epochs - pruned_epochs
    selected_epochs = sum((1 for state in states.values() if state.fault_selected))
    rejected_selected_epochs = sum((1 for state in states.values() if state.fault_selected and state.anchor_rejections > 0))
    final_anchor_bytes = direct_anchor_bytes() if anchor_mode == 'direct' else aggregate_anchor_bytes() if active_aggregate_anchor is not None else 0
    safety_violations = pruned_without_finality + pruned_without_root_match + pruned_without_valid_anchor
    result = {'schema_version': 1, 'experiment': 'E5', 'trial_kind': 'pruning', 'case': case, 'policy': policy, 'anchor_mode': anchor_mode, 'fault': fault, 'logical_time': {'end_time_ms': end_time_ms, 'arrival_rate_eps': arrival_rate, 'finality_delay_ms': finality_delay_ms, 'certificate_delay_ms': certificate_delay_ms, 'retry_delay_ms': retry_delay_ms}, 'counts': {'total_epochs': total_epochs, 'pruned_epochs': pruned_epochs, 'pending_epochs': pending_epochs, 'selected_fault_epochs': selected_epochs, 'rejected_selected_epochs': rejected_selected_epochs, 'fault_injections': fault_injections, 'fault_rejections': fault_rejections, 'anchor_attempts': anchor_attempts, 'anchor_accepts': anchor_accepts, 'anchor_rejects': anchor_rejects, 'checkpoint_count': checkpoint_count, 'checkpoint_dropped': checkpoint_dropped, 'unsafe_prunes': unsafe_prunes}, 'safety': {'pruned_before_finality': pruned_without_finality, 'pruned_without_local_root_match': pruned_without_root_match, 'pruned_without_valid_anchor': pruned_without_valid_anchor, 'total_gate_violations': safety_violations}, 'backlog': {'hot_window_epochs': hot_window, 'max_retained_epochs': max_retained_count, 'final_retained_epochs': retained_count, 'max_retained_payload_bytes': max_retained_bytes, 'final_retained_payload_bytes': retained_count * payload_bytes_per_epoch, 'max_hot_window_overflow_epochs': max_overflow, 'backlog_area_epoch_ms': backlog_area_epoch_ms}, 'storage': {'payload_bytes_per_epoch': payload_bytes_per_epoch, 'verifier_resident_anchor_bytes': final_anchor_bytes, 'direct_certificate_bytes': direct_cert_bytes, 'aggregate_checkpoint_bytes': checkpoint_bytes, 'proof_bytes': summarise(proof_bytes)}, 'latency': {'root_recompute_ns': summarise(root_recompute_ns), 'append_or_root_update_ns': summarise(append_update_ns), 'checkpoint_sign_ns': summarise(checkpoint_sign_ns), 'verify_anchor_ns': summarise(verify_anchor_ns), 'time_to_prune_from_arrival_ms': summarise(time_to_prune_from_arrival_ms), 'time_to_prune_from_finality_ms': summarise(time_to_prune_from_finality_ms), 'pending_duration_ms': summarise(pending_durations_ms)}, 'root_recomputation': {'hash_calls': root_recompute_hashes, 'canonical_bytes': root_recompute_bytes}, 'timeline': _downsample(timeline, timeline_limit), 'samples': {'verify_anchor_ns': verify_anchor_ns, 'time_to_prune_from_finality_ms': time_to_prune_from_finality_ms, 'pending_duration_ms': pending_durations_ms}}
    safe_policy = policy == 'safe'
    result['checks'] = {'safe_policy_zero_gate_violations': not safe_policy or safety_violations == 0, 'unsafe_counterexample_exhibits_violation': safe_policy or safety_violations > 0, 'all_pruned_epochs_have_nonnegative_latency': all((value >= 0 for value in time_to_prune_from_arrival_ms)), 'retained_count_consistent': pending_epochs == retained_count, 'fault_rejection_observed_when_applicable': fault in {'none', 'not_final', 'local_root_mismatch', 'missing_certificate'} or fault_rejections > 0}
    result['checks']['all_internal_checks_pass'] = all(result['checks'].values())
    return result
