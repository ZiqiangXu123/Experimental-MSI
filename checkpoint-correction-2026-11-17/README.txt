Checkpoint correction — public release package, 17 November 2026

This independent package contains the corrected E5 and E7 source, tests,
configurations, source patch, rebuilt zipapps and regression records. Its base is
public commit 4789284b0c40d524c397a3093ca5747e932cb36a:
https://github.com/ZiqiangXu123/Experimental-MSI/commit/4789284b0c40d524c397a3093ca5747e932cb36a

It is a correction of that public version, not the June 2026 publication-run
version. The original June experiments are preserved separately. In particular,
the June E7 compressed-payload mutation patch has not been merged into this
correction. Do not substitute these binaries for the binaries that produced the
original results. public_source_provenance.json identifies the inspected base
blobs; changed_files.json and changes.patch describe the scientific code changes.

Changes and scope
-----------------
E5 separates authenticated checkpoint installation from read-only evidence
validation. E7 binds the certificate mode, prefix, accumulator root and authority
to protected state. The legacy no_anchor option remains an alias for jointly
removing protected-root binding and anchor verification. Output schema 2 separates
deterministic expected-reject counts, non-canonical block/root accepts,
metadata/policy counts and all fuzz inputs. Unavailable historical categories
remain unknown. A mixed-oracle zero-event probability bound is not reported.

This package does not modify E6 target/registry directory synchronisation. It
does not establish power-loss safety, native blockchain finality, key rotation,
physical radio-outage behaviour or a new isolated anchor-only ablation result.

The original 1,164,000-input integrity corpus, 552 E5 cases, benchmark timings
and coverage counts have not been rerun using this correction. They remain
original reported results. The publication configurations are included for plan
validation; the release checks do not run those full workloads.

Reproduction
------------
Use Python 3.11 or later and a system OpenSSL libcrypto with Ed25519 support.
The tests use real Ed25519; cryptographic verification is not mocked.
From this package root:

  python3 check_release.py

This rebuilds the two zipapps from the included source, checks each zipapp's
module bytes against its source, runs E5 and E7 tests and both CLI help commands,
and writes fresh records under release_checks/. Expected test counts are E5 20
and E7 19. The included tests/run_tests.sh wrappers can also run either suite;
CHECKPOINT_PYTHON optionally selects the Python executable.

On macOS, configure DYLD_LIBRARY_PATH to an OpenSSL 3 library directory before
launching Python if normal discovery selects Apple's non-functional libcrypto
stub. That directory must contain the libcrypto.dylib lookup name. On Linux, the
normal system libcrypto discovery is usually sufficient. No bundled machine
library, virtual environment or cache is included.

Rebuilt executables:
  experiment/experiments/e5_anchor_pruning/exp05_anchor.pyz
  experiment/experiments/e7_integrity_availability/exp07_security_availability.pyz

  python3 experiment/experiments/e5_anchor_pruning/exp05_anchor.pyz --help
  python3 experiment/experiments/e7_integrity_availability/exp07_security_availability.pyz --help

Historical and current evidence
-------------------------------
evidence/e5 and evidence/e7 contain records of the earlier correction checks,
including the original-versus-corrected checkpoint comparison. Those historical
records are not new release runs. release_checks/ contains the fresh release
validation. Both are focused correctness checks, not performance measurements.

Machine-specific paths in historical evidence are replaced with named
placeholders. Historical scripts containing those placeholders document the old
invocation and are not portable launchers. evidence/redaction_record.json records
the original and public-copy hashes. Embedded historical checksums describe the
pre-redaction records. The root SHA256SUMS verifies the files distributed in this
public package; it excludes itself. evidence/historical_package_SHA256.json is a
retained historical manifest, not a current package verifier.

The Python scientific source, Python tests, configurations and changes.patch are
copied unchanged from the previously checked correction. Packaging changes are
limited to this README, portable shell test launchers, check_release.py, path
redactions in historical records, rebuilt zipapps and release manifests. No June
experiment or original benchmark output has been overwritten.

The patch applies to the pinned repository with git apply changes.patch. This
package is also self-contained for the focused tests above. A repository-level
licence, if supplied by the authors, governs reuse; this packaging step does not
choose or add a new licence.

Verify SHA256SUMS on the downloaded package before rerunning checks. A new run
rewrites release_checks/ and rebuilds zipapps, so its output is a new local check
rather than the unchanged downloaded release.
