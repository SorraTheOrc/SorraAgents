"""Tests for scripts/speak.sh -- TTS audio generation and playback.

Validates all acceptance criteria from SA-0MRHSXIT00089DQ3:
- AC1: Script exists and is executable
- AC2: Rolling buffer of 5 files in .pi/speak/
- AC3: Playback fallback (pw-play -> aplay)
- AC4: TTS API endpoint and timeout
- AC5: Usage on missing argument
- AC6: Oldest file removed before rotation
- AC7: Documentation updated (test verifies documentation exists)
- AC8: Full test suite passes (meta -- covered by running all tests)

These tests use mock executables for curl, pw-play, and aplay to avoid
external dependencies.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "skill" / "speak" / "scripts" / "speak.sh"
_SYSTEM_PATH = os.environ.get("PATH", "")


def _make_mock_curl(bindir):
    """Create a mock curl that logs invocations and writes a dummy response."""
    script = bindir / "curl"
    script.write_text(
        "#!/bin/bash\n"
        '# mock curl: logs args, writes dummy wav, outputs HTTP 200\n'
        'echo "CURL:$*" >> "$MOCK_LOG"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        "    -o) shift\n"
        '        echo "mock-wav-content" > "$1"\n'
        "        ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        '# simulate curl -w "%{http_code}"\n'
        'echo "200"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


def _make_mock_pw_play(bindir):
    """Create a mock pw-play that logs invocations."""
    script = bindir / "pw-play"
    script.write_text(
        "#!/bin/bash\n"
        'echo "PW_PLAY:$*" >> "$MOCK_LOG"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


def _make_mock_aplay(bindir):
    """Create a mock aplay that logs invocations."""
    script = bindir / "aplay"
    script.write_text(
        "#!/bin/bash\n"
        'echo "APLAY:$*" >> "$MOCK_LOG"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script


def _env_with_bindir(bindir, speak_dir, mock_log):
    """Create an environment dict with the given bin dir on PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{_SYSTEM_PATH}"
    env["MOCK_LOG"] = str(mock_log)
    env["SPEAK_DIR"] = str(speak_dir)
    return env


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_env(tmp_path):
    """Create a temporary environment with mock curl, pw-play, and aplay."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _make_mock_curl(bindir)
    _make_mock_pw_play(bindir)
    _make_mock_aplay(bindir)

    speak_dir = tmp_path / ".pi" / "speak"
    mock_log = tmp_path / "mock.log"
    return _env_with_bindir(bindir, speak_dir, mock_log)


# ===================================================================
# AC1: Script existence and executability
# ===================================================================


class TestScriptExists:
    """AC1: A bash script scripts/speak.sh exists and is executable."""

    def test_script_exists(self):
        """AC1: Script file exists at scripts/speak.sh."""
        assert SCRIPT_PATH.exists(), f"scripts/speak.sh not found at {SCRIPT_PATH}"

    def test_script_is_executable(self):
        """AC1: Script is executable (chmod +x)."""
        st = os.stat(SCRIPT_PATH)
        assert bool(st.st_mode & stat.S_IXUSR), (
            "scripts/speak.sh is not executable (chmod +x)"
        )

    def test_script_is_bash(self):
        """AC1: Script has a valid bash shebang."""
        first_line = SCRIPT_PATH.read_text().splitlines()[0]
        assert first_line.startswith("#!/bin/bash") or first_line.startswith(
            "#!/usr/bin/env bash"
        ), f"Script should have a bash shebang, got: {first_line}"


# ===================================================================
# AC5: Missing argument handling
# ===================================================================


class TestMissingArgument:
    """AC5: If no text argument is provided, print usage and exit non-zero."""

    def test_no_args_exits_nonzero(self, mock_env):
        """AC5: Running without arguments exits non-zero."""
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode != 0, (
            f"Script should exit non-zero when no text provided, "
            f"got exit code {result.returncode}"
        )

    def test_no_args_shows_usage(self, mock_env):
        """AC5: Running without arguments prints usage instructions."""
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            capture_output=True, text=True, env=mock_env,
        )
        output = result.stdout + result.stderr
        assert "Usage" in output or "usage" in output, (
            f"Script should print usage when no arguments given, "
            f"got: {output[:200]}"
        )

    def test_help_flag_exits_zero(self, mock_env):
        """--help shows usage and exits 0."""
        result = subprocess.run(
            ["bash", str(SCRIPT_PATH), "--help"],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode == 0, (
            f"--help should exit 0, got {result.returncode}"
        )
        assert "Usage" in result.stdout, (
            "--help should print usage information"
        )


# ===================================================================
# AC4: API endpoint configuration
# ===================================================================


class TestApiEndpoint:
    """AC4: Script uses the correct TTS API endpoint with OpenAI-compatible format."""

    def _run_and_get_curl_log(self, mock_env, tmp_path):
        """Helper: run speak and return curl invocation log."""
        log_file = tmp_path / "mock.log"
        log_file.write_text("")
        subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=mock_env,
        )
        return log_file.read_text() if log_file.exists() else ""

    def test_uses_correct_endpoint(self, mock_env, tmp_path):
        """AC4: Script calls the correct TTS API endpoint."""
        log_content = self._run_and_get_curl_log(mock_env, tmp_path)
        assert "http://100.79.231.101:8000/v1/audio/speech" in log_content or \
            "100.79.231.101" in log_content, (
            f"Expected API endpoint in curl args, got: {log_content}"
        )

    def test_curl_timeout_is_set(self, mock_env, tmp_path):
        """AC4: Curl command includes a max-time timeout."""
        log_content = self._run_and_get_curl_log(mock_env, tmp_path)
        assert "--max-time" in log_content, (
            f"Expected --max-time in curl args, got: {log_content}"
        )

    def test_uses_post_method(self, mock_env, tmp_path):
        """AC4: Curl uses POST method."""
        log_content = self._run_and_get_curl_log(mock_env, tmp_path)
        assert "-X POST" in log_content or "-XPOST" in log_content, (
            f"Expected POST method in curl args, got: {log_content}"
        )

    def test_uses_json_content_type(self, mock_env, tmp_path):
        """AC4: Curl sends Content-Type: application/json."""
        log_content = self._run_and_get_curl_log(mock_env, tmp_path)
        assert "application/json" in log_content, (
            f"Expected JSON content type in curl args, got: {log_content}"
        )


# ===================================================================
# AC2, AC6: Rolling buffer management
# ===================================================================


class TestRollingBuffer:
    """AC2, AC6: Rolling buffer of exactly 5 files, oldest removed at capacity."""

    def test_creates_speak_directory(self, mock_env, tmp_path):
        """AC2: Script creates .pi/speak/ directory if it doesn't exist."""
        subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=mock_env,
        )
        speak_dir = tmp_path / ".pi" / "speak"
        assert speak_dir.is_dir(), (
            f".pi/speak/ directory should be created, not found at {speak_dir}"
        )

    def test_generates_speech_wav(self, mock_env, tmp_path):
        """AC2: Script generates speech.wav file in .pi/speak/."""
        subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=mock_env,
        )
        speech_wav = tmp_path / ".pi" / "speak" / "speech.wav"
        assert speech_wav.exists(), (
            f"speech.wav should be created, not found at {speech_wav}"
        )

    def test_buffer_never_exceeds_5_files(self, mock_env, tmp_path):
        """AC6: Buffer never exceeds 5 files."""
        speak_dir = tmp_path / ".pi" / "speak"
        speak_dir.mkdir(parents=True, exist_ok=True)

        for i in range(5):
            fname = "speech.wav" if i == 0 else f"speech.{i}.wav"
            (speak_dir / fname).write_text("old-data")

        for _ in range(3):
            subprocess.run(
                ["bash", str(SCRIPT_PATH), "Hello world"],
                capture_output=True, text=True, env=mock_env,
            )

        wav_files = [
            f for f in speak_dir.iterdir()
            if f.suffix == ".wav" and f.name.startswith("speech")
        ]
        assert len(wav_files) <= 5, (
            f"Buffer should not exceed 5 files, found {len(wav_files)}"
        )

    def test_rolling_buffer_naming_convention(self, mock_env, tmp_path):
        """AC2, AC6: Buffer files follow naming: speech.wav, speech.1.wav, etc."""
        speak_dir = tmp_path / ".pi" / "speak"
        speak_dir.mkdir(parents=True, exist_ok=True)

        for i in range(6):
            subprocess.run(
                ["bash", str(SCRIPT_PATH), f"Message {i}"],
                capture_output=True, text=True, env=mock_env,
            )

        wav_names = {
            f.name for f in speak_dir.iterdir()
            if f.suffix == ".wav" and f.name.startswith("speech")
        }
        assert len(wav_names) <= 5, (
            f"Should have at most 5 files, got {len(wav_names)}"
        )
        assert "speech.wav" in wav_names, (
            f"speech.wav should exist after run, got: {wav_names}"
        )
        for name in wav_names:
            assert name == "speech.wav" or (
                name.startswith("speech.") and name.endswith(".wav")
            ), f"Unexpected filename: {name}"


