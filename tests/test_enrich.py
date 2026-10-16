import io
import json
from unittest import mock

import pytest

from discolab.enrich import DeezerEnricher, DeezerError

QUOTA = {
    "error": {
        "type": "Exception",
        "message": "Quota limit exceeded",
        "code": 4,
    }
}


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _enricher() -> DeezerEnricher:
    # A rate no test will wait on.
    return DeezerEnricher(rate=1_000_000)


def test_quota_body_raises_rather_than_reading_as_no_genres():
    """Deezer refuses with HTTP 200, so the refusal is in the body.

    Read as an ordinary reply that body has no "data" key, which is the same
    value an artist with no match returns. The caller then records the artist as
    asked and empty, permanently, because of a rate limit.
    """
    body = json.dumps(QUOTA).encode()
    with mock.patch("urllib.request.urlopen", return_value=_Response(body)):
        with mock.patch("discolab.enrich.time.sleep"):
            with pytest.raises(DeezerError):
                _enricher()._get("/search/artist", {"q": "Aphex Twin"})


def test_a_normal_reply_is_returned():
    body = json.dumps({"data": [{"id": 7, "name": "Aphex Twin"}]}).encode()
    with mock.patch("urllib.request.urlopen", return_value=_Response(body)):
        payload = _enricher()._get("/search/artist", {"q": "Aphex Twin"})
    assert payload["data"][0]["name"] == "Aphex Twin"


def test_a_near_miss_is_not_the_artist():
    """Rome resolves to Romé, which is a different artist."""
    body = json.dumps({"data": [{"id": 9, "name": "Rom\u00e9"}]}).encode()
    with mock.patch("urllib.request.urlopen", return_value=_Response(body)):
        assert _enricher().facts("Rome") is None
