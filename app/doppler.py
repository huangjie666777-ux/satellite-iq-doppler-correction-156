"""Radial velocity and piecewise-linear Doppler frequency profiles."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from .coords import SPEED_OF_LIGHT, teme_to_ecef, teme_vel_to_ecef
from .passes import PassInterval, Site, _propagate


@dataclass(frozen=True)
class DopplerNode:
    time: datetime
    radial_velocity_m_s: float
    baseband_frequency_hz: float


def radial_velocity_m_s(satrec, site: Site, t: datetime) -> float:
    """Station-to-satellite LOS velocity; positive means moving away."""
    r_teme, v_teme = _propagate(satrec, t)
    sat_ecef = teme_to_ecef(r_teme, t)
    vel_ecef = teme_vel_to_ecef(r_teme, v_teme, t)
    dx = sat_ecef[0] - site.ecef[0]
    dy = sat_ecef[1] - site.ecef[1]
    dz = sat_ecef[2] - site.ecef[2]
    rng = math.sqrt(dx * dx + dy * dy + dz * dz)
    return ((dx * vel_ecef[0] + dy * vel_ecef[1] + dz * vel_ecef[2])
            * 1000.0 / rng)


def ensure_recording_interval(interval: PassInterval, start: datetime,
                              duration_s: float) -> None:
    end = start + timedelta(seconds=duration_s)
    if not (interval.start <= start and end <= interval.end):
        raise ValueError(
            f"recording {start.isoformat()}..{end.isoformat()} is not fully "
            f"inside visibility interval {interval.start.isoformat()}.."
            f"{interval.end.isoformat()}")


def doppler_nodes(satrec, site: Site, start: datetime, duration_s: float,
                  transmit_hz: float, center_hz: float) -> list[DopplerNode]:
    """1 s nodes, always including the recording endpoint."""
    whole = int(math.floor(duration_s + 1e-9))
    offsets = [float(i) for i in range(whole + 1)]
    if abs(offsets[-1] - duration_s) > 1e-9:
        offsets.append(float(duration_s))
    nodes = []
    for offset in offsets:
        t = start + timedelta(seconds=offset)
        velocity = radial_velocity_m_s(satrec, site, t)
        frequency = (transmit_hz - center_hz
                     - transmit_hz * velocity / SPEED_OF_LIGHT)
        nodes.append(DopplerNode(t, velocity, frequency))
    return nodes


def interpolate_frequency(nodes: list[DopplerNode], offsets_s: np.ndarray
                          ) -> np.ndarray:
    xs = np.asarray([(n.time - nodes[0].time).total_seconds() for n in nodes],
                    dtype=np.float64)
    ys = np.asarray([n.baseband_frequency_hz for n in nodes],
                    dtype=np.float64)
    return np.interp(offsets_s, xs, ys)
