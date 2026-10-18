"""Command-line interface for standalone MCP OAuth login and refresh."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata

from .callback import (
    CALLBACK_TIMEOUT_SECONDS,
    CallbackTimeout,
    InteractiveOAuthHandlers,
    NonInteractiveAuthorizationRequired,
    reject_interactive_callback,
    reject_interactive_redirect,
)
from .client import initialize_mcp_connection
from .config import ConfigError, ServerConfig, load_config
from .storage import FileTokenStorage, StorageError, default_storage_path, write_private_text


EXIT_CONFIG = 2
EXIT_LOGIN_REQUIRED = 3
EXIT_AUTH = 4
EXIT_TIMEOUT = 5
EXIT_IO = 6

LOGIN_TIMEOUT_SECONDS = CALLBACK_TIMEOUT_SECONDS + 30.0
REFRESH_TIMEOUT_SECONDS = 60.0


class LoginRequiredError(RuntimeError):
    """Stored credentials cannot support a non-interactive refresh."""


def _client_metadata(config: ServerConfig) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        redirect_uris=[f"http://localhost:{config.redirect_port}/callback"],
        client_name="harness-oauth-sidecar",
    )


async def login(config: ServerConfig, storage: FileTokenStorage) -> None:
    handlers = InteractiveOAuthHandlers(config.redirect_port)
    provider = OAuthClientProvider(
        server_url=config.server_url,
        client_metadata=_client_metadata(config),
        storage=storage,
        redirect_handler=handlers.redirect,
        callback_handler=handlers.callback,
        timeout=CALLBACK_TIMEOUT_SECONDS,
    )
    try:
        await asyncio.wait_for(
            initialize_mcp_connection(config.server_url, provider, timeout=LOGIN_TIMEOUT_SECONDS),
            timeout=LOGIN_TIMEOUT_SECONDS,
        )
        await storage.set_discovery_metadata(
            provider.context.oauth_metadata,
            provider.context.protected_resource_metadata,
        )
    finally:
        await handlers.close()


async def refresh_access_token(config: ServerConfig, storage: FileTokenStorage) -> str:
    tokens = await storage.get_tokens()
    client_info = await storage.get_client_info()
    if tokens is None or client_info is None:
        raise LoginRequiredError("No stored OAuth login was found.")
    if not tokens.refresh_token or not client_info.client_id:
        raise LoginRequiredError("Stored OAuth credentials cannot be refreshed non-interactively.")
    oauth_metadata, resource_metadata = await storage.get_discovery_metadata()

    provider = OAuthClientProvider(
        server_url=config.server_url,
        client_metadata=_client_metadata(config),
        storage=storage,
        redirect_handler=reject_interactive_redirect,
        callback_handler=reject_interactive_callback,
        timeout=REFRESH_TIMEOUT_SECONDS,
    )
    provider.context.oauth_metadata = oauth_metadata
    provider.context.protected_resource_metadata = resource_metadata
    if resource_metadata is not None and resource_metadata.authorization_servers:
        provider.context.auth_server_url = str(resource_metadata.authorization_servers[0])
    # MCP 1.30 does not reconstruct token_expiry_time when it loads an OAuthToken
    # from storage. A positive timestamp in the past makes its own refresh-token
    # branch run before the MCP request, so this command always returns a fresh token.
    provider.context.token_expiry_time = 1.0

    await asyncio.wait_for(
        initialize_mcp_connection(config.server_url, provider, timeout=REFRESH_TIMEOUT_SECONDS),
        timeout=REFRESH_TIMEOUT_SECONDS,
    )
    current_tokens = provider.context.current_tokens
    if current_tokens is None or not current_tokens.access_token:
        raise LoginRequiredError("OAuth refresh did not produce an access token.")
    if current_tokens.refresh_token is None:
        current_tokens = current_tokens.model_copy(update={"refresh_token": tokens.refresh_token})
        await storage.set_tokens(current_tokens)
    return current_tokens.access_token


def _load_command_config(path: Path) -> tuple[ServerConfig, FileTokenStorage]:
    config = load_config(path)
    return config, FileTokenStorage(default_storage_path(config.name))


def login_command(args: argparse.Namespace) -> int:
    try:
        config, storage = _load_command_config(args.server_config)
        asyncio.run(login(config, storage))
        print(f"OAuth login succeeded for {config.name}. Credentials saved to {storage.path}")
        return 0
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIG
    except (CallbackTimeout, TimeoutError):
        print("Login timed out waiting for the browser callback.", file=sys.stderr)
        return EXIT_TIMEOUT
    except StorageError as error:
        print(str(error), file=sys.stderr)
        return EXIT_IO
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"OAuth login failed ({type(error).__name__}). Check connectivity and try again.", file=sys.stderr)
        return EXIT_AUTH


def refresh_command(args: argparse.Namespace) -> int:
    try:
        config, storage = _load_command_config(args.server_config)
        access_token = asyncio.run(refresh_access_token(config, storage))
        if args.output is None:
            print(access_token)
        else:
            write_private_text(args.output, access_token + "\n")
        return 0
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return EXIT_CONFIG
    except (LoginRequiredError, NonInteractiveAuthorizationRequired):
        print("Non-interactive OAuth refresh failed. Run 'python -m oauth_sidecar login <server-config.json>' again.", file=sys.stderr)
        return EXIT_LOGIN_REQUIRED
    except (CallbackTimeout, TimeoutError):
        print("OAuth refresh timed out. No interactive authorization was attempted.", file=sys.stderr)
        return EXIT_TIMEOUT
    except StorageError as error:
        print(str(error), file=sys.stderr)
        return EXIT_IO
    except OSError:
        print("The access-token output file could not be written securely.", file=sys.stderr)
        return EXIT_IO
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(
            f"OAuth refresh failed ({type(error).__name__}). Re-run login; no interactive authorization was attempted.",
            file=sys.stderr,
        )
        return EXIT_AUTH


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="python -m oauth_sidecar",
        description="Bootstrap and refresh OAuth access tokens for remote MCP servers.",
    )
    commands = root.add_subparsers(dest="command", required=True)

    login_parser = commands.add_parser("login", help="perform one interactive OAuth login")
    login_parser.add_argument("server_config", type=Path, help="server configuration JSON")
    login_parser.set_defaults(handler=login_command)

    refresh_parser = commands.add_parser("refresh", help="refresh and emit an OAuth access token")
    refresh_parser.add_argument("server_config", type=Path, help="server configuration JSON")
    refresh_parser.add_argument("-o", "--output", type=Path, help="write the token to a mode-0600 file")
    refresh_parser.set_defaults(handler=refresh_command)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
