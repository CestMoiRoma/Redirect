"""`python -m redirect`: serve the app with uvicorn on $PORT (no reload)."""

import uvicorn

from .config import Settings


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "redirect.main:app",
        host="0.0.0.0",  # noqa: S104 - meant to be reached from the LAN / the container network
        port=settings.port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        server_header=False,
    )


if __name__ == "__main__":
    main()
