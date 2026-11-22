# MSI experimental implementation and publication artefacts

Research artefacts for *A Merkle-Assisted Tiered Secure Storage Framework for Sharding Blockchain*.

The [17 November 2026 release](https://github.com/ZiqiangXu123/Experimental-MSI/releases/tag/publication-artifacts-2026-11-17) provides the recovered June 2026 publication-run archives, their original code, the measured E6 cost inputs, and a separate checkpoint-binding correction. The manuscript cites [commit `d9fa46d3fc49`](https://github.com/ZiqiangXu123/Experimental-MSI/tree/d9fa46d3fc49a23283c77d041460924c88d62b5b) for this release.

## Start here

| Material | Location |
| --- | --- |
| Original E1–E8 publication runs and original code | [Release downloads](https://github.com/ZiqiangXu123/Experimental-MSI/releases/tag/publication-artifacts-2026-11-17) and [run index](publication-artifacts/original-runs.json) |
| Experiment-to-paper mapping, recovery notes and archive checksums | [Original-run guide](publication-artifacts/README-ORIGINAL-RUNS.txt) |
| E6 measured component and migration costs, all five upstream inputs | [E6 input bundle](publication-artifacts/inputs/e6/README.txt) |
| November checkpoint-binding correction, tests and rebuilt zipapps | [Correction package](checkpoint-correction-2026-11-17/README.txt) |
| Raspberry Pi/HPC measurements | [Gateway suite](experiments-revise/README.md) |
| Ethereum replay inputs | The [export](experiments-revise/datasets/ethereum/manifest.json) contains 128 blocks. The gateway replay used the first 69 blocks under a cumulative 32 MiB payload limit. |
| Earlier integrated HPC source | [Baseline suite](experiment/README.md) |

## Verify the recovered data

From the repository root, using Python 3.11 or later:

```sh
python3 publication-artifacts/tools/verify_e6_catalogue.py
```

This checks the retained input hashes, rebuilds the 48 component-cost values from the five upstream CSVs, and compares the policy summary with the reported table. The E6 guide also gives the optional command for checking the full downloaded raw run. These are data and summary checks, not new timed experiments.

Download release archives into a new directory and verify them with the release's `SHA256SUMS.txt` (on macOS, `shasum -a 256 -c SHA256SUMS.txt`; on Linux, `sha256sum -c SHA256SUMS.txt`). Each original-run archive retains its historical bytes and checksum. The root names are listed in the run index.

For gateway table reconstruction, commands run inside `experiments-revise`:

```sh
cd experiments-revise
python3 tools/verify_package.py
python3 tools/reproduce_tables.py --out work/recomputed
```

## Keep the versions separate

- The June publication archives support the original measurements. The E2 archive contains the authoritative E2 plan; a later loose copy had been overwritten by an E3 smoke plan. The archived E7 provenance records its June compressed-payload mutation correction.
- `checkpoint-correction-2026-11-17/` is based on the public source at `4789284b0c40d524c397a3093ca5747e932cb36a`. It fixes current-checkpoint binding and supplies focused regression tests. The 17 November release records the focused E5 and E7 tests. The timing, coverage and ablation results in the June archives come from the original runs.
- `experiment/` and the gateway's vendored source remain historical baselines. For the checkpoint correction, use the separately named package; do not combine versions without recording the change.
- Internal policy label **E9** is the mode-assignment study within the E6 refolding-policy package, reported in manuscript Section VII-F.

The recovery verifies archived consistency and selected reported numbers. It does not establish native sharded-blockchain integration, measured energy, physical power-loss safety or radio-outage behaviour. E6 target/registry directory synchronisation remains a separate implementation issue. The original archives retain failed cases, negative results and historical environment metadata. Machine-specific paths in provenance are historical source identifiers; use the relative mappings and site templates when reproducing.
