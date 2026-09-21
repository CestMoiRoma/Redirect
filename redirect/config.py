"""Runtime configuration, read from environment variables only."""

import os
from dataclasses import dataclass, field
from pathlib import Path

# The container is meant to have its own IP on the LAN: links then need no port
DEFAULT_PORT = 80
DEFAULT_DATA_DIR = "/data"
DB_FILENAME = "redirect.db"
# The levels both logging and uvicorn know
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ADMIN_PASSWORD_MIN_LENGTH = 8
DEFAULT_LOG_RETENTION_DAYS = 90
_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


@dataclass(frozen=True)
class Settings:
    port: int = DEFAULT_PORT
    data_dir: Path = Path(DEFAULT_DATA_DIR)
    public_base_url: str | None = None
    log_level: str = "INFO"
    # None: no login, the interface and the API are open (a warning says so). Never shown in a repr.
    admin_password: str | None = field(default=None, repr=False)
    # None: the interface switches the scripts' API on and off. True or False: forced by API_ENABLED.
    api_enabled: bool | None = None
    # None: the interface decides (local only, at first). True or False: forced by LOCAL_ONLY.
    local_only: bool | None = None
    # Events older than this are purged from the log
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS

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
        # Taken as typed (spaces included); only an empty or blank value means "no password"
        password = env.get("ADMIN_PASSWORD", "")
        if not password.strip():
            password = ""
        elif len(password) < ADMIN_PASSWORD_MIN_LENGTH:
            raise ValueError(f"ADMIN_PASSWORD must be at least {ADMIN_PASSWORD_MIN_LENGTH} characters long")
        retention_raw = env.get("LOG_RETENTION_DAYS", "").strip() or str(DEFAULT_LOG_RETENTION_DAYS)
        if not retention_raw.isdigit() or int(retention_raw) < 1:
            raise ValueError(f"LOG_RETENTION_DAYS must be a whole number of days, 1 or more, got {retention_raw!r}")
        api_raw = env.get("API_ENABLED", "").strip().lower()
        if api_raw and api_raw not in _TRUE + _FALSE:
            raise ValueError(f"API_ENABLED must be true or false, got {env['API_ENABLED']!r}")
        local_raw = env.get("LOCAL_ONLY", "").strip().lower()
        if local_raw and local_raw not in _TRUE + _FALSE:
            raise ValueError(f"LOCAL_ONLY must be true or false, got {env['LOCAL_ONLY']!r}")
        return cls(
            port=port,
            data_dir=Path(env.get("DATA_DIR", "").strip() or DEFAULT_DATA_DIR),
            public_base_url=base,
            log_level=level_raw.upper(),
            admin_password=password or None,
            api_enabled=(api_raw in _TRUE) if api_raw else None,
            local_only=(local_raw in _TRUE) if local_raw else None,
            log_retention_days=int(retention_raw),
        )
