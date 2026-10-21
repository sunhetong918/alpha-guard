from __future__ import annotations

import json
import os
import threading

import pytest

from research import model_http
from research.model_http import model_post_json
from research.model_key_store import ModelKeyStore, ModelKeyStoreError
from research.model_profiles import (
    active_model_profile, build_profile, normalize_endpoint, profile_identity,
)
from research.model_provider import (
    ModelProviderError, extract_text, model_capabilities, model_identity,
)
from research.model_provider import test_model_connection as connection_test
from research.service import ResearchService
from research.store import ResearchStore


def _profile(**overrides):
    values = {"name": "本地测试", "protocol": "openai_compatible",
              "base_url": "https://api.example.com/v1", "model": "test-model",
              "timeout_seconds": 30, "allow_private_network": False, "enabled": True}
    values.update(overrides)
    return build_profile(values)


def test_endpoint_validation_separates_public_https_from_private_http():
    assert normalize_endpoint("https://api.example.com/v1", allow_private_network=False) == "https://api.example.com/v1"
    assert normalize_endpoint("https://api.example.com", allow_private_network=False) == "https://api.example.com"
    with pytest.raises(ValueError):
        normalize_endpoint("http://api.example.com/v1", allow_private_network=False)
    assert normalize_endpoint("http://127.0.0.1:11434/v1", allow_private_network=True) == "http://127.0.0.1:11434/v1"
    for bad in ("ftp://api.example.com", "https://api.example.com/v1?x=1",
                "https://user:pass@api.example.com", "https://api.example.com/v1#frag",
                "https://api.example.com:99999", ""):
        with pytest.raises(ValueError):
            normalize_endpoint(bad, allow_private_network=True)


def test_build_profile_assigns_ids_and_preserves_key_flags():
    first = _profile()
    assert len(first.id) == 32
    with pytest.raises(ValueError):
        build_profile({"id": first.id, "name": "x", "protocol": "openai_compatible",
                       "base_url": "https://a.example.com", "model": "m",
                       "timeout_seconds": 30})
    edited = build_profile({"name": "改名", "model": "m2",
                            "protocol": "openai_compatible",
                            "base_url": "https://api.example.com/v1",
                            "timeout_seconds": 30}, existing=first)
    assert edited.id == first.id and edited.key_configured == first.key_configured
    with pytest.raises(ValueError):
        build_profile({"name": "x", "unexpected": 1})


def test_profile_identity_separates_same_model_on_different_endpoints():
    one = profile_identity(_profile(base_url="https://a.example.com"))
    two = profile_identity(_profile(base_url="https://b.example.com"))
    assert one != two and one.startswith("profile:openai_compatible:")


class _FakeKeyring:
    def __init__(self):
        self.values = {}
        self.broken = False

    def get_password(self, service, account):
        if self.broken:
            raise RuntimeError("backend offline")
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        if self.broken:
            raise RuntimeError("backend offline")
        self.values[(service, account)] = password


def test_model_keys_prefer_keyring_and_fall_back_to_private_file(tmp_path):
    backend = _FakeKeyring()
    store = ModelKeyStore(keyring_backend=backend, base_dir=tmp_path / "keys")
    profile_id = "a" * 32
    store.save(profile_id, "sk-test-1234567890")
    assert backend.values and not (tmp_path / "keys").exists()
    assert store.load(profile_id) == "sk-test-1234567890"
    assert store.load("b" * 32) is None

    backend.broken = True
    store.save(profile_id, "sk-file-1234567890")  # a dead backend falls back to the file
    key_file = tmp_path / "keys" / f"model-profile:{profile_id}.key"
    assert os.stat(tmp_path / "keys").st_mode & 0o777 == 0o700
    assert os.stat(key_file).st_mode & 0o777 == 0o600
    fallback = ModelKeyStore(keyring_backend=None, discover_keyring=False, base_dir=tmp_path / "keys")
    assert fallback.load(profile_id) == "sk-file-1234567890"
    fallback.delete(profile_id)
    assert fallback.load(profile_id) is None
    with pytest.raises(ModelKeyStoreError):
        fallback.save(profile_id, "short")
    with pytest.raises(ModelKeyStoreError):
        fallback.save(profile_id, " has surrounding space 12345 ")


