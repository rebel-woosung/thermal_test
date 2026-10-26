"""pi_fast — pi with a higher gain: reaches the target sooner at the cost of a little more overshoot."""

from __future__ import annotations

from fan_control.target_control.pi import PiControl


class PiFastControl(PiControl):
    """pi stepping 1.4× harder toward zero predicted error."""

    GAIN = 0.7
