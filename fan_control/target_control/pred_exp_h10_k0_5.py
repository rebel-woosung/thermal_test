"""pred_exp_h10_k0_5 — pred_exp_k1 predicting 10 s ahead and stepping 0.5 % per °C of predicted error."""

from __future__ import annotations

from fan_control.target_control.pred_exp_k1 import PredExpControl


class PredExpH10K05Control(PredExpControl):
    """pred_exp_k1 with HORIZON_SEC 10 and GAIN_PCT_PER_C 0.5."""

    HORIZON_SEC = 10.0
    GAIN_PCT_PER_C = 0.5
