# Sample Metadata Harmonization — Specification

**Status:** specification agreed with the maintainer; no implementation plan yet.
This document says *what* the system must do and *what it must never do*. The
implementation plan — sequencing, module layout, migrations — is a separate
document, written against this one.
**Measured against:** the production read API, 2026-10-31, as an anonymous
caller: the 35 projects `/api/v1/projects/` lists, 19,913 samples. Every number
in §2 came from that pull; none is estimated. Private and hidden-public projects
are not in it, so the real vocabulary is larger, never smaller.
**OncoTree release referenced:** `oncotree_2025_10_03` (897 tumour types).

---

## 1 · Purpose

Sample metadata is free text. Each project spells cancer type, tissue, sample
type, sex, age and survival its own way, in columns it names itself, so a
question like "every GBM sample on the site" or "all lung cancer, by subtype"
cannot be answered by search, by site statistics, or by the API.

This spec defines a **harmonization layer**: a second, controlled set of
per-sample fields, derived from what submitters wrote and never replacing it,
with a recorded reason for every value, a review loop that owners and admins
can work through at the scale of thousands of samples, and an automated curator
— an LLM, behind an interface that does not care which — for the long tail
that rules cannot reach.

---

## 2 · The measured problem

### 2.1 Only three fields are searchable, and they are lifted by exact column name

`Cancer_type`, `Tissue_of_origin` and `Sample_type` reach search because
`extra_metadata._apply_metadata_to_runs` copies a sheet column into them only
when the column is named exactly `cancer_type`, `sample_type` or
`tissue_of_origin` (`caper/caper/extra_metadata.py:273-278`). The public corpus
has **102 distinct sheet columns**; the information this spec needs is spread
across names such as `primary_disease`, `diagnosis_tcga_code`,
`ccle_disease_subtype`, `lineage_subtype`, `primary_tumor_site`,
`organ_of_origin`, `biopsy_site`, `tumor_status`, `primary_or_metastasis`,
`sex`, `patient_sex`, `age`, `age_group`, `vital_status`, `days_to_death`,
`days_to_last_follow_up` and `days_to_last_followup`. None of these is
searchable today.

### 2.2 The cancer-type field

| | |
| --- | ---: |
| Distinct `Cancer_type` spellings | **256** |
| Samples with a non-blank value | 10,620 of 19,913 |
| …whose value is an exact OncoTree name or code (case/punctuation folded) | 5,205 in 55 spellings |
| …that need a synonym, an ID lookup or the curator | **5,415 in 199 spellings** |

Same type, several spellings: "Pancreas Adenocarcinoma" (232), "Pancreatic
Adenocarcinoma" (144), "Pancreas" (151); "Esophagus Adenocarcinoma" (97),
"Esophageal Adenocarcinoma" (114). Mixed granularity: "Lung" (503) beside "Lung
Adenocarcinoma" (206) beside "Non-Small Cell Lung Cancer (NSCLC),
Adenocarcinoma" (57). Context-dependent: "Squamous cell carcinoma" (120) and
"Adenocarcinoma" (76) name no organ.

Search matches these by substring (`search.py:756-770`,
`search_index.py:284-289`), so "lung" finds "Lung adenocarcinoma" but "GBM"
does not find "Glioblastoma Multiforme" and "NSCLC" does not find "LUAD". Site
statistics count the raw strings (`site_stats.py:431-465`), so one cancer type
appears as several slices.

### 2.3 Whole projects carry no cancer type at all

25 of the 35 projects have a blank cancer type on every sample, among them
Pediatric Pancancer (3,796), PCAWG (2,095), CUGA (1,411), both Medulloblastoma
subsets (482) and TCGA (101). For some of them the only evidence is the project
name or description ("Medulloblastoma hg38 subset").

### 2.4 Sample type conflates three different facts

29 spellings of `Sample_type`, e.g. "Primary tumour - blood derived (bone
marrow)", "Metastatic tumour - metastasis local to lymph node", "NCI-60 cell
line", "Expanded patient-derived model", "Recurrent". One string carries **what
the specimen is** (tumour tissue, cell line, patient-derived model), **its
disease state** (primary, metastatic, recurrent) and sometimes **where it was
taken** (blood, bone marrow, lymph node). §6 separates them.

### 2.5 Some projects already supply identifiers

| Column | Projects | Distinct | Maps to OncoTree directly |
| --- | ---: | ---: | --- |
| `RRID` (Cellosaurus) | 2 | 804 | — (identity, see §6.5) |
| `DepMap_ID` | 2 | 804 | — (identity) |
| `Cellosaurus_NCIt_id` | 2 | 165 | 79 of 165 values, 489 of 804 samples, via OncoTree's own NCIt cross-references |
| `diagnosis_tcga_code` | 1 | 22 | 14 of 22; `GBM`, `OV`, `LIHC`, `KIRC`, `KICH`, `SARC`, `MISC` are TCGA study codes, not OncoTree codes |
| `phenotype_doid` | 1 | 1 | — (`DOID:769`, neuroblastoma) |

Identifiers are the strongest evidence available and must be used before any
text matching (§8).

### 2.6 The other fields

| Field | Seen as | Notes |
| --- | --- | --- |
| Sex | `sex` (3 projects: Male/Female/None), `patient_sex` (4: male/female) | |
| Age | `age` (3 projects, 92 distinct, 6 values "Adult"), `age_group` (1: Adults/AYA/Pediatrics) | Numeric and categorical forms coexist |
| Overall survival | `days_to_death` (null on 3,077 of 6,570), `days_to_last_follow_up` (4), `days_to_last_followup` (1), `vital_status` (1: Alive/Dead/Not Reported/Unknown) | Derivable |
| Progression-free survival | — | **No public project supplies it today.** The field is defined (§6.9) so it has a place when one does |
| Disease state | `primary_or_metastasis` (2), `tumor_status` (1, includes "Metastatic \| Primary" ×42 and "Non-Malignant"), `tm_sample_type_original` (GLASS: TP/R1/R2) | Plus the conflated `Sample_type` of §2.4 |
| Species | not stated anywhere | Implied by reference build (mm10) for the Pradella murine projects |

