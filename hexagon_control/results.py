from dataclasses import dataclass
from typing import Optional


@dataclass
class OperationResult:
    ok: bool
    message: str
    pid: Optional[int] = None
    started: bool = False
