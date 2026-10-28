"""pred_lin_h10_k1 — pred_lin_k1 predicting 10 s ahead."""

from __future__ import annotations

from fan_control.target_control.pred_lin_k1 import PredLinControl


class PredLinH10K1Control(PredLinControl):
    """pred_lin_k1 with HORIZON_SEC 10."""

    HORIZON_SEC = 10.0
