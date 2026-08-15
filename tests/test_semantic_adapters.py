import pytest

from src.semantic.adapters import UnknownAdapter, get_adapter


def test_native_adapter_returns_documents_untouched():
    text = "version: '0.2.0.dev0'\nsemantic_model:\n  - name: retail\n"
    out = get_adapter("native").extract({"documents": [text]})
    assert out == [text], "the adapter must not re-serialize; byte-identical or bust"


def test_unknown_adapter_names_the_available_ones():
    with pytest.raises(UnknownAdapter) as exc:
        get_adapter("nope")
    assert "native" in str(exc.value)