### 2.7 OncoTree no longer has a `GBM` code

OncoTree follows the WHO 2021 CNS classification. Glioblastoma is `GB`,
"Glioblastoma, IDH-Wildtype" (level 4, under adult diffuse glioma); there is no
code for the pre-2021 "glioblastoma multiforme", which included IDH-mutant
tumours now called astrocytomas. TCGA's GBM samples are labelled with the
pre-2021 term. Mapping them to `GB` would assert an IDH status nobody measured;
mapping them only to a parent node would stop "GBM" finding them. §5.2–5.3 resolve
this.

### 2.8 Review is tractable when grouped by value

| Project | Samples | Distinct cancer-type values | Distinct sample-type values |
| --- | ---: | ---: | ---: |
| Hartwig Medical Foundation | 4,170 | 39 | 1 |
| Pediatric Pancancer | 3,796 | 1 (blank) | 1 |
| PCAWG unfiltered | 2,908 | 37 | 13 |
| TCGA unfiltered | 2,471 | 26 | 5 |
| Human Cancer Models Initiative | 989 | 57 | 3 |
| DepMap | 476 | 96 | 1 |

A reviewer of the largest project decides 39 things, not 4,170. §10 is built on
this.

---

## 3 · Decisions already made

| # | Decision |
| --- | --- |
| D1 | **OncoTree** is the cancer-type vocabulary, with each node's NCIt code stored beside it. |
| D2 | Cancer type **may be inferred from a project's name and description**, recorded as a distinct, lower-confidence source. |
| D3 | **Public and hidden-public** projects are harmonized. **Private projects are not**, and their metadata never reaches the curator. |
| D4 | **Admins and project owners** review. The review interface must stay workable for projects with over a thousand samples. |
| D5 | Between humans, **the most recent decision wins**, whoever made it. |
| D6 | The curator is an **LLM behind a backend-agnostic interface**. Which model or service (a direct LLM API, Biomni, other) is undecided; nothing in this spec depends on it. |
| D7 | The curator runs in a **separate worker process**, never in a web request. |
| D8 | Harmonized data is exposed in **search filters, site statistics, the API**, and to a lesser extent project and sample pages. |
| D9 | **Cell-line, PDX and organism identity is kept** as first-class fields. A cell line also gets a cancer type, but never loses its name. |
| D10 | **Non-human samples, including engineered mouse models, get an OncoTree cancer type** like any other sample; `species` and `specimen_kind` say what they are. |
| D11 | **GBM is searchable both as one disease and by its current subtypes** (`GB`, `ASTR`) (§5.3). |
| D12 | **Age is stored as a number**, with a pediatric / non-pediatric flag as the only grouping (§6.8). |
| D13 | **Owners are told by email** when a project's review is ready to look at — after creation, or after an update that brings metadata or samples not seen before (§16.1). |
| D14 | **When the curator re-proposes after a flag, owners and site admins are emailed** (§16.1). |
| D15 | **Pediatric means under 18** (§6.8). |
| D16 | **A rule change never overrides a human correction; it sends affected confirmations back to review** and emails the owners (§9.4, §16.1). |
| D17 | **The criteria for giving a sample a label are defined and shown in the interface** at the point of decision, and the curator is held to the same criteria (§5.7). |

---

## 4 · Scope

**In scope:** the harmonized fields of §6; their provenance (§7); the
resolution order (§8); review units and decisions (§9–10); the curator contract
(§11); the worker (§12); visibility (§13); storage roles (§14); what the
consumers read (§15); the review interface (§16).

**Out of scope:**

- Changing what submitters wrote. `extra_metadata_from_csv`, `Cancer_type`,
  `Sample_type`, `Tissue_of_origin` and `Sample_metadata_JSON` stay exactly as
  they are and keep every current use (§17, I1).
- Validating or constraining metadata at upload. Uploads keep accepting any
  sheet. (A later change may suggest vocabulary terms at upload; nothing here
  requires or forbids it.)
- Harmonizing project-level fields other than as evidence for samples.
- Treatment, stage, grade, race and ethnicity. They are present in some
  projects (`tumor_stage`, `pathologic_stage`, `treatment_status`, `race`) and
  the storage of §14 must not preclude adding them as further fields, but they
  are not specified here.
- The implementation plan.

---

## 5 · Vocabularies

### 5.1 OncoTree, pinned

- A specific OncoTree release is pinned (initially `oncotree_2025_10_03`), held
  locally, and recorded on every harmonized cancer-type value. The site never
  depends on `oncotree.mskcc.org` being reachable at request time.
- Every node is a valid answer at every level. The 32 level-1 nodes are
  tissues (`LUNG`, `BRAIN`, `BOWEL`…), so a site-only value such as "Lung" maps
  to `LUNG` without losing or inventing meaning. A value maps to the **most
  specific node its evidence supports, and no more specific**.
- Moving to a new release is an explicit, admin-triggered operation. It uses
  the release's `history`, `precursors` and `revocations` to carry values
  across; a value whose code no longer exists returns to review rather than
  being guessed.

### 5.2 Legacy and study labels

Some labels the corpus uses correctly are not OncoTree nodes: pre-2021 "GBM" /
"glioblastoma multiforme" (§2.7) and the TCGA study codes `GBM`, `OV`, `LIHC`,
`KIRC`, `KICH`, `SARC`, `MISC`.

- A curated **crosswalk table** maps each such label to the OncoTree node it
  safely implies (e.g. TCGA `LIHC` → `HCC`, `KIRC` → `CCRCC`, `KICH` →
  `CHRCC`). Where the honest mapping is a parent node — pre-2021 GBM →
  `DIFG`/`ADIFG`, `OV` → `OVARY` level unless histology is given — the parent is
  used.
- The harmonized value also keeps a **`legacy_label`** (e.g. `GBM`) whenever
  the evidence used one, including after an upgrade (below).
