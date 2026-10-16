from contextlib import contextmanager
from dataclasses import replace
import base64
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from siigo_bridge.auth import (
    AuthManager, BridgeError, CredentialStore, Credentials,
    intent_expiry,
)

TEST_MACHINE = "0" * 24


def token(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


def initial():
    return Credentials(token(time.time() - 10), "old-refresh", TEST_MACHINE, "local-key")


class MemoryStore:
    def __init__(self, value=None):
        self.value = value
        self.guard = threading.RLock()
        self.fail_new = False

    @contextmanager
    def locked(self):
        with self.guard:
            yield

    def load(self):
        if self.value is None:
            raise BridgeError("Login required", "credential_missing", 401)
        return self.value

    def save(self, value):
        if self.fail_new and value.refresh == "new-refresh":
            raise BridgeError("Save failed", "store_failed", 503)
        self.value = value


def successful_rotation(manager, store, counter):
    def request(*args, **kwargs):
        assert store.value.refresh_uncertain is True
        counter.append(1)
        return {"status": {"state": "成功", "access": token(time.time() + 3600), "refresh": "new-refresh"}}
    manager._request = request


def test_missing_store(tmp_path):
    with pytest.raises(BridgeError) as error:
        CredentialStore(tmp_path / "missing").load()
    assert error.value.code == "credential_missing"


def test_schema_and_secret_repr():
    value = initial()
    assert value.access not in repr(value)
    assert value.refresh not in repr(value)
    assert value.api_key not in repr(value)
    with pytest.raises(BridgeError):
        replace(value, machine_id="BAD")
    with pytest.raises(BridgeError):
        replace(value, refresh_uncertain=1)


@pytest.mark.skipif(os.name != "nt", reason="Windows current-user DPAPI")
def test_dpapi_roundtrip_and_corruption(tmp_path):
    store = CredentialStore(tmp_path / "credentials.dpapi")
    value = initial()
    with store.locked():
        store.save(value)
        assert store.load() == value
    ciphertext = store.path.read_bytes()
    assert value.refresh.encode() not in ciphertext
    assert value.api_key.encode() not in ciphertext
    store.path.write_bytes(b"corrupt")
    with pytest.raises(BridgeError):
        store.load()


def test_bad_encrypted_schema_is_rejected(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path / "credentials.dpapi")
    store.path.write_bytes(b"encrypted")
    monkeypatch.setattr("siigo_bridge.auth._crypt", lambda *_: b'{"version":1}')
    with pytest.raises(BridgeError) as error:
        store.load()
    assert error.value.code == "credential_invalid"


def test_install_preserves_local_key_and_clears_uncertainty():
    store = MemoryStore(replace(initial(), refresh_uncertain=True))
    manager = AuthManager(store)
    result = manager.install("new-access", "new-refresh", TEST_MACHINE)
    assert result.api_key == "local-key"
    assert not result.refresh_uncertain
    assert store.value == result


def test_credentials_never_refreshes_and_uncertain_key_is_readable():
    store = MemoryStore(replace(initial(), refresh_uncertain=True))
    manager = AuthManager(store)
    manager._request = lambda *a, **k: pytest.fail("network must not be called")
    assert manager.credentials().api_key == "local-key"
    for operation in (lambda: manager.credentials(refresh=True), manager.refresh):
        with pytest.raises(BridgeError) as error:
            operation()
        assert error.value.code == "auth_uncertain"


def test_access_singleflight_across_managers():
    store = MemoryStore(initial())
    managers = [AuthManager(store), AuthManager(store)]
    counter = []
    for manager in managers:
        successful_rotation(manager, store, counter)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda i: managers[i % 2].credentials(refresh=True).access, range(16)))
    assert len(set(values)) == 1
    assert len(counter) == 1
    assert store.value.refresh == "new-refresh"
    assert not store.value.refresh_uncertain


