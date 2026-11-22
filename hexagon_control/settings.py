import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List


USER_PROFILE = Path(os.environ.get("USERPROFILE") or Path.home())
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or USER_PROFILE / "AppData" / "Local")


class SettingsStore:
    def __init__(self, path: Path = None):
        self.path = Path(path or LOCAL_APPDATA / "HexagonClaw" / "control-center.json")
        self.load_error = ""
        self.data = self._load()

    def _defaults(self) -> Dict[str, Any]:
        return {
            "models_dir": str(USER_PROFILE / ".cache" / "geniex" / "models"),
            "custom_models_dirs": [],
            "port": 18803,
            "models_max": 2,
            "tool_calling": {},
        }

    def _load(self) -> Dict[str, Any]:
        defaults = self._defaults()
        if not self.path.is_file():
            return defaults
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("La configurazione non è un oggetto JSON")
        except (OSError, ValueError) as exc:
            self.load_error = str(exc)
            return defaults
        for key, value in defaults.items():
            if key not in loaded or not isinstance(loaded[key], type(value)):
                loaded[key] = value
        loaded["custom_models_dirs"] = [str(p) for p in loaded["custom_models_dirs"]]
        return loaded

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False,
            prefix=self.path.name + ".", suffix=".tmp",
        )
        temporary = Path(handle.name)
        try:
            with handle:
                json.dump(self.data, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def models_dir(self) -> Path:
        return Path(self.data["models_dir"])

    @property
    def models_dirs(self) -> List[Path]:
        paths = [self.models_dir, *(Path(p) for p in self.data["custom_models_dirs"])]
        unique = []
        seen = set()
        for path in paths:
            key = os.path.normcase(str(path.absolute()))
            if key not in seen:
                seen.add(key)
                unique.append(path)
        return unique

    def add_models_dir(self, path: Path) -> bool:
        resolved = str(Path(path).expanduser().resolve())
        if any(os.path.normcase(str(p.absolute())) == os.path.normcase(resolved)
               for p in self.models_dirs):
            return False
        self.data["custom_models_dirs"].append(resolved)
        self.save()
        return True

    def remove_models_dir(self, path: Path) -> bool:
        resolved = os.path.normcase(str(Path(path).absolute()))
        original = len(self.data["custom_models_dirs"])
        self.data["custom_models_dirs"] = [
            p for p in self.data["custom_models_dirs"]
            if os.path.normcase(str(Path(p).absolute())) != resolved
        ]
        changed = len(self.data["custom_models_dirs"]) != original
        if changed:
            self.save()
        return changed
