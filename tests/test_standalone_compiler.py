import hashlib

from builder.models import Manifest
from builder.standalone import compile_standalone

import pytest


def test_default_manifest_uses_a_concrete_anthropic_model_id():
    manifest = Manifest()
    assert manifest.model == "claude-sonnet-4-5-20250929"
    assert compile_standalone(manifest)["model"] == "claude-sonnet-4-5-20250929"


def test_max_output_tokens_compiles_with_default_and_custom_value():
    assert compile_standalone({})["maxOutputTokens"] == 8192
    assert compile_standalone({"max_output_tokens": 16384})["maxOutputTokens"] == 16384


@pytest.mark.parametrize("value", [0, 64001, "100", 100.0])
def test_max_output_tokens_validation(value):
    with pytest.raises(ValueError):
        Manifest.model_validate({"max_output_tokens": value})


def test_additional_environment_variables_compile():
    compiled = compile_standalone({"additional_env_vars": ["GITLAB_TOKEN", "MY_CUSTOM_TOKEN"]})
    assert compiled["additionalEnvVars"] == ["GITLAB_TOKEN", "MY_CUSTOM_TOKEN"]


@pytest.mark.parametrize("value", ["", "1TOKEN", "BAD-NAME", "header.payload.signature"])
def test_additional_environment_variable_names_are_validated(value):
    with pytest.raises(ValueError, match="environment variable names"):
        Manifest.model_validate({"additional_env_vars": [value]})


@pytest.mark.parametrize("values", [
    ["MY_TOKEN", "MY_TOKEN"],
    ["A" * 65],
    ["AKIA" + "A" * 16],
])
def test_additional_environment_variables_reject_duplicates_and_secret_shapes(values):
    with pytest.raises(ValueError, match="unique|secret values"):
        Manifest.model_validate({"additional_env_vars": values})


def test_pricing_compiles_and_defaults_to_none():
    pricing = {"input_per_million": 3, "output_per_million": 15,
               "cache_read_per_million": 0.3, "cache_write_per_million": 3.75}
    assert compile_standalone({"pricing": pricing})["pricing"] == pricing
    assert compile_standalone({})["pricing"] is None


def test_negative_pricing_is_rejected():
    with pytest.raises(ValueError):
        Manifest.model_validate({"pricing": {"cache_read_per_million": -0.1}})


def test_full_prompt_override_is_used_verbatim(tmp_path):
    override = "You are a custom agent.\n\nUse only explicitly granted access.  "
    compiled = compile_standalone({
        "display_name": "Generated title must not leak",
        "capabilities": ["repo_read"],
        "mounts": [{"host_path": str(tmp_path), "target": "secret-workspace", "read_only": True}],
        "skills": [{"name": "hidden-skill", "description": "Must not leak", "content": "Hidden instructions"}],
        "prompt": {"behavior": "Generated behavior must not leak", "override": override},
    })
    assert compiled["systemPrompt"] == override
    for generated_text in ("Generated title", "Capabilities", "secret-workspace", "hidden-skill", "Generated behavior"):
        assert generated_text not in compiled["systemPrompt"]


def test_default_prompt_generation_is_unchanged():
    expected = (
        "# Reviewer\n\n"
        "## Capabilities\n\n"
        "This agent has no registered tools.\n\n"
        "## Behavior\n\n"
        "Stop when the task is complete. Do not fabricate file contents or test results.\n"
        "Report what changed and which checks you actually ran. Say when you are uncertain.\n\n"
        "Only the manifest-declared tools and instructions are available. Do not assume ambient configuration or tools.\n"
    )
    assert compile_standalone({"capabilities": [], "mounts": [], "prompt": {"override": None}})["systemPrompt"] == expected


