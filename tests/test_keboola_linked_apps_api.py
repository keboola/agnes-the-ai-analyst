"""Keboola linked-app ingest over the Storage / Data-Science REST contracts.

The MCP lister path cannot serve this: verified against a live
keboola-mcp-server 1.74.6, `get_data_apps` answers with a compact
human-readable block (`data_apps[0]: links[1]{type,title,url}: …`) and ships
only `TextContent`, so the materialize path fails with "did not return
parseable JSON" and there is no `structuredContent` to fall back to.

The payload fixtures below are trimmed from real responses against a live
project (8 apps / 6 configs), including the two `keboola.sandboxes` rows that
share the endpoint.
"""

from __future__ import annotations

from src.data_apps.keboola_adapter import _data_science_base, records_from_apis

# Two sandboxes (Snowflake warehouse hosts) + four data apps, as returned.
APPS = [
    {"id": 31600263, "componentId": "keboola.sandboxes", "configId": "9001", "state": "running",
     "url": "https://rl74503-com_keboola_gcp_us_east4.snowflakecomputing.com"},
    {"id": 38430760, "componentId": "keboola.sandboxes", "configId": "9002", "state": "running",
     "url": "https://rl74503-com_keboola_gcp_us_east4.snowflakecomputing.com"},
    {"id": 41997754, "componentId": "keboola.data-apps", "configId": "01kn1zzecd34r2ga6ykywa8cka",
     "state": "stopped", "name": None,
     "url": "https://semantic-layer-ui-41997754.hub.us-east4.gcp.keboola.com"},
    {"id": 43667273, "componentId": "keboola.data-apps", "configId": "01ksn1c1sq9e7a2g0mscbhhq4j",
     "state": "stopped", "name": None,
     "url": "https://data-lineage-dev-43667273.hub.us-east4.gcp.keboola.com"},
    {"id": 43686441, "componentId": "keboola.data-apps", "configId": "01ky24bpvxg16y5p2sd899spyb",
     "state": "running", "name": None,
     "url": "https://e2e-sales-demo-43686441.hub.us-east4.gcp.keboola.com"},
    # Deployed, but its config was deleted upstream.
    {"id": 43999999, "componentId": "keboola.data-apps", "configId": "gone",
     "state": "stopped", "name": None,
     "url": "https://orphan-43999999.hub.us-east4.gcp.keboola.com"},
]

CONFIGS = [
    {"id": "01kn1zzecd34r2ga6ykywa8cka", "name": "Semantic Layer UI test", "description": "", "isDeleted": False},
    {"id": "01ksn1c1sq9e7a2g0mscbhhq4j", "name": "Data Lineage Dev", "description": "lineage", "isDeleted": False},
    {"id": "01ky24bpvxg16y5p2sd899spyb", "name": "E2E Sales Demo", "description": "", "isDeleted": False},
    {"id": "01kzdeleted00000000000000", "name": "Removed", "description": "", "isDeleted": True},
]


def _by_id(records):
    return {r.external_app_id: r for r in records}


def test_the_join_gives_each_app_its_name_and_its_real_url():
    """Neither endpoint is sufficient alone: `/apps` carries the URL and a
    null name, the configs carry the name and no URL."""
    records, keep = records_from_apis(APPS, CONFIGS)
    got = _by_id(records)
    assert got["41997754"].name == "Semantic Layer UI test"
    assert got["41997754"].external_url == "https://semantic-layer-ui-41997754.hub.us-east4.gcp.keboola.com"
    assert got["43667273"].description == "lineage"
    assert keep == []


def test_sandboxes_are_not_data_apps():
    """The same endpoint returns `keboola.sandboxes` rows whose `url` is a
    Snowflake warehouse host. Linking one would put a database endpoint
    behind an "Open" button."""
    records, _ = records_from_apis(APPS, CONFIGS)
    assert "31600263" not in _by_id(records)
    assert not any("snowflakecomputing" in r.external_url for r in records)


def test_an_app_whose_config_vanished_still_gets_a_usable_name():
    """It still has a live deployment; a blank row would be worse."""
    got = _by_id(records_from_apis(APPS, CONFIGS)[0])
    assert got["43999999"].name == "Keboola app 43999999"


def test_a_deleted_config_never_names_an_app():
    records, _ = records_from_apis(APPS, CONFIGS)
    assert not any(r.name == "Removed" for r in records)


def test_an_app_with_no_usable_url_is_kept_not_pruned():
    """Present upstream is present — it must not read as a deletion — but
    there is nothing to link to, so no row is written."""
    apps = APPS + [{"id": "555", "componentId": "keboola.data-apps", "configId": "x", "url": ""}]
    records, keep = records_from_apis(apps, CONFIGS)
    assert "555" not in _by_id(records)
    assert "555" in keep


def test_a_javascript_url_is_refused():
    """`external_url` is rendered as a raw href — this ingest is the only
    writer, so the scheme gate lives here."""
    apps = [{"id": "666", "componentId": "keboola.data-apps", "configId": "x",
             "url": "javascript:alert(document.domain)"}]
    records, keep = records_from_apis(apps, CONFIGS)
    assert records == []
    assert keep == ["666"]


