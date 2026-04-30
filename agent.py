"""
CRE Distress Agent — v0.1
A NYC-focused agent that scans property distress signals
for a distressed-multifamily acquisitions analyst.
"""
import logging
import os
import sys

from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel

from tools.violations import get_property_distress_signals as _violations_impl
from tools.market_signals import get_market_signals as _market_signals_impl
from tools.macro_signals import get_macro_signals as _macro_signals_impl

logging.getLogger("LiteLLM").setLevel(logging.ERROR)
load_dotenv()


# Wrap the tool with the @tool decorator so Strands exposes it to the model.
# We import the implementation from tools/violations.py to keep concerns separate:
# the tool file knows the API, the agent file knows the loop.
@tool
def get_property_distress_signals(bbl: str) -> str:
    """Fetch HPD violations for a NYC BBL and return a distress assessment.

    Use this when the user asks about a specific property's condition,
    code violations, or whether a property looks distressed. Returns JSON
    with violation counts, severity breakdown, most recent inspection date,
    and a derived distress_score (none / low / medium / high).

    Args:
        bbl: 10-digit NYC Borough-Block-Lot identifier as a string.
             Format: 1-digit borough + 5-digit block + 4-digit lot.
             Example: "2026140035" (Bronx, block 02614, lot 0035).
    """
    return _violations_impl(bbl)


@tool
def get_market_signals(
    borough: str,
    days_back: int = 90,
    min_sale_price: int = 1_000_000,
) -> str:
    """Fetch recent ACRIS deed sales for a NYC borough and return market comps.

    Use this when the user asks about recent sales activity, comp trades,
    market velocity, or what the market looks like in a given borough.
    Returns JSON with sale_count, median_price, sample_sales, and a
    market_signal (active / slow / no_data).

    Args:
        borough: NYC borough name (Manhattan, Bronx, Brooklyn, Queens,
                 Staten Island). Case-insensitive.
        days_back: How many calendar days back to search. Default 90.
        min_sale_price: Minimum transaction size to include. Default $1,000,000.
    """
    return _market_signals_impl(borough, days_back, min_sale_price)


@tool
def get_macro_signals(days_back: int = 30) -> str:
    """Fetch recent FRED rate data (10Y Treasury, SOFR) and return a macro signal.

    Use this when the user asks about interest rates, the rate environment,
    or macro conditions affecting the deal. Returns JSON with current vs.
    prior values for the 10-Year Treasury (DGS10) and SOFR, signed bps_change
    for each (positive = rates rose, negative = rates fell), and a top-level
    macro_signal (rates_moved / stable / no_data).

    Args:
        days_back: How many calendar days back to compare against. Default 30.
    """
    return _macro_signals_impl(days_back)


SYSTEM_PROMPT = """You are an analyst tool for a CRE distressed-multifamily acquisitions team in NYC.

Your job is to help the analyst quickly assess whether a property is showing signs of distress that would warrant deeper investigation as an acquisition target.

When the user gives you a BBL or asks about a property, call the get_property_distress_signals tool. Then write a SHORT assessment with this structure:

DISTRESS PROFILE
- Distress score and a one-line justification
- Open violations count and severity breakdown
- Most recent violation date

WHAT THIS SUGGESTS
- 1-2 sentences on what these signals likely mean for an acquisitions analyst.
  Example: "61 Class C violations with the most recent dated yesterday suggests an actively deteriorating property under owner neglect — worth pulling ownership records and ACRIS filings."

NEXT MOVES
- 2-3 concrete next steps an analyst would take

Style rules:
- Be specific. Cite actual numbers from the tool output.
- No filler. Don't say "Here is your analysis." Just deliver it.
- If distress_score is "none," say so plainly and recommend deprioritizing the lead.
- If the tool returns an error, surface the error clearly — don't make up data."""


def run(user_query: str):
    model = LiteLLMModel(
        model_id="openrouter/tencent/hy3-preview:free",
        params={"max_tokens": 8192, "temperature": 0.3},
    )
    agent = Agent(
        model=model,
        tools=[
            get_property_distress_signals,
            get_market_signals,
            get_macro_signals,
        ],
        system_prompt=SYSTEM_PROMPT,
    )
    result = agent(user_query)
    print("\n" + "=" * 60)
    print("AGENT RESPONSE")
    print("=" * 60)
    print(result)


if __name__ == "__main__":
    # Default test query — uses the demo BBL from our manual testing
    query = sys.argv[1] if len(sys.argv) > 1 else (
        "Assess BBL 2026140035 for distress signals. "
        "Is this a worth pursuing as an acquisition target?"
    )
    run(query)