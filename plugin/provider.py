"""Fire Analysis Processing provider. The algorithm is the same file that is
installed as a standalone script (burn_severity_summary.py, copied in by
dev/build_plugin.py); here its id is fireanalysis:burnseveritysummary."""
import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .burn_severity_summary import VERSION, BurnSeveritySummary


class _PluginBurnSeveritySummary(BurnSeveritySummary):
    # The provider is already called "Fire Analysis"; no group inside it.
    def group(self):
        return ''

    def groupId(self):
        return ''


class FireAnalysisProvider(QgsProcessingProvider):
    def id(self):
        return 'fireanalysis'

    def name(self):
        return 'Fire Analysis'

    def longName(self):
        return f'Fire Analysis (Burn Severity Summary {VERSION})'

    def versionInfo(self):
        return VERSION

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.svg'))

    def loadAlgorithms(self):  # noqa: N802 (QGIS API name)
        self.addAlgorithm(_PluginBurnSeveritySummary())
