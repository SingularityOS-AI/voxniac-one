#!/usr/bin/env node
/**
 * stitch-automate.mjs
 *
 * Automatiza la generacion de pantallas UI para Voxniac ONE usando el SDK
 * oficial de npm "@google/stitch-sdk" (v0.3.5). Llamadas directas al SDK de
 * alto nivel (stitch / Project / Screen) — sin el conector MCP de Claude Code.
 *
 * La API key se lee de STITCH_API_KEY en el entorno (via .env + dotenv).
 * NUNCA imprimas ni loguees su valor.
 *
 * Uso:
 *   node stitch-automate.mjs generate --prompt "..." [--project <id>] [--device MOBILE|DESKTOP|TABLET|AGNOSTIC] [--out <dir>] [--title "Nuevo Proyecto"]
 *   node stitch-automate.mjs edit --project <id> --screen <id> --prompt "..." [--device ...] [--model GEMINI_3_PRO|GEMINI_3_FLASH] [--out <dir>]
 *   node stitch-automate.mjs variants --project <id> --screen <id> --prompt "..." [--count 3] [--range REFINE|EXPLORE|REIMAGINE] [--aspects LAYOUT,COLOR_SCHEME] [--device ...] [--model ...] [--out <dir>]
 *   node stitch-automate.mjs list-projects
 *   node stitch-automate.mjs list-screens --project <id>
 */

import "dotenv/config";
import { stitch, StitchError } from "@google/stitch-sdk";
import { writeFile, mkdir } from "node:fs/promises";
import path from "node:path";

const DOWNLOAD_TIMEOUT_MS = 60_000; // timeout razonable para los fetch de descarga

// ---------------------------------------------------------------------------
// Helpers de CLI (sin librerias externas — parsing manual de process.argv)
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const opts = {};
  for (let i = 0; i < rest.length; i++) {
    const token = rest[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const next = rest[i + 1];
      if (next === undefined || next.startsWith("--")) {
        opts[key] = true; // flag booleano
      } else {
        opts[key] = next;
        i++;
      }
    }
  }
  return { command, opts };
}

function printUsage() {
  console.log(`Uso:
  node stitch-automate.mjs generate --prompt "..." [--project <id>] [--device MOBILE|DESKTOP|TABLET|AGNOSTIC] [--out <dir>] [--title "Titulo"]
  node stitch-automate.mjs edit --project <id> --screen <id> --prompt "..." [--device ...] [--model GEMINI_3_PRO|GEMINI_3_FLASH] [--out <dir>]
  node stitch-automate.mjs variants --project <id> --screen <id> --prompt "..." [--count 3] [--range REFINE|EXPLORE|REIMAGINE] [--aspects LAYOUT,COLOR_SCHEME] [--device ...] [--model ...] [--out <dir>]
  node stitch-automate.mjs list-projects
  node stitch-automate.mjs list-screens --project <id>
`);
}

// ---------------------------------------------------------------------------
// Funciones reutilizables del SDK
// ---------------------------------------------------------------------------

/**
 * Genera una pantalla nueva. Si no se pasa projectId, crea un proyecto nuevo primero.
 * @param {string|undefined} projectId
 * @param {string} prompt
 * @param {"MOBILE"|"DESKTOP"|"TABLET"|"AGNOSTIC"|undefined} deviceType
 * @param {string} [newProjectTitle]
 * @returns {Promise<{project: import("@google/stitch-sdk").Project, screen: import("@google/stitch-sdk").Screen}>}
 */
async function generateScreen(projectId, prompt, deviceType, newProjectTitle) {
  let project;
  try {
    if (projectId) {
      project = stitch.project(projectId);
    } else {
      const title = newProjectTitle || `Voxniac ONE - ${new Date().toISOString()}`;
      project = await stitch.createProject(title);
      console.log(`[stitch] Proyecto nuevo creado: ${project.id}`);
    }
  } catch (err) {
    throw wrapError("crear/referenciar proyecto", err);
  }

  let screen;
  try {
    screen = await project.generate(prompt, deviceType);
  } catch (err) {
    throw wrapError("generar pantalla (project.generate)", err);
  }

  return { project, screen };
}

