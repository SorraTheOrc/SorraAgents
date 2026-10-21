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

import json
import os
import shlex
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


def _make_mock_curl(bindir, http_status="200", response_body="mock-wav-content"):
    """Create a mock curl that logs invocations, writes a dummy response, and records the -d payload.

    Args:
        bindir: Directory for the mock binary.
        http_status: HTTP status string to output (default "200").
        response_body: Body content written to -o files (default "mock-wav-content").
    """
    script = bindir / "curl"
    script.write_text(
        "#!/bin/bash\n"
        '# mock curl: logs args, writes response body, outputs HTTP status\n'
        'echo "CURL:$*" >> "$MOCK_LOG"\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        "    -d) shift\n"
        '        printf \'%s\' "$1" > "$MOCK_PAYLOAD_FILE"\n'
        "        ;;\n"
        "    -o) shift\n"
        f"        printf '%s' {shlex.quote(response_body)} > \"$1\"\n"
        "        ;;\n"
        "  esac\n"
        "  shift\n"
        "done\n"
        '# simulate curl -w "%{http_code}"\n'
        f'echo {shlex.quote(http_status)}\n'
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


def _env_with_bindir(bindir, speak_dir, mock_log, payload_file=None):
    """Create an environment dict with the given bin dir on PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{_SYSTEM_PATH}"
    env["MOCK_LOG"] = str(mock_log)
    env["SPEAK_DIR"] = str(speak_dir)
    env["MOCK_PAYLOAD_FILE"] = str(
        payload_file if payload_file is not None else mock_log.parent / "payload.json"
    )
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
        assert first_line.startswith("#!/bin/bash") or first_line.startswith(  # noqa: PIE810
            "#!/usr/bin/env bash"
        ), f"Script should have a bash shebang, got: {first_line}"


# ===================================================================
# AC5: Missing argument handling
# ===================================================================


class TestMissingArgument:
    """AC5: If no text argument is provided, print usage and exit non-zero."""

    def test_no_args_exits_nonzero(self, mock_env):
        """AC5: Running without arguments exits non-zero."""
        result = subprocess.run(  # noqa: PLW1510
            ["bash", str(SCRIPT_PATH)],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode != 0, (
            f"Script should exit non-zero when no text provided, "
            f"got exit code {result.returncode}"
        )

    def test_no_args_shows_usage(self, mock_env):
        """AC5: Running without arguments prints usage instructions."""
        result = subprocess.run(  # noqa: PLW1510
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
        result = subprocess.run(  # noqa: PLW1510
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
        subprocess.run(  # noqa: PLW1510
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
        subprocess.run(  # noqa: PLW1510
            ["bash", str(SCRIPT_PATH), "Hello world"],
            capture_output=True, text=True, env=mock_env,
        )
        speak_dir = tmp_path / ".pi" / "speak"
        assert speak_dir.is_dir(), (
            f".pi/speak/ directory should be created, not found at {speak_dir}"
        )

    def test_generates_speech_wav(self, mock_env, tmp_path):
        """AC2: Script generates speech.wav file in .pi/speak/."""
        subprocess.run(  # noqa: PLW1510
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
            subprocess.run(  # noqa: PLW1510
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
            subprocess.run(  # noqa: PLW1510
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

        subprocess.run(  # noqa: PLW1510
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

        subprocess.run(  # noqa: PLW1510
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

        result = subprocess.run(  # noqa: PLW1510
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

        subprocess.run(  # noqa: PLW1510
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
# Stream mode tests
# ===================================================================


class TestStreamMode:
    """--stream flag writes raw audio to stdout, no file ops."""

    def test_stream_outputs_audio_to_stdout(self, mock_env, tmp_path):
        """--stream sends raw audio to stdout."""
        log_file = tmp_path / "mock.log"
        log_file.write_text("")

        result = subprocess.run(  # noqa: PLW1510
            ["bash", str(SCRIPT_PATH), "--stream", "Hello stream"],
            capture_output=True, text=True, env=mock_env,
        )

        assert result.returncode == 0, (
            f"--stream should exit 0, got {result.returncode}: stderr={result.stderr}"
        )
        # stdout should contain the mock audio content from mock curl
        assert "mock-wav-content" in result.stdout, (
            f"stdout should contain audio content, got: {result.stdout[:200]}"
        )

    def test_stream_does_not_create_speak_dir(self, mock_env, tmp_path):
        """--stream does not create .pi/speak/ directory."""
        _ = subprocess.run(  # noqa: PLW1510
            ["bash", str(SCRIPT_PATH), "--stream", "No file"],
            capture_output=True, text=True, env=mock_env,
        )
        speak_dir = tmp_path / ".pi" / "speak"
        assert not speak_dir.exists(), (
            "--stream should not create .pi/speak/ directory"
        )

    def test_stream_no_args_shows_error(self, mock_env):
        """--stream without text argument prints error."""
        result = subprocess.run(  # noqa: PLW1510
            ["bash", str(SCRIPT_PATH), "--stream"],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode != 0, (
            "--stream without text should exit non-zero"
        )
        output = result.stdout + result.stderr
        assert "Error" in output, (
            "--stream without text should show error"
        )

    def test_stream_still_uses_correct_endpoint(self, mock_env, tmp_path):
        """--stream still calls the correct TTS API endpoint."""
        log_file = tmp_path / "mock.log"
        log_file.write_text("")
        subprocess.run(  # noqa: PLW1510
            ["bash", str(SCRIPT_PATH), "--stream", "Check API"],
            capture_output=True, text=True, env=mock_env,
        )
        log_content = log_file.read_text()
        assert "100.79.231.101" in log_content, (
            f"Expected API endpoint in curl args, got: {log_content}"
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

        result = subprocess.run(  # noqa: PLW1510
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


# ===================================================================
# Timing wrapper (SA-0MTACU3CQ0085KVO)
# ===================================================================


class TestTimingWrapper:
    """The Python wrapper (speak.py) wraps speak.sh in timing."""

    WRAPPER_PATH = REPO_ROOT / "skill" / "speak" / "scripts" / "speak.py"

    def test_wrapper_exists(self):
        """AC2: The wrapper file exists next to speak.sh."""
        assert self.WRAPPER_PATH.exists(), (
            f"speak.py wrapper not found at {self.WRAPPER_PATH}"
        )

    def test_wrapper_emits_timing_report(self, mock_env):
        """AC3: Wrapper emits a timing report on stderr after a run."""
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(self.WRAPPER_PATH), "Test via wrapper"],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode == 0, (
            f"Wrapper should pass through exit code 0, got {result.returncode}: "
            f"stderr={result.stderr}"
        )
        timing = result.stderr
        assert "Timing Report" in timing, (
            f"Wrapper should emit a timing report on stderr, got: {timing[:300]}"
        )

    def test_wrapper_forwards_stream_clean_stdout(self, mock_env):
        """--stream keeps stdout clean and stderr gets the report."""
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(self.WRAPPER_PATH), "--stream", "Hi"],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode == 0
        assert "Timing Report" in result.stderr

    def test_wrapper_help_delegates(self, mock_env):
        """--help delegates to speak.sh usage text."""
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(self.WRAPPER_PATH), "--help"],
            capture_output=True, text=True, env=mock_env,
        )
        assert result.returncode == 0
        assert "Usage" in (result.stdout + result.stderr)

    def test_script_itself_not_modified(self):
        """AC2: speak.sh itself is NOT modified — it contains no Timer refs."""
        content = SCRIPT_PATH.read_text()
        assert "timing" not in content.lower()
        assert "hrtime" not in content.lower()


# ===================================================================
# Voice selection (TTS-0MVGRB71F006LQNX)
# ===================================================================


def _run_speak(args, env):
    """Run speak.sh with *args* and return the completed process."""
    return subprocess.run(  # noqa: PLW1510
        ["bash", str(SCRIPT_PATH), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _voice_from_payload(payload_file):
    """Read the JSON payload the mock curl recorded and return its voice field."""
    assert payload_file.exists(), "mock curl did not record a payload (missing -d?)"
    payload = json.loads(payload_file.read_text())
    assert "voice" in payload, f"payload has no voice field: {payload}"
    return payload["voice"]


@pytest.fixture
def error_env(tmp_path):
    """Environment whose mock curl returns HTTP 400 with a JSON error body."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    body = (
        '{"detail": "Voice \'bogus\' not found. '
        'Available voices: ryan, default, nova, ginny_di"}'
    )
    _make_mock_curl(bindir, http_status="400", response_body=body)
    _make_mock_pw_play(bindir)
    _make_mock_aplay(bindir)

    speak_dir = tmp_path / ".pi" / "speak"
    mock_log = tmp_path / "mock.log"
    mock_log.write_text("")
    return _env_with_bindir(bindir, speak_dir, mock_log)


