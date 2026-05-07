"""
Tool: post_math_dispatch
POST_MATH_DISPATCH band classifier — v2 architecture's deterministic router.

Runs immediately after compute_underwriting_delta and classifies the deal
into one of three bands based on the ABSOLUTE observed IRR level (not the
delta magnitude). The band determines the downstream control flow:

    Green  (IRR > 10%)        -> auto_render, no checkpoint, straight to synthesis
    Yellow (7.5% <= IRR <= 10%) -> advisory, watch-list flag in synthesis
    Red    (IRR < 7.5%)       -> requires_human_review, checkpoint before synthesis

The bands are tied to deal quality, not deal movement. A deal can move 3pt
and remain green; another can move 1pt and cross into red. v2 routes on
"is the deal good now," not "did the deal move a lot."

This is pure Python. No network. The LLM never calls or interprets it —
the band is the load-bearing routing primitive.
"""
from __future__ import annotations

import json
from typing import Optional


# Band thresholds — absolute observed IRR, decimal form.
# Green / Yellow boundary: 10% IRR (typical institutional CRE hurdle)
# Yellow / Red boundary: 7.5% IRR (below which the deal is genuinely bad)
GREEN_THRESHOLD = 0.10
RED_THRESHOLD = 0.075

# Edge labels — consumed by the synthesis prompt branch selector.
EDGE_AUTO_RENDER = "auto_render"
EDGE_ADVISORY = "advisory"
EDGE_REQUIRES_HUMAN_REVIEW = "requires_human_review"


def _classify_band(irr_observed_pct: float) -> tuple[str, str, str]:
    """Return (band, edge_label, dispatch_reason) for an observed IRR.

    Boundary semantics:
      irr > 0.10           -> green
      0.075 <= irr <= 0.10 -> yellow  (10.0% itself is yellow, not green)
      irr < 0.075          -> red     (7.5% itself is yellow, not red)
    """
    if irr_observed_pct > GREEN_THRESHOLD:
        return ("green", EDGE_AUTO_RENDER,
                f"observed IRR {irr_observed_pct * 100:.2f}% exceeds {GREEN_THRESHOLD * 100:.1f}% green hurdle")
    if irr_observed_pct >= RED_THRESHOLD:
        return ("yellow", EDGE_ADVISORY,
                f"observed IRR {irr_observed_pct * 100:.2f}% sits in watch band "
                f"({RED_THRESHOLD * 100:.1f}%-{GREEN_THRESHOLD * 100:.1f}%)")
    return ("red", EDGE_REQUIRES_HUMAN_REVIEW,
            f"observed IRR {irr_observed_pct * 100:.2f}% below {RED_THRESHOLD * 100:.1f}% red threshold")


def post_math_dispatch(underwriting_result: dict) -> str:
    """Classify a deal into green / yellow / red based on observed IRR.

    Args:
        underwriting_result: parsed JSON dict from compute_underwriting_delta.
                             Must contain 'irr_observed_pct' (decimal).

    Returns:
        JSON string with:
          - band                   ("green" | "yellow" | "red")
          - edge_label             ("auto_render" | "advisory" | "requires_human_review")
          - irr_observed_pct       (echoed input, decimal)
          - irr_assumed_pct        (echoed for context)
          - irr_delta_pct          (echoed for context)
          - thresholds             ({green: 0.10, red: 0.075})
          - dispatch_reason        (one-line human-readable explanation)
          - error / message        (only on failure)
    """
    if not isinstance(underwriting_result, dict):
        return json.dumps({
            "band": "red",
            "edge_label": EDGE_REQUIRES_HUMAN_REVIEW,
            "error": "invalid_input",
            "message": "underwriting_result must be a dict",
            "dispatch_reason": "dispatch could not classify; defaulting to red for safety",
        })

    if underwriting_result.get("error"):
        return json.dumps({
            "band": "red",
            "edge_label": EDGE_REQUIRES_HUMAN_REVIEW,
            "error": "underwriting_error",
            "message": underwriting_result.get("message", "underwriting tool returned error envelope"),
            "dispatch_reason": "underwriting math failed; defaulting to red for safety",
        })

    irr = underwriting_result.get("irr_observed_pct")
    if irr is None or not isinstance(irr, (int, float)):
        return json.dumps({
            "band": "red",
            "edge_label": EDGE_REQUIRES_HUMAN_REVIEW,
            "error": "missing_irr",
            "message": "underwriting_result.irr_observed_pct is null or missing",
            "dispatch_reason": "no observed IRR to classify; defaulting to red for safety",
        })

    band, edge_label, reason = _classify_band(float(irr))

    return json.dumps({
        "band": band,
        "edge_label": edge_label,
        "irr_observed_pct": underwriting_result.get("irr_observed_pct"),
        "irr_assumed_pct": underwriting_result.get("irr_assumed_pct"),
        "irr_delta_pct": underwriting_result.get("irr_delta_pct"),
        "thresholds": {
            "green_above": GREEN_THRESHOLD,
            "red_below": RED_THRESHOLD,
        },
        "dispatch_reason": reason,
    }, indent=2)


