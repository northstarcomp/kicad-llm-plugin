"""
KiCad LLM Plugin v1.7.0 RC8 (Full Model List)    # [OK] correct version in docstring
"""

import os, sys, json, traceback
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))  # [OK] module-level, abspath — correct
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# ── ConfigManager ─────────────────────────────────────────────────────────

class ConfigManager:
    def __init__(self):
        # [OK] ~/.local/share/kicad/ — correct KiCad 10 Linux location (FIX-2)
        self.config_path = Path.home() / ".local" / "share" / "kicad" / "kicad_llm_config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)  # [OK] parents=True (FIX-16)
        self.data = self._load()

    def _load(self):
        if self.config_path.exists():
            try: return json.loads(self.config_path.read_text())
            except: pass   # [OK] silent fallback to defaults
        return {"last_model_index": 0, "api_keys": {}}

    def save(self):
        try: self.config_path.write_text(json.dumps(self.data, indent=2))
        except: pass  # [OK] silent failure — plugin still works if disk is full

    def get_api_key(self, provider): return self.data.get("api_keys", {}).get(provider, "")
    def set_api_key(self, provider, key):
        self.data.setdefault("api_keys", {})[provider] = key; self.save()
    def get_last_model_index(self): return self.data.get("last_model_index", 0)
    def set_last_model_index(self, idx): self.data["last_model_index"] = idx; self.save()


def _make_config():                    # [OK] safe factory — failures don't crash plugin
    try: return ConfigManager()
    except:
        traceback.print_exc()
        class _NullConfig:
            def get_api_key(self, p): return ""
            def set_api_key(self, p, k): pass
            def get_last_model_index(self): return 0
            def set_last_model_index(self, i): pass
        return _NullConfig()


# ── S-Expression Parser ───────────────────────────────────────────────────

# [OK] Proper recursive-descent parser (SEXP-1 from v1.7.0) — handles multi-line
# properties, escaped quotes, and nested structures correctly. Much more robust
# than the regex approach used in v1.6.0.

def parse_sexp(text): ...
def tokenize(text): ...
def read_expr(tokens, pos): ...
def find_all(expr, tag): ...
def get_prop(node, name): ...
def first_atom(node, tag): ...


# ── Context collection ────────────────────────────────────────────────────

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
        # [OK] datasheet_links always present in dict — avoids KeyError in prompt builder
        "datasheet_links": {}
    }
    # [OK] str() casts on all KiCad API returns — prevents wxString crash (FIX-7)
    # [OK] GetFootprints() — correct KiCad 7+ API (FIX-8)
    for fp in board.GetFootprints(): ...
    for net_code, _ in board.GetNetInfo().NetsByName().items(): ...

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
    # [OK] full traceback printed to KiCad scripting console on parse error (SEXP-8)
    try: return _parse_kicad_sch(sch_file, include_datasheet_links)
    except: traceback.print_exc(); return None


def _parse_kicad_sch(sch_file, include_datasheet_links=False):
    ...
    for sym in find_all(root, 'symbol'):
        lib_id = first_atom(sym, 'lib_id')
        ref    = get_prop(sym, 'Reference') or "?"
        value  = get_prop(sym, 'Value')     or "?"
        entry  = {"lib_id": lib_id, "ref": ref, "value": value}

        if include_datasheet_links:
            ds = get_prop(sym, 'Datasheet')
            if ds and ds.startswith("http"):
                # [OK] Deduplication by URL — groups refs sharing the same datasheet
                if ds not in datasheet_links:
                    datasheet_links[ds] = []
                datasheet_links[ds].append(ref)

        # [BUG-RC8-1] Power detection uses value string matching:
        #   value.upper() in ("VCC", "GND", "+3V3", "+5V")
        # This incorrectly classifies ANY symbol whose Value field matches
        # those strings as a power symbol. A resistor with Value "GND" (used
        # as a net-tie or termination), or a test point labelled "+5V", would
        # be moved to pwr_symbols and hidden from the main symbol list.
        # FIX: remove value-string check; rely only on lib_id prefix and #PWR ref.
        if lib_id.lower().startswith("power:") or value.upper() in ("VCC", "GND", "+3V3", "+5V"):
            pwr_symbols.append(entry)   # ← BUG: value check too broad
        # FIXED version:
        # if lib_id.lower().startswith("power:") or (ref and ref.startswith("#PWR")):
        #     pwr_symbols.append(entry)

    # [BUG-RC8-3] Net label extraction only handles KiCad 10 format.
    # KiCad 10: (net_label (text "VCC") ...)  — text is a (text "...") child list ✓
    # KiCad 8/9: (net_label "VCC" ...)         — text is the second atom of the node ✗ missing
    # If a KiCad 8/9 schematic is opened, ALL net label names will be silently
    # lost, producing an empty sch_nets list and a less useful LLM prompt.
    for tag in ('net_label', 'global_label', 'hierarchical_label'):
        for node in find_all(root, tag):
            for child in node:
                if isinstance(child, list) and child and child[0] == 'text':
                    if len(child) > 1: sch_nets.add(child[1]); break
    # FIXED version handles both formats:
    # for child in node:
    #     if isinstance(child, list) and child[0] == 'text' and len(child) > 1:
    #         txt = child[1]; break
    # if not txt and len(node) > 1 and isinstance(node[1], str):
    #     txt = node[1]   # ← KiCad 8/9 inline string fallback
    # if txt: sch_nets.add(txt)

    result = {
        "sch_file": sch_file.name,
        "symbols": symbols,
        "pwr_symbols": pwr_symbols,
        "sch_nets": sorted(sch_nets),
        "no_connects": len(find_all(root, 'no_connect')),
        # [NOTE-1] wire_count was present in v1.7.0 design but dropped in RC8.
        # It's a useful metric (very low wire count on a complex board hints at
        # an incomplete schematic). Trivial to add back:
        # "wire_count": len(find_all(root, 'wire')),
    }
    if include_datasheet_links and datasheet_links:
        result["datasheet_links"] = datasheet_links
    return result


