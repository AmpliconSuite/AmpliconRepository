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
| `ac2_nine_samples.tar.gz` | `test_create_ac2_project`, `test_ac2_single_and_batch_sample_download_contents` | nine GRCh37 glioma samples classified with AC 2.0.0 |
| `ac2_five_fan_samples.tar.gz` | `test_create_ac2_fan_project` | five GLASS tumour/normal pairs, one FAN feature each |
| `ac2_four_samples_hg38.tar.gz` | `test_create_ac2_hg38_project` | four GRCh38 cell lines covering all five AC classifications |

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

## The three AmpliconClassifier 2.0 fixtures

All three are AmpliconArchitect runs with an AC 2.0.0 classification over them,
laid out the way the pipeline writes them:

```
AA_outputs/<sample>/<sample>_AA_results/<sample>_amplicon<N>_{graph,cycles}.txt
AA_outputs/<sample>/<sample>_AA_results/<sample>_amplicon<N>.png
AA_outputs/<sample>/<sample>_AA_results/<sample>_summary.txt
AC_classification/<prefix>_*                     # one AmpliconClassifier run
```

They are three archives rather than one because the three things worth covering
do not occur together in any one cohort:

- **`ac2_nine_samples.tar.gz`** — nine GRCh37 glioma samples (de Carvalho et al.),
  eleven amplicons, ten ecDNA and one FAN. The plain "does an AC 2.0 archive go
  in at all" case, and the one the download test uses, because at 1.5 MB it is
  the cheapest project to build twice.
- **`ac2_five_fan_samples.tar.gz`** — five GLASS tumour/normal pairs, thirteen
  features, five of them FAN in five distinct samples. Sample names here are the
  long `<tumour>__<normal>` form, which is worth having in the suite on its own.
  This is the only fixture with a complete pipeline sample directory —
  `cnvkit_output/`, `_run_metadata.json`, `_sample_metadata.json` — so it is what
  pins the AA and AC versions the site reads out of those files.
- **`ac2_four_samples_hg38.tar.gz`** — BT474, HCC1954, SKBR3 and TR14 against
  GRCh38: thirty-three features covering **every** classification AC 2.0 emits
  (ecDNA, BFB, FAN, Linear, Complex-non-cyclic). BT474 alone carries all five,
  and the other three are there so the project is not a single sample.

The AA `.pdf` renderings are excluded from all three. They were 19 MB of the 21 MB
in the GRCh38 set alone, the site shows the PNGs, and nothing asserts on them.

### Rebuilding them

The sources are lab data, not in this repository. Given the AA run directories,
each fixture is the same three steps — copy the samples, run AC over them, tar
the result. Written out for the GRCh38 one:

```bash
SRC=/path/to/consolidated_AA_set
WORK=$(mktemp -d); cd "$WORK"

# 1. Copy the samples, minus the PDFs.
for s in BT474 HCC1954 SKBR3 TR14; do
    ar=$(find "$SRC" -maxdepth 3 -type d -name "${s}_AA_results")
    mkdir -p "AA_outputs/$s/${s}_AA_results"
    cp $ar/${s}_amplicon*_graph.txt $ar/${s}_amplicon*_cycles.txt \
       $ar/${s}_amplicon*.png $ar/${s}_summary.txt "AA_outputs/$s/${s}_AA_results/"
done

# 2. AmpliconClassifier's two input files. Paths must be absolute, and the
#    summary map must sit next to the .input file or AC refuses to start.
for n in $(ls AA_outputs); do
    for g in AA_outputs/$n/${n}_AA_results/${n}_amplicon*_graph.txt; do
        printf '%s\t%s\t%s\n' "$n" \
            "$(realpath ${g%_graph.txt}_cycles.txt)" "$(realpath $g)"
    done >> ac_hg38.input
    printf '%s\t%s\n' "$n" \
        "$(realpath AA_outputs/$n/${n}_AA_results/${n}_summary.txt)" \
        >> ac_hg38_summary_map.txt
done

# 3. Run AC 2.0.0.
amplicon_classifier.py -i ac_hg38.input --ref GRCh38 \
    -o AC_classification/hg38_fixture --verbose_classification --jobs 4

# 4. Package.
tar czf ac2_four_samples_hg38.tar.gz AA_outputs AC_classification
```

The two GRCh37 fixtures are built identically with `--ref GRCh37 --add_chr_tag`
— those runs have no `chr` prefix on their contigs. For the GLASS one, keep the
whole sample directory (cnvkit output and metadata JSONs included) and exclude
`*.pdf`, `*_logs.txt` and `*.log` instead.

The counts asserted in `tests/test_create_edit_project.py` are the counts these
recipes produced with AC 2.0.0. They are frozen once the archive is committed —
nothing re-runs AC at test time — but rebuilding a fixture against a different
AC release can move them, and the tests will say what they became. The nine-sample
archive is an example: the original run of that cohort called all twelve features
ecDNA, and the AC 2.0.0 rebuild calls eleven, one of them FAN.
