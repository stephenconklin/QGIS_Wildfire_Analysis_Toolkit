"""
Draws the plugin icon, plugin/icon.png (256 x 256): a flame whose outline is a
burned-area perimeter, filled like one of the plugin's severity maps: the MTBS
classes nested as flames (unburned to low at the edge, low, moderate, and the
high-severity core low in the flame, where a fire burns hottest), on a
land-coloured tile with faint contours.

Headless (macOS), from the repository root:
    dev/run_headless.sh /Applications/QGIS-final-4_2_3.app make_icon.py [out.png] [preview folder] [size]
The website copy: make_icon.py "$PWD/docs/images/icon_512.png" "" 512
Or in the QGIS Python console:
    import sys; sys.argv = ["", "/path/to/plugin/icon.png"]
    exec(open("/path/to/dev/make_icon.py").read())
"""
import math
import os
import sys

from qgis.PyQt.QtCore import QPointF, QRectF, Qt
from qgis.PyQt.QtGui import (QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath,
                             QPen, QTransform)

try:
    REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
except NameError:
    REPO = "/Users/stephenconklin/Documents/ConklinGeospatial/Projects/GITHub/QGIS_Wildfire_Analysis_Toolkit"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "plugin", "icon.png")
PREVIEW = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
SIZE = int(sys.argv[3]) if len(sys.argv) > 3 else 256  # 512 for the website copy, docs/images/icon_512.png

app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
S = 512  # drawn at twice the size, then scaled down for smooth edges

# The MTBS dnbr6 class colours (#006400, #7fffd4, #ffff00, #ff0000), a touch deeper so
# the neon yellow and aquamarine don't glare at icon size
UNBURNED_LOW, LOW, MODERATE, HIGH = "#0d6b2c", "#5fdcb8", "#ffe03a", "#e3221b"


def blob(cx, cy, rx, ry, angle, waves, n=120, tail=0.0):
    """A smooth irregular closed shape: an ellipse (rx, ry, rotated by angle degrees)
    whose radius wobbles by the (harmonic, amplitude, phase) waves. tail narrows the
    back end (local -x), giving the teardrop of a wind-driven fire."""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        k = 1 + sum(amp * math.sin(h * a + ph) for h, amp, ph in waves)
        k *= 1 - tail * (1 - math.cos(a)) / 2
        pts.append(QPointF(rx * k * math.cos(a), ry * k * math.sin(a)))
    path = QPainterPath()
    # quadratic curves through the midpoints: smooth, no corners
    mid = lambda p, q: QPointF((p.x() + q.x()) / 2, (p.y() + q.y()) / 2)  # noqa: E731
    path.moveTo(mid(pts[-1], pts[0]))
    for i, p in enumerate(pts):
        path.quadTo(p, mid(p, pts[(i + 1) % n]))
    path.closeSubpath()
    t = QTransform()
    t.translate(cx, cy)
    t.rotate(angle)
    return t.map(path)


def flame(scale=1.0, cx=256, base=446, dx=0.0, dy=0.0):
    """The flame outline (drawn for 512 px), scaled about the middle of its base."""
    path = QPainterPath()
    path.moveTo(256, 446)
    path.cubicTo(150, 446, 104, 372, 112, 300)    # left bulge
    path.cubicTo(118, 236, 160, 200, 170, 146)    # up to the left tongue
    path.cubicTo(194, 186, 206, 200, 216, 214)    # back in, under the tongue
    path.cubicTo(212, 160, 236, 104, 268, 52)     # main tip
    path.cubicTo(304, 110, 334, 150, 338, 196)    # down the right side
    path.cubicTo(350, 178, 358, 158, 356, 132)    # right tongue
    path.cubicTo(400, 196, 412, 252, 406, 306)    # right bulge
    path.cubicTo(398, 382, 352, 446, 256, 446)    # round the base
    path.closeSubpath()
    t = QTransform()
    t.translate(cx + dx, base + dy)
    t.scale(scale, scale)
    t.translate(-256, -446)
    return t.map(path)


def _rounded(rect, radius):
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def draw(size=S):
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / S, size / S)

    # Land tile: rounded square, soft dry-grass gradient
    tile = QRectF(24, 24, S - 48, S - 48)
    grad = QLinearGradient(0, 24, 0, S - 24)
    grad.setColorAt(0, QColor("#f1ead2"))
    grad.setColorAt(1, QColor("#d8cca4"))
    p.setPen(QPen(QColor("#8c7f5a"), 6))
    p.setBrush(QBrush(grad))
    p.drawRoundedRect(tile, 84, 84)

    # Faint terrain contours on the land
    p.save()
    p.setClipPath(_rounded(tile, 84))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(140, 120, 70, 60), 3))
    for i, r in enumerate((60, 110, 160, 210, 260, 310)):
        p.drawPath(blob(150, 380, r * 1.25, r, 18, [(3, 0.05, i * 0.7), (5, 0.03, 1.0 + i)]))
    p.restore()

    # The flame, filled like a severity map: the classes nested from the edge to the
    # hot core low in the flame
    outer = flame()
    low = [flame(0.80, dy=-14)]
    moderate = [flame(0.58, dx=3, dy=-26)]
    high = [flame(0.34, dx=5, dy=-34)]

    def fill(path, colour, edge_alpha=60):
        p.setBrush(QColor(colour))
        p.setPen(QPen(QColor(0, 0, 0, edge_alpha), 3))
        p.drawPath(path)

    # a soft shadow lifts the flame off the land
    p.save()
    p.translate(6, 8)
    p.setBrush(QColor(60, 45, 20, 50))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPath(outer)
    p.restore()
    fill(outer, UNBURNED_LOW, 0)
    p.save()
    p.setClipPath(outer)  # every class patch stays inside the burned area
    for path in low:
        fill(path, LOW)
    for path in moderate:
        fill(path, MODERATE)
    for path in high:
        fill(path, HIGH)
    p.restore()
    # Burned-area perimeter, as the plugin draws it on its maps
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#000000"), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.drawPath(outer)
    p.end()
    return img


img = draw().scaled(SIZE, SIZE, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
img.save(OUT)
print(f"Wrote {OUT}")
if PREVIEW:
    os.makedirs(PREVIEW, exist_ok=True)
    # how it looks where QGIS shows it: Plugin Manager (64), toolbox (16, 24), on light and dark
    sheet = QImage(560, 300, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#ffffff"))
    sp = QPainter(sheet)
    sp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    sp.fillRect(280, 0, 280, 300, QColor("#2b2b2b"))
    for x0 in (0, 280):
        sp.drawImage(QRectF(x0 + 10, 10, 256, 256), img)
    sheet2 = QImage(560, 120, QImage.Format.Format_ARGB32)
    sheet2.fill(QColor("#ffffff"))
    sq = QPainter(sheet2)
    sq.fillRect(280, 0, 280, 120, QColor("#2b2b2b"))
    for x0 in (0, 280):
        x = x0 + 10
        for s in (64, 32, 24, 16):
            sq.drawImage(QRectF(x, 20, s, s), img.scaled(s, s, Qt.AspectRatioMode.IgnoreAspectRatio,
                                                         Qt.TransformationMode.SmoothTransformation))
            x += s + 16
    sq.end()
    sp.end()
    sheet.save(os.path.join(PREVIEW, "icon_large.png"))
    sheet2.save(os.path.join(PREVIEW, "icon_small.png"))
    print(f"Previews in {PREVIEW}")
