# Connect Codex and Claude Code to project data

OpenCLI exposes existing project records, cited knowledge snippets, and research runs through
the same REST contracts used by its MCP tools. Record and context reads do not collect sources,
start research, or invoke a model.

## Credentials and project scope

Every request is authorized again by the API for the exact workspace and project in the URL.
A caller needs a current user session or OIDC bearer with membership in the governed workspace.
Legacy Studio-only projects are deliberately limited to the local or bootstrap platform
administrator bridge.

`API_AUTH_TOKEN` is only the optional fleet transport credential. It is not a user identity and
does not grant access to any workspace or project. When fleet authentication is enabled, an HTTP
client sends it as `X-API-Token` and sends the user's bearer separately as `Authorization`:

```text
X-API-Token: <fleet transport token>
Authorization: Bearer <user session or OIDC token>
```

Do not place either credential in prompts, repository files, or MCP tool arguments.

## REST

Set these shell variables in your local secret manager or shell environment, then query one exact
project:

```bash
curl --fail-with-body \
  -H "X-API-Token: $API_AUTH_TOKEN" \
  -H "Authorization: Bearer $OPENCLI_CALLER_TOKEN" \
  "http://localhost:8031/api/v1/workspaces/$WORKSPACE_ID/projects/$PROJECT_ID/agent-data/records?limit=20"

curl --fail-with-body \
  -H "X-API-Token: $API_AUTH_TOKEN" \
  -H "Authorization: Bearer $OPENCLI_CALLER_TOKEN" \
  --get --data-urlencode "q=How does project authentication work?" \
  "http://localhost:8031/api/v1/workspaces/$WORKSPACE_ID/projects/$PROJECT_ID/agent-data/context"
```

The records response is `{items, total}`. Each item contains normalized `data`, a content-hash
`version`, `updated_at`, and safe source identifiers. Raw payloads, enrichment internals, source
configuration, and credential-shaped fields are excluded.

The context response is `{query, matches, gaps}`. A match identifies a normalized project record,
a published page from an explicitly bound project knowledge library (with an optional legacy
product scope), or a durable
`completed`/`partial` research run for that exact project. Knowledge matches include page revision,
content hash, update time, and source references. Research matches include the run ID, result
version hash, run status, template, gaps, and cited source metadata.

A research match with `evidence_kind: model-analyzed-finding` contains only a finding whose quoted
evidence can be checked against retained source content. Its `evidence` entries preserve the
validated `source_id` and quote. A `captured-source` or `captured-change` match is explicitly
labelled as captured evidence and must not be presented as a model conclusion. Run summaries are
not treated as evidence. Partial-run gaps are returned both with the match and in the top-level
`gaps` list so a downstream Agent cannot silently hide missing analysis. Context retrieval never
starts fetching or model work, and no workspace-wide or global knowledge fallback is used.

Check `/agent-data/capabilities` before offering research. Research readiness and execution use:

```text
GET  /api/v1/workspaces/{workspace_id}/projects/{project_id}/research/readiness
POST /api/v1/workspaces/{workspace_id}/projects/{project_id}/research/runs
GET  /api/v1/workspaces/{workspace_id}/projects/{project_id}/research/runs
GET  /api/v1/workspaces/{workspace_id}/projects/{project_id}/research/runs/{run_id}
```

Starting research requires a stable project-scoped `request_id`. Poll the returned run instead of
reissuing the task with a new ID. Existing record/context queries never start this route.

## stdio MCP setup

The `opencli-mcp` process is an HTTP client of the OpenCLI API. Give it the API URL, the optional
fleet credential, and an explicit user caller token. The caller token is required for project data
tools even when the fleet token is configured.

Codex uses a server entry in its local MCP configuration:

```toml
[mcp_servers.opencli]
command = "opencli-mcp"
env_vars = ["OPENCLI_ADMIN_API_URL", "API_AUTH_TOKEN", "OPENCLI_MCP_CALLER_TOKEN"]
```

Set those three environment variables in the process that starts Codex. `env_vars`
forwards their values to the MCP process; `${VARIABLE}` inside a Codex `env` value
would be passed literally and does not perform shell expansion.

Claude Code can use the equivalent project or user MCP JSON configuration:

```json
{
  "mcpServers": {
    "opencli": {
      "command": "opencli-mcp",
      "env": {
        "OPENCLI_ADMIN_API_URL": "http://localhost:8031",
        "API_AUTH_TOKEN": "<fleet transport token>",
        "OPENCLI_MCP_CALLER_TOKEN": "<user session or OIDC token>"
      }
    }
  }
}
```

Use your client's secret/environment injection facility instead of committing literal values.
If fleet authentication is disabled for a loopback-only deployment, omit `API_AUTH_TOKEN`; the
caller token is still needed for project authorization.

## Streamable HTTP MCP

For `/mcp`, the MCP client must support both headers. OpenCLI forwards the incoming
`Authorization` bearer as the caller and uses its own configured fleet token only for transport to
the REST API. An HTTP MCP request that supplies only the fleet token has no project identity and
project tools fail authorization.

Available project tools are:

- `query_project_records`
- `query_project_context`
- `get_project_data_capabilities`
- `get_project_research_readiness`
- `start_project_research`
- `list_project_research_runs`
- `get_project_research_run`

Record and context tools are read-only. `start_project_research` is the only tool in this group
that starts work, and it delegates to the shared REST research engine with the supplied stable
request ID.

The same research and retrieval tools work with configured local model servers. See
[local model setup](local-models.md) for Ollama, LM Studio and other OpenAI-compatible services.

## Failure meanings

Record queries search redacted projections of at most the 500 most recently updated readable
project records. `truncated` reports this scan limit; `total_is_exact: false` means `total` is only
a known lower bound, not proof that older matches do not exist. Each record's normalized data
and lineage projection is bounded to depth 8, 200 nodes, 16,000 text characters and 4,000
characters per string. `projection_truncated` reports omitted content on both records and the
list; a query count is also inexact when a scanned projection was truncated. Context retrieval
propagates these limitations in `gaps`. Credential fields are removed before matching and
counting; neither candidate selection nor truncation depends on secret-value query matches.

- `401` means the user bearer is absent or invalid.
- `503` with `OIDC is not configured` means the bearer is not a valid local/bootstrap
  identity and this deployment has no configured OIDC verifier; no project data is returned.
- `403` means the authenticated caller lacks workspace membership or permission.
- `404` for a project can also mean that the project is outside the requested workspace; the API
  does not reveal cross-workspace existence.
- An empty `matches` list with `gaps` is a successful retrieval with no supported answer. It is not
  a synthesized research result.
