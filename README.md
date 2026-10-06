# QGIS Burn Severity Summary

A QGIS Processing tool that classifies burn severity from a dNBR raster (or RdNBR or RBR) and reports how much of a burned area, and of any sub-areas inside it, falls in each class, as percent, hectares and acres.

It was built to compare the 2025 Dragon Bravo Fire on the North Rim of Grand Canyon National Park with the Bright Angel Creek watershed inside it, and works for any fire and any set of sub-areas. One dialog replaces about a dozen manual steps (fix geometries, dissolve, intersect, tag, merge, reclassify, zonal histogram, field calculations and reshaping). The last output is a one-row-per-class table ready for a QGIS layout bar chart.

![Burn severity of the Dragon Bravo Fire, with the Bright Angel Creek watershed outlined](docs/images/severity_map.png)

![Bar chart of burn severity as percent of burned area for the Dragon Bravo Fire and the Bright Angel watershed](docs/images/chart.png)

## Requirements

- QGIS 3.40 or later, including QGIS 4. Tested on QGIS 3.44 and 4.2.
- No extra plugins or Python packages. It uses only QGIS's built-in Processing tools.

## Installation

Choose one.

**As a plugin.** Build the zip with `python3 dev/build_plugin.py`, then in QGIS use *Plugins › Manage and Install Plugins › Install from ZIP* and pick `dist/qgis_burn_severity-<version>.zip`. The tool appears in the Processing Toolbox under **Fire Analysis › Burn severity summary** (algorithm id `fireanalysis:burnseveritysummary`).

**As a script.** In the Processing Toolbox, click the Python icon and choose **Add Script to Toolbox…**, then pick `burn_severity_summary.py`. The tool appears under **Scripts › Fire Analysis › Burn severity summary** (algorithm id `script:burnseveritysummary`). QGIS copies the file into your profile's `processing/scripts` folder, so add it again after downloading a new version.

![The Burn severity summary dialog](docs/images/dialog.png)

## Quick start

Try it on the bundled [test data](testdata/README.md):

| Parameter | Value |
|---|---|
| Raw dNBR raster | `testdata/dnbr.tif` |
| Burned-area polygons | `testdata/testdata.gpkg`, layer `burn_area` |
| Sub-area polygons | `testdata/testdata.gpkg`, layer `watershed` |

Leave everything else at the defaults and click **Run**. The Bright Angel row of the zonal histogram should read 3,997 / 78,398 / 57,213 / 16,001 / 9 pixels.

## Inputs

