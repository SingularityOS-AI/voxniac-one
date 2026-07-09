# PLAN UI — "Control Room" por capas (rediseño Fase 3.6)

> Spec del Arquitecto (2026-07-09). Referencia visual: proyecto Stitch "Voxniac ONE Control Room"
> (stitch.withgoogle.com/projects/2392454939321961709) — 3 pantallas generadas, una por capa.
> El repo git ya existe (main, tags); commitear al terminar.

## Objetivo

Reestructurar `static/index.html` + `static/style.css` al layout "control room tipo proceso":
**sidebar izquierda fija** con el pipeline de 3 capas + **área principal** que muestra la capa activa.
La app ya está on-brand (voxniac.com); esto es reorganización de layout + pulido, NO un cambio de identidad.

## Reglas de hierro (violarlas = RECHAZADO)

1. **Cero cambios de comportamiento en app.js**: todos los `id` existentes se conservan tal cual
   (`callBtn`, `prospectCallBtn`, `prospectPhone`, `tunnelDot`, `tunnelStatusTxt`, `interviewMicBtn`,
   `interviewSendBtn`, `interviewInput`, `interviewMessages`, `interviewApproveBtn`, `interviewBackBtn`,
   `interviewResetBtn`, `profileReloadBtn`, `profileReloadStatus`, `interviewStatus`, `interviewStateBadge`,
   `interviewerModelSelect`, `llmSelect`, `agentOpening`, `callHint`, `wsDot`, `wsStatusTxt`,
   `transcriptMessages`, `prospectTranscriptMessages`, `prospectCallResult`, `prospectCallStatus`,
   `latStt`, `latTtft`, `latTtfa`, `latE2e`, `statusArea`, `historyBody`, `setupCard`).
   Solo se permite en app.js el código NUEVO para cambiar de capa (mostrar/ocultar paneles) y
   quitar el `<details>` si se reemplaza por el panel de Capa 3 (ajuste mínimo documentado).
2. **Cero dependencias nuevas**: nada de Tailwind, ningún framework, ningún CDN nuevo. CSS propio.
   Las fuentes de Google ya cargadas (Space Grotesk / Inter / JetBrains Mono) son las únicas externas.
3. Tokens de marca (idénticos a voxniac.com y al CSS actual): fondo `#FAF6EF`, tinta `#2B2420`,
   acento naranja `#C7502E` (usar el naranja ya definido en style.css), tarjetas blancas con borde suave,
   labels uppercase en JetBrains Mono. Nada de grises genéricos de SaaS.

## Layout

- **Top bar**: wordmark "VOXNIAC ONE" + sub "Streaming Voice Agent · Live Cascade" a la izquierda;
  pill de estado del WS (`wsDot`/`wsStatusTxt`) a la derecha (como hoy).
- **Sidebar izquierda (~240px, fija)**: título mono "PIPELINE" y 3 items conectados verticalmente
  por una línea sutil, cada uno con badge numérico, nombre y punto de estado:
  1. **CAPA 1 — Voice Engine** → panel: selector LLM, agent opening, botón Start Call, transcript en vivo
     del navegador, 4 tiles de latencia, historial de turnos. (Todo lo que hoy está en la vista principal.)
  2. **CAPA 2 — Phone Outreach** → panel: pill del túnel, input teléfono + botón "Call prospect",
     resultado/estado de llamada, transcript en vivo de la llamada Twilio.
  3. **CAPA 3 — Agent Setup** → panel: selector de modelo del entrevistador, chat de la entrevista,
     mic + input + Send, controles Approve/Adjust/Reset/Reload profile, estados.
  Item activo: fondo crema más oscuro/borde naranja. Cambiar de capa = mostrar su panel (CSS class,
  sin recargar, sin perder estado de WebSockets — los paneles se ocultan con `display:none`, no se
  desmontan del DOM).
- **Responsive**: bajo ~900px la sidebar colapsa a una fila horizontal de 3 pestañas arriba.
- Al llegar un evento de llamada Twilio (transcript del monitor) SI el usuario no está en Capa 2,
  poner un punto/badge de actividad en el item Capa 2 de la sidebar (no cambiar de capa solo).

## Detalles de pulido (de las pantallas Stitch)

- Tiles de métricas: número grande en mono + label uppercase pequeño, borde inferior naranja sutil.
- Burbujas: usuario crema-gris a la izquierda, agente con tinte naranja a la derecha (ya existe similar).
- Botones: primario naranja pleno (como "Call prospect" actual), secundarios outline tinta.
- El `<details>` "Agent Setup" desaparece: la Capa 3 es un panel de primera clase.

## Criterios de aceptación

1. Las 3 capas navegan sin recargar y sin romper: Start Call funciona, tunnel pill se actualiza,
   entrevista responde, mic graba, Reload profile responde — mismo comportamiento verificado hoy.
2. `pytest -v` (65) y `ruff check .` intactos (no se toca Python; si se toca server.py por algo, justificar).
3. Sin scroll horizontal a 1280px ni a 375px de ancho.
4. Commit en main con mensaje claro + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
