"""Committable input schema. Validation never reads mounts or executes servers."""
import hashlib
import json
import re
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .catalog import BASH_PROGRAMS, BY_ID

DEFAULT_BEHAVIOR = (
    "Stop when the task is complete. Do not fabricate file contents or test results.\n"
    "Report what changed and which checks you actually ran. Say when you are uncertain."
)
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
TARGET = re.compile(r"^[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_.-]+)*$")
AWS_REGION = re.compile(r"^[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+$")
BEDROCK_MODEL = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")
SECRET = re.compile(r"(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[A-Z0-9]{16}|-----BEGIN .*PRIVATE KEY)")


def valid_name(value: str) -> str:
    if not NAME.fullmatch(value) or len(value) > 64 or "--" in value or ".." in value:
        raise ValueError("Use a lowercase name with letters, numbers, single hyphens or periods (max 64)")
    return value


def target_path(value: str) -> str:
    if len(value) > 128 or not TARGET.fullmatch(value) or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("Use a relative mount name, such as repo or repo/src")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class Mount(StrictModel):
    host_path: str = Field(min_length=1, max_length=4096)
    target: str = "repo"
    read_only: bool = Field(default=True, strict=True)

    _target = field_validator("target")(target_path)

    @field_validator("host_path")
    @classmethod
    def narrow_path(cls, value: str) -> str:
        if not value.startswith("/") or "\\" in value or "\0" in value or any(p in (".", "..") for p in value.split("/")):
            raise ValueError("Use an absolute host path without traversal")
        if value == "/" or re.fullmatch(r"/(?:Users|home)/[^/]+/?", value):
            raise ValueError("Choose a narrow source folder, not a filesystem or home root")
        if any(p in {".ssh", ".aws", ".gnupg", ".kube", ".docker"} or p.startswith(".env") for p in value.split("/")):
            raise ValueError("Credential paths cannot be mount roots")
        return value.rstrip("/")


class FixedCommand(StrictModel):
    argv: list[str] = Field(min_length=1, max_length=64)
    working_directory: str = "repo"
    timeout_seconds: int = Field(default=120, ge=1, le=600, strict=True)

    _cwd = field_validator("working_directory")(target_path)

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: list[str]) -> list[str]:
        if any(not s.strip() or "\0" in s or len(s) > 4096 for s in value) or value[0].startswith("-"):
            raise ValueError("Provide an executable and fixed argument strings")
        return value


class BuildTestSteps(StrictModel):
    format: FixedCommand | None = None
    build: FixedCommand | None = None
    test: FixedCommand | None = None


