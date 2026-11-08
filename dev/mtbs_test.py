"""
Online test of the MTBS fire records: the cache of the USGS records file, fire
lookup, nearby fires, Find MTBS fires, matching a fire to its record, and the
whole pipeline from an MTBS fire id (Tunnel 2022: perimeter from the map service,
MTBS's image pair from HLS, MTBS's thresholds, comparison with MTBS's classes).

With --download, also the one-time 420 MB perimeter file (kept in
$TMPDIR/fireanalysis_mtbs_cache_dev between runs) and the pipeline for Dragon
Bravo 2025, a fire the map service lacks: its perimeter from the file and
MTBS's Sentinel-2B / Sentinel-2C pair from HLS. Needs ~/.netrc for HLS.

Headless (macOS):
    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app mtbs_test.py [--download]
"""
import json
import os
import sys
import tempfile
import time

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:  # exec() from the console has no __file__
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit"
sys.path.insert(0, REPO)
DOWNLOAD = "--download" in sys.argv
os.environ["FIREANALYSIS_MTBS_CACHE"] = (os.path.join(tempfile.gettempdir(), "fireanalysis_mtbs_cache_dev") if DOWNLOAD
                                         else tempfile.mkdtemp(prefix="fireanalysis_mtbs_cache_"))

from qgis.core import (Qgis, QgsApplication, QgsGeometry, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback, QgsProcessingUtils, QgsProject, QgsVectorLayer)

_app = None
if QgsApplication.instance() is None:
    _app = QgsApplication([], False)
    _app.initQgis()
    from processing.core.Processing import Processing
    Processing.initialize()

import datetime  # noqa: E402

from fireanalysis import VERSION, mtbs  # noqa: E402
from fireanalysis.algorithms.burn_severity_pipeline import BurnSeverityPipeline  # noqa: E402
from fireanalysis.algorithms.find_mtbs_fires import FindMtbsFires  # noqa: E402

OUT = tempfile.mkdtemp(prefix="fireanalysis_mtbs_test_")
TUNNEL = "AZ3530411158920220417"
DRAGON = "AZ3623811210320250704"


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.log = []

    def reportError(self, error, fatalError=False):
        self.log.append("ERROR " + error)

    def pushInfo(self, info):
        self.log.append(info)

    def pushWarning(self, warning):
        self.log.append("WARNING " + warning)


def _run(alg, params):
    alg.initAlgorithm()
    p = {d.name(): d.defaultValue() for d in alg.parameterDefinitions()}
    p.update(params)
    context = QgsProcessingContext()
    context.setProject(QgsProject.instance())
    feedback = _Feedback()
    try:
        res, ok = alg.create().run(p, context, feedback)
    except Exception as e:
        return None, feedback.log + ["ERROR " + str(e)], context
    return (res if ok else None), feedback.log, context


failed = 0


def check(name, ok, detail=""):
    global failed
    failed += not ok
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok or not detail else "\n      " + str(detail)[:1500]))


print(f"Wildfire Analysis Toolkit {VERSION} MTBS test on QGIS {Qgis.QGIS_VERSION}; cache "
      f"{os.environ['FIREANALYSIS_MTBS_CACHE']}; outputs in {OUT}")

# 1. The records file: downloaded once, then reused; a HEAD check finds it unchanged
fb = _Feedback()
t0 = time.time()
path = mtbs.ensure("records", fb)
recs = mtbs.load_records(path)
info = mtbs.cache_info("records")
check(f"records: {len(recs):,} fires cached ({time.time() - t0:.1f} s), USGS file of {info.get('last_modified')}",
      len(recs) > 30000 and info.get("last_modified") and os.path.exists(path), " | ".join(fb.log))
mtime = os.path.getmtime(path)
fb = _Feedback()
mtbs.ensure("records", fb, force_check=True)
check("records: a check against an unchanged USGS file downloads nothing",
      os.path.getmtime(path) == mtime and not any("Downloading" in m for m in fb.log), " | ".join(fb.log))
saved = mtbs.DATASETS["records"]
mtbs.DATASETS["records"] = ("https://invalid.invalid/mtbs.zip",) + saved[1:]
fb = _Feedback()
try:
    again = mtbs.ensure("records", fb, force_check=True)
finally:
    mtbs.DATASETS["records"] = saved
check("records: offline, the cached copy is used with a warning",
      again == path and any(m.startswith("WARNING Could not check") for m in fb.log), " | ".join(fb.log))

# 2. Lookup
r = mtbs.resolve(recs, "Dragon Bravo 2025")
check(f"lookup: Dragon Bravo 2025 -> {r['event_id']}, thresholds {r['low_t']:.0f}/{r['mod_t']:.0f}/{r['high_t']:.0f}, "
      f"offset {r['dnbr_offst']:.0f}, post {r['post_id']}",
      r["event_id"] == DRAGON and (r["low_t"], r["mod_t"], r["high_t"], r["dnbr_offst"]) == (66, 216, 471, 22)
      and r["post_id"].startswith("C12S"))