- **Upgrading with evidence.** A pre-2021 GBM sample whose metadata states IDH
  status is mapped to the post-2021 node that status implies — IDH-wildtype →
  `GB`, IDH-mutant → `ASTR` (formerly "GBM, IDH-mutant", now astrocytoma,
  IDH-mutant, grade 4) — and keeps `legacy_label: GBM`. Without IDH evidence it
  stays on the parent node (`ADIFG`). The curator may make this upgrade only by
  citing the IDH evidence in its rationale.
- The crosswalk is data, maintained by admins, versioned like the synonyms of
  §9.3.

### 5.3 Search groupings

People ask for "GBM" as one disease and also by its current subtypes, and
OncoTree has no single node for the former. A **search grouping** is a named,
admin-maintained union of OncoTree subtrees and legacy labels, searchable and
offered in filters beside the tree. The first one:

| Grouping | Matches |
| --- | --- |
| **GBM** (glioblastoma, any IDH status) | the `GB` subtree (incl. `SCGBM`), plus every sample with `legacy_label: GBM` — which includes legacy samples upgraded to `ASTR` and those left at `ADIFG` |

So searching **GBM** finds all glioblastomas, old and new nomenclature;
**GB** finds IDH-wildtype glioblastoma only; **ASTR** finds IDH-mutant
astrocytomas of every grade, including the former IDH-mutant GBMs. An
`ASTR` sample with no GBM label (a grade 2–3 astrocytoma) is not in the GBM
grouping. Groupings are versioned like synonyms; adding one is an admin action,
not a spec change.

### 5.4 Cellosaurus

Cell lines are identified by Cellosaurus accession (`CVCL_…`, which is the
RRID). Cellosaurus supplies species, donor sex and age, the NCIt disease, and
problematic-line flags. Lookups are cached locally with a fetch date; the site
does not call Cellosaurus at request time.

### 5.5 NCBI Taxonomy

Species is an NCBI Taxonomy ID with its name (`9606` Homo sapiens, `10090` Mus
musculus).

### 5.6 Closed enumerations

Specimen kind, disease state and sex are small closed lists owned by this spec
(§6). Adding a value is a spec change.

### 5.7 Labeling criteria

Every value a sample can be given — each OncoTree node, each enumeration value,
each absence reason (§7.3) — has **labeling criteria**: what it means, what
evidence qualifies a sample for it, what does not, and what to use instead when
the evidence falls short. They are the rules a human applies when confirming or
correcting, and the same rules the curator is given (§11.1), so the two cannot
drift apart.

- **One store, versioned.** Criteria are data in the vocabulary store (§14),
  edited by admins. Each change records who, when and why, and every decision
  and curator answer records the criteria version it was made under (§7.1).
  Changing criteria text changes no value by itself.
- **For an OncoTree node,** the criteria shown are: the node's path in the tree
  (`BRAIN › DIFG › ADIFG › GB`), its NCIt definition (from the node's NCIt
  code, held in the vocabulary store), the general rule that a value is the
  most specific node the evidence supports and no more specific (§5.1), and an
  admin-written note where a near-miss is common. Initial notes:

  | Node | Note |
  | --- | --- |
  | `GB` | Requires stated IDH-wildtype status. "Glioblastoma"/"GBM" alone → `ADIFG`, keeping `legacy_label: GBM` (§5.2). |
  | `ASTR` | Requires stated IDH-mutant status. A pre-2021 GBM with IDH-mutant evidence belongs here, keeping `legacy_label: GBM`. |
  | level-1 tissue nodes (`LUNG`, `BOWEL`…) | Use when only the organ is known. A histology word with no organ ("Adenocarcinoma") is not evidence of an organ. |

- **For an enumeration value,** the criteria are those below; changing these
  is a spec change, not an admin edit.

  | Field | Value | Qualifies | Does not qualify |
  | --- | --- | --- | --- |
  | `specimen_kind` | `tumor_tissue` | Material taken directly from a patient's tumour — resection, biopsy, or blood/bone marrow for a haematological cancer — and sequenced without culture or engraftment | Anything passaged in culture or grown in an animal |
  | | `normal_tissue` | Non-tumour patient tissue sequenced as the sample of interest | A matched normal used only as the reference |
  | | `cell_line` | An established, continuously passaged culture; a Cellosaurus entry is strong evidence | A short-term primary culture (→ `patient_derived_other`); an organoid (→ `organoid`) |
  | | `pdx` | Patient tumour engrafted and grown in a mouse | A cell line grown as a xenograft (→ `cell_line`, with the xenograft noted) |
  | | `organoid` | Patient-derived 3D culture described as an organoid | A spheroid (→ `patient_derived_other`) |
  | | `patient_derived_other` | A patient-derived model whose form is unstated or is something else: spheroid, short-term culture, "expanded patient-derived model" | A model whose form is stated and listed above |
  | | `engineered_model` | A tumour or cell population created by genetic engineering, rather than derived from a naturally occurring tumour | An established line that was later modified (→ `cell_line`) |
  | | `other` | None of the above; a note is required | — |
  | `disease_state` | `primary` | From the original tumour at its site of origin, before any recurrence; pre-invasive lesions (e.g. DCIS) count here. For a model, derived from such a tumour | A biopsy from an organ other than the primary's |
  | | `metastatic` | From a site the cancer spread to, lymph-node metastases included, or a model derived from one. A biopsy site that differs from the primary organ is evidence | Locally advanced disease at the primary site |
  | | `recurrent` | From tumour that returned after treatment or remission (e.g. GLASS `R1`/`R2`) | A second, unrelated primary |
  | | `non_malignant` | Stated benign or non-neoplastic | Pre-invasive or in-situ lesions (→ `primary`) |
  | `sex` | `female` / `male` | As recorded for the donor (for a model, the donor of the original tumour) | Anything inferred from sequence data; the curator does not infer sex |

