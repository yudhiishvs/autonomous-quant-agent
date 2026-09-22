import { test, expect } from "@playwright/test";
import {
    initialState,
    passingVersion,
    failingVersion,
} from "../../src/studio/fixtures";
import { evaluate } from "../../src/studio/policy";
import {
    saveVersion,
    evaluateAndActivate,
    changeMode,
    portfolioTotal,
    previewAllocation,
} from "../../src/studio/operations";
import { decode, STORAGE_KEY } from "../../src/studio/persistence";

test("policy requires exact passing evidence and bounded authority", () => {
    const s = initialState();
    const p = s.strategies[0];
    p.mode = "Automatic Management";
    expect(evaluate(p, passingVersion(), 100000000).outcome).toBe("automatic");
    expect(evaluate(p, failingVersion(), 100000000).outcome).toBe("blocked");
    const v = passingVersion();
    v.report = undefined;
    expect(evaluate(p, v, 100000000).outcome).toBe("blocked");
    const excessive = passingVersion();
    excessive.rules.capital = 4000;
    expect(evaluate(p, excessive, 100000000).outcome).toBe("blocked");
    p.authority.capital = 1000;
    expect(evaluate(p, passingVersion(), 100000000).outcome).toBe("review");
});
test("automatic research cannot activate and revocation preserves running version", () => {
    const s = initialState();
    const p = s.strategies[0];
    p.mode = "Automatic Research";
    expect(evaluate(p, passingVersion(), 100000000).outcome).toBe("review");
    p.mode = "Automatic Management";
    p.versions.push(passingVersion());
    const activated = evaluateAndActivate(
        s,
        p.id,
        passingVersion().id,
        100000000,
    );
    expect(activated.strategies[0].deployment?.versionId).toBe("fixture-pass");
    const revoked = changeMode(activated, p.id, "Collaborative");
    expect(revoked.strategies[0].deployment?.versionId).toBe("fixture-pass");
    expect(revoked.strategies[0].generation).toBeGreaterThan(p.generation);
});
test("editing creates an unreviewed version without changing deployment or historical evidence", () => {
    const s = initialState();
    const p = s.strategies[0];
    p.versions = [passingVersion()];
    p.deployment = {
        versionId: "fixture-pass",
        paused: false,
        positions: [],
        activatedAt: 1,
    };
    const next = saveVersion(
        s,
        p.id,
        { ...p.versions[0].rules, capital: 800 },
        "Smaller budget",
    );
    expect(next.strategies[0].versions).toHaveLength(2);
    expect(next.strategies[0].versions[1].report).toBeUndefined();
    expect(next.strategies[0].versions[1].review).toBe("unreviewed");
    expect(next.strategies[0].deployment?.versionId).toBe("fixture-pass");
    expect(s.strategies[0].versions).toHaveLength(1);
});
test("portfolio allocation preserves total and rejects invalid changes", () => {
    const s = initialState();
    expect(portfolioTotal(s.portfolio)).toBe(25000);
    const preview = previewAllocation(s.portfolio, "VTI", 10);
    expect(preview.trade).toBe(-5500);
    expect(preview.portfolio.cash).toBe(8000);
    expect(portfolioTotal(preview.portfolio)).toBe(25000);
    expect(() => previewAllocation(s.portfolio, "VTI", 101)).toThrow();
    expect(() => previewAllocation(s.portfolio, "VTI", 100)).toThrow();
});
test("storage rejects malformed obsolete and structurally corrupt data", () => {
    expect(STORAGE_KEY).toBe("aqa.studio.demo.v2");
    expect(decode("{")).toBeNull();
    expect(decode("{}")).toBeNull();
    const s = initialState();
    expect(decode(JSON.stringify(s))?.portfolio.cash).toBe(2500);
    const bad = { ...s, strategies: [{ id: "bad" }] };
    expect(decode(JSON.stringify(bad))).toBeNull();
});

test("automatic activation respects account capacity and other reserved allocations", () => {
    const s = initialState();
    s.portfolio = { ...s.portfolio, cash: 1000, holdings: [] };
    const p = s.strategies[0];
    p.mode = "Automatic Management";
    p.versions.push(passingVersion());
    const n = evaluateAndActivate(s, p.id, "fixture-pass");
    expect(n.strategies[0].deployment).toBeUndefined();
    expect(n.events[0].reason).toContain("capital");
});
test("locked risk boundaries require additional authority even with passing fixture checks", () => {
    const s = initialState(),
        p = s.strategies[0];
    p.mode = "Automatic Management";
    p.authority.baseline = { ...p.versions[0].rules, maxDrawdown: 10 };
    expect(evaluate(p, passingVersion()).outcome).toBe("review");
});
test("empty strategies invalid timestamps and negative limits recover rather than crash or bypass policy", () => {
    const s = initialState();
    expect(decode(JSON.stringify({ ...s, strategies: [] }))).toBeNull();
    const invalid = initialState();
    (invalid.strategies[0] as any).lastAutomatic = "invalid";
    expect(decode(JSON.stringify(invalid))).toBeNull();
    const negative = initialState();
    negative.strategies[0].authority.minDays = -1;
    expect(decode(JSON.stringify(negative))).toBeNull();
});
