# Test data

A small clip of the 2025 Dragon Bravo Fire data (North Rim, Grand Canyon National Park), for trying the tool and for `dev/smoke_test.py`. Everything is in NAD83 / UTM zone 12N (EPSG:26912) with 30 m pixels. The window is the Bright Angel Creek watershed's extent plus a 2 km margin (about 20 × 31 km).

| File | Contents |
|---|---|
| `dnbr.tif` | Raw dNBR (pre-fire NBR minus post-fire NBR), Float32. |
| `prefire_nbr.tif` | Pre-fire NBR, Float32, for RdNBR and RBR. It is on a slightly different grid from `dnbr.tif`; the tool resamples it. |
| `testdata.gpkg`, layer `burn_area` | Burned-area polygons, clipped to the window (zone A). |
| `testdata.gpkg`, layer `watershed` | Bright Angel Creek watershed boundary (zone B). |
| `testdata.gpkg`, layer `subareas` | Test polygons with a `name` field: the watershed split into west and east halves, a box with no burned area inside, and a polygon with no name. Use it with *Sub-area name field* = `name`. |

`dev/make_testdata.py` rebuilds these files from the full data.

## Expected results (dNBR, USGS thresholds)

| Zone | Unburned | Low | Moderate-low | Moderate-high | High |
|---|---|---|---|---|---|
| Burned area in the window (zone A) | 7,248 | 140,263 | 107,045 | 31,598 | 33 |
| Bright Angel (zone B) | 3,997 | 78,398 | 57,213 | 16,001 | 9 |

Values are pixel counts. The whole watershed is inside the window, so the Bright Angel counts match a run on the full data.

## Sources

- **dNBR** was computed from pre-fire Sentinel-2 imagery (13 September 2024) and post-fire Landsat imagery (September 2025). Contains modified Copernicus Sentinel data (2024). Landsat data courtesy of the U.S. Geological Survey.
- **Pre-fire NBR**: Sentinel-2, 13 September 2024. Contains modified Copernicus Sentinel data (2024).
- **Burned-area polygons and the watershed boundary** were derived for GEOG 400 (Raster GIS) Lab 6 at Fort Lewis College.

The pre- and post-fire images come from different sensors, so treat the RdNBR and RBR results as a test of the tool rather than as a calibrated severity assessment.