# ── Plugin registration ───────────────────────────────────────────────────

try:
    import pcbnew   # [OK] inside try/except (FIX-17)
    import wx       # [OK] inside try/except (FIX-17)
    config = _make_config()  # [OK] safe factory after imports (FIX-18)

    class LLMAnalyserPlugin(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "LLM Schematic/PCB Analyser"
            self.category = "Analyse"
            self.description = "v1.7 RC8 - Full Model List"
            self.show_toolbar_button = True           # [OK] required for KiCad 10 (FIX-4)
            icon      = os.path.join(_HERE, "icon.png")
            icon_dark = os.path.join(_HERE, "icon_dark.png")
            self.icon_file_name      = icon      if os.path.isfile(icon)      else ""  # [OK] FIX-5
            self.dark_icon_file_name = icon_dark if os.path.isfile(icon_dark) else self.icon_file_name  # [OK] FIX-6

        def Run(self):
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("Open a PCB first.", "LLM Analyser", wx.OK | wx.ICON_WARNING)
                return
            # [BUG-RC8-2] _collect_context called here with include_datasheet_links=False,
            # then called AGAIN in _on_run() with include_ds from the checkbox.
            # This parses the .kicad_sch file TWICE on every run — unnecessary IO.
            # FIXED: pass this initial info to the dialog; _on_run() only re-collects
            # if the datasheet checkbox is ticked (since this call won't have DS links).
            info = _collect_context(board, include_datasheet_links=False)
            dlg = _LLMDialog(None, info)
            dlg.ShowModal()
            dlg.Destroy()

except Exception:
    traceback.print_exc()  # [OK] full traceback to scripting console


# ── Dialog ────────────────────────────────────────────────────────────────

class _LLMDialog(wx.Dialog):

    _MODELS = [
        # [OK] Full model list — good coverage across providers
        ("Grok 4 (xAI)",             "grok-4",                    "https://api.x.ai/v1",      "xai"),
        ("Grok 4 Fast (xAI)",        "grok-4-fast",               "https://api.x.ai/v1",      "xai"),
        ("Grok 3 (xAI)",             "grok-3-latest",             "https://api.x.ai/v1",      "xai"),
        ("Grok 3 Mini (xAI)",        "grok-3-mini-latest",        "https://api.x.ai/v1",      "xai"),
        ("Claude Sonnet 4",          "claude-sonnet-4-20250514",  None,                        "anthropic"),
        ("Claude Opus 4",            "claude-opus-4-20250514",    None,                        "anthropic"),
        ("GPT-4o (OpenAI)",          "gpt-4o",                    None,                        "openai"),
        ("GPT-4o-mini (OpenAI)",     "gpt-4o-mini",               None,                        "openai"),
        # [OK] Gemini added with correct native API format
        ("Gemini 2.5 Pro (Google)",  "gemini-2.5-pro",            None,                        "gemini"),
        ("Gemini 2.5 Flash (Google)","gemini-2.5-flash",          None,                        "gemini"),
        # [OK] Ollama local models — no API key required
        ("Ollama llama3 (local)",    "llama3",                    "http://localhost:11434/v1", "openai"),
        ("Ollama mistral (local)",   "mistral",                   "http://localhost:11434/v1", "openai"),
        ("Ollama gemma2 (local)",    "gemma2",                    "http://localhost:11434/v1", "openai"),
        ("Ollama qwen2.5 (local)",   "qwen2.5",                   "http://localhost:11434/v1", "openai"),
    ]

    def __init__(self, parent, info):
        ...
        self._info = info  # [OK] stores initial info from Run()
        self._include_datasheet = False
        self._build_ui()
        self._load_last_model_and_key()

    def _build_ui(self):
        ...
        # [OK] Datasheet checkbox with explanatory note that hides/shows
        self._ds_checkbox = wx.CheckBox(p, label="Include Datasheet Links")
        self._ds_note = wx.StaticText(p, label="Unique datasheet links will be included...")
        self._ds_note.Hide()
        ...

    def _on_ds_checkbox(self, event):
        # [OK] Toggle note visibility when checkbox changes
        self._include_datasheet = self._ds_checkbox.GetValue()
        if self._include_datasheet: self._ds_note.Show()
        else: self._ds_note.Hide()
        self.Layout()

    def _on_model_changed(self, _event):
        idx = int(self._model.GetSelection())  # [OK] int() cast — wxString fix (FIX-10)
        _, _, default_url, api_type = self._MODELS[idx]
        self._url.SetValue(default_url or "")
        self._key.SetValue(config.get_api_key(api_type))

    def _on_run(self, _event):
        idx = int(self._model.GetSelection())   # [OK] FIX-10
        _, model_id, default_url, api_type = self._MODELS[idx]
        api_key  = str(self._key.GetValue()).strip()           # [OK] FIX-7
        base_url = str(self._url.GetValue()).strip() or default_url
        include_ds = self._ds_checkbox.GetValue()

        if api_key: config.set_api_key(api_type, api_key)
        config.set_last_model_index(idx)

        # [BUG-RC8-4] "gemini" is excluded from the no-key guard:
        #   api_type not in ["openai", "gemini"]
        # But Gemini DOES require an API key! Without the guard, selecting
        # Gemini with a blank key sends a request to Google which returns
        # a 400 error — no helpful "Please enter an API key" message.
        # "openai" is correctly excluded because Ollama uses api_type="openai"
        # and Ollama needs no key.
        if not api_key and api_type not in ["openai", "gemini"]:  # ← BUG: gemini should NOT be excluded
            wx.MessageBox("Please enter an API key.", "Error", wx.OK | wx.ICON_WARNING)
            return
        # FIXED version:
        # if not api_key and api_type != "openai":   # only openai/Ollama needs no key

        ...
        # [BUG-RC8-2 continued] Second _collect_context call here — should be conditional
        info = _collect_context(pcbnew.GetBoard(), include_datasheet_links=include_ds)  # ← BUG: always re-collects
        # FIXED version:
        # if include_ds and not self._info.get("datasheet_links"):
        #     info = _collect_context(pcbnew.GetBoard(), include_datasheet_links=True)
        # else:
        #     info = self._info   # ← reuse already-collected data

        ...

    def _copy_to_clipboard(self, text):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
        # [OK] Status label instead of modal MessageBox popup (FIX-3)
        self._copy_status.SetLabel("✓ Copied")
        wx.CallLater(3000, lambda: self._copy_status.SetLabel("") if self else None)

    def _prompt_both(self, info, include_ds):
        lines = ["You are an expert reviewing both schematic and PCB.",
                 "Cross-reference schematic intent vs PCB implementation.",
                 f"Board: {info['title']}", "Symbols:"]
        # [NOTE-2] Symbols truncated to 80 — v1.7.0 design allowed 150.
        # For large boards (100+ components) this will cut off many symbols.
        # Consider raising to 150 to match the v1.7.0 design limit.
        for sym in info.get("symbols", [])[:80]:
            lines.append(f"  {sym['ref']} {sym['value']}")
            # [NOTE-3] lib_id, footprint, unit, and pin_count are NOT included
            # in the prompt despite being available in the info dict.
            # Including them (as in v1.7.0) gives the LLM more context:
            #   f"  {sym['ref']} {sym['value']} [{sym['lib_id']}] fp:{fp_short}"
        lines += ["PCB Footprints:"]
        # [NOTE-2] Same 80-component limit for footprints
        for fp in info["footprints"][:80]:
            lines.append(f"  {fp['ref']} {fp['value']}")
            # [NOTE-3] Layer not included — useful for identifying F.Cu vs B.Cu placement
        ...

    def _call_llm(self, model_id, api_key, base_url, api_type, info, include_ds):
        ...
        elif api_type == "gemini":
            model_name = model_id
            # [WARN-1] Gemini API key passed as a URL query parameter.
            # This is how Google's REST API works — it's the correct approach —
            # but the key will appear in server access logs and any network proxy
            # or debug traffic. The alternative (using an Authorization header)
            # requires OAuth tokens, which is more complex. Acceptable for now.
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            hdrs = {"Content-Type": "application/json"}
            # [OK] Gemini request format correct: contents/parts structure
            body = {"contents": [{"parts": [{"text": f"{system}\n\n{prompt}"}]}]}
        ...

        # [OK] Gemini response parsed correctly:
        #   candidates[0].content.parts[0].text
        elif api_type == "gemini":
            text  = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata", {})   # [OK] correct Gemini usage key
            return text, usage

        # [OK] Gemini token fields handled in _on_run:
        #   promptTokenCount / candidatesTokenCount / totalTokenCount
