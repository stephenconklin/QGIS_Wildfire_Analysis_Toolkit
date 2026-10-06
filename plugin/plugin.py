"""Registers the Wildfire Analysis Toolkit Processing provider; the plugin adds no other GUI."""
from qgis.core import QgsApplication

from .provider import FireAnalysisProvider


class BurnSeverityPlugin:
    def __init__(self):
        self.provider = None

    def initProcessing(self):  # noqa: N802 (QGIS API name)
        self.provider = FireAnalysisProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):  # noqa: N802 (QGIS API name)
        self.initProcessing()

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