def test_store_crud_and_activation_pointer(tmp_path):
    database = tmp_path / "state.db"
    with ResearchStore(database) as store:
        first = store.save_model_profile(_profile())
        second = store.save_model_profile(_profile(name="另一个"))
        assert [item.name for item in store.list_model_profiles()] == ["另一个", first.name]
        settings = store.load_settings()
        settings["model_profile_id"] = second.id
        store.save_settings(settings)
        assert store.load_settings()["model_profile_id"] == second.id
        with pytest.raises(ValueError):
            store.save_settings({**settings, "model_profile_id": "f" * 32})
        store.delete_model_profile(first.id)  # deleting a non-active profile keeps the pointer
        assert store.load_settings()["model_profile_id"] == second.id


def _key_store_factory():
    state = {"saved": {}, "deleted": []}

    class Store:
        def save(self, profile_id, key):
            state["saved"][profile_id] = key

        def load(self, profile_id):
            return state["saved"].get(profile_id)

        def delete(self, profile_id):
            state["deleted"].append(profile_id)
            state["saved"].pop(profile_id, None)

    return Store, state


def test_service_profile_lifecycle_hides_keys(tmp_path, monkeypatch):
    store_class, state = _key_store_factory()
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", store_class)
    service = ResearchService(tmp_path / "state.db")
    saved = service.save_model_profile({"profile": {
        "name": "中转", "protocol": "openai_compatible",
        "base_url": "https://relay.example.com/v1", "model": "gpt-test",
        "timeout_seconds": 20, "enabled": True}, "api_key": "sk-live-abcdef123456"})
    assert saved["key_configured"] is True and "api_key" not in saved
    assert state["saved"][saved["id"]] == "sk-live-abcdef123456"
    listed = service.list_model_profiles()
    assert listed == [saved]
    service.activate_model_profile(saved["id"])
    snapshot = service.snapshot()
    assert snapshot["capabilities"]["model_profile_active"]["id"] == saved["id"]
    assert snapshot["capabilities"]["model_provider"] == "profile:中转"
    service.delete_model_profile(saved["id"])
    assert state["deleted"] == [saved["id"]]
    assert service.list_model_profiles() == []
    assert service.snapshot()["capabilities"]["model_profile_active"] is None


def test_identity_and_cache_isolation_for_profiles():
    from datetime import UTC, datetime

    from research.analysis import extraction_cache_key
    from research.models import evidence_from_item

    class _Settings:
        research_model_provider = "anthropic"
        anthropic_model = "claude-sonnet-4-6"
        model_profile_id = None

    moment = datetime(2026, 10, 2, tzinfo=UTC)
    evidence = evidence_from_item({
        "external_id": "x", "source_url": "https://e.example.com/a", "title": "正文标题",
        "content_text": "正文", "content_kind": "article", "completeness": "full",
    }, now=moment)
    one = model_identity(_Settings(), _profile(base_url="https://a.example.com"))
    two = model_identity(_Settings(), _profile(base_url="https://b.example.com"))
    env = model_identity(_Settings())
    assert len({one, two, env}) == 3
    assert extraction_cache_key(evidence, [], [], one) != extraction_cache_key(evidence, [], [], two)


@pytest.fixture(scope="module")
def local_endpoint():
    received = []

    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            received.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "key": self.headers.get("x-api-key"), "body": body})
            if self.path.endswith("/v1/messages"):
                payload = {"content": [{"type": "text", "text": "你好"}]}
            elif "/fail" in self.path:
                self.send_response(401)
                self.end_headers()
                return
            elif "/redirect" in self.path:
                self.send_response(302)
                self.send_header("Location", "https://elsewhere.example.com")
                self.end_headers()
                return
            elif "/bad" in self.path:
                payload = {"unexpected": True}
            else:
                payload = {"choices": [{"message": {"role": "assistant", "content": "ok-text"}}]}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", received
    server.shutdown()


