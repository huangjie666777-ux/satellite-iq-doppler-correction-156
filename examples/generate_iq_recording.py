"""Generate the reproducible cf32_le Doppler-shifted carrier example."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.doppler import doppler_nodes, interpolate_frequency
from app.main import _compute
from app.schemas import ForecastRequest

OUT_DIR = ROOT / "examples" / "iq"
START = datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc)
TRANSMIT_HZ = 1_000_000.0
CENTER_HZ = 995_000.0
SAMPLE_RATE_HZ = 200_000.0
SAMPLE_COUNT = 400_000


def main() -> None:
    req = ForecastRequest(**json.loads((ROOT / "examples/request.json").read_text()))
    sat, tle, site, interval = next(
        item for item in _compute(req)
        if item[0].id == "ISS" and item[2].station_id == "BEIJING")
    end = START + timedelta(seconds=SAMPLE_COUNT / SAMPLE_RATE_HZ)
    if not (interval.start <= START and end <= interval.end):
        raise RuntimeError("example window is not inside the selected pass")
    nodes = doppler_nodes(tle.satrec, site, START,
                          SAMPLE_COUNT / SAMPLE_RATE_HZ,
                          TRANSMIT_HZ, CENTER_HZ)
    offsets = np.arange(SAMPLE_COUNT, dtype=np.float64) / SAMPLE_RATE_HZ
    frequency = interpolate_frequency(nodes, offsets)
    phase = np.zeros(SAMPLE_COUNT, dtype=np.float64)
    phase[1:] = np.cumsum(0.5 * (frequency[:-1] + frequency[1:])
                          / SAMPLE_RATE_HZ)
    samples = np.exp(1j * 2.0 * np.pi * phase).astype(np.complex64)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "example.sigmf-data").write_bytes(samples.tobytes())
    metadata = {
        "global": {
            "core:datatype": "cf32_le",
            "core:sample_rate": SAMPLE_RATE_HZ,
        },
        "captures": [{
            "core:datetime": START.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "core:frequency": CENTER_HZ,
            "core:sample_start": 0,
        }],
        "annotations": [],
    }
    (OUT_DIR / "example.sigmf-meta").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
