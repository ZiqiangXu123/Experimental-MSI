# Publication artefacts recovered on 17 November 2026

[Download the release](https://github.com/ZiqiangXu123/Experimental-MSI/releases/tag/publication-artifacts-2026-11-17). This directory indexes the June HPC publication runs without replacing them with later remeasurements.

- `original-runs.json`: archive names, SHA-256 values, run roots, counts and experiment mapping.
- `README-ORIGINAL-RUNS.txt`: version and packaging details, including E2 and E7 provenance.
- `original-code-manifest.json`: every member of `original-code-20260625.zip` and its source.
- `inputs/e6/`: measured cost catalogues and all five upstream inputs, with relative paths and original hashes.
- `tools/verify_e6_catalogue.py`: offline checks using the Python standard library.
- `SHA256SUMS-release.txt`: checksums for every release attachment except the checksum file itself.

The seven run archives are E1, E2, E3, E4, E5, E6/E9-final and E7/E8-final. The earlier E6-only snapshot is superseded by the final combined archive. E5 and E6 archives omit some regenerable working datasets; the guide specifies this. Recorded inputs, raw measurements and summary checks must not be described as a new full workload rerun.

The original archives preserve historical local paths and their checksums. The small E6 bundle maps those historical identifiers to local relative files. No original numeric values were changed during publication preparation. Energy fields remain empty where no measurement was available.

For a separate corrected implementation, use `../checkpoint-correction-2026-11-17/`. Its tests address checkpoint binding, not new performance measurements.
