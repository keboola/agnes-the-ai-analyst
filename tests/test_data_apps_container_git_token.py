"""The credential a hosted container clones with must be git-scoped.

The deploy path handed the container its `data-app:<slug>` *service* token as
the git password. The git surface (`app/api/data_apps_git.py`) is the only
caller of `resolve_token_to_user` that passes `allow_data_app_git_scope=True`,
and it admits exactly `data-app-git:<slug>` — a service token is rejected there
like anywhere else. So every hosted app's entrypoint failed its first clone
with ``remote: authentication required`` and crash-looped forever: data apps
could not deploy at all.

Reproduced end to end on a live instance before the fix — container
`Restarting (128)`, `fatal: Authentication failed for
'http://app:8000/data-apps.git/<slug>/'` on repeat, the row stuck in `error` —
and confirmed by swapping only the token in the generated `config.json` for a
git-scoped one: the clone succeeded, `npm install` and the vite build ran, and
the app served HTTP 200. Nothing else about the pipeline was wrong.

Nothing in the Agnes log said so, which is why this needed a container-log
read to find: the rejection happens inside the git surface, which does not log
a denial.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

SOURCE = Path("app/api/data_apps.py")


def _claims(tok: str) -> dict:
    payload = tok.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def test_the_deploy_path_does_not_hand_the_service_token_to_git():
    """The one-line regression. `clone_token=jwt_token` is the bug verbatim."""
    src = SOURCE.read_text(encoding="utf-8")
    assert "clone_token=git_token" in src
    assert "clone_token=jwt_token" not in src, (
        "the service token is `data-app:<slug>`-scoped and the git surface refuses it — "
        "every container would crash-loop on its first clone"
    )


def test_the_container_token_is_git_scoped():
    src = SOURCE.read_text(encoding="utf-8")
    body = src[src.index("def _mint_container_git_token") : src.index("_GIT_CREDENTIAL_TTL")]
    assert 'f"data-app-git:{repo_slug}"' in body, "must carry the scope the git surface admits"


def test_the_container_token_is_scoped_to_the_repo_not_the_app():
    """A draft clones its PARENT's repo, and the git surface pins the scope's
    slug to the repo being requested. Minting against the draft's own slug
    would be refused — the failure would look identical to the original bug
    and only for drafts."""
    src = SOURCE.read_text(encoding="utf-8")
    # `slug` (the app's own) rides along for the token NAME — the sweep that
    # revokes superseded credentials keys on it, so a draft's deploy cannot
    # revoke its parent's. The scope half is still `repo_slug`.
    assert "_mint_container_git_token(repo_slug, slug, owner)" in src
    mint_at = src.index("_mint_container_git_token(repo_slug, slug, owner)")
    resolve_at = src.index('repo_slug = parent["slug"]')
    assert resolve_at < mint_at, "repo_slug must be resolved to the parent before the mint"


def test_the_container_token_does_not_expire():
    """The container re-clones whenever it is recreated, including waking from
    `sleep_mode: recreate`. A 24h token would leave an app that deployed on
    Monday unable to wake on Wednesday — indistinguishable from a hosting bug."""
    src = SOURCE.read_text(encoding="utf-8")
    body = src[src.index("def _mint_container_git_token") : src.index("_GIT_CREDENTIAL_TTL")]
    assert "expires_at=None" in body
    assert "_GIT_CREDENTIAL_TTL" not in body, "that TTL belongs to the analyst's authoring credential"


def test_a_failed_deploy_revokes_the_unused_container_token():
    """Symmetry with `_rollback_new_service_token`: a credential no container
    ever received is dead weight if left live."""
    src = SOURCE.read_text(encoding="utf-8")
    up_block = src[src.index("        _runner().up(slug, spec, config_json)") :][:600]
    assert "_revoke_quietly(git_token_id)" in up_block
    spec_block = src[src.index("        config_json = build_config_json(") :][:1400]
    assert "_revoke_quietly(git_token_id)" in spec_block


def test_the_two_minters_stay_distinct():
    """They differ in scope AND lifetime; collapsing them re-creates the bug in
    one direction or breaks `AGNES_TOKEN` in the other — the service token is
    what the app calls the rest of the REST API with, and `data-app-git:` is
    refused everywhere except the git surface."""
    src = SOURCE.read_text(encoding="utf-8")
    service = src[src.index("def _mint_service_token") : src.index("def _revoke_quietly")]
    assert 'f"data-app:{slug}"' in service, "the service token must NOT become git-scoped"


def test_scope_prefixes_match_what_the_git_surface_admits():
    """Pin the two strings together across the module boundary — a rename on
    one side would silently restore the crash loop."""
    resolver = Path("app/auth/pat_resolver.py").read_text(encoding="utf-8")
    m = re.search(r'DATA_APP_GIT_SCOPE_PREFIX = "([^"]+)"', resolver)
    assert m, "DATA_APP_GIT_SCOPE_PREFIX moved — re-point this guard"
    prefix = m.group(1)
    src = SOURCE.read_text(encoding="utf-8")
    assert f'f"{prefix}{{repo_slug}}"' in src, (
        f"the container token's scope must start with {prefix!r}, the prefix the git surface checks"
    )


def test_the_clone_url_prefers_a_reachable_base_over_the_compose_hostname():
    """`_mint_git_credential`'s URL is handed to a REMOTE sandbox, an analyst
    laptop and the MCP tool — none of which can resolve `http://app:8000`.

    `get_public_url()` reads `PUBLIC_URL` / `server.public_url` only, and a
    compose deployment sets `SERVER_URL` instead, so the chain fell straight
    through to the compose-internal name. Watched live on a box with
    `SERVER_URL=https://…` and no `server.public_url`: the agent fetched its
    credential, ran `git clone`, and the egress hook reported the target host
    as `app`.
    """
    src = SOURCE.read_text(encoding="utf-8")
    m = re.search(r"    base = get_public_url\(\)(.*?)\n", src)
    assert m, "the credential base chain moved — re-point this guard"
    chain = m.group(1)
    assert "SERVER_URL" in chain, "SERVER_URL must sit between the public URL and the internal fallback"
    assert chain.index("SERVER_URL") < chain.index("AGNES_INTERNAL_URL"), (
        "the internal compose hostname must stay the LAST resort"
    )


def test_superseded_container_tokens_are_revoked_after_a_successful_deploy():
    """Devin Review on this PR: nothing ever cancelled these.

    They are deliberately expiry-less (a container re-clones whenever it is
    recreated, including waking from sleep), and the id was not recorded
    anywhere — so every deploy left another permanent read/write credential
    on the app's repo, and deleting the app left them all valid.

    Order matters as much as the call: revoking before the runner accepts the
    deploy would strand a previously-deployed container that is still asleep
    and will re-clone with the old credential when it wakes — the same
    reasoning the service token's own revoke is placed after `_runner().up`.
    """
    src = SOURCE.read_text(encoding="utf-8")
    call = "_revoke_container_git_tokens(owner[\"id\"], repo_slug, slug, keep=git_token_id)"
    assert call in src, "superseded container git tokens are never revoked"
    assert src.index("_runner().up(slug, spec, config_json)") < src.index(call), (
        "revoking before the runner accepts the deploy strands a sleeping container"
    )


def test_the_token_name_is_per_app_so_a_draft_cannot_revoke_its_parent():
    """A draft shares its parent's REPO, so a name keyed only on `repo_slug`
    would make the two indistinguishable — and the draft's deploy would
    revoke the credential the parent's container wakes with."""
    src = SOURCE.read_text(encoding="utf-8")
    body = src[src.index("def _container_git_token_name") : src.index("def _revoke_container_git_tokens(")]
    assert "{app_slug}" in body, "the name must distinguish a draft from its parent"
    assert "{repo_slug}" in body, "the scope half must still follow the repo"


