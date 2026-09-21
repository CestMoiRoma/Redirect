"""mDNS: a hostname ending in .local is announced on the LAN, so it resolves without any DNS setup.

The name is published as the host of an HTTP service ("Redirect (<name>)"): browsers of services list it, and
the host record answers the address queries for <name>.local. It only reaches the LAN when the container has an
address on it (the macvlan setup of compose.yaml); behind a Docker bridge, only the bridge hears it.
"""

import logging
import threading
from typing import Protocol

import ifaddr
from zeroconf import IPVersion, ServiceInfo, Zeroconf

MDNS_SUFFIX = ".local"
SERVICE_TYPE = "_http._tcp.local."

logger = logging.getLogger("redirect")


class Announcer(Protocol):
    def announce(self, hostname: str | None) -> bool:
        """Publish `hostname` (None: publish nothing), withdrawing what was published. True when published."""
        ...

    def close(self) -> None: ...


def is_mdns_name(hostname: str) -> bool:
    return hostname.endswith(MDNS_SUFFIX) and len(hostname) > len(MDNS_SUFFIX)


def lan_addresses() -> list[str]:
    """IPv4 addresses of this machine (the container's), loopback excluded."""
    found = {
        str(ip.ip)
        for adapter in ifaddr.get_adapters()
        for ip in adapter.ips
        if ip.is_IPv4 and not str(ip.ip).startswith("127.")
    }
    return sorted(found)


class ZeroconfAnnouncer:
    """The real announcer. Thread-safe: settings are saved from the server's worker threads."""

    def __init__(self, port: int) -> None:
        self.port = port
        self._lock = threading.Lock()
        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None

    def announce(self, hostname: str | None) -> bool:
        with self._lock:
            self._withdraw()
            if hostname is None or not is_mdns_name(hostname):
                return False
            addresses = lan_addresses()
            if not addresses:
                logger.warning("mDNS: no network address to announce %s on", hostname)
                return False
            label = hostname.removesuffix(MDNS_SUFFIX)
            info = ServiceInfo(
                SERVICE_TYPE,
                f"Redirect ({label}).{SERVICE_TYPE}",
                port=self.port,
                server=f"{hostname}.",
                parsed_addresses=addresses,
                properties={"path": "/"},
            )
            try:
                if self._zeroconf is None:
                    self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
                # Probes the network first (about a second), then answers for the name until withdrawn
                self._zeroconf.register_service(info, allow_name_change=True)
            except Exception as exc:  # a busy port 5353, no multicast route: the links still work by address
                logger.warning("mDNS: cannot announce %s: %s", hostname, exc)
                return False
            self._info = info
            logger.info("mDNS: %s announced on %s", hostname, ", ".join(addresses))
            return True

    def _withdraw(self) -> None:
        if self._zeroconf is not None and self._info is not None:
            self._zeroconf.unregister_service(self._info)
            self._info = None

    def close(self) -> None:
        with self._lock:
            self._withdraw()
            if self._zeroconf is not None:
                self._zeroconf.close()
                self._zeroconf = None
