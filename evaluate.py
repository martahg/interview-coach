"""
Evaluador post-sesión. No es tiempo real: lee la transcripción guardada
y genera un informe en markdown con feedback de contenido y de inglés.

Uso:
    python evaluate.py sessions/20261028-091500-recruiter.json
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
    """Convierte los turnos en texto legible: INTERVIEWER: ... / CANDIDATE: ..."""
    lines = []
    for t in turns:
        speaker = "CANDIDATE" if t["role"] == "user" else "INTERVIEWER"
        content = t["content"]
        # El contenido puede venir como texto o como lista de bloques
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        lines.append(f"{speaker}: {content}")
    return "\n\n".join(lines)


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python evaluate.py <ruta a sessions/xxx.json>")

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
        max_tokens=8000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    report = "".join(b.text for b in response.content if b.type == "text")
    report_path = session_path.with_suffix(".report.md")
    report_path.write_text(report, encoding="utf-8")
    print(f"Informe guardado en {report_path}")


if __name__ == "__main__":
    main()