- `pediatric` is derived (§6.8) and has no criteria of its own: a reviewer
  edits `age.years` or `source_group`, not the flag.

---

## 6 · The harmonized fields

Each field is stored as the provenance-bearing value of §7. Each lists the
evidence the resolver consults, strongest first. "Blank" below means absent,
empty, NaN, `None`, `NA`, `Unknown`, `Not Available`, `Not Reported` and
`Data Not Available` (case-insensitive).

### 6.1 `cancer_type`

- **Value:** an OncoTree node — `code`, `name`, `main_type`, `tissue`,
  `level`, `ncit` — plus optional `legacy_label` (§5.2).
- **Evidence:** OncoTree code columns; NCIt IDs (`Cellosaurus_NCIt_id`, or from
  the Cellosaurus record of §6.5) via OncoTree's NCIt references; crosswalked
  codes (`diagnosis_tcga_code`, DOID); text columns (`cancer_type`,
  `Cancer_type`, `primary_disease`, `ccle_disease_subtype`, `lineage_subtype`,
  `diagnosis_*_name`, `tissue_source_site`…); the tissue/site fields as
  disambiguation context; the project name and description (D2).
- A cell line's cancer type is that of the tumour it was derived from.
- Species does not limit it (D10): an engineered murine medulloblastoma model
  is `MBL` with `species` Mus musculus and `specimen_kind: engineered_model`.
  OncoTree is a human classification, so a non-human mapping is to the human
  type the model is built or described to represent, and the rationale says so.

### 6.2 `tissue`

- **Value:** an OncoTree level-1 tissue node: the organ of the primary tumour.
- **Evidence:** the `cancer_type` result's own tissue when one exists; otherwise
  `primary_tumor_site`, `organ_of_origin`, `primary_site`, `Tissue_of_origin`,
  `lineage`.
- For a metastasis this is the **primary** organ, not the biopsy site.

### 6.3 `specimen_site`

- **Value:** an OncoTree level-1 tissue node, or one of `blood`, `bone_marrow`,
  `lymph_node`: where the analysed material was taken.
- **Evidence:** `biopsy_site`, `site_of_resection_or_biopsy`,
  `sample_collection_site`, and the site part of conflated `Sample_type`
  strings (§2.4).
- Optional. Blank is normal.

### 6.4 `specimen_kind`

- **Value:** one of
  `tumor_tissue`, `normal_tissue`, `cell_line`, `pdx`, `organoid`,
  `patient_derived_other` (spheroids, "Expanded patient-derived model" where the
  form is not stated), `engineered_model` (e.g. engineered murine ecDNA models),
  `other`.
- **Evidence:** the presence of a model identity (§6.5) implies `cell_line`
  unless stated otherwise; `growth_pattern` ("3D Organoid", "3D Spheroid");
  `Sample_type` / `sample_type`; `model_manipulation`; project description.

### 6.5 `model`

- **Value:** `{name, cellosaurus_id, depmap_id, other_ids}` for a cell line or
  patient-derived model; absent for patient tissue.
- **Evidence:** `RRID`, `DepMap_ID`, `cell_line_name`,
  `stripped_cell_line_name`, `Sanger_Model_ID`, `case_id`/`associated_model_ids`
  (HCMI); sample and project names for well-known lines (e.g. COLO320DM,
  HCC827, GBM39) only through the curator, at curator confidence.
- `name` is the submitter's own name for the line, verbatim (D9). Where
  `cellosaurus_id` resolves, the Cellosaurus record also supplies evidence for
  `cancer_type`, `species`, `sex` and `age`, and its problematic-line flag is
  carried onto the value.

### 6.6 `disease_state`

- **Value:** one of `primary`, `metastatic`, `recurrent`, `non_malignant`.
- **Evidence:** `primary_or_metastasis`, `tumor_status`,
  `tm_sample_type_original` (GLASS `TP` → primary, `R1`/`R2` → recurrent),
  `Sample_type` / `sample_type`.
- Single-valued. A raw value naming more than one state ("Metastatic |
  Primary") is not silently resolved: it is left unset at low confidence and
  queued for review.
- For a cell line, the state of the tumour it was derived from.

### 6.7 `species`

- **Value:** NCBI Taxonomy ID and name.
- **Evidence:** Cellosaurus record; the project's reference build (mm10 →
  Mus musculus; hg19/GRCh37/hg38/GRCh38 → Homo sapiens) — a build-derived value
  is an ID lookup (§7.2), not a guess, but a sheet stating otherwise wins.

### 6.8 `sex`, `age`

- **`sex`:** `female` | `male`. Of the donor, for models too.
- **`age`:** `{years, source_group}`. `years` is a number, at
  diagnosis/sampling as the source states it, and is the canonical form. Units
  other than years (days, months) are converted and the conversion recorded in
  the evidence.
  `source_group` is set only when the source gives a category instead of a
  number — HCMI's `age_group` (`Pediatrics`/`AYA`/`Adults`), or a non-numeric
  `age` such as "Adult" — and holds the source's label verbatim.
- **`pediatric`:** `true` | `false` | unset — the only age grouping the site
  defines. From `years` when known: `true` below 18, `false` otherwise. (18
  is the most widely shared meaning — legal minor, and the usual cohort
  definition — between childhood-cancer statistics' under 15 and US pediatric
  oncology trials' up to 21. It is one configuration value; a different
  cut-off is a range filter away for anyone who needs one.) From a
  `source_group` only when it is unambiguous: "Pediatrics"/"Pediatric"/"Child" →
  `true`, "Adult(s)" → `false`; a category that spans the boundary (HCMI's
  `AYA`, 15–39) leaves it unset.
- No other age groups are defined. Users group by age as they see fit, using
  the numeric range filter (§15.1) or `years` in the API.

### 6.9 `overall_survival`, `progression_free_survival`

- **Value:** `{days, event}`, `event` being `true` (death / progression
  observed) or `false` (censored).
