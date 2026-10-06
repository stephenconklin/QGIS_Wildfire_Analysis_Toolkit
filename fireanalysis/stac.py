"""Minimal STAC API item search through QGIS's network stack.

QgsBlockingNetworkRequest follows the QGIS proxy and SSL settings, works from
a Processing worker thread, and stops when the feedback is cancelled.
"""
import json

from qgis.core import QgsBlockingNetworkRequest
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

MAX_PAGES = 100


class StacError(Exception):
    pass


def _request(url, body=None, feedback=None):
    req = QNetworkRequest(QUrl(url))
    req.setRawHeader(b'Accept', b'application/geo+json, application/json')
    blocking = QgsBlockingNetworkRequest()
    if body is None:
        err = blocking.get(req, True, feedback)
    else:
        req.setRawHeader(b'Content-Type', b'application/json')
        err = blocking.post(req, QByteArray(json.dumps(body).encode()), True, feedback)
    if feedback is not None and feedback.isCanceled():
        return None
    if err != QgsBlockingNetworkRequest.NoError:
        raise StacError(f'Request to {url.split("?")[0]} failed: {blocking.errorMessage()}')
    content = bytes(blocking.reply().content())
    try:
        return json.loads(content)
    except ValueError:
        raise StacError(f'Request to {url.split("?")[0]} did not return JSON: {content[:200]!r}') from None


def search(endpoint, collections, bbox, datetime, limit=100, feedback=None):
    """Yield every item (GeoJSON dict) matching the search, following `next` links.

    endpoint: the API's /search URL. bbox: [west, south, east, north] in
    EPSG:4326. datetime: 'start/end' in RFC 3339.
    """
    page = _request(endpoint, {'collections': list(collections), 'bbox': list(bbox),
                               'datetime': datetime, 'limit': limit}, feedback)
    for _ in range(MAX_PAGES):
        if page is None:  # cancelled
            return
        yield from page.get('features', [])
        nxt = next((link for link in page.get('links', []) if link.get('rel') == 'next'), None)
        if not nxt or not page.get('features'):
            return
        if nxt.get('method', 'GET').upper() == 'POST':
            body = nxt.get('body') or {}
            page = _request(nxt['href'], body, feedback)
        else:
            page = _request(nxt['href'], None, feedback)
    raise StacError(f'More than {MAX_PAGES} pages of results; narrow the dates or the area.')
