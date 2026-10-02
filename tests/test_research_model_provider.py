from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from config import Settings, get_settings
from research.model_provider import (
    ModelCancelled, ModelProviderError, extract_text, model_capabilities, model_identity,
)
from research.ingest import _stop_process


def _client(tmp_path: Path, body: str) -> Settings:
    executable = tmp_path / "fake-claude"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return Settings(_env_file=None, RESEARCH_MODEL_PROVIDER="claude_cli",
                    RESEARCH_CLAUDE_EXECUTABLE=str(executable))


def test_local_client_is_one_turn_without_tools_hooks_or_prompt_arguments(tmp_path):
    settings = _client(tmp_path, """import sys, json
args=sys.argv[1:]
assert args[args.index('--tools')+1] == ''
assert args[args.index('--max-turns')+1] == '1'
assert '--safe-mode' in args and '--strict-mcp-config' in args
assert '--no-session-persistence' in args
assert 'malicious source $(touch stolen)' not in args
payload={'source':sys.stdin.read()}
print(json.dumps({'result':json.dumps(payload),'num_turns':1,'is_error':False}))
""")
    source = "malicious source $(touch stolen)"
    assert json.loads(extract_text(source, settings, system="extract only")) == {"source": source}
    assert not (tmp_path / "stolen").exists()


@pytest.mark.parametrize("payload", [
    {"is_error": True, "result": "secret-api-token"},
    {"is_error": False, "result": "secret-api-token", "num_turns": 2},
    {"result": {}},
])
def test_invalid_client_results_do_not_expose_raw_diagnostics(tmp_path, payload):
    settings = _client(tmp_path, "import json\nprint(" + repr(json.dumps(payload)) + ")\n")
    with pytest.raises(ModelProviderError) as error:
        extract_text("source", settings, system="extract only")
    assert "secret-api-token" not in str(error.value)


def test_cancellation_ends_real_client_process(tmp_path):
    pid_path = tmp_path / "child.pid"
    settings = _client(tmp_path, f"""import os, pathlib, time
pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))
time.sleep(30)
""")
    started = time.monotonic()
    cancel_requested_at = None

    def cancel_after_child_starts():
        nonlocal cancel_requested_at
        if pid_path.exists() or time.monotonic() - started >= 5:
            if cancel_requested_at is None:
                cancel_requested_at = time.monotonic()
            return True
        return False

    with pytest.raises(ModelCancelled):
        extract_text("source", settings, system="extract only", cancelled=cancel_after_child_starts)
    assert pid_path.exists(), "client did not finish initialization before cancellation"
    assert cancel_requested_at is not None
    assert time.monotonic() - cancel_requested_at < 4
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)


def _process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphaned child may briefly remain a zombie until the host reaps it.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                           text=True, check=False, timeout=2).stdout.strip()
    return bool(state) and not state.startswith("Z")


@pytest.mark.skipif(os.name == "nt", reason="POSIX session descendants")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_cleanup_kills_term_resistant_descendant_even_when_parent_already_exited(tmp_path, parent_exits):
    child_pid_path = tmp_path / "descendant.pid"
    child_script = (
        "import os,pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent_script = (
        "import pathlib,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable,'-c',{child_script!r}])\n"
        f"while not pathlib.Path({str(child_pid_path)!r}).exists(): time.sleep(.01)\n"
        + ("" if parent_exits else "time.sleep(30)\n")
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    parent = subprocess.Popen([sys.executable, "-c", parent_script], start_new_session=True)
    try:
        deadline = time.monotonic() + 4
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert child_pid_path.exists(), "descendant did not finish initialization"
        child = int(child_pid_path.read_text())
        if parent_exits:
            parent.wait(timeout=3)
        assert _process_is_running(child)
        _stop_process(parent)
        deadline = time.monotonic() + 3
        while _process_is_running(child) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not _process_is_running(child)
        assert parent.poll() is not None
        assert unrelated.poll() is None
    finally:
        # This test owns both sessions, including descendants on failure.
        for owned in (parent, unrelated):
            try:
                os.killpg(owned.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            owned.wait(timeout=3)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group isolation")
def test_cleanup_never_signals_inherited_process_group(monkeypatch):
    from research import ingest
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setattr(ingest.os, "killpg", lambda *args: pytest.fail("inherited group must not be signalled"))
    try:
        _stop_process(process)
        assert process.poll() is not None
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)


def test_provider_identity_separates_same_named_model_cache():
    api = Settings(_env_file=None)
    local = Settings(_env_file=None, RESEARCH_MODEL_PROVIDER="claude_cli")
    assert model_identity(api) != model_identity(local)


def test_missing_operator_executable_does_not_fall_back_to_other_provider(tmp_path):
    settings = Settings(_env_file=None, RESEARCH_MODEL_PROVIDER="claude_cli",
                        RESEARCH_CLAUDE_EXECUTABLE=str(tmp_path / "missing"))
    assert model_capabilities(settings)["model_configured"] is False
    with pytest.raises(ModelProviderError, match="model_client_unavailable"):
        extract_text("source", settings, system="extract only")


def test_installed_env_can_persist_transcriber_browser_and_provider(tmp_path):
    config = tmp_path / ".env"
    argv = '["/usr/bin/transcriber","{input}","{output_dir}"]'
    config.write_text("RESEARCH_MODEL_PROVIDER=claude_cli\nALPHA_GUARD_BROWSER_ENABLED=true\n"
                      f"ALPHA_GUARD_TRANSCRIPTION_COMMAND='{argv}'\n")
    settings = get_settings(config)
    assert settings.research_model_provider == "claude_cli"
    assert settings.research_browser_enabled is True
    assert settings.transcription_command == argv


@pytest.mark.parametrize("tool_event", [False, True])
def test_codex_isolated_output_rejects_tool_events(tmp_path, tool_event):
    script = tmp_path / "fake-codex"
    kind = "command_execution" if tool_event else "agent_message"
    script.write_text(f"""#!{sys.executable}
import sys, json, pathlib
args=sys.argv[1:]
assert '--ignore-user-config' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1]=='read-only'
for name in ('shell_tool','apps','hooks','plugins','code_mode_host',
             'browser_use','browser_use_external','browser_use_full_cdp_access','computer_use',
             'goals','in_app_local_automation','multi_agent_v2','memories','code_mode','code_mode_only'):
 assert args[args.index(name)-1]=='--disable'
assert 'secret-source' not in args
assert 'secret-source' in sys.stdin.read()
pathlib.Path(args[args.index('-o')+1]).write_text('{{"ok":true}}')
print(json.dumps({{'type':'item.completed','item':{{'type':{kind!r}}}}}))
print(json.dumps({{'type':'turn.completed'}}))
""")
    script.chmod(0o700)
    settings = Settings(_env_file=None, RESEARCH_MODEL_PROVIDER="codex_cli",
                        RESEARCH_CODEX_EXECUTABLE=str(script))
    if tool_event:
        with pytest.raises(ModelProviderError, match="unexpected_model_tool_call"):
            extract_text("secret-source", settings, system="extract only")
    else:
        assert json.loads(extract_text("secret-source", settings, system="extract only")) == {"ok": True}
