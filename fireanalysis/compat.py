"""QGIS 3.44+ / 4.x helpers. Enums are written only in their scoped form: QGIS 4
(Qt6) accepts nothing else, and the plugins.qgis.org Qt6 check flags every flat
name, even as a fallback. burn_severity_summary.py keeps its own copy so it can
still be installed as a single script."""
from qgis.core import Qgis, QgsField
from qgis.PyQt.QtCore import QMetaType

_FIELD_TYPES = {'int': QMetaType.Type.Int, 'double': QMetaType.Type.Double, 'string': QMetaType.Type.QString}


def field(name, kind, length=0):
    """kind: 'int', 'double' or 'string'."""
    if kind == 'string' and not length:
        length = 50
    return QgsField(name, _FIELD_TYPES[kind], len=length)


def polygon_source():
    return Qgis.ProcessingSourceType.VectorPolygon


def polygon_wkb():
    return Qgis.WkbType.Polygon


def polygon_geometry_type():
    return Qgis.GeometryType.Polygon


def meters():
    return Qgis.DistanceUnit.Meters


def date_type():
    return Qgis.ProcessingDateTimeParameterDataType.Date


def integer_type():
    return Qgis.ProcessingNumberParameterType.Integer


def double_type():
    return Qgis.ProcessingNumberParameterType.Double


def advanced(param):
    param.setFlags(param.flags() | Qgis.ProcessingParameterFlag.Advanced)
    return param


def warn(feedback, text):
    """pushWarning shows in orange."""
    feedback.pushWarning(text)
