# Vermont Community Resilience Index (VT CRI) — Project Summary

*Overview updated 2026-11-04 to reflect the refactored, single-index, municipality-only ETL. Intended as a single orientation document for anyone inheriting or evaluating this project.*

---

## 1. What the project is

The **Vermont Community Resilience Index (VT CRI)** is an interactive health-equity planning tool, delivered as a public-facing **ArcGIS Online Experience Builder** application. It helps public health officials examine one question:

> *What is the capacity of communities throughout Vermont to mitigate and recover from a public health crisis or health emergency?*

"Community resilience" here is defined as **the socioeconomic and housing capacity of a population to mitigate and recover from a public health crisis or personal health emergency in a manner that sustains physical, social, and mental health.**

The application has **two components**:

| Component | What it shows | Statistic |
|-----------|---------------|-----------|
| **1. Composite Index** | A single 0–100 resilience score per municipality | Geometric mean of the indicator percentile ranks, re-ranked 0–100 |
| **2. Indicator Displays** | Spatial distribution of each individual indicator, plus a BIPOC / White race disaggregation | Standard mean difference (−1 to 1) |

There is **one composite index** (no sub-themes), published at the Vermont **municipality** geography unit only (census tracts were removed from the workflow). Both components are recomputed for each **area frame**: statewide, 14 counties, 12 VDH districts, and hospital service areas (HSA). The index score, percentile ranks, and standard mean differences are each reported per frame.

## 2. Purpose & justification

- **Audience:** Vermont Department of Health officials and local public-health planners.
- **Need it fills:** Translates dozens of Census variables into one defensible, comparable measure of how well a community can absorb and recover from a health shock, surfaced at a municipal granularity that statewide averages hide.
- **Methodological lineage:** Follows the **CDC Social Vulnerability Index** precedent and the **OECD Handbook on Constructing Composite Indicators**. The design is documented against peer-reviewed literature (Cutter 1996, Flanagan 2011, Beccari 2016, Rufat 2021, etc.).
- **Equity framing:** A **BIPOC / White** race disaggregation (plus age and sex where available) is a deliberate health-equity feature, reported only at the area-frame level with small-count suppression to protect small-population groups.

## 3. The 11 indicators

All indicators are **equally weighted** (the most common and most defensible default per Beccari 2016). Each is built from **ACS 5-year estimates** pulled via the Census API.

1. Supportive Income — % population with income > 200% of Federal poverty level *(C17002)*
2. Employment — % adult population employed *(B23001)*
3. Without Public Assistance — % households not receiving public assistance *(B19058)*
4. Median Household Income — inflation-adjusted dollar value *(B19013)*
5. High School Education — % adults 25+ with HS diploma+ *(B15001)*
6. Health Insurance — % population insured *(B27010)*
7. Manageable Housing Costs — % housing units w/ costs < 30% of income *(B25070 + B25095)*
8. Broadband Internet — % households w/ computer + broadband *(B28005)*
9. Owner-Occupied Homes — % owner-occupied units *(B25003)*
10. English Proficiency — % population speaking English "well" or better *(B16005)*
11. Vehicle Access — % housing units with a vehicle *(B25044)*

Ten indicators are percentages; **Median Household Income is a dollar value** (a count-path indicator with no denominator). Variable-level detail (table IDs, numerators/denominators) is in `data/reference/VDH_CRI_IndexVariableAppendix.xlsx`; the disaggregation variables are in `data/reference/VDH_CRI_DisaggreatedVariableAppendix.xlsx` and `config/demographics_variables.json`.

**Broadband and the earliest window:** ACS table `B28005` (and the `B28009` race iterations) did not exist before ~2013, so it is unavailable for the **2010–2014** window. The script detects this at runtime, drops Broadband Internet for that window only, and builds that window's composite index from the remaining **10 indicators**.

## 4. Methodology in four steps

1. **Identify variables** — map each indicator to specific ACS table variables (numerators/denominators; a household-count weight for median income).
2. **Preprocess indicators** — aggregate variables → a normalized rate per municipality (numerator ÷ denominator) → convert to **percentile rank (0–100)**, computed *separately per area frame* (percentile rank is relative to the units included in the frame).
3. **Calculate index** — combine the indicator percentile ranks with a **geometric mean** (multiply, take the *n*th root), then re-rank 0–100 within each area frame.
4. **Postprocess** — classify into 4 quantile classes: Low / Medium-Low / Medium-High / High Resilience, symbolized on the maps.

**Why geometric mean (the key methodological choice):** It is **non-compensatory** — a high score on one indicator does *not* cancel out a low score on another. A community weak in several areas cannot hide behind one strong indicator, and improvements to low-scoring indicators move the needle more.

**Indicator Displays** use **standard mean difference**: `(municipality value − area-frame mean) ÷ area-frame standard deviation`, giving a roughly −1 to 1 value showing how far above/below the frame mean each place sits. Statewide and county frame means come directly from the ACS aggregate for that frame; **VDH district and HSA frame means are built from the member municipalities** — rate indicators sum numerators and denominators; median income uses a household-count-weighted mean.

