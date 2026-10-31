"""
Entrevista simulada por voz en tiempo real.

Flujo: micrófono -> VAD -> STT (Deepgram) -> LLM (Claude) -> TTS (ElevenLabs) -> altavoz
Al terminar (Ctrl+C o fin de la llamada) guarda la transcripción en sessions/.

Uso:
    python interview.py
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

ROOT = Path(__file__).parent
START_MSG = "(The call has just connected. Greet the candidate and start the interview.)"
load_dotenv(ROOT / ".env")


def build_system_prompt(persona: str) -> str:
    """Rellena la plantilla de la persona con tu CV y la oferta."""
    template = (ROOT / "prompts" / f"{persona}.md").read_text(encoding="utf-8")
    cv = (ROOT / "inputs" / "cv.md").read_text(encoding="utf-8")
    jd = (ROOT / "inputs" / "jd.md").read_text(encoding="utf-8")
    # replace() en vez de format() para no romper con llaves que haya en el CV/JD
    return template.replace("{cv}", cv).replace("{jd}", jd)


def save_session(persona: str, messages: list) -> Path | None:
    """Guarda la conversación (sin el system prompt) para el evaluador."""
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

    # 1. TRANSPORTE: entrada/salida de audio. Aquí, micro y altavoz locales.
    #    El VAD (Silero) detecta cuándo empiezas y dejas de hablar.
    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        )
    )

    # 2. STT: audio -> texto en streaming.
    #    Configurado para transcripción lo más literal posible (sin smart_format),
    #    porque el evaluador necesita ver cómo hablaste de verdad.
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(
            model="nova-3-general",
            language="en-US",
            smart_format=False,
            punctuate=True,
            interim_results=True,
        ),
        # filler_words no se pasa: el deepgram-sdk instalado (7.16.0) solo expone ese
        # parámetro en el endpoint de audio pregrabado, no en connect() de streaming,
        # así que cualquier valor en `addons` rompe la conexión en tiempo real.
    )

    # 3. LLM: el "cerebro" del entrevistador.
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=os.getenv("INTERVIEWER_MODEL", "claude-sonnet-5-5"),
            system_instruction=build_system_prompt(persona),
        ),
    )

    # 4. TTS: texto -> voz en streaming (empieza a hablar antes de tener la frase entera).
    tts = ElevenLabsTTSService(
        api_key=os.environ["ELEVENLABS_API_KEY"],
        settings=ElevenLabsTTSService.Settings(voice=os.environ["ELEVENLABS_VOICE_ID"]),
    )

    # 5. CONTEXTO: el historial de la conversación que se envía al LLM en cada turno.
    #    El system prompt va en el `system_instruction` del LLM (arriba); aquí solo
    #    vive el historial user/assistant que añaden los "aggregators" cada turno.
    context = LLMContext(
        messages=[{"role": "user", "content": START_MSG}]
    )
    context_aggregator = LLMContextAggregatorPair(context)

    # 6. PIPELINE: el orden en que fluyen los frames (unidades de datos: audio, texto, eventos).
    pipeline = Pipeline(
        [
            transport.input(),               # audio del micro
            stt,                             # -> texto de lo que dices
            context_aggregator.user(),       # -> lo añade al historial como turno "user"
            llm,                             # -> genera la respuesta en streaming
            tts,                             # -> la convierte en audio
            transport.output(),              # -> la reproduce
            context_aggregator.assistant(),  # -> guarda lo que dijo el entrevistador
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            allow_interruptions=True,  # "barge-in": si hablas, el entrevistador se calla
            enable_metrics=True,       # latencias por componente en el log
        ),
    )

    # El entrevistador abre la llamada sin esperar a que hables.
    await worker.queue_frames([LLMRunFrame()])

    runner = WorkerRunner(handle_sigint=True)
    await runner.add_workers(worker)
    try:
        logger.info(f"Entrevista '{persona}' en marcha. Ctrl+C para terminar.")
        await runner.run()
    finally:
        path = save_session(persona, context.get_messages())
        if path:
            logger.info(f"Transcripción guardada en {path}")
            logger.info(f"Evalúala con: python evaluate.py {path}")
        else:
            logger.info("Sesión sin respuestas del candidato: no se guarda.")


if __name__ == "__main__":
    asyncio.run(main())
