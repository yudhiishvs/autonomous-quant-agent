import {
    assets,
    modes,
    validRules,
    validAuthority,
    type State,
    type Holding,
    type Version,
} from "./model";
export const STORAGE_KEY = "aqa.studio.demo.v2";
const text = (x: unknown, max = 2000): x is string =>
    typeof x === "string" && x.length <= max;
const time = (x: unknown) =>
    typeof x === "number" &&
    Number.isFinite(x) &&
    x >= 0 &&
    x <= 8640000000000000;
const list = (x: unknown, max: number): x is unknown[] =>
    Array.isArray(x) && x.length <= max;
const unique = (values: string[]) => new Set(values).size === values.length;
const keys = (x: object, allowed: string[]) =>
    Object.keys(x).every((k) => allowed.includes(k));
const holding = (h: Holding) =>
    h &&
    keys(h, ["symbol", "quantity", "price"]) &&
    assets.includes(h.symbol) &&
    Number.isFinite(h.quantity) &&
    h.quantity >= 0 &&
    h.quantity <= 1000000 &&
    Number.isFinite(h.price) &&
    h.price > 0 &&
    h.price <= 1000000;
const version = (v: Version) =>
    v &&
    keys(v, [
        "id",
        "number",
        "description",
        "rules",
        "createdAt",
        "review",
        "report",
    ]) &&
    text(v.id, 100) &&
    Number.isInteger(v.number) &&
    v.number >= 1 &&
    text(v.description, 300) &&
    time(v.createdAt) &&
    validRules(v.rules) &&
    ["unreviewed", "approved", "blocked", "needs approval"].includes(
        v.review,
    ) &&
    (!v.report ||
        (keys(v.report, [
            "scenario",
            "fingerprint",
            "versionId",
            "checks",
            "paperDays",
            "createdAt",
        ]) &&
            v.report.versionId === v.id &&
            ["pass", "fail"].includes(v.report.scenario) &&
            text(v.report.fingerprint) &&
            time(v.report.createdAt) &&
            Number.isInteger(v.report.paperDays) &&
            v.report.paperDays >= 0 &&
            v.report.paperDays <= 365 &&
            v.report.checks &&
            Object.entries(v.report.checks).every(
                ([k, val]) => text(k, 80) && typeof val === "boolean",
            )));
export function decode(raw: string | null): State | null {
    if (!raw || raw.length > 2000000) return null;
    try {
        const s = JSON.parse(raw) as State;
        if (
            !s ||
            !keys(s, [
                "schema",
                "entered",
                "eligible",
                "portfolio",
                "conversations",
                "strategies",
                "events",
                "settings",
                "notice",
            ]) ||
            s.schema !== 2 ||
            typeof s.entered !== "boolean" ||
            typeof s.eligible !== "boolean" ||
            !text(s.notice)
        )
            return null;
        const p = s.portfolio;
        if (
            !p ||
            !keys(p, ["cash", "holdings", "source", "timestamp"]) ||
            !Number.isFinite(p.cash) ||
            p.cash < 0 ||
            p.cash > 100000000 ||
            !text(p.timestamp, 100) ||
            !["sample", "manual", "cash"].includes(p.source) ||
            !list(p.holdings, 5) ||
            !p.holdings.every(holding) ||
            !unique(p.holdings.map((h) => h.symbol))
        )
            return null;
        if (
            !list(s.conversations, 100) ||
            !s.conversations.every(
                (c) =>
                    c &&
                    keys(c, [
                        "id",
                        "name",
                        "draft",
                        "messages",
                        "strategyId",
                    ]) &&
                    text(c.id, 100) &&
                    text(c.name, 80) &&
                    text(c.draft) &&
                    list(c.messages, 500) &&
                    c.messages.every(
                        (m) =>
                            m &&
                            keys(m, [
                                "id",
                                "role",
                                "text",
                                "artifact",
                                "strategyId",
                            ]) &&
                            text(m.id, 100) &&
                            ["user", "assistant"].includes(m.role) &&
                            text(m.text) &&
                            (!m.artifact ||
                                ["portfolio", "strategy", "report"].includes(
                                    m.artifact,
                                )),
                    ),
            )
        )
            return null;
        if (
            !list(s.strategies, 100) ||
            !s.strategies.length ||
            !s.strategies.every(
                (t) =>
                    t &&
                    keys(t, [
                        "id",
                        "name",
                        "conversationId",
                        "accountId",
                        "mode",
                        "authority",
                        "versions",
                        "generation",
                        "researchPaused",
                        "researchUsed",
                        "deployment",
                        "lastAutomatic",
                    ]) &&
                    text(t.id, 100) &&
                    text(t.name, 80) &&
                    s.conversations.some((c) => c.id === t.conversationId) &&
                    t.accountId === "demo-account" &&
                    modes.includes(t.mode) &&
                    Number.isInteger(t.generation) &&
                    t.generation >= 0 &&
                    typeof t.researchPaused === "boolean" &&
                    Number.isInteger(t.researchUsed) &&
                    t.researchUsed >= 0 &&
                    validAuthority(t.authority) &&
                    (t.lastAutomatic === undefined || time(t.lastAutomatic)) &&
                    list(t.versions, 200) &&
                    t.versions.length > 0 &&
                    t.versions.every(version) &&
                    unique(t.versions.map((v) => v.id)) &&
                    (!t.deployment ||
                        (keys(t.deployment, [
                            "versionId",
                            "paused",
                            "positions",
                            "activatedAt",
                        ]) &&
                            t.versions.some(
                                (v) => v.id === t.deployment?.versionId,
                            ) &&
                            typeof t.deployment.paused === "boolean" &&
                            list(t.deployment.positions, 5) &&
                            t.deployment.positions.every(holding) &&
                            time(t.deployment.activatedAt))),
            )
        )
            return null;
        if (
            !unique(s.strategies.map((t) => t.id)) ||
            !unique(s.conversations.map((c) => c.id)) ||
            !s.conversations.every(
                (c) =>
                    (!c.strategyId ||
                        s.strategies.some((t) => t.id === c.strategyId)) &&
                    c.messages.every(
                        (m) =>
                            !m.strategyId ||
                            s.strategies.some((t) => t.id === m.strategyId),
                    ),
            )
        )
            return null;
        if (
            !list(s.events, 10000) ||
            !s.events.every(
                (e) =>
                    e &&
                    keys(e, [
                        "id",
                        "at",
                        "action",
                        "reason",
                        "strategyId",
                        "versionId",
                    ]) &&
                    text(e.id, 100) &&
                    time(e.at) &&
                    text(e.action, 100) &&
                    text(e.reason) &&
                    (!e.strategyId ||
                        s.strategies.some((t) => t.id === e.strategyId)) &&
                    (!e.versionId ||
                        s.strategies.some((t) =>
                            t.versions.some((v) => v.id === e.versionId),
                        )),
            )
        )
            return null;
        const o = s.settings;
        if (
            !o ||
            !keys(o, ["provider", "connected", "budget", "notifications"]) ||
            !["OpenAI", "Anthropic", "Google"].includes(o.provider) ||
            typeof o.connected !== "boolean" ||
            typeof o.notifications !== "boolean" ||
            !Number.isFinite(o.budget) ||
            o.budget < 0 ||
            o.budget > 1000
        )
            return null;
        return s;
    } catch {
        return null;
    }
}
