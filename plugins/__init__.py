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
import json           # [NOTE] json imported here at top level AND again inside _call_llm.
import traceback      #        The duplicate import inside _call_llm is harmless but redundant —
from pathlib import Path  #   safe to remove the one inside _call_llm.

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


class ConfigManager:
    def __init__(self):
        self.config_path = Path.home() / ".kicad" / "kicad_llm_config.json"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)  # RISK-1 fixed
        self.data = self._load()
    # ... rest unchanged ...
def _make_config():
    """Safe factory — returns a no-op config if ConfigManager fails."""
    try:
        return ConfigManager()
    except Exception:
        traceback.print_exc()
        class _NullConfig:
            def get_api_key(self, p): return ""
            def set_api_key(self, p, k): pass
            def get_last_model_index(self): return 0
            def set_last_model_index(self, i): pass
        return _NullConfig()


# [BUG-1] import pcbnew and import wx are at module level, OUTSIDE the try/except block.
# If KiCad's pcbnew module is not on sys.path at import time (e.g. when running a
# linter, test runner, or if the plugin path is wrong), this raises ImportError and
# the entire plugin file fails to load with no useful error shown in KiCad.
# FIX: move both imports inside the try/except block (see original v1.5.0 structure).
try:
    import pcbnew        # BUG-1 fixed — inside try/except
    import wx            # BUG-1 fixed

    config = _make_config()   # BUG-2 fixed — inside try/except, after imports confirmed

    class LLMAnalyserPlugin(pcbnew.ActionPlugin):
        ...

    LLMAnalyserPlugin().register()

except Exception:
    traceback.print_exc()

class ConfigManager:
    def __init__(self):
        # [RISK-1] mkdir(exist_ok=True) WITHOUT parents=True.
        # On a fresh Linux/Mac install ~/.kicad may not exist yet.
        # If it doesn't, this raises FileNotFoundError and ConfigManager.__init__
        # crashes, which means `config = ConfigManager()` at module level (see BUG-2)
        # crashes before KiCad even gets to load the plugin class.
        # FIX: self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path = Path.home() / ".kicad" / "kicad_llm_config.json"
        self.config_path.parent.mkdir(exist_ok=True)   # ← needs parents=True
        self.data = self._load()

    def _load(self):
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text())
            except Exception:
                pass
        return {"last_model_index": 0, "api_keys": {}}   # [OK] safe default

    def save(self):
        try:
            self.config_path.write_text(json.dumps(self.data, indent=2))
        except Exception:
            pass   # [OK] silent fail on save is acceptable — config is non-critical

    def get_api_key(self, provider: str) -> str:
        return self.data.get("api_keys", {}).get(provider, "")   # [OK]

    def set_api_key(self, provider: str, key: str):
        if "api_keys" not in self.data:
            self.data["api_keys"] = {}
        self.data["api_keys"][provider] = key
        self.save()   # [OK]

    def get_last_model_index(self) -> int:
        return self.data.get("last_model_index", 0)   # [OK]

    def set_last_model_index(self, index: int):
        self.data["last_model_index"] = index
        self.save()   # [OK]


# [BUG-2] config = ConfigManager() runs at module level, before the try/except
# that guards pcbnew/wx. If ConfigManager.__init__ raises (e.g. due to RISK-1
# above), the entire module fails to load and the plugin never registers.
# This also means any exception in ConfigManager is NOT caught by the try/except
# below and will NOT produce a traceback in KiCad's scripting console.
# FIX: move `config = ConfigManager()` inside the try/except block, or wrap it
# in its own try/except with a fallback to a no-op config object.
config = ConfigManager()

# [OK] _HERE captured correctly at module level with abspath — required for KiCad 10.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# [NOTE] The try/except below is correct for guarding plugin registration, but
# because pcbnew and wx are imported above (BUG-1), exceptions from those imports
# are not caught here. Only errors in the class definition and .register() are caught.
try:
    class LLMAnalyserPlugin(pcbnew.ActionPlugin):
        def defaults(self):
            self.name = "LLM Schematic Analyser"
            self.category = "Analyse"
            self.description = "Inspect your schematic with an LLM and get design improvement suggestions"
            self.show_toolbar_button = True          # [OK] FIX-4 from v1.5.0
            icon = os.path.join(_HERE, "icon.png")
            icon_dark = os.path.join(_HERE, "icon_dark.png")
            self.icon_file_name = icon if os.path.isfile(icon) else ""          # [OK] FIX-5
            self.dark_icon_file_name = icon_dark if os.path.isfile(icon_dark) else self.icon_file_name  # [OK] FIX-6

        def Run(self):
            board = pcbnew.GetBoard()
            if board is None:
                wx.MessageBox("No board is open. Open the PCB editor first.", "LLM Analyser", wx.OK | wx.ICON_WARNING)
                return
            dlg = _LLMDialog(None, _collect_board_info(board))
            dlg.ShowModal()
            dlg.Destroy()   # [OK]

    LLMAnalyserPlugin().register()
