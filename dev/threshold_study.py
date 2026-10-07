"""
Compare threshold-estimation rules with MTBS's analyst-chosen thresholds, on the
outputs of dev/mtbs_validation.py (no network needed).

    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app threshold_study.py <validation output folder>

For each fire it computes, on our HLS dNBR:
  - the "oracle" thresholds that best reproduce MTBS's own classes (the best any
    rule could do on this dNBR),
  - each candidate rule for the low, moderate and high thresholds,
and then the mean absolute error of each rule against MTBS (x1000 units) and the
kappa of the resulting classes against the MTBS dnbr6.
"""
import glob
import json
import os
import sys

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit"
sys.path.insert(0, REPO)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsCoordinateTransform, QgsCoordinateTransformContext, QgsGeometry, QgsVectorLayer)

_app = None
if QgsApplication.instance() is None:
    _app = QgsApplication([], False)
    _app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal  # noqa: E402

from fireanalysis import mtbs as mtbs_service, raster  # noqa: E402
from fireanalysis.algorithms.classify_severity import match_threshold  # noqa: E402

gdal.UseExceptions()
NONE = 9999
CTX = QgsCoordinateTransformContext()


def load(folder):
    a = json.load(open(os.path.join(folder, "perimeter.geojson")))["features"][0]["properties"]
    ds = gdal.Open(os.path.join(folder, "dnbr.tif"))
    d = ds.ReadAsArray().astype(float)
    d[d == -9999] = np.nan
    pre = gdal.Open(os.path.join(folder, "nbr_pre.tif")).ReadAsArray().astype(float)
    pre[pre == -9999] = np.nan
    mtbs = gdal.Open(os.path.join(folder, "mtbs_dnbr6.tif")).ReadAsArray()
    lyr = QgsVectorLayer(os.path.join(folder, "perimeter.geojson"), "p", "ogr")
    g = QgsGeometry.unaryUnion([f.geometry() for f in lyr.getFeatures()])
    inside = raster.rasterize(g, lyr.crs(), ds, CTX)
    rc = QgsCoordinateReferenceSystem.fromWkt(ds.GetProjection())
    gr = QgsGeometry(g)
    gr.transform(QgsCoordinateTransform(lyr.crs(), rc, CTX))
    ring = raster.rasterize(gr.buffer(2090, 16).difference(gr.buffer(90, 16)), rc, ds, CTX) & ~inside
    return a, d, pre, mtbs, inside, ring & np.isfinite(d)


def classes(d, low, mod, high, green=-0.150):
    c = np.ones(d.shape, np.uint8)
    c[d >= low] = 2
    c[d >= mod] = 3
    c[d >= high] = 4
    c[d <= green] = 5
    return c


def kappa(a, b):
    conf = np.zeros((5, 5))
    np.add.at(conf, (a - 1, b - 1), 1)
    n = conf.sum()
    po, pe = np.trace(conf) / n, (conf.sum(0) * conf.sum(1)).sum() / n ** 2
    return (po - pe) / (1 - pe)


def rnd(x):
    return round(x * 200) / 200


def high_rules(d, pre, off, ok, extended):
    r_high = 0.640 if extended else 0.750
    r = (d - off) / np.sqrt(np.maximum(np.abs(pre), 0.001))
    out = {}
    for name, sel in [("all", ok)] + [(f"pre>={p}", ok & (pre >= p)) for p in (0.3, 0.4, 0.5)] + \
            [(f"pre>=P{q}", ok & (pre >= np.nanpercentile(pre[ok], q))) for q in (75, 90)]:
        tgt = r[sel] >= r_high
        if sel.sum() < 100 or tgt.mean() < 0.005:
            out[name] = NONE / 1000
            continue
        out[name] = rnd(match_threshold(d[sel], tgt, lo=0.0)[0])
    out["cascade"] = next((out[k] for k in ("pre>=0.5", "pre>=0.4", "pre>=0.3") if out[k] < NONE / 1000),
                          NONE / 1000)
    p90 = np.nanpercentile(pre[ok], 90)
    out["invert P90"] = rnd(off + r_high * np.sqrt(max(p90, 0.001)))  # dNBR where RdNBR = r_high at dense pre-NBR
    return out


