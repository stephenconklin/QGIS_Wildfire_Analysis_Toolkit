# Design: from severity summary to fire analysis pipeline

Status: agreed 2026-10-06. Phase 1 (HLS search and fetch) built and tested on QGIS 3.44 and 4.2.3, headless and desktop.

## Goal

A QGIS plugin that takes a fire's area of interest and dates, finds and fetches
pre- and post-fire imagery, calibrates and masks it correctly, and produces
burn severity products comparable to MTBS: NBR, dNBR, RdNBR/RBR, a perimeter,
`dnbr6`-style classes, area summaries, styled layers, a map and a metadata
record. The model is the MTBS QGIS Fire Mapping Tool (FMT3 v2.1.0), redone as
Processing algorithms that run on QGIS 3.40 to 4.x, headless or in models.

## What FMT3 does, and what we change

FMT3 workflow: pre-process zipped scenes from a fixed folder tree (stack, NBR
x1000, optional Albers) → fire record in SpatiaLite → choose initial, extended or
single-scene assessment and pre/post/perimeter scenes (one sensor per mapping)
→ dNBR → hand-digitize perimeter and cloud mask → subset to perimeter + 3 km,
offset = median dNBR outside the perimeter, Otsu suggestions for thresholds →
RdNBR = (dNBR − offset) / sqrt(|preNBR / 1000|) → `dnbr6` classes → metadata
text file.

| FMT3 | This plugin |
|---|---|
| PyQt5 GUI, QGIS 3 only, Windows paths and hand-renamed config files | Processing algorithms, QGIS 3.40–4.x, batch, models, `processing.run`, headless tests |
| User searches GloVis / EarthExplorer and downloads whole scenes | STAC search on the AOI; reads only the AOI window from cloud-optimized GeoTIFFs |
| Landsat C2 L2 bands appear to be stacked as raw DN, without the 0.0000275 / −0.2 scaling (to be confirmed on a real scene) | Every source is scaled to reflectance from its own metadata before any index |
| Cloud, shadow and water mask digitized by hand | Mask from Fmask / QA_PIXEL / SCL, editable afterwards |
| No mixing of Landsat and Sentinel-2 | HLS is harmonized, so L30 and S30 can be paired |

We use the MTBS conventions FMT3 documents (classes, the −150 increased-greenness
threshold, the offset from unburned pixels, RdNBR 640 / 750 for the high class)
but write our own code. FMT3 has no license file, so none of its code is copied.

## Decisions

1. **Units.** Indices are float, raw units (NBR −1 to 1) inside the pipeline. An
   export option writes x1000 integers for MTBS compatibility.
2. **Structure.** The single-file rule is dropped for the plugin.
   `burn_severity_summary.py` stays a standalone script with its ids and
   parameters unchanged; it is one stage of the pipeline.
3. **Data access.** STAC, with providers added one at a time:

   | Provider | Collections | Login |
   |---|---|---|
   | NASA CMR-STAC `LPCLOUD` (first) | HLSL30 / HLSS30 v2.0 | Earthdata, `~/.netrc` |
   | Microsoft Planetary Computer | Landsat C2 L2 (1984–), Sentinel-2 L2A | none (URL signing) |
   | Earth Search (AWS) | Sentinel-2 L2A | none |

   Local files (Landsat C2 L2 including US ARD tiles, Sentinel-2 L2A, HLS) are
   also accepted, because real inputs are often hand-assembled (Lab 6).
4. **Earthdata login** reads `~/.netrc` (`machine urs.earthdata.nasa.gov`). A
   later plugin setting may take credentials from the QGIS authentication
   manager instead; the code takes a netrc path so that change stays local.
5. **Dependencies.** Only what ships with QGIS: PyQGIS, GDAL, numpy. No
   rasterio, pystac-client or earthaccess.

## Why HLS first

