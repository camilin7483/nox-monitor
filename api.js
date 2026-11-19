'use strict';
/**
 * Nox API — REST HTTP server for WhatsApp Auto-Responder
 * Provides programmatic access to bot stats, messages, and management.
 * Runs on port 3456 by default.
 *
 * Usage: node api.js (standalone) or require('./api.js')({ db, stats, ... })
 */

const http = require('http');
const url = require('url');
const fs = require('fs');
const path = require('path');
const { DATA_DIR, PACKAGED } = require('./runtime-paths');
const { spawn } = require('child_process');

let port = PACKAGED ? 0 : 3456;
const API_HOST = PACKAGED ? '127.0.0.1' : process.env.NOX_API_HOST || '127.0.0.1';
// Token: prefer env, fallback to nox-config.json, then default (warn if default)
let API_TOKEN = process.env.NOX_API_TOKEN || '';
if (!API_TOKEN) {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'nox-config.json'), 'utf8'));
    API_TOKEN = cfg.api_token || '';
  } catch {}
}
if (!API_TOKEN) {
  if (PACKAGED) throw new Error('El perfil local de Nox todavía no está inicializado.');
  API_TOKEN = 'nox-token';
}
const DIR = DATA_DIR;
const STATE_FILE = path.join(DIR, 'responder-state.json');
const LOG_FILE = path.join(DIR, 'responder.log');
const LOCK_FILE = path.join(DIR, 'responder.lock');
const BRIDGE_FILE = path.join(DIR, 'bot-stats.json');
const RESPONDER_JS = path.join(__dirname, 'responder.js');

// ── shared state (injected or loaded lazily) ──
let _db = null;
let _dbInitAttempted = false;
function ensureDb() {
  if (_db) return _db;
  if (_dbInitAttempted) return _db;
  _dbInitAttempted = true;
  try {
    const ChatDatabase = require('./db.js');
    _db = new ChatDatabase();
    console.log(`[API] DB auto-loaded: ${JSON.stringify(_db.stats())}`);
  } catch (e) {
    console.log(`[API] DB auto-load failed: ${e.message}`);
    _db = null;
  }
  return _db;
}
let _stats = { ia: 0, fallback: 0, total: 0 };
let _modelStats = {};
let _plugins = [];

// ── helpers ──
function atomicWriteJson(filePath, data) {
  const tmp = filePath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  try { fs.chmodSync(tmp, 0o600); } catch {}
  fs.renameSync(tmp, filePath);
}
const _rateMap = new Map(); // ip -> {count, reset}
function isRateLimited(ip) {
  const now = Date.now();
  const entry = _rateMap.get(ip) || { count: 0, reset: now + 60000 };
  if (now > entry.reset) { entry.count = 0; entry.reset = now + 60000; }
  entry.count++;
  _rateMap.set(ip, entry);
  // limit 60 req/min per IP
  return entry.count > 60;
}
function json(res, data, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': `http://${API_HOST}:${port}`,
    'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
  });
  res.end(JSON.stringify(data, null, 2));
}

function error(res, msg, status = 400) {
  json(res, { error: msg }, status);
}

function auth(req) {
  const hdr = req.headers['authorization'] || '';
  return hdr === `Bearer ${API_TOKEN}`;
}

function requireAuth(req, res) {
  if (!auth(req)) { json(res, { error: 'Unauthorized' }, 401); return false; }
  return true;
}

function readState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return {}; }
}

function readBridge() {
  try { return JSON.parse(fs.readFileSync(BRIDGE_FILE, 'utf8')); } catch { return {}; }
}

function readLog(n = 200) {
  try {
    const lines = fs.readFileSync(LOG_FILE, 'utf8').split('\n').filter(Boolean);
    return lines.slice(-n);
  } catch { return []; }
}

function getPid() {
  try {
    const lock = JSON.parse(fs.readFileSync(LOCK_FILE, 'utf8'));
    try { process.kill(Number(lock.pid), 0); return Number(lock.pid); } catch { return null; }
  } catch { return null; }
}

