import {
    cents,
    latest,
    uid,
    validRules,
    type State,
    type Rules,
    type Mode,
    type Portfolio,
    type Asset,
} from "./model";
import { evaluate } from "./policy";
export function event(
    s: State,
    action: string,
    reason: string,
    strategyId?: string,
    versionId?: string,
) {
    s.events.unshift({
        id: uid(),
        at: Date.now(),
        action,
        reason,
        strategyId,
        versionId,
    });
}
export function portfolioTotal(p: Portfolio) {
    return (
        (cents(p.cash) +
            p.holdings.reduce((n, h) => n + cents(h.quantity * h.price), 0)) /
        100
    );
}
export function previewAllocation(
    p: Portfolio,
    symbol: Asset,
    percent: number,
) {
    if (!Number.isFinite(percent) || percent < 0 || percent > 100)
        throw Error("Enter an allocation from 0 to 100%.");
    const holding = p.holdings.find((h) => h.symbol === symbol);
    if (!holding) throw Error("Select a holding.");
    const trade =
        (Math.round((cents(portfolioTotal(p)) * percent) / 100) -
            cents(holding.quantity * holding.price)) /
        100;
    if (cents(trade) > cents(p.cash))
        throw Error("This change needs more sample cash than is available.");
    return {
        trade,
        portfolio: {
            ...p,
            cash: (cents(p.cash) - cents(trade)) / 100,
            holdings: p.holdings.map((h) =>
                h.symbol === symbol
                    ? {
                          ...h,
                          quantity:
                              (cents(h.quantity * h.price) + cents(trade)) /
                              100 /
                              h.price,
                      }
                    : h,
            ),
        },
    };
}
export function saveVersion(
    state: State,
    id: string,
    rules: Rules,
    description: string,
) {
    if (!validRules(rules))
        throw Error("Check the assets, rules and numeric limits.");
    const s = structuredClone(state),
        p = s.strategies.find((x) => x.id === id);
    if (!p) throw Error("Strategy not found.");
    const version = {
        id: uid(),
        number: latest(p).number + 1,
        rules: structuredClone(rules),
        description,
        createdAt: Date.now(),
        review: "unreviewed" as const,
    };
    p.versions.push(version);
    p.generation++;
    event(
        s,
        "Strategy revised",
        "New version needs its own evidence and review. Running version is unchanged.",
        id,
        version.id,
    );
    return s;
}
export function changeMode(state: State, id: string, mode: Mode) {
    const s = structuredClone(state),
        p = s.strategies.find((x) => x.id === id)!;
    p.mode = mode;
    p.generation++;
    event(
        s,
        "Mode changed",
        `${mode}. Pending automatic activation canceled; running version preserved.`,
        id,
        latest(p).id,
    );
    return s;
}
export function evaluateAndActivate(
    state: State,
    id: string,
    versionId: string,
    now = Date.now(),
) {
    const s = structuredClone(state),
        p = s.strategies.find((x) => x.id === id);
    if (!p) return s;
    const v = p.versions.find((x) => x.id === versionId);
    if (!v || p.deployment?.versionId === versionId) return s;
    const result = evaluate(p, v, now);
    v.review =
        result.outcome === "automatic"
            ? "approved"
            : result.outcome === "blocked"
              ? "blocked"
              : "needs approval";
    event(
        s,
        `Policy ${result.outcome === "automatic" ? "approval" : result.outcome === "blocked" ? "rejection" : "escalation"}`,
        result.reason,
        id,
        v.id,
    );
    if (result.outcome === "automatic") {
        p.deployment = {
            versionId: v.id,
            paused: false,
            positions: p.deployment?.positions ?? [],
            activatedAt: now,
        };
        p.lastAutomatic = now;
        event(
            s,
            "Simulated activation",
            `Policy authorized ${v.id}; no real order, existing positions unchanged, zero pending orders.`,
            id,
            v.id,
        );
        s.notice = `${p.name} v${v.number} automatically approved and activated in simulation. Existing holdings were not changed.`;
    }
    return s;
}
