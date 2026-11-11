"""What a client sends the proxy first: a TLS ClientHello as Python's ssl (OpenSSL)
writes it into a memory BIO, and the alerts the proxy answers a refused one with."""
import ipaddress
import ssl

# fatal TLS alerts (RFC 8446, 6.2): access_denied, unrecognized_name, internal_error
ACCESS_DENIED = bytes((21, 3, 3, 0, 2, 2, 49))
UNRECOGNIZED_NAME = bytes((21, 3, 3, 0, 2, 2, 112))
INTERNAL_ERROR = bytes((21, 3, 3, 0, 2, 2, 80))


def raw_client_hello(name, padding=0):
    """The least a ClientHello naming name needs (RFC 8446, 4.1.2; RFC 6066, 3), with no
    checks on the name; padding bytes after the extensions that their length does not cover."""
    raw = name.encode("utf-8")
    names = b"\x00" + len(raw).to_bytes(2, "big") + raw
    ext = b"\x00\x00" + (len(names) + 2).to_bytes(2, "big") + len(names).to_bytes(2, "big") + names
    body = (b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
            + len(ext).to_bytes(2, "big") + ext + bytes(padding))
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def client_hello(host=None, tls12=False):
    """The ClientHello a real client sends for host (None: without a server name), for
    TLS 1.3 or, with tls12, at most TLS 1.2. OpenSSL names no server for an address (RFC
    6066 forbids it), so for one this is the raw ClientHello that names it anyway."""
    if host is not None:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return raw_client_hello(host)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if tls12:
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = ctx.wrap_bio(incoming, outgoing, server_side=False, server_hostname=host)
    try:
        tls.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return outgoing.read()


def answer(sock, hello):
    """What the proxy answered a ClientHello on sock: "open" when the tunnel carries the
    ClientHello to the echo server and back, else the bytes before it closed."""
    data = b""
    while len(data) < len(hello):
        chunk = sock.recv(len(hello) - len(data))
        if not chunk:
            break
        data += chunk
    return "open" if data == hello else data
