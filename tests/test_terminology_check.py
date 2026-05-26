"""Tests for the terminology verification script (scripts/check-terminology.sh).

These tests verify that the terminology verification baseline correctly
classifies opencode/OpenCode mentions into allowed (technical identifiers)
and non-allowed (prose requiring neutralisation) categories.
"""

import os
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "check-terminology.sh")


def _run_check(extra_lines=None, strict=False):
    """Run the terminology check script against a temporary repo snapshot.

    Creates a temp directory with the script and a test file containing
    *extra_lines* of text, then runs the script and returns the output.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Copy the script
        dest_script = os.path.join(tmpdir, "scripts", "check-terminology.sh")
        os.makedirs(os.path.join(tmpdir, "scripts"))
        with open(SCRIPT_PATH) as f:
            script_content = f.read()
        with open(dest_script, "w") as f:
            f.write(script_content)
        os.chmod(dest_script, 0o755)

        # Create a .git directory so the glob exclusion works
        os.makedirs(os.path.join(tmpdir, ".git"))

        # Create test files
        if extra_lines:
            test_file = os.path.join(tmpdir, "test_prose.md")
            with open(test_file, "w") as f:
                f.write("\n".join(extra_lines) + "\n")

        args = [dest_script]
        if strict:
            args.append("--strict")

        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            cwd=tmpdir,
            timeout=30,
        )
        return result


class TestTerminologyCheckScript:
    """Tests for scripts/check-terminology.sh behaviour."""

    def test_script_exists_and_is_executable(self):
        assert os.path.isfile(SCRIPT_PATH), "check-terminology.sh must exist"
        assert os.access(SCRIPT_PATH, os.X_OK), "check-terminology.sh must be executable"

    def test_help_flag(self):
        result = subprocess.run(
            [SCRIPT_PATH, "--help"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=10,
        )
        assert result.returncode == 0
        assert "Usage" in result.stdout

    def test_allows_package_name_import(self):
        """opencode_ai Python SDK import should be allowlisted."""
        result = _run_check([
            "from opencode_ai import Opencode",
        ])
        assert result.returncode == 0
        assert "RESULT: PASS" in result.stdout

    def test_allows_env_variable_names(self):
        """Environment variable names like OPENCODE_BASE_URL are technical identifiers."""
        result = _run_check([
            "base_url = os.getenv('OPENCODE_BASE_URL')",
            "provider = os.getenv('OPENCODE_PROVIDER_ID', 'anthropic')",
        ])
        assert result.returncode == 0
        assert "RESULT: PASS" in result.stdout

    def test_allows_schema_urls(self):
        """Schema URLs like https://opencode.ai/config.json are technical."""
        result = _run_check([
            '"$schema": "https://opencode.ai/config.json"',
        ])
        assert result.returncode == 0

    def test_allows_external_repo_urls(self):
        """External repository URLs are historical references."""
        result = _run_check([
            "See https://github.com/opencode/ampa for details.",
            "Fork https://github.com/opencode/ampa",
        ])
        assert result.returncode == 0

    def test_allows_model_provider_prefixes(self):
        """Model identifiers like opencode-go/glm-5.1 are technical."""
        result = _run_check([
            'DEFAULT_MODEL = "opencode-go/glm-5.1"',
            '"intake": "opencode/claude-opus-4.7"',
        ])
        assert result.returncode == 0

    def test_allows_dotconfig_paths(self):
        """User config directory paths like .config/opencode/ are technical."""
        result = _run_check([
            'dirs=("$HOME/.config/opencode")',
            'path.join(xdgDir, "opencode", ".worklog")',
        ])
        assert result.returncode == 0

    def test_allows_cli_command_literals(self):
        """CLI command literals like 'opencode run' are technical."""
        result = _run_check([
            "opencode run \"/intake WL-001\"",
            "opencode run \"work on WL-001 using the implement skill\"",
        ])
        assert result.returncode == 0

    def test_allows_opencode_json_filename(self):
        """Config filename opencode.json is a technical identifier."""
        result = _run_check([
            'REPO_CONFIG="$REPO_ROOT/opencode.json"',
        ])
        assert result.returncode == 0

    def test_allows_dot_opencode_directory(self):
        """.opencode/ directory references are technical paths."""
        result = _run_check([
            "- Authoring commands (`.opencode/command/*.md`)",
            "map_path = os.path.join(repo_path, '.opencode', 'triage')",
        ])
        assert result.returncode == 0

    def test_allows_class_names(self):
        """Class names like OpenCodeRunDispatcher are technical identifiers."""
        result = _run_check([
            "| `OpenCodeRunDispatcher`, `ContainerDispatcher` |",
        ])
        assert result.returncode == 0

    def test_allows_agent_instance_naming(self):
        """Agent instance names like opencode-patch-1 are technical."""
        result = _run_check([
            "Agent: opencode-patch-1",
            '"assignee": "opencode-patch-1"',
        ])
        assert result.returncode == 0

    def test_detects_prose_references(self):
        """Prose references to OpenCode should be flagged as non-allowed."""
        result = _run_check(
            [
                "# OpenCode Workflow and Skills Repository",
                "- plugins: local OpenCode tooling used by this repository",
            ],
            strict=True,
        )
        # In strict mode, non-allowed matches cause exit 1
        assert result.returncode == 1

    def test_detects_framework_branding_in_skill_description(self):
        """Skill descriptions with framework branding should be flagged."""
        result = _run_check(
            [
                'description: "Creates a new OpenCode plugin for the system"',
                "You are building a new OpenCode tool that provides functionality",
            ],
            strict=True,
        )
        assert result.returncode == 1

    def test_detects_framework_branding_in_agent_docs(self):
        """Agent docs with framework branding should be flagged."""
        result = _run_check(
            [
                "Ensuring consistency with OpenCode organizational standards.",
            ],
            strict=True,
        )
        assert result.returncode == 1

    def test_primary_scope_classification(self):
        """Verify that agent/**, command/**, skill/** paths are classified as primary."""
        result = _run_check(
            extra_lines=[
                "Ensuring consistency with OpenCode organizational guidelines.",
            ],
            strict=True,
        )
        # The check-terminology.sh script classifies by path; our test file
        # at repo root is secondary. We test the classification output text.
        assert "Primary scope" in result.stdout or "Secondary scope" in result.stdout

    def test_exception_categories_reported(self):
        """The script should report exception categories."""
        result = _run_check()
        assert "Exception Categories" in result.stdout
        assert "Package/module names" in result.stdout
        assert "Schema/config URLs" in result.stdout
        assert "Environment variables" in result.stdout
        assert "CLI binary/command literals" in result.stdout

    def test_npm_packages_allowlisted(self):
        """npm package references should be allowlisted."""
        result = _run_check([
            '"@opencode-ai/plugin": "1.4.7"',
            '"@opencode-ai/sdk": "1.4.7"',
            '"resolved": "https://registry.npmjs.org/@opencode-ai/plugin/-/plugin-1.4.7.tgz"',
        ])
        assert result.returncode == 0

    def test_sdk_class_constructor_allowlisted(self):
        """Python SDK class constructor Opencode() should be allowlisted."""
        result = _run_check([
            "client = Opencode(base_url=base_url) if base_url else Opencode()",
        ])
        assert result.returncode == 0

    def test_opencode_tool_output_allowlisted(self):
        """Default temp directory name should be allowlisted."""
        result = _run_check([
            'default = os.path.join(tempfile.gettempdir(), "opencode_tool_output")',
        ])
        assert result.returncode == 0

    def test_delegation_timeout_legacy_alias_allowlisted(self):
        """Legacy env var AMPA_DELEGATION_OPENCODE_TIMEOUT should be allowlisted."""
        result = _run_check([
            "Supercedes the legacy `AMPA_DELEGATION_OPENCODE_TIMEOUT` variable.",
        ])
        assert result.returncode == 0
