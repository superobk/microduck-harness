"""Operator configuration, secret handling and task-pinned model revisions.

HTTP tests use the real ASGI application. Model clients are local fakes; no
provider, robot, motor bus or LAN device is contacted by these tests.
"""
import asyncio
import json
import os
import stat

import httpx
import pytest
from pydantic import ValidationError

from microduck_harness.backends import DemoWorld
from microduck_harness.config import Settings
from microduck_harness.decisions import Decision
from microduck_harness.engine import Engine
from microduck_harness.gateway import create_app
from microduck_harness.model_config import (
    ModelConfigConflict, ModelConfigError, ModelConfigStore, ModelConfigUpdate,
)


def settings_for(tmp_path, **kwargs):
    return Settings(database=str(tmp_path / "harness.sqlite3"), **kwargs)


class CountingWorld(DemoWorld):
    def __init__(self, settings):
        super().__init__(settings, realtime=False)
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        return await super().stop()


def update(store, **values):
    store.update(ModelConfigUpdate(expected_revision=store.revision, **values))


def test_persist_reload_redact_preserve_and_clear_key(tmp_path):
    settings = settings_for(tmp_path)
    store = ModelConfigStore(settings)
    secret = "operator-secret-for-local-test"
    update(store, decision_mode="glm", glm_base_url="https://models.example/v1",
           glm_model="private-model-53", glm_api_key_action="set", glm_api_key=secret)
    assert store.revision == 1
    assert store.path.name == "model-settings.json"
    assert store.credential_path.name == "model-settings.credentials.json"
    assert secret not in store.path.read_text()
    assert secret in store.credential_path.read_text()
    for path in (store.path, store.credential_path):
        assert path.stat().st_mode & 0o777 == 0o600
    assert secret not in json.dumps(store.public())
    assert store.public()["api_key_configured"] is True
    assert store.effective_settings(settings).glm_api_key == secret

    restored = ModelConfigStore(settings)
    assert restored.revision == 1 and restored.key() == secret
    update(restored, glm_model="new-exact-id", glm_api_key_action="keep")
    assert restored.key() == secret
    assert ModelConfigStore(settings).configuration.glm_model == "new-exact-id"
    with pytest.raises(ModelConfigConflict):
        restored.update(ModelConfigUpdate(expected_revision=0, glm_model="stale-editor"))
    assert restored.revision == 2

    update(restored, glm_api_key_action="clear")
    assert restored.key() == ""
    assert restored.configuration.glm_auth_mode == "none"
    assert secret not in restored.credential_path.read_text()
    assert ModelConfigStore(settings).key() == ""


def test_environment_reference_is_saved_without_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PRIVATE_GLM_KEY", "environment-secret-test")
    store = ModelConfigStore(settings_for(tmp_path))
    update(store, decision_mode="glm", glm_base_url="http://127.0.0.1:1234/v1",
           glm_auth_mode="environment", glm_api_key_env="TEST_PRIVATE_GLM_KEY")
    assert store.key() == "environment-secret-test"
    assert store.public()["ready"] is True
    assert "environment-secret-test" not in store.path.read_text()
    assert not store.credential_path.exists()
    monkeypatch.delenv("TEST_PRIVATE_GLM_KEY")
    assert store.public()["ready"] is False
    assert store.public()["api_key_configured"] is False


@pytest.mark.parametrize("url", [
    "ftp://models.example", "https://user:secret@models.example/v1",
    "https://models.example/v1?api_key=secret", "https://models.example/#secret",
    "https://models.example:bad/v1", "https://models.example:99999/v1",
    "http://", "https://bad host/v1", "https://models.example/\nsecret",
])
def test_invalid_endpoint_cannot_change_saved_revision(tmp_path, url):
    store = ModelConfigStore(settings_for(tmp_path))
    with pytest.raises(ModelConfigError):
        update(store, glm_base_url=url)
    assert store.revision == 0
    assert not store.path.exists()


