
"""
KiCad LLM Plugin v1.7.0
Original: jasiek/kicad-llm-plugin (MIT)
Fork:     northstarcomp/kicad-llm-plugin

RC8 bugs fixed
==============
BUG-RC8-1  Power detection: value-based check ("GND","VCC" etc.) incorrectly
           classified real components (e.g. a net-tie labelled "GND") as power
           symbols. Fixed: only lib_id prefix and #PWR reference used.
BUG-RC8-2  Double _collect_context() call: Run() collected data with
           include_datasheet_links=False, then _on_run() collected it AGAIN
           with the user's checkbox state — parsing the schematic file twice
           on every run. Fixed: Run() passes initial info to dialog; _on_run()
           re-collects only if the datasheet checkbox differs from initial state.
BUG-RC8-3  Net label extraction only handled KiCad 10 (text child node format).
           KiCad 8/9 uses an inline string atom as the second element.
           Fixed: both formats handled (matches v1.7.0 _get_label_text logic).
BUG-RC8-4  Gemini excluded from the no-API-key guard:
           "api_type not in ['openai', 'gemini']" — Gemini DOES need a key.
           Without the guard, a blank key produces an unhelpful HTTP error.
           Fixed: only 'openai' excluded (Ollama needs no key).
"""

"""
KiCad LLM Plugin v1.5.0
Complete polished version with persistent keys, nice token display, and copy buttons.
KiCad LLM Plugin — __init__.py
Original: jasiek/kicad-llm-plugin  (MIT)
Fork:     northstarcomp/kicad-llm-plugin
Version:  1.5.0

KiCad 10 fixes
==============
1. show_toolbar_button = True  set explicitly in defaults()
2. icon_file_name uses os.path.abspath(__file__) — required for KiCad 10
3. dark_icon_file_name provided for dark-theme support
4. GetFootprints() replaces removed GetModules()
5. All KiCad API returns cast to str() — wxString causes sort/compare crashes
6. int(GetSelection()) — KiCad's wx can return wxString from GetSelection()

API support
===========
- Anthropic  : /v1/messages  (system field at top level — correct format)
- xAI        : /v1/responses (Responses API — supports all Grok models)
- OpenAI     : /v1/chat/completions
- Ollama     : /v1/chat/completions  (OpenAI-compatible)

New in v1.5.0
=============
- Persistent API keys per provider (~/.kicad/kicad_llm_config.json)
- Remembers last used model across sessions
- Token usage in dedicated panel (not appended to result text)
- Copy buttons for result and token usage
- Clear Key button per provider

Security note: API keys stored in plaintext. File permissions set to 0o600.
"""


import os
import sys
import json
import traceback
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


class ConfigManager:
    def __init__(self):
        self.config_path = Path.home() / ".local" / "share" / "kicad" / "kicad_llm_config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if self.config_path.exists():
            try: return json.loads(self.config_path.read_text())
            except: pass
        return {"last_model_index": 0, "api_keys": {}}

    def save(self):
        try: self.config_path.write_text(json.dumps(self.data, indent=2))
        except: pass

    def get_api_key(self, provider): return self.data.get("api_keys", {}).get(provider, "")
    def set_api_key(self, provider, key):
        self.data.setdefault("api_keys", {})[provider] = key; self.save()
    def get_last_model_index(self): return self.data.get("last_model_index", 0)
    def set_last_model_index(self, idx): self.data["last_model_index"] = idx; self.save()


def _make_config():
    try: return ConfigManager()
    except:
        traceback.print_exc()
        class _NullConfig:
            def get_api_key(self, p): return ""
            def set_api_key(self, p, k): pass
            def get_last_model_index(self): return 0
            def set_last_model_index(self, i): pass
        return _NullConfig()


# Recursive S-Expression Parser
def parse_sexp(text):
    tokens = tokenize(text)
    results, pos = [], 0
    while pos < len(tokens):
        expr, pos = read_expr(tokens, pos)
        if expr is not None: results.append(expr)
    return results

