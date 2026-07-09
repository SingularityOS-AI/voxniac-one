# PLAN FASE 3.5 — Setup por voz, monitor de llamadas Twilio y perfil estructurado

> Spec aprobada por el Arquitecto (Fable 5) — 2026-07-09. Deadline hackathon AMD: 11-jul.
> Base: PLAN_ONE.md, PLAN_FASE3.md. Código en `C:\Users\gabri\Desktop\voxniac\voxniac-zero-ONE`.

## Contexto y hallazgo

El CEO reportó que "la conversación de Twilio no se registra". Diagnóstico real (explorador 2026-07-09):

- `cascade.py` es agnóstico del transporte: emite `stt_partial` / `stt_final` / `agent_token` / `agent_done` / `metrics` vía `transport.send_event()` y llama `log_turn()` (cascade.py:636) en TODOS los turnos, browser y Twilio por igual → el JSONL **sí** tiene los turnos de Twilio.
- `TwilioTransport.send_event()` (transports.py:97-103) solo traduce `barge_in → clear` e **ignora todo lo demás** → la UI nunca ve el transcript de una llamada telefónica.
- `voxniac_one_log.jsonl` no tiene `call_id` ni canal → imposible reconstruir una llamada concreta.

## Prioridades (en este orden — si el tiempo aprieta, se corta por abajo)

### P1 — Monitor en vivo de llamadas Twilio + registro por llamada

**Objetivo:** al lanzar una llamada desde "Call a Prospect", la UI muestra el transcript en vivo (igual que en Live Call) y la llamada queda registrada como unidad.

1. **Fan-out de eventos:** crear un bus de eventos simple (módulo nuevo `event_bus.py` o dentro de `server.py`): un set global de WebSockets de UI suscritos como monitores. Endpoint nuevo `GET /ws/monitor` (o reutilizar el WS de UI existente si es más simple — decisión del implementador, documentarla). Cada sesión Twilio publica al bus los mismos eventos que ya genera el cascade: `stt_final`, `agent_done`, `metrics` (los `stt_partial`/`agent_token` son opcionales; si entran fácil, mejor demo). Envolver cada evento con contexto: `{"channel":"twilio","call_id":"...","event":{...}}`.
   - Implementación sugerida (no obligatoria): `TwilioTransport.send_event()` deja de botar eventos y además de su lógica actual los publica al bus. Cero cambios en `cascade.py` si se resuelve en el transporte.
2. **call_id:** generar un id por llamada (timestamp + últimos 4 dígitos del teléfono, ej. `20260709_1432_4446`) al crear la sesión de `/ws/twilio` (y también para sesiones browser, con prefijo `browser_`). Pasarlo a `log_turn()` → columnas nuevas en el JSONL: `call_id`, `channel`.
3. **Transcript por llamada:** al cerrar la sesión Twilio, escribir `transcripts/CALL_<call_id>.md` con: número, hora inicio/fin, modelo LLM, y la secuencia usuario/agente con métricas por turno. Directorio `transcripts/` junto a `recordings/`. Debe escribirse también si la llamada se cae (finally/try — nunca perder el transcript).
4. **UI:** en la tarjeta "Call a Prospect" (index.html + app.js), un área de transcript que se llena en vivo con los eventos del monitor cuando hay llamada activa. Reusar los estilos de burbujas del Live Call. Indicador de estado: "📞 En llamada con +1XXX… / colgó".

**Criterio de aceptación:** lanzar una llamada real (o simular una sesión `/ws/twilio` con frames mulaw de prueba), ver los turnos aparecer en la tarjeta, y al colgar existe `transcripts/CALL_*.md` completo + turnos en JSONL con `call_id` y `channel:"twilio"`.

### P2 — Entrevistador por nota de voz (setup hablado)

**Objetivo:** el CEO puede contarle su negocio al promptizador hablando tranquilo, no solo tecleando.

