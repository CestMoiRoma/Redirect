# Redirect

Redirect gives out fixed links and sends each one to a target you can change at any time. Say an Excel file on the local network changes URL from time to time: people receive `http://192.168.1.20:8080/redirect/compta/budget-2026` once, and when the file moves you change the target in Redirect instead of sending a new link. A link answers with an HTTP 302 to its current target, and Redirect never serves or proxies the file itself.

![The Redirect interface: the list of redirects and the form to create one](test/screenshot/redirect/02-list.png)

## Run it with Docker

The image is `ghcr.io/cestmoiroma/redirect`, for `linux/amd64` and `linux/arm64`. The `compose.yaml` of this repository comes down to this:

```yaml
services:
  redirect:
    image: ghcr.io/cestmoiroma/redirect:latest
    restart: unless-stopped
    ports:
      - '8080:8080'
    volumes:
      - redirect-data:/data

volumes:
  redirect-data:
```

Start it with `docker compose up -d`, or without compose:

```sh
docker run -d --name redirect --restart unless-stopped -p 8080:8080 -v redirect-data:/data ghcr.io/cestmoiroma/redirect:latest
```

The interface is then at `http://<host>:8080/` and the links at `http://<host>:8080/redirect/<path>`. The redirects live in a SQLite file, `redirect.db`, in the `redirect-data` volume, so they survive restarts and upgrades. Each release is also tagged with its version, such as `0.1.0`: pin it to upgrade when you choose.

| Variable              | Default         | What it does                                                                                                     |
| --------------------- | --------------- | ---------------------------------------------------------------------------------------------------------------- |
| `PORT`                | `8080`          | Port the server listens on inside the container. Publish that port if you change it.                             |
| `DATA_DIR`            | `/data`         | Folder of `redirect.db`. The server runs as uid 10001, which must be able to write there.                        |
| `PUBLIC_BASE_URL`     | unset           | Base of the links the interface shows, e.g. `https://go.example.org`. Unset, links follow the browser's address. |
| `LOG_LEVEL`           | `INFO`          | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`.                                                               |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` | Addresses whose `X-Forwarded-Proto` and `X-Forwarded-For` headers are trusted.                                   |

### Behind a reverse proxy

Set `PUBLIC_BASE_URL` to the address people use, for example `https://go.example.org`. Without it, the links in the interface follow the `Host` header the proxy forwards, and they stay `http://` even when the proxy serves HTTPS: Redirect only trusts `X-Forwarded-Proto` from the loopback. Setting `FORWARDED_ALLOW_IPS` to the proxy's address makes it honour that header. When `PUBLIC_BASE_URL` is set, it always wins.

### Keep it on a trusted network

Redirect has no authentication: anyone who can reach its port can create, change or delete redirects, and a web page opened on a machine of the same network can reach the API through DNS rebinding. Run it on a network you trust, or put a reverse proxy that requires authentication in front of the interface and the API, and publish the container's port to that proxy only. The links under `/redirect/` are public by nature: anyone who has one can follow it.

## Create a link and change its target later

Open the interface, fill in the path, a name and the target URL under New redirect, then click Create redirect. Copy link on the new entry puts its full public link in the clipboard, ready to share. When the file moves, click Edit on the entry and change the target URL: the link people already have leads to the new target from their next click.

## Work on the code

You need `just` and Docker. Lint, tests and screenshots run in a pinned image built from `test/ci/Dockerfile` for `linux/amd64`, emulated on ARM machines, and never on the host. The application image is a separate one, built from the root `Dockerfile`.

- `just run` starts the app on the host with auto-reload, at `http://127.0.0.1:8080`, with its data in `./data`. It needs `uv`.
- `just lint` runs every formatter and linter in check mode. `just fmt` applies the formatters.
- `just test redirect` runs pytest, then the end-to-end scenario in Chromium, which rewrites the screenshots.
- `just ci` runs what CI runs, in the same order, and ends by building and testing the application image. Commit first: the screenshots are compared with `HEAD`.
- `just image` builds the application image as `redirect:local`. `just up` and `just down` run it with compose.

The screenshots in `test/screenshot/redirect/` must match the commit byte for byte. After an intended UI change, run `just redirect-gui-goldens`, look at every image that changed, and commit them with the change. A new screen goes into `test/redirect/screenshots.py`, and `just gui-goldens-stability redirect 3` proves its captures are stable.

## Release an image

Pushing a tag `vX.Y.Z` publishes the image. The version must equal `version` in `pyproject.toml` and `__version__` in `redirect/__init__.py`, or the `publish` job stops before building anything. Bump both, run `uv lock`, commit, then tag that commit:

```sh
git tag v0.1.0
git push origin v0.1.0
```

The workflow in `.github/workflows/ci.yml` first runs the jobs of `just ci`: lint, tests, screenshots and the application image test. When they pass, the `publish` job builds the image for `linux/amd64` and `linux/arm64`, pushes it by digest, and runs the image test again on both platforms against the pushed image. Only then does it tag the image `X.Y.Z` and `latest`, so a failure leaves the existing tags where they were.

Nothing runs until the repository variable `CI_ENABLED` is `true`. The workflow pushes with its own `GITHUB_TOKEN`, granted `packages: write`, and GHCR creates `redirect` as a private package on the first push: make it public in the package settings, or `docker pull` asks for a login.
