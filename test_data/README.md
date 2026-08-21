# Test datasets

Archives the test suite uploads through the real project-creation path. They are
committed rather than fetched, so `pytest` works on a fresh checkout with nothing
configured — no environment variables, no paths into anyone's home directory, no
credentials.

| File | Used by | What it is |
| --- | --- | --- |
| `one_amprepo_sample.tar.gz` | most creation/edit/download tests | one hg19 sample, already aggregated |
| `one_amprepo_sample.xlsx` | metadata tests | the metadata sheet for the above |
| `Contino_unagg_040423.tar.gz` | unaggregated-ingestion tests | multi-sample, not yet aggregated |
| `two_hg38_samples_no_ecdna.tar.gz` | add-samples-to-project tests | two hg38 samples with no ecDNA |
| `coral_four_samples.tar.gz` | `test_create_coral_ac2_project` | CoRAL 2.2.0 reconstructions + the AC 2.0.0 run over them |

Keep them small. Every one of these is in every clone of the repository forever.

## `coral_four_samples.tar.gz`

Four cell lines — GBM39EC, H2009, HCC1395, TR14 — carrying seven amplicons
between them (six ecDNA, one Linear), reconstructed with CoRAL 2.2.0 against
GRCh38.

```
CoRAL_runs/<sample>/<sample>_amplicon<N>_{graph,cycles}.{txt,png,pdf}
CoRAL_runs/<sample>/<sample>_amplicon_summary.txt
CoRAL_runs/<sample>/<sample>_CNV_SEEDS.bed
CoRAL_runs/<sample>/cnvkit_output/<sample>_sorted{,.call,.bintest}.cns
AC_classification/coral_fixture_*                # one AmpliconClassifier run
```

Two things about the shape of it are load-bearing:

- **The AmpliconClassifier output has to be in the archive.** The aggregator does
  not run AC; stage 4 aborts with `No *_result_table.tsv files found` if it is
  missing. A directory of bare CoRAL runs is not a valid upload.
- **`cnvkit_output/` has to be in the archive** or the features come back with no
  `CNV_BED_file`, and the test asserts they have one.

The `*_reconstruct.log` files are *not* included. They are CoRAL's runtime
chatter, nothing reads them, and across the source dataset they came to 231 MB —
more than everything else put together.

### Rebuilding it

The source is the full 26-sample CoRAL run (not in this repository; it lives with
the lab data). Given that directory:

```bash
SRC=/path/to/CoRAL_runs
WORK=$(mktemp -d)
mkdir -p "$WORK/CoRAL_runs"

# 1. Copy the four samples, minus the reconstruction logs.
for s in HCC1395 GBM39EC H2009 TR14; do
    rsync -a --exclude='*_reconstruct.log' "$SRC/$s" "$WORK/CoRAL_runs/"
done

# 2. Build AmpliconClassifier's two input files. Paths must be absolute.
cd "$WORK"
for s in HCC1395 GBM39EC H2009 TR14; do
    for g in CoRAL_runs/$s/${s}_amplicon*_graph.txt; do
        printf '%s\t%s\t%s\n' "$s" \
            "$(realpath ${g%_graph.txt}_cycles.txt)" "$(realpath $g)"
    done >> coral_fixture.input
    printf '%s\t%s\n' "$s" \
        "$(realpath CoRAL_runs/$s/${s}_amplicon_summary.txt)" \
        >> coral_fixture_summary_map.txt
done

# 3. Run AC 2.0.0. The summary map must sit next to the .input file or AC
#    refuses to start. Takes about fifteen seconds for four samples.
amplicon_classifier.py --ref GRCh38 --input coral_fixture.input \
    -o AC_classification/coral_fixture --jobs 4 --bfb_threads 2 \
    --verbose_classification

# 4. Package.
tar czf coral_four_samples.tar.gz CoRAL_runs AC_classification
```

If you change which samples are in it, the counts asserted in
`tests/test_create_edit_project.py::test_create_coral_ac2_project` change with
them, and the test will tell you what they became.

One trap worth knowing: a sample directory with **no** amplicons (CoRAL solved
nothing) is still ingested as a sample when `cnvkit_output/` is present, and its
empty feature row is labelled `Reconstruction_tool: AmpliconArchitect`. That drags
the project-level `Reconstruction_tools` to `"CoRAL, AmpliconArchitect"` for a
project that never went near AA. MDA-MB231 was dropped from this fixture for that
reason — it is a real behaviour, but not the one this test is about.

## Datasets that are still external

Three ingestion tests still skip unless you point an environment variable at an
archive you have locally:

| Variable | Test |
| --- | --- |
| `CAPER_AC2_TEST_ARCHIVE` | `test_create_ac2_project`, and one download test |
| `CAPER_AC2_FAN_TEST_ARCHIVE` | `test_create_ac2_fan_project` |
| `CAPER_AC2_HG38_TEST_ARCHIVE` | `test_create_ac2_hg38_project` |

They can be given the same treatment as the CoRAL one — cut a small subset,
commit it, drop the variable — whenever it is worth the repository size.
