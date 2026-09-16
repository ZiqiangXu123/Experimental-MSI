from __future__ import annotations
import bisect, math, random
from dataclasses import dataclass
from .index import DatasetSpec, hot_indices, index_to_key

class TruncatedZipf:

    def __init__(self, size: int, alpha: float):
        w = [1 / (i + 1) ** alpha for i in range(size)]
        total = sum(w)
        a = 0
        self.cdf = []
        for x in w:
            a += x / total
            self.cdf.append(a)
        self.cdf[-1] = 1

    def sample(self, r):
        return bisect.bisect_left(self.cdf, r.random())

@dataclass
class QuerySampler:
    spec: DatasetSpec
    distribution: str
    seed: int
    leaf_only: bool = False
    burst_period_s: float = 1.0

    def __post_init__(self):
        self.r = random.Random(self.seed)
        self.perm = list(range(self.spec.shard_count))
        self.r.shuffle(self.perm)
        self.hot = hot_indices(self.spec)
        self.zs08 = TruncatedZipf(self.spec.shard_count, 0.8)
        self.zs12 = TruncatedZipf(self.spec.shard_count, 1.2)
        self.ze08 = TruncatedZipf(self.spec.historical_epochs, 0.8)
        self.ze12 = TruncatedZipf(self.spec.historical_epochs, 1.2)
        self.zh08 = TruncatedZipf(len(self.hot), 0.8)
        self.zh12 = TruncatedZipf(len(self.hot), 1.2)

    def sample(self, elapsed: float):
        if self.leaf_only:
            return self._hot(elapsed)
        d = self.distribution
        if d == 'uniform':
            return (self.r.randrange(self.spec.shard_count), self.r.randrange(self.spec.historical_epochs))
        if d == 'zipf0.8':
            return self._zipf(self.zs08, self.ze08)
        if d == 'zipf1.2':
            return self._zipf(self.zs12, self.ze12)
        if d == 'recency':
            s = self.r.randrange(self.spec.shard_count)
            half = max(1, self.spec.historical_epochs / 100)
            p = 1 - math.exp(math.log(0.5) / half)
            u = max(1e-15, self.r.random())
            age = min(self.spec.historical_epochs - 1, int(math.log(1 - u) / math.log(1 - p)))
            return (s, self.spec.historical_epochs - 1 - age)
        if d == 'bursty':
            phase = int(elapsed / max(self.burst_period_s, 1e-06))
            c = max(1, self.spec.shard_count // 8)
            start = phase * c % self.spec.shard_count
            hot = [self.perm[(start + i) % self.spec.shard_count] for i in range(c)]
            if self.r.random() < 0.9:
                return (hot[self.r.randrange(len(hot))], self.spec.historical_epochs - 1 - self.r.randrange(min(self.spec.historical_epochs, 1000)))
            return (self.r.randrange(self.spec.shard_count), self.r.randrange(self.spec.historical_epochs))
        raise ValueError(d)

    def _zipf(self, zs, ze):
        return (self.perm[zs.sample(self.r)], self.spec.historical_epochs - 1 - ze.sample(self.r))

    def _hot(self, elapsed):
        d = self.distribution
        if d == 'uniform' or len(self.hot) == 1:
            idx = self.hot[self.r.randrange(len(self.hot))]
        elif d == 'zipf0.8':
            idx = self.hot[len(self.hot) - 1 - self.zh08.sample(self.r)]
        elif d == 'zipf1.2':
            idx = self.hot[len(self.hot) - 1 - self.zh12.sample(self.r)]
        elif d == 'recency':
            idx = sorted(self.hot, reverse=True)[min(len(self.hot) - 1, int(-math.log(max(1e-15, self.r.random())) * max(1, len(self.hot) / 20)))]
        elif d == 'bursty':
            phase = int(elapsed / max(self.burst_period_s, 1e-06))
            mod = max(1, self.spec.shard_count // 4)
            c = [x for x in self.hot if index_to_key(self.spec, x)[0] % mod == phase % mod]
            idx = (c or self.hot)[self.r.randrange(len(c or self.hot))]
        else:
            raise ValueError(d)
        return index_to_key(self.spec, idx)