def tokenize(text):
    tokens, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\n\r': i += 1
        elif c == ';':
            while i < n and text[i] != '\n': i += 1
        elif c in '()': tokens.append(c); i += 1
        elif c == '"':
            i += 1; s = []
            while i < n:
                if text[i] == '\\' and i+1 < n: s.append(text[i+1]); i += 2
                elif text[i] == '"': i += 1; break
                else: s.append(text[i]); i += 1
            tokens.append('"' + ''.join(s) + '"')
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r();"': j += 1
            tokens.append(text[i:j]); i = j
    return tokens

def read_expr(tokens, pos):
    if pos >= len(tokens): return None, pos
    tok = tokens[pos]
    if tok == '(': 
        pos += 1; lst = []
        while pos < len(tokens) and tokens[pos] != ')':
            expr, pos = read_expr(tokens, pos)
            if expr is not None: lst.append(expr)
        return lst, pos + 1
    elif tok.startswith('"'): return tok[1:-1], pos + 1
    else: return tok, pos + 1

def find_all(expr, tag):
    results = []
    if isinstance(expr, list):
        if expr and expr[0] == tag: results.append(expr)
        for child in expr: results.extend(find_all(child, tag))
    return results

def get_prop(node, name):
    for child in node:
        if isinstance(child, list) and len(child) >= 3 and child[0] == 'property' and child[1] == name:
            return child[2]
    return ""

def first_atom(node, tag):
    for child in node:
        if isinstance(child, list) and child and child[0] == tag:
            return child[1] if len(child) > 1 else ""
    return ""


def _collect_context(board, include_datasheet_links=False):
    info = {
        "context": "pcb",
        "title": "(untitled)",
        "footprints": [],
        "nets": [],
        "sch_file": "",
        "symbols": [],
        "pwr_symbols": [],
        "sch_nets": [],
        "no_connects": 0,
        "wire_count": 0,       # NOTE-1: always present so header never KeyErrors
        "datasheet_links": {}
    }

    title_block = board.GetTitleBlock()
    title = str(title_block.GetTitle()).strip()
    if title: info["title"] = title

    for fp in board.GetFootprints():
        info["footprints"].append({
            "ref": str(fp.GetReference()),
            "value": str(fp.GetValue()),
            "layer": str(board.GetLayerName(fp.GetLayer())),
        })
    for net_code, _ in board.GetNetInfo().NetsByName().items():
        if net_code: info["nets"].append(str(net_code))

    pcb_path = str(board.GetFileName())
    if pcb_path:
        sch_data = _find_and_parse_schematic(Path(pcb_path), include_datasheet_links)
        if sch_data:
            info.update(sch_data)
            info["context"] = "both"
    return info


def _find_and_parse_schematic(pcb_path, include_datasheet_links=False):
    candidates = list(pcb_path.parent.glob("*.kicad_sch"))
    if not candidates: return None
    preferred = [f for f in candidates if f.stem == pcb_path.stem]
    sch_file = preferred[0] if preferred else candidates[0]
    try: return _parse_kicad_sch(sch_file, include_datasheet_links)
    except: traceback.print_exc(); return None


