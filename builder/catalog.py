"""The shared capability catalog. Adding a capability adds one contribution."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    description: str
    tools: tuple[str, ...] = ()
    configuration: str | None = None
    broad_access: bool = False


BASH_PROGRAMS = (
    "dotnet", "git", "grep", "head", "find", "echo", "tail", "glab", "cat", "ls",
    "printf", "python3", "tee", "xargs", "cd", "touch", "bash", "sed", "rm", "wc",
    "jq", "pwd", "dd", "mkdir", "cp", "base64", "install", "mv", "which", "awk",
    "sort", "rmdir",
)


CAPABILITIES = (
    Capability("repo_read", "Read repository", "Read files, search content, and find paths.", ("Read", "Grep", "Glob")),
    Capability("file_edit", "Edit files", "Create and edit files inside writable mounts.", ("Write", "Edit")),
    Capability("run_tests", "Run tests / build", "Run one configured command. The agent cannot supply arguments.", configuration="test_command"),
    Capability("build_test", "Format, build, and test", "Run configured format, build, and test commands as one call; stops at the first failing step.", configuration="build_test_steps"),
    Capability("custom_commands", "Custom fixed commands", "Run any number of configured commands with fixed arguments. The agent cannot supply arguments.", configuration="commands"),
    Capability("bash_commands", "Run shell commands", "Run Bash commands whose leading program is on the selected allowlist. The agent supplies the full command string.", configuration="allowed_bash_programs"),
    Capability("git_read", "Read Git history", "Fixed status, diff, and recent log commands.", configuration="git_directory"),
    Capability("git_write", "Commit and push changes", "Stage specific files or everything tracked, commit with an agent-supplied message, and optionally push in one call; hooks and signing are disabled.", configuration="git_directory"),
    Capability("gitlab_mr", "Create and comment on GitLab MRs", "Create a merge request or post a note on an existing one via glab, with title/description/message passed as real fields instead of shell text.", configuration="git_directory"),
    Capability("mcp_tools", "Selected MCP tools", "Expose only tools selected from an imported server snapshot.", configuration="mcp_servers"),
    Capability("artifact_io", "Read/write named artifact files", "Read and/or write a fixed set of manifest-declared files by logical name instead of path; writes can be schema-validated. Compiles to at most two tools (ReadArtifact, WriteArtifact) no matter how many files are declared.", ("ReadArtifact", "WriteArtifact"), configuration="artifact_files"),
)
BY_ID = {cap.id: cap for cap in CAPABILITIES}


def catalog() -> list[dict]:
    return [asdict(cap) for cap in CAPABILITIES]
