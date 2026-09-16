from __future__ import annotations
import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any
from .constants import DEFAULT_MSS_BYTES, NETWORK_PROFILES

@dataclass(frozen=True)
class LinkProfile:
    profile_id: str
    name: str
    rtt_ms: float
    bandwidth_mbps: float
    loss_rate: float
    mss_bytes: int = DEFAULT_MSS_BYTES
    rto_floor_ms: float = 10.0
    rto_multiplier: float = 2.0

    @classmethod
    def from_id(cls, profile_id: str, overrides: dict[str, Any] | None=None) -> 'LinkProfile':
        if profile_id not in NETWORK_PROFILES:
            raise ValueError(f'unknown network profile {profile_id}')
        values = dict(NETWORK_PROFILES[profile_id])
        if overrides:
            values.update(overrides)
        profile = cls(profile_id=profile_id, **values)
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.rtt_ms < 0:
            raise ValueError('RTT must be non-negative')
        if self.bandwidth_mbps <= 0:
            raise ValueError('bandwidth must be positive')
        if not 0.0 <= self.loss_rate < 1.0:
            raise ValueError('loss_rate must be in [0,1)')
        if self.mss_bytes < 64:
            raise ValueError('mss_bytes is implausibly small')

@dataclass(frozen=True)
class LinkSample:
    application_bytes: int
    original_packets: int
    retransmitted_packets: int
    retransmission_rounds: int
    virtual_wire_bytes: int
    configured_rtt_ns: int
    physical_baseline_rtt_ns: int
    injected_rtt_ns: int
    propagation_delay_ns: int
    serialization_delay_ns: int
    retransmission_wait_ns: int
    total_delay_ns: int
    target_deadline_ns: int | None = None
    actual_delay_ns: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {'application_bytes': self.application_bytes, 'original_packets': self.original_packets, 'retransmitted_packets': self.retransmitted_packets, 'retransmission_rounds': self.retransmission_rounds, 'virtual_wire_bytes': self.virtual_wire_bytes, 'configured_rtt_ns': self.configured_rtt_ns, 'physical_baseline_rtt_ns': self.physical_baseline_rtt_ns, 'injected_rtt_ns': self.injected_rtt_ns, 'propagation_delay_ns': self.propagation_delay_ns, 'serialization_delay_ns': self.serialization_delay_ns, 'retransmission_wait_ns': self.retransmission_wait_ns, 'total_delay_ns': self.total_delay_ns, 'target_deadline_ns': self.target_deadline_ns, 'actual_delay_ns': self.actual_delay_ns}

def _uniform01(*parts: object) -> float:
    h = hashlib.sha256()
    for part in parts:
        raw = str(part).encode('utf-8')
        h.update(len(raw).to_bytes(4, 'big'))
        h.update(raw)
    value = int.from_bytes(h.digest()[:8], 'big')
    return (value + 0.5) / float(1 << 64)

def _loss_count(packets: int, loss_rate: float, context: tuple[object, ...], round_index: int) -> int:
    if packets <= 0 or loss_rate <= 0.0:
        return 0
    lost = 0
    for packet_index in range(packets):
        if _uniform01(*context, round_index, packet_index) < loss_rate:
            lost += 1
    return lost

def sample_transfer(profile: LinkProfile, application_bytes: int, *, baseline_rtt_ns: int, seed: int, endpoint: str, direction: str, request_id: int, transaction_index: int) -> LinkSample:
    if application_bytes < 0:
        raise ValueError('application_bytes must be non-negative')
    configured_rtt_ns = int(round(profile.rtt_ms * 1000000.0))
    baseline_rtt_ns = max(0, int(baseline_rtt_ns))
    injected_rtt_ns = max(0, configured_rtt_ns - baseline_rtt_ns)
    propagation_delay_ns = injected_rtt_ns // 2
    original_packets = max(1, math.ceil(application_bytes / profile.mss_bytes))
    context = (seed, profile.profile_id, endpoint, direction, request_id, transaction_index)
    outstanding = _loss_count(original_packets, profile.loss_rate, context, 0)
    retransmitted = 0
    rounds = 0
    while outstanding:
        rounds += 1
        retransmitted += outstanding
        if rounds > 64:
            raise RuntimeError('virtual loss process did not converge')
        outstanding = _loss_count(outstanding, profile.loss_rate, context, rounds)
    virtual_packets = original_packets + retransmitted
    retransmitted_bytes = retransmitted * profile.mss_bytes
    virtual_wire_bytes = application_bytes + retransmitted_bytes
    serialization_delay_ns = int(round(virtual_wire_bytes * 8.0 / (profile.bandwidth_mbps * 1000000.0) * 1000000000.0))
    rto_ms = max(profile.rto_floor_ms, profile.rto_multiplier * profile.rtt_ms)
    retransmission_wait_ns = int(round(rounds * rto_ms * 1000000.0))
    total = propagation_delay_ns + serialization_delay_ns + retransmission_wait_ns
    return LinkSample(application_bytes=application_bytes, original_packets=original_packets, retransmitted_packets=retransmitted, retransmission_rounds=rounds, virtual_wire_bytes=virtual_wire_bytes, configured_rtt_ns=configured_rtt_ns, physical_baseline_rtt_ns=baseline_rtt_ns, injected_rtt_ns=injected_rtt_ns, propagation_delay_ns=propagation_delay_ns, serialization_delay_ns=serialization_delay_ns, retransmission_wait_ns=retransmission_wait_ns, total_delay_ns=total)

def precise_delay_ns(delay_ns: int) -> int:
    delay_ns = max(0, int(delay_ns))
    if delay_ns == 0:
        return 0
    start = time.perf_counter_ns()
    target = start + delay_ns
    while True:
        now = time.perf_counter_ns()
        remaining = target - now
        if remaining <= 0:
            break
        if remaining > 2000000:
            time.sleep((remaining - 500000) / 1000000000.0)
        elif remaining > 250000:
            time.sleep((remaining - 100000) / 1000000000.0)
        else:
            pass
    return time.perf_counter_ns() - start

def apply_transfer_delay(sample: LinkSample) -> LinkSample:
    actual = precise_delay_ns(sample.total_delay_ns)
    return LinkSample(**{**sample.__dict__, 'actual_delay_ns': actual})

def no_loss_base_transfer_ns(profile: LinkProfile, application_bytes: int, baseline_rtt_ns: int) -> int:
    configured = int(round(profile.rtt_ms * 1000000.0))
    baseline = max(0, int(baseline_rtt_ns))
    injected = max(0, configured - baseline) // 2
    serialization = int(round(application_bytes * 8.0 / (profile.bandwidth_mbps * 1000000.0) * 1000000000.0))
    return injected + serialization
