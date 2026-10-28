"""pred_exp_h10_k1 — pred_exp_k1 predicting 10 s ahead."""

from __future__ import annotations

from fan_control.target_control.pred_exp_k1 import PredExpControl


class PredExpH10K1Control(PredExpControl):
    """pred_exp_k1 with HORIZON_SEC 10."""

    HORIZON_SEC = 10.0