@pytest.mark.parametrize("values", [
    {"glm_api_key_action": "set", "glm_api_key": ""},
    {"glm_api_key_action": "set", "glm_api_key": "********"},
    {"glm_api_key_action": "set", "glm_api_key": "••••••••"},
    {"glm_api_key_action": "set", "glm_api_key": "secret\nheader"},
    {"glm_api_key_action": "set", "glm_api_key": "secret key"},
    {"glm_api_key_action": "set", "glm_api_key": "secret-密钥"},
    {"glm_api_key_action": "keep", "glm_api_key": "not-ignored"},
    {"glm_api_key_action": "clear", "glm_api_key": "not-ignored"},
    {"glm_api_key_action": "set", "glm_api_key": "new", "glm_auth_mode": "environment"},
])
def test_invalid_key_or_ambiguous_operation_is_rejected(values):
    with pytest.raises(ValidationError):
        ModelConfigUpdate(expected_revision=0, **values)


def test_origin_change_requires_reauthorization_even_after_disabling_auth(tmp_path, monkeypatch):
    store = ModelConfigStore(settings_for(tmp_path))
    update(store, glm_base_url="https://first.example/v1", glm_api_key_action="set", glm_api_key="first-key")
    update(store, glm_base_url="https://first.example:443/compatible/v1")  # Same origin, new base path.
    assert store.key() == "first-key"
    with pytest.raises(ModelConfigError, match="new model origin"):
        update(store, glm_base_url="https://second.example/v1")
    assert store.configuration.glm_base_url == "https://first.example:443/compatible/v1"

    update(store, glm_base_url="https://second.example/v1", glm_auth_mode="none")
    assert store.key() == ""
    update(store, glm_auth_mode="stored")
    assert store.key() == ""  # A dormant old key is not sent to a different origin.
    update(store, glm_api_key_action="set", glm_api_key="second-key")
    assert store.key() == "second-key"
    monkeypatch.setenv("EXPLICIT_NEW_ORIGIN_KEY", "environment-new-key")
    update(store, glm_base_url="https://third.example/v1", glm_auth_mode="environment",
           glm_api_key_env="EXPLICIT_NEW_ORIGIN_KEY")
    assert store.key() == "environment-new-key"


def test_failed_configuration_commit_keeps_prior_credential_resolvable(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    store = ModelConfigStore(settings)
    update(store, glm_base_url="https://models.example/v1", glm_api_key_action="set", glm_api_key="old-key")
    saved = store.path.read_bytes()
    original_replace = os.replace

    def fail_only_config(source, target):
        if target == store.path:
            raise OSError("simulated disk error")
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_only_config)
    with pytest.raises(ModelConfigError, match="atomically"):
        update(store, glm_model="not-committed", glm_api_key_action="set", glm_api_key="new-key")
    assert store.path.read_bytes() == saved
    assert store.revision == 1 and store.key() == "old-key"
    restored = ModelConfigStore(settings)
    assert restored.key() == "old-key"
    assert not list(tmp_path.glob(".model-settings-*"))


@pytest.mark.parametrize("initially_available", [True, False])
def test_environment_reference_cannot_transfer_origin_by_disabling_auth(tmp_path, monkeypatch, initially_available):
    if initially_available:
        monkeypatch.setenv("BOUND_ENV_KEY", "old-origin-environment-secret")
    else:
        monkeypatch.delenv("BOUND_ENV_KEY", raising=False)
    settings = settings_for(tmp_path)
    store = ModelConfigStore(settings)
    update(store, glm_base_url="https://first.example/v1", glm_auth_mode="environment", glm_api_key_env="BOUND_ENV_KEY")
    with pytest.raises(ModelConfigError, match="explicit mode and variable reference"):
        update(store, glm_base_url="https://second.example/v1")
    update(store, glm_auth_mode="none", glm_base_url="https://second.example/v1")
    store = ModelConfigStore(settings)
    monkeypatch.setenv("BOUND_ENV_KEY", "old-origin-environment-secret")
    with pytest.raises(ModelConfigError, match="explicit mode and variable reference"):
        update(store, glm_auth_mode="environment")
    assert store.key() == ""
    update(store, glm_auth_mode="environment", glm_api_key_env="BOUND_ENV_KEY")
    assert store.key() == "old-origin-environment-secret"


def test_explicit_zero_port_is_a_different_origin_from_default_port(tmp_path):
    store = ModelConfigStore(settings_for(tmp_path))
    update(store, glm_base_url="http://models.example:0/v1", glm_api_key_action="set", glm_api_key="key")
    with pytest.raises(ModelConfigError, match="new model origin"):
        update(store, glm_base_url="http://models.example/v1")


