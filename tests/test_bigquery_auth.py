"""Tests for BQ metadata-token auth helper."""

from unittest.mock import patch, MagicMock
import json
import pytest

from connectors.bigquery.auth import (
    get_metadata_token,
    clear_token_cache,
    BQMetadataAuthError,
)
from connectors.bigquery.auth import _METADATA_TOKEN_URL as _METADATA_TOKEN_URL_FOR_TEST


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Reset the module-level token cache between tests so cache state from one
    test does not leak into another."""
    clear_token_cache()
    yield
    clear_token_cache()


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
        """Metadata unreachable + ADC also unavailable → raise with both reasons.
        Issue #112 added an ADC fallback; the test mocks both paths to fail
        so the original 'metadata-only failure' contract is exercised."""
        from urllib.error import URLError
        with patch("connectors.bigquery.auth.urllib.request.urlopen", side_effect=URLError("no route")), \
             patch("connectors.bigquery.auth._fetch_adc_token",
                   side_effect=BQMetadataAuthError("no ADC creds")):
            with pytest.raises(BQMetadataAuthError, match="metadata server unreachable"):
                get_metadata_token()

    def test_raises_on_missing_access_token_field(self):
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m, \
             patch("connectors.bigquery.auth._fetch_adc_token",
                   side_effect=BQMetadataAuthError("no ADC creds")):
            m.return_value = _mock_urlopen({"error": "bad"})
            with pytest.raises(BQMetadataAuthError, match="no access_token in response"):
                get_metadata_token()

    def test_raises_on_http_error(self):
        """When metadata server returns 4xx/5xx (e.g. SA misconfiguration → 403)
        AND ADC is also unavailable, raise BQMetadataAuthError. ADC fallback
        is mocked to fail so this asserts the metadata-error branch."""
        from urllib.error import HTTPError
        err = HTTPError(
            url=_METADATA_TOKEN_URL_FOR_TEST,
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=None,
        )
        with patch("connectors.bigquery.auth.urllib.request.urlopen", side_effect=err), \
             patch("connectors.bigquery.auth._fetch_adc_token",
                   side_effect=BQMetadataAuthError("no ADC creds")):
            with pytest.raises(BQMetadataAuthError):
                get_metadata_token()

    def test_falls_back_to_adc_when_metadata_unreachable(self):
        """Issue #112 — when GCE metadata is unreachable (laptop dev), fall
        through to ADC. The combined wrapper must return a token from ADC."""
        from urllib.error import URLError
        with patch("connectors.bigquery.auth.urllib.request.urlopen",
                   side_effect=URLError("no route")), \
             patch("connectors.bigquery.auth._fetch_adc_token",
                   return_value=("adc-token", 3600)):
            assert get_metadata_token() == "adc-token"


class TestTokenCache:
    """get_metadata_token() must cache valid tokens until shortly before expiry."""

    def test_second_call_returns_cached_value_without_new_urlopen(self):
        """Within the cache TTL, only one urlopen happens."""
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen(
                {"access_token": "ya29.fresh", "expires_in": 3599}
            )
            t1 = get_metadata_token()
            t2 = get_metadata_token()
        assert t1 == "ya29.fresh"
        assert t2 == "ya29.fresh"
        assert m.call_count == 1, \
            f"second call should hit cache, not metadata server; got {m.call_count} urlopen calls"

    def test_cache_refetches_after_expiry(self):
        """Once the safety-buffered expiry passes, the next call re-fetches."""
        # Populate cache with a token that expires almost immediately (small expires_in
        # is below the safety buffer, so the populator code path SKIPS caching).
        # Use a different mechanism: prime cache with a normal token, then advance
        # monotonic time past expiry and verify a second urlopen happens.
        import connectors.bigquery.auth as auth_mod

        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen(
                {"access_token": "ya29.first", "expires_in": 3600}
            )
            t1 = get_metadata_token()
            assert t1 == "ya29.first"
            assert m.call_count == 1

            # Force the cache expiry into the past (jump monotonic forward by
            # editing the cached tuple directly — public API doesn't expose this).
            with auth_mod._cache_lock:
                cached_token, _ = auth_mod._token_cache
                auth_mod._token_cache = (cached_token, 0.0)  # already expired

            # Next call should re-fetch
            m.return_value = _mock_urlopen(
                {"access_token": "ya29.refreshed", "expires_in": 3600}
            )
            t2 = get_metadata_token()
        assert t2 == "ya29.refreshed"
        assert m.call_count == 2

    def test_no_caching_when_expires_in_missing_or_too_small(self):
        """If the response lacks expires_in (or it's smaller than the safety buffer),
        skip caching so the next call retries — protects against poison-cached
        zero-TTL responses."""
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            # First response: no expires_in field at all
            m.return_value = _mock_urlopen({"access_token": "ya29.no_ttl"})
            t1 = get_metadata_token()
            t2 = get_metadata_token()
        assert t1 == "ya29.no_ttl"
        assert t2 == "ya29.no_ttl"
        # Both calls hit the network because the first response wasn't cached
        assert m.call_count == 2

    def test_clear_token_cache_forces_refetch(self):
        """Public clear_token_cache() invalidates so the next call re-fetches."""
        with patch("connectors.bigquery.auth.urllib.request.urlopen") as m:
            m.return_value = _mock_urlopen(
                {"access_token": "ya29.cached", "expires_in": 3600}
            )
            get_metadata_token()
            assert m.call_count == 1
            clear_token_cache()
            m.return_value = _mock_urlopen(
                {"access_token": "ya29.after_clear", "expires_in": 3600}
            )
            t = get_metadata_token()
        assert t == "ya29.after_clear"
        assert m.call_count == 2
