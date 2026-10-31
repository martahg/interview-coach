# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Marta Hernández Granado

"""
Post-session evaluator. Not real time: it reads a saved transcript and writes
a markdown report with feedback on content and English.

Usage:
    python evaluate.py sessions/20261031-110741-recruiter.json
"""

import json
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def format_transcript(turns: list) -> str:
    """Merge consecutive turns from the same speaker and format them as
    INTERVIEWER: ... / CANDIDATE: ...

    The voice pipeline can store one answer as several fragments; merging them
    lets the evaluator read whole sentences.
    """
    merged = []
    for t in turns:
        speaker = "CANDIDATE" if t["role"] == "user" else "INTERVIEWER"
        content = t["content"]
        # Content can be a plain string or a list of content blocks
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if merged and merged[-1][0] == speaker:
            merged[-1] = (speaker, f"{merged[-1][1]} {content}")
        else:
            merged.append((speaker, content))
    return "\n\n".join(f"{s}: {c}" for s, c in merged)


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python evaluate.py <path to sessions/xxx.json>")

    session_path = Path(sys.argv[1])
    session = json.loads(session_path.read_text(encoding="utf-8"))

    system_prompt = (ROOT / "prompts" / "evaluator.md").read_text(encoding="utf-8")
    cv = (ROOT / "inputs" / "cv.md").read_text(encoding="utf-8")
    jd = (ROOT / "inputs" / "jd.md").read_text(encoding="utf-8")

    user_message = (
        f"<job_description>\n{jd}\n</job_description>\n\n"
        f"<cv>\n{cv}\n</cv>\n\n"
        f"<interview_type>{session['persona']}</interview_type>\n\n"
        f"<transcript>\n{format_transcript(session['turns'])}\n</transcript>"
    )

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=os.getenv("EVALUATOR_MODEL", "claude-opus-5-5"),
        max_tokens=16000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    if response.stop_reason == "max_tokens":
        print("⚠️  The report was cut off: increase max_tokens.")

    report = "".join(b.text for b in response.content if b.type == "text")
    report_path = session_path.with_suffix(".report.md")
    report_path.write_text(report, encoding="utf-8")
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()