const { chromium } = require('playwright');
const fs = require('fs');
const { createHash } = require('node:crypto');
const path = require('path');
const ChatDatabase = require('./db.js');
const { loadPlugins, runOnMessageHooks, runOnResponseHooks, getPluginRoutes } = require('./plugins/index.js');
const ApiServer = require('./api.js');
const assistant = require('./assistant.js');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { DATA_DIR, PACKAGED } = require('./runtime-paths');


const PROFILE_DIR = path.join(DATA_DIR, 'chrome-data');
const AI_STATUS_FILE = path.join(DATA_DIR, 'ai-status.json');
const BROWSER_STATUS_FILE = path.join(DATA_DIR, 'browser-status.json');
const WHATSAPP_URL = 'https://web.whatsapp.com/';
const LOG_FILE = path.join(DATA_DIR, 'responder.log');
const STATE_FILE = path.join(DATA_DIR, 'responder-state.json');
const LOCK_FILE = path.join(DATA_DIR, 'responder.lock');
const SEND_QUEUE_FILE = path.join(DATA_DIR, 'send-queue.json');
const POLL_MS = 1500;
const RESTART_DELAY_MS = 5000;
const CHAT_SETTLE_MS = 1200;
const CONTEXT_MESSAGE_LIMIT = 10;
const OLLAMA_URL = 'http://127.0.0.1:11434';
const OLLAMA_MODEL = 'qwen2.5:7b';        // Primary - better quality
const OLLAMA_MODEL_FAST = 'qwen2.5:3b';   // Fallback - faster
const OLLAMA_WHISPER = 'whisper';          // Audio transcription (via faster-whisper Python)
const OLLAMA_WHISPER_PY = path.join(__dirname, 'transcribe.py');
// Preferencias locales para la selección automática de modelos.
const LMSTUDIO_URL = loadEnvVar('LMSTUDIO_URL') || loadNoxConfigValue('llm.baseUrl', 'http://127.0.0.1:1234/v1');
const LMSTUDIO_MODEL = loadEnvVar('LMSTUDIO_MODEL') || loadNoxConfigValue('llm.model', 'dolphin3.0-qwen2.5-1.5b');
const LMSTUDIO_API_KEY = loadEnvVar('LMSTUDIO_API_KEY');
// ── Browser provider (bundled chromium vs existing Chrome via CDP) ──
const BROWSER_PROVIDER = loadEnvVar('BROWSER_PROVIDER') || loadNoxConfigValue('browser.provider', 'bundled');
const BROWSER_CDP_ENDPOINT = loadEnvVar('BROWSER_CDP_ENDPOINT') || loadNoxConfigValue('browser.cdpEndpoint', 'http://127.0.0.1:9222');
const personalizations = path.join(DATA_DIR, 'local-personalization.js');
const SPECIAL_CHATS = !PACKAGED && fs.existsSync(personalizations) ? require(personalizations) : {};
// ── Env loader (reads .env next to this file) ──
function loadEnvVar(name) {
  try {
    const envPath = path.join(DATA_DIR, '.env');
    if (!fs.existsSync(envPath)) return '';
    const content = fs.readFileSync(envPath, 'utf8');
    const match = content.match(new RegExp(`^${name}=(.*)$`, 'm'));
    return match ? match[1].trim() : '';
  } catch (e) {
    return '';
  }
}
function loadNoxConfigValue(dottedKey, fallback = '') {
  try {
    const cfgPath = path.join(DATA_DIR, 'nox-config.json');
    if (!fs.existsSync(cfgPath)) return fallback;
    const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
    const parts = String(dottedKey).split('.');
    let cur = cfg;
    for (const p of parts) {
      if (cur && typeof cur === 'object' && p in cur) cur = cur[p];
      else return fallback;
    }
    if (cur === undefined || cur === null || cur === '') return fallback;
    return String(cur);
  } catch {
    return fallback;
  }
}
function isLoopbackHttpUrl(u) {
  try {
    const parsed = new URL(u);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
    return parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost';
  } catch { return false; }
}
const UNREAD_TAB_RE = /Unread|No leídos|No leidos/i;
const AFTER_SEND_COOLDOWN_MS = 60000;
const ERROR_COOLDOWN_MS = 15000;
const PENDING_RETRY_MS = 20000;
const MAX_PENDING_AGE_MS = 30 * 60 * 1000;
const MAX_PENDING_ATTEMPTS = 3;
const MAX_SEND_QUEUE_AGE_MS = 30 * 60 * 1000;
const MAX_SEND_QUEUE_ATTEMPTS = 5;
const MAX_REPLY_LENGTH = 600;
const ANTI_REPETITION_DEPTH = 5;
const LOG_SILENT_CYCLES = 30;

// ── Database (initialized in main()) ──
let db;
let closeBrowser = null;
let browserState;
let activePlugins = [];
const TOPIC_KEYWORDS = {
  trabajo: ['trabajo', 'oficina', 'intranet', 'documento', 'proyecto', 'reunión', 'reunion', 'tarea'],
  personal: ['como estás', 'como estas', 'bien', 'mal', 'familia', 'casa', 'salud'],
  tecnico: ['código', 'codigo', 'programar', 'bug', 'error', 'servidor', 'web', 'app'],
  planes: ['plan', 'salir', 'finde', 'semana', 'mañana', 'hoy', 'noche'],
  humor: ['jaja', 'jeje', 'lol', 'chiste', 'risa', '😂', '😅', '🤣'],
};

// ── Language patterns ──
const ENGLISH_INDICATORS = [
  'hello', 'hi ', 'hey', 'how are you', 'what\'s up', 'good morning', 'good night',
  'thanks', 'thank you', 'please', 'sorry', 'yes', 'no', 'ok', 'okay',
  'i ', 'you ', 'we ', 'they ', 'my ', 'your ', 'his ', 'her ',
  'the ', 'this ', 'that ', 'is ', 'are ', 'was ', 'were ',
  'bro', 'dude', 'man', 'guys', 'nice', 'cool', 'awesome',
];
const SPANISH_INDICATORS = [
  'hola', 'como estás', 'como estas', 'qué tal', 'que tal', 'bien', 'gracias',
  'por favor', 'lo siento', 'sisas', 'parce', 'wey', 'weón', 'weon', 'chao',
  'qué', 'que', 'quién', 'quien', 'cómo', 'como', 'dónde', 'donde', 'por qué',
  'entonces', 'pues', 'vale', 'oye', 'mira', 'sabes', 'pana', 'mano', 'hermano',
  'nada', 'todo', 'si', 'no', 'ya', 'ahí', 'ahi', 'allá', 'alla',
  'mañana', 'hoy', 'anoche', 'rato', 'vez', 'cosa', 'gusta', 'quiero',
  'jajaja', 'jaja', 'jeje', 'xd', 'pq', 'xq', 'porque', 'también',
  'tambien', 'bueno', 'listo', 'dale', 'hagale', 'siga', 'entonces',
  'claro', 'obvio', 'cierto', 'exacto', 'mejor', 'peor', 'igual',
];

// ── Rate limiting ──
const RATE_WINDOW_MS = 120000;  // 2 min window
const RATE_MAX_PER_CHAT = 4;    // max 4 replies per chat per window
const chatRateCounts = new Map();  // chatName -> [{timestamp}, ...]

// ── Adaptive cooldown ──
const COOLDOWN_ACTIVE_MS = 20000;   // short cooldown when in active convo
const COOLDOWN_DEFAULT_MS = 60000;  // normal cooldown
const COOLDOWN_LONG_MS = 300000;    // 5 min after inactivity
const POLL_INTERVAL = configuredNumber('poll_ms', POLL_MS);
const REPLY_LIMIT = configuredNumber('max_reply_length', MAX_REPLY_LENGTH);
const NORMAL_COOLDOWN = configuredNumber('cooldown', COOLDOWN_DEFAULT_MS);

function configuredNumber(key, fallback) {
  if (!PACKAGED) return fallback;
  const value = Number(loadNoxConfigValue(key, fallback));
  return Number.isSafeInteger(value) && value > 0 && value <= 10_000_000 ? value : fallback;
}

const lastActivityByChat = new Map();  // chatName -> timestamp

const repliedIncomingByChat = new Map();
const lastSentByChat = new Map();
const recentRepliesByChat = new Map();
const skipUntilByChat = new Map();
const pendingByChat = new Map();

const stats = { ia: 0, fallback: 0, total: 0 };
const conversationTopics = new Map();
const genTimesByChat = new Map();

function logMetric(key, dataStr) {
  log(`[METRIC] ${key} ${dataStr}`);
}

const BRIDGE_FILE = path.join(DATA_DIR, 'bot-stats.json');

function atomicWriteJson(filePath, data) {
  const tmp = filePath + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  try { fs.chmodSync(tmp, 0o600); } catch {}
  fs.renameSync(tmp, filePath);
}

function atomicWriteText(filePath, content, mode = 0o600) {
  const tmp = filePath + '.tmp';
  fs.writeFileSync(tmp, content);
  try { fs.chmodSync(tmp, mode); } catch {}
  fs.renameSync(tmp, filePath);
}

