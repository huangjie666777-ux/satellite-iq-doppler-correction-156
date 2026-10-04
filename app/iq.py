"""SigMF validation, Doppler correction, diagnostics and ZIP delivery."""
from __future__ import annotations

import io
import json
import math
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .doppler import DopplerNode, interpolate_frequency

MAX_SAMPLES = 1 << 20
MAX_DURATION_S = 60.0
MIN_SAMPLE_RATE_HZ = 1000.0
MAX_SAMPLE_RATE_HZ = 200_000.0
SAMPLE_BYTES = 8
WINDOW_SIZE = 1024
CHUNK_SIZE = 65_536


class IQError(ValueError):
    pass


@dataclass(frozen=True)
class SigMFRecording:
    start_time: datetime
    center_frequency_hz: float
    sample_rate_hz: float
    samples: np.ndarray


def _reject_nan_constant(value: str) -> None:
    if value.strip().lower() in {"nan", "infinity", "+infinity", "-infinity"}:
        raise IQError("metadata must not contain NaN or Infinity")


def _require_object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise IQError(f"{name} must be an object")
    return value


def parse_sigmf(metadata_bytes: bytes, sample_bytes: bytes) -> SigMFRecording:
    if not metadata_bytes:
        raise IQError("SigMF metadata is empty")
    try:
        text = metadata_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IQError("SigMF metadata must be UTF-8 JSON") from exc
    try:
        meta = json.loads(text, parse_constant=_reject_nan_constant)
    except json.JSONDecodeError as exc:
        raise IQError(f"invalid SigMF JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise IQError("SigMF metadata root must be an object")

    allowed = {"global", "captures", "annotations"}
    if set(meta) != allowed:
        raise IQError("metadata must contain exactly global, captures and annotations")
    glob = _require_object(meta["global"], "global")
    if set(glob) != {"core:datatype", "core:sample_rate"}:
        raise IQError("global may contain only core:datatype and core:sample_rate")
    if glob["core:datatype"] != "cf32_le":
        raise IQError("only single-channel little-endian complex float cf32_le is supported")
    sample_rate = glob["core:sample_rate"]
    if type(sample_rate) not in (int, float) or isinstance(sample_rate, bool):
        raise IQError("core:sample_rate must be a number")
    sample_rate = float(sample_rate)
    if not math.isfinite(sample_rate):
        raise IQError("core:sample_rate must be finite")
    if not (MIN_SAMPLE_RATE_HZ <= sample_rate <= MAX_SAMPLE_RATE_HZ):
        raise IQError("sample rate must be in [1000 Hz, 200000 Hz]")

    captures = meta["captures"]
    if not isinstance(captures, list) or len(captures) != 1:
        raise IQError("exactly one capture is required")
    capture = _require_object(captures[0], "capture")
    if set(capture) != {"core:datetime", "core:frequency", "core:sample_start"}:
        raise IQError("capture may contain only datetime, frequency and sample_start")
    if capture["core:sample_start"] != 0:
        raise IQError("capture must start at sample 0")
    center_frequency = capture["core:frequency"]
    if type(center_frequency) not in (int, float) or isinstance(center_frequency, bool):
        raise IQError("core:frequency must be a number")
    center_frequency = float(center_frequency)
    if not math.isfinite(center_frequency) or center_frequency <= 0.0:
        raise IQError("center frequency must be positive and finite")

    start_text = capture["core:datetime"]
    if not isinstance(start_text, str) or not start_text.endswith("Z"):
        raise IQError("capture UTC time must be an ISO-8601 string ending in Z")
    try:
        start = datetime.fromisoformat(start_text[:-1] + "+00:00")
    except ValueError as exc:
        raise IQError("invalid capture UTC time") from exc

    annotations = meta["annotations"]
    if not isinstance(annotations, list) or annotations:
        raise IQError("annotations must be an empty list")

    if not sample_bytes:
        raise IQError("recording samples are empty")
    if len(sample_bytes) % SAMPLE_BYTES:
        raise IQError("sample file is truncated or contains trailing non-sample bytes")
    sample_count = len(sample_bytes) // SAMPLE_BYTES
    if sample_count > MAX_SAMPLES:
        raise IQError("recording exceeds 2^20 complex samples")
    duration = sample_count / sample_rate
    if duration > MAX_DURATION_S + 1e-12:
        raise IQError("recording exceeds 60 seconds")

    raw = np.frombuffer(sample_bytes, dtype="<f4")
    if not np.all(np.isfinite(raw)):
        raise IQError("all I/Q samples must be finite")
    samples = raw.view(np.complex64)
    return SigMFRecording(start.astimezone(timezone.utc), center_frequency,
                          sample_rate, np.array(samples, copy=True))


def check_nyquist(nodes: list[DopplerNode], sample_rate_hz: float) -> None:
    for node in nodes:
        if abs(node.baseband_frequency_hz) >= sample_rate_hz / 2.0:
            raise IQError(
                f"baseband frequency {node.baseband_frequency_hz:.3f} Hz at "
                f"{node.time.isoformat()} is outside (-fs/2, fs/2)")


def correct_samples(samples: np.ndarray, nodes: list[DopplerNode],
                    sample_rate_hz: float) -> np.ndarray:
    """Multiply by exp(-j*2*pi*integral(freq dt)); phase starts at zero."""
    count = samples.size
    corrected = np.empty_like(samples)
    offsets = np.arange(count, dtype=np.float64) / sample_rate_hz
    frequencies = interpolate_frequency(nodes, offsets)
    phase_offset = 0.0
    for start in range(0, count, CHUNK_SIZE):
        stop = min(start + CHUNK_SIZE, count)
        freq = frequencies[start:stop]
        phase = np.zeros(stop - start, dtype=np.float64)
        if phase.size > 1:
            increments = (freq[:-1] + freq[1:]) * 0.5 / sample_rate_hz
            phase[1:] = np.cumsum(increments)
        phase += phase_offset
        corrected[start:stop] = samples[start:stop] * np.exp(
            -1j * (2.0 * math.pi * phase), dtype=np.complex64)
        if stop < count:
            phase_offset += float(np.sum(
                (frequencies[start:stop - 1] + frequencies[start + 1:stop])
                * 0.5 / sample_rate_hz))
    return corrected


def _window_diagnostics(before: np.ndarray, after: np.ndarray,
                        sample_rate_hz: float) -> list[dict]:
    window = np.hanning(WINDOW_SIZE).astype(np.float32)
    freqs = np.fft.fftshift(np.fft.fftfreq(WINDOW_SIZE, 1.0 / sample_rate_hz))
    records = []
    for index, start in enumerate(range(0, before.size - WINDOW_SIZE + 1,
                                        WINDOW_SIZE)):
        x0 = before[start:start + WINDOW_SIZE] * window
        x1 = after[start:start + WINDOW_SIZE] * window

        def peak_and_power(x: np.ndarray) -> tuple[float, float]:
            spectrum = np.fft.fftshift(np.fft.fft(x))
            peak = float(freqs[int(np.argmax(np.abs(spectrum)))])
            return peak, float(np.mean(np.abs(x) ** 2, dtype=np.float64))

        peak0, power0 = peak_and_power(x0)
        peak1, power1 = peak_and_power(x1)
        records.append({
            "window_index": index,
            "sample_start": start,
            "sample_count": WINDOW_SIZE,
            "window": "Hann, non-overlapping, no power-normalization",
            "before_correction": {
                "peak_frequency_hz": peak0,
                "mean_square_power": power0,
            },
            "after_correction": {
                "peak_frequency_hz": peak1,
                "mean_square_power": power1,
            },
        })
    return records


def build_delivery(metadata_bytes: bytes, recording: SigMFRecording,
                   corrected: np.ndarray, nodes: list[DopplerNode],
                   transmit_hz: float) -> bytes:
    try:
        metadata = json.loads(metadata_bytes.decode("utf-8"),
                             parse_constant=_reject_nan_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IQError("invalid source SigMF metadata") from exc
    metadata["captures"][0]["core:frequency"] = transmit_hz
    diagnostics = {
        "units": {
            "time": "UTC / seconds relative to recording start",
            "frequency": "Hz",
            "radial_velocity": "m/s, positive means moving away",
            "sample_rate": "complex samples/s",
            "mean_square_power": "mean |Hann*x|^2 in linear amplitude units",
        },
        "sample_count": int(corrected.size),
        "sample_rate_hz": recording.sample_rate_hz,
        "recording_start_utc": recording.start_time.isoformat().replace(
            "+00:00", "Z"),
        "transmit_frequency_hz": transmit_hz,
        "source_center_frequency_hz": recording.center_frequency_hz,
        "corrected_center_frequency_hz": transmit_hz,
        "baseband_frequency_hz": (
            "transmit_frequency_hz - source_center_frequency_hz - "
            "transmit_frequency_hz * radial_velocity_m_s / c"),
        "doppler_nodes": [{
            "time_utc": n.time.isoformat().replace("+00:00", "Z"),
            "offset_s": (n.time - recording.start_time).total_seconds(),
            "radial_velocity_m_s": n.radial_velocity_m_s,
            "baseband_frequency_hz": n.baseband_frequency_hz,
        } for n in nodes],
        "windows": _window_diagnostics(recording.samples, corrected,
                                       recording.sample_rate_hz),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("corrected.sigmf-meta",
                    json.dumps(metadata, ensure_ascii=False, indent=2))
        zf.writestr("corrected.sigmf-data",
                    corrected.astype(np.complex64, copy=False).tobytes())
        zf.writestr("diagnostics.json",
                    json.dumps(diagnostics, ensure_ascii=False, indent=2,
                               allow_nan=False))
    return buf.getvalue()