class NamedCommand(FixedCommand):
    name: str
    description: str = Field(min_length=1, max_length=4096)

    @field_validator("name")
    @classmethod
    def command_name(cls, value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise ValueError("Command names must contain only letters, numbers, hyphens and underscores")
        return value

    @field_validator("description")
    @classmethod
    def meaningful_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Use a meaningful command description")
        return value


class ArtifactFile(StrictModel):
    """A named file the agent reads/writes by logical name instead of a path.

    Declaring N of these still compiles to at most two tools total (ReadArtifact,
    WriteArtifact) — the enum of names grows, not the tool count — which is the
    point: the model picks a closed-set name, never guesses a mount alias or path.
    """
    name: str
    description: str = Field(min_length=1, max_length=4096)
    path: str
    mode: Literal["read", "write", "both"] = "both"
    write_schema: dict | None = None

    _path = field_validator("path")(target_path)

    @field_validator("name")
    @classmethod
    def artifact_name(cls, value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise ValueError("Artifact names must contain only letters, numbers, hyphens and underscores")
        return value

    @field_validator("description")
    @classmethod
    def meaningful_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Use a meaningful artifact description")
        return value

    @field_validator("write_schema")
    @classmethod
    def valid_write_schema(cls, value: dict | None) -> dict | None:
        if value is not None:
            try:
                Draft202012Validator.check_schema(value)
            except SchemaError as error:
                raise ValueError(f"Invalid write_schema: {error.message}") from error
        return value

    @model_validator(mode="after")
    def schema_requires_write_mode(self):
        if self.write_schema is not None and self.mode == "read":
            raise ValueError("write_schema has no effect on a read-only artifact file")
        return self


class ToolSnapshot(StrictModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=4096)
    inputSchema: dict

    @field_validator("description")
    @classmethod
    def meaningful_description(cls, value: str) -> str:
        if value and not value.strip():
            raise ValueError("Use a meaningful tool description or an empty string")
        return value

    @field_validator("name")
    @classmethod
    def tool_name(cls, value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise ValueError("Tool names must contain only letters, numbers, hyphens and underscores")
        return value

    @field_validator("inputSchema")
    @classmethod
    def object_schema(cls, value: dict) -> dict:
        if value.get("type") != "object" or len(json.dumps(value)) > 50000:
            raise ValueError("Tool inputSchema must be an object schema under 50 KB")
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                reference = item.get("$ref")
                if reference is not None and (not isinstance(reference, str) or not reference.startswith("#")):
                    raise ValueError("Tool inputSchema cannot contain external $ref values")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        return value


class McpServer(StrictModel):
    name: str
    type: Literal["stdio", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list, max_length=64)
    env: dict[str, Literal["placeholder"]] = Field(default_factory=dict)
    url: str | None = None
    bearer_token_env: str | None = Field(default=None, max_length=128)
    snapshot: list[ToolSnapshot] = Field(default_factory=list, max_length=256)
    selected_tools: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("name")
    @classmethod
    def server_name(cls, value: str) -> str:
        if not IDENTIFIER.fullmatch(value) or value in {"__proto__", "constructor", "prototype", "harness"}:
            raise ValueError("Use a distinct server name with letters, numbers, hyphens and underscores")
        return value

    @field_validator("bearer_token_env")
    @classmethod
    def bearer_environment_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("bearer_token_env must be an environment variable name or null")
        return value

    @model_validator(mode="after")
    def transport_and_snapshot(self):
        if self.type == "stdio":
            if self.bearer_token_env is not None:
                raise ValueError("stdio servers cannot use bearer_token_env")
            if not self.command or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.command) or self.url:
                raise ValueError("stdio requires a bare executable name and no URL")
        else:
            if not self.url or self.command or self.args or self.env:
                raise ValueError("HTTP requires a URL and no command, arguments or environment")
            u = urlsplit(self.url)
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"localhost", "127.0.0.1", "::1"}):
                raise ValueError("Use HTTPS (HTTP is allowed only for loopback servers)")
            if not u.hostname or u.username or u.password or u.fragment:
                raise ValueError("MCP URLs cannot contain credentials or fragments")
            if any(re.search(r"token|secret|key|password|auth|signature", k, re.I) for k, _ in parse_qsl(u.query)):
                raise ValueError("MCP credentials belong in runtime environment configuration, not URLs")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in self.env):
            raise ValueError("Invalid environment variable name")
        if any("\0" in arg or len(arg) > 4096 for arg in self.args):
            raise ValueError("Invalid MCP argument")
        names = [tool.name for tool in self.snapshot]
        if len(names) != len(set(names)) or len(self.selected_tools) != len(set(self.selected_tools)):
            raise ValueError("Snapshot and selected tool names must be unique")
        if not set(self.selected_tools) <= set(names):
            raise ValueError("Every selected MCP tool needs a captured schema in the snapshot")
        return self

    def launch_config(self) -> dict:
        if self.type == "streamable-http":
            return {"type": self.type, "url": self.url, **({"bearerTokenEnv": self.bearer_token_env} if self.bearer_token_env else {})}
        return {"type": "stdio", "command": self.command, "args": self.args, **({"env": self.env} if self.env else {})}


class Skill(StrictModel):
    name: str
    description: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=10000)

    _name = field_validator("name")(valid_name)


class PromptOverrides(StrictModel):
    behavior: str = Field(default=DEFAULT_BEHAVIOR, min_length=1, max_length=10000)
    override: str | None = Field(default=None, max_length=20000)


class Pricing(StrictModel):
    input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    cache_read_per_million: float | None = Field(default=None, ge=0)
    cache_write_per_million: float | None = Field(default=None, ge=0)


