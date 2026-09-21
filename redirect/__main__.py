"""`python -m redirect`: serve the app with uvicorn on $PORT (no reload).

`python -m redirect api-key` prints a new API key and replaces the stored one: the way to get a key when the
interface is off (HEADLESS). The running server takes the new key at once.
"""

import sys

import uvicorn

from .config import Settings

USAGE = "usage: python -m redirect [api-key]"


def new_api_key() -> None:
    from .auth import new_api_key_body
    from .db import Database
    from .main import issue_api_key

    settings = Settings.from_env()
    db = Database(settings.db_path)
    db.migrate()
    print(issue_api_key(db, db.clock, new_api_key_body).key)


def main() -> None:
    arguments = sys.argv[1:]
    if arguments not in ([], ["api-key"]):
        sys.exit(USAGE)
    try:
        settings = Settings.from_env()
        # Importing the app builds it from the same environment: a bad PUBLIC_BASE_URL is reported here too
        from .main import app
    except ValueError as exc:
        # A configuration mistake gets one readable line, not a traceback
        sys.exit(f"redirect: {exc}")
    if arguments == ["api-key"]:
        new_api_key()
        return
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
