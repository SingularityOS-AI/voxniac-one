# Plan — Voxniac ONE: Fase 3 final (UI Control Room v2) + Fase 4 (Campañas)

## Contexto

Voxniac ONE (repo `C:\Users\gabri\Desktop\voxniac\voxniac-zero-ONE`, GitHub privado `SingularityOS-AI/voxniac-one`, main en `bb15a6a` QA-aprobado) ya tiene: motor de voz streaming, llamadas Twilio reales, entrevistador/promptizador (gpt-oss-120B), monitor en vivo y layout "Control Room" de 3 capas. El CEO pide cerrar la Fase 3 (estética final usando `frontend-stich\DESIGN.md` como fuente de verdad + poda de controles sin función + reordenar capas + builder estilo Lovable en Agent Setup) y ejecutar la Fase 4 (gestión de campañas estilo neura-sales: leads, caliente/frío, prompt por lead, llamada), subiendo todo al mismo GitHub esta noche. Mañana: pitch + deploy AMD. Deadline hackathon: 11-jul.

**Hallazgos de exploración que condicionan el plan:**
- `frontend-stich/` = pantallas Stitch estáticas amarradas a Tailwind CDN, sin los ids que usa `app.js` → NO se adopta su HTML; se portan sus tokens (`DESIGN.md`) a CSS propio. El SDK (`stitch-automate.mjs`, skill `/stitch`) se usa para diseñar la pantalla nueva de Capa 4.
- neura-sales = Next.js + Gemini + **Vapi**. Reutilizable: prompt templates (`src/app/api/generate-prompt/route.ts:26-59`), clasificación HOT/WARM/COLD (`src/ai/flows/classify-lead-sentiment.ts:41-57`, definida pero nunca cableada), modelo de lead (`src/lib/types.ts:3-20`), esquema SQLite (`src/lib/db.ts:29-71`). NO reutilizable: Vapi (las llamadas salen por NUESTRO motor Twilio — tesis del hackathon), scraper Google Maps (frágil y fabrica teléfonos/emails falsos), Firestore, Genkit.
- Decisiones del CEO: GitHub privado ya activo; sin números reales de leads (ofuscar; API Apollo pagada vendrá después); scraper mejor → interfaz enchufable, no esta noche; alcance "todo o nada".

## Nuevo orden de capas

1. **Capa 1 — Voice Engine** (voz + modelo, como hoy)
2. **Capa 2 — Agent Setup** (builder: chat izquierda + editor manual derecha)
3. **Capa 3 — Phone Outreach** (como hoy)
4. **Capa 4 — Campaigns** (nueva)

## Etapa A — Fase 3 final: estética DESIGN.md + reorden + poda

Archivos: `static/index.html`, `static/style.css`, `static/app.js` (solo navegación), sin tocar Python.

