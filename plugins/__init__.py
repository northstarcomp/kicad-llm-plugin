"""
KiCad LLM Plugin v2.0.0
Original: jasiek/kicad-llm-plugin (MIT)
Fork:     northstarcomp/kicad-llm-plugin

v2.0.0 Major Release
- Hierarchical schematic support (root + direct child sheets)
- Footprint → Pin → Net mapping
- Board layer stack + DRC integration
- True Streaming (xAI, OpenAI, Anthropic, Gemini)
- Focus Selector + Custom Instructions
- Component Filter (UI + Logic)
- Re-run with Intelligent Comparison View
- Save Report
"""

import os
import sys
import json
import traceback
from pathlib import Path
from datetime import datetime

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
            try:
                return json.loads(self.config_path.read_text())
            except:
                pass
        return {
            "last_model_index": 0,
            "api_keys": {},
            "last_focus": "Full Review",
            "custom_prompt": "",
            "component_filter": ""
        }

    def save(self):
        try:
            self.config_path.write_text(json.dumps(self.data, indent=2))
        except:
            pass

    def get_api_key(self, provider):
        return self.data.get("api_keys", {}).get(provider, "")

    def set_api_key(self, provider, key):
        self.data.setdefault("api_keys", {})[provider] = key
        self.save()

    def get_last_model_index(self):
        return self.data.get("last_model_index", 0)

    def set_last_model_index(self, idx):
        self.data["last_model_index"] = idx
        self.save()

    def get_last_focus(self):
        return self.data.get("last_focus", "Full Review")

    def set_last_focus(self, focus):
        self.data["last_focus"] = focus
        self.save()

    def get_custom_prompt(self):
        return self.data.get("custom_prompt", "")

    def set_custom_prompt(self, prompt):
        self.data["custom_prompt"] = prompt
        self.save()

    def get_component_filter(self):
        return self.data.get("component_filter", "")

    def set_component_filter(self, value):
        self.data["component_filter"] = value
        self.save()


config = ConfigManager()


# ─────────────────────────────────────────────────────────────
# S-Expression Parser + Hierarchical Support
# ─────────────────────────────────────────────────────────────

def parse_sexp(text):
    tokens = tokenize(text)
    results, pos = [], 0
    while pos < len(tokens):
        expr, pos = read_expr(tokens, pos)
        if expr is not None:
            results.append(expr)
    return results

def tokenize(text):
    tokens, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\n\r':
            i += 1
        elif c == ';':
            while i < n and text[i] != '\n':
                i += 1
        elif c in '()':
            tokens.append(c)
            i += 1
        elif c == '"':
            i += 1
            s = []
            while i < n:
                if text[i] == '\\' and i + 1 < n:
                    s.append(text[i + 1])
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    s.append(text[i])
                    i += 1
            tokens.append('"' + ''.join(s) + '"')
        else:
            j = i
            while j < n and text[j] not in ' \t\n\r();"':
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens

def read_expr(tokens, pos):
    if pos >= len(tokens):
        return None, pos
    tok = tokens[pos]
    if tok == '(':
        pos += 1
        lst = []
        while pos < len(tokens) and tokens[pos] != ')':
            expr, pos = read_expr(tokens, pos)
            if expr is not None:
                lst.append(expr)
        return lst, pos + 1
    elif tok.startswith('"'):
        return tok[1:-1], pos + 1
    else:
        return tok, pos + 1

def find_all(expr, tag):
    results = []
    if isinstance(expr, list):
        if expr and expr[0] == tag:
            results.append(expr)
        for child in expr:
            results.extend(find_all(child, tag))
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


