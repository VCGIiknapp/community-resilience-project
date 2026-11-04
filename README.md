# Vermont Community Resilience Index (VT CRI) — Project Summary

*Distilled overview prepared from the project's own methodology, user guide, variable appendices, and the current production ETL script. Intended as a single orientation document for anyone inheriting or evaluating this project.*

---

## 1. What the project is

The **Vermont Community Resilience Index (VT CRI)** is an interactive health-equity planning tool, delivered as a public-facing **ArcGIS Online Experience Builder** application. It helps public health officials examine one question:

> *What is the capacity of communities throughout Vermont to mitigate and recover from a public health crisis or health emergency?*

"Community resilience" here is defined as **the socioeconomic and housing capacity of a population to mitigate and recover from a public health crisis or personal health emergency in a manner that sustains physical, social, and mental health.**

The application has **three components**:

| Component | What it shows | Statistic |
|-----------|---------------|-----------|
| **1. Composite Index** *(core)* | A single 0–100 resilience score per geography, in 3 themes (Overall, Socioeconomic, Housing) | Geometric mean of indicator percentile ranks |
| **2. Indicator Displays** | Spatial distribution of each individual indicator, plus race/ethnicity disaggregation | Standard mean difference (−1 to 1) |
| **3. Population Reference Maps** | Contextual population layers (not inputs to the index) | Raw counts/rates |

Everything is published at two geography units — **Census tract** and **municipality** (255/256 VT towns & cities) — and recomputed for multiple **area frames**: statewide, 14 counties, 12 VDH districts, and 13 hospital service areas.

## 2. Purpose & justification

- **Audience:** Vermont Department of Health officials and local public-health planners.
- **Need it fills:** Translates dozens of Census variables into one defensible, comparable measure of how well a community can absorb and recover from a health shock, surfaced at a granularity (tract/town) that statewide averages hide.
- **Methodological lineage:** Follows the **CDC Social Vulnerability Index** precedent (8 of 11 indicators are CDC-influenced) and the **OECD Handbook on Constructing Composite Indicators**. The design is documented against peer-reviewed literature (Cutter 1996, Flanagan 2011, Beccari 2016, Rufat 2021, etc.).
- **Equity framing:** Race/ethnicity disaggregation (8 of 11 indicators) is a deliberate health-equity feature, reported at the area-frame level to protect small-population groups.

## 3. The 11 indicators

All indicators are **equally weighted** (the most common and most defensible default per Beccari 2016). Each is built from **ACS 5-year estimates** pulled via the Census API.

**Socioeconomic Theme (7)**
1. Supportive Income — % population with income > 200% of Federal poverty level *(C17002)*
2. Employment — % adult population employed *(B23001 / DP03)*
3. Without Public Assistance — % households not receiving public assistance *(B19058)*
4. Median Household Income — inflation-adjusted *(B19013)*
5. High School Education — % adults 25+ with HS diploma+ *(B15001 / DP02)*
6. Health Insurance — % population insured *(B27010 / DP03)*
7. Manageable Housing Costs — % housing units w/ costs < 30% of income *(B25070 + B25095 / DP04)*

**Housing Theme (4)**
8. Broadband Internet — % households w/ computer + broadband *(B28005 / DP02)*
9. Owner-Occupied Homes — % owner-occupied units *(B25003)*
10. English Proficiency — % population speaking English "well" or better *(B16005)*
11. Vehicle Access — % housing units with a vehicle *(B25044 / DP04)*

The **Overall Theme** = all 11. Variable-level detail (table IDs, numerators/denominators, available years 2008–2012 → 2018–2022) is in `dataAppendix/IndexVariables_Updated.xlsx` and `DisaggregatedVariables.xlsx`.

## 4. Methodology in four steps

1. **Identify variables** — map each indicator to specific ACS table variables (numerators/denominators).
2. **Preprocess indicators** — aggregate variables → a normalized rate per geography (numerator ÷ total population) → convert to **percentile rank (0–100)**, computed *separately per geography unit and per area frame* (because percentile rank is relative to the units included).
3. **Calculate index** — combine the 11 percentile ranks with a **geometric mean** (multiply, take the *n*th root), then re-rank 0–100. Repeated for each theme and each area frame.
4. **Postprocess** — classify into 4 quantile classes: Low / Medium-Low / Medium-High / High Resilience, symbolized on the maps.

