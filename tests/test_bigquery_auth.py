"""Tests for BQ metadata-token auth helper."""

from unittest.mock import patch, MagicMock
import json
import pytest

from connectors.bigquery.auth import (
    get_metadata_token,
    BQMetadataAuthError,
)
from connectors.bigquery.auth import _METADATA_TOKEN_URL as _METADATA_TOKEN_URL_FOR_TEST


def _mock_urlopen(payload: dict, status: int = 200):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda self: self
    resp.__exit__ = lambda self, *a: None
    return resp


class TestGetMetadataToken:
    def test_returns_token_string(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen({"access_token": "ya29.test", "expires_in": 3599})
            token = get_metadata_token()
        assert token == "ya29.test"

    def test_passes_metadata_flavor_header(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen({"access_token": "tok"})
            get_metadata_token()
            req = m.call_args[0][0]
            assert req.get_header("Metadata-flavor") == "Google"
            assert "metadata.google.internal" in req.full_url

    def test_raises_on_unreachable_metadata(self):
        from urllib.error import URLError
        with patch("connectors.bigquery.auth.urllib.request.urlopen", side_effect=URLError("no route")):
            with pytest.raises(BQMetadataAuthError, match="metadata server unreachable"):
                get_metadata_token()

    def test_raises_on_missing_access_token_field(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen({"error": "bad"})
            with pytest.raises(BQMetadataAuthError, match="no access_token in response"):
                get_metadata_token()

    def test_raises_on_http_error(self):
        """When metadata server returns 4xx/5xx (e.g. SA misconfiguration → 403),
        raise BQMetadataAuthError. urllib.error.HTTPError is a subclass of URLError."""
        from urllib.error import HTTPError
        err = HTTPError(
            url=_METADATA_TOKEN_URL_FOR_TEST,
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        with patch("connectors.bigquery.auth.urllib.request.urlopen", side_effect=err):
            with pytest.raises(BQMetadataAuthError):
                get_metadata_token()
