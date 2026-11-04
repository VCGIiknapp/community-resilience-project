# Vermont Community Resilience Index (VT CRI) — Project Summary

*Distilled overview prepared from the project's own methodology, user guide, variable appendices, and the current production ETL script. Intended as a single orientation document for anyone inheriting or evaluating this project.*

---

## 1. What the project is

The **Vermont Community Resilience Index (VT CRI)** is an interactive health-equity planning tool, delivered as a public-facing **ArcGIS Online Experience Builder** application. It helps public health officials examine one question:

> *What is the capacity of communities throughout Vermont to mitigate and recover from a public health crisis or health emergency?*

"Community resilience" here is defined as **the socioeconomic and housing capacity of a population to mitigate and recover from a public health crisis or personal health emergency in a manner that sustains physical, social, and mental health.**

The application has **two components**:

| Component | What it shows | Statistic |
|-----------|---------------|-----------|
| **1. Composite Index** *(core)* | A single 0–100 resilience score per municipality | Geometric mean of the 11 indicator percentile ranks |
| **2. Indicator Displays** | Spatial distribution of each individual indicator, plus race/ethnicity disaggregation | Standard mean difference (−1 to 1) |

There is now **one composite index** built from all 11 indicators (the former Socioeconomic and Housing sub-indices were retired on 2026-11-04). Everything is published at a single geography unit — **municipality** (255/256 VT towns & cities) — and recomputed for multiple **area frames**: statewide, 14 counties, 12 VDH districts, and 13 hospital service areas. *(Census tract was retired as a published unit on 2026-11-04; block group was removed on 2026-11-03. Municipalities only going forward.)*

## 2. Purpose & justification

- **Audience:** Vermont Department of Health officials and local public-health planners.
- **Need it fills:** Translates dozens of Census variables into one defensible, comparable measure of how well a community can absorb and recover from a health shock, surfaced at a granularity (town) that statewide averages hide.
- **Methodological lineage:** Follows the **CDC Social Vulnerability Index** precedent (8 of 11 indicators are CDC-influenced) and the **OECD Handbook on Constructing Composite Indicators**. The design is documented against peer-reviewed literature (Cutter 1996, Flanagan 2011, Beccari 2016, Rufat 2021, etc.).
- **Equity framing:** Race/ethnicity disaggregation (8 of 11 indicators) is a deliberate health-equity feature, reported at the area-frame level to protect small-population groups.

## 3. The 11 indicators

All 11 indicators feed the **single composite index** and are **equally weighted** (the most common and most defensible default per Beccari 2016). Each is built from **ACS 5-year estimates** pulled via the Census API. The two conceptual domains below are descriptive only — they are *no longer* separate sub-indices.

**Socioeconomic domain (7)**
1. Supportive Income — % population with income > 200% of Federal poverty level *(C17002)*
2. Employment — % adult population employed *(B23001 / DP03)*
3. Without Public Assistance — % households not receiving public assistance *(B19058)*
4. Median Household Income — inflation-adjusted *(B19013)*
5. High School Education — % adults 25+ with HS diploma+ *(B15001 / DP02)*
6. Health Insurance — % population insured *(B27010 / DP03)*
7. Manageable Housing Costs — % housing units w/ costs < 30% of income *(B25070 + B25095 / DP04)*

**Housing domain (4)**
8. Broadband Internet — % households w/ computer + broadband *(B28005 / DP02)*
9. Owner-Occupied Homes — % owner-occupied units *(B25003)*
10. English Proficiency — % population speaking English "well" or better *(B16005)*
11. Vehicle Access — % housing units with a vehicle *(B25044 / DP04)*

Variable-level detail (table IDs, numerators/denominators, available years 2008–2012 → 2019–2023) is in `dataAppendix/IndexVariables_Updated.xlsx` and `DisaggregatedVariables.xlsx`.

## 4. Methodology in four steps

1. **Identify variables** — map each indicator to specific ACS table variables (numerators/denominators).
2. **Preprocess indicators** — aggregate variables → a normalized rate per municipality (numerator ÷ total population) → convert to **percentile rank (0–100)**, computed *separately per area frame* (because percentile rank is relative to the units included).
3. **Calculate the index** — combine the 11 percentile ranks with a **geometric mean** (multiply, take the *n*th root), then re-rank 0–100. Repeated for each area frame. There is one index — no per-theme variants.
4. **Postprocess** — classify into 4 quantile classes: Low / Medium-Low / Medium-High / High Resilience, symbolized on the maps.

**Why geometric mean (the key methodological choice):** It is **non-compensatory** — a high score on one indicator does *not* cancel out a low score on another. A community weak in several areas cannot hide behind one strong indicator, and improvements to low-scoring indicators move the needle more. This better reflects whole-population resilience than a simple average would.

**Indicator Displays** use **standard mean difference**: `(area-frame mean − municipality value) ÷ area-frame standard deviation`, giving a −1 to 1 value showing how far above/below the local mean each place sits. For the District and HSA area frames (which have no ACS geography of their own), the area-frame mean is a **population-weighted** aggregate of the member municipalities — rates use Σ(numerator)/Σ(denominator); median household income uses a household-count-weighted mean.