class TestVoiceSelection:
    """AC1: --voice selects the server-side voice; default stays ryan."""

    def test_default_voice_is_ryan(self, mock_env, tmp_path):
        """Omitting --voice keeps the backwards-compatible ryan default."""
        result = _run_speak(["Hello"], mock_env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "ryan"

    def test_voice_flag_selects_voice(self, mock_env, tmp_path):
        """--voice nova sends voice=nova in the POST body."""
        result = _run_speak(["--voice", "nova", "Hello"], mock_env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"

    def test_voice_flag_selects_voice_in_stream_mode(self, mock_env, tmp_path):
        """--stream shares the same payload builder, so --voice applies there too."""
        result = _run_speak(["--stream", "--voice", "nova", "Hello"], mock_env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"

    def test_voice_flag_before_stream_flag(self, mock_env, tmp_path):
        """Options parse in any order: --voice before --stream works."""
        result = _run_speak(["--voice", "ginny_di", "--stream", "Hello"], mock_env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "ginny_di"


class TestVoiceEnvironmentFallback:
    """AC2: TTS_VOICE is the fallback; --voice takes precedence."""

    def test_tts_voice_env_fallback(self, mock_env, tmp_path):
        """TTS_VOICE=nova selects nova when --voice is absent."""
        env = dict(mock_env, TTS_VOICE="nova")
        result = _run_speak(["Hello"], env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"

    def test_voice_flag_overrides_env(self, mock_env, tmp_path):
        """--voice ginny_di wins over TTS_VOICE=nova."""
        env = dict(mock_env, TTS_VOICE="nova")
        result = _run_speak(["--voice", "ginny_di", "Hello"], env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "ginny_di"

    def test_default_is_ryan_without_env_or_flag(self, mock_env, tmp_path):
        """Neither TTS_VOICE nor --voice set: ryan (explicit backwards compatibility)."""
        env = dict(mock_env)
        env.pop("TTS_VOICE", None)
        result = _run_speak(["Hello"], env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "ryan"


class TestArgumentParsing:
    """AC3: options parse in any order; missing --voice value is a usage error."""

    def test_voice_first_then_text(self, mock_env, tmp_path):
        result = _run_speak(["--voice", "nova", "Hello"], mock_env)
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"

    def test_missing_voice_value_exits_nonzero(self, mock_env):
        """A trailing --voice with no value is a clear usage error."""
        result = _run_speak(["--voice"], mock_env)
        assert result.returncode != 0, (
            f"--voice with no value should exit non-zero, got {result.returncode}"
        )
        combined = result.stdout + result.stderr
        assert "Usage" in combined, f"usage should be shown, got: {combined[:300]}"
        assert "voice" in combined.lower(), (
            f"error should mention the missing voice value, got: {combined[:300]}"
        )

    def test_voice_followed_by_option_is_missing_value(self, mock_env):
        """--voice followed by another option is treated as a missing value."""
        result = _run_speak(["--voice", "--stream", "Hello"], mock_env)
        assert result.returncode != 0, (
            "--voice followed by an option should be a usage error"
        )
        combined = result.stdout + result.stderr
        assert "Usage" in combined

    def test_help_documents_voice_and_env(self, mock_env):
        """AC4: --help documents --voice and TTS_VOICE."""
        result = _run_speak(["--help"], mock_env)
        assert result.returncode == 0
        assert "--voice" in result.stdout
        assert "TTS_VOICE" in result.stdout

    def test_unknown_option_is_usage_error(self, mock_env):
        """Unrecognised options fail clearly instead of being treated as text."""
        result = _run_speak(["--bogus", "Hello"], mock_env)
        assert result.returncode != 0
        assert "Usage" in (result.stdout + result.stderr)


class TestUnknownVoiceErrorBody:
    """AC5: non-2xx responses surface the server's error body on stderr."""

    def test_save_mode_surfaces_server_error_body(self, error_env):
        result = _run_speak(["--voice", "bogus", "Hello"], error_env)
        assert result.returncode != 0
        assert "HTTP 400" in result.stderr, (
            f"status should be reported, got: {result.stderr[:300]}"
        )
        assert "Available voices:" in result.stderr, (
            f"server error body should be surfaced, got: {result.stderr[:300]}"
        )

    def test_stream_mode_surfaces_server_error_body(self, error_env):
        result = _run_speak(["--stream", "--voice", "bogus", "Hello"], error_env)
        assert result.returncode != 0
        assert "Available voices:" in result.stderr, (
            f"server error body should be surfaced in stream mode, "
            f"got: {result.stderr[:300]}"
        )

    def test_server_error_body_not_written_to_stdout(self, error_env):
        """Stream mode must not emit an error body as if it were audio."""
        result = _run_speak(["--stream", "--voice", "bogus", "Hello"], error_env)
        assert result.returncode != 0
        assert "Available voices:" not in result.stdout


class TestVoiceDocumentation:
    """AC4: SKILL.md documents --voice, TTS_VOICE, and a nova example."""

    def test_skill_md_documents_voice_flag_and_env(self):
        content = (REPO_ROOT / "skill" / "speak" / "SKILL.md").read_text()
        assert "--voice" in content
        assert "TTS_VOICE" in content

    def test_skill_md_has_nova_example(self):
        content = (REPO_ROOT / "skill" / "speak" / "SKILL.md").read_text()
        assert "--voice nova" in content, (
            "SKILL.md should include a working example selecting the nova voice"
        )


class TestTimingWrapperVoicePassthrough:
    """AC6: speak.py forwards --voice unchanged to speak.sh."""

    WRAPPER_PATH = REPO_ROOT / "skill" / "speak" / "scripts" / "speak.py"

    def test_wrapper_passes_voice_through(self, mock_env, tmp_path):
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(self.WRAPPER_PATH), "--voice", "nova", "Hello"],
            capture_output=True,
            text=True,
            env=mock_env,
        )
        assert result.returncode == 0, result.stderr
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"


# ===================================================================
# Endpoint selection (TTS-0MVH8TM11003AUZZ)
# ===================================================================

DEFAULT_ENDPOINT = "http://100.79.231.101:8000/v1/audio/speech"
LOCAL_ENDPOINT = "http://127.0.0.1:8000/v1/audio/speech"
ALT_ENDPOINT = "http://192.0.2.10:9000/v1/audio/speech"


def _curl_request_urls(mock_log):
    """Return the request URLs the mock curl was invoked with, in call order.

    The mock logs one ``CURL:<args>`` line per invocation; the request URL is
    the token immediately following the ``-X <method>`` pair.
    """
    assert mock_log.exists(), "mock curl never ran (no log file)"
    urls = []
    for line in mock_log.read_text().splitlines():
        if not line.startswith("CURL:"):
            continue
        tokens = line[len("CURL:"):].split()
        if "POST" not in tokens:
            continue
        urls.append(tokens[tokens.index("POST") + 1])
    return urls


def _endpoint_env(mock_env, **overrides):
    """Copy *mock_env* with inherited TTS_* overrides removed for determinism."""
    env = dict(mock_env)
    env.pop("TTS_API_URL", None)
    env.pop("TTS_VOICE", None)
    env.update(overrides)
    return env


class TestEndpointSelection:
    """AC1: --endpoint selects the endpoint; the default is unchanged."""

    def test_endpoint_flag_selects_url(self, mock_env, tmp_path):
        """--endpoint posts to the given URL in save-to-file mode."""
        result = _run_speak(["--endpoint", LOCAL_ENDPOINT, "Hello"], _endpoint_env(mock_env))
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]

    def test_endpoint_flag_selects_url_in_stream_mode(self, mock_env, tmp_path):
        """--stream shares the endpoint variable, so --endpoint applies there too."""
        result = _run_speak(
            ["--stream", "--endpoint", LOCAL_ENDPOINT, "Hello"],
            _endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]

    def test_default_endpoint_unchanged(self, mock_env, tmp_path):
        """Omitting both --endpoint and TTS_API_URL keeps the remote default."""
        result = _run_speak(["Hello"], _endpoint_env(mock_env))
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [DEFAULT_ENDPOINT]

    def test_endpoint_streamed_audio_still_on_stdout(self, mock_env, tmp_path):
        """Selecting an endpoint must not disturb --stream's raw-audio stdout."""
        result = _run_speak(
            ["--endpoint", LOCAL_ENDPOINT, "--stream", "Hello"],
            _endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert "mock-wav-content" in result.stdout


class TestEndpointPrecedence:
    """AC2: --endpoint > TTS_API_URL > built-in default."""

    def test_tts_api_url_env_fallback(self, mock_env, tmp_path):
        """TTS_API_URL selects the endpoint when --endpoint is absent."""
        env = _endpoint_env(mock_env, TTS_API_URL=ALT_ENDPOINT)
        result = _run_speak(["Hello"], env)
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [ALT_ENDPOINT]

    def test_endpoint_flag_overrides_env(self, mock_env, tmp_path):
        """--endpoint wins over TTS_API_URL."""
        env = _endpoint_env(mock_env, TTS_API_URL=ALT_ENDPOINT)
        result = _run_speak(["--endpoint", LOCAL_ENDPOINT, "Hello"], env)
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]

    def test_endpoint_flag_overrides_env_in_stream_mode(self, mock_env, tmp_path):
        """Flag-over-env precedence holds in stream mode too."""
        env = _endpoint_env(mock_env, TTS_API_URL=ALT_ENDPOINT)
        result = _run_speak(
            ["--stream", "--endpoint", LOCAL_ENDPOINT, "Hello"], env
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]

class TestEndpointArgumentParsing:
    """AC3: --endpoint parses in any order; bad forms are usage errors."""

    def test_endpoint_before_voice(self, mock_env, tmp_path):
        """--endpoint then --voice (both before the text) works."""
        result = _run_speak(
            ["--endpoint", LOCAL_ENDPOINT, "--voice", "nova", "Hello"],
            _endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"

    def test_voice_before_endpoint(self, mock_env, tmp_path):
        """--voice then --endpoint (both before the text) works."""
        result = _run_speak(
            ["--voice", "ginny_di", "--endpoint", ALT_ENDPOINT, "Hello"],
            _endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [ALT_ENDPOINT]
        assert _voice_from_payload(tmp_path / "payload.json") == "ginny_di"

    def test_endpoint_stream_and_voice_any_order(self, mock_env, tmp_path):
        """--stream, --endpoint and --voice parse in any order."""
        result = _run_speak(
            ["--endpoint", LOCAL_ENDPOINT, "--stream", "--voice", "nova", "Hello"],
            _endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]
        assert _voice_from_payload(tmp_path / "payload.json") == "nova"

    def test_missing_endpoint_value_exits_nonzero(self, mock_env):
        """A trailing --endpoint with no value is a clear usage error."""
        result = _run_speak(["--endpoint"], _endpoint_env(mock_env))
        assert result.returncode != 0, (
            f"--endpoint with no value should exit non-zero, got {result.returncode}"
        )
        combined = result.stdout + result.stderr
        assert "Usage" in combined, f"usage should be shown, got: {combined[:300]}"
        assert "endpoint" in combined.lower(), (
            f"error should mention the missing endpoint value, got: {combined[:300]}"
        )

    def test_endpoint_followed_by_option_is_missing_value(self, mock_env):
        """--endpoint followed by another option is a missing value."""
        result = _run_speak(["--endpoint", "--stream", "Hello"], _endpoint_env(mock_env))
        assert result.returncode != 0, (
            "--endpoint followed by an option should be a usage error"
        )
        combined = result.stdout + result.stderr
        assert "Usage" in combined
        assert "endpoint" in combined.lower()

    def test_endpoint_equals_form_rejected(self, mock_env):
        """--endpoint=<url> is rejected with space-separated guidance."""
        result = _run_speak(
            [f"--endpoint={LOCAL_ENDPOINT}", "Hello"], _endpoint_env(mock_env)
        )
        assert result.returncode != 0, (
            "--endpoint=<url> should be rejected in favour of --endpoint <url>"
        )
        combined = result.stdout + result.stderr
        assert "space-separated" in combined.lower(), (
            f"error should point at the space-separated form, got: {combined[:300]}"
        )
        assert "Usage" in combined

    def test_equals_form_does_not_reach_curl(self, mock_env, tmp_path):
        """A rejected --endpoint=<url> must not call the API."""
        _run_speak([f"--endpoint={LOCAL_ENDPOINT}", "Hello"], _endpoint_env(mock_env))
        log = tmp_path / "mock.log"
        assert not log.exists() or _curl_request_urls(log) == []


class TestEndpointHelpAndDocs:
    """AC4: --help and SKILL.md document --endpoint."""

    def test_help_documents_endpoint_flag(self, mock_env):
        result = _run_speak(["--help"], mock_env)
        assert result.returncode == 0
        assert "--endpoint" in result.stdout, (
            f"--help should document --endpoint, got: {result.stdout[:500]}"
        )

    def test_help_documents_endpoint_example_and_env(self, mock_env):
        """Help keeps documenting TTS_API_URL and gains an --endpoint example."""
        result = _run_speak(["--help"], mock_env)
        assert result.returncode == 0
        assert "TTS_API_URL" in result.stdout
        assert "--endpoint http" in result.stdout, (
            f"--help should include an --endpoint example, got: {result.stdout[:800]}"
        )

    def test_skill_md_documents_endpoint_flag_and_env(self):
        content = (REPO_ROOT / "skill" / "speak" / "SKILL.md").read_text()
        assert "--endpoint" in content
        assert "TTS_API_URL" in content

    def test_skill_md_has_local_endpoint_example(self):
        content = (REPO_ROOT / "skill" / "speak" / "SKILL.md").read_text()
        assert "--endpoint" in content and LOCAL_ENDPOINT in content, (
            "SKILL.md should show --endpoint pointing at the local server"
        )


class TestTimingWrapperEndpointPassthrough:
    """AC5: speak.py forwards --endpoint unchanged to speak.sh."""

    WRAPPER_PATH = REPO_ROOT / "skill" / "speak" / "scripts" / "speak.py"

    def test_wrapper_passes_endpoint_through(self, mock_env, tmp_path):
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(self.WRAPPER_PATH), "--endpoint", LOCAL_ENDPOINT, "Hello"],
            capture_output=True,
            text=True,
            env=_endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [LOCAL_ENDPOINT]

    def test_wrapper_keeps_default_endpoint_when_flag_absent(self, mock_env, tmp_path):
        """Wrapper passthrough must not change the default behaviour."""
        result = subprocess.run(  # noqa: PLW1510
            ["python3", str(self.WRAPPER_PATH), "Hello"],
            capture_output=True,
            text=True,
            env=_endpoint_env(mock_env),
        )
        assert result.returncode == 0, result.stderr
        assert _curl_request_urls(tmp_path / "mock.log") == [DEFAULT_ENDPOINT]
