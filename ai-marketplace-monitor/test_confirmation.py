"""The second opinion that would have stopped the MediaPad notification.

groq_oss20 rated a Huawei MediaPad T3 10 at EUR 40 a "Great deal (5)",
justifying it with a EUR 90 resale value it invented. gemini_35_lite, given
the same listing in the same round, rated it 2 and said the tablet is worth
about what it was listed for. Whichever answered first won.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_marketplace_monitor.ai import AIResponse  # noqa: E402
from ai_marketplace_monitor.monitor import MarketplaceMonitor  # noqa: E402


class FakeAgent:
    def __init__(self, name: str, score: int, comment: str = "") -> None:
        self.config = mock.Mock(name=name)
        self.config.name = name
        self.score = score
        self.comment = comment or f"{name} says {score}"
        self.calls: list = []

    def evaluate(self, listing, item_config, marketplace_config, use_cache=True):
        self.calls.append(use_cache)
        return AIResponse(self.score, self.comment, name=self.config.name)


def monitor_with(agents):
    m = MarketplaceMonitor.__new__(MarketplaceMonitor)
    m.ai_agents = agents
    m.logger = None
    return m


class TestSecondOpinion(unittest.TestCase):
    def confirm(self, agents, first):
        return monitor_with(agents).confirm_by_ai(
            mock.Mock(title="Tablet HUAWEI MediaPad T3 10"),
            item_config=mock.Mock(), marketplace_config=mock.Mock(),
            first_opinion=first,
        )

    def test_disagreement_blocks_the_notification(self):
        first = AIResponse(5, "55% off a EUR 90 resale value", name="groq_oss20")
        agents = [FakeAgent("groq_oss20", 5), FakeAgent("gemini_35_lite", 2)]
        result = self.confirm(agents, first)
        self.assertEqual(result.score, 2, "a disputed 5 must not stay a 5")

    def test_agreement_keeps_the_deal(self):
        first = AIResponse(5, "genuinely underpriced", name="groq")
        agents = [FakeAgent("groq", 5), FakeAgent("gemini", 5)]
        self.assertEqual(self.confirm(agents, first).score, 5)

    def test_the_first_service_is_not_asked_again(self):
        first = AIResponse(5, "x", name="groq")
        groq, gemini = FakeAgent("groq", 5), FakeAgent("gemini", 5)
        self.confirm([groq, gemini], first)
        self.assertEqual(groq.calls, [], "must not confirm itself")
        self.assertEqual(len(gemini.calls), 1)

    def test_confirmation_never_reads_the_cache(self):
        """The cache key does not name the service, so a cached read would
        return the very answer being checked."""
        first = AIResponse(5, "x", name="groq")
        gemini = FakeAgent("gemini", 2)
        self.confirm([FakeAgent("groq", 5), gemini], first)
        self.assertEqual(gemini.calls, [False])

    def test_no_other_service_leaves_the_verdict_alone(self):
        first = AIResponse(5, "x", name="groq")
        self.assertEqual(self.confirm([FakeAgent("groq", 5)], first).score, 5)

    def test_a_failing_confirmer_is_skipped_not_fatal(self):
        first = AIResponse(5, "x", name="groq")
        broken = FakeAgent("broken", 5)
        broken.evaluate = mock.Mock(side_effect=RuntimeError("429"))
        agents = [FakeAgent("groq", 5), broken, FakeAgent("gemini", 2)]
        self.assertEqual(self.confirm(agents, first).score, 2)


if __name__ == "__main__":
    unittest.main()
