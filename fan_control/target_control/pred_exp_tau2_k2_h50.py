"""pred_exp_tau2_k2_h50 — pred_exp_tau2_k2_h45 predicting 50 s ahead."""

from __future__ import annotations

from fan_control.target_control.pred_exp_tau2_k2_h45 import PredExpTau2K2H45Control


class PredExpTau2K2H50Control(PredExpTau2K2H45Control):
    """pred_exp_tau2_k2_h45 with HORIZON_SEC 50."""

    HORIZON_SEC = 50.0
