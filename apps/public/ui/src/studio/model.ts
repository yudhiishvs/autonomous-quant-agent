export const modes = [
    "Collaborative",
    "Automatic Research",
    "Automatic Management",
] as const;
export type Mode = (typeof modes)[number];
export const assets = ["VTI", "VXUS", "BND", "AAPL", "MSFT"] as const;
export type Asset = (typeof assets)[number];
export type Page =
    | "Home"
    | "Conversations"
    | "Portfolio"
    | "Strategies"
    | "Activity"
    | "Settings";
export type Holding = { symbol: Asset; quantity: number; price: number };
export type Portfolio = {
    cash: number;
    holdings: Holding[];
    source: "sample" | "manual" | "cash";
    timestamp: string;
};
export type Rules = {
    assets: Asset[];
    entry: string;
    exit: string;
    timing: "Monthly" | "Weekly";
    sizing: number;
    capital: number;
    maxDrawdown: number;
};
export type Report = {
    scenario: "pass" | "fail";
    fingerprint: string;
    versionId: string;
    checks: Record<string, boolean>;
    paperDays: number;
    createdAt: number;
};
export type Version = {
    id: string;
    number: number;
    description: string;
    rules: Rules;
    createdAt: number;
    review: "unreviewed" | "approved" | "blocked" | "needs approval";
    report?: Report;
};
export type Authority = {
    assets: Asset[];
    capital: number;
    exposure: number;
    minSizing: number;
    maxSizing: number;
    minDays: number;
    budget: number;
    requiredChecks: string[];
    paperDays: number;
    existingHoldings: boolean;
    timingChange: boolean;
    alwaysManual: string[];
};
export type Strategy = {
    id: string;
    name: string;
    conversationId: string;
    accountId: "demo-account";
    mode: Mode;
    authority: Authority;
    versions: Version[];
    generation: number;
    researchPaused: boolean;
    researchUsed: number;
    deployment?: {
        versionId: string;
        paused: boolean;
        positions: Holding[];
        activatedAt: number;
    };
    lastAutomatic?: number;
};
export type Message = {
    id: string;
    role: "user" | "assistant";
    text: string;
    artifact?: "portfolio" | "strategy" | "report";
    strategyId?: string;
};
export type Conversation = {
    id: string;
    name: string;
    draft: string;
    messages: Message[];
    strategyId?: string;
};
export type Event = {
    id: string;
    at: number;
    action: string;
    reason: string;
    strategyId?: string;
    versionId?: string;
};
export type State = {
    schema: 2;
    entered: boolean;
    eligible: boolean;
    portfolio: Portfolio;
    conversations: Conversation[];
    strategies: Strategy[];
    events: Event[];
    settings: {
        provider: "OpenAI" | "Anthropic" | "Google";
        connected: boolean;
        budget: number;
        notifications: boolean;
    };
    notice: string;
};
export const uid = () => crypto.randomUUID();
export const money = (n: number) =>
    new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
    }).format(n);
export const cents = (n: number) => Math.round(n * 100);
export const fingerprint = (r: Rules) =>
    JSON.stringify([
        r.assets,
        r.entry,
        r.exit,
        r.timing,
        r.sizing,
        r.capital,
        r.maxDrawdown,
    ]);
export const latest = (s: Strategy) => s.versions[s.versions.length - 1];
export function validRules(r: Rules) {
    return (
        r.assets.length > 0 &&
        new Set(r.assets).size === r.assets.length &&
        r.assets.every((a) => assets.includes(a)) &&
        r.entry.trim().length > 0 &&
        r.exit.trim().length > 0 &&
        r.entry.length <= 300 &&
        r.exit.length <= 300 &&
        ["Monthly", "Weekly"].includes(r.timing) &&
        Number.isFinite(r.capital) &&
        r.capital >= 100 &&
        r.capital <= 100000 &&
        Number.isFinite(r.sizing) &&
        r.sizing >= 1 &&
        r.sizing <= 100 &&
        Number.isFinite(r.maxDrawdown) &&
        r.maxDrawdown >= 1 &&
        r.maxDrawdown <= 50
    );
}