tunnel = mtbs.resolve(recs, TUNNEL)
check(f"lookup by id: {mtbs.describe(tunnel)}", tunnel["incid_name"].upper() == "TUNNEL" and tunnel["year"] == 2022)
fires, where = mtbs.nearby(r["lon"], r["lat"], 150, 2015, 2025)
check(f"nearby: {len(fires)} wildfires within 150 km, 2015-2025, from {where}",
      len(fires) >= 20 and where.startswith("MTBS fire records") and all(f["incid_type"] == "Wildfire" for f in fires))

# 3. Find MTBS fires
res, log, ctx = _run(FindMtbsFires(), {"SEARCH": "Tunnel 2022", "OUTPUT": "TEMPORARY_OUTPUT"})
layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx) if res else None
feats = list(layer.getFeatures()) if layer else []
check(f"Find MTBS fires: Tunnel 2022 perimeter(s) from the map service ({len(feats)})",
      any(f["event_id"] == TUNNEL and f["perimeter"].startswith("MTBS map service") and f.geometry().area() > 0
          for f in feats), " | ".join(log))
check("Find MTBS fires: the record's fields are on the output",
      feats and all(f["low_t"] is not None and f["ig_date"] and f["pre_id"] for f in feats))
res, log, ctx = _run(FindMtbsFires(), {"START_YEAR": 2024, "END_YEAR": 2024, "GEOMETRY": 1,
                                       "OUTPUT": "TEMPORARY_OUTPUT"})
layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx) if res else None
check(f"Find MTBS fires: points for 2024 ({layer.featureCount() if layer else 0})",
      layer is not None and layer.featureCount() > 100, " | ".join(log))
res, log, ctx = _run(FindMtbsFires(), {"OUTPUT": "TEMPORARY_OUTPUT"})
check("Find MTBS fires: perimeters need a filter", res is None and any("Give a name" in m for m in log))
if not DOWNLOAD:
    res, log, ctx = _run(FindMtbsFires(), {"SEARCH": DRAGON, "OUTPUT": "TEMPORARY_OUTPUT"})
    layer = QgsProcessingUtils.mapLayerFromString(res["OUTPUT"], ctx) if res else None
    check("Find MTBS fires: a fire the map service lacks asks for the 420 MB download",
          layer is not None and layer.featureCount() == 0 and any("420 MB" in m for m in log), " | ".join(log))

# 4. Matching a fire given without a record
gj, how = mtbs.perimeter(tunnel)
check(f"perimeter: Tunnel from the {how}", gj is not None and gj["features"][0]["properties"]["event_id"] == TUNNEL)
tunnel_path = os.path.join(OUT, "tunnel.geojson")
with open(tunnel_path, "w") as f:
    json.dump(gj, f)
geom = QgsGeometry.unaryUnion([f.geometry() for f in QgsVectorLayer(tunnel_path, "t", "ogr").getFeatures()])
from qgis.core import QgsCoordinateReferenceSystem  # noqa: E402
ig = datetime.date.fromisoformat(tunnel["ig_date"])
found, how = BurnSeverityPipeline._match(None, geom, QgsCoordinateReferenceSystem("EPSG:4326"), ig,
                                         ig + datetime.timedelta(days=20), tunnel["burnbndac"], QgsProcessingContext(),
                                         _Feedback())
check(f"match by location and date: {found and found['event_id']} ({how})", found and found["event_id"] == TUNNEL)
found, how = BurnSeverityPipeline._match({"irwin": "{" + r["irwinid"].lower() + "}"}, geom,
                                         QgsCoordinateReferenceSystem("EPSG:4326"), None, None, None,
                                         QgsProcessingContext(), _Feedback())
check(f"match by WFIGS IRWIN id: {found and found['event_id']}", found and found["event_id"] == DRAGON)

pipe = BurnSeverityPipeline()
pipe.initAlgorithm()
fb = _Feedback()
pair = pipe._mtbs_pair(tunnel, "landsat", tunnel_path, QgsProcessingContext(), fb)
check("MTBS pair: Landsat chosen, MTBS used Sentinel-2: no MTBS pair (a Landsat pair is suggested)",
      pair is None and any("a pair is suggested" in m for m in fb.log), " | ".join(fb.log))
pair = pipe._mtbs_pair(tunnel, "s2", tunnel_path, QgsProcessingContext(), _Feedback())
check(f"MTBS pair: Sentinel-2 chosen: MTBS's scenes from Planetary Computer ({pair and pair['pre'][1]})",
      pair is not None and pair["pre"][1] == "s2" and pair["pre"][2] == mtbs.parse_scene(tunnel["pre_id"])["date"].isoformat())

