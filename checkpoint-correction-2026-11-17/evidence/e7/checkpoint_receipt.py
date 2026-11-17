import dataclasses
import json
from msi_security_avail_exp.accumulator import build_accumulator, extract_accumulator_path
from msi_security_avail_exp.crypto import Ed25519OpenSSL
from msi_security_avail_exp.fixture import build_fixture, honest_frame
from msi_security_avail_exp.models import Response
from msi_security_avail_exp.pipeline import checkpoint_statement, verify_frame
from msi_security_avail_exp.protocol import decode_response, encode_response

fixture = build_fixture({'epoch_length': 8, 'block_bytes': 128, 'mode': 'full',
                         'anchor_mode': 'aggregate', 'anchor_prefix': 4,
                         'anchor_position': 1, 'query_position': 1, 'fixture_seed': 73})
backend = Ed25519OpenSSL()
current = fixture.certificate
old_accumulator = build_accumulator(fixture.accumulator_statements[:2])
old = dataclasses.replace(current, k_star=2, accumulator_root=old_accumulator.root,
                          checkpoint_signature=backend.sign(fixture.private_seed,
                              checkpoint_statement(fixture.shard, 2, old_accumulator.root)),
                          inclusion_path=extract_accumulator_path(old_accumulator.levels, 1))
p = decode_response(honest_frame(fixture, 1))
records = []
for label, certificate in [('current_prefix_4', current), ('old_authentic_prefix_2_still_covers_position_1', old)]:
    before = fixture.certificate
    frame = encode_response(Response(p.q, p.root, p.meta, p.payload, p.witness, certificate))
    decision = verify_frame(frame, p.q, fixture, openssl=backend)
    records.append({'case': label, 'candidate_prefix': certificate.k_star,
                    'candidate_root_hex': certificate.accumulator_root.hex(),
                    'candidate_checkpoint_signature_valid': backend.verify(certificate.public_key,
                        checkpoint_statement(certificate.shard, certificate.k_star, certificate.accumulator_root),
                        certificate.checkpoint_signature),
                    'accepted': decision.accepted, 'stage': decision.stage, 'reason': decision.reason,
                    'protected_checkpoint_unchanged': before == fixture.certificate})
print(json.dumps({'query': list(p.q), 'statement_position': fixture.entry.meta.k,
                  'protected_prefix': current.k_star, 'protected_root_hex': current.accumulator_root.hex(),
                  'crypto': backend.metadata(), 'cases': records}, indent=2))