/**
 * Edita una pantalla existente dentro de un proyecto.
 * @param {string} projectId
 * @param {string} screenId
 * @param {string} prompt
 * @param {"MOBILE"|"DESKTOP"|"TABLET"|"AGNOSTIC"|undefined} deviceType
 * @param {"GEMINI_3_PRO"|"GEMINI_3_FLASH"|undefined} modelId
 * @returns {Promise<import("@google/stitch-sdk").Screen>}
 */
async function editScreen(projectId, screenId, prompt, deviceType, modelId) {
  let project, screen;
  try {
    project = stitch.project(projectId);
    screen = await project.getScreen(screenId);
  } catch (err) {
    throw wrapError(`obtener pantalla ${screenId} del proyecto ${projectId}`, err);
  }

  try {
    return await screen.edit(prompt, deviceType, modelId);
  } catch (err) {
    throw wrapError("editar pantalla (screen.edit)", err);
  }
}

/**
 * Genera variantes de una pantalla existente.
 * @param {string} projectId
 * @param {string} screenId
 * @param {string} prompt
 * @param {{variantCount?: number, creativeRange?: "REFINE"|"EXPLORE"|"REIMAGINE", aspects?: string[]}} options
 * @param {"MOBILE"|"DESKTOP"|"TABLET"|"AGNOSTIC"|undefined} deviceType
 * @param {"GEMINI_3_PRO"|"GEMINI_3_FLASH"|undefined} modelId
 * @returns {Promise<import("@google/stitch-sdk").Screen[]>}
 */
async function generateVariants(projectId, screenId, prompt, options, deviceType, modelId) {
  let project, screen;
  try {
    project = stitch.project(projectId);
    screen = await project.getScreen(screenId);
  } catch (err) {
    throw wrapError(`obtener pantalla ${screenId} del proyecto ${projectId}`, err);
  }

  try {
    return await screen.variants(prompt, options, deviceType, modelId);
  } catch (err) {
    throw wrapError("generar variantes (screen.variants)", err);
  }
}

/**
 * Descarga code.html y screen.png de una pantalla a outDir, siguiendo el mismo
 * patron de carpetas que las 4 pantallas ya existentes en frontend-stich/.
 * @param {import("@google/stitch-sdk").Screen} screen
 * @param {string} outDir
 */
async function downloadScreenAssets(screen, outDir) {
  await mkdir(outDir, { recursive: true });

  let htmlUrl, imageUrl;
  try {
    htmlUrl = await screen.getHtml();
  } catch (err) {
    throw wrapError("obtener URL de HTML (screen.getHtml)", err);
  }
  try {
    imageUrl = await screen.getImage();
  } catch (err) {
    throw wrapError("obtener URL de imagen (screen.getImage)", err);
  }

  const htmlPath = path.join(outDir, "code.html");
  const imagePath = path.join(outDir, "screen.png");

  await downloadToFile(htmlUrl, htmlPath, "text");
  await downloadToFile(imageUrl, imagePath, "binary");

  console.log(`[stitch] Guardado: ${htmlPath}`);
  console.log(`[stitch] Guardado: ${imagePath}`);

  return { htmlPath, imagePath };
}

/**
 * Descarga una URL a disco con timeout via AbortSignal.timeout().
 * @param {string} url
 * @param {string} destPath
 * @param {"text"|"binary"} mode
 */
