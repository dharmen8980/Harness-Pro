"""Pure compiler for the provider-agnostic standalone runtime."""
from __future__ import annotations

from copy import deepcopy
import hashlib

from .models import Manifest

GIT_PREFIX = ["git", "--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def _object(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


PATH = {"type": "string", "minLength": 1, "maxLength": 4096, "description": "A path beginning with a declared mount alias, for example repo/src/app.py."}
COMMIT_MESSAGE = {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": "^[^\x00]+$"}
NATIVE_TOOLS = {
    "Read": {
        "description": "Read a UTF-8 text file inside a declared mount.",
        "inputSchema": _object({
            "path": PATH,
            "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
        }, ["path"]),
    },
    "Grep": {
        "description": "Search UTF-8 text files inside a declared mount for literal text.",
        "inputSchema": _object({
            "pattern": {"type": "string", "minLength": 1, "maxLength": 1000},
            "path": PATH,
            "glob": {"type": "string", "minLength": 1, "maxLength": 256},
        }, ["pattern", "path"]),
    },
    "Glob": {
        "description": "Find paths inside a declared mount using a glob pattern.",
        "inputSchema": _object({
            "pattern": {"type": "string", "minLength": 1, "maxLength": 256},
            "path": PATH,
        }, ["pattern"]),
    },
    "Write": {
        "description": "Create or replace a UTF-8 text file inside a writable mount.",
        "inputSchema": _object({
            "path": PATH,
            "content": {"type": "string", "maxLength": 1000000},
        }, ["path", "content"]),
    },
    "Edit": {
        "description": "Replace exact text inside a UTF-8 file in a writable mount.",
        "inputSchema": _object({
            "path": PATH,
            "old_text": {"type": "string", "minLength": 1, "maxLength": 1000000},
            "new_text": {"type": "string", "maxLength": 1000000},
            "replace_all": {"type": "boolean", "default": False},
        }, ["path", "old_text", "new_text"]),
    },
    "Bash": {
        "description": ("Run a Bash command whose leading program is on the manifest's allowlist. "
                        "Each call is a fresh, non-interactive shell; there is no persistent state or "
                        "cwd between calls. The working directory is always the primary mount's root "
                        "(commonly aliased \"repo\") — do not `cd` into it first, use paths relative to "
                        "it directly."),
        "inputSchema": _object({
            "command": {"type": "string", "minLength": 1, "maxLength": 4096},
        }, ["command"]),
    },
    "GitCommit": {
        "description": ("Stage and commit files in the configured git directory, then optionally push the "
                        "current branch. Hooks and commit signing are disabled. Any git failure (a nonzero "
                        "exit code from add, commit, or push) is returned as this tool's result instead of "
                        "raising, so the exact stderr is visible; a later step never runs after an earlier "
                        "one failed."),
        "inputSchema": _object({
            "files": {
                "type": "array", "items": PATH, "minItems": 1, "maxItems": 64,
                "description": "Paths to stage, each beginning with the git directory's mount alias. Omit to stage every tracked change (git add -A).",
            },
            "commit_message": COMMIT_MESSAGE,
            "push": {"type": "boolean", "default": False,
                     "description": "Push the current branch to its remote (with upstream tracking) after committing."},
        }, ["commit_message"]),
    },
    "BuildTest": {
        "description": ("Run the configured format, build, and test commands in that order. "
                        "Stops at the first step with a nonzero exit code and returns exactly that "
                        "step's exit code and output; a later step never runs after an earlier one failed."),
        "inputSchema": _object({}, []),
    },
    "GitLabMrCreate": {
        "description": "Create a GitLab merge request from the current branch using glab.",
        "inputSchema": _object({
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "description": {"type": "string", "maxLength": 100000},
            "target_branch": {"type": "string", "minLength": 1, "maxLength": 256},
        }, ["title", "description"]),
    },
    "GitLabMrNote": {
        "description": "Post a comment on an existing GitLab merge request using glab.",
        "inputSchema": _object({
            "mr_iid": {"type": "integer", "minimum": 1},
            "message": {"type": "string", "minLength": 1, "maxLength": 100000},
        }, ["mr_iid", "message"]),
    },
}


def _artifact_tools(manifest: Manifest) -> list[dict]:
    """Compile artifact_files into at most two tools, regardless of file count.

    The model picks a logical name from a closed enum — it never supplies a path —
    so the mount-alias guessing that broke real runs (model inventing "_artifacts/"
    instead of the declared "artifacts/" alias) is structurally impossible here.
    """
    if "artifact_io" not in manifest.capabilities:
        return []
    readable = sorted(a.name for a in manifest.artifact_files if a.mode in ("read", "both"))
    writable = sorted(a.name for a in manifest.artifact_files if a.mode in ("write", "both"))
    tools = []
    if readable:
        tools.append({
            "name": "ReadArtifact",
            "description": "Read a manifest-declared artifact file by its logical name. Takes a name, never a path.",
            "inputSchema": _object({"name": {"type": "string", "enum": readable}}, ["name"]),
            "kind": "native",
        })
    if writable:
        tools.append({
            "name": "WriteArtifact",
            "description": ("Write a manifest-declared artifact file by its logical name. Takes a name, never "
                            "a path; content is validated against that file's declared schema when one exists, "
                            "and the write is rejected with the validation error if it does not match."),
            "inputSchema": _object({
                "name": {"type": "string", "enum": writable},
                "content": {"type": "string", "maxLength": 1000000},
            }, ["name", "content"]),
            "kind": "native",
        })
    return tools


def _action(name: str, description: str, argv: list[str], cwd: str, timeout: int = 120,
            arg_input: dict | None = None) -> dict:
    action = {
        "name": name,
        "description": description,
        "inputSchema": _object(
            {arg_input["name"]: deepcopy(arg_input["schema"])} if arg_input else None,
            [arg_input["name"]] if arg_input else None,
        ),
        "kind": "action",
        "argv": list(argv),
        "cwd": cwd,
        "timeoutSeconds": timeout,
    }
    if arg_input:
        action["argInput"] = deepcopy(arg_input)
    return action


def _actions(manifest: Manifest) -> list[dict]:
    actions: list[dict] = []
    if "run_tests" in manifest.capabilities:
        command = manifest.test_command
        assert command is not None
        actions.append(_action("run_tests", "Run the configured test or build command with no arguments.", command.argv, command.working_directory, command.timeout_seconds))
    if "custom_commands" in manifest.capabilities:
        actions.extend(_action(command.name, command.description, command.argv, command.working_directory, command.timeout_seconds) for command in manifest.commands)
    if "git_read" in manifest.capabilities:
        assert manifest.git_directory is not None
        actions.extend([
            _action("git_status", "Show Git status.", [*GIT_PREFIX, "status", "--short"], manifest.git_directory),
            _action("git_diff", "Show the Git diff without external diff programs.", [*GIT_PREFIX, "diff", "--no-ext-diff", "--no-textconv"], manifest.git_directory),
            _action("git_log", f"Show the {manifest.git_log_count} most recent commits.", [*GIT_PREFIX, "log", f"-{manifest.git_log_count}", "--oneline", "--no-decorate"], manifest.git_directory),
        ])
    return actions


def _mcp(manifest: Manifest) -> tuple[list[dict], list[dict]]:
    if "mcp_tools" not in manifest.capabilities:
        return [], []
    servers, tools = [], []
    for server in sorted(manifest.mcp_servers, key=lambda item: item.name):
        if not server.selected_tools:
            continue
        launch = {"name": server.name, "type": server.type}
        if server.type == "stdio":
            launch.update({"command": server.command, "args": list(server.args), "env": sorted(server.env)})
        else:
            launch["url"] = server.url
            if server.bearer_token_env:
                launch["bearerTokenEnv"] = server.bearer_token_env
        servers.append(launch)
        snapshots = {tool.name: tool for tool in server.snapshot}
        for tool_name in sorted(server.selected_tools):
            snapshot = snapshots[tool_name]
            identifier = "remote_" + hashlib.sha256(f"{server.name}/{tool_name}".encode()).hexdigest()[:12]
            tools.append({
                "name": f"mcp__harness__{identifier}",
                "description": snapshot.description or snapshot.name,
                "inputSchema": snapshot.inputSchema,
                "kind": "mcp",
                "server": server.name,
                "remoteName": snapshot.name,
                "snapshot": snapshot.model_dump(mode="json"),
            })
    return servers, tools


def _prompt(manifest: Manifest, tools: list[dict]) -> str:
    if manifest.prompt.override is not None:
        return manifest.prompt.override
    lines = [f"# {manifest.display_name}", "", "## Capabilities", ""]
    if tools:
        lines.extend(f"- `{tool['name']}` — {tool['description']}" for tool in tools)
    else:
        lines.append("This agent has no registered tools.")
    if manifest.mounts:
        lines.extend(["", "## Workspace", ""])
        lines.extend(
            f"- `{mount.target}` maps to an explicitly declared {'read-only' if mount.read_only else 'writable'} host directory."
            for mount in manifest.mounts
        )
        lines.extend(["", "Use mount-alias paths such as `repo/src/app.py`; do not use host absolute paths."])
    lines.extend(["", "## Behavior", "", manifest.prompt.behavior.rstrip()])
    for skill in manifest.skills:
        lines.extend(["", f"## Skill: {skill.name}", "", skill.description, "", skill.content.rstrip()])
    lines.extend(["", "Only the manifest-declared tools and instructions are available. Do not assume ambient configuration or tools.", ""])
    return "\n".join(lines)


def compile_standalone(manifest: Manifest | dict) -> dict:
    """Compile a manifest without reading files, environment variables, or the network."""
    if not isinstance(manifest, Manifest):
        manifest = Manifest.model_validate(manifest)
    native_names = []
    for capability in manifest.capabilities:
        if capability == "repo_read":
            native_names.extend(["Read", "Grep", "Glob"])
        elif capability == "file_edit":
            native_names.extend(["Write", "Edit"])
        elif capability == "bash_commands":
            native_names.append("Bash")
        elif capability == "git_write":
            native_names.append("GitCommit")
        elif capability == "build_test":
            native_names.append("BuildTest")
        elif capability == "gitlab_mr":
            native_names.extend(["GitLabMrCreate", "GitLabMrNote"])
    native_tools = [{"name": name, **deepcopy(NATIVE_TOOLS[name]), "kind": "native"} for name in dict.fromkeys(native_names)]
    native_tools.extend(_artifact_tools(manifest))
    actions = _actions(manifest)
    servers, mcp_tools = _mcp(manifest)
    tools = [*native_tools, *actions, *mcp_tools]
    return {
        "version": 1,
        "name": manifest.name,
        "provider": {
            "kind": manifest.provider,
            "baseUrl": manifest.base_url,
            "region": manifest.region if manifest.provider == "bedrock" else None,
            "apiKeyEnv": manifest.api_key_env,
        },
        "model": manifest.model,
        "maxTurns": manifest.turn_limit,
        "wallClockLimitSeconds": manifest.wall_clock_limit_seconds,
        "maxOutputTokens": manifest.max_output_tokens,
        "maxFileBytes": manifest.max_file_bytes,
        "maxToolOutput": manifest.max_tool_output,
        "defaultCommandTimeoutSeconds": manifest.default_command_timeout_seconds,
        "mcpReadTimeoutSeconds": manifest.mcp_read_timeout_seconds,
        "pricing": manifest.pricing.model_dump(mode="json") if manifest.pricing is not None else None,
        "systemPrompt": _prompt(manifest, tools),
        "mounts": [
            {"target": mount.target, "hostPath": mount.host_path, "readOnly": mount.read_only}
            for mount in manifest.mounts
        ],
        "tools": tools,
        "bashAllowlist": list(manifest.allowed_bash_programs),
        "additionalEnvVars": list(manifest.additional_env_vars),
        "gitDirectory": manifest.git_directory,
        "artifactFiles": [
            {"name": a.name, "path": a.path, "mode": a.mode, "writeSchema": a.write_schema}
            for a in manifest.artifact_files
        ],
        "buildTestSteps": {
            name: ({
                "argv": list(step.argv),
                "workingDirectory": step.working_directory,
                "timeoutSeconds": step.timeout_seconds,
            } if step is not None else None)
            for name, step in (
                ("format", manifest.build_test_steps.format),
                ("build", manifest.build_test_steps.build),
                ("test", manifest.build_test_steps.test),
            )
        } if manifest.build_test_steps else None,
        "mcpServers": servers,
    }