def test_directory_sync_failure_after_rename_commits_visible_revision(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    store = ModelConfigStore(settings)
    update(store, glm_base_url="https://models.example/v1", glm_api_key_action="set", glm_api_key="key-one")
    original_sync, original_replace = os.fsync, os.replace

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated directory sync failure after commit")
        return original_sync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    update(store, glm_api_key_action="set", glm_api_key="key-two")
    assert store.revision == 2 and ModelConfigStore(settings).revision == 2
    assert store.public()["persistence_warning"]
    assert store.key() == "key-two"
    assert "key-one" in store.credential_path.read_text()  # Retain the prior generation until durability is confirmed.
    monkeypatch.setattr(os, "fsync", original_sync)

    def fail_configuration_replace(source, target):
        if target == store.path:
            raise OSError("second failure before the configuration commit")
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_configuration_replace)
    with pytest.raises(ModelConfigError, match="atomically"):
        update(store, glm_api_key_action="set", glm_api_key="key-three")
    restored = ModelConfigStore(settings)
    assert restored.revision == 2 and restored.key() == "key-two"


def test_owner_only_file_permissions_are_enforced_on_reload(tmp_path):
    settings = settings_for(tmp_path)
    store = ModelConfigStore(settings)
    update(store, glm_model="saved-id")
    store.path.chmod(0o644)
    with pytest.raises(ModelConfigError, match="0600"):
        ModelConfigStore(settings)


@pytest.mark.asyncio
async def test_actual_asgi_config_auth_redaction_persistence_and_conflicts(tmp_path):
    cfg = settings_for(tmp_path, gateway_token="operator-token")
    world = CountingWorld(cfg)
    engine = Engine(cfg, backend=world)
    app = create_app(cfg, engine)
    secret = "asgi-private-key-do-not-echo"
    headers = {"Authorization": "Bearer operator-token"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
            assert (await client.get("/api/model-config")).status_code == 401
            assert (await client.patch("/api/model-config", json={"expected_revision": 0})).status_code == 401
            denied = await client.patch("/api/model-config", headers={**headers, "Origin": "https://other.test"},
                                        json={"expected_revision": 0})
            assert denied.status_code == 403
            saved = await client.patch("/api/model-config", headers=headers, json={
                "expected_revision": 0, "glm_base_url": "https://models.example/v1",
                "glm_model": "private-id", "glm_api_key_action": "set", "glm_api_key": secret,
                "glm_timeout_s": 8.5, "glm_json_mode": False,
            })
            assert saved.status_code == 200, saved.text
            assert saved.json()["saved"]["revision"] == 1
            assert saved.json()["active"]["revision"] == 1
            assert saved.json()["saved"]["api_key_configured"] is True
            assert secret not in saved.text
            conflict = await client.patch("/api/model-config", headers=headers,
                                          json={"expected_revision": 0, "glm_model": "stale"})
            assert conflict.status_code == 409
            bad = await client.patch("/api/model-config", headers=headers, json={
                "expected_revision": 1, "glm_api_key_action": "set", "glm_api_key": secret + "\n"})
            assert bad.status_code == 422
            assert secret not in bad.text and '"input"' not in bad.text
            invalid = await client.patch("/api/model-config", headers=headers,
                                         json={"expected_revision": 1, "glm_timeout_s": None})
            assert invalid.status_code == 422
            for path in ("/health", "/api/model-config", "/api/state", "/api/events"):
                response = await client.get(path, headers=headers)
                assert response.status_code == 200
                assert secret not in response.text
            health = (await client.get("/health")).json()
            assert health["model_configuration"]["active_revision"] == 1
            assert "glm_base_url" not in json.dumps(health)
            assert world.stop_calls == 0 and world.actions == []
            assert ModelConfigStore(cfg).key() == secret
            assert engine.settings.glm_timeout_s == 8.5
            assert engine.settings.glm_json_mode is False
            tool_names = {tool.name for tool in await app.state.mcp.list_tools()}
            assert not any("config" in name or "model" in name for name in tool_names)
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_default_demo_remains_readable_but_settings_mutation_needs_token(tmp_path):
    cfg = settings_for(tmp_path)
    world = CountingWorld(cfg)
    engine = Engine(cfg, backend=world)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg, engine)),
                                    base_url="http://gateway.test") as client:
            assert (await client.get("/api/model-config")).status_code == 200
            assert (await client.get("/api/model-config")).json()["writes_enabled"] is False
            response = await client.patch("/api/model-config", json={"expected_revision": 0, "glm_model": "other"})
            assert response.status_code == 403
            assert world.stop_calls == 0
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_live_without_model_api_starts_readonly_and_blocks_tasks_before_any_stop(tmp_path):
    cfg = settings_for(tmp_path, mode="live", decision_mode="hybrid", gateway_token="gateway-token",
                       executor_token="executor-token", vision_token="vision-token")
    world = CountingWorld(cfg)
    engine = Engine(cfg, backend=world)
    headers = {"Authorization": "Bearer gateway-token"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg, engine)),
                                    base_url="http://gateway.test", headers=headers) as client:
            assert (await client.get("/")).status_code == 200
            assert (await client.get("/api/model-config")).json()["saved"]["ready"] is False
            assert (await client.get("/health")).json()["model_configuration"]["ready"] is False
            response = await client.post("/api/tasks", json={
                "request_id": "blocked-without-model", "goal": {"kind": "explore", "max_steps": 1}})
            assert response.status_code == 409
            assert engine.runner is None and world.stop_calls == 0
            invalid = await client.patch("/api/model-config", json={"expected_revision": 0, "decision_mode": "demo"})
            assert invalid.status_code == 422
    finally:
        await engine.close()


