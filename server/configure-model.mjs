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
  displayName: isClef ? "Auto (Clef)" : "Auto (Jev)",
  description:
    isClef
      ? "Clef chooses GPT-6 Luna at any effort or GPT-6.1 Sol up to your selected effort. The effort selector sets the maximum Sol effort."
      : "Auto-routing by Jev: calls use GPT-6 Luna, GPT-6.1 Sol, or GPT-6 Astra at the reasoning depth they need.",
  priority: 95,
  defaultEffort: "medium",
  reasoningLevels: [
    { effort: "low", description: isClef ? "Maximum Sol effort: low. Luna may use any effort." : "Quick reasoning" },
    { effort: "medium", description: isClef ? "Maximum Sol effort: medium. Luna may use any effort." : "Balanced reasoning" },
    { effort: "high", description: isClef ? "Maximum Sol effort: high. Luna may use any effort." : "Deep reasoning" },
    { effort: "xhigh", description: isClef ? "Maximum Sol effort: extra high. Luna may use any effort." : "Extended reasoning" },
    { effort: "max", description: isClef ? "Maximum Sol effort: max. Luna may use any effort." : "Maximum reasoning" },
  ],
  contextWindow: 258400,
  autoCompact: 219640,
  searchTool: { mode: "hosted" },
  supportsSearchHistory: true,
  inputModalities: ["text", "image"],
});

export const AUTO_MODELS = Object.freeze([JEV_MODEL]);
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
const managedSlugs = new Set(["jev/auto-cheap", "jev/auto", "jev/auto-deep"]);
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
