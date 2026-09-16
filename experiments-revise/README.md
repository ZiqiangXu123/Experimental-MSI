# MSI experimental implementation and data

This repository contains the original E1–E8 experiment implementation, Raspberry Pi 4 and HPC measurements, and scripts that reconstruct the numerical summaries from the recorded results. The framework evaluates authenticated retrieval of pruned history using full-tree, leaf-vector and external-witness representations.

## Contents

| Path | Content |
| --- | --- |
| `implementation/` | Pi suite 1.0.1, provider service, tests and Slurm wrappers |
| `implementation/vendor/experiment/` | Original E1–E8 source, configurations, tests and execution scripts |
| `source_variants/hpc_1_0_0/` | Historical HPC driver files and NFS-retention adapter |
| `analysis/` | Performance, storage/migration and platform analysis scripts |
| `tables/` | Reference CSV and JSON summaries |
| `results/` | Recorded measurements, inputs, plans and environment metadata |
| `datasets/ethereum/` | Ethereum snapshot manifest; payload restoration status below |
| `provenance/` | Cohort selection, missing inputs and source-file provenance |
| `tools/` | Package verification, HPC preparation and table reconstruction |

Ordinary source comments and docstrings have been removed. Interpreter and Slurm directives remain executable. Recorded measurements and their provenance retain their original bytes. `provenance/source_files.json` maps source files before and after cleaning; historical source hashes in run manifests therefore need not equal current source hashes.

## Requirements and result reconstruction

Use Linux, Bash and Python 3.10 or newer. The implementation and analysis use the Python standard library. Experiment execution also requires system OpenSSL/libcrypto with Ed25519 support. Slurm is required only for batch submission. Pi thermal telemetry uses `vcgencmd` when available.

From the repository root:

```bash
python3 tools/verify_package.py
python3 tools/reproduce_tables.py --out work/recomputed
```

The second command reconstructs the three analysis categories under `work/recomputed/` using the retained measurements. It does not rerun timed experiments or require the missing case/proof fixtures. `tables/` contains the corresponding reference data. Package verification checks the files listed in `SHA256SUMS.txt`; a successful check does not establish that missing inputs have been restored or that every original experiment can be replayed.

## Recorded cohorts

Paths below are relative to `results/`. Preserve these as separate runs; historical runs are not extra repetitions of the selected runs.

| Run | Role in the analysis |
| --- | --- |
| `HPC_results/hpc_real_422175` | Completed HPC run used for the primary platform comparison |
| `ethernet2/pi_a_paper_ethereum_ethernet2` | Cooled Pi Ethernet replacement used for primary Pi measurements |
| `Pi_results/pi-a-paper_ethereum_wifi` | Completed Wi-Fi run, reported with its recorded platform conditions |
| `HPC_results/hpc_real_422173` | Earlier run that failed during NFS workspace cleanup; excluded from primary performance results |
| `HPC_results/hpc_migration_check_422174` | Separate 60-case migration/recovery diagnostic; not additional performance repetitions |
| `Pi_results/pi-a-paper_ethereum_ethernet` | Superseded hot Ethernet run, retained for provenance |
| `Pi_results/pi-a-paper_synthetic_ethernet` | Earlier synthetic-only Ethernet run, retained for provenance |

`provenance/cohorts.json` records the selection. Skipped and failed records remain part of the evidence. The successful HPC and Pi runs contain five trials per configuration. A `paper` run with the Ethereum dataset also executes synthetic workloads with 512 and 4096 blocks.

## Inputs still required for exact replay

`provenance/pending_inputs.json` lists the outstanding inputs. Existing measurements can be reanalysed, but the following omissions prevent a claim of complete replay of all recorded experiments:

- Restore each recorded suite run's `cases/` and `proof_fixtures/` directories under that run's existing directory in `results/`. Keep the original case identifiers and relative layout. Recorded input JSON may also contain machine-specific absolute paths that must be mapped when replaying an individual worker.
- Restore the original Ethereum `blocks.jsonl` to `datasets/ethereum/`. The included `manifest.json` is the snapshot manifest retained with the measurements.
- The complete original E1–E8 publication raw outputs and the measured E6 component-cost catalogue are not included. The retained `original_e2` and migration records are subsequent remeasurements of selected configurations.

