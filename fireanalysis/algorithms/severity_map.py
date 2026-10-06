"""Severity map (PDF): a print layout of the MTBS classes with the burned area,
optional sub-areas, legend, scale bar, north arrow, the area summary and a
provenance note, exported to PDF and PNG. Layouts must be built on the main
thread, so this algorithm does not run in the background."""
import datetime

from qgis.core import (Qgis, QgsFeatureRequest, QgsFillSymbol, QgsLayoutExporter, QgsLayoutItemLabel,
                       QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutItemPicture, QgsLayoutItemScaleBar,
                       QgsLayoutPoint, QgsLayoutSize, QgsPalettedRasterRenderer, QgsPrintLayout,
                       QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFileDestination, QgsProcessingParameterRasterLayer,
                       QgsProcessingParameterString, QgsProcessingParameterVectorLayer, QgsProject, QgsRasterLayer,
                       QgsRectangle, QgsUnitTypes)
from qgis.PyQt.QtGui import QColor, QFont

from .. import VERSION
from ..compat import _try, polygon_source
from .classify_severity import CLASS_COLORS, CLASS_NAMES, NON_MAPPING

PAGE_W, PAGE_H = 297.0, 210.0  # A4 landscape, mm


def _mm():
    return _try(lambda: Qgis.LayoutUnit.Millimeters, lambda: QgsUnitTypes.LayoutMillimeters)


def _no_threading_flag():
    return _try(lambda: Qgis.ProcessingAlgorithmFlag.NoThreading, lambda: QgsProcessingAlgorithm.FlagNoThreading)


def _class_renderer(layer):
    classes = [QgsPalettedRasterRenderer.Class(v, QColor(CLASS_COLORS[v]), CLASS_NAMES[v])
               for v in sorted(CLASS_NAMES) if v != NON_MAPPING]
    classes.append(QgsPalettedRasterRenderer.Class(NON_MAPPING, QColor('#bdbdbd'), CLASS_NAMES[NON_MAPPING]))
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes))


def _label(layout, text, x, y, w, h, size=10, bold=False, html=False):
    item = QgsLayoutItemLabel(layout)
    item.setText(text)
    if html:
        item.setMode(_try(lambda: QgsLayoutItemLabel.Mode.ModeHtml, lambda: QgsLayoutItemLabel.ModeHtml))
    font = QFont('Arial', size)
    font.setBold(bold)
    try:
        from qgis.core import QgsTextFormat
        fmt = QgsTextFormat()
        fmt.setFont(font)
        fmt.setSize(size)
        item.setTextFormat(fmt)
    except (ImportError, AttributeError):
        item.setFont(font)
    layout.addLayoutItem(item)
    item.attemptMove(QgsLayoutPoint(x, y, _mm()))
    item.attemptResize(QgsLayoutSize(w, h, _mm()))
    return item


