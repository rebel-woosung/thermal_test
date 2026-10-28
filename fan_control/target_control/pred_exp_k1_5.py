"""pred_exp_k1_5 — pred_exp_k1 stepping 1.5 % per °C of predicted error."""

from __future__ import annotations

from fan_control.target_control.pred_exp_k1 import PredExpControl


class PredExpK15Control(PredExpControl):
    """pred_exp_k1 with GAIN_PCT_PER_C 1.5."""

    GAIN_PCT_PER_C = 1.5