The Ethereum export contains 128 blocks, heights **25,983,936–25,984,063**, with 64,208,046 payload bytes. The expected SHA-256 of `blocks.jsonl` is:

```text
190ca4433f979dfd137e27c9ee04c0c9edd85b97027e9e1ff5322ba67f38fc60
```

The measured dataset was the first 69 records admitted by the 32 MiB payload limit. Once the original file is restored, validate it from the repository root:

```bash
PYTHONPATH=implementation python3 - <<'PY'
from pathlib import Path
from msi_supplement.data import load_dataset
manifest, payloads = load_dataset(Path('datasets/ethereum'))
print(manifest['blocks_sha256'], len(payloads))
PY
```

An authorised Ethereum JSON-RPC endpoint can export the same height interval into a **new** directory:

```bash
read -r -s -p "Ethereum RPC URL: " ETH_RPC_URL
export ETH_RPC_URL
bash implementation/run.sh dataset ethereum --out "$PWD/work/ethereum-export" --start 25983936 --count 128 --rpc-env ETH_RPC_URL
unset ETH_RPC_URL
```

Check the exported `blocks.jsonl` hash against the expected hash before treating it as identical input. Provider-specific JSON fields can affect byte equality. Omitting `--start` selects a new interval ending at the endpoint's current finalised head and creates a different dataset. Credentials are runtime inputs and are not recorded in the export.

## Raspberry Pi experiments

Pi B runs the external storage/witness provider. Pi A runs the verifier. Install the repository on both devices. From the repository root on each Pi:

```bash
bash implementation/run.sh doctor
bash implementation/run.sh test
```

On Pi B, create a token once and start the provider:

```bash
cd implementation
bash run.sh token --out secrets/provider.token
bash run.sh serve --data-root provider_data --token-file secrets/provider.token --host 0.0.0.0 --port 8765
```

Copy the same token privately to `implementation/secrets/provider.token` on Pi A. The service must remain running. On Pi A, from the repository root, select the provider's address on the tested interface and run Ethernet:

```bash
read -r -p "Pi B Ethernet IP address: " MSI_PROVIDER_IP
bash implementation/run.sh run --role pi-verifier --profile paper --out "$PWD/work/pi-ethernet" --provider "http://${MSI_PROVIDER_IP}:8765" --token-file "$PWD/implementation/secrets/provider.token" --network-kind ethernet --setup-timeout 900 --dataset "$PWD/datasets/ethereum"
```

After selecting the Wi-Fi route to Pi B, run:

```bash
read -r -p "Pi B Wi-Fi IP address: " MSI_PROVIDER_IP
bash implementation/run.sh run --role pi-verifier --profile paper --out "$PWD/work/pi-wifi" --provider "http://${MSI_PROVIDER_IP}:8765" --token-file "$PWD/implementation/secrets/provider.token" --network-kind wifi --setup-timeout 900 --dataset "$PWD/datasets/ethereum"
```

`--network-kind` records a label; it does not select or change the operating-system route. Use active cooling and retain thermal/throttling records. `run.sh` changes its working directory to `implementation/`; the absolute paths above keep datasets and outputs at repository level. Output directories must be new. To run the synthetic workloads without Ethereum input, omit `--dataset` and use a different output directory. Keep provider tokens and generated signing seeds outside published results.

## HPC experiments

The recorded HPC runs used suite 1.0.0; the Pi runs used 1.0.1. Prepare an independent HPC working copy so that the two driver versions are not mixed. The successful HPC copy includes the `nfs-retain-1` adapter, which retains experiment workspaces while preserving the measured migration, source reclamation, atomic metadata swap and recovery operations.

For the LTU module environment, from the repository root:

```bash
module load Python/3.11.5-GCCcore-13.2.0
python3 tools/prepare_hpc.py --out work/hpc_suite
cd work/hpc_suite
bash run.sh doctor
python3 -m unittest discover -s tests -p test_nfs_retention.py -v
sbatch --partition=day --time=04:00:00 --export=ALL,MSI_KEEP_MIGRATION_WORKSPACES=1 hpc_ltu.sbatch real ../../datasets/ethereum
```

The `real` mode validates the supplied Ethereum export and runs both real and synthetic workloads. For a separate synthetic-only run, submit from the prepared HPC directory:

