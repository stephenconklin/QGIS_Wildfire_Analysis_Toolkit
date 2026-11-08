"""Wildfire Analysis Toolkit Processing provider (id fireanalysis, kept from 2.0 development so algorithm ids stay fireanalysis:...).

Burn severity summary is the same file that is installed as a standalone
script (burn_severity_summary.py, copied in by dev/build_plugin.py); here its id
is fireanalysis:burnseveritysummary. The imagery algorithms come from the
fireanalysis package."""
import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .burn_severity_summary import BurnSeveritySummary
from .fireanalysis import VERSION
from .fireanalysis.algorithms.build_dnbr import BuildDnbr
from .fireanalysis.algorithms.burn_severity_pipeline import BurnSeverityPipeline
from .fireanalysis.algorithms.classify_severity import ClassifySeverity
from .fireanalysis.algorithms.draft_perimeter import DraftPerimeter
from .fireanalysis.algorithms.fetch_hls_nbr import FetchHlsNbr
from .fireanalysis.algorithms.find_hls_scenes import FindHlsScenes
from .fireanalysis.algorithms.find_mtbs_fires import FindMtbsFires
from .fireanalysis.algorithms.planetary_algorithms import FetchPlanetaryNbr, FindPlanetaryScenes
from .fireanalysis.algorithms.prepare_local_nbr import PrepareLocalNbr
from .fireanalysis.algorithms.severity_map import SeverityMap
from .fireanalysis.algorithms.suggest_image_pairs import SuggestImagePairs


class _PluginBurnSeveritySummary(BurnSeveritySummary):
    # The standalone script uses group "Fire Analysis"; inside the provider it goes in Severity.
    def group(self):
        return 'Severity'

    def groupId(self):
        return 'severity'


class FireAnalysisProvider(QgsProcessingProvider):
    def id(self):
        return 'fireanalysis'

    def name(self):
        return 'Wildfire Analysis Toolkit'

    def longName(self):
        return f'Wildfire Analysis Toolkit {VERSION}'

    def versionInfo(self):
        return VERSION

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), 'icon.svg'))

    def loadAlgorithms(self):  # noqa: N802 (QGIS API name)
        for alg in (BurnSeverityPipeline(), SeverityMap(), FindMtbsFires(), FindHlsScenes(), SuggestImagePairs(), FetchHlsNbr(), PrepareLocalNbr(),
                    FindPlanetaryScenes(), FetchPlanetaryNbr(),
                    BuildDnbr(), DraftPerimeter(), ClassifySeverity(), _PluginBurnSeveritySummary()):
            self.addAlgorithm(alg)