- **OS evidence:** `vital_status`, `days_to_death`, `days_to_last_follow_up` /
  `days_to_last_followup`. Event observed → `days_to_death`; otherwise →
  last follow-up, censored. A sample with neither stays unset.
- **PFS:** no public source today; the same shape, filled when one exists.
- Survival is a patient property; for a cell line it is `not_applicable`
  (§7.3).
- Months are converted to days and the conversion recorded.

---

## 7 · Provenance

### 7.1 The value record

Every harmonized field on every sample is a record, never a bare value:

```
{
  value:        <the field's value, §6>          # or null, with absence_reason
  absence_reason: missing | unmappable | not_applicable | conflicting   # only when value is null
  source:       human | id_lookup | synonym | curator | project_inference
  confidence:   high | medium | low               # see 7.2; absent when source is human
  evidence:     [{column, raw_value}, …]          # what it was derived from
  rationale:    <one line>                        # required for curator and project_inference
  decided_by:   <username>                        # human only
  decided_at:   <timestamp>
  vocab_version: <oncotree release / crosswalk version / Cellosaurus fetch date>
  rule:         <the synonym or crosswalk entry, at its version>   # synonym / id_lookup via crosswalk only
  criteria_version: <labeling criteria version in force, §5.7>
  basis:        shown_evidence | publication | lab_records | other   # human only
  note:         <free text>                       # human only; required when basis is other
  confirmed_from: {source, rule}                  # confirm only: what produced the confirmed value, §9.4
  review_status: unreviewed | confirmed | corrected | flagged | reproposed | rule_changed
}
```

### 7.2 Sources and confidence

| Source | Meaning | Confidence |
| --- | --- | --- |
| `human` | An owner or admin set or confirmed it | — (treated as authoritative) |
| `id_lookup` | Derived from a stable identifier: OncoTree code, NCIt ID, crosswalked study code, RRID/DepMap ID, reference build | `high` |
| `synonym` | Normalized text matched an OncoTree name, a curated synonym or a crosswalk label | `high` for an exact OncoTree name, `medium` for a curated synonym |
| `curator` | Proposed by the automated curator from sample-level evidence | `medium` or `low`, as the curator states it (§11) |
| `project_inference` | Inferred from project name/description only (D2) | always `low` |

Confidence is a three-level label, not a number: model-reported probabilities
are not calibrated and must not be presented as if they were.

### 7.3 Absence is a value

`missing` — no evidence at all. `unmappable` — evidence exists but names no
vocabulary term. `not_applicable` — the field does not apply (survival for a
cell line). `conflicting` — evidence disagrees (§6.6). These are reported and
filterable; an unset field is never silently equivalent to "no".

---

## 8 · Resolution order

For each field of each sample, the resolver tries, in order, and stops at the
first that yields a value:

1. a **human decision** covering the sample (§10);
2. **identifier lookup**;
3. **synonym / exact-name / crosswalk** match;
4. the **curator's** answer for the review group (§11);
5. **project inference** (cancer type and tissue only).

Stages 2–3 are deterministic, need no network, and run as part of indexing.
Stages 4–5 come from stored curator answers; the resolver never calls the
curator itself. A sample whose deterministic stages fail and whose group has no
curator answer yet is `missing`/`unmappable` until the worker (§12) supplies
one.

When stages 2–3 produce disagreeing values from different columns (e.g. a code
says `LUAD`, the text says "Breast"), the result is `conflicting` and the group
is queued for review; neither is preferred silently.

---

## 9 · Review units

### 9.1 The group

The unit of review, curation and decision is a **group**: within one project
lineage, for one harmonized field, the samples sharing the same tuple of raw
evidence values for that field. Two samples whose cancer-type evidence is
`("Squamous cell carcinoma", tissue "Lung")` are one group; the same string
with tissue "Head and neck" is another.

The group key is `(version_chain_id, field, normalized evidence tuple)`.
Normalization is case-folding, whitespace and punctuation collapse, and
blank-equivalence (§6); it never merges strings that differ in a letter.

### 9.2 Groups follow the project across versions

Keyed on `version_chain_id`, a group and its decisions survive re-aggregation:
a new version whose samples carry the same evidence lands in the same groups
and inherits their values. A sample whose evidence changed lands in a new group
and starts unreviewed. Deleted-version tombstones (`version_deleted_from_history`)
are never harmonized.

### 9.3 Global synonyms

A **synonym** maps a normalized string, with no context, to a vocabulary term,
for strings that mean the same thing everywhere ("Pancreatic Adenocarcinoma" →
`PAAD`). Synonyms are site-wide, so **only admins** create, change or retire
them. An owner's decision is always project-scoped (§10.2). An admin may promote
a project-scoped decision to a synonym. Synonyms and the crosswalk are
versioned; each change records who, when and why. Changes are **published** as a
new version of the rule set; one publication may change many entries.

### 9.4 When a rule changes

A published change to a synonym, a crosswalk entry or the vocabulary release can
change the value a rule produces for a group. What happens depends on the
group's most recent human decision (§10):

| Group's state | Effect |
| --- | --- |
| No human decision | Takes the new rule value. |
| **Corrected** | Untouched. A correction is a human's own judgment and stands. |
| **Confirmed**, and the confirmed value came from a rule this publication changed, and the new value differs | Goes **back to review** with `review_status: rule_changed`. The confirmed value stays in effect until a human acts; nothing is overwritten without one. The review interface shows old value, new rule value, and the rule change that caused it. |
| Confirmed, any other case | Untouched. |

Example: the synonym "Glioblastoma" → `GB` is changed to → `ADIFG`. A project
whose owner *corrected* its "Glioblastoma" group to `GB` (knowing the cohort is
IDH-wildtype) keeps `GB`. A project whose owner only *confirmed* the `GB` the
rule had proposed goes back to review. This is why a confirmation records which
rule produced the value it confirmed (§7.1).

---

## 10 · Human decisions

### 10.1 Who

Admins (`is_staff`) on any harmonized project; owners/members on their own
(`is_user_a_project_member`). The same permission check governs viewing the
review interface and writing decisions, including flags. Every decision
requires a logged-in user, and records who made it.