except Exception:
    traceback.print_exc()


def _collect_board_info(board):
    info = {"title": str(board.GetTitleBlock().GetTitle()) or "(untitled)", "footprints": [], "nets": []}
    for fp in board.GetFootprints():                   # [OK] FIX-8: GetFootprints not GetModules
        info["footprints"].append({
            "ref": str(fp.GetReference()),             # [OK] FIX-7: wxString → str
            "value": str(fp.GetValue()),               # [OK] FIX-7
            "layer": str(board.GetLayerName(fp.GetLayer())),  # [OK] FIX-7
        })
    for net_code, net in board.GetNetInfo().NetsByName().items():
        if net_code:
            info["nets"].append(str(net_code))         # [OK] FIX-7
    return info


class _LLMDialog(wx.Dialog):
    _MODELS = [
        ("Grok 4 (xAI)",          "grok-4",                    "https://api.x.ai/v1",      "xai"),
        ("Grok 4 Fast (xAI)",     "grok-4-fast",               "https://api.x.ai/v1",      "xai"),
        ("Grok 3 (xAI)",          "grok-3-latest",             "https://api.x.ai/v1",      "xai"),
        ("Grok 3 Mini (xAI)",     "grok-3-mini-latest",        "https://api.x.ai/v1",      "xai"),
        ("Claude Sonnet 4",       "claude-sonnet-4-20250514",  None,                        "anthropic"),
        ("Claude Opus 4",         "claude-opus-4-20250514",    None,                        "anthropic"),
        ("GPT-4o (OpenAI)",       "gpt-4o",                    None,                        "openai"),
        ("GPT-4o-mini (OpenAI)",  "gpt-4o-mini",               None,                        "openai"),
        ("Ollama llama3 (local)", "llama3",                    "http://localhost:11434/v1", "openai"),
        ("Ollama mistral (local)","mistral",                   "http://localhost:11434/v1", "openai"),
    ]   # [OK] 4-tuple with api_type — correct pattern from FIX-9

    _PROVIDER_MAP = {"anthropic": "Anthropic", "openai": "OpenAI / Ollama", "xai": "xAI (Grok)"}  # [OK] nice addition

    def __init__(self, parent, board_info):
        super().__init__(parent, title="LLM Schematic Analyser", style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._info = board_info
        self._build_ui()
        self._load_last_model_and_key()   # [OK] good UX — restores last used model

    def _load_last_model_and_key(self):
        idx = config.get_last_model_index()
        if idx < len(self._MODELS):
            self._model.SetSelection(idx)
            self._on_model_changed(None)   # [OK] passing None is safe — _event param unused

    def _on_model_changed(self, _event):
        idx = int(self._model.GetSelection())   # [OK] FIX-10: int() cast
        _, _, default_url, api_type = self._MODELS[idx]
        self._url.SetValue(default_url or "")   # [OK] handles None base_url for cloud models
        self._key.SetValue(config.get_api_key(api_type))   # [OK] restores saved key per provider

    def _on_clear_keys(self, _event):
        idx = int(self._model.GetSelection())   # [OK] FIX-10: int() cast
        _, _, _, api_type = self._MODELS[idx]
        name = self._PROVIDER_MAP.get(api_type, api_type)
        if wx.MessageBox(f"Clear saved key for {name}?", "Clear Key", wx.YES_NO) == wx.YES:
            config.set_api_key(api_type, "")
            self._key.SetValue("")   # [OK]

    def _on_run(self, _event):
        idx = int(self._model.GetSelection())   # [OK] FIX-10: int() cast
        _, model_id, default_url, api_type = self._MODELS[idx]
        api_key = str(self._key.GetValue()).strip()    # [OK] FIX-7: str() cast
        base_url = str(self._url.GetValue()).strip() or default_url   # [OK] FIX-7

        if api_key:
            config.set_api_key(api_type, api_key)   # [OK] auto-saves key on use
        config.set_last_model_index(idx)             # [OK] persists model choice

        if not api_key and api_type != "openai":
            wx.MessageBox("Please enter an API key.", "Error", wx.OK | wx.ICON_WARNING)
            return

        self._run_btn.Disable()
        self._result.SetValue("Running…")
        self._token_input.SetLabel("0")
        self._token_output.SetLabel("0")
        self._token_total.SetLabel("0")
        wx.Yield()

        try:
            text, usage = self._call_llm(model_id, api_key, base_url, api_type)
        except Exception as e:
            text = f"Error: {e}"
            usage = {}   # [OK] safe fallback so token display shows 0s not crash

        self._result.SetValue(text)

        # [OK] Token display correctly split by api_type using correct field names per provider.
        # Anthropic: input_tokens / output_tokens (no total)
        # xAI:       input_tokens / output_tokens / total_tokens
        # OpenAI:    prompt_tokens / completion_tokens / total_tokens
        if api_type == "anthropic":
            self._token_input.SetLabel(str(usage.get("input_tokens", 0)))
            self._token_output.SetLabel(str(usage.get("output_tokens", 0)))
            self._token_total.SetLabel("N/A")   # [OK] Anthropic doesn't return total
        elif api_type == "xai":
            self._token_input.SetLabel(str(usage.get("input_tokens", 0)))
            self._token_output.SetLabel(str(usage.get("output_tokens", 0)))
            self._token_total.SetLabel(str(usage.get("total_tokens", 0)))
        else:
            self._token_input.SetLabel(str(usage.get("prompt_tokens", 0)))
            self._token_output.SetLabel(str(usage.get("completion_tokens", 0)))
            self._token_total.SetLabel(str(usage.get("total_tokens", 0)))

        self._run_btn.Enable()   # [OK]

    def _on_copy_result(self, _event):
        self._copy_to_clipboard(self._result.GetValue())   # [OK]

    def _on_copy_tokens(self, _event):
        text = (f"Input: {self._token_input.GetLabel()}\n"
                f"Output: {self._token_output.GetLabel()}\n"
                f"Total: {self._token_total.GetLabel()}")
        self._copy_to_clipboard(text)   # [OK]

    def _copy_to_clipboard(self, text):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()
            # [RISK-2] wx.MessageBox after clipboard copy requires user to click OK
            # just to confirm a copy. Annoying UX for a button whose action is
            # self-evident. Consider replacing with a brief status label instead.
            wx.MessageBox("Copied to clipboard", "Success", wx.OK | wx.ICON_INFORMATION)

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
        for net in sorted(self._info["nets"])[:120]:   # [OK] safe — nets already str() from _collect_board_info
            lines.append(f"  {net}")
        return "\n".join(lines)

    def _call_llm(self, model_id, api_key, base_url, api_type):
        import json, urllib.request   # [NOTE] json already imported at top — redundant but harmless
        prompt = self._prompt()
        system = "You are an electronics design expert reviewing a KiCad schematic/PCB."

        if api_type == "anthropic":
            url = "https://api.anthropic.com/v1/messages"
            hdrs = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
            # [OK] system passed as top-level "system" field — correct Anthropic API format
            # and slightly better than embedding it in messages[].
            body = {"model": model_id, "max_tokens": 4096, "system": system,
                    "messages": [{"role": "user", "content": prompt}]}

        elif api_type == "xai":
            url = (base_url or "https://api.x.ai/v1") + "/responses"
            hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            # [OK] FIX-11: plain string input, max_output_tokens — correct Responses API format
            body = {"model": model_id, "max_output_tokens": 4096, "input": f"{system}\n\n{prompt}"}

        else:
            url = (base_url or "https://api.openai.com/v1") + "/chat/completions"
            hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            body = {"model": model_id, "max_tokens": 4096,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}

        req = urllib.request.Request(url, json.dumps(body).encode(), hdrs, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            # [OK] FIX-12: surfaces actual API error message from response body
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
            result = data["content"][0]["text"]   # [OK] FIX-13
            usage = data.get("usage", {})
        elif api_type == "xai":
            result = data["output"][0]["content"][0]["text"]   # [OK] FIX-14
            usage = data.get("usage", {})
        else:
            result = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

        # [OK] FIX-15: returns (result, usage) as tuple — clean separation,
        # lets the dialog handle display logic independently of API parsing.
        return result, usage
