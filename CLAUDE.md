# CLAUDE.md — developer notes for QGIS Burn Severity Summary

This file orients Claude Code (and any developer) to the project. Read it before changing `burn_severity_summary.py`.

## What this is

A single-file QGIS Processing script that classifies burn severity (raw dNBR with the USGS Key & Benson 2006 thresholds, or RdNBR / RBR from dNBR plus a pre-fire NBR) and summarizes percent, hectares and acres per class for a burned area (zone A) and optional sub-areas inside it (one zone B, or one zone per value of a name field). It ships two ways: as a Processing script, and as a plugin that wraps the same file. User docs are in `README.md`; version history is in `CHANGELOG.md`.

It automates a manual workflow first done for GEOG 400 (Raster GIS) Lab 6 at Fort Lewis College, which compared the 2025 Dragon Bravo Fire with the Bright Angel Creek watershed, Grand Canyon. The original step-by-step methods are in the lab's `_Notes/Severity_Chart_Methods.md` (see *Full Lab 6 data* below).

The repository is local only so far: no remote, nothing pushed. Plugin metadata already points at `https://github.com/stephenconklin/QGIS_Burn_Severity`.

## Files

| Path | Purpose |
|---|---|
| `burn_severity_summary.py` | The whole tool: one `QgsProcessingAlgorithm` subclass plus QGIS 3/4 compatibility helpers. |
| `plugin/` | Plugin wrapper: `__init__.py` (`classFactory`), `plugin.py` (registers the provider), `provider.py` (provider `fireanalysis`; subclasses the algorithm to drop its group), `metadata.txt` (version filled in at build), `icon.svg`. |
| `dev/build_plugin.py` | Plain Python. Copies `plugin/*`, the script, `LICENSE` and `README.md` into `dist/qgis_burn_severity-<version>.zip`, setting `version=` from `VERSION` and `changelog=` from the latest `CHANGELOG.md` entry. `dist/` is gitignored. |
| `dev/smoke_test.py` | Runs every mode on `testdata/` (two zones, zone A only, per-name zones, RdNBR, RBR), the full Lab 6 data if present, and the error cases. Checks pixel counts, summary field names and row count, and that the severity raster has a CRS. Works in the QGIS Python Console or headless. |
| `dev/run_headless.sh` | Runs the smoke test with a QGIS macOS app's bundled Python (sets `PYTHONHOME`, `PYTHONPATH`, `PROJ_DATA`, offscreen Qt). Default app `/Applications/QGIS.app`. |
| `dev/make_testdata.py` | Rebuilds `testdata/` from the full Lab 6 data (run inside QGIS). |
| `testdata/` | About 6 MB: `dnbr.tif`, `prefire_nbr.tif` and `testdata.gpkg` (layers `burn_area`, `watershed`, `subareas`), plus a README with expected counts and data sources. `.gitignore` excludes GIS files except these. |
| `docs/images/` | README images: `severity_map.png` (rendered offscreen in a separate `QgsProject` from a full-data run, severity clipped to zone A), `chart.png` (the Lab 6 layout chart, `Severity_Chart_600dpi_white_bg.png`, resized to 1200 px), `dialog.png` (the tool dialog with test-data inputs, 900 px). |
| `LICENSE` | MIT. |

## Identity — do not change casually

- Algorithm `name()` `burnseveritysummary`: id `script:burnseveritysummary` as a script, `fireanalysis:burnseveritysummary` in the plugin. Saved models refer to these.
- Script group **Fire Analysis** (`groupId()` `fireanalysis`). In the plugin, the provider has that name and the algorithm has no group.
- Parameter names are the API for `processing.run`: `DNBR`, `INDEX`, `PRE_NBR`, `BURN_AREA`, `ZONE_A_NAME`, `WATERSHED` (now any sub-area layer; name kept for compatibility), `SUBAREA_FIELD`, `ZONE_B_NAME`, `TABLE`, `CLASS_NAMES`, `ZONE_A_CODE`, `ZONE_B_CODE`, and outputs `ZONES`, `SEVERITY`, `HISTOGRAM`, `SUMMARY`. Renaming one is a major version change.
- The `INDEX` enum order (0 dNBR, 1 RdNBR, 2 RBR) is API too. Append new indices; don't reorder.
- Summary fields `ord`, `sev_class`, then `pct_`, `ha_`, `ac_` + zone code, grouped by measure. Default codes `db` / `ba` keep the Lab 6 layout chart working.

## Environment

The primary user runs QGIS 4.2.3 on macOS (Qt 6.11, Python 3.12, GDAL 3.13). QGIS 3.44.10 is also installed as `/Applications/QGIS.app` and 4.2.2 as `/Applications/QGIS-final-4_2_2.app`. The minimum supported version is QGIS 3.40 (`metadata.txt`). The code prefers the newer enum names and falls back to the old ones (`_try`, `_field`, `_polygon_type`, `_any_vector_type`, `_meters`, `_no_geometry`, `_advanced`).

The code only runs inside QGIS. Test with both versions before committing:

```
dev/run_headless.sh /Applications/QGIS.app                   # 3.44
dev/run_headless.sh /Applications/QGIS-final-4_2_3.app       # 4.2
```

