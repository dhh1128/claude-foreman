"""Visible pane excerpts, including a cropped capture from this worker's Codex pane."""

from pathlib import Path

from zamu import pane


FIXTURES = Path(__file__).parent / "fixtures"


def test_claude_completion_verbs_are_status_not_worker_words():
    for verb in ("Churned", "Cogitated", "Brewed", "Worked"):
        capture = ("I'm continuing with the acceptance tests.\n"
                   f"✻ {verb} for 2m 13s\n❯ \n")
        assert pane.last_visible_line(capture) == "I'm continuing with the acceptance tests."


def test_historical_claude_prompt_does_not_hide_later_worker_output():
    capture = "❯ Run the acceptance tests\nI'm continuing with the acceptance tests.\n"
    assert pane.last_visible_line(capture) == "I'm continuing with the acceptance tests."


def test_real_codex_visible_capture_quotes_assistant_words():
    capture = (FIXTURES / "codex_pane.txt").read_text()
    assert pane.last_visible_line(capture) == \
        "that historical prompts preserve output below them."


def test_codex_single_line_reply_loses_bullet_chrome():
    capture = ("• I will check the failing tests.\n"
               "• Working (11s • esc to interrupt)\n"
               "› Ask Codex to do anything\n"
               "GPT-6-Sol high · ~/code/repo · Read brief\n"
               "? for shortcuts\n")
    assert pane.last_visible_line(capture) == "I will check the failing tests."


def test_codex_chrome_only_has_no_quote():
    capture = ("• Working (11s • esc to interrupt)\n"
               "› Ask Codex to do anything\n"
               "GPT-6-Sol high · ~/code/repo · Read brief\n"
               "? for shortcuts\n")
    assert pane.last_visible_line(capture) is None
