import difflib
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List


class PresetError(ValueError):
    pass


class PresetManager:
    def __init__(self, ini_path: Path, backend):
        self.ini_path = Path(ini_path)
        self.backend = backend

    def read(self) -> str:
        if not self.ini_path.is_file():
            return ""
        return self.ini_path.read_text(encoding="utf-8-sig")

    def parse(self, text: str) -> Dict[str, Dict[str, str]]:
        sections: Dict[str, Dict[str, str]] = {}
        current = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1].strip()
                sections.setdefault(current, {})
            elif current and "=" in line:
                key, value = line.split("=", 1)
                sections[current][key.strip()] = value.strip()
        defaults = sections.pop("*", {})
        return {name: {**defaults, **values} for name, values in sections.items()}

    def validate(self, text: str) -> List[str]:
        errors = []
        sections = {}
        current = None
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") or line.endswith("]"):
                if not (line.startswith("[") and line.endswith("]")) or not line[1:-1].strip():
                    errors.append(f"Riga {number}: sezione INI non valida")
                    continue
                current = line[1:-1].strip()
                if current in sections:
                    errors.append(f"Riga {number}: sezione duplicata [{current}]")
                sections[current] = {}
                continue
            if current is None:
                if number == 1 and re.fullmatch(r"version\s*=\s*1", line):
                    continue
                errors.append(f"Riga {number}: parametro fuori da una sezione")
                continue
            if "=" not in line:
                errors.append(f"Riga {number}: atteso chiave = valore")
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                errors.append(f"Riga {number}: nome parametro non valido")
            if not value:
                errors.append(f"Riga {number}: valore vuoto per {key}")
            if key in sections[current]:
                errors.append(f"Riga {number}: parametro {key} duplicato in [{current}]")
            sections[current][key] = value
            if key in ("ctx-size", "batch-size", "ubatch-size", "threads", "threads-batch"):
                try:
                    if int(value) <= 0:
                        raise ValueError
                except ValueError:
                    errors.append(f"Riga {number}: {key} deve essere un intero positivo")
            elif key == "flash-attn" and value.lower() not in ("on", "off", "auto"):
                errors.append(f"Riga {number}: flash-attn deve essere on, off o auto")
            elif key == "tensor-split":
                try:
                    parts = [float(part.strip()) for part in value.split(",")]
                    if not parts or any(not math.isfinite(part) or part <= 0 for part in parts):
                        raise ValueError
                    if abs(sum(parts) - 100.0) > 0.01:
                        errors.append(f"Riga {number}: tensor-split deve sommare a 100")
                    devices = sections[current].get("device", "")
                    if devices and len(devices.split(",")) != len(parts):
                        errors.append(f"Riga {number}: tensor-split e device hanno cardinalità diverse")
                except ValueError:
                    errors.append(f"Riga {number}: tensor-split deve contenere percentuali positive")
        resolved = self.parse(text)
        for name, values in resolved.items():
            try:
                batch = int(values.get("batch-size", "4096"))
                ubatch = int(values.get("ubatch-size", "512"))
                if ubatch > batch:
                    errors.append(f"[{name}]: ubatch-size non può superare batch-size")
            except ValueError:
                pass
        return errors

    def diff(self, proposed: str) -> str:
        current = self.read()
        return "\n".join(difflib.unified_diff(
            current.splitlines(), proposed.splitlines(),
            fromfile=str(self.ini_path), tofile="anteprima", lineterm="",
        ))

    def save_text(self, text: str):
        errors = self.validate(text)
        if errors:
            raise PresetError("\n".join(errors))
        self.ini_path.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if self.ini_path.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = self.ini_path.with_name(self.ini_path.name + ".bak-" + stamp)
            backup.write_bytes(self.ini_path.read_bytes())
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=self.ini_path.parent,
            prefix=self.ini_path.name + ".", suffix=".tmp", delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(text)
            os.replace(temporary, self.ini_path)
        finally:
            temporary.unlink(missing_ok=True)
        return backup

    def updated_text(self, section: str, key: str, value: str) -> str:
        lines = self.read().splitlines()
        start = None
        end = len(lines)
        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped == f"[{section}]":
                start = index
            elif start is not None and stripped.startswith("[") and stripped.endswith("]"):
                end = index
                break
        if start is None:
            raise PresetError(f"Sezione [{section}] non trovata")
        replaced = False
        for index in range(start + 1, end):
            raw = lines[index]
            if raw.split("=", 1)[0].strip().lower() == key.lower() and "=" in raw:
                lines[index] = f"{key} = {value}"
                replaced = True
                break
        if not replaced:
            lines.insert(end, f"{key} = {value}")
        return "\n".join(lines) + "\n"

    def update_parameter(self, section: str, key: str, value: str):
        return self.save_text(self.updated_text(section, key, value))

    def preview_router(self, models_dir: Path, port: int = 18803, models_max: int = 2):
        plan = self.backend.router_plan(models_dir, port, models_max)
        content = plan.get("ini_content", "")
        errors = self.validate(content)
        if errors:
            raise PresetError("Preset generato non valido:\n" + "\n".join(errors))
        return plan, self.diff(content)

    def regenerate_router(self, models_dir: Path, port: int = 18803, models_max: int = 2):
        plan = self.backend.router_plan(models_dir, port, models_max)
        backup = self.save_text(plan.get("ini_content", ""))
        return plan, backup
