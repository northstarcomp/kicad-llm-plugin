"""
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

# [FIX-A] _HERE captured at module level with abspath BEFORE anything else.
# KiCad may change cwd between module load and defaults() — capturing here
# guarantees the icon path is always correct.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# NOTE: pcbnew and wx are imported INSIDE the try/except below — see [FIX-B].


# ════════════════════════════════════════════════════════════════════════════
#  Config — persistent API keys and last-used model
# ════════════════════════════════════════════════════════════════════════════

class ConfigManager:
    CONFIG_PATH = Path.home() / ".kicad" / "kicad_llm_config.json"

    def __init__(self):
        # [FIX-C] parents=True creates all intermediate dirs, not just the last.
        self.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        if self.CONFIG_PATH.exists():
            try:
                return json.loads(self.CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"last_model_index": 0, "api_keys": {}}

    def save(self):
        try:
            self.CONFIG_PATH.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            # [FIX-D] Restrict to owner read/write on POSIX (no-op on Windows).
            try:
                os.chmod(self.CONFIG_PATH, 0o600)
            except Exception:
                pass
        except Exception:
            pass

    def get_api_key(self, provider: str) -> str:
        return self.data.get("api_keys", {}).get(provider, "")

    def set_api_key(self, provider: str, key: str):
        self.data.setdefault("api_keys", {})[provider] = key
        self.save()

    def get_last_model_index(self) -> int:
        return self.data.get("last_model_index", 0)

    def set_last_model_index(self, index: int):
        self.data["last_model_index"] = index
        self.save()


# [FIX-E] ConfigManager instantiated inside its own try/except so filesystem
# errors don't prevent the rest of the plugin from loading.
try:
    config = ConfigManager()
except Exception:
    traceback.print_exc()
    class _NullConfig:
        def get_api_key(self, p): return ""
        def set_api_key(self, p, k): pass
        def get_last_model_index(self): return 0
        def set_last_model_index(self, i): pass
    config = _NullConfig()


# ════════════════════════════════════════════════════════════════════════════
#  Plugin registration
# ════════════════════════════════════════════════════════════════════════════

try:
    import pcbnew   # [FIX-B] inside try/except — ImportError now caught+logged
    import wx       # [FIX-B] inside try/except

    class LLMAnalyserPlugin(pcbnew.ActionPlugin):

        def defaults(self):
            self.name        = "LLM Schematic Analyser"
            self.category    = "Analyse"
            self.description = ("Inspect your schematic with an LLM "
                                "and get design improvement suggestions")
            self.show_toolbar_button = True          # [FIX-4] explicit True
            icon      = os.path.join(_HERE, "icon.png")
            icon_dark = os.path.join(_HERE, "icon_dark.png")
            self.icon_file_name      = icon      if os.path.isfile(icon)      else ""
            self.dark_icon_file_name = icon_dark if os.path.isfile(icon_dark) else self.icon_file_name

        def Run(self):
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox(
                    "No board is open.\n"
                    "Open the PCB editor first (even an empty board works).",
                    "LLM Analyser", wx.OK | wx.ICON_WARNING)
                return
            dlg = _LLMDialog(None, _collect_board_info(board))
            dlg.ShowModal()
            dlg.Destroy()

    LLMAnalyserPlugin().register()

except Exception:
    traceback.print_exc()


# ════════════════════════════════════════════════════════════════════════════
#  Board data collection
# ════════════════════════════════════════════════════════════════════════════

def _collect_board_info(board):
    info = {
        "title":      str(board.GetTitleBlock().GetTitle()) or "(untitled)",
        "footprints": [],
        "nets":       [],
    }
    for fp in board.GetFootprints():                          # [FIX-8] GetModules() removed in KiCad 7
        info["footprints"].append({
            "ref":   str(fp.GetReference()),                  # [FIX-7] wxString→str
            "value": str(fp.GetValue()),                      # [FIX-7] wxString→str
            "layer": str(board.GetLayerName(fp.GetLayer())), # [FIX-7] wxString→str
        })
    for net_code, _ in board.GetNetInfo().NetsByName().items():
        if net_code:
            info["nets"].append(str(net_code))               # [FIX-7] wxString→str
    return info


# ════════════════════════════════════════════════════════════════════════════
#  Dialog
# ════════════════════════════════════════════════════════════════════════════

class _LLMDialog(wx.Dialog):

    _MODELS = [
        ("Grok 4 (xAI)",           "grok-4",                    "https://api.x.ai/v1",      "xai"),
        ("Grok 4 Fast (xAI)",      "grok-4-fast",               "https://api.x.ai/v1",      "xai"),
        ("Grok 3 (xAI)",           "grok-3-latest",             "https://api.x.ai/v1",      "xai"),
        ("Grok 3 Mini (xAI)",      "grok-3-mini-latest",        "https://api.x.ai/v1",      "xai"),
        ("Claude Sonnet 4",        "claude-sonnet-4-20250514",  None,                        "anthropic"),
        ("Claude Opus 4",          "claude-opus-4-20250514",    None,                        "anthropic"),
        ("GPT-4o (OpenAI)",        "gpt-4o",                    None,                        "openai"),
        ("GPT-4o-mini (OpenAI)",   "gpt-4o-mini",               None,                        "openai"),
        ("Ollama llama3 (local)",  "llama3",                    "http://localhost:11434/v1", "openai"),
        ("Ollama mistral (local)", "mistral",                   "http://localhost:11434/v1", "openai"),
    ]

    _PROVIDER_MAP = {
        "anthropic": "Anthropic",
        "openai":    "OpenAI / Ollama",
        "xai":       "xAI (Grok)",
    }

    def __init__(self, parent, board_info):
        super().__init__(parent, title="LLM Schematic Analyser",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._info = board_info
        self._build_ui()
        self._load_saved_state()

    def _build_ui(self):
        p = self
        s = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(p, label=(
            f"Board: {self._info['title']}  |  "
            f"Footprints: {len(self._info['footprints'])}  "
            f"Nets: {len(self._info['nets'])}"
        ))
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
        self._btn_clear = wx.Button(p, label="Clear Key", size=(80, 24))
        self._btn_clear.Bind(wx.EVT_BUTTON, self._on_clear_key)
        row.Add(self._btn_clear, 0, wx.LEFT, 6)
        s.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(p, label="Base URL:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._url = wx.TextCtrl(p)
        self._url.SetHint("Leave blank for cloud providers")
        row3.Add(self._url, 1)
        s.Add(row3, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._run_btn = wx.Button(p, label="▶  Run Analysis")
        self._run_btn.Bind(wx.EVT_BUTTON, self._on_run)
        s.Add(self._run_btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        h1 = wx.BoxSizer(wx.HORIZONTAL)
        h1.Add(wx.StaticText(p, label="AI Response:"), 0, wx.ALIGN_CENTER_VERTICAL)
        h1.AddStretchSpacer()
        self._btn_copy_result = wx.Button(p, label="Copy", size=(60, 24))
        self._btn_copy_result.Bind(wx.EVT_BUTTON, self._on_copy_result)
        h1.Add(self._btn_copy_result, 0)
        s.Add(h1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)

        self._result = wx.TextCtrl(p,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_WORDWRAP,
            size=(-1, 260))
        s.Add(self._result, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        token_box = wx.StaticBoxSizer(wx.StaticBox(p, label="Token Usage"), wx.VERTICAL)
        grid = wx.FlexGridSizer(3, 2, 4, 12)
        grid.Add(wx.StaticText(p, label="Input:"))
        self._token_input  = wx.StaticText(p, label="—")
        grid.Add(self._token_input)
        grid.Add(wx.StaticText(p, label="Output:"))
        self._token_output = wx.StaticText(p, label="—")
        grid.Add(self._token_output)
        grid.Add(wx.StaticText(p, label="Total:"))
        self._token_total  = wx.StaticText(p, label="—")
        grid.Add(self._token_total)
        token_box.Add(grid, 0, wx.ALL, 8)
        self._btn_copy_tokens = wx.Button(p, label="Copy Token Usage", size=(140, 24))
        self._btn_copy_tokens.Bind(wx.EVT_BUTTON, self._on_copy_tokens)
        token_box.Add(self._btn_copy_tokens, 0, wx.LEFT | wx.BOTTOM, 8)
        s.Add(token_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        btn_close = wx.Button(p, wx.ID_CLOSE, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        s.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        p.SetSizerAndFit(s)
        self.SetSize((720, 640))

    def _load_saved_state(self):
        idx = config.get_last_model_index()
        if idx >= len(self._MODELS):   # [FIX-9a] bounds check
            idx = 0
        self._model.SetSelection(idx)
        self._on_model_changed(None)

    def _on_model_changed(self, _event):
        idx = int(self._model.GetSelection())              # [FIX-10] wxString→int
        _, _, default_url, api_type = self._MODELS[idx]
        self._url.SetValue(default_url or "")
        self._key.SetValue(config.get_api_key(api_type))

    def _on_clear_key(self, _event):
        idx = int(self._model.GetSelection())              # [FIX-10]
        _, _, _, api_type = self._MODELS[idx]
        name = self._PROVIDER_MAP.get(api_type, api_type)
        if wx.MessageBox(f"Clear saved API key for {name}?",
                         "Clear Key", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            config.set_api_key(api_type, "")
            self._key.SetValue("")

    def _on_run(self, _event):
        idx = int(self._model.GetSelection())              # [FIX-10]
        _, model_id, default_url, api_type = self._MODELS[idx]
        api_key  = str(self._key.GetValue()).strip()       # [FIX-7] wxString→str
        base_url = str(self._url.GetValue()).strip() or default_url  # [FIX-7]

        if api_key:
            config.set_api_key(api_type, api_key)
        config.set_last_model_index(idx)

        if not api_key and api_type != "openai":
            wx.MessageBox("Please enter an API key.", "LLM Analyser",
                          wx.OK | wx.ICON_WARNING)
            return

        self._run_btn.Disable()
        self._result.SetValue("Running… please wait.")
        self._token_input.SetLabel("—")
        self._token_output.SetLabel("—")
        self._token_total.SetLabel("—")
        wx.Yield()

        try:
            text, usage = self._call_llm(model_id, api_key, base_url, api_type)
        except Exception as exc:
            text  = f"Error:\n{exc}"
            usage = {}

        self._result.SetValue(text)
        self._update_token_display(api_type, usage)
        self._run_btn.Enable()

    def _update_token_display(self, api_type, usage):
        if api_type == "anthropic":
            self._token_input.SetLabel(str(usage.get("input_tokens",      "—")))
            self._token_output.SetLabel(str(usage.get("output_tokens",    "—")))
            self._token_total.SetLabel("N/A")
        elif api_type == "xai":
            self._token_input.SetLabel(str(usage.get("input_tokens",      "—")))
            self._token_output.SetLabel(str(usage.get("output_tokens",    "—")))
            self._token_total.SetLabel(str(usage.get("total_tokens",      "—")))
        else:
            self._token_input.SetLabel(str(usage.get("prompt_tokens",     "—")))
            self._token_output.SetLabel(str(usage.get("completion_tokens","—")))
            self._token_total.SetLabel(str(usage.get("total_tokens",      "—")))

    def _on_copy_result(self, _event):
        self._copy_to_clipboard(str(self._result.GetValue()))  # [FIX-7]

    def _on_copy_tokens(self, _event):
        self._copy_to_clipboard(
            f"Input:  {self._token_input.GetLabel()}\n"
            f"Output: {self._token_output.GetLabel()}\n"
            f"Total:  {self._token_total.GetLabel()}"
        )

    def _copy_to_clipboard(self, text: str):
        # [FIX-G] Removed the "Copied!" MessageBox — silent clipboard write
        # is standard UX; the confirmation dialog required an extra click.
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()

    def _prompt(self):
        lines = [
            "You are an electronics design expert reviewing a KiCad schematic/PCB.",
            "Based on the component list and net names below, identify:",
            "1. Fatal flaws",
            "2. Design-rule / best-practice violations",
            "3. Nice-to-have improvements",
            "",
            f"Board: {self._info['title']}",
            "",
            "Footprints (ref, value, layer):",
        ]
        for fp in self._info["footprints"]:
            lines.append(f"  {fp['ref']}  {fp['value']}  ({fp['layer']})")
        lines += ["", "Nets (up to 120):"]
        for net in sorted(self._info["nets"])[:120]:
            lines.append(f"  {net}")
        return "\n".join(lines)

    def _call_llm(self, model_id, api_key, base_url, api_type):
        import urllib.request, urllib.error
        prompt = self._prompt()
        system = "You are an electronics design expert reviewing a KiCad schematic/PCB."

        if api_type == "anthropic":
            url  = "https://api.anthropic.com/v1/messages"
            hdrs = {"Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01"}
            body = {"model": model_id, "max_tokens": 4096,
                    "system": system,           # [FIX-H] top-level field, not a message role
                    "messages": [{"role": "user", "content": prompt}]}

        elif api_type == "xai":
            url  = (base_url or "https://api.x.ai/v1") + "/responses"
            hdrs = {"Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"}
            body = {"model": model_id,
                    "max_output_tokens": 4096,   # [FIX-11] not max_tokens
                    "input": f"{system}\n\n{prompt}"}  # [FIX-11] plain string

        else:
            url  = (base_url or "https://api.openai.com/v1") + "/chat/completions"
            hdrs = {"Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}"}
            body = {"model": model_id, "max_tokens": 4096,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user",   "content": prompt}]}

        req = urllib.request.Request(url, json.dumps(body).encode(), hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")     # [FIX-12]
            try:
                parsed = json.loads(raw)
                msg = parsed.get("error", {})
                if isinstance(msg, dict):
                    msg = msg.get("message", raw)
            except Exception:
                msg = raw
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {msg}")

        if api_type == "anthropic":
            result = data["content"][0]["text"]
        elif api_type == "xai":
            result = data["output"][0]["content"][0]["text"]     # [FIX-14]
        else:
            result = data["choices"][0]["message"]["content"]

        return result, data.get("usage", {})
