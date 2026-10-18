# Harness Pro

Harness Pro compiles one committable JSON manifest into a small,
provider-agnostic agent runtime. The manifest explicitly selects the model,
workspace, tools, limits, MCP schemas, and behavior for one headless run.

The compiler is deterministic and side-effect free: it does not read mounted
paths, resolve credentials, contact providers, launch commands, or connect to
MCP servers. Those actions occur only when a user explicitly runs the local
CLI.

> **Security boundary:** Harness Pro is a local process, not an operating-system
> sandbox. Native file tools enforce mount and read-only policy in software,
> but configured commands and MCP servers run with the host permissions of the
> Harness process. Use a separate OS-level isolation boundary when that matters.

## Install

Python 3.11 or newer is required.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Install optional features as needed:

```sh
# Web API and builder UI
python -m pip install -e '.[api]'

# MCP discovery and MCP-backed runs
python -m pip install -e '.[snapshot]'

# Everything needed for local development and verification
python -m pip install -e '.[api,snapshot,dev]'
```

## Quick start

Copy an example and replace its placeholder host path and model. Anthropic
manifests use `ANTHROPIC_API_KEY` by default:

```sh
cp examples/standalone-anthropic.json my-agent.json
export ANTHROPIC_API_KEY=...

harness compile --manifest my-agent.json -o runtime.json
harness run --manifest my-agent.json \
  --prompt "Review the mounted source and report your findings." \
  --trace-file trace.jsonl
```

`compile` emits the exact runtime configuration without reading a mount or
resolving an environment variable. `run` performs one provider conversation,
executes only registered tools, writes the JSONL trace to `trace.jsonl`, and
prints the final assistant text.

Without `--trace-file`, the trace is written to stdout. The equivalent module
entry points are `python -m harness`, `python -m harness.compile`, and
`python -m harness.run`.

## Manifest contract

Manifests are strict versioned inputs. Unknown fields are rejected, credential
values are rejected, and paths, commands, URLs, names, and runtime limits are
validated before compilation.

```json
{
  "version": 1,
  "name": "reviewer",
  "display_name": "Reviewer",
  "description": "Review one explicitly mounted source tree.",
  "provider": "anthropic",
  "base_url": null,
  "api_key_env": "ANTHROPIC_API_KEY",
  "model": "claude-sonnet-4-5-20250929",
  "capabilities": ["repo_read"],
  "mounts": [
    {
      "host_path": "/absolute/path/to/project/src",
      "target": "repo",
      "read_only": true
    }
  ],
  "test_command": null,
  "commands": [],
  "git_directory": null,
  "mcp_servers": [],
  "skills": [],
  "prompt": {
    "behavior": "Report only findings you actually observe."
  },
  "pricing": {
    "input_per_million": 3.0,
    "output_per_million": 15.0,
    "cache_read_per_million": 0.3,
    "cache_write_per_million": 3.75
  },
  "turn_limit": 12,
  "wall_clock_limit_seconds": 900
}
```

Tool paths use the declared alias, such as `repo/src/app.py`; providers never
receive host absolute paths in tool definitions. A read-only mount still makes
every file beneath that directory visible to read tools, so choose narrow
source directories and do not mount credential trees.

`pricing` is optional; omit it and cost stays unreported. All four sub-fields
are optional dollars-per-million-tokens rates that you supply and are
responsible for keeping current.

### Capabilities

| Capability | Compiled operations | Additional requirement |
| --- | --- | --- |
| `repo_read` | `Read`, `Grep`, `Glob` | An explicit mount |
| `file_edit` | `Write`, `Edit` | At least one writable mount |
| `run_tests` | One fixed-argv `run_tests` action | `test_command` inside a mount |
| `custom_commands` | One fixed-argv action per named command | Nonempty `commands` list inside mounts |
| `git_read` | Fixed status, diff, and recent-log actions | `git_directory` inside a mount |
| `git_write` | `git_commit` with one bounded message | Writable `git_directory` |
| `mcp_tools` | Individually selected, snapshot-verified MCP tools | Imported server snapshots |

The test/build operation and custom commands accept no model-supplied
arguments. For example, a manifest can expose a fixed `glab mr list` command:

```json
{
  "capabilities": ["custom_commands"],
  "commands": [
    {
      "name": "list_mrs",
      "description": "List GitLab merge requests.",
      "argv": ["glab", "mr", "list"],
      "working_directory": "repo",
      "timeout_seconds": 120
    }
  ]
}
```

Git commit uses
`git commit -a`, so it commits tracked changes but does not stage untracked
files; hooks and commit signing are disabled. There is no arbitrary shell or
general web capability.

Inline skills are prompt text, not executable packages. Capability and
workspace guidance is generated from the compiled tool list, so prompt text
cannot grant additional access.

## Providers

### Anthropic

