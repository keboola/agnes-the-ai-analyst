"""BigQuery auth helper — fetch ephemeral access token from GCE metadata server.

Used by the BQ extractor and orchestrator when running on GCE with a service
account attached to the VM. No key file required.
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
_METADATA_TIMEOUT_S = 5


class BQMetadataAuthError(RuntimeError):
    """Raised when GCE metadata token cannot be obtained."""


def get_metadata_token() -> str:
    """Return a fresh access token from the GCE metadata server.

    Raises:
        BQMetadataAuthError: if the metadata server is unreachable or the
            response is malformed.
    """
    req = urllib.request.Request(
        _METADATA_TOKEN_URL,
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise BQMetadataAuthError(f"metadata server unreachable: {e}") from e
    except json.JSONDecodeError as e:
        raise BQMetadataAuthError(f"metadata response not JSON: {e}") from e

    token = payload.get("access_token")
    if not token:
        raise BQMetadataAuthError("no access_token in response")
    return token
