"""Tests for tools/dispatch.py — POST_MATH_DISPATCH band classifier.

Boundary semantics (locked contract):
  irr > 0.10           -> green   (auto_render)
  0.075 <= irr <= 0.10 -> yellow  (advisory)
  irr < 0.075          -> red     (requires_human_review)
"""
import json

import pytest

from tools.dispatch import (
    EDGE_ADVISORY,
    EDGE_AUTO_RENDER,
    EDGE_REQUIRES_HUMAN_REVIEW,
    GREEN_THRESHOLD,
    RED_THRESHOLD,
    post_math_dispatch,
)


def _band(irr_observed):
    return json.loads(post_math_dispatch({"irr_observed_pct": irr_observed}))


@pytest.mark.parametrize(
    "irr,expected_band,expected_edge",
    [
        # Green region
        (0.20, "green", EDGE_AUTO_RENDER),
        (0.14, "green", EDGE_AUTO_RENDER),
        (0.1001, "green", EDGE_AUTO_RENDER),  # just above green threshold
        # Boundary: 10.0% itself is yellow, not green
        (0.10, "yellow", EDGE_ADVISORY),
        # Yellow region
        (0.095, "yellow", EDGE_ADVISORY),
        (0.085, "yellow", EDGE_ADVISORY),
        # Boundary: 7.5% itself is yellow, not red
        (0.075, "yellow", EDGE_ADVISORY),
        # Red region
        (0.0749, "red", EDGE_REQUIRES_HUMAN_REVIEW),
        (0.05, "red", EDGE_REQUIRES_HUMAN_REVIEW),
        (0.0, "red", EDGE_REQUIRES_HUMAN_REVIEW),
        (-0.02, "red", EDGE_REQUIRES_HUMAN_REVIEW),
    ],
)
def test_band_classification(irr, expected_band, expected_edge):
    result = _band(irr)
    assert result["band"] == expected_band, f"IRR {irr} -> expected {expected_band}, got {result['band']}"
    assert result["edge_label"] == expected_edge


def test_thresholds_echoed():
    result = _band(0.12)
    assert result["thresholds"]["green_above"] == GREEN_THRESHOLD
    assert result["thresholds"]["red_below"] == RED_THRESHOLD


def test_dispatch_reason_human_readable():
    result = _band(0.05)
    assert "5.00%" in result["dispatch_reason"]
    assert "7.5%" in result["dispatch_reason"]


def test_missing_irr_defaults_to_red():
    """Fail-safe: if dispatch can't compute, default to the most cautious band."""
    result = json.loads(post_math_dispatch({"irr_observed_pct": None}))
    assert result["band"] == "red"
    assert result["edge_label"] == EDGE_REQUIRES_HUMAN_REVIEW
    assert result.get("error") == "missing_irr"


def test_underwriting_error_envelope_defaults_to_red():
    result = json.loads(post_math_dispatch({"error": "invalid_deal_profile", "message": "missing field"}))
    assert result["band"] == "red"
    assert result.get("error") == "underwriting_error"


def test_bad_input_type_defaults_to_red():
    result = json.loads(post_math_dispatch("not a dict"))  # type: ignore[arg-type]
    assert result["band"] == "red"
    assert result.get("error") == "invalid_input"


def test_echoes_irr_context():
    """Dispatch should echo IRR fields so downstream synthesis has context."""
    underwriting = {
        "irr_observed_pct": 0.085,
        "irr_assumed_pct": 0.14,
        "irr_delta_pct": -0.055,
    }
    result = json.loads(post_math_dispatch(underwriting))
    assert result["irr_observed_pct"] == 0.085
    assert result["irr_assumed_pct"] == 0.14
    assert result["irr_delta_pct"] == -0.055