**Race disaggregation (BIPOC / White).** Race resolves to exactly two groups at every frame: a composite **BIPOC** (all non-white ACS race iterations combined — summed numerators/denominators for rate indicators, a household-weighted mean of medians for Median Household Income) and **White**. Age and sex groups are reported where ACS provides them. **Suppression is applied only at the area-frame level** (never to individual towns): municipalities under the 25-count threshold are still pooled into their district/HSA aggregate, and a frame-level group is suppressed only if its own combined count (denominator, or household weight for median income) is below 25.

## 5. ETL overview

**Engine:** [src/CommunityResilienceIndexCurrentDev.py](src/CommunityResilienceIndexCurrentDev.py)

```
Census ACS5 API  ──(requests, by variable ID)──►  per-indicator DataFrames (municipality / county / statewide)
        │                                                  │
VDH municipality crosswalk (SDE) ──► county / VDH district / HSA / statewide rollups
        │                                                  │
        ▼                                                  ▼
  Normalize (num ÷ denom)  ►  Percentile rank (pandas .rank(pct=True)*100), per area frame
        │
        ▼
  Geometric mean of all indicator ranks (scipy gmean)  ►  re-rank 0–100  ►  quantile class
        │
        ▼
  Standard mean difference per indicator per frame   +   BIPOC / White disaggregation
        │
        ▼
  One appended Excel workbook per output type (all 5-year windows stacked)  ──►  ArcGIS Pro / ArcGIS Online
```

- **Structure:** the pipeline runs as `run_period(end_year)` and a driver loop over the `YEARS` list at the top of the script. Each window's results are **appended** into one combined workbook per output type.
- **Windows:** `YEARS = [2014, 2019, 2024]` → the 5-year ACS windows **2010-2014, 2015-2019, 2020-2024**. Edit the list to add/remove windows. Every output row carries an **`ACS 5-Year`** vintage column.
- **Inputs:** Census API key (`config/CensusAPI.txt`); municipality crosswalk feature class and other UNC paths (`config/network_paths.json`, git-ignored); disaggregation variables (`config/demographics_variables.json`).
- **Geographies computed:** municipality (county subdivision); area frames: statewide, county, VDH district, hospital service area.
- **Outputs:** written to `data/output/` as one combined workbook per deliverable:
  - `townsIndexGeometric.xlsx` — single composite index (one score per municipality per area frame)
  - `townsIndicators.xlsx` — per-indicator value, frame means, percentile ranks, and standard mean differences
  - `townsUnpopulated.xlsx` — zero/null-population municipalities
  - `statewideDisaggregated.xlsx`, `countiesDisaggregated.xlsx`, `districtsDisaggregated.xlsx`, `hospitalsDisaggregated.xlsx` — BIPOC / White (+ age/sex) disaggregation per frame

## 6. Core strengths (what to protect)

1. **Sound, well-documented methodology** — grounded in CDC SVI precedent and the OECD Handbook, with an explicit rationale for every major choice (equal weighting, geometric mean, percentile ranking, area frames).
2. **A single, flexible composite index** across multiple area frames at municipal granularity.
3. **Health-equity disaggregation** — a BIPOC / White breakdown with frame-level small-count suppression.
4. **Full reproducibility from source** — everything derives from documented ACS variables via the API; the variable appendices and `config/demographics_variables.json` make the pipeline auditable end-to-end.
5. **Multi-year coverage** (2010-2014 through 2020-2024, stacked in one file) enables trend analysis.

## 7. Status & things to verify before the next publication

The ETL is now a single, function-structured, municipality-only pipeline (the former tract logic, sub-index themes, and orphaned legacy code have been removed). Before publishing a run, confirm the following against a prior published period:

- **Live-run validation.** The refactor has been syntax-checked but not executed end-to-end here (it requires `arcpy`, the Census API key, and the SDE crosswalk). Diff the combined outputs against a prior published period before release.
- **BIPOC median definition.** The BIPOC group for Median Household Income is a **household-count-weighted mean** of the non-white race medians (medians cannot be summed). Confirm this matches the intended methodology.
- **Broadband gap in 2010-2014.** The composite index for that window is built from 10 indicators, and broadband percentile columns are empty (NaN) for those rows in the combined index file — expected, but note it when the dashboard consumes the file.
- **2021-2025 window.** Not currently in `YEARS`; add `2025` once the ACS 5-year release is available.
- **HSA in the dashboard.** The script still computes the hospital-service-area frame; confirm which frames the current dashboard surfaces.

## 8. Repository layout

```
Community Resilience Project/
├── README.md                     ← this summary
├── src/
│   └── CommunityResilienceIndexCurrentDev.py   ← the ETL (functions, YEARS loop)
├── config/                       ← CensusAPI.txt, network_paths.json, demographics_variables.json (keys/paths git-ignored)
├── data/
│   ├── reference/                ← crosswalk + ACS variable appendices
│   └── output/                   ← combined multi-year deliverables (one workbook per type)
├── dashboard/                    ← dashboard mockups + design notes
├── gis/                          ← .aprx, .atbx, .gdb, published layers (git-ignored)
└── references/                   ← literature PDFs (git-ignored)
```

---

*Sources: project methodology and user guide, the ACS variable appendices in `data/reference/`, `config/demographics_variables.json`, and `src/CommunityResilienceIndexCurrentDev.py`.*
