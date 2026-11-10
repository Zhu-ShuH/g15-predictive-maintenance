"""Thin standard-library HTTP transport for the G15 query application."""

from g15.service.http import QueryHTTPServer, create_query_server

__all__ = ["QueryHTTPServer", "create_query_server"]