## 5. ETL overview

**Engine:** [src/CommunityResilienceIndexCurrentDev.py](src/CommunityResilienceIndexCurrentDev.py)
```
Census ACS5 API  ──(requests, by variable ID)──►  per-indicator DataFrames
        │                                                  │
VDH municipality crosswalk (SDE geodatabase) ──► county / VDH district / HSA / statewide rollups
        │                                                  │
        ▼                                                  ▼
  Normalize (num ÷ denom)  ►  Percentile rank (pandas .rank(pct=True)*100)
        │                          per municipality × per area frame
        ▼
  Geometric mean of the 11 ranks (scipy gmean)  ►  re-rank 0–100  ►  quantile class
        │
        ▼
  Excel feeder tables per 5-year period  ──►  ArcGIS Pro (join to municipality layer)
        │                                                  (CommunityResilienceIndex.aprx)
        ▼
  ArcGIS Online  ──►  Experience Builder application
```

- **Inputs:** Census API key (`config/CensusAPI.txt`, git-ignored); municipality crosswalk feature class (path in `config/network_paths.json`, git-ignored — see `config/network_paths.example.json`).
- **Geographies computed:** county subdivision (municipality) is the published unit; county and statewide are computed as ACS reference aggregates. Area frames: statewide, county, VDH district, hospital service area. *(Census tract and block group removed from the active pipeline; municipalities only.)*
- **Outputs:** written to `data/output/<period>/` (e.g. `2019-2023/`) as Excel tables per 5-year window:
  - `townsIndexGeometric<period>.xlsx` — the single composite index per municipality × area frame
  - `townsIndicators<period>.xlsx` — per-indicator value, area-frame mean, percentile, and SMD
  - `townsUnpopulated<period>.xlsx` — zero/null-population municipalities
  - `statewideDisaggregated` / `countiesDisaggregated` / `districtsDisaggregated` / `hospitalsDisaggregated` — race/ethnicity disaggregation
- **Final clean deliverables:** `dataAppendix/` holds the published methodology, user guide, and variable appendices.

## 6. Core strengths (what to protect)

1. **Sound, well-documented methodology.** The composite-index design is grounded in CDC SVI precedent and the OECD Handbook, with an explicit, defensible rationale for every major choice (equal weighting, geometric mean, percentile ranking, area frames). The `dataAppendix` methodology + user guide are genuinely strong and publication-ready.
2. **The composite index itself** — one index × multiple area frames × municipalities — is a legitimately useful, flexible analytic product.
3. **Health-equity disaggregation.** Race/ethnicity breakdowns with small-population protection is a meaningful differentiator over the base CDC SVI.
4. **Full reproducibility from source.** Everything derives from documented ACS variables via API; the variable appendices make the pipeline auditable end-to-end.
5. **Multi-year coverage** (2008–2012 through 2019–2023) enables trend analysis.
6. **A clean deliverable layer already exists** in `dataAppendix/` — the distillation target is already partly built.

## 7. Risks / things to verify before the next run

- **Live validation not yet run for the current refactor.** The script now runs top-to-bottom (dict-driven, with helper functions; no orphaned legacy section). It has been syntax-checked but **not** executed end-to-end here, because it requires `arcpy`, the Census API, and the SDE crosswalk. Before publishing, run it against live ACS and **diff the outputs against a prior published period** (`data/output/2018-2022/`). Statewide and county values should be identical to the prior method; District/HSA means and SMDs will have **moved** because they are now population-weighted rather than an unweighted mean of town rates.
- **Area-frame weighting change (2026-11-04).** District/HSA area-frame means are population-weighted (rates: Σnum/Σdenom; median income: household-count-weighted). The `2019-2023` outputs reflect this; the older `2018-2022` tables in the repo were produced by the prior (unweighted) method, so the two periods are not directly comparable until `2018-2022` is re-run.
- **Hardcoded absolute scratch paths.** Some intermediate/debug dumps still write to machine-specific locations; the published outputs are now `__file__`-relative under `data/output/<period>/`. Verify no stray absolute path remains before handing the pipeline to another operator.
- **Year labels are embedded in filenames** but are now derived from the single `PERIOD_END_YEAR` run parameter at the top of the script — set that one value per run to avoid mismatched output windows.

## 8. Recommended target structure

```
CommunityResilienceIndex/
├── README.md                     ← this summary
├── src/CommunityResilienceIndexCurrentDev.py   ← the live pipeline (functions, params)
├── config/                       ← API key (git-ignored), network paths (git-ignored), example template
├── data/
│   └── output/<period>/          ← published feeder tables (municipality only)
├── dataAppendix/                 ← methodology, user guide, variable appendices
├── gis/                          ← .aprx, .atbx, .gdb, published layers (git-ignored)
└── references/                   ← literature PDFs (git-ignored)
```

---

*Sources: `dataAppendix/CommunityResilienceMethodology_updated.docx`, `summarizedUserGuide_updated.docx`, `IndexVariables_Updated.xlsx`, `DisaggregatedVariables.xlsx`, and `src/CommunityResilienceIndexCurrentDev.py`.*
