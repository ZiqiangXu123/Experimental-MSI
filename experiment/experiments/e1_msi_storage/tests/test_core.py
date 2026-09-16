from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from msi_storage_exp.benchmark import aux_model_per_epoch, raw_payload_model_per_epoch
from msi_storage_exp.constants import DIGEST_BYTES, MSI_ENTRY_BYTES
from msi_storage_exp.encoding import build_merkle, canonical_block, encode_msi_entry, leaf_hash, object_ref
from msi_storage_exp.util import next_power_of_two

class CoreTests(unittest.TestCase):

    def test_next_power_of_two(self) -> None:
        expected = {1: 1, 2: 2, 3: 4, 4: 4, 5: 8, 31: 32, 32: 32, 33: 64}
        for n, n_prime in expected.items():
            self.assertEqual(next_power_of_two(n), n_prime)

    def test_canonical_block_length(self) -> None:
        for size in (34, 128, 1024):
            block = canonical_block(shard=1, epoch=2, position=3, block_bytes=size, seed=7)
            self.assertEqual(len(block), size)

    def test_merkle_node_counts(self) -> None:
        for n in (1, 3, 4, 5, 33):
            blocks = [canonical_block(shard=1, epoch=1, position=i, block_bytes=128, seed=11) for i in range(1, n + 1)]
            leaves = [leaf_hash(1, 1, i, b) for i, b in enumerate(blocks, start=1)]
            material = build_merkle(leaves)
            self.assertEqual(len(material.padded_leaves), next_power_of_two(n))
            self.assertEqual(len(material.non_root_nodes), 2 * next_power_of_two(n) - 2)

    def test_msi_fixed_width(self) -> None:
        root = bytes(DIGEST_BYTES)
        record = encode_msi_entry(epoch_position=1, n=5, root=root, payload_ref=object_ref('payload', 1, 1, 'raw'), aux_ref=object_ref('aux', 1, 1, 'full'), layout='epoch_packed', mode='full', codec='raw-v1')
        self.assertEqual(len(record), MSI_ENTRY_BYTES)

    def test_aux_formulas(self) -> None:
        for n in (1, 3, 4, 5, 33):
            np = next_power_of_two(n)
            _, _, _, full_hash = aux_model_per_epoch(n, 'full')
            _, _, _, leaf_hash_bytes = aux_model_per_epoch(n, 'leaf')
            self.assertEqual(full_hash, (2 * np - 2) * DIGEST_BYTES)
            self.assertEqual(leaf_hash_bytes, np * DIGEST_BYTES)

    def test_payload_layout_object_counts(self) -> None:
        _, _, per_block_objects = raw_payload_model_per_epoch(5, 128, 'per_block')
        _, _, packed_objects = raw_payload_model_per_epoch(5, 128, 'epoch_packed')
        self.assertEqual(per_block_objects, 5)
        self.assertEqual(packed_objects, 1)
if __name__ == '__main__':
    unittest.main(verbosity=2)
