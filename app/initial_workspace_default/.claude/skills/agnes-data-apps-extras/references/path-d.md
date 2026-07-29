# Path D — Agnes harness

The general `dataapp-development` skill's `references/deployment-paths.md`
enumerates deployment paths A/B/C and detects them by scanning for
`mcp__*[Kk]eboola*` tools and `which kbagent`. **Path D — Agnes harness**
is the Agnes-specific addition:

- **Detection:** the presence of the Agnes data-app MCP tools (`data_app_*`
  — `data_app_get`, `data_app_deploy`, `data_app_create_draft`,
  `data_app_delete_draft`, `data_app_git_credential`, `data_app_logs`, plus
  the chat-only `agnes_data_app_preview`/`_refresh`/`_close`/`_credentials`
  tools) in the current MCP tool list. No Keboola-platform MCP tool and no
  `kbagent` binary are present in an Agnes chat sandbox.
- **Deploy:** via the Agnes MCP tools above (never a raw `git push` outside
  the tool-mediated flow, and never shell access to the runtime container).
- **Reachability:** the running app is served at `/apps/<slug>/` on the
  Agnes instance, and previewed in-chat via the `agnes_data_app_preview`
  tool family (see the parent skill's section 3).
- **Managed repo:** the app's git repo is hosted by Agnes internally at
  `/data-apps.git/<slug>`; drafts are pinned branches of the same repo, not
  a second repo.

**Status:** this file is an interim overlay. Path D is also being
contributed upstream to `keboola/ai-kit`'s `references/deployment-paths.md`
as a first-class section (Agnes is itself Keboola-OSS, so it belongs in the
shared skill). Once that upstream PR merges, this file collapses to a
one-line pointer: *"Path D is documented in the general skill's
`deployment-paths.md`."* Until then, this overlay is the source of truth for
Agnes sessions.

If both a Keboola-platform MCP and the Agnes `data_app_*` tools are present
in the same session, follow the general skill's "pick one path per session"
rule — ask the user which project/harness they mean before doing anything.