```bash
sbatch --partition=day --time=04:00:00 --export=ALL,MSI_KEEP_MIGRATION_WORKSPACES=1 hpc_ltu.sbatch synthetic
```

The independent migration diagnostic is:

```bash
sbatch hpc_nfs_migration_check.sbatch
```

Results are written under the prepared suite's `outputs/`; Slurm output reports the exact result directory. The LTU wrapper loads the stated Python module; the submission commands above select the `day` partition. Other clusters require corresponding module and scheduler settings. Check the job's exit code and the run manifest's completion/failure fields before using a result.

## Original E1–E8 workflows

| Component | Experiment |
| --- | --- |
| `e1_msi_storage` | MSI and auxiliary-state storage scaling |
| `e2_local_query` | Local verification latency and resource costs |
| `e3_communication_network` | Wire size and network sensitivity |
| `e4_concurrent_scaling` | Shard concurrency, history length, fairness and saturation |
| `e5_anchor_pruning` | Root anchoring, checkpoint update and safe pruning |
| `e6_refolding_policy` | Migration/recovery and mode-allocation policy; policy code uses the internal label E9 |
| `e7_integrity_availability` | E7 adversarial integrity and E8 outage/availability experiments |

From the repository root:

```bash
cd implementation/vendor/experiment
bash scripts/rebuild_zipapps.sh
bash scripts/run_all_tests.sh
bash scripts/run_all_local_smoke.sh
```

For Slurm publication configurations, in the same directory, set the applicable site allocation. On LTU:

```bash
export PYTHON_MODULE=Python/3.11.5-GCCcore-13.2.0
export SBATCH_PARTITION=day
bash scripts/preflight_all.sh
for MSI_COMPONENT in e1 e2 e3 e4 e5 e7; do
    bash scripts/submit_component_publication.sh "$MSI_COMPONENT"
done
```

The E7 submission also runs E8. E3 and E4 support two-node runs through `bash scripts/submit_component_publication.sh e3 dual` and the corresponding `e4 dual` command. These are different deployment configurations and must retain separate result directories. Site account, QoS, reservation and optional container settings can be set through the variables in `scripts/site_env.example.sh`.

E6 publication execution requires completed E1, E2, E3 and E8 results. After their analysis jobs complete, build the measured catalogue and submit E6 from `implementation/vendor/experiment/`:

```bash
MSI_E1_RUN=$(cat experiments/e1_msi_storage/LAST_RUN_DIR)
MSI_E2_RUN=$(cat experiments/e2_local_query/LAST_RUN_DIR)
MSI_E3_RUN=$(cat experiments/e3_communication_network/LAST_RUN_DIR)
MSI_E7_RUN=$(cat experiments/e7_integrity_availability/LAST_RUN_DIR)
mkdir -p runs/catalogues
python3 experiments/e6_refolding_policy/exp06_refolding_policy.pyz build-component-catalog --e1-results "$MSI_E1_RUN/results" --e2-results "$MSI_E2_RUN/results" --e3-results "$MSI_E3_RUN/results" --availability-csv "$MSI_E7_RUN/results/availability_catalog.csv" --output runs/catalogues/component_costs.csv --source-revision "$(sha256sum ../../../SHA256SUMS.txt | cut -d ' ' -f 1)"
export E6_COMPONENT_COST_CATALOG="$PWD/runs/catalogues/component_costs.csv"
bash scripts/submit_component_publication.sh e6
```

Reference and template costs are rejected by the publication gate. Completed run directories contain their plan, raw measurements, analysis outputs and manifests; `LAST_RUN_DIR` identifies the latest submitted run for each component. Rebuild zipapps after changing source. E1 configuration `notes` are retained because they participate in the original point identifiers.

## Measurement scope

CPU time, local cryptographic latency, process RSS and end-to-end retrieval latency are distinct metrics. The memory-pressure experiment applies a 256 MiB virtual-address limit with a 64 MiB pressure buffer; this is not a measurement of whole-device memory consumption. Recorded storage timings describe the tested filesystems and durability operations, not flash endurance. `energy_windows.json` records workload timing; no external power trace or measured joule result is included. The Ethereum inputs are trusted-RPC JSON snapshots checked for internal consistency. The prototype does not verify native Ethereum consensus/finality or implement a live sharded consensus protocol. E8 availability uses virtual time, while the Pi network and disconnect records describe the separately instrumented two-device experiments.