@pytest.mark.asyncio
async def test_asgi_update_during_inference_pins_old_task_and_new_task_uses_saved_revision(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    instances = []

    class LocalModel:
        def __init__(self, url, *, model, api_key, timeout_s, response_format):
            self.url, self.model, self.api_key = url, model, api_key
            self.timeout_s, self.response_format = timeout_s, response_format
            self.closed = False
            self.calls = 0
            instances.append(self)

        async def choose(self, state, options):
            assert not self.closed
            self.calls += 1
            if self.model == "old-model":
                entered.set()
                await release.wait()
                assert not self.closed
            return Decision("forward", None, None, {}, 1, "glm")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr("microduck_harness.engine.GLMClient", LocalModel)
    cfg = settings_for(tmp_path, gateway_token="token", decision_mode="glm",
                       glm_base_url="https://models.example/v1", glm_model="old-model", glm_api_key="old-key")
    world = CountingWorld(cfg)
    engine = Engine(cfg, backend=world)
    old_settings = engine.settings
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg, engine)),
                                    base_url="http://gateway.test", headers={"Authorization": "Bearer token"}) as client:
            first = (await client.post("/api/tasks", json={
                "request_id": "old-revision", "goal": {"kind": "explore", "max_steps": 1}})).json()
            await asyncio.wait_for(entered.wait(), 2)
            stop_calls = world.stop_calls
            saved = await client.patch("/api/model-config", json={
                "expected_revision": 0, "glm_model": "new-model", "glm_timeout_s": 7.5,
                "glm_api_key_action": "set", "glm_api_key": "new-key", "glm_json_mode": False})
            assert saved.status_code == 200, saved.text
            assert saved.json()["pending"] is True
            assert saved.json()["active"]["revision"] == 0
            assert saved.json()["saved"]["revision"] == 1
            assert engine.settings is old_settings
            assert old_settings.glm_model == "old-model" and old_settings.glm_api_key == "old-key"
            assert len(instances) == 1 and not instances[0].closed
            assert world.stop_calls == stop_calls  # Saving settings neither cancels nor stops a job.
            release.set()
            await asyncio.wait_for(engine.runner, 2)
            old_task = (await client.get("/api/tasks/" + first["id"])).json()
            assert old_task["model_id"] == "old-model" and old_task["model_config_revision"] == 0
            assert old_task["status"] == "budget_exhausted" and len(world.actions) == 1
            assert instances[0].calls == 1 and not instances[0].closed
            second = (await client.post("/api/tasks", json={
                "request_id": "new-revision", "goal": {"kind": "explore", "max_steps": 1}})).json()
            await asyncio.wait_for(engine.runner, 2)
            assert second["model_id"] == "new-model" and second["model_config_revision"] == 1
            assert instances[0].closed and len(instances) == 2
            assert instances[1].calls == 1
            assert instances[1].api_key == "new-key"
            assert instances[1].timeout_s == 7.5 and instances[1].response_format is False
            assert engine.settings.glm_model == "new-model"
            assert (await client.get("/api/model-config")).json()["pending"] is False
            assert "new-key" not in (await client.get("/api/events")).text
    finally:
        release.set()
        await engine.close()
