"""Query capacity harness for Issue #136 (Gate 0).

This package lives under tools/ (not src/) because it is measurement tooling,
not a product module.  It builds a small D0 store, drives the real read-only
query path over HTTP, records trusted client/server in-flight accounting, and
verifies responses against an independent SQLite oracle.
"""

__version__ = "0.1.0"