# ===================================================================
# AC3: Playback fallback
# ===================================================================


class TestPlaybackFallback:
    """AC3: Script attempts pw-play first, falls back to aplay."""

    def test_uses_pw_play_when_available(self, mock_env, tmp_path):
        """AC3: pw-play is called when available."""
        log_file = tmp_path / "mock.log"
        log_file.write_text("")

        subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=mock_env,
        )

        log_content = log_file.read_text()
        assert "PW_PLAY" in log_content, (
            f"Expected pw-play to be called, got: {log_content}"
        )

    def test_falls_back_to_aplay(self, tmp_path):
        """AC3: Falls back to aplay when pw-play is not found."""
        bindir = tmp_path / "bin_no_pwplay"
        bindir.mkdir()

        _make_mock_aplay(bindir)
        _make_mock_curl(bindir)

        speak_dir = tmp_path / ".pi" / "speak"
        mock_log = tmp_path / "mock.log"
        mock_log.write_text("")
        env = _env_with_bindir(bindir, speak_dir, mock_log)

        subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=env,
        )

        log_content = mock_log.read_text()
        assert "APLAY" in log_content, (
            f"Expected aplay to be called as fallback, got: {log_content}"
        )

    def test_playback_failure_not_fatal(self, tmp_path):
        """AC3: Playback failure does not cause script to exit with error."""
        bindir = tmp_path / "bin_no_playback"
        bindir.mkdir()

        _make_mock_curl(bindir)
        # No pw-play or aplay mocks created

        speak_dir = tmp_path / ".pi" / "speak"
        mock_log = tmp_path / "mock.log"
        mock_log.write_text("")
        env = _env_with_bindir(bindir, speak_dir, mock_log)

        result = subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0, (
            f"Playback failure should not be fatal, "
            f"exit code: {result.returncode}, stderr: {result.stderr}"
        )

    def test_termux_playback_takes_priority(self, tmp_path):
        """Termux: termux-media-player is preferred over pw-play when available."""
        bindir = tmp_path / "bin_termux"
        bindir.mkdir()

        # Create mock termux-media-player
        tmux_script = bindir / "termux-media-player"
        tmux_script.write_text(
            "#!/bin/bash\n"
            'echo "TERMUX_MEDIA_PLAYER:$*" >> "$MOCK_LOG"\n'
            "exit 0\n"
        )
        tmux_script.chmod(0o755)

        # Also provide pw-play (should NOT be called since termux-media-player is found first)
        _make_mock_pw_play(bindir)
        _make_mock_curl(bindir)

        speak_dir = tmp_path / ".pi" / "speak"
        mock_log = tmp_path / "mock.log"
        mock_log.write_text("")
        env = _env_with_bindir(bindir, speak_dir, mock_log)

        subprocess.run(
            ["bash", str(SCRIPT_PATH), "Hello termux"],
            capture_output=True, text=True, env=env,
        )

        log_content = mock_log.read_text()
        assert "TERMUX_MEDIA_PLAYER" in log_content, (
            f"Expected termux-media-player to be called first, got: {log_content}"
        )
        # pw-play should not be called since termux-media-player succeeded
        assert "PW_PLAY" not in log_content, (
            f"pw-play should not be called when termux-media-player succeeds, "
            f"got: {log_content}"
        )


