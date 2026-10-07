"""Styles for result layers, applied when Processing loads them into the project.

Processing loads outputs without their .qml default style, so the styles are
set by a layer post-processor (QgsProcessingLayerPostProcessorInterface), which
QGIS calls on the main thread once every result layer is in the layer tree.
"""
from qgis.core import (Qgis, QgsColorRampShader, QgsContrastEnhancement, QgsExpression, QgsFillSymbol,
                       QgsMultiBandColorRenderer, QgsPalettedRasterRenderer, QgsProcessingContext,
                       QgsProcessingLayerPostProcessorInterface, QgsProcessingUtils, QgsProject,
                       QgsRasterMinMaxOrigin, QgsRasterShader,
                       QgsRuleBasedRenderer, QgsSingleBandGrayRenderer, QgsSingleBandPseudoColorRenderer)
from qgis.PyQt.QtGui import QColor

from .compat import _try

# Severity ramp for continuous indices, unburned green to high red, at fractions of the range
RAMP = ((0.0, '#1a9641'), (0.27, '#a6d96a'), (0.4, '#ffffbf'), (0.63, '#fdae61'), (1.0, '#d7191c'))


def _stretch():
    return _try(lambda: QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum,
                lambda: QgsContrastEnhancement.StretchToMinimumMaximum)


def mtbs_classes(layer):
    """The MTBS dnbr6 palette."""
    from .algorithms.classify_severity import CLASS_COLORS, CLASS_NAMES
    classes = [QgsPalettedRasterRenderer.Class(v, QColor(CLASS_COLORS[v]), CLASS_NAMES[v]) for v in sorted(CLASS_NAMES)]
    layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 1, classes))


def index_ramp(lo, hi):
    """A style function: band 1 on the severity ramp from lo to hi."""
    def style(layer):
        ramp = QgsColorRampShader(lo, hi)
        ramp.setColorRampType(_try(lambda: Qgis.ShaderInterpolationMethod.Linear,
                                   lambda: QgsColorRampShader.Interpolated))
        ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(lo + f * (hi - lo), QColor(c),
                                                                    f'{lo + f * (hi - lo):.2f}') for f, c in RAMP])
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        renderer.setClassificationMin(lo)
        renderer.setClassificationMax(hi)
        layer.setRenderer(renderer)
    return style


def gray(lo, hi):
    """A style function: band 1 in grey from lo to hi."""
    def style(layer):
        renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), 1)
        ce = QgsContrastEnhancement(layer.dataProvider().dataType(1))
        ce.setContrastEnhancementAlgorithm(_stretch())
        ce.setMinimumValue(lo)
        ce.setMaximumValue(hi)
        renderer.setContrastEnhancement(ce)
        layer.setRenderer(renderer)
    return style


def false_color(layer):
    """Bands 1-3 (SWIR2, NIR, red) as RGB, cumulative cut stretch (2-98 % by default)."""
    layer.setRenderer(QgsMultiBandColorRenderer(layer.dataProvider(), 1, 2, 3))
    layer.setContrastEnhancement(_stretch(), _try(lambda: Qgis.RasterRangeLimit.CumulativeCut,
                                                  lambda: QgsRasterMinMaxOrigin.CumulativeCut))


def _outline(color, width, dashed=False):
    props = {'style': 'no', 'outline_color': color, 'outline_width': str(width), 'outline_width_unit': 'MM'}
    if dashed:
        props['outline_style'] = 'dash'
    return QgsFillSymbol.createSimple(props)


def zones(fire):
    """A style function for zones.gpkg: the burned area (zone named fire) as a black
    outline, sub-areas as dashed blue outlines, as on the severity map."""
    def style(layer):
        root = QgsRuleBasedRenderer.Rule(None)
        root.appendChild(QgsRuleBasedRenderer.Rule(_outline('0,0,0,255', 0.6), 0, 0,
                                                   f'"zone" = {QgsExpression.quotedValue(fire)}', 'Burned area'))
        root.appendChild(QgsRuleBasedRenderer.Rule(_outline('30,90,200,255', 0.5, True), 0, 0, 'ELSE', 'Sub-areas'))
        layer.setRenderer(QgsRuleBasedRenderer(root))
    return style


def outline(color, width=0.5, dashed=False):
    """A style function: polygons as an outline only."""
    def style(layer):
        from qgis.core import QgsSingleSymbolRenderer
        layer.setRenderer(QgsSingleSymbolRenderer(_outline(color, width, dashed)))
    return style


class StyleOnLoad(QgsProcessingLayerPostProcessorInterface):
    """Applies a style function, sets the layer name (Processing otherwise prefers the
    file name), and optionally switches the layer off in the layer tree."""
    keep = []  # QGIS holds only a pointer: keep the Python objects alive

    def __init__(self, style, visible=True, name=None):
        super().__init__()
        self.style, self.visible, self.name = style, visible, name

    def postProcessLayer(self, layer, context, feedback):
        if layer is None or not layer.isValid():
            return
        if self.name:
            layer.setName(self.name)
        layer.reload()  # a rerun into the same folder reuses the loaded layer: show the new data
        try:
            self.style(layer)
            layer.triggerRepaint()
        except Exception as e:  # a style must never fail the run
            feedback.pushInfo(f'Could not style {layer.name()}: {e}')
        if not self.visible:
            project = context.project() or QgsProject.instance()
            node = project.layerTreeRoot().findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(False)


def load_on_completion(context, path, name, style=None, visible=True, group='', sort_key=0, vector=False):
    """Load path into the project when the algorithm finishes, named name, in group
    (created if needed; '' = the usual place), styled and shown or hidden. Layers with
    a higher sort_key are placed above."""
    details = QgsProcessingContext.LayerDetails(name, context.project() or QgsProject.instance(), name,
                                                QgsProcessingUtils.LayerHint.Vector if vector
                                                else QgsProcessingUtils.LayerHint.Raster)
    details.groupName = group
    details.layerSortKey = sort_key
    context.addLayerToLoadOnCompletion(path, details)
    post = StyleOnLoad(style or (lambda layer: None), visible, name)
    StyleOnLoad.keep = StyleOnLoad.keep[-50:] + [post]
    context.layerToLoadOnCompletionDetails(path).setPostProcessor(post)


def unique_group(context, base, path=None):
    """The group for a run's layers: the group already holding path, when a rerun
    writes to the same files (QGIS then reuses the loaded layers); else base, or
    base (2), (3)... when the project already has a group of that name."""
    import os
    project = context.project() or QgsProject.instance()
    root = project.layerTreeRoot() if project else None
    if root is not None and path:
        want = os.path.normcase(os.path.abspath(path))
        for node in root.findLayers():
            layer = node.layer()
            src = layer.source().split('|')[0] if layer is not None else ''
            if src and os.path.normcase(os.path.abspath(src)) == want:
                parent = node.parent()
                return parent.name() if parent is not None and parent is not root else ''
    name, n = base, 2
    while root is not None and root.findGroup(name) is not None:
        name, n = f'{base} ({n})', n + 1
    return name
