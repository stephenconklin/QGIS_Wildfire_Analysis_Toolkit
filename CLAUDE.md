# CLAUDE.md — developer notes for QGIS Burn Severity Summary

This file orients Claude Code (and any developer) to the project. Read it before changing `burn_severity_summary.py`.

## What this is

A single-file QGIS Processing script that classifies a raw dNBR raster into USGS (Key & Benson 2006) burn severity classes and summarizes percent, hectares and acres per class for two zones: a burned area (zone A) and the part of it inside a watershed (zone B). User-facing documentation is in `README.md`; version history is in `CHANGELOG.md`.

It automates a manual workflow first done for GEOG 400 (Raster GIS) Lab 6 at Fort Lewis College, which compared the 2025 Dragon Bravo Fire with the Bright Angel Creek watershed, Grand Canyon. The original step-by-step methods are in the lab's `_Notes/Severity_Chart_Methods.md` (see *Test data* below).

## Files

| Path | Purpose |
|---|---|
| `burn_severity_summary.py` | The whole tool: one `QgsProcessingAlgorithm` subclass plus QGIS 3/4 compatibility helpers. |
| `dev/smoke_test.py` | Loads the script straight from the repo, runs it on the Lab 6 data with temporary outputs, prints the summary and checks the pixel counts against known values. Prints PASS or FAIL. |
| `README.md` | User docs: install, parameters, outputs, method, example, charting, troubleshooting. |
| `CHANGELOG.md` | Version history. Bump `VERSION` in the script and add an entry for every change. |
| `QGIS_Burn_Severity.code-workspace` | VS Code workspace. |

## Identity — do not change casually

- Algorithm id `script:burnseveritysummary` (`name()` returns `burnseveritysummary`). Saved models and scripts refer to it.
- Group: **Fire Analysis** (`groupId()` `fireanalysis`). Renamed from "GEOG 400" before 1.0.0.
- Parameter names (`DNBR`, `BURN_AREA`, `WATERSHED`, `ZONE_A_NAME`, `ZONE_B_NAME`, `TABLE`, `CLASS_NAMES`, `ZONES`, `SEVERITY`, `HISTOGRAM`, `SUMMARY`) are the API for `processing.run`. Renaming one breaks callers; treat it as a major version change.
- Summary field names `ord`, `sev_class`, `pct_db`, `pct_ba`, `ha_db`, `ha_ba`, `ac_db`, `ac_ba` are fixed so an existing layout chart keeps working after a rerun. `db` means zone A and `ba` means zone B, whatever the zone names are.

## Environment

The primary user runs QGIS 4.2.3 "Belém do Pará" on macOS (Qt 6.11, Python 3.12, GDAL 3.13). The script should also keep working on recent QGIS 3.x (Qt5): it uses the newer enum names where they exist and falls back to the old ones (`_try`, `_field`, `_polygon_type`, `_meters`, `_no_geometry`). It has not yet been run on 3.x.

The code only runs inside QGIS. Outside QGIS the only check is `python3 -m py_compile burn_severity_summary.py`. Real testing means running `dev/smoke_test.py` in the QGIS Python console. If the QGIS MCP server is connected, Claude can run it directly with `execute_code`: `exec(open('<repo>/dev/smoke_test.py').read())`.

## Architecture

`processAlgorithm` runs the steps in the order of the README's *Method* section, each as a child algorithm through the `_run` helper (`processing.run(..., is_child_algorithm=True)`):

1. Zones: `fixgeometries` ×2 → `dissolve` → `intersection` → `dissolve` → `fieldcalculator` ×2 → `mergevectorlayers` → `retainfields` (written to `ZONES`).
2. `reclassifybytable`: `RANGE_BOUNDARIES` 1 (min ≤ value < max), `NODATA_FOR_MISSING` true, NoData 0, Int32 (written to `SEVERITY`).
3. `zonalhistogram`, prefix `C_` (written to `HISTOGRAM`).
4. Summary: reads the histogram, writes one feature per class to a no-geometry sink (`SUMMARY`).

Decisions:

- **Pixel area comes from the raster's cell size**, not a hard-coded 0.09 ha, so other resolutions work. The tool refuses rasters whose CRS isn't in meters.
- **Class values come from the threshold table**, so `C_<value>` fields are looked up by value. A class absent from every zone has no histogram field and counts as 0.
- **Percent is of classified pixels in the zone**, not the polygon area. The totals are slightly below polygon area because of Zonal Histogram's pixel-center rule.
- Range boundaries 0 and 1 gave identical counts on the Lab 6 raster; 1 was chosen to match the documented method.
- The tool never touches the project or GUI. Output loading is left to Processing.

## Test data

The Lab 6 inputs live outside the repo, in the user's OneDrive (not committed; the raw data zip is 2.6 GB):

```
/Users/stephenconklin/Library/CloudStorage/OneDrive-FortLewisCollege/GEOG-400-Raster-GIS/Lab-6/QGISProject/
  data/Output/dNBR.tif                                raw dNBR, EPSG:26912, 30 m (about 84 MB)
  data/Output/BurnArea_Clip.shp                       816 polygons, zone A
  data/BrightAngelWatershed/BrightAngelWatershed.shp  zone B boundary
  _Notes/Severity_Chart_Methods.md                    the original manual method
```

`data/Output/dNBR_Burn_Area_Recscale.tif` in the same folder is rescaled to 0–1. It is the wrong input and gives nonsense classes (almost no Unburned, about 10% High); the README's troubleshooting table covers this.

Expected pixel counts (classes 1–5):

| Zone | C_1 | C_2 | C_3 | C_4 | C_5 | Total |
|---|---|---|---|---|---|---|
| Dragon Bravo | 11,556 | 290,803 | 188,947 | 55,906 | 44 | 547,256 |
| Bright Angel | 3,997 | 78,398 | 57,213 | 16,001 | 9 | 155,618 |

## Installed copies

**Add Script to Toolbox** copies the file into `~/Library/Application Support/QGIS/QGIS4/profiles/default/processing/scripts/`, and QGIS runs that copy, not the repo file. After changing the script, copy it there again (or replace the copy with a symlink to the repo file), then refresh the provider: `QgsApplication.processingRegistry().providerById('script').refreshAlgorithms()`. An older copy also sits in the Lab 6 project's `scripts/` folder; the repo is the source of truth.

## Conventions

- Keep it a single file with no dependencies beyond QGIS.
- Bump `VERSION` (semver) and add a `CHANGELOG.md` entry for every change; keep the README in step with parameters and outputs.
- Run `dev/smoke_test.py` in QGIS before committing a change to the algorithm.
- Never add Claude or Claude Code author or co-author credits anywhere: commits, PRs, README, changelog or code.