class Manifest(StrictModel):
    version: Literal[1] = 1
    name: str = "reviewer"
    display_name: str = Field(default="Reviewer", min_length=1, max_length=100)
    description: str = Field(default="Review an explicitly mounted source directory.", max_length=512)
    provider: Literal["anthropic", "openai-compatible", "bedrock"] = "anthropic"
    base_url: str | None = Field(default=None, max_length=2048)
    region: str | None = Field(default=None, max_length=64)
    api_key_env: str | None = Field(default="ANTHROPIC_API_KEY", max_length=128)
    model: str = Field(default="claude-sonnet-4-5-20250929", min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=lambda: ["repo_read"], max_length=len(BY_ID))
    mounts: list[Mount] = Field(default_factory=lambda: [Mount(host_path="/absolute/path/to/project/src")], max_length=32)
    test_command: FixedCommand | None = None
    build_test_steps: BuildTestSteps | None = None
    commands: list[NamedCommand] = Field(default_factory=list, max_length=16)
    artifact_files: list[ArtifactFile] = Field(default_factory=list, max_length=32)
    allowed_bash_programs: list[str] = Field(default_factory=list, max_length=len(BASH_PROGRAMS))
    additional_env_vars: list[str] = Field(default_factory=list, max_length=32)
    git_directory: str | None = None
    mcp_servers: list[McpServer] = Field(default_factory=list, max_length=16)
    skills: list[Skill] = Field(default_factory=list, max_length=16)
    prompt: PromptOverrides = Field(default_factory=PromptOverrides)
    pricing: Pricing | None = None
    turn_limit: int = Field(default=12, ge=1, le=200, strict=True)
    wall_clock_limit_seconds: int = Field(default=900, ge=1, le=86400, strict=True)
    max_output_tokens: int = Field(default=8192, ge=1, le=64000, strict=True)
    max_file_bytes: int = Field(default=1_000_000, ge=1, le=10_000_000, strict=True)
    max_tool_output: int = Field(default=1_000_000, ge=1, le=10_000_000, strict=True)
    default_command_timeout_seconds: int = Field(default=120, ge=1, le=600, strict=True)
    git_log_count: int = Field(default=10, ge=1, le=1000, strict=True)
    mcp_read_timeout_seconds: int = Field(default=30, ge=1, le=300, strict=True)

    _name = field_validator("name")(valid_name)

    @field_validator("model", "display_name")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Provide nonempty text")
        return value

    @field_validator("additional_env_vars")
    @classmethod
    def additional_environment_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in values):
            raise ValueError("additional_env_vars entries must be environment variable names")
        if len(values) != len(set(values)):
            raise ValueError("additional_env_vars entries must be unique")
        # Belt and suspenders: reject valid-looking identifiers shaped like pasted secret values.
        if any(len(value) > 64 or SECRET.fullmatch(value) for value in values):
            raise ValueError("additional_env_vars entries must be names, not secret values")
        return values

    @model_validator(mode="after")
    def consistency(self):
        if self.provider == "anthropic":
            if self.base_url is not None:
                raise ValueError("Anthropic uses its fixed Messages API endpoint; omit base_url")
            if self.api_key_env is None:
                raise ValueError("Anthropic requires api_key_env")
        elif self.provider == "bedrock":
            if not self.region or not AWS_REGION.fullmatch(self.region):
                raise ValueError("Bedrock region must be an AWS region such as us-east-1")
            if self.base_url is not None:
                raise ValueError("Bedrock derives its Messages API endpoint from region; omit base_url")
            if self.api_key_env is None:
                raise ValueError("Bedrock requires api_key_env")
            if not BEDROCK_MODEL.match(self.model):
                raise ValueError("Bedrock model must be anthropic.<model> or a cross-region inference profile such as us.anthropic.<model>")
        else:
            if not self.base_url:
                raise ValueError("openai-compatible providers require base_url")
            url = urlsplit(self.base_url)
            loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
            if (url.scheme != "https" and not (url.scheme == "http" and loopback)) or not url.hostname:
                raise ValueError("base_url must use HTTPS (HTTP is allowed only for loopback endpoints)")
            if url.username or url.password or url.query or url.fragment:
                raise ValueError("base_url cannot contain credentials, a query, or a fragment")
            if "api_key_env" not in self.model_fields_set:
                object.__setattr__(self, "api_key_env", "OPENAI_API_KEY")
        if self.api_key_env is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.api_key_env):
            raise ValueError("api_key_env must be an environment variable name or null")
        if len(self.capabilities) != len(set(self.capabilities)) or set(self.capabilities) - BY_ID.keys():
            raise ValueError("Select unique capabilities from the catalog")
        if (len(self.allowed_bash_programs) != len(set(self.allowed_bash_programs))
                or set(self.allowed_bash_programs) - set(BASH_PROGRAMS)):
            raise ValueError("Select unique Bash programs from the catalog")
        for values in ([m.target for m in self.mounts], [s.name for s in self.mcp_servers], [s.name for s in self.skills], [c.name for c in self.commands], [a.name for a in self.artifact_files]):
            if len(values) != len(set(values)):
                raise ValueError("Mount targets, server names, skill names, command names, and artifact file names must each be unique")
        if sum(len(server.selected_tools) for server in self.mcp_servers) > 128:
            raise ValueError("Select at most 128 MCP tools across all servers")
        if "file_edit" in self.capabilities and not any(not m.read_only for m in self.mounts):
            raise ValueError("Edit files requires at least one explicitly writable mount")
        if "run_tests" in self.capabilities and self.test_command is None:
            raise ValueError("Run tests requires a fixed command")
        if ("build_test" in self.capabilities
                and (self.build_test_steps is None
                     or not any((self.build_test_steps.format, self.build_test_steps.build, self.build_test_steps.test)))):
            raise ValueError("Build/test requires at least one configured step")
        if "custom_commands" in self.capabilities and not self.commands:
            raise ValueError("Custom commands requires at least one named command")
        if "bash_commands" in self.capabilities and not self.allowed_bash_programs:
            raise ValueError("Bash commands requires at least one allowed program")
        if "artifact_io" in self.capabilities and not self.artifact_files:
            raise ValueError("Artifact IO requires at least one named artifact file")
        compiled_names = set()
        if "repo_read" in self.capabilities:
            compiled_names.update(("Read", "Grep", "Glob"))
        if "file_edit" in self.capabilities:
            compiled_names.update(("Write", "Edit"))
        if "bash_commands" in self.capabilities:
            compiled_names.add("Bash")
        if "run_tests" in self.capabilities:
            compiled_names.add("run_tests")
        if "git_read" in self.capabilities:
            compiled_names.update(("git_status", "git_diff", "git_log"))
        if "git_write" in self.capabilities:
            compiled_names.add("GitCommit")
        if "build_test" in self.capabilities:
            compiled_names.add("BuildTest")
        if "gitlab_mr" in self.capabilities:
            compiled_names.update(("GitLabMrCreate", "GitLabMrNote"))
        if "mcp_tools" in self.capabilities:
            compiled_names.update(
                "mcp__harness__remote_" + hashlib.sha256(f"{server.name}/{tool}".encode()).hexdigest()[:12]
                for server in self.mcp_servers for tool in server.selected_tools
            )
        if "artifact_io" in self.capabilities:
            if any(a.mode in ("read", "both") for a in self.artifact_files):
                compiled_names.add("ReadArtifact")
            if any(a.mode in ("write", "both") for a in self.artifact_files):
                compiled_names.add("WriteArtifact")
        if "custom_commands" in self.capabilities and compiled_names & {command.name for command in self.commands}:
            raise ValueError("Command names cannot collide with other compiled tool names")
        directories = [command.working_directory for command in self.commands]
        directories.extend(a.path.rsplit("/", 1)[0] if "/" in a.path else a.path for a in self.artifact_files)
        if "run_tests" in self.capabilities and self.test_command:
            directories.append(self.test_command.working_directory)
        if "build_test" in self.capabilities and self.build_test_steps:
            directories.extend(
                step.working_directory
                for step in (self.build_test_steps.format, self.build_test_steps.build, self.build_test_steps.test)
                if step is not None
            )
        if {"git_read", "git_write", "gitlab_mr"} & set(self.capabilities):
            if not self.git_directory:
                raise ValueError("Git capabilities require a working directory")
            directories.append(target_path(self.git_directory))
        for directory in directories:
            if not any(directory == m.target or directory.startswith(m.target + "/") for m in self.mounts):
                raise ValueError("Command working directories and artifact file paths must be inside an explicit mount")
        if "git_write" in self.capabilities and not any(not m.read_only and (self.git_directory == m.target or self.git_directory.startswith(m.target + "/")) for m in self.mounts):
            raise ValueError("Git commits require a writable Git working directory")
        for artifact in self.artifact_files:
            if artifact.mode in ("write", "both") and not any(
                not m.read_only and (artifact.path == m.target or artifact.path.startswith(m.target + "/"))
                for m in self.mounts
            ):
                raise ValueError(f"Artifact file '{artifact.name}' is writable but its path is not inside a writable mount")
        serialized = self.model_dump_json()
        if len(serialized.encode()) > 500000 or "\\u0000" in serialized:
            raise ValueError("Manifest exceeds 500 KB or contains a null character")
        if SECRET.search(serialized):
            raise ValueError("Credentials cannot be included in a manifest; declare environment-variable names instead")
        return self
