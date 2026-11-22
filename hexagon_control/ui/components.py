from typing import Iterable, Optional

from prompt_toolkit import PromptSession
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Interface:
    def __init__(self):
        self.console = Console(legacy_windows=False)
        try:
            self.session = PromptSession()
        except Exception:
            self.session = None

    def prompt(self, message: str = "hexagon ›", default: Optional[str] = None) -> str:
        suffix = f" [{default}]" if default is not None else ""
        try:
            if self.session is not None:
                value = self.session.prompt(f"{message}{suffix} ").strip()
            else:
                value = input(f"{message}{suffix} ").strip()
        except EOFError:
            return "q"
        return value if value or default is None else default

    def confirm(self, message: str) -> bool:
        return self.prompt(message + " (si/no)", "no").casefold() in ("si", "sì", "s", "yes", "y")

    def pause(self):
        if self.session is not None:
            self.session.prompt("Invio per continuare… ")
        else:
            input("Invio per continuare… ")

    def result(self, result):
        self.console.print(Panel(Text(result.message), border_style="green" if result.ok else "red"))

    def error(self, error):
        self.console.print(Panel(Text(str(error)), title="Operazione non riuscita", border_style="red"))

    def table(self, title: str, columns: Iterable[str], rows: Iterable[Iterable]):
        table = Table(title=title, border_style="cyan", header_style="bold magenta")
        for column in columns:
            table.add_column(column)
        for row in rows:
            table.add_row(*(str(value) for value in row))
        self.console.print(table)
        return table
