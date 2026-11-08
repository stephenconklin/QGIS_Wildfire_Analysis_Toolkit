# Prompt: update the plugin's web page

Paste the text below into a Claude Code session opened in the repository of
conklingeospatial.com (an Astro site), after you have uploaded 2.1.1 to plugins.qgis.org.
It assumes the plugin is still awaiting review there. Once QGIS staff have approved it,
change the line marked **(review status)** to say it is listed, before pasting.

---

Update the project page for my QGIS plugin, the Wildfire Analysis Toolkit, at
`/projects/qgis-wildfire-analysis-toolkit/` (find its source file under the projects
content). The page was written while version 2.0 was in development. Bring it up to
date with the released plugin (version 2.1, which adds MTBS fire records) and give it
the plugin's new icon.
Keep the page's existing structure, tone (plain, technical, concrete numbers), images and
fire gallery. Change or add only what the facts below support; don't invent features,
numbers, quotes or test results. Show me the diff before committing.

## What changed since the page was written

1. **Released.** Version 2.1.1 was released on 2026-11-08 (GitHub tag `v2.1.1`:
   https://github.com/stephenconklin/QGIS_Wildfire_Analysis_Toolkit/releases/tag/v2.1.1)
   and is the first version submitted to the official QGIS plugin repository
   (plugins.qgis.org). Versions 2.0.0 and 2.1.0 were tagged on GitHub but never
   published there; 2.1.1 is 2.1.0 with the new icon.
   **(review status)** It has been submitted to plugins.qgis.org and is awaiting review by
   QGIS staff. Replace "version 2.0 in development" in the hero label with "QGIS plugin,
   2026 (version 2.1)". While it awaits review, say so in one short line in the install
   section ("Submitted to the QGIS plugin repository; awaiting review"), and give the
   GitHub install route below instead of the Plugin Manager search.
2. **Install.** Once it is listed: in QGIS, *Plugins › Manage and Install Plugins*,
   search for "Wildfire Analysis Toolkit", *Install Plugin*. Until then: download the
   plugin zip from the repository (build it with `python3 dev/build_plugin.py`, as the
   README explains) and use *Install from ZIP* in the same dialog. Requires QGIS 3.44 or later,
   including QGIS 4. Free and open source, GPL-2.0-or-later. Code, README and issue
   tracker: https://github.com/stephenconklin/QGIS_Wildfire_Analysis_Toolkit. Add a short
   "Get it" section or line with these links, near the top or at the end.
3. **MTBS fire records, fetched automatically (new in 2.1).** This is the headline
   feature for the update; give it its own section after "The pipeline".
   - MTBS (Monitoring Trends in Burn Severity, USGS and USFS) has mapped more than
     31,000 US fires since 1984 (31,514 in the records file on 2026-11-08). For each fire
     it publishes the name, ignition date, assessment type, the pre- and post-fire
     satellite scenes it used, the dNBR offset and the analyst's severity thresholds.
   - The plugin downloads the MTBS fire records file from USGS (about 3.5 MB) the first
     time it is needed, caches it in the QGIS profile, checks at most once a day for a
     newer copy, and keeps working offline from the cache. Users never download or load
     MTBS data themselves.
   - New tool **Find MTBS fires**: look up fires by name ("Dragon Bravo 2025"), MTBS fire
     id, years or area, with the whole MTBS record, as perimeters or points.
   - **The whole pipeline from a fire's name.** Type an MTBS fire's id or name and year
     into *Burn severity (whole pipeline)* and leave everything else empty. It fetches
     the fire's perimeter, uses MTBS's own image dates and assessment type, applies
     MTBS's thresholds (adjusted to this image pair's unburned offset), and compares the
     result with MTBS's published classes. Example: Tunnel Fire, Arizona, 2022 (17,641
     acres), from its fire id alone, ran in 23 seconds to about 3.5 minutes (depending on
     the imagery servers) and agreed with MTBS's published classes on 87 % of pixels
     (Cohen's kappa 0.75).
   - A perimeter the user gives is matched to its MTBS record automatically: a NIFC/WFIGS
     perimeter by its IRWIN incident id, any other by an MTBS fire inside it that ignited
     between the fire's dates.
   - Perimeters come from the USFS MTBS map service. For the newest fires (the last year
     or so), which only the full USGS perimeter file has, the user can allow a one-time
     420 MB download, which is then cached.
   - Supports Sentinel-2C scenes, which MTBS uses for 2025 fires such as Dragon Bravo.
   - MTBS's image pair comes from NASA HLS when HLS has those scenes; when it lacks one
     (a missing tile, Landsat 4–7) or its search service is down, both scenes come from
     Microsoft Planetary Computer, so a pair is always processed the same way.
   - Nearby-fire threshold estimates now use the cached records: more current than the
     map service (420 fires from 2025 on, against 90 in the map service) and they no
     longer depend on it staying up.
4. **Dragon Bravo in MTBS.** MTBS has now published the Dragon Bravo Fire
   (AZ3623811210320250704): an extended assessment from Sentinel-2B (2025-06-20) and
   Sentinel-2C (2026-06-20), 148,455 acres, thresholds 66 / 216 / 471 (dNBR x1000).
   From its name alone ("Dragon Bravo 2025"), the pipeline fetched MTBS's perimeter
   (from the one-time 420 MB file), took MTBS's own Sentinel-2B and Sentinel-2C scenes
   from Microsoft Planetary Computer (NASA HLS lacks one of the tiles MTBS used for that
   date), applied MTBS's thresholds, and classified the 148,539 acres as 8.6 % unburned to
   low, 27.4 % low, 40.6 % moderate and 23.4 % high, in about three minutes. MTBS has not
   yet published its class map for 2025 fires, so there is no pixel comparison for this
   fire yet.
   If it fits, add one sentence to the "Extended assessment" section noting that MTBS's
   own extended assessment uses a 2026-06-20 post-fire image, close to the page's
   2026-06-30 one.
5. **Validation, with its basis** (the page states the kappa without its sample). Keep
   the claim and add what it rests on:
   - 17 published MTBS fires over 15,000 acres in Arizona, New Mexico, Colorado and Utah,
     2022–2024, rerun from NASA HLS with MTBS's dates, perimeters and thresholds: mean
     kappa 0.775 (range 0.711–0.864), 88.7 % of pixels in the same class, at least 99.8 %
     within one class.
   - 6 fires from 2000–2012 through Planetary Computer Landsat: mean kappa 0.69.
   - Estimated thresholds (for fires MTBS hasn't mapped) are a starting point: about 0.56
     with the plain nearby-fire median on the 17 fires, 0.61 with the offset adjustment on
     21 fires.
   - Drafted perimeters: mean IoU 0.69–0.73 against 18 MTBS perimeters with a fixed
     threshold; 0.78 with the offset-aware draft inside a generous outline. The 0.78 for
     IoU and the 0.78 for kappa are separate results that happen to round alike; say so,
     or show kappa as 0.775.
6. **Tool list.** The plugin now has 13 Processing tools in four groups: Fire records
   (Find MTBS fires), Pipeline (whole pipeline, severity map PDF), Imagery (find and
   fetch HLS; find and fetch Landsat / Sentinel-2 from Planetary Computer; prepare local
   scenes; suggest image pairs) and Severity (build dNBR, draft burned-area perimeter,
   classify severity, burn severity summary).
7. **Stack.** Add "USGS MTBS fire records and perimeters, USFS MTBS map and image
   services" to the Stack list.

## Branding: the plugin icon

The plugin has a new icon: a flame whose black outline is a burned-area perimeter,
filled with the MTBS severity classes nested as smaller flames (green unburned to low at
the edge, aquamarine low, yellow moderate, red high core), on a beige land tile with
faint contours. It is the icon QGIS shows in the Plugin Manager and on every tool in the
Processing Toolbox, so use it on the site too, for continuity:

- **File.** A 512 × 512 PNG with a transparent background around the rounded tile, at
  `/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit/docs/images/icon_512.png`
  (or, if that path isn't reachable, from
  https://raw.githubusercontent.com/stephenconklin/QGIS_Wildfire_Analysis_Toolkit/v2.1.1/docs/images/icon_512.png).
  Copy it into the site's assets next to this project's other images, and let the site's
  image pipeline make the sizes it needs. Don't redraw, recolour or crop it.
- **Where.** Beside the page title in the hero (about 64–96 px, with alt text "Wildfire
  Analysis Toolkit icon"), and on this project's card in the projects list, if cards
  there show icons or thumbnails. First look at how the Stacked Terrain Builder project
  page (the "Next project" link) and its card use that plugin's icon, if they do, and
  follow the same pattern and sizes, so the two plugin pages match.
- If the site sets per-page favicons or social preview images, use the icon only where
  that is already the site's pattern; otherwise leave them alone.

## Images

Don't create or alter images. If a new screenshot would help the MTBS section (the
pipeline dialog with only *MTBS fire* filled in, the report's *MTBS record* and
comparison section, or *Find MTBS fires* results on the map), add a placeholder comment
in the page source naming the screenshot and where it goes, and list them for me.
No new screenshots exist yet: use placeholders only.

## Text Update

Replace the following text:
"The toolkit started as a single Processing script. It was built to compare the 2025 Dragon Bravo Fire on the North Rim of Grand Canyon National Park with the Bright Angel Creek watershed inside it, and it grew into a full fire analysis pipeline modeled on the USGS/USFS MTBS QGIS Fire Mapping Tool."

With this text (preserving the Fire Mapping Tool hyperlink, and making corrections as necessary):
"The toolkit started as a single Processing script. It was built as a companion to a manual burn severity analysis, and used to compare the 2025 Dragon Bravo Fire on the North Rim of Grand Canyon National Park with the Bright Angel Creek watershed inside it. The toolkit grew into a full fire analysis pipeline modeled on the USGS/USFS MTBS QGIS Fire Mapping Tool."

## Checks

- Every number on the page must come from this prompt or already be on the page.
- Keep links working: the GitHub repository above and the external MTBS links.
- Build the site locally and check the page at phone width before committing.