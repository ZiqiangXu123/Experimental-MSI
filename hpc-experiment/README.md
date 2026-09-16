# Unified MSI HPC Experiment Suite

This artefact integrates the seven uploaded La Trobe HPC experiment packages into one runnable experiment suite. It keeps the executable source, deterministic configurations, Slurm launch files, tests, Apptainer definitions, and the E6 component-cost template. Old separate readmes, Chinese notes, reference-output folders, stale manifests, and pre-built zipapps have been removed.

## Experiment scope

| Component | Folder | Purpose |
| --- | --- | --- |
| E1 | `experiments/e1_msi_storage` | Measures verifier-resident MSI storage and auxiliary-state scaling. |
| E2 | `experiments/e2_local_query` | Measures local verified-query computation without network, disk, queueing, or availability effects. |
| E3 | `experiments/e3_communication_network` | Measures communication cost and edge-network sensitivity using portable or dual-node execution. |
| E4 | `experiments/e4_concurrent_scaling` | Measures concurrent multi-shard throughput, long-history lookup, fairness, saturation, and service headroom. |
| E5 | `experiments/e5_anchor_pruning` | Measures certified root anchoring and validates safe commit-and-prune behaviour. |
| E6 | `experiments/e6_refolding_policy` | Measures root-preserving state refolding and resource-aware mode allocation. |
| E7 | `experiments/e7_integrity_availability` | Measures adversarial integrity and availability under withholding, outage, deadline, and retry settings. |

## Included content

The suite contains these required runtime assets:

| Path | Content |
| --- | --- |
| `experiments/*/src` | Python source for each component. |
| `experiments/*/configs` | Smoke, reference, and publication configuration files. |
| `experiments/*/slurm` | Slurm job scripts for HPC execution. |
| `experiments/*/scripts` | Component-level build, preflight, smoke, publication, recovery, and archive scripts. |
| `experiments/*/tests` | Unit tests for deterministic core behaviour. |
| `experiments/*/apptainer` | Optional rootless container definitions. |
| `experiments/e6_refolding_policy/costs` | Template input for availability-derived component costs. |
| `scripts` | Unified top-level wrappers for building, testing, local smoke runs, preflight, and Slurm submission. |

## Requirements

Use a Linux login node or compute node with Python 3.10 or newer. E1 can run on Python 3.9, but Python 3.10 or newer is the simplest common setting for the full suite. The Python standard library is sufficient; no pip, Conda, compiler, database, or administrator installation is required. E2 to E7 require system OpenSSL/libcrypto with Ed25519 support. Slurm is required for HPC submission. Apptainer or Singularity is optional and only needed when the cluster Python or libcrypto is unsuitable.

## First deployment

Unpack the suite and enter the root directory:

```bash
unzip unified_latrobe_hpc_experiment.zip
cd unified_latrobe_hpc_experiment
```

Create a site configuration file when the cluster requires an account, partition, QoS, reservation, a particular Python module, or an uploaded container:

```bash
cp scripts/site_env.example.sh scripts/site_env.sh
```

Edit `scripts/site_env.sh` and set only values confirmed by the cluster allocation. Empty values preserve site defaults.

Build the component zipapps from the cleaned source:

```bash
bash scripts/rebuild_zipapps.sh
```

Run the unit tests:

```bash
bash scripts/run_all_tests.sh
```

Run preflight checks before Slurm submission:

```bash
bash scripts/preflight_all.sh
```

## Local smoke execution

Run one local smoke test from the root directory:

```bash
bash scripts/run_component_local_smoke.sh e1
```

Replace `e1` with `e2`, `e3`, `e4`, `e5`, `e6`, or `e7` to run another component. To run every local smoke workflow sequentially:

```bash
bash scripts/run_all_local_smoke.sh
```

Local smoke results validate the artefact and execution pipeline only. They are not publication measurements.

## Slurm smoke execution

Submit one component smoke run:

```bash
bash scripts/submit_component_smoke.sh e1
```

The run directory is written under `runs/<component>` unless `RUN_BASE` is set in `scripts/site_env.sh`. Component scripts also write their most recent run path to `LAST_RUN_DIR` inside the component folder.

## Publication execution

Submit the portable publication workflow for one component:

```bash
bash scripts/submit_component_publication.sh e1
```

For E3 and E4, use the preferred dual-node workflow only when the allocation supports two-node jobs:

```bash
bash scripts/submit_component_publication.sh e3 dual
bash scripts/submit_component_publication.sh e4 dual
```

E6 policy publication requires a measured component-cost catalogue. Set `E6_COMPONENT_COST_CATALOG` in `scripts/site_env.sh` or pass the catalogue path to the component-level E6 policy script after building the E1, E2, E3, and availability-derived inputs.

## Component-level commands

The unified scripts call the original component workflows. You can still work inside a component folder when you need fine control:

```bash
cd experiments/e5_anchor_pruning
bash scripts/preflight.sh
bash scripts/run_local_smoke.sh
bash scripts/submit_smoke.sh
bash scripts/submit_publication.sh
```

Every component follows the same pattern: build the zipapp, run preflight, run smoke, submit publication jobs, inspect missing array indices when needed, analyse the raw outputs, validate the results, and archive the run.

## Output layout

A completed run normally contains `plan.jsonl`, `raw` or stage-specific raw folders, `results`, `logs`, and SHA-256 result manifests. The exact raw folder names differ by component because E6 and E7 split migration, policy, integrity, and availability outputs.

## Rebuilding after edits

After changing any source file, rebuild zipapps and rerun tests before submitting jobs:

```bash
bash scripts/rebuild_zipapps.sh
bash scripts/run_all_tests.sh
```

## Notes on cleaned content

This integrated suite removes all previous Chinese documentation and separate readmes. Source comments and Python docstrings have been stripped from the retained executable files. Slurm `#SBATCH` lines and shebangs remain because they are execution directives rather than explanatory comments.