HLS v2.0 L30 (Landsat 8/9) and S30 (Sentinel-2) are bandpass-adjusted to OLI
and BRDF-normalized on the same 30 m MGRS grid, so pre- and post-fire pixels
line up without resampling inside a UTM zone, sensors can be mixed, revisit is
2–3 days, and there is one calibration (scale 0.0001, offset 0, fill −9999) and
one mask (Fmask). Limits: no data before 2013, 30 m only, and MTBS itself uses
Landsat C2, so direct Landsat stays the reference for MTBS comparisons.

Lab 6 shows why harmonization matters: its pair is Sentinel-2 L2A (13 Sep
2024, tiles 12SUF and 12SVF, 20 m) before and Landsat 9 C2 L2 ARD (15 Sep 2025,
Albers tile CU 007011) after, with no MTD file to supply the S2 `BOA_ADD_OFFSET`
(−1000 for processing baseline 04.00 and later). Either the missing offset or
the sensor difference could suppress the High class (0.01 % in both zones).

## Earthdata access: what works

Verified 2026-10-06 against HLS.L30.T12SUF.2025258T180302 (QGIS 4.2.3, GDAL 3.13):

- STAC search at `https://cmr.earthdata.nasa.gov/stac/LPCLOUD/search` needs no
  login. POST with `collections`, `bbox`, `datetime`, `limit`; the `next` link
  is a GET URL with a `cursor`. Scene cloud cover is `eo:cloud_cover`.
- Band files are COGs: `https://data.lpdaac.earthdatacloud.nasa.gov/lp-prod-protected/HLS{L30,S30}.020/<id>/<id>.<band>.tif`,
  Int16, scale 0.0001, nodata −9999, 3660 x 3660 px, 256 px blocks, WGS 84 UTM.
- GDAL `/vsicurl/` works with **netrc plus a cookie jar**: `GDAL_HTTP_NETRC=YES`
  and `GDAL_HTTP_COOKIEFILE` / `GDAL_HTTP_COOKIEJAR`. curl follows the 302 to
  `urs.earthdata.nasa.gov`, sends the netrc login there only, and gets back an
  `asf-urs` cookie (valid 7 days) that later requests reuse. A 512 x 512 window
  read took 0.1 s after a 3 s open.
- **Bearer tokens do not work** with `/vsicurl/` here. LP DAAC answers HEAD with
  a CloudFront URL signed for HEAD only (`/head-…`); GDAL reuses it for the GET
  range read, gets 403, and its retry drops the token. With
  `CPL_VSIL_CURL_USE_HEAD=NO` it still failed. Not pursued.
- All of these options are set with `gdal.SetPathSpecificOption` on the
  `/vsicurl/https://data.lpdaac.earthdatacloud.nasa.gov/` prefix, so other GDAL
  traffic in the session (web layers) never gets the cookies or netrc.
- Never turn on `CPL_CURL_VERBOSE` with live credentials: it prints the
  `Authorization: Basic` header that curl sends to URS.

The Dragon Bravo AOI covers four MGRS tiles (12SUF, 12SVF, 12SUE, 12SVE), so
fetching always mosaics.

## Algorithms

| Phase | Algorithm | Id | Status |
|---|---|---|---|
| 1 | Find HLS scenes | `fireanalysis:findhlsscenes` | done (2.0.0-dev) |
| 1 | Fetch HLS NBR | `fireanalysis:fetchhlsnbr` | done (2.0.0-dev) |
| 2 | Build dNBR (align, offset, RdNBR, RBR) | `fireanalysis:builddnbr` | done (2.0.0-dev) |
| 2 | Prepare local scene NBR (Landsat C2 L2, ARD, Sentinel-2 L2A) | `fireanalysis:preparelocalnbr` | done (2.0.0-dev) |
| 3 | Planetary Computer and Earth Search providers | | |
| 4 | Scene pair suggestion (AOI-clear %, day-of-year match) | `fireanalysis:suggesthlspairs` | done (2.0.0-dev) |
| 5 | Threshold estimates and `dnbr6` | `fireanalysis:classifyseverity` | done (2.0.0-dev) |
| 5 | Perimeter draft | | |
| 6 | Whole pipeline in one step, with report | `fireanalysis:hlsburnseverity` | done (2.0.0-dev); layout PDF still to do |
| 5 | Burn severity summary | `fireanalysis:burnseveritysummary` | done (1.1.0) |
| 6 | Layout PDF (map, chart) | | |