def _parse_kicad_sch(sch_file, include_datasheet_links=False):
    text = sch_file.read_text(encoding="utf-8", errors="replace")
    parsed = parse_sexp(text)
    if not parsed: return None
    root = parsed[0]

    symbols = []
    pwr_symbols = []
    sch_nets = set()
    datasheet_links = {}

    for sym in find_all(root, 'symbol'):
        lib_id = first_atom(sym, 'lib_id')
        ref = get_prop(sym, 'Reference') or "?"
        value = get_prop(sym, 'Value') or "?"
        entry = {"lib_id": lib_id, "ref": ref, "value": value}

        if include_datasheet_links:
            ds = get_prop(sym, 'Datasheet')
            if ds and ds.startswith("http"):
                if ds not in datasheet_links:
                    datasheet_links[ds] = []
                datasheet_links[ds].append(ref)

        # BUG-RC8-1 FIX: only use lib_id prefix and #PWR reference for power detection.
        # The old value.upper() in ("VCC","GND",...) check incorrectly classified
        # any symbol whose Value happened to match those strings (e.g. a net-tie
        # resistor labelled "GND") as a power symbol.
        if (lib_id.lower().startswith("power:") or
                (ref and ref.startswith("#PWR"))):
            pwr_symbols.append(entry)
        else:
            symbols.append(entry)

    # BUG-RC8-3 FIX: support both KiCad 10 format (text child node) and
    # KiCad 8/9 format (inline string as second atom of the label node).
    for tag in ('net_label', 'global_label', 'hierarchical_label'):
        for node in find_all(root, tag):
            txt = ""
            # KiCad 10: (net_label (text "name") ...)
            for child in node:
                if isinstance(child, list) and child and child[0] == 'text':
                    if len(child) > 1:
                        txt = child[1]
                    break
            # KiCad 8/9 fallback: (net_label "name" ...)
            if not txt and len(node) > 1 and isinstance(node[1], str):
                txt = node[1]
            if txt:
                sch_nets.add(txt)

    result = {
        "sch_file":    sch_file.name,
        "symbols":     symbols,
        "pwr_symbols": pwr_symbols,
        "sch_nets":    sorted(sch_nets),
        "no_connects": len(find_all(root, 'no_connect')),
        "wire_count":  len(find_all(root, 'wire')),   # NOTE-1: restored — low count hints at incomplete schematic
    }

    if include_datasheet_links and datasheet_links:
        result["datasheet_links"] = datasheet_links

    return result