- Portar tokens de `frontend-stich\DESIGN.md` a variables CSS en `style.css` (paleta con roles #a63818/#c7502e/#FAF6EF/#2B2420, escala tipográfica exacta, spacing base 8px, radios, tarjetas blancas borde 1px rgba(43,36,32,.1) con header mono uppercase + línea inferior, "pipeline connector" 1px al 15-20%, badges pulsantes, consola oscura para log).
- Reordenar sidebar al nuevo orden 1-4 (Capa 4 con badge "SOON" hasta Etapa C).
- **Poda** (nada sin función real): fuera Diagnostics/Terminal del nav, sliders Pitch/Speed/Stability, métricas placeholder (Network Jitter, Neural Metrics, Voice Cloning bar), avatares de CDN Google. Iconos: inline SVG o Material Symbols (mismo origen Google Fonts ya usado) — solo para controles reales.
- **Regla de hierro heredada de `PLAN_UI_CONTROL_ROOM.md`: cero ids renombrados/eliminados, cero Tailwind/CDN nuevos, paneles con display:none sin desmontar.**

## Etapa B — Capa 2 builder (chat + editor manual)

- Layout split en el panel Agent Setup: **izquierda** el chat del entrevistador (existente, ids intactos); **derecha** "Profile Editor" con campos editables: `agent_opening`, `prompt_blocks.{personality, environment, tone, goal, guardrails}` (textareas), `truth_base` (textarea JSON validado), `voice`, `llm_model`. Esto recupera en UI los guardrails/personalidad que hoy solo viven en `agent_profile.json`.
- Backend (`server.py` + `vz_config.py`): `GET /profile` (perfil actual) y `POST /profile` (validar → escribir `agent_profile.json` → hot-reload, reusando `reload_agent_profile()` existente). Al "Approve" del entrevistador (flujo existente), el editor se refresca con los bloques generados para ajuste manual.
- Tests pytest de ambos endpoints (roundtrip con tmp_path, validación de JSON inválido → 400).

## Etapa C — Fase 4: Capa Campaigns

**Diseño primero:** generar la pantalla "CAPA 4 — Campaigns" con el SDK de Stitch (skill `/stitch`: `node stitch-automate.mjs generate` desde `frontend-stich/`, prompt con lenguaje de DESIGN.md; verificar code.html/screen.png). Solo referencia visual — el HTML real es nuestro.

**Backend** — módulo nuevo `leads.py` + endpoints en `server.py`:
- SQLite stdlib (`leads.db`, gitignored). Modelo por lead (de types.ts): contactName, companyName, phone, email, status COLD/WARM/HOT, isBallena, industry, companySize, seniority, painPoints (JSON), customFirstMessage, customSystemPrompt, lastCallDate, lastCallId, classificationReasoning.
- `POST /leads/import` — CSV formato export Apollo (columnas reales documentadas en la exploración). **Ofuscación en import:** teléfono → se guarda enmascarado (`+1305•••4821`) y NUNCA el real; email → `dominio` solo. `GET /leads` (filtro por status), `PATCH /leads/{id}`.
- `POST /leads/{id}/generate_prompt` — port a Python/Fireworks del template de neura-sales (inyecta contactName/companyName/industry/painPoints → devuelve firstMessage + systemPrompt, editable). Modelo: gpt-oss-120b (mismo del promptizador), `response_format json`.
- `POST /leads/{id}/call` — dispara llamada por NUESTRO motor: extiende `POST /call` para aceptar override opcional `{first_message, system_prompt, lead_id}` que la sesión Twilio usa en lugar del perfil global (sin tocar el perfil en disco). **DEMO_SAFE_MODE (default true): toda llamada de lead marca a `CALL_ME_NUMBER`, jamás al número del lead.**
- **Cablear la clasificación** (lo que neura-sales dejó suelto): al cerrar una sesión Twilio con `lead_id`, correr el port de classify-lead-sentiment (Fireworks) sobre el transcript → actualizar `status` HOT/WARM/HOT + reasoning + lastCallId.
- Scraper: `scrapers/` con interfaz stub documentada (post-hackathon; decisión: el de Maps fabrica datos falsos, descartado).
- Tests: import+ofuscación, generate_prompt (LLM mockeado), override de /call, clasificación (mock) → update de status.

**UI Capa 4:** tabla de leads (chips COLD/WARM/HOT, 🐋 ballena), botón Import CSV, panel de detalle (painPoints, prompt generado editable, botones "Generate prompt" y "Call"), transcript en vivo reusando el monitor existente.

## Seguridad / Gates

- `frontend-stich/node_modules/` y `frontend-stich/.env` (STITCH_API_KEY) NO entran al repo — verificar .gitignore ANTES del primer `git add` que incluya esa carpeta. `leads.db` gitignored. Escaneo anti-secretos antes de cada push (patrón ya usado hoy).
- Nada de números reales: ofuscación en import + DEMO_SAFE_MODE.
- Backup = git; push a origin tras cada etapa.

## Equipo y verificación

- `implementador` ejecuta A → B → C secuencial (mismo agente, con presupuesto por etapa); Arquitecto verifica en Chrome real tras cada etapa (navegación, WS vivos, consola limpia, sin scroll horizontal); `revisor-qa` valida el conjunto al final contra este plan.
- `pytest -v` + `ruff check .` en verde en cada commit; `node --check` para app.js.
- E2E final: importar CSV demo → generar prompt de un lead → "Call" (a CALL_ME_NUMBER) → ver transcript en vivo → status actualizado HOT/WARM/COLD.
- Commits por etapa + tag `fase-4` al final; push a `SingularityOS-AI/voxniac-one`.
- Documentar: spec `PLAN_FASE4_CAMPAIGNS.md` en el repo (este plan), nota en vault + INDEX, memoria actualizada. Si el contexto llega a ~40%, generar HANDOFF antes de continuar (Gate 4).
