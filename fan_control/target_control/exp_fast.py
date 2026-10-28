"""exp_fast — exp on a shorter TAU_CL_SEC: reaches the target sooner at the cost of a little more overshoot."""

from __future__ import annotations

from fan_control.target_control.exp import ExpControl


class ExpFastControl(ExpControl):
    """exp approaching the target along a 20 s exponential instead of 30 s."""

    TAU_CL_SEC = 20.0
