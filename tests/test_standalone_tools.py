import asyncio
import json
import os
import subprocess
import pytest

from builder.standalone import compile_standalone
from harness.errors import PolicyViolation, ToolExecutionError
from harness.tools import MAX_TOOL_OUTPUT, ToolDispatcher


def config(tmp_path, *, read_only=False):
    return compile_standalone({
        "capabilities": ["repo_read", "file_edit"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": read_only}],
    })


def test_native_write_edit_read_stay_inside_mount(tmp_path):
    dispatcher = ToolDispatcher(config(tmp_path))
    asyncio.run(dispatcher.execute("Write", {"path": "repo/a.txt", "content": "one\ntwo\n"}))
    asyncio.run(dispatcher.execute("Edit", {"path": "repo/a.txt", "old_text": "two", "new_text": "three"}))
    assert asyncio.run(dispatcher.execute("Read", {"path": "repo/a.txt"})) == "1: one\n2: three"
    assert asyncio.run(dispatcher.execute("Grep", {"path": "repo", "pattern": "three"})) == "repo/a.txt:2:three"
    assert asyncio.run(dispatcher.execute("Glob", {"path": "repo", "pattern": "*.txt"})) == "repo/a.txt"
    with pytest.raises(PolicyViolation, match="traversal"):
        asyncio.run(dispatcher.execute("Write", {"path": "repo/../escape", "content": "x"}))


def test_trailing_slash_on_a_directory_path_is_not_traversal(tmp_path):
    dispatcher = ToolDispatcher(config(tmp_path))
    asyncio.run(dispatcher.execute("Write", {"path": "repo/sub/a.txt", "content": "x"}))
    assert asyncio.run(dispatcher.execute("Glob", {"path": "repo/sub/", "pattern": "*.txt"})) == "repo/sub/a.txt"
    with pytest.raises(PolicyViolation, match="traversal"):
        asyncio.run(dispatcher.execute("Glob", {"path": "repo//sub", "pattern": "*.txt"}))


def test_unprefixed_path_falls_back_to_the_repo_mount(tmp_path):
    dispatcher = ToolDispatcher(config(tmp_path))
    asyncio.run(dispatcher.execute("Write", {"path": "a.txt", "content": "one\n"}))
    assert asyncio.run(dispatcher.execute("Read", {"path": "repo/a.txt"})) == "1: one"
    assert asyncio.run(dispatcher.execute("Read", {"path": "a.txt"})) == "1: one"


def test_unprefixed_path_has_no_fallback_without_a_repo_mount(tmp_path):
    dispatcher = ToolDispatcher(compile_standalone({
        "capabilities": ["repo_read", "file_edit"],
        "mounts": [{"host_path": str(tmp_path), "target": "workspace", "read_only": False}],
    }))
    with pytest.raises(PolicyViolation, match="outside every declared mount"):
        asyncio.run(dispatcher.execute("Write", {"path": "a.txt", "content": "x"}))


def test_read_only_and_symlink_escape_are_rejected(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    dispatcher = ToolDispatcher(config(tmp_path))
    with pytest.raises(PolicyViolation, match="escapes"):
        asyncio.run(dispatcher.execute("Read", {"path": "repo/link/secret"}))
    readonly_config = config(tmp_path)
    readonly_config["mounts"][0]["readOnly"] = True
    readonly = ToolDispatcher(readonly_config)
    with pytest.raises(PolicyViolation, match="read-only"):
        asyncio.run(readonly.execute("Write", {"path": "repo/new", "content": "x"}))


def test_nested_read_only_mount_overrides_writable_parent(tmp_path):
    parent, nested = tmp_path / "parent", tmp_path / "protected"
    parent.mkdir(); nested.mkdir()
    compiled = compile_standalone({
        "capabilities": ["file_edit"],
        "mounts": [
            {"host_path": str(parent), "target": "repo", "read_only": False},
            {"host_path": str(nested), "target": "repo/vendor", "read_only": True},
        ],
    })
    dispatcher = ToolDispatcher(compiled)
    with pytest.raises(PolicyViolation, match="read-only"):
        asyncio.run(dispatcher.execute("Write", {"path": "repo/vendor/a", "content": "x"}))


def test_mount_symlink_cannot_turn_a_narrow_path_into_the_filesystem_root(tmp_path):
    link = tmp_path / "root-link"
    link.symlink_to("/", target_is_directory=True)
    compiled = compile_standalone({
        "capabilities": ["repo_read"],
        "mounts": [{"host_path": str(link), "target": "repo", "read_only": True}],
    })
    with pytest.raises(ToolExecutionError, match="filesystem or home root"):
        ToolDispatcher(compiled)


def artifact_config(tmp_path, artifact_files, *, artifacts_read_only=False):
    os.makedirs(tmp_path / "artifacts", exist_ok=True)
    return compile_standalone({
        "capabilities": ["artifact_io"],
        "mounts": [{"host_path": str(tmp_path / "artifacts"), "target": "artifacts", "read_only": artifacts_read_only}],
        "artifact_files": artifact_files,
    })


def test_read_artifact_resolves_the_declared_path_by_logical_name(tmp_path):
    (tmp_path / "artifacts" / "run-context.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    (tmp_path / "artifacts" / "run-context.json").write_text('{"mr": 232}', encoding="utf-8")
    compiled = artifact_config(tmp_path, [
        {"name": "run_context", "description": "Run context.", "path": "artifacts/run-context.json", "mode": "read"},
    ])
    dispatcher = ToolDispatcher(compiled)
    assert asyncio.run(dispatcher.execute("ReadArtifact", {"name": "run_context"})) == '1: {"mr": 232}'
    with pytest.raises(PolicyViolation, match="is not one of"):
        asyncio.run(dispatcher.execute("ReadArtifact", {"name": "not_declared"}))


def test_write_artifact_resolves_the_declared_path_and_rejects_a_model_supplied_path(tmp_path):
    compiled = artifact_config(tmp_path, [
        {"name": "needs_decision", "description": "Decision.", "path": "artifacts/needs_decision.json", "mode": "write"},
    ])
    dispatcher = ToolDispatcher(compiled)
    asyncio.run(dispatcher.execute("WriteArtifact", {"name": "needs_decision", "content": "{}"}))
    assert (tmp_path / "artifacts" / "needs_decision.json").read_text(encoding="utf-8") == "{}"
    with pytest.raises(PolicyViolation, match="Additional properties"):
        asyncio.run(dispatcher.execute("WriteArtifact", {"name": "needs_decision", "content": "{}", "path": "repo/elsewhere.json"}))


def test_write_artifact_validates_content_against_its_declared_schema(tmp_path):
    compiled = artifact_config(tmp_path, [{
        "name": "test_plan", "description": "Test plan.", "path": "artifacts/test-plan.json", "mode": "write",
        "write_schema": {"type": "object", "required": ["criteria"], "properties": {"criteria": {"type": "array"}}},
    }])
    dispatcher = ToolDispatcher(compiled)
    with pytest.raises(ToolExecutionError, match="does not match its declared schema"):
        asyncio.run(dispatcher.execute("WriteArtifact", {"name": "test_plan", "content": "{}"}))
    with pytest.raises(ToolExecutionError, match="is not valid JSON"):
        asyncio.run(dispatcher.execute("WriteArtifact", {"name": "test_plan", "content": "not json"}))
    asyncio.run(dispatcher.execute("WriteArtifact", {"name": "test_plan", "content": '{"criteria": []}'}))
    assert json.loads((tmp_path / "artifacts" / "test-plan.json").read_text(encoding="utf-8")) == {"criteria": []}


def test_fixed_action_rejects_any_model_supplied_arguments(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["run_tests"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "test_command": {"argv": ["python3", "-c", "print('fixed')"], "working_directory": "repo"},
    })
    dispatcher = ToolDispatcher(compiled)
    result = json.loads(asyncio.run(dispatcher.execute("run_tests", {})))
    assert result == {"exit_code": 0, "output": "fixed\n"}
    with pytest.raises(PolicyViolation, match="Additional properties"):
        asyncio.run(dispatcher.execute("run_tests", {"argv": ["sh"]}))


def test_custom_fixed_command_executes_without_arguments_and_bounds_output(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["custom_commands"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "commands": [{
            "name": "large_report", "description": "Emit a large fixed report.",
            "argv": ["python3", "-c", f"import sys;sys.stdout.write('x'*{MAX_TOOL_OUTPUT + 100})"],
            "working_directory": "repo",
        }],
    })
    dispatcher = ToolDispatcher(compiled)
    result = asyncio.run(dispatcher.execute("large_report", {}))
    assert len(result.encode("utf-8")) <= MAX_TOOL_OUTPUT
    assert json.loads(result)["output"].endswith("[output truncated]")
    with pytest.raises(PolicyViolation, match="Additional properties"):
        asyncio.run(dispatcher.execute("large_report", {"extra": "argument"}))


def build_test_config(path, steps):
    return compile_standalone({
        "capabilities": ["build_test"],
        "mounts": [{"host_path": str(path), "target": "repo", "read_only": True}],
        "build_test_steps": steps,
    })


def command(code):
    return {"argv": ["python3", "-c", code], "working_directory": "repo"}


def test_build_test_runs_all_configured_steps_in_order(tmp_path):
    steps = {
        name: command(
            "from pathlib import Path; p=Path('order'); "
            f"p.write_text((p.read_text() if p.exists() else '') + '{name}\\n'); print('{name}')"
        )
        for name in ("format", "build", "test")
    }
    dispatcher = ToolDispatcher(build_test_config(tmp_path, steps))
    result = json.loads(asyncio.run(dispatcher.execute("BuildTest", {})))
    assert result == {"step": "test", "exit_code": 0, "output": "test\n"}
    assert (tmp_path / "order").read_text() == "format\nbuild\ntest\n"


def test_build_test_stops_after_the_first_failing_step(tmp_path):
    dispatcher = ToolDispatcher(build_test_config(tmp_path, {
        "format": command("from pathlib import Path; Path('format-ran').write_text('yes'); raise SystemExit(7)"),
        "build": command("from pathlib import Path; Path('build-ran').write_text('yes')"),
        "test": command("from pathlib import Path; Path('test-ran').write_text('yes')"),
    }))
    result = json.loads(asyncio.run(dispatcher.execute("BuildTest", {})))
    assert result == {"step": "format", "exit_code": 7, "output": ""}
    assert (tmp_path / "format-ran").exists()
    assert not (tmp_path / "build-ran").exists()
    assert not (tmp_path / "test-ran").exists()


def test_build_test_runs_the_only_configured_step(tmp_path):
    dispatcher = ToolDispatcher(build_test_config(tmp_path, {
        "test": command("print('only test')"),
    }))
    result = json.loads(asyncio.run(dispatcher.execute("BuildTest", {})))
    assert result == {"step": "test", "exit_code": 0, "output": "only test\n"}


def test_build_test_wrapped_output_is_bounded(tmp_path, monkeypatch):
    class Process:
        returncode = 0

        async def communicate(self):
            return b"x" * (MAX_TOOL_OUTPUT + 100), b""

    async def create(*_argv, **_kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    dispatcher = ToolDispatcher(build_test_config(tmp_path, {
        "test": command("print('ignored')"),
    }))
    result = asyncio.run(dispatcher.execute("BuildTest", {}))
    assert len(result.encode("utf-8")) <= MAX_TOOL_OUTPUT
    assert json.loads(result)["output"].endswith("[output truncated]")


def bash_config(tmp_path, programs, **overrides):
    return compile_standalone({
        "capabilities": ["bash_commands"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "allowed_bash_programs": programs,
        **overrides,
    })


def test_allowed_bash_command_executes_and_returns_output(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    result = json.loads(asyncio.run(dispatcher.execute("Bash", {"command": "printf 'allowed\\n'"})))
    assert result == {"exit_code": 0, "output": "allowed\n"}


def test_additional_environment_variables_are_selectively_passed_to_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_TOKEN", "custom-token-value")
    monkeypatch.setenv("UNDECLARED_TOKEN", "must-not-be-passed")
    dispatcher = ToolDispatcher(bash_config(
        tmp_path, ["python3"], additional_env_vars=["MY_CUSTOM_TOKEN"],
    ))
    command = (
        "python3 -c \"import json,os; print(json.dumps({"
        "'custom': os.environ.get('MY_CUSTOM_TOKEN'), "
        "'undeclared': os.environ.get('UNDECLARED_TOKEN')}))\""
    )

    result = json.loads(asyncio.run(dispatcher.execute("Bash", {"command": command})))

    assert result["exit_code"] == 0
    assert json.loads(result["output"]) == {"custom": "custom-token-value", "undeclared": None}


def test_disallowed_bash_program_is_rejected_without_execution(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    with pytest.raises(PolicyViolation, match="not allowed: touch"):
        asyncio.run(dispatcher.execute("Bash", {"command": "touch should-not-exist"}))
    assert not (tmp_path / "should-not-exist").exists()


def test_compound_bash_command_is_entirely_rejected_if_one_program_is_disallowed(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    command = "printf first > should-not-exist && touch also-should-not-exist"
    with pytest.raises(PolicyViolation, match="not allowed: touch"):
        asyncio.run(dispatcher.execute("Bash", {"command": command}))
    assert not (tmp_path / "should-not-exist").exists()
    assert not (tmp_path / "also-should-not-exist").exists()


def test_compound_bash_command_executes_when_all_programs_are_allowed(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf", "grep"]))
    command = "printf 'first\\nsecond\\n' | grep second"
    result = json.loads(asyncio.run(dispatcher.execute("Bash", {"command": command})))
    assert result == {"exit_code": 0, "output": "second\n"}


def test_compound_bash_command_with_and_executes_when_all_programs_are_allowed(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    command = "printf first && printf second"
    result = json.loads(asyncio.run(dispatcher.execute("Bash", {"command": command})))
    assert result == {"exit_code": 0, "output": "firstsecond"}


def test_bash_command_substitution_is_rejected_before_execution(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    command = "printf $(touch command-substitution-ran)"
    with pytest.raises(PolicyViolation, match="command substitution"):
        asyncio.run(dispatcher.execute("Bash", {"command": command}))
    assert not (tmp_path / "command-substitution-ran").exists()


def test_bash_backtick_substitution_is_rejected_before_execution(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    command = "printf `touch backtick-substitution-ran`"
    with pytest.raises(PolicyViolation, match="backtick command substitution"):
        asyncio.run(dispatcher.execute("Bash", {"command": command}))
    assert not (tmp_path / "backtick-substitution-ran").exists()


def test_bash_command_with_literal_newline_is_rejected_before_execution(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    command = "printf first\ntouch newline-command-ran"
    with pytest.raises(PolicyViolation, match="literal newline"):
        asyncio.run(dispatcher.execute("Bash", {"command": command}))
    assert not (tmp_path / "newline-command-ran").exists()


@pytest.mark.parametrize(
    "command",
    [
        "printf %s <(touch input-process-substitution-ran)",
        "printf output >(touch output-process-substitution-ran)",
    ],
)
def test_bash_process_substitution_is_rejected_before_execution(tmp_path, command):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    with pytest.raises(PolicyViolation, match="process substitution"):
        asyncio.run(dispatcher.execute("Bash", {"command": command}))
    assert not list(tmp_path.glob("*-process-substitution-ran"))


def test_bash_pipe_character_inside_quotes_is_not_treated_as_a_pipe_operator(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf", "grep"]))
    command = 'printf "spinner\\nloading\\n" | grep "spinner\\|loading"'
    result = json.loads(asyncio.run(dispatcher.execute("Bash", {"command": command})))
    assert result == {"exit_code": 0, "output": "spinner\nloading\n"}


def test_bash_command_with_unbalanced_quoting_is_rejected(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    with pytest.raises(PolicyViolation, match="unbalanced quoting"):
        asyncio.run(dispatcher.execute("Bash", {"command": 'printf "unterminated'}))


def test_bash_command_allows_literal_parentheses_without_forbidden_prefix(tmp_path):
    dispatcher = ToolDispatcher(bash_config(tmp_path, ["printf"]))
    result = json.loads(asyncio.run(dispatcher.execute("Bash", {"command": "printf '(allowed)'"})))
    assert result == {"exit_code": 0, "output": "(allowed)"}


def prepare_git_repo(path):
    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.name", "Harness Test")
    git("config", "user.email", "harness@example.invalid")
    (path / "tracked.txt").write_text("before\n")
    git("add", "tracked.txt")
    git("commit", "-m", "Initial")
    (path / "tracked.txt").write_text("after\n")
    return git


def git_write_config(path):
    return compile_standalone({
        "capabilities": ["git_write"],
        "mounts": [{"host_path": str(path), "target": "repo", "read_only": False}],
        "git_directory": "repo",
    })


def gitlab_mr_config(path):
    return compile_standalone({
        "capabilities": ["gitlab_mr"],
        "mounts": [{"host_path": str(path), "target": "repo", "read_only": True}],
        "additional_env_vars": ["GITLAB_TOKEN", "GITLAB_HOST"],
        "git_directory": "repo",
    })


def install_glab_shim(path, monkeypatch, *, print_env=False):
    shim = path / "glab"
    if print_env:
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os\n"
            "keys = ('GITLAB_TOKEN', 'GITLAB_HOST')\n"
            "print(json.dumps({key: os.environ[key] for key in keys if key in os.environ}))\n"
        )
    else:
        shim.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")


@pytest.mark.parametrize(("target_branch", "expected_tail"), [
    (None, []),
    ("release/next", ["--target-branch", "release/next"]),
])
def test_gitlab_mr_create_passes_fields_as_real_argv_elements(tmp_path, monkeypatch, target_branch, expected_tail):
    install_glab_shim(tmp_path, monkeypatch)
    dispatcher = ToolDispatcher(gitlab_mr_config(tmp_path))
    description = "Use `BuildTest`\n\n- format\n- build\n- test"
    arguments = {"title": "Add safe MR tools", "description": description}
    if target_branch:
        arguments["target_branch"] = target_branch
    result = json.loads(asyncio.run(dispatcher.execute("GitLabMrCreate", arguments)))
    argv = json.loads(result["output"])
    assert result["exit_code"] == 0
    assert argv == [
        "mr", "create", "--title", "Add safe MR tools", "--description", description, *expected_tail,
    ]


def test_gitlab_mr_note_coerces_iid_and_preserves_message_argv(tmp_path, monkeypatch):
    install_glab_shim(tmp_path, monkeypatch)
    dispatcher = ToolDispatcher(gitlab_mr_config(tmp_path))
    message = "Validated with `BuildTest`\n\n- all checks passed"
    result = json.loads(asyncio.run(dispatcher.execute("GitLabMrNote", {"mr_iid": 42, "message": message})))
    assert result["exit_code"] == 0
    assert json.loads(result["output"]) == ["mr", "note", "42", "--message", message]


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("GitLabMrCreate", {"title": "Title", "description": "Description"}),
    ("GitLabMrNote", {"mr_iid": 42, "message": "Message"}),
])
def test_gitlab_mr_tools_pass_glab_auth_environment(tmp_path, monkeypatch, tool_name, arguments):
    install_glab_shim(tmp_path, monkeypatch, print_env=True)
    monkeypatch.setenv("GITLAB_TOKEN", "test-token-value")
    monkeypatch.setenv("GITLAB_HOST", "gitlab.example.test")
    dispatcher = ToolDispatcher(gitlab_mr_config(tmp_path))
    result = json.loads(asyncio.run(dispatcher.execute(tool_name, arguments)))
    assert result["exit_code"] == 0
    assert json.loads(result["output"]) == {
        "GITLAB_TOKEN": "test-token-value",
        "GITLAB_HOST": "gitlab.example.test",
    }


@pytest.mark.parametrize(("tool_name", "arguments"), [
    ("GitLabMrCreate", {"title": "Title", "description": "Description"}),
    ("GitLabMrNote", {"mr_iid": 42, "message": "Message"}),
])
def test_gitlab_mr_tools_do_not_fabricate_glab_auth_environment(tmp_path, monkeypatch, tool_name, arguments):
    install_glab_shim(tmp_path, monkeypatch, print_env=True)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    dispatcher = ToolDispatcher(gitlab_mr_config(tmp_path))
    result = json.loads(asyncio.run(dispatcher.execute(tool_name, arguments)))
    assert result["exit_code"] == 0
    assert json.loads(result["output"]) == {}


def test_git_commit_stages_everything_by_default(tmp_path):
    git = prepare_git_repo(tmp_path)
    dispatcher = ToolDispatcher(git_write_config(tmp_path))
    message = "feat: commit safely; $(never-executed)\n\nMulti-line body."
    result = json.loads(asyncio.run(dispatcher.execute("GitCommit", {"commit_message": message})))
    assert result["step"] == "commit" and result["exit_code"] == 0
    assert git("log", "-1", "--pretty=%B").stdout.strip() == message
    assert git("status", "--short").stdout == ""


def test_git_commit_stages_only_the_given_files(tmp_path):
    git = prepare_git_repo(tmp_path)
    (tmp_path / "untracked.txt").write_text("new\n")
    dispatcher = ToolDispatcher(git_write_config(tmp_path))
    result = json.loads(asyncio.run(dispatcher.execute(
        "GitCommit", {"files": ["repo/untracked.txt"], "commit_message": "add one file"})))
    assert result["step"] == "commit" and result["exit_code"] == 0
    # tracked.txt's unstaged change from prepare_git_repo was never added.
    assert git("status", "--short").stdout == " M tracked.txt\n"
    assert git("show", "--name-only", "--pretty=", "HEAD").stdout.strip() == "untracked.txt"


def test_git_commit_stages_files_when_git_directory_is_nested_in_the_mount(tmp_path):
    # git_directory ("repo/backend") is a subdirectory of the mount ("repo"),
    # not the mount root itself -- a config explicitly allowed by the compiler
    # (see Manifest.git_directory validation). Staged file paths must be made
    # relative to the actual git directory (the `git add` cwd), not the outer
    # mount root, or `git add` gets handed a doubled/wrong pathspec.
    backend = tmp_path / "backend"
    backend.mkdir()
    git = prepare_git_repo(backend)
    (backend / "new.txt").write_text("hello\n")
    config = compile_standalone({
        "capabilities": ["git_write"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
        "git_directory": "repo/backend",
    })
    dispatcher = ToolDispatcher(config)
    result = json.loads(asyncio.run(dispatcher.execute(
        "GitCommit", {"files": ["repo/backend/new.txt"], "commit_message": "nested git dir"})))
    assert result["step"] == "commit" and result["exit_code"] == 0
    assert git("show", "--name-only", "--pretty=", "HEAD").stdout.strip() == "new.txt"


def test_git_commit_rejects_a_file_outside_a_nested_git_directory_but_inside_the_mount(tmp_path):
    # Same nested git_directory setup as above, but the requested file lives
    # in a sibling directory under the mount that is outside the actual git
    # directory -- this must be rejected, not silently resolved against the
    # mount root.
    backend = tmp_path / "backend"
    backend.mkdir()
    prepare_git_repo(backend)
    other = tmp_path / "other"
    other.mkdir()
    (other / "o.txt").write_text("z\n")
    config = compile_standalone({
        "capabilities": ["git_write"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
        "git_directory": "repo/backend",
    })
    dispatcher = ToolDispatcher(config)
    with pytest.raises(PolicyViolation, match="outside the git directory"):
        asyncio.run(dispatcher.execute(
            "GitCommit", {"files": ["repo/other/o.txt"], "commit_message": "should be rejected"}))


def test_git_commit_rejects_a_file_outside_the_git_directory(tmp_path):
    prepare_git_repo(tmp_path)
    config = compile_standalone({
        "capabilities": ["git_write", "repo_read"],
        "mounts": [
            {"host_path": str(tmp_path), "target": "repo", "read_only": False},
            {"host_path": str(tmp_path.parent), "target": "other", "read_only": True},
        ],
        "git_directory": "repo",
    })
    dispatcher = ToolDispatcher(config)
    with pytest.raises(PolicyViolation, match="outside the git directory"):
        asyncio.run(dispatcher.execute("GitCommit", {"files": ["other/x"], "commit_message": "x"}))


def test_git_commit_stops_before_committing_if_add_fails(tmp_path):
    git = prepare_git_repo(tmp_path)
    dispatcher = ToolDispatcher(git_write_config(tmp_path))
    result = json.loads(asyncio.run(dispatcher.execute(
        "GitCommit", {"files": ["repo/does-not-exist.txt"], "commit_message": "should not land"})))
    assert result["step"] == "add"
    assert result["exit_code"] != 0
    assert git("log", "--oneline").stdout.count("\n") == 1  # only the Initial commit


def test_git_commit_does_not_push_when_commit_fails(tmp_path):
    dispatcher = ToolDispatcher(git_write_config(tmp_path))
    # No git repo has been initialized at all, so `git add` fails immediately
    # and push must never be attempted.
    result = json.loads(asyncio.run(dispatcher.execute(
        "GitCommit", {"commit_message": "x", "push": True})))
    assert result["step"] == "add"
    assert result["exit_code"] != 0


@pytest.mark.parametrize("message", ["", "x" * 4097, "bad\0message"])
def test_git_commit_rejects_invalid_messages_before_execution(tmp_path, message):
    dispatcher = ToolDispatcher(git_write_config(tmp_path))
    with pytest.raises(PolicyViolation, match="commit_message"):
        asyncio.run(dispatcher.execute("GitCommit", {"commit_message": message}))


def test_git_commit_output_is_bounded(tmp_path, monkeypatch):
    captured = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"x" * (MAX_TOOL_OUTPUT + 100), b""

    async def create(*argv, **_kwargs):
        captured.append(argv)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    dispatcher = ToolDispatcher(git_write_config(tmp_path))
    result = asyncio.run(dispatcher.execute("GitCommit", {"commit_message": "safe message"}))
    assert captured[-1][-2:] == ("-m", "safe message")
    assert len(result.encode("utf-8")) <= MAX_TOOL_OUTPUT
    assert json.loads(result)["output"].endswith("[output truncated]")
