"""pred_exp_k2 — pred_exp_k1 stepping 2 % per °C of predicted error."""

from __future__ import annotations

from fan_control.target_control.pred_exp_k1 import PredExpControl


class PredExpK2Control(PredExpControl):
    """pred_exp_k1 with GAIN_PCT_PER_C 2."""

    GAIN_PCT_PER_C = 2.0
