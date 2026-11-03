"""Unit tests for controlled fault injection (Issue #6, owner C1).

Acceptance command (Issue #6)::

    python -m pytest -q tests/unit/test_faults*.py

Every numeric assertion is hand-checkable: the comment above each expected
array shows the closed-form expression it was computed from. Only the vibration
channel is allowed to change; ``seq``, ``timestamp_ns`` and ``temperature`` are
asserted untouched wherever it matters.
"""

from __future__ import annotations

import copy
import datetime as dt
from collections.abc import Sequence

import numpy as np
import pytest

from g15.common.types import SimulationChunk
from g15.config import (
    FaultEvent as ConfigFaultEvent,
)
from g15.config import (
    FaultGenerationConfig,
    ScenarioBlock,
    ScenarioConfig,
    SignalConfig,
    SignalsBlock,
)
from g15.simulation.engine import simulate_chunk, timestamp_ns_for
from g15.simulation.faults import FaultEvent, inject_faults

_DEVICE = "DEV-001"
_RATE = 100
_START_UTC = "2026-10-14T00:00:00Z"


def _chunk(start_seq: int, values: Sequence[float]) -> SimulationChunk:
    """Build a hand-written chunk so expected arrays stay readable."""
    vibration = np.array(values, dtype=np.float64)
    size = int(vibration.size)
    return SimulationChunk(
        seq=np.arange(start_seq, start_seq + size, dtype=np.int64),
        timestamp_ns=timestamp_ns_for(start_seq, size, 0, _RATE),
        vibration=vibration,
        temperature=np.full(size, 35.0, dtype=np.float64),
    )


def _event(
    event_id: str, kind: str, start_seq: int, duration_points: int, amplitude: float
) -> dict[str, object]:
    """Return one ``fault_generation.events`` entry."""
    return {
        "event_id": event_id,
        "type": kind,
        "start_seq": start_seq,
        "duration_points": duration_points,
        "amplitude": amplitude,
    }


def _config(
    events: Sequence[object],
    total_points: int,
    *,
    mode: str = "manual",
    probability: float = 0.0,
) -> dict[str, object]:
    """Return a minimal scenario configuration for the injector."""
    return {
        "scenario": {
            "seed": 42,
            "sample_rate_hz": _RATE,
            "start_time_utc": _START_UTC,
            "total_points": total_points,
            "device_id": _DEVICE,
        },
        "fault_generation": {
            "mode": mode,
            "stochastic_probability_per_window": probability,
            "events": list(events),
        },
    }


def _scenario(events: Sequence[object], total_points: int) -> dict[str, object]:
    """Return a full scenario (with signals) accepted by ``simulate_chunk``."""
    config = _config(events, total_points)
    config["signals"] = {
        "vibration": {
            "baseline": 0.5,
            "degradation_rate_per_sample": 0.0,
            "noise_std": 0.0,
            "unit": "g",
        },
        "temperature": {
            "baseline": 35.0,
            "degradation_rate_per_sample": 0.0,
            "noise_std": 0.0,
            "unit": "degC",
        },
    }
    return config


def test_spike_peaks_at_event_start_and_decays() -> None:
    """``spike`` = ``A * exp(-k / (D/4))``; here D=4 so tau=1."""
    chunk = _chunk(0, [1.0] * 8)
    config = _config([_event("F001", "spike", 2, 4, 5.0)], 8)

    result, truth = inject_faults(chunk, config)

    # +5*exp(0)=5, +5*exp(-1), +5*exp(-2), +5*exp(-3)
    expected = [1.0, 1.0, 6.0, 2.8393972059, 1.6766764162, 1.2489353418, 1.0, 1.0]
    assert result.vibration == pytest.approx(expected, abs=1e-9)
    assert truth == [
        FaultEvent("F001", _DEVICE, "spike", 2, 6, 5.0),
    ]


def test_step_adds_constant_offset_and_recovers() -> None:
    """``step`` = ``+A`` over ``[start, end)``, back to baseline afterwards."""
    chunk = _chunk(8, [1.0] * 6)
    config = _config([_event("F002", "step", 10, 3, 2.0)], 14)

    result, truth = inject_faults(chunk, config)

    assert result.vibration == pytest.approx([1.0, 1.0, 3.0, 3.0, 3.0, 1.0])
    assert truth[0].start_seq == 10
    assert truth[0].end_seq_exclusive == 13
    assert truth[0].duration_points == 3


