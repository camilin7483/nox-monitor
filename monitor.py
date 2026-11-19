#!/usr/bin/env python3
"""
Nox Monitor v2 — Advanced Dashboard for WhatsApp Auto-Responder
Dark-themed, multi-tab, with diagnostics, charts & bot control.
"""
import json
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox, font
from pathlib import Path
from datetime import datetime, timezone
from collections import deque, Counter
import re as _re
import os
import signal
import math
import threading
import queue
import time
import unicodedata

from assistant_ui import AssistantPanels
from nox_paths import APP_DIR, DATA_DIR, HIDDEN_CONSOLE, NODE, PACKAGED

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

DIR = DATA_DIR
STATE_FILE = DIR / 'responder-state.json'
LOG_FILE = DIR / 'responder.log'
LOCK_FILE = DIR / 'responder.lock'
BROWSER_STATUS_FILE = DIR / 'browser-status.json'
CONFIG_FILE = DIR / 'nox-config.json'
START_SH = APP_DIR / 'start.sh'
STOP_SH = APP_DIR / 'stop.sh'
RESPONDER_JS = APP_DIR / 'responder.js'
REFRESH_MS = 3000
LOG_TAIL = 500
TZ_LOCAL = datetime.now(timezone.utc).astimezone().tzinfo
LOG_TIMESTAMP = r'\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\]'


