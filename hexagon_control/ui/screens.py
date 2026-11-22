from pathlib import Path

from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

import kilo_config
from tui import HexagonClawTUI, ram_label


class Screens:
    def __init__(self, ui, processes, catalog, presets, links, diagnostics, kilo, updates, settings):
        self.ui = ui
        self.processes = processes
        self.catalog = catalog
        self.preset_manager = presets
        self.links = links
        self.diagnostics_service = diagnostics
        self.kilo_service = kilo
        self.update_service = updates
        self.settings = settings

    def _sync_endpoint(self):
        self.catalog.base_url = self.processes.base_url

    def _show_processes(self, managed_only=False):
        rows = [
            (p.get("pid"), f"{p.get('memory_mb', 0)} MB", p.get("path") or "N/D", p.get("command_line") or "")
            for p in self.processes.server_processes(managed_only=managed_only)
        ]
        self.ui.table("Processi llama-server" + (" gestiti" if managed_only else ""),
                      ("PID", "Memoria", "Eseguibile", "Comando"), rows)

    def _port(self) -> int:
        value = self.ui.prompt("Porta loopback", str(self.settings.data["port"]))
        try:
            port = int(value)
            if not 1 <= port <= 65535:
                raise ValueError
            return port
        except ValueError:
            self.ui.error("Porta non valida")
            return self.settings.data["port"]

    def _models(self):
        models, errors = self.catalog.inventory()
        for error in errors:
            self.ui.console.print(Text(error, style="yellow"))
        return models

    def _select_model(self, models):
        if not models:
            self.ui.error("Nessun modello GGUF disponibile")
            return None
        choice = self.ui.prompt("Numero o filtro modello")
        if choice.isdigit() and 1 <= int(choice) <= len(models):
            return models[int(choice) - 1]
        matches = [m for m in models if choice.casefold() in str(m.get("name", "")).casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            self.ui.error("Nessun modello corrispondente")
        else:
            self.ui.error("Selezione ambigua; usa il numero di riga")
        return None

    def _model_table(self, models):
        self.ui.table("Inventario GGUF", ("#", "Modello", "Arch.", "Quant.", "GiB", "Device", "Tool calling"), [
            (index, m.get("name"), m.get("architecture") or "N/D", m.get("quantization") or "N/D",
             m.get("size_gb", "N/D"), m.get("device") or "N/D", m.get("tool_calling", "non verificato"))
            for index, m in enumerate(models, 1)
        ])

    def router(self):
        while True:
            online, latency, models = self.processes.health()
            self.ui.table("Router multi-modello", ("Parametro", "Valore"), [
                ("Endpoint", self.processes.base_url),
                ("Stato", "ONLINE" if online else "OFFLINE"),
                ("Latenza", f"{latency:.1f} ms" if online else "N/D"),
                ("Modelli registrati", len(models) if online else "N/D"),
                ("Modelli caricati negli slot", "N/D"),
                ("Models-max", self.settings.data["models_max"]),
                ("Binding", "127.0.0.1 (predefinito)"),
                ("Log sessione", self.processes.log_dir),
            ])
            self._show_processes(managed_only=True)
            choice = self.ui.prompt("Router: A avvia, P anteprima, S stop, R riavvia, L log, X stop gestiti, B indietro").casefold()
            try:
                if choice == "b":
                    return
                if choice == "":
                    continue
                if choice == "a":
                    maximum = int(self.ui.prompt("Modelli simultanei", str(self.settings.data["models_max"])))
                    if maximum <= 0:
                        self.ui.error("models-max deve essere positivo")
                        continue
                    self.settings.data["models_max"] = maximum
                    self.settings.save()
                    result = self.processes.start("Router", port=self._port(), models_max=maximum)
                    self._sync_endpoint()
                    self.ui.result(result)
                elif choice == "p":
                    plan, diff = self.preset_manager.preview_router(
                        self.settings.models_dir, self.settings.data["port"], self.settings.data["models_max"])
                    self._model_table(plan.get("models", []))
                    self.ui.console.print(Panel(" ".join(map(str, plan.get("cli_args", []))), title="Comando previsto"))
                    self.ui.console.print(Syntax(diff or "Nessuna differenza", "diff", word_wrap=True))
                elif choice == "s":
                    if self.ui.confirm("Arrestare il server avviato da questa sessione?"):
                        self.ui.result(self.processes.stop(self.settings.data["port"]))
                elif choice == "r":
                    if self.ui.confirm("Riavviare il router posseduto dalla sessione?"):
                        stopped = self.processes.stop(self.settings.data["port"])
                        self.ui.result(stopped)
                        if stopped.ok:
                            self.ui.result(self.processes.start("Router"))
                elif choice == "l":
                    self.ui.console.print(Panel(self.processes.read_logs(self.settings.data["port"]), title="Log router"))
                elif choice == "x":
                    if self.ui.confirm("Arrestare TUTTI i llama-server della directory runtime gestita?"):
                        ok = self.processes.stack_manager.stop_server()
                        self.ui.console.print("Arresto confermato" if ok else "Arresto non confermato")
            except Exception as exc:
                self.ui.error(exc)

    def direct(self):
        models = self._models()
        self._model_table(models)
        selected = None
        while True:
            if selected:
                self.ui.console.print(Panel(
                    f"{selected.get('path')}\n{selected.get('strategy', '')}", title="Modello selezionato"))
            choice = self.ui.prompt("Direct: M scegli, P anteprima, A avvia, S stop, R ricarica, B indietro").casefold()
            try:
                if choice == "b":
                    return
                if choice == "m":
                    selected = self._select_model(models)
                elif choice == "r":
                    models = self._models()
                    self._model_table(models)
                elif choice in ("p", "a"):
                    if selected is None:
                        selected = self._select_model(models)
                        if selected is None:
                            continue
                    plan = self.catalog.backend.direct_plan(Path(selected["path"]), self.settings.data["port"])
                    if choice == "p":
                        config = plan["config"]
                        self.ui.table("Piano Direct", ("Parametro", "Valore"), [
                            ("Alias", plan["alias"]), ("Device", config["device"]),
                            ("Tensor split", config.get("tensor_split") or "100%"),
                            ("Contesto", config["context_size"]),
                            ("Batch / ubatch", f"{config['batch_size']} / {config['ubatch_size']}"),
                            ("Strategia", config["strategy"]),
                        ])
                        self.ui.console.print(Panel(" ".join(map(str, plan["cli_args"])), title="Comando previsto"))
                    elif self.ui.confirm(f"Avviare {plan['alias']} su {self.settings.data['port']}?"):
                        result = self.processes.start("Direct", Path(selected["path"]), self.settings.data["port"])
                        self._sync_endpoint()
                        self.ui.result(result)
                elif choice == "s":
                    if self.ui.confirm("Arrestare il server Direct avviato da questa sessione?"):
                        self.ui.result(self.processes.stop(self.settings.data["port"]))
            except Exception as exc:
                self.ui.error(exc)

    def prerequisites(self):
        self.ui.console.print("Controlli locali in sola lettura; nessun update o riparazione automatica.")
        try:
            checks = self.diagnostics_service.prerequisites()
            self.ui.table("Prerequisiti, integrità e sicurezza offline", ("Controllo", "Esito", "Dettaglio"), [
                (c["name"], c["status"], c["detail"]) for c in checks
            ])
        except Exception as exc:
            self.ui.error(exc)
        self.ui.pause()

    def kilo(self):
        online, _, models = self.processes.health()
        default_main = kilo_config.RECOMMENDED_MAIN_MODEL
        default_small = kilo_config.RECOMMENDED_SMALL_MODEL
        if models:
            if default_main not in models:
                default_main = models[0]
            if default_small not in models:
                default_small = models[0]
        self.ui.console.print(Panel(
            f"Router: {'ONLINE' if online else 'OFFLINE; verrà avviato con il solo controllo runtime'}\n"
            f"Ruoli: {', '.join(self.kilo_service.roles)}", title="Router + Kilo Code"))
        main = self.ui.prompt("Modello principale", default_main)
        small = self.ui.prompt("Modello ausiliario", default_small)
        role = self.ui.prompt("Ruolo", "interattivo").casefold()
        pure = self.ui.confirm("Avviare Kilo in modalità pure?")
        action = self.ui.prompt("P prepara configurazione, A avvia Kilo, B annulla", "a").casefold()
        if action == "b":
            return
        try:
            if action == "p":
                self.ui.result(self.kilo_service.prepare(main, small))
            elif action == "a":
                if self.ui.confirm("Rigenerare kilo.json e avviare Kilo Code?"):
                    self.ui.result(self.kilo_service.launch(main, small, role, pure))
        except Exception as exc:
            self.ui.error(exc)

    def diagnostics(self):
        while True:
            port = self.settings.data["port"]
            online, latency, models = self.processes.health()
            self.ui.table("Diagnostica e monitoraggio", ("Metrica", "Valore"), [
                ("Porta", f"{port} ({'aperta' if self.diagnostics_service.port_open(port) else 'chiusa'})"),
                ("HTTP /v1/models", f"{'OK' if online else 'non raggiungibile'}; {latency:.1f} ms" if online else "OFFLINE"),
                ("Modelli registrati", len(models) if online else "N/D"),
                ("RAM unificata", ram_label()),
                ("FastRPC / utilizzo NPU-GPU", self.diagnostics_service.accelerator_usage()),
            ])
            self._show_processes()
            choice = self.ui.prompt("Diagnostica: R refresh, P porta, B benchmark, L log, E errori, Q indietro").casefold()
            try:
                if choice == "q":
                    return
                if choice == "p":
                    self.processes.set_port(self._port())
                    self._sync_endpoint()
                elif choice == "b":
                    if not online:
                        self.ui.error("Server offline")
                        continue
                    selected = self._select_model([{"name": model} for model in models])
                    if selected is None:
                        continue
                    tokens = self.ui.prompt("Massimo token generati", "100")
                    chat = HexagonClawTUI(
                        base_url=self.processes.base_url, model=selected["name"],
                        workspace_dir=self.processes.workspace_dir,
                    )
                    chat.prompt_session = None
                    chat.console = self.ui.console
                    try:
                        chat.run_benchmark(tokens)
                    finally:
                        chat.close()
                elif choice in ("l", "e"):
                    text = self.diagnostics_service.recent_logs(errors_only=choice == "e")
                    self.ui.console.print(Panel(text, title="Log recenti"))
            except Exception as exc:
                self.ui.error(exc)

    def inventory(self):
        while True:
            models = self._models()
            self._model_table(models)
            choice = self.ui.prompt("Modelli: D dettagli, T test tool calling, I integrità/hash, R refresh, B indietro").casefold()
            try:
                if choice == "b":
                    return
                if choice == "r" or choice == "":
                    continue
                selected = self._select_model(models)
                if selected is None:
                    continue
                if choice == "d":
                    self.ui.table("Metadati modello", ("Campo", "Valore"),
                                  [(key, value) for key, value in selected.items()])
                elif choice == "t":
                    if self.ui.confirm("Il test carica il modello e invia una richiesta tool calling locale. Continuare?"):
                        self.ui.result(self.catalog.verify_tool_call(selected["alias"], Path(selected["path"])))
                elif choice == "i":
                    header = self.catalog.read_header(selected["path"])
                    self.ui.table("Header GGUF", ("Campo", "Valore"), list(header.items()))
                    if self.ui.confirm("Calcolare SHA-256 dell'intero file?"):
                        self.ui.console.print(self.catalog.sha256(selected["path"]))
            except Exception as exc:
                self.ui.error(exc)

    def presets(self):
        while True:
            choice = self.ui.prompt("INI: V visualizza, C controlla, E modifica, P anteprima, G rigenera, B indietro").casefold()
            try:
                if choice == "b":
                    return
                if choice == "v":
                    text = self.preset_manager.read()
                    self.ui.console.print(Syntax(text or "File non presente", "ini", word_wrap=True))
                elif choice == "c":
                    errors = self.preset_manager.validate(self.preset_manager.read())
                    self.ui.console.print("Sintassi valida" if not errors else "\n".join(errors))
                elif choice == "e":
                    sections = ["*", *self.preset_manager.parse(self.preset_manager.read())]
                    section = self.ui.prompt("Sezione: " + ", ".join(sections), "*")
                    key = self.ui.prompt("Parametro (ctx-size, tensor-split, batch-size, ubatch-size, flash-attn)")
                    if key not in ("ctx-size", "tensor-split", "batch-size", "ubatch-size", "flash-attn"):
                        self.ui.error("Parametro non consentito dall'editor guidato")
                        continue
                    value = self.ui.prompt("Nuovo valore")
                    proposed = self.preset_manager.updated_text(section, key, value)
                    self.ui.console.print(Syntax(self.preset_manager.diff(proposed), "diff", word_wrap=True))
                    if self.ui.confirm("Validare e salvare questa modifica con backup?"):
                        backup = self.preset_manager.save_text(proposed)
                        self.ui.console.print(f"Salvato; backup: {backup or 'nessun file precedente'}")
                elif choice in ("p", "g"):
                    plan, diff = self.preset_manager.preview_router(
                        self.settings.models_dir, self.settings.data["port"], self.settings.data["models_max"])
                    self.ui.console.print(Syntax(diff or "Nessuna differenza", "diff", word_wrap=True))
                    if choice == "g" and diff and self.ui.confirm("Salvare il preset rigenerato con backup?"):
                        backup = self.preset_manager.save_text(plan["ini_content"])
                        self.ui.console.print(f"INI aggiornato; backup: {backup or 'N/D'}")
            except Exception as exc:
                self.ui.error(exc)

    def paths_and_links(self):
        while True:
            self.ui.table("Directory modelli", ("Percorso", "Tipo"), [
                (self.settings.models_dir, "principale"),
                *((path, "personalizzata") for path in self.settings.data["custom_models_dirs"]),
            ])
            choice = self.ui.prompt("Percorsi/link: A aggiungi dir, R rimuovi dir, H hard-link, Y symlink, D rimuovi link, I integrità, B indietro").casefold()
            try:
                if choice == "b":
                    return
                if choice == "a":
                    path = Path(self.ui.prompt("Directory GGUF esistente")).expanduser()
                    if not path.is_dir():
                        self.ui.error("Directory non trovata")
                    else:
                        self.settings.add_models_dir(path)
                elif choice == "r":
                    path = Path(self.ui.prompt("Directory personalizzata da rimuovere dalla configurazione"))
                    self.ui.console.print("Rimossa" if self.settings.remove_models_dir(path) else "Non presente o non modificabile")
                elif choice in ("h", "y"):
                    source = Path(self.ui.prompt("GGUF di origine")).expanduser()
                    default = self.settings.models_dir / source.name
                    destination = Path(self.ui.prompt("Destinazione link", str(default))).expanduser()
                    kind = "hardlink" if choice == "h" else "symlink"
                    if self.ui.confirm(f"Creare {kind} senza sovrascrivere file esistenti?"):
                        self.ui.result(self.links.create(source, destination, kind))
                elif choice == "d":
                    link = Path(self.ui.prompt("Link da rimuovere"))
                    if self.ui.confirm("Rimuovere soltanto questo link?"):
                        self.ui.result(self.links.remove(link))
                elif choice == "i":
                    path = Path(self.ui.prompt("File GGUF da verificare"))
                    self.ui.console.print(str(self.catalog.read_header(path)))
                    if self.ui.confirm("Calcolare anche SHA-256?"):
                        self.ui.console.print(self.catalog.sha256(path))
            except Exception as exc:
                self.ui.error(exc)

    def updates(self):
        while True:
            self.update_service.stack_manager.render_stack_dashboard(self.ui.console)
            choice = self.ui.prompt("Update: R refresh locale, C check online, S sincronizza, G aggiorna GenieX, B indietro").casefold()
            try:
                if choice == "b":
                    return
                if choice in ("", "r"):
                    continue
                if choice == "c":
                    if self.ui.confirm("Consultare ora le release pubbliche su GitHub?"):
                        self.update_service.stack_manager.render_updates_dashboard(self.ui.console)
                elif choice == "s":
                    if self.ui.confirm("Allineare il runtime a GenieX con backup e rollback automatico?"):
                        ok, message, _ = self.update_service.synchronize()
                        self.ui.console.print(Panel(message, border_style="green" if ok else "red"))
                elif choice == "g":
                    if self.ui.confirm("Aggiornare GenieX e poi riallineare il runtime?"):
                        self.ui.console.print("[cyan]Aggiornamento GenieX in corso (download e riavvio del servizio; puo' richiedere alcuni minuti)...[/cyan]")
                        ok, message = self.update_service.update_geniex()
                        self.ui.console.print(Panel(message, border_style="green" if ok else "red"))
            except Exception as exc:
                self.ui.error(exc)

    def chat(self):
        online, _, models = self.processes.health()
        if not online:
            if not self.ui.confirm("Router offline. Avviarlo senza aggiornare il runtime?"):
                return
            result = self.processes.start("Router")
            self._sync_endpoint()
            self.ui.result(result)
            if not result.ok:
                return
            online, _, models = self.processes.health()
        model = models[0] if models else None
        if models:
            selected = self._select_model([{"name": item} for item in models])
            if selected:
                model = selected["name"]
        chat = HexagonClawTUI(
            base_url=self.processes.base_url, model=model,
            workspace_dir=self.processes.workspace_dir,
        )
        chat.console = self.ui.console
        try:
            chat.run()
        finally:
            chat.close()