function writeMonitorBridge(genInfo = {}) {
  try {
    // Preserve existing bridge fields that monitor may have written (moderator approvals)
    let existing = {};
    try { existing = JSON.parse(fs.readFileSync(BRIDGE_FILE, 'utf8')); } catch {}
    const bridge = {
      timestamp: new Date().toISOString(),
      stats: { ia: stats.ia, fallback: stats.fallback, total: stats.total },
      lastGen: genInfo.chat ? genInfo : null,
      model: {
        primary: Object.values(modelStats).at(-1)?.model || LMSTUDIO_MODEL,
        profiles: configuredModelStats(),
        fast: OLLAMA_MODEL_FAST,
        browser: typeof BROWSER_PROVIDER !== 'undefined' ? BROWSER_PROVIDER : 'cdp',
        stats: {
          'lmstudio': (() => { const s = modelStats[LMSTUDIO_MODEL] || { tries: 0, success: 0, timeMs: [] }; return { tries: s.tries, success: s.success, avgMs: s.timeMs.length > 0 ? Math.round(s.timeMs.reduce((a,b) => a+b, 0) / s.timeMs.length) : 0 }; })(),
          '7b': (() => { const s = modelStats['qwen2.5:7b'] || { tries: 0, success: 0, timeMs: [] }; return { tries: s.tries, success: s.success, avgMs: s.timeMs.length > 0 ? Math.round(s.timeMs.reduce((a,b) => a+b, 0) / s.timeMs.length) : 0 }; })(),
          '3b': (() => { const s = modelStats['qwen2.5:3b'] || { tries: 0, success: 0, timeMs: [] }; return { tries: s.tries, success: s.success, avgMs: s.timeMs.length > 0 ? Math.round(s.timeMs.reduce((a,b) => a+b, 0) / s.timeMs.length) : 0 }; })(),
          'deepseek': (() => { const s = Object.values(modelStats).find(record => record.provider === 'DeepSeek') || { tries: 0, success: 0, timeMs: [] }; return { tries: s.tries, success: s.success, avgMs: s.timeMs.length > 0 ? Math.round(s.timeMs.reduce((a,b) => a+b, 0) / s.timeMs.length) : 0 }; })(),
        },
      },
      // Preserve pendingApprovals and moderator approval fields from existing bridge
      pendingApprovals: Array.isArray(existing.pendingApprovals) ? existing.pendingApprovals : [],
      _approveChat: existing._approveChat || null,
      _approveReply: existing._approveReply || null,
      _approveError: existing._approveError || null,
      _approveAttempts: existing._approveAttempts || 0,
      chats: {},
    };
    // Build per-chat summary from state
    const now = Date.now();
    for (const [name, lastReply] of lastSentByChat) {
      const recent = recentRepliesByChat.get(name) || [];
      const lastAct = lastActivityByChat.get(name) || 0;
      const topic = conversationTopics.get(name) || 'general';
      bridge.chats[name] = {
        lastReply: (typeof lastReply === 'string' ? lastReply : '').substring(0, 80),
        language: '?',
        topic,
        lastActivity: lastAct,
        recentReplies: recent.slice(-3),
      };
    }
    // Bridge language from DB if available (overrides default '?' if detected)
    try {
      const allChats = db.getAllChats();
      for (const chat of allChats) {
        if (bridge.chats[chat.name] && chat.language) {
          bridge.chats[chat.name].language = chat.language;
        }
      }
    } catch {}
    atomicWriteJson(BRIDGE_FILE, bridge);
  } catch {}
}

function detectTopic(messages) {
  const text = messages.join(' ').toLowerCase();
  let bestTopic = 'general';
  let bestScore = 0;
  for (const [topic, keywords] of Object.entries(TOPIC_KEYWORDS)) {
    let score = 0;
    for (const kw of keywords) {
      if (text.includes(kw)) score++;
    }
    if (score > bestScore) { bestScore = score; bestTopic = topic; }
  }
  return bestTopic;
}

function detectLanguage(messages) {
  // Only check last message to avoid pollution from history
  const check = messages.slice(-1).join(' ').toLowerCase();
  if (!check.trim()) return 'es';  // default to Spanish
  let enScore = 0, esScore = 0;
  for (const w of ENGLISH_INDICATORS) { if (check.includes(w)) enScore++; }
  for (const w of SPANISH_INDICATORS) { if (check.includes(w)) esScore++; }
  // Require English majority to label as English; default Spanish
  if (enScore === 0 && esScore === 0) return 'es';
  return enScore > esScore ? 'en' : 'es';
}

function replyRateLimitUntil(chatName) {
  const now = Date.now();
  const entries = (chatRateCounts.get(chatName) || []).filter(time => now - time < RATE_WINDOW_MS);
  chatRateCounts.set(chatName, entries);
  return entries.length >= RATE_MAX_PER_CHAT ? entries[0] + RATE_WINDOW_MS : 0;
}

function computeCooldown(chatName) {
  const lastAct = lastActivityByChat.get(chatName) || 0;
  const elapsed = Date.now() - lastAct;
  if (elapsed < 300000) return COOLDOWN_ACTIVE_MS;  // active in last 5 min
  if (elapsed < 3600000) return NORMAL_COOLDOWN; // within 1 hour
  return COOLDOWN_LONG_MS;  // cold chat
}

function safeAppendLog(line) {
  try {
    fs.appendFileSync(LOG_FILE, line + '\n');
    try { fs.chmodSync(LOG_FILE, 0o600); } catch {}
  } catch {}
}

function log(msg) {
  const line = `[${new Date().toISOString()}] ${msg}`;
  try { console.log(line); } catch {}
  // Rotate if >5MB (keep 3 backups) then append — single write
  try {
    try {
      const st = fs.statSync(LOG_FILE);
      if (st.size > 5 * 1024 * 1024) {
        const bak1 = LOG_FILE + '.1';
        const bak2 = LOG_FILE + '.2';
        const bak3 = LOG_FILE + '.3';
        try { if (fs.existsSync(bak2)) fs.renameSync(bak2, bak3); } catch {}
        try { if (fs.existsSync(bak1)) fs.renameSync(bak1, bak2); } catch {}
        try { fs.renameSync(LOG_FILE, bak1); } catch {}
      }
    } catch {}
    fs.appendFileSync(LOG_FILE, line + '\n');
    try { fs.chmodSync(LOG_FILE, 0o600); } catch {}
  } catch {}
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function processIsAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; }
  catch { return false; }
}

function releaseLock() {
  try {
    const lock = JSON.parse(fs.readFileSync(LOCK_FILE, 'utf8'));
    if (lock.pid === process.pid) fs.unlinkSync(LOCK_FILE);
  } catch {}
}

function acquireLock() {
  // Atomic lock using O_EXCL to avoid TOCTOU
  const startedAt = new Date().toISOString();
  const lockData = JSON.stringify({ pid: process.pid, startedAt });
  try {
    // Try exclusive create
    const fd = fs.openSync(LOCK_FILE, 'wx', 0o600);
    fs.writeSync(fd, lockData);
    fs.closeSync(fd);
  } catch (e) {
    if (e.code === 'EEXIST') {
      // Check if stale
      try {
        const existing = JSON.parse(fs.readFileSync(LOCK_FILE, 'utf8'));
        const pid = Number(existing.pid);
        if (processIsAlive(pid)) {
          // Verify it's actually our responder (check cmdline to avoid PID recycling)
          try {
            const cmd = fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8');
            if (!cmd.includes('responder.js')) {
              // PID recycled, stale lock
              fs.unlinkSync(LOCK_FILE);
              const fd2 = fs.openSync(LOCK_FILE, 'wx', 0o600);
              fs.writeSync(fd2, lockData);
              fs.closeSync(fd2);
            } else {
              throw new Error(`another responder is already running with pid ${pid}`);
            }
          } catch (inner) {
            if (inner.message && inner.message.includes('another responder')) throw inner;
            // If /proc not available, fallback to kill -0 check
            throw new Error(`another responder is already running with pid ${pid}`);
          }
        } else {
          // Stale lock
          try { fs.unlinkSync(LOCK_FILE); } catch {}
          const fd2 = fs.openSync(LOCK_FILE, 'wx', 0o600);
          fs.writeSync(fd2, lockData);
          fs.closeSync(fd2);
        }
      } catch (err) {
        if (err.message && err.message.includes('another responder')) throw err;
        // If JSON parse fails, overwrite
        try { fs.unlinkSync(LOCK_FILE); } catch {}
        const fd2 = fs.openSync(LOCK_FILE, 'wx', 0o600);
        fs.writeSync(fd2, lockData);
        fs.closeSync(fd2);
      }
    } else if (e.code !== 'ENOENT' && !String(e.message || '').includes('Unexpected')) {
      throw e;
    } else {
      // ENOENT parent missing — ensure dir then retry
      try { fs.mkdirSync(path.dirname(LOCK_FILE), { recursive: true }); } catch {}
      const fd = fs.openSync(LOCK_FILE, 'wx', 0o600);
      fs.writeSync(fd, lockData);
      fs.closeSync(fd);
    }
  }
  try { fs.chmodSync(LOCK_FILE, 0o600); } catch {}
  process.once('exit', releaseLock);
  const stop = code => {
    // Dejar que Chrome vacíe la sesión persistente al pulsar Detener o Reiniciar.
    const deadline = setTimeout(() => process.exit(code), 7000);
    Promise.resolve().then(() => closeBrowser?.()).catch(() => {}).finally(() => {
      clearTimeout(deadline);
      process.exit(code);
    });
  };
  process.once('SIGINT', () => stop(130));
  process.once('SIGTERM', () => stop(143));
  if (PACKAGED) {
    const requestPath = path.join(DATA_DIR, 'stop-request.json');
    const watcher = setInterval(() => {
      try {
        const request = JSON.parse(fs.readFileSync(requestPath, 'utf8'));
        if (request.pid === process.pid && request.startedAt === startedAt) {
          clearInterval(watcher);
          stop(0);
        }
      } catch {}
    }, 180);
    watcher.unref();
  }
}

