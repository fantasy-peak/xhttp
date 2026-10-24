"""A small VLESS / XHTTP (stream-one) client with a local HTTP proxy.

The wire format is the same one Xray's XHTTP transport uses in
``stream-one`` mode: one HTTP/2 POST whose request body is the uplink and
whose response body is the downlink.  The first bytes in both directions are
the VLESS request/response headers.

Only the client half is implemented here; the server can be Xray-core or any
compatible XHTTP stream-one implementation.
"""

__version__ = "0.1.0"
