"""Entry rules shared by the independent Paper wallet and the live coordinator."""
import math


KEY = "MAIN_FIRST_STRONG"
LABEL = "主账户·每局首个强信号"
SCORE_FLOOR = 0.65
BUDGET = 5.0
SLOTS = 2


def qualified(signal):
    return (signal.side in ("Up", "Down") and math.isfinite(signal.confidence)
            and SCORE_FLOOR <= signal.confidence <= 1)
