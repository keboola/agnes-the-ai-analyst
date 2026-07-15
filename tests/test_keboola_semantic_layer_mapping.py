"""Pure-function mapping/validation logic for the Keboola semantic-layer
importer (connectors/keboola/semantic_layer.py). No live API calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    require_master_token,
)


class TestRequireMasterToken:
    def test_passes_silently_for_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": True}

        require_master_token(storage_client)  # must not raise

    def test_raises_for_non_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": False}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)

    def test_raises_for_missing_field(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)
