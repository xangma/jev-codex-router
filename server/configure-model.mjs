// Idempotently declare jev/auto in the embedded router's protected user-model
// overlay. Existing user-curated routes are preserved.
import {
  readUserModels,
  writeUserModels,
} from "../router/src/user-models.mjs";

const isClef = process.env.JEV_DECISION_PROVIDER === "clef";
export const JEV_MODEL = Object.freeze({
  slug: "jev/auto",
  gatewayModel: "jev-auto",
  compHash: "jev-auto-user-v1",
  upstreamModel: "auto",
  provider: "jev",
  listed: true,
  displayName: isClef ? "Auto Standard (Clef)" : "Auto (Jev)",
  description:
    isClef
      ? "Clef chooses GPT-6 Luna or GPT-6.1 Sol with sufficient reasoning, balancing care and cost."
      : "Auto-routing by Jev: calls use GPT-6 Luna, GPT-6.1 Sol, or GPT-6 Astra at the reasoning depth they need.",
  priority: 95,
  defaultEffort: "medium",
  reasoningLevels: [
    { effort: "low", description: "Quick reasoning" },
    { effort: "medium", description: "Balanced reasoning" },
    { effort: "high", description: "Deep reasoning" },
    { effort: "xhigh", description: "Extended reasoning" },
    { effort: "max", description: "Maximum reasoning" },
  ],
  contextWindow: 258400,
  autoCompact: 219640,
  searchTool: { mode: "hosted" },
  supportsSearchHistory: true,
  inputModalities: ["text", "image"],
});

export const AUTO_MODELS = Object.freeze(isClef ? [
  {
    ...JEV_MODEL, slug: "jev/auto-cheap", gatewayModel: "jev-auto-cheap", compHash: "jev-auto-cheap-user-v1",
    upstreamModel: "auto-cheap", displayName: "Auto Cheap (Clef)",
    description: "Clef chooses GPT-6 Luna or GPT-6.1 Sol, preferring low reasoning and increasing it when difficulty or failures justify more care.",
    defaultEffort: "low",
  },
  JEV_MODEL,
  {
    ...JEV_MODEL, slug: "jev/auto-deep", gatewayModel: "jev-auto-deep", compHash: "jev-auto-deep-user-v1",
    upstreamModel: "auto-deep", displayName: "Auto Deep (Clef)",
    description: "Clef chooses GPT-6 Luna or GPT-6.1 Sol, preferring careful reasoning for difficult decisions while keeping trivial work at low effort.",
    defaultEffort: "high",
  },
] : [JEV_MODEL]);

const existing = readUserModels();
const managedSlugs = new Set(["jev/auto-cheap", "jev/auto", "jev/auto-deep"]);
const next = [
  ...existing.filter((model) => !managedSlugs.has(model?.slug)),
  ...AUTO_MODELS,
];
writeUserModels(next);
process.stdout.write(`${JSON.stringify({
  configured: JEV_MODEL.slug,
  configuredModels: AUTO_MODELS.map(({ slug }) => slug),
  preservedModels: existing.filter((model) => !managedSlugs.has(model?.slug)).length,
})}\n`);
