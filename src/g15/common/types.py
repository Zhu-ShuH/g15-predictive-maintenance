"""Shared numerical data types for the G15 module boundary (Issue #17)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

__all__ = ["SimulationChunk"]


@dataclass(frozen=True)
class SimulationChunk:
    """One shard of a simulated run, matching ``docs/interface.md`` section 5.1.

    All four arrays have the same length and are ready to be handed to
    ``pipeline/serialization.py`` (owned by C4) without further conversion.

    Attributes:
        seq: ``int64`` absolute sampling indices, contiguous and increasing.
        timestamp_ns: ``int64`` UTC Unix nanoseconds, one per sample.
        vibration: ``float64`` vibration amplitude in ``g``.
        temperature: ``float64`` temperature in ``degC``.

    Note:
        This is the unique public definition (Issue #17).  The legacy
        ``g15.simulation.engine.SimulationChunk`` name re-exports this same
        object.  Empty in-memory chunks are valid; serialization independently
        validates the arrays and refuses empty on-disk shards.
    """

    seq: NDArray[np.int64]
    timestamp_ns: NDArray[np.int64]
    vibration: NDArray[np.float64]
    temperature: NDArray[np.float64]

    def __len__(self) -> int:
        """Return the number of samples in the chunk."""
        return int(self.seq.shape[0])

    @property
    def sample_count(self) -> int:
        """Number of simulated samples (the point count used by the teacher metric)."""
        return len(self)

    @property
    def scalar_value_count(self) -> int:
        """Number of scalar signal values carried by the chunk.

        ``docs/interface.md`` section 5.1 requires performance reports to keep
        this separate from ``sample_count``: one sample carries two scalars
        (vibration and temperature), so the two numbers must never be conflated.
        """
        return int(self.vibration.shape[0] + self.temperature.shape[0])

    @property
    def first_seq(self) -> int:
        """Absolute index of the first sample, or ``-1`` for an empty chunk."""
        return int(self.seq[0]) if len(self) else -1

    @property
    def last_seq_exclusive(self) -> int:
        """Absolute index one past the last sample, or ``-1`` for an empty chunk."""
        return int(self.seq[-1]) + 1 if len(self) else -1

    def validate(self) -> None:
        """Check the chunk against the frozen shard contract.

        Raises:
            ValueError: if array lengths differ, a dtype is wrong, ``seq`` is not
                strictly increasing and contiguous, or a signal value is not
                finite.
        """
        expected = {
            "seq": (np.dtype(np.int64), self.seq),
            "timestamp_ns": (np.dtype(np.int64), self.timestamp_ns),
            "vibration": (np.dtype(np.float64), self.vibration),
            "temperature": (np.dtype(np.float64), self.temperature),
        }
        lengths = set()
        for name, (dtype, array) in expected.items():
            if array.ndim != 1:
                raise ValueError(
                    f"{name} must be one-dimensional, got ndim={array.ndim}"
                )
            if array.dtype != dtype:
                raise ValueError(f"{name} must be {dtype}, got {array.dtype}")
            lengths.add(int(array.shape[0]))
        if len(lengths) > 1:
            raise ValueError(
                f"chunk arrays must be equal length, got {sorted(lengths)}"
            )
        if len(self):
            steps = np.diff(self.seq)
            if not bool(np.all(steps == 1)):
                raise ValueError("seq must be contiguous and strictly increasing by 1")
            for name in ("vibration", "temperature"):
                array = getattr(self, name)
                if not bool(np.all(np.isfinite(array))):
                    index = int(np.argmax(~np.isfinite(array)))
                    raise ValueError(f"{name} is not finite at local index {index}")