def test_the_data_science_host_is_derived_from_the_stack():
    """The connection stores only the Storage endpoint; the Data Science
    service is its sibling host, derived the way Keboola's own MCP does."""
    assert _data_science_base("https://connection.us-east4.gcp.keboola.com") == (
        "https://data-science.us-east4.gcp.keboola.com"
    )
    assert _data_science_base("https://connection.north-europe.azure.keboola.com/") == (
        "https://data-science.north-europe.azure.keboola.com"
    )
    assert _data_science_base("") == ""


def test_an_empty_apps_payload_yields_no_records_rather_than_guesses():
    assert records_from_apis([], CONFIGS) == ([], [])
    assert records_from_apis(None, None) == ([], [])


# ── discovery vs adoption ───────────────────────────────────────────────────
#
# The admin surface must be able to SHOW what a project has without linking
# it. That split changes what pruning means, which is the subtle part: an app
# nobody adopted is not "missing", and an adopted app that vanished upstream
# still has to disappear.


class _FakeRepo:
    def __init__(self, linked=()):
        self.rows = {r: dict(source_ref=r) for r in linked}
        self.upserts = []
        self.pruned = []

    def list_linked(self, *, source_ref_prefix):
        return [v for k, v in self.rows.items() if k.startswith(source_ref_prefix)]

    def get_by_source_ref(self, ref):
        return self.rows.get(ref)

    def upsert_linked(self, *, slug, source_ref, name, description, external_url):
        self.upserts.append(source_ref)
        self.rows[source_ref] = dict(source_ref=source_ref)

    def soft_delete_missing_linked(self, *, source_ref_prefix, keep_source_refs):
        gone = [
            k for k in self.rows
            if k.startswith(source_ref_prefix) and k not in set(keep_source_refs)
        ]
        self.pruned.extend(gone)
        for k in gone:
            del self.rows[k]
        return len(gone)


CID = "conn-1"


def _patch_fetch(monkeypatch, records):
    from src.data_apps import linked_projection as lp

    monkeypatch.setattr(lp, "_keboola_connection", lambda cid: ({}, "tok", "https://connection.x.keboola.com"))
    monkeypatch.setattr(lp.adapter, "fetch_records", lambda *a, **k: (records, []))


def _recs(*ids):
    from src.data_apps.keboola_adapter import LinkedAppRecord

    return [LinkedAppRecord(external_app_id=i, name=f"App {i}", description="", external_url=f"https://{i}.example") for i in ids]


def test_discovery_writes_nothing(monkeypatch):
    from src.data_apps.linked_projection import discover_keboola_apps

    repo = _FakeRepo()
    _patch_fetch(monkeypatch, _recs("1", "2"))
    found = discover_keboola_apps(CID, repo=repo)
    assert [f.external_app_id for f in found] == ["1", "2"]
    assert all(not f.adopted for f in found)
    assert repo.upserts == [], "discovery must not link anything"


def test_discovery_marks_what_is_already_adopted(monkeypatch):
    from src.data_apps.keboola_adapter import source_ref
    from src.data_apps.linked_projection import discover_keboola_apps

    repo = _FakeRepo(linked=[source_ref(CID, "2")])
    _patch_fetch(monkeypatch, _recs("1", "2"))
    got = {f.external_app_id: f.adopted for f in discover_keboola_apps(CID, repo=repo)}
    assert got == {"1": False, "2": True}


def test_adoption_touches_only_the_ids_asked_for(monkeypatch):
    from src.data_apps.keboola_adapter import source_ref
    from src.data_apps.linked_projection import adopt_keboola_apps

    repo = _FakeRepo()
    _patch_fetch(monkeypatch, _recs("1", "2", "3"))
    assert adopt_keboola_apps(CID, ["2"], repo=repo) == 1
    assert repo.upserts == [source_ref(CID, "2")]
    assert repo.pruned == [], "adoption must never prune"


def test_refresh_never_adopts_something_new(monkeypatch):
    """The whole point of opt-in: a rescan must not drag in apps the admin
    deliberately left out."""
    from src.data_apps.keboola_adapter import source_ref
    from src.data_apps.linked_projection import sync_adopted_keboola_apps

    repo = _FakeRepo(linked=[source_ref(CID, "1")])
    _patch_fetch(monkeypatch, _recs("1", "2", "3"))
    sync_adopted_keboola_apps(CID, repo=repo)
    assert repo.upserts == [source_ref(CID, "1")]
    assert source_ref(CID, "2") not in repo.rows


def test_refresh_still_hides_an_adopted_app_that_vanished(monkeypatch):
    """The other half — otherwise a deleted app lingers forever."""
    from src.data_apps.keboola_adapter import source_ref
    from src.data_apps.linked_projection import sync_adopted_keboola_apps

    repo = _FakeRepo(linked=[source_ref(CID, "1"), source_ref(CID, "9")])
    _patch_fetch(monkeypatch, _recs("1"))
    res = sync_adopted_keboola_apps(CID, repo=repo)
    assert res.hidden == 1
    assert repo.pruned == [source_ref(CID, "9")]


def test_refresh_on_a_connection_with_nothing_adopted_is_a_no_op(monkeypatch):
    from src.data_apps.linked_projection import sync_adopted_keboola_apps

    repo = _FakeRepo()
    _patch_fetch(monkeypatch, _recs("1", "2"))
    res = sync_adopted_keboola_apps(CID, repo=repo)
    assert (res.created, res.updated, res.hidden) == (0, 0, 0)
    assert repo.upserts == []
