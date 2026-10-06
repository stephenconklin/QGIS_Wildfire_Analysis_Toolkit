# QGIS Burn Severity Summary

A QGIS Processing tool that classifies a dNBR (differenced Normalized Burn Ratio) raster into USGS burn severity classes and reports how much of two areas falls in each class, as percent, hectares and acres.

It was built to compare a whole fire with one watershed inside it: the 2025 Dragon Bravo Fire on the North Rim of Grand Canyon National Park and the Bright Angel Creek watershed. It works for any fire and any sub-area. The tool replaces about a dozen manual steps (fix geometries, dissolve, intersect, tag, merge, reclassify, zonal histogram, field calculations and reshaping) with one dialog. Its last output is a one-row-per-class table ready for a QGIS layout bar chart.

## Requirements

- QGIS 4.x. Developed and tested on QGIS 4.2.3. The script also includes fallbacks for QGIS 3.28 and later, but has not yet been tested on 3.x.
- No extra plugins or Python packages; it uses only QGIS's built-in Processing tools.

## Installation

1. Download `burn_severity_summary.py`.
2. In QGIS, open the **Processing Toolbox**. Click the Python icon at the top and choose **Add Script to Toolbox…**.
3. Select the file. The tool appears under **Scripts › Fire Analysis › Burn severity summary**.

QGIS copies the file into your profile's `processing/scripts` folder. If you download a newer version later, add it again the same way.

## Inputs

| Parameter | Description |
|---|---|
| **Raw dNBR raster** | Pre-fire NBR minus post-fire NBR, in its native units (about −0.6 to 1.3). **Do not use a rescaled (0–1) or ×1000 copy**: the default thresholds only apply to raw dNBR. The raster must be in a projected CRS with meter units. |
| **Burned-area polygons (zone A)** | Polygons of the burned area to summarize. They are dissolved into one zone. |
| **Watershed boundary (zone B)** | Any polygon layer. Zone B is the part of zone A inside it. |
| **Zone A name / Zone B name** | Labels written to the `zone` field. Defaults: *Dragon Bravo*, *Bright Angel*. |
| **Severity thresholds** | Reclassification table: Minimum, Maximum, Value, using *min ≤ value < max*. Defaults to the USGS thresholds below. Rows can be added or changed for fire-specific thresholds. |
| **Class names** | Comma-separated names in class-value order. Must match the number of rows in the table. |

Default USGS thresholds (Key & Benson 2006):

| Minimum | Maximum | Value | Class |
|---|---|---|---|
| −1e10 | 0.10 | 1 | Unburned (includes enhanced regrowth) |
| 0.10 | 0.27 | 2 | Low |
| 0.27 | 0.44 | 3 | Moderate-low |
| 0.44 | 0.66 | 4 | Moderate-high |
| 0.66 | 1e10 | 5 | High |

## Outputs

Each output can be a temporary layer or a saved file (GeoPackage recommended).

| Output | Contents |
|---|---|
| **Severity zones** | Two polygons, one per zone, with a single `zone` field. |
| **Severity classes (USGS)** | Integer raster of class values; 0 is NoData. |
| **Zonal histogram** | The zones with pixel counts per class: `C_1` … `C_5`. |
| **Severity area summary** | A table with no geometry and one row per class. This is the chart source. |

Summary table fields:

| Field | Meaning |
|---|---|
| `ord` | Sort order, 1–5. |
| `sev_class` | Class name. |
| `pct_db`, `pct_ba` | Percent of zone A's / zone B's classified pixels. |
| `ha_db`, `ha_ba` | Hectares. |
| `ac_db`, `ac_ba` | Acres, rounded to whole acres. |

The `db` / `ba` suffixes are fixed: `db` is always zone A and `ba` is always zone B, whatever names you give the zones. This keeps a saved layout chart working when you rerun the tool.

## Method

The tool runs these steps. The tool names are the Processing Toolbox names, so the workflow can also be followed by hand.

### 1. Build the two analysis zones

1. **Fix Geometries** (`native:fixgeometries`) on the burned-area polygons and on the watershed.
2. **Dissolve** (`native:dissolve`) the fixed burned area with no dissolve fields into one polygon: **zone A**.
3. **Intersection** (`native:intersection`) of zone A with the fixed watershed, then **Dissolve** again: **zone B**, the burned area inside the watershed.
4. **Field Calculator** (`native:fieldcalculator`) on each zone adds a text field `zone` holding the zone's name.
5. **Merge Vector Layers** (`native:mergevectorlayers`) combines both zones in the dNBR raster's CRS.
6. **Retain Fields** (`native:retainfields`) keeps only `zone`.

### 2. Classify dNBR

**Reclassify by Table** (`native:reclassifybytable`) on band 1 of the dNBR raster, using the threshold table, *min ≤ value < max*, NoData for values outside every range, output NoData 0 and an Int32 output.

### 3. Count pixels per class in each zone

**Zonal Histogram** (`native:zonalhistogram`) on the classified raster with the zones, column prefix `C_`. A pixel counts toward a zone when its center falls inside the polygon. A class with no pixels in either zone is treated as 0.

### 4. Compute area and percent

For each class *i*, with pixel area taken from the raster's cell size:

| Value | Formula |
|---|---|
| Hectares | `C_i × pixel area (ha)`. A 30 m pixel is 0.09 ha. |
| Acres | `round(hectares × 2.4710538)` |
| Percent of zone | `100 × C_i / (C_1 + … + C_n)` |

The results are written as one row per class so a chart can use the class as its category axis.

## Example: Dragon Bravo Fire, 2025

dNBR from 30 m imagery, NAD83 / UTM 12N (EPSG:26912).

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
- **Series 1:** X = `"sev_class"`, Y = `"pct_db"`.
- **Series 2:** X = `"sev_class"`, Y = `"pct_ba"`.
- **Y axis:** percent of each zone's burned area.

Plotting percent puts both zones on the same scale despite their different sizes. Use `ac_db` and `ac_ba` for bar labels to keep the absolute areas visible.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Almost nothing in *Unburned* and far too much in *High* | The dNBR raster is rescaled (for example to 0–1). Use the raw dNBR raster. |
| "dNBR raster must be in a CRS with meter units" | Reproject the dNBR raster to a projected CRS (for example UTM) with **Warp (Reproject)** first. |
| "Invalid value for minimum" when editing the threshold table | A pasted minus sign may be a typographic `−` rather than a hyphen `-`. Type the value with the keyboard. |
| "N classes in the table but M class names" | Give one class name per table row, in the same order. |
| The tool still shows old behavior after an update | Add the new file to the Toolbox again, then refresh the Toolbox or restart QGIS. |

## Limitations

- **Generic thresholds.** The Key & Benson (2006) thresholds were not calibrated for any particular fire. Field-validated burn severity products such as BAER Soil Burn Severity maps use fire-specific thresholds.
- **dNBR depends on pre-fire vegetation.** Absolute dNBR understates severity in sparse vegetation. Relativized indices (RdNBR, RBR) compare vegetation types more fairly.
- **Sensor consistency.** Pre- and post-fire images from different sensors (for example Sentinel-2 and Landsat) can shift dNBR values.

## References

- Key, C. H., & Benson, N. C. (2006). Landscape Assessment (LA). In *FIREMON: Fire Effects Monitoring and Inventory System* (Gen. Tech. Rep. RMRS-GTR-164-CD). USDA Forest Service, Rocky Mountain Research Station.

## Author

Stephen Conklin