def test_model_http_refuses_private_endpoint_without_explicit_allow(local_endpoint):
    endpoint, _ = local_endpoint
    with pytest.raises(model_http.ContentAccessError) as error:
        model_post_json(endpoint=endpoint, path="/chat/completions", payload={"m": 1},
                        api_key="sk-abc123456789", protocol="openai_compatible",
                        timeout_seconds=10, allow_private=False)
    assert str(error.value) == "non_public_address"


def test_model_http_posts_bounded_json_and_maps_errors(local_endpoint):
    endpoint, received = local_endpoint
    result = model_post_json(endpoint=endpoint, path="/chat/completions", payload={"model": "m"},
                             api_key="sk-abc123456789", protocol="openai_compatible",
                             timeout_seconds=10, allow_private=True)
    assert result["choices"][0]["message"]["content"] == "ok-text"
    assert received[-1]["auth"] == "Bearer sk-abc123456789"
    anthropic = model_post_json(endpoint=endpoint, path="/v1/messages", payload={"model": "m"},
                                api_key="sk-abc123456789", protocol="anthropic_compatible",
                                timeout_seconds=10, allow_private=True)
    assert anthropic["content"][0]["text"] == "你好"
    assert received[-1]["key"] == "sk-abc123456789"
    for path, code in (("/fail", "provider_rejected"), ("/redirect", "redirect_not_allowed")):
        with pytest.raises(model_http.ContentAccessError) as error:
            model_post_json(endpoint=endpoint, path=path, payload={"model": "m"},
                            api_key=None, protocol="openai_compatible",
                            timeout_seconds=10, allow_private=True)
        assert str(error.value) == code


def test_extract_text_profile_branch_and_connection_test(local_endpoint):
    endpoint, _ = local_endpoint
    from config import Settings

    settings = Settings(_env_file=None)
    profile = _profile(base_url=endpoint + "/x", allow_private_network=True)
    text = extract_text("prompt", settings, system="sys", profile=profile, api_key="sk-abc123456789")
    assert text == "ok-text"
    anthropic_profile = _profile(protocol="anthropic_compatible",
                                 base_url=endpoint + "/y", allow_private_network=True)
    assert extract_text("prompt", settings, system="sys", profile=anthropic_profile,
                        api_key="sk-abc123456789") == "你好"
    bad = _profile(base_url=endpoint + "/bad", allow_private_network=True)
    with pytest.raises(ModelProviderError) as error:
        extract_text("prompt", settings, system="sys", profile=bad, api_key=None)
    assert str(error.value) == "invalid_model_response"
    receipt = connection_test(profile, "sk-abc123456789")
    assert receipt["status"] == "success" and receipt["elapsed_seconds"] >= 0
    refusing = _profile(base_url=endpoint + "/fail", allow_private_network=True)
    assert connection_test(refusing, "sk-abc123456789")["status"] == "failed"


def test_capabilities_report_profile_state():
    from config import Settings

    settings = Settings(_env_file=None)
    profile = _profile()
    active = model_capabilities(settings, profile)
    assert active["model_provider"] == "profile:本地测试" and active["model_configured"] is True
    assert model_capabilities(settings, None)["model_provider"] == "anthropic"


def test_active_model_profile_requires_enabled_profile(tmp_path):
    profile = _profile()
    with ResearchStore(tmp_path / "state.db") as store:
        store.save_model_profile(profile)
        settings = store.load_settings()
        store.save_settings({**settings, "model_profile_id": profile.id})
        assert active_model_profile(store) is not None
        disabled = profile.model_copy(update={"enabled": False})
        store.save_model_profile(disabled)
        assert active_model_profile(store) is None
        store.save_model_profile(profile)
        with pytest.raises(ValueError):
            store.save_settings({**settings, "model_profile_id": "e" * 32})
        store.delete_model_profile(profile.id)  # a dangling pointer resolves to no profile
        assert active_model_profile(store) is None


