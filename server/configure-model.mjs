// Idempotently declare jev/auto in the embedded router's protected user-model
// overlay. Existing user-curated routes are preserved.
import {
  readUserModels,
  writeUserModels,
} from "../router/src/user-models.mjs";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const isClef = process.env.JEV_DECISION_PROVIDER === "clef";
export const JEV_MODEL = Object.freeze({
  slug: "jev/auto",
  gatewayModel: "jev-auto",
  compHash: "jev-auto-user-v1",
  upstreamModel: "auto",
  provider: "jev",
  listed: true,
  displayName: isClef ? "Auto (Clef)" : "Auto (Jev)",
  description:
    isClef
      ? "Clef chooses GPT-6 Luna or GPT-6.1 Sol within your configured effort bounds. The effort selector sets the maximum Sol effort."
      : "Auto-routing by Jev: calls use GPT-6 Luna, GPT-6.1 Sol, or GPT-6 Astra at the reasoning depth they need.",
  priority: 95,
  defaultEffort: "medium",
  reasoningLevels: [
    { effort: "low", description: isClef ? "Maximum Sol effort: low. Luna follows your effort bounds." : "Quick reasoning" },
    { effort: "medium", description: isClef ? "Maximum Sol effort: medium. Luna follows your effort bounds." : "Balanced reasoning" },
    { effort: "high", description: isClef ? "Maximum Sol effort: high. Luna follows your effort bounds." : "Deep reasoning" },
    { effort: "xhigh", description: isClef ? "Maximum Sol effort: extra high. Luna follows your effort bounds." : "Extended reasoning" },
    { effort: "max", description: isClef ? "Maximum Sol effort: max. Luna follows your effort bounds." : "Maximum reasoning" },
  ],
  contextWindow: 258400,
  autoCompact: 219640,
  searchTool: { mode: "hosted" },
  supportsSearchHistory: true,
  inputModalities: ["text", "image"],
});

const localExecutor = isClef ? JSON.parse(execFileSync(
  process.env.JEV_PYTHON || process.env.PYTHON || "python3",
  [fileURLToPath(new URL("./local_executor.py", import.meta.url)), "show"],
  { encoding: "utf8", maxBuffer: 32768 },
)).executor : null;

export const LOCAL_AUTO_MODEL = localExecutor ? Object.freeze({
  ...JEV_MODEL,
  slug: "jev/auto-local",
  gatewayModel: "jev-auto-local",
  compHash: "jev-auto-local-user-v1",
  upstreamModel: "auto-local",
  displayName: "Auto (Clef Local)",
  description: "Clef chooses your configured local executor or GPT-6.1 Sol. The effort selector sets the maximum Sol effort.",
  priority: 94,
  reasoningLevels: JEV_MODEL.reasoningLevels.map(({ effort }) => ({
    effort,
    description: `Maximum Sol effort: ${effort}. Local execution follows its configured efforts.`,
  })),
  contextWindow: Math.min(JEV_MODEL.contextWindow, localExecutor.context_window),
  autoCompact: Math.floor(Math.min(JEV_MODEL.contextWindow, localExecutor.context_window) * 0.85),
  // Hosted search is attached to ordinary Codex requests when advertised,
  // making a local executor ineligible even for simple coding turns.
  searchTool: undefined,
  supportsSearchHistory: false,
  inputModalities: ["text"],
}) : null;

export const AUTO_MODELS = Object.freeze([JEV_MODEL, ...(LOCAL_AUTO_MODEL ? [LOCAL_AUTO_MODEL] : [])]);
// Keep old conversation routes usable without offering duplicate picker modes.
export const LEGACY_AUTO_MODELS = Object.freeze(isClef ? [
  {
    ...JEV_MODEL, slug: "jev/auto-cheap", gatewayModel: "jev-auto-cheap", compHash: "jev-auto-cheap-user-v1",
    upstreamModel: "auto-cheap", displayName: "Auto Cheap (Clef)",
    listed: false,
    defaultEffort: "low",
  },
  {
    ...JEV_MODEL, slug: "jev/auto-deep", gatewayModel: "jev-auto-deep", compHash: "jev-auto-deep-user-v1",
    upstreamModel: "auto-deep", displayName: "Auto Deep (Clef)",
    listed: false,
    defaultEffort: "high",
  },
] : []);

const existing = readUserModels();
const managedSlugs = new Set(["jev/auto-cheap", "jev/auto", "jev/auto-deep", "jev/auto-local"]);
const next = [
  ...existing.filter((model) => !managedSlugs.has(model?.slug)),
  ...AUTO_MODELS,
  ...LEGACY_AUTO_MODELS,
];
writeUserModels(next);
process.stdout.write(`${JSON.stringify({
  configured: JEV_MODEL.slug,
  configuredModels: AUTO_MODELS.map(({ slug }) => slug),
  preservedModels: existing.filter((model) => !managedSlugs.has(model?.slug)).length,
})}\n`);
