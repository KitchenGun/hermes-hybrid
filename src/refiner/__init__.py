"""Request Refiner — natural-language → structured intent extraction.

Sits between the Discord gateway and the Orchestrator/Job_Factory.
Reads the matched profile's ``intent_schema.json`` and produces a
validated intent object plus a confidence score. Low-confidence or
ambiguous requests are returned as clarification prompts.

LangChain analogy:
    ChatPromptTemplate + PydanticOutputParser wrapped in a Runnable.
"""
from src.refiner.refiner import (
    RefinedRequest,
    Refiner,
    RefinerError,
    RefinerResult,
)

__all__ = [
    "RefinedRequest",
    "Refiner",
    "RefinerError",
    "RefinerResult",
]