def test_ipc_model_commands_roundtrip(tmp_path):
    from research.ipc import execute

    service = ResearchService(tmp_path / "state.db")
    with pytest.raises(ValueError):
        execute(service, {"command": "model_save", "api_key": "sk-x"})
    saved = execute(service, {"command": "model_save", "profile": {
        "name": "IPC", "protocol": "openai_compatible", "base_url": "https://api.example.com",
        "model": "m", "timeout_seconds": 15.0, "allow_private_network": False, "enabled": True}})
    identity = saved["result"]["id"]
    assert execute(service, {"command": "model_activate", "id": identity})["result"]["model_profile_id"] == identity
    assert execute(service, {"command": "model_activate", "id": ""})["result"]["model_profile_id"] is None
    assert execute(service, {"command": "model_delete", "id": identity})["result"]["deleted"] is True
    with pytest.raises(ValueError):
        execute(service, {"command": "model_delete", "id": identity})


def _service_profile(service):
    return service.save_model_profile({"profile": {
        "name": "original", "protocol": "openai_compatible",
        "base_url": "https://relay.example.com/v1", "model": "test",
        "timeout_seconds": 20, "enabled": True}, "api_key": "fixture-key-original"})


@pytest.mark.parametrize("operation", ["save", "delete", "activate"])
@pytest.mark.parametrize("owner", ["research", "digest"])
def test_model_mutations_reject_busy_lease_before_credentials_change(tmp_path, monkeypatch, operation, owner):
    from research.locking import ResearchLease

    store_class, state = _key_store_factory()
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", store_class)
    service = ResearchService(tmp_path / "state.db")
    saved = _service_profile(service)
    service.activate_model_profile(saved["id"])
    if owner == "research":
        class RunningThread:
            def is_alive(self):
                return True
        service._thread = RunningThread()
    with ResearchLease(service.path):
        with pytest.raises(ValueError, match="running|stop monitoring"):
            if operation == "save":
                service.save_model_profile({"profile": {key: value for key, value in saved.items()
                    if key != "updated_at"}, "api_key": "fixture-key-replacement"})
            elif operation == "delete":
                service.delete_model_profile(saved["id"])
            else:
                service.activate_model_profile(None)
    assert state["saved"] == {saved["id"]: "fixture-key-original"}
    assert state["deleted"] == []
    with ResearchStore(service.path) as store:
        assert store.get_model_profile(saved["id"]).name == "original"
        assert store.load_settings()["model_profile_id"] == saved["id"]
    service._thread = None
    service.delete_model_profile(saved["id"])
    assert service.list_model_profiles() == []


def test_profile_save_restores_prior_key_when_database_write_fails(tmp_path, monkeypatch):
    store_class, state = _key_store_factory()
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", store_class)
    service = ResearchService(tmp_path / "state.db")
    saved = _service_profile(service)
    def fail_save(*args, **kwargs):
        raise OSError("database unavailable")
    monkeypatch.setattr(ResearchStore, "save_model_profile", fail_save)
    with pytest.raises(OSError, match="database unavailable"):
        service.save_model_profile({"profile": {key: value for key, value in saved.items()
            if key != "updated_at"}, "api_key": "fixture-key-replacement"})
    assert state["saved"] == {saved["id"]: "fixture-key-original"}
    with ResearchStore(service.path) as store:
        assert store.get_model_profile(saved["id"]).key_updated_at == saved["key_updated_at"]


def test_profile_capacity_rejected_before_new_key_is_written(tmp_path, monkeypatch):
    store_class, state = _key_store_factory()
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", store_class)
    service = ResearchService(tmp_path / "state.db")
    with ResearchStore(service.path) as store:
        for index in range(20):
            store.save_model_profile(_profile(name=f"profile-{index}"))
    with pytest.raises(ValueError, match="at most 20"):
        _service_profile(service)
    assert state == {"saved": {}, "deleted": []}


