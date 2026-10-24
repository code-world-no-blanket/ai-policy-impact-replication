"""AI terms that mark a CONTRIBUTING.md as stating an AI policy.

Kept identical to CONTRIBUTING_TERMS in scripts/find_ai_policy_repos.py and
scripts/find_no_ai_policy_repos.py (a test checks this), so no repository can be
counted both as having a policy and as having none.
"""

from __future__ import annotations

import re

CONTRIBUTING_TERMS = [
    "AI",
    "artificial intelligence",
    "LLM",
    "LLMs",
    "large language model",
    "large language models",
    "generative AI",
    "GenAI",
    "ChatGPT",
    "Claude",
    "OpenAI",
    "Anthropic",
    "Copilot",
    "AI-generated",
    "AI-assisted",
    "machine-generated",
    "vibe coding",
    "AI agent",
    "coding agent",
]
# Whole words or phrases in any case; hyphens and spacing between words ignored,
# as GitHub code search matches them.
MENTIONS_AI = re.compile(
    r"\b(?:"
    + "|".join(
        r"[-\s]+".join(map(re.escape, term.replace("-", " ").split()))
        for term in CONTRIBUTING_TERMS
    )
    + r")\b",
    re.IGNORECASE,
)
POLICY_FILENAME = "AI_POLICY.md"
CONTRIBUTING_FILENAME = "CONTRIBUTING.md"