function isClosedError(error) {
  return /Target page|Target closed|context or browser has been closed|Browser has been closed|Page closed|Se cerró la página de WhatsApp/i
    .test(error?.message || '');
}

async function ensureWhatsAppPage(context) {
  let page = context.pages().find(item => !item.isClosed() && item.url().startsWith(WHATSAPP_URL));
  if (!page) {
    setBrowserState('opening');
    page = context.pages().find(item => !item.isClosed() && item.url() === 'about:blank') || await context.newPage();
    await page.goto(WHATSAPP_URL, { waitUntil: 'domcontentloaded', timeout: 30000 });
  }
  return page;
}

function loadState() {
  try {
    const state = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    for (const [name, value] of Object.entries(state.repliedIncomingByChat || {}))
      repliedIncomingByChat.set(name, value);
    for (const [name, value] of Object.entries(state.lastSentByChat || {}))
      lastSentByChat.set(name, value);
    for (const [name, value] of Object.entries(state.recentRepliesByChat || {}))
      recentRepliesByChat.set(name, value);
    for (const [name, value] of Object.entries(state.skipUntilByChat || {}))
      if (Number(value) > Date.now()) skipUntilByChat.set(name, Number(value));
    for (const [name, value] of Object.entries(state.pendingByChat || {}))
      if (!value?.createdAt || Date.now() - Number(value.createdAt) <= MAX_PENDING_AGE_MS)
        pendingByChat.set(name, value);
    for (const [name, value] of Object.entries(state.conversationTopics || {}))
      conversationTopics.set(name, value);
    for (const [name, value] of Object.entries(state.lastActivityByChat || {}))
      if (Number(value) > 0) lastActivityByChat.set(name, Number(value));
  } catch {}
}

function saveState() {
  try {
    const data = {
      repliedIncomingByChat: Object.fromEntries(repliedIncomingByChat),
      lastSentByChat: Object.fromEntries(lastSentByChat),
      recentRepliesByChat: Object.fromEntries(recentRepliesByChat),
      skipUntilByChat: Object.fromEntries(skipUntilByChat),
      pendingByChat: Object.fromEntries(pendingByChat),
      conversationTopics: Object.fromEntries(conversationTopics),
      lastActivityByChat: Object.fromEntries(lastActivityByChat),
    };
    atomicWriteJson(STATE_FILE, data);
  } catch {}
}

function cleanText(text) {
  return (text || '')
    .replace(/\u200e/g, '').replace(/\u00a0/g, ' ')
    .replace(/\s+/g, ' ').trim();
}

