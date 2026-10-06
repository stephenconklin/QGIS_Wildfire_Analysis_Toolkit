"""QGIS plugin entry point for the Wildfire Analysis Toolkit."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import BurnSeverityPlugin
    return BurnSeverityPlugin()
