"""One-shot binary SSH channel over the authenticated Siigo WebSocket."""
from collections import deque
from importlib.resources import files
import json
import re
import shlex
import struct
import threading
import time
from urllib.parse import urlencode

from websockets.sync.client import connect

from .auth import BridgeError
from .model_registry import MODEL_REGISTRY

_REGISTRY_LITERAL = json.dumps(MODEL_REGISTRY, ensure_ascii=True, separators=(",", ":"))
WORKER_SOURCE = "'use strict';\nconst MODEL_REGISTRY = " + _REGISTRY_LITERAL + ";\n" + files("siigo_bridge").joinpath("worker.js").read_text(encoding="utf-8")
ENVELOPE_LIMIT = 6 * 16 * 1024 * 1024 + 4096
WINDOW = 1024 * 1024
MAX_PACKET = 32768


def _u32(n):
    return struct.pack(">I", n)


def _string(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _u32(len(value)) + value


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, count):
        if count < 0 or self.pos + count > len(self.data):
            raise ValueError("short packet")
        result = self.data[self.pos:self.pos + count]
        self.pos += count
        return result

    def uint(self):
        return struct.unpack(">I", self.take(4))[0]

    def string(self):
        return self.take(self.uint())

    def end(self):
        if self.pos != len(self.data):
            raise ValueError("trailing packet data")


def _error(code="transport_error", status=502):
    return BridgeError("Siigo transport failed" if status != 504 else "Siigo transport timed out", code, status)


