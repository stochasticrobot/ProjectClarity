"""Small, deliberately artificial documents and an HTTP substitute for tests.

For example, text_pdf() builds a one-page PDF containing a chosen sentence; it
does not download a PDF or pretend the sentence is research evidence. FakeHTTP
stands in for the connection pool only: the real URL checks, robots handling,
byte accounting, parsers, graph, and file writes remain in the path under test.
"""

import io
import ipaddress
import socket

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from urllib3.response import HTTPResponse


def text_pdf(text="A Canadian AI security research fixture.", *, pages=1, encrypted=False):
    """Build a valid tiny PDF without adding a second PDF-generation dependency.

    Args:
        text: Text to place in the generated PDF fixture.
        pages: Number of pages to generate in the tiny PDF fixture.
        encrypted: Whether to encrypt the synthetic PDF so unsupported encrypted-input
            handling can be tested.

    Returns:
        Bytes of a complete small PDF containing the specified text and page/encryption
        settings.
    """
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 10 Tf 10 100 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("fixture-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def public_dns(host, port, **kwargs):
    """Return a fixed public address for names; retain literal IPs for deny-list tests.

    Args:
        host: Host name passed to the DNS or HTTP-pool interface under test.
        port: Destination port to include in the synthetic socket address.
        **kwargs: DNS-resolution options accepted for socket.getaddrinfo compatibility; the
            fixture does not perform DNS.

    Returns:
        A getaddrinfo-shaped list containing one synthetic address; literal IPs are retained
        to exercise production rejection rules.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = ipaddress.ip_address("93.184.216.34")
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    return [(family, socket.SOCK_STREAM, 6, "", (str(address), port))]


class FakeHTTP:
    """Return real urllib3 response objects without creating sockets or resolving DNS."""

    def __init__(self, routes):
        """Store permitted fixture routes and fresh request/connection logs; open no network
        resources.

        Args:
            self: Fake HTTP router holding permitted routes and request/connection logs for
                assertions.
            routes: Mapping from (host, request path) to (status, headers, body bytes) for
                permitted fixture requests.

        Returns:
            None. Initialize the fixed route table and empty request/connection logs without
            opening sockets.
        """
        self.routes = routes  # (Host, request path) -> (status, headers, bytes)
        self.requests = []
        self.connections = []

    def pool(self, host, **options):
        """Match HTTP(S)ConnectionPool's small surface used by our web reader.

        Args:
            self: Fake HTTP router holding permitted routes and request/connection logs for
                assertions.
            host: Host name passed to the DNS or HTTP-pool interface under test.
            **options: Connection settings recorded by the fixture for assertions, without
                opening a real connection.

        Returns:
            A small fake connection-pool instance implementing request() and close() for the
            production fetcher.
        """
        self.connections.append((host, options))
        fixture = self

        class Pool:
            """The minimal pool interface the real fetcher needs, backed only by in-memory routes."""
            def request(self, method, path, **kwargs):
                """Record the call and return a streaming fixture response; unexpected destinations fail
                the test.

                Args:
                    self: Fixture pool using the enclosing router and host; it owns no real network
                        socket.
                    method: HTTP method passed by the production fetcher to the fixture connection pool.
                    path: HTTP request path selecting a route for the enclosing fake pool's host.
                    **kwargs: HTTP request options accepted by the fixture interface; no networking
                        occurs.

                Returns:
                    A streaming urllib3.HTTPResponse backed by the matching fixture bytes; an unexpected
                    route fails the test.
                """
                fixture.requests.append((method, path, kwargs))
                key = (kwargs["headers"]["Host"], path)
                if key not in fixture.routes:
                    raise AssertionError(f"Unexpected source request: {key}")
                status, headers, body = fixture.routes[key]
                return HTTPResponse(body=io.BytesIO(body), status=status, headers=headers, preload_content=False)

            def close(self):
                """Match the production cleanup interface; this fake owns no socket to close.

                Args:
                    self: Fixture pool using the enclosing router and host; it owns no real network
                        socket.

                Returns:
                    None. Satisfy the production cleanup interface; this fake owns no socket.
                """
                pass

        return Pool()