**Find HLS scenes.** Inputs: AOI polygons, start and end dates, sensors
(L30, S30), maximum scene cloud cover. Output: footprint polygons (EPSG:4326),
one per granule, with `granule`, `sensor`, `tile`, `date`, `doy`, `cloud`
(scene %), `aoi_pct` (share of the AOI inside the footprint). Search needs no
login.

**Fetch HLS NBR.** Inputs: AOI, the scenes to use (features from *Find HLS
scenes*, all of one date and one sensor; several tiles are mosaicked), buffer
(default 3 km, the FMT3 subset margin, needed later for the offset ring),
target CRS (default: the WGS 84 UTM zone of the AOI centroid), Fmask classes to
mask. Grid: 30 m, extent snapped to multiples of 30 m, so two runs with the same
AOI and CRS give identical grids. Reads NIR, SWIR2, red and Fmask windows
through `/vsicurl/` (nearest neighbour inside the tiles' own zone, bilinear for
reflectance when reprojecting), scales by 0.0001, masks fill and the chosen
Fmask bits, and writes NBR (Float32, raw units, nodata −9999). Optional
outputs: SWIR2 / NIR / red reflectance stack for display, and the Fmask window.
The log reports the share of the AOI removed by each Fmask class, because
Fmask can call fresh burn scars shadow or water.

Fmask bits (HLS v2.0): 0 cirrus, 1 cloud, 2 adjacent to cloud or shadow,
3 cloud shadow, 4 snow/ice, 5 water, 6–7 aerosol level (3 = high); 255 = fill.
NBR bands: L30 B05 / B07, S30 B8A / B12; red is B04 in both.

## Code layout

```
burn_severity_summary.py   standalone script (unchanged ids and parameters)
fireanalysis/              package shipped inside the plugin
  earthdata.py             netrc check, GDAL path-specific options
  stac.py                  STAC search through QgsBlockingNetworkRequest
  hls.py                   HLS constants, granule ids, URLs, Fmask
  algorithms/              one module per Processing algorithm
plugin/                    provider and plugin entry point
dev/                       build, smoke test (offline), online test
```

Network calls use `QgsBlockingNetworkRequest`, which follows QGIS proxy and SSL
settings and can be cancelled from Processing feedback. GDAL handles raster
reads. Online tests are separate (`dev/online_test.py`) so the offline smoke
test still runs without a network or a login.

## Validation

**Done (2026-10-06): Lab 6 rebuild and HLS.** On the test-data burned area
(284,008 px, grid from *Fetch HLS NBR*), each calibration choice was rebuilt from
the lab's raw files and compared with `dNBR.tif`:

| dNBR built from | mean | RMSE vs lab | Unb / Low / ModL / ModH / High % |
|---|---|---|---|
| lab `dNBR.tif` | 0.278 | — | 1.3 / 50.5 / 38.0 / 10.2 / 0.0 |
| S2 DN/10000 (no offset) − L9 raw DN | 0.278 | **0.026** | 1.5 / 50.2 / 37.8 / 10.4 / 0.0 |
| S2 (DN−1000)/10000 − L9 DN x 0.0000275 − 0.2 (correct) | 0.476 | 0.240 | 2.3 / 20.1 / 26.7 / 26.9 / 24.0 |
| HLS S30 2024-09-13 − L30 2025-09-15 | 0.500 | 0.265 | 2.2 / 17.3 / 25.8 / 27.7 / 27.0 |

