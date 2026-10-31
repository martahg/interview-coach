# Interview Coach (MVP)

Simulador de entrevistas en inglés por voz en tiempo real. Hablas con un "recruiter" generado por IA y, al terminar, otro modelo te devuelve un informe con feedback de contenido (estructura STAR, lo que faltó) y de inglés (errores concretos, patrones, nivel CEFR estimado).

```
inputs/cv.md, inputs/jd.md ──┐
inputs/keyterms.txt ─────────┤
prompts/recruiter.md ────────┤
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
              VAD + Smart Turn         STT                  LLM                   TTS
```

El reto no es cada pieza por separado, es que **todo ocurra en menos de ~1 segundo** para que se sienta natural. Para conseguirlo, nada espera a que el paso anterior termine del todo: cada pieza trabaja en **streaming** y pasa resultados parciales a la siguiente. Por ejemplo, el TTS empieza a hablar con la primera frase del LLM mientras este sigue generando la segunda.

### Conceptos clave

**Frame.** La unidad de datos que circula por el pipeline. Un trozo de audio de 20ms es un frame, una palabra transcrita es un frame, "el usuario ha empezado a hablar" es un frame. Piensa en mensajes de una cola, pero en memoria y con milisegundos de vida.

**Pipeline.** Una lista ordenada de procesadores. Cada frame entra por arriba y cada procesador decide si lo transforma, lo deja pasar o genera frames nuevos. Es el mismo patrón que un pipeline de middlewares HTTP o un flujo de Pub/Sub, pero síncrono en el tiempo real del audio.

**Transport.** La puerta de entrada y salida del audio. En el MVP es tu micro y tu altavoz (`LocalAudioTransport`). Más adelante se cambia por WebRTC para usarlo desde el navegador o el móvil, y el resto del pipeline no se toca.

**VAD (Voice Activity Detection).** Un modelo pequeño (Silero, corre en local en tu CPU) que distingue voz de silencio o ruido. Responde a "¿está hablando alguien ahora mismo?".

**Turn-taking / endpointing.** Decidir que **has terminado tu turno**. Es lo más delicado de todo el sistema: si espera poco, te interrumpe cuando haces una pausa para pensar; si espera mucho, la conversación se siente lenta. Aquí se combinan dos señales y el turno solo se cierra cuando **las dos** están de acuerdo:
- **Smart Turn**: un modelo local que analiza la entonación y decide si la frase suena terminada.
- **Silencio mínimo** (`USER_SPEECH_TIMEOUT`, 1,5 s por defecto): evita que una pausa breve para pensar se tome como fin de turno.

**Barge-in (interrupciones).** Si empiezas a hablar mientras el entrevistador habla, se calla y te escucha. Lo activa `allow_interruptions=True`. Pipecat corta el audio pendiente y descarta la parte de la respuesta que no llegó a decirse.

**Contexto.** El historial de la conversación que se envía al LLM en cada respuesta. El system prompt (persona + CV + oferta) va aparte, en el `system_instruction` del LLM. Los LLM no tienen memoria propia: "recordar" lo que dijiste hace 5 minutos es simplemente que eso va incluido en cada petición.

---

## 2. Cada pieza y por qué la elegí

### Pipecat — el orquestador
Framework open source en Python (licencia BSD) que resuelve la parte difícil: mover frames entre servicios, gestionar el streaming, los turnos, las interrupciones y la cancelación. Tú solo declaras el orden de las piezas. Alternativa: LiveKit Agents, más orientado a producción con muchos usuarios simultáneos; para uso personal Pipecat es más directo.

### Deepgram — STT (speech-to-text)
Convierte tu voz en texto mientras hablas. Devuelve dos tipos de resultado:
- **Interim**: lo que cree que estás diciendo ahora, cambia sobre la marcha.
- **Final**: la versión definitiva de un fragmento.

Configuración importante en `interview.py`:
- `smart_format=False`: no "embellece" el texto, para que el evaluador vea cómo hablaste de verdad.
- `keyterm`: lista de nombres propios y jerga técnica (de `inputs/keyterms.txt`) que el STT debe reconocer mejor. Sin ella, términos como "serializable" o el nombre de una empresa salen mal transcritos y el evaluador los confunde con errores tuyos.
- Las muletillas ("um", "uh") no se conservan: el SDK actual de Deepgram no expone `filler_words` en streaming. El evaluador lo sabe y no juzga la fluidez por su ausencia.

### Claude — LLM (el entrevistador)
Genera las respuestas del recruiter a partir del contexto. Usa un modelo rápido (Sonnet) porque lo que importa es el **tiempo hasta el primer token**: cuanto antes empiece a generar, antes empieza a hablar el TTS.

El comportamiento entero está en `prompts/recruiter.md`. Cosas que importan para voz:
- **Sin markdown ni listas**: el TTS leería los asteriscos o sonaría a robot.
- **Una pregunta por turno**: igual que un humano.
- **No dar feedback durante la llamada**: el entrevistador se mantiene en su papel; el feedback es trabajo del evaluador.

La API de Claude exige al menos un mensaje de usuario, así que la conversación arranca con un mensaje interno (`START_MSG`) que pide al entrevistador abrir la llamada. Se filtra al guardar la sesión.

### ElevenLabs — TTS (text-to-speech)
Convierte el texto en voz natural, también en streaming. `ELEVENLABS_VOICE_ID` elige la voz; cuando añadas el entrevistador técnico, dale otra voz distinta para que la diferencia de rol se note.

