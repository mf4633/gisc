"""gisc -- a stateless civil/GIS compiler.

civil intent -> frontend -> IR -> adapters -> view + provenance.

gisc stores nothing. It reads the owner's files and services and writes only
the output folder it was asked for.
"""

__version__ = "0.1.0"

from gisc.errors import AdapterError, GiscError, MissingCRSError, StubError

__all__ = ["__version__", "GiscError", "AdapterError", "MissingCRSError", "StubError"]
