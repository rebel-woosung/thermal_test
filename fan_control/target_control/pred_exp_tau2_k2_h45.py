"""pred_exp_tau2_k2_h45 — pred_exp_k1 on TAU_SEC re-fit from the CR03 duty sweep, stepping 2 % per °C of the error 45 s ahead."""

from __future__ import annotations

from fan_control.target_control.pred_exp_k1 import PredExpControl


class PredExpTau2K2H45Control(PredExpControl):
    """pred_exp_k1 with the sweep-fit TAU_SEC, GAIN_PCT_PER_C 2 and HORIZON_SEC 45."""

    TAU_SEC: tuple[tuple[int, float], ...] = (
        (20, 115.0), (30, 83.0), (40, 59.0), (50, 48.0), (60, 38.0),
        (70, 36.0), (80, 30.0), (90, 28.0), (100, 26.0),
    )
    GAIN_PCT_PER_C = 2.0
    HORIZON_SEC = 45.0
