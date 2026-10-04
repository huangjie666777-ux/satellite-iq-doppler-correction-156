import io
import json
import zipfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app.main import app, playback

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
META_PATH = ROOT / "examples/iq/example.sigmf-meta"
DATA_PATH = ROOT / "examples/iq/example.sigmf-data"


def forecast_text():
    return (ROOT / "examples/request.json").read_text()


def post_iq(metadata=META_PATH.read_bytes(), samples=DATA_PATH.read_bytes(),
            **overrides):
    data = {
        "forecast": forecast_text(),
        "satellite_id": "ISS",
        "station_id": "BEIJING",
        "transmit_frequency_hz": "1000000",
    }
    data.update(overrides)
    return client.post(
        "/api/iq/correct", data=data,
        files={"metadata": ("example.sigmf-meta", metadata,
                            "application/json"),
               "samples": ("example.sigmf-data", samples,
                           "application/octet-stream")})


def test_correct_doppler_delivery():
    r = post_iq()
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    assert zf.namelist() == [
        "corrected.sigmf-meta", "corrected.sigmf-data", "diagnostics.json"]
    meta = json.loads(zf.read("corrected.sigmf-meta"))
    assert meta["global"]["core:datatype"] == "cf32_le"
    assert meta["global"]["core:sample_rate"] == 200000
    assert meta["captures"][0]["core:frequency"] == 1000000
    assert meta["captures"][0]["core:datetime"] == "2024-01-01T02:30:00Z"
    out = np.frombuffer(zf.read("corrected.sigmf-data"), dtype="<f4")
    assert out.size == 800000
    diag = json.loads(zf.read("diagnostics.json"))
    assert diag["sample_count"] == 400000
    assert len(diag["windows"]) == 390
    first = diag["windows"][0]
    assert first["before_correction"]["peak_frequency_hz"] != 0.0
    assert abs(first["after_correction"]["peak_frequency_hz"]) < 500.0
    before_power = first["before_correction"]["mean_square_power"]
    after_power = first["after_correction"]["mean_square_power"]
    assert np.isclose(after_power, before_power, rtol=2e-7, atol=1e-9)
    assert len(diag["doppler_nodes"]) == 3


def test_source_files_are_not_modified():
    meta_before = META_PATH.read_bytes()
    data_before = DATA_PATH.read_bytes()
    post_iq()
    assert META_PATH.read_bytes() == meta_before
    assert DATA_PATH.read_bytes() == data_before


def test_rejects_empty_truncated_nonfinite_and_bad_metadata():
    assert post_iq(samples=b"").status_code == 422
    assert post_iq(samples=DATA_PATH.read_bytes()[:-1]).status_code == 422
    bad = bytearray(DATA_PATH.read_bytes())
    bad[0:4] = np.asarray([np.inf], dtype="<f4").tobytes()
    assert post_iq(samples=bytes(bad)).status_code == 422
    meta = json.loads(META_PATH.read_text())
    meta["global"]["core:datatype"] = "ci16_le"
    assert post_iq(metadata=json.dumps(meta).encode()).status_code == 422


def test_rejects_recording_outside_selected_interval():
    meta = json.loads(META_PATH.read_text())
    meta["captures"][0]["core:datetime"] = "2024-01-01T02:00:00Z"
    assert post_iq(metadata=json.dumps(meta).encode()).status_code == 422


def test_rejects_nyquist_violation_and_bad_frequency():
    meta = json.loads(META_PATH.read_text())
    meta["captures"][0]["core:frequency"] = 1_200_000
    assert post_iq(metadata=json.dumps(meta).encode()).status_code == 422
    assert post_iq(transmit_frequency_hz="0").status_code == 422


def test_empty_target_playback_fails_and_releases_controller():
    bad_plan = {
        "satellite_id": "ISS", "station_id": "BEIJING",
        "interval_index": 0,
        "interval_start": "2024-01-01T02:29:41Z",
        "interval_end": "2024-01-01T02:29:42Z",
        "preset_seconds": 1.0, "homing_seconds": 1.0,
        "current_position": {"az_deg": 0.0, "el_deg": 5.0},
        "home_position": {"az_deg": 0.0, "el_deg": 5.0},
        "targets": [],
        "total_az_travel_deg": 0.0, "notes": [],
    }
    r = client.post("/api/playback", json={"plan": bad_plan, "port": 1})
    assert r.status_code == 200
    assert playback.status()["state"] == "failed"
    assert "invalid playback plan" in playback.status()["detail"]
    second = client.post("/api/playback", json={"plan": bad_plan, "port": 1})
    assert second.status_code == 200
    assert playback.status()["state"] == "failed"
