"""A/B testing services (E09).

W35 adds deterministic per-recipient variant assignment + the launch
state-machine helpers. Significance computation + winner promotion live
in a sibling module added by W36.
"""

from app.ab_testing.assignment import assign_variant, pick_variant_index
from app.ab_testing.significance import EvalResult, evaluate_test

__all__ = ["assign_variant", "pick_variant_index", "evaluate_test", "EvalResult"]
