import { assets, modes, validRules, type State } from "./model";
export const STORAGE_KEY = "aqa.studio.demo.v2";
export function decode(raw: string | null): State | null {
    if (!raw || raw.length > 2000000) return null;
    try {
        const s = JSON.parse(raw) as State;
        if (
            s.schema !== 2 ||
            typeof s.entered !== "boolean" ||
            typeof s.eligible !== "boolean" ||
            typeof s.notice !== "string"
        )
            return null;
        if (
            !s.portfolio ||
            !Number.isFinite(s.portfolio.cash) ||
            s.portfolio.cash < 0 ||
            typeof s.portfolio.timestamp !== "string" ||
            !["sample", "manual", "cash"].includes(s.portfolio.source) ||
            !Array.isArray(s.portfolio.holdings) ||
            s.portfolio.holdings.length > 50
        )
            return null;
        if (
            !s.portfolio.holdings.every(
                (h) =>
                    assets.includes(h.symbol) &&
                    Number.isFinite(h.quantity) &&
                    h.quantity >= 0 &&
                    h.quantity <= 1000000 &&
                    Number.isFinite(h.price) &&
                    h.price > 0 &&
                    h.price <= 1000000,
            ) ||
            new Set(s.portfolio.holdings.map((h) => h.symbol)).size !==
                s.portfolio.holdings.length
        )
            return null;
        if (
            !Array.isArray(s.conversations) ||
            s.conversations.length > 100 ||
            !s.conversations.every(
                (c) =>
                    typeof c.id === "string" &&
                    typeof c.name === "string" &&
                    typeof c.draft === "string" &&
                    Array.isArray(c.messages) &&
                    c.messages.length <= 500 &&
                    c.messages.every(
                        (m) =>
                            typeof m.id === "string" &&
                            ["user", "assistant"].includes(m.role) &&
                            typeof m.text === "string",
                    ),
            )
        )
            return null;
        if (
            !Array.isArray(s.strategies) ||
            s.strategies.length > 100 ||
            !s.strategies.every(
                (p) =>
                    typeof p.id === "string" &&
                    typeof p.name === "string" &&
                    typeof p.conversationId === "string" &&
                    p.accountId === "demo-account" &&
                    modes.includes(p.mode) &&
                    Number.isInteger(p.generation) &&
                    typeof p.researchPaused === "boolean" &&
                    Number.isFinite(p.researchUsed) &&
                    p.researchUsed >= 0 &&
                    Array.isArray(p.versions) &&
                    p.versions.length > 0 &&
                    p.versions.length <= 200 &&
                    p.versions.every(
                        (v) =>
                            typeof v.id === "string" &&
                            Number.isInteger(v.number) &&
                            typeof v.description === "string" &&
                            Number.isFinite(v.createdAt) &&
                            validRules(v.rules) &&
                            [
                                "unreviewed",
                                "approved",
                                "blocked",
                                "needs approval",
                            ].includes(v.review) &&
                            (!v.report ||
                                (v.report.versionId === v.id &&
                                    ["pass", "fail"].includes(
                                        v.report.scenario,
                                    ) &&
                                    typeof v.report.fingerprint === "string" &&
                                    Number.isFinite(v.report.paperDays) &&
                                    typeof v.report.checks === "object" &&
                                    v.report.checks !== null)),
                    ) &&
                    p.authority &&
                    p.authority.assets.every((a) => assets.includes(a)) &&
                    [
                        "capital",
                        "exposure",
                        "minSizing",
                        "maxSizing",
                        "minDays",
                        "budget",
                        "paperDays",
                    ].every((k) =>
                        Number.isFinite(
                            p.authority[k as keyof typeof p.authority],
                        ),
                    ) &&
                    Array.isArray(p.authority.requiredChecks) &&
                    Array.isArray(p.authority.alwaysManual) &&
                    typeof p.authority.existingHoldings === "boolean" &&
                    typeof p.authority.timingChange === "boolean" &&
                    (!p.deployment ||
                        (p.versions.some(
                            (v) => v.id === p.deployment?.versionId,
                        ) &&
                            typeof p.deployment.paused === "boolean" &&
                            Array.isArray(p.deployment.positions) &&
                            Number.isFinite(p.deployment.activatedAt))),
            )
        )
            return null;
        if (
            !Array.isArray(s.events) ||
            s.events.length > 10000 ||
            !s.events.every(
                (e) =>
                    typeof e.id === "string" &&
                    Number.isFinite(e.at) &&
                    typeof e.action === "string" &&
                    typeof e.reason === "string",
            )
        )
            return null;
        if (
            !s.settings ||
            !["OpenAI", "Anthropic", "Google"].includes(s.settings.provider) ||
            typeof s.settings.connected !== "boolean" ||
            typeof s.settings.notifications !== "boolean" ||
            !Number.isFinite(s.settings.budget) ||
            s.settings.budget < 0
        )
            return null;
        return s;
    } catch {
        return null;
    }
}
