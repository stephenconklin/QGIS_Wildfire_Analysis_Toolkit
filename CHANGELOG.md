# Changelog

## 1.0.0 — 2026-10-06

- First release. One Processing tool (**Scripts › Fire Analysis › Burn severity summary**, id `script:burnseveritysummary`) that builds a burned-area zone and a watershed sub-zone, classifies raw dNBR into USGS (Key & Benson 2006) severity classes, counts pixels per class with Zonal Histogram, and writes a one-row-per-class summary table (percent, hectares and acres for each zone) for layout charts.
- Editable threshold table and class names; pixel area read from the raster.
- `dev/smoke_test.py` checks results against the Dragon Bravo / Bright Angel Lab 6 counts.