function cleanMessageText(text) {
  return cleanText(text)
    .replace(/\b\d{1,2}:\d{2}\s*(AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?\b/gi, '')
    .replace(/[✓✔]+/g, '')
    .replace(/\b(wds-ic-[a-z-]+|message prepared|mensaje preparado)\b/gi, '')
    .replace(/^(you|t[uú]|yo)\s*:\s*/i, '')
    .trim();
}

function isMediaOnlyMessage(text) {
  return /^(sticker|\[sticker\]|\[media\]|foto|photo|imagen|image|video|audio|gif|documento|document)$/i
    .test(cleanMessageText(text));
}

function normalizedForCompare(text) {
  return cleanMessageText(text).toLowerCase().replace(/[^a-z0-9áéíóúñ]+/gi, '');
}

function contextIncludesMessage(context, message) {
  const needle = normalizedForCompare(message);
  if (needle.length < 4) return false;
  return normalizedForCompare(context).includes(needle.slice(0, Math.min(needle.length, 60)));
}

function messageSignature(text) {
  const sig = cleanMessageText(text).slice(-160);
  return sig.replace(/[^a-z0-9áéíóúñ]/gi, '').length >= 2 ? sig : '';
}

function trackRecentReply(chatName, reply) {
  let replies = recentRepliesByChat.get(chatName) || [];
  replies.push(reply);
  if (replies.length > ANTI_REPETITION_DEPTH) replies = replies.slice(-ANTI_REPETITION_DEPTH);
  recentRepliesByChat.set(chatName, replies);
}

function getPreviewFromRowText(rowText, name) {
  const parts = String(rowText || '')
    .split(/\n+/)
    .map(cleanText)
    .filter(Boolean)
    .filter(part => part !== name)
    .filter(part => !/^\d{1,2}:\d{2}\s*(AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?$/i.test(part))
    .filter(part => !/^(yesterday|ayer|today|hoy)$/i.test(part))
    .filter(part => !/^\d+$/.test(part))
    .filter(part => !/unread messages?|mensajes? no le[ií]dos?/i.test(part))
    .filter(part => !/^(you|t[uú]|yo)\s*:?\s*$/i.test(part));
  const preview = parts.length ? parts[parts.length - 1] : cleanText(rowText).replace(name, '');
  return cleanText(preview)
    .replace(/\b\d{1,2}:\d{2}\s*(AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?\b/gi, '')
    .replace(/\b\d+\s*(unread messages?|mensajes? no le[ií]dos?)\b/gi, '')
    .replace(/\b(wds-ic-[a-z-]+|message prepared|mensaje preparado)\b/gi, '')
    .trim();
}

function cleanChatName(name) {
  return (name || '').replace(/\s+/g, ' ').trim();
}

function isGroupRow(row) {
  return row.locator('[data-testid="group-icon"]').count().then(c => c > 0);
}

async function clickUnreadTab(page) {
  const unreadTab = page.locator('[role="tab"]').filter({ hasText: UNREAD_TAB_RE }).first();
  if (await unreadTab.count() === 0) return false;
  const selected = () => unreadTab.evaluate(tab => (
    tab.getAttribute('aria-pressed') ?? tab.getAttribute('aria-selected')
  ));
  try {
    if (await selected() === 'true') return true;
    await unreadTab.click({ force: true, timeout: 5000 });
    await sleep(1500);
    return await selected() !== 'false';
  } catch {
    return false;
  }
}

async function clickAllTab(page) {
  const allTab = page.locator('[role="tab"]').filter({ hasText: /All|Todos/i }).first();
  if (await allTab.count() > 0) { await allTab.click().catch(() => {}); await sleep(500); }
}

async function getFirstUnreadRow(page, allowed) {
  const now = Date.now();
  const rows = await page.locator('[role="row"]').all();
  for (const row of rows) {
    const titleSpan = row.locator('span[title]').first();
    if (await titleSpan.count() === 0) continue;
    const name = cleanChatName(await titleSpan.getAttribute('title'));
    if (!allowed.has(name) || pendingByChat.has(name)) continue;
    if (await isGroupRow(row)) {
      skipUntilByChat.set(name, now + AFTER_SEND_COOLDOWN_MS);
      log('↩ Grupo excluido de respuestas automáticas.');
      continue;
    }
    const skipUntil = skipUntilByChat.get(name) || 0;
    if (skipUntil > now) continue;
    skipUntilByChat.delete(name);
    const rowText = await row.innerText().catch(() => '');
    const preview = getPreviewFromRowText(rowText, name);
    const lastSent = lastSentByChat.get(name);
    if (lastSent && normalizedForCompare(preview).includes(normalizedForCompare(lastSent).slice(0, 32))) continue;
    return { row, name, preview };
  }
  return null;
}

async function findVisibleChatRowByName(page, name) {
  await clickAllTab(page);
  const rows = await page.locator('[role="row"]').all();
  for (const row of rows) {
    const titleSpan = row.locator('span[title]').first();
    if (await titleSpan.count() === 0) continue;
    const rowName = cleanChatName(await titleSpan.getAttribute('title'));
    if (rowName === name) {
      const rowText = await row.innerText().catch(() => '');
      return { row, name, preview: getPreviewFromRowText(rowText, name), fromPending: true };
    }
  }
  return null;
}

async function waitForChatReady(page) {
  await page.locator('footer div[contenteditable="true"][role="textbox"]').last().waitFor({ timeout: 8000 });
  await sleep(CHAT_SETTLE_MS);
}

async function getConversationContext(page, headerName, limit) {
  const msgLimit = limit || CONTEXT_MESSAGE_LIMIT;
  const messages = await page.evaluate(({ msgLimit, headerName }) => {
    let nodes = Array.from(document.querySelectorAll('[data-pre-plain-text]'));
    if (nodes.length === 0) nodes = Array.from(document.querySelectorAll('.message-in, .message-out'));
    const selfPattern = /^(You|T[uú]|Yo|Usted)$/i;
    function messageSide(el) {
      let cur = el;
      for (let i = 0; cur && i < 8; i++, cur = cur.parentElement) {
        const cls = String(cur.className || '');
        if (/\bmessage-out\b/.test(cls)) return 'out';
        if (/\bmessage-in\b/.test(cls)) return 'in';
        if (cur.getAttribute?.('data-id')?.includes('true_')) return 'out';
        if (cur.getAttribute?.('data-id')?.includes('false_')) return 'in';
      }
      return '';
    }
    const result = [];
    for (const node of nodes.slice(-msgLimit)) {
      const container = node.closest('.message-in, .message-out, [tabindex="-1"]') || node;
      const side = messageSide(node);
      const hasIncomingClass = side === 'in';
      const hasOutgoingClass = side === 'out';
      const prePlain = node.getAttribute('data-pre-plain-text') || '';
      const senderMatch = prePlain.match(/\]\s*([^:]+):/);
      const senderName = senderMatch ? senderMatch[1].trim() : '';
      let isOutgoing = hasOutgoingClass;
      if (hasIncomingClass) {
        isOutgoing = false;
      } else if (!senderName) {
        isOutgoing = true;
      } else if (selfPattern.test(senderName)) {
        isOutgoing = true;
      } else if (headerName && senderName === headerName) {
        isOutgoing = false;
      } else {
        try {
          const rect = container.getBoundingClientRect();
          isOutgoing = rect.left > window.innerWidth / 2;
        } catch {}
      }
      const incoming = !isOutgoing;
      let text = (node.querySelector('.selectable-text')?.innerText
        || node.innerText || node.textContent || '')
        .replace(/\u200e/g, '').replace(/\u00a0/g, ' ')
        .replace(/\s+/g, ' ').replace(/\d{1,2}:\d{2}\s*(AM|PM|a\.?\s*m\.?|p\.?\s*m\.?)?/gi, '')
        .replace(/[✓✔]+/g, '').replace(/\b(wds-ic-[a-z-]+|message prepared|mensaje preparado)\b/gi, '')
        .trim();

      // Detect media types
      if (!text && node.querySelector('img, video')) text = '[media]';
      if (!text && node.querySelector('[data-icon*="sticker"]')) text = '[sticker]';
      // Detect audio messages
      const audioEl = node.querySelector('audio');
      const audioBtn = node.querySelector('[data-testid="audio-player-play-button"], [data-icon="audio"]');
      if (audioEl || audioBtn) {
        const duration = audioEl?.duration ? Math.round(audioEl.duration) : '?';
        // Try to get duration text from the node
        const durationSpan = node.querySelector('span[data-testid*="audio"], span:last-child');
        const durText = durationSpan ? durationSpan.textContent.trim() : `${duration}s`;
        text = text || `[audio: ${durText}]`;
      }

      const prefix = incoming ? 'Tú' : 'Yo';
      result.push({
        incoming,
        line: `${prefix}: ${text}`,
        id: node.closest('[data-id]')?.getAttribute('data-id') || '',
      });
    }
    return result.filter(m => m.line.replace(/^(Tú|Yo):\s*/, '').trim());
  }, { msgLimit, headerName });

  const knownOutgoing = [
    lastSentByChat.get(headerName),
    ...(recentRepliesByChat.get(headerName) || []),
  ].map(normalizedForCompare).filter(v => v.length >= 6);
  for (const m of messages) {
    const body = m.line.replace(/^(Tú|Yo):\s*/, '');
    const norm = normalizedForCompare(body);
    if (m.incoming && knownOutgoing.some(out => norm.includes(out.slice(0, Math.min(out.length, 60))))) {
      m.incoming = false;
      m.line = `Yo: ${body}`;
    }
  }

  const incoming = messages.filter(m => m.incoming);
  const last = messages.length > 0 ? messages[messages.length - 1] : null;
  let lastOutgoingIndex = -1;
  messages.forEach((m, idx) => { if (!m.incoming) lastOutgoingIndex = idx; });
  const unansweredIncoming = messages
    .slice(lastOutgoingIndex + 1)
    .filter(m => m.incoming)
    .map(m => m.line.replace(/^Tú:\s*/, ''));
  return {
    context: messages.map(m => m.line).join('\n'),
    lastIncoming: incoming.length > 0 ? incoming[incoming.length - 1].line.replace(/^Tú:\s*/, '') : '',
    lastIsIncoming: last ? last.incoming : false,
    lastMessageId: last?.id || '',
    hasIncoming: incoming.length > 0,
    unansweredIncoming,
    allIncoming: incoming.map(m => m.line.replace(/^Tú:\s*/, '')),
    turns: messages
      .map(m => ({ role: m.incoming ? 'user' : 'assistant', content: m.line.replace(/^(Tú|Yo):\s*/, '').trim() }))
      .filter(t => t.content),
  };
}

async function getMessageInput(page) {
  const footerInput = page.locator('footer div[contenteditable="true"][role="textbox"]').last();
  if (await footerInput.count() > 0) return footerInput;
  return null;
}

async function backToChatList(page) {
  await page.keyboard.press('Escape').catch(() => {});
  await sleep(600);
}

const modelStats = Object.create(null);

const defaultLocalConnections = [
  { id: 'auto-ollama', kind: 'Ollama', name: 'Ollama local', url: OLLAMA_URL,
    preferred: [OLLAMA_MODEL_FAST, OLLAMA_MODEL] },
  ...(isLoopbackHttpUrl(LMSTUDIO_URL) ? [{ id: 'auto-lmstudio', kind: 'LM Studio', name: 'LM Studio local', url: LMSTUDIO_URL,
    key: LMSTUDIO_API_KEY, preferred: [LMSTUDIO_MODEL] }] : []),
];
let localDiscovery = { expires: 0, profile: null };
let inferenceState = '';

function setInferenceState(state, profile, error = '') {
  const status = { pid: process.pid, state, provider: profile?.kind || '', model: profile?.model || '', error };
  const encoded = JSON.stringify(status);
  if (encoded === inferenceState) return;
  try {
    atomicWriteJson(AI_STATUS_FILE, status);
    inferenceState = encoded;
  } catch {
    log('[AI] No se pudo guardar el estado del modelo.');
  }
}

function allowedAutoChats(settings) {
  const names = Array.isArray(settings.allowedChats) ? settings.allowedChats : [];
  return new Set(names.filter(name => typeof name === 'string').map(cleanChatName).filter(Boolean));
}

async function resolveReplyProfile(settings) {
  const selected = assistant.activeProfile(settings);
  if (selected) return selected;
  if (localDiscovery.expires > Date.now()) return localDiscovery.profile;

  let local = null;
  for (const connection of defaultLocalConnections) {
    try {
      const models = await assistant.listModels(connection, { localOnly: true });
      const model = connection.preferred.find(name => models.includes(name)) || models[0];
      if (model) {
        local = { ...connection, model };
        break;
      }
    } catch {}
  }
  localDiscovery = { expires: Date.now() + 15000, profile: local };
  return local;
}

const profileUsage = new Map();

function trackProfileAttempt(profile) {
  const id = JSON.stringify([profile.id, profile.model]);
  if (!profileUsage.has(id)) {
    if (profileUsage.size >= 100) profileUsage.delete(profileUsage.keys().next().value);
    profileUsage.set(id, { name: profile.name, provider: profile.kind, model: profile.model, tries: 0, success: 0, timeMs: [] });
  }
  const record = profileUsage.get(id);
  modelStats[profile.model] = record;
  record.tries++;
  return record;
}

function configuredModelStats() {
  return [...profileUsage.values()].map(({ timeMs, ...record }) => ({
    ...record,
    avgMs: timeMs.length ? Math.round(timeMs.reduce((sum, ms) => sum + ms, 0) / timeMs.length) : 0,
  }));
}

async function transcribeAudio(audioPath) {
  // Safe: validate path and use spawn args to avoid shell injection
  try {
    if (!audioPath || typeof audioPath !== 'string' || audioPath.length > 500 || audioPath.includes('\n') || audioPath.includes('\0')) {
      log(`[WHISPER] invalid audioPath`);
      return null;
    }
    const { spawnSync } = require('child_process');
    const result = spawnSync('python3', [OLLAMA_WHISPER_PY, audioPath], { timeout: 60000, encoding: 'utf8', shell: false });
    if (result.error) {
      log(`[WHISPER] error: ${result.error.message}`);
      return null;
    }
    const stdout = (result.stdout || '').trim();
    if (!stdout) {
      log(`[WHISPER] empty output`);
      return null;
    }
    const data = JSON.parse(stdout);
    if (data.error) {
      log(`[WHISPER] error: ${data.error}`);
      return null;
    }
    log(`[WHISPER] Transcripción lista (${data.language}, ${Math.round(data.duration)} s).`);
    return data.text || null;
  } catch (e) {
    log(`[WHISPER] error: ${e.message}`);
    return null;
  }
}

async function buildPrompt(chatName, incomingMsgs) {
  const lang = detectLanguage(incomingMsgs);
  try {
    db.updateChat(chatName, { language: lang });
  } catch {}
  let info = '';
  try {
    const dbMemories = db.getMemoriesForPrompt(chatName);
    if (dbMemories) info += `Datos: ${dbMemories}\n`;
    const summary = db.getSummary(chatName);
    if (summary) info += `Resumen anterior: ${summary}\n`;
  } catch {}

  try {
    const corrections = db.getActiveCorrections(chatName);
    if (corrections && corrections.length > 0) {
      info += `Correcciones: ${corrections.map(c => c.pattern || c.instruction).join(' | ')}\n`;
    }
  } catch {}

  return { info: info.trim(), lang };
}

async function generateAIReply(chatName, context, incomingMsgs, isMedia, turns, settings, profile) {
  const { info, lang } = await buildPrompt(chatName, incomingMsgs);
  const recentReplies = recentRepliesByChat.get(chatName) || [];

  const topic = detectTopic(incomingMsgs);
  conversationTopics.set(chatName, topic);

  const configured = assistant.prepareContext(settings, incomingMsgs.slice(-5).join('\n'));
  let systemMsg = configured.systemPrompt || assistant.NOX_SYSTEM_PROMPT;
  if (configured.additional) systemMsg += '\n\n' + configured.additional;
  if (recentReplies.length > 0)
    systemMsg += `\nNo repitas estas respuestas recientes: ${recentReplies.slice(-3).join(' | ')}`;
  if (info) systemMsg += `\n${info}`;

  let finalText;
  if (configured.systemPrompt) {
    finalText = isMedia
      ? 'El contacto envió un archivo multimedia. Solo conoces el tipo de mensaje.'
      : incomingMsgs.slice(-5).join('\n');
  } else if (isMedia) {
    finalText = 'El contacto envió un archivo multimedia. Responde en una oración según el contexto disponible, sin inventar detalles del archivo.';
  } else {
    finalText = `Mensajes nuevos:\n${incomingMsgs.slice(-5).map(m => `- ${m || '(vacio)'}`).join('\n')}\nRespóndelos de forma natural en 1-2 oraciones.`;
  }
  if (!configured.systemPrompt) finalText += lang === 'en' ? `\nReply in English.` : `\nResponde en español.`;

  const history = (turns || [])
    .filter(t => t && t.content && String(t.content).trim())
    .slice(-8)
    .map(t => ({ role: t.role === 'assistant' ? 'assistant' : 'user', content: String(t.content).trim().slice(0, 300) }));

  const messages = [
    { role: 'system', content: systemMsg },
    ...history,
    { role: 'user', content: finalText },
  ];

  setInferenceState('testing', profile);

  for (const p of activePlugins) {
    if (p.onMessage) {
      try {
        const mod = p.onMessage({ chatName, message: incomingMsgs.join('\n'), context, db });
        if (mod) log(`🔌 ${p.name} modified message`);
      } catch (e) {
        log(`🔌 ${p.name} onMessage error: ${e.message}`);
      }
    }
  }

  const started = Date.now();
  const counter = trackProfileAttempt(profile);
  try {
    let reply = await assistant.generateReply(profile, messages, { maxLength: REPLY_LIMIT });
    for (const plugin of activePlugins) {
      if (!plugin.onResponse) continue;
      try {
        const modified = plugin.onResponse({ chatName, reply, context, db });
        if (typeof modified === 'string' && modified.trim()) {
          reply = assistant.validateReply(modified, REPLY_LIMIT);
        }
      } catch {
        log(`🔌 ${plugin.name}: no se pudo aplicar el ajuste a la respuesta.`);
      }
    }
    counter.success++;
    counter.timeMs.push(Date.now() - started);
    if (counter.timeMs.length > 100) counter.timeMs.shift();
    setInferenceState('ready', profile);
    return reply;
  } catch (error) {
    const message = error.code ? error.message : 'No se pudo preparar la respuesta de IA.';
    setInferenceState('error', profile, message);
    log(`[AI] ${message}`);
    throw error;
  }
}

function rememberPending(chat, reason = 'unread') {
  const now = Date.now();
  const existing = pendingByChat.get(chat.name) || {};
  pendingByChat.set(chat.name, {
    name: chat.name,
    preview: cleanMessageText(chat.preview || existing.preview || ''),
    signature: messageSignature(chat.preview || existing.preview || ''),
    reason,
    createdAt: existing.createdAt || now,
    attempts: existing.attempts || 0,
    providerFailures: existing.providerFailures || 0,
    recordedIncoming: existing.recordedIncoming || '',
    nextAttemptAt: existing.nextAttemptAt || now,
    lastError: existing.lastError || '',
  });
  saveState();
}

function clearPending(name) {
  if (pendingByChat.delete(name)) saveState();
}

function markPendingFailed(name, error) {
  const pending = pendingByChat.get(name);
  if (!pending) return;
  pending.attempts = Number(pending.attempts || 0) + 1;
  pending.nextAttemptAt = Date.now() + PENDING_RETRY_MS;
  pending.lastError = error?.message || String(error || 'unknown error');
  if (pending.attempts >= MAX_PENDING_ATTEMPTS || Date.now() - Number(pending.createdAt || 0) > MAX_PENDING_AGE_MS) {
    log('↩ Se agotaron los intentos de una respuesta pendiente.');
    pendingByChat.delete(name);
  } else { pendingByChat.set(name, pending); }
  saveState();
}

function deferPendingForAI(name) {
  const pending = pendingByChat.get(name);
  if (!pending) return;
  const failures = Math.min(Number(pending.providerFailures || 0) + 1, 5);
  pending.providerFailures = failures;
  pending.nextAttemptAt = Date.now() + Math.min(PENDING_RETRY_MS * (2 ** (failures - 1)), 300000);
  pending.lastError = 'Respuesta pendiente de IA.';
  saveState();
}

function getDuePending(allowed) {
  const now = Date.now();
  for (const [name, pending] of pendingByChat.entries()) {
    if (!allowed.has(name)) continue;
    if (now - Number(pending.createdAt || 0) > MAX_PENDING_AGE_MS) { pendingByChat.delete(name); saveState(); continue; }
    if (Number(pending.nextAttemptAt || 0) <= now && Number(pending.attempts || 0) < MAX_PENDING_ATTEMPTS)
      return { ...pending, name };
  }
  return null;
}



// ══════════════════════════════════════════
//  REMINDER DETECTION
// ══════════════════════════════════════════

// Rough time patterns: "mañana a las 3", "en 2 horas", "a las 5pm", "el lunes", etc.
const REMINDER_PATTERNS = [
  /\b(mañana|hoy|pasado\s*mañana)\s+(a\s+las?\s+)?(\d{1,2})(:(\d{2}))?\s*(am|pm|de\s+la\s+mañana|de\s+la\s+tarde|de\s+la\s+noche)?/i,
  /\b(en\s+)\d+\s+(minutos?|horas?|d[ií]as?)\b/i,
  /\b(a\s+las?\s+)(\d{1,2})(:(\d{2}))?\s*(am|pm)?\b/i,
  /\b(recu[eé]rdame|recordarme|no\s*olvides|av[ií]same|apunta|agenda)\b/i,
  /\b(el\s+)?(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s+(a\s+las?\s+)?(\d{1,2})(:(\d{2}))?\s*(am|pm)?/i,
];

function detectReminderIntent(text) {
  const lower = text.toLowerCase();
  // Keyword trigger: must have reminder keywords
  const hasTrigger = /\b(recu[eé]rdame|recordar|no\s*olvides|av[ií]same|apunta|agenda|recu[eé]rdate)\b/i.test(lower);
  if (!hasTrigger) return null;

  // Try to extract time
  const now = new Date();
  let targetDate = null;
  let message = text;

  // Pattern: "mañana a las X"
  const m1 = lower.match(/\b(mañana)\s+(a\s+las?\s+)?(\d{1,2})(:(\d{2}))?\s*(am|pm|de\s+la\s+mañana|de\s+la\s+tarde|de\s+la\s+noche)/i);
  if (m1) {
    targetDate = new Date(now);
    targetDate.setDate(targetDate.getDate() + 1);
    let hour = parseInt(m1[3]);
    const minute = m1[5] ? parseInt(m1[5]) : 0;
    const period = (m1[6] || '').toLowerCase();
    if (period.includes('pm') || period.includes('tarde') || period.includes('noche')) {
      if (hour < 12) hour += 12;
    } else if (period.includes('am') || period.includes('mañana')) {
      if (hour === 12) hour = 0;
    }
    targetDate.setHours(hour, minute, 0, 0);
  }

  // Pattern: "en X horas/minutos"
  if (!targetDate) {
    const m2 = lower.match(/\b(en\s+)(\d+)\s+(minutos?|horas?|d[ií]as?)\b/i);
    if (m2) {
      const num = parseInt(m2[2]);
      const unit = m2[3].toLowerCase();
      targetDate = new Date(now);
      if (unit.startsWith('hora')) targetDate.setHours(targetDate.getHours() + num);
      else if (unit.startsWith('minuto')) targetDate.setMinutes(targetDate.getMinutes() + num);
      else if (unit.startsWith('d') || unit.startsWith('dia') || unit.startsWith('día')) targetDate.setDate(targetDate.getDate() + num);
    }
  }

  // Pattern: "a las X" (today)
  if (!targetDate) {
    const m3 = lower.match(/\b(a\s+las?\s+)(\d{1,2})(:(\d{2}))?\s*(am|pm)?\b/i);
    if (m3) {
      targetDate = new Date(now);
      let hour = parseInt(m3[2]);
      const minute = m3[4] ? parseInt(m3[4]) : 0;
      const period = (m3[5] || '').toLowerCase();
      if (period === 'pm' && hour < 12) hour += 12;
      else if (period === 'am' && hour === 12) hour = 0;
      targetDate.setHours(hour, minute, 0, 0);
      if (targetDate <= now) targetDate.setDate(targetDate.getDate() + 1); // next day if past
    }
  }

  if (!targetDate) return { reminder: true, message: text, date: null, note: 'reminder detected but no time parsed' };

  return {
    reminder: true,
    message: text.replace(/^(recu[eé]rdame|av[ií]same|no\s*olvides|apunta)\s*/i, '').trim(),
    date: targetDate,
    timestamp: Math.floor(targetDate.getTime() / 1000),
    note: '',
  };
}

// ══════════════════════════════════════════
//  CORRECTION LEARNING
// ══════════════════════════════════════════

const CORRECTION_PATTERNS = [
  /no\s+(respondas|digas|conteste[s]?|escribas|uses|hables)\s+(as[ií]|eso|de\s+esa\s+forma)/i,
  /(deja\s+de|cambia\s+el\s+tono|no\s+me\s+gusta\s+como|no\s+seas)\s/i,
  /(mejor\s+di|mejor\s+responde|mejor\s+contesta)\s/i,
];

function detectCorrectionIntent(text, chatName) {
  for (const pat of CORRECTION_PATTERNS) {
    if (pat.test(text)) {
      // Extract what the correction is about
      let instruction = text.substring(0, 200);
      let pattern = '';
      // Try to identify what they don't like
      const noMatch = text.match(/no\s+(respondas|digas|conteste[s]?|escribas|uses|hables)\s+(as[ií]|eso|de\s+esa\s+forma)(.*)/i);
      if (noMatch) {
        pattern = (noMatch[3] || '').trim().substring(0, 80);
      }
      const preferMatch = text.match(/(mejor\s+di|mejor\s+responde|mejor\s+contesta)\s+(.*)/i);
      if (preferMatch) {
        instruction = preferMatch[2].trim().substring(0, 200);
      }
      return { correction: true, pattern, instruction };
    }
  }
  return null;
}

// ══════════════════════════════════════════
//  REMINDER SCHEDULER
// ══════════════════════════════════════════

let reminderInterval = null;

function startReminderScheduler(pageRef) {
  if (reminderInterval) clearInterval(reminderInterval);
  reminderInterval = setInterval(async () => {
    try {
      if (!db || !db.getDueReminders) return;
      const allowed = allowedAutoChats(assistant.readSettings());
      const due = db.getDueReminders();
      for (const r of due) {
        if (!allowed.has(cleanChatName(r.chat_name))) continue;
        try {
          // Find the chat and send the reminder
          const chatRow = await findVisibleChatRowByName(pageRef._page || pageRef, r.chat_name);
          if (chatRow) {
            await chatRow.row.click();
            await sleep(2000);
            const input = await getMessageInput(pageRef._page || pageRef);
            if (input) {
              await input.click();
              await input.fill(`⏰ Recordatorio: ${r.message}`);
              await sleep(200);
              await pageRef.keyboard.press('Enter');
              await sleep(1000);
              db.markReminderSent(r.id);
              log('⏰ Recordatorio enviado.');
            }
            await backToChatList(pageRef._page || pageRef);
          }
        } catch (e) {
          log('⏰ No se pudo enviar un recordatorio.');
        }
      }
    } catch {}
  }, 30000); // Check every 30s
  if (reminderInterval.unref) reminderInterval.unref();
}

function stopReminderScheduler() {
  if (reminderInterval) { clearInterval(reminderInterval); reminderInterval = null; }
}

// ── DB Fact extraction & summary (async, non-blocking) ──
let _dbFactsInFlight = new Set();
async function dbUpdateChatFacts(chatName, context, incomingMsgs, lastReply) {
  if (!chatName) return;
  // Throttle: only run if not already in flight and every 3rd message or if no recent fact extraction
  if (_dbFactsInFlight.has(chatName)) return;
  try {
    const count = db.getMessageCountStmt ? db.getMessageCountStmt.get(chatName)?.cnt || 0 : 0;
    // Only extract facts every 5 messages to avoid DDOS to Ollama
    if (count % 5 !== 0 && count > 5) return;
  } catch {}
  _dbFactsInFlight.add(chatName);
  try {
    const recentMsgs = db.getRecentMessages(chatName, 10);
    // Build a short summary from the last messages
    const convoText = recentMsgs.map(m => `${m.role === 'user' ? 'U' : 'A'}: ${m.content.substring(0, 100)}`).join('\n');
    // Extract key facts via AI (lightweight)
    const factPrompt = `De esta conversación extrae 1-3 datos importantes sobre la persona (gustos, trabajo, familia, planes, estado de ánimo). Responde SOLO con los datos separados por |:\n\n${convoText}`;
    const res = await fetch(`${OLLAMA_URL}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: OLLAMA_MODEL_FAST,
        messages: [
          { role: 'system', content: 'Extrae datos personales de la conversación. Responde SOLO con hechos separados por |. Si no hay datos relevantes, responde "NONE".' },
          { role: 'user', content: factPrompt },
        ],
        temperature: 0.3,
        num_predict: 60,
        stream: false,
      }),
      signal: AbortSignal.timeout(10000),
    });
    if (res.ok) {
      const data = await res.json();
      const facts = (data?.message?.content || '').trim();
      if (facts && facts !== 'NONE') {
        for (const fact of facts.split('|')) {
          const clean = fact.trim().replace(/^-\s*/, '');
          if (clean.length > 5) db.addMemory(chatName, clean, 'personal', 0.6);
        }
      }
    }
    // Generate summary from last 50 messages
    const totalMsgs = recentMsgs.length;
    if (totalMsgs >= 4) {
      const summaryRes = await fetch(`${OLLAMA_URL}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: OLLAMA_MODEL_FAST,
          messages: [
            { role: 'system', content: 'Resume la conversación en 1 oración corta.' },
            { role: 'user', content: `Resume: ${convoText.substring(0, 500)}` },
          ],
          temperature: 0.3,
          num_predict: 40,
          stream: false,
        }),
        signal: AbortSignal.timeout(8000),
      });
      if (summaryRes.ok) {
        const summaryData = await summaryRes.json();
        const summaryText = (summaryData?.message?.content || '').trim();
        if (summaryText && summaryText.length > 5 && summaryText.length < 200 && !summaryText.startsWith('NONE')) {
          db.updateSummary(chatName, summaryText);
        }
      }
    }
  } catch (e) {
    // Silently fail — DB updates are non-critical
    if (process.env.DEBUG_DB) log(`[DB FACTS] ${e.message}`);
  } finally {
    _dbFactsInFlight.delete(chatName);
  }
}