def test_pending_pair_retries_only_persistence():
    store = MemoryStore(initial())
    manager = AuthManager(store)
    counter = []
    successful_rotation(manager, store, counter)
    store.fail_new = True
    with pytest.raises(BridgeError):
        manager.refresh()
    assert store.value.refresh_uncertain
    with pytest.raises(BridgeError):
        manager.credentials()
    assert len(counter) == 1
    store.fail_new = False
    result = manager.refresh()
    assert result.refresh == "new-refresh"
    assert store.value == result
    assert len(counter) == 1


def test_pending_rotation_does_not_overwrite_new_login():
    store = MemoryStore(initial())
    first = AuthManager(store)
    successful_rotation(first, store, [])
    store.fail_new = True
    with pytest.raises(BridgeError):
        first.refresh()
    second = AuthManager(store)
    newer = second.install(token(time.time() + 7200), "login-refresh", TEST_MACHINE)
    store.fail_new = False
    assert first.credentials() == newer
    assert store.value == newer


def test_network_uncertainty_survives_new_manager():
    store = MemoryStore(initial())
    manager = AuthManager(store)
    def fail(*args, **kwargs):
        assert store.value.refresh_uncertain
        raise BridgeError("Request failed", "auth_uncertain")
    manager._request = fail
    with pytest.raises(BridgeError):
        manager.refresh()
    other = AuthManager(store)
    other._request = lambda *a, **k: pytest.fail("uncertain token must not be replayed")
    with pytest.raises(BridgeError):
        other.refresh()
    assert other.credentials().api_key == "local-key"


def test_marker_save_failure_prevents_network():
    class FailingStore(MemoryStore):
        def save(self, value):
            raise BridgeError("Save failed", "store_failed")
    manager = AuthManager(FailingStore(initial()))
    manager._request = lambda *a, **k: pytest.fail("marker must be durable first")
    with pytest.raises(BridgeError):
        manager.refresh()


def test_incomplete_rotation_never_publishes_half_pair():
    store = MemoryStore(initial())
    manager = AuthManager(store)
    manager._request = lambda *a, **k: {"status": {"state": "成功", "access": "half"}}
    with pytest.raises(BridgeError):
        manager.refresh()
    assert store.value.refresh == "old-refresh"
    assert store.value.refresh_uncertain


def test_claim_requires_observation_and_never_retries_ambiguous_claim():
    manager = AuthManager(MemoryStore())
    intent = {"claimCapability": "claim", "observationId": "observe", "expiresAt": {"$date": "2099-01-01T00:00:00Z"}}
    with pytest.raises(BridgeError):
        manager.claim_login(intent, TEST_MACHINE)
    manager._request = lambda *a, **k: [{"state": "已授权"}]
    assert manager.observe_login(intent) == "已授权"
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise BridgeError("Ambiguous claim", "auth_uncertain")
    manager._request = fail
    with pytest.raises(BridgeError):
        manager.claim_login(intent, TEST_MACHINE)
    with pytest.raises(BridgeError):
        manager.claim_login(intent, TEST_MACHINE)
    assert len(calls) == 1


def test_claim_storage_retry_does_not_reclaim_or_expire():
    store = MemoryStore()
    store.fail_new = True
    manager = AuthManager(store)
    intent = {"claimCapability": "claim", "observationId": "observe", "expiresAt": {"$date": "2099-01-01T00:00:00Z"}}
    manager._request = lambda *a, **k: [{"state": "已授权"}]
    manager.observe_login(intent)
    network_claims = []
    def claim(*args, **kwargs):
        network_claims.append(1)
        return {"status": {"state": "成功", "access": token(time.time() + 3600), "refresh": "new-refresh"}}
    manager._request = claim
    with pytest.raises(BridgeError):
        manager.claim_login(intent, TEST_MACHINE)
    store.fail_new = False
    intent["expiresAt"] = {"$date": "2000-01-01T00:00:00Z"}
    saved = manager.claim_login(intent, TEST_MACHINE)
    assert saved.refresh == "new-refresh"
    assert store.value == saved
    assert len(network_claims) == 1


def test_expiry_rejects_missing_timezone_and_nan():
    for value in ("2099-01-01", float("nan")):
        with pytest.raises(BridgeError):
            intent_expiry({"expiresAt": value})
