import {
    fingerprint,
    type State,
    type Version,
    type Rules,
    type Authority,
} from "./model";
export const checks = [
    "Data coverage",
    "Drawdown limit",
    "Holdout stability",
    "Cost sensitivity",
];
export const baseRules: Rules = {
    assets: ["VTI"],
    entry: "Invest when the monthly trend is positive.",
    exit: "Move the strategy allocation to cash when the trend turns negative.",
    timing: "Monthly",
    sizing: 20,
    capital: 2000,
    maxDrawdown: 15,
};
export const defaultAuthority: Authority = {
    assets: ["VTI", "BND"],
    capital: 2500,
    exposure: 25,
    minSizing: 5,
    maxSizing: 25,
    minDays: 7,
    budget: 5,
    requiredChecks: [...checks],
    paperDays: 5,
    existingHoldings: false,
    timingChange: false,
    alwaysManual: [
        "Add assets outside the allowlist",
        "Change entry or exit logic",
        "Close pre-existing holdings",
    ],
};
export function fixtureVersion(
    scenario: "pass" | "fail",
    id: string,
    number: number,
): Version {
    const rules = {
        ...baseRules,
        assets: [...baseRules.assets],
        sizing: scenario === "pass" ? 15 : 35,
    };
    return {
        id,
        number,
        rules,
        description:
            scenario === "pass"
                ? "Lower position size, unchanged monthly rules."
                : "Higher position size to illustrate a failed drawdown check.",
        createdAt: Date.now(),
        review: "unreviewed",
        report: {
            scenario,
            fingerprint: fingerprint(rules),
            versionId: id,
            checks: Object.fromEntries(
                checks.map((c) => [
                    c,
                    scenario === "pass" || c !== "Drawdown limit",
                ]),
            ),
            paperDays: 7,
            createdAt: Date.now(),
        },
    };
}
export const passingVersion = () => fixtureVersion("pass", "fixture-pass", 2);
export const failingVersion = () => fixtureVersion("fail", "fixture-fail", 2);
export function initialState(): State {
    return {
        schema: 2,
        entered: false,
        eligible: false,
        portfolio: {
            cash: 2500,
            source: "sample",
            timestamp: "2026-09-18 · illustrative prices",
            holdings: [
                { symbol: "VTI", quantity: 40, price: 200 },
                { symbol: "VXUS", quantity: 100, price: 60 },
                { symbol: "BND", quantity: 50, price: 70 },
                { symbol: "AAPL", quantity: 25, price: 200 },
            ],
        },
        conversations: [
            {
                id: "welcome",
                name: "A clearer picture of my portfolio",
                draft: "",
                strategyId: "monthly",
                messages: [
                    {
                        id: "hello",
                        role: "assistant",
                        text: "Welcome. We can make sense of what you own, explore an idea, or work through a strategy together. Everything here uses synthetic information. Where would you like to start?",
                        artifact: "portfolio",
                    },
                ],
            },
        ],
        strategies: [
            {
                id: "monthly",
                name: "A measured monthly approach",
                conversationId: "welcome",
                accountId: "demo-account",
                mode: "Collaborative",
                authority: structuredClone(defaultAuthority),
                versions: [
                    {
                        id: "monthly-v1",
                        number: 1,
                        description:
                            "Explore a monthly trend rule using a broad US stock ETF.",
                        rules: structuredClone(baseRules),
                        createdAt: Date.now(),
                        review: "unreviewed",
                    },
                ],
                generation: 0,
                researchPaused: false,
                researchUsed: 0,
            },
        ],
        events: [],
        settings: {
            provider: "OpenAI",
            connected: false,
            budget: 10,
            notifications: true,
        },
        notice: "",
    };
}
export const curves = {
    pass: [100, 102, 101, 104, 106, 105, 108, 110, 108, 112],
    fail: [100, 110, 103, 94, 79, 86, 89, 96, 99, 105],
    benchmark: [100, 103, 101, 105, 108, 103, 109, 111, 107, 110],
};