function recordIncoming(chat, incomingMsgs) {
  // ── Store incoming messages in DB ──
  try {
    for (const msg of incomingMsgs) {
      const isMedia = isMediaOnlyMessage(msg);
      db.addUserMessage(chat.name, msg, isMedia, detectTopic([msg]));
    }
  } catch {}

  // ── Reminder detection ──
  const reminderText = incomingMsgs.join(' ');
  const reminderIntent = detectReminderIntent(reminderText);
  if (reminderIntent && reminderIntent.date && db?.addReminder) {
    try {
      db.addReminder(chat.name, reminderIntent.timestamp, reminderIntent.message);
      log('⏰ Recordatorio agendado.');
    } catch (e) {
      log(`⏰ Error agendando recordatorio: ${e.message}`);
    }
  }

  // ── Correction learning ──
  const correctionIntent = detectCorrectionIntent(reminderText, chat.name);
  if (correctionIntent && db?.addCorrection) {
    try {
      db.addCorrection(chat.name, correctionIntent.pattern, correctionIntent.instruction);
      log('📝 Corrección guardada.');
    } catch (e) {
      log(`📝 Error guardando corrección: ${e.message}`);
    }
  }
}

async function replyToUnreadChat(page, chat, settings, profile) {
  if (!allowedAutoChats(settings).has(chat.name) || !profile) return false;
  rememberPending(chat, chat.fromPending ? 'retry' : 'unread');
  await chat.row.click();
  await waitForChatReady(page);

  const isSpecialChat = !!SPECIAL_CHATS[chat.name];
  const contextLimit = isSpecialChat ? CONTEXT_MESSAGE_LIMIT * 2 : CONTEXT_MESSAGE_LIMIT;
  const { context, hasIncoming, allIncoming, unansweredIncoming, lastIsIncoming, lastMessageId, turns } =
    await getConversationContext(page, chat.name, contextLimit);

  if (!hasIncoming) {
    if (!turns.length) throw new Error('La conversación todavía no mostró mensajes.');
    log('↩ Conversación sin mensajes entrantes.');
    skipUntilByChat.set(chat.name, Date.now() + computeCooldown(chat.name));
    clearPending(chat.name);
    saveState();
    await backToChatList(page);
    return false;
  }

  const incomingMsgs = (unansweredIncoming && unansweredIncoming.length > 0
    ? unansweredIncoming
    : (lastIsIncoming ? allIncoming.slice(-1) : []))
    .filter(m => m.trim());

  if (!lastIsIncoming || incomingMsgs.length === 0) {
    log('↩ El último mensaje no era entrante.');
    skipUntilByChat.set(chat.name, Date.now() + computeCooldown(chat.name));
    clearPending(chat.name);
    saveState();
    await backToChatList(page);
    return false;
  }

  // Dedup: skip if the latest incoming message was already replied to
  const latestIncoming = incomingMsgs.length > 0 ? incomingMsgs[incomingMsgs.length - 1] : '';
  const incomingSignature = messageSignature(latestIncoming || incomingMsgs.join(' '));
  if (incomingSignature && repliedIncomingByChat.get(chat.name) === incomingSignature) {
    log('↩ El bot ya respondió al último mensaje.');
    skipUntilByChat.set(chat.name, Date.now() + computeCooldown(chat.name));
    clearPending(chat.name);
    saveState();
    await backToChatList(page);
    return false;
  }

  const pending = pendingByChat.get(chat.name);
  const rateLimitedUntil = replyRateLimitUntil(chat.name);
  if (rateLimitedUntil) {
    pending.nextAttemptAt = rateLimitedUntil;
    saveState();
    await backToChatList(page);
    return false;
  }

  const observed = createHash('sha256').update(lastMessageId || incomingMsgs.join('\n')).digest('hex');
  if (pending.recordedIncoming !== observed) {
    recordIncoming(chat, incomingMsgs);
    pending.recordedIncoming = observed;
    saveState();
  }

  const anyMedia = incomingMsgs.some(isMediaOnlyMessage);
  const startTime = Date.now();
  let reply;
  try {
    reply = await generateAIReply(chat.name, context, incomingMsgs, anyMedia, turns, settings, profile);
  } catch {
    const elapsed = Date.now() - startTime;
    deferPendingForAI(chat.name);
    logMetric('gen', `time_ms=${elapsed} success=false`);
    writeMonitorBridge({ chat: chat.name, timeMs: elapsed, success: false, timestamp: new Date().toISOString() });
    await backToChatList(page);
    return false;
  }
  const elapsed = Date.now() - startTime;

  try {
    if (!allowedAutoChats(assistant.readSettings()).has(chat.name)) {
      await backToChatList(page);
      return false;
    }
  } catch {
    await backToChatList(page);
    return false;
  }

  const current = await getConversationContext(page, chat.name, contextLimit);
  if (!current.lastIsIncoming || current.context !== context || current.lastMessageId !== lastMessageId) {
    log('↩ La conversación cambió mientras se generaba la respuesta.');
    throw new Error('La conversación cambió mientras se generaba la respuesta.');
  }
  const input = await getMessageInput(page);
  if (!input) throw new Error('No se encontró el campo para responder.');

  await input.fill(reply, { timeout: 8000 });
  await input.press('Enter', { timeout: 8000 });
  await sleep(1200);

  if (incomingSignature) repliedIncomingByChat.set(chat.name, incomingSignature);
  lastSentByChat.set(chat.name, reply);
  trackRecentReply(chat.name, reply);
  // Track activity for adaptive cooldown
  lastActivityByChat.set(chat.name, Date.now());
  skipUntilByChat.set(chat.name, Date.now() + computeCooldown(chat.name));
  clearPending(chat.name);
  saveState();

  // ── Persist in DB ──
  try {
    db.addAssistantMessage(chat.name, reply, conversationTopics.get(chat.name) || 'general');
    // Lightweight fact extraction from conversation
    dbUpdateChatFacts(chat.name, context, incomingMsgs, reply).catch(() => {});
  } catch {}
  chatRateCounts.get(chat.name).push(Date.now());
  stats.ia++;
  const times = genTimesByChat.get(chat.name) || [];
  times.push(elapsed);
  if (times.length > 20) times.shift();
  genTimesByChat.set(chat.name, times);
  logMetric('gen', `time_ms=${elapsed} success=true len=${reply.length}`);
  writeMonitorBridge({ chat: chat.name, timeMs: elapsed, success: true, len: reply.length, timestamp: new Date().toISOString() });
  log('✓ Respuesta de IA enviada.');

  stats.total++;
  if (stats.total % 50 === 0) {
    const pct = stats.total > 0 ? Math.round(stats.ia / stats.total * 100) : 0;
    log(`[STATS] ${stats.total} respuestas | IA: ${stats.ia} | Fallback: ${stats.fallback} | ${pct}% IA`);
  }

  await backToChatList(page);
  return true;
}

