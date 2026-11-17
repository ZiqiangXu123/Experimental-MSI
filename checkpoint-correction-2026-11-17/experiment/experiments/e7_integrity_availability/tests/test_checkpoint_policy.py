from __future__ import annotations

import copy
import hashlib
import unittest
from dataclasses import replace

from msi_security_avail_exp.accumulator import build_accumulator, extract_accumulator_path
from msi_security_avail_exp.crypto import Ed25519OpenSSL
from msi_security_avail_exp.fixture import build_fixture, honest_frame
from msi_security_avail_exp.models import AggregateCertificate, DirectCertificate, Response
from msi_security_avail_exp.pipeline import checkpoint_statement, verify_frame
from msi_security_avail_exp.protocol import decode_response, encode_response


class CheckpointPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = build_fixture({'epoch_length': 8, 'block_bytes': 128,
                                      'mode': 'full', 'anchor_mode': 'aggregate',
                                      'anchor_prefix': 4, 'anchor_position': 1,
                                      'query_position': 1, 'fixture_seed': 73})
        self.backend = Ed25519OpenSSL()
        self.query = (self.fixture.shard, self.fixture.epoch, 1)
        self.parsed = decode_response(honest_frame(self.fixture, 1))
        self.current = self.fixture.certificate
        assert isinstance(self.current, AggregateCertificate)

    def frame(self, certificate):
        p = self.parsed
        return encode_response(Response(p.q, p.root, p.meta, p.payload, p.witness, certificate))

    def assert_rejected_without_state_change(self, certificate) -> None:
        before = copy.deepcopy(self.fixture)
        decision = verify_frame(self.frame(certificate), self.query, self.fixture,
                                openssl=self.backend)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.stage, 'VerifyAnchor')
        # The E7 verifier is read-only: a failed response cannot install a checkpoint.
        self.assertEqual(self.fixture, before)

    def older_valid_certificate(self):
        accumulator = build_accumulator(self.fixture.accumulator_statements[:2])
        return replace(self.current, k_star=2, accumulator_root=accumulator.root,
                       checkpoint_signature=self.backend.sign(
                           self.fixture.private_seed,
                           checkpoint_statement(self.fixture.shard, 2, accumulator.root)),
                       inclusion_path=extract_accumulator_path(accumulator.levels, 1))

    def test_current_valid_checkpoint_accepts(self) -> None:
        decision = verify_frame(self.frame(self.current), self.query, self.fixture,
                                openssl=self.backend)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.block, self.fixture.blocks[1])

    def test_authentic_old_covering_checkpoint_rejected(self) -> None:
        old = self.older_valid_certificate()
        self.assertGreaterEqual(old.k_star, self.fixture.entry.meta.k)
        self.assertTrue(self.backend.verify(old.public_key,
                                           checkpoint_statement(old.shard, old.k_star,
                                                                old.accumulator_root),
                                           old.checkpoint_signature))
        # This is an authentic old checkpoint and still covers the queried statement.
        old_state = copy.deepcopy(self.fixture)
        old_state.certificate = old
        self.assertTrue(verify_frame(self.frame(old), self.query, old_state,
                                     openssl=self.backend).accepted)
        self.assert_rejected_without_state_change(old)

    def test_noncovering_prefix_rejected(self) -> None:
        self.fixture = build_fixture({'epoch_length': 8, 'block_bytes': 128,
                                      'mode': 'full', 'anchor_mode': 'aggregate',
                                      'anchor_prefix': 4, 'anchor_position': 3,
                                      'query_position': 1, 'fixture_seed': 73})
        self.parsed = decode_response(honest_frame(self.fixture, 1))
        self.current = self.fixture.certificate
        accumulator = build_accumulator(self.fixture.accumulator_statements[:2])
        certificate = replace(self.current, k_star=2, accumulator_root=accumulator.root,
                              checkpoint_signature=self.backend.sign(self.fixture.private_seed,
                                  checkpoint_statement(self.fixture.shard, 2, accumulator.root)),
                              inclusion_path=extract_accumulator_path(accumulator.levels, 1))
        self.assertTrue(self.backend.verify(certificate.public_key,
            checkpoint_statement(certificate.shard, 2, certificate.accumulator_root),
            certificate.checkpoint_signature))
        self.assert_rejected_without_state_change(certificate)

    def test_honest_modes_accept(self) -> None:
        for mode in ('full', 'leaf', 'ext'):
            for anchor_mode in ('direct', 'aggregate'):
                with self.subTest(mode=mode, anchor_mode=anchor_mode):
                    fixture = build_fixture({'epoch_length': 8, 'block_bytes': 128,
                                             'mode': mode, 'anchor_mode': anchor_mode,
                                             'anchor_prefix': 4, 'anchor_position': 1,
                                             'query_position': 1, 'fixture_seed': 73})
                    self.assertTrue(verify_frame(honest_frame(fixture, 1),
                        (fixture.shard, fixture.epoch, 1), fixture,
                        openssl=self.backend).accepted)

    def test_wrong_position_rejected(self) -> None:
        self.assert_rejected_without_state_change(replace(self.current, statement_position=2))

    def test_wrong_statement_rejected(self) -> None:
        self.assert_rejected_without_state_change(replace(self.current, statement=self.current.statement[:-1] + bytes([self.current.statement[-1] ^ 1])))

    def test_signed_alternative_root_at_current_prefix_rejected(self) -> None:
        statements = list(self.fixture.accumulator_statements)
        statements[-1] = statements[-1][:-1] + bytes([statements[-1][-1] ^ 1])
        other = build_accumulator(statements)
        certificate = replace(self.current, accumulator_root=other.root,
                              checkpoint_signature=self.backend.sign(self.fixture.private_seed,
                                  checkpoint_statement(self.fixture.shard, 4, other.root)),
                              inclusion_path=extract_accumulator_path(other.levels, 1))
        self.assert_rejected_without_state_change(certificate)

    def test_wrong_key_even_with_valid_signature_rejected(self) -> None:
        seed = hashlib.sha256(b'other authorised-looking but untrusted key').digest()
        certificate = replace(self.current, public_key=self.backend.public_from_seed(seed),
                              checkpoint_signature=self.backend.sign(seed,
                                  checkpoint_statement(self.current.shard, self.current.k_star,
                                                       self.current.accumulator_root)))
        self.assert_rejected_without_state_change(certificate)

    def test_invalid_signature_rejected(self) -> None:
        signature = bytes([self.current.checkpoint_signature[0] ^ 1]) + self.current.checkpoint_signature[1:]
        self.assert_rejected_without_state_change(replace(self.current, checkpoint_signature=signature))

    def test_current_checkpoint_cannot_be_bypassed_by_direct_certificate(self) -> None:
        direct = DirectCertificate(public_key=self.fixture.public_key,
                                   statement=self.current.statement,
                                   signature=self.backend.sign(self.fixture.private_seed,
                                                               self.current.statement))
        self.assert_rejected_without_state_change(direct)


if __name__ == '__main__':
    unittest.main()
