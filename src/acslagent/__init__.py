"""ACSLAgent: LLM-driven ACSL specification synthesis with Frama-C as checker.

A LangGraph reproduction of ACSLAgent-FB4C. Every step of the original
pipeline is one graph node, and every node writes its state to trace.jsonl.
"""

__version__ = "0.2.0"