### 10.2 Scope

A decision targets either **a group** (every sample in it) or **one sample**.
Both are project-scoped.

### 10.3 Most recent wins (D5)

A sample's human value for a field is the **most recent human decision that
covers it**, whether that decision was made on its group or on the sample
itself, by an owner or by an admin. A later group decision therefore replaces
an earlier per-sample override; a later per-sample override replaces the group
value for that sample only. "Most recent wins" is between human decisions on a
project; a change to a site-wide rule is not one, and is handled by §9.4.

### 10.4 Decisions are a log

Decisions are append-only. The current value is computed from the log; every
earlier value, who set it and when stays readable. Undo is a new decision.

### 10.5 Review actions

| Action | Effect |
| --- | --- |
| **Confirm** | Asserts: *the evidence shown meets the labeling criteria (§5.7) for this value.* It asserts nothing beyond the shown evidence. The current value becomes `source: human`, `review_status: confirmed`, `basis: shown_evidence`, and the decision records the source and rule the value came from. |
| **Correct** | Asserts: *this value's criteria are met*, on a stated **basis** — `shown_evidence` (the evidence shown supports a different value), `publication`, `lab_records`, or `other`. A note is required for `other` and optional otherwise. Sets a value (or an absence reason) by hand: `source: human`, `review_status: corrected`. |
| **Flag** | "This is wrong", with a required comment. `review_status: flagged`; the group is queued for the curator with the comment (§11.3). The value stays visible, marked flagged, until replaced. |
| **Revert** | A new decision restoring the previous value. |

The curator **never** replaces a `human` value, under any circumstance
(§17, I3). Flagging a human value hands it back: the flag is itself the human
decision that the value is no longer endorsed.

---

## 11 · Curator contract

### 11.1 Interface

The curator is any backend implementing one operation:

**Request** — a batch of groups, each with:
- the field to harmonize;
- the group's evidence tuple, with column names;
- up to N example sample names (names only);
- the project's name and description;
- for flagged groups, the reviewer's comment and the previous answer;
- the candidate vocabulary terms the deterministic stages shortlisted, and access
  to vocabulary lookup (search OncoTree by name/code/NCIt; resolve a Cellosaurus
  ID);
- the labeling criteria (§5.7) for every candidate term and for the field's
  enumeration values, at a stated criteria version. The curator must apply
  them as a human reviewer would, and its rationale names the criterion met.

**Response** — per group: a value (or an absence reason), a confidence of
`medium` or `low`, a one-line rationale citing the evidence used, and whether
the answer rests on project-level inference only (which makes the source
`project_inference`).

### 11.2 Validation

The worker, not the backend, validates every answer: a code must exist in the
pinned release, an enumeration value must be in its list, numbers must be in
range. An invalid answer is discarded and the group stays unresolved; it is
never stored as a value.

### 11.3 Flag round-trip

A flag sends the group back with the reviewer's comment and the rejected
answer. The new answer is stored as `curator` with `review_status: reproposed`
and shown to reviewers as such. It never becomes `human` without a human
action.

### 11.4 What leaves the site

Only the request contents of §11.1, and only for projects allowed by §13.
Never: genomic data, files, project members, user accounts, or anything from a
private project. Every request and response is stored with the group, so any
value can be traced to the exact exchange that produced it.

### 11.5 Backend independence

The contract is the same whether the backend is a direct LLM API call, a
self-hosted Biomni instance or anything else. Answers are cached by a hash of
the request; re-running unchanged groups costs nothing and is reproducible. The
backend identity and model are recorded on each answer.

---

## 12 · The worker

- A **separate process** (D7). The web application only enqueues work and reads
  results; no page request waits on the curator.
- **Triggers:** a project's content changed or it became harmonizable
  (`project_events.project_changed`, `project_content_changed`,
  `project_visibility_changed` — the existing single place that says a project
  changed); a flag; a synonym, crosswalk or vocabulary-release change; an admin
  "re-run" on a project.
- **Idempotent.** Re-running a project with nothing changed changes nothing.
- **Failure never propagates.** A curator outage, quota error or invalid answer
  leaves groups unresolved and logged; it never fails an upload, a reindex or a
  page. This mirrors the rule `project_events.py` already applies to the
  feature index.
- **Bounded.** A per-run cap on curator calls and a site-wide rate limit, both
  configurable; a project with more pending groups than the cap is completed
  over several runs.
- **Observable.** Per project: groups pending, resolved by each source, flagged,
  failed; last run and its outcome. Admins can see this without reading logs.

---

## 13 · Visibility

- **Harmonizable** means `public` or `hidden_public`, `current`, not soft-deleted,
  not a version tombstone (D3).
- A **private** project is never sent to the curator, never contributes to
  harmonized search, facets or statistics, and has no review interface.
- **Becoming private:** its harmonized values leave search, facets, statistics
  and API output at the same time its rows' visibility changes. Its stored human
  decisions are retained, inert, so that becoming public again restores them.
  No curator call is made for it while private.
- **Becoming harmonizable** enqueues it (§12).
- **hidden_public** keeps its existing meaning: harmonized fields on its rows
  are visible exactly where its rows already are, and nowhere else. Its counts go
  into the statistics bucket it already goes into today.
- **Owners** see, in the review interface, only their own projects' groups. A
  synonym an admin created from another project's data does not reveal which
  project that was.

---

## 14 · Storage roles

Implementation chooses names and layout; these roles and their authority are
fixed.