So the lab dNBR has both errors: no S2 offset and unscaled Landsat. HLS agrees
with the correct calibration: pre-fire NBR r = 0.998 with the offset-corrected
S2 (mean difference 0.025), post-fire NBR r = 0.992 with the scaled L9 (mean
difference 0.001; HLS L30 is built from the same LC09 path 37 acquisition). The
remaining gap is bandpass adjustment and BRDF normalization. For context, the
BAER Soil Burn Severity map is 7 / 65 / 26 / 2 % unburned-very low / low /
moderate / high; soil severity normally reads lower than dNBR classes.

**Fmask water in the burn.** Inside the test-data burned area, Fmask flags
0.03 % as water before the fire and 0.66 % after (1,827 pixels new). Their
pre-fire NBR is 0.41 (vegetated), so these are burn scars, spread over all
classes with a tilt to moderate-high and high. Small here, so water stays masked
by default (real water would otherwise reach the offset ring and the classes);
the Fetch log reports the share. Pixels with zero or negative NIR or SWIR2 get
no NBR: 1,048 in the post-fire grid, 1,024 of them flagged water.

**dNBR offset.** Ring 90–2,090 m outside the full Lab 6 perimeter: 0.026,
std 0.053, robust 0.037, 0.1 % of the ring above 0.27. Around the clipped
test-data perimeter the ring runs into the fire (14 % above 0.27, std 0.22),
which is what the burned-sample warning catches. The median still held (0.036).

**Done (2026-10-06): 17 published MTBS fires.** `dev/mtbs_validation.py` reruns
every MTBS wildfire over 15,000 acres in AZ, NM, CO and UT from 2022 to 2024 (Yellow
Lake 2024 was not yet in the image service) from HLS, with MTBS's pre/post dates
and sensor family, perimeter and thresholds, and compares the classes with the
MTBS `dnbr6` mosaic inside the fire's own perimeter (the yearly mosaic also holds
neighbouring fires: Pipeline 2022 borders Tunnel 2022, which had burned before
Pipeline's pre-fire date; comparing the whole mosaic first gave a false 63 %).

- Mean agreement 88.7 %, kappa 0.775 (0.711 to 0.864); within one class on at
  least 99.8 % of pixels; high-severity share within 1 point. Landsat (L8, L9,
  L8 to L9) and Sentinel-2 (S2A, S2B, mixed) pairs do equally well.
- Oracle thresholds (best reproduction of MTBS's classes on our dNBR) are within
  6 (low) and 12 (high), x1000, of MTBS's on average: our dNBR is on the MTBS scale.
- Ring offset vs MTBS's sampled offset: median difference 9, up to 47 (x1000).

**Threshold estimates** (`dev/threshold_study.py`, leave-one-out on the same
fires): MAE x1000 for low / moderate / high and mean kappa of the classes.

| Rule | Low | Mod | High | Kappa |
|---|---|---|---|---|
| MTBS's own thresholds | | | | 0.775 |
| Median of nearby MTBS fires, 150 km, 10 years | 21 | 55 | 88 | 0.56 |
| Same, 100 km / 250 km | 20 / 18 | 53 / 60 | 86 / 96 | 0.57 / 0.57 |
| Offset + 2 SD (ring) | 53 | | | |
| Offset + 2 robust SD (ring) | 35 | | | 0.49 (with the two rules below) |
| RdNBR >= 750 match, all pixels | | | 265 | |
| RdNBR >= 750 match where pre-fire NBR >= 0.5 | | | 52 (no high: 4 wrong of 17) | |
| Moderate midway between MTBS low and high | | 29 | | |
| Key & Benson 100 / 270 / 660 | | 51 | 169 | 0.48 |

Everything plateaus near kappa 0.56: the gap to 0.775 is the analyst's judgment
(MTBS high thresholds run from 350 to 775 on fires of similar vegetation). The
default is the nearby-fires median: general, explainable, and it carries
regional analyst practice. The plain RdNBR >= 750 match fails in the Southwest
because sparse pre-fire vegetation inflates RdNBR (half of these fires' area
reaches 750), which the FMT3 guide warns about for heterogeneous cover.
