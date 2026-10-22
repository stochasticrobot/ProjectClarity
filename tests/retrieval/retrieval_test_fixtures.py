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
    """Build a valid tiny PDF without adding a second PDF-generation dependency."""
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
    """Return a fixed public address for names; retain literal IPs for deny-list tests."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = ipaddress.ip_address("93.184.216.34")
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    return [(family, socket.SOCK_STREAM, 6, "", (str(address), port))]


class FakeHTTP:
    """Return real urllib3 response objects without creating sockets or resolving DNS."""

    def __init__(self, routes):
        """Store permitted fixture routes and fresh request/connection logs; open no network resources."""
        self.routes = routes  # (Host, request path) -> (status, headers, bytes)
        self.requests = []
        self.connections = []

    def pool(self, host, **options):
        """Match HTTP(S)ConnectionPool's small surface used by our web reader."""
        self.connections.append((host, options))
        fixture = self

        class Pool:
            """The minimal pool interface the real fetcher needs, backed only by in-memory routes."""
            def request(self, method, path, **kwargs):
                """Record the call and return a streaming fixture response; unexpected destinations fail the test."""
                fixture.requests.append((method, path, kwargs))
                key = (kwargs["headers"]["Host"], path)
                if key not in fixture.routes:
                    raise AssertionError(f"Unexpected source request: {key}")
                status, headers, body = fixture.routes[key]
                return HTTPResponse(body=io.BytesIO(body), status=status, headers=headers, preload_content=False)

            def close(self):
                """Match the production cleanup interface; this fake owns no socket to close."""
                pass

        return Pool()