| Role | Authoritative? | Contents |
| --- | --- | --- |
| Vocabulary store | yes (versioned) | Pinned OncoTree release(s), crosswalk, synonyms, search groupings, labeling criteria (§5.7), NCIt definitions for OncoTree nodes, Cellosaurus cache |
| Decision log | yes (append-only) | Human decisions (§10) |
| Curator answers | yes (append-only) | Requests, responses, validation result, backend/model (§11) |
| Group registry | derived | Groups per project lineage and their current state |
| Seen set | yes (append-only) | Per lineage, every sample name and group key ever harmonized, with when first seen; decides "not seen before" for email (§16.1). Authoritative because it cannot be rebuilt once samples are removed |
| Harmonized sample values | derived | The §7 record per sample per field |
| Feature-index rows | derived | A `harmonized` sub-document on each row, so search filters on it (requires a `SCHEMA_VERSION` bump in `feature_index.py`) |
| Work queue | operational | Pending worker jobs |

Every derived store must be rebuildable from the authoritative stores plus the
project documents, and a check comparable to
`manage.py rebuild_feature_index --check` must report drift.

---

## 15 · Consumers

### 15.1 Search

- A **cancer-type filter by OncoTree node**, matching the node and all its
  descendants: choosing `LUNG` returns LUAD, LUSC, SCLC… and samples mapped to
  `LUNG` itself. Typed text resolves to nodes through names, codes, synonyms,
  legacy labels (§5.2) and search groupings (§5.3), and shows what it resolved
  to. Groupings are offered beside the tree: GBM appears as one choice, with
  `GB` and `ASTR` reachable under it.
- Filters for `specimen_kind`, `disease_state`, `species`, `sex`, an age range
  (min/max years), pediatric / non-pediatric (§6.8), and "has OS data".
- An option to restrict to a minimum confidence (e.g. exclude
  `project_inference`).
- **Existing raw-text filters keep working unchanged**; harmonized filters are
  additional.
- AGENTS.md requires `views.search_results()` and `search.perform_search()` to
  stay aligned; any harmonized filter offered in the UI must give the same answer
  on every search path that serves it, or be served by only one path.

### 15.2 Site statistics

The cancer-type chart counts **harmonized** values at a chosen OncoTree level
(main type or tissue), with an explicit **"Not harmonized"** slice for samples
without a value. Private projects contribute nothing harmonized.

### 15.3 Project and sample pages

- Sample page: harmonized values beside the raw metadata, each with its source
  and confidence visible on hover or in a column; low-confidence and
  project-inferred values visibly marked.
- Project page: a coverage summary (share of samples harmonized per field, by
  source) and, for owners and admins, the entry to review.
- The metadata TSV download (`project_metadata_download`) gains the harmonized
  values as additional, clearly prefixed columns; existing columns are unchanged.

### 15.4 API

- Sample responses gain a `harmonized` object: per field, the §7 record minus
  `decided_by` (who decided is not public; that a human decided is).
- New filter parameters mirroring §15.1, including OncoTree node with
  descendant matching and minimum confidence.
- `/api/v1/features/facets/` gains harmonized facets.
- The vocabulary release in use is reported, so a consumer can cite it.
- Existing parameters and fields keep their current meaning (`cancer_type`
  etc. remain the raw values).
- The OpenAPI schema and `llms.txt` describe the new fields.

---

## 16 · Review interface

Required properties, for projects with thousands of samples:

1. **Grouped by default.** One row per group (§9.1): evidence values, proposed
   value, source, confidence, sample count, status. A project like Hartwig is
   reviewed as ~40 cancer-type rows, not 4,170.
2. **One tab per field**, each with a count of groups needing attention.
3. **Needs-attention first.** Default order: flagged, conflicting, reproposed,
   rule changed, low confidence, medium, high, human. Filterable by status, source and
   confidence.
4. **Server-side paging, sorting and filtering.** Nothing loads a whole
   project's samples into the browser.
5. **Bulk actions:** select many groups → confirm / correct to one value / flag
   with one comment.
6. **Drill-down:** expand a group to its samples, paged, for per-sample
   overrides.
7. **Value picker:** OncoTree as a searchable tree (name, code, synonyms),
   enumerations as lists; it cannot produce a value outside the vocabulary.
   Every value in it shows its labeling criteria (§5.7) — tree path,
   definition, what qualifies, what does not, what to use instead — before it
   is chosen, not only in a help page.
8. **Criteria at the point of decision.** The confirm and correct dialogs put
   the chosen value's criteria beside the group's evidence, so the reviewer
   checks one against the other, and state in one sentence what the action
   asserts (§10.5). Correct asks for its basis. A bulk confirm lists each
   distinct value being confirmed with its criteria. A `rule_changed` group
   shows old value, new rule value, the rule change and both values' criteria.
9. **Numeric fields** (age, survival) are reviewed as distributions and
   outlier lists — negative days, age > 110, OS beyond a plausible horizon,
   apparent unit mismatches — not row by row.
10. **Rationale and evidence visible** for every non-human value, including the
   curator's rationale and, for admins, the stored exchange.
11. **Offline round-trip:** download the group table as CSV, edit the value
    column, upload it back as decisions, with a preview of what will change
    before anything is applied. The upload records a basis for every
    correction it makes, as the dialog does.
12. **History** per group and per sample, from the decision log (§10.4).

### 16.1 Email notifications (D13, D14, D16)

- **What starts one.** Only four events:
  1. a project becomes harmonizable for the first time — created public or
     hidden-public, or made so later;
  2. an update to it — re-aggregation, a new metadata sheet, added samples —
     that brings something **not seen before** in its lineage: a sample name
     the lineage has never had, or evidence that forms a group (§9.1) the
     lineage has never had;
  3. a **flag** (§10.5): a logged-in reviewer flags one or more groups or
     samples, and the curator re-proposes. One flagging action — a single flag
     or one bulk flag of many groups — is one event;
  4. a **rule change** (§9.4): one published version of the synonyms, crosswalk
     or vocabulary release puts any of the project's confirmed groups back into
     review as `rule_changed`. One publication is one event per project,
     however many entries it changed.
  An update that re-presents only known samples and known evidence — the usual
  re-aggregation — starts nothing, even though the worker re-checks it.
