"""Current-user Windows credentials and serialized Siigo authentication."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time

import requests

ORIGIN = "https://www.siigo.cn"
DEVICE = "Windows local Siigo API bridge"

class _LoginIntent(dict):
    def __repr__(self):
        return "<Siigo login intent>"

    __str__ = __repr__



class BridgeError(Exception):
    def __init__(self, message: str, code: str = "bridge_error", http_status: int = 502):
        super().__init__(message)
        self.message = message
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class Credentials:
    access: str = field(repr=False)
    refresh: str = field(repr=False)
    machine_id: str
    api_key: str = field(repr=False)
    device: str = DEVICE
    refresh_uncertain: bool = False

    def __post_init__(self):
        if (any(not isinstance(value, str) or not value for value in
                (self.access, self.refresh, self.api_key, self.device))
                or not isinstance(self.machine_id, str)
                or re.fullmatch(r"[0-9a-f]{24}", self.machine_id) is None
                or type(self.refresh_uncertain) is not bool):
            raise BridgeError("Invalid credential data", "credential_invalid", 401)


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, protect: bool) -> bytes:
    if os.name != "nt":
        raise BridgeError("Windows current-user DPAPI is required", "dpapi_unavailable", 503)
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR,
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_Blob)]
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(_Blob)]
    crypt.CryptProtectData.restype = crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    pinned = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = _Blob(len(data), pinned)
    target = _Blob()
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    description = "Siigo API bridge" if protect else None
    try:
        if not function(ctypes.byref(source), description, None, None, None, 1,
                        ctypes.byref(target)):
            raise BridgeError("Credential encryption operation failed", "dpapi_failed", 503)
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        if target.pbData:
            kernel.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))


class CredentialStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            root = os.environ.get("LOCALAPPDATA")
            if not root:
                raise BridgeError("Local application data directory unavailable", "store_unavailable", 503)
            path = Path(root) / "SiigoApiBridge" / "credentials.dpapi"
        self.path = Path(path)

    @contextmanager
    def locked(self):
        if os.name != "nt":
            raise BridgeError("Windows credential locking is required", "dpapi_unavailable", 503)
        import msvcrt
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+b")
        except OSError:
            raise BridgeError("Credential lock unavailable", "store_failed", 503) from None
        acquired = False
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            deadline = time.monotonic() + 40
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise BridgeError("Credential lock timed out", "store_busy", 503) from None
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.close()

    def load(self) -> Credentials:
        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError:
            raise BridgeError("Login required", "credential_missing", 401) from None
        except OSError:
            raise BridgeError("Credential store could not be read", "store_failed", 503) from None
        try:
            value = json.loads(_crypt(encrypted, False))
            if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
                raise ValueError()
            if set(value) != {"version", "access", "refresh", "machine_id", "api_key", "device", "refresh_uncertain"}:
                raise ValueError()
            return Credentials(**{key: item for key, item in value.items() if key != "version"})
        except (ValueError, TypeError, UnicodeError):
            raise BridgeError("Invalid credential store", "credential_invalid", 401) from None

    def save(self, credentials: Credentials) -> None:
        value = {"version": 1, **vars(credentials)}
        encrypted = _crypt(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(), True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A failed replace leaves only DPAPI ciphertext in the private temporary file.
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".credentials-", suffix=".dpapi", delete=False) as output:
                temporary = output.name
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise BridgeError("Credential store could not be saved", "store_failed", 503) from None


def intent_expiry(intent: dict) -> float:
    try:
        value = intent["expiresAt"]
        if isinstance(value, dict):
            value = value["$date"]
        if isinstance(value, str):
            expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError()
            result = expiry.timestamp()
        elif type(value) in (int, float):
            result = value / 1000
        else:
            raise ValueError()
        if not math.isfinite(result):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        raise BridgeError("Invalid login expiry", "login_invalid", 502) from None


def _expiry(token: str) -> float:
    try:
        segment = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))["exp"]
        if type(value) not in (float, int) or not math.isfinite(value):
            raise ValueError()
        return float(value)
    except (IndexError, KeyError, ValueError, TypeError, UnicodeError):
        raise BridgeError("Access token expiry invalid; login required", "auth_required", 401) from None


class AuthManager:
    def __init__(self, store: CredentialStore):
        self.store = store
        self._lock = threading.RLock()
        self._pending: Credentials | None = None
        self._pending_base: Credentials | None = None
        self._authorized: set[str] = set()
        self._claimed: set[str] = set()
        self._pending_claims: dict[str, tuple[str, str, str]] = {}

    def _latest(self) -> Credentials:
        current = self.store.load()
        if self._pending is not None:
            if current == self._pending_base:
                self.store.save(self._pending)
                current = self._pending
            self._pending = self._pending_base = None
        return current

    def credentials(self, *, refresh: bool = False) -> Credentials:
        with self._lock, self.store.locked():
            current = self._latest()
            if refresh:
                if current.refresh_uncertain:
                    raise BridgeError("Refresh outcome uncertain; login required", "auth_uncertain", 401)
                if _expiry(current.access) <= time.time() + 300:
                    current = self._rotate(current)
            return current

    def install(self, access: str, refresh: str, machine_id: str) -> Credentials:
        with self._lock, self.store.locked():
            try:
                previous = self.store.load()
                key = previous.api_key
            except BridgeError as error:
                if error.code != "credential_missing":
                    raise
                key = secrets.token_urlsafe(32)
            current = Credentials(access, refresh, machine_id, key)
            self.store.save(current)
            self._pending = self._pending_base = None
            return current

    def _request(self, method: str, kind: str, *, body=None, pipeline=None, access=None):
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", kind) is None:
            raise BridgeError("Invalid resource", "invalid_resource", 400)
        options = {"pipeline": pipeline} if pipeline is not None else {"fullResource": True}
        headers = {"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"}
        if access is not None:
            headers["Authorization"] = "Bearer " + access
        try:
            response = requests.request(method, ORIGIN + "/resources/" + kind,
                params={"options": json.dumps(options, ensure_ascii=False, separators=(",", ":"))},
                json=body, headers=headers, timeout=25)
            if response.status_code in (401, 403):
                raise BridgeError("Login required", "auth_required", 401)
            if not 200 <= response.status_code < 300:
                raise BridgeError("Authentication service rejected request", "auth_upstream", 502)
            return response.json()
        except (requests.RequestException, ValueError):
            raise BridgeError("Authentication request failed; login may be required", "auth_uncertain", 502) from None

    def _rotate(self, current: Credentials) -> Credentials:
        if current.refresh_uncertain:
            raise BridgeError("Refresh outcome uncertain; login required", "auth_uncertain", 401)
        marker = replace(current, refresh_uncertain=True)
        self.store.save(marker)
        result = self._request("POST", "RefreshToken", body={"token": current.refresh, "device": current.device}, access=current.access)
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(status, dict) or status.get("state") != "成功":
            raise BridgeError("Refresh rejected; login required", "auth_required", 401)
        access, refresh = status.get("access"), status.get("refresh")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise BridgeError("Refresh result incomplete; login required", "auth_uncertain", 401)
        updated = replace(current, access=access, refresh=refresh, refresh_uncertain=False)
        self._pending, self._pending_base = updated, marker
        self.store.save(updated)
        self._pending = self._pending_base = None
        return updated


    def refresh(self) -> Credentials:
        with self._lock, self.store.locked():
            # A pending successful rotation needs persistence only, not a second rotation.
            pending = self._pending is not None
            current = self._latest()
            if pending:
                if current.refresh_uncertain:
                    raise BridgeError("Refresh outcome uncertain; login required", "auth_uncertain", 401)
                return current
            return self._rotate(current)

    def get_resource(self, kind: str, pipeline: list) -> list:
        value = self._request("GET", kind, pipeline=pipeline, access=self.credentials(refresh=True).access)
        if not isinstance(value, list):
            raise BridgeError("Invalid resource response", "upstream_invalid", 502)
        return value

    def start_login(self) -> dict:
        value = self._request("POST", "WechatCrossDeviceLoginRequest", body={"appId": "wx9bad5ca3c783fc9b", "device": DEVICE})
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key] for key in ("authorizationCapability", "claimCapability", "observationId")):
            raise BridgeError("Invalid login response", "login_invalid", 502)
        intent_expiry(value)
        return _LoginIntent(value)

    def _intent_key(self, intent: dict) -> str:
        if intent_expiry(intent) <= time.time():
            raise BridgeError("Login intent expired", "login_expired", 401)
        key = intent.get("claimCapability")
        if not isinstance(key, str) or not key or not isinstance(intent.get("observationId"), str):
            raise BridgeError("Invalid login intent", "login_invalid", 400)
        return key

    def observe_login(self, intent: dict) -> str:
        with self._lock:
            key = self._intent_key(intent)
            value = self._request("GET", "WechatCrossDeviceAuthorizationObservation", pipeline=[{"$match": {"observationId": intent["observationId"]}}])
            if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict) or not isinstance(value[0].get("state"), str):
                raise BridgeError("Invalid login observation", "login_invalid", 502)
            state = value[0]["state"]
            if state == "已授权":
                self._authorized.add(key)
            else:
                self._authorized.discard(key)
            return state

    def claim_login(self, intent: dict, machine_id: str) -> Credentials:
        with self._lock:
            key = intent.get("claimCapability")
            if key not in self._pending_claims:
                key = self._intent_key(intent)
                if key not in self._authorized or key in self._claimed:
                    raise BridgeError("Login is not claimable", "login_not_authorized", 401)
                self._claimed.add(key)
                result = self._request("POST", "WechatCrossDeviceLoginClaimRequest", body={"claimCapability": key})
                status = result.get("status") if isinstance(result, dict) else None
                if not isinstance(status, dict) or status.get("state") != "成功":
                    raise BridgeError("Login claim failed", "auth_required", 401)
                access, refresh = status.get("access"), status.get("refresh")
                if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
                    raise BridgeError("Login result incomplete", "auth_uncertain", 401)
                self._pending_claims[key] = (access, refresh, machine_id)
            credentials = self.install(*self._pending_claims[key])
            del self._pending_claims[key]
            return credentials
