# CLAUDE.md — developer notes for QGIS Burn Severity Summary

This file orients Claude Code (and any developer) to the project. Read it before changing `burn_severity_summary.py` or `fireanalysis/`. The pipeline plan and the verified service details are in `docs/DESIGN.md`.

## What this is

A single-file QGIS Processing script that classifies burn severity (raw dNBR with the USGS Key & Benson 2006 thresholds, or RdNBR / RBR from dNBR plus a pre-fire NBR) and summarizes percent, hectares and acres per class for a burned area (zone A) and optional sub-areas inside it (one zone B, or one zone per value of a name field). It ships two ways: as a Processing script, and as a plugin that wraps the same file. Since 2.0 (in development) the plugin is growing into a fire analysis pipeline modeled on the MTBS QGIS Fire Mapping Tool (FMT3): the `fireanalysis/` package adds imagery algorithms (HLS search and fetch so far). The script stays standalone and unchanged. User docs are in `README.md`; version history is in `CHANGELOG.md`.

It automates a manual workflow first done for GEOG 400 (Raster GIS) Lab 6 at Fort Lewis College, which compared the 2025 Dragon Bravo Fire with the Bright Angel Creek watershed, Grand Canyon. The original step-by-step methods are in the lab's `_Notes/Severity_Chart_Methods.md` (see *Full Lab 6 data* below).

The repository is local only so far: no remote, nothing pushed. Plugin metadata already points at `https://github.com/stephenconklin/QGIS_Burn_Severity`.

## Files