class Forward:
    def __init__(self, access: str, machine_id: str, payload: dict, timeout: float = 180,
                 cancel_event: threading.Event | None = None):
        self._lock = threading.RLock()
        self._closed = False
        self._ws = None
        self._remote = None
        self._cancel = cancel_event or threading.Event()
        self._deadline = time.monotonic() + min(float(timeout), 180)
        self._out = deque()
        self._queued = 0
        self._stderr = 0
        self._send_window = 0
        self._receive_window = WINDOW
        self._max_packet = 0
        self._exec_ok = False
        self._exit = None
        self._eof = False
        self._remote_closed = False
        try:
            valid = isinstance(payload, dict) and (
                (set(payload) == {"action", "body"} and payload.get("action") in ("responses", "chat")
                 and isinstance(payload.get("body"), dict)) or
                (set(payload) == {"action"} and payload.get("action") == "models"))
            if not valid or not re.fullmatch(r"[0-9a-f]{24}", machine_id):
                raise _error("invalid_request", 400)
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(body) > ENVELOPE_LIMIT:
                raise _error("request_too_large", 413)
            options = {"pipeline": [{"$match": {"_id": {"$oid": machine_id}}}, {"$limit": 1}, {"$custom": True}], "stream": True}
            headers = {"accept": "application/json", "content-type": "application/json; charset=utf-8", "authorization": "Bearer " + access}
            url = "wss://www.siigo.cn/resources/Machine?" + urlencode({"type": "json", "options": json.dumps(options, separators=(",", ":")), "headers": json.dumps(headers, separators=(",", ":"))})
            self._check()
            self._ws = connect(url, open_timeout=min(15, self._remaining()), close_timeout=1,
                               max_size=WINDOW + 65536, max_queue=4, compression=None, proxy=None)
            self._check()
            self._send(b"\x5a" + _string("session") + _u32(0) + _u32(WINDOW) + _u32(MAX_PACKET))
            while self._remote is None:
                self._pump()
            self._send(b"\x62" + _u32(self._remote) + _string("exec") + b"\x01" + _string("node -e " + shlex.quote(WORKER_SOURCE)))
            while not self._exec_ok:
                self._pump()
            offset = 0
            while offset < len(body):
                self._check()
                if not self._send_window:
                    self._pump()
                    continue
                count = min(self._send_window, self._max_packet, len(body) - offset)
                self._send(b"\x5e" + _u32(self._remote) + _string(body[offset:offset + count]))
                self._send_window -= count
                offset += count
            self._send(b"\x60" + _u32(self._remote))
            prelude = bytearray()
            while True:
                chunk = self._next()
                if chunk is None:
                    raise _error()
                split = chunk.find(b"\n")
                if split < 0:
                    prelude.extend(chunk)
                    if len(prelude) > 16384:
                        raise _error()
                    continue
                prelude.extend(chunk[:split])
                if len(prelude) > 16384:
                    raise _error()
                meta = json.loads(prelude.decode("utf-8"))
                status = meta["status"]
                if type(status) is not int or not 100 <= status <= 599 or not isinstance(meta.get("headers"), dict):
                    raise _error()
                self.status = status
                self.headers = {}
                for key, value in meta["headers"].items():
                    if isinstance(key, str) and key.lower() in {"content-type", "cache-control", "retry-after"} and isinstance(value, str) and len(value) <= 4096 and all(32 <= ord(c) < 127 for c in value):
                        self.headers[key.lower()] = value
                self._remainder = chunk[split + 1:]
                break
        except BaseException as exc:
            self.close()
            if isinstance(exc, (BridgeError, KeyboardInterrupt, SystemExit)):
                raise
            raise _error() from None

    def _remaining(self):
        return max(0.001, self._deadline - time.monotonic())

    def _check(self):
        if self._cancel.is_set() or self._closed:
            raise _error("cancelled", 499)
        if time.monotonic() >= self._deadline:
            raise _error("transport_timeout", 504)

    def _send(self, packet):
        self._check()
        with self._lock:
            self._check()
            self._ws.send(packet)

    def _credit(self, count):
        if count and not self._remote_closed:
            self._receive_window += count
            self._send(b"\x5d" + _u32(self._remote) + _u32(count))

    def _pump(self):
        self._check()
        try:
            packet = self._ws.recv(timeout=min(.25, self._remaining()))
        except TimeoutError:
            return
        except Exception:
            raise _error() from None
        try:
            if not isinstance(packet, bytes):
                raise ValueError()
            r = _Reader(packet)
            kind = r.take(1)[0]
            if kind == 80:
                # OpenSSH may advertise host keys before opening our channel.
                r.string()
                reply = r.take(1)[0]
                if reply not in (0, 1):
                    raise ValueError()
                r.take(len(r.data) - r.pos)
                if reply:
                    self._send(b"\x52")
                return
            if r.uint() != 0:
                raise ValueError()
            if kind == 91:
                if self._remote is not None:
                    raise ValueError()
                self._remote, self._send_window, self._max_packet = r.uint(), r.uint(), r.uint()
                if not self._max_packet:
                    raise ValueError()
            elif kind == 92:
                raise _error("channel_rejected")
            elif self._remote is None:
                raise ValueError()
            elif kind == 93:
                increment = r.uint()
                if not increment or self._send_window + increment > 0xffffffff:
                    raise ValueError()
                self._send_window += increment
            elif kind in (94, 95):
                if self._eof or self._remote_closed:
                    raise ValueError()
                if kind == 95 and r.uint() != 1:
                    raise ValueError()
                data = r.string()
                if len(data) > MAX_PACKET or len(data) > self._receive_window:
                    raise ValueError()
                self._receive_window -= len(data)
                if kind == 95:
                    self._stderr += len(data)
                    if self._stderr > 65536:
                        raise ValueError()
                    self._credit(len(data))
                else:
                    self._queued += len(data)
                    if self._queued > WINDOW:
                        raise ValueError()
                    self._out.append(data)
            elif kind == 96:
                if self._eof:
                    raise ValueError()
                self._eof = True
            elif kind == 97:
                self._remote_closed = True
            elif kind == 98:
                name, reply = r.string(), r.take(1)[0]
                if reply not in (0, 1):
                    raise ValueError()
                if name == b"exit-status":
                    if self._exit is not None:
                        raise ValueError()
                    self._exit = r.uint()
                elif name == b"exit-signal":
                    raise _error()
                else:
                    r.take(len(r.data) - r.pos)
                if reply:
                    self._send(bytes([99 if name == b"exit-status" else 100]) + _u32(self._remote))
            elif kind == 99:
                if self._exec_ok:
                    raise ValueError()
                self._exec_ok = True
            elif kind == 100:
                raise _error("command_rejected")
            else:
                raise ValueError()
            r.end()
        except BridgeError:
            raise
        except Exception:
            raise _error("invalid_channel_packet") from None

    def _next(self):
        while True:
            self._check()
            if self._out:
                data = self._out.popleft()
                self._queued -= len(data)
                self._credit(len(data))
                if data:
                    return data
            elif self._remote_closed:
                if self._exit != 0 or not self._eof:
                    raise _error("incomplete_response")
                return None
            else:
                self._pump()

    def iter_bytes(self):
        try:
            if self._remainder:
                self._check()
                yield self._remainder
                self._remainder = b""
            while True:
                chunk = self._next()
                if chunk is None:
                    return
                yield chunk
        except BridgeError:
            raise
        except Exception:
            raise _error() from None
        finally:
            self.close()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            ws = self._ws
            if ws is None:
                return
            if self._remote is not None and not self._remote_closed:
                try:
                    ws.send(b"\x62" + _u32(self._remote) + _string("signal") + b"\x00" + _string("TERM"))
                    ws.send(b"\x61" + _u32(self._remote))
                except Exception:
                    pass
            try:
                ws.close()
            except Exception:
                pass
