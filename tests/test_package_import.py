"""Structural import smoke test only; no business behavior is asserted."""

from importlib import import_module


def test_package_modules_can_be_imported():
    """Import the package skeleton without executing business work."""
    import_module("g15")
    # g15.__main__ is intentionally excluded: invoking the CLI must fail closed.
    for name in (
        "g15.cli",
        "g15.config",
        "g15.simulation",
        "g15.simulation.engine",
        "g15.simulation.degradation",
        "g15.simulation.noise",
        "g15.simulation.faults",
        "g15.pipeline",
        "g15.pipeline.producer",
        "g15.pipeline.consumer",
        "g15.pipeline.schema",
        "g15.pipeline.serialization",
        "g15.analysis",
        "g15.analysis.features",
        "g15.analysis.health",
        "g15.analysis.alarms",
        "g15.storage",
        "g15.storage.database",
        "g15.storage.repository",
        "g15.reporting",
        "g15.reporting.builder",
        "g15.reporting.metrics",
        "g15.common",
        "g15.common.errors",
        "g15.common.logger",
        "g15.common.types",
    ):
        import_module(name)
