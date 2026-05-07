"""Shared pytest fixtures.

The agent imports `strands` at module load. strands-agents requires
Python 3.10+ which may not be available in every CI environment, so we
stub it before any test module imports `agent`. This keeps the test
suite hermetic — no LLM calls, no API keys required.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# ─── Strands stub (must run before agent.py is imported) ──────────────────────

class _StubAgent:
    """Records prompts so tests can assert on them; returns canned text."""

    last_prompts: list[str] = []
    canned_responses: list[str] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __call__(self, prompt: str) -> str:
        _StubAgent.last_prompts.append(prompt)
        if _StubAgent.canned_responses:
            return _StubAgent.canned_responses.pop(0)
        return "STUB-LLM"


def _install_strands_stubs() -> None:
    if "strands" not in sys.modules:
        strands = types.ModuleType("strands")
        strands.Agent = _StubAgent
        sys.modules["strands"] = strands
    if "strands.models" not in sys.modules:
        sys.modules["strands.models"] = types.ModuleType("strands.models")
    if "strands.models.litellm" not in sys.modules:
        litellm_mod = types.ModuleType("strands.models.litellm")
        litellm_mod.LiteLLMModel = lambda **kwargs: None
        sys.modules["strands.models.litellm"] = litellm_mod


_install_strands_stubs()

# Make repo root importable so `import agent` and `from tools.X import Y` work.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def stub_agent_class():
    """Reset and yield the stub agent class so tests can plant canned responses."""
    _StubAgent.last_prompts = []
    _StubAgent.canned_responses = []
    yield _StubAgent


# ─── Fake `requests` for HPD / ACRIS / FRED ──────────────────────────────────

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def fake_requests(monkeypatch):
    """Monkeypatch `requests.get` to return fixture payloads keyed by URL substring.

    Tests register payloads via `fake_requests.register("substring", payload)`
    and the first match (in registration order) is returned.
    """

    class FakeRegistry:
        def __init__(self):
            self.routes: list[tuple[str, FakeResponse]] = []
            self.calls: list[tuple[str, dict]] = []

        def register(self, url_substring: str, payload, status_code: int = 200):
            self.routes.append((url_substring, FakeResponse(payload, status_code)))

        def __call__(self, url, **kwargs):
            self.calls.append((url, kwargs))
            for substring, response in self.routes:
                if substring in url:
                    return response
            raise AssertionError(
                f"FakeRequests received an unmocked URL: {url}\n"
                f"Registered substrings: {[s for s, _ in self.routes]}"
            )

    registry = FakeRegistry()
    import requests
    monkeypatch.setattr(requests, "get", registry)
    return registry


# ─── Tmp snapshots and runs dirs to keep tests off real disk ─────────────────

@pytest.fixture
def tmp_runs_and_snapshots(monkeypatch, tmp_path):
    """Redirect agent.RUNS_DIR and agent.SNAPSHOTS_DIR to a tmp_path."""
    import agent
    runs = tmp_path / "runs"
    snaps = tmp_path / "snapshots"
    runs.mkdir()
    snaps.mkdir()
    monkeypatch.setattr(agent, "RUNS_DIR", runs)
    monkeypatch.setattr(agent, "SNAPSHOTS_DIR", snaps)
    return {"runs": runs, "snapshots": snaps, "root": tmp_path}


# ─── Demo deal fixture ────────────────────────────────────────────────────────

@pytest.fixture
def demo_deal():
    import json
    deal_path = REPO_ROOT / "deals" / "midtown-south-office-001.json"
    with open(deal_path) as f:
        return json.load(f)
