E6 migration and E9 mode-assignment input bundle
==============================================

These are retained inputs and summaries from the original La Trobe HPC run
e6-migration-publication-20260625-123230. Files were recovered from experiment.zip.
The CSV files and original checksum files preserve their original bytes.
No new performance experiment was run to make this bundle.

Contents
--------
component_costs.csv                 12 component rows: 4 epoch lengths x 3 modes
migration_cost_catalog.csv          96 measured migration-cost rows
policy_summary.csv                 4,040 grouped original policy-summary rows
upstream/E1/theory_vs_serialization.csv
upstream/E2/latency_summary.csv
upstream/E2/throughput_resources.csv
upstream/E3/wire_sizes.csv
upstream/E7/availability_catalog.csv
original_checksums/                 The two original input checksum lists
manifest.json                      Relative paths, hashes and ZIP source members
SHA256SUMS                         Bundle hashes, including manifest.json

Each upstream input appears once in this bundle. manifest.json maps it to its
original run and ZIP member. Original CSV provenance fields retain historical
HPC paths; those paths are not required to run the offline checks. The original
checksum lists are resolved to these relative paths by verify_e6_catalogue.py.
Changing an original CSV changes its hash and invalidates the recorded policy
input identity; make any redacted or reformatted copy a separately named file.

Experiment-number mapping
-------------------------
Internal E6 is the root-preserving migration experiment. Internal E9 is the
mode-assignment study reported in manuscript Section VII-F (Mode Assignment
Results), within the combined refolding/policy experiment package. The E7
directory contains the E8 availability study used to build the component
catalogue. The filename provenance/availability_catalog_E7.csv in the original
run and upstream/E7/availability_catalog.csv here refer to the same source data.

Offline verification (Python 3, standard library only)
----------------------------------------------------
From the repository root:

  python3 publication-artifacts/tools/verify_e6_catalogue.py

This checks the hashes of the 10 restored files, the five upstream input hashes
and both policy-input hashes, recalculates all 48 component values from their
upstream CSVs, and checks the seven policy-table rows and DP context values
reported in the manuscript against the original policy summary.

For the optional full retained-policy reanalysis, download and extract the
release asset:

  e6-migration-publication-20260625-123230-E6-E9-final.tar.gz

Then pass the extracted run directory (not its parent):

  python3 publication-artifacts/tools/verify_e6_catalogue.py \
    --run-dir /path/to/e6-migration-publication-20260625-123230

This also checks 2,006 retained files by hash, matches the 1,410 migration and
590 policy record IDs to their plans, checks each policy record's input hashes,
and recomputes the 4,040 policy-summary groups from 19,120 observations. Numeric
summary comparisons use a relative and absolute tolerance of 1e-12 to allow
floating-point interpolation differences. Archived all_checks_pass flags are
read as recorded; the migration or integrity tests are not executed again.

The script only reads files and prints JSON to stdout. It exits with status 0
for a completed consistency check and status 1 for a failed check. Without
--run-dir, raw-run checks are explicitly reported as NOT_REQUESTED.

Interpretation and version limits
---------------------------------
- Energy entries in component_costs.csv are empty. This bundle does not supply
  energy measurements. The retained throughput-resources input is preserved
  even though it did not supply usable energy values for this catalogue.
- The catalogue field cpu_ns comes from E2 acceptance-latency medians; the
  verifier reproduces that original input choice without relabelling it as a
  fresh CPU-time measurement.
- Availability values come from the archived E8 virtual-time study with a 5%
  correlated failure probability, a 500-ms deadline and no retry. They are not
  measurements of physical radio outages.
- The deployment-package availability_measured.csv was an unfilled template;
  it is not an input to this bundle. Synthetic reference_results fixtures are
  also not used here.
- The data predate later checkpoint-binding corrections. Their recovery does
  not constitute a full rerun of the corrected implementation, nor validate
  power-loss persistence or native sharded-blockchain integration.
