"""Seeded, chunk-addressable Gaussian noise streams for the G15 simulator.

Design notes
------------
* ``numpy.random.default_rng`` is the only generator used, as required by
  ``docs/interface.md`` section 4 ("随机性").
* A sample's noise value is addressed by ``(seed, stream name, absolute block
  index)`` through :class:`numpy.random.SeedSequence`.  The numbers attached to
  sample index ``i`` therefore never depend on how a run is split into chunks,
  nor on how often a chunk is retried or resumed after a crash: chunk ``[a, b)``
  generated alone is bit-identical to the same range inside a one-shot run.
* The signal streams are independent.  Changing the vibration noise model
  cannot shift a single temperature sample, because each stream owns its own
  :class:`~numpy.random.SeedSequence` entropy (``docs/interface.md`` section 4,
  "稳定的子随机流").
* The stream tag uses :func:`zlib.crc32`, which is stable across platforms and
  Python releases, unlike the process-randomised built-in :func:`hash`.

The module keeps no mutable state: every function is a pure function of its
arguments.
"""

from __future__ import annotations

import math
import zlib
from typing import Final

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "BLOCK_POINTS",
    "STREAM_TEMPERATURE",
    "STREAM_VIBRATION",
    "gaussian_noise",
    "standard_normal",
    "stream_entropy",
]

#: Name of the vibration noise sub-stream.
STREAM_VIBRATION: Final[str] = "vibration"

#: Name of the temperature noise sub-stream.
STREAM_TEMPERATURE: Final[str] = "temperature"

#: Number of normal draws generated per random-access block.
#:
#: A block is the unit of random access: any requested index range is served by
#: regenerating the whole blocks it spans.  The overhead is therefore at most
#: two blocks per call, which is negligible for the chunk sizes used by the
#: project (``chunk_points`` defaults to 102400) while keeping the numbers
#: independent of the chunking.
BLOCK_POINTS: Final[int] = 16384

_UINT32_MASK: Final[int] = 0xFFFFFFFF
_UINT64_MASK: Final[int] = 0xFFFFFFFFFFFFFFFF


def _as_int(value: object, path: str) -> int:
    """Return ``value`` as a Python int, rejecting floats that are not whole.

    Raises:
        ValueError: if ``value`` is not an integral, finite number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        if isinstance(value, (float, np.floating)):
            if not math.isfinite(float(value)) or float(value) != int(value):
                raise ValueError(f"{path} must be a whole number, got {value!r}")
            value = int(value)
        else:
            raise ValueError(f"{path} must be a whole number, got {value!r}")
    return int(value)


def _as_non_negative_int(value: object, path: str) -> int:
    """Return ``value`` as a non-negative Python int.

    Raises:
        ValueError: if ``value`` is not an integral, finite, non-negative number.
    """
    result = _as_int(value, path)
    if result < 0:
        raise ValueError(f"{path} must be non-negative, got {result}")
    return result


def _as_finite_float(value: object, path: str) -> float:
    """Return ``value`` as a finite float.

    Raises:
        ValueError: if ``value`` is not a finite real number.
    """
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path} must be a finite number, got {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"{path} must be finite, got {value!r}")
    return result


def _stream_tag(stream: str) -> int:
    """Return a stable 32-bit tag for a stream name.

    Uses CRC-32 rather than :func:`hash` so the tag does not change between
    interpreter runs or platforms.
    """
    if not isinstance(stream, str) or not stream:
        raise ValueError(f"stream name must be a non-empty string, got {stream!r}")
    return zlib.crc32(stream.encode("utf-8")) & _UINT32_MASK


def stream_entropy(seed: int, stream: str, block: int) -> list[int]:
    """Return the :class:`~numpy.random.SeedSequence` entropy for one block.

    Args:
        seed: Run seed taken from the scenario configuration.
        stream: Sub-stream name, e.g. :data:`STREAM_VIBRATION`.
        block: Absolute block index; block ``b`` covers sample indices
            ``[b * BLOCK_POINTS, (b + 1) * BLOCK_POINTS)``.

    Returns:
        Three non-negative integers, used as ``SeedSequence(entropy)``.
    """
    return [
        _as_non_negative_int(seed, "seed") & _UINT64_MASK,
        _stream_tag(stream),
        _as_non_negative_int(block, "block") & _UINT64_MASK,
    ]


def block_generator(seed: int, stream: str, block: int) -> np.random.Generator:
    """Return the generator that owns one ``(seed, stream, block)`` triple.

    The returned generator is freshly seeded on every call, so callers must not
    assume any continuity between blocks; use :func:`standard_normal` for
    index-addressed access.
    """
    entropy = stream_entropy(seed, stream, block)
    return np.random.default_rng(np.random.SeedSequence(entropy))


def standard_normal(
    seed: int,
    stream: str,
    start: int,
    count: int,
    *,
    block_points: int = BLOCK_POINTS,
) -> NDArray[np.float64]:
    """Return ``count`` standard normal samples for indices ``[start, start+count)``.

    The result is a pure function of ``(seed, stream, start, count, block_points)``.

    Args:
        seed: Run seed.
        stream: Sub-stream name.
        start: First sample index; must be non-negative.
        count: Number of samples; must be non-negative.
        block_points: Block size of the random-access grid.

    Returns:
        ``float64`` array of shape ``(count,)``.

    Raises:
        ValueError: for negative or non-integral indices, or a non-positive
            block size.
    """
    first = _as_non_negative_int(start, "start")
    size = _as_non_negative_int(count, "count")
    block_size = _as_int(block_points, "block_points")
    if block_size <= 0:
        raise ValueError(f"block_points must be positive, got {block_size}")
    out = np.empty(size, dtype=np.float64)
    if size == 0:
        return out

    last = first + size - 1
    for block in range(first // block_size, last // block_size + 1):
        block_start = block * block_size
        low = max(first, block_start)
        high = min(first + size, block_start + block_size)
        data = block_generator(seed, stream, block).standard_normal(block_size)
        out[low - first : high - first] = data[low - block_start : high - block_start]
    return out


def gaussian_noise(  # pylint: disable=too-many-arguments
    seed: int,
    stream: str,
    start: int,
    count: int,
    std: float,
    *,
    block_points: int = BLOCK_POINTS,
) -> NDArray[np.float64]:
    """Return ``count`` samples of ``N(0, std**2)`` for ``[start, start+count)``.

    A ``std`` of exactly ``0.0`` returns zeros without touching the generator,
    which keeps zero-noise scenarios fast and still chunk-independent.

    Args:
        seed: Run seed.
        stream: Sub-stream name.
        start: First sample index; must be non-negative.
        count: Number of samples; must be non-negative.
        std: Standard deviation in the unit of the target signal; must be
            finite and non-negative.
        block_points: Block size of the random-access grid.

    Returns:
        ``float64`` array of shape ``(count,)``.

    Raises:
        ValueError: for a negative or non-finite ``std``, or any invalid index
            argument (see :func:`standard_normal`).
    """
    deviation = _as_finite_float(std, "noise_std")
    if deviation < 0.0:
        raise ValueError(f"noise_std must be non-negative, got {deviation}")
    size = _as_non_negative_int(count, "count")
    if deviation == 0.0:
        return np.zeros(size, dtype=np.float64)
    values = standard_normal(seed, stream, start, size, block_points=block_points)
    values *= deviation
    return values
