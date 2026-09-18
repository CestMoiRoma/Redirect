"""`python -m redirect`: serve the app with uvicorn on $PORT (no reload)."""

import sys

import uvicorn

from .config import Settings


def main() -> None:
    try:
        settings = Settings.from_env()
        # Importing the app builds it from the same environment: a bad PUBLIC_BASE_URL is reported here too
        from .main import app
    except ValueError as exc:
        # A configuration mistake gets one readable line, not a traceback
        sys.exit(f"redirect: {exc}")
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - meant to be reached from the LAN / the container network
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        server_header=False,
    )


if __name__ == "__main__":
    main()
