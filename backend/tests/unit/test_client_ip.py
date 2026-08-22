"""How the caller's address is resolved, and why the obvious reading is wrong.

`X-Forwarded-For` is written by whoever is talking to us. Two things depend on
getting it right — the rate limiter's identity and the `events` audit trail —
and both of them were wrong in the same direction until a production audit
asked what happens with no proxy in front of the app.

These are unit tests on the function rather than requests through the app,
because the question is entirely about one header and one socket address, and a
test that needs Redis to ask it is a test nobody runs while changing this.
"""

from __future__ import annotations

from starlette.requests import Request

from app.middleware.rate_limit import client_ip

PEER = "10.0.0.9"
"""The socket's actual peer — the proxy's address in a proxied deployment."""


def _request(*, forwarded: str | None = None, peer: str | None = PEER) -> Request:
    """A request carrying only what `client_ip` looks at."""
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": headers,
            "client": (peer, 51234) if peer is not None else None,
        }
    )


# --- no proxy: the header is not evidence --------------------------------


def test_the_header_is_ignored_when_nothing_is_proxying() -> None:
    """The default, and the reason it is the default.

    With `docker-compose.prod.yml` as it shipped at M16 — the API published
    straight to a port — this header comes from the caller and nowhere else.
    """
    assert client_ip(_request(forwarded="203.0.113.7")) == PEER


def test_a_forged_header_cannot_mint_new_identities() -> None:
    """The bug this whole setting exists for.

    The limiter keys anonymous traffic on the address. If a caller can choose
    it, they get a fresh budget with every request and the limiter is
    decoration — including on `/api/v1/auth`, where M16's audit put the
    expensive cost class precisely to make a login flood expensive.
    """
    seen = {client_ip(_request(forwarded=f"198.51.100.{n}")) for n in range(20)}

    assert seen == {PEER}


def test_the_peer_is_used_when_no_header_is_sent() -> None:
    assert client_ip(_request()) == PEER


def test_an_unknown_peer_is_named_rather_than_crashing() -> None:
    """`request.client` is None under some ASGI transports, including tests."""
    assert client_ip(_request(peer=None)) == "unknown"


# --- behind a proxy: count from the right --------------------------------


def test_one_trusted_proxy_reads_the_entry_it_appended() -> None:
    """A proxy appends the address it received the request from.

    So with exactly one hop the list is `[real client]`, and the rightmost
    entry is the only one anybody but the client wrote.
    """
    request = _request(forwarded="203.0.113.7")

    assert client_ip(request, trusted_proxy_hops=1) == "203.0.113.7"


def test_a_forgery_in_front_of_the_real_entry_is_discarded() -> None:
    """The failure the old implementation had *even with* a proxy.

    A client that sends `X-Forwarded-For: 1.2.3.4` has it appended to, not
    replaced: the app sees `1.2.3.4, <real>`. Reading the first entry — which
    is what this code did — reads the forgery.
    """
    request = _request(forwarded="1.2.3.4, 203.0.113.7")

    assert client_ip(request, trusted_proxy_hops=1) == "203.0.113.7"


def test_two_hops_skip_the_proxy_that_is_ours() -> None:
    """A CDN in front of Caddy: `[client, cdn]`, and the client is two in."""
    request = _request(forwarded="203.0.113.7, 192.0.2.50")

    assert client_ip(request, trusted_proxy_hops=2) == "203.0.113.7"


def test_fewer_entries_than_hops_falls_back_to_the_peer() -> None:
    """A request that did not come through the chain we were told to expect.

    Taking the leftmost entry here is exactly what a forged header wants: send
    one value, be believed, because the list is "too short" to check. The peer
    address is not what we wanted but it is a fact.
    """
    request = _request(forwarded="1.2.3.4")

    assert client_ip(request, trusted_proxy_hops=2) == PEER


def test_a_missing_header_behind_a_proxy_falls_back_to_the_peer() -> None:
    assert client_ip(_request(), trusted_proxy_hops=1) == PEER


def test_whitespace_and_empty_entries_are_not_addresses() -> None:
    """Proxies write `a, b`; some write `a,,b`. An empty string is not an id."""
    request = _request(forwarded=" 203.0.113.7 ,, 192.0.2.50 ")

    assert client_ip(request, trusted_proxy_hops=2) == "203.0.113.7"