def main(root):
    rows = []
    for folder in sorted(glob.glob(os.path.join(root, "*/"))):
        if not os.path.exists(os.path.join(folder, "mtbs_dnbr6.tif")):
            continue
        a, d, pre, mtbs, inside, ring = load(folder)
        ok = inside & np.isfinite(d) & np.isfinite(pre) & (mtbs >= 1) & (mtbs <= 5)
        if ok.sum() < 1000:
            continue
        v = d[ring]
        off, sd = float(np.median(v)), float(v.std())
        rob = 1.4826 * float(np.median(np.abs(v - off)))
        T = [a["low_threshold"], a["moderate_threshold"], a["high_threshold"]]
        extended = str(a["asmnt_type"]).startswith("Extended")
        oracle = []
        for c in (2, 3, 4):
            t = match_threshold(d[ok], mtbs[ok] >= c, lo=-0.2, hi=1.5)[0] if (mtbs[ok] >= c).mean() > 0.001 else NONE / 1000
            oracle.append(t)
        lows = {"off+2sd": rnd(off + 2 * sd), "off+2rob": rnd(off + 2 * rob), "off+2.5rob": rnd(off + 2.5 * rob),
                "off+3rob": rnd(off + 3 * rob)}
        highs = high_rules(d, pre, off, ok, extended)
        regional = {}
        for radius in (100, 150, 250):
            fires = mtbs_service.nearby_fires(a["longitude"], a["latitude"], radius, a["year"] - 10, a["year"])
            t, n, nf, scope = mtbs_service.median_thresholds(fires, a["asmnt_type"], exclude={a["fire_id"]})
            regional[radius] = ([NONE if v is None else v * 1000 for v in t], nf)
        rows.append({"regional": regional, "name": a["fire_name"], "mtbs": T, "oracle": [x * 1000 for x in oracle],
                     "offset": off * 1000, "sd": sd * 1000, "rob": rob * 1000,
                     "lows": {k: x * 1000 for k, x in lows.items()}, "highs": {k: x * 1000 for k, x in highs.items()},
                     "d": d, "ok": ok, "mtbs_cls": mtbs})
        print(f"{a['fire_name'][:14]:14s} MTBS {T}  oracle {[round(x * 1000) for x in oracle]}  "
              f"off {off * 1000:.0f} sd {sd * 1000:.0f} rob {rob * 1000:.0f}  "
              f"lows {[round(x * 1000) for x in lows.values()]}  highs " +
              " ".join(f"{k}:{x * 1000:.0f}" for k, x in highs.items()))

    def err(vals, ref):
        e = [abs(v - r) for v, r in zip(vals, ref) if r < NONE and v < NONE]
        both_none = sum(1 for v, r in zip(vals, ref) if (r >= NONE) == (v >= NONE))
        return (np.mean(e) if e else float("nan")), both_none

    print(f"\n{len(rows)} fires. Mean absolute error vs MTBS (x1000); 'none agree' = fires where rule and MTBS agree on whether the class exists")
    ref_low = [r["mtbs"][0] for r in rows]
    ref_high = [r["mtbs"][2] for r in rows]
    print(f"  oracle low   {err([r['oracle'][0] for r in rows], ref_low)[0]:6.1f}   oracle high {err([r['oracle'][2] for r in rows], ref_high)[0]:6.1f}")
    for k in rows[0]["lows"]:
        print(f"  low  {k:12s} {err([r['lows'][k] for r in rows], ref_low)[0]:6.1f}")
    for k in rows[0]["highs"]:
        e, agree = err([r["highs"][k] for r in rows], ref_high)
        print(f"  high {k:12s} {e:6.1f}   none agree {agree}/{len(rows)}")

    # Leave-one-out regional default: each fire gets the median MTBS threshold of the other fires
    # (over fires that have the class)
    def loo(i, k):
        vals = [r["mtbs"][k] for j, r in enumerate(rows) if j != i and r["mtbs"][k] < NONE]
        return float(np.median(vals)) if vals else NONE
    for i, r in enumerate(rows):
        r["loo"] = [loo(i, k) for k in range(3)]
    for radius in (100, 150, 250):
        vals = [r["regional"][radius][0] for r in rows]
        print(f"  radius {radius} km (n fires {min(r['regional'][radius][1] for r in rows)}-"
              f"{max(r['regional'][radius][1] for r in rows)}): low {err([v[0] for v in vals], ref_low)[0]:5.1f}, "
              f"mod {err([v[1] for v in vals], [r['mtbs'][1] for r in rows])[0]:5.1f}, "
              f"high {err([v[2] for v in vals], ref_high)[0]:5.1f}")
    print(f"  low  LOO median     {err([r['loo'][0] for r in rows], ref_low)[0]:6.1f}")
    print(f"  mod  LOO median     {err([r['loo'][1] for r in rows], [r['mtbs'][1] for r in rows])[0]:6.1f}")
    print(f"  high LOO median     {err([r['loo'][2] for r in rows], ref_high)[0]:6.1f}")
    print(f"  high Key-Benson 660 {err([660] * len(rows), ref_high)[0]:6.1f}")
    print(f"  mod  midway (MTBS low, high) {err([(r['mtbs'][0] + r['mtbs'][2]) / 2 if r['mtbs'][2] < NONE else NONE for r in rows], [r['mtbs'][1] for r in rows])[0]:6.1f}")
    print(f"  mod  Key-Benson 270 {err([270] * len(rows), [r['mtbs'][1] for r in rows])[0]:6.1f}")

    # Kappa of whole rule sets against MTBS classes
    print("\nKappa against MTBS dnbr6 (none -> class off)")
    sets = [("MTBS thresholds", lambda r: r["mtbs"]), ("oracle", lambda r: r["oracle"]),
            ("Key & Benson 100/270/660", lambda r: [100, 270, 660]),
            ("LOO median", lambda r: r["loo"]),
            ("off+2rob / LOO mod, high", lambda r: [r["lows"]["off+2rob"], r["loo"][1], r["loo"][2]]),
            ("off+2rob / 270 / LOO high", lambda r: [r["lows"]["off+2rob"], 270, r["loo"][2]])]
    def mid(lo, hi):
        return (lo + hi) / 2 if hi < NONE else NONE
    for radius in (100, 150, 250):
        sets.append((f"radius {radius} km medians", lambda r, radius=radius: r["regional"][radius][0]))
        sets.append((f"radius {radius} low, mod / cascade high",
                     lambda r, radius=radius: [r["regional"][radius][0][0],
                                               min(r["regional"][radius][0][1], r["highs"]["cascade"]),
                                               r["highs"]["cascade"]]))
    for lname, lfun in (("low 50", lambda r: 50), ("low LOO", lambda r: r["loo"][0]),
                        ("low off+2rob", lambda r: r["lows"]["off+2rob"])):
        for hname in ("cascade", "pre>=0.5"):
            sets.append((f"{lname} / midway / {hname}",
                         lambda r, lfun=lfun, hname=hname: [lfun(r), mid(lfun(r), r["highs"][hname]), r["highs"][hname]]))
        sets.append((f"{lname} / LOO mod / cascade",
                     lambda r, lfun=lfun: [lfun(r), min(r["loo"][1], r["highs"]["cascade"]), r["highs"]["cascade"]]))
    for lk in ("off+2sd", "off+2rob"):
        for hk in ("all", "pre>=0.5", "pre>=P90"):
            for mk in ("midway", "270"):
                def f(r, lk=lk, hk=hk, mk=mk):
                    lo, hi = r["lows"][lk], r["highs"][hk]
                    mod = 270 if mk == "270" else ((lo + hi) / 2 if hi < NONE else NONE)
                    return [lo, min(mod, hi), hi]
                sets.append((f"{lk} / {mk} / {hk}", f))
    for name, f in sets:
        ks = []
        for r in rows:
            t = [x / 1000 for x in f(r)]
            ks.append(kappa(r["mtbs_cls"][r["ok"]], classes(r["d"][r["ok"]], *t)))
        print(f"  {name:34s} mean kappa {np.mean(ks):.3f}  min {np.min(ks):.3f}")


main(sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("/var/folders/*/*/T/fireanalysis_mtbs_*"))[-1])
if _app is not None:
    _app.exitQgis()