def test_teardown_revokes_the_container_token_too():
    """A deleted app must not leave a live credential on its repository."""
    src = SOURCE.read_text(encoding="utf-8")
    body = src[src.index("def _revoke_service_token(") : src.index("def _revoke_container_git_tokens_for_row(")]
    assert "_revoke_container_git_tokens_for_row(row)" in body


def test_a_schemeless_base_still_yields_a_credentialed_clone_url():
    """Devin Review on #1239: `.replace("://", …)` is a no-op without a scheme.

    Compose files do carry `SERVER_URL` as a bare `host:port`. The fallback
    then produced `host:port/data-apps.git/<slug>` with no `agnes:<jwt>@` at
    all — a clone URL that authenticates as nobody, which is the failure the
    fallback was added to prevent, one step further along.
    """
    from app.api.data_apps import _clone_url_with_credential

    url = _clone_url_with_credential("agnes.example.com:8000", "JWT", "sales")
    assert url == "https://agnes:JWT@agnes.example.com:8000/data-apps.git/sales", url


def test_a_schemed_base_is_left_alone():
    from app.api.data_apps import _clone_url_with_credential

    assert (
        _clone_url_with_credential("http://agnes.example.com:8000", "JWT", "sales")
        == "http://agnes:JWT@agnes.example.com:8000/data-apps.git/sales"
    )
    assert _clone_url_with_credential("https://a.example.com", "JWT", "s").startswith("https://agnes:JWT@")