1. **UI (tarjeta Agent Setup):** botón de micrófono junto al input de texto. Presionar → graba con `MediaRecorder` (webm/opus); soltar/segundo click → detiene y envía. Estado visual grabando (rojo pulsante) y "transcribiendo…".
2. **Backend:** endpoint `POST /interview/audio` que recibe el blob de audio, lo manda a **Deepgram pre-recorded REST** (`https://api.deepgram.com/v1/listen`, modelo nova-3, mismo API key ya en `.env`; acepta webm/opus directo, sin transcodificar). El texto resultante se inyecta al `InterviewSession` exactamente como un `user_msg` de texto y la respuesta fluye por el WS `/ws/interview` ya existente.
3. **Eco en el chat:** el texto transcrito aparece como burbuja del usuario en el chat de Agent Setup (evento `user_transcribed` por el WS o en la respuesta del POST — decisión del implementador).
4. **Selector de modelo del entrevistador:** los valores hoy están hardcodeados en interviewer.py:59-62. Hacerlos configurables: `INTERVIEWER_MODEL_ID` leído de config/env con default `accounts/fireworks/models/gpt-oss-120b` y alternativa documentada `accounts/fireworks/models/gpt-oss-20b`. Un select simple en la tarjeta Agent Setup (120B calidad / 20B rápido) es suficiente; persiste en config.json.
5. **Sin TTS de respuesta** en esta fase: voz entra, texto sale. (El entrevistador hablando es Fase 4 si acaso.)

**Criterio de aceptación:** grabar una nota de voz en Agent Setup describiendo un negocio → aparece transcrita como mensaje propio → el entrevistador responde con su siguiente pregunta. Flujo de texto intacto.

### P3 — Perfil estructurado estilo ElevenLabs (control del agente)

**Objetivo:** más control sobre el comportamiento del agente sin tocar código. NO es ingeniería inversa: es adoptar el framework público de prompting de ElevenLabs (6 bloques) y Vapi.

1. **Nueva estructura de `agent_profile.json`** (retrocompatible — si existe `system_prompt` plano y no hay bloques, se usa tal cual):
   ```json
   {
     "prompt_blocks": {
       "personality": "quién es el agente (nombre, rol, carácter)",
       "environment": "está en una llamada telefónica saliente, audio puede cortarse…",
       "tone": "frases de 1-2 oraciones, <30 palabras, natural, sin re-presentarse",
       "goal": "objetivo de la llamada y pasos (calificar → agendar → escalar)",
       "guardrails": "qué jamás decir/hacer; regla de escalación a humano"
     },
     "truth_base": { "servicios/precios, ICP, dolores, objeciones (≥3)": "…" },
     "agent_opening": "…", "voice": "…", "llm_model": "…"
   }
   ```
2. **Builder determinista:** función `build_system_prompt(profile)` (en vz_config.py o módulo propio) que compone los bloques + truth_base en un system prompt con secciones marcadas (`## PERSONALITY`, etc.). `cascade.py` consume el resultado del builder en vez del string plano (cambio mínimo en los 4 puntos de consumo: cascade.py:148, 202, 225, 537 — idealmente centralizar en un solo accessor).
3. **Promptizador:** `interviewer.py` genera el perfil ya en esta estructura (ajustar el prompt del plan/approve). Las reglas duras anti-loop/apertura corta de Fase 2 van en `tone`/`guardrails`, no hardcodeadas.
4. **Hot-reload** existente debe seguir funcionando (editar un bloque del JSON a mano → siguiente llamada lo usa).
5. **Migrar el perfil actual de Sharon** a la nueva estructura (y el fallback embebido).

**Criterio de aceptación:** editar `guardrails` a mano cambia el comportamiento en la siguiente sesión sin reiniciar; entrevista nueva genera perfil con bloques; perfil viejo plano sigue funcionando.

## Reglas de la casa (no negociables)

- Cero dependencias nuevas de pip si no son imprescindibles (Deepgram REST se llama con httpx/aiohttp ya presente).
- Nunca tocar `.env` ni loguear secretos; nada de teléfonos completos en nombres de archivo si el transcript se va a compartir (últimos 4 dígitos OK).
- No romper: `/ws/call` (browser), `/ws/twilio`, `call_launcher.py`, hot-reload de perfil, `stream_chat` retrocompatible.
- Archivos completos y ejecutables. Probar con `pytest -v` lo que sea testeable sin red; smoke manual documentado para lo demás.
- Latencia de voz intocable: nada de trabajo síncrono pesado dentro del loop de audio (el transcript por llamada se escribe al cerrar, el fan-out es fire-and-forget).
