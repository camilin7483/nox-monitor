'use strict';

const { DATA_DIR, PACKAGED } = require('./runtime-paths');
const Database = PACKAGED ? require('node:sqlite').DatabaseSync : require('better-sqlite3');
const path = require('path');

const DB_PATH = path.join(DATA_DIR, 'responder.db');

class ChatDatabase {
  constructor(dbPath = DB_PATH) {
    this.db = new Database(dbPath);
    this.db.exec('PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON;');
    this._ensureTables();
    this._prepareStmts();
  }

  _ensureTables() {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS chats (
        name TEXT PRIMARY KEY COLLATE NOCASE,
        language TEXT DEFAULT 'es',
        tone TEXT DEFAULT 'casual',
        topic TEXT DEFAULT 'general',
        summary TEXT DEFAULT '',
        facts TEXT DEFAULT '[]',
        created_at INTEGER DEFAULT (strftime('%s','now')),
        updated_at INTEGER DEFAULT (strftime('%s','now')),
        total_messages INTEGER DEFAULT 0,
        total_replies INTEGER DEFAULT 0,
        last_active INTEGER DEFAULT (strftime('%s','now'))
      );

      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT NOT NULL COLLATE NOCASE,
        role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
        content TEXT NOT NULL,
        is_media INTEGER DEFAULT 0,
        topic TEXT DEFAULT 'general',
        timestamp INTEGER DEFAULT (strftime('%s','now')),
        FOREIGN KEY (chat_name) REFERENCES chats(name) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT NOT NULL COLLATE NOCASE,
        fact TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        confidence REAL DEFAULT 0.7,
        source TEXT DEFAULT '',
        created_at INTEGER DEFAULT (strftime('%s','now')),
        accessed_count INTEGER DEFAULT 1,
        last_accessed INTEGER DEFAULT (strftime('%s','now')),
        FOREIGN KEY (chat_name) REFERENCES chats(name) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT NOT NULL COLLATE NOCASE,
        remind_at INTEGER NOT NULL,
        message TEXT NOT NULL,
        note TEXT DEFAULT '',
        status TEXT DEFAULT 'pending' CHECK(status IN ('pending','sent','cancelled')),
        created_at INTEGER DEFAULT (strftime('%s','now')),
        sent_at INTEGER,
        FOREIGN KEY (chat_name) REFERENCES chats(name) ON DELETE CASCADE
      );

