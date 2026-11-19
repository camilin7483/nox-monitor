'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { listModels, generate, generateReply } = require('./assistant');

const messages = [
  { role: 'system', content: 'Responde brevemente.' },
  { role: 'user', content: 'Hola' },
  { role: 'assistant', content: 'Hola, ¿qué tal?' },
  { role: 'user', content: '¿Funciona?' },
];

function reply(body, status = 200) {
  return Response.json(body, { status });
}

function mockProvider(t, handler) {
  return t.mock.method(globalThis, 'fetch', async (url, options) => {
    assert.equal(options.redirect, 'error');
    return handler(url, options, options.body && JSON.parse(options.body));
  });
}

test('Ollama normalizes pasted endpoints and lists only local chat models in automatic mode', async t => {
  const profile = { kind: 'Ollama', url: 'http://127.0.0.1:11434/api/tags', model: 'qwen2.5:3b' };
  mockProvider(t, (url, options, body) => {
    if (options.method === 'GET') {
      assert.equal(url, 'http://127.0.0.1:11434/api/tags');
      return reply({ models: [
        { model: 'qwen2.5:3b' },
        { model: 'nomic-embed-text:latest' },
        { model: 'kimi:cloud' },
        { model: 'remote-custom', remote_host: 'https://ollama.com' },
      ] });
    }
    assert.equal(url, 'http://127.0.0.1:11434/api/chat');
    assert.deepEqual(body.messages, messages);
    assert.equal(body.stream, false);
    return reply({ message: { content: ' Funciona ' }, done_reason: 'stop' });
  });
  assert.deepEqual(await listModels(profile, { localOnly: true }), ['qwen2.5:3b']);
  assert.deepEqual(await listModels(profile), ['kimi:cloud', 'qwen2.5:3b', 'remote-custom']);
  assert.equal(await generate(profile, messages), 'Funciona');
});

test('Gemini uses its native catalogue, pagination, API key header and text generation', async t => {
  const profile = { kind: 'Gemini', url: 'https://generativelanguage.googleapis.com/v1beta/openai', model: 'models/gemini-test', key: 'synthetic' };
  mockProvider(t, (url, options, body) => {
    assert.equal(options.headers['x-goog-api-key'], 'synthetic');
    assert.equal(options.headers.Authorization, undefined);
    if (url.endsWith('/models')) {
      return reply({ models: [
        { name: 'models/gemini-test', supportedGenerationMethods: ['generateContent'] },
        { name: 'models/embedding-test', supportedGenerationMethods: ['embedContent'] },
      ], nextPageToken: 'page/2' });
    }
    if (url.endsWith('/models?pageToken=page%2F2')) {
      return reply({ models: [{ name: 'models/gemini-other', supportedGenerationMethods: ['generateContent'] }] });
    }
    assert.equal(url, 'https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent');
    assert.deepEqual(body.systemInstruction, { parts: [{ text: messages[0].content }] });
    assert.deepEqual(body.contents.map(turn => turn.role), ['user', 'model', 'user']);
    return reply({ candidates: [{ content: { parts: [{ thought: true, text: 'Private reasoning' }, { text: 'Correcto' }] } }] });
  });
  assert.deepEqual(await listModels(profile), ['gemini-other', 'gemini-test']);
  assert.equal(await generate(profile, messages), 'Correcto');
});

test('Anthropic sends system instructions separately and follows cursor pagination', async t => {
  const profile = { kind: 'Anthropic', url: 'https://api.anthropic.com', model: 'claude-test', key: 'synthetic' };
  mockProvider(t, (url, options, body) => {
    assert.equal(options.headers['x-api-key'], 'synthetic');
    assert.equal(options.headers['anthropic-version'], '2023-06-01');
    if (url.endsWith('/models')) return reply({ data: [{ id: 'claude-test' }], has_more: true, last_id: 'cursor' });
    if (url.endsWith('/models?after_id=cursor')) return reply({ data: [{ id: 'claude-next' }], has_more: false });
    assert.equal(url, 'https://api.anthropic.com/v1/messages');
    assert.equal(body.system, messages[0].content);
    assert.deepEqual(body.messages, messages.slice(1));
    return reply({ content: [{ type: 'thinking', thinking: 'reasoning' }, { type: 'text', text: 'Sí' }] });
  });
  assert.deepEqual(await listModels(profile), ['claude-next', 'claude-test']);
  assert.equal(await generate(profile, messages), 'Sí');
});

test('OpenAI uses Responses when the API explicitly reports Chat Completions is incompatible', async t => {
  const profile = { kind: 'OpenAI', url: 'https://api.openai.com/v1/models', model: 'responses-only', key: 'synthetic' };
  mockProvider(t, (url, options, body) => {
    assert.equal(options.headers.Authorization, 'Bearer synthetic');
    if (url.endsWith('/chat/completions')) {
      return reply({ error: { message: 'This is not a chat model. Use the Responses API.' } }, 404);
    }
    assert.equal(url, 'https://api.openai.com/v1/responses');
    assert.equal(body.store, false);
    return reply({ output: [
      { type: 'reasoning', content: [{ type: 'output_text', text: 'private' }] },
      { type: 'message', content: [{ type: 'output_text', text: 'Sí' }] },
    ] });
  });
  assert.equal(await generate(profile, messages), 'Sí');
});

