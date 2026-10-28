# Interview Coach (MVP)

Simulador de entrevistas en inglés por voz en tiempo real. Hablas con un "recruiter" generado por IA y, al terminar, otro modelo te devuelve un informe con feedback de contenido (estructura STAR, lo que faltó) y de inglés (errores concretos, patrones, nivel CEFR estimado).

```
inputs/cv.md, inputs/jd.md ─┐
prompts/recruiter.md ───────┤
                            ▼
                    interview.py  ──►  sessions/xxx.json  ──►  evaluate.py  ──►  sessions/xxx.report.md
                  (voz, tiempo real)     (transcripción)        (sin prisa)         (tu feedback)
```

Son dos programas separados a propósito: la entrevista necesita **latencia baja** y la evaluación necesita **calidad**. Cada uno usa el modelo adecuado.

---

## 1. Cómo funciona una conversación por voz

Una conversación por voz con IA es una cadena de cuatro tareas que se ejecutan una tras otra en cada turno:

```
Tú hablas ─► [¿has terminado?] ─► [audio a texto] ─► [pensar respuesta] ─► [texto a audio] ─► Oyes la respuesta
                  VAD                  STT                  LLM                   TTS
```

El reto no es cada pieza por separado, es que **todo ocurra en menos de ~1 segundo** para que se sienta natural. Para conseguirlo, nada espera a que el paso anterior termine del todo: cada pieza trabaja en **streaming** y pasa resultados parciales a la siguiente. Por ejemplo, el TTS empieza a hablar con la primera frase del LLM mientras este sigue generando la segunda.

### Conceptos clave

**Frame.** La unidad de datos que circula por el pipeline. Un trozo de audio de 20ms es un frame, una palabra transcrita es un frame, "el usuario ha empezado a hablar" es un frame. Piensa en mensajes de una cola, pero en memoria y con milisegundos de vida.

**Pipeline.** Una lista ordenada de procesadores. Cada frame entra por arriba y cada procesador decide si lo transforma, lo deja pasar o genera frames nuevos. Es el mismo patrón que un pipeline de middlewares HTTP o un flujo de Pub/Sub, pero síncrono en el tiempo real del audio.

**Transport.** La puerta de entrada y salida del audio. En el MVP es tu micro y tu altavoz (`LocalAudioTransport`). Más adelante se cambia por WebRTC para usarlo desde el navegador o el móvil, y el resto del pipeline no se toca.

**VAD (Voice Activity Detection).** Un modelo pequeño (Silero, corre en local en tu CPU) que distingue voz de silencio o ruido. Responde a "¿está hablando alguien ahora mismo?". Sin él, el sistema no sabría cuándo te toca a ti y cuándo a él.

**Turn-taking / endpointing.** Decidir que **has terminado tu turno**. Es lo más delicado de todo el sistema: si espera poco, te interrumpe cuando haces una pausa para pensar; si espera mucho, la conversación se siente lenta. En una entrevista haces muchas pausas para pensar, así que es probable que tengas que ajustarlo (ver sección 5).

**Barge-in (interrupciones).** Si empiezas a hablar mientras el entrevistador habla, se calla y te escucha. Lo activa `allow_interruptions=True`. Pipecat corta el audio pendiente y descarta la parte de la respuesta que no llegó a decirse.

**Contexto.** El historial de la conversación (system prompt + todos los turnos) que se envía al LLM en cada respuesta. Los LLM no tienen memoria propia: "recordar" lo que dijiste hace 5 minutos es simplemente que eso va incluido en cada petición.

---

## 2. Cada pieza y por qué la elegí

### Pipecat — el orquestador
Framework open source en Python (licencia BSD) que resuelve la parte difícil: mover frames entre servicios, gestionar el streaming, las interrupciones y la cancelación. Tú solo declaras el orden de las piezas. Alternativa: LiveKit Agents, más orientado a producción con muchos usuarios simultáneos; para uso personal Pipecat es más directo.

### Deepgram — STT (speech-to-text)
Convierte tu voz en texto mientras hablas. Devuelve dos tipos de resultado:
- **Interim**: lo que cree que estás diciendo ahora, cambia sobre la marcha.
- **Final**: la versión definitiva de un fragmento.

Configuración importante en `interview.py`:
- `smart_format=False`: no "embellece" el texto (formatos de números, fechas…).
- `filler_words=True`: conserva "um", "uh", "like". Para una app normal se quitan; para ti son **información** sobre tu fluidez.

Esto es lo que más condiciona el valor del producto. Si el STT corrige tus errores al transcribir, el evaluador no podrá verlos. Pruébalo pronto con frases que sabes que dices mal.

### Claude — LLM (el entrevistador)
Genera las respuestas del recruiter a partir del contexto. Usa un modelo rápido (Sonnet) porque lo que importa es el **tiempo hasta el primer token**: cuanto antes empiece a generar, antes empieza a hablar el TTS.