function readSendQueue() {
  try {
    const queue = JSON.parse(fs.readFileSync(SEND_QUEUE_FILE, 'utf8') || '[]');
    return Array.isArray(queue) ? queue : [];
  } catch {
    return [];
  }
}

function writeSendQueue(queue) {
  try { atomicWriteJson(SEND_QUEUE_FILE, queue); } catch {}
}

async function sendMessageToChat(page, chatName, message, source = 'manual') {
  const name = cleanChatName(chatName);
  const text = cleanText(message).slice(0, REPLY_LIMIT);
  if (!name || !text) throw new Error('missing chat or message');

  const chat = await findVisibleChatRowByName(page, name);
  if (!chat) {
    log(`↩ ${source} send: chat not visible (${name.substring(0, 25)})`);
    return false;
  }

  await chat.row.click();
  await waitForChatReady(page);

  const input = await getMessageInput(page);
  if (!input) throw new Error('message input not found');

  await input.click();
  await input.fill(text);
  await sleep(200);
  await page.keyboard.press('Enter');
  await sleep(1200);

  lastSentByChat.set(name, text);
  trackRecentReply(name, text);
  lastActivityByChat.set(name, Date.now());
  skipUntilByChat.set(name, Date.now() + computeCooldown(name));
  saveState();

  try {
    db.addAssistantMessage(name, text, conversationTopics.get(name) || 'manual');
  } catch {}

  log(`✓ Envío ${source} completado.`);
  await backToChatList(page);
  return true;
}