# ── helpers ──────────────────────────────────────────────────────────
def _atomic_write_text(path: Path, content: str, mode: int = 0o600):
    """Write atomically via tmp+rename to avoid torn reads on shared files."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    # Ensure parent exists
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(content, encoding='utf-8')
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    # atomic replace
    tmp.replace(path)

def _atomic_write_json(path: Path, data, mode: int = 0o600):
    content = json.dumps(data, indent=2, ensure_ascii=False)
    _atomic_write_text(path, content, mode=mode)

def load_env_value(name: str) -> str:
    """Read a KEY=VALUE pair from the .env file next to this script."""
    env_file = DIR / '.env'
    try:
        if not env_file.exists():
            return ''
        for line in env_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            if key.strip() == name:
                return value.strip().strip('"').strip("'")
    except OSError:
        return ''
    return ''

def bot_pid():
    if PACKAGED:
        from runtime_control import running_process
        active = running_process()
        return (active['pid'], active['startedAt']) if active else (None, None)
    try:
        raw = LOCK_FILE.read_text(encoding='utf-8')
        lock = json.loads(raw)
        pid = int(lock['pid'])
        # Validate pid range
        if pid <= 0 or pid > 4194304:
            return None, None
        process_dir = Path('/proc') / str(pid)
        command = (process_dir / 'cmdline').read_bytes().split(b'\0')
        names = (b'responder.js', b'./responder.js', os.fsencode(RESPONDER_JS.resolve()))
        if (not command or os.path.basename(command[0]) not in (b'node', b'nodejs')
                or not any(arg in names for arg in command[1:])
                or (process_dir / 'cwd').resolve(strict=True) != DIR.resolve()):
            return None, None
        return pid, lock.get('startedAt', '')
    except (FileNotFoundError, json.JSONDecodeError, ValueError, KeyError, subprocess.CalledProcessError, OSError):
        return None, None

def bot_running():
    return bot_pid()[0] is not None

def tail_log(n=LOG_TAIL):
    """Tail last n lines without loading entire file into memory twice."""
    if not LOG_FILE.exists():
        return []
    try:
        # Use deque to keep only last n lines, streaming file
        from collections import deque as _dq
        lines = _dq(maxlen=n)
        with LOG_FILE.open('r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                stripped = line.rstrip('\n')
                if stripped:
                    lines.append(stripped)
        return list(lines)
    except (OSError, UnicodeDecodeError):
        return []

def fmt_ago(ts):
    if not ts or ts == 0: return '\u2014'
    try:
        if isinstance(ts, (int, float)):
            # Heuristic: timestamps > 1e12 are ms, else s
            val = float(ts)
            if val > 1e12:
                val = val / 1000
            elif val > 1e10:
                val = val / 1000
            dt = datetime.fromtimestamp(val, tz=TZ_LOCAL)
        else:
            dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00')).astimezone(TZ_LOCAL)
        s = int((datetime.now(TZ_LOCAL) - dt).total_seconds())
        if s < 0: return 'now'
        if s < 60: return f'{s}s'
        if s < 3600: return f'{s//60}m {s%60}s'
        return f'{s//3600}h {s%3600//60}m'
    except (ValueError, OSError, OverflowError, TypeError):
        return '\u2014'

def parse_metric(line):
    """Parse [METRIC] lines into dict. Handles values with spaces by preserving remainder."""
    m = _re.fullmatch(rf'(?:{LOG_TIMESTAMP} )?\[METRIC\] (\w+) (.+)', line)
    if m:
        key = m.group(1)
        data = {}
        raw = m.group(2)
        # Split on spaces but keep key=value together; values are assumed no spaces except chat names replaced with _
        for pair in raw.split():
            if '=' in pair:
                k, v = pair.split('=', 1)
                data[k] = v
        return key, data
    return None, None


def private_log_summary(line):
    """Build an event from known metadata without showing any original chat text."""
    event = _re.fullmatch(rf'({LOG_TIMESTAMP}) (.+)', line)
    if not event:
        return None
    timestamp, body = event.groups()
    if body.startswith('[METRIC] gen '):
        metric = generation_metric(body)
        if metric is None:
            return None
        duration, success, _ = metric
        result = 'respuesta generada' if success else 'generación fallida'
        return f'{timestamp} [METRIC] {result} · {duration:g} ms'
    if _re.fullmatch(r'\[STATS\] \d+ respuestas \| IA: \d+ \| Fallback: \d+ \| \d+% IA', body):
        return f'{timestamp} {body}'
    if body in ('Cargando WhatsApp Web…', 'WhatsApp listo.', 'MONITORING'):
        return f'{timestamp} {body}'
    if _re.fullmatch(r'Session start #\d+', body):
        return f'{timestamp} Abriendo sesión de WhatsApp'
    if body.startswith(('Session stopped:', 'FATAL:')):
        return f'{timestamp} Error de sesión o inicio'
    if body.startswith('[AI]'):
        return f'{timestamp} [AI] Error de generación · consulta Modelos'
    if _re.match(r'→ AI \(\d+ms\):', body):
        return f'{timestamp} [AI] Respuesta generada'
    if body.startswith(('→ AI fallback', '[FALLBACK ACTIVATED]')):
        return f'{timestamp} [AI] Generación fallida'
    return None


def generation_metric(line):
    key, data = parse_metric(line)
    if key != 'gen':
        return None
    duration = data.get('time_ms', '')
    success = data.get('success')
    if not _re.fullmatch(r'\d{1,9}(?:\.\d{1,3})?', duration) or success not in ('true', 'false'):
        return None
    return float(duration), success == 'true', data.get('chat')


def fmt_number(n):
    if n >= 1000000: return f'{n/1e6:.1f}M'
    if n >= 1000: return f'{n/1e3:.1f}K'
    return str(n)

def notify(title, msg):
    try:
        subprocess.run(['notify-send', title, msg], capture_output=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        pass

def read_nox_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

def write_nox_config(data):
    _atomic_write_json(CONFIG_FILE, data, mode=0o600)

def _get_api_token():
    tok = os.environ.get('NOX_API_TOKEN', '').strip()
    if tok:
        return tok
    try:
        cfg = read_nox_config()
        if cfg.get('api_token'):
            return str(cfg['api_token']).strip()
    except (OSError, AttributeError):
        pass
    return 'nox-token'

def _sanitize_export_name(name: str) -> str:
    # Keep alnum, hyphen, underscore, limit 30 chars
    safe = _re.sub(r'[^a-zA-Z0-9_-]', '_', name.strip())[:30]
    safe = _re.sub(r'_+', '_', safe).strip('_')
    return safe or 'chat'

def edit_responder_js(key, value):
    """Edit a constant in responder.js using regex — atomic with .bak and validation."""
    try:
        js = RESPONDER_JS.read_text(encoding='utf-8')
    except OSError as e:
        raise RuntimeError(f'Cannot read responder.js: {e}') from e
    if key in ('POLL_MS', 'MAX_REPLY_LENGTH', 'COOLDOWN_DEFAULT_MS'):
        numeric = int(value)
        if numeric <= 0 or numeric > 10_000_000:
            raise ValueError(f'{key} out of range: {numeric}')
        replacement = f'const {key} = {numeric};'
        if key == 'COOLDOWN_DEFAULT_MS':
            replacement += '  // normal cooldown'
    elif key == 'OLLAMA_MODEL':
        val = str(value).strip()
        if not val or len(val) > 100 or "'" in val or '"' in val or '\n' in val:
            raise ValueError(f'Invalid model name: {val}')
        replacement = f"const OLLAMA_MODEL = '{val}';        // Primary - better quality"
    elif key == 'OLLAMA_MODEL_FAST':
        val = str(value).strip()
        if not val or len(val) > 100 or "'" in val or '"' in val or '\n' in val:
            raise ValueError(f'Invalid model name: {val}')
        replacement = f"const OLLAMA_MODEL_FAST = '{val}';   // Fallback - faster"
    else:
        raise ValueError(f'Unsupported config key: {key}')

    pattern = rf"^const {key}\s*=.*$"
    js_new, count = _re.subn(pattern, replacement, js, count=1, flags=_re.MULTILINE)
    if count == 0:
        raise RuntimeError(f'Could not update {key} in responder.js')
    # Create backup
    try:
        bak = RESPONDER_JS.with_suffix('.js.bak')
        if not bak.exists():
            bak.write_text(js, encoding='utf-8')
    except OSError:
        pass
    # Atomic write preserving executable bit
    try:
        orig_mode = RESPONDER_JS.stat().st_mode
    except OSError:
        orig_mode = 0o644
    _atomic_write_text(RESPONDER_JS, js_new, mode=orig_mode)
    # Validate syntax with node --check (best effort, don't fail if node missing)
    try:
        subprocess.run([NODE, '--check', str(RESPONDER_JS)], check=True, capture_output=True, timeout=5, **HIDDEN_CONSOLE)
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
        # Rollback on syntax error
        try:
            _atomic_write_text(RESPONDER_JS, js, mode=orig_mode)
        except OSError:
            pass
        raise RuntimeError(f'responder.js syntax check failed after edit: {e}') from e


# ── tema de Nox ────────────────────────────────────────────────────────
class NoxTheme:
    BG = '#151815'
    BG2 = '#1c211c'
    BG3 = '#252b24'
    RAIL = '#111611'
    FG = '#edf1e7'
    FG2 = '#a4b09f'
    GREEN = '#b5df91'
    RED = '#ff9b92'
    YELLOW = '#e9c47f'
    BLUE = '#9ec7e8'
    ORANGE = '#ecb088'
    PURPLE = '#c9b8e6'
    CYAN = '#9bd8c4'
    SEL = '#30432e'
    BORDER = '#374235'
    MONO = 'DejaVu Sans Mono'

    @classmethod
    def setup(cls):
        style = ttk.Style()
        root = style.master
        families = set(font.families(root))
        cls.MONO = next((name for name in (
            'IBM Plex Mono', 'Cascadia Mono', 'JetBrainsMono Nerd Font Mono', 'DejaVu Sans Mono'
        ) if name in families), 'TkFixedFont')
        for name in ('TkDefaultFont', 'TkTextFont', 'TkFixedFont'):
            font.nametofont(name).configure(family=cls.MONO, size=10)

        style.theme_create('nox', parent='clam')
        style.theme_use('nox')
        style.configure('.', background=cls.BG, foreground=cls.FG, fieldbackground=cls.BG2,
                        selectbackground=cls.SEL, selectforeground=cls.FG,
                        troughcolor=cls.BG, arrowcolor=cls.FG2, borderwidth=0,
                        bordercolor=cls.BORDER, lightcolor=cls.BG2, darkcolor=cls.BG2)
        style.configure('TButton', background=cls.BG3, foreground=cls.FG, focuscolor=cls.GREEN,
                        borderwidth=1, relief='flat', padding=(10, 7),
                        font=(cls.MONO, 9, 'bold'))
        style.map('TButton', background=[('pressed', cls.SEL), ('active', '#354033'),
                                         ('focus', '#354033')], foreground=[('disabled', cls.FG2)])
        style.configure('Accent.TButton', background=cls.GREEN, foreground=cls.RAIL,
                        focuscolor=cls.RAIL)
        style.map('Accent.TButton', background=[('disabled', cls.BG3), ('active', '#cae8b4'),
                                                ('focus', '#cae8b4')],
                  foreground=[('disabled', cls.FG2)])
        style.configure('Quiet.TButton', background=cls.RAIL, foreground=cls.FG2,
                        bordercolor=cls.BORDER, relief='solid')
        style.configure('Treeview', background=cls.BG2, rowheight=27)
        style.map('Treeview', background=[('selected', cls.SEL)], foreground=[('selected', cls.FG)])
        style.configure('Treeview.Heading', background=cls.BG3, padding=(7, 5), relief='flat')
        style.configure('TProgressbar', background=cls.GREEN, troughcolor=cls.BG3)
        style.configure('TScrollbar', background=cls.BG3, width=12)
        style.layout('Workspace.TNotebook.Tab', [])
        style.configure('Workspace.TNotebook', borderwidth=0, tabmargins=0)

        for name, background, padding in (
            ('Nav.TRadiobutton', cls.RAIL, (10, 6)),
            ('Provider.TRadiobutton', cls.BG2, (8, 6)),
        ):
            style.layout(name, [('Radiobutton.padding', {'sticky': 'nswe', 'children': [
                ('Radiobutton.focus', {'sticky': 'nswe', 'children': [
                    ('Radiobutton.label', {'sticky': 'w'})
                ]})
            ]})])
            style.configure(name, background=background, foreground=cls.FG2, focuscolor=cls.GREEN,
                            padding=padding, font=(cls.MONO, 10))
            style.map(name, background=[('selected', cls.SEL), ('active', cls.BG3), ('focus', cls.BG3)],
                      foreground=[('selected', cls.GREEN), ('active', cls.FG)])

        for name in ('Sidebar.TFrame', 'Bar.TFrame'):
            style.configure(name, background=cls.RAIL)
        style.configure('Surface.TFrame', background=cls.BG2)
        style.configure('Brand.TLabel', background=cls.RAIL, foreground=cls.FG, font=(cls.MONO, 22, 'bold'))
        style.configure('SidebarMeta.TLabel', background=cls.RAIL, foreground=cls.FG2, font=(cls.MONO, 9))
        style.configure('BarMuted.TLabel', background=cls.RAIL, foreground=cls.FG2, font=(cls.MONO, 9))
        style.configure('Title.TLabel', font=(cls.MONO, 24, 'bold'))
        style.configure('Section.TLabel', font=(cls.MONO, 11, 'bold'))
        style.configure('Muted.TLabel', foreground=cls.FG2)
        style.configure('Signal.TLabel', foreground=cls.GREEN)
        style.configure('Badge.TLabel', background=cls.BG3, foreground=cls.FG,
                        padding=(7, 4), font=(cls.MONO, 9))
        style.configure('SurfaceTitle.TLabel', background=cls.BG2, foreground=cls.GREEN, font=(cls.MONO, 10, 'bold'))
        style.configure('Surface.TLabel', background=cls.BG2, foreground=cls.FG)
        style.configure('SurfaceMuted.TLabel', background=cls.BG2, foreground=cls.FG2, font=(cls.MONO, 9))
        style.configure('TEntry', padding=(7, 6), insertcolor=cls.FG, borderwidth=1)
        style.map('TEntry', bordercolor=[('focus', cls.GREEN)])
        style.configure('TCombobox', padding=(7, 6), insertcolor=cls.FG, borderwidth=1)
        style.map('TCombobox', fieldbackground=[('readonly', cls.BG2)], foreground=[('readonly', cls.FG)],
                  bordercolor=[('focus', cls.GREEN)])
        style.configure('TLabelframe', borderwidth=1, relief='solid', bordercolor=cls.BORDER)
        style.configure('TLabelframe.Label', foreground=cls.FG2, font=(cls.MONO, 10, 'bold'))
        for resource, value in {
            'background': cls.BG2, 'foreground': cls.FG, 'selectBackground': cls.SEL,
            'selectForeground': cls.FG, 'font': (cls.MONO, 10),
        }.items():
            root.option_add('*TCombobox*Listbox.' + resource, value)
        return style


# ── sparkline widget ──────────────────────────────────────────────────
class Sparkline(tk.Canvas):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=NoxTheme.BG2, highlightthickness=0, **kw)
        self.data = deque(maxlen=60)
        self._max = 1
        self.bind('<Configure>', lambda _event: self._draw())

    def add(self, val):
        self.data.append(val)
        if val > self._max: self._max = val
        self._draw()

    def set_data(self, data):
        self.data = deque(data, maxlen=60)
        self._max = max(data) if data else 1
        self._draw()

    def _draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        for fraction in (0.25, 0.5, 0.75):
            self.create_line(12, h * fraction, w - 12, h * fraction, fill=NoxTheme.BORDER)
        if not self.data:
            self.create_text(w // 2, h // 2, text='\u2014', fill=NoxTheme.FG2, font=(NoxTheme.MONO, 9))
            return
        vals = list(self.data)
        mx = max(vals) or 1
        step = (w - 36) / (len(vals) - 1) if len(vals) > 1 else 0
        pts = []
        for i, v in enumerate(vals):
            x = 12 + i * step
            y = h - (v / mx) * (h - 10) - 5
            pts.extend([x, y])
        if len(pts) >= 4:
            try:
                self.create_line(pts, fill=NoxTheme.GREEN, width=2, smooth=True)
            except tk.TclError:
                self.create_line(pts, fill=NoxTheme.GREEN, width=2)
        lv = vals[-1]
        lx = 12 + (len(vals) - 1) * step
        ly = h - (lv / mx) * (h - 10) - 5
        self.create_oval(lx - 4, ly - 4, lx + 4, ly + 4, fill=NoxTheme.GREEN, outline='')
        self.create_text(lx - 3, max(ly - 10, 9), text=f'{lv:.0f}', fill=NoxTheme.GREEN,
                         font=(NoxTheme.MONO, 8, 'bold'), anchor='e')


# ── log streamer ──────────────────────────────────────────────────────
class LogStreamer(threading.Thread):
    """Background thread that tails the log file handles rotation/inode changes."""

    def __init__(self, path, q):
        super().__init__(daemon=True)
        self.path = path
        self.q = q
        self._pos = 0
        self._buf = ''
        self._inode = None
        self._stop = threading.Event()

    def run(self):
        try:
            with open(self.path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(0, 2)
                self._pos = f.tell()
                try:
                    self._inode = os.fstat(f.fileno()).st_ino
                except OSError:
                    self._inode = None
        except (OSError, FileNotFoundError):
            pass

        while not self._stop.is_set():
            try:
                # Check inode to detect rotation
                try:
                    st = os.stat(self.path)
                    if self._inode is not None and st.st_ino != self._inode:
                        self._pos = 0
                        self._buf = ''
                        self._inode = st.st_ino
                    elif st.st_size < self._pos:
                        # Truncated/rotated
                        self._pos = 0
                        self._buf = ''
                except (FileNotFoundError, OSError):
                    self._pos = 0
                    self._buf = ''
                    self._inode = None
                with open(self.path, 'r', encoding='utf-8', errors='ignore') as f:
                    try:
                        cur_inode = os.fstat(f.fileno()).st_ino
                        if self._inode is None:
                            self._inode = cur_inode
                    except OSError:
                        pass
                    fsz = f.seek(0, 2)
                    fsz = f.tell()
                    if self._pos > fsz:
                        self._pos = 0
                        self._buf = ''
                    f.seek(self._pos)
                    new_data = f.read(64 * 1024)  # cap read size
                    if new_data:
                        self._pos = f.tell()
                        lines = (self._buf + new_data).split('\n')
                        self._buf = lines.pop() if lines else ''
                        for line in lines:
                            stripped = line.strip()
                            if stripped:
                                # Avoid queue unbounded growth
                                if self.q.qsize() < 5000:
                                    self.q.put(stripped)
            except (FileNotFoundError, PermissionError, OSError):
                self._pos = 0
                self._buf = ''
            for _ in range(10):
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    def stop(self):
        self._stop.set()


# ── main app ──────────────────────────────────────────────────────────
class MonitorApp(AssistantPanels):
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Nox Monitor · WhatsApp')
        self.root.geometry('1200x800')
        self.root.minsize(1020, 680)
        self.root.configure(bg=NoxTheme.BG)
        NoxTheme.setup()
        self.root.bind('<Control-r>', lambda _event: self._refresh_now())
        self._refresh_timer = None
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        # data caches
        self._state = {}
        self._lines = []
        self._bridge = {}  # bot-stats.json cache
        self._pid, self._started = bot_pid()
        self._gen_times = deque(maxlen=200)
        self._gen_errors = 0
        self._gen_success = 0
        self._metrics = {chat: deque(maxlen=20) for chat in []}
        self._chat_times = {}
        self._log_search_term = tk.StringVar()
        self._log_filter = tk.StringVar(value='all')
        self._autoscroll = tk.BooleanVar(value=True)
        self._private_history_visible = False
        self._selected_chat = None
        self._memory_chat = None
        self._restart_count = 0
        self._bot_action_busy = False
        self._error_rate_notified = False
        self._tray_icon = None

        # session history
        self._session_history = []

        # model stats tracking (from bridge)
        self._configured_model_stats = []
        self._model_stats_7b = {'tries': 0, 'success': 0, 'avgMs': 0}
        self._model_stats_3b = {'tries': 0, 'success': 0, 'avgMs': 0}
        self._model_stats_ds = {'tries': 0, 'success': 0, 'avgMs': 0}
        self._model_stats_lm = {'tries': 0, 'success': 0, 'avgMs': 0}

        # bridge file cache for moderator
        self._pending_approvals = []

        # recent gens tracker (for error rate notification)
        self._recent_gens = deque(maxlen=10)

        # config fields
        self._config_max_reply = tk.StringVar(value='300')
        self._config_cooldown = tk.StringVar(value='60000')
        self._config_poll_ms = tk.StringVar(value='3000')
        self._config_model = tk.StringVar(value='qwen2.5:3b')
        self._config_lm_model = tk.StringVar(value='dolphin3.0-qwen2.5-1.5b')
        self._config_lm_url = tk.StringVar(value='http://127.0.0.1:1234/v1')
        self._config_browser = tk.StringVar(value='bundled')
        self._load_config()

        # DeepSeek (external API) — key from .env
        self._deepseek_enabled = bool(load_env_value('DEEPSEEK_API_KEY'))

        # load initial log
        self._lines = tail_log()
        self._parse_log_metrics()

        # start log streamer
        self._log_queue = queue.Queue()
        self._streamer = LogStreamer(LOG_FILE, self._log_queue)
        self._streamer.start()

        self._build_ui()
        self._setup_tray()
        self._poll_log_queue()
        self._refresh()
        if PACKAGED and not (DIR / '.bienvenida').exists():
            self.root.after(450, self._first_run)
        self.root.mainloop()

    def _load_config(self):
        try:
            # Read actual values from responder.js first (legacy)
            js_text = RESPONDER_JS.read_text()
            js_model = _re.search(r"^const OLLAMA_MODEL\s*=\s*'([^']+)'", js_text, _re.MULTILINE)
            js_max = _re.search(r"^const MAX_REPLY_LENGTH\s*=\s*(\d+)", js_text, _re.MULTILINE)
            js_cooldown = _re.search(r"^const COOLDOWN_DEFAULT_MS\s*=\s*(\d+)", js_text, _re.MULTILINE)
            js_poll = _re.search(r"^const POLL_MS\s*=\s*(\d+)", js_text, _re.MULTILINE)
            if js_model: self._config_model.set(js_model.group(1))
            if js_max: self._config_max_reply.set(js_max.group(1))
            if js_cooldown: self._config_cooldown.set(js_cooldown.group(1))
            if js_poll: self._config_poll_ms.set(js_poll.group(1))
            # Fuente única nueva: nox-config.json + .env (LM Studio / navegador)
            cfg = read_nox_config()
            for key, variable in (('max_reply_length', self._config_max_reply),
                                  ('cooldown', self._config_cooldown), ('poll_ms', self._config_poll_ms)):
                if PACKAGED and isinstance(cfg.get(key), int):
                    variable.set(str(cfg[key]))
            llm = cfg.get('llm', {}) if isinstance(cfg, dict) else {}
            browser = cfg.get('browser', {}) if isinstance(cfg, dict) else {}
            if llm.get('model'): self._config_lm_model.set(str(llm['model']))
            if llm.get('baseUrl'): self._config_lm_url.set(str(llm['baseUrl']))
            if browser.get('provider'): self._config_browser.set(str(browser['provider']).lower())
            env_lm_model = load_env_value('LMSTUDIO_MODEL')
            env_lm_url = load_env_value('LMSTUDIO_URL')
            env_browser = load_env_value('BROWSER_PROVIDER')
            if env_lm_model: self._config_lm_model.set(env_lm_model)
            if env_lm_url: self._config_lm_url.set(env_lm_url)
            if env_browser: self._config_browser.set(env_browser.lower())
        except:
            pass

    # ── ui builder ──────────────────────────────────────────────────
    def _build_ui(self):
        shell = ttk.Frame(self.root)
        shell.pack(fill='both', expand=True)
        footer = ttk.Frame(shell, style='Bar.TFrame', padding=(14, 7))
        footer.pack(side='bottom', fill='x')
        footer_line = tk.Frame(shell, height=1, bg=NoxTheme.BORDER)
        footer_line.pack(side='bottom', fill='x')
        sidebar = ttk.Frame(shell, style='Sidebar.TFrame', width=196, padding=(12, 12))
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        tk.Frame(shell, width=1, bg=NoxTheme.BORDER).pack(side='left', fill='y')

        brand = ttk.Frame(sidebar, style='Sidebar.TFrame')
        brand.pack(fill='x', padx=6, pady=(2, 0))
        mark = tk.Canvas(brand, width=28, height=26, bg=NoxTheme.RAIL, highlightthickness=0)
        mark.pack(side='left', padx=(0, 8))
        mark.create_line(1, 16, 7, 16, 12, 5, 17, 23, 22, 12, 27, 12, fill=NoxTheme.GREEN, width=2)
        ttk.Label(brand, text='nox', style='Brand.TLabel').pack(side='left')
        ttk.Label(sidebar, text='Monitor de WhatsApp', style='SidebarMeta.TLabel').pack(
            anchor='w', padx=6, pady=(2, 16))

        main = ttk.Frame(shell)
        main.pack(side='left', fill='both', expand=True)
        top = ttk.Frame(main, padding=(16, 9))
        top.pack(fill='x')
        self._indicator = tk.Canvas(top, width=12, height=12, bg=NoxTheme.BG, highlightthickness=0)
        self._indicator.pack(side='left', padx=(0, 7))
        self._dot = self._indicator.create_oval(2, 2, 10, 10, fill=NoxTheme.FG2, outline='')
        self._status_lbl = ttk.Label(top, text='Cargando…', style='Section.TLabel')
        self._status_lbl.pack(side='left')
        self._pid_lbl = ttk.Label(top, style='Muted.TLabel')
        self._pid_lbl.pack(side='left', padx=(10, 0))
        self._uptime_lbl = ttk.Label(top, style='Muted.TLabel')
        self._ollama_lbl = ttk.Label(footer, style='BarMuted.TLabel', wraplength=315)
        self._ollama_lbl.pack(side='left')

        buttons = ttk.Frame(top)
        buttons.pack(side='right')
        self._bot_buttons = []
        for label, command, style in (
            ('Reiniciar', self._restart_bot, 'TButton'),
            ('Detener', self._stop_bot, 'TButton'),
            ('Iniciar', self._start_bot, 'Accent.TButton'),
        ):
            button = ttk.Button(buttons, text=label, command=command, style=style)
            button.pack(side='left', padx=(4, 0))
            self._bot_buttons.append(button)
        tk.Frame(main, height=1, bg=NoxTheme.BORDER).pack(fill='x')

        notebook = ttk.Notebook(main, style='Workspace.TNotebook', takefocus=False)
        notebook.pack(fill='both', expand=True)
        self._build_dashboard(notebook)
        self._build_assistant_panels(notebook)
        self._build_chats_tab(notebook)
        self._build_moderator_tab(notebook)
        self._build_logs_tab(notebook)
        self._build_trends_tab(notebook)
        self._build_diagnostics(notebook)
        self._build_plugins_tab(notebook)
        self._build_config_tab(notebook)

        self._notebook = notebook
        self._nav_selection = tk.StringVar(value=notebook.select())
        tabs = {notebook.tab(tab, 'text').strip(): tab for tab in notebook.tabs()}
        self._tabs = tabs
        groups = (
            ('Operación', ('Resumen', 'Conversaciones', 'Moderación', 'Registro')),
            ('Inteligencia', ('Modelos', 'Instrucciones')),
            ('Sistema', ('Tendencias', 'Diagnóstico', 'Plugins', 'Ajustes')),
        )
        commands = [(name, tabs[name], section) for section, names in groups for name in names]
        for section, names in groups:
            ttk.Label(sidebar, text=section, style='SidebarMeta.TLabel').pack(
                anchor='w', padx=7, pady=(9, 3))
            for name in names:
                tab = tabs[name]
                destination = lambda target=tab: notebook.select(target)
                item = ttk.Radiobutton(sidebar, text=name, value=tab, variable=self._nav_selection,
                                       style='Nav.TRadiobutton', command=destination)
                item.pack(fill='x', pady=1)
                item.bind('<Return>', lambda _event, widget=item: widget.invoke())
        notebook.bind('<<NotebookTabChanged>>', lambda _event: self._nav_selection.set(notebook.select()))

        open_palette = lambda: self._open_command_palette(notebook, commands)
        ttk.Button(sidebar, text='Ctrl+P   Ir a…', style='Quiet.TButton', command=open_palette).pack(
            side='bottom', fill='x', pady=(7, 1))
        self.root.bind('<Control-p>', lambda _event: open_palette())
        ttk.Label(footer, text='Ctrl+R  Actualizar', style='BarMuted.TLabel').pack(side='right', padx=(12, 0))
        self._build_privacy_notice(footer).pack(side='right', padx=(12, 0))

    def _first_run(self):
        (DIR / '.bienvenida').touch(mode=0o600)
        assets = Path(os.environ.get('NOX_ASSETS', APP_DIR / 'assets'))
        icon = assets / 'nox.png'
        if icon.is_file():
            self._setup_icon = tk.PhotoImage(file=icon)
            self.root.iconphoto(True, self._setup_icon)

        setup = tk.Toplevel(self.root, bg=NoxTheme.BG)
        setup.title('Empieza con Nox Monitor')
        setup.geometry('700x460')
        setup.transient(self.root)
        body = ttk.Frame(setup, padding=25)
        body.pack(side='right', fill='both', expand=True)
        image_path = assets / 'banner.png'
        if image_path.is_file():
            setup.banner = tk.PhotoImage(file=image_path)
            tk.Label(setup, image=setup.banner, bg=NoxTheme.BG, bd=0).pack(side='left', fill='y')
        ttk.Label(body, text='Bienvenido a Nox', style='Title.TLabel').pack(anchor='w', pady=(10, 12))
        ttk.Label(body, text='Escanea el QR de WhatsApp en la ventana privada. Tu sesión quedará guardada en este equipo.',
                  wraplength=380).pack(anchor='w', pady=(0, 12))
        ttk.Label(body, text='Después, elige un proveedor en Modelos. Puedes usar Ollama o conectar una API con tu propia clave.',
                  style='Muted.TLabel', wraplength=380).pack(anchor='w', pady=(0, 12))
        status = ttk.Label(body, text='Abriendo WhatsApp…', style='Signal.TLabel', wraplength=380)
        status.pack(anchor='w', pady=(0, 8))
        ttk.Button(body, text='Abrir Modelos', style='Accent.TButton',
                   command=lambda: (setup.destroy(), self._notebook.select(self._tabs['Modelos']))).pack(
                       anchor='w', pady=(12, 6))
        ttk.Button(body, text='Continuar al panel', command=setup.destroy).pack(anchor='w', pady=(2, 0))

        def update():
            if setup.winfo_exists():
                status.configure(text=self._status_lbl.cget('text'))
                setup.after(800, update)
        update()
        self._start_bot()

    def _open_command_palette(self, notebook, commands):
        previous_focus = self.root.focus_get()
        popup = tk.Toplevel(self.root, bg=NoxTheme.BG2, padx=16, pady=14)
        popup.title('Ir a… — Nox Monitor')
        popup.transient(self.root)
        popup.resizable(False, False)
        popup.geometry(f'470x398+{self.root.winfo_rootx() + (self.root.winfo_width() - 470) // 2}'
                       f'+{self.root.winfo_rooty() + 68}')
        search = tk.StringVar(master=popup)
        ttk.Label(popup, text='Ir a…', style='SurfaceTitle.TLabel').pack(anchor='w', pady=(0, 7))
        entry = ttk.Entry(popup, textvariable=search)
        entry.pack(fill='x')
        entries = tk.Listbox(popup, bg=NoxTheme.BG2, fg=NoxTheme.FG, selectbackground=NoxTheme.SEL,
                             selectforeground=NoxTheme.GREEN, highlightthickness=1,
                             highlightbackground=NoxTheme.BORDER, highlightcolor=NoxTheme.GREEN,
                             borderwidth=0, exportselection=False, activestyle='none',
                             font=(NoxTheme.MONO, 10), height=11)
        entries.pack(fill='both', expand=True, pady=(8, 6))
        hint = ttk.Label(popup, style='SurfaceMuted.TLabel')
        hint.pack(anchor='w')
        matches = []

        def search_key(value):
            normalized = unicodedata.normalize('NFKD', value.casefold())
            return ''.join(c for c in normalized if not unicodedata.combining(c))

        def filter_commands(*_event):
            words = search_key(search.get().strip().lstrip('/')).split()
            matches[:] = [
                (name, target, group) for name, target, group in commands
                if all(word in search_key(f'{name} {group}') for word in words)
            ]
            entries.delete(0, 'end')
            for name, _, group in matches:
                entries.insert('end', f'  {name:<18} {group}')
            if matches:
                entries.selection_set(0)
                entries.activate(0)
            hint.configure(text='↑ ↓   Seleccionar   Intro   Abrir   Esc   Cerrar' if matches
                           else 'No hay secciones con esa búsqueda.')

        def move(delta):
            if matches:
                current = entries.curselection()
                row = min(len(matches) - 1, max(0, (current[0] if current else 0) + delta))
                entries.selection_clear(0, 'end')
                entries.selection_set(row)
                entries.activate(row)
                entries.see(row)
            return 'break'

        def close(_event=None):
            popup.destroy()
            if previous_focus and previous_focus.winfo_viewable():
                previous_focus.focus_set()
            return 'break'

        def open_selection(_event=None):
            selected = entries.curselection()
            if selected:
                notebook.select(matches[selected[0]][1])
                return close()
            return 'break'

        search.trace_add('write', filter_commands)
        filter_commands()
        entry.bind('<Up>', lambda _event: move(-1))
        entry.bind('<Down>', lambda _event: move(1))
        popup.bind('<Return>', open_selection)
        popup.bind('<Escape>', close)
        entries.bind('<Double-1>', open_selection)
        popup.protocol('WM_DELETE_WINDOW', close)
        popup.wait_visibility()
        popup.grab_set()
        entry.focus_set()
        return 'break'

    def _build_dashboard(self, nb):
        dash = ttk.Frame(nb, padding=(24, 18, 24, 12))
        nb.add(dash, text='Resumen')
        ttk.Label(dash, text='Resumen', style='Title.TLabel').grid(row=0, columnspan=6, sticky='w')
        ttk.Label(dash, text='Rendimiento de respuestas y actividad del bot.', style='Muted.TLabel').grid(
            row=1, columnspan=6, sticky='w', pady=(5, 18))
        for col in range(6):
            dash.columnconfigure(col, weight=1, uniform='dashboard')
        cards = ttk.Frame(dash)
        cards.grid(row=2, column=0, columnspan=6, sticky='ew')
        for col in range(6):
            cards.columnconfigure(col, weight=1, uniform='stats')
        dash.rowconfigure(3, weight=2)
        dash.rowconfigure(4, weight=1)

        self._cards = {}
        stats = [
            ('ia_pct', 'Tasa de IA', '\u2014', 0),
            ('ia_n', 'Respuestas IA', '\u2014', 1),
            ('fb_n', 'Fallos de IA', '\u2014', 2),
            ('avg_ms', 'Media (ms)', '\u2014', 3),
            ('peak_ms', 'Pico (ms)', '\u2014', 4),
            ('chats', 'Conversaciones', '\u2014', 5),
        ]
        for key, label, _, col in stats:
            c = ttk.Frame(cards, padding=(8, 5))
            c.grid(row=0, column=col, padx=3, pady=(0, 8), sticky='nsew')
            ttk.Label(c, text=label, font=(NoxTheme.MONO, 8)).pack()
            lbl = ttk.Label(c, text='\u2014', font=(NoxTheme.MONO, 23, 'bold'))
            lbl.pack()
            self._cards[key] = lbl

        gen_f = ttk.LabelFrame(dash, text='Tiempo de respuesta', padding=4)
        gen_f.grid(row=3, column=0, columnspan=4, sticky='nsew', padx=(0, 4))
        self._gen_chart = Sparkline(gen_f, height=80)
        self._gen_chart.pack(fill=tk.BOTH, expand=True)

        self._gen_avg_lbl = ttk.Label(gen_f, text='', font=(NoxTheme.MONO, 8))
        self._gen_avg_lbl.pack(anchor=tk.W)

        act_f = ttk.LabelFrame(dash, text='Chats más activos', padding=4)
        act_f.grid(row=3, column=4, columnspan=2, sticky='nsew')
        self._active_list = tk.Text(act_f, height=8, width=1, highlightthickness=0, font=(NoxTheme.MONO, 9), wrap=tk.WORD,
                                    bg=NoxTheme.BG2, fg=NoxTheme.FG, state=tk.DISABLED,
                                    borderwidth=0, padx=4, pady=4)
        self._active_list.pack(fill=tk.BOTH, expand=True)

        prev_f = ttk.LabelFrame(dash, text='Actividad reciente', padding=4)
        prev_f.grid(row=4, column=0, columnspan=6, sticky='nsew', pady=(6, 0))
        self._preview_text = tk.Text(prev_f, height=6, width=1, highlightthickness=0, font=(NoxTheme.MONO, 9), wrap=tk.WORD,
                                     bg=NoxTheme.BG2, fg=NoxTheme.FG, state=tk.DISABLED,
                                     borderwidth=0, padx=4, pady=4)
        self._preview_text.pack(fill=tk.BOTH, expand=True)
        for tag, color in [('green', NoxTheme.GREEN), ('red', NoxTheme.RED),
                           ('yellow', NoxTheme.YELLOW), ('blue', NoxTheme.BLUE),
                           ('dim', NoxTheme.FG2)]:
            self._preview_text.tag_configure(tag, foreground=color)

    def _build_chats_tab(self, nb):
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text='Conversaciones')
        tab.columnconfigure(0, weight=2)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=1)

        cf = ttk.LabelFrame(tab, text='Conversaciones', padding=4)
        cf.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        cols = ('name', 'lang', 'last_in', 'last_out', 'replies', 'avg_ms', 'cd')
        self._chat_tree = ttk.Treeview(cf, columns=cols, show='headings', height=15)
        for c, w in [('name', 130), ('lang', 38), ('last_in', 150), ('last_out', 150), ('replies', 40),
                      ('avg_ms', 65), ('cd', 70)]:
            self._chat_tree.heading(c, text=c.replace('_', ' ').title())
            self._chat_tree.column(c, width=w, anchor=tk.CENTER if c in ('replies', 'avg_ms', 'cd') else tk.W)
        self._chat_tree.bind('<<TreeviewSelect>>', self._on_chat_select)
        sy = ttk.Scrollbar(cf, command=self._chat_tree.yview)
        self._chat_tree.configure(yscrollcommand=sy.set)
        self._chat_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy.pack(side=tk.RIGHT, fill=tk.Y)

        df = ttk.LabelFrame(tab, text='Detalle de conversación', padding=6)
        df.grid(row=0, column=1, sticky='nsew')
        detail_body = ttk.Frame(df)
        detail_body.pack(fill='both', expand=True)
        self._detail_text = tk.Text(detail_body, width=24, font=(NoxTheme.MONO, 10), wrap=tk.WORD,
                                    bg=NoxTheme.BG2, fg=NoxTheme.FG, state=tk.DISABLED,
                                    borderwidth=0, padx=6, pady=6)
        sy2 = ttk.Scrollbar(detail_body, command=self._detail_text.yview)
        self._detail_text.configure(yscrollcommand=sy2.set)
        self._detail_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sy2.pack(side=tk.RIGHT, fill=tk.Y)
        for tag, color in [('green', NoxTheme.GREEN), ('red', NoxTheme.RED),
                           ('yellow', NoxTheme.YELLOW), ('cyan', NoxTheme.BLUE),
                           ('heading', NoxTheme.FG)]:
            self._detail_text.tag_configure(tag, foreground=color)
        self._detail_text.tag_configure('heading', font=(NoxTheme.MONO, 10, 'bold'))

        # Export button at bottom of detail frame
        btn_frame = ttk.Frame(df)
        btn_frame.pack(fill=tk.X, pady=(4, 0))
        self._chat_actions = (
            ttk.Button(btn_frame, text='Exportar Markdown', command=self._export_chat_markdown),
            ttk.Button(btn_frame, text='Consultar memoria', command=self._fetch_chat_from_api),
        )
        for button in self._chat_actions:
            button.pack(side=tk.LEFT, padx=2)

    def _build_logs_tab(self, nb):
        tab = ttk.Frame(nb, padding=6)
        nb.add(tab, text='Registro')

        fb = ttk.Frame(tab)
        fb.pack(fill=tk.X, pady=(0, 4))
        for val, lbl in [('all', 'All'), ('ai', 'AI'), ('error', 'Errors'),
                          ('stats', 'Stats'), ('skip', 'Skip'), ('metric', 'Metrics')]:
            ttk.Radiobutton(fb, text=lbl, variable=self._log_filter,
                            value=val).pack(side=tk.LEFT, padx=2)

        ttk.Label(fb, text='  Buscar').pack(side=tk.LEFT, padx=(10, 2))
        search_e = ttk.Entry(fb, textvariable=self._log_search_term, width=18)
        search_e.pack(side=tk.LEFT)
        search_e.bind('<KeyRelease>', lambda e: self._render_logs())

        ttk.Checkbutton(fb, text='Seguir registro', variable=self._autoscroll).pack(side=tk.LEFT, padx=6)
        self._log_count_lbl = ttk.Label(fb, text='')
        self._log_count_lbl.pack(side=tk.RIGHT, padx=6)
        ttk.Button(fb, text='\u2715', width=3,
                   command=lambda: (self._log_filter.set('all'), self._log_search_term.set(''),
                                    self._render_logs())).pack(side=tk.RIGHT)

        self._log_text = tk.Text(tab, font=(NoxTheme.MONO, 9), wrap=tk.WORD,
                                 bg=NoxTheme.BG2, fg=NoxTheme.FG, insertbackground=NoxTheme.FG,
                                 padx=6, pady=4, state=tk.DISABLED, borderwidth=0)
        ls = ttk.Scrollbar(tab, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=ls.set)
        self._log_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        ls.pack(side=tk.RIGHT, fill=tk.Y)
        for tag, color in [('green', NoxTheme.GREEN), ('red', NoxTheme.RED),
                           ('yellow', NoxTheme.YELLOW), ('blue', NoxTheme.BLUE),
                           ('dim', NoxTheme.FG2), ('highlight', NoxTheme.ORANGE)]:
            self._log_text.tag_configure(tag, foreground=color)
        self._log_text.tag_configure('match', background=NoxTheme.SEL)

    def _build_diagnostics(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text='Diagnóstico')
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.columnconfigure(2, weight=1)
        tab.rowconfigure(2, weight=1)
        tab.rowconfigure(3, weight=0)

        sys_f = ttk.LabelFrame(tab, text='Sistema', padding=8)
        sys_f.grid(row=0, column=0, sticky='nsew', padx=3, pady=3)
        self._diag_sys = {}
        for i, key in enumerate(['pid', 'uptime', 'restarts', 'log_size', 'state_size', 'cpu_ram']):
            f = ttk.Frame(sys_f)
            f.pack(fill=tk.X, pady=1)
            ttk.Label(f, text=key.replace('_', ' ').title() + ':', width=12, anchor=tk.E).pack(side=tk.LEFT)
            self._diag_sys[key] = ttk.Label(f, text='\u2014', anchor=tk.W)
            self._diag_sys[key].pack(side=tk.LEFT, padx=6)

        model_f = ttk.LabelFrame(tab, text='Selección automática y entorno', padding=8)
        model_f.grid(row=0, column=1, sticky='nsew', padx=3, pady=3)
        self._diag_model = {}
        for i, key in enumerate(['provider', 'model', 'browser', 'reachable', 'ping']):
            f = ttk.Frame(model_f)
            f.pack(fill=tk.X, pady=1)
            ttk.Label(f, text=key.replace('_', ' ').title() + ':', width=12, anchor=tk.E).pack(side=tk.LEFT)
            self._diag_model[key] = ttk.Label(f, text='\u2014', anchor=tk.W)
            self._diag_model[key].pack(side=tk.LEFT, padx=6)

        gen_f = ttk.LabelFrame(tab, text='Generation Stats', padding=8)
        gen_f.grid(row=0, column=2, sticky='nsew', padx=3, pady=3)
        self._diag_gen = {}
        for i, key in enumerate(['total', 'ia', 'fb', 'ia_pct', 'avg_ms', 'min_ms', 'max_ms', 'errors']):
            f = ttk.Frame(gen_f)
            f.pack(fill=tk.X, pady=1)
            ttk.Label(f, text=key.replace('_', ' ').title() + ':', width=10, anchor=tk.E).pack(side=tk.LEFT)
            self._diag_gen[key] = ttk.Label(f, text='\u2014', anchor=tk.W)
            self._diag_gen[key].pack(side=tk.LEFT, padx=6)

        dist_f = ttk.LabelFrame(tab, text='Generation Time Distribution', padding=4)
        dist_f.grid(row=1, column=0, columnspan=3, sticky='nsew', pady=6)
        tab.rowconfigure(1, weight=1)
        self._dist_canvas = tk.Canvas(dist_f, height=100, bg=NoxTheme.BG2, highlightthickness=0)
        self._dist_canvas.pack(fill=tk.X, pady=4)

        raw_f = ttk.Frame(tab)
        raw_f.grid(row=2, column=0, columnspan=3, sticky='ew', pady=(4, 0))
        self._diag_raw = {}
        for key, lbl in [('total_lines', 'Total log lines'), ('last_hour', 'Lines last hour'),
                          ('metrics', 'METRIC entries'), ('gen_events', 'Generation events')]:
            f = ttk.Frame(raw_f)
            f.pack(side=tk.LEFT, padx=10)
            ttk.Label(f, text=lbl + ':', font=(NoxTheme.MONO, 8)).pack()
            self._diag_raw[key] = ttk.Label(f, text='\u2014', font=(NoxTheme.MONO, 11, 'bold'))
            self._diag_raw[key].pack()

        ttk.Button(tab, text='\U0001f4ca Export Stats JSON', command=self._export_stats).grid(
            row=3, column=2, sticky='e', pady=4)

        # session history
        hist_f = ttk.LabelFrame(tab, text='Historial de sesión', padding=4)
        hist_f.grid(row=4, column=0, columnspan=3, sticky='nsew', pady=(4, 0))
        tab.rowconfigure(4, weight=1)
        hist_inner = ttk.Frame(hist_f)
        hist_inner.pack(fill=tk.BOTH, expand=True)
        self._hist_listbox = tk.Listbox(hist_inner, height=5, font=(NoxTheme.MONO, 8),
                                        bg=NoxTheme.BG2, fg=NoxTheme.FG,
                                        borderwidth=0, activestyle='none',
                                        selectbackground=NoxTheme.SEL)
        sy_hist = ttk.Scrollbar(hist_inner, command=self._hist_listbox.yview)
        self._hist_listbox.configure(yscrollcommand=sy_hist.set)
        self._hist_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sy_hist.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_config_tab(self, nb):
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text='Ajustes')

        scope = ttk.LabelFrame(tab, text='Respuestas automáticas', padding=(10, 6))
        scope.pack(fill=tk.X, pady=(0, 8))
        self._allowed_chat_count = tk.StringVar()
        ttk.Label(scope, textvariable=self._allowed_chat_count).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(scope, text='Configurar conversaciones', command=self._edit_allowed_chats).pack(side=tk.RIGHT)
        self._update_allowed_chat_count()

        bf = ttk.LabelFrame(tab, text='Comportamiento del bot', padding=10)
        bf.pack(fill=tk.X, pady=(0, 8))

        row = ttk.Frame(bf)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text='Límite de caracteres', width=22, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(row, textvariable=self._config_max_reply, width=12).pack(side=tk.LEFT)

        row = ttk.Frame(bf)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text='Pausa entre respuestas', width=22, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(row, textvariable=self._config_cooldown, width=12).pack(side=tk.LEFT)
        ttk.Label(row, text='  Milisegundos', foreground=NoxTheme.FG2).pack(side=tk.LEFT)

        row = ttk.Frame(bf)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text='Intervalo de revisión', width=22, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(row, textvariable=self._config_poll_ms, width=12).pack(side=tk.LEFT)

        mf = ttk.LabelFrame(tab, text='Selección automática y entorno', padding=10)
        mf.pack(fill=tk.X, pady=(0, 8))

        row = ttk.Frame(mf)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text='Modelo de LM Studio', width=22, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(row, textvariable=self._config_lm_model, width=40).pack(side=tk.LEFT)

        row = ttk.Frame(mf)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text='URL de LM Studio', width=22, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Entry(row, textvariable=self._config_lm_url, width=40).pack(side=tk.LEFT)

        row = ttk.Frame(mf)
        row.pack(fill=tk.X, pady=3)
        ttk.Label(row, text='Navegador', width=22, anchor=tk.E).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Radiobutton(row, text='Chrome abierto', variable=self._config_browser,
                        value='cdp').pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(row, text='Navegador de Nox', variable=self._config_browser,
                        value='bundled').pack(side=tk.LEFT, padx=2)

        ttk.Label(tab, text='Estos ajustes se aplican al reiniciar.', foreground=NoxTheme.FG2).pack()

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(pady=(6, 0))
        ttk.Button(btn_frame, text='Guardar y reiniciar',
                   command=self._apply_config, width=20).pack()

    def _update_allowed_chat_count(self):
        from assistant_ui import load_assistant_settings
        try:
            names = load_assistant_settings().get('allowedChats', [])
            count = len(names) if isinstance(names, list) else 0
        except (OSError, ValueError):
            self._allowed_chat_count.set('Revisa la configuración de IA.')
            return
        self._allowed_chat_count.set(f'Conversaciones autorizadas: {count}' if count
                                     else 'Respuestas automáticas en pausa')

    def _edit_allowed_chats(self):
        from assistant_ui import edit_assistant_settings, load_assistant_settings
        try:
            names = load_assistant_settings().get('allowedChats', [])
        except (OSError, ValueError) as error:
            messagebox.showerror('Respuestas automáticas', str(error), parent=self.root)
            return
        dialog = self._ai_dialog('Conversaciones autorizadas', '630x390')
        body = ttk.Frame(dialog, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Conversaciones autorizadas', style='Title.TLabel').pack(anchor='w')
        ttk.Label(body, text='Escribe el nombre exacto que aparece en WhatsApp, uno por línea. '
                  'Solo el contenido de esas conversaciones se usará para generar respuestas automáticas.',
                  wraplength=570).pack(anchor='w', pady=(6, 10))
        editor = tk.Text(body, height=7, wrap='none', bg=NoxTheme.BG2, fg=NoxTheme.FG,
                         insertbackground=NoxTheme.FG, padx=8, pady=6)
        editor.pack(fill='both', expand=True)
        if isinstance(names, list):
            editor.insert('1.0', '\n'.join(name for name in names if isinstance(name, str)))
        ttk.Label(body, text='La lista vacía pone en pausa las respuestas automáticas.').pack(anchor='w', pady=6)

        def save():
            lines = editor.get('1.0', 'end-1c').splitlines()
            allowed = list(dict.fromkeys(line.strip() for line in lines if line.strip()))
            if any(len(name) > 200 or '\0' in name for name in allowed):
                messagebox.showerror('Conversaciones autorizadas', 'Revisa los nombres ingresados.', parent=dialog)
                return
            try:
                with edit_assistant_settings() as settings:
                    settings['allowedChats'] = allowed
            except (OSError, ValueError) as error:
                messagebox.showerror('Conversaciones autorizadas', str(error), parent=dialog)
                return
            self._update_allowed_chat_count()
            dialog.destroy()

        actions = ttk.Frame(body)
        actions.pack(fill='x')
        ttk.Button(actions, text='Cancelar', command=dialog.destroy).pack(side='right')
        ttk.Button(actions, text='Guardar conversaciones', command=save, style='Accent.TButton').pack(
            side='right', padx=(0, 8))
        editor.focus_set()

    def _apply_config(self):
        try:
            max_reply = int(self._config_max_reply.get())
            cooldown = int(self._config_cooldown.get())
            poll_ms = int(self._config_poll_ms.get())
            lm_model = self._config_lm_model.get().strip()
            lm_url = self._config_lm_url.get().strip()
            browser = self._config_browser.get().strip().lower()
            if any(value <= 0 or value > 10_000_000 for value in (max_reply, cooldown, poll_ms)):
                raise ValueError('Los valores numéricos deben estar entre 1 y 10.000.000.')
            if not lm_model or len(lm_model) > 120 or any(char in lm_model for char in '\r\n'):
                raise ValueError('Escribe un modelo válido de LM Studio.')
            from urllib.parse import urlparse as _urlparse
            endpoint = _urlparse(lm_url)
            if (endpoint.scheme not in ('http', 'https') or endpoint.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or endpoint.username is not None or endpoint.query or endpoint.fragment):
                raise ValueError('LM Studio URL debe ser loopback 127.0.0.1/localhost')
            if browser not in ('cdp', 'bundled'):
                raise ValueError('Invalid browser')
        except ValueError as ve:
            messagebox.showerror('Config', str(ve))
            return

        try:
            if not PACKAGED:
                edit_responder_js('MAX_REPLY_LENGTH', max_reply)
                edit_responder_js('COOLDOWN_DEFAULT_MS', cooldown)
                edit_responder_js('POLL_MS', poll_ms)
        except Exception as e:
            messagebox.showerror('Config', f'Failed to write config: {e}')
            return

        # Preserve existing api_token + legacy model keys
        try:
            _existing_cfg = read_nox_config()
        except:
            _existing_cfg = {}
        if not isinstance(_existing_cfg, dict):
            _existing_cfg = {}
        write_nox_config({
            **_existing_cfg,
            'max_reply_length': max_reply,
            'cooldown': cooldown,
            'poll_ms': poll_ms,
            'model': _existing_cfg.get('model', 'qwen2.5:3b'),
            'model_fast': _existing_cfg.get('model_fast', 'qwen2.5:3b'),
            'api_token': _existing_cfg.get('api_token', _get_api_token()),
            'llm': {
                **_existing_cfg.get('llm', {}),
                'provider': 'lmstudio',
                'baseUrl': lm_url,
                'model': lm_model,
                'timeoutMs': 30000,
                'maxTokens': 220,
                'temperature': 0.5,
            },
            'browser': {'provider': browser, 'cdpEndpoint': _existing_cfg.get('browser', {}).get('cdpEndpoint', 'http://127.0.0.1:9222')},
        })
        try:
            self._update_env_kv({'LMSTUDIO_MODEL': lm_model, 'LMSTUDIO_URL': lm_url, 'BROWSER_PROVIDER': browser})
        except Exception as e:
            messagebox.showerror('Config', f'Failed to write .env: {e}')
            return

        self._add_session_event('config_change')
        notify('Nox Monitor', f'Config applied: {lm_model} [{browser}]')
        self._restart_bot()

    def _update_env_kv(self, kv):
        env_file = DIR / '.env'
        lines = []
        try:
            if env_file.exists():
                lines = env_file.read_text(encoding='utf-8').splitlines()
        except OSError:
            lines = []
        seen = set()
        out = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                out.append(line)
                continue
            key, _, _ = stripped.partition('=')
            key = key.strip()
            if key in kv:
                out.append(f'{key}={kv[key]}')
                seen.add(key)
            else:
                out.append(line)
        for key, val in kv.items():
            if key not in seen:
                out.append(f'{key}={val}')
        tmp = env_file.with_name(env_file.name + '.tmp')
        tmp.write_text('\n'.join(out) + '\n', encoding='utf-8')
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(env_file)

    # ── moderator tab ───────────────────────────────────────────────
    def _build_moderator_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text='Moderación')

        header = ttk.Label(tab, text='Respuestas por revisar', font=(NoxTheme.MONO, 10, 'bold'))
        header.pack(anchor=tk.W, pady=(0, 6))

        self._moderator_list = ttk.Treeview(tab, columns=('chat', 'reply', 'age'), show='headings', height=10)
        self._moderator_list.heading('chat', text='Chat')
        self._moderator_list.heading('reply', text='Respuesta')
        self._moderator_list.heading('age', text='Antigüedad')
        self._moderator_list.column('chat', width=120)
        self._moderator_list.column('reply', width=400)
        self._moderator_list.column('age', width=80, anchor=tk.CENTER)
        self._moderator_list.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        btn_f = ttk.Frame(tab)
        btn_f.pack(fill=tk.X)
        self._moderation_actions = (
            ttk.Button(btn_f, text='Aprobar', command=self._approve_reply, width=12),
            ttk.Button(btn_f, text='Descartar', command=self._reject_reply, width=12),
        )
        for button in self._moderation_actions:
            button.pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_f, text='Actualizar', command=self._refresh_moderator, width=12).pack(side=tk.LEFT, padx=2)

        self._moderator_status = ttk.Label(tab, text='No pending approvals')
        self._moderator_status.pack(anchor=tk.W, pady=(4, 0))

    def _refresh_moderator(self):
        """Reload pending approvals from bridge."""
        for button in self._moderation_actions:
            button.state(['!disabled' if self._private_history_visible else 'disabled'])
        if not self._private_history_visible:
            self._pending_approvals = []
            self._moderator_list.delete(*self._moderator_list.get_children())
            self._moderator_status.config(text='Muestra el historial privado para revisar respuestas.')
            return
        try:
            bridge = json.loads((DIR / 'bot-stats.json').read_text(encoding='utf-8'))
            self._pending_approvals = bridge.get('pendingApprovals', []) if isinstance(bridge.get('pendingApprovals'), list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._pending_approvals = []

        selection = self._moderator_list.selection()
        prior = self._moderator_list.item(selection[0], 'values')[:2] if selection else ()
        self._moderator_list.delete(*self._moderator_list.get_children())
        for item in self._pending_approvals:
            age = fmt_ago(item.get('timestamp', 0))
            values = (item.get('chat', '?'), item.get('reply', '')[:80], age)
            row = self._moderator_list.insert('', tk.END, values=values)
            if values[:2] == prior:
                self._moderator_list.selection_set(row)
        self._moderator_status.config(
            text=f'Por revisar: {len(self._pending_approvals)}' if self._pending_approvals else 'No hay respuestas por revisar.'
        )

    def _approve_reply(self):
        self._moderate_reply(approve=True)

    def _reject_reply(self):
        self._moderate_reply(approve=False)

    def _moderate_reply(self, approve):
        if not self._private_history_visible:
            return
        selected = self._moderator_list.selection()
        if not selected:
            return
        item = self._pending_approvals[self._moderator_list.index(selected[0])]
        path = DIR / 'bot-stats.json'
        try:
            bridge = json.loads(path.read_text(encoding='utf-8'))
            pending = bridge.get('pendingApprovals', [])
            if item not in pending:
                self._refresh_moderator()
                return
            if approve:
                if bridge.get('_approveChat') and bridge.get('_approveReply'):
                    self._moderator_status.config(text='Espera a que el bot procese la aprobación anterior.')
                    return
                bridge.update(_approveChat=item['chat'], _approveReply=item['reply'],
                              _approveAttempts=0, _approveError=None)
            pending.remove(item)
            _atomic_write_json(path, bridge)
        except (OSError, ValueError, KeyError, TypeError) as error:
            messagebox.showerror('Moderación', str(error), parent=self.root)
            return
        self._refresh_moderator()
        self._moderator_status.config(text='En cola para enviar.' if approve else 'Respuesta descartada.')

    # ── trends tab ─────────────────────────────────────────────────
    def _build_trends_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text='Tendencias')

        # Model usage stats
        mf = ttk.LabelFrame(tab, text='Uso de modelos', padding=8)
        mf.pack(fill=tk.X, pady=(0, 8))

        self._model_usage_text = tk.Text(mf, height=6, font=(NoxTheme.MONO, 10),
                                         bg=NoxTheme.BG2, fg=NoxTheme.FG,
                                         state=tk.DISABLED, borderwidth=0, padx=6, pady=4)
        self._model_usage_text.pack(fill=tk.X)
        self._model_usage_text.tag_configure('green', foreground=NoxTheme.GREEN)
        self._model_usage_text.tag_configure('yellow', foreground=NoxTheme.YELLOW)
        self._model_usage_text.tag_configure('purple', foreground=NoxTheme.PURPLE)
        self._model_usage_text.tag_configure('cyan', foreground=NoxTheme.CYAN)

        # Chat activity ranking
        af = ttk.LabelFrame(tab, text='Conversaciones más activas', padding=8)
        af.pack(fill=tk.BOTH, expand=True)

        self._trend_tree = ttk.Treeview(af, columns=('rank', 'name', 'count', 'avg_ms', 'last'), show='headings', height=12)
        self._trend_tree.heading('rank', text='#')
        self._trend_tree.heading('name', text='Chat')
        self._trend_tree.heading('count', text='Replies')
        self._trend_tree.heading('avg_ms', text='Avg ms')
        self._trend_tree.heading('last', text='Last Active')
        self._trend_tree.column('rank', width=30, anchor=tk.CENTER)
        self._trend_tree.column('name', width=120)
        self._trend_tree.column('count', width=60, anchor=tk.CENTER)
        self._trend_tree.column('avg_ms', width=70, anchor=tk.CENTER)
        self._trend_tree.column('last', width=100, anchor=tk.CENTER)
        self._trend_tree.pack(fill=tk.BOTH, expand=True)

    def _update_trends(self):
        """Update model stats and chat ranking."""
        self._model_usage_text.config(state=tk.NORMAL)
        self._model_usage_text.delete('1.0', tk.END)
        s7 = self._model_stats_7b
        s3 = self._model_stats_3b
        sd = self._model_stats_ds
        slm = self._model_stats_lm
        lm_name = self._config_lm_model.get().strip() if hasattr(self, '_config_lm_model') else 'lmstudio'
        self._model_usage_text.insert(tk.END, f'{lm_name} (primary): ')
        self._model_usage_text.insert(tk.END, f'tries={slm.get("tries", 0)}  success={slm.get("success", 0)}  avg={slm.get("avgMs", 0)}ms\n', 'green')
        if self._deepseek_enabled:
            self._model_usage_text.insert(tk.END, 'deepseek (secondary): ')
            self._model_usage_text.insert(tk.END, f'tries={sd.get("tries", 0)}  success={sd.get("success", 0)}  avg={sd.get("avgMs", 0)}ms\n', 'purple')
        self._model_usage_text.insert(tk.END, '7b (fallback):  ')
        self._model_usage_text.insert(tk.END, f'tries={s7.get("tries", 0)}  success={s7.get("success", 0)}  avg={s7.get("avgMs", 0)}ms\n', 'yellow')
        self._model_usage_text.insert(tk.END, '3b (fallback): ')
        self._model_usage_text.insert(tk.END, f'tries={s3.get("tries", 0)}  success={s3.get("success", 0)}  avg={s3.get("avgMs", 0)}ms\n', 'yellow')
        total_tries = slm.get('tries', 0) + sd.get('tries', 0) + s7.get('tries', 0) + s3.get('tries', 0)
        if total_tries > 0:
            fb_source = total_tries - slm.get('tries', 0)
            fb_rate = fb_source / total_tries * 100
            self._model_usage_text.insert(tk.END, f'Fallback rate: {fb_rate:.1f}%\n', 'cyan')
        if self._configured_model_stats:
            self._model_usage_text.insert('1.0', 'Perfiles configurados\n', 'cyan')
            for item in reversed(self._configured_model_stats):
                self._model_usage_text.insert('2.0',
                    f"{item['name']} / {item['model']} · {item['success']}/{item['tries']} · {item['avgMs']} ms\n")
            self._model_usage_text.see('1.0')
        self._model_usage_text.config(state=tk.DISABLED)

        # Chat ranking
        self._trend_tree.delete(*self._trend_tree.get_children())
        if not self._private_history_visible:
            return
        sorted_chats = sorted(self._chat_times.items(), key=lambda x: sum(x[1]), reverse=True)
        for i, (chat, times) in enumerate(sorted_chats[:20], 1):
            avg = round(sum(times) / len(times))
            last = fmt_ago(self._state.get('lastActivityByChat', {}).get(chat, 0))
            self._trend_tree.insert('', tk.END, values=(i, chat[:25], len(times), avg, last))

    # ── plugins tab ─────────────────────────────────────────────────
    def _build_plugins_tab(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text='Plugins')

        self._plugins_list = ttk.Treeview(tab, columns=('name', 'desc', 'hooks'), show='headings', height=8)
        self._plugins_list.heading('name', text='Plugin')
        self._plugins_list.heading('desc', text='Description')
        self._plugins_list.heading('hooks', text='Hooks')
        self._plugins_list.column('name', width=120)
        self._plugins_list.column('desc', width=300)
        self._plugins_list.column('hooks', width=150, anchor=tk.CENTER)
        self._plugins_list.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Scan plugins directory
        import glob as pyglob
        plugin_files = pyglob.glob(str(APP_DIR / 'plugins' / '*.js'))
        plugin_files = [f for f in plugin_files if not f.endswith('index.js') and not os.path.basename(f).startswith('_')]

        for fpath in plugin_files:
            try:
                js_text = Path(fpath).read_text()
                name_m = _re.search(r"module\.exports\s*=\s*\{\s*\n\s*name\s*:\s*['\"]([^'\"]+)['\"]", js_text)
                desc_m = _re.search(r"description\s*:\s*['\"]([^'\"]+)['\"]", js_text)
                has_on_msg = "'onMessage'" in js_text or 'onMessage:' in js_text or 'onMessage (' in js_text
                has_on_resp = "'onResponse'" in js_text or 'onResponse:' in js_text or 'onResponse (' in js_text
                has_on_int = "'onInterval'" in js_text or 'onInterval:' in js_text or 'onInterval (' in js_text
                plugin_name = name_m.group(1) if name_m else Path(fpath).stem
                plugin_desc = desc_m.group(1) if desc_m else ''
                hooks = []
                if has_on_msg: hooks.append('onMessage')
                if has_on_resp: hooks.append('onResponse')
                if has_on_int: hooks.append('onInterval')
                self._plugins_list.insert('', tk.END, values=(
                    plugin_name,
                    plugin_desc,
                    ', '.join(hooks) or 'none',
                ))
            except Exception as e:
                self._plugins_list.insert('', tk.END, values=(os.path.basename(fpath), f'Error: {e}', ''))

        if not self._plugins_list.get_children():
            self._plugins_list.insert('', tk.END, values=('(none)', 'No plugins found', ''))

        ttk.Label(tab, text='Plugins are .js files in the plugins/ directory. Restart bot to reload.',
                  foreground=NoxTheme.FG2).pack(anchor=tk.W)

    def _update_model_stats_from_bridge(self):
        """Update model stats cache from bridge file."""
        try:
            bridge = json.loads((DIR / 'bot-stats.json').read_text())
            model_data = bridge.get('model', {})
            self._configured_model_stats = model_data.get('profiles', [])
            self._model_stats_7b = model_data.get('stats', {}).get('7b', self._model_stats_7b)
            self._model_stats_3b = model_data.get('stats', {}).get('3b', self._model_stats_3b)
            self._model_stats_ds = model_data.get('stats', {}).get('deepseek', self._model_stats_ds)
            self._model_stats_lm = model_data.get('stats', {}).get('lmstudio', self._model_stats_lm)
        except:
            pass

    def _build_privacy_notice(self, parent):
        frame = ttk.Frame(parent, style='Bar.TFrame')
        self._privacy_action = tk.StringVar(value='Mostrar historial privado')
        ttk.Button(frame, textvariable=self._privacy_action, command=self._toggle_private_history).pack(side=tk.RIGHT)
        return frame

    def _toggle_private_history(self):
        self._set_private_history(not self._private_history_visible)
        self._refresh_now()

    def _set_private_history(self, visible):
        self._private_history_visible = visible
        self._privacy_action.set('Ocultar historial privado' if visible else 'Mostrar historial privado')
        if not visible:
            self._selected_chat = self._memory_chat = None
            self._bridge = {}
            self._log_search_term.set('')

    # ── refresh ────────────────────────────────────────────────────
    def _refresh(self):
        try:
            self._pid, self._started = bot_pid()
            running = self._pid is not None

            status, color = self._browser_status(running)
            self._indicator.itemconfig(self._dot, fill=color)
            if not self._bot_action_busy:
                self._status_lbl.config(text=status)
            self._pid_lbl.config(text=f'PID {self._pid}' if running else 'PID \u2014')
            self._uptime_lbl.config(text=f'Up {fmt_ago(self._started)}' if running else '')
            profile = self._update_model_summary()

            total = self._gen_success + self._gen_errors
            ia_pct = round(self._gen_success / total * 100, 1) if total else 0
            self._cards['ia_pct'].config(text=f'{ia_pct}%')
            self._cards['ia_n'].config(text=str(self._gen_success))
            self._cards['fb_n'].config(text=str(self._gen_errors))

            times = list(self._gen_times)
            if times:
                avg = round(sum(times) / len(times))
                peak = max(times)
                self._cards['avg_ms'].config(text=str(avg))
                self._cards['peak_ms'].config(text=str(peak))
                self._gen_avg_lbl.config(text=f'Avg: {avg}ms  Min: {min(times)}ms  Max: {peak}ms')
            else:
                self._cards['avg_ms'].config(text='\u2014')
                self._cards['peak_ms'].config(text='\u2014')
                self._gen_avg_lbl.config(text='')

            self._gen_chart.set_data(list(self._gen_times))

            try:
                self._state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
            except:
                self._state = {}
            sent = self._state.get('lastSentByChat', {})
            recent = self._state.get('recentRepliesByChat', {})
            skip = self._state.get('skipUntilByChat', {})
            chat_count = len(set(list(sent.keys()) + list(recent.keys())))
            self._cards['chats'].config(text=str(chat_count))

            self._update_active_chats()
            self._update_preview()
            self._update_chat_tree()
            self._render_logs()
            self._update_model_stats_from_bridge()
            self._update_diagnostics(profile)
            self._refresh_moderator()
            self._update_trends()

        finally:
            self._refresh_timer = self.root.after(REFRESH_MS, self._refresh)

    def _browser_status(self, running):
        if not running:
            return 'Bot detenido', NoxTheme.FG2
        try:
            browser = json.loads(BROWSER_STATUS_FILE.read_text(encoding='utf-8'))
            state = browser.get('state') if browser.get('pid') == self._pid else None
        except (OSError, ValueError, AttributeError):
            state = None
        return {
            'ready': ('WhatsApp conectado', NoxTheme.GREEN),
            'qr': ('Escanea el QR en el navegador', NoxTheme.YELLOW),
            'installing': ('Preparando el navegador privado…', NoxTheme.YELLOW),
            'error': ('Falló el navegador · Ver Registro', NoxTheme.RED),
        }.get(state, ('Abriendo WhatsApp…', NoxTheme.YELLOW))

    def _refresh_now(self):
        if self._refresh_timer is not None:
            self.root.after_cancel(self._refresh_timer)
        self._refresh()

    def _parse_log_metrics(self):
        """Extract gen times and stats from log lines. Called once on init."""
        for line in self._lines:
            self._record_generation(line)

    def _record_generation(self, line):
        metric = generation_metric(line)
        if metric is None:
            return False
        duration, success, chat = metric
        self._gen_times.append(duration)
        if success:
            self._gen_success += 1
        else:
            self._gen_errors += 1
        self._recent_gens.append(int(success))
        if chat:
            self._chat_times.setdefault(chat, deque(maxlen=20)).append(duration)
        return True

    def _update_active_chats(self):
        self._active_list.config(state=tk.NORMAL)
        self._active_list.delete('1.0', tk.END)
        if not self._private_history_visible:
            self._active_list.insert(tk.END, 'Historial privado oculto.\n', 'dim')
        elif not self._chat_times:
            self._active_list.insert(tk.END, 'Todavía no hay conversaciones con actividad.\n', 'dim')
        else:
            sorted_chats = sorted(self._chat_times.items(), key=lambda x: sum(x[1]), reverse=True)
            for i, (chat, times) in enumerate(sorted_chats[:8], 1):
                avg = round(sum(times) / len(times))
                last = times[-1] if times else 0
                bar = '\u2588' * min(int(sum(times) / 100), 20) or '\u258f'
                self._active_list.insert(tk.END, f'{i:2d}. {chat[:20]:20s} ')
                self._active_list.insert(tk.END, f'avg {avg:4d}ms  ', 'blue')
                self._active_list.insert(tk.END, f'last {int(last):4d}ms\n', 'green')
        self._active_list.config(state=tk.DISABLED)

    def _update_preview(self):
        self._preview_text.config(state=tk.NORMAL)
        self._preview_text.delete('1.0', tk.END)
        relevant = [summary for line in self._lines[-100:] if (summary := private_log_summary(line))]
        if not relevant:
            self._preview_text.insert(tk.END, 'Aún no hay actividad operativa para mostrar.\n', 'dim')
        for l in relevant[-15:]:
            tag = None
            if 'generada' in l: tag = 'green'
            elif 'fallida' in l or 'Error' in l: tag = 'red'
            self._preview_text.insert(tk.END, l[:120] + '\n', tag)
        self._preview_text.config(state=tk.DISABLED)

    def _update_chat_tree(self):
        if not self._private_history_visible:
            self._chat_tree.delete(*self._chat_tree.get_children())
            for button in self._chat_actions:
                button.state(['disabled'])
            self._show_chat_detail('')
            return
        selection = self._chat_tree.selection()
        sel_name = selection[0] if selection else self._selected_chat
        existing = set(self._chat_tree.get_children())
        sent = self._state.get('lastSentByChat', {})
        recent = self._state.get('recentRepliesByChat', {})
        skip = self._state.get('skipUntilByChat', {})

        # Read bridge for live language data
        try:
            bridge = json.loads((DIR / 'bot-stats.json').read_text())
            self._bridge = bridge
            bridge_chats = bridge.get('chats', {})
        except:
            bridge_chats = {}

        all_names = set(sent.keys()) | set(recent.keys())
        for removed in existing - all_names:
            self._chat_tree.delete(removed)
        for pos, name in enumerate(sorted(all_names)):
            last_sent = str(sent.get(name, ''))[:60]
            replies = len(recent.get(name, []))
            cooldown = fmt_ago(skip.get(name, 0)) if skip.get(name, 0) else '—'
            times = self._chat_times.get(name, [])
            avg = round(sum(times) / len(times)) if times else '—'
            language = bridge_chats.get(name, {}).get('language', '?').upper()
            values = (name[:28], language, '—', last_sent, replies, avg, cooldown)
            if name in existing:
                self._chat_tree.item(name, values=values)
                self._chat_tree.move(name, '', pos)
            else:
                self._chat_tree.insert('', pos, iid=name, values=values)
        if not all_names:
            self._selected_chat = self._memory_chat = None
            for button in self._chat_actions:
                button.state(['disabled'])
            self._show_chat_detail('')
            return
        selected = sel_name if sel_name in all_names else None
        if selected != self._selected_chat:
            self._memory_chat = None
        if selected and self._chat_tree.selection() != (selected,):
            self._chat_tree.selection_set(selected)
        self._selected_chat = selected
        for button in self._chat_actions:
            button.state(['!disabled' if selected else 'disabled'])
        if not selected or self._memory_chat != selected:
            self._show_chat_detail(selected or '')

    def _show_chat_detail(self, name):
        self._detail_text.config(state=tk.NORMAL)
        self._detail_text.delete('1.0', tk.END)
        if not self._private_history_visible or not name:
            text = ('Usa Mostrar historial privado para ver tus conversaciones.' if not self._private_history_visible
                    else 'Selecciona una conversación para ver su detalle.')
            self._detail_text.insert(tk.END, text)
            self._detail_text.config(state=tk.DISABLED)
            return
        recent = self._state.get('recentRepliesByChat', {}).get(name, [])
        sent = self._state.get('lastSentByChat', {}).get(name, '')
        skip = self._state.get('skipUntilByChat', {}).get(name, 0)
        times = list(self._chat_times.get(name, []))

        self._detail_text.insert(tk.END, name or 'Sin conversaciones', 'heading')
        self._detail_text.insert(tk.END, '\n\n')

        # Show language from bridge
        bridge_chat = self._bridge.get('chats', {}).get(name, {})
        chat_lang = bridge_chat.get('language', '?')
        lang_label = 'English' if chat_lang == 'en' else ('Español' if chat_lang == 'es' else '?')
        self._detail_text.insert(tk.END, f'Language: {lang_label}\n', 'cyan')
        # Show topic from bridge or state
        chat_topic = bridge_chat.get('topic', self._state.get('conversationTopics', {}).get(name, '?'))
        self._detail_text.insert(tk.END, f'Topic: {chat_topic}\n\n', 'yellow')

        if sent:
            self._detail_text.insert(tk.END, 'Last sent:\n', 'cyan')
            self._detail_text.insert(tk.END, f'  {sent}\n\n')

        if skip and isinstance(skip, (int, float)) and skip > 0:
            self._detail_text.insert(tk.END, f'Cooldown: {fmt_ago(skip)}\n', 'yellow')

        if times:
            avg = round(sum(times) / len(times))
            mx = max(times)
            mn = min(times)
            self._detail_text.insert(tk.END, f'\nGeneration times ({len(times)} responses):\n', 'cyan')
            self._detail_text.insert(tk.END, f'  Avg: {avg}ms  Min: {mn}ms  Max: {mx}ms\n')
            bar = ''
            for t in times[-20:]:
                bar += '\u2588' if t > avg else '\u2584'
            self._detail_text.insert(tk.END, f'  [{bar}]\n')

        if recent:
            self._detail_text.insert(tk.END, f'\nRecent replies:\n', 'cyan')
            for i, r in enumerate(reversed(recent), 1):
                tag = 'green' if i == 1 else None
                self._detail_text.insert(tk.END, f'  {i}. {r[:120]}', tag)
                self._detail_text.insert(tk.END, '\n')

        if not sent and not recent:
            self._detail_text.insert(tk.END, '(no data)', 'dim')
        self._detail_text.config(state=tk.DISABLED)

    def _on_chat_select(self, e):
        if not self._private_history_visible:
            return
        sel = self._chat_tree.selection()
        selected = sel[0] if sel else None
        if selected != self._selected_chat:
            self._selected_chat = selected
            self._memory_chat = None
            for button in self._chat_actions:
                button.state(['!disabled' if selected else 'disabled'])
            self._show_chat_detail(selected or '')

    # ── export / api fetch ──────────────────────────────────────────
    def _api_base(self):
        if not PACKAGED:
            return 'http://127.0.0.1:3456'
        address = json.loads((DIR / 'api-status.json').read_text(encoding='utf-8'))
        port = address.get('port')
        if address.get('pid') != self._pid or not isinstance(port, int) or not 1 <= port <= 65535:
            raise OSError('Inicia el bot antes de abrir los datos de WhatsApp.')
        return f'http://127.0.0.1:{port}'

    def _export_chat_markdown(self):
        if not self._private_history_visible:
            return
        name = self._selected_chat
        if not name:
            messagebox.showinfo('Export', 'Select a chat first')
            return
        token = _get_api_token()
        self._status_lbl.config(text='Exportando…')
        def _do_export():
            try:
                import urllib.request, urllib.parse
                url = f'{self._api_base()}/api/export?name={urllib.parse.quote(name)}'
                req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=5) as r:
                    md = r.read().decode('utf-8')
                safe = _sanitize_export_name(name)
                out_path = DIR / f'export_{safe}.md'
                _atomic_write_text(out_path, md, mode=0o600)
                self.root.after(0, lambda: messagebox.showinfo('Exportar', 'Conversación exportada.'))
            except (OSError, ValueError, json.JSONDecodeError) as e:
                self.root.after(0, lambda error=e: (messagebox.showerror('Export Error', f'API error: {error}\n\nMake sure bot is running with API enabled.'), self._status_lbl.config(text='Export failed')))
            except Exception as e:
                self.root.after(0, lambda error=e: (messagebox.showerror('Export Error', f'API error: {error}\n\nMake sure bot is running with API enabled.'), self._status_lbl.config(text='Export failed')))
        threading.Thread(target=_do_export, daemon=True).start()
        return

    def _fetch_chat_from_api(self):
        if not self._private_history_visible:
            return
        name = self._selected_chat
        if not name:
            messagebox.showinfo('Fetch', 'Select a chat first')
            return
        token = _get_api_token()
        self._status_lbl.config(text='Consultando…')
        def _do_fetch():
            try:
                import urllib.request, urllib.parse
                url = f'{self._api_base()}/api/memory?name={urllib.parse.quote(name)}'
                req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=5) as r:
                    data = json.loads(r.read())
                def _show():
                    if not self._private_history_visible or self._selected_chat != name:
                        return
                    self._memory_chat = name
                    self._detail_text.config(state=tk.NORMAL)
                    self._detail_text.delete('1.0', tk.END)
                    self._detail_text.insert(tk.END, name or 'Sin conversaciones', 'heading')
                    self._detail_text.insert(tk.END, '\n\n')
                    summary = data.get('summary', '')
                    if summary:
                        self._detail_text.insert(tk.END, f'Summary: {summary}\n\n', 'cyan')
                    memories = data.get('memories', [])
                    if memories:
                        self._detail_text.insert(tk.END, f'Memories ({len(memories)}):\n', 'yellow')
                        for m in memories:
                            self._detail_text.insert(tk.END, f'  \u2022 {m.get("fact","")} ({m.get("category","")}, {round(m.get("confidence",0)*100)}%)\n')
                        self._detail_text.insert(tk.END, '\n')
                    reminders = data.get('reminders', [])
                    if reminders:
                        self._detail_text.insert(tk.END, f'Reminders ({len(reminders)}):\n', 'cyan')
                        for rem in reminders:
                            self._detail_text.insert(tk.END, f'  \u2022 {rem.get("message","")} (due {rem.get("remind_at", "?")})\n')
                        self._detail_text.insert(tk.END, '\n')
                    corrections = data.get('corrections', [])
                    if corrections:
                        self._detail_text.insert(tk.END, f'Corrections ({len(corrections)}):\n', 'orange')
                        for c in corrections:
                            self._detail_text.insert(tk.END, f'  \u2022 {c.get("instruction", c.get("pattern", "?"))}\n')
                    if not summary and not memories and not reminders:
                        self._detail_text.insert(tk.END, '(No database data for this chat)', 'dim')
                    self._detail_text.config(state=tk.DISABLED)
                    self._status_lbl.config(text='Fetch done')
                self.root.after(0, _show)
            except Exception as e:
                self.root.after(0, lambda error=e: (messagebox.showerror('Fetch Error', f'API error: {error}\n\nMake sure bot is running with API enabled.'), self._status_lbl.config(text='Fetch failed')))
        threading.Thread(target=_do_fetch, daemon=True).start()

    # ── log renderer ──────────────────────────────────────────────
    def _render_logs(self):
        filt = self._log_filter.get()
        search = self._log_search_term.get().strip().lower()

        filtered = []
        for raw_line in reversed(self._lines):
            l = raw_line if self._private_history_visible else private_log_summary(raw_line)
            if l is None: continue
            if filt == 'ai' and not _re.search(r'\[QWEN|\[AI\]|VALIDATION|AI \(|✓ |\[TOPIC\]|\[METRIC\]', l): continue
            if filt == 'error' and not _re.search(r'✗|Error|FATAL|rechazada|↩ |FALLBACK|fallida', l): continue
            if filt == 'stats' and '[STATS]' not in l: continue
            if filt == 'skip' and not _re.search(r'↩ |○ |skip', l): continue
            if filt == 'metric' and '[METRIC]' not in l: continue
            if search and search not in l.lower(): continue
            filtered.append(l)

        self._log_count_lbl.config(text=f'{len(filtered)} lines')
        self._log_text.config(state=tk.NORMAL)
        self._log_text.delete('1.0', tk.END)
        if not filtered and not self._private_history_visible:
            self._log_text.insert(tk.END, 'El registro privado está oculto. Aquí se muestran solo eventos operativos.\n', 'dim')
        tag_map = {
            '✓ ': 'green', 'aceptada': 'green', '↩ ': 'yellow', 'skip': 'yellow',
            '○ ': 'dim',
            '✗ ': 'red', 'Error': 'red', 'FATAL': 'red', 'rechazada': 'red',
            '[QWEN': 'blue', '[VALIDATION': 'blue', '[CONTEXT': 'blue', '[PROMPT': 'blue',
            '[METRIC]': 'blue',
            '[STATS]': 'yellow',
            '[TOPIC]': 'dim',
        }
        for line in reversed(filtered[:300]):
            tag = None
            for pat, t in tag_map.items():
                if pat in line:
                    tag = t
                    break
            self._log_text.insert(tk.END, line + '\n', tag)

        if search:
            self._highlight_search(search)

        if self._autoscroll.get():
            self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)

    def _highlight_search(self, term):
        self._log_text.tag_remove('match', '1.0', tk.END)
        start = '1.0'
        while True:
            pos = self._log_text.search(term, start, stopindex=tk.END, nocase=True)
            if not pos: break
            end = f'{pos}+{len(term)}c'
            self._log_text.tag_add('match', pos, end)
            start = end

    # ── diagnostics ────────────────────────────────────────────────
    def _update_diagnostics(self, profile=None):
        self._diag_sys['pid'].config(text=str(self._pid) if self._pid else '\u2014')
        self._diag_sys['uptime'].config(text=fmt_ago(self._started) if self._pid else '\u2014')
        self._diag_sys['restarts'].config(text=str(self._restart_count))
        try:
            log_sz = os.path.getsize(LOG_FILE) / 1024
            st_sz = os.path.getsize(STATE_FILE) / 1024 if STATE_FILE.exists() else 0
            self._diag_sys['log_size'].config(text=f'{log_sz:.1f} KB')
            self._diag_sys['state_size'].config(text=f'{st_sz:.1f} KB')
        except (OSError, ValueError):
            pass
        try:
            if self._pid:
                cpu = subprocess.run(['ps', '-p', str(self._pid), '-o', '%cpu,%mem', '--no-headers'],
                                     capture_output=True, text=True, timeout=2)
                self._diag_sys['cpu_ram'].config(text=cpu.stdout.strip() or '—')
            else:
                self._diag_sys['cpu_ram'].config(text='—')
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired, ValueError):
            self._diag_sys['cpu_ram'].config(text='—')

        # Fuente única: nox-config.json + .env (LM Studio / navegador)
        try:
            cfg = read_nox_config()
            llm = cfg.get('llm', {}) if isinstance(cfg, dict) else {}
            browser_cfg = cfg.get('browser', {}) if isinstance(cfg, dict) else {}
            model_name = llm.get('model') or (self._config_lm_model.get().strip() if hasattr(self, '_config_lm_model') else 'qwen2.5-coder-1.5b-unsensored-dpo')
            browser_name = browser_cfg.get('provider') or (self._config_browser.get().strip() if hasattr(self, '_config_browser') else 'cdp')
        except:
            model_name = 'qwen2.5-coder-1.5b-unsensored-dpo'
            browser_name = 'cdp'

        model = profile['model'] if profile else model_name
        probe = self._catalog_status(profile)
        reachable = bool(probe and model in probe['models'])
        self._diag_model['reachable'].config(text='Modelo encontrado' if reachable else 'Comprobar en Modelos',
                                             foreground=NoxTheme.GREEN if reachable else NoxTheme.FG2)
        self._diag_model['ping'].config(text=f"{probe['ms']} ms" if probe else '—')
        self._diag_model['provider'].config(text=profile['kind'] if profile else 'Automático (LM Studio primero)')
        self._diag_model['model'].config(text=model)
        self._diag_model['browser'].config(text=browser_name + (' (mi Chrome)' if browser_name == 'cdp' else ''))

        total = self._gen_success + self._gen_errors
        pct = round(self._gen_success / total * 100, 1) if total else 0
        self._diag_gen['total'].config(text=str(total))
        self._diag_gen['ia'].config(text=str(self._gen_success))
        self._diag_gen['fb'].config(text=str(self._gen_errors))
        self._diag_gen['ia_pct'].config(text=f'{pct}%')
        times = list(self._gen_times)
        if times:
            self._diag_gen['avg_ms'].config(text=f'{round(sum(times)/len(times))}')
            self._diag_gen['min_ms'].config(text=f'{min(times):.0f}')
            self._diag_gen['max_ms'].config(text=f'{max(times):.0f}')
        else:
            for k in ('avg_ms', 'min_ms', 'max_ms'):
                self._diag_gen[k].config(text='\u2014')
        self._diag_gen['errors'].config(text=str(self._gen_errors))

        self._draw_distribution()

        self._diag_raw['total_lines'].config(text=str(len(self._lines)))
        try:
            # Estimate last_hour lines by reading tail 50KB instead of whole file
            tail_text = ''
            try:
                with LOG_FILE.open('r', encoding='utf-8', errors='ignore') as f:
                    f.seek(0, 2)
                    sz = f.tell()
                    f.seek(max(0, sz - 50000))
                    tail_text = f.read()
            except (OSError, FileNotFoundError):
                tail_text = ''
            lines_last = tail_text.count('\n') if tail_text else 0
            self._diag_raw['last_hour'].config(text=str(lines_last))
            metric_count = sum(1 for l in self._lines if '[METRIC]' in l)
            gen_count = sum(1 for l in self._lines if _re.search(r'AI \(\d+ms\)', l))
            self._diag_raw['metrics'].config(text=str(metric_count))
            self._diag_raw['gen_events'].config(text=str(gen_count))
        except (OSError, ValueError, _re.error):
            pass

    def _draw_distribution(self):
        self._dist_canvas.delete('all')
        w = self._dist_canvas.winfo_width() or 800
        h = self._dist_canvas.winfo_height() or 100
        times = list(self._gen_times)
        if len(times) < 3:
            self._dist_canvas.create_text(w // 2, h // 2, text='Not enough data',
                                          fill=NoxTheme.FG2, font=(NoxTheme.MONO, 9))
            return

        mx = max(times)
        mn = min(times)
        rang = mx - mn if mx > mn else 1
        buckets = 20
        bsize = rang / buckets
        counts = [0] * buckets
        for t in times:
            idx = min(int((t - mn) / bsize), buckets - 1)
            counts[idx] += 1

        max_count = max(counts) or 1
        bar_w = max((w - 40) / buckets, 4)
        for i, c in enumerate(counts):
            bar_h = (c / max_count) * (h - 30)
            x = 20 + i * bar_w
            self._dist_canvas.create_rectangle(x, h - 10 - bar_h, x + bar_w - 1,
                                               h - 10, fill=NoxTheme.GREEN,
                                               outline='', width=0)
            if c == max_count or i == 0 or i == buckets - 1:
                val = mn + i * bsize
                self._dist_canvas.create_text(x + bar_w / 2, h - 8,
                                              text=f'{val:.0f}', fill=NoxTheme.FG2,
                                              font=(NoxTheme.MONO, 7), anchor=tk.N)

        avg = sum(times) / len(times)
        avg_x = 20 + ((avg - mn) / rang) * (w - 40)
        self._dist_canvas.create_line(avg_x, 5, avg_x, h - 10,
                                      fill=NoxTheme.ORANGE, width=1, dash=(3, 3))
        self._dist_canvas.create_text(avg_x, 5, text=f'avg {avg:.0f}',
                                      fill=NoxTheme.ORANGE, font=(NoxTheme.MONO, 7), anchor=tk.S)

    def _export_stats(self):
        profile = self._update_model_summary()
        probe = self._catalog_status(profile)
        data = {
            'timestamp': datetime.now().isoformat(),
            'bot': {
                'pid': self._pid,
                'uptime': fmt_ago(self._started),
                'running': self._pid is not None,
                'restart_count': self._restart_count,
            },
            'model_connection': {
                'provider': profile['kind'] if profile else 'automatic',
                'model': profile['model'] if profile else None,
                'catalog_ping_ms': probe['ms'] if probe else None,
            },
            'generation': {
                'total': self._gen_success + self._gen_errors,
                'success': self._gen_success,
                'error': self._gen_errors,
                'ia_pct': round(self._gen_success / (self._gen_success + self._gen_errors) * 100, 1) if (self._gen_success + self._gen_errors) else 0,
                'avg_ms': round(sum(self._gen_times) / len(self._gen_times)) if self._gen_times else 0,
                'min_ms': round(min(self._gen_times)) if self._gen_times else 0,
                'max_ms': round(max(self._gen_times)) if self._gen_times else 0,
            },
            'state': {
                'chats_count': len(self._state.get('lastSentByChat', {})),
                'pending_count': len(self._state.get('pendingByChat', {})),
            }
        }
        out = DIR / 'monitor-export.json'
        _atomic_write_json(out, data)
        messagebox.showinfo('Export', f'Saved to {out}')

    # ── session history ─────────────────────────────────────────────
    def _add_session_event(self, event_type):
        self._session_history.append({
            'time': datetime.now().isoformat(),
            'event': event_type
        })
        if len(self._session_history) > 200:
            self._session_history = self._session_history[-200:]
        self._update_session_history()

    def _update_session_history(self):
        if not hasattr(self, '_hist_listbox'):
            return
        self._hist_listbox.delete(0, tk.END)
        for entry in reversed(self._session_history[-50:]):
            t = entry['time'][11:19]
            e = entry['event'].replace('_', ' ').title()
            tag = None
            if e == 'Error':
                tag = 'red'
            elif e in ('Start', 'Restart'):
                tag = 'green'
            elif e == 'Stop':
                tag = 'yellow'
            self._hist_listbox.insert(tk.END, f'{t}  {e}')
            if tag:
                try:
                    self._hist_listbox.itemconfig(tk.END, fg=getattr(NoxTheme, tag.upper()))
                except:
                    pass

    # ── log streaming ──────────────────────────────────────────────
    def _poll_log_queue(self):
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._lines.append(line)
                if self._record_generation(line) and len(self._recent_gens) >= 10:
                    recent_success = sum(self._recent_gens)
                    if recent_success < 5 and not self._error_rate_notified:
                        notify('Nox Monitor', 'Error rate > 50% in last 10 generations!')
                        self._add_session_event('error')
                        self._error_rate_notified = True
                    elif recent_success >= 5:
                        self._error_rate_notified = False
        except queue.Empty:
            pass
        if len(self._lines) > 2000:
            self._lines = self._lines[-1000:]
        self.root.after(200, self._poll_log_queue)

    # ── system tray ────────────────────────────────────────────────
    def _create_tray_image(self):
        size = 64
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=(78, 201, 176, 255))
        draw.ellipse([8, 8, 56, 56], fill=(30, 30, 30, 255))
        draw.text((32, 32), 'N', fill=(78, 201, 176, 255), font=None, anchor='mm')
        return img

    def _setup_tray(self):
        if not HAS_TRAY:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem('Show', lambda: self.root.after(0, self.root.deiconify)),
                pystray.MenuItem('Restart Bot', lambda: self.root.after(0, self._restart_bot)),
                pystray.MenuItem('Quit', lambda: self.root.after(0, self._tray_quit)),
            )
            self._tray_icon = pystray.Icon('nox-monitor', self._create_tray_image(), 'Nox Monitor', menu)
            threading.Thread(target=self._tray_icon.run, daemon=True).start()
        except Exception as e:
            print(f'[monitor] pystray init failed: {e}')
            self._tray_icon = None

    def _tray_quit(self):
        if self._tray_icon:
            self._tray_icon.stop()
            self._tray_icon = None
        self.root.destroy()

    # ── bot control ────────────────────────────────────────────────
    def _run_bot_script(self, script, action, timeout=25):
        # Synchronous helper — call from background thread only
        try:
            command = ([os.environ['NOX_PYTHON_EXECUTABLE'], str(APP_DIR / 'runtime_control.py'), script.stem]
                       if PACKAGED else ['bash', str(script)])
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=DIR,
                timeout=timeout,
                **HIDDEN_CONSOLE,
            )
            output = '\n'.join(
                part.strip() for part in (proc.stdout, proc.stderr)
                if part and part.strip()
            )
            if proc.returncode != 0:
                def _fail():
                    self._status_lbl.config(text=f'{action} failed')
                    messagebox.showerror(
                        'Bot Control',
                        f'{action} failed with exit code {proc.returncode}.\n\n{output or "No output"}'
                    )
                    self._add_session_event('error')
                self.root.after(0, _fail)
                return False
            return True
        except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as e:
            def _err(error=e):
                self._status_lbl.config(text=f'{action} failed')
                messagebox.showerror('Bot Control', f'{action} failed:\n\n{error}')
                self._add_session_event('error')
            self.root.after(0, _err)
            return False
        except Exception as e:
            def _err2(error=e):
                self._status_lbl.config(text=f'{action} failed')
                messagebox.showerror('Bot Control', f'{action} failed:\n\n{error}')
                self._add_session_event('error')
            self.root.after(0, _err2)
            return False

    def _stop_bot(self):
        self._control_bot('stop', 'Deteniendo…', (STOP_SH,))

    def _start_bot(self):
        self._control_bot('start', 'Iniciando…', (START_SH,))

    def _restart_bot(self):
        self._control_bot('restart', 'Reiniciando…', (STOP_SH, START_SH))

    def _control_bot(self, action, status, scripts):
        if self._bot_action_busy:
            return
        self._bot_action_busy = True
        self._status_lbl.configure(text=status)
        for button in self._bot_buttons:
            button.state(['disabled'])

        def finish(success):
            self._bot_action_busy = False
            for button in self._bot_buttons:
                button.state(['!disabled'])
            if success:
                if action == 'restart':
                    self._restart_count += 1
                self._add_session_event(action)
            self._refresh_now()

        def worker():
            success = all(self._run_bot_script(script, status.rstrip('…')) for script in scripts)
            self.root.after(0, finish, success)

        threading.Thread(target=worker, daemon=True).start()

    def _on_close(self):
        if HAS_TRAY and self._tray_icon:
            if self._private_history_visible:
                self._set_private_history(False)
                self._refresh_now()
            self.root.withdraw()
        else:
            if self._streamer:
                self._streamer.stop()
            self.root.destroy()


if __name__ == '__main__':
    MonitorApp()
