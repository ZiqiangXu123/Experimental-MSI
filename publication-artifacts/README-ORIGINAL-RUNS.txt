ORIGINAL PUBLICATION-RUN ARCHIVES

These seven GitHub Release assets preserve the original tar.gz bytes recovered
from experiment.zip. The Release attachment SHA256SUMS.txt
checks those bytes. The original-runs.json index gives the exact filenames,
checksums, original run directories, source directories and key counts.

This is an archive recovery and publication step, not a new benchmark run.
Current source corrections must be published separately from these historical
results. Keep both versions identifiable; do not overwrite the old results with
new outputs or relabel them as results from the corrected implementation.

CONTENTS AND PAPER MAPPING

E1: 80 storage-accounting points.
E2: 290 planned blocks, 1330 process-launch cases, 133 configurations and
    13300000 measured queries.
E3: 624 cases: 64 wire-size audits and 560 network launches; 112000 queries.
E4: 400 cases and 2239301 accepted measured queries. H4a and H4c failed their
    stated thresholds; those negative results remain in the archive and paper.
E5: 552 cases = 120 anchor-query + 200 checkpoint-update + 230 safe-pruning
    + 2 unsafe-counterexample cases. The unsafe counterexamples are separate
    from the zero-violation safe-policy result.
E6: 1410 migration cases, including 1176 performance, 96 equivalence-audit and
    138 fault-injection cases. Its combined E6-E9-final archive also contains
    590 policy cases, 19120 policy observations and the measured 12-row
    component catalogue. The code's E9 label maps to the paper's mode-assignment
    study. The older E6-only archive is not duplicated.
E7/E8: 247 integrity cases (200 deterministic, 32 fuzz, 15 unsafe ablation);
    1164000 primary attack-response and fuzz inputs, with the 15000 unsafe
    ablation inputs counted separately. E8 contains 7200 availability cases
    and 72000000 virtual-time queries.

AUTHORITATIVE E2 SNAPSHOT

Use e2-publication-20260625-001128.tar.gz. Its plan has 290 E2 blocks and
1330 unique cases, all matched to retained E2 raw records. Its plan SHA-256 is
881ce0fed1ea884cc8c3617f685ec47b84c434f119e7ecedd45ccdacbd0c0abf,
which agrees with results/summary.json.
The expanded folder in the uploaded bundle later had its plan replaced by
four E3 smoke blocks and gained 16 E3 raw files. Those later files are not part
of this E2 publication archive and should be kept as separate ancillary tests.

E7 VERSION CHAIN

The June run's software_and_plan.sha256 names the before-fix pyz. The provenance
directory preserves that file, the June patched/final pyz, their checksums, the
original affected blocks, and notes on rerunning blocks 65 and 69. The June
repair replaced compressed-payload mutations that could decode to unchanged
payloads. It is not the later active-checkpoint-binding correction.
Use provenance/SHA256SUMS and FINAL_RUN_NOTES.txt to follow this history. Do not
apply the old top-level software checksum to the final June pyz without this
explanation. The separately published historical code asset retains the final
June program files supplied with the bundle; the earlier E7 pyz remains in the
run archive's provenance directory.

BOUNDARIES OF THESE MATERIALS

The archives preserve original reports, including their historical terminology.
The manuscript now distinguishes invalid evidence, non-canonical data and benign
fuzz mutations, and explains that no_anchor is a combined ablation. Use those
current definitions when interpreting the old counts. A zero observed error in
this corpus is not a new formal security guarantee.

The E5 archive omits 22 generated accumulator-dataset files present in the
expanded upload. The E6-E9-final archive omits 2284 generated dataset files from
the expanded upload. These original snapshots retain their run records and
results; dataset generation is needed for a complete persisted-store replay.
E4's archive does include its generated index datasets. This release does not
claim that every archive contains a complete persisted store.

No E2 resource group has publication-valid energy measurements. Controlled
network and virtual-time availability tests are not physical radio-outage tests.
Process-crash recovery is not sudden-power-loss testing. Native sharded-chain
integration, physical energy, radio-outage and power-loss results are not added
by recovering these archives.

FILES INTENTIONALLY NOT DUPLICATED

Python virtual environments, caches, expanded copies of the same run archives,
sample/reference outputs and the earlier E6-only archive are not added as new
release assets. They are not needed to retain the chosen original run snapshots.
Original provenance and logs already inside a selected tar remain unchanged.