def test_the_mint_path_goes_through_the_helper():
    """Otherwise the guard sits in a function nothing calls."""
    src = SOURCE.read_text(encoding="utf-8")
    assert "return _clone_url_with_credential(base, jwt_token, slug)" in src


# ---------------------------------------------------------------------------
# The container's credential must be clone-ONLY (agnes-reviewer-rbac on #1239)
# ---------------------------------------------------------------------------


def test_the_container_token_is_marked_clone_only():
    """The scope says WHICH repo, never what may be done to it.

    `data-app-git:<slug>` is the same scope the analyst's 24-hour authoring
    PAT carries, and the container's credential is minted for the app's
    OWNER — so the git surface saw an owner and allowed pushes. "The clone
    token" was in fact a non-expiring read/write credential, sitting in every
    hosted container's `config.json` where any code in the app can read it.
    """
    src = SOURCE.read_text(encoding="utf-8")
    body = src[src.index("def _mint_container_git_token") : src.index("def _container_git_token_name")]
    assert '"git_write": False' in body, "the container credential is still push-capable"


def test_the_analyst_authoring_credential_is_not_marked_clone_only():
    """It exists to push. Absent claim = writable, so it and every token
    minted before this release are unaffected."""
    src = SOURCE.read_text(encoding="utf-8")
    body = src[src.index("def _mint_git_credential") : src.index("def _clone_url_with_credential")]
    assert "git_write" not in body


def test_the_git_surface_denies_a_push_from_a_clone_only_token():
    import pathlib

    git_src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "data_apps_git.py"
    ).read_text(encoding="utf-8")

    block = git_src[git_src.index("is_push = _is_push_request") : git_src.index("return user, app_row, allowed")]
    assert 'payload.get("git_write") is False' in block, "nothing enforces the clone-only claim"
    # …and it must be checked BEFORE ownership, or an owner-minted token passes.
    assert block.index('payload.get("git_write")') < block.index("allowed = is_owner or admin")


def test_absent_claim_still_allows_a_push():
    """`is False`, not falsy: a token with no claim must keep working."""
    import pathlib

    git_src = (
        pathlib.Path(__file__).resolve().parents[1] / "app" / "api" / "data_apps_git.py"
    ).read_text(encoding="utf-8")
    assert 'not payload.get("git_write")' not in git_src, (
        "a falsy check would revoke push for every pre-existing token"
    )