try:
    import pcbnew
    import wx
    config = _make_config()

    class LLMAnalyserPlugin(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "LLM Schematic/PCB Analyser"
            self.category = "Analyse"
            self.description = "v1.7.0 - Full Model List + Unlimited prompt data"
            self.show_toolbar_button = True
            icon = os.path.join(_HERE, "icon.png")
            icon_dark = os.path.join(_HERE, "icon_dark.png")
            self.icon_file_name = icon if os.path.isfile(icon) else ""
            self.dark_icon_file_name = icon_dark if os.path.isfile(icon_dark) else self.icon_file_name

        def Run(self):
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox(
                    "Open a PCB first.\n\n"
                    "Tip: save your schematic before running — the plugin reads "
                    "the .kicad_sch file alongside the .kicad_pcb.",
                    "LLM Analyser", wx.OK | wx.ICON_WARNING)
                return
            # BUG-RC8-2 FIX: collect once here, pass to dialog.
            # Previously Run() collected with include_datasheet_links=False,
            # then _on_run() collected AGAIN with the checkbox state — parsing
            # the schematic file twice every run. Now: one collection at open,
            # _on_run() re-collects only if the datasheet checkbox is ticked
            # (since the first pass didn't include DS links).
            info = _collect_context(board, include_datasheet_links=False)
            dlg = _LLMDialog(None, info)
            dlg.ShowModal()
            dlg.Destroy()

    LLMAnalyserPlugin().register()
except Exception:
    traceback.print_exc()


class _LLMDialog(wx.Dialog):
    _MODELS = [
        # xAI Grok (Full)
        ("Grok 4 (xAI)",                      "grok-4",                    "https://api.x.ai/v1",     "xai"),
        ("Grok 4 Fast (xAI)",                 "grok-4-fast",               "https://api.x.ai/v1",     "xai"),
        ("Grok 3 (xAI)",                      "grok-3-latest",             "https://api.x.ai/v1",     "xai"),
        ("Grok 3 Mini (xAI)",                 "grok-3-mini-latest",        "https://api.x.ai/v1",     "xai"),

        # Anthropic Claude
        ("Claude Sonnet 4 (Anthropic)",       "claude-sonnet-4-20250514",  None,                      "anthropic"),
        ("Claude Opus 4 (Anthropic)",         "claude-opus-4-20250514",    None,                      "anthropic"),

        # OpenAI
        ("GPT-4o (OpenAI)",                   "gpt-4o",                    None,                      "openai"),
        ("GPT-4o-mini (OpenAI)",              "gpt-4o-mini",               None,                      "openai"),

        # Google Gemini (Native)
        ("Gemini 2.5 Pro (Google)",           "gemini-2.5-pro",            None,                      "gemini"),
        ("Gemini 2.5 Flash (Google)",         "gemini-2.5-flash",          None,                      "gemini"),

        # Ollama (Local)
        ("Ollama llama3 (local)",             "llama3",                    "http://localhost:11434/v1", "openai"),
        ("Ollama mistral (local)",            "mistral",                   "http://localhost:11434/v1", "openai"),
        ("Ollama gemma2 (local)",             "gemma2",                    "http://localhost:11434/v1", "openai"),
        ("Ollama qwen2.5 (local)",            "qwen2.5",                   "http://localhost:11434/v1", "openai"),
    ]

    _PROVIDER_MAP = {
        "anthropic": "Anthropic",
        "openai":    "OpenAI / Ollama",
        "xai":       "xAI (Grok)",
        "gemini":    "Google Gemini",
    }

    def __init__(self, parent, info):
        ctx = info.get("context", "pcb")
        title = f"LLM Analyser — {'Schematic + PCB' if ctx == 'both' else 'PCB Only'}"
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._info = info
        self._include_datasheet = False
        self._build_ui()
        self._load_last_model_and_key()

    def _build_ui(self):
        p = self
        s = wx.BoxSizer(wx.VERTICAL)
        ctx = self._info.get("context", "pcb")

        fp_ct  = len(self._info.get("footprints", []))
        net_ct = len(self._info.get("nets", []))
        sym_ct = len(self._info.get("symbols", []))
        wires  = self._info.get("wire_count", 0)
        nc_ct  = self._info.get("no_connects", 0)
        header_text = f"Board: {self._info['title']}  |  Footprints: {fp_ct}  Nets: {net_ct}"
        if ctx == "both":
            header_text += f"  |  Symbols: {sym_ct}  Wires: {wires}  NC: {nc_ct}"
        header = wx.StaticText(p, label=header_text)
        header.SetFont(header.GetFont().Bold())
        s.Add(header, 0, wx.ALL, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(p, label="Model:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._model = wx.Choice(p, choices=[m[0] for m in self._MODELS])
        self._model.Bind(wx.EVT_CHOICE, self._on_model_changed)
        row.Add(self._model, 1, wx.RIGHT, 12)
        row.Add(wx.StaticText(p, label="API Key:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._key = wx.TextCtrl(p, style=wx.TE_PASSWORD, size=(220, -1))
        row.Add(self._key, 0)
        self._btn_clear = wx.Button(p, label="Clear Key", size=(75, 24))
        self._btn_clear.Bind(wx.EVT_BUTTON, self._on_clear_keys)
        row.Add(self._btn_clear, 0, wx.LEFT, 6)
        s.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(p, label="Base URL:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._url = wx.TextCtrl(p)
        self._url.SetHint("Leave blank for most providers")
        row3.Add(self._url, 1)
        s.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # Datasheet checkbox
        ds_row = wx.BoxSizer(wx.HORIZONTAL)
        self._ds_checkbox = wx.CheckBox(p, label="Include Datasheet Links")
        self._ds_checkbox.Bind(wx.EVT_CHECKBOX, self._on_ds_checkbox)
        ds_row.Add(self._ds_checkbox, 0, wx.ALIGN_CENTER_VERTICAL)
        s.Add(ds_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._ds_note = wx.StaticText(p, label="Unique datasheet links will be included if available in part description.")
        self._ds_note.Hide()
        s.Add(self._ds_note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._run_btn = wx.Button(p, label="▶  Run Analysis")
        self._run_btn.Bind(wx.EVT_BUTTON, self._on_run)
        s.Add(self._run_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        h1 = wx.BoxSizer(wx.HORIZONTAL)
        h1.Add(wx.StaticText(p, label="AI Response:"), 0)
        h1.AddStretchSpacer()
        self._copy_status = wx.StaticText(p, label="")
        h1.Add(self._copy_status, 0, wx.RIGHT, 8)
        self._btn_copy_result = wx.Button(p, label="Copy", size=(60, 24))
        self._btn_copy_result.Bind(wx.EVT_BUTTON, self._on_copy_result)
        h1.Add(self._btn_copy_result, 0)
        s.Add(h1, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._result = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP, size=(-1, 260))
        s.Add(self._result, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        token_box = wx.StaticBoxSizer(wx.StaticBox(p, label="Token Usage"), wx.VERTICAL)
        grid = wx.FlexGridSizer(3, 2, 4, 12)
        self._token_input = wx.StaticText(p, label="—")
        self._token_output = wx.StaticText(p, label="—")
        self._token_total = wx.StaticText(p, label="—")
        for lbl, val in [("Input:", self._token_input), ("Output:", self._token_output), ("Total:", self._token_total)]:
            grid.Add(wx.StaticText(p, label=lbl)); grid.Add(val)
        token_box.Add(grid, 0, wx.ALL, 8)
        self._btn_copy_tokens = wx.Button(p, label="Copy Token Usage", size=(140, 24))
        self._btn_copy_tokens.Bind(wx.EVT_BUTTON, self._on_copy_tokens)
        token_box.Add(self._btn_copy_tokens, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(token_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_close = wx.Button(p, wx.ID_CLOSE, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        s.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        p.SetSizerAndFit(s)
        self.SetSize((740, 680))

    def _on_ds_checkbox(self, event):
        self._include_datasheet = self._ds_checkbox.GetValue()
        if self._include_datasheet:
            self._ds_note.Show()
        else:
            self._ds_note.Hide()
        self.Layout()

    def _load_last_model_and_key(self):
        idx = config.get_last_model_index()
        if idx < len(self._MODELS):
            self._model.SetSelection(idx)
            self._on_model_changed(None)

    def _on_model_changed(self, _event):
        idx = int(self._model.GetSelection())
        _, _, default_url, api_type = self._MODELS[idx]
        self._url.SetValue(default_url or "")
        self._key.SetValue(config.get_api_key(api_type))

    def _on_clear_keys(self, _event):
        idx = int(self._model.GetSelection())
        _, _, _, api_type = self._MODELS[idx]
        if wx.MessageBox(f"Clear key for {self._PROVIDER_MAP.get(api_type, api_type)}?", "Clear Key", wx.YES_NO) == wx.YES:
            config.set_api_key(api_type, "")
            self._key.SetValue("")

    def _on_run(self, _event):
        idx = int(self._model.GetSelection())
        _, model_id, default_url, api_type = self._MODELS[idx]
        api_key = str(self._key.GetValue()).strip()
        base_url = str(self._url.GetValue()).strip() or default_url
        include_ds = self._ds_checkbox.GetValue()

        if api_key: config.set_api_key(api_type, api_key)
        config.set_last_model_index(idx)

        if not api_key and api_type not in ["openai"]:  # BUG-RC8-4 FIX: Gemini needs a key too
            wx.MessageBox("Please enter an API key.", "Error", wx.OK | wx.ICON_WARNING)
            return

        self._run_btn.Disable()
        self._copy_status.SetLabel("")
        self._result.SetValue("Running…")
        self._token_input.SetLabel("—"); self._token_output.SetLabel("—"); self._token_total.SetLabel("—")
        wx.Yield()

        # BUG-RC8-2 FIX: only re-collect if user ticked datasheet checkbox AND
        # the initial collection didn't include DS links (it never does).
        # This avoids parsing the schematic file twice on every plain run.
        if include_ds and not self._info.get("datasheet_links"):
            info = _collect_context(pcbnew.GetBoard(), include_datasheet_links=True)
        else:
            info = self._info

        try:
            text, usage = self._call_llm(model_id, api_key, base_url, api_type, info, include_ds)
        except Exception as e:
            text = f"Error: {e}"; usage = {}

        self._result.SetValue(text)

        if api_type == "anthropic":
            inp = usage.get("input_tokens", 0); out = usage.get("output_tokens", 0)
            self._token_input.SetLabel(str(inp)); self._token_output.SetLabel(str(out))
            self._token_total.SetLabel(str(inp + out))
        elif api_type == "xai":
            self._token_input.SetLabel(str(usage.get("input_tokens", 0)))
            self._token_output.SetLabel(str(usage.get("output_tokens", 0)))
            self._token_total.SetLabel(str(usage.get("total_tokens", 0)))
        elif api_type == "gemini":
            self._token_input.SetLabel(str(usage.get("promptTokenCount", 0)))
            self._token_output.SetLabel(str(usage.get("candidatesTokenCount", 0)))
            self._token_total.SetLabel(str(usage.get("totalTokenCount", 0)))
        else:
            self._token_input.SetLabel(str(usage.get("prompt_tokens", 0)))
            self._token_output.SetLabel(str(usage.get("completion_tokens", 0)))
            self._token_total.SetLabel(str(usage.get("total_tokens", 0)))

        self._run_btn.Enable()

    def _on_copy_result(self, _event):
        self._copy_to_clipboard(str(self._result.GetValue()))

    def _on_copy_tokens(self, _event):
        self._copy_to_clipboard(f"Input: {self._token_input.GetLabel()}\nOutput: {self._token_output.GetLabel()}\nTotal: {self._token_total.GetLabel()}")

    def _copy_to_clipboard(self, text):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
        self._copy_status.SetLabel("✓ Copied")
        wx.CallLater(3000, lambda: self._copy_status.SetLabel("") if self else None)

    def _prompt(self, info, include_ds):
        ctx = info.get("context", "pcb")
        return self._prompt_both(info, include_ds) if ctx == "both" else self._prompt_pcb_only(info)

    def _prompt_pcb_only(self, info):
        fps  = info["footprints"]
        nets = sorted(info["nets"])
        lines = [
            "You are an expert PCB design engineer reviewing a KiCad PCB.",
            "The schematic file was not found — PCB analysis only.",
            "",
            "Identify:",
            "1. Fatal layout flaws (missing connections, wrong layers, no GND plane)",
            "2. DRC / best-practice violations (trace width, via sizes, clearances)",
            "3. Component placement issues (bypass caps far from ICs, connector access)",
            "4. Nice-to-have improvements",
            "",
            f"Board: {info['title']}",
            f"Footprints: {len(fps)}   Nets: {len(nets)}",
            "",
            # NOTE-2: no limit — send all footprints; NOTE-3: include layer
            "Footprints (ref, value, layer):",
        ]
        for fp in fps:   # NOTE-2: was [:80] — now unlimited
            lines.append(f"  {fp['ref']}  {fp['value']}  ({fp['layer']})")
        lines += ["", "Nets:"]
        for n in nets:   # NOTE-2: was [:100] — now unlimited
            lines.append(f"  {n}")
        return "\n".join(lines)

    def _prompt_both(self, info, include_ds):
        syms  = info.get("symbols", [])
        pwr   = info.get("pwr_symbols", [])
        nets  = info.get("sch_nets", [])
        fps   = info["footprints"]
        pcbnets = sorted(info["nets"])
        nc    = info.get("no_connects", 0)
        wires = info.get("wire_count", 0)   # NOTE-1: restored

        lines = [
            "You are an expert electronics engineer reviewing a KiCad project.",
            "You have BOTH schematic and PCB data — cross-reference them.",
            "",
            "Identify:",
            "1. Schematic errors:",
            "   - Unconnected pins, missing power connections",
            "   - Missing decoupling/bypass caps on IC power pins",
            "   - Wrong passive values (pull-ups, filter caps, termination resistors)",
            "   - Power symbol issues (missing PWR_FLAG, wrong voltage labels)",
            "   - Pin assignment problems (swapped diff pairs, wrong GPIO use)",
            "2. PCB errors:",
            "   - Layout flaws, DRC violations, placement issues",
            "   - Bypass caps not adjacent to IC power pins",
            "3. Schematic ↔ PCB mismatches:",
            "   - Nets in schematic missing from PCB (and vice versa)",
            "   - Footprints on PCB with no schematic symbol",
            "   - Reference designator mismatches between schematic and PCB",
            "4. Signal integrity / EMC concerns",
            "5. Nice-to-have improvements",
            "",
            "Reference components by their actual designators (R1, U3, C4) and net names.",
            "",
            "════ SCHEMATIC ════",
            f"File: {info.get('sch_file', 'unknown')}",
            # NOTE-1: wire_count restored
            f"Symbols: {len(syms)}  Power symbols: {len(pwr)}  "
            f"No-connects: {nc}  Wires: {wires}  Net labels: {len(nets)}",
            "",
            # NOTE-3: lib_id included so LLM knows the component family
            # NOTE-2: limit raised to 300
            f"Symbols ({len(syms)})  [ref, value, library]:",
        ]
        for sym in syms:   # NOTE-2: was [:80] — now unlimited
            lines.append(f"  {sym['ref']}  {sym['value']}  [{sym['lib_id']}]")

        lines += ["", f"Power symbols ({len(pwr)}):"]
        for sym in pwr:
            lines.append(f"  {sym['ref']}  {sym['value']}  [{sym['lib_id']}]")

        lines += ["", f"Net labels / global labels ({len(nets)}):"]
        for net in nets:   # NOTE-2: was [:80] — now unlimited
            lines.append(f"  {net}")

        lines += [
            "",
            "════ PCB ════",
            f"Board: {info['title']}",
            # NOTE-3: layer included so LLM can spot wrong-layer placements
            f"",
            f"Footprints ({len(fps)})  [ref, value, layer]:",
        ]
        for fp in fps:     # NOTE-2: was [:80] — now unlimited
            lines.append(f"  {fp['ref']}  {fp['value']}  ({fp['layer']})")

        lines += ["", f"PCB nets ({len(pcbnets)}):"]
        for net in pcbnets:  # NOTE-2: was [:80] — now unlimited
            lines.append(f"  {net}")

        if include_ds and info.get("datasheet_links"):
            lines += ["", "Datasheet Links (unique, grouped by URL):"]
            for url, refs in info["datasheet_links"].items():
                lines.append(f"  {', '.join(refs[:5])}{'…' if len(refs) > 5 else ''}  → {url}")

        return "\n".join(lines)

    def _call_llm(self, model_id, api_key, base_url, api_type, info, include_ds):
        import urllib.request
        prompt = self._prompt(info, include_ds)
        system = "You are an expert electronics engineer. Be specific with designators and net names."

        if api_type == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            hdrs = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
            body = {"model": model_id, "max_tokens": 4096, "system": system, "messages": [{"role": "user", "content": prompt}]}
        elif api_type == "xai":
            url = (base_url or "https://api.x.ai/v1") + "/responses"
            hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            body = {"model": model_id, "max_output_tokens": 4096, "input": f"{system}\n\n{prompt}"}
        elif api_type == "gemini":
            model_name = model_id
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            hdrs = {"Content-Type": "application/json"}
            body = {"contents": [{"parts": [{"text": f"{system}\n\n{prompt}"}]}]}
        else:
            url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
            hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            body = {"model": model_id, "max_tokens": 4096, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}

        req = urllib.request.Request(url, json.dumps(body).encode(), hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            try:
                err_json = json.loads(err_body)
                err_msg = err_json.get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", err_body)
            except Exception:
                err_msg = err_body
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {err_msg}")

        if api_type == "anthropic":
            return data["content"][0]["text"], data.get("usage", {})
        elif api_type == "xai":
            return data["output"][0]["content"][0]["text"], data.get("usage", {})
        elif api_type == "gemini":
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata", {})
            return text, usage
        else:
            return data["choices"][0]["message"]["content"], data.get("usage", {})

