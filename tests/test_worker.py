"""Run the packaged Node worker against isolated local HTTP and model fixtures."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import subprocess
import threading

import pytest
from siigo_bridge.transport import WORKER_SOURCE


NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for isolated worker tests")


@contextmanager
def upstream(respond):
    seen = []
    errors = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            try:
                body = self.rfile.read(int(self.headers['content-length']))
                seen.append((self.path, dict(self.headers), json.loads(body)))
                respond(self)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # Bounded error reads intentionally cancel the upstream body.
            except Exception as exc:
                errors.append(exc)
                self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/ModelApi', seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not errors


def run_worker(tmp_path, envelope, base='http://127.0.0.1:1/ModelApi'):
    env = os.environ.copy()
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               AGENT_PROVIDER_BASE_URL=base, AGENT_PROVIDER_API_KEY='fixture-secret-key')
    result = subprocess.run([NODE, '-e', WORKER_SOURCE], input=json.dumps(envelope).encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=5)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    prelude, body = result.stdout.split(b'\n', 1)
    return json.loads(prelude), body


def test_chat_stream_keeps_native_events_and_split_utf8(tmp_path):
    wire = ('data: {"choices":[{"delta":{"reasoning_content":"雪🙂",'
            '"tool_calls":[{"index":0,"function":{"arguments":"{\\\"x\\\":1}"}}]},'
            '"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n').encode()
    body = {'model': 'fixture', 'stream': True,
            'messages': [{'role': 'assistant', 'content': None, 'reasoning_content': 'native',
                          'tool_calls': [{'id': 'call1', 'type': 'function',
                                          'function': {'name': 'local', 'arguments': '{}'}}]},
                         {'role': 'tool', 'tool_call_id': 'call1', 'content': 'Windows'}],
            'thinking': {'type': 'enabled'}, 'reasoning_effort': 'max'}

    def respond(handler):
        if handler.path != '/ModelApi/chat/completions':
            handler.send_error(404)
            return
        handler.send_response(200)
        handler.send_header('content-type', 'text/event-stream')
        handler.send_header('content-length', str(len(wire)))
        handler.end_headers()
        # Split in the middle of a UTF-8 sequence and native SSE fields.
        cut = wire.index('雪'.encode()) + 1
        for chunk in (wire[:cut], wire[cut:cut + 2], wire[cut + 2:]):
            handler.wfile.write(chunk)
            handler.wfile.flush()

    with upstream(respond) as (base, seen):
        meta, received = run_worker(tmp_path, {'action': 'chat', 'body': body}, base)
    assert meta == {'status': 200, 'headers': {'content-type': 'text/event-stream'}}
    assert received == wire


@pytest.mark.parametrize('error,expected', [
    ({'code': 'AccessDenied.Unpurchased', 'message': 'fixture-secret-key', 'id': 'private'}, 'AccessDenied.Unpurchased'),
    ({'code': 'unsafe secret!', 'type': 'quota:exhausted'}, 'quota:exhausted'),
    ({'code': 'x' * 81, 'type': 'private\nheader'}, 'upstream_error'),
])
@pytest.mark.parametrize('action', ['chat', 'responses'])
def test_upstream_errors_preserve_only_safe_code(tmp_path, error, expected, action):
    raw = json.dumps({'error': error, 'token': 'fixture-secret-key'}).encode()

    def respond(handler):
        handler.send_response(403)
        for key, value in [('content-type', 'text/plain'), ('retry-after', 'private'),
                           ('cache-control', 'private'), ('x-request-id', 'private')]:
            handler.send_header(key, value)
        handler.send_header('content-length', str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    with upstream(respond) as (base, seen):
        meta, received = run_worker(tmp_path, {'action': action, 'body': {}}, base)
    assert meta == {'status': 403, 'headers': {'content-type': 'application/json'}}
    assert json.loads(received) == {'error': {'message': 'Siigo upstream request failed',
                                            'code': expected, 'type': expected}}
    assert len(seen) == 1
    assert b'fixture-secret-key' not in received
    assert b'private' not in received


def test_error_body_bound_cancels_without_waiting_for_eof(tmp_path):
    released = threading.Event()

    def respond(handler):
        handler.send_response(429)
        handler.send_header('content-length', str(65536 * 4))
        handler.end_headers()
        handler.wfile.write(b' ' * 65536)
        handler.wfile.flush()
        # Worker must finish without waiting for the remainder of this body.
        released.wait(8)
        handler.close_connection = True

    with upstream(respond) as (base, seen):
        try:
            meta, received = run_worker(tmp_path, {'action': 'chat', 'body': {}}, base)
        finally:
            released.set()
    assert meta == {'status': 429, 'headers': {'content-type': 'application/json'}}
    assert json.loads(received)['error']['code'] == 'upstream_error'
    assert len(seen) == 1


def test_catalog_filters_protocols_and_private_metadata(tmp_path):
    directory = tmp_path / '.models'
    directory.mkdir()
    native_models = [
        ('6abc6c6212ee6ff04f7eaee3', 'openai-responses'),
        ('6a7a84c9becddb9d3bd45315', 'openai-completions'),
        ('6a82c75d2b005013ff2208a0', 'openai-images'),
        ('a' * 24, 'openai-responses'),
        ('b' * 24, 'unknown'),
        ('6a509b23a36e63734c28aef9', 'openai-completions'),
    ]
    for index, (native_id, api) in enumerate(native_models):
        model = {'api': api,
                 'name': f'Fixture {index}', 'reasoning': True, 'input': ['text', 'image', 'secret'],
                 'contextWindow': 272000, 'maxTokens': 128000,
                 'thinkingLevelMap': {'off': None, 'minimal': None, 'low': 'low', 'high': 3,
                                      'private': 'fixture-secret-key'},
                 'compat': {'supportsStore': False, 'supportsDeveloperRole': False,
                            'supportsStrictMode': True, 'supportsReasoningEffort': True,
                            'supportsReasoningParams': True,
                            'requiresReasoningContentOnAssistantMessages': True,
                            'thinkingFormat': 'deepseek', 'maxTokensField': 'max_tokens',
                            'extraBody': {'secret': 'fixture-secret-key'}},
                 'baseUrl': 'https://private.invalid', 'apiKey': 'fixture-secret-key',
                 'extraBody': {'secret': 'fixture-secret-key'}}
        (directory / f'{native_id}.json').write_text(json.dumps(model), encoding='utf-8')
    (directory / 'not-a-model.json').write_text('{}', encoding='utf-8')
    meta, received = run_worker(tmp_path, {'action': 'models'})
    assert meta['status'] == 200
    models = json.loads(received)['data']
    assert {model['id'] for model in models} == {'gpt-6.1-sol', 'deepseek-v4.1-flash'}
    by_id = {model['id']: model for model in models}
    assert by_id['gpt-6.1-sol']['name'] == 'GPT-6.1 Sol'
    assert by_id['gpt-6.1-sol']['contextWindow'] == 1050000
    assert by_id['deepseek-v4.1-flash']['contextWindow'] == 1048576
    assert by_id['gpt-6.1-sol']['contextWindowSourceUrl'].startswith('https://developers.openai.com/')
    for model in models:
        assert model['contextWindowSource'] == 'manufacturer'
        assert model['upstreamDeclaredContextWindow'] == 272000
        assert model['thinkingLevelMap'] == {'off': None, 'minimal': None, 'low': 'low'}
        assert model['input'] == ['text', 'image']
        assert model['compat'] == {'supportsStore': False, 'supportsDeveloperRole': False,
                                   'supportsStrictMode': True, 'supportsReasoningEffort': True,
                                   'supportsReasoningParams': True,
                                   'requiresReasoningContentOnAssistantMessages': True,
                                   'thinkingFormat': 'deepseek', 'maxTokensField': 'max_tokens'}
        assert set(model) == {'id', 'object', 'owned_by', 'api', 'name', 'reasoning', 'input',
                              'contextWindow', 'maxTokens', 'thinkingLevelMap', 'compat',
                              'contextWindowSource', 'contextWindowSourceUrl', 'upstreamDeclaredContextWindow'}
    assert b'fixture-secret-key' not in received
    assert b'private.invalid' not in received
    assert b'6abc6c6212ee6ff04f7eaee3' not in received


@pytest.mark.parametrize('envelope', [
    {'action': 'chat', 'body': {}, 'url': 'http://private.invalid'},
    {'action': '/chat/completions', 'body': {}},
    {'action': 'chat', 'body': []},
    {'action': 'models', 'body': {}},
])
def test_envelope_cannot_choose_arbitrary_endpoint(tmp_path, envelope):
    meta, received = run_worker(tmp_path, envelope)
    assert meta['status'] == 400
    assert json.loads(received)['error']['code'] == 'invalid_request'
