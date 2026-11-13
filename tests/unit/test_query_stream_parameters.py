"""Strict HTTP selection syntax; absence remains legacy v1."""

import pytest

from g15.query.application import InvalidQueryError
from g15.service.http import _parse_parameters


def test_parameter_absence_is_distinct_from_explicit_both():
    """Both valid requests select both streams but only one opts into v2."""
    assert _parse_parameters("") == (50, None, None, None)
    assert _parse_parameters("streams=both") == (50, None, None, "both")
    assert _parse_parameters(
        "page_size=2&windows_cursor=a&alarms_cursor=b&streams=both"
    ) == (2, "a", "b", "both")


@pytest.mark.parametrize(
    "query",
    [
        "streams=",
        "streams=both&streams=windows",
        "streams=Both",
        "streams=windows,alarms",
        "streams=none",
        "streams=%20both",
        "streams=both&unknown=true",
        "streams=both&page_size=2&windows_cursor=a&alarms_cursor=b&extra=c",
    ],
)
def test_invalid_selection_parameters_are_not_silently_accepted(query):
    """Blank, repeated, unknown and over-counted parameters stay invalid."""
    with pytest.raises(InvalidQueryError):
        _parse_parameters(query)
