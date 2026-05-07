"""Smoke test: round-trip a 'hello' through whatever model MODEL_ID points at.

Honors the same MODEL_ID env var as agent.py so swapping to Claude is a
one-line .env change for both. Falls back to the v1 Hy3 baseline if unset.
"""
import logging
import os

from dotenv import load_dotenv
from strands import Agent
from strands.models.litellm import LiteLLMModel

logging.getLogger("LiteLLM").setLevel(logging.ERROR)

load_dotenv()

DEFAULT_MODEL_ID = "openrouter/tencent/hy3-preview:free"
model_id = os.getenv("MODEL_ID", DEFAULT_MODEL_ID)

print(f"Round-tripping a 'hello' through: {model_id}")

model = LiteLLMModel(
    model_id=model_id,
    params={"max_tokens": 4096, "temperature": 0.3},
)

agent = Agent(model=model)
response = agent("Say hello in one short sentence and tell me what model you are.")
print("---")
print(response)