def test_standalone_compiler_is_deterministic_and_provider_neutral(tmp_path):
    manifest = Manifest.model_validate({
        "provider": "openai-compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": None,
        "model": "qwen3-coder",
        "capabilities": ["repo_read", "file_edit", "run_tests"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
        "test_command": {"argv": ["python3", "-m", "pytest", "-q"], "working_directory": "repo"},
    })
    first = compile_standalone(manifest)
    assert first == compile_standalone(manifest)
    assert first["provider"] == {
        "kind": "openai-compatible", "baseUrl": "http://127.0.0.1:11434/v1", "region": None,
        "apiKeyEnv": None,
    }
    assert [tool["name"] for tool in first["tools"]] == ["Read", "Grep", "Glob", "Write", "Edit", "run_tests"]
    assert first["tools"][-1]["argv"] == ["python3", "-m", "pytest", "-q"]
    assert first["tools"][-1]["inputSchema"]["additionalProperties"] is False
    assert str(tmp_path) not in first["systemPrompt"]


def test_multiple_custom_commands_compile_as_named_fixed_actions(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["custom_commands"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "commands": [
            {"name": "list_mrs", "description": "List merge requests.", "argv": ["glab", "mr", "list"], "working_directory": "repo"},
            {"name": "lint", "description": "Run the project linter.", "argv": ["python3", "-m", "ruff", "check"], "working_directory": "repo", "timeout_seconds": 45},
        ],
    })
    assert [(tool["name"], tool["description"]) for tool in compiled["tools"]] == [
        ("list_mrs", "List merge requests."), ("lint", "Run the project linter."),
    ]
    assert compiled["tools"][0]["argv"] == ["glab", "mr", "list"]
    assert compiled["tools"][0]["inputSchema"] == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }
    assert compiled["tools"][1]["timeoutSeconds"] == 45


def test_custom_commands_are_absent_without_the_capability(tmp_path):
    compiled = compile_standalone({
        "capabilities": [],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "commands": [{"name": "lint", "description": "Run lint.", "argv": ["ruff", "check"]}],
    })
    assert compiled["tools"] == []