async function processSendQueue(page) {
  const queue = readSendQueue();
  if (queue.length === 0) return false;

  const now = Date.now();
  const next = [];
  let handled = false;

  for (const item of queue) {
    const age = now - Number(item.createdAt || now);
    if (age > MAX_SEND_QUEUE_AGE_MS) {
      log(`↩ queued send expired: ${(item.chat || '?').substring(0, 25)}`);
      continue;
    }
    if (handled || Number(item.nextAttemptAt || 0) > now) {
      next.push(item);
      continue;
    }

    handled = true;
    try {
      const sent = await sendMessageToChat(page, item.chat, item.message, 'queued');
      if (sent) continue;
      throw new Error('chat row not visible');
    } catch (e) {
      const attempts = Number(item.attempts || 0) + 1;
      if (attempts >= MAX_SEND_QUEUE_ATTEMPTS) {
        log(`✗ queued send dropped for ${(item.chat || '?').substring(0, 25)}: ${e.message}`);
      } else {
        next.push({
          ...item,
          attempts,
          nextAttemptAt: now + PENDING_RETRY_MS,
          lastError: e.message,
        });
        log(`✗ queued send failed for ${(item.chat || '?').substring(0, 25)}: ${e.message}`);
      }
      await backToChatList(page).catch(() => {});
    }
  }

  writeSendQueue(next);
  return handled;
}

function setBrowserState(state, error = '') {
  if (browserState?.state === state && browserState.error === error) return;
  const current = { pid: process.pid, state, error };
  try {
    atomicWriteJson(BROWSER_STATUS_FILE, current);
    browserState = current;
  } catch (failure) {
    log(`No se pudo guardar el estado del navegador: ${failure.message}`);
  }
}

async function whatsappPageState(page) {
  if (page.isClosed()) throw new Error('Se cerró la página de WhatsApp.');
  if (await page.locator('#pane-side').isVisible()) return 'ready';
  if (await page.locator('canvas').first().isVisible()) return 'qr';
  return 'loading';
}

async function waitForWhatsAppReady(page) {
  log('Cargando WhatsApp Web…');
  for (let i = 0; i < 90; i++) {
    const state = await whatsappPageState(page);
    setBrowserState(state);
    if (state === 'ready') {
      log('WhatsApp listo.');
      return true;
    }
    // Un QR puede esperar el tiempo que haga falta en la misma ventana.
    if (state === 'qr') return false;
    await sleep(1000);
  }
  return false;
}

async function chromiumExecutable() {
  if (fs.existsSync(chromium.executablePath())) return undefined;
  const installed = process.platform === 'win32'
    ? [process.env.ProgramFiles, process.env['ProgramFiles(x86)'], process.env.LOCALAPPDATA].filter(Boolean)
      .flatMap(root => ['Google/Chrome/Application/chrome.exe', 'Microsoft/Edge/Application/msedge.exe']
        .map(relative => path.join(root, relative)))
    : ['/opt/google/chrome/chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser',
      '/usr/bin/microsoft-edge-stable'];
  const executable = installed.find(file => fs.existsSync(file));
  if (executable) return executable;
  if (!PACKAGED) throw new Error('No se encontró Chrome ni Chromium.');
  setBrowserState('installing');
  const controller = new AbortController();
  closeBrowser = async () => controller.abort();
  const cli = path.join(path.dirname(require.resolve('playwright/package.json')), 'cli.js');
  try {
    await promisify(execFile)(process.execPath, [cli, 'install', 'chromium', '--no-shell'],
      { signal: controller.signal, timeout: 600000, windowsHide: true, maxBuffer: 2 * 1024 * 1024 });
  } catch {
    throw new Error('No se pudo preparar Chromium. Revisa tu conexión y pulsa Reiniciar.');
  }
  if (!fs.existsSync(chromium.executablePath())) throw new Error('Chromium no quedó disponible.');
  setBrowserState('opening');
  return undefined;
}

