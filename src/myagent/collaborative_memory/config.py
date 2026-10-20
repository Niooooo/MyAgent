"""Explicit opt-in and resource limits for collaborative memory."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CollaborativeMemoryConfig:
    mode: str = "off"
    search_rounds: int = 3
    hits_per_round: int = 8
    read_chars: int = 16000
    scan_bytes: int = 2_000_000
    candidate_tasks: int = 4
    expansion_nodes: int = 8
    model_calls: int = 4
    injection_chars: int = 6000
    timeout_seconds: int = 15
    model_output_tokens: int = 1500
    event_chars: int = 65536
    learning_attempts: int = 3

    def __post_init__(self) -> None:
        if self.mode not in {"off", "record", "learn"}:
            raise ValueError("collaborative memory mode must be off, record or learn")
        for name, value in vars(self).items():
            if name != "mode" and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer")
        if self.injection_chars < 512:
            raise ValueError("injection_chars must be at least 512")
        maxima = {
            "search_rounds": 10, "hits_per_round": 100, "read_chars": 1_000_000,
            "scan_bytes": 64_000_000, "candidate_tasks": 20, "expansion_nodes": 100,
            "model_calls": 20, "injection_chars": 32768, "timeout_seconds": 120,
            "model_output_tokens": 4096, "event_chars": 262144, "learning_attempts": 5,
        }
        for name, maximum in maxima.items():
            if getattr(self, name) > maximum:
                raise ValueError(f"{name} must not exceed {maximum}")
