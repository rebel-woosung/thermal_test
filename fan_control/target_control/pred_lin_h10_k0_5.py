"""pred_lin_h10_k0_5 — pred_lin_k1 predicting 10 s ahead and stepping 0.5 % per °C of predicted error."""

from __future__ import annotations

from fan_control.target_control.pred_lin_k1 import PredLinControl


class PredLinH10K05Control(PredLinControl):
    """pred_lin_k1 with HORIZON_SEC 10 and GAIN_PCT_PER_C 0.5."""

    HORIZON_SEC = 10.0
    GAIN_PCT_PER_C = 0.5
