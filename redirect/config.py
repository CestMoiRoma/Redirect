"""Runtime configuration, read from environment variables only."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8080
DEFAULT_DATA_DIR = "/data"
DB_FILENAME = "redirect.db"
# The levels both logging and uvicorn know
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class Settings:
    port: int = DEFAULT_PORT
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    public_base_url: str | None = None
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_FILENAME

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        port_raw = env.get("PORT", "").strip() or str(DEFAULT_PORT)
        try:
            port = int(port_raw)
        except ValueError:
            raise ValueError(f"PORT must be an integer, got {port_raw!r}") from None
        if not 1 <= port <= 65535:
            raise ValueError(f"PORT must be between 1 and 65535, got {port}")
        level_raw = env.get("LOG_LEVEL", "").strip() or "INFO"
        if level_raw.upper() not in LOG_LEVELS:
            raise ValueError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {level_raw!r}")
        base = env.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or None
        return cls(
            port=port,
            data_dir=Path(env.get("DATA_DIR", "").strip() or DEFAULT_DATA_DIR),
            public_base_url=base,
            log_level=level_raw.upper(),
        )
