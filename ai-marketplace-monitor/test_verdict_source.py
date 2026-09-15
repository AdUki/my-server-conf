"""Every verdict says whether arithmetic or a model produced it."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_marketplace_monitor.ai import AIResponse  # noqa: E402
from ai_marketplace_monitor.notification import (  # noqa: E402
    SCRIPT_VERDICT_NAME, verdict_source,
)


class TestVerdictSource(unittest.TestCase):
    def test_benchmark_verdict_is_labelled_script(self):
        self.assertEqual(
            verdict_source(AIResponse(5, "x", name=SCRIPT_VERDICT_NAME)), "Script")

    def test_model_verdict_names_the_service(self):
        self.assertEqual(
            verdict_source(AIResponse(5, "x", name="groq_oss20")), "AI (groq_oss20)")

    def test_unnamed_verdict_falls_back_to_ai(self):
        self.assertEqual(verdict_source(AIResponse(5, "x", name="")), "AI")

    def test_the_name_the_monitor_actually_sets_is_recognised(self):
        """Guards the string the deterministic path uses in monitor.py."""
        monitor = (Path(__file__).resolve().parent / ".venv" / "lib")
        hits = list(monitor.glob("python*/site-packages/ai_marketplace_monitor/monitor.py"))
        if not hits:
            self.skipTest("installed package not found")
        self.assertIn(f'name="{SCRIPT_VERDICT_NAME}"', hits[0].read_text(),
                      "monitor.py no longer tags deterministic verdicts with "
                      "the name notification.py looks for")


if __name__ == "__main__":
    unittest.main(verbosity=1)
