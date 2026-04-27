"""F1.1 hardening — completion-token budget for reasoning models.

gpt-5.5 is a reasoning model: every chat-completion consumes a
chunk of ``max_completion_tokens`` on internal reasoning before
emitting any visible output. The pre-F1.1 budget of 256 (sized
for gpt-5.4, which has no reasoning overhead) was entirely
consumed by reasoning on every call — ``finish_reason`` came
back as ``"length"`` and ``message.content`` was empty, so every
receipt looked like an extraction failure regardless of how
clear the image actually was.

This test pins the budget high enough that reasoning + the
small JSON object the prompt asks for both fit. If a future
change drops it back below ~1024 we want CI to fail loudly
rather than re-introduce a silent 100% recall regression in
prod.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import model_router


def test_max_completion_tokens_high_enough_for_reasoning_overhead() -> None:
    assert model_router._MAX_COMPLETION_TOKENS >= 1024, (
        "gpt-5.5 reasoning overhead consumes most of the budget before "
        "output; below ~1024 the model truncates at finish_reason='length' "
        "with empty content. See F1.1 incident."
    )
