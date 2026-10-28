"""pred_lin_k2 — pred_lin_k1 stepping 2 % per °C of predicted error."""

from __future__ import annotations

from fan_control.target_control.pred_lin_k1 import PredLinControl


class PredLinK2Control(PredLinControl):
    """pred_lin_k1 with GAIN_PCT_PER_C 2."""

    GAIN_PCT_PER_C = 2.0
