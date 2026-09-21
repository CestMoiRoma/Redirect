"""The mDNS announcer on its own. Announcing on a real network is proven by the image test."""

import pytest

from redirect import mdns


@pytest.mark.parametrize(
    ("hostname", "announced"),
    [("redirect.local", True), ("a.b.local", True), ("redirect.lan", False), (".local", False), ("local", False)],
)
def test_only_names_ending_in_local_are_announced(hostname, announced):
    assert mdns.is_mdns_name(hostname) is announced


def test_lan_addresses_are_ipv4_without_the_loopback():
    addresses = mdns.lan_addresses()
    assert all(address.count(".") == 3 and not address.startswith("127.") for address in addresses)


@pytest.mark.parametrize("hostname", [None, "redirect.lan"])
def test_nothing_is_published_for_other_names(hostname):
    announcer = mdns.ZeroconfAnnouncer(port=80)
    assert announcer.announce(hostname) is False
    # No mDNS socket was even opened
    assert announcer._zeroconf is None
    announcer.close()