def _parse_kicad_sch(sch_file, include_datasheet_links=False):
    try:
        text = sch_file.read_text(encoding="utf-8", errors="replace")
        parsed = parse_sexp(text)
        if not parsed:
            return None
        root = parsed[0]

        symbols, pwr_symbols, sch_nets = [], [], set()
        datasheet_links = {}
        wire_count = len(find_all(root, 'wire'))

        for sym in find_all(root, 'symbol'):
            lib_id = first_atom(sym, 'lib_id')
            ref = get_prop(sym, 'Reference') or "?"
            value = get_prop(sym, 'Value') or "?"
            entry = {"lib_id": lib_id, "ref": ref, "value": value}

            if include_datasheet_links:
                ds = get_prop(sym, 'Datasheet')
                if ds and ds.startswith("http"):
                    datasheet_links.setdefault(ds, []).append(ref)

            if lib_id.lower().startswith("power:") or (ref and ref.startswith("#PWR")):
                pwr_symbols.append(entry)
            else:
                symbols.append(entry)

        for tag in ('net_label', 'global_label', 'hierarchical_label'):
            for node in find_all(root, tag):
                txt = ""
                for child in node:
                    if isinstance(child, list) and child and child[0] == 'text':
                        txt = child[1] if len(child) > 1 else ""
                        break
                if not txt and len(node) > 1 and isinstance(node[1], str):
                    txt = node[1]
                if txt:
                    sch_nets.add(txt)

        result = {
            "symbols": symbols,
            "pwr_symbols": pwr_symbols,
            "sch_nets": sorted(sch_nets),
            "no_connects": len(find_all(root, 'no_connect')),
            "wire_count": wire_count,
        }
        if include_datasheet_links and datasheet_links:
            result["datasheet_links"] = datasheet_links
        return result
    except:
        return None


def _find_and_parse_schematic(pcb_path, include_datasheet_links=False):
    candidates = list(pcb_path.parent.glob("*.kicad_sch"))
    if not candidates:
        return None
    preferred = [f for f in candidates if f.stem == pcb_path.stem]
    root_sch = preferred[0] if preferred else candidates[0]

    root_data = _parse_kicad_sch(root_sch, include_datasheet_links)
    if not root_data:
        return None

    all_symbols = root_data.get("symbols", [])
    all_pwr = root_data.get("pwr_symbols", [])
    all_nets = set(root_data.get("sch_nets", []))
    total_wires = root_data.get("wire_count", 0)
    total_nc = root_data.get("no_connects", 0)
    datasheet_links = root_data.get("datasheet_links", {})

    try:
        text = root_sch.read_text(encoding="utf-8", errors="replace")
        parsed = parse_sexp(text)
        if parsed:
            for sheet in find_all(parsed[0], 'sheet'):
                for child in sheet:
                    if isinstance(child, list) and child and child[0] == 'file' and len(child) > 1:
                        child_path = root_sch.parent / child[1]
                        if child_path.exists():
                            child_data = _parse_kicad_sch(child_path, include_datasheet_links)
                            if child_data:
                                all_symbols.extend(child_data.get("symbols", []))
                                all_pwr.extend(child_data.get("pwr_symbols", []))
                                all_nets.update(child_data.get("sch_nets", []))
                                total_wires += child_data.get("wire_count", 0)
                                total_nc += child_data.get("no_connects", 0)
                                if include_datasheet_links and "datasheet_links" in child_data:
                                    for url, refs in child_data["datasheet_links"].items():
                                        datasheet_links.setdefault(url, []).extend(refs)
    except:
        pass

    return {
        "sch_file": root_sch.name,
        "symbols": all_symbols,
        "pwr_symbols": all_pwr,
        "sch_nets": sorted(all_nets),
        "no_connects": total_nc,
        "wire_count": total_wires,
        "datasheet_links": datasheet_links if include_datasheet_links else {},
    }


def get_drc_violations(board):
    violations = []
    try:
        if hasattr(board, "GetDRC"):
            drc = board.GetDRC()
            if drc and hasattr(drc, "GetViolations"):
                for v in drc.GetViolations():
                    violations.append(str(v))
    except:
        pass
    return violations


def apply_component_filter(info: dict, filter_text: str) -> dict:
    if not filter_text or not filter_text.strip():
        return info
    f = filter_text.lower().strip()
    filtered = info.copy()

    if "symbols" in info:
        filtered["symbols"] = [s for s in info["symbols"] if f in s.get("ref", "").lower() or f in s.get("value", "").lower()]
    if "footprints" in info:
        filtered["footprints"] = [fp for fp in info["footprints"] if f in fp.get("ref", "").lower() or f in fp.get("value", "").lower()]
    if "footprint_nets" in info:
        filtered["footprint_nets"] = {k: v for k, v in info["footprint_nets"].items() if f in k.lower() or f in v.lower()}
    if "sch_nets" in info:
        filtered["sch_nets"] = [n for n in info["sch_nets"] if f in n.lower()]
    if "nets" in info:
        filtered["nets"] = [n for n in info["nets"] if f in n.lower()]
    return filtered


