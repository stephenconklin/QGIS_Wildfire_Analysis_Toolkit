"""QGIS plugin entry point for Burn Severity Summary."""


def classFactory(iface):  # noqa: N802 (QGIS API name)
    from .plugin import BurnSeverityPlugin
    return BurnSeverityPlugin()