# 5. The pipeline from an MTBS fire id alone (Tunnel 2022, HLS)
folder = os.path.join(OUT, "tunnel")
t0 = time.time()
res, log, ctx = _run(BurnSeverityPipeline(), {"MTBS_FIRE": TUNNEL, "OUTPUT_FOLDER": folder, "LOAD": 2})
check(f"pipeline from an MTBS fire id: runs ({time.time() - t0:.0f} s)", res is not None,
      " | ".join([m for m in log if "WARNING" in m or "ERROR" in m] + log[-5:]))
rep = json.load(open(os.path.join(folder, "report.json"))) if res else {}
m = rep.get("mtbs") or {}
check("pipeline: MTBS perimeter, record and name", m.get("event_id") == TUNNEL and rep.get("fire") == "Tunnel"
      and m.get("perimeter_source", "").startswith("MTBS map service")
      and os.path.exists(os.path.join(folder, "mtbs_perimeter.geojson")), str(m)[:400])
pair = rep.get("pair") or {}
check(f"pipeline: MTBS's own image pair ({pair.get('pre_date')} {pair.get('pre_sensor')} / {pair.get('post_date')} "
      f"{pair.get('post_sensor')})", m.get("pair_used") and pair.get("pre_date") ==
      mtbs.parse_scene(tunnel["pre_id"])["date"].isoformat(), str(pair)[:300])
t = rep.get("thresholds") or {}
check(f"pipeline: MTBS's thresholds at this pair's offset ({t.get('low')}, {t.get('moderate')}, {t.get('high')}; "
      f"offset {rep.get('offset', {}).get('value', 0):.3f})", t.get("from_mtbs"), str(t.get("notes")))
c = m.get("comparison") or {}
check(f"pipeline: compared with MTBS's classes (kappa {c.get('kappa', 0):.3f}, {c.get('agreement', 0):.1f} % agree)",
      c.get("kappa", 0) > 0.6 and os.path.exists(os.path.join(folder, "mtbs_dnbr6.tif")), str(c)[:300])
page = open(os.path.join(folder, "report.html"), encoding="utf-8").read() if res else ""
check("pipeline: the report has the MTBS section", "<h2>MTBS record</h2>" in page and "Compared with MTBS" in page)

# 6. Optional: the 420 MB perimeter file, and Dragon Bravo 2025 (not in the map service)
if DOWNLOAD:
    fb = _Feedback()
    t0 = time.time()
    gj, how = mtbs.perimeter(r, fb, allow_download=True)
    check(f"perimeter file: Dragon Bravo from the {how} ({time.time() - t0:.0f} s)",
          gj is not None and "file" in how, " | ".join(fb.log))
    folder = os.path.join(OUT, "dragon_bravo")
    t0 = time.time()
    res, log, ctx = _run(BurnSeverityPipeline(), {"MTBS_FIRE": "Dragon Bravo 2025", "MTBS_DOWNLOAD": True,
                                                  "OUTPUT_FOLDER": folder, "LOAD": 2})
    check(f"pipeline, Dragon Bravo 2025: runs ({time.time() - t0:.0f} s)", res is not None,
          " | ".join([m for m in log if "WARNING" in m or "ERROR" in m] + log[-5:]))
    rep = json.load(open(os.path.join(folder, "report.json"))) if res else {}
    pair = rep.get("pair") or {}
    check(f"pipeline, Dragon Bravo: MTBS's pair {pair.get('pre_date')} {pair.get('pre_sensor')} / "
          f"{pair.get('post_date')} {pair.get('post_sensor')} (Sentinel-2C)",
          pair.get("pre_date") == "2025-06-20" and pair.get("post_date") == "2026-06-20"
          and pair.get("post_sensor") == "sentinel-2c", str(pair)[:300])
    # HLS lacks MTBS's post-fire tile 12SUF on 2026-06-20: both scenes then come from Planetary Computer
    check(f"pipeline, Dragon Bravo: both scenes from one archive ({pair.get('fetched_from')})",
          pair.get("fetched_from", {}).get("pre") == pair.get("fetched_from", {}).get("post"))
    six = next((m for m in rep.get("log", {}).get("classifyseverity", []) if "Non-mapping" in m), "")
    check(f"pipeline, Dragon Bravo: under 2 % of the burned area unmapped ({six.strip() or 'none'})",
          not six or float(six.split(":")[1].split("%")[0]) < 2)
    t = rep.get("thresholds") or {}
    print(f"      thresholds {t.get('low')}, {t.get('moderate')}, {t.get('high')}; offset "
          f"{rep.get('offset', {}).get('value', 0):.3f}; comparison {(rep.get('mtbs') or {}).get('comparison')}")
    check("pipeline, Dragon Bravo: MTBS thresholds, extended assessment, no published classes yet",
          t.get("from_mtbs") and rep.get("assessment", "").startswith("Extended")
          and "not" in str((rep.get("mtbs") or {}).get("comparison", {}).get("note", "")))

print(f"\n{'ALL PASSED' if not failed else f'{failed} FAILED'}")
if _app is not None:
    _app.exitQgis()
    sys.exit(1 if failed else 0)
elif failed:
    raise RuntimeError(f"{failed} MTBS test check(s) failed")