| Parameter | Description |
|---|---|
| **Raw dNBR raster** | Pre-fire NBR minus post-fire NBR, in raw units (about −0.6 to 1.3). It must be in a projected CRS with meter units. |
| **Severity index and thresholds** | **dNBR**, USGS thresholds (Key & Benson 2006, five classes; default); **RdNBR** (Miller & Thode 2007) or **RBR** (Parks et al. 2014), each four classes. See [Severity indices](#severity-indices). |
| **Pre-fire NBR raster** | Raw pre-fire NBR (−1 to 1). Required for RdNBR and RBR, ignored for dNBR. It may be on a different grid; it is resampled to the dNBR grid. |
| **Burned-area polygons (zone A)** | Polygons of the burned area. They are dissolved into one zone. |
| **Zone A name** | Label for zone A. Default *Dragon Bravo*. |
| **Sub-area polygons** | Optional. Any polygon layer, such as a watershed. Each sub-area zone is the part of zone A inside it. Leave empty to summarize zone A alone. |
| **Sub-area name field** | Optional. With a field set, each distinct value becomes its own zone, for example one per sub-watershed. Without one, all sub-area polygons together form one zone B. |
| **Zone B name** | Label for zone B when no name field is set. Default *Bright Angel*. |

Advanced:

| Parameter | Description |
|---|---|
| **Custom thresholds** | A reclassification table (Minimum, Maximum, Value; min ≤ value < max) that replaces the preset for the chosen index. Empty means use the preset. |
| **Custom class names** | Comma-separated names in table order. Empty means the preset names (or *Class 1*, *Class 2*… with a custom table). |
| **Summary field code for zone A / zone B** | Short codes used in the summary field names. Defaults `db` and `ba`. |

### Rescaled input is refused

The preset thresholds only make sense for raw index values. When they are in use, the tool stops if the dNBR raster's values all lie between 0 and 1 (a 0–1 rescaled copy) or reach beyond ±2 (a ×1000 copy), and if a pre-fire NBR lies outside −1 to 1. With custom thresholds the same checks only warn.

## Outputs

Each output can be a temporary layer or a saved file (GeoPackage recommended).

| Output | Contents |
|---|---|
| **Severity zones** | One polygon per zone, with a `zone` field. |
| **Severity classes** | Integer raster of class values; 0 is NoData. |
| **Zonal histogram** | The zones with pixel counts per class: `C_1`, `C_2`, … |
| **Severity area summary** | A table with no geometry and one row per class. This is the chart source. |

Summary table fields:

| Field | Meaning |
|---|---|
| `ord` | Sort order, 1 to the number of classes. |
| `sev_class` | Class name. |
| `pct_<code>` | Percent of the zone's classified pixels, one field per zone. |
| `ha_<code>` | Hectares. |
| `ac_<code>` | Acres, rounded to whole acres. |

`<code>` is `db` for zone A and `ba` for zone B unless changed under Advanced, which gives `pct_db`, `pct_ba`, `ha_db` and so on. Keeping the codes fixed means a saved layout chart keeps working when you rerun the tool with other zone names. When a name field creates one zone per value, each zone's code is the first six letters and digits of its name in lowercase (for example `Bright Angel West` → `bright`), with a number added if two would clash. Six characters keep every field name within the 10-character limit of shapefiles.

## Severity indices

| Index | Formula (raw units) | Classes and thresholds |
|---|---|---|
| dNBR | NBR<sub>pre</sub> − NBR<sub>post</sub> | Unburned < 0.10 ≤ Low < 0.27 ≤ Moderate-low < 0.44 ≤ Moderate-high < 0.66 ≤ High |
| RdNBR | dNBR / √\|NBR<sub>pre</sub>\| | Unchanged < 0.069 ≤ Low < 0.315 ≤ Moderate < 0.640 ≤ High |
| RBR | dNBR / (NBR<sub>pre</sub> + 1.001) | Unchanged < 0.035 ≤ Low < 0.130 ≤ Moderate < 0.298 ≤ High |

Published RdNBR and RBR thresholds are in ×1000 units (69, 315, 640 and 35, 130, 298). They are divided by 1000 here because the tool works in raw units. For RdNBR, \|NBR<sub>pre</sub>\| is floored at 0.001 so bare or sparse pixels can't divide by nearly zero. RdNBR and RBR adjust dNBR for how much vegetation there was before the fire, so they compare sparse and dense vegetation more fairly than dNBR does.

## Method

The tool runs these steps. The names are those in the Processing Toolbox, so the workflow can also be followed by hand.

### 1. Build the zones

1. **Fix Geometries** (`native:fixgeometries`) on the burned-area polygons, and on the sub-areas if given.
2. **Dissolve** (`native:dissolve`) the fixed burned area into one polygon: **zone A**.
3. **Dissolve** the sub-areas, by the name field if one is set.
4. **Intersection** (`native:intersection`) of the sub-areas with zone A, then **Dissolve** again: the **sub-area zones**, the burned area inside each sub-area.
5. **Field Calculator** (`native:fieldcalculator`) adds a text field `zone` to each zone layer: the zone A name, the zone B name, or the name field's value.
6. **Merge Vector Layers** (`native:mergevectorlayers`) combines the zones in the dNBR raster's CRS, and **Retain Fields** (`native:retainfields`) keeps only `zone`.

### 2. Classify severity

For RdNBR or RBR, **Raster calculator** (`native:modelerrastercalc`) first computes the index from dNBR and the pre-fire NBR on the dNBR grid.

**Reclassify by Table** (`native:reclassifybytable`) then classifies band 1 with the threshold table, using min ≤ value < max, NoData for values outside every range, output NoData 0 and an Int32 output. If the output has no CRS, the tool writes the dNBR's CRS into it.

### 3. Count pixels per class in each zone

**Zonal Histogram** (`native:zonalhistogram`) on the classified raster with the zones, column prefix `C_`. A pixel counts toward a zone when its center falls inside the polygon. A class with no pixels in any zone counts as 0.

### 4. Compute area and percent

For each class *i*, with the pixel area taken from the raster's cell size:

| Value | Formula |
|---|---|
| Hectares | `C_i × pixel area (ha)`. A 30 m pixel is 0.09 ha. |
| Acres | `round(hectares × 2.4710538)` |
| Percent of zone | `100 × C_i / (C_1 + … + C_n)` |

The results are written as one row per class, so a chart can use the class as its category axis.

## Example: Dragon Bravo Fire, 2025

dNBR from 30 m imagery, NAD83 / UTM 12N (EPSG:26912), USGS thresholds.

| Class | Dragon Bravo % | Dragon Bravo ac | Bright Angel % | Bright Angel ac |
|---|---|---|---|---|
| Unburned | 2.11 | 2,570 | 2.57 | 889 |
| Low | 53.14 | 64,673 | 50.38 | 17,435 |
| Moderate-low | 34.53 | 42,021 | 36.77 | 12,724 |
| Moderate-high | 10.22 | 12,433 | 10.28 | 3,559 |
| High | 0.01 | 10 | 0.01 | 2 |
| **Total** | **100** | **121,707** | **100** | **34,609** |

Totals from pixel counts are slightly below the polygon areas because of the pixel-center rule.

## Charting the results in a layout

The summary table plugs straight into a QGIS layout chart item (*Add Item › Add Chart*):

- **Chart type:** bar chart.
- **Source layer:** the summary table, with **Sort features** on, sorted by `ord`, ascending.
- **One series per zone:** X = `"sev_class"`, Y = `"pct_db"` for zone A, `"pct_ba"` for zone B, and so on.
- **Y axis:** percent of each zone's burned area.

Plotting percent puts zones of different sizes on the same scale. Use the `ac_` fields for bar labels to keep the absolute areas visible.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "…looks rescaled to 0-1" or "…looks scaled (for example x1000)" | The raster isn't in raw units. Use the raw dNBR or NBR. If it really is raw, enter thresholds under *Custom thresholds* to skip the check. |
| "dNBR raster must be in a CRS with meter units" | Reproject the dNBR to a projected CRS such as UTM with **Warp (Reproject)** first. |
| "…needs the pre-fire NBR raster" | RdNBR and RBR are computed from the pre-fire NBR. Add it, or choose dNBR. |
| "N classes in the threshold table but M class names" | Give one class name per table row, in the same order. |
| "No burned area inside: …" | Those sub-areas don't overlap the burned area and are left out of the summary. |
| "Invalid value for minimum" when typing a threshold | A pasted minus sign may be a typographic `−` rather than a hyphen `-`. Type it with the keyboard. |
| The tool still shows old behavior after an update | Reinstall the plugin, or add the script again, then refresh the Toolbox or restart QGIS. |

## Limitations

- **Generic thresholds.** None of the preset thresholds were calibrated for a particular fire. Field-validated products such as BAER Soil Burn Severity maps use fire-specific thresholds. Use custom thresholds when you have calibrated ones.
- **Sensor consistency.** Pre- and post-fire images from different sensors (for example Sentinel-2 and Landsat) can shift NBR values and with them every index.
- **Pixel-center rule.** Zonal Histogram counts a pixel in a zone only if its center is inside, so small or narrow zones are approximated.

## Development

| Path | Purpose |
|---|---|
| `burn_severity_summary.py` | The tool, a single file. |
| `plugin/` | Plugin wrapper: provider, entry point, `metadata.txt`, icon. |
| `dev/build_plugin.py` | Builds `dist/qgis_burn_severity-<version>.zip`. |
| `dev/smoke_test.py` | Runs every mode on `testdata/` and checks the counts, inside QGIS (Python Console) or headless. |
| `dev/run_headless.sh` | Runs the smoke test with a QGIS app's bundled Python, for example `dev/run_headless.sh /Applications/QGIS.app`. |
| `dev/make_testdata.py` | Rebuilds `testdata/` from the full Lab 6 data. |

Version history is in [CHANGELOG.md](CHANGELOG.md).

## References

- Key, C. H., & Benson, N. C. (2006). Landscape Assessment (LA). In *FIREMON: Fire Effects Monitoring and Inventory System* (Gen. Tech. Rep. RMRS-GTR-164-CD). USDA Forest Service, Rocky Mountain Research Station.
- Miller, J. D., & Thode, A. E. (2007). Quantifying burn severity in a heterogeneous landscape with a relative version of the delta Normalized Burn Ratio (dNBR). *Remote Sensing of Environment*, 109(1), 66–80.
- Parks, S. A., Dillon, G. K., & Miller, C. (2014). A new metric for quantifying burn severity: The relativized burn ratio. *Remote Sensing*, 6(3), 1827–1844.

## License

MIT. See [LICENSE](LICENSE).

## Author

Stephen Conklin
