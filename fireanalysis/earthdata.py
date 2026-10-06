"""NASA Earthdata login for GDAL reads of protected cloud files.

Credentials come from a netrc file (default ~/.netrc, or the path in the
FIREANALYSIS_NETRC environment variable) with a line such as

    machine urs.earthdata.nasa.gov login <user> password <password>

GDAL/curl read that file themselves: on the first request the data host
redirects to urs.earthdata.nasa.gov, curl sends the netrc login there only, and
the session cookie it gets back goes into a private cookie jar that later reads
reuse. Bearer tokens don't work with /vsicurl/ on LP DAAC (see docs/DESIGN.md).

All options are path-specific, so they apply only to the Earthdata data hosts,
never to other GDAL traffic in the QGIS session. Never set CPL_CURL_VERBOSE with
live credentials: it prints the Basic authorization header sent to URS.

This module reads the netrc only to check it. A future plugin setting can hand
configure_gdal() a different netrc path (for example one written from the QGIS
authentication manager) without changing anything else.
"""
import atexit
import netrc
import os
import shutil
import stat
import sys
import tempfile

URS_HOST = 'urs.earthdata.nasa.gov'

# /vsicurl/ prefixes of the Earthdata hosts that need the login
DATA_PREFIXES = (
    '/vsicurl/https://data.lpdaac.earthdatacloud.nasa.gov/',
)

_cookie_dir = None
_configured = None  # netrc path the GDAL options were last set for


class EarthdataError(Exception):
    pass


def default_netrc_path():
    env = os.environ.get('FIREANALYSIS_NETRC')
    if env:
        return os.path.expanduser(env)
    home = os.path.expanduser('~')
    if sys.platform == 'win32' and not os.path.exists(os.path.join(home, '.netrc')):
        return os.path.join(home, '_netrc')
    return os.path.join(home, '.netrc')


def check_netrc(path=None):
    """Return (netrc path, login, warnings). Raise EarthdataError with a fix if
    the file or the Earthdata entry is missing. Never returns the password."""
    path = path or default_netrc_path()
    how = (f'Add this line to {path} (and make the file readable only by you):\n'
           f'    machine {URS_HOST} login <your Earthdata user> password <your password>\n'
           'Create a free account at https://urs.earthdata.nasa.gov.')
    if not os.path.isfile(path):
        raise EarthdataError(f'No netrc file at {path}. {how}')
    try:
        entry = netrc.netrc(path).authenticators(URS_HOST)
    except (netrc.NetrcParseError, OSError) as e:
        raise EarthdataError(f'Could not read {path}: {e}') from None
    if not entry or not entry[0] or not entry[2]:
        raise EarthdataError(f'{path} has no login and password for {URS_HOST}. {how}')
    warnings = []
    if sys.platform != 'win32' and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(f'{path} can be read by other users; run: chmod 600 {path}')
    return path, entry[0], warnings


def _cookie_jar():
    global _cookie_dir
    if _cookie_dir is None:
        _cookie_dir = tempfile.mkdtemp(prefix='fireanalysis_earthdata_')  # mode 0700
        atexit.register(shutil.rmtree, _cookie_dir, True)
    return os.path.join(_cookie_dir, 'cookies.txt')


def configure_gdal(netrc_path=None):
    """Check the netrc and set GDAL's options for the Earthdata data hosts.
    Returns the warnings from check_netrc(). Safe to call more than once."""
    global _configured
    from osgeo import gdal
    path, _login, warnings = check_netrc(netrc_path)
    if _configured == path:
        return warnings
    jar = _cookie_jar()
    options = {
        'GDAL_HTTP_NETRC': 'YES',
        'GDAL_HTTP_NETRC_FILE': path,
        'GDAL_HTTP_COOKIEFILE': jar,
        'GDAL_HTTP_COOKIEJAR': jar,
        'GDAL_DISABLE_READDIR_ON_OPEN': 'EMPTY_DIR',
        'CPL_VSIL_CURL_ALLOWED_EXTENSIONS': '.tif',
        'GDAL_HTTP_MAX_RETRY': '4',
        'GDAL_HTTP_RETRY_DELAY': '2',
    }
    for prefix in DATA_PREFIXES:
        for key, value in options.items():
            gdal.SetPathSpecificOption(prefix, key, value)
    _configured = path
    return warnings


def open_error_hint(url):
    """Explain a failed /vsicurl/ open of a protected Earthdata file."""
    return (f'Could not read {url}. If the file exists, the Earthdata login was '
            f'probably refused: check the {URS_HOST} line in {_configured or default_netrc_path()} '
            'and that you can sign in at https://urs.earthdata.nasa.gov.')