**Why geometric mean (the key methodological choice):** It is **non-compensatory** — a high score on one indicator does *not* cancel out a low score on another. A community weak in several areas cannot hide behind one strong indicator, and improvements to low-scoring indicators move the needle more. This better reflects whole-population resilience than a simple average would.

**Indicator Displays** use **standard mean difference**: `(geography value − area-frame mean) ÷ area-frame standard deviation`, giving a −1 to 1 value showing how far above/below the local mean each place sits.

## 5. ETL overview

**Engine:** [src/CommunityResilienceIndexCurrentDev.py](src/CommunityResilienceIndexCurrentDev.py)
```
Census ACS5 API  ──(requests, by variable ID)──►  per-indicator DataFrames
        │                                                  │
VDH municipality crosswalk (SDE geodatabase) ──► county / VDH district / HSA / statewide rollups
        │                                                  │
        ▼                                                  ▼
  Normalize (num ÷ denom)  ►  Percentile rank (pandas .rank(pct=True)*100)
        │                          per geography × per area frame
        ▼
  Geometric mean per theme (scipy gmean)  ►  re-rank 0–100  ►  quantile class
        │
        ▼
  ~20 Excel/CSV feeder tables per 5-year period  ──►  ArcGIS Pro (join to tract/town layers)
        │                                                  (CommunityResilienceIndex.aprx)
        ▼
  ArcGIS Online  ──►  Experience Builder application
```

- **Inputs:** Census API key (`CensusAPI.txt`); municipality crosswalk table (`VDH_VT_MunicipalitiesCrosswalk` in SDE).
- **Geographies computed:** tract, county subdivision (municipality), county, statewide. *(Block group was removed from the active pipeline on 2026-11-03 — tracts and municipalities only. Area frames unchanged: statewide, county, VDH district, hospital service area.)*
- **Outputs:** written to `python/dashboardPython/` as stacked Excel tables per 5-year window (`2008-2012` … `2018-2022`, plus `2010s`/`2020s` combined): indicator tables, index (geometric) tables, disaggregated demographics (county/district/hospital/statewide), population references, and "unpopulated"/suppressed tables.
- **Final clean deliverables:** `dataAppendix/` holds the published methodology, user guide, variable appendices, and the index CSVs (`TractsIndexData.csv`, `MunicipalitiesIndexData.csv`, `*Indicators.csv`).

## 6. Core strengths (what to protect)

1. **Sound, well-documented methodology.** The composite-index design is grounded in CDC SVI precedent and the OECD Handbook, with an explicit, defensible rationale for every major choice (equal weighting, geometric mean, percentile ranking, area frames). The `dataAppendix` methodology + user guide are genuinely strong and publication-ready.
2. **The composite index itself** — three themes × multiple area frames × two geographies — is a legitimately useful, flexible analytic product.
3. **Health-equity disaggregation.** Race/ethnicity breakdowns with small-population protection is a meaningful differentiator over the base CDC SVI.
4. **Full reproducibility from source.** Everything derives from documented ACS variables via API; the variable appendices make the pipeline auditable end-to-end.
5. **Multi-year coverage** (2008–2012 through 2018–2022) enables trend analysis.
6. **A clean deliverable layer already exists** in `dataAppendix/` — the distillation target is already partly built.

## 7. Components that can be cut or consolidated

The project is ~1,700 files (excluding the Python venv), **809 of them `.xlsx`** — overwhelmingly intermediate/dev artifacts. The sprawl, not the index, is the problem. Recommended cuts, roughly by confidence:

**Cut / archive outright**
- **`python/venv/`** — a committed virtual environment (~tens of MB). Never keep in a project; recreate from `requirements.txt`.
- **Dead code generations in `python/`** — `cri.py`, `cri2.0.py`, `cri2017–2020.py`, `svi.py`, `svi1.0.py`, `*stacked.py` per year, `export2017–2020/`, `manual2017-2019/`, `backup.py`, `scratch.py`, `old_lowerinc.py`, `errorTEST.py`, `*TEST*`. These are superseded by the single current dev script. Keep **one** canonical script.
- **Hundreds of intermediate `.xlsx`/`.csv`** in `python/` and `python/dashboardPython/` that are regenerable ETL outputs (raw/AGOformat/complete/suppressed variants per year). Keep only the current published period's feeder tables; regenerate the rest on demand.
- **Duplicate/variant images & legends** at the root (multiple `*Indicator_Legend.png/jpg`, `oval*Transparant*`, `circleRedTransparant`, splash-screen JPGs) — keep one copy of each final asset.
- **One-off exploration folders** — `multivarate analysis/`, `r_test/`, `moeExploration/`, `pdf2png/`, `criDataStackedManual/`, `physical_vulnerability_experiments.xls`, `stdev_normdist.xlsx` — research scaffolding, not part of the production pipeline. Archive.
- **Scattered meeting notes** at root (`amyRedman*`, `weeklyMeetingDan*`, `*Sept19.docx`, `_ToDoList*`, `toDoNotes.txt`, `notes.docx`) — move to a single `/archive/meetings/` folder.