      CREATE TABLE IF NOT EXISTS corrections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_name TEXT NOT NULL COLLATE NOCASE,
        pattern TEXT NOT NULL,
        instruction TEXT NOT NULL DEFAULT '',
        created_at INTEGER DEFAULT (strftime('%s','now')),
        active INTEGER DEFAULT 1,
        FOREIGN KEY (chat_name) REFERENCES chats(name) ON DELETE CASCADE
      );

      CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_name, timestamp DESC);
      CREATE INDEX IF NOT EXISTS idx_memories_chat_cat ON memories(chat_name, category);
      CREATE INDEX IF NOT EXISTS idx_memories_chat_conf ON memories(chat_name, confidence DESC);
      CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, remind_at);
    `);
  }

  _prepareStmts() {
    // ── Chats ──
    this.getChatStmt = this.db.prepare('SELECT * FROM chats WHERE name = ?');
    this.upsertChatStmt = this.db.prepare(`
      INSERT INTO chats (name, language, tone, topic, summary, facts, total_messages, total_replies, last_active, updated_at)
      VALUES (@name, @language, @tone, @topic, @summary, @facts, @total_messages, @total_replies, strftime('%s','now'), strftime('%s','now'))
      ON CONFLICT(name) DO UPDATE SET
        language          = excluded.language,
        tone              = excluded.tone,
        topic             = excluded.topic,
        summary           = excluded.summary,
        facts             = excluded.facts,
        total_messages    = excluded.total_messages,
        total_replies     = excluded.total_replies,
        last_active       = strftime('%s','now'),
        updated_at        = strftime('%s','now')
    `);
    this.updateChatActiveStmt = this.db.prepare(
      'UPDATE chats SET last_active = strftime(\'%s\',\'now\'), updated_at = strftime(\'%s\',\'now\') WHERE name = ?'
    );
    this.incrementRepliesStmt = this.db.prepare(
      'UPDATE chats SET total_replies = total_replies + 1, last_active = strftime(\'%s\',\'now\'), updated_at = strftime(\'%s\',\'now\') WHERE name = ?'
    );
    this.allChatsStmt = this.db.prepare(
      'SELECT name, language, topic, total_messages, total_replies, last_active FROM chats ORDER BY last_active DESC'
    );

    // ── Messages ──
    this.addMessageStmt = this.db.prepare(
      'INSERT INTO messages (chat_name, role, content, is_media, topic) VALUES (@chat_name, @role, @content, @is_media, @topic)'
    );
    this.getRecentMessagesStmt = this.db.prepare(
      'SELECT role, content, is_media, timestamp FROM messages WHERE chat_name = ? ORDER BY id DESC LIMIT ?'
    );
    this.getMessageCountStmt = this.db.prepare(
      'SELECT COUNT(*) as cnt FROM messages WHERE chat_name = ?'
    );
    this.deleteOldMessagesStmt = this.db.prepare(
      'DELETE FROM messages WHERE chat_name = ? AND id NOT IN (SELECT id FROM messages WHERE chat_name = ? ORDER BY id DESC LIMIT 100)'
    );

    // ── Memories ──
    this.getMemoriesStmt = this.db.prepare(
      'SELECT id, fact, category, confidence FROM memories WHERE chat_name = ? AND confidence >= 0.5 ORDER BY confidence DESC, last_accessed DESC LIMIT 20'
    );
    this.getMemoriesForPromptStmt = this.db.prepare(
      'SELECT fact FROM memories WHERE chat_name = ? AND confidence >= 0.6 ORDER BY created_at DESC LIMIT 5'
    );
    this.addMemoryStmt = this.db.prepare(
      'INSERT INTO memories (chat_name, fact, category, confidence, source) VALUES (@chat_name, @fact, @category, @confidence, @source)'
    );
    this.clearMemoriesStmt = this.db.prepare('DELETE FROM memories WHERE chat_name = ?');
    this.updateMemoryAccessStmt = this.db.prepare(
      'UPDATE memories SET accessed_count = accessed_count + 1, last_accessed = strftime(\'%s\',\'now\') WHERE id = ?'
    );
    this.deleteLowConfidenceMemoriesStmt = this.db.prepare(
      'DELETE FROM memories WHERE chat_name = ? AND confidence < 0.3'
    );

    // ── Migrate from JSON state ──
    this.getChatByNameStmt = this.db.prepare('SELECT name FROM chats WHERE name = ?');
  }

  // ══════════════════════════════════════════
  //  CHAT METHODS
  // ══════════════════════════════════════════

  getChat(name) {
    return this.getChatStmt.get(name) || null;
  }

  ensureChat(name, opts = {}) {
    const existing = this.getChat(name);
    if (existing) return existing;
    this.upsertChatStmt.run({
      name,
      language: opts.language || 'es',
      tone: opts.tone || 'casual',
      topic: opts.topic || 'general',
      summary: '',
      facts: '[]',
      total_messages: 0,
      total_replies: 0,
    });
    return this.getChat(name);
  }

  updateChatActivity(name) {
    this.updateChatActiveStmt.run(name);
  }

  incrementReplies(name) {
    this.incrementRepliesStmt.run(name);
    this.updateChatActiveStmt.run(name);
  }

  updateChat(name, data) {
    const chat = this.getChat(name);
    if (!chat) return this.ensureChat(name, data);
    this.upsertChatStmt.run({
      name,
      language: data.language || chat.language || 'es',
      tone: data.tone || chat.tone || 'casual',
      topic: data.topic || chat.topic || 'general',
      summary: data.summary ?? chat.summary ?? '',
      facts: data.facts ?? chat.facts ?? '[]',
      total_messages: data.total_messages ?? chat.total_messages ?? 0,
      total_replies: data.total_replies ?? chat.total_replies ?? 0,
    });
  }

  getAllChats() {
    return this.allChatsStmt.all();
  }

  // ══════════════════════════════════════════
  //  MESSAGE METHODS
  // ══════════════════════════════════════════

  addUserMessage(name, content, isMedia = false, topic = 'general') {
    this.ensureChat(name);
    this.addMessageStmt.run({ chat_name: name, role: 'user', content, is_media: isMedia ? 1 : 0, topic });
    // Increment counter
    const chat = this.getChat(name);
    this.updateChat(name, { total_messages: (chat?.total_messages || 0) + 1 });
    this.updateChatActiveStmt.run(name);
    // Prune old messages if needed
    const count = this.getMessageCountStmt.get(name);
    if (count && count.cnt > 200) {
      this.deleteOldMessagesStmt.run(name, name);
    }
  }

  addAssistantMessage(name, content, topic = 'general') {
    this.ensureChat(name);
    this.addMessageStmt.run({ chat_name: name, role: 'assistant', content, is_media: 0, topic });
    this.incrementReplies(name);
  }

  getRecentMessages(name, limit = 30) {
    return this.getRecentMessagesStmt.all(name, limit).reverse();
  }

  getConversationForPrompt(name, limit = 30) {
    const msgs = this.getRecentMessages(name, limit);
    return msgs.map(m => {
      const role = m.role === 'user' ? 'Usuario' : 'Asistente';
      return `${role}: ${m.content}`;
    }).join('\n');
  }

  // ══════════════════════════════════════════
  //  MEMORY METHODS
  // ══════════════════════════════════════════

  getMemories(name) {
    return this.getMemoriesStmt.all(name);
  }

  getMemoriesForPrompt(name) {
    const facts = this.getMemoriesForPromptStmt.all(name);
    if (facts.length === 0) return '';
    return facts.map(f => `- ${f.fact}`).join('\n');
  }

  addMemory(name, fact, category = 'general', confidence = 0.7, source = 'ai') {
    // Avoid duplicate near-identical facts
    const existing = this.getMemories(name);
    for (const mem of existing) {
      const dist = levenshtein(fact.toLowerCase(), mem.fact.toLowerCase());
      if (dist < Math.min(fact.length, mem.fact.length) * 0.3) {
        // Boost existing
        this.db.prepare('UPDATE memories SET confidence = MIN(1.0, confidence + 0.1), accessed_count = accessed_count + 1, last_accessed = strftime(\'%s\',\'now\') WHERE id = ?').run(mem.id);
        return;
      }
    }
    this.addMemoryStmt.run({ chat_name: name, fact, category, confidence, source });
    // Clean low confidence
    this.deleteLowConfidenceMemoriesStmt.run(name);
  }

  clearMemories(name) {
    this.clearMemoriesStmt.run(name);
  }

  // ══════════════════════════════════════════
  //  SUMMARY & FACTS EXTRACTION
  // ══════════════════════════════════════════

  updateSummary(name, summary) {
    this.db.prepare('UPDATE chats SET summary = ?, updated_at = strftime(\'%s\',\'now\') WHERE name = ?').run(summary, name);
  }

  getSummary(name) {
    const chat = this.getChat(name);
    return chat?.summary || '';
  }

  // ══════════════════════════════════════════
  //  REMINDER METHODS
  // ══════════════════════════════════════════

  addReminder(chatName, remindAt, message, note = '') {
    this.ensureChat(chatName);
    this.db.prepare('INSERT INTO reminders (chat_name, remind_at, message, note) VALUES (?, ?, ?, ?)').run(chatName, remindAt, message, note);
  }

  getDueReminders() {
    return this.db.prepare("SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= strftime('%s','now') ORDER BY remind_at ASC LIMIT 10").all();
  }

  markReminderSent(id) {
    this.db.prepare("UPDATE reminders SET status = 'sent', sent_at = strftime('%s','now') WHERE id = ?").run(id);
  }

  cancelReminder(id) {
    this.db.prepare("UPDATE reminders SET status = 'cancelled' WHERE id = ?").run(id);
  }

  getPendingReminders(chatName) {
    return this.db.prepare("SELECT * FROM reminders WHERE chat_name = ? AND status = 'pending' ORDER BY remind_at ASC").all(chatName);
  }

  // ══════════════════════════════════════════
  //  CORRECTION METHODS
  // ══════════════════════════════════════════

  addCorrection(chatName, pattern, instruction = '') {
    this.ensureChat(chatName);
    this.db.prepare('INSERT INTO corrections (chat_name, pattern, instruction) VALUES (?, ?, ?)').run(chatName, pattern, instruction);
  }

  getActiveCorrections(chatName) {
    return this.db.prepare("SELECT * FROM corrections WHERE chat_name = ? AND active = 1 ORDER BY created_at DESC LIMIT 20").all(chatName);
  }

  deactivateCorrection(id) {
    this.db.prepare("UPDATE corrections SET active = 0 WHERE id = ?").run(id);
  }

  getAllActiveCorrections() {
    return this.db.prepare("SELECT * FROM corrections WHERE active = 1 ORDER BY created_at DESC").all();
  }

  // ══════════════════════════════════════════
  //  EXPORT METHODS
  // ══════════════════════════════════════════

  exportChatToMarkdown(chatName) {
    const chat = this.getChat(chatName);
    if (!chat) return null;
    const msgs = this.getRecentMessages(chatName, 200);
    const memories = this.getMemories(chatName);
    let md = `# Conversación con ${chatName}\n\n`;
    md += `**Idioma:** ${chat.language || '?'}\n`;
    md += `**Tono:** ${chat.tone || 'casual'}\n`;
    md += `**Tema principal:** ${chat.topic || 'general'}\n`;
    md += `**Mensajes totales:** ${chat.total_messages || 0}\n`;
    md += `**Respuestas del bot:** ${chat.total_replies || 0}\n`;
    md += `**Última actividad:** ${chat.last_active ? new Date(chat.last_active * 1000).toISOString() : '?'}\n\n`;
    if (chat.summary) md += `> *Resumen: ${chat.summary}*\n\n`;
    if (memories.length > 0) {
      md += `## Datos conocidos\n\n`;
      for (const m of memories) {
        md += `- ${m.fact} (${m.category}, ${Math.round(m.confidence * 100)}% confianza)\n`;
      }
      md += '\n';
    }
    md += `## Historial\n\n`;
    md += `| Fecha | Quién | Mensaje |\n`;
    md += `|-------|-------|--------|\n`;
    for (const m of msgs) {
      const date = new Date(m.timestamp * 1000).toISOString().slice(0, 19).replace('T', ' ');
      const who = m.role === 'user' ? chatName : 'Nox (bot)';
      const content = m.content.replace(/\|/g, '\\|').substring(0, 200);
      md += `| ${date} | ${who} | ${content} |\n`;
    }
    return md;
  }

  // ══════════════════════════════════════════
  //  CLEANUP
  // ══════════════════════════════════════════

  close() {
    this.db.close();
  }

  vacuum() {
    this.db.exec('VACUUM');
  }

  stats() {
    return {
      chats: this.db.prepare('SELECT COUNT(*) as c FROM chats').get().c,
      messages: this.db.prepare('SELECT COUNT(*) as c FROM messages').get().c,
      memories: this.db.prepare('SELECT COUNT(*) as c FROM memories').get().c,
    };
  }
}