# ─────────────────────────────────────────────────────────
# Standalone tests — run `python tools/dispatch.py`
# Boundary cases pinned to the contract: 10.0001%, 10.0%, 7.5%, 7.4999%
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("TEST 1: Green band — IRR 12% (well above 10%)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.12, "irr_assumed_pct": 0.14, "irr_delta_pct": -0.02})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "green"
    assert parsed["edge_label"] == EDGE_AUTO_RENDER
    print("[PASS] 12% -> green")

    print("\n" + "=" * 60)
    print("TEST 2: Boundary — IRR 10.01% (just above green threshold)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.1001})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "green", f"10.01% should be green, got {parsed['band']}"
    print("[PASS] 10.01% -> green")

    print("\n" + "=" * 60)
    print("TEST 3: Boundary — IRR exactly 10.0% (should be yellow, not green)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.10})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "yellow", f"10.0% should be yellow, got {parsed['band']}"
    print("[PASS] 10.00% -> yellow (boundary belongs to yellow)")

    print("\n" + "=" * 60)
    print("TEST 4: Yellow band — IRR 9% (mid watch band)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.09})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "yellow"
    assert parsed["edge_label"] == EDGE_ADVISORY
    print("[PASS] 9% -> yellow")

    print("\n" + "=" * 60)
    print("TEST 5: Boundary — IRR exactly 7.5% (should be yellow, not red)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.075})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "yellow", f"7.5% should be yellow, got {parsed['band']}"
    print("[PASS] 7.50% -> yellow (boundary belongs to yellow)")

    print("\n" + "=" * 60)
    print("TEST 6: Boundary — IRR 7.49% (just below red threshold)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.0749})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "red", f"7.49% should be red, got {parsed['band']}"
    print("[PASS] 7.49% -> red")

    print("\n" + "=" * 60)
    print("TEST 7: Red band — IRR 5% (deal is bad)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": 0.05})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "red"
    assert parsed["edge_label"] == EDGE_REQUIRES_HUMAN_REVIEW
    print("[PASS] 5% -> red")

    print("\n" + "=" * 60)
    print("TEST 8: Negative IRR — IRR -2% (catastrophic)")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": -0.02})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "red"
    print("[PASS] -2% -> red")

    print("\n" + "=" * 60)
    print("TEST 9: Missing IRR — defaults to red for safety")
    print("=" * 60)
    out = post_math_dispatch({"irr_observed_pct": None})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "red"
    assert parsed.get("error") == "missing_irr"
    print("[PASS] missing IRR -> red (fail-safe)")

    print("\n" + "=" * 60)
    print("TEST 10: Underwriting error envelope — defaults to red for safety")
    print("=" * 60)
    out = post_math_dispatch({"error": "invalid_deal_profile", "message": "missing field"})
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "red"
    assert parsed.get("error") == "underwriting_error"
    print("[PASS] upstream error -> red (fail-safe)")

    print("\n" + "=" * 60)
    print("TEST 11: Bad input — defaults to red for safety")
    print("=" * 60)
    out = post_math_dispatch("not a dict")  # type: ignore[arg-type]
    parsed = json.loads(out)
    print(out)
    assert parsed["band"] == "red"
    print("[PASS] bad input -> red (fail-safe)")

    print("\nAll dispatch tests passed.")