| Path | Purpose |
|---|---|
| `burn_severity_summary.py` | The whole tool: one `QgsProcessingAlgorithm` subclass plus QGIS 3/4 compatibility helpers. |
| `fireanalysis/` | Pipeline package, plugin only. `__init__.py` holds the plugin `VERSION`. `compat.py` (QGIS 3/4 helpers, a copy of the script's), `earthdata.py` (netrc check, GDAL path-specific options), `stac.py` (STAC search via `QgsBlockingNetworkRequest`), `hls.py` (HLS constants, granule ids, URLs, Fmask), `raster.py` (the shared analysis grid `analysis_grid`, read on a reference grid, rasterize QGIS geometries, write outputs), `local.py` (identify and calibrate downloaded Landsat C2 L2 / ARD and Sentinel-2 L2A products; `Scene` holds band file lists or signed URLs), `planetary.py` (Planetary Computer search, SAS token cache, STAC items to `Scene`), `mtbs.py` (MTBS map service: nearby fires and median thresholds), `algorithms/` (one module per algorithm). Relative imports only, so it works both inside the plugin folder and from the repo root. |
| `plugin/` | Plugin wrapper: `__init__.py` (`classFactory`), `plugin.py` (registers the provider), `provider.py` (provider `fireanalysis`; registers the imagery algorithms and subclasses the summary to put it in group Severity), `metadata.txt` (version filled in at build), `icon.svg`. |
| `docs/DESIGN.md` | Pipeline plan: FMT3 comparison, decisions, Earthdata findings, algorithm specs, phases, validation plan. |
| `dev/build_plugin.py` | Plain Python. Copies `plugin/*`, the script, `fireanalysis/**/*.py`, `LICENSE` and `README.md` into `dist/qgis_burn_severity-<version>.zip`, setting `version=` from `fireanalysis/__init__.py` and `changelog=` from the latest `CHANGELOG.md` entry. `dist/` is gitignored. |
| `dev/smoke_test.py` | Runs every mode on `testdata/` (two zones, zone A only, per-name zones, RdNBR, RBR), the full Lab 6 data if present, and the error cases. Checks pixel counts, summary field names and row count, and that the severity raster has a CRS. Works in the QGIS Python Console or headless. |
| `dev/mtbs_validation.py` | Online, slow (about an hour for all 18 fires). Reruns MTBS fires (USFS EDW MTBS MapServer for perimeters and attributes, the MTBS CONUS ImageServer for the yearly `dnbr6` mosaic) with HLS on MTBS's dates and compares classes inside the fire's own perimeter. Optional arguments: fire ids. Writes `report.json` and per-fire folders in a temp dir. |
| `dev/threshold_study.py` | Offline, on a `mtbs_validation.py` output folder: scores threshold-estimation rules against MTBS (MAE x1000, kappa). |
| `dev/perimeter_study.py` | Offline, on a validation folder: IoU of Draft burned-area perimeter vs MTBS perimeters for thresholds 0.05–0.15, minimum patches 1–20 ha, with and without a 1 km seed at the MTBS fire location. |
| `dev/pipeline_test.py` | Offline test of Draft burned-area perimeter (synthetic mask: speck removed, hole filled; IoU > 0.5 vs the test burned area), Prepare local scene NBR (synthetic Landsat and Sentinel-2 products with known DN: scaling, MTL parsing, S2 offset by MTD or date, QA mask, error cases), Build dNBR: a synthetic post-fire NBR (pre-fire NBR resampled to the `dnbr.tif` grid, minus dNBR) must give back `dnbr.tif` (max error ~3e-8), a ring offset matching an independent OGR/GDAL calculation (0.0126, 142,451 px), the RdNBR/RBR formulas, and the error cases. |
| `dev/online_test.py` | Live test of the whole chain on the test-data burned area (S30 2024-09-13, L30 2025-09-15): find (expected tiles), fetch (identical pre/post grids, UTM 12N, NBR range), missing-netrc and wrong-layer errors, Build dNBR (ring offset; the clipped test perimeter must trigger the burned-sample warning; the full Lab 6 perimeter, if present, gives 0.026), then Burn severity summary. Needs a network and `~/.netrc`. About 1 min. |
| `dev/run_headless.sh` | Runs a dev script (default `smoke_test.py`; second argument, e.g. `online_test.py`) with a QGIS macOS app's bundled Python (sets `PYTHONHOME`, `PYTHONPATH`, `PROJ_DATA`, offscreen Qt). Default app `/Applications/QGIS.app`. |
| `dev/make_testdata.py` | Rebuilds `testdata/` from the full Lab 6 data (run inside QGIS). |
| `testdata/` | About 6 MB: `dnbr.tif`, `prefire_nbr.tif` and `testdata.gpkg` (layers `burn_area`, `watershed`, `subareas`), plus a README with expected counts and data sources. `.gitignore` excludes GIS files except these. |
| `docs/images/` | README images: `severity_map.png` (rendered offscreen in a separate `QgsProject` from a full-data run, severity clipped to zone A), `chart.png` (the Lab 6 layout chart, `Severity_Chart_600dpi_white_bg.png`, resized to 1200 px), `dialog.png` (the tool dialog with test-data inputs, 900 px). |
| `LICENSE` | MIT. |

## Identity — do not change casually

- Algorithm `name()` `burnseveritysummary`: id `script:burnseveritysummary` as a script, `fireanalysis:burnseveritysummary` in the plugin. Saved models refer to these.
- Script group **Fire Analysis** (`groupId()` `fireanalysis`). In the plugin, the provider has that name and the summary sits in group **Severity** (`severity`); group ids aren't part of algorithm ids.
- Plugin algorithms `fireanalysis:findhlsscenes` (params `AOI`, `START`, `END`, `SENSORS`, `MAX_CLOUD`, output `OUTPUT`) and `fireanalysis:fetchhlsnbr` (`AOI`, `SCENES`, `BUFFER`, `CRS`, `MASK`, `NETRC`, outputs `NBR`, `REFLECTANCE`, `FMASK`), group **Imagery**. `fireanalysis:builddnbr` (`PRE_NBR`, `POST_NBR`, `PERIMETER`, `OFFSET_METHOD`, `UNBURNED`, `OFFSET_VALUE`, `RING_GAP`, `RING_WIDTH`, `SUBTRACT_OFFSET`, outputs `DNBR`, `RDNBR`, `RBR`, numbers `OFFSET`, `OFFSET_STD`, `OFFSET_N`), group **Severity**. Unreleased, so these can still change; after 2.0.0 the same rules apply as for the summary. `OFFSET_METHOD` order is API: append only. `fireanalysis:hlsburnseverity` (`PERIMETER`, `FIRE_NAME`, `FIRE_START`, `FIRE_END`, `ASSESSMENT`, `SUBAREAS`, `SUBAREA_FIELD`, `SUBAREA_NAME`, `ESTIMATE`, `LOW_T`, `MODERATE_T`, `HIGH_T`, `PRE_GRANULES`, `POST_GRANULES`, `MIN_CLEAR`, `OUTPUT_FOLDER`; outputs `FOLDER`, `DNBR`, `CLASSES`, `SUMMARY`, `REPORT`), group Pipeline: runs the other algorithms as steps through `_child` (instances, not `processing.run`, so it works without the provider registered) and loads Burn severity summary from `burn_severity_summary.py` next to the package. `fireanalysis:suggesthlspairs` (`AOI`, `FIRE_START`, `FIRE_END`, `ASSESSMENT`, `SENSORS`, `MIN_CLEAR`, `SEASON_DAYS`, `PRE_YEARS`, `POST_DAYS`, `COUNT`, output `OUTPUT`), group Imagery. Find HLS scenes also has `AOI_CLEAR` (fields `aoi_clear`, `aoi_data`); Fetch HLS NBR has `GRANULES` (text, instead of `SCENES`). `fireanalysis:draftperimeter` (`DNBR`, `THRESHOLD`, `MIN_AREA`, `HOLE_AREA`, `SEED`, output `OUTPUT`), group Severity. `fireanalysis:findplanetaryscenes` (`AOI`, `START`, `END`, `SOURCE` 0 Landsat / 1 Sentinel-2, `MAX_CLOUD`, `AOI_CLEAR`, output `OUTPUT`) and `fireanalysis:fetchplanetarynbr` (`AOI`, `SCENES`, `ITEMS`, `SOURCE`, `BUFFER`, `CRS`, `PIXEL`, `MASK`, outputs `NBR`, `REFLECTANCE`), group Imagery; Fetch shares `write_scene_nbr` with `fireanalysis:preparelocalnbr` (`SCENE`, `AOI`, `BUFFER`, `CRS`, `PIXEL`, `MASK`, `S2_OFFSET`, outputs `NBR`, `REFLECTANCE`), group Imagery. `fireanalysis:classifyseverity` (`DNBR`, `PRE_NBR`, `PERIMETER`, `ASSESSMENT`, `LOW_T`, `MODERATE_T`, `HIGH_T`, `OFFSET`, `OFFSET_STD`, `GREENNESS_T`, `ESTIMATE` (0 nearby MTBS fires, 1 own data), `RADIUS`, `YEARS`, output `OUTPUT`, numbers `LOW_T`, `MODERATE_T`, `HIGH_T`), group Severity. The `MASK` enum follows `hls.FMASK_CLASSES` order: append only.
- Parameter names are the API for `processing.run`: `DNBR`, `INDEX`, `PRE_NBR`, `BURN_AREA`, `ZONE_A_NAME`, `WATERSHED` (now any sub-area layer; name kept for compatibility), `SUBAREA_FIELD`, `ZONE_B_NAME`, `TABLE`, `CLASS_NAMES`, `ZONE_A_CODE`, `ZONE_B_CODE`, and outputs `ZONES`, `SEVERITY`, `HISTOGRAM`, `SUMMARY`. Renaming one is a major version change.
- The `INDEX` enum order (0 dNBR, 1 RdNBR, 2 RBR) is API too. Append new indices; don't reorder.
- Summary fields `ord`, `sev_class`, then `pct_`, `ha_`, `ac_` + zone code, grouped by measure. Default codes `db` / `ba` keep the Lab 6 layout chart working.

## Environment

The primary user runs QGIS 4.2.3 on macOS (Qt 6.11, Python 3.12, GDAL 3.13). QGIS 3.44.10 is also installed as `/Applications/QGIS.app` and 4.2.2 as `/Applications/QGIS-final-4_2_2.app`. The minimum supported version is QGIS 3.40 (`metadata.txt`). The code prefers the newer enum names and falls back to the old ones (`_try`, `_field`, `_polygon_type`, `_any_vector_type`, `_meters`, `_no_geometry`, `_advanced`).

The code only runs inside QGIS. Test with both versions before committing:

```
dev/run_headless.sh /Applications/QGIS.app                   # 3.44
dev/run_headless.sh /Applications/QGIS-final-4_2_3.app       # 4.2
dev/run_headless.sh /Applications/QGIS.app online_test.py    # HLS, needs network + ~/.netrc
```

Bundled Pythons: 3.44 has GDAL 3.12, 4.2.3 has GDAL 3.13; both have numpy and the JP2OpenJPEG driver. The python.org `python3` on this Mac has no CA certificates, so test network code only with a bundled Python. Don't run the bundled Python with `-I`: it ignores `PYTHONHOME` and crashes.

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

### Imagery (fireanalysis)

- Network: STAC calls go through `QgsBlockingNetworkRequest` (QGIS proxy/SSL settings, cancel via feedback). Raster reads go through GDAL `/vsicurl/`.
- Earthdata: netrc plus a cookie jar, set with `gdal.SetPathSpecificOption` on `/vsicurl/https://data.lpdaac.earthdatacloud.nasa.gov/` only. Bearer tokens failed (LP DAAC signs the HEAD redirect for HEAD only; see DESIGN.md). **Never set `CPL_CURL_VERBOSE` or print request headers with live credentials**: curl sends `Authorization: Basic` to URS and the trace prints it. That leaked the user's password into a session transcript once (2026-10-06).
- Fetch HLS NBR: AOI → 4326 → target CRS (default UTM of the AOI centroid), buffer, bounds snapped outward to 30 m; `gdal.Warp` to MEM per band (nearest within the tiles' zone, bilinear when reprojecting, nearest for Fmask); NBR where data, not masked, NIR and SWIR2 > 0.
- Dragon Bravo covers MGRS tiles 12SUF/12SVF/12SUE/12SVE; the test-data AOI needs 12SUF and 12SVF.
- Build dNBR: post-fire grid is the reference (MTBS adjusts the pre-fire image). Offset = median; quality limits from the FMT3 guide (±0.1, std 0.05) plus a burned-sample check (> 2 % of the sample above dNBR 0.27). The test-data `burn_area` is clipped to the window, so its ring includes burned land (14 % above 0.27, std 0.22); the full Lab 6 perimeter gives 0.026 ± 0.053.
- Planetary Computer: STAC `https://planetarycomputer.microsoft.com/api/stac/v1/search`, collections `landsat-c2-l2` (assets `nir08`, `swir22`, `red`, `qa_pixel`; `raster:bands` scale 2.75e-05 offset -0.2; platform `landsat-5`… ) and `sentinel-2-l2a` (`B8A`, `B12`, `B04`, `SCL`, 20 m; `s2:processing_baseline` decides the -1000 offset; the COGs keep raw DN). Sign hrefs with `?<token>` from `/api/sas/v1/token/<collection>` (about 1 h; cached). GET search with `ids=` for item ids. Landsat grids sit on 15 m offsets, so our 30 m-snapped grid resamples them bilinearly.
- MTBS services: boundaries at `https://apps.fs.usda.gov/arcx/rest/services/EDW/EDW_MTBS_01/MapServer` (one layer per year: 2022 = 79, 2023 = 80, 2024 = 82; lowercase fields `fire_id`, `pre_id`, `post_id`, `dnbr_offst`, `low_threshold`, …, thresholds x1000, 9999 = no class). Classes at `https://imagery.geoplatform.gov/iipp/rest/services/Fire_Aviation/USFS_EDW_MTBS_CONUS/ImageServer/exportImage` with `mosaicRule` `{"mosaicMethod":"esriMosaicAttribute","where":"year=YYYY"}`; ask for the output grid in UTM with nearest neighbour and it comes back aligned with ours. The yearly mosaic holds every fire of the year: compare only inside the fire's own perimeter (Pipeline 2022 borders Tunnel 2022). MTBS scene ids: `8PPPRRRYYYYMMDD` / `9…` Landsat 8/9, `A12SVEYYYYMMDD_30m` / `B…` Sentinel-2A/B. Per-fire bundles need the e-mail ZipServlet; not used.
- Local scenes: Landsat scale/offset parsed from any `REFLECTANCE_MULT_BAND_n` / `_ADD_BAND_n` text (MTL txt/xml, ARD json); fill DN 0; QA_PIXEL bits 0 fill, 1 dilated cloud, 2 cirrus, 3 cloud, 4 shadow, 5 snow, 7 water. Sentinel-2: `BOA_ADD_OFFSET band_id` 8 = B8A, 12 = B12, 3 = B04; SCL 0/1 fill, 3 shadow, 6 water, 8/9 cloud, 10 cirrus, 11 snow. Lab 6 local vs HLS on the same grid: S2 pre r 0.999 (local 0.030 lower: HLS bandpass adjustment), L9 post r 0.994 (0.001).
- Validation (2026-10-06, 17 fires, `dev/mtbs_validation.py` + `dev/threshold_study.py`): with MTBS thresholds, mean kappa 0.775 (0.711–0.864), agreement 88.7 %; the oracle thresholds on our dNBR are within 6 / 12 (x1000) of MTBS's low / high. Estimators (leave-one-out): nearby MTBS medians ~0.56 at any radius 100–250 km, own-data rule 0.49, Key & Benson 0.48. Every rule plateaus near 0.56: the remaining gap is analyst judgment, so don't tune the estimators further on these fires. RdNBR >= 750 covers ~50 % of these Southwest fires (sparse pre-fire vegetation inflates RdNBR); matching only where pre-fire NBR >= 0.5 fixes most of the high-threshold error.
- Historical validation (`--pc`, 6 fires 2000–2012): kappa 0.60–0.77, mean 0.69. L7/L5 pairs are weakest and their MTBS offsets differ most from ours. Perimeter study (18 fires): dNBR >= 0.05 and 20 ha patches is best (mean IoU 0.69 all patches, 0.73 seeded; the seed fails when the MTBS point is outside the burn: Pass, Mitchell). `gdal.SieveFilter` must get mask `None`: with the band as its own mask, zero pixels are excluded and small patches are never removed.
- The MTBS map service drops connections and stalls; `mtbs.nearby_fires` retries 4 times, `dev/mtbs_validation.py` retries with 120 s timeouts (a run once hung for 15 minutes on one socket before that).
- OGR memory driver: `MEM` from GDAL 3.11, `Memory` before (deprecated after); `raster.rasterize` picks by version.
- Fmask water inside the Dragon Bravo burn: 0.03 % before, 0.66 % after (pre-fire NBR 0.41, so burn scars). Water stays masked by default because of real water; documented in the README.

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

**The lab `dNBR.tif` is miscalibrated.** Rebuilt from the raw zip files (`Lab6-data-2026.zip`: Sentinel-2 L2A 2024-09-13 B8A/B12 20 m jp2 for 12SUF and 12SVF, no MTD/SCL; Landsat 9 C2 L2 US ARD `LC09_CU_007011_20250915` SR bands, Albers WGS84, no QA_PIXEL), it matches (RMSE 0.026) S2 NBR from DN/10000 *without* the −1000 offset minus L9 NBR from *raw DN*. Correctly calibrated: mean dNBR 0.476 in the test-data burned area, classes 2/20/27/27/24 %; HLS gives 0.500 and 2.2/17.3/25.8/27.7/27.0 %. BAER SBS (soil, not comparable 1:1): 7/65/26/2 %. The summary tests keep using the lab raster as their reference; the README example carries a note.

Expected dNBR pixel counts on the full data (classes 1–5): Dragon Bravo 11,556 / 290,803 / 188,947 / 55,906 / 44 (547,256); Bright Angel 3,997 / 78,398 / 57,213 / 16,001 / 9 (155,618). Test-data counts are in `testdata/README.md` and `dev/smoke_test.py`.

## Installed copies

The Lab 6 project's QGIS has the script installed through **Add Script to Toolbox**, which copies it to `~/Library/Application Support/QGIS/QGIS4/profiles/default/processing/scripts/burn_severity_summary.py`. QGIS runs that copy, not the repo file. As of 2026-10-06 it is still the 1.0.0 copy. Replacing it with a symlink to the repo file (or copying again) is the user's decision; Claude's attempt to do it was blocked. After any change there, refresh with `QgsApplication.processingRegistry().providerById('script').refreshAlgorithms()`. The Lab 6 project also has an old copy in its `scripts/` folder, which can be deleted.

## Conventions

- Keep `burn_severity_summary.py` a single file with no dependencies beyond QGIS, so it still installs as a script. Everything new goes in `fireanalysis/`, depending only on PyQGIS, GDAL and numpy (no rasterio, pystac-client, earthaccess).
- Use the MTBS conventions FMT3 documents, but don't copy FMT3 code (no license).
- Bump the version (semver) and add a `CHANGELOG.md` entry for every change: `VERSION` in `fireanalysis/__init__.py` for the plugin, and `VERSION` in `burn_severity_summary.py` when the script itself changes. Keep the README's parameter and output tables and the smoke-test expectations up to date.
- Run the smoke test headless on 3.44 and 4.2 before committing a change to the algorithm, and in the desktop session when the change touches outputs. Run `pipeline_test.py` and `online_test.py` too when `fireanalysis/` changes.
- Never add Claude or Claude Code author or co-author credits anywhere: commits, PRs, README, changelog or code.