def test_profile_delete_restores_profile_and_activation_when_key_cleanup_fails(tmp_path, monkeypatch):
    store_class, state = _key_store_factory()
    monkeypatch.setattr("research.model_key_store.ModelKeyStore", store_class)
    service = ResearchService(tmp_path / "state.db")
    saved = _service_profile(service)
    service.activate_model_profile(saved["id"])
    def fail_delete(self, identity):
        state["saved"].pop(identity, None)
        raise OSError("credential store unavailable")
    monkeypatch.setattr(store_class, "delete", fail_delete)
    with pytest.raises(OSError, match="credential store unavailable"):
        service.delete_model_profile(saved["id"])
    assert state["saved"][saved["id"]] == "fixture-key-original"
    with ResearchStore(service.path) as store:
        assert store.get_model_profile(saved["id"]) is not None
        assert store.load_settings()["model_profile_id"] == saved["id"]


@pytest.mark.parametrize("protocol,payload", [
    ("openai_compatible", {}),
    ("openai_compatible", {"error": {"message": "secret-provider-diagnostic"}}),
    ("openai_compatible", {"choices": []}),
    ("openai_compatible", {"choices": [{"message": {"content": None}}]}),
    ("openai_compatible", {"choices": [{"message": {"content": "  \n"}}]}),
    ("openai_compatible", {"choices": [{"message": {"content": 42}}]}),
    ("openai_compatible", {"choices": [{"message": {"content": "x" * 100001}}]}),
    ("openai_compatible", {"content": [{"type": "text", "text": "ok"}]}),
    ("anthropic_compatible", {}),
    ("anthropic_compatible", {"content": []}),
    ("anthropic_compatible", {"content": [{"type": "text", "text": None}]}),
    ("anthropic_compatible", {"content": [{"type": "text", "text": 42}]}),
    ("anthropic_compatible", {"content": [{"type": "text", "text": "  \n"}]}),
    ("anthropic_compatible", {"content": [{"type": "text", "text": "x" * 100001}]}),
    ("anthropic_compatible", {"content": [{"type": "tool_use", "id": "tool-1", "input": {}}]}),
    ("anthropic_compatible", {"choices": [{"message": {"content": "ok"}}]}),
])
def test_connection_and_analysis_reject_the_same_invalid_response(monkeypatch, protocol, payload):
    from config import Settings

    monkeypatch.setattr(model_http, "model_post_json", lambda **kwargs: payload)
    profile = _profile(protocol=protocol)
    receipt = connection_test(profile, "secret-test-key")
    assert receipt["status"] == "failed"
    assert receipt["detail"] == "invalid_model_response"
    assert "secret" not in json.dumps(receipt)
    with pytest.raises(ModelProviderError, match="^invalid_model_response$"):
        extract_text("material", Settings(_env_file=None), system="extract", profile=profile, api_key=None)


@pytest.mark.parametrize("protocol,payload,expected", [
    ("openai_compatible", {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}, "ok"),
    ("anthropic_compatible", {"content": [{"type": "text", "text": "o"}, {"type": "text", "text": "k"}]}, "ok"),
])
def test_connection_and_analysis_share_text_parser_without_exposing_output(monkeypatch, protocol, payload, expected):
    from config import Settings

    requests = []
    def post(**kwargs):
        requests.append(kwargs)
        return payload
    monkeypatch.setattr(model_http, "model_post_json", post)
    profile = _profile(protocol=protocol, timeout_seconds=90)
    receipt = connection_test(profile, "secret-test-key")
    assert receipt["status"] == "success"
    assert "content" not in receipt and "text" not in receipt
    assert requests[0]["payload"]["max_tokens"] == 16 and requests[0]["timeout_seconds"] == 30
    assert extract_text("material", Settings(_env_file=None), system="extract", profile=profile, api_key=None) == expected
    assert len(requests) == 2
