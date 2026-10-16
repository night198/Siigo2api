// MODEL_REGISTRY is embedded from trusted packaged data by the transport.
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { once } = require('node:events');
const LIMIT = 6 * 16 * 1024 * 1024 + 4096;
const controller = new AbortController();
let started = false;
let timedOut = false;
let outputClosed = false;
const timer = setTimeout(() => {
  timedOut = true;
  controller.abort();
  process.stdin.destroy(new Error('timeout'));
  if (started) process.exit(1);
}, 180000);
process.on('SIGTERM', () => { controller.abort(); process.exit(1); });
process.stdout.on('error', () => { outputClosed = true; controller.abort(); process.exit(1); });
process.stdout.on('close', () => { outputClosed = true; controller.abort(); process.exit(1); });
async function write(data) {
  if (outputClosed) throw new Error('output closed');
  if (!process.stdout.write(data)) await once(process.stdout, 'drain', { signal: controller.signal });
}
async function prelude(status, headers) {
  started = true;
  await write(JSON.stringify({ status, headers }) + '\n');
}
async function failure(status, code) {
  await prelude(status, { 'content-type': 'application/json' });
  await write(JSON.stringify({ error: { message: 'Siigo request failed', type: code, code } }));
}
function safeHeaders(response) {
  const headers = {};
  for (const key of ['content-type', 'cache-control', 'retry-after']) {
    const value = response.headers.get(key);
    if (value && value.length <= 4096 && /^[\x20-\x7e]*$/.test(value)) headers[key] = value;
  }
  return headers;
}
async function upstreamErrorCode(response) {
  if (!response.body) return 'upstream_error';
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  let complete = false;
  try {
    while (size < 65536) {
      const { done, value } = await reader.read();
      if (done) { complete = true; break; }
      const count = Math.min(value.length, 65536 - size);
      chunks.push(value.subarray(0, count));
      size += count;
      if (count !== value.length) break;
    }
    if (!complete) await reader.cancel();
    const error = JSON.parse(Buffer.concat(chunks, size).toString('utf8'))?.error;
    for (const code of [error?.code, error?.type]) {
      if (typeof code === 'string' && /^[A-Za-z0-9_.:-]{1,80}$/.test(code)) return code;
    }
  } catch {
    try { await reader.cancel(); } catch {}
  } finally {
    reader.releaseLock();
  }
  return 'upstream_error';
}
async function catalog() {
  const dir = path.join(os.homedir(), '.models');
  const byUpstream = new Map(MODEL_REGISTRY.map(entry => [entry.upstream_id, entry]));
  const data = [];
  for (const filename of (await fs.readdir(dir)).sort()) {
    if (!/^[0-9a-f]{24}\.json$/.test(filename)) continue;
    const stat = await fs.lstat(path.join(dir, filename));
    if (!stat.isFile() || stat.size > 1024 * 1024) continue;
    let model;
    try { model = JSON.parse(await fs.readFile(path.join(dir, filename), 'utf8')); }
    catch { continue; }
    if (!model || !['openai-responses', 'openai-completions'].includes(model.api)) continue;
    const registry = byUpstream.get(filename.slice(0, -5));
    if (!registry || registry.api !== model.api) continue;
    const item = { id: registry.public_id, object: 'model', name: registry.name, owned_by: 'siigo', api: model.api,
      contextWindow: registry.manufacturer_context_window, contextWindowSource: 'manufacturer',
      contextWindowSourceUrl: registry.context_source_url };
    if (Number.isSafeInteger(model.contextWindow) && model.contextWindow > 0) item.upstreamDeclaredContextWindow = model.contextWindow;
    if (typeof model.reasoning === 'boolean') item.reasoning = model.reasoning;
    if (Array.isArray(model.input)) item.input = model.input.filter(x => x === 'text' || x === 'image');
    if (Number.isSafeInteger(model.maxTokens) && model.maxTokens > 0) item.maxTokens = model.maxTokens;
    if (model.thinkingLevelMap && typeof model.thinkingLevelMap === 'object' && !Array.isArray(model.thinkingLevelMap)) {
      item.thinkingLevelMap = {};
      for (const key of ['off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']) {
        const value = model.thinkingLevelMap[key];
        if (value === null || typeof value === 'string') item.thinkingLevelMap[key] = value;
      }
    }
    if (model.compat && typeof model.compat === 'object' && !Array.isArray(model.compat)) {
      item.compat = {};
      for (const key of ['supportsStore', 'supportsDeveloperRole', 'supportsStrictMode', 'supportsReasoningEffort', 'supportsReasoningParams', 'requiresReasoningContentOnAssistantMessages']) {
        if (typeof model.compat[key] === 'boolean') item.compat[key] = model.compat[key];
      }
      if (['max_tokens', 'max_completion_tokens'].includes(model.compat.maxTokensField)) item.compat.maxTokensField = model.compat.maxTokensField;
      if (['qwen', 'deepseek', 'openai'].includes(model.compat.thinkingFormat)) item.compat.thinkingFormat = model.compat.thinkingFormat;
    }
    data.push(item);
  }
  await prelude(200, { 'content-type': 'application/json' });
  await write(JSON.stringify({ object: 'list', data }));
}
async function main() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > LIMIT) { await failure(400, 'invalid_request'); return; }
    chunks.push(chunk);
  }
  let envelope;
  try { envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks))); }
  catch { await failure(400, 'invalid_request'); return; }
  if (!envelope || typeof envelope !== 'object' || Array.isArray(envelope)) {
    await failure(400, 'invalid_request'); return;
  }
  if (envelope.action === 'models' && Object.keys(envelope).length === 1) {
    await catalog(); return;
  }
  const routes = { responses: '/responses', chat: '/chat/completions' };
  if (!Object.hasOwn(routes, envelope.action) || Object.keys(envelope).sort().join(',') !== 'action,body' ||
      !envelope.body || typeof envelope.body !== 'object' || Array.isArray(envelope.body)) {
    await failure(400, 'invalid_request'); return;
  }
  const configuredBase = process.env.AGENT_PROVIDER_BASE_URL?.trim();
  const base = configuredBase ? configuredBase.replace(/\/+$/, '') :
    (await fs.readFile('/var/run/secrets/coless.cn/baseUrl', 'utf8')).trim().replace(/\/+$/, '') + '/ModelApi';
  const key = process.env.AGENT_PROVIDER_API_KEY ||
    (await fs.readFile('/var/run/secrets/coless.cn/accessToken', 'utf8')).trim();
  if (!key) throw new Error('provider unavailable');
  const response = await fetch(base + routes[envelope.action], {
    method: 'POST', headers: { authorization: 'Bearer ' + key, 'content-type': 'application/json' },
    body: JSON.stringify(envelope.body), signal: controller.signal, redirect: 'error'
  });
  if (!response.ok) {
    const code = await upstreamErrorCode(response);
    await prelude(response.status, { 'content-type': 'application/json' });
    await write(JSON.stringify({ error: { message: 'Siigo upstream request failed', type: code, code } }));
    return;
  }
  await prelude(response.status, safeHeaders(response));
  if (response.body) for await (const chunk of response.body) await write(chunk);
}
main().catch(async () => {
  controller.abort();
  if (!started && !outputClosed) {
    try { await failure(timedOut ? 504 : 502, timedOut ? 'upstream_timeout' : 'upstream_error'); }
    catch { process.exitCode = 1; }
  } else process.exitCode = 1;
}).finally(() => { clearTimeout(timer); });