- `provider` is `"anthropic"`.
- `base_url` must be `null` or omitted; the runtime uses the fixed Messages API
  endpoint.
- `api_key_env` must name a populated environment variable.

### OpenAI-compatible

- `provider` is `"openai-compatible"`.
- `base_url` is required. HTTPS is required except for loopback HTTP endpoints.
- The runtime calls `<base_url>/chat/completions`.
- `api_key_env` names the credential environment variable, or may be `null`
  for an unauthenticated endpoint.

### Amazon Bedrock

- `provider` is `"bedrock"` and `region` is a required AWS region such as
  `"us-east-1"`; `base_url` must be `null` or omitted.
- `api_key_env` names the bearer-token environment variable. The conventional
  name is `AWS_BEARER_TOKEN_BEDROCK`, populated with a short-term or long-term
  Bedrock API key generated through AWS.
- `model` must carry the `anthropic.` prefix, such as
  `"anthropic.claude-sonnet-5"`.

This path uses Bedrock Mantle bearer-token authentication only; it does not
implement SigV4 and adds no AWS SDK dependency.

See [standalone-local.json](examples/standalone-local.json) for a loopback
configuration. Provider and MCP credential values are resolved only in the
runtime process and are never placed in the compiled config.

## Examples

- [Anthropic reviewer](examples/standalone-anthropic.json)
- [OpenAI-compatible local reviewer](examples/standalone-local.json)
- [Fixer](examples/standalone-fixer.json)
- [Release committer](examples/standalone-release-committer.json)

All example host paths are placeholders unless they explicitly point to a
local test fixture. Review every manifest before running it.

## MCP discovery

MCP tools require a locally captured schema. The builder API never connects to
a server or launches an executable.

Start with a server launch description such as
[standalone-mcp-server.json](examples/standalone-mcp-server.json), then capture
its advertised tools on your own machine:

```sh
python -m builder.snapshot examples/standalone-mcp-server.json -o snapshot.json
```

Inspect the result, add the desired names to `selected_tools`, and place that
server object in the manifest's `mcp_servers` array. The UI can also import the
snapshot and select tools individually.

For a stdio server, each key in `env` is the name of an environment variable
required at runtime; its committable value remains the literal
`"placeholder"`. Only those declared values and a small process-basics
environment are passed to the subprocess. A streamable HTTP server may set
`bearer_token_env` to the name of one environment variable; snapshot capture
resolves it locally and the runtime resolves it again when connecting. This is
Bearer-only authentication, and the token value is never stored in the
manifest.

Before every remote call, the runtime checks the selected tool's name,
normalized description, and input schema against the snapshot. This detects a
changed interface, but it does not freeze the server implementation or limit
its side effects. Treat every configured MCP server as trusted code.

## Builder UI and compile API

The browser builder edits and validates manifests, previews the compiled
system prompt and tool schemas, and downloads the manifest JSON. It never runs
an agent.

```sh
npm --prefix frontend ci
npm --prefix frontend run build
python -m uvicorn builder.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. For UI development, run
`npm --prefix frontend run dev` separately; Vite proxies `/api` to port 8000.

| Endpoint | Response |
| --- | --- |
| `GET /api/capabilities` | Capability catalog, defaults, presets, and manifest JSON Schema |
| `POST /api/compile` | Compiled provider, prompt, mounts, tool schemas, and MCP launch config |

Post a manifest JSON object directly to `/api/compile`. Invalid inputs return
HTTP 422 without echoing submitted values. There is intentionally no API
endpoint that resolves credentials, contacts a provider, or executes tools.

## Trace and exit contract

Each run emits newline-delimited JSON using schema `harness.trace.v1`. Every
record has a monotonic `sequence`, UTC `timestamp`, and `event` field. Events
include:

- `run.started` and `run.completed`
- `provider.request` and `provider.response`
- `turn.started` and `turn.completed`
- `tool.started` and `tool.completed`
- `policy.decision`

Known provider and MCP credential values are replaced with `[REDACTED]` before
records are written. Traces still contain prompts, provider responses, tool
arguments, and tool output, which can include source content. Store and share
them accordingly.

Process exit codes are stable:

| Code | Meaning |
| --- | --- |
| `0` | Completed successfully |
| `2` | Invalid manifest or compile input |
| `3` | Turn limit exhausted |
| `4` | Tool or path policy violation |
| `5` | Provider configuration or response failure |
| `6` | Wall-clock limit exceeded |
| `7` | Tool or other runtime failure |

## Verify

```sh
python -m pytest -q
python scripts/export_manifest_schema.py
npm --prefix frontend run types
npm --prefix frontend run build
```

The Python suite exercises manifest compilation, provider translation, path
policy, bounded commands, Git commit behavior, MCP snapshot checks, tracing,
and the compile-only API.
