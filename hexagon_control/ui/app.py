import sys
import time

from rich.panel import Panel
from rich.text import Text

from .components import Interface
from .screens import Screens


class ControlCenter:
    def __init__(self, processes, catalog, presets, links, diagnostics, kilo, updates, settings):
        self.ui = Interface()
        self.processes = processes
        self.settings = settings
        self.screens = Screens(self.ui, processes, catalog, presets, links, diagnostics, kilo, updates, settings)
        self.actions = {
            "1": self.screens.router,
            "2": self.screens.direct,
            "3": self.screens.prerequisites,
            "4": self.screens.kilo,
            "5": self.screens.diagnostics,
            "6": self.screens.inventory,
            "7": self.screens.presets,
            "8": self.screens.paths_and_links,
            "9": self.screens.updates,
            "c": self.screens.chat,
            "l": self.live_dashboard,
        }

    def dashboard_renderable(self):
        online, latency, models = self.processes.health()
        owned = ", ".join(f"{record.mode}:{port}" for port, record in self.processes.owned.items()) or "nessuno"
        managed = self.processes.server_processes(managed_only=True)
        process_status = ", ".join(f"PID {p['pid']} ({p['memory_mb']} MB)" for p in managed) or "nessuno"
        content = Text("HEXAGON CONTROL CENTER\n", style="bold cyan")
        content.append("Windows 11 ARM64 | Snapdragon X2 Elite Extreme\n", style="dim")
        content.append(f"Endpoint: {self.processes.base_url}\n")
        content.append("Router: ", style="bold")
        content.append("ONLINE" if online else "OFFLINE", style="green" if online else "red")
        if online:
            content.append(f" | {latency:.1f} ms | {len(models)} modelli registrati")
        content.append("\nSlot caricati: N/D (non esposti in modo affidabile dall'endpoint)\n", style="dim")
        content.append(f"Processi avviati da questa sessione: {owned}\n")
        content.append(f"Server gestiti: {process_status}\n")
        content.append(
            "1 Router   2 Singolo modello   3 Verifica   4 Kilo Code   5 Diagnostica\n"
            "6 Modelli  7 Preset INI        8 Link       9 Aggiornamenti\n"
            "C Chat     L Live dashboard    ? Guida      Q Esci",
            style="bold",
        )
        return Panel(content, border_style="cyan", padding=(1, 2))

    def live_dashboard(self):
        self.ui.console.print("Aggiornamento ogni 2 secondi; premi un tasto per tornare al menu.")
        try:
            import msvcrt
            from rich.live import Live
            with Live(self.dashboard_renderable(), console=self.ui.console, refresh_per_second=4) as live:
                while True:
                    live.update(self.dashboard_renderable())
                    for _ in range(20):
                        if msvcrt.kbhit():
                            msvcrt.getwch()
                            return
                        time.sleep(0.1)
        except (ImportError, KeyboardInterrupt):
            self.ui.console.print(self.dashboard_renderable())

    def help(self):
        self.ui.table("Navigazione", ("Tasto", "Funzione"), [
            ("1…9", "Nove aree operative"),
            ("C", "Chat OpenAI-compatibile locale"),
            ("L", "Dashboard aggiornata ogni 2 secondi"),
            ("B", "Indietro nei sottomenu"),
            ("R", "Aggiorna la schermata corrente"),
            ("Q", "Uscita e scelta del ciclo di vita dei server"),
        ])

    def run(self):
        if self.settings.load_error:
            self.ui.console.print(f"Configurazione non leggibile; uso valori predefiniti: {self.settings.load_error}")
        while True:
            try:
                self.ui.console.print(self.dashboard_renderable())
                choice = self.ui.prompt().casefold()
                if choice in ("q", "quit", "exit"):
                    break
                if choice in ("?", "help"):
                    self.help()
                    continue
                action = self.actions.get(choice)
                if action is None:
                    if choice:
                        self.ui.console.print("Scelta non valida. ? mostra la guida.")
                    continue
                action()
            except KeyboardInterrupt:
                self.ui.console.print("\nOperazione corrente interrotta. Q per uscire.")
            except Exception as exc:
                self.ui.error(exc)
        self.exit_lifecycle()

    def exit_lifecycle(self):
        if not self.processes.owned:
            return
        self.ui.console.print("Server avviati da questa sessione: " + ", ".join(map(str, self.processes.owned)))
        keep = not self.ui.confirm("Arrestare questi server prima di uscire?")
        result = self.processes.shutdown(keep_running=keep)
        self.ui.result(result)
