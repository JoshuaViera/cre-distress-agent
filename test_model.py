"""Smoke test: confirm Hy3 via OpenRouter responds."""
import logging
import os
from dotenv import load_dotenv
from strands import Agent
from strands.models.litellm import LiteLLMModel

# Silence LiteLLM's noisy logging
logging.getLogger("LiteLLM").setLevel(logging.ERROR)

load_dotenv()

# LiteLLM reads OPENROUTER_API_KEY from env automatically
model = LiteLLMModel(
    model_id="openrouter/tencent/hy3-preview:free",
    params={"max_tokens": 4096, "temperature": 0.3},
)

agent = Agent(model=model)
response = agent("Say hello in one short sentence and tell me what model you are.")
print("---")
print(response)