class SeverityMap(QgsProcessingAlgorithm):
    CLASSES = 'CLASSES'
    PERIMETER = 'PERIMETER'
    SUBAREAS = 'SUBAREAS'
    SUMMARY = 'SUMMARY'
    TITLE = 'TITLE'
    NOTE = 'NOTE'
    PDF = 'PDF'
    PNG = 'PNG'

    def name(self):
        return 'severitymap'

    def displayName(self):
        return 'Severity map (PDF)'

    def group(self):
        return 'Pipeline'

    def groupId(self):
        return 'pipeline'

    def flags(self):
        return super().flags() | _no_threading_flag()

    def shortHelpString(self):
        return (
            '<p>Makes an A4 landscape map of the MTBS severity classes (from <i>Classify severity</i> or '
            '<i>Burn severity (whole pipeline)</i>, <code>dnbr6.tif</code>) with the burned-area outline, optional '
            'sub-areas, a legend, scale bar and north arrow, the area summary table, and a note of the '
            'images, offset and thresholds read from the raster metadata. Exports PDF and, optionally, '
            'PNG. The layout is built in a separate project, so the current project is not changed.</p>'
            '<p>Runs on the main thread (QGIS layouts require it), so QGIS waits while it works.</p>'
            f'<p><i>Version {VERSION}</i></p>')

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(self.CLASSES, 'Severity classes (dnbr6)'))
        self.addParameter(QgsProcessingParameterFeatureSource(self.PERIMETER, 'Burned-area polygons',
                                                              [polygon_source()]))
        self.addParameter(QgsProcessingParameterFeatureSource(self.SUBAREAS, 'Sub-area polygons (optional)',
                                                              [polygon_source()], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.SUMMARY, 'Severity area summary table (optional)', optional=True))
        self.addParameter(QgsProcessingParameterString(self.TITLE, 'Title', 'Burn severity'))
        self.addParameter(QgsProcessingParameterString(self.NOTE, 'Extra note (optional)', optional=True,
                                                       multiLine=True))
        self.addParameter(QgsProcessingParameterFileDestination(self.PDF, 'Map (PDF)', 'PDF files (*.pdf)'))
        self.addParameter(QgsProcessingParameterFileDestination(
            self.PNG, 'Map (PNG)', 'PNG files (*.png)', optional=True, createByDefault=False))

    def processAlgorithm(self, parameters, context, feedback):
        from osgeo import gdal
        classes_layer = self.parameterAsRasterLayer(parameters, self.CLASSES, context)
        path = classes_layer.source()
        md = (gdal.Open(path).GetMetadata() if gdal.Open(path) else {}) or {}
        project = QgsProject()
        project.setCrs(classes_layer.crs())

        raster = QgsRasterLayer(path, 'Severity')
        _class_renderer(raster)
        layers = [raster]

        def vector(param, name, colour, width, dash=False):
            source = self.parameterAsSource(parameters, param, context)
            if source is None:
                return None
            layer = source.materialize(QgsFeatureRequest())  # a memory copy owned by this map's project
            layer.setName(name)
            symbol = QgsFillSymbol.createSimple({'color': '0,0,0,0', 'outline_color': colour,
                                                 'outline_width': str(width),
                                                 'outline_style': 'dash' if dash else 'solid'})
            layer.renderer().setSymbol(symbol)
            return layer

        perimeter = vector(self.PERIMETER, 'Burned area', '0,0,0,255', 0.35)
        if perimeter is None:
            raise QgsProcessingException('Could not load the burned-area polygons.')
        sub = vector(self.SUBAREAS, 'Sub-areas', '30,90,200,255', 0.5, dash=True) \
            if parameters.get(self.SUBAREAS) else None
        top = [l for l in (sub, perimeter) if l is not None]
        for layer in top + layers:
            project.addMapLayer(layer)

        layout = QgsPrintLayout(project)
        layout.initializeDefaults()
        page = layout.pageCollection().page(0)
        page.setPageSize(QgsLayoutSize(PAGE_W, PAGE_H, _mm()))

        # Map: left two thirds
        m = QgsLayoutItemMap(layout)
        layout.addLayoutItem(m)
        m.attemptMove(QgsLayoutPoint(8, 20, _mm()))
        m.attemptResize(QgsLayoutSize(185, 182, _mm()))
        m.setCrs(classes_layer.crs())
        m.setLayers(top + layers)
        extent = QgsRectangle(perimeter.extent())
        if perimeter.crs() != classes_layer.crs():
            from qgis.core import QgsCoordinateTransform
            extent = QgsCoordinateTransform(perimeter.crs(), classes_layer.crs(), project).transformBoundingBox(extent)
        extent.scale(1.08)
        m.zoomToExtent(extent)
        m.setFrameEnabled(True)

        _label(layout, parameters.get(self.TITLE) or 'Burn severity', 8, 6, 280, 10, 18, bold=True)

        # Legend
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle('Burn severity (MTBS classes)')
        legend.setLinkedMap(m)
        if hasattr(legend, 'setSyncMode'):  # QGIS 4
            legend.setSyncMode(Qgis.LegendSyncMode.Manual)
        else:
            legend.setAutoUpdateModel(False)
        root = legend.model().rootGroup()
        root.clear()
        for layer in [raster] + top:
            root.addLayer(layer)
        # The class colours need no "Severity" heading or "Band 1" line under the legend title
        from qgis.core import QgsLegendRenderer, QgsLegendStyle, QgsMapLayerLegendUtils
        node = root.findLayer(raster.id())
        QgsLegendRenderer.setNodeLegendStyle(node, QgsLegendStyle.Hidden)
        nodes = legend.model().layerLegendNodes(node)
        keep = [i for i, n in enumerate(nodes) if not str(n.data(0) or '').startswith('Band ')]
        QgsMapLayerLegendUtils.setLegendNodeOrder(node, keep)
        legend.model().refreshLayerLegend(node)
        layout.addLayoutItem(legend)
        legend.attemptMove(QgsLayoutPoint(200, 20, _mm()))

        # Scale bar and north arrow
        bar = QgsLayoutItemScaleBar(layout)
        bar.setStyle('Single Box')
        bar.setLinkedMap(m)
        bar.applyDefaultSize()
        bar.setUnits(_try(lambda: Qgis.DistanceUnit.Kilometers, lambda: QgsUnitTypes.DistanceKilometers))
        bar.setUnitLabel('km')
        bar.setNumberOfSegments(4)
        # Four segments of a round length that fit the 90 mm column (about 20 mm each)
        km_per_mm = m.extent().width() / 1000.0 / 185.0
        nice = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100]
        bar.setUnitsPerSegment(max([n for n in nice if n <= 20 * km_per_mm] or [nice[0]]))
        bar.setSegmentSizeMode(_try(lambda: Qgis.ScaleBarSegmentSizeMode.Fixed,
                                    lambda: QgsLayoutItemScaleBar.SegmentSizeFixed))
        layout.addLayoutItem(bar)
        bar.attemptMove(QgsLayoutPoint(200, 188, _mm()))  # right column, clear of the map
        arrow = QgsLayoutItemPicture(layout)
        arrow.setPicturePath(':/images/north_arrows/layout_default_north_arrow.svg')
        layout.addLayoutItem(arrow)
        arrow.attemptMove(QgsLayoutPoint(178, 24, _mm()))
        arrow.attemptResize(QgsLayoutSize(10, 14, _mm()))

        # Summary table (as text, so it needs no table renderer)
        y = 95
        summary = self.parameterAsVectorLayer(parameters, self.SUMMARY, context) if parameters.get(self.SUMMARY) else None
        if summary is not None and summary.isValid():
            fields = [n for n in summary.fields().names() if n == 'sev_class' or n.startswith(('pct_', 'ac_'))]
            head = ''.join(f'<th style="text-align:{"left" if n == "sev_class" else "right"};padding:0 4px">'
                           f'{"Class" if n == "sev_class" else n.replace("pct_", "% ").replace("ac_", "acres ")}</th>'
                           for n in fields)
            body = ''
            for f in summary.getFeatures():
                cells = ''
                for n in fields:
                    v = f[n]
                    if n.startswith('ac_'):
                        v = f'{float(v):,.0f}'
                    elif n.startswith('pct_'):
                        v = f'{float(v):.1f}'
                    cells += f'<td style="text-align:{"left" if n == "sev_class" else "right"};padding:0 4px">{v}</td>'
                body += f'<tr>{cells}</tr>'
            _label(layout, f'<table style="font-family:Arial;font-size:9pt;border-collapse:collapse">'
                           f'<tr>{head}</tr>{body}</table>', 200, y, 92, 40, 9, html=True)
            y += 42

        # Provenance note from the metadata written by the pipeline algorithms
        def thr(key):
            v = md.get(key)
            if v is None:
                return '?'
            v = float(v)
            return 'none' if v >= 9.999 else f'{v:.3f}'
        lines = []
        def when(prefix):
            date = md.get(f'{prefix}_HLS_DATE') or md.get(f'{prefix}_SOURCE_DATE')
            sensor = md.get(f'{prefix}_HLS_SENSOR') or md.get(f'{prefix}_SOURCE_SENSOR')
            return f'{date}{f" ({sensor})" if sensor else ""}' if date else None
        pre, post = when('PRE'), when('POST')
        if pre or post:
            lines.append(f'Images: pre-fire {pre or "?"}, post-fire {post or "?"}.')
        if 'LOW_T' in md:
            lines.append(f'dNBR thresholds: low {thr("LOW_T")}, moderate {thr("MODERATE_T")}, '
                         f'high {thr("HIGH_T")}; offset {float(md.get("DNBR_OFFSET", 0)):.3f}.')
            notes = [md.get(f'THRESHOLD_NOTE_{k}') for k in ('LOW', 'MODERATE', 'HIGH')]
            if any(n and n != 'given' for n in notes):
                lines.append('Thresholds estimated; check them against the imagery before use.')
        if parameters.get(self.NOTE):
            lines.append(self.parameterAsString(parameters, self.NOTE, context))
        lines.append(f'Made with Fire Analysis {VERSION} for QGIS, {datetime.date.today().isoformat()}. '
                     'Data: NASA HLS / USGS Landsat / ESA Sentinel-2.')
        _label(layout, '<br>'.join(lines), 200, y, 92, 60, 8, html=True)

        pdf = self.parameterAsFileOutput(parameters, self.PDF, context)
        exporter = QgsLayoutExporter(layout)
        settings = QgsLayoutExporter.PdfExportSettings()
        settings.dpi = 200
        result = exporter.exportToPdf(pdf, settings)
        if result != QgsLayoutExporter.Success:
            raise QgsProcessingException(f'PDF export failed ({result}).')
        results = {self.PDF: pdf}
        if parameters.get(self.PNG) not in (None, ''):
            png = self.parameterAsFileOutput(parameters, self.PNG, context)
            img = QgsLayoutExporter.ImageExportSettings()
            img.dpi = 150
            if exporter.exportToImage(png, img) == QgsLayoutExporter.Success:
                results[self.PNG] = png
        feedback.pushInfo(f'Map: {pdf}')
        return results
