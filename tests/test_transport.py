"""Exercise real WebSockets and channel flow control without cloud credentials."""
import json
import threading
import time
from contextlib import contextmanager

import pytest
from websockets.sync.client import connect as real_connect
from websockets.sync.server import serve

from siigo_bridge.auth import BridgeError
from siigo_bridge import transport as t


@contextmanager
def endpoint(monkeypatch, behavior, global_reply=None):
    errors = []
    captured = {}

    def handle(ws):
        try:
            opening = ws.recv()
            assert opening == b'\x5a' + t._string('session') + t._u32(0) + t._u32(t.WINDOW) + t._u32(t.MAX_PACKET)
            if global_reply is not None:
                ws.send(b'\x50' + t._string('hostkeys-00@openssh.com') +
                        bytes([int(global_reply)]) + t._string(b'host-key'))
                if global_reply:
                    assert ws.recv() == b'\x52'
            ws.send(b'\x5b' + t._u32(0) + t._u32(7) + t._u32(4096) + t._u32(1024))
            command = t._Reader(ws.recv())
            assert command.take(1) == b'\x62'
            assert command.uint() == 7
            assert command.string() == b'exec'
            assert command.take(1) == b'\x01'
            captured['command'] = command.string().decode()
            command.end()
            ws.send(b'\x63' + t._u32(0))
            body = bytearray()
            sizes = []
            window = 4096
            while True:
                r = t._Reader(ws.recv())
                kind = r.take(1)[0]
                assert r.uint() == 7
                if kind == 96:
                    r.end()
                    break
                assert kind == 94
                data = r.string()
                r.end()
                assert 0 < len(data) <= min(1024, window)
                sizes.append(len(data))
                body.extend(data)
                window -= len(data)
                if not window:
                    window = 3072
                    ws.send(b'\x5d' + t._u32(0) + t._u32(window))
            captured['payload'] = json.loads(body)
            captured['sizes'] = sizes
            behavior(ws, captured)
        except Exception as exc:
            errors.append(exc)

    server = serve(handle, '127.0.0.1', 0, compression=None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.socket.getsockname()[1]

    def connect(_url, **kwargs):
        return real_connect(f'ws://127.0.0.1:{port}', **kwargs)

    monkeypatch.setattr(t, 'connect', connect)
    try:
        yield captured, errors
    finally:
        server.shutdown()
        thread.join(3)


def output(ws, chunks, status=0, eof=True):
    for chunk in chunks:
        ws.send(b'\x5e' + t._u32(0) + t._string(chunk))
    if eof:
        ws.send(b'\x60' + t._u32(0))
    ws.send(b'\x62' + t._u32(0) + t._string('exit-status') + b'\x00' + t._u32(status))
    ws.send(b'\x61' + t._u32(0))
    # Receive replenishment/close until the transport closes the WebSocket.
    try:
        while True:
            ws.recv(timeout=2)
    except Exception:
        pass


@pytest.mark.parametrize('action', ['responses', 'chat'])
def test_large_upload_and_exact_split_output(monkeypatch, action):
    text = 'shell $(no) ; 雪' * 9000
    raw = 'data: 雪🙂\n\n'.encode()
    prelude = b'{"status":200,"headers":{"content-type":"text/event-stream","x-secret":"hidden"}}\n'
    with endpoint(monkeypatch, lambda ws, _: output(ws, [prelude + raw[:8], raw[8:10], raw[10:]])) as (seen, errors):
        forward = t.Forward('cloud-access', 'a' * 24, {'action': action, 'body': {'input': text}}, timeout=5)
        assert forward.status == 200
        assert forward.headers == {'content-type': 'text/event-stream'}
        assert b''.join(forward.iter_bytes()) == raw
        assert seen['payload']['body']['input'] == text
        assert seen['payload']['action'] == action
        assert text not in seen['command']
        assert 'cloud-access' not in seen['command']
        assert len(seen['sizes']) > 32
        assert not errors


@pytest.mark.parametrize('wants_reply', [False, True])
def test_global_hostkeys_does_not_break_channel(monkeypatch, wants_reply):
    chunks = [b'{"status":200,"headers":{}}\n{"ok":true}']
    with endpoint(monkeypatch, lambda ws, _: output(ws, chunks), global_reply=wants_reply) as (_, errors):
        forward = t.Forward('access', 'a' * 24, {'action': 'models'}, timeout=5)
        assert b''.join(forward.iter_bytes()) == b'{"ok":true}'
        assert not errors


@pytest.mark.parametrize('status,eof', [(1, True), (0, False)])
def test_remote_incomplete_is_not_success(monkeypatch, status, eof):
    with endpoint(monkeypatch, lambda ws, _: output(ws, [b'{"status":200,"headers":{}}\nbody'], status, eof)):
        forward = t.Forward('access', 'a' * 24, {'action': 'models'}, timeout=5)
        with pytest.raises(BridgeError):
            b''.join(forward.iter_bytes())


def test_wrong_recipient_rejected(monkeypatch):
    def bad(ws, _):
        ws.send(b'\x5e' + t._u32(9) + t._string(b'not ours'))
        time.sleep(.1)
    with endpoint(monkeypatch, bad):
        with pytest.raises(BridgeError):
            t.Forward('access', 'a' * 24, {'action': 'models'}, timeout=5)


def test_header_wait_cancellation_sends_term(monkeypatch):
    cancel = threading.Event()
    ready = threading.Event()
    terminated = threading.Event()
    failures = []

    def stalled(ws, _):
        ready.set()
        while True:
            r = t._Reader(ws.recv(timeout=3))
            kind = r.take(1)[0]
            assert r.uint() == 7
            if kind == 98:
                assert r.string() == b'signal'
                assert r.take(1) == b'\x00'
                assert r.string() == b'TERM'
                r.end()
                terminated.set()
                return

    with endpoint(monkeypatch, stalled):
        def construct():
            try:
                t.Forward('access', 'a' * 24, {'action': 'models'}, timeout=5, cancel_event=cancel)
            except BridgeError as exc:
                failures.append(exc)
        thread = threading.Thread(target=construct)
        thread.start()
        assert ready.wait(2)
        cancel.set()
        thread.join(2)
        assert not thread.is_alive()
        assert failures and failures[0].code == 'cancelled'
        assert terminated.wait(1)


@pytest.mark.parametrize('packet', [b'', b'\x00', b'\x00\x00\x00\x05x'])
def test_binary_reader_rejects_truncated_strings(packet):
    with pytest.raises(ValueError):
        t._Reader(packet).string()


def test_oversized_prelude_rejected(monkeypatch):
    with endpoint(monkeypatch, lambda ws, _: output(ws, [b'x' * 16385])):
        with pytest.raises(BridgeError):
            t.Forward('access', 'a' * 24, {'action': 'models'}, timeout=5)