- **When it is sent.** Once, when the work that event started is **finished**:
  every group it created or changed has been through the deterministic stages
  and the curator, or the curator has definitively failed on it. Not when the
  event happens, and not partway through a project that takes several worker
  runs (§12, *Bounded*). If nothing ends up needing attention, no email is sent.
  For a rule change, "finished" means the worker has re-evaluated every group
  of the project the publication touched.
  For a flag, "finished" means every flagged item has a re-proposal or a
  definitive curator failure; a failure is reported in the email rather than
  silently leaving the item flagged.
- **Who.**
  - Events 1, 2 and 4: the project's owners/members. Admins are not emailed for these;
    they see the site-wide queue (§12, *Observable*) instead.
  - Event 3: the project's owners/members **and the site admins**
    (`is_staff`). The flagger is necessarily one of these (§10.1) and receives
    one copy.
- **What.** One message per event: the project name, what was new (samples,
  groups) or, for a flag, who flagged, how many items and how many were
  re-proposed, or, for a rule change, which rules changed and how many groups
  went back to review; counts by field and status; and a link straight to the project's
  review interface, opened on the needs-attention view (for a flag, on the
  `reproposed` or `rule_changed` items). It contains no metadata values, so it is safe to forward.
- **Opt-out.** A new key in `user_preferences`, beside the existing
  `onProjectUpdate`, default on, honoured exactly as `onProjectUpdate` is in
  `user_preferences.py`. The email says how to turn it off.
- **Sent by the worker**, through the site's existing `EmailMessage` path and
  `reply_to()`. A send failure is logged and retried by the worker; it never
  blocks harmonization (I5).
- **Private projects are never emailed about** (I2), since they are never
  harmonized.

---

## 17 · Invariants

| # | Must hold |
| --- | --- |
| I1 | Raw metadata is never modified by harmonization. Every current reader of `extra_metadata_from_csv`, `Cancer_type`, `Sample_type`, `Tissue_of_origin` and `Sample_metadata_JSON` sees what it sees today. |
| I2 | Nothing from a private project is sent to the curator or appears in harmonized search, facets, statistics or API output. |
| I3 | The curator never overwrites a `human` value. |
| I4 | Every stored harmonized value has a §7 record, and every vocabulary value exists in the release recorded on it. |
| I5 | Harmonization failure never fails an upload, a reindex, a page or an API call. |
| I6 | Derived stores are rebuildable from authoritative ones, and drift is detectable. |
| I7 | Model identity (cell line / PDX name and IDs) is never dropped or replaced by a cancer type. |
| I8 | A value is never more specific than its evidence (§5.1). A legacy label is upgraded to a post-2021 code only on cited evidence (§5.2), and the legacy label is kept. |
| I9 | Version-deleted tombstones are never harmonized; decisions survive re-aggregation only through `version_chain_id`. |

---

## 18 · Acceptance criteria

Against a snapshot of the public corpus equivalent to §2:

1. "Pancreas Adenocarcinoma", "Pancreatic Adenocarcinoma" and the TCGA code
   `PAAD` all resolve to `PAAD` without the curator.
2. Searching the **GBM** grouping returns TCGA's glioblastoma samples and every
   `GB` sample. Searching `GB` returns IDH-wildtype glioblastomas only, never a
   sample whose only evidence is a pre-2021 label. A pre-2021 GBM sample with
   IDH-mutant evidence is found by both **GBM** and `ASTR`.
3. Choosing the `LUNG` node returns samples labelled "Lung", "Lung
   Adenocarcinoma", "Lung Squamous Cell Carcinoma" and "Non-Small Cell Lung
   Cancer (NSCLC), Adenocarcinoma".
4. Every DepMap/CCLE sample with an `RRID` has `model.cellosaurus_id`, keeps its
   submitted line name, and has `specimen_kind: cell_line`.
5. The Medulloblastoma subsets receive `cancer_type` `MBL` with
   `source: project_inference`, `confidence: low`.
6. Hartwig samples get `disease_state: metastatic`; GLASS `R1`/`R2` samples get
   `recurrent`; HCMI "Metastatic | Primary" samples are `conflicting` and
   queued.
7. TCGA samples with `days_to_death` get `overall_survival.event: true`; those
   with only follow-up get `false`; cell lines get `not_applicable`.
8. Making a project private removes its harmonized values from search, facets,
   statistics and API output, and a curator stub records no request containing
   its data before or after.
9. A human correction on a group survives re-aggregation of the project when
   the group's evidence is unchanged, and a curator re-run does not alter it.
10. The deterministic stages alone (§8, 2–3) report, per field, how many
    samples they resolved; that report is the baseline the curator is measured
    against.
11. A sample with `age.years` 16 is `pediatric: true`; HCMI `Pediatrics` →
    `true`, `Adults` → `false`, `AYA` → unset; an age range filter of 40–60
    returns samples by `years` only.
12. Creating a public project produces exactly one email to its owners, sent
    after its curation finishes, even when that takes several worker runs.
    Re-aggregating it with the same samples and metadata sends none. Adding one
    new sample, or a sheet with a value not seen before, sends one. An owner who
    opted out receives none, and no email names a metadata value.
13. An owner bulk-flags 20 groups: once all 20 have a re-proposal or a
    definitive failure, exactly one email goes to the project's owners and the
    site admins, linking to the `reproposed` items. An anonymous user cannot
    flag.
14. The §9.4 example holds: after "Glioblastoma" → `GB` is republished as →
    `ADIFG`, a project whose group was *corrected* to `GB` keeps `GB` and gets no
    email; a project whose group was *confirmed* from the rule keeps `GB`, shows
    `rule_changed`, and gets one email; a project with no human decision
    becomes `ADIFG`.
15. Every value offered by the picker displays its criteria; the confirm and
    correct dialogs show the criteria beside the evidence; a correction cannot
    be saved without a basis, nor with basis `other` without a note; every
    stored decision and curator answer carries a criteria version, and the
    curator request for a group contains the criteria at that version.

---

## 19 · Open questions

1. **Curator backend and model** (D6) — direct LLM API, self-hosted Biomni, or
   other. Affects cost and hosting only, not this spec.