**Consolidate (don't delete the content, dedupe the copies)**
- **Methodology/user-guide versions** — `dataAppendix/` alone has `methodology.docx`, `CommunityResilienceMethodology.docx/.pdf`, `..._updated.docx`, plus `summarizedUserGuide` × 3. Keep the single latest `_updated` of each; archive the rest.
- **Reference literature PDFs** (OECD Handbook is ~148 MB alone, Cutter, Rufat ×2, Becker, etc.) — valuable but belongs in a `/references/` folder, not the working root.
- **Vendored ACS downloads** (`ACS downloads/`) — regenerable via API; keep zipped or drop.

**Scope decision — resolved (2026-11-03)**
- **Block-group geography has been removed** from the active pipeline. Geographies going forward are **Census tracts and municipalities only**; indices are still calculated at the four area frames (statewide, county, VDH district, hospital service area). Block-group output files were dropped from `data/output/`, and all live block-group references were stripped from `src/CommunityResilienceIndexCurrentDev.py` (the active loop now iterates `['tract', 'county subdivision']`). Block-group references that remain in the file are confined to the orphaned legacy section — see §8.

## 8. Risks / things to verify before the next run

These are observations from the **current dev script** (`CommunityResilienceIndexCurrentDev.py`) and should be confirmed against the published outputs:

- **The script is a 7,260-line monolith with no functions**, and only the top ~511 lines are live. The active dict-driven "Main Loop" acquires ACS data, builds `geographyDataframes`, computes thematic geometric means, and writes `thematicDataFrame` to `D:/ScratchSpace/geog_data_output.csv`. **Everything below ~line 520 is orphaned legacy code**: it references `tractsPercentile`, `incomeTownsDataFrame`, `incomeBlockGroupsDataFrame`, etc., none of which are created in this file (first occurrence = first use), so run top-to-bottom it would `NameError` at line 532. The dashboard feeder tables in `data/output/` were produced by the *archived* prior versions of the script, not this one. The remaining ~230 block-group references all live in this dead section. **Decision needed:** wire the legacy index/export logic to `thematicDataFrame` (the real refactor), or delete the orphaned section outright. Until then, this file does not by itself regenerate the published outputs.
- **Possible column-list errors in the index definitions** (worth auditing): in the statewide/tract theme lists, `ownerOccupiedHousingStatewidePercentile` appears **twice** and `vehicleAccessStatewidePercentile` appears **absent** from `stateOverall`/`stateHousing`; and `districtHousing` / `hospitalHousing` appear to reference each other's percentile columns (swapped). If these are live, the statewide Overall/Housing index may be dropping Vehicle Access and double-counting Owner-Occupied. Verify against intended methodology before the next publication.
- **Hardcoded absolute paths** (`D:/ScratchSpace/...`, `C:/Users/Ian.Knapp/OneDrive - .../dashboardPython/...`, UNC SDE paths) make the pipeline machine-bound and single-owner. Parameterize for continuity.
- **Year labels are embedded in filenames**, not derived from a single `year` parameter — easy to mismatch output windows.

## 9. Recommended target structure

```
CommunityResilienceIndex/
├── README.md                     ← this summary, trimmed
├── src/cri_pipeline.py           ← ONE refactored script (functions, params)
├── config/                       ← API key ref, year, paths, crosswalk
├── data/
│   ├── reference/                ← crosswalk, ACS variable appendices
│   └── output/<period>/          ← current published feeder tables only
├── docs/                         ← methodology, user guide (latest only), appendices
├── gis/                          ← .aprx, .atbx, .gdb, published layers
├── references/                   ← literature PDFs
└── archive/                      ← everything cut above, zipped, out of the way
```

---

*Sources: `dataAppendix/CommunityResilienceMethodology_updated.docx`, `summarizedUserGuide_updated.docx`, `IndexVariables_Updated.xlsx`, `DisaggregatedVariables.xlsx`, and `CommunityResilienceIndexCurrentDev.py`.*