def test_burst_is_windowed_oscillation() -> None:
    """``burst`` = ``A*sin(2*pi*k/8)*hann(k)``; D=8, A=4, sr=100."""
    chunk = _chunk(0, [0.0] * 8)
    config = _config([_event("F003", "burst", 0, 8, 4.0)], 8)

    result, truth = inject_faults(chunk, config)

    expected = [
        0.0,
        0.5324658286,  # 4*sin(pi/4)*(0.5-0.5*cos(2*pi/7))
        2.4450418680,  # 4*sin(pi/2)*(0.5-0.5*cos(4*pi/7))
        2.6883759550,  # 4*sin(3*pi/4)*(0.5-0.5*cos(6*pi/7))
        0.0,  # sin(pi) = 0
        -1.7289056850,
        -0.7530203960,
        0.0,  # Hann window closes the event
    ]
    assert result.vibration == pytest.approx(expected, abs=1e-9)
    assert result.vibration[0] == 0.0
    assert result.vibration[-1] == 0.0
    assert truth[0].to_dict()["type"] == "burst"


def test_overlapping_events_add_up() -> None:
    """Section 7.2 default: additive superposition, never overwrite."""
    chunk = _chunk(0, [0.0] * 14)
    config = _config(
        [
            _event("F010", "step", 5, 5, 2.0),
            _event("F011", "step", 8, 4, 3.0),
        ],
        14,
    )

    result, truth = inject_faults(chunk, config)

    # [5,8) +2 | [8,10) +2+3 | [10,12) +3
    expected = [0, 0, 0, 0, 0, 2, 2, 2, 5, 5, 3, 3, 0, 0]
    assert result.vibration == pytest.approx(expected)
    assert [event.event_id for event in truth] == ["F010", "F011"]


def test_no_events_leaves_signal_untouched() -> None:
    """An empty event list is a legal "no injection" run."""
    chunk = _chunk(0, [1.0, 2.0, 3.0, 4.0])
    result, truth = inject_faults(chunk, _config([], 4))

    assert result is chunk
    assert truth == []
    assert result.vibration == pytest.approx([1.0, 2.0, 3.0, 4.0])


def test_event_crossing_chunk_boundary_matches_one_shot() -> None:
    """Chunking must not change a single sample of the injected signal."""
    config = _scenario([_event("F020", "step", 8, 5, 2.0)], 20)

    one_shot, _ = inject_faults(simulate_chunk(config, 0, 20), config)
    first, truth_first = inject_faults(simulate_chunk(config, 0, 10), config)
    second, truth_second = inject_faults(simulate_chunk(config, 10, 10), config)

    joined = np.concatenate([first.vibration, second.vibration])
    assert np.array_equal(joined, one_shot.vibration)
    assert joined[7] == pytest.approx(0.5)
    assert joined[8] == pytest.approx(2.5)
    assert joined[12] == pytest.approx(2.5)
    assert joined[13] == pytest.approx(0.5)
    # Same global record in both chunks: the truth keeps absolute coordinates.
    assert truth_first == truth_second
    assert truth_first[0].start_seq == 8
    assert truth_first[0].end_seq_exclusive == 13


def test_event_outside_chunk_is_not_reported() -> None:
    """A chunk that only touches the gap must report no event at all."""
    chunk = _chunk(20, [1.0] * 5)
    config = _config([_event("F030", "step", 18, 2, 9.0)], 30)

    result, truth = inject_faults(chunk, config)

    # Event is [18, 20): the last point 19 is in the previous chunk, not here.
    assert truth == []
    assert result is chunk


def test_single_point_event_touches_one_sample() -> None:
    """``duration_points=1`` affects exactly one sample."""
    chunk = _chunk(0, [1.0] * 4)
    config = _config([_event("F040", "spike", 2, 1, 9.0)], 4)

    result, truth = inject_faults(chunk, config)

    assert result.vibration == pytest.approx([1.0, 1.0, 10.0, 1.0])
    assert truth[0].duration_points == 1