El comportamiento entero está en `prompts/recruiter.md`. Cosas que importan para voz:
- **Sin markdown ni listas**: el TTS leería los asteriscos o sonaría a robot.
- **Una pregunta por turno**: igual que un humano.
- **No dar feedback durante la llamada**: el entrevistador se mantiene en su papel; el feedback es trabajo del evaluador.

### ElevenLabs — TTS (text-to-speech)
Convierte el texto en voz natural, también en streaming. `ELEVENLABS_VOICE_ID` elige la voz; cuando añadas el entrevistador técnico, dale otra voz distinta para que la diferencia de rol se note. Alternativa: Cartesia, algo más rápida y algo menos expresiva.

### Claude — evaluador (`evaluate.py`)
No forma parte del pipeline de voz. Es una llamada normal a la API al terminar, con un modelo más potente (Opus), porque aquí no importa tardar 30 segundos. Recibe la transcripción, tu CV y la oferta, y escribe el informe según `prompts/evaluator.md`.

---

## 3. Mapa de ficheros

| Fichero | Qué hace |
|---|---|
| `interview.py` | Monta el pipeline de voz y guarda la transcripción al terminar |
| `evaluate.py` | Lee una transcripción y genera el informe |
| `prompts/recruiter.md` | Persona, guion y reglas del entrevistador (`{cv}` y `{jd}` se rellenan solos) |
| `prompts/evaluator.md` | Rúbrica del informe |
| `inputs/cv.md`, `inputs/jd.md` | Tu CV y la oferta |
| `sessions/` | Se crea solo: transcripciones (`.json`) e informes (`.report.md`) |

Para añadir otro entrevistador (por ejemplo, el técnico), crea `prompts/tech_lead.md` y ejecuta con `PERSONA=tech_lead`.

---

## 4. Puesta en marcha

Requisitos: Python 3.11+, y en macOS `brew install portaudio` (lo necesita PyAudio para el micro).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # y rellena las claves
# pega tu CV en inputs/cv.md y la oferta en inputs/jd.md

python interview.py         # habla; Ctrl+C para terminar
python evaluate.py sessions/<fichero>.json
```

**Usa auriculares.** En local no hay cancelación de eco: si el micro capta el altavoz, el sistema oye al entrevistador, cree que eres tú y se interrumpe a sí mismo.

Claves necesarias: Deepgram, Anthropic y ElevenLabs. Deepgram y ElevenLabs tienen tier gratuito suficiente para probar.

---

## 5. Qué verificar al instalar

No pude ejecutar el código en el entorno donde lo escribí, así que compila pero no está probado contra la versión exacta de Pipecat que instales. Pipecat evoluciona rápido; si algún import falla, mira los ejemplos de su repo para tu versión. Los puntos más probables:

1. **Imports de contexto** (`LLMContext`, `LLMContextAggregatorPair`). En versiones antiguas se usaba `OpenAILLMContext` y `llm.create_context_aggregator(context)`.
2. **`vad_analyzer` en los parámetros del transport.** En versiones recientes el VAD y la detección de turno pueden configurarse en otro sitio.
3. **`LiveOptions`** viene del SDK de Deepgram; comprueba que la versión instalada lo expone así.

### Primeros ajustes que probablemente necesites
- **Te corta al pensar**: aumenta el silencio que el VAD necesita para dar tu turno por terminado. En `SileroVADAnalyzer` se configura con `params=VADParams(stop_secs=...)`; para entrevistas, entre 1 y 1,5 segundos suele ir mejor que el valor por defecto. Pipecat también tiene detectores de turno basados en modelo (smart turn), que miran la entonación y no solo el silencio; es la mejora natural cuando el ajuste de tiempo no baste.
- **Latencia**: con `enable_metrics=True` el log muestra cuánto tarda cada pieza. Lo normal es que el LLM sea la más lenta; un prompt más corto o un modelo más rápido es la primera palanca.

---

## 6. Siguientes pasos (por orden de valor)

1. **Entrevistador técnico** (`prompts/tech_lead.md`): system design y deep dive en tus proyectos.
2. **Memoria entre sesiones**: el evaluador guarda tus puntos débiles en un fichero y el siguiente entrevistador los recibe para insistir en ellos.
3. **Investigación de la empresa**: un paso previo que busca información pública de la empresa y la añade al prompt.
4. **WebRTC** (`SmallWebRTCTransport` de Pipecat): usarlo desde el navegador o el móvil, con cancelación de eco.
5. **Pronunciación**: análisis del audio (no del texto), por ejemplo con Azure Pronunciation Assessment.
6. **Modo C1**: otro `persona` (examinador Cambridge) y otra rúbrica en el evaluador. El núcleo es el mismo.
