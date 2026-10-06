"""QGIS 3.x / 4.x compatibility. Prefer the newer enum names (QGIS >= 3.30/3.36/3.38)
and fall back to the old ones. burn_severity_summary.py keeps its own copy so
it can still be installed as a single script."""
from qgis.core import (Qgis, QgsField, QgsProcessing, QgsProcessingParameterDateTime,
                       QgsProcessingParameterDefinition, QgsProcessingParameterNumber,
                       QgsUnitTypes, QgsWkbTypes)


def _try(new, old):
    try:
        return new()
    except AttributeError:
        return old()


def field(name, kind, length=0):
    """kind: 'int', 'double' or 'string'. QGIS >= 3.38 wants QMetaType, older QVariant."""
    if kind == 'string' and not length:
        length = 50
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = {'int': QMetaType.Type.Int, 'double': QMetaType.Type.Double,
             'string': QMetaType.Type.QString}[kind]
        return QgsField(name, t, len=length)
    except (AttributeError, TypeError):
        from qgis.PyQt.QtCore import QVariant
        t = {'int': QVariant.Int, 'double': QVariant.Double, 'string': QVariant.String}[kind]
        return QgsField(name, t, len=length)


def polygon_source():
    return _try(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                lambda: QgsProcessing.TypeVectorPolygon)


def polygon_wkb():
    return _try(lambda: Qgis.WkbType.Polygon, lambda: QgsWkbTypes.Polygon)


def meters():
    return _try(lambda: Qgis.DistanceUnit.Meters, lambda: QgsUnitTypes.DistanceMeters)


def date_type():
    return _try(lambda: Qgis.ProcessingDateTimeParameterDataType.Date,
                lambda: QgsProcessingParameterDateTime.Date)


def integer_type():
    return _try(lambda: Qgis.ProcessingNumberParameterType.Integer,
                lambda: QgsProcessingParameterNumber.Integer)


def double_type():
    return _try(lambda: Qgis.ProcessingNumberParameterType.Double,
                lambda: QgsProcessingParameterNumber.Double)


def advanced(param):
    flag = _try(lambda: Qgis.ProcessingParameterFlag.Advanced,
                lambda: QgsProcessingParameterDefinition.FlagAdvanced)
    param.setFlags(param.flags() | flag)
    return param


def warn(feedback, text):
    """pushWarning (QGIS >= 3.16) shows in orange; fall back to plain info."""
    getattr(feedback, 'pushWarning', feedback.pushInfo)(text)