def test_first_and_last_sample_of_the_run_can_be_injected() -> None:
    """``start_seq=0`` and ``end_seq_exclusive=total_points`` are legal."""
    config = _scenario([_event("F050", "step", 3, 1, 1.0)], 4)
    result, truth = inject_faults(simulate_chunk(config, 0, 4), config)
    assert result.vibration[3] == pytest.approx(1.5)
    assert truth[0].end_seq_exclusive == 4


def test_temperature_and_clock_are_unchanged() -> None:
    """Section 7.2 authorises vibration injection only."""
    chunk = _chunk(0, [1.0] * 8)
    config = _config([_event("F060", "burst", 1, 6, 3.0)], 8)

    result, _ = inject_faults(chunk, config)

    assert np.array_equal(result.temperature, chunk.temperature)
    assert np.array_equal(result.seq, chunk.seq)
    assert np.array_equal(result.timestamp_ns, chunk.timestamp_ns)
    assert result.vibration.dtype == np.float64


def test_input_chunk_is_not_mutated() -> None:
    """Injection copies on write; the caller's chunk keeps its values."""
    chunk = _chunk(0, [1.0] * 6)
    before = chunk.vibration.copy()

    result, _ = inject_faults(chunk, _config([_event("F070", "step", 1, 2, 4.0)], 6))

    assert np.array_equal(chunk.vibration, before)
    assert result is not chunk
    assert result.vibration[1] == pytest.approx(5.0)


def test_injection_is_repeatable_and_consumes_no_random_numbers() -> None:
    """The three waveforms are deterministic; ``rng`` is signature-only."""
    chunk = _chunk(0, [0.0] * 10)
    config = _config([_event("F080", "burst", 2, 6, 1.5)], 10)
    rng = np.random.default_rng(7)
    state = rng.bit_generator.state

    first, _ = inject_faults(chunk, config, rng)
    second, _ = inject_faults(chunk, config, rng)

    assert np.array_equal(first.vibration, second.vibration)
    assert rng.bit_generator.state == state


def test_truth_record_matches_section_5_3() -> None:
    """The exported record has exactly the frozen key set."""
    config = _config([_event("F090", "step", 1, 2, 2.0)], 8)
    _, truth = inject_faults(_chunk(0, [0.0] * 8), config)

    assert truth[0].to_dict() == {
        "event_id": "F090",
        "device_id": _DEVICE,
        "type": "step",
        "start_seq": 1,
        "end_seq_exclusive": 3,
        "amplitude": 2.0,
    }


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (_event("F100", "ramp", 0, 2, 1.0), "unsupported fault type"),
        (_event("F100", "step", 0, 0, 1.0), "duration_points must be positive"),
        (_event("F100", "step", -1, 2, 1.0), "start_seq must be non-negative"),
        (_event("F100", "step", 0, 2, float("nan")), "amplitude must be finite"),
    ],
)
def test_invalid_event_is_rejected(event: dict[str, object], message: str) -> None:
    """Malformed events fail loudly instead of silently injecting nothing."""
    with pytest.raises(ValueError, match=message):
        inject_faults(_chunk(0, [0.0] * 6), _config([event], 6))


def test_duplicate_event_id_is_rejected() -> None:
    """One identifier per event: the truth file is keyed by ``event_id``."""
    config = _config(
        [
            _event("F110", "step", 0, 2, 1.0),
            _event("F110", "step", 3, 2, 1.0),
        ],
        6,
    )
    with pytest.raises(ValueError, match="duplicated"):
        inject_faults(_chunk(0, [0.0] * 6), config)


def test_event_ending_past_total_points_is_rejected() -> None:
    """An event outside the run would poison the detection denominator."""
    config = _config([_event("F120", "step", 4, 4, 1.0)], 6)
    with pytest.raises(ValueError, match="outside scenario.total_points"):
        inject_faults(_chunk(0, [0.0] * 6), config)


def test_stochastic_probability_is_not_implemented() -> None:
    """The random schedule is not in the V1.0 contract; refuse instead of guess."""
    config = _config([], 6, mode="stochastic", probability=0.1)
    with pytest.raises(NotImplementedError):
        inject_faults(_chunk(0, [0.0] * 6), config)


