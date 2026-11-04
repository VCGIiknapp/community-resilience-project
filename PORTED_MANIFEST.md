# Ported / Archived Manifest

*Generated 2026-11-03. This clean project is a **copy**; the original
`Community Resilience Project/` folder is **100% intact and serves as the archive**.
Nothing was deleted. 71 keeper items (457 MB) were copied here.*

## What was PORTED into this clean folder (71 items)

| Destination | Contents |
|-------------|----------|
| `README.md`, `CRI_ProjectSummary.md` | Project orientation summary |
| `src/` | `CommunityResilienceIndexCurrentDev.py` — the single canonical ETL (from `D:\PythonScripts\...\CRI\`) |
| `config/` | `CensusAPI.txt` + config/external-input notes |
| `data/reference/` | Index & disaggregated variable appendices, population-reference variables, VDH appendix (xlsx/pdf/csv), `townList.xlsx`, `townsCrosswalk.csv` (9 files) |
| `data/output/2018-2022/` | Published index CSVs (Tracts/Municipalities Index + Indicators) and the current 2018-2022 / 2020s dashboard feeder tables (28 files; 5 block-group feeders removed 2026-11-03) |
| `docs/` | Latest methodology (`_updated.docx` + pdf), summarized user guide (`_updated` + pdf), indicator definitions (docx/pdf), 2024 Community Engagement Guide, webpage summary, app help sidebar, `figures/` (4 PNGs) |
| `gis/` | **Entire ArcGIS Pro project copied intact** (`CommunityResilienceIndex.aprx/.atbx/.gdb` + templates) plus `Indicator Legend.pagx`, `Layout.pagx` |
| `references/` | OECD Handbook, Cutter, Rufat, Becker, Nguefack-Tsague, Papathoma-Köhle, EU SVI Guide, GARE Equity Toolkit, `literature.docx`, `otherstates.docx` |

## What was NOT ported (remains only in the original folder = archive)

These were intentionally left behind. Retrieve any individually from the original folder if needed.

- **`python/venv/`** — committed virtual environment; recreate from pip instead.
- **Superseded code generations** in `python/` — `cri.py`, `cri2.0.py`, `cri2017-2020.py`, `svi*.py`,
  per-year `*stacked.py`, `export2017-2020/`, `manual2017-2019/`, `baseline*`, `backup.py`,
  `scratch.py`, `old_lowerinc.py`, `errorTEST.py`, and all `*TEST*` scripts.
- **Regenerable intermediate data** — hundreds of `.xlsx`/`.csv` in `python/` and the
  pre-2018-2022 periods in `python/dashboardPython/` (2008-2012 … 2016-2020, plus raw/AGOformat/
  complete/suppressed variants). Regenerate from the ETL as needed.
- **Prior-version narrative docs** — older `methodology.docx`, `CommunityResilienceMethodology.docx`,
  non-`_updated` `summarizedUserGuide*`, and duplicate indicator-definition files.
- **Exploratory / research folders** — `multivarate analysis/`, `r_test/`, `moeExploration/`,
  `pdf2png/`, `criDataStackedManual/`, `healthEquityNov30/`, `info/`, `Index/`,
  `physical_vulnerability_experiments.xls`, `stdev_normdist.xlsx`.
- **Duplicate images / legends** — the many `*Indicator_Legend.png/jpg`, `oval*`/`circle*` graphics,
  splash-screen and navigation JPGs, `applicationNavigationImages/`.
- **Meeting notes & misc** — `amyRedman*`, `weeklyMeetingDan*`, dated `*.docx` notes, `_ToDoList*`,
  `toDoNotes.txt`, `notes.docx`, presentation `.pptx` files.
- **Vendored source downloads** — `ACS downloads/` (regenerable via API), `ACS2020_Table_Shells.xlsx`.
- **Secondary GIS exports** — `ProProjectCRI_SampleForRemoteDesktop.aprx`, `*.mapx`, shapefiles
  (`hospital_drivetimes.*`, `powerbi2019.*`), `powerbitest.pbix`, geojson.
- **Duplicate annotated PDF** — `annotated - Rufat (2021)...pdf` (plain copy ported instead).

## Notes
- No data was modified; all ported files are byte copies.
- Before running the ETL from this folder, update the hardcoded paths listed in `config/README.md`.

## Change log
- **2026-11-03 — Block groups removed.** Geographies going forward are Census tracts and
  municipalities only (area frames unchanged: statewide, county, VDH district, hospital service
  area). All *live* block-group references were stripped from
  `src/CommunityResilienceIndexCurrentDev.py` (syntax-verified); the active loop now iterates
  `['tract', 'county subdivision']`. Five block-group feeder files were deleted from
  `data/output/2018-2022/`. Remaining block-group references in the script are confined to the
  orphaned legacy section below ~line 520 (see README §8).
