"""Native tools and fixed-command dispatch with manifest policy enforcement."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .errors import PolicyViolation, ToolExecutionError


MAX_FILE_BYTES = 1_000_000
MAX_TOOL_OUTPUT = 1_000_000
MAX_FILE_BYTES_CEILING = 10_000_000
MAX_TOOL_OUTPUT_CEILING = 10_000_000
DEFAULT_COMMAND_TIMEOUT = 120
COMMAND_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "PATHEXT")
GIT_PREFIX = ["git", "--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


@dataclass(frozen=True)
class Mount:
    target: str
    root: Path
    read_only: bool


class PathPolicy:
    def __init__(self, mounts: list[dict]):
        resolved_mounts = []
        for item in mounts:
            try:
                root = Path(item["hostPath"]).resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ToolExecutionError(f"Declared mount does not exist: {item['target']}") from error
            if not root.is_dir():
                raise ToolExecutionError(f"Declared mount is not a directory: {item['target']}")
            parts = root.parts
            if root == Path(root.anchor) or (len(parts) == 3 and parts[1] in {"Users", "home"}):
                raise ToolExecutionError(f"Declared mount resolves to a filesystem or home root: {item['target']}")
            if any(part in {".ssh", ".aws", ".gnupg", ".kube", ".docker"} or part.startswith(".env") for part in parts):
                raise ToolExecutionError(f"Declared mount resolves to a credential path: {item['target']}")
            resolved_mounts.append(Mount(item["target"], root, item["readOnly"]))
        self.mounts = sorted(resolved_mounts, key=lambda mount: (-len(PurePosixPath(mount.target).parts), mount.target))

    @staticmethod
    def _parts(alias_path: str) -> tuple[str, ...]:
        if not isinstance(alias_path, str) or not alias_path or "\0" in alias_path or "\\" in alias_path:
            raise PolicyViolation("Path must be a nonempty mount-alias path")
        if alias_path.startswith("/"):
            raise PolicyViolation("Absolute paths and path traversal are not allowed")
        # A single trailing slash (e.g. a directory path for Glob/Grep) is a
        # normal way to name a directory, not traversal — only strip one so
        # "a//" still trips the empty-segment check below like "a//b" does.
        if alias_path.endswith("/") and alias_path != "/":
            alias_path = alias_path[:-1]
        if any(part in {"", ".", ".."} for part in alias_path.split("/")):
            raise PolicyViolation("Absolute paths and path traversal are not allowed")
        return tuple(alias_path.split("/"))

    def mount_for(self, alias_path: str) -> tuple[Mount, tuple[str, ...]]:
        parts = self._parts(alias_path)
        for mount in self.mounts:
            mount_parts = PurePosixPath(mount.target).parts
            if parts[:len(mount_parts)] == mount_parts:
                return mount, parts[len(mount_parts):]
        # Models trained on cwd-relative tool conventions routinely omit the mount
        # alias for the primary project mount (e.g. "src/app.py" instead of
        # "repo/src/app.py"). Only the mount named "repo" absorbs this fallback —
        # explicit aliases for every other declared mount still resolve exactly.
        for mount in self.mounts:
            if mount.target == "repo":
                return mount, parts
        raise PolicyViolation(f"Path is outside every declared mount: {alias_path}")

    def resolve(self, alias_path: str, *, write: bool = False) -> tuple[Path, Mount]:
        mount, relative = self.mount_for(alias_path)
        if write and mount.read_only:
            raise PolicyViolation(f"Write denied by read-only mount: {mount.target}")
        candidate = mount.root.joinpath(*relative)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as error:
            raise PolicyViolation(f"Path cannot be safely resolved inside mount {mount.target}") from error
        try:
            resolved.relative_to(mount.root)
        except ValueError as error:
            raise PolicyViolation(f"Resolved path escapes mount {mount.target}") from error
        return resolved, mount

    def alias(self, path: Path, mount: Mount) -> str:
        try:
            relative = path.relative_to(mount.root)
        except ValueError as error:
            raise PolicyViolation(f"Path is outside mount {mount.target}") from error
        return mount.target if not relative.parts else f"{mount.target}/{relative.as_posix()}"


class ToolDispatcher:
    def __init__(self, config: dict, mcp_runtime: Any = None):
        self.config = config
        self.max_file_bytes = min(config.get("maxFileBytes", MAX_FILE_BYTES), MAX_FILE_BYTES_CEILING)
        self.max_tool_output = min(config.get("maxToolOutput", MAX_TOOL_OUTPUT), MAX_TOOL_OUTPUT_CEILING)
        self.default_command_timeout = config.get("defaultCommandTimeoutSeconds", DEFAULT_COMMAND_TIMEOUT)
        self.path_policy = PathPolicy(config["mounts"])
        self.tools = {tool["name"]: tool for tool in config["tools"]}
        self.artifact_files = {a["name"]: a for a in config.get("artifactFiles", [])}
        self.mcp_runtime = mcp_runtime
        for tool in config["tools"]:
            try:
                Draft202012Validator.check_schema(tool["inputSchema"])
            except SchemaError as error:
                raise ValueError(f"Invalid compiled schema for {tool['name']}") from error

    @staticmethod
    def _validate(tool: dict, arguments: dict) -> None:
        if not isinstance(arguments, dict):
            raise PolicyViolation("Tool arguments must be a JSON object")
        try:
            Draft202012Validator(tool["inputSchema"]).validate(arguments)
        except ValidationError as error:
            location = ".".join(map(str, error.absolute_path))
            detail = f"{location}: {error.message}" if location else error.message
            raise PolicyViolation(f"Arguments rejected for {tool['name']}: {detail}") from error

    async def execute(self, name: str, arguments: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            raise PolicyViolation(f"Tool is not registered by the manifest: {name}")
        self._validate(tool, arguments)
        kind = tool["kind"]
        if kind == "action":
            return await self._action(tool, arguments)
        if kind == "mcp":
            if self.mcp_runtime is None:
                raise ToolExecutionError("MCP runtime is unavailable")
            return await self.mcp_runtime.call(tool, arguments)
        if name == "Bash":
            return await self._bash(arguments)
        if name == "GitCommit":
            return await self._gitcommit(arguments)
        if name == "BuildTest":
            return await self._buildtest(arguments)
        if name == "GitLabMrCreate":
            return await self._gitlabmrcreate(arguments)
        if name == "GitLabMrNote":
            return await self._gitlabmrnote(arguments)
        handler = getattr(self, f"_{name.lower()}", None)
        if handler is None:
            raise PolicyViolation(f"No native implementation is registered for {name}")
        return handler(arguments)

    def _read(self, arguments: dict) -> str:
        path, _mount = self.path_policy.resolve(arguments["path"])
        try:
            if not path.is_file():
                raise ToolExecutionError(f"Not a file: {arguments['path']}")
            data = path.read_bytes()
            if len(data) > self.max_file_bytes:
                raise ToolExecutionError(f"File exceeds {self.max_file_bytes} bytes")
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as error:
            raise ToolExecutionError("Read supports UTF-8 text files only") from error
        except OSError as error:
            raise ToolExecutionError(f"Read failed ({type(error).__name__})") from error
        offset = arguments.get("offset", 1)
        limit = arguments.get("limit", 2000)
        selected = lines[offset - 1:offset - 1 + limit]
        return "\n".join(f"{number}: {line}" for number, line in enumerate(selected, offset))

    @staticmethod
    def _glob_pattern(pattern: str) -> str:
        if "\0" in pattern or "\\" in pattern or pattern.startswith("/") or any(part == ".." for part in pattern.split("/")):
            raise PolicyViolation("Glob pattern cannot be absolute or contain traversal")
        return pattern

    def _glob(self, arguments: dict) -> str:
        pattern = self._glob_pattern(arguments["pattern"])
        search_mounts: list[tuple[Mount, Path]] = []
        if arguments.get("path"):
            base, mount = self.path_policy.resolve(arguments["path"])
            search_mounts.append((mount, base))
        else:
            search_mounts.extend((mount, mount.root) for mount in self.path_policy.mounts)
        results: set[str] = set()
        try:
            for mount, base in search_mounts:
                if not base.is_dir():
                    raise ToolExecutionError(f"Not a directory: {self.path_policy.alias(base, mount)}")
                for path in base.glob(pattern):
                    alias = self.path_policy.alias(path, mount)
                    resolved, selected = self.path_policy.resolve(alias)
                    if resolved.exists() and selected == mount:
                        results.add(alias + ("/" if resolved.is_dir() else ""))
                        if len(results) >= 1000:
                            break
        except OSError as error:
            raise ToolExecutionError(f"Glob failed ({type(error).__name__})") from error
        return "\n".join(sorted(results))

    def _grep(self, arguments: dict) -> str:
        needle = arguments["pattern"]
        path, mount = self.path_policy.resolve(arguments["path"])
        if not path.exists():
            raise ToolExecutionError(f"Path does not exist: {arguments['path']}")
        pattern = self._glob_pattern(arguments.get("glob", "**/*"))
        candidates = [path] if path.is_file() else path.glob(pattern)
        matches: list[str] = []
        inspected = 0
        output_size = 0
        try:
            for candidate in candidates:
                if inspected >= 10000 or len(matches) >= 500:
                    break
                if not candidate.is_file():
                    continue
                alias = self.path_policy.alias(candidate, mount)
                resolved, selected = self.path_policy.resolve(alias)
                if selected != mount:
                    continue
                inspected += 1
                data = resolved.read_bytes()
                if len(data) > self.max_file_bytes or b"\0" in data:
                    continue
                try:
                    lines = data.decode("utf-8").splitlines()
                except UnicodeDecodeError:
                    continue
                for number, line in enumerate(lines, 1):
                    if needle in line:
                        match = f"{alias}:{number}:{line}"
                        encoded_size = len(match.encode("utf-8")) + 1
                        if output_size + encoded_size > self.max_tool_output:
                            matches.append("[output truncated]")
                            return "\n".join(matches)
                        matches.append(match)
                        output_size += encoded_size
                        if len(matches) >= 500:
                            break
        except OSError as error:
            raise ToolExecutionError(f"Grep failed ({type(error).__name__})") from error
        return "\n".join(matches)

    def _write(self, arguments: dict) -> str:
        byte_length = len(arguments["content"].encode("utf-8"))
        if byte_length > self.max_file_bytes:
            raise ToolExecutionError(f"Content exceeds {self.max_file_bytes} bytes")
        path, _mount = self.path_policy.resolve(arguments["path"], write=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Re-check after creating parents so a pre-existing symlink cannot redirect the write.
            checked, _ = self.path_policy.resolve(arguments["path"], write=True)
            checked.write_text(arguments["content"], encoding="utf-8", newline="\n")
        except OSError as error:
            raise ToolExecutionError(f"Write failed ({type(error).__name__})") from error
        return f"Wrote {byte_length} bytes to {arguments['path']}"

    def _edit(self, arguments: dict) -> str:
        path, _mount = self.path_policy.resolve(arguments["path"], write=True)
        try:
            data = path.read_bytes()
            if len(data) > self.max_file_bytes:
                raise ToolExecutionError(f"File exceeds {self.max_file_bytes} bytes")
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolExecutionError("Edit supports UTF-8 text files only") from error
        except OSError as error:
            raise ToolExecutionError(f"Edit failed ({type(error).__name__})") from error
        count = text.count(arguments["old_text"])
        if count == 0:
            raise ToolExecutionError("old_text was not found")
        if count > 1 and not arguments.get("replace_all", False):
            raise ToolExecutionError("old_text is not unique; set replace_all to edit every match")
        updated = text.replace(arguments["old_text"], arguments["new_text"], -1 if arguments.get("replace_all") else 1)
        if len(updated.encode("utf-8")) > self.max_file_bytes:
            raise ToolExecutionError(f"Edited file would exceed {self.max_file_bytes} bytes")
        try:
            checked, _ = self.path_policy.resolve(arguments["path"], write=True)
            checked.write_text(updated, encoding="utf-8", newline="")
        except OSError as error:
            raise ToolExecutionError(f"Edit failed ({type(error).__name__})") from error
        return f"Replaced {count if arguments.get('replace_all') else 1} occurrence(s) in {arguments['path']}"

    def _readartifact(self, arguments: dict) -> str:
        # arguments["name"] is schema-validated against the compiled enum before
        # dispatch reaches here, so it is always a key the manifest declared.
        spec = self.artifact_files[arguments["name"]]
        return self._read({"path": spec["path"]})

    def _writeartifact(self, arguments: dict) -> str:
        spec = self.artifact_files[arguments["name"]]
        schema = spec.get("writeSchema")
        if schema is not None:
            try:
                parsed = json.loads(arguments["content"])
            except json.JSONDecodeError as error:
                raise ToolExecutionError(f"Content for '{arguments['name']}' is not valid JSON: {error.msg}") from error
            try:
                Draft202012Validator(schema).validate(parsed)
            except ValidationError as error:
                location = ".".join(map(str, error.absolute_path))
                detail = f"{location}: {error.message}" if location else error.message
                raise ToolExecutionError(f"Content for '{arguments['name']}' does not match its declared schema: {detail}") from error
        return self._write({"path": spec["path"], "content": arguments["content"]})

    async def _bash(self, arguments: dict) -> str:
        command = arguments["command"]
        for marker, description in (
            ("$(", "command substitution ($(...))"),
            ("`", "backtick command substitution"),
            ("\n", "a literal newline"),
            ("<(", "process substitution (<(...))"),
            (">(", "process substitution (>(...))"),
        ):
            if marker in command:
                raise PolicyViolation(f"Bash command is not allowed: {description}")
        allowlist = set(self.config.get("bashAllowlist", []))
        operators = {"&&", "||", ";", "|"}
        try:
            lexer = shlex.shlex(command, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError as error:
            raise PolicyViolation("Bash command has unbalanced quoting") from error
        segment: list[str] = []
        for token in tokens + [";"]:
            if token in operators:
                if not segment:
                    raise PolicyViolation("Bash command contains a segment with no leading program")
                if segment[0] not in allowlist:
                    raise PolicyViolation(f"Bash leading program is not allowed: {segment[0]}")
                segment = []
            else:
                segment.append(token)
        if not self.config["mounts"]:
            raise ToolExecutionError("Bash requires at least one declared mount for its working directory")
        cwd = self.config["mounts"][0]["target"]
        return await self._run_command(["bash", "-c", command], cwd, self.default_command_timeout)

    async def _gitcommit(self, arguments: dict) -> str:
        git_dir = self.config.get("gitDirectory")
        if not git_dir:
            raise ToolExecutionError("No git directory is configured")
        git_root, git_mount = self.path_policy.resolve(git_dir)
        files = arguments.get("files")
        if files:
            relative_files = []
            for file_path in files:
                resolved, mount = self.path_policy.resolve(file_path)
                if mount.root != git_mount.root:
                    raise PolicyViolation(f"File is outside the git directory: {file_path}")
                try:
                    relative_files.append(str(resolved.relative_to(git_root)))
                except ValueError as error:
                    raise PolicyViolation(f"File is outside the git directory: {file_path}") from error
            add_argv = [*GIT_PREFIX, "add", "--", *relative_files]
        else:
            add_argv = [*GIT_PREFIX, "add", "-A"]
        # Reserve room for the '"step": "..."' key each result below is wrapped in,
        # so the final wrapped JSON stays within the tool-output budget too, not just the
        # bare {"exit_code", "output"} shape _run_command bounds on its own.
        step_reserve = len('{"step": "commit"}')
        add_result = json.loads(await self._run_command(add_argv, git_dir, self.default_command_timeout, reserve=step_reserve))
        if add_result["exit_code"] != 0:
            return json.dumps({"step": "add", **add_result})
        commit_argv = [*GIT_PREFIX, "-c", "commit.gpgSign=false", "commit", "-m", arguments["commit_message"]]
        commit_result = json.loads(await self._run_command(commit_argv, git_dir, self.default_command_timeout, reserve=step_reserve))
        if commit_result["exit_code"] != 0 or not arguments.get("push"):
            return json.dumps({"step": "commit", **commit_result})
        push_result = json.loads(await self._run_command(
            [*GIT_PREFIX, "push", "-u", "origin", "HEAD"], git_dir, self.default_command_timeout, reserve=step_reserve))
        return json.dumps({"step": "push", **push_result})

    async def _buildtest(self, arguments: dict) -> str:
        steps = self.config.get("buildTestSteps")
        if not steps:
            raise ToolExecutionError("No build/format/test commands are configured")
        step_reserve = len('{"step": "format"}')
        last_result = None
        for step_name in ("format", "build", "test"):
            step = steps.get(step_name)
            if not step:
                continue
            result = json.loads(await self._run_command(
                step["argv"], step["workingDirectory"], step["timeoutSeconds"], reserve=step_reserve))
            last_result = (step_name, result)
            if result["exit_code"] != 0:
                break
        if last_result is None:
            raise ToolExecutionError("No build/format/test commands are configured")
        step_name, result = last_result
        return json.dumps({"step": step_name, **result})

    async def _gitlabmrcreate(self, arguments: dict) -> str:
        git_dir = self.config.get("gitDirectory")
        if not git_dir:
            raise ToolExecutionError("No git directory is configured")
        argv = [
            "glab", "mr", "create", "--title", arguments["title"],
            "--description", arguments["description"],
        ]
        if arguments.get("target_branch"):
            argv.extend(["--target-branch", arguments["target_branch"]])
        return await self._run_command(argv, git_dir, self.default_command_timeout)

    async def _gitlabmrnote(self, arguments: dict) -> str:
        git_dir = self.config.get("gitDirectory")
        if not git_dir:
            raise ToolExecutionError("No git directory is configured")
        argv = ["glab", "mr", "note", str(arguments["mr_iid"]), "--message", arguments["message"]]
        return await self._run_command(argv, git_dir, self.default_command_timeout)

    async def _action(self, tool: dict, arguments: dict) -> str:
        argv = list(tool["argv"])
        if arg_input := tool.get("argInput"):
            argv.extend([arg_input["flag"], arguments[arg_input["name"]]])
        return await self._run_command(argv, tool["cwd"], tool["timeoutSeconds"])

    async def _run_command(
        self, argv: list[str], cwd_alias: str, timeout_seconds: int, *,
        reserve: int = 0,
    ) -> str:
        cwd, _mount = self.path_policy.resolve(cwd_alias)
        if not cwd.is_dir():
            raise ToolExecutionError(f"Configured working directory does not exist: {cwd_alias}")
        env = {key: os.environ[key] for key in COMMAND_ENV_KEYS if key in os.environ}
        for name in self.config.get("additionalEnvVars", []):
            if name in os.environ:
                env[name] = os.environ[name]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=os.name == "posix",
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except TimeoutError:
                await self._kill(process)
                raise ToolExecutionError(f"Command timed out after {timeout_seconds} seconds")
            except asyncio.CancelledError:
                await self._kill(process)
                raise
            except BaseException:
                await self._kill(process)
                raise
        except FileNotFoundError as error:
            raise ToolExecutionError(f"Configured executable was not found: {argv[0]}") from error
        except OSError as error:
            raise ToolExecutionError(f"Command failed to start ({type(error).__name__})") from error
        combined = stdout + ((b"\n" if stdout and stderr else b"") + stderr)
        output = combined.decode("utf-8", errors="replace")
        budget = self.max_tool_output - reserve
        result = json.dumps({"exit_code": process.returncode, "output": output}, ensure_ascii=False)
        if len(result.encode("utf-8")) <= budget:
            return result
        marker = "\n[output truncated]"
        low, high, best = 0, len(output), ""
        while low <= high:
            middle = (low + high) // 2
            candidate = json.dumps({"exit_code": process.returncode, "output": output[:middle] + marker}, ensure_ascii=False)
            if len(candidate.encode("utf-8")) <= budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        return best

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