**En el plan gratuito, usa una voz predeterminada de ElevenLabs** (las que vienen con la cuenta). Las voces de la Voice Library de la comunidad devuelven `402 Payment Required` por API aunque las añadas a "My Voices".

Alternativas: Cartesia, o el TTS de Deepgram (Aura-2), que reutiliza la misma key del STT.

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
| `inputs/cv.md`, `inputs/jd.md` | Tu CV y la oferta (en inglés) |
| `inputs/keyterms.txt` | Opcional: términos para el STT, uno por línea (`#` para comentarios) |
| `.env` | Claves y configuración (no se sube al repo) |
| `sessions/` | Se crea solo: transcripciones (`.json`) e informes (`.report.md`) |

Para añadir otro entrevistador (por ejemplo, el técnico), crea `prompts/tech_lead.md` y ejecuta con `PERSONA=tech_lead`.

### Variables de `.env`

| Variable | Para qué |
|---|---|
| `DEEPGRAM_API_KEY`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY` | Claves de los servicios |
| `ELEVENLABS_VOICE_ID` | Voz del entrevistador |
| `INTERVIEWER_MODEL` | Modelo del entrevistador (prioriza latencia) |
| `EVALUATOR_MODEL` | Modelo del evaluador (prioriza calidad) |
| `PERSONA` | Fichero de `prompts/` a usar como entrevistador |
| `USER_SPEECH_TIMEOUT` | Opcional. Segundos de silencio para cerrar tu turno (1,5 por defecto) |

---

## 4. Puesta en marcha

Requisitos: Python 3.11+ (probado con 3.13), y en macOS `brew install portaudio` (lo necesita PyAudio para el micro).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # rellena las claves en .env, NO en .env.example
```

Rellena `inputs/cv.md`, `inputs/jd.md` y, si quieres, `inputs/keyterms.txt`. Para que tus datos reales no se suban al repo:
```bash
git update-index --skip-worktree inputs/cv.md inputs/jd.md inputs/keyterms.txt
```

Y a practicar:
```bash
python interview.py                          # habla; Ctrl+C para terminar
python evaluate.py sessions/<fichero>.json   # genera sessions/<fichero>.report.md
```

**Usa auriculares.** En local no hay cancelación de eco: si el micro capta el altavoz, el sistema oye al entrevistador, cree que eres tú y se interrumpe a sí mismo.

**Para terminar, pulsa Ctrl+C sin decir nada.** Todo lo que digas se transcribe y acaba en el informe.

La primera vez, macOS pide permiso para que la terminal use el micrófono (Ajustes del Sistema → Privacidad y seguridad → Micrófono).

Claves necesarias: Deepgram, Anthropic y ElevenLabs. Deepgram y ElevenLabs tienen tier gratuito suficiente para probar; la API de Anthropic es de pago (unos pocos dólares dan para muchas sesiones) y se factura aparte de la suscripción de Claude.

---

## 5. Ajustes y problemas conocidos

Probado con **Pipecat 1.14** y **deepgram-sdk 7.16**. Pipecat evoluciona rápido; si actualizas y algún import falla, compara con los ejemplos de su repo para tu versión.

| Síntoma | Causa probable | Solución |
|---|---|---|
| Te corta cuando haces una pausa para pensar | El turno se cierra demasiado pronto | Sube `USER_SPEECH_TIMEOUT` (prueba 2) |
| Tarda mucho en responder cuando terminas | Turno demasiado conservador, o LLM lento | Baja `USER_SPEECH_TIMEOUT` (1,2); mira la latencia de cada pieza en el log (`enable_metrics=True`) |
| Nombres propios o términos técnicos mal transcritos | El STT no los conoce | Añádelos a `inputs/keyterms.txt` |
| Deepgram 401, ElevenLabs 403 y Anthropic "no api_key" a la vez | `.env` vacío o claves en `.env.example` | Rellena `.env` |
| ElevenLabs `402` o "completed with no audio" | Voz de la Voice Library en plan gratuito | Usa una voz predeterminada |
| ElevenLabs "voice_id does not exist" | Voz no añadida a tu cuenta, o ID mal copiado | Revisa el ID en "My Voices" |
| El entrevistador se interrumpe a sí mismo | El micro capta el altavoz | Auriculares |

---

## 6. Siguientes pasos (por orden de valor)

1. **Entrevistador técnico** (`prompts/tech_lead.md`): system design y deep dive en tus proyectos.
2. **Keyterms automáticos**: extraer de `cv.md` y `jd.md` los términos para el STT al arrancar, sin mantener el fichero a mano.
3. **Memoria entre sesiones**: el evaluador guarda tus puntos débiles en un fichero y el siguiente entrevistador los recibe para insistir en ellos.
4. **Colgar al despedirse**: una herramienta `end_call` para que el entrevistador termine la sesión sin Ctrl+C.
5. **Investigación de la empresa**: un paso previo que busca información pública de la empresa y la añade al prompt.
6. **WebRTC** (`SmallWebRTCTransport` de Pipecat): usarlo desde el navegador o el móvil, con cancelación de eco.
7. **Pronunciación**: análisis del audio (no del texto), por ejemplo con Azure Pronunciation Assessment.
8. **Modo C1**: otro `persona` (examinador Cambridge) y otra rúbrica en el evaluador. El núcleo es el mismo.