def test_custom_commands_require_configuration_and_mounted_working_directories(tmp_path):
    with pytest.raises(ValueError, match="at least one named command"):
        compile_standalone({"capabilities": ["custom_commands"]})
    with pytest.raises(ValueError, match="inside an explicit mount"):
        compile_standalone({
            "capabilities": ["custom_commands"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "commands": [{"name": "lint", "description": "Run lint.", "argv": ["ruff", "check"], "working_directory": "other"}],
        })


def test_bash_program_allowlist_validation():
    with pytest.raises(ValueError, match="unique Bash programs"):
        Manifest.model_validate({"allowed_bash_programs": ["git", "not-a-program"]})
    with pytest.raises(ValueError, match="unique Bash programs"):
        Manifest.model_validate({"allowed_bash_programs": ["git", "git"]})
    with pytest.raises(ValueError, match="at least one allowed program"):
        Manifest.model_validate({"capabilities": ["bash_commands"]})

    manifest = Manifest.model_validate({
        "capabilities": ["bash_commands"],
        "allowed_bash_programs": ["git", "grep"],
    })
    assert manifest.allowed_bash_programs == ["git", "grep"]


def test_bash_capability_compiles_native_tool_and_allowlist(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["bash_commands"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "allowed_bash_programs": ["git", "grep"],
    })
    assert [tool["name"] for tool in compiled["tools"]] == ["Bash"]
    assert compiled["tools"][0]["inputSchema"] == {
        "type": "object",
        "properties": {"command": {"type": "string", "minLength": 1, "maxLength": 4096}},
        "required": ["command"],
        "additionalProperties": False,
    }
    assert compiled["bashAllowlist"] == ["git", "grep"]


def test_custom_command_name_collisions_are_rejected(tmp_path):
    mount = [{"host_path": str(tmp_path), "target": "repo", "read_only": False}]
    with pytest.raises(ValueError, match="collide"):
        compile_standalone({
            "capabilities": ["repo_read", "custom_commands"], "mounts": mount,
            "commands": [{"name": "Read", "description": "Conflict.", "argv": ["true"]}],
        })
    with pytest.raises(ValueError, match="collide"):
        compile_standalone({
            "capabilities": ["run_tests", "custom_commands"], "mounts": mount,
            "test_command": {"argv": ["true"]},
            "commands": [{"name": "run_tests", "description": "Conflict.", "argv": ["true"]}],
        })
    with pytest.raises(ValueError, match="must each be unique"):
        compile_standalone({
            "capabilities": ["custom_commands"], "mounts": mount,
            "commands": [
                {"name": "lint", "description": "First.", "argv": ["true"]},
                {"name": "lint", "description": "Second.", "argv": ["true"]},
            ],
        })


@pytest.mark.parametrize(("capability", "name", "read_only"), [
    ("git_read", "git_status", True), ("git_read", "git_diff", True),
    ("git_read", "git_log", True), ("git_write", "GitCommit", False),
])
def test_custom_command_names_cannot_collide_with_git_actions(tmp_path, capability, name, read_only):
    with pytest.raises(ValueError, match="collide"):
        compile_standalone({
            "capabilities": [capability, "custom_commands"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": read_only}],
            "git_directory": "repo",
            "commands": [{"name": name, "description": "Conflict.", "argv": ["true"]}],
        })


def test_custom_command_names_cannot_collide_with_mcp_tools(tmp_path):
    server = {
        "name": "docs", "type": "streamable-http", "url": "https://example.com/mcp",
        "snapshot": [{"name": "read_doc", "description": "Read docs", "inputSchema": {"type": "object"}}],
        "selected_tools": ["read_doc"],
    }
    name = "mcp__harness__remote_" + hashlib.sha256(b"docs/read_doc").hexdigest()[:12]
    with pytest.raises(ValueError, match="collide"):
        compile_standalone({
            "capabilities": ["mcp_tools", "custom_commands"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "mcp_servers": [server],
            "commands": [{"name": name, "description": "Conflict.", "argv": ["true"]}],
        })


def test_bearer_token_environment_compiles_for_streamable_http_only():
    server = {
        "name": "docs", "type": "streamable-http", "url": "https://example.com/mcp",
        "bearer_token_env": "DOCS_MCP_TOKEN",
        "snapshot": [{"name": "read_doc", "description": "Read docs", "inputSchema": {"type": "object"}}],
        "selected_tools": ["read_doc"],
    }
    compiled = compile_standalone({"capabilities": ["mcp_tools"], "mcp_servers": [server]})
    assert compiled["mcpServers"] == [{
        "name": "docs", "type": "streamable-http", "url": "https://example.com/mcp",
        "bearerTokenEnv": "DOCS_MCP_TOKEN",
    }]
    with pytest.raises(ValueError, match="stdio servers cannot use bearer_token_env"):
        Manifest.model_validate({"mcp_servers": [{"name": "docs", "command": "server", "bearer_token_env": "TOKEN"}]})


@pytest.mark.parametrize("value", ["", "1TOKEN", "BAD-NAME", "TOKEN NAME"])
def test_bearer_token_environment_name_is_validated(value):
    with pytest.raises(ValueError, match="environment variable name"):
        Manifest.model_validate({
            "mcp_servers": [{"name": "docs", "type": "streamable-http", "url": "https://example.com/mcp", "bearer_token_env": value}],
        })


def test_git_write_compiles_one_native_commit_tool(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["git_write"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
        "git_directory": "repo",
    })
    assert compiled["gitDirectory"] == "repo"
    assert len(compiled["tools"]) == 1
    tool = compiled["tools"][0]
    assert tool["name"] == "GitCommit"
    assert tool["kind"] == "native"
    assert tool["inputSchema"]["required"] == ["commit_message"]
    assert set(tool["inputSchema"]["properties"]) == {"files", "commit_message", "push"}


def test_git_write_is_absent_without_the_capability(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["git_read"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "git_directory": "repo",
    })
    assert "GitCommit" not in {tool["name"] for tool in compiled["tools"]}


def test_git_write_still_requires_a_writable_git_mount(tmp_path):
    with pytest.raises(ValueError, match="writable Git working directory"):
        compile_standalone({
            "capabilities": ["git_write"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "git_directory": "repo",
        })


def test_build_test_compiles_one_empty_input_native_tool_and_step_config(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["build_test"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "build_test_steps": {
            "format": {"argv": ["ruff", "format"], "working_directory": "repo", "timeout_seconds": 30},
            "test": {"argv": ["pytest"], "working_directory": "repo", "timeout_seconds": 45},
        },
    })
    assert compiled["buildTestSteps"] == {
        "format": {"argv": ["ruff", "format"], "workingDirectory": "repo", "timeoutSeconds": 30},
        "build": None,
        "test": {"argv": ["pytest"], "workingDirectory": "repo", "timeoutSeconds": 45},
    }
    assert len(compiled["tools"]) == 1
    tool = compiled["tools"][0]
    assert tool["name"] == "BuildTest"
    assert tool["kind"] == "native"
    assert tool["inputSchema"] == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    }


@pytest.mark.parametrize("steps", [None, {}])
def test_build_test_requires_at_least_one_configured_step(steps):
    manifest = {"capabilities": ["build_test"]}
    if steps is not None:
        manifest["build_test_steps"] = steps
    with pytest.raises(ValueError, match="at least one configured step"):
        compile_standalone(manifest)


def test_build_test_requires_step_directories_inside_a_mount(tmp_path):
    with pytest.raises(ValueError, match="inside an explicit mount"):
        compile_standalone({
            "capabilities": ["build_test"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "build_test_steps": {"build": {"argv": ["dotnet", "build"], "working_directory": "other"}},
        })


def test_gitlab_mr_compiles_create_and_note_native_tools(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["gitlab_mr"],
        "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
        "git_directory": "repo",
    })
    assert compiled["gitDirectory"] == "repo"
    assert [(tool["name"], tool["kind"]) for tool in compiled["tools"]] == [
        ("GitLabMrCreate", "native"), ("GitLabMrNote", "native"),
    ]


def test_gitlab_mr_requires_a_git_directory_inside_a_mount(tmp_path):
    with pytest.raises(ValueError, match="working directory"):
        compile_standalone({"capabilities": ["gitlab_mr"]})
    with pytest.raises(ValueError, match="inside an explicit mount"):
        compile_standalone({
            "capabilities": ["gitlab_mr"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "git_directory": "other",
        })


@pytest.mark.parametrize(("capability", "name", "configuration"), [
    ("build_test", "BuildTest", {"build_test_steps": {"test": {"argv": ["true"]}}}),
    ("gitlab_mr", "GitLabMrCreate", {"git_directory": "repo"}),
    ("gitlab_mr", "GitLabMrNote", {"git_directory": "repo"}),
])
def test_custom_command_names_cannot_collide_with_new_native_tools(tmp_path, capability, name, configuration):
    with pytest.raises(ValueError, match="collide"):
        compile_standalone({
            "capabilities": [capability, "custom_commands"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "commands": [{"name": name, "description": "Conflict.", "argv": ["true"]}],
            **configuration,
        })


def test_artifact_io_compiles_to_at_most_two_tools_regardless_of_file_count(tmp_path):
    compiled = compile_standalone({
        "capabilities": ["artifact_io"],
        "mounts": [
            {"host_path": str(tmp_path), "target": "repo", "read_only": True},
            {"host_path": str(tmp_path), "target": "artifacts", "read_only": False},
        ],
        "artifact_files": [
            {"name": "run_context", "description": "Run context.", "path": "artifacts/run-context.json", "mode": "read"},
            {"name": "changed_files", "description": "Changed files.", "path": "artifacts/changed-files.txt", "mode": "read"},
            {"name": "needs_decision", "description": "Decision handoff.", "path": "artifacts/needs_decision.json", "mode": "both"},
            {"name": "test_plan", "description": "Test plan.", "path": "artifacts/test-plan.json", "mode": "write"},
        ],
    })
    names = [tool["name"] for tool in compiled["tools"]]
    assert names == ["ReadArtifact", "WriteArtifact"]
    read_tool, write_tool = compiled["tools"]
    assert read_tool["inputSchema"]["properties"]["name"]["enum"] == ["changed_files", "needs_decision", "run_context"]
    assert write_tool["inputSchema"]["properties"]["name"]["enum"] == ["needs_decision", "test_plan"]
    assert compiled["artifactFiles"] == [
        {"name": "run_context", "path": "artifacts/run-context.json", "mode": "read", "writeSchema": None},
        {"name": "changed_files", "path": "artifacts/changed-files.txt", "mode": "read", "writeSchema": None},
        {"name": "needs_decision", "path": "artifacts/needs_decision.json", "mode": "both", "writeSchema": None},
        {"name": "test_plan", "path": "artifacts/test-plan.json", "mode": "write", "writeSchema": None},
    ]


def test_artifact_io_emits_only_the_tool_matching_declared_modes(tmp_path):
    read_only_mount = [{"host_path": str(tmp_path), "target": "artifacts", "read_only": True}]
    compiled = compile_standalone({
        "capabilities": ["artifact_io"],
        "mounts": read_only_mount,
        "artifact_files": [{"name": "spec", "description": "Spec.", "path": "artifacts/spec.json", "mode": "read"}],
    })
    assert [tool["name"] for tool in compiled["tools"]] == ["ReadArtifact"]


def test_artifact_io_requires_configuration():
    with pytest.raises(ValueError, match="at least one named artifact file"):
        compile_standalone({"capabilities": ["artifact_io"]})


def test_artifact_file_names_must_be_unique(tmp_path):
    with pytest.raises(ValueError, match="must each be unique"):
        compile_standalone({
            "capabilities": ["artifact_io"],
            "mounts": [{"host_path": str(tmp_path), "target": "artifacts", "read_only": False}],
            "artifact_files": [
                {"name": "spec", "description": "First.", "path": "artifacts/a.json", "mode": "read"},
                {"name": "spec", "description": "Second.", "path": "artifacts/b.json", "mode": "read"},
            ],
        })


def test_artifact_file_path_must_be_inside_a_declared_mount(tmp_path):
    with pytest.raises(ValueError, match="inside an explicit mount"):
        compile_standalone({
            "capabilities": ["artifact_io"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": True}],
            "artifact_files": [{"name": "spec", "description": "Spec.", "path": "artifacts/spec.json", "mode": "read"}],
        })


def test_writable_artifact_file_requires_a_writable_mount(tmp_path):
    with pytest.raises(ValueError, match="is writable but its path is not inside a writable mount"):
        compile_standalone({
            "capabilities": ["artifact_io"],
            "mounts": [{"host_path": str(tmp_path), "target": "artifacts", "read_only": True}],
            "artifact_files": [{"name": "plan", "description": "Plan.", "path": "artifacts/test-plan.json", "mode": "write"}],
        })


def test_artifact_file_name_cannot_collide_with_other_compiled_tools(tmp_path):
    with pytest.raises(ValueError, match="collide"):
        compile_standalone({
            "capabilities": ["custom_commands", "artifact_io"],
            "mounts": [{"host_path": str(tmp_path), "target": "repo", "read_only": False}],
            "commands": [{"name": "ReadArtifact", "description": "Conflict.", "argv": ["true"]}],
            "artifact_files": [{"name": "spec", "description": "Spec.", "path": "repo/spec.json", "mode": "read"}],
        })


def test_write_schema_is_validated_as_json_schema_and_rejects_read_only_files(tmp_path):
    with pytest.raises(ValueError, match="Invalid write_schema"):
        compile_standalone({
            "capabilities": ["artifact_io"],
            "mounts": [{"host_path": str(tmp_path), "target": "artifacts", "read_only": False}],
            "artifact_files": [{
                "name": "plan", "description": "Plan.", "path": "artifacts/plan.json", "mode": "write",
                "write_schema": {"type": "not-a-real-type"},
            }],
        })
    with pytest.raises(ValueError, match="no effect on a read-only artifact file"):
        compile_standalone({
            "capabilities": ["artifact_io"],
            "mounts": [{"host_path": str(tmp_path), "target": "artifacts", "read_only": True}],
            "artifact_files": [{
                "name": "spec", "description": "Spec.", "path": "artifacts/spec.json", "mode": "read",
                "write_schema": {"type": "object"},
            }],
        })


def test_openai_compatible_endpoint_is_explicit_and_safe():
    with pytest.raises(ValueError, match="base_url"):
        Manifest.model_validate({"provider": "openai-compatible"})
    with pytest.raises(ValueError, match="HTTPS"):
        Manifest.model_validate({"provider": "openai-compatible", "base_url": "http://example.com/v1"})
    manifest = Manifest.model_validate({"provider": "openai-compatible", "base_url": "https://example.com/v1"})
    assert manifest.api_key_env == "OPENAI_API_KEY"


def test_bedrock_manifest_compiles_region():
    compiled = compile_standalone({
        "provider": "bedrock", "region": "us-east-1", "api_key_env": "AWS_BEARER_TOKEN_BEDROCK",
        "model": "anthropic.claude-sonnet-5",
    })
    assert compiled["provider"] == {
        "kind": "bedrock", "baseUrl": None, "region": "us-east-1",
        "apiKeyEnv": "AWS_BEARER_TOKEN_BEDROCK",
    }


@pytest.mark.parametrize("region", [None, "US-EAST-1", "us_east_1", "east"])
def test_bedrock_requires_valid_region(region):
    manifest = {"provider": "bedrock", "model": "anthropic.claude-sonnet-5"}
    if region is not None:
        manifest["region"] = region
    with pytest.raises(ValueError, match="region"):
        Manifest.model_validate(manifest)


def test_bedrock_rejects_base_url():
    with pytest.raises(ValueError, match="base_url"):
        Manifest.model_validate({
            "provider": "bedrock", "region": "us-east-1", "base_url": "https://example.com",
            "model": "anthropic.claude-sonnet-5",
        })


def test_bedrock_requires_anthropic_model_prefix():
    with pytest.raises(ValueError, match="anthropic\\."):
        Manifest.model_validate({"provider": "bedrock", "region": "us-east-1", "model": "claude-sonnet-5"})


def test_bedrock_accepts_cross_region_inference_profile_prefix():
    manifest = Manifest.model_validate({
        "provider": "bedrock", "region": "us-east-1",
        "model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    })
    assert manifest.model == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


def test_bedrock_requires_api_key_environment_name():
    with pytest.raises(ValueError, match="api_key_env"):
        Manifest.model_validate({
            "provider": "bedrock", "region": "us-east-1", "api_key_env": None,
            "model": "anthropic.claude-sonnet-5",
        })


def test_bedrock_rules_do_not_change_existing_providers():
    anthropic = Manifest.model_validate({"provider": "anthropic", "region": "not-a-region"})
    assert compile_standalone(anthropic)["provider"]["region"] is None
    manifest = Manifest.model_validate({
        "provider": "openai-compatible", "base_url": "https://example.com/v1", "region": "not-a-region",
        "model": "gpt-5",
    })
    assert compile_standalone(manifest)["provider"]["region"] is None and manifest.model == "gpt-5"
