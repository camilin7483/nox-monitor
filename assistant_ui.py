"""Paneles locales de configuración y prueba para Nox Monitor."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from time import monotonic
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse
from uuid import uuid4

from nox_paths import APP_DIR, DATA_DIR, NODE, HIDDEN_CONSOLE, file_lock, save_json

SETTINGS = DATA_DIR / 'assistant-settings.json'
AI_STATUS = DATA_DIR / 'ai-status.json'
BROWSER_STATUS = DATA_DIR / 'browser-status.json'
PRESETS = {
    'Ollama': ('http://127.0.0.1:11434', False),
    'LM Studio': ('http://127.0.0.1:1234/v1', False),
    'OpenAI': ('https://api.openai.com/v1', True),
    'Anthropic': ('https://api.anthropic.com/v1', True),
    'Gemini': ('https://generativelanguage.googleapis.com/v1beta', True),
    'DeepSeek': ('https://api.deepseek.com/v1', True),
    'OpenRouter': ('https://openrouter.ai/api/v1', True),
    'Compatible con OpenAI': ('', False),
}
NEW = 'Nuevo perfil'
MAX_SOURCE = 30_000


def load_assistant_settings():
    try:
        settings = json.loads(SETTINGS.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'profiles': [], 'sources': []}
    def valid_records(key, fields):
        records = settings.get(key)
        if not isinstance(records, list) or not all(
                isinstance(record, dict) and all(isinstance(record.get(field), str) for field in fields)
                for record in records):
            return False
        ids = [record['id'] for record in records]
        return all(ids) and len(ids) == len(set(ids))

    if (not isinstance(settings, dict)
            or not valid_records('profiles', ('id', 'kind', 'name', 'url', 'model'))
            or not valid_records('sources', ('id', 'title', 'text'))
            or any(not isinstance(settings.get(key, ''), str) for key in ('systemPrompt', 'instructions'))):
        raise ValueError('El archivo de configuración de IA tiene un formato inválido.')
    return settings


@contextmanager
def edit_assistant_settings():
    # El bloqueo cubre la lectura y la escritura entre instancias del monitor.
    lock = SETTINGS.with_name('.assistant-settings-lock')
    with file_lock(lock):
        settings = load_assistant_settings()
        yield settings
        save_json(SETTINGS, settings)


def active_profile(settings):
    active = settings.get('active')
    if not active:
        return None
    for profile in settings['profiles']:
        if profile['id'] == active:
            return profile
    raise ValueError('El perfil activo ya no existe. Activa otro desde Modelos.')


def ai_text(parent, height=5):
    style = ttk.Style(parent)
    foreground = style.lookup('.', 'foreground')
    return tk.Text(parent, height=height, width=36, wrap='word', undo=True, font='TkTextFont',
                   bg=style.lookup('.', 'fieldbackground'), fg=foreground, insertbackground=foreground,
                   selectbackground=style.lookup('.', 'selectbackground'), selectforeground=foreground,
                   borderwidth=0, padx=12, pady=9, highlightthickness=1,
                   highlightbackground=style.lookup('.', 'bordercolor'),
                   highlightcolor=style.lookup('Signal.TLabel', 'foreground'))


def replace_text(widget, text):
    widget.delete('1.0', 'end')
    widget.insert('1.0', text)
    widget.edit_reset()


class AssistantPanels:
    def _build_assistant_panels(self, notebook):
        self._ai_results = queue.Queue()
        self._model_catalog = {}
        self._model_catalog_errors = set()
        self._catalog_check_after = {}
        self._local_probe_pending = set()
        self._model_tests = {}
        self._profile_id = None
        self._profile_labels = {}
        self._ai_active_label = tk.StringVar(value='Selección automática')
        self._ai_profile_check = tk.StringVar(value='Perfil sin comprobar.')
        self._ai_runtime = tk.StringVar(value='Bot detenido.')
        self._ai_status = tk.StringVar()
        self._build_model_panel(notebook)
        self._build_instructions_panel(notebook)
        try:
            settings = load_assistant_settings()
            replace_text(self._system_editor, settings.get('systemPrompt', ''))
            replace_text(self._instruction_editor, settings.get('instructions', ''))
        except (ValueError, OSError) as error:
            self._ai_status.set(str(error))
        self._sync_profiles(select_active=True)
        self._sync_sources()
        self.root.after(100, self._poll_ai_results)

    def _panel(self, notebook, tab, title, description):
        panel = ttk.Frame(notebook, padding=(24, 18, 24, 14))
        notebook.add(panel, text=tab)
        ttk.Label(panel, text=title, style='Title.TLabel').pack(anchor='w')
        ttk.Label(panel, text=description, style='Muted.TLabel', wraplength=760).pack(anchor='w', pady=(5, 15))
        return panel

    def _build_model_panel(self, notebook):
        panel = self._panel(notebook, 'Modelos', 'Modelos',
                            'Configura una conexión, prueba una respuesta y activa el perfil para Nox.')
        content = ttk.Frame(panel)
        content.pack(fill='both', expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, minsize=246)
        content.rowconfigure(0, weight=1)
        form = ttk.Frame(content)
        form.grid(row=0, column=0, sticky='nsew', padx=(0, 20))

        self._profile_choice = tk.StringVar(value=NEW)
        self._profile_name = tk.StringVar(value='Ollama')
        self._provider_kind = tk.StringVar(value='Ollama')
        self._provider_url = tk.StringVar(value=PRESETS['Ollama'][0])
        self._provider_key = tk.StringVar()
        self._provider_model = tk.StringVar()
        self._cloud_hint = tk.StringVar()

        providers = ttk.Frame(form)
        providers.pack(fill='x', pady=(0, 8))
        groups = (('Local', ('Ollama', 'LM Studio')), ('Servicios y otras APIs',
                  tuple(kind for kind in PRESETS if kind not in ('Ollama', 'LM Studio'))))
        for title, kinds in groups:
            ttk.Label(providers, text=title, style='Section.TLabel').pack(anchor='w', pady=(0, 5))
            row = ttk.Frame(providers)
            row.pack(fill='x', pady=(0, 8))
            for index, kind in enumerate(kinds):
                col = index % 3
                row.columnconfigure(col, weight=1, uniform='providers')
                provider = ttk.Radiobutton(row, text='Otra API' if kind == 'Compatible con OpenAI' else kind,
                                           value=kind, variable=self._provider_kind,
                                           style='Provider.TRadiobutton', command=self._choose_provider)
                provider.grid(row=index // 3, column=col, sticky='ew', padx=(0, 4), pady=(0, 3))
                provider.bind('<Return>', lambda _event, item=provider: item.invoke())

        fields = ttk.Frame(form)
        fields.pack(fill='x')
        for col in range(2):
            fields.columnconfigure(col, weight=1, uniform='provider-field')
        ttk.Label(fields, text='URL del servidor').grid(row=0, column=0, sticky='w', pady=(0, 4))
        ttk.Label(fields, text='Clave API').grid(row=0, column=1, sticky='w', padx=(8, 0), pady=(0, 4))
        ttk.Entry(fields, textvariable=self._provider_url, width=15).grid(row=1, column=0, sticky='ew')
        ttk.Entry(fields, textvariable=self._provider_key, show='•', width=13).grid(
            row=1, column=1, sticky='ew', padx=(8, 0))
        ttk.Label(fields, text='Nombre del perfil').grid(row=2, columnspan=2, sticky='w', pady=(10, 4))
        ttk.Entry(fields, textvariable=self._profile_name).grid(row=3, columnspan=2, sticky='ew')
        ttk.Label(fields, text='Modelo de chat').grid(row=4, columnspan=2, sticky='w', pady=(10, 4))

        model = ttk.Frame(fields)
        model.grid(row=5, columnspan=2, sticky='ew')
        self._model_selector = ttk.Combobox(model, textvariable=self._provider_model, width=14,
                                            postcommand=self._update_catalog)
        self._model_selector.pack(side='left', fill='x', expand=True)
        self._discover_button = ttk.Button(model, text='Buscar modelos', command=self._discover_models)
        self._discover_button.pack(side='right', padx=(8, 0))
        ttk.Label(form, textvariable=self._cloud_hint, style='Muted.TLabel', wraplength=430).pack(
            anchor='w', pady=(6, 0))
        self._update_cloud_hint()
        actions = ttk.Frame(form)
        actions.pack(fill='x', pady=(12, 3))
        ttk.Button(actions, text='Guardar y activar', style='Accent.TButton',
                   command=lambda: self._save_profile(True)).pack(side='left')
        ttk.Button(actions, text='Probar modelo', command=lambda: self._open_lab(self._profile_draft())).pack(
            side='left', padx=6)
        ttk.Button(actions, text='Guardar perfil', command=lambda: self._save_profile(False)).pack(side='left')
        ttk.Label(form, textvariable=self._ai_status, style='Signal.TLabel', wraplength=445).pack(
            anchor='w', pady=(5, 0))

        side = ttk.Frame(content, width=246)
        side.grid(row=0, column=1, sticky='nsew')
        status = ttk.Frame(side, style='Surface.TFrame', padding=(12, 12))
        status.pack(fill='x')
        ttk.Label(status, text='Perfil en uso', style='SurfaceTitle.TLabel').pack(anchor='w')
        ttk.Label(status, textvariable=self._ai_active_label, style='Surface.TLabel',
                  justify='left', wraplength=222).pack(anchor='w', pady=(7, 3))
        ttk.Label(status, textvariable=self._ai_profile_check, style='SurfaceMuted.TLabel',
                  wraplength=222).pack(anchor='w')
        bar = ttk.Frame(side, style='Surface.TFrame', padding=(12, 9))
        bar.pack(fill='x', pady=(3, 0))
        ttk.Label(bar, textvariable=self._ai_runtime, style='SurfaceMuted.TLabel',
                  wraplength=222).pack(anchor='w')
        ttk.Label(side, text='Perfiles guardados', style='Section.TLabel').pack(anchor='w', pady=(16, 7))
        self._profile_selector = ttk.Treeview(side, show='tree', height=4, selectmode='browse')
        self._profile_selector.column('#0', width=200, minwidth=100)
        self._profile_selector.pack(fill='x')
        self._profile_selector.bind('<<TreeviewSelect>>', self._select_saved_profile)
        self._profile_empty = ttk.Label(side, text='Aún no tienes perfiles.', style='Muted.TLabel')
        self._profile_empty.place(in_=self._profile_selector, x=9, y=8)
        ttk.Button(side, text='+ Nuevo perfil', command=self._new_profile).pack(fill='x', pady=(7, 4))
        self._delete_profile_button = ttk.Button(side, text='Eliminar perfil', command=self._delete_profile)
        self._delete_profile_button.pack(fill='x', pady=(0, 4))
        ttk.Button(side, text='Usar selección automática', command=self._use_automatic).pack(fill='x')

    def _new_profile(self):
        self._profile_choice.set(NEW)
        self._profile_selector.selection_remove(*self._profile_selector.selection())
        self._load_profile_choice()
        self._ai_status.set('')

    def _choose_provider(self):
        self._profile_choice.set(NEW)
        self._profile_id = None
        self._profile_selector.selection_remove(*self._profile_selector.selection())
        self._delete_profile_button.state(['disabled'])
        self._select_preset()
        self._ai_status.set('')

    def _select_saved_profile(self, _event=None):
        selected = self._profile_selector.selection()
        if not selected:
            return
        label = next((name for name, p in self._profile_labels.items() if p['id'] == selected[0]), NEW)
        if label != self._profile_choice.get():
            self._profile_choice.set(label)
            self._load_profile_choice()
            self._ai_status.set('')

    def _profile_draft(self):
        return dict(id=self._profile_id or '', kind=self._provider_kind.get(), name=self._profile_name.get().strip(),
                    url=self._provider_url.get().strip(), key=self._provider_key.get().strip(),
                    model=self._provider_model.get().strip())

    def _update_cloud_hint(self):
        self._cloud_hint.set('Si activas esta API, recibirá el texto del chat y el contexto que Nox use.'
                             if PRESETS[self._provider_kind.get()][1] or self._provider_kind.get() == 'Compatible con OpenAI'
                             else ('Usa la URL base que expone /models.' if self._provider_kind.get() != 'Ollama' else 'La clave es opcional en un servidor Ollama local.'))

    def _select_preset(self, _event=None):
        name = self._provider_kind.get()
        self._profile_name.set(name)
        self._provider_url.set(PRESETS[name][0])
        self._provider_key.set('')
        self._provider_model.set('')
        self._update_cloud_hint()
        self._update_catalog()

    def _sync_profiles(self, select_active=False):
        try:
            settings = load_assistant_settings()
        except (OSError, ValueError):
            return
        try:
            current = active_profile(settings)
            profile_error = ''
        except ValueError as error:
            current, profile_error = None, str(error)
        self._ai_active_label.set(f"{current['kind']}\n{current['model']}" if current else
                                  'Perfil activo no encontrado' if profile_error else 'Selección automática')
        self._profile_labels = {f"{p['name']} ({p['kind']})": p for p in settings['profiles']}
        self._profile_selector.delete(*self._profile_selector.get_children())
        for name, profile in self._profile_labels.items():
            self._profile_selector.insert('', 'end', iid=profile['id'],
                                          text=name + ('  •' if current and profile['id'] == current['id'] else ''))
        if self._profile_labels:
            self._profile_empty.place_forget()
        else:
            self._profile_empty.place(in_=self._profile_selector, x=9, y=8)
        wanted = current['id'] if select_active and current else self._profile_id
        choice = next((label for label, p in self._profile_labels.items() if p['id'] == wanted), NEW)
        self._show_profile_check(current)
        if profile_error:
            self._ai_profile_check.set(profile_error)
        self._profile_choice.set(choice)
        if wanted and choice != NEW:
            self._profile_selector.selection_set(wanted)
            self._profile_selector.see(wanted)
        self._delete_profile_button.state(['!disabled'] if choice != NEW else ['disabled'])
        if select_active:
            self._load_profile_choice()

    def _load_profile_choice(self, _event=None):
        profile = self._profile_labels.get(self._profile_choice.get())
        self._delete_profile_button.state(['!disabled'] if profile else ['disabled'])
        if profile is None:
            self._profile_id = None
            self._provider_kind.set('Ollama')
            self._select_preset()
            return
        self._profile_id = profile['id']
        for field, variable in (('kind', self._provider_kind), ('name', self._profile_name),
                                ('url', self._provider_url), ('key', self._provider_key), ('model', self._provider_model)):
            variable.set(profile.get(field, ''))
        self._update_cloud_hint()
        self._update_catalog()

    def _edit_ai(self, update, status):
        try:
            with edit_assistant_settings() as settings:
                update(settings)
        except (ValueError, OSError) as error:
            messagebox.showerror('Configuración de IA', str(error), parent=self.root)
            return False
        status()
        return True

    def _save_profile(self, activate):
        profile = self._profile_draft()
        try:
            parsed = urlparse(profile['url'])
            if not profile['name'] or len(profile['name']) > 60 or not profile['model'] or len(profile['model']) > 200:
                raise ValueError('Escribe un nombre de perfil y un ID de modelo válidos.')
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or
                    parsed.query or parsed.fragment):
                raise ValueError('Usa una URL HTTP o HTTPS sin credenciales ni parámetros.')
            if parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1') and profile['key']:
                raise ValueError('Usa HTTPS para enviar una clave fuera de este equipo.')
            if PRESETS[profile['kind']][1] and not profile['key']:
                raise ValueError('Escribe la clave API de este proveedor.')
        except ValueError as error:
            self._ai_status.set(str(error))
            return
        profile['id'] = profile['id'] or uuid4().hex

        original = self._profile_labels.get(self._profile_choice.get())

        def update(settings):
            previous = next((p for p in settings['profiles'] if p['id'] == profile['id']), None)
            if original != previous:
                raise ValueError('El perfil cambió en otra ventana. Vuelve a abrir Nox Monitor.')
            others = [p for p in settings['profiles'] if p['id'] != profile['id']]
            if any(p['name'] == profile['name'] and p['kind'] == profile['kind'] for p in others):
                raise ValueError('Ya existe un perfil con ese nombre para este proveedor.')
            settings['profiles'] = sorted([*others, profile], key=lambda p: p['name'].casefold())
            if activate:
                settings['active'] = profile['id']

        if self._edit_ai(update, lambda: self._ai_status.set(
                'Modelo guardado y activo. Se usará en la próxima respuesta.' if activate else 'Perfil guardado.')):
            self._profile_id = profile['id']
            self._sync_profiles()
            self._update_model_summary()

    def _use_automatic(self):
        if self._edit_ai(lambda settings: settings.pop('active', None),
                         lambda: self._ai_status.set('Selección automática activada.')):
            self._sync_profiles()
            self._update_model_summary()

    def _delete_profile(self):
        if self._profile_id is None:
            self._ai_status.set('Selecciona un perfil guardado para eliminarlo.')
            return
        profile_id = self._profile_id
        if not messagebox.askyesno('Eliminar perfil', '¿Eliminar este perfil? Si está activo, Nox volverá a la selección automática.',
                                  parent=self.root):
            return

        def update(settings):
            settings['profiles'] = [p for p in settings['profiles'] if p['id'] != profile_id]
            if settings.get('active') == profile_id:
                settings.pop('active')

        if self._edit_ai(update, lambda: self._ai_status.set('Perfil eliminado.')):
            self._profile_id = None
            self._sync_profiles(select_active=True)
            self._update_model_summary()

    @staticmethod
    def _profile_connection(profile):
        return profile['kind'], profile['url'], profile.get('key', '')

    def _connection_id(self):
        return self._profile_connection(self._profile_draft())

    def _update_catalog(self):
        result = self._model_catalog.get(self._connection_id(), {})
        self._model_selector.configure(values=result.get('models', ()))

    def _catalog_status(self, profile):
        if profile is None:
            return None
        return self._model_catalog.get(self._profile_connection(profile))

    def _show_profile_check(self, profile):
        if profile is None:
            self._ai_profile_check.set('Activa un perfil para elegir el modelo del bot.')
            return
        identifier = (*self._profile_connection(profile), profile['model'])
        attempt = self._model_tests.get(identifier)
        catalog = self._catalog_status(profile)
        if attempt and 'error' in attempt:
            text = attempt['error']
        elif self._profile_connection(profile) in self._model_catalog_errors:
            text = 'Catálogo sin conexión.'
        elif attempt is not None:
            time = f" · {attempt['ms']} ms" if isinstance(attempt.get('ms'), (int, float)) else ''
            text = 'Respuesta verificada' + time
        elif catalog and profile['model'] in catalog.get('models', ()):
            text = 'Modelo en el catálogo · falta probar respuesta'
        elif catalog:
            text = 'Modelo fuera del catálogo · prueba el ID manual'
        else:
            text = 'Pendiente de probar una respuesta'
        self._ai_profile_check.set(text)

    def _discover_models(self):
        profile = self._profile_draft()
        identifier = self._connection_id()
        if not profile['url']:
            self._ai_status.set('Escribe la URL del servidor.')
            return
        self._discover_button.state(['disabled'])
        self._catalog_check_after[identifier] = monotonic() + 30
        self._local_probe_pending.discard(identifier)
        self._ai_status.set('Consultando los modelos del proveedor…')

        def done(result):
            self._discover_button.state(['!disabled'])
            if self._connection_id() != identifier:
                self._ai_status.set('La conexión cambió. Busca los modelos de la URL actual.')
                return
            if 'error' in result:
                self._model_catalog.pop(identifier, None)
                self._model_catalog_errors.add(identifier)
                self._update_catalog()
                self._ai_status.set(result['error'])
                self._update_model_summary()
                return
            models = result['models']
            self._model_catalog_errors.discard(identifier)
            self._model_catalog[identifier] = result
            self._update_catalog()
            if not self._provider_model.get() and len(models) == 1:
                self._provider_model.set(models[0])
            self._ai_status.set(f'{len(models)} modelos encontrados. Abre la lista para elegir uno.' if models
                                else 'Conexión disponible. El catálogo no devolvió modelos.')
            self._update_model_summary()

        self._run_ai({'action': 'models', 'profile': profile}, done, 30)

    def _build_instructions_panel(self, notebook):
        panel = self._panel(notebook, 'Instrucciones', 'Instrucciones del bot',
                            'Dale a Nox reglas de respuesta y fuentes que pueda consultar en cada conversación.')
        body = ttk.Frame(panel)
        body.pack(fill='both', expand=True)
        body.columnconfigure(0, weight=3, uniform='instructions')
        body.columnconfigure(1, weight=2, uniform='instructions')
        body.rowconfigure(0, weight=1)

        editors = ttk.Frame(body)
        editors.grid(row=0, column=0, sticky='nsew', padx=(0, 14))
        ttk.Label(editors, text='Prompt del sistema', style='Section.TLabel').pack(anchor='w')
        ttk.Label(editors, text='Déjalo vacío para usar las instrucciones predeterminadas de Nox.', style='Muted.TLabel').pack(anchor='w', pady=(3, 6))
        self._system_editor = ai_text(editors, 4)
        self._system_editor.pack(fill='both', expand=True, pady=(0, 10))
        ttk.Label(editors, text='Instrucciones permanentes', style='Section.TLabel').pack(anchor='w')
        ttk.Label(editors, text='Reglas que Nox recibe al preparar cada respuesta.', style='Muted.TLabel').pack(anchor='w', pady=(3, 6))
        self._instruction_editor = ai_text(editors, 4)
        self._instruction_editor.pack(fill='both', expand=True)

        sources = ttk.LabelFrame(body, text='Base de conocimiento', padding=12)
        sources.grid(row=0, column=1, sticky='nsew')
        ttk.Label(sources, text='Nox recupera hasta tres fragmentos relevantes de tus fuentes según las palabras de la consulta.',
                  style='Muted.TLabel', wraplength=255).pack(anchor='w', pady=(0, 10))
        catalog = ttk.Frame(sources)
        catalog.pack(fill='both', expand=True)
        self._source_list = ttk.Treeview(catalog, show='tree', height=4, selectmode='browse')
        self._source_list.column('#0', minwidth=130, width=170)
        self._source_list.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(catalog, command=self._source_list.yview)
        scroll.pack(side='right', fill='y')
        self._source_list.configure(yscrollcommand=scroll.set)
        self._source_list.bind('<Return>', lambda _event: self._edit_source(True))
        self._source_list.bind('<Double-1>', lambda _event: self._edit_source(True))
        controls = ttk.Frame(sources)
        controls.pack(fill='x', pady=(8, 0))
        ttk.Button(controls, text='Añadir', command=self._edit_source).pack(side='left')
        ttk.Button(controls, text='Editar', command=lambda: self._edit_source(True)).pack(side='left', padx=4)
        ttk.Button(controls, text='Quitar', command=self._remove_source).pack(side='left')
        self._source_status = tk.StringVar()
        ttk.Label(sources, textvariable=self._source_status, style='Muted.TLabel', wraplength=255).pack(anchor='w', pady=(8, 0))

        actions = ttk.Frame(panel)
        actions.pack(fill='x', pady=(12, 2))
        ttk.Button(actions, text='Guardar instrucciones', style='Accent.TButton',
                   command=self._save_instructions).pack(side='left')
        ttk.Button(actions, text='Probar con un mensaje', command=self._open_lab).pack(side='left', padx=8)
        self._instructions_status = tk.StringVar()
        ttk.Label(panel, textvariable=self._instructions_status, wraplength=740).pack(anchor='w')

    def _instruction_draft(self):
        data = {'systemPrompt': self._system_editor.get('1.0', 'end-1c').strip(),
                'instructions': self._instruction_editor.get('1.0', 'end-1c').strip()}
        if any(len(value) > 10_000 for value in data.values()):
            raise ValueError('Cada campo admite hasta 10.000 caracteres.')
        return data

    def _save_instructions(self):
        try:
            draft = self._instruction_draft()
        except ValueError as error:
            self._instructions_status.set(str(error))
            return
        self._edit_ai(lambda settings: settings.update(draft),
                      lambda: self._instructions_status.set('Guardadas. Se aplican desde la próxima respuesta.'))

    def _sync_sources(self, select=None):
        try:
            sources = load_assistant_settings()['sources']
        except (ValueError, OSError) as error:
            self._source_status.set(str(error))
            return
        self._source_list.delete(*self._source_list.get_children())
        for source in sources:
            self._source_list.insert('', 'end', iid=source['id'], text=source['title'])
        if select:
            self._source_list.selection_set(select)
            self._source_list.see(select)
        self._source_status.set(f'Fuentes guardadas: {len(sources)}' if sources else 'Añade texto o importa un archivo .txt o .md.')

    def _edit_source(self, existing=False):
        selection = self._source_list.selection()
        if existing and not selection:
            self._source_status.set('Selecciona una fuente para editarla.')
            return
        try:
            sources = load_assistant_settings()['sources']
        except (OSError, ValueError) as error:
            self._source_status.set(str(error))
            return
        source = next((s for s in sources if existing and s['id'] == selection[0]), {})
        if existing and not source:
            self._sync_sources()
            return
        popup = self._ai_dialog('Editar fuente' if source else 'Añadir fuente', '620x480')
        body = ttk.Frame(popup, padding=18)
        body.pack(fill='both', expand=True)
        title = tk.StringVar(value=source.get('title', ''))
        ttk.Label(body, text='Nombre de la fuente').pack(anchor='w')
        entry = ttk.Entry(body, textvariable=title)
        entry.pack(fill='x', pady=(4, 10))
        ttk.Label(body, text='Contenido').pack(anchor='w')
        editor = ai_text(body, 9)
        editor.pack(fill='both', expand=True, pady=(4, 8))
        replace_text(editor, source.get('text', ''))
        status = tk.StringVar()
        ttk.Label(body, textvariable=status, wraplength=560).pack(anchor='w')
        actions = ttk.Frame(body)
        actions.pack(fill='x', pady=(6, 0))
        ttk.Button(actions, text='Cancelar', command=popup.destroy).pack(side='right')

        def import_source():
            selected = filedialog.askopenfilename(parent=popup, filetypes=[('Texto y Markdown', ('*.txt', '*.md'))])
            if not selected:
                return
            try:
                with Path(selected).open(encoding='utf-8-sig') as file:
                    text = file.read(MAX_SOURCE + 1)
                if len(text) > MAX_SOURCE:
                    raise ValueError('El archivo supera los 30.000 caracteres.')
                title.set(Path(selected).stem[:100])
                replace_text(editor, text)
                status.set('')
            except (OSError, ValueError) as error:
                status.set(f'Usa un archivo de texto en UTF-8. {error}')

        def save():
            name = title.get().strip()
            text = editor.get('1.0', 'end-1c').strip()
            if not name or len(name) > 100 or not text or len(text) > MAX_SOURCE:
                status.set('Escribe un título de hasta 100 caracteres y un contenido de hasta 30.000.')
                return
            record = dict(id=source.get('id') or uuid4().hex, title=name, text=text)
            try:
                with edit_assistant_settings() as settings:
                    prior = next((i for i, item in enumerate(settings['sources']) if item['id'] == record['id']), None)
                    if prior is not None:
                        if settings['sources'][prior] != source:
                            raise ValueError('Esta fuente cambió en otra ventana. Vuelve a abrirla.')
                        settings['sources'][prior] = record
                    elif source:
                        raise ValueError('Esta fuente se eliminó en otra ventana.')
                    else:
                        if len(settings['sources']) >= 40:
                            raise ValueError('La biblioteca admite hasta 40 fuentes.')
                        settings['sources'].append(record)
            except (OSError, ValueError) as error:
                status.set(str(error))
                return
            popup.destroy()
            self._sync_sources(select=record['id'])

        ttk.Button(actions, text='Guardar fuente', style='Accent.TButton', command=save).pack(side='right', padx=6)
        ttk.Button(actions, text='Importar archivo…', command=import_source).pack(side='left')
        entry.focus_set()

    def _remove_source(self):
        chosen = self._source_list.selection()
        if not chosen:
            self._source_status.set('Selecciona una fuente para quitarla.')
            return
        if not messagebox.askyesno('Quitar fuente', '¿Quitar la fuente seleccionada?', parent=self.root):
            return
        if self._edit_ai(lambda settings: settings.update(
                sources=[s for s in settings['sources'] if s['id'] != chosen[0]]), lambda: None):
            self._sync_sources()

    def _ai_dialog(self, title, geometry):
        popup = tk.Toplevel(self.root, bg=self.root.cget('bg'))
        popup.title(title)
        popup.geometry(geometry)
        popup.transient(self.root)
        popup.bind('<Escape>', lambda _event: popup.destroy())
        popup.grab_set()
        return popup

    def _open_lab(self, profile=None):
        try:
            settings = load_assistant_settings()
            settings.update(self._instruction_draft())
            profile = profile if profile is not None else active_profile(settings)
        except (ValueError, OSError) as error:
            messagebox.showerror('Prueba del bot', str(error), parent=self.root)
            return
        popup = self._ai_dialog('Prueba del bot', '690x575')
        frame = ttk.Frame(popup, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Prueba del bot', style='Title.TLabel').pack(anchor='w')
        model = profile and profile.get('model')
        ttk.Label(frame, text=f"Generar envía la prueba a {profile['kind']} ({model})." if model else
                  'Activa un modelo en Modelos para generar una respuesta. Puedes previsualizar las fuentes ahora.',
                  style='Muted.TLabel', wraplength=620).pack(anchor='w', pady=(5, 12))
        ttk.Label(frame, text='Mensaje de prueba').pack(anchor='w')
        question = ai_text(frame, 3)
        question.pack(fill='x', pady=(4, 10))
        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        status = tk.StringVar(value='La prueba utiliza las instrucciones que tienes escritas y las fuentes guardadas.')
        ttk.Label(frame, textvariable=status, wraplength=620).pack(anchor='w', pady=(11, 6))
        result = ai_text(frame, 8)
        result.configure(state='disabled')
        result.pack(fill='both', expand=True)
        question.focus_set()

        def run(action):
            query = question.get('1.0', 'end-1c').strip()
            if not query or len(query) > 4000:
                status.set('Escribe un mensaje de hasta 4.000 caracteres.')
                return
            preview.state(['disabled'])
            test.state(['disabled'])
            status.set('Preparando la prueba…')

            def done(data):
                if action == 'test':
                    identifier = (*self._profile_connection(profile), model)
                    self._model_tests[identifier] = {
                        key: data[key] for key in ('error', 'ms') if key in data
                    }
                    self._update_model_summary()
                if not popup.winfo_exists():
                    return
                preview.state(['!disabled'])
                if model:
                    test.state(['!disabled'])
                if 'error' in data:
                    status.set(data['error'])
                    output = ''
                else:
                    refs = data['references']
                    status.set(f'Fragmentos recuperados: {len(refs)}')
                    parts = []
                    if data.get('reply'):
                        parts.append('Respuesta de Nox\n' + data['reply'])
                    if refs:
                        excerpts = '\n\n'.join(f"{r['title']}\n{r['excerpt']}" for r in refs)
                        parts.append('Fuentes recuperadas\n\n' + excerpts)
                    output = '\n\n'.join(parts)
                    if not output:
                        output = 'No se encontraron referencias relacionadas con el mensaje.'
                result.configure(state='normal')
                replace_text(result, output)
                result.configure(state='disabled')

            self._run_ai({'action': action, 'query': query, 'settings': {
                key: settings[key] for key in ('systemPrompt', 'instructions', 'sources')
            }, 'profile': profile if action == 'test' else None,
                'maxLength': self._config_max_reply.get()}, done, 95)

        preview = ttk.Button(actions, text='Ver fuentes recuperadas', command=lambda: run('preview'))
        preview.pack(side='left')
        test = ttk.Button(actions, text='Generar respuesta', style='Accent.TButton', command=lambda: run('test'))
        test.pack(side='left', padx=8)
        if model:
            ttk.Label(actions, text='Ctrl+Intro', style='Muted.TLabel').pack(side='left')

        def generate_from_editor(_event):
            if model and not test.instate(['disabled']):
                run('test')
            return 'break'

        question.bind('<Control-Return>', generate_from_editor)
        if not model:
            test.state(['disabled'])

    def _run_ai(self, payload, completed, timeout):
        def worker():
            try:
                response = subprocess.run([NODE, str(APP_DIR / 'assistant.js')],
                                          input=json.dumps(payload), capture_output=True,
                                          encoding='utf-8', timeout=timeout, **HIDDEN_CONSOLE)
                result = json.loads(response.stdout)
                expected = 'models' if payload['action'] == 'models' else 'references'
                if not isinstance(result, dict) or (
                        not isinstance(result.get('error'), str) and not isinstance(result.get(expected), list)):
                    raise ValueError('Respuesta inválida del proceso de IA')
            except subprocess.TimeoutExpired:
                result = {'error': 'El proveedor tardó demasiado en responder. Puedes reintentar la prueba.'}
            except OSError:
                result = {'error': 'No se pudo iniciar el proceso de IA. Verifica que Node.js esté instalado.'}
            except ValueError:
                result = {'error': 'El proceso de IA terminó sin devolver un resultado válido.'}
            self._ai_results.put((completed, result))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_ai_results(self):
        try:
            while True:
                callback, result = self._ai_results.get_nowait()
                callback(result)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._poll_ai_results)

    def _update_model_summary(self):
        try:
            settings = load_assistant_settings()
            profile = active_profile(settings)
        except (OSError, ValueError) as error:
            self._ollama_lbl.configure(text='IA · Revisar configuración')
            self._ai_active_label.set('Revisar configuración')
            self._ai_profile_check.set(str(error))
            self._ai_runtime.set('Revisa el archivo de configuración de IA.')
            return None
        label = f"{profile['kind']} / {profile['model']}" if profile else 'Selección automática'
        self._ollama_lbl.configure(text='IA · ' + label)
        self._ai_active_label.set(f"{profile['kind']}\n{profile['model']}" if profile else label)
        self._show_profile_check(profile)
        self._check_active_local_catalog(profile)
        self._refresh_runtime_status(settings)
        return profile

    def _check_active_local_catalog(self, profile):
        if not profile or profile['kind'] not in ('Ollama', 'LM Studio'):
            return
        identifier = self._profile_connection(profile)
        if identifier in self._local_probe_pending or monotonic() < self._catalog_check_after.get(identifier, 0):
            return
        try:
            host = urlparse(profile['url']).hostname
        except ValueError:
            return
        if host not in ('127.0.0.1', 'localhost', '::1'):
            return
        self._local_probe_pending.add(identifier)

        def checked(result):
            if identifier not in self._local_probe_pending:
                return
            self._local_probe_pending.discard(identifier)
            failed = 'error' in result
            self._catalog_check_after[identifier] = monotonic() + (15 if failed else 60)
            if failed:
                self._model_catalog_errors.add(identifier)
                self._model_catalog.pop(identifier, None)
            else:
                self._model_catalog_errors.discard(identifier)
                self._model_catalog[identifier] = result
            if self._connection_id() == identifier:
                self._update_catalog()
            self._update_model_summary()

        self._run_ai({'action': 'models', 'profile': profile}, checked, 30)

    def _refresh_runtime_status(self, settings):
        pid = getattr(self, '_pid', None)
        names = settings.get('allowedChats', [])
        allowed = isinstance(names, list) and any(isinstance(name, str) and name.strip() for name in names)
        paused = 'IA automática en pausa · autoriza conversaciones en Ajustes.'
        if not pid:
            self._ai_runtime.set('Bot detenido.' if allowed else paused)
            return

        def process_status(path):
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                return record if record.get('pid') == pid else {}
            except (OSError, ValueError, AttributeError):
                return {}

        browser = process_status(BROWSER_STATUS)
        browser_state = browser.get('state')
        if browser_state == 'qr':
            self._ai_runtime.set('WhatsApp requiere vinculación por QR.')
            return
        if browser_state in ('opening', 'loading', 'installing', 'error'):
            browser_labels = {
                'opening': 'Abriendo WhatsApp…',
                'loading': 'Abriendo WhatsApp…',
                'installing': 'Preparando el navegador privado…',
                'error': browser.get('error') or 'WhatsApp desconectado · reconectando.',
            }
            self._ai_runtime.set(browser_labels[browser_state])
            return
        if not allowed:
            self._ai_runtime.set(paused)
            return
        runtime = process_status(AI_STATUS)
        model = runtime.get('model') or runtime.get('provider') or 'Proveedor'
        labels = {
            'testing': f'Bot · consultando {model}',
            'ready': f'Bot · última generación de {model}',
            'error': runtime.get('error') or f'Bot · error de IA en {model}',
        }
        self._ai_runtime.set(labels.get(runtime.get('state'), 'IA lista para probar con una conversación autorizada.'))
