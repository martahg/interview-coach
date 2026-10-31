"""
Real-time voice mock interview.

Flow: microphone -> VAD -> STT (Deepgram) -> LLM (Claude) -> TTS (ElevenLabs) -> speaker
When the call ends (Ctrl+C), the transcript is saved to sessions/.

Usage:
    python interview.py
    NUM_QUESTIONS=3 python interview.py   # short interview (demo)
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

ROOT = Path(__file__).parent
START_MSG = "(The call has just connected. Greet the candidate and start the interview.)"
load_dotenv(ROOT / ".env")


def build_system_prompt(persona: str) -> str:
    """Fill the persona template with the CV, the job description and the interview length."""
    template = (ROOT / "prompts" / f"{persona}.md").read_text(encoding="utf-8")
    cv = (ROOT / "inputs" / "cv.md").read_text(encoding="utf-8")
    jd = (ROOT / "inputs" / "jd.md").read_text(encoding="utf-8")
    num_questions = os.getenv("NUM_QUESTIONS", "4 to 6")
    # replace() instead of format(), so curly braces in the CV or JD don't break it
    return (
        template.replace("{cv}", cv)
        .replace("{jd}", jd)
        .replace("{num_questions}", num_questions)
    )


def load_keyterms() -> list[str]:
    """Read inputs/keyterms.txt (optional): one term per line, # for comments."""
    path = ROOT / "inputs" / "keyterms.txt"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def save_session(persona: str, messages: list) -> Path | None:
    """Save the conversation (without the internal start message) for the evaluator."""
    sessions = ROOT / "sessions"
    sessions.mkdir(exist_ok=True)
    path = sessions / f"{datetime.now():%Y%m%d-%H%M%S}-{persona}.json"
    turns = [
        m for m in messages
        if m.get("role") in ("user", "assistant") and m.get("content") != START_MSG
    ]
    if not any(t["role"] == "user" for t in turns):
        return None
    path.write_text(
        json.dumps({"persona": persona, "turns": turns}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


async def main():
    persona = os.getenv("PERSONA", "recruiter")

    # 1. TRANSPORT: audio in and out. Here, the local microphone and speaker.
    #    VAD is configured on the user aggregator (step 5), as Pipecat 1.x expects.
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    # 2. STT: streaming speech-to-text.
    #    As literal as possible (no smart_format): the evaluator needs to see how you
    #    actually spoke. keyterm boosts recognition of names and jargon listed in
    #    inputs/keyterms.txt.
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model="nova-3-general",
            language="en-US",
            smart_format=False,
            punctuate=True,
            interim_results=True,
            keyterm=load_keyterms() or None,
        ),
        # filler_words is not set: deepgram-sdk 7.16 only supports it for pre-recorded
        # audio, not for streaming, and passing it through `addons` breaks the
        # real-time connection.
    )

    # 3. LLM: the interviewer. A fast model, because time to first token drives latency.
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=os.getenv("INTERVIEWER_MODEL", "claude-sonnet-5-5"),
            system_instruction=build_system_prompt(persona),
        ),
    )

    # 4. TTS: streaming text-to-speech (starts speaking before the full reply is ready).
    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(voice=os.environ["ELEVENLABS_VOICE_ID"]),
    )

    # 5. CONTEXT AND TURN-TAKING
    #    The system prompt lives in the LLM's system_instruction. The context only holds
    #    the user/assistant history, which the aggregators extend on every turn.
    #    The Claude API needs at least one user message, so START_MSG opens the call.
    context = LLMContext(messages=[{"role": "user", "content": START_MSG}])

    #    A user turn ends only when Smart Turn thinks you have finished AND you have
    #    been silent for USER_SPEECH_TIMEOUT seconds. Raise it in .env if you get cut
    #    off while thinking; lower it if responses feel slow.
    context_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3()),
                    SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=float(os.getenv("USER_SPEECH_TIMEOUT", "1.5"))
                    ),
                ],
            ),
        ),
    )

    # 6. PIPELINE: the order in which frames (audio, text, events) flow.
    pipeline = Pipeline(
        [
            transport.input(),               # microphone audio
            stt,                             # -> text of what you said
            context_aggregator.user(),       # -> added to the history as a "user" turn
            llm,                             # -> streamed reply
            tts,                             # -> converted to audio
            transport.output(),              # -> played back
            context_aggregator.assistant(),  # -> what the interviewer said is saved
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,  # barge-in: if you speak, the interviewer stops
            enable_metrics=True,       # per-service latency in the log
        ),
    )

    # The interviewer opens the call without waiting for you.
    await worker.queue_frames([LLMRunFrame()])

    runner = WorkerRunner(handle_sigint=True)
    await runner.add_workers(worker)
    try:
        logger.info(f"Interview '{persona}' running. Press Ctrl+C to end.")
        await runner.run()
    finally:
        path = save_session(persona, context.get_messages())
        if path:
            logger.info(f"Transcript saved to {path}")
            logger.info(f"Evaluate it with: python evaluate.py {path}")
        else:
            logger.info("No answers from the candidate: session not saved.")


if __name__ == "__main__":
    asyncio.run(main())