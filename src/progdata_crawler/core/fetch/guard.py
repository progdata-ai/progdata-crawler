"""Protection against requests to internal addresses (SSRF).

A crawler that fetches any URL it is given can be pointed at addresses that only make
sense inside a network: `127.0.0.1`, `10.0.0.0/8`, or the cloud metadata service at
`169.254.169.254`, which hands out credentials. When the service runs on a server, those
addresses must be refused.

The check happens where the connection is made, not only before: `GuardedBackend`
resolves the host itself, refuses if any address is internal, and connects to the
address it checked. A name that resolves to a public address at check time and an
internal one a moment later (DNS rebinding) cannot slip through, and every redirect
opens a new connection that is checked again.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import cast
from urllib.parse import urlsplit

import httpcore
import httpx

from progdata_crawler.core.errors import CrawlerError
from progdata_crawler.models.envelope import ErrorCode

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

Resolver = Callable[[str, int], Awaitable[list[str]]]
"""Resolves a host name to IP address strings."""


def is_public_ip(address: IpAddress) -> bool:
    """True for addresses on the public internet. False for private, loopback,
    link-local, multicast, reserved and unspecified addresses, including IPv4 addresses
    wrapped in IPv6."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _refuse(host: str, reason: str) -> CrawlerError:
    return CrawlerError(
        ErrorCode.ADDRESS_NOT_ALLOWED,
        f"requests to internal network addresses are not allowed ({reason})",
        host=host,
    )


async def resolve_public(host: str, port: int, resolver: Resolver = system_resolve) -> list[str]:
    """The public addresses of `host`. Raises `address_not_allowed` if any address it
    resolves to is internal, so a name cannot mix public and internal addresses."""
    host = host.strip("[]")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if not is_public_ip(literal):
            raise _refuse(host, str(literal))
        return [str(literal)]
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise CrawlerError(
            ErrorCode.FETCH_FAILED, f"could not resolve host {host!r}", host=host
        ) from exc
    if not addresses:
        raise CrawlerError(ErrorCode.FETCH_FAILED, f"could not resolve host {host!r}", host=host)
    for text in addresses:
        if not is_public_ip(ipaddress.ip_address(text.split("%")[0])):
            raise _refuse(host, f"resolves to {text}")
    return addresses


class AddressGuard:
    """Checks URLs and hosts against the internal-address rule."""

    def __init__(self, *, allow_private: bool, resolver: Resolver = system_resolve) -> None:
        self.allow_private = allow_private
        self._resolver = resolver

    async def check_url(self, url: str) -> None:
        """Raise `address_not_allowed` when `url` points at an internal address."""
        if self.allow_private:
            return
        parts = urlsplit(url)
        if parts.hostname is None:
            return
        port = parts.port or (443 if parts.scheme == "https" else 80)
        await resolve_public(parts.hostname, port, self._resolver)

    async def public_addresses(self, host: str, port: int) -> list[str]:
        return await resolve_public(host, port, self._resolver)


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """A network backend that only connects to public addresses it has checked itself."""

    def __init__(self, guard: AddressGuard) -> None:
        self._guard = guard
        # httpcore defines AnyIOBackend conditionally; anyio is always installed with httpx.
        self._backend = cast(httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend())

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._guard.public_addresses(host, port)
        last_error: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise _refuse(path, "unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class GuardedTransport(httpx.AsyncHTTPTransport):
    """An httpx transport whose connections go through `GuardedBackend`.

    Proxy settings from the environment are ignored on purpose: through a proxy the
    connection would be to the proxy, and the guard could not see the real target.
    """

    def __init__(self, guard: AddressGuard) -> None:
        super().__init__(http2=True, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            http1=True,
            http2=True,
            network_backend=GuardedBackend(guard),
        )


def guarded_client(
    guard: AddressGuard,
    *,
    user_agent: str,
    timeout_s: float,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """An httpx client for calls to other services (webhooks, search APIs). It does not
    follow redirects, and with a guard that refuses private networks every connection
    goes through `GuardedTransport`, ignoring proxy settings."""
    guarded = not guard.allow_private
    if transport is None and guarded:
        transport = GuardedTransport(guard)
    return httpx.AsyncClient(
        transport=transport,
        trust_env=not guarded,
        follow_redirects=False,
        timeout=timeout_s,
        headers={"User-Agent": user_agent, **(headers or {})},
    )