async function downloadToFile(url, destPath, mode) {
  let response;
  try {
    response = await fetch(url, { signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS) });
  } catch (err) {
    if (err.name === "TimeoutError") {
      throw new Error(`Timeout (${DOWNLOAD_TIMEOUT_MS}ms) descargando ${destPath}`);
    }
    throw new Error(`Fallo de red descargando ${destPath}: ${err.message}`);
  }

  if (!response.ok) {
    throw new Error(`Descarga fallida (${response.status} ${response.statusText}) para ${destPath}`);
  }

  if (mode === "text") {
    const text = await response.text();
    await writeFile(destPath, text, "utf-8");
  } else {
    const buffer = Buffer.from(await response.arrayBuffer());
    await writeFile(destPath, buffer);
  }
}

/** Envuelve errores del SDK (StitchError) o desconocidos en un mensaje claro. */
function wrapError(action, err) {
  if (err instanceof StitchError) {
    return new Error(
      `[stitch] Error al ${action}: [${err.code}] ${err.message}${
        err.suggestion ? ` — sugerencia: ${err.suggestion}` : ""
      }`
    );
  }
  return new Error(`[stitch] Error al ${action}: ${err.message || err}`);
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function main() {
  const { command, opts } = parseArgs(process.argv.slice(2));

  if (!command || opts.help) {
    printUsage();
    process.exitCode = command ? 0 : 1;
    return;
  }

  const outDir = opts.out ? path.resolve(opts.out) : path.resolve("stitch_output", command);

  switch (command) {
    case "generate": {
      if (!opts.prompt) {
        console.error("Error: falta --prompt");
        process.exitCode = 1;
        return;
      }
      const { project, screen } = await generateScreen(
        opts.project,
        opts.prompt,
        opts.device,
        opts.title
      );
      console.log(`[stitch] Pantalla generada: project=${project.id} screen=${screen.id}`);
      await downloadScreenAssets(screen, outDir);
      break;
    }

    case "edit": {
      if (!opts.project || !opts.screen || !opts.prompt) {
        console.error("Error: faltan --project, --screen y/o --prompt");
        process.exitCode = 1;
        return;
      }
      const edited = await editScreen(opts.project, opts.screen, opts.prompt, opts.device, opts.model);
      console.log(`[stitch] Pantalla editada: project=${opts.project} screen=${edited.id}`);
      await downloadScreenAssets(edited, outDir);
      break;
    }

    case "variants": {
      if (!opts.project || !opts.screen || !opts.prompt) {
        console.error("Error: faltan --project, --screen y/o --prompt");
        process.exitCode = 1;
        return;
      }
      const variantOptions = {
        variantCount: opts.count ? Number(opts.count) : undefined,
        creativeRange: opts.range,
        aspects: opts.aspects ? opts.aspects.split(",").map((a) => a.trim()) : undefined,
      };
      const variants = await generateVariants(
        opts.project,
        opts.screen,
        opts.prompt,
        variantOptions,
        opts.device,
        opts.model
      );
      console.log(`[stitch] ${variants.length} variante(s) generada(s)`);
      for (let i = 0; i < variants.length; i++) {
        const variantDir = path.join(outDir, `variant_${i + 1}_${variants[i].id}`);
        console.log(`[stitch] Variante ${i + 1}: screen=${variants[i].id}`);
        await downloadScreenAssets(variants[i], variantDir);
      }
      break;
    }

    case "list-projects": {
      const projects = await stitch.projects();
      console.log(`[stitch] ${projects.length} proyecto(s):`);
      for (const p of projects) {
        console.log(`  - ${p.id}`);
      }
      break;
    }

    case "list-screens": {
      if (!opts.project) {
        console.error("Error: falta --project");
        process.exitCode = 1;
        return;
      }
      const project = stitch.project(opts.project);
      const screens = await project.screens();
      console.log(`[stitch] ${screens.length} pantalla(s) en proyecto ${opts.project}:`);
      for (const s of screens) {
        console.log(`  - ${s.id}`);
      }
      break;
    }

    default:
      console.error(`Comando desconocido: ${command}`);
      printUsage();
      process.exitCode = 1;
  }
}

main().catch((err) => {
  console.error(err.message || err);
  process.exitCode = 1;
});

export { generateScreen, editScreen, generateVariants, downloadScreenAssets };
