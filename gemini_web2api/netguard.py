"""URL safety checks and bounded downloads for user-supplied media URLs.

The image pipeline accepts arbitrary URLs from an API caller, so without these
checks a request could reach cloud metadata endpoints (169.254.169.254), the
loopback interface, or any host on the operator's private network and reflect
the response back through Gemini. Every hop of a redirect chain is validated,
the transfer is size-capped, and the whole download is bounded by a timeout.
"""
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
DEFAULT_USER_AGENT = "Mozilla/5.0"

# Hostnames that never resolve to a routable target and are commonly used to
# reach internal services.
BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
}

# Ranges ipaddress does not classify as "private" but that must never be reached
# from a caller-supplied URL: ISP carrier NAT, NAT64/DNS64 translation prefixes
# and the IETF documentation blocks.
_EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),      # RFC 6598 carrier-grade NAT
    ipaddress.ip_network("64:ff9b::/96"),       # RFC 6052 NAT64
    ipaddress.ip_network("2001:db8::/32"),      # documentation
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
]


class UnsafeURLError(ValueError):
    """The URL was rejected before any network traffic was sent."""


class ResponseTooLargeError(ValueError):
    """The remote body exceeded the configured size cap."""


def is_blocked_ip(address):
    """True when the address is not a public, routable unicast address."""
    if isinstance(address, str):
        try:
            address = ipaddress.ip_address(address)
        except ValueError:
            return True
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    if address.is_multicast or address.is_reserved or address.is_unspecified:
        return True
    for network in _EXTRA_BLOCKED_NETWORKS:
        if address.version == network.version and address in network:
            return True
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return is_blocked_ip(mapped)
    return False


def _resolve_ipv4_literals(hostname):
    """Parse bare IPv4 literals in the alternate notations urllib accepts.

    Browsers/urllib happily turn "2130706433" or "0x7f.1" into 127.0.0.1, so a
    plain ipaddress.ip_address() check is not enough.
    """
    candidate = hostname.strip()
    if not candidate:
        return []
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    literals = []
    try:
        literals.append(ipaddress.ip_address(candidate))
        return literals
    except ValueError:
        pass

    parts = candidate.split(".")
    if all(part == "" for part in parts):
        return []
    numbers = []
    for part in parts:
        try:
            if part.lower().startswith("0x"):
                numbers.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return []
    if len(numbers) == 1:
        total = numbers[0]
        if 0 <= total <= 0xFFFFFFFF:
            literals.append(ipaddress.ip_address(total))
            return literals
        return []
    if len(numbers) == 4 and all(0 <= n <= 255 for n in numbers):
        literals.append(ipaddress.ip_address(bytes(numbers)))
    return literals


def validate_url(url, allow_private=False):
    """Validate a user-supplied URL, returning the parsed result.

    Raises UnsafeURLError when the scheme is unsupported, the host is a known
    internal name, or any resolved address falls outside the public ranges.
    """
    if not isinstance(url, str) or not url.strip():
        raise UnsafeURLError("image URL is empty")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"unsupported URL scheme: {parsed.scheme or 'none'}")
    hostname = parsed.hostname or ""
    if not hostname:
        raise UnsafeURLError("image URL has no host")
    if allow_private:
        return parsed
    if hostname.strip(".").lower() in BLOCKED_HOSTNAMES:
        raise UnsafeURLError(f"blocked host: {hostname}")

    literals = _resolve_ipv4_literals(hostname)
    if literals:
        for address in literals:
            if is_blocked_ip(address):
                raise UnsafeURLError(f"blocked address: {address}")
        return parsed

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise UnsafeURLError(f"cannot resolve host {hostname}: {error}") from error
    if not infos:
        raise UnsafeURLError(f"cannot resolve host {hostname}")
    for info in infos:
        address = info[4][0]
        if is_blocked_ip(address):
            raise UnsafeURLError(f"{hostname} resolves to a non-public address: {address}")
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable urllib's automatic redirects so every hop can be re-validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener(proxy=None, ssl_ctx=None):
    handlers = [_NoRedirect()]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    if ssl_ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))
    return urllib.request.build_opener(*handlers)


def fetch_bytes(url, *, max_bytes, timeout=30, proxy=None, ssl_ctx=None,
                allow_private=False, user_agent=DEFAULT_USER_AGENT,
                max_redirects=MAX_REDIRECTS, log=None):
    """Download a URL with SSRF checks, a size cap and a redirect limit.

    Raises UnsafeURLError / ResponseTooLargeError. Transport failures propagate
    as their original exceptions so callers can distinguish them.
    """
    opener = _build_opener(proxy=proxy, ssl_ctx=ssl_ctx)
    target = url
    for hop in range(max_redirects + 1):
        validate_url(target, allow_private=allow_private)
        request = urllib.request.Request(target, headers={"User-Agent": user_agent})
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303, 307, 308):
                location = error.headers.get("Location") if error.headers else None
                error.close()
                if not location:
                    raise UnsafeURLError(f"redirect without Location from {target}") from error
                target = urllib.parse.urljoin(target, location)
                if log:
                    log(f"Image fetch redirect {error.code} -> {urllib.parse.urlsplit(target).netloc}")
                continue
            raise
        try:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ResponseTooLargeError(
                    f"remote media advertises {declared} bytes, limit is {max_bytes}")
            body = response.read(max_bytes + 1)
        finally:
            response.close()
        if len(body) > max_bytes:
            raise ResponseTooLargeError(f"remote media exceeds the {max_bytes} byte limit")
        return body
    raise UnsafeURLError(f"too many redirects (limit {max_redirects}) fetching {url}")


def default_ssl_context():
    """Shared verifying SSL context (also used by the protocol client)."""
    return ssl.create_default_context()