// Simple levenshtein for memory dedup
function levenshtein(a, b) {
  if (a.length === 0) return b.length;
  if (b.length === 0) return a.length;
  const m = [];
  for (let i = 0; i <= b.length; i++) m[i] = [i];
  for (let j = 0; j <= a.length; j++) m[0][j] = j;
  for (let i = 1; i <= b.length; i++)
    for (let j = 1; j <= a.length; j++)
      m[i][j] = b[i - 1] === a[j - 1]
        ? m[i - 1][j - 1]
        : Math.min(m[i - 1][j - 1] + 1, m[i][j - 1] + 1, m[i - 1][j] + 1);
  return m[b.length][a.length];
}

module.exports = ChatDatabase;

// ── Standalone exports for reminders API ──
module.exports.ReminderMethods = {
  create(db, chatName, remindAt, message, note = '') {
    db.prepare('INSERT INTO reminders (chat_name, remind_at, message, note) VALUES (?, ?, ?, ?)').run(chatName, remindAt, message, note);
  },
  getDue(db) {
    return db.prepare("SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= strftime('%s','now') ORDER BY remind_at ASC LIMIT 10").all();
  },
  markSent(db, id) {
    db.prepare("UPDATE reminders SET status = 'sent', sent_at = strftime('%s','now') WHERE id = ?").run(id);
  },
  cancel(db, id) {
    db.prepare("UPDATE reminders SET status = 'cancelled' WHERE id = ?").run(id);
  },
  getPending(db, chatName) {
    return db.prepare("SELECT * FROM reminders WHERE chat_name = ? AND status = 'pending' ORDER BY remind_at ASC").all(chatName);
  },
};

// ── Standalone exports for corrections API ──
module.exports.CorrectionMethods = {
  add(db, chatName, pattern, instruction = '') {
    db.prepare('INSERT INTO corrections (chat_name, pattern, instruction) VALUES (?, ?, ?)').run(chatName, pattern, instruction);
  },
  getActive(db, chatName) {
    return db.prepare("SELECT * FROM corrections WHERE chat_name = ? AND active = 1 ORDER BY created_at DESC LIMIT 20").all(chatName);
  },
  deactivate(db, id) {
    db.prepare("UPDATE corrections SET active = 0 WHERE id = ?").run(id);
  },
  getAll(db) {
    return db.prepare("SELECT c.*, ch.name as chat_name FROM corrections c JOIN chats ch ON c.chat_name = ch.name WHERE c.active = 1 ORDER BY c.created_at DESC").all();
  },
};
