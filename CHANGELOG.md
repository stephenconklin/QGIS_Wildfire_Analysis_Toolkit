# Changelog

## 1.1.0 — 2026-10-06

- **RdNBR and RBR.** A new *Severity index and thresholds* option adds RdNBR (Miller & Thode 2007) and RBR (Parks et al. 2014), computed from the dNBR and a new optional *Pre-fire NBR raster*, each with its published four-class thresholds. The pre-fire NBR is resampled to the dNBR grid. dNBR with the USGS thresholds stays the default.
- **One zone, or many.** The sub-area layer (formerly *Watershed boundary*) is optional: leave it empty to summarize the burned area alone. Set the new *Sub-area name field* to get one zone per value, for example one per sub-watershed. Each zone's summary fields use a short code taken from its name. Sub-areas with no burned area inside, or with no name, are skipped with a warning.
- **Stops on rescaled input.** With the preset thresholds, the tool refuses a dNBR raster whose values look rescaled to 0–1 or scaled ×1000, and a pre-fire NBR outside −1 to 1, instead of producing wrong classes. Custom thresholds turn this into a warning.
- **Custom thresholds moved to Advanced.** The threshold table and class names are now optional overrides; empty means the preset for the chosen index. Two new Advanced options set the summary field codes for zone A and zone B (default `db` and `ba`, as before).
- **Fixed: the severity raster was written as Float32**, not Int32: the `DATA_TYPE` option index was wrong (5 is Float32; Int32 is 3). The index is now looked up by name. Pixel counts are unchanged, and Zonal Histogram no longer warns about a floating-point raster.
- **Fixed: the severity raster could have no CRS.** In a QGIS 4.2.3 desktop session, Reclassify by Table wrote the classes without a CRS for the Lab 6 dNBR (an ArcGIS-exported GeoTIFF), though the same run headless kept it. The tool now checks the output and writes the dNBR's CRS into it when it is missing.
- **Fixed: on QGIS versions older than 3.30**, the summary table could not be created (a compatibility fallback called itself).
- Tested headless on QGIS 3.44 and 4.2. The minimum supported version is now QGIS 3.40.
- Also available as a QGIS plugin: `python3 dev/build_plugin.py` builds a zip with a **Fire Analysis** Processing provider (algorithm id `fireanalysis:burnseveritysummary`).
- Test data in `testdata/` (a clip around the Bright Angel watershed), with `dev/make_testdata.py` to rebuild it. `dev/smoke_test.py` now checks every mode and the error cases, inside QGIS or headless through `dev/run_headless.sh`.
- MIT license.

## 1.0.0 — 2026-10-06

- First release. One Processing tool (**Scripts › Fire Analysis › Burn severity summary**, id `script:burnseveritysummary`) that builds a burned-area zone and a watershed sub-zone, classifies raw dNBR into USGS (Key & Benson 2006) severity classes, counts pixels per class with Zonal Histogram, and writes a one-row-per-class summary table (percent, hectares and acres for each zone) for layout charts.
- Editable threshold table and class names; pixel area read from the raster.
- `dev/smoke_test.py` checks results against the Dragon Bravo / Bright Angel Lab 6 counts.
