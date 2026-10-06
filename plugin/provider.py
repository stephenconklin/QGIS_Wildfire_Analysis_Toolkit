"""Fire Analysis Processing provider.

Burn severity summary is the same file that is installed as a standalone
script (burn_severity_summary.py, copied in by dev/build_plugin.py); here its id
is fireanalysis:burnseveritysummary. The imagery algorithms come from the
fireanalysis package."""
import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .burn_severity_summary import BurnSeveritySummary
from .fireanalysis import VERSION
from .fireanalysis.algorithms.fetch_hls_nbr import FetchHlsNbr
from .fireanalysis.algorithms.find_hls_scenes import FindHlsScenes


class _PluginBurnSeveritySummary(BurnSeveritySummary):
    # The script's group is "Fire Analysis", which is the provider's name here.
    def group(self):
        return 'Severity'

    def groupId(self):
        return 'severity'


class FireAnalysisProvider(QgsProcessingProvider):
    def id(self):
        return 'fireanalysis'

    def name(self):
        return 'Fire Analysis'

    def longName(self):
        return f'Fire Analysis {VERSION}'

    def versionInfo(self):
        return VERSION

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.svg'))

    def loadAlgorithms(self):  # noqa: N802 (QGIS API name)
        for alg in (FindHlsScenes(), FetchHlsNbr(), _PluginBurnSeveritySummary()):
            self.addAlgorithm(alg)