# ===================================================================
# AC7: Documentation exists
# ===================================================================


class TestDocumentation:
    """AC7: Documentation is updated."""

    def test_skill_doc_exists(self):
        """AC7: skill/speak/SKILL.md exists with substantial content."""
        skill_doc = REPO_ROOT / "skill" / "speak" / "SKILL.md"
        assert skill_doc.exists(), (
            f"skill/speak/SKILL.md documentation not found at {skill_doc}"
        )
        content = skill_doc.read_text()
        assert len(content) > 100, (
            "skill/speak/SKILL.md should contain substantial documentation"
        )


# ===================================================================
# End-to-end happy path
# ===================================================================


class TestHappyPath:
    """Full happy-path integration with mocked dependencies."""

    def test_speak_creates_wav_and_plays(self, mock_env, tmp_path):
        """With valid text: creates WAV in correct dir and attempts playback."""
        log_file = tmp_path / "mock.log"
        log_file.write_text("")

        result = subprocess.run(
            ["bash", str(SCRIPT_PATH), "Test message for TTS"],
            capture_output=True, text=True, env=mock_env,
        )

        assert result.returncode == 0, (
            f"Script should exit 0 on success, got {result.returncode}: "
            f"stderr={result.stderr}"
        )

        speak_dir = tmp_path / ".pi" / "speak"
        speech_wav = speak_dir / "speech.wav"
        assert speech_wav.exists(), (
            f"speech.wav should exist after successful run at {speech_wav}"
        )
        assert speech_wav.read_text().strip() == "mock-wav-content", (
            "speech.wav should contain mock content from mock curl"
        )

        log_content = log_file.read_text()
        assert "PW_PLAY" in log_content, (
            f"pw-play should be invoked for playback, got: {log_content}"
        )
