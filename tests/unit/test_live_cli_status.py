"""Windows service-status readers must not terminate a healthy capture process."""

from __future__ import annotations

import json
import os
import threading

import pytest

from g15.live.__main__ import _write_json


@pytest.mark.skipif(os.name != "nt", reason="Windows shared-delete lock semantics")
def test_cli_state_publication_waits_for_a_real_transient_windows_reader(tmp_path):
    target = tmp_path / "service-status.json"
    _write_json(target, {"status": "starting"})
    errors = []
    finished = threading.Event()

    def publish():
        try:
            _write_json(target, {"status": "running", "processed_samples": 256})
        except BaseException as exc:  # preserve the writer's actual failure
            errors.append(exc)
        finally:
            finished.set()

    # Ordinary Windows readers do not share DELETE. Replacing their open
    # pathname must wait for the reader to close, not kill the live controller.
    with target.open("rb") as reader:
        writer = threading.Thread(target=publish)
        writer.start()
        assert not finished.wait(0.05)
        assert json.load(reader) == {"status": "starting"}
    writer.join(timeout=3)
    assert not writer.is_alive()
    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "status": "running",
        "processed_samples": 256,
    }
