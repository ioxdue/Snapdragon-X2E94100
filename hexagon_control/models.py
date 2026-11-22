import hashlib
import os
import struct
from pathlib import Path
from typing import Dict, List, Tuple

import httpx

from .results import OperationResult


class ModelCatalog:
    def __init__(self, backend, settings, base_url: str):
        self.backend = backend
        self.settings = settings
        self.base_url = base_url.rstrip("/")

    def inventory(self) -> Tuple[List[Dict], List[str]]:
        models = []
        errors = []
        seen = set()
        for directory in self.settings.models_dirs:
            try:
                result = self.backend.inventory(directory)
            except Exception as exc:
                errors.append(f"{directory}: {exc}")
                continue
            for item in result.get("models", []):
                path = str(item.get("path", ""))
                key = os.path.normcase(path)
                if not path or key in seen:
                    continue
                seen.add(key)
                item["tool_calling"] = self.settings.data["tool_calling"].get(path, {}).get("status", "non verificato")
                models.append(item)
        return sorted(models, key=lambda item: str(item.get("name", "")).casefold()), errors

    def verify_tool_call(self, model_id: str, model_path: Path) -> OperationResult:
        body = {
            "model": model_id,
            "temperature": 0,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Use the get_time tool to answer with the current time."}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Get the current local time",
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            }],
            "tool_choice": "auto",
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(180.0, connect=2.0), trust_env=False) as client:
                response = client.post(self.base_url + "/chat/completions", json=body)
                response.raise_for_status()
                payload = response.json()
            choices = payload.get("choices") or []
            message = choices[0].get("message", {}) if choices else {}
            supported = bool(message.get("tool_calls"))
            status = "verificato" if supported else "non rilevato"
            detail = "Tool call nativo restituito dal server" if supported else "Nessun tool_calls nella risposta; supporto non confermato"
            ok = supported
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
            status = "fallito"
            detail = str(exc)
            ok = False
        self.settings.data["tool_calling"][str(model_path)] = {"status": status, "model": model_id}
        self.settings.save()
        return OperationResult(ok, detail)

    @staticmethod
    def read_header(path: Path) -> Dict[str, int]:
        path = Path(path)
        with path.open("rb") as source:
            header = source.read(24)
        if len(header) < 24 or header[:4] != b"GGUF":
            raise ValueError("Header GGUF mancante o troncato")
        version, tensor_count, metadata_count = struct.unpack("<IQQ", header[4:24])
        if version not in (1, 2, 3):
            raise ValueError(f"Versione GGUF non supportata: {version}")
        return {"version": version, "tensor_count": tensor_count, "metadata_count": metadata_count}

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
