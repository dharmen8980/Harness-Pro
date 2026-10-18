# MCP OAuth token-refresher sidecar

This is a standalone helper for bootstrapping and refreshing OAuth credentials
for remote MCP servers. It is not part of the harness process or the harness's
trust boundary: run it externally, then pass only the emitted short-lived access
token into the harness through the existing `bearer_token_env` mechanism.

## Setup and configuration

Use the repository virtual environment, which already contains the MCP SDK, or
install this separate package into another environment:

```console
source .venv/bin/activate
python -m pip install -e ./oauth-sidecar
```

Create `atlassian-oauth.json`:

```json
{
  "name": "atlassian",
  "server_url": "https://mcp.atlassian.com/v2/mcp",
  "redirect_port": 8765
}
```

`redirect_port` is optional and defaults to `8765`. The name is used for the
credential file at `~/.config/harness-oauth/<name>.json`.

## Commands

Run the one-time login from a machine where a human can use a browser:

```console
python -m oauth_sidecar login atlassian-oauth.json
```

The command prints the authorization URL and makes a best-effort attempt to open
it in the default browser. Complete consent there. Live login cannot be automated
because the authorization server requires human consent.

Refresh later without a browser and capture the access token on stdout:

```console
TOKEN=$(python -m oauth_sidecar refresh atlassian-oauth.json)
```

Or write it directly to a private file without also printing it:

```console
python -m oauth_sidecar refresh atlassian-oauth.json -o /run/secrets/atlassian-access-token
```

`refresh` requires the prior login and a stored refresh token. It fails loudly
and tells the operator to log in again if the authorization server rejects the
refresh; it never opens a browser or waits for interactive input.

## Security properties

- Credential storage is created under a mode-`0700` directory, and both stored
  credentials and `-o` output are atomically created with mode `0600`.
- Refresh tokens and dynamically registered client secrets are never printed.
  The only intentional secret output is the access token from `refresh`.
- The one-shot callback listener binds only to `127.0.0.1`, returns a fixed HTML
  response, and closes after receiving the callback.
- The MCP SDK generates an S256 PKCE verifier/challenge and a random OAuth state,
  then compares the returned state with `secrets.compare_digest` before it
  exchanges the authorization code. The sidecar returns the callback state to
  the SDK rather than duplicating that protocol logic.
- The SDK's validated discovery models are stored with the credentials so a
  later, separate `refresh` process uses the discovered token endpoint even
  when the authorization server is on a different origin from the MCP server.
- SDK OAuth error logging is suppressed around the connection, and CLI failures
  do not print exception bodies that could contain credentials.

In production, run `refresh` from a cron job or an orchestrator pre-spawn hook on
a schedule shorter than the access token lifetime. Write its output into the
secret mechanism that populates the harness container's `bearer_token_env`
variable (for example, a Kubernetes Secret or Vault). This tool deliberately
does not schedule itself and does not inject environment variables or manage
containers.