// ── route handlers ──
async function handleRequest(req, res) {
  const parsed = url.parse(req.url, true);
  const pathname = parsed.pathname.replace(/\/+$/, '') || '/';
  const method = req.method.toUpperCase();

  // CORS preflight
  if (method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': `http://${API_HOST}:${port}`,
      'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    });
    return res.end();
  }

  // Simple rate limit
  const clientIp = req.socket.remoteAddress || 'unknown';
  if (isRateLimited(clientIp)) {
    return error(res, 'Rate limited', 429);
  }

  try {
    // ── Public endpoints (no auth required) ──
    if (pathname === '/api/health' && method === 'GET') {
      const pid = getPid();
      return json(res, {
        status: pid ? 'running' : 'stopped',
        pid,
        timestamp: new Date().toISOString(),
      });
    }

    // ── Authenticated endpoints ──
    if (!requireAuth(req, res)) return;

    switch (pathname) {
      // GET /api/stats — overall statistics
      case '/api/stats': {
        const state = readState();
        const bridge = readBridge();
        const pid = getPid();
        const logLines = readLog(500);
        const metricLines = logLines.filter(l => l.includes('[METRIC]'));
        const genLines = logLines.filter(l => l.includes('AI (') || l.includes('→ AI'));
        json(res, {
          pid,
          stats: _stats,
          model: bridge.model || null,
          chats: {
            total: Object.keys(state.lastSentByChat || {}).length,
            pending: Object.keys(state.pendingByChat || {}).length,
          },
          generations: {
            total: genLines.length,
            metrics: metricLines.length,
          },
        });
        break;
      }

      // GET /api/chats — list all chats with info
      case '/api/chats': {
        const state = readState();
        const bridge = readBridge();
        const bridgeChats = bridge.chats || {};
        const chats = [];
        const allNames = new Set([
          ...Object.keys(state.lastSentByChat || {}),
          ...Object.keys(state.recentRepliesByChat || {}),
          ...Object.keys(bridgeChats),
        ]);
        for (const name of allNames) {
          const bc = bridgeChats[name] || {};
          chats.push({
            name,
            language: bc.language || '?',
            topic: bc.topic || 'general',
            lastReply: bc.lastReply || '',
            lastActivity: bc.lastActivity || 0,
            pending: !!state.pendingByChat?.[name],
            cooldownUntil: state.skipUntilByChat?.[name] || 0,
            recentReplies: (state.recentRepliesByChat?.[name] || []).slice(-5),
          });
        }
        chats.sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0));
        json(res, { chats });
        break;
      }

      // GET /api/chat/:name — single chat detail
      case '/api/chat': {
        const name = parsed.query.name;
        if (!name) return error(res, 'Missing ?name= parameter');
        const state = readState();
        const bridge = readBridge();
        const bc = (bridge.chats || {})[name] || {};
        json(res, {
          name,
          language: bc.language || '?',
          topic: bc.topic || 'general',
          lastReply: bc.lastReply || '',
          lastActivity: bc.lastActivity || 0,
          pending: !!state.pendingByChat?.[name],
          recentReplies: (state.recentRepliesByChat?.[name] || []).slice(-10),
          lastSent: state.lastSentByChat?.[name] || '',
        });
        break;
      }

      // GET /api/logs?n=100&filter=ai — tail logs
      case '/api/logs': {
        const n = Math.min(parseInt(parsed.query.n) || 100, 1000);
        const filt = (parsed.query.filter || '').toLowerCase();
        let lines = readLog(n);
        if (filt === 'ai') lines = lines.filter(l => /AI \(|✓ |\[TOPIC\]|\[QWEN|VALIDATION/.test(l));
        else if (filt === 'error') lines = lines.filter(l => /✗|Error|FATAL|rechazada/.test(l));
        else if (filt === 'metric') lines = lines.filter(l => l.includes('[METRIC]'));
        else if (filt === 'skip') lines = lines.filter(l => /↩ |○ /.test(l));
        json(res, { lines, count: lines.length });
        break;
      }

      // GET /api/memory?name=X — get chat memories and recent messages
      case '/api/memory': {
        const name = parsed.query.name;
        if (!name) return error(res, 'Missing ?name=');
        ensureDb();
        if (!_db) return error(res, 'DB not available', 503);
        const memories = _db.getMemories ? _db.getMemories(name) : [];
        const messages = _db.getRecentMessages ? _db.getRecentMessages(name, 30) : [];
        const summary = _db.getSummary ? _db.getSummary(name) : '';
        const corrections = _db.getActiveCorrections ? _db.getActiveCorrections(name) : [];
        const reminders = _db.getPendingReminders ? _db.getPendingReminders(name) : [];
        json(res, { name, memories, messages, summary, corrections, reminders });
        break;
      }

      // POST /api/send — send a message via bot (requires bot to be running)
      case '/api/send': {
        if (method !== 'POST') return error(res, 'Method not allowed', 405);
        let body = '';
        await new Promise(resolve => {
          req.on('data', chunk => { body += chunk; });
          req.on('end', resolve);
        });
        if (body.length > 10000) return error(res, 'Payload too large', 413);
        let data;
        try { data = JSON.parse(body); } catch { return error(res, 'Invalid JSON'); }
        if (!data || !data.chat || !data.message) return error(res, 'Missing chat or message');
        if (typeof data.chat !== 'string' || typeof data.message !== 'string') return error(res, 'Invalid types');
        if (!data.chat.trim() || !data.message.trim()) return error(res, 'Missing chat or message');
        if (data.chat.length > 100 || data.message.length > 2000) return error(res, 'Chat or message too long');
        // Write to a "send queue" file that the bot picks up — atomic
        const sendQueue = path.join(DIR, 'send-queue.json');
        let queue = [];
        try { queue = JSON.parse(fs.readFileSync(sendQueue, 'utf8') || '[]'); if (!Array.isArray(queue)) queue = []; } catch {}
        if (queue.length > 100) return error(res, 'Queue full, try later', 429);
        queue.push({ chat: data.chat.trim(), message: data.message.trim().slice(0, 600), createdAt: Date.now(), id: Date.now() + Math.random().toString(36).slice(2, 6) });
        try { atomicWriteJson(sendQueue, queue); } catch (e) { return error(res, 'Failed to queue: ' + e.message, 500); }
        json(res, { queued: true, item: queue[queue.length - 1] });
        break;
      }

      // GET /api/export/:name — export chat as markdown
      case '/api/export': {
        const name = parsed.query.name;
        if (!name) return error(res, 'Missing ?name=');
        if (typeof name !== 'string' || name.length > 100) return error(res, 'Invalid name', 400);
        ensureDb();
        if (!_db || !_db.exportChatToMarkdown) return error(res, 'DB or export not available', 503);
        const md = _db.exportChatToMarkdown(name);
        if (!md) return error(res, 'Chat not found', 404);
        res.writeHead(200, {
          'Content-Type': 'text/markdown; charset=utf-8',
          'Content-Disposition': `attachment; filename="${name.replace(/[^a-zA-Z0-9]/g, '_')}.md"`,
          'Access-Control-Allow-Origin': `http://${API_HOST}:${port}`,
        });
        res.end(md);
        break;
      }

      // GET /api/bridge — raw bridge data
      case '/api/bridge': {
        json(res, readBridge());
        break;
      }

      // POST /api/restart — restart the bot
      case '/api/restart': {
        if (method !== 'POST') return error(res, 'Method not allowed', 405);
        // El reinicio puede detener este proceso. El hijo espera a que se acuse recibo.
        const [executable, args] = PACKAGED
          ? [process.env.NOX_PYTHON_EXECUTABLE, [path.join(__dirname, 'runtime_control.py'), 'restart']]
          : ['bash', ['-c', 'sleep 1 && bash ./stop.sh && exec bash ./start.sh']];
        const child = spawn(executable, args, { cwd: DIR, detached: true, stdio: 'ignore', windowsHide: true });
        child.once('error', () => json(res, { restarted: false, error: 'Failed to schedule restart' }, 500));
        child.once('spawn', () => {
          child.unref();
          json(res, { scheduled: true }, 202);
        });
        break;
      }

      // GET /api/config — read current config from responder.js + nox-config.json + .env file
      case '/api/config': {
        const js = fs.readFileSync(RESPONDER_JS, 'utf8');
        const extract = (re) => { const m = js.match(re); return m ? m[1] : '?'; };
        let nox = {};
        try { nox = JSON.parse(fs.readFileSync(path.join(DIR, 'nox-config.json'), 'utf8')); } catch {}
        const readDotEnv = (name) => {
          try {
            const content = fs.readFileSync(path.join(DIR, '.env'), 'utf8');
            const m = content.match(new RegExp(`^${name}=(.*)$`, 'm'));
            return m ? m[1].trim() : '';
          } catch { return ''; }
        };
        json(res, {
          model: extract(/const OLLAMA_MODEL = '([^']+)'/),
          modelFast: extract(/const OLLAMA_MODEL_FAST = '([^']+)'/),
          maxReplyLength: PACKAGED && nox.max_reply_length || extract(/const MAX_REPLY_LENGTH = (\d+)/),
          pollMs: PACKAGED && nox.poll_ms || extract(/const POLL_MS = (\d+)/),
          temperature: extract(/temperature:\s*([\d.]+)/),
          numPredict: extract(/num_predict:\s*(\d+)/),
          lmstudio: {
            url: readDotEnv('LMSTUDIO_URL') || nox.llm?.baseUrl || 'http://127.0.0.1:1234/v1',
            model: readDotEnv('LMSTUDIO_MODEL') || nox.llm?.model || 'qwen2.5-coder-1.5b-unsensored-dpo',
          },
          browser: {
            provider: readDotEnv('BROWSER_PROVIDER') || nox.browser?.provider || 'cdp',
            cdpEndpoint: readDotEnv('BROWSER_CDP_ENDPOINT') || nox.browser?.cdpEndpoint || 'http://127.0.0.1:9222',
          },
          persona: readDotEnv('PERSONA') || nox.persona || 'sheldon',
        });
        break;
      }

      default:
        json(res, {
          endpoints: [
            'GET /api/health',
            'GET /api/stats',
            'GET /api/chats',
            'GET /api/chat?name=X',
            'GET /api/logs?n=100&filter=ai',
            'GET /api/memory?name=X',
            'GET /api/bridge',
            'GET /api/config',
            'GET /api/export?name=X',
            'POST /api/send',
            'POST /api/restart',
          ],
        });
    }
  } catch (e) {
    error(res, e.message, 500);
  }
}

