"""pred_exp_tau3_k2 — pred_exp_k2 on TAU_SEC at 10 / 20 % re-fit from the CR13 fan-target run."""

from __future__ import annotations

from fan_control.target_control.pred_exp_k2 import PredExpK2Control


class PredExpTau3K2Control(PredExpK2Control):
    """pred_exp_k2 with TAU_SEC 60 s at 10 % and 40 s at 20 %; 30 % and up unchanged."""

    TAU_SEC: tuple[tuple[int, float], ...] = (
        (10, 60.0), (20, 40.0), (30, 73.0), (40, 49.0), (50, 38.0), (60, 29.0),
        (70, 25.0), (80, 21.0), (90, 20.0), (100, 20.0),
    )
