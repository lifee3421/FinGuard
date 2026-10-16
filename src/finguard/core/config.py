from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class CoreConfig:
    model: str = "tool-calling-model-id"
    base_url: str = "https://api.example.com/v1"
    api_key: str | None = None
    max_steps: int = 12
    max_tool_calls: int = 40
    model_timeout_seconds: float = 60.0
    tool_timeout_seconds: float = 20.0
    initial_cash: str = "100000.00"
    run_lease_ttl_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "CoreConfig":
        cls._load_dotenv()
        defaults = cls()
        return cls(
            model=os.getenv("LLM_MODEL", defaults.model),
            base_url=os.getenv("LLM_BASE_URL", defaults.base_url),
            api_key=os.getenv("LLM_API_KEY"),
            max_steps=int(os.getenv("FIN_GUARD_MAX_STEPS", str(defaults.max_steps))),
            max_tool_calls=int(os.getenv("FIN_GUARD_MAX_TOOL_CALLS", str(defaults.max_tool_calls))),
            model_timeout_seconds=float(os.getenv("FIN_GUARD_MODEL_TIMEOUT", str(defaults.model_timeout_seconds))),
            tool_timeout_seconds=float(os.getenv("FIN_GUARD_TOOL_TIMEOUT", str(defaults.tool_timeout_seconds))),
            run_lease_ttl_seconds=float(os.getenv("FIN_GUARD_LEASE_TTL", str(defaults.run_lease_ttl_seconds))),
            initial_cash=os.getenv("FIN_GUARD_INITIAL_CASH", defaults.initial_cash),
        )

    @staticmethod
    def _load_dotenv() -> None:
        """Load a local .env file without overriding explicit environment values."""
        path = Path(os.getenv("FIN_GUARD_ENV_FILE", ".env"))
        if not path.is_file():
            return
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key or key in os.environ:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value