The desktop session matters too, because it has behaved differently from headless runs (see the CRS bug below). If the QGIS MCP server is connected, Claude can run the smoke test in the live session with `execute_code`: `exec(open('<repo>/dev/smoke_test.py').read())`. Each `execute_code` call starts with a fresh namespace. In the session's Python, `gdal.Open(...).GetProjection()` has given misleading results, so check output CRSs with `QgsRasterLayer(path).crs()` or the app's `gdalinfo` (set `PROJ_DATA` to `<app>/Contents/Resources/qgis/proj`).

## Architecture

`processAlgorithm` runs on a worker thread and never touches the project or GUI. Child algorithms run through `_run` (`processing.run(..., is_child_algorithm=True)`).

1. **Checks.** Thresholds (preset or custom; an empty matrix comes back as `[None]`), names, codes, pre-fire NBR present for RdNBR/RBR, meter CRS. `_check_range` uses exact `bandStatistics(1)`. Passing a stats-flags argument triggers a deprecation warning on 3.40+ whatever its type. The range check stops the run with preset thresholds and only warns with custom ones.
2. **Zones.** Zone A: `fixgeometries` → `dissolve`. Sub-areas: `fixgeometries` → `dissolve` (by name field or all) → `intersection` with zone A → `dissolve`. Each is tagged with `zone` by `fieldcalculator` plus `retainfields`, then `mergevectorlayers` (dNBR CRS) → `retainfields` → `ZONES`. Zone order is zone A, then the sub-zones; the summary columns follow it. Named sub-areas that don't intersect zone A, and features with an empty name, are warned about and skipped.
3. **Index.** dNBR is used directly. RdNBR/RBR come from `native:modelerrastercalc` (`A@1` = dNBR, `B@1` = pre-fire NBR) with the dNBR extent, cell size and CRS, so the pre-fire NBR is resampled. `INDEX_EXPRESSIONS` holds the formulas.
4. **Classify.** `reclassifybytable`, `RANGE_BOUNDARIES` 1 (min ≤ value < max), NoData 0, `DATA_TYPE` looked up by name (`_int32_option`; Int32 is index 3, and 1.0.0 wrongly used 5 = Float32). Then `_ensure_crs`.
5. **Count.** `zonalhistogram`, prefix `C_`.
6. **Summary.** One feature per class in a no-geometry sink. Pixel area comes from the raster cell size. A class missing from the histogram counts as 0.

`PRESETS` holds the index labels, tables and class names. Published RdNBR/RBR thresholds are ×1000, so they are divided by 1000 for raw units.

### The CRS bug

In the QGIS 4.2.3 desktop session, `native:reclassifybytable` on the Lab 6 `dNBR.tif` (an ArcGIS-exported GeoTIFF) wrote a GeoTIFF with no CRS, even with a plain direct call. The same call headless (3.44 and 4.2.3), or on the re-written `testdata/dnbr.tif`, kept the CRS. The cause wasn't found. `_ensure_crs` opens the output with GDAL in update mode and writes the dNBR's CRS (by authid) if it is missing. The smoke test asserts that every severity raster has a CRS.

## Full Lab 6 data

Outside the repo, in the user's OneDrive (the raw data zip is 2.6 GB):

```
/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/GEOG-400-Raster-GIS/Lab-6/QGISProject/
  data/Output/dNBR.tif                              raw dNBR, EPSG:26912, 30 m, -0.604 to 0.759
  data/Output/NBR_Sentinel_Before_20240913.tif      pre-fire NBR, different grid (13.45 px x-offset)
  data/Output/BurnArea_Clip.shp                     816 polygons (some invalid geometries), zone A
  data/BrightAngelWatershed/BrightAngelWatershed.shp zone B boundary
  data/Output/dNBR_Burn_Area_Recscale.tif           0-1 rescaled: the wrong input; must be refused
  data/Output/SeverityAnalysis/                     the manual workflow's outputs and chart PNGs
  _Notes/Severity_Chart_Methods.md                  the original manual method
```

Expected dNBR pixel counts on the full data (classes 1–5): Dragon Bravo 11,556 / 290,803 / 188,947 / 55,906 / 44 (547,256); Bright Angel 3,997 / 78,398 / 57,213 / 16,001 / 9 (155,618). Test-data counts are in `testdata/README.md` and `dev/smoke_test.py`.

## Installed copies

The Lab 6 project's QGIS has the script installed through **Add Script to Toolbox**, which copies it to `~/Library/Application Support/QGIS/QGIS4/profiles/default/processing/scripts/burn_severity_summary.py`. QGIS runs that copy, not the repo file. As of 2026-10-06 it is still the 1.0.0 copy. Replacing it with a symlink to the repo file (or copying again) is the user's decision; Claude's attempt to do it was blocked. After any change there, refresh with `QgsApplication.processingRegistry().providerById('script').refreshAlgorithms()`. The Lab 6 project also has an old copy in its `scripts/` folder, which can be deleted.

## Conventions

- Keep the tool a single file with no dependencies beyond QGIS. The plugin only wraps it.
- Bump `VERSION` (semver) and add a `CHANGELOG.md` entry for every change. Keep the README's parameter and output tables and the smoke-test expectations up to date.
- Run the smoke test headless on 3.44 and 4.2 before committing a change to the algorithm, and in the desktop session when the change touches outputs.
- Never add Claude or Claude Code author or co-author credits anywhere: commits, PRs, README, changelog or code.