def _collect_context(board, include_datasheet_links=False):
    info = {
        "context": "pcb",
        "title": "(untitled)",
        "footprints": [],
        "footprint_nets": {},
        "nets": [],
        "sch_file": "",
        "symbols": [],
        "pwr_symbols": [],
        "sch_nets": [],
        "no_connects": 0,
        "wire_count": 0,
        "datasheet_links": {},
        "layer_stack": {},
        "drc_violations": [],
    }

    title_block = board.GetTitleBlock()
    title = str(title_block.GetTitle()).strip()
    if title:
        info["title"] = title

    info["layer_stack"] = {
        "copper_layer_count": board.GetCopperLayerCount(),
        "layers": [str(board.GetLayerName(i)) for i in range(board.GetLayerCount()) if board.IsLayerEnabled(i)]
    }

    for fp in board.GetFootprints():
        ref = str(fp.GetReference())
        value = str(fp.GetValue())
        layer = str(board.GetLayerName(fp.GetLayer()))
        info["footprints"].append({"ref": ref, "value": value, "layer": layer})
        for pad in fp.Pads():
            pin_num = str(pad.GetNumber())
            net_name = str(pad.GetNetname())
            if net_name:
                info["footprint_nets"][f"{ref}.{pin_num}"] = net_name

    for net_code, _ in board.GetNetInfo().NetsByName().items():
        if net_code:
            info["nets"].append(str(net_code))

    info["drc_violations"] = get_drc_violations(board)

    pcb_path = str(board.GetFileName())
    if pcb_path:
        sch_data = _find_and_parse_schematic(Path(pcb_path), include_datasheet_links)
        if sch_data:
            info.update(sch_data)
            info["context"] = "both"
    return info


# ─────────────────────────────────────────────────────────────
# Plugin Registration
# ─────────────────────────────────────────────────────────────