test('OpenAI can fall back to legacy max_tokens for an explicitly incompatible endpoint', async t => {
  const profile = { kind: 'OpenAI', url: 'http://127.0.0.1:8000/v1', model: 'old-chat' };
  const fetch = mockProvider(t, (url, options, body) => {
    if (body.max_completion_tokens) {
      return reply({ error: { code: 'unsupported_parameter', param: 'max_completion_tokens' } }, 400);
    }
    assert.equal(body.max_tokens, 768);
    return reply({ choices: [{ message: { content: 'Sí' } }] });
  });
  assert.equal(await generate(profile, messages), 'Sí');
  assert.equal(fetch.mock.callCount(), 2);
});

for (const kind of ['LM Studio', 'DeepSeek', 'OpenRouter', 'Compatible con OpenAI']) {
  test(`${kind} discovers text models and reads compatible message parts`, async t => {
    const profile = { kind, url: 'http://127.0.0.1:1234/v1/chat/completions', model: 'chat-1' };
    mockProvider(t, (url, options, body) => {
      if (options.method === 'GET') {
        assert.equal(url, 'http://127.0.0.1:1234/v1/models');
        return reply({ data: [
          { id: 'chat-1' },
          { id: 'visual-only', architecture: { output_modalities: ['image'] } },
          { id: 'text-embedding-nomic-embed-text-v1.5' },
        ] });
      }
      assert.equal(url, 'http://127.0.0.1:1234/v1/chat/completions');
      assert.equal(body.max_tokens, 768);
      return reply({ choices: [{ message: { content: [{ type: 'text', text: 'Sí' }] } }] });
    });
    assert.deepEqual(await listModels(profile), ['chat-1']);
    assert.equal(await generate(profile, messages), 'Sí');
  });
}

test('Provider errors classify authentication without leaking echoed input or retrying', async t => {
  const profile = { kind: 'OpenAI', url: 'https://api.openai.com/v1', model: 'test', key: 'invalid-secret' };
  const fetch = mockProvider(t, () => reply({ error: { message: 'invalid-secret' } }, 401));
  await assert.rejects(generate(profile, messages), error =>
    error.code === 'authentication' && error.status === 401 && !JSON.stringify(error).includes('invalid-secret'));
  assert.equal(fetch.mock.callCount(), 1);
});

test('Model exhaustion surfaces an error instead of returning a truncated response', async t => {
  const profile = { kind: 'OpenRouter', url: 'https://openrouter.ai/api/v1', model: 'test' };
  mockProvider(t, () => reply({ choices: [{ finish_reason: 'length', message: { content: 'Respuesta parcial' } }] }));
  await assert.rejects(generate(profile, messages), { code: 'truncated' });
});

test('Credentials require HTTPS off loopback and cannot be smuggled into URLs or headers', async t => {
  const fetch = mockProvider(t, () => { throw new Error('No debería hacer una solicitud'); });
  await assert.rejects(listModels({ kind: 'Ollama', url: 'http://192.168.0.7:11434', key: 'secret' }), { code: 'insecure_url' });
  await assert.rejects(listModels({ kind: 'OpenAI', url: 'https://key@api.openai.com/v1' }), { code: 'invalid_url' });
  await assert.rejects(listModels({ kind: 'OpenAI', url: 'https://api.openai.com/v1', key: 'abc\nx-malicious:true' }), { code: 'invalid_key' });
  assert.equal(fetch.mock.callCount(), 0);
});


test('reply validation preserves short answers and meaningful labels', async t => {
  const profile = { kind: 'Ollama', url: 'http://127.0.0.1:11434', model: 'test' };
  const returned = ['Claro.', 'Sí', 'Código: ABC-463', 'Nox: 👍'];
  mockProvider(t, (url, options, body) => {
    assert.equal(body.messages.filter(item => item.role === 'system').length, 1);
    assert.match(body.messages[0].content, /máximo de 30 caracteres/);
    return reply({ message: { content: returned.shift() }, done_reason: 'stop' });
  });
  for (const text of ['Claro.', 'Sí', 'Código: ABC-463', '👍']) {
    assert.equal(await generateReply(profile, messages, { maxLength: 30 }), text);
  }
});

test('reply validation retries overlong text once with a shorter instruction', async t => {
  const profile = { kind: 'Ollama', url: 'http://127.0.0.1:11434', model: 'test' };
  let tries = 0;
  mockProvider(t, (url, options, body) => {
    if (++tries === 1) return reply({ message: { content: 'x'.repeat(61) } });
    assert.match(body.messages[0].content, /menos de 60 caracteres/);
    return reply({ message: { content: 'Una respuesta breve.' } });
  });
  assert.equal(await generateReply(profile, messages, { maxLength: 60 }), 'Una respuesta breve.');
  assert.equal(tries, 2);
});

test('a timeout consumes the shared generation budget before a retry', async t => {
  const profile = { kind: 'Ollama', url: 'http://127.0.0.1:11434', model: 'test' };
  let clock = 10000;
  t.mock.method(Date, 'now', () => clock);
  const fetch = mockProvider(t, () => {
    clock += 90000;
    return reply({ message: { content: ' ' } });
  });
  await assert.rejects(generateReply(profile, messages), error => error.code === 'timeout');
  assert.equal(fetch.mock.callCount(), 1);
});
