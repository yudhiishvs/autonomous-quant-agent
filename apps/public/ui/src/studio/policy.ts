import { checks } from "./fixtures";
import {
    fingerprint,
    validRules,
    validAuthority,
    type Strategy,
    type Version,
} from "./model";
export type Decision = {
    outcome: "automatic" | "review" | "blocked";
    reason: string;
};
export function evaluate(s: Strategy, v: Version, now = Date.now()): Decision {
    const blocked = (reason: string): Decision => ({
        outcome: "blocked",
        reason,
    });
    const review = (reason: string): Decision => ({
        outcome: "review",
        reason,
    });
    if (!validAuthority(s.authority))
        return blocked("Invalid authority; configure valid limits first.");
    const r = v.report,
        a = s.authority;
    if (
        !validRules(v.rules) ||
        !r ||
        r.versionId !== v.id ||
        r.fingerprint !== fingerprint(v.rules)
    )
        return blocked("Evidence is missing or belongs to different rules.");
    if (![...checks, ...a.requiredChecks].every((c) => r.checks[c] === true))
        return blocked("A required illustrative check failed or is missing.");
    if (r.paperDays < a.paperDays)
        return blocked(
            "The required illustrative paper-observation period is incomplete.",
        );
    if (s.researchPaused) return blocked("Research is paused.");
    if (s.researchUsed > a.budget)
        return blocked("The research budget has been exhausted.");
    if (s.mode !== "Automatic Management")
        return review(
            `${s.mode} requires explicit approval for strategy activation.`,
        );
    if (
        v.rules.assets.some((x) => !a.assets.includes(x)) ||
        v.rules.capital > a.capital ||
        v.rules.sizing > a.exposure ||
        v.rules.sizing < a.minSizing ||
        v.rules.sizing > a.maxSizing
    )
        return review(
            "The revision exceeds the granted asset, capital or sizing authority.",
        );
    if (
        v.rules.entry !== a.baseline.entry ||
        v.rules.exit !== a.baseline.exit ||
        v.rules.maxDrawdown !== a.baseline.maxDrawdown
    )
        return review(
            "Changing entry, exit or risk-limit logic always requires human approval.",
        );
    if (v.rules.timing !== a.baseline.timing && !a.timingChange)
        return review("Timing changes are outside the granted authority.");
    if (
        s.lastAutomatic !== undefined &&
        now - s.lastAutomatic < a.minDays * 86400000
    )
        return blocked("The maximum update frequency has been reached.");
    return {
        outcome: "automatic",
        reason: "Exact-version fixture evidence passes every required check and the change is within granted authority. Existing holdings remain untouched.",
    };
}
