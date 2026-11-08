#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hexagon Claw TUI - Terminal User Interface in stile OpenClaw
per llama.cpp su Snapdragon X2 Elite Extreme (Qualcomm Hexagon NPU / Adreno GPU / Oryon CPU).
"""

import sys
import os
import platform
import time
import json
import ctypes
import argparse
import difflib
import math
from html import escape as escape_html
from urllib.parse import urlsplit
from pathlib import Path
from typing import List, Dict, Optional, Tuple

# Configurazione UTF-8 per la console Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.rule import Rule

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.styles import Style as PtStyle
from prompt_toolkit.formatted_text import HTML

from stack_manager import DEFAULT_WORKSPACE, StackManager


# ---------------------------------------------------------------------------
# Rilevamento Telemetria Sistema Windows ARM64 (Senza dipendenze esterne)
# ---------------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def get_system_ram() -> Optional[Tuple[float, float, int]]:
    """Restituisce (total_gb, used_gb, percent_used)"""
    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total = stat.ullTotalPhys / (1024 ** 3)
            used = (stat.ullTotalPhys - stat.ullAvailPhys) / (1024 ** 3)
            return round(total, 1), round(used, 1), stat.dwMemoryLoad
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Gestione Preset e Modelli dal file INI
# ---------------------------------------------------------------------------
def load_ini_presets(ini_path: Path) -> Dict[str, Dict[str, str]]:
    """Carica i preset hardware dal file hexagon-models.ini generato da launch-hexagon.ps1"""
    presets = {}
    if not ini_path.exists():
        return presets

    current_section = None
    with open(ini_path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1].strip()
                presets.setdefault(current_section, {})
            elif current_section and "=" in line:
                key, val = line.split("=", 1)
                presets[current_section][key.strip()] = val.strip()

    defaults = presets.pop("*", {})
    return {name: {**defaults, **values} for name, values in presets.items()}


def iter_sse_events(lines):
    """Legge eventi SSE, inclusi data: senza spazio e payload su più righe."""
    data = []
    ended = False
    for line in lines:
        if ended:
            continue  # Consuma il corpo HTTP per poter riusare la connessione.
        if line == "":
            if not data:
                continue
            payload = "\n".join(data)
            data.clear()
            if payload.strip() == "[DONE]":
                ended = True
                yield None
                continue
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("Evento SSE non valido")
            yield event
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        # Alcuni endpoint omettono la riga vuota conclusiva.
        yield from iter_sse_events([*('data:' + value for value in data), ""])


class ReasoningFilter:
    """Separa tag think anche quando il delimitatore attraversa più frammenti."""
    def __init__(self):
        self.pending = ""
        self.thinking = False

    def feed(self, piece: str):
        self.pending += piece
        while self.pending:
            tag = "</think>" if self.thinking else "<think>"
            index = self.pending.find(tag)
            if index >= 0:
                if index:
                    yield self.thinking, self.pending[:index]
                self.pending = self.pending[index + len(tag):]
                self.thinking = not self.thinking
                continue
            keep = 0
            for size in range(1, min(len(tag), len(self.pending) + 1)):
                if self.pending.endswith(tag[:size]):
                    keep = size
            ready = self.pending[:-keep] if keep else self.pending
            self.pending = self.pending[-keep:] if keep else ""
            if ready:
                yield self.thinking, ready
            break

    def flush(self):
        if self.pending:
            yield self.thinking, self.pending
            self.pending = ""


def measured_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def generation_metrics(timings, usage):
    """Non interpreta i frammenti SSE come token; usa solo contatori del server."""
    tokens = measured_number(timings.get("predicted_n"))
    if tokens is None:
        tokens = measured_number(usage.get("completion_tokens"))
    if tokens is not None and tokens != int(tokens):
        tokens = None
    milliseconds = measured_number(timings.get("predicted_ms"))
    seconds = milliseconds / 1000 if milliseconds is not None else None
    speed = measured_number(timings.get("predicted_per_second"))
    if speed is None and tokens is not None and seconds:
        speed = tokens / seconds
    return int(tokens) if tokens is not None else None, speed, seconds


def speed_label(speed):
    return f"{speed:.1f} t/s" if speed is not None else "N/D"


def ram_label():
    ram = get_system_ram()
    return f"{ram[1]:.1f}/{ram[0]:.1f} GiB ({ram[2]}%)" if ram else "N/D"


# ---------------------------------------------------------------------------
# Autocompletamento OpenClaw con prompt_toolkit
# ---------------------------------------------------------------------------
# Sinonimi accettati ma non mostrati nei completamenti: la superficie visibile
# resta il comando canonico di SLASH_COMMANDS.
COMMAND_ALIASES = {
    "/q": "/exit",
    "/quit": "/exit",
    "/h": "/help",
    "/?": "/help",
    "/m": "/model",
    "/max_tokens": "/maxtokens",
    "/max-tokens": "/maxtokens",
}

SLASH_COMMANDS = {
    "/help": "Mostra tutti i comandi disponibili e i tasti di scelta rapida",
    "/model": "Cambia modello attivo o elenca i modelli disponibili sul router",
    "/models": "Elenca tutti i modelli registrati con dettagli hardware",
    "/status": "Visualizza stato server, RAM e configurazione dichiarata",
    "/stack": "Verifica, installa e aggiorna lo stack tramite GenieX",
    "/bench": "Esegue un benchmark prestazionale in tempo reale (t/s e latenza)",
    "/clear": "Pulisce la schermata e avvia una nuova conversazione (anche: clear)",
    "/new": "Sinonimo di /clear (avvia una nuova sessione)",
    "/history": "Mostra o cancella la history dei comandi (/history clear)",
    "/clear-history": "Cancella la history dei comandi senza toccare la conversazione",
    "/system": "Visualizza o imposta il system prompt attivo",
    "/temp": "Imposta la temperatura di campionamento (es. /temp 0.2)",
    "/maxtokens": "Imposta il limite massimo di token generati (es. /maxtokens 4096)",
    "/reasoning": "Imposta ragionamento off oppure auto (scelta del server)",
    "/tokens": "Analisi utilizzo token e percentuale finestra di contesto",
    "/server": "Stato e controllo del server locale; stop arresta solo la directory gestita",
    "/exit": "Esci da Hexagon Claw TUI",
}


class OpenClawCompleter(Completer):
    def __init__(self, get_models_cb):
        self.get_models_cb = get_models_cb

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.lstrip()

        # Completamento per comandi slash
        if stripped.startswith("/"):
            parts = stripped.split(" ", 1)
            cmd = parts[0]

            if len(parts) == 1:
                for c, desc in SLASH_COMMANDS.items():
                    if c.startswith(cmd.lower()):
                        yield Completion(
                            c,
                            start_position=-len(cmd),
                            display=f"{c:<10}  {desc}",
                        )
            elif cmd.lower() in ("/model", "/m"):
                # Suggerisci modelli disponibili
                arg = parts[1] if len(parts) > 1 else ""
                models = self.get_models_cb()
                for m in models:
                    if m.lower().startswith(arg.lower()):
                        yield Completion(
                            m,
                            start_position=-len(arg),
                            display=m,
                        )
            elif cmd.lower() == "/history":
                arg = parts[1] if len(parts) > 1 else ""
                if "clear".startswith(arg.lower()):
                    yield Completion("clear", start_position=-len(arg), display="clear")
            elif cmd.lower() == "/server":
                arg = parts[1] if len(parts) > 1 else ""
                for sub in ("status", "start", "stop", "restart"):
                    if sub.startswith(arg.lower()):
                        yield Completion(
                            sub,
                            start_position=-len(arg),
                            display=sub,
                        )
            elif cmd.lower() == "/stack":
                arg = parts[1] if len(parts) > 1 else ""
                for sub in ("status", "check", "sync", "update-geniex", "update-llama"):
                    if sub.startswith(arg.lower()):
                        yield Completion(
                            sub,
                            start_position=-len(arg),
                            display=sub,
                        )


# ---------------------------------------------------------------------------
# Classe Principale: HexagonClawTUI
# ---------------------------------------------------------------------------
class HexagonClawTUI:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:18803/v1",
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        workspace_dir: Optional[Path] = None,
        max_tokens: int = 2048,
        reasoning: str = "off",
    ):
        self.base_url = base_url.rstrip("/")
        url = urlsplit(self.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise ValueError("URL HTTP(S) non valido")
        _ = url.port  # Rifiuta porte non valide prima del loop interattivo.
        if max_tokens <= 0:
            raise ValueError("max_tokens deve essere positivo")
        self.max_tokens = max_tokens
        if reasoning not in ("off", "auto"):
            raise ValueError("reasoning deve essere off oppure auto")
        self.reasoning = reasoning
        self.workspace_dir = Path(workspace_dir or DEFAULT_WORKSPACE).resolve()
        self.ini_path = self.workspace_dir / "hexagon-models.ini"

        self.console = Console(legacy_windows=False)
        self.presets = {}
        self.stack_mgr = StackManager(workspace_dir=self.workspace_dir)
        self.available_models: List[str] = []
        self.active_model = model or "LFM2.5-24B-A2B-QAD-Q4_0"

        self.system_prompt = system_prompt if system_prompt is not None else (
            "Sei un assistente AI specializzato in ingegneria del software e calcolo accelerato, "
            "per un ambiente target Qualcomm Snapdragon X2 Elite Extreme. "
            "Rispondi in italiano in modo conciso, tecnico e preciso."
        )
        self.temperature = 0.7
        self.messages: List[Dict[str, str]] = []

        # Metriche dell'ultima generazione
        self.last_gen_speed: Optional[float] = None
        self.last_gen_tokens: Optional[int] = None
        self.last_gen_time: Optional[float] = None
        self.total_tokens_session: int = 0
        self.http = httpx.Client(timeout=httpx.Timeout(180.0, connect=2.0),
                                 trust_env=url.hostname not in ("127.0.0.1", "localhost", "::1"))

        # Inizializza sessione prompt_toolkit con fallback per ambienti non-interattivi
        self.history_file = Path.home() / ".hexagon_claw_history"
        pt_style = PtStyle.from_dict({
            "prompt": "ansicyan bold",
            "model": "ansigreen",
            "arrow": "ansiyellow bold",
            "bottom-toolbar": "bg:#222222 #dddddd",
        })
        try:
            self.prompt_session = PromptSession(
                history=FileHistory(str(self.history_file)),
                auto_suggest=AutoSuggestFromHistory(),
                completer=OpenClawCompleter(self.get_available_model_ids),
                bottom_toolbar=self.status_bar,
                style=pt_style,
            )
        except Exception:
            self.prompt_session = None

    def close(self):
        self.http.close()

    def status_bar(self):
        used = self.estimate_context_tokens()
        limit = int(self.get_model_info(self.active_model)["ctx"])
        context = f"~{used:,}/{limit:,} token" if limit else f"~{used:,} token"
        return HTML(
            f'<ansigreen><b>{escape_html(self.active_model)}</b></ansigreen> '
            f'<ansibrightblack>│ contesto {context} │ reasoning {self.reasoning} '
            f'│ {speed_label(self.last_gen_speed)} │ ↑↓ history │ /help</ansibrightblack>'
        )

    def get_available_model_ids(self) -> List[str]:
        if self.available_models:
            return self.available_models
        if self.presets:
            return list(self.presets.keys())
        return []

    # -----------------------------------------------------------------------
    # Connessione al Server & Verifica Health
    # -----------------------------------------------------------------------
    def check_server_health(self) -> Tuple[bool, float, List[str]]:
        t0 = time.perf_counter()
        try:
            response = self.http.get(f"{self.base_url}/models", timeout=2.0)
            response.raise_for_status()
            models = [m["id"] for m in response.json().get("data", [])
                      if isinstance(m.get("id"), str) and m["id"]]
            return True, (time.perf_counter() - t0) * 1000, models
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            return False, 0.0, []

    def refresh_models(self):
        # Il launcher Router genera hexagon-models.ini nel checkout; con --workspace diverso
        # si ricade su quel file per non perdere i preset generati.
        candidates = list(dict.fromkeys(
            (self.ini_path, Path(DEFAULT_WORKSPACE) / "hexagon-models.ini")))
        self.presets = {}
        for candidate in candidates:
            try:
                presets = load_ini_presets(candidate)
            except (OSError, UnicodeError) as exc:
                self.console.print(Text(f"Preset non leggibili ({candidate}): {exc}", style="yellow"))
                continue
            if presets:
                self.presets = presets
                break
        health = self.check_server_health()
        online, _, models = health
        self.available_models = models if online else []
        if models and self.active_model not in models:
            self.active_model = next((m for m in models if "LFM" in m), models[0])
        return health

    def get_model_info(self, model_id: str) -> Dict[str, str]:
        """Riporta il preset dichiarato; non misura l'offload effettivo."""
        preset = self.presets.get(model_id, {})
        try:
            context = max(0, int(preset.get("ctx-size", "0")))
        except ValueError:
            context = 0
        device = preset.get("device", "N/D")
        split = preset.get("tensor-split", "N/D")
        return {"device": device, "split": split, "ctx": str(context),
                "strategy": "Preset locale" if preset else "Preset non disponibile"}

    # -----------------------------------------------------------------------
    # Rendering Header OpenClaw
    # -----------------------------------------------------------------------

    def print_banner(self, health=None):
        online, latency, _ = health if health is not None else self.check_server_health()
        info = self.get_model_info(self.active_model)
        limit = int(info["ctx"])
        used = self.estimate_context_tokens()
        context = f"~{used:,} / {limit:,} token" if limit else f"~{used:,} token / limite N/D"
        header = Text("🐉 HEXAGON CLAW TUI", style="bold cyan")
        header.append(" │ Target: Snapdragon X2 Elite Extreme\n")
        header.append(f"Server: {'ONLINE' if online else 'OFFLINE'}", style="green" if online else "red")
        if online:
            header.append(f" ({latency:.1f} ms)")
        header.append(f" │ Modello: {self.active_model}\n")
        header.append(f"Preset dichiarato: {info['device']} │ tensor-split: {info['split']}\n")
        header.append(f"Contesto stimato: {context} │ RAM: {ram_label()} │ Speed: {speed_label(self.last_gen_speed)}")
        self.console.print(Panel(header, border_style="cyan", padding=(0, 1),
                                 subtitle="/help comandi │ Ctrl+C interrompe │ /exit esce"))

    def estimate_context_tokens(self) -> int:
        """Stima approssimativa dei token correnti nella cronologia"""
        total_chars = sum(len(m["content"]) for m in self.messages)
        total_chars += len(self.system_prompt)
        return int(total_chars / 3.6)

    # -----------------------------------------------------------------------
    # Streaming Inferenziale con Parsing Ragionamento OpenClaw
    # -----------------------------------------------------------------------
    def _chat_events(self, body):
        body = {**body, "stream": True, "stream_options": {"include_usage": True}}
        if self.reasoning == "off":
            # llama.cpp: disable thinking per request, rather than only hiding it.
            body["reasoning_effort"] = "none"
        with self.http.stream("POST", f"{self.base_url}/chat/completions", json=body) as response:
            if response.status_code != 200:
                detail = response.read().decode("utf-8", errors="replace")
                raise ValueError(f"HTTP {response.status_code}: {detail}")
            finished = False
            for event in iter_sse_events(response.iter_lines()):
                if event is None:
                    finished = True
                    continue
                if event.get("error"):
                    raise ValueError(str(event["error"]))
                choices = event.get("choices") or []
                if choices and choices[0].get("finish_reason") is not None:
                    finished = True
                yield event
            if not finished:
                raise ValueError("Stream terminato senza conferma di completamento")

    def stream_chat(self, user_prompt: str):
        user_message = {"role": "user", "content": user_prompt}
        body = {
            "model": self.active_model,
            "messages": [{"role": "system", "content": self.system_prompt}, *self.messages, user_message],
            "temperature": self.temperature, "max_tokens": self.max_tokens,
        }
        self.console.print()
        self.console.print(Rule(Text(f"🐉 {self.active_model}", style="bold cyan"), style="dim"))
        answer_parts = []
        reasoning = ReasoningFilter()
        timings, usage = {}, {}
        finish_reason = None
        display_thinking = False
        reasoning_seen = False
        t0 = time.perf_counter()
        status = self.console.status(
            Text(f"{self.active_model} sta elaborando… │ Ctrl+C interrompe", style="cyan"),
            spinner="dots",
        )
        status.start()
        waiting = True

        def display(thinking, piece):
            nonlocal display_thinking, reasoning_seen, waiting
            if waiting:
                status.stop()
                waiting = False
            if thinking and piece.strip():
                reasoning_seen = True
            if thinking != display_thinking:
                self.console.print("\n🧠 Ragionamento:" if thinking else "\nRisposta:", style="dim")
                display_thinking = thinking
            self.console.print(Text(piece, style="dim italic" if thinking else ""), end="", soft_wrap=True)
            if not thinking:
                answer_parts.append(piece)

        try:
            for event in self._chat_events(body):
                timings.update(event.get("timings") or {})
                usage.update(event.get("usage") or {})
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                finish_reason = choices[0].get("finish_reason") or finish_reason
                # Entrambi i campi possono essere presenti nello stesso evento.
                if delta.get("reasoning_content"):
                    display(True, delta["reasoning_content"])
                for thinking, piece in reasoning.feed(delta.get("content") or ""):
                    display(thinking, piece)
            for thinking, piece in reasoning.flush():
                display(thinking, piece)
        except KeyboardInterrupt:
            self.console.print("\n[yellow]Generazione interrotta; turno non salvato.[/yellow]")
            return
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.console.print(Text(f"\nErrore durante lo streaming: {exc}", style="red"))
            return
        finally:
            if waiting:
                status.stop()

        elapsed = time.perf_counter() - t0
        tokens, speed, seconds = generation_metrics(timings, usage)
        self.last_gen_speed = speed
        self.last_gen_tokens = tokens
        self.last_gen_time = seconds
        self.total_tokens_session += tokens or 0
        answer = "".join(answer_parts)
        if answer.strip():
            self.messages.extend([user_message, {"role": "assistant", "content": answer}])
        elif reasoning_seen:
            self.console.print("\n[yellow]Ricevuto solo ragionamento; nessuna risposta finale. Turno non salvato.[/yellow]")
        else:
            self.console.print("\n[yellow]Nessuna risposta finale ricevuta; turno non salvato.[/yellow]")
        if finish_reason == "length":
            self.console.print("\n[yellow]Raggiunto il limite di generazione.[/yellow]")
        self.console.print(Text(f"\n⚡ {speed_label(speed)} │ token: {tokens if tokens is not None else 'N/D'} "
                                f"│ tempo totale: {elapsed:.2f}s\n", style="dim"))

    # -----------------------------------------------------------------------
    # Gestione Comandi Slash
    # -----------------------------------------------------------------------

    def handle_slash_command(self, cmd_line: str) -> bool:
        """Restituisce False se il comando richiede l'uscita dalla TUI, True altrimenti"""
        parts = cmd_line.strip().split(" ", 1)
        cmd = COMMAND_ALIASES.get(parts[0].lower(), parts[0].lower())
        args = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/exit":
            self.console.print("[yellow]Chiusura Hexagon Claw TUI. Arrivederci! 🐉[/yellow]")
            return False

        elif cmd == "/help":
            self.show_help()

        elif cmd == "/models":
            self.list_models()

        elif cmd == "/model":
            if args:
                self.switch_model(args)
            else:
                self.list_models()

        elif cmd == "/status":
            self.show_status_dashboard()

        elif cmd in ("/clear", "/new"):
            self.messages.clear()
            self.console.clear()
            self.console.print(Text("✓ Nuova conversazione: schermata e contesto azzerati.", style="green"))
            self.console.print(Text(f"Modello attivo: {self.active_model} │ /help per i comandi", style="dim"))

        elif cmd in ("/history", "/clear-history"):
            self.handle_history_command("clear" if cmd == "/clear-history" else args)

        elif cmd == "/system":
            if args:
                self.system_prompt = args
                self.console.print("[green]✓ System prompt aggiornato:[/green]")
                self.console.print(Panel(Text(self.system_prompt), border_style="green"))
            else:
                self.console.print(Panel(Text(self.system_prompt), title="System Prompt Attuale", border_style="cyan"))

        elif cmd == "/temp":
            if args:
                try:
                    val = float(args)
                    if 0.0 <= val <= 2.0:
                        self.temperature = val
                        self.console.print(f"[green]✓ Temperatura impostata a {self.temperature:.2f}[/green]")
                    else:
                        self.console.print("[red]Errore: la temperatura deve essere tra 0.0 e 2.0[/red]")
                except ValueError:
                    self.console.print("[red]Valore numerico non valido per la temperatura.[/red]")
            else:
                self.console.print(f"Temperatura attuale: [cyan]{self.temperature:.2f}[/cyan]")

        elif cmd == "/maxtokens":
            if args:
                try:
                    val = int(args)
                    if val > 0:
                        self.max_tokens = val
                        self.console.print(f"[green]✓ Limite di generazione impostato a {self.max_tokens} token[/green]")
                    else:
                        self.console.print("[red]Errore: max_tokens deve essere positivo (> 0)[/red]")
                except ValueError:
                    self.console.print("[red]Valore numerico non valido per max_tokens (es. /maxtokens 4096)[/red]")
            else:
                self.console.print(f"Limite di generazione attuale: [cyan]{self.max_tokens}[/cyan] token")

        elif cmd == "/tokens":
            self.show_tokens_info()

        elif cmd == "/reasoning":
            setting = args.lower()
            if setting and setting not in ("off", "auto"):
                self.console.print("[red]Uso: /reasoning off|auto[/red]")
            else:
                if setting:
                    self.reasoning = setting
                detail = "disattivato nella richiesta" if self.reasoning == "off" else "scelta del server"
                self.console.print(Text(f"Ragionamento: {self.reasoning} ({detail})."))

        elif cmd == "/bench":
            self.run_benchmark(args)

        elif cmd == "/server":
            self.handle_server_command(args)

        elif cmd == "/stack":
            self.handle_stack_command(args)

        else:
            self.console.print(Text(f"Comando sconosciuto: {cmd}. Digita /help per la lista comandi.", style="red"))
            suggestions = difflib.get_close_matches(cmd, SLASH_COMMANDS, n=3, cutoff=0.55)
            if suggestions:
                self.console.print(Text(f"Forse intendi: {', '.join(suggestions)}", style="cyan"))

        return True

    def handle_history_command(self, action: str):
        action = action.strip().lower()
        if not action:
            self.console.print(Text(f"History dei comandi: {self.history_file}"))
            self.console.print("[dim]Usa ↑↓ per navigarla; /history clear per cancellarla.[/dim]")
            return
        if action != "clear":
            self.console.print("Uso: /history [clear]")
            return
        try:
            self.history_file.unlink(missing_ok=True)
        except OSError as exc:
            self.console.print(Text(f"Impossibile cancellare la history dei comandi: {exc}", style="red"))
            return
        if self.prompt_session is not None:
            self.prompt_session.history = FileHistory(str(self.history_file))
        self.console.print("[green]✓ History dei comandi cancellata; conversazione invariata.[/green]")

    def show_help(self):
        t = Table(title="🐉 Comandi Slash - Hexagon Claw TUI", border_style="cyan", header_style="bold magenta")
        t.add_column("Comando", style="bold cyan", width=16)
        t.add_column("Argomenti", style="yellow", width=18)
        t.add_column("Descrizione", style="white")

        t.add_row("/help", "", "Mostra questa guida ai comandi (alias: /h, /?)")
        t.add_row("/models", "", "Elenca tutti i modelli GGUF disponibili sul router con dettagli hardware")
        t.add_row("/model", "<nome>", "Seleziona e cambia istantaneamente il modello attivo (alias: /m)")
        t.add_row("/status", "", "Stato server, RAM misurata e preset dichiarato")
        t.add_row("/stack", "status|check|sync|update-*", "Verifica, installa e aggiorna lo stack da GenieX")
        t.add_row("/bench", Text("[n_tokens]"), "Esegue un benchmark sintetico e misura prefill/generation t/s")
        t.add_row("/clear, /new", "", "Pulisce la schermata e avvia una nuova conversazione; vale anche clear")
        t.add_row("/history", "clear", "Mostra o cancella la history comandi; alias: /clear-history")
        t.add_row("/system", Text("[nuovo prompt]"), "Visualizza o ridefinisce il system prompt dell'assistente")
        t.add_row("/temp", "<0.0 - 2.0>", "Modifica la temperatura di campionamento (default: 0.7)")
        t.add_row("/maxtokens", "<numero>", "Modifica il limite di generazione per risposta (default: 2048; alias: /max_tokens, /max-tokens)")
        t.add_row("/reasoning", "off|auto", "Disattiva il ragionamento o usa la scelta del server (default: off)")
        t.add_row("/tokens", "", "Stima il consumo di token corrente e l'impegno della KV cache")
        t.add_row("/server", "status|start|stop|restart", "Controlla il router; stop arresta solo i llama-server della directory gestita")
        t.add_row("/exit, /quit, /q", "", "Termina la TUI ed esce al terminale")

        self.console.print(t)
        self.console.print("[dim]/ apre i completamenti │ ↑↓ history │ Ctrl+C interrompe │ Ctrl+D esce[/dim]")

    def list_models(self):
        self.refresh_models()
        t = Table(title="Modelli Disponibili nel Router Hexagon", border_style="cyan", header_style="bold magenta")
        t.add_column("ID Modello", style="bold green")
        t.add_column("Device", style="cyan")
        t.add_column("Tensor-split dichiarato", style="yellow")
        t.add_column("Ctx dichiarato", style="magenta")
        t.add_column("Fonte", style="white")

        for m in self.get_available_model_ids():
            info = self.get_model_info(m)
            t.add_row(
                Text(m + (" ★ ATTIVO" if m == self.active_model else "")),
                Text(info["device"]),
                Text(info["split"]),
                info['ctx'] if int(info['ctx']) else "N/D",
                info["strategy"],
            )

        self.console.print(t)
        self.console.print("[dim]Usa [cyan]/model <nome>[/cyan] per selezionarne uno.[/dim]\n")

    def switch_model(self, model_query: str):
        self.refresh_models()
        models = self.get_available_model_ids()
        exact = [m for m in models if model_query.casefold() == m.casefold()]
        candidates = exact or [m for m in models if model_query.casefold() in m.casefold()]

        if not candidates:
            self.console.print(Text(f"Nessun modello trovato per '{model_query}'. Usa /models per l'elenco.", style="red"))
            return

        if len(candidates) > 1:
            self.console.print("[yellow]Modello ambiguo. Candidati trovati:[/yellow]")
            for c in candidates:
                self.console.print(Text(f"  • {c}"))
            return

        chosen = candidates[0]
        self.active_model = chosen
        info = self.get_model_info(chosen)
        self.console.print(Text(f"✓ Modello attivo: {chosen}", style="bold green"))
        self.console.print(Text(f"  Preset: {info['device']}; tensor-split: {info['split']}; "
                                f"contesto: {info['ctx'] if int(info['ctx']) else 'N/D'}"))

    def show_status_dashboard(self):
        online, latency, _ = self.check_server_health()
        info = self.get_model_info(self.active_model)
        table = Table(title="🐉 Stato TUI / server", border_style="cyan")
        table.add_column("Parametro", style="bold cyan")
        table.add_column("Valore")
        host = f"{platform.system()} {platform.machine()}".strip() or "N/D"
        table.add_row("Piattaforma host", host)
        table.add_row("Piattaforma target", "Snapdragon X2 Elite Extreme (X2E94100)")
        table.add_row("CPU logiche rilevate", str(os.cpu_count() or "N/D"))
        table.add_row("RAM misurata", ram_label())
        table.add_row("Server URL", Text(self.base_url))
        table.add_row("Connessione", "ONLINE" if online else "OFFLINE")
        table.add_row("Latenza HTTP router", f"{latency:.2f} ms" if online else "N/D")
        table.add_row("Modello", Text(self.active_model))
        table.add_row("Device nel preset", Text(info["device"]))
        table.add_row("Tensor-split nel preset", Text(info["split"]))
        table.add_row("Contesto nel preset", info["ctx"] if int(info["ctx"]) else "N/D")
        table.add_row("Token generati misurati nella sessione", str(self.total_tokens_session))
        table.add_row("Velocità ultimo run (server)", speed_label(self.last_gen_speed))
        self.console.print(table)

    def show_tokens_info(self):
        used = self.estimate_context_tokens()
        limit = int(self.get_model_info(self.active_model)["ctx"])
        table = Table(title="Contesto stimato", border_style="cyan")
        table.add_column("Metrica", style="bold cyan")
        table.add_column("Valore")
        table.add_row("Messaggi nella cronologia", str(len(self.messages)))
        table.add_row("Token attivi stimati (caratteri / 3.6)", f"~{used:,}")
        table.add_row("Limite dichiarato nel preset", f"{limit:,}" if limit else "N/D")
        table.add_row("Riempimento stimato", f"{100 * used / limit:.2f}%" if limit else "N/D")
        self.console.print(table)

    def run_benchmark(self, args_str: str):
        try:
            max_tokens = int(args_str) if args_str else 100
            if max_tokens <= 0:
                raise ValueError
        except ValueError:
            self.console.print("[red]Uso: /bench <numero intero positivo di token>[/red]")
            return
        body = {
            "model": self.active_model, "max_tokens": max_tokens, "temperature": 0.0,
            "messages": [
                {"role": "system", "content": "Sei un assistente per benchmark prestazionali."},
                {"role": "user", "content": "Spiega brevemente il meccanismo di Attention nei Transformer."},
            ],
        }
        self.console.print(Text(f"Benchmark su {self.active_model}: massimo {max_tokens} token"))
        t0 = time.perf_counter()
        first_piece = None
        timings, usage = {}, {}
        status = self.console.status(Text("Benchmark in corso… │ Ctrl+C interrompe", style="cyan"), spinner="dots")
        status.start()
        waiting = True
        try:
            for event in self._chat_events(body):
                timings.update(event.get("timings") or {})
                usage.update(event.get("usage") or {})
                choices = event.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content") or delta.get("reasoning_content"):
                        if first_piece is None:
                            first_piece = time.perf_counter()
                            status.stop()
                            waiting = False
            elapsed = time.perf_counter() - t0
        except KeyboardInterrupt:
            self.console.print("[yellow]Benchmark interrotto.[/yellow]")
            return
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.console.print(Text(f"Benchmark fallito: {exc}", style="red"))
            return
        finally:
            if waiting:
                status.stop()
        tokens, speed, seconds = generation_metrics(timings, usage)
        ttft = (first_piece - t0) * 1000 if first_piece is not None else None
        table = Table(title=Text(f"🐉 Benchmark: {self.active_model}"), border_style="green")
        table.add_column("Parametro")
        table.add_column("Misura")
        table.add_row("TTFT osservato dal client", f"{ttft:.2f} ms" if ttft is not None else "N/D")
        prompt_ms = measured_number(timings.get("prompt_ms"))
        if prompt_ms is not None:
            table.add_row("Tempo prefill (server)", f"{prompt_ms:.2f} ms")
        prefill = measured_number(timings.get("prompt_per_second"))
        if prefill is not None:
            table.add_row("Throughput prefill (server)", f"{prefill:.2f} t/s")
        table.add_row("Token generati (server)", str(tokens) if tokens is not None else "N/D")
        table.add_row("Tempo generazione (server)", f"{seconds:.2f} s" if seconds is not None else "N/D")
        table.add_row("Throughput generazione (server)", speed_label(speed))
        table.add_row("Tempo totale client", f"{elapsed:.2f} s")
        table.add_row("Throughput end-to-end", speed_label(tokens / elapsed) if tokens is not None and elapsed > 0 else "N/D")
        self.console.print(table)

    def handle_server_command(self, action: str):
        action = action.strip().lower() if action else "status"
        if action == "status":
            online, latency, models = self.check_server_health()
            self.console.print(Text(f"{'ONLINE' if online else 'OFFLINE'}: {self.base_url}; "
                                    f"latenza {latency:.1f} ms; modelli {len(models)}"))
            return
        if action not in ("start", "stop", "restart"):
            self.console.print("Uso: /server [status | start | stop | restart]")
            return
        # Il controllo processi locale non può amministrare un endpoint remoto.
        url = urlsplit(self.base_url)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or sys.platform != "win32":
            self.console.print("[yellow]Controllo processi disponibile per endpoint Windows locali.[/yellow]")
            return
        if action in ("stop", "restart"):
            if not self.stack_mgr.stop_server():
                self.console.print("[red]Arresto dei processi llama-server non confermato.[/red]")
                return
            self.console.print("[green]Processi llama-server della directory gestita arrestati; "
                               "altri llama-server locali non sono stati toccati.[/green]")
            if action == "stop":
                return
        if self.check_server_health()[0]:
            self.console.print("[yellow]Endpoint già attivo.[/yellow]")
            return
        if not self.stack_mgr.start_server_router(port=url.port or (443 if url.scheme == "https" else 80)):
            self.console.print("[red]Impossibile avviare launch-hexagon.ps1 dal workspace.[/red]")
            return
        self.console.print("[cyan]Avvio Router; attesa endpoint fino a 10 secondi...[/cyan]")
        for _ in range(10):
            time.sleep(1)
            if self.check_server_health()[0]:
                self.refresh_models()
                self.console.print("[green]Server pronto.[/green]")
                return
        self.console.print("[yellow]Endpoint non ancora disponibile; verificare la console del server.[/yellow]")

    def handle_stack_command(self, args_str: str):
        sub = args_str.strip().lower() if args_str else "status"
        if sub in ("status", "verify", "v"):
            self.stack_mgr.render_stack_dashboard(self.console)
        elif sub in ("check", "updates", "up"):
            self.stack_mgr.render_updates_dashboard(self.console)
        elif sub in ("sync", "sincronizza", "update-llama", "llama", "update-llamacpp"):
            self.console.print("[cyan]Allineamento automatico al runtime GenieX...[/cyan]")
            ok, message, _ = self.stack_mgr.sync_runtime_from_geniex()
            self.console.print(Text(message, style="green" if ok else "red"))
        elif sub in ("update-geniex", "geniex"):
            self.console.print("[cyan]Aggiornamento GenieX e riallineamento dello stack...[/cyan]")
            ok, message = self.stack_mgr.update_geniex_cli()
            self.console.print(Text(message, style="green" if ok else "red"))
        else:
            self.console.print("Uso: /stack [status | check | sync | update-geniex | update-llama]")

    # -----------------------------------------------------------------------
    # Loop Principale Interattivo REPL
    # -----------------------------------------------------------------------

    def run(self):
        self.console.clear()
        health = self.refresh_models()
        # Il launcher locale include il bootstrap anche quando Python e' avviato direttamente.
        url = urlsplit(self.base_url)
        if not health[0] and sys.platform == "win32" and url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost"):
            self.handle_server_command("start")
            health = self.refresh_models()
        self.print_banner(health)

        online, _, _ = health
        if not online:
            self.console.print(
                Text(f"Server offline su {self.base_url}. /server start avvia il router locale.", style="yellow")
            )

        while True:
            try:
                # Prompt stilizzato OpenClaw: 🐉 hexagon [Model] ›
                if self.prompt_session is not None:
                    prompt_text = HTML(
                        f'<ansicyan><b>🐉 hexagon</b></ansicyan> '
                        f'<ansigreen>[{escape_html(self.active_model)}]</ansigreen> '
                        f'<ansiyellow><b>›</b></ansiyellow> '
                    )
                    user_input = self.prompt_session.prompt(prompt_text).strip()
                else:
                    self.console.print(Text(f"🐉 hexagon [{self.active_model}] › ", style="cyan"), end="")
                    user_input = input().strip()

                if not user_input:
                    continue

                command = "/clear" if user_input.casefold() == "clear" else user_input
                if command.startswith("/"):
                    should_continue = self.handle_slash_command(command)
                    if not should_continue:
                        break
                else:
                    self.stream_chat(command)

            except KeyboardInterrupt:
                # Ctrl+C nel prompt cancella la riga corrente
                self.console.print("\n[dim]Per uscire digita /exit o premi Ctrl+D[/dim]")
                continue
            except EOFError:
                # Ctrl+D esce
                self.console.print("\n[yellow]Uscita in corso... 🐉[/yellow]")
                break


# ---------------------------------------------------------------------------
# Entry Point CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Hexagon Claw TUI - Interfaccia Terminale OpenClaw per llama.cpp su Snapdragon X2 Elite",
    )
    parser.add_argument("--url", default="http://127.0.0.1:18803/v1", help="URL endpoint OpenAI compatibile")
    parser.add_argument("--model", default=None, help="Modello iniziale da selezionare")
    parser.add_argument("--system", default=None, help="System prompt personalizzato")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE, help="Directory launcher e preset INI")
    parser.add_argument("--max-tokens", type=int, default=2048, help="Limite di generazione per risposta (default: 2048)")
    parser.add_argument("--reasoning", choices=("off", "auto"), default="off",
                        help="Ragionamento: off nella richiesta, auto usa la scelta del server (default: off)")
    args = parser.parse_args()
    try:
        tui = HexagonClawTUI(base_url=args.url, model=args.model, system_prompt=args.system,
                             workspace_dir=args.workspace, max_tokens=args.max_tokens, reasoning=args.reasoning)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        tui.run()
    finally:
        tui.close()


if __name__ == "__main__":
    main()