try:
    import pcbnew
    import wx

    class LLMAnalyserPlugin(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "LLM Schematic/PCB Analyser v2.0"
            self.category = "Analyse"
            self.description = "v2.0 - Hierarchical + Pin-to-Net + Streaming + DRC"
            self.show_toolbar_button = True
            icon = os.path.join(_HERE, "icon.png")
            icon_dark = os.path.join(_HERE, "icon_dark.png")
            self.icon_file_name = icon if os.path.isfile(icon) else ""
            self.dark_icon_file_name = icon_dark if os.path.isfile(icon_dark) else self.icon_file_name

        def Run(self):
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("Open a PCB first.", "LLM Analyser", wx.OK | wx.ICON_WARNING)
                return
            info = _collect_context(board, include_datasheet_links=False)
            dlg = _LLMDialog(None, info)
            dlg.ShowModal()
            dlg.Destroy()

    LLMAnalyserPlugin().register()

except Exception:
    traceback.print_exc()


# ─────────────────────────────────────────────────────────────
# Main Dialog Class
# ─────────────────────────────────────────────────────────────

class _LLMDialog(wx.Dialog):
    _MODELS = [
        ("Grok 4.3 (xAI)", "grok-4.3", "https://api.x.ai/v1", "xai"),
        ("Grok 4.20 Reasoning (xAI)", "grok-4.20-0309-reasoning", "https://api.x.ai/v1", "xai"),
        ("Grok 4 (xAI)", "grok-4", "https://api.x.ai/v1", "xai"),
        ("Grok 3 (xAI)", "grok-3-latest", "https://api.x.ai/v1", "xai"),
        ("Grok 3 Mini (xAI)", "grok-3-mini-latest", "https://api.x.ai/v1", "xai"),
        ("Claude Opus 4.7 (Anthropic)", "claude-opus-4-7", None, "anthropic"),
        ("Claude Sonnet 4 (Anthropic)", "claude-sonnet-4-20250514", None, "anthropic"),
        ("GPT-5.5 Pro (OpenAI)", "gpt-5.5-pro", None, "openai"),
        ("GPT-5.5 (OpenAI)", "gpt-5.5", None, "openai"),
        ("GPT-5 (OpenAI)", "gpt-5", None, "openai"),
        ("GPT-4o (OpenAI)", "gpt-4o", None, "openai"),
        ("GPT-4 Turbo (OpenAI)", "gpt-4-turbo", None, "openai"),
        ("GPT-4 (OpenAI)", "gpt-4", None, "openai"),
        ("Gemini 3.5 Flash (Google)", "gemini-3.5-flash", None, "gemini"),
        ("Gemini 3.1 Pro (Google)", "gemini-3.1-pro", None, "gemini"),
        ("Gemini 2.5 Pro (Google)", "gemini-2.5-pro", None, "gemini"),
        ("Gemini 2.5 Flash (Google)", "gemini-2.5-flash", None, "gemini"),
        ("Ollama llama3 (local)", "llama3", "http://localhost:11434/v1", "openai"),
        ("Ollama mistral (local)", "mistral", "http://localhost:11434/v1", "openai"),
        ("Ollama gemma2 (local)", "gemma2", "http://localhost:11434/v1", "openai"),
        ("Ollama qwen2.5 (local)", "qwen2.5", "http://localhost:11434/v1", "openai"),
    ]

    def __init__(self, parent, info):
        super().__init__(parent, title="LLM Analyser v2.0", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._info = info
        self._last_response = ""
        self._previous_response = ""
        self._last_model_name = ""
        self._token_usage = {}

        self._build_ui()
        self._load_last_model_and_key()

    def _build_ui(self):
        p = self
        s = wx.BoxSizer(wx.VERTICAL)

        self._header = wx.StaticText(p, label="Loading project data...")
        self._header.SetFont(self._header.GetFont().Bold())
        s.Add(self._header, 0, wx.ALL | wx.EXPAND, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(p, label="Model:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._model = wx.Choice(p, choices=[m[0] for m in self._MODELS])
        self._model.Bind(wx.EVT_CHOICE, self._on_model_changed)
        row.Add(self._model, 1, wx.RIGHT, 12)
        row.Add(wx.StaticText(p, label="API Key:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._key = wx.TextCtrl(p, style=wx.TE_PASSWORD, size=(220, -1))
        row.Add(self._key, 0)
        self._btn_clear = wx.Button(p, label="Clear", size=(65, 26))
        self._btn_clear.Bind(wx.EVT_BUTTON, self._on_clear_keys)
        row.Add(self._btn_clear, 0, wx.LEFT, 6)
        s.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        row_url = wx.BoxSizer(wx.HORIZONTAL)
        row_url.Add(wx.StaticText(p, label="Base URL:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._url = wx.TextCtrl(p)
        self._url.SetHint("Leave blank for default")
        row_url.Add(self._url, 1)
        s.Add(row_url, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        focus_row = wx.BoxSizer(wx.HORIZONTAL)
        focus_row.Add(wx.StaticText(p, label="Focus:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._focus = wx.Choice(p, choices=["Full Review", "Power & Decoupling", "Signal Integrity", "BOM Check", "Custom"])
        self._focus.SetStringSelection(config.get_last_focus())
        focus_row.Add(self._focus, 0, wx.RIGHT, 12)
        focus_row.Add(wx.StaticText(p, label="Custom Instructions:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._custom = wx.TextCtrl(p, size=(320, -1))
        self._custom.SetValue(config.get_custom_prompt())
        focus_row.Add(self._custom, 1)
        s.Add(focus_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        filter_row.Add(wx.StaticText(p, label="Component Filter:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._component_filter = wx.TextCtrl(p, size=(320, -1))
        self._component_filter.SetHint("e.g. U1, power, C1-C10")
        self._component_filter.SetValue(config.get_component_filter())
        filter_row.Add(self._component_filter, 1)
        s.Add(filter_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        ds_row = wx.BoxSizer(wx.HORIZONTAL)
        self._ds_checkbox = wx.CheckBox(p, label="Include Datasheet Links (deduplicated)")
        self._ds_checkbox.Bind(wx.EVT_CHECKBOX, self._on_ds_checkbox)
        ds_row.Add(self._ds_checkbox, 0)
        s.Add(ds_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

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
        self._btn_copy = wx.Button(p, label="Copy", size=(60, 24))
        self._btn_copy.Bind(wx.EVT_BUTTON, self._on_copy_result)
        h1.Add(self._btn_copy, 0)
        s.Add(h1, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._result = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP, size=(-1, 320))
        s.Add(self._result, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        token_box = wx.StaticBoxSizer(wx.StaticBox(p, label="Token Usage"), wx.VERTICAL)
        grid = wx.FlexGridSizer(3, 2, 4, 12)
        self._token_input = wx.StaticText(p, label="—")
        self._token_output = wx.StaticText(p, label="—")
        self._token_total = wx.StaticText(p, label="—")
        for lbl, val in [("Input:", self._token_input), ("Output:", self._token_output), ("Total:", self._token_total)]:
            grid.Add(wx.StaticText(p, label=lbl))
            grid.Add(val)
        token_box.Add(grid, 0, wx.ALL, 8)
        self._btn_copy_tokens = wx.Button(p, label="Copy Token Usage", size=(140, 24))
        self._btn_copy_tokens.Bind(wx.EVT_BUTTON, self._on_copy_tokens)
        token_box.Add(self._btn_copy_tokens, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(token_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        self._btn_save = wx.Button(p, label="Save Report")
        self._btn_save.Bind(wx.EVT_BUTTON, self._on_save_report)
        btn_row.Add(self._btn_save, 0, wx.RIGHT, 8)
        self._btn_rerun = wx.Button(p, label="Run Again (Compare)")
        self._btn_rerun.Bind(wx.EVT_BUTTON, self._on_rerun)
        btn_row.Add(self._btn_rerun, 0, wx.RIGHT, 8)
        btn_close = wx.Button(p, wx.ID_CLOSE, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_row.AddStretchSpacer()
        btn_row.Add(btn_close, 0)
        s.Add(btn_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        p.SetSizerAndFit(s)
        self.SetSize((860, 780))

    def _on_model_changed(self, event):
        idx = int(self._model.GetSelection())
        _, _, default_url, api_type = self._MODELS[idx]
        self._url.SetValue(default_url or "")
        self._key.SetValue(config.get_api_key(api_type))

    def _on_ds_checkbox(self, event):
        self._include_datasheet = self._ds_checkbox.GetValue()
        self._ds_note.Show() if self._include_datasheet else self._ds_note.Hide()
        self.Layout()

    def _on_clear_keys(self, event):
        idx = int(self._model.GetSelection())
        _, _, _, api_type = self._MODELS[idx]
        if wx.MessageBox("Clear key for this provider?", "Clear Key", wx.YES_NO) == wx.YES:
            config.set_api_key(api_type, "")
            self._key.SetValue("")

    def _append_to_result(self, text):
        current = self._result.GetValue()
        self._result.SetValue(current + text)
        self._result.SetInsertionPointEnd()

    def _update_header(self, active_filter=""):
        fp_ct = len(self._info.get("footprints", []))
        net_ct = len(self._info.get("nets", []))
        layers = self._info.get("layer_stack", {}).get("copper_layer_count", 0)
        drc_ct = len(self._info.get("drc_violations", []))
        base = f"Board: {self._info.get('title', 'Untitled')} | Footprints: {fp_ct} | Nets: {net_ct} | Layers: {layers}"
        if drc_ct > 0:
            base += f" | ⚠ DRC: {drc_ct}"
        if active_filter:
            base += f"   [Filtered: {active_filter}]"
            self._header.SetForegroundColour(wx.Colour(180, 0, 0))
        else:
            self._header.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        self._header.SetLabel(base)

    def _update_token_display(self):
        if not self._token_usage:
            self._token_input.SetLabel("—")
            self._token_output.SetLabel("—")
            self._token_total.SetLabel("—")
            return
        if "prompt_tokens" in self._token_usage:
            self._token_input.SetLabel(str(self._token_usage.get("prompt_tokens", 0)))
            self._token_output.SetLabel(str(self._token_usage.get("completion_tokens", 0)))
            self._token_total.SetLabel(str(self._token_usage.get("total_tokens", 0)))
        elif "input_tokens" in self._token_usage:
            inp = self._token_usage.get("input_tokens", 0)
            out = self._token_usage.get("output_tokens", 0)
            self._token_input.SetLabel(str(inp))
            self._token_output.SetLabel(str(out))
            self._token_total.SetLabel(str(inp + out))
        elif "promptTokenCount" in self._token_usage:
            self._token_input.SetLabel(str(self._token_usage.get("promptTokenCount", 0)))
            self._token_output.SetLabel(str(self._token_usage.get("candidatesTokenCount", 0)))
            self._token_total.SetLabel(str(self._token_usage.get("totalTokenCount", 0)))

    def _load_last_model_and_key(self):
        idx = config.get_last_model_index()
        if idx < len(self._MODELS):
            self._model.SetSelection(idx)
            self._on_model_changed(None)
        saved_filter = config.get_component_filter()
        if saved_filter:
            self._update_header(saved_filter)
        else:
            self._update_header()

    def _get_focus_system_prompt(self, focus_mode, custom_instructions=""):
        base = "You are an expert electronics engineer reviewing a KiCad project with both schematic and PCB data."
        focus_map = {
            "Full Review": "Perform a comprehensive cross-reference review covering schematic vs PCB mismatches, power integrity, signal integrity, placement, and best practices.",
            "Power & Decoupling": "Focus heavily on power delivery, decoupling capacitors, power symbol correctness, missing decoupling on IC power pins, and power plane integrity.",
            "Signal Integrity": "Focus on signal integrity: high-speed signals, differential pairs, impedance, termination, crosstalk, and return paths.",
            "BOM Check": "Focus on BOM accuracy, missing/duplicated designators, wrong part values, and footprint vs symbol mismatches.",
            "Custom": "Follow the user's specific instructions below."
        }
        instructions = focus_map.get(focus_mode, focus_map["Full Review"])
        if custom_instructions.strip():
            instructions += f"\n\nAdditional user instructions:\n{custom_instructions.strip()}"
        return f"{base}\n{instructions}"

    def _prompt_both(self, info, include_ds, focus_mode="Full Review", custom_instructions=""):
        syms = info.get("symbols", [])
        pwr = info.get("pwr_symbols", [])
        sch_nets = info.get("sch_nets", [])
        fps = info.get("footprints", [])
        fp_nets = info.get("footprint_nets", {})
        pcb_nets = sorted(info.get("nets", []))
        drc = info.get("drc_violations", [])
        layers = info.get("layer_stack", {})

        lines = [
            self._get_focus_system_prompt(focus_mode, custom_instructions),
            "",
            f"Board: {info.get('title', 'Untitled')} | Layers: {layers.get('copper_layer_count', 0)}",
            f"Symbols: {len(syms)} | Power Symbols: {len(pwr)} | DRC Violations: {len(drc)}",
            "",
        ]

        if drc:
            lines.append("════ DRC VIOLATIONS ════")
            for v in drc[:12]:
                lines.append(f"  - {v}")
            lines.append("")

        lines.append("════ SCHEMATIC (Hierarchical) ════")
        for s in syms[:120]:
            lines.append(f"  {s['ref']}  {s['value']}  [{s['lib_id']}]")

        if pwr:
            lines.append("\nPower Symbols:")
            for p in pwr[:20]:
                lines.append(f"  {p['ref']}  {p['value']}")

        if sch_nets:
            lines.append(f"\nNet Labels ({len(sch_nets)}):")
            for n in sch_nets[:60]:
                lines.append(f"  {n}")

        lines.append("\n════ PCB LAYOUT ════")
        for fp in fps[:120]:
            lines.append(f"  {fp['ref']}  {fp['value']}  ({fp['layer']})")

        if fp_nets:
            lines.append("\nPin-to-Net Mapping (sample):")
            for k, v in list(fp_nets.items())[:50]:
                lines.append(f"  {k} → {v}")

        lines.append(f"\nPCB Nets ({len(pcb_nets)}):")
        for n in pcb_nets[:80]:
            lines.append(f"  {n}")

        if include_ds and info.get("datasheet_links"):
            lines.append("\n════ DATASHEET LINKS ════")
            for url, refs in list(info["datasheet_links"].items())[:8]:
                lines.append(f"  {', '.join(refs[:4])} → {url}")

        lines.append("\nProvide a detailed, structured analysis with specific designators.")
        return "\n".join(lines)

    def _prompt_pcb_only(self, info):
        fps = info.get("footprints", [])
        fp_nets = info.get("footprint_nets", {})
        nets = sorted(info.get("nets", []))
        layers = info.get("layer_stack", {})

        lines = [
            "You are an expert PCB design engineer. Schematic file was not found.",
            f"Board: {info.get('title', 'Untitled')} | Layers: {layers.get('copper_layer_count', 0)}",
            "",
            "Footprints:",
        ]
        for fp in fps[:180]:
            lines.append(f"  {fp['ref']}  {fp['value']}  ({fp['layer']})")

        if fp_nets:
            lines.append("\nPin-to-Net Mapping:")
            for k, v in list(fp_nets.items())[:70]:
                lines.append(f"  {k} → {v}")

        lines.append("\nNets:")
        for n in nets[:120]:
            lines.append(f"  {n}")

        lines.append("\nAnalyze for layout issues, DRC problems, and best practices.")
        return "\n".join(lines)

    def _call_llm_streaming(self, model_id, api_key, base_url, api_type, prompt, system_prompt=""):
        import urllib.request
        import json as json_module
        import urllib.error

        if api_type == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
            body = {"model": model_id, "max_tokens": 4096, "system": system_prompt, "messages": [{"role": "user", "content": prompt}], "stream": True}
        elif api_type == "xai":
            url = (base_url or "https://api.x.ai/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            body = {"model": model_id, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}], "stream": True}
        elif api_type == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:streamGenerateContent?key={api_key}&alt=sse"
            headers = {"Content-Type": "application/json"}
            body = {"contents": [{"parts": [{"text": f"{system_prompt}\n\n{prompt}"}]}], "generationConfig": {"maxOutputTokens": 4096}}
        else:
            url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            body = {"model": model_id, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}], "stream": True}

        req = urllib.request.Request(url, json_module.dumps(body).encode("utf-8"), headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                for line in response:
                    line = line.decode("utf-8").strip()
                    if not line or line == "data: [DONE]":
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        try:
                            data = json_module.loads(data_str)
                            if api_type == "anthropic":
                                if "delta" in data and "text" in data.get("delta", {}):
                                    yield data["delta"]["text"]
                            elif api_type == "gemini":
                                if "candidates" in data and data["candidates"]:
                                    for part in data["candidates"][0].get("content", {}).get("parts", []):
                                        if "text" in part:
                                            yield part["text"]
                            else:
                                if "choices" in data and data["choices"]:
                                    delta = data["choices"][0].get("delta", {})
                                    if "content" in delta and delta["content"]:
                                        yield delta["content"]
                        except:
                            continue
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err}")
        except Exception as e:
            raise RuntimeError(f"Streaming error: {str(e)}")

    def _call_llm_non_streaming(self, model_id, api_key, base_url, api_type, prompt, system_prompt=""):
        import urllib.request
        import json as json_module

        if api_type == "gemini":
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={api_key}"
            headers = {"Content-Type": "application/json"}
            body = {"contents": [{"parts": [{"text": f"{system_prompt}\n\n{prompt}"}]}], "generationConfig": {"maxOutputTokens": 4096}}
        elif api_type == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
            body = {"model": model_id, "max_tokens": 4096, "system": system_prompt, "messages": [{"role": "user", "content": prompt}]}
        else:
            url = (base_url or ("https://api.openai.com/v1" if api_type == "openai" else "https://api.x.ai/v1")) + "/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            body = {"model": model_id, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}]}

        req = urllib.request.Request(url, json_module.dumps(body).encode("utf-8"), headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json_module.loads(resp.read())

        if api_type == "anthropic":
            return data["content"][0]["text"], data.get("usage", {})
        elif api_type == "gemini":
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            return text, data.get("usageMetadata", {})
        else:
            return data["choices"][0]["message"]["content"], data.get("usage", {})

    def _on_run(self, event, is_rerun=False, previous_context=""):
        idx = int(self._model.GetSelection())
        model_name, model_id, default_url, api_type = self._MODELS[idx]
        api_key = str(self._key.GetValue()).strip()
        base_url = str(self._url.GetValue()).strip() or default_url
        focus = self._focus.GetStringSelection()
        custom = self._custom.GetValue().strip()
        include_ds = self._ds_checkbox.GetValue()
        filter_text = self._component_filter.GetValue().strip()

        if api_key:
            config.set_api_key(api_type, api_key)
        config.set_last_model_index(idx)
        config.set_last_focus(focus)
        config.set_custom_prompt(custom)
        config.set_component_filter(filter_text)

        if not api_key and api_type not in ["openai"]:
            wx.MessageBox("Please enter an API key.", "Error", wx.OK | wx.ICON_WARNING)
            return

        self._run_btn.Disable()
        self._last_model_name = model_name

        if not is_rerun:
            self._result.SetValue("Collecting data...\n")
            self._previous_response = ""

        wx.Yield()

        board = pcbnew.GetBoard()
        info = _collect_context(board, include_datasheet_links=include_ds)

        if filter_text:
            info = apply_component_filter(info, filter_text)
            wx.CallAfter(self._update_header, filter_text)

        if info.get("context") == "both":
            prompt = self._prompt_both(info, include_ds, focus, custom)
        else:
            prompt = self._prompt_pcb_only(info)

        system_prompt = self._get_focus_system_prompt(focus, custom)

        if is_rerun and previous_context:
            prompt = f"Previous analysis:\n{previous_context}\n\n---\n\nNew request:\n{prompt}"

        try:
            stream = self._call_llm_streaming(model_id, api_key, base_url, api_type, prompt, system_prompt)

            if stream is None:
                text, usage = self._call_llm_non_streaming(model_id, api_key, base_url, api_type, prompt, system_prompt)
                self._result.SetValue(text)
                self._last_response = text
                self._token_usage = usage or {}
            else:
                self._result.SetValue("")
                full = []
                for chunk in stream:
                    full.append(chunk)
                    wx.CallAfter(self._append_to_result, chunk)
                    wx.Yield()
                self._last_response = "".join(full)

        except Exception as e:
            self._result.SetValue(f"Error:\n{str(e)}")
            self._run_btn.Enable()
            return

        if is_rerun and previous_context:
            final = self._generate_comparison_view(previous_context, self._last_response)
            self._result.SetValue(final)

        wx.CallAfter(self._update_token_display)
        self._run_btn.Enable()

    def _generate_comparison_view(self, previous, current):
        from difflib import unified_diff
        prev_lines = previous.strip().splitlines(keepends=True)
        curr_lines = current.strip().splitlines(keepends=True)
        diff = list(unified_diff(prev_lines, curr_lines, fromfile="Previous", tofile="New", lineterm=""))

        output = ["═" * 85, "RE-RUN COMPARISON VIEW", "═" * 85, ""]
        if diff:
            output.append("### Changes Detected:")
            output.append("```diff")
            output.extend(diff[:100])
            output.append("```")
        else:
            output.append("No significant changes detected.")

        output.append("\n### Previous Analysis\n" + previous.strip())
        output.append("\n### New Analysis\n" + current.strip())
        return "\n".join(output)

    def _on_rerun(self, event):
        if not self._last_response:
            wx.MessageBox("No previous analysis to compare.", "Info", wx.OK | wx.ICON_INFORMATION)
            return
        self._previous_response = self._last_response
        self._on_run(event, is_rerun=True, previous_context=self._last_response)

    def _on_save_report(self, event):
        board = pcbnew.GetBoard()
        if not board:
            return
        pcb_path = Path(board.GetFileName())
        if not pcb_path.exists():
            wx.MessageBox("Save your PCB first.", "Error", wx.OK | wx.ICON_WARNING)
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
        filename = f"{pcb_path.stem}_LLM_Review_{ts}.md"
        path = pcb_path.parent / filename

        content = f"# LLM Analysis Report – {pcb_path.stem}\n\n"
        content += f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        content += f"**Model:** {self._last_model_name}\n\n"
        if self._previous_response:
            content += "## Previous Analysis\n" + self._previous_response + "\n\n---\n\n"
        content += "## Latest Analysis\n" + self._result.GetValue()

        try:
            path.write_text(content, encoding="utf-8")
            wx.MessageBox(f"Report saved to:\n{path}", "Success", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            wx.MessageBox(f"Failed to save:\n{e}", "Error", wx.OK | wx.ICON_ERROR)

    def _on_copy_result(self, event):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self._result.GetValue()))
            wx.TheClipboard.Close()
        self._copy_status.SetLabel("✓ Copied")
        wx.CallLater(2500, lambda: self._copy_status.SetLabel("") if self else None)

    def _on_copy_tokens(self, event):
        text = f"Input: {self._token_input.GetLabel()}\nOutput: {self._token_output.GetLabel()}\nTotal: {self._token_total.GetLabel()}"
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
        self._copy_status.SetLabel("✓ Copied")
        wx.CallLater(2500, lambda: self._copy_status.SetLabel("") if self else None)


# End of file