async function launchWhatsAppSession() {
  let context;
  let browser;
  try {
    if (BROWSER_PROVIDER.toLowerCase() === 'cdp') {
      if (!isLoopbackHttpUrl(BROWSER_CDP_ENDPOINT)) {
        throw new Error('Configura una dirección de Chrome local en Ajustes.');
      }
      try {
        browser = await chromium.connectOverCDP(BROWSER_CDP_ENDPOINT, { timeout: 8000 });
      } catch {
        throw new Error('No pude conectar al Chrome abierto. Revisa Ajustes o elige Navegador de Nox.');
      }
      closeBrowser = () => browser.close();
      context = browser.contexts()[0] || await browser.newContext();
    } else {
      fs.mkdirSync(PROFILE_DIR, { recursive: true, mode: 0o700 });
      const options = { headless: false, args: ['--disable-dev-shm-usage'], timeout: 15000 };
      options.executablePath = await chromiumExecutable();
      context = await chromium.launchPersistentContext(PROFILE_DIR, options);
      closeBrowser = () => context.close();
    }

    const page = await ensureWhatsAppPage(context);
    await page.bringToFront().catch(() => {});
    await waitForWhatsAppReady(page);
    return { context, page, browser };
  } catch (error) {
    await closeBrowser?.().catch(() => {});
    closeBrowser = null;
    throw error;
  }
}

async function monitorWhatsApp(context, waPage) {
  // Start reminder scheduler
  if (db?.getDueReminders) {
    // We need a way to reference the page for sending reminders
    // Store reference to waPage for scheduler
    startReminderScheduler(waPage);
    log('⏰ Reminder scheduler started');
  }

  log('='.repeat(50));
  log('MONITORING');
  log('='.repeat(50));

  let cycle = 0;

  while (true) {
    try {
      const state = await whatsappPageState(waPage);
      setBrowserState(state);
      if (state !== 'ready') {
        await sleep(2000);
        continue;
      }

      cycle++;
      const queuedHandled = await processSendQueue(waPage);
      if (queuedHandled) {
        await sleep(POLL_INTERVAL);
        continue;
      }

      let settings;
      let profile;
      try {
        settings = assistant.readSettings();
        const allowed = allowedAutoChats(settings);
        if (!allowed.size) setInferenceState('paused', null);
        if (allowed.size) {
          profile = await resolveReplyProfile(settings);
          if (profile) {
            const pending = getDuePending(allowed);
            const chat = pending ? await findVisibleChatRowByName(waPage, pending.name) :
              await clickUnreadTab(waPage) ? await getFirstUnreadRow(waPage, allowed) : null;
            if (chat) {
              try {
                await replyToUnreadChat(waPage, chat, settings, profile);
              } catch (error) {
                if (isClosedError(error)) throw error;
                log('✗ La respuesta quedó pendiente por un error del navegador.');
                markPendingFailed(chat.name, new Error('Error del navegador.'));
                await backToChatList(waPage);
              }
            } else if (pending) {
              markPendingFailed(pending.name, new Error('Conversación pendiente fuera de la vista.'));
            }
          } else {
            setInferenceState('error', null, 'Activa un modelo en Modelos o inicia un proveedor local.');
          }
        }
      } catch (error) {
        if (isClosedError(error)) throw error;
        setInferenceState('error', profile, 'Revisa la configuración de IA en Modelos.');
      }

      await sleep(POLL_INTERVAL);

      // Periodic health metric
      if (cycle % 50 === 0) {
        const totalGen = stats.ia + stats.fallback;
        const iaPct = totalGen > 0 ? Math.round(stats.ia / totalGen * 100) : 0;
        logMetric('health', `cycle=${cycle} total=${totalGen} ia=${stats.ia} fb=${stats.fallback} ia_pct=${iaPct} chats=${genTimesByChat.size}`);
        // Cleanup stale rate limit entries every 50 cycles
        const cutoff = Date.now() - RATE_WINDOW_MS;
        for (const [chat, times] of chatRateCounts.entries()) {
          const filtered = times.filter(t => t > cutoff);
          if (filtered.length === 0) chatRateCounts.delete(chat);
          else chatRateCounts.set(chat, filtered);
        }
      }

      // DB stats every 100 cycles
      if (cycle % 100 === 0 && db?.stats) {
        try {
          const s = db.stats();
          logMetric('db', `chats=${s.chats} msgs=${s.messages} mems=${s.memories}`);
        } catch {}
      }

      // Vacuum every 500 cycles (~weekly behaviour)
      if (cycle % 500 === 0 && db?.vacuum) {
        try { db.vacuum(); log('🧹 DB vacuum completed'); } catch {}
      }

      // Periodic bridge update for monitor
      if (cycle % 25 === 0) writeMonitorBridge();

      // Process any pending moderator approvals from bridge
      if (cycle % 5 === 0) {
        try {
          if (fs.existsSync(BRIDGE_FILE)) {
            const bridgeData = JSON.parse(fs.readFileSync(BRIDGE_FILE, 'utf8'));
            if (bridgeData._approveChat && bridgeData._approveReply) {
              const approvedChat = bridgeData._approveChat;
              const approvedReply = bridgeData._approveReply;
              const sent = await sendMessageToChat(waPage, approvedChat, approvedReply, 'approved');
              if (sent) {
                log(`📋 Moderator approved reply sent for ${approvedChat.substring(0, 25)}`);
                bridgeData._approveChat = null;
                bridgeData._approveReply = null;
                bridgeData._approveError = null;
                bridgeData._approveAttempts = 0;
              } else {
                bridgeData._approveAttempts = Number(bridgeData._approveAttempts || 0) + 1;
                bridgeData._approveError = 'chat row not visible';
                if (bridgeData._approveAttempts >= 3) {
                  log(`📋 Moderator approval dropped for ${approvedChat.substring(0, 25)}: chat not visible`);
                  bridgeData._approveChat = null;
                  bridgeData._approveReply = null;
                }
              }
              const freshBridge = JSON.parse(fs.readFileSync(BRIDGE_FILE, 'utf8'));
              if (freshBridge._approveChat === approvedChat && freshBridge._approveReply === approvedReply) {
                Object.assign(freshBridge, {
                  _approveChat: bridgeData._approveChat,
                  _approveReply: bridgeData._approveReply,
                  _approveAttempts: bridgeData._approveAttempts,
                  _approveError: bridgeData._approveError,
                });
                atomicWriteJson(BRIDGE_FILE, freshBridge);
              }
            }
          }
        } catch {}
      }
    } catch (e) {
      if (isClosedError(e) || waPage.isClosed()) {
        log('Se cerró la pestaña de WhatsApp. Reconectando…');
        waPage = await ensureWhatsAppPage(context);
        startReminderScheduler(waPage);
        continue;
      }
      log(`[${cycle}] Error: ${e.message}`);
      await sleep(ERROR_COOLDOWN_MS);
    }
  }
}

async function main() {
  acquireLock();
  // ── Initialize Database ──
  try {
    db = new ChatDatabase();
    const dbStats = db.stats();
    log(`📊 DB: ${dbStats.chats} chats, ${dbStats.messages} mensajes, ${dbStats.memories} recuerdos`);
  } catch (e) {
    log(`⚠️ DB init failed: ${e.message} — continuing without persistence`);
    db = { getMemoriesForPrompt: () => '', getSummary: () => '', getRecentMessages: () => [], addUserMessage: () => {}, addAssistantMessage: () => {}, addMemory: () => {}, updateSummary: () => {} };
  }

  // ── Load plugins ──
  try {
    activePlugins = loadPlugins();
    log(`🔌 Loaded ${activePlugins.length} plugin(s)`);
    ApiServer.setPlugins(activePlugins);
  } catch (e) {
    log(`🔌 Plugin load error: ${e.message}`);
    activePlugins = [];
  }

  // ── Start API server ──
  try {
    const apiServer = ApiServer.startServer({ db, stats, modelStats });
    log('🌐 Iniciando la API local de Nox.');
    // Graceful shutdown
    process.once('SIGINT', () => { try { apiServer.close(); } catch {} });
  } catch (e) {
    log(`🌐 API server error: ${e.message}`);
  }

  // Start plugin interval hooks
  if (activePlugins.length > 0) {
    setInterval(() => {
      for (const p of activePlugins) {
        if (p.onInterval) {
          try { p.onInterval({ db, log }); } catch {}
        }
      }
    }, 60000);
  }

  log('='.repeat(50));
  log('WhatsApp Auto-Responder v7 — AI Conversacional');
  log('='.repeat(50));
  loadState();
  setBrowserState('opening');
  setInferenceState('idle', null);
  let restartCount = 0;
  while (true) {
    let session = null;
    try {
      restartCount++;
      log(`Session start #${restartCount}`);
      session = await launchWhatsAppSession();
      await monitorWhatsApp(session.context, session.page);
    } catch (e) {
      log(`Session stopped: ${e.message}`);
      const reason = e.message.split('\n')[0];
      setBrowserState('error', reason);
    } finally {
      stopReminderScheduler();
      if (session) await closeBrowser?.().catch(() => {});
      closeBrowser = null;
    }
    log(`Restarting in ${Math.round(RESTART_DELAY_MS / 1000)}s...`);
    await sleep(RESTART_DELAY_MS);
  }
}

main().catch(e => { log(`FATAL: ${e.message}`); process.exit(1); });