def test_burst_below_three_points_is_rejected() -> None:
    """A ruling on PR #27: short bursts are refused, not injected as silence.

    For ``D <= 2`` the Hann window ``0.5 - 0.5*cos(2*pi*k/(D-1))`` is
    identically zero (and ``D == 1`` additionally sits on the carrier zero
    crossing), so a "successful" injection would leave the signal untouched
    while the truth record still counts towards the detection denominator.
    """
    for duration in (1, 2):
        config = _config([_event(f"FB0{duration}", "burst", 2, duration, 5.0)], 6)
        with pytest.raises(ValueError, match="duration_points >= 3"):
            inject_faults(_chunk(0, [0.0] * 6), config)
    # One-point spike/step stay legal: only burst carries the restriction.
    config = _config([_event("FB10", "spike", 2, 1, 5.0)], 6)
    result, truth = inject_faults(_chunk(0, [0.0] * 6), config)
    assert truth[0].duration_points == 1
    assert result.vibration[2] == pytest.approx(5.0)


def test_rng_state_is_unchanged_by_injection() -> None:
    """``rng`` is accepted but never consumed: its state must be bit-identical.

    This is the PR #27 review requirement; it guarantees that a chunk can be
    regenerated (retry, reordering, different shard size) without the fault
    injection perturbing the shared random stream.
    """
    config = _config(
        [
            _event("FR01", "spike", 1, 4, 2.0),
            _event("FR02", "burst", 4, 8, 3.0),
        ],
        12,
    )
    rng = np.random.default_rng(7)
    state_before = copy.deepcopy(rng.bit_generator.state)
    first, _ = inject_faults(_chunk(0, [1.0] * 12), config, rng=rng)
    assert rng.bit_generator.state == state_before
    # A fresh generator with the same seed must reproduce the same waveform.
    second, _ = inject_faults(
        _chunk(0, [1.0] * 12), config, rng=np.random.default_rng(7)
    )
    assert np.array_equal(first.vibration, second.vibration)


def _loader_scenario(events: tuple[ConfigFaultEvent, ...]) -> ScenarioConfig:
    """Return a real ``ScenarioConfig`` as the C3 loader would build it."""
    return ScenarioConfig(
        schema_version="1.0",
        scenario=ScenarioBlock(
            scenario_id="loader_compat_001",
            description="constructed with the frozen dataclasses",
            seed=42,
            start_time_utc=dt.datetime(2026, 10, 14, tzinfo=dt.timezone.utc),
            device_id=_DEVICE,
            sample_rate_hz=_RATE,
            total_points=8,
            chunk_points=8,
        ),
        signals=SignalsBlock(
            vibration=SignalConfig(
                name="vibration",
                baseline=1.0,
                degradation_rate_per_sample=0.0,
                noise_std=0.0,
                unit="g",
            ),
            temperature=SignalConfig(
                name="temperature",
                baseline=35.0,
                degradation_rate_per_sample=0.0,
                noise_std=0.0,
                unit="degC",
            ),
        ),
        fault_generation=FaultGenerationConfig(
            mode="manual",
            stochastic_probability_per_window=0.0,
            events=events,
        ),
    )


def test_accepts_scenario_config_from_loader() -> None:
    """The injector reads the C3 ``ScenarioConfig`` attributes directly."""
    scenario = _loader_scenario(
        (ConfigFaultEvent("FL01", "step", 5, 3, 2.0),),
    )
    chunk = simulate_chunk(scenario, 0, 8)
    result, truth = inject_faults(chunk, scenario)
    assert truth[0].event_id == "FL01"
    assert truth[0].start_seq == 5
    assert truth[0].end_seq_exclusive == 8
    assert result.vibration[5:8] == pytest.approx([3.0, 3.0, 3.0])
    # The same loader type propagates the short-burst rejection.
    short = _loader_scenario(
        (ConfigFaultEvent("FL02", "burst", 0, 2, 5.0),),
    )
    with pytest.raises(ValueError, match="duration_points >= 3"):
        inject_faults(_chunk(0, [0.0] * 8), short)
