import os
from pathlib import Path

from .results import OperationResult


class LinkManager:
    def __init__(self, settings):
        self.settings = settings

    def create(self, source: Path, destination: Path, kind: str = "hardlink") -> OperationResult:
        source = Path(source).expanduser().resolve()
        destination = Path(destination).expanduser().absolute()
        if source.suffix.lower() != ".gguf" or destination.suffix.lower() != ".gguf":
            return OperationResult(False, "Origine e destinazione devono essere file .gguf")
        if not source.is_file():
            return OperationResult(False, f"Modello non trovato: {source}")
        if source == destination:
            return OperationResult(False, "Origine e destinazione coincidono")
        if destination.exists() or destination.is_symlink():
            try:
                if destination.samefile(source):
                    return OperationResult(True, "I percorsi indicano già lo stesso file")
            except OSError:
                pass
            return OperationResult(False, "Destinazione già presente; nessun file sovrascritto")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if kind == "hardlink":
                os.link(source, destination)
            elif kind == "symlink":
                os.symlink(source, destination, target_is_directory=False)
            else:
                return OperationResult(False, "Tipo link non valido")
        except OSError as exc:
            return OperationResult(False, f"Creazione {kind} fallita: {exc}")
        return OperationResult(True, f"Creato {kind}: {destination} -> {source}")

    def remove(self, link: Path) -> OperationResult:
        link = Path(link).expanduser().absolute()
        allowed = any(self._is_relative_to(link, root.absolute()) for root in self.settings.models_dirs)
        if not allowed:
            return OperationResult(False, "Il link deve appartenere a una directory modelli configurata")
        if not link.exists() and not link.is_symlink():
            return OperationResult(False, "Link non trovato")
        try:
            if not link.is_symlink() and link.stat().st_nlink <= 1:
                return OperationResult(False, "Il file non è un hard-link: rimozione rifiutata")
            link.unlink()
        except OSError as exc:
            return OperationResult(False, f"Rimozione link fallita: {exc}")
        return OperationResult(True, f"Link rimosso senza eliminare gli altri riferimenti: {link}")

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False