// ── plugin hook callers ──
function getPlugins() { return _plugins; }
function setPlugins(plugins) { _plugins = plugins; }

// ── start server ──
function startServer(opts = {}) {
  if (opts.db) _db = opts.db;
  else ensureDb();
  if (opts.stats) _stats = opts.stats;
  if (opts.modelStats) _modelStats = opts.modelStats;

  const server = http.createServer(handleRequest);
  server.on('error', e => {
    console.error(`[API] disabled: ${e.code || 'ERR'} ${e.message}`);
  });
  server.listen(port, API_HOST, () => {
    port = server.address().port;
    if (PACKAGED) atomicWriteJson(path.join(DATA_DIR, 'api-status.json'), { pid: process.pid, port });
    console.log(`[API] Nox REST server running on http://${API_HOST}:${port}`);
    if (API_TOKEN === 'nox-token') {
      console.log(`[API] WARNING: using default token, set NOX_API_TOKEN env var`);
    } else {
      console.log(`[API] Auth configured`);
    }
  });
  return server;
}

// ── standalone mode ──
if (require.main === module) {
  const server = startServer();
  process.on('SIGINT', () => { server.close(); process.exit(0); });
  process.on('SIGTERM', () => { server.close(); process.exit(143); });
}

module.exports = { startServer, getPlugins, setPlugins };
