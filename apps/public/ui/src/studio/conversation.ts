import { latest, money, uid, type State, type Message } from "./model";
import { portfolioTotal } from "./operations";
export function answer(s: State, cid: string, input: string): Message {
    const c = s.conversations.find((x) => x.id === cid),
        p = s.strategies.find((x) => x.id === c?.strategyId) ?? s.strategies[0],
        v = latest(p),
        q = input.toLowerCase();
    const message: Message = {
        id: uid(),
        role: "assistant",
        text: "",
        strategyId: p.id,
    };
    if (/portfolio|holding|vti|vxus|bnd|aapl|msft/.test(q)) {
        const holding = s.portfolio.holdings.find((h) =>
            q.includes(h.symbol.toLowerCase()),
        );
        const total = portfolioTotal(s.portfolio);
        message.text = holding
            ? `${holding.symbol} represents ${total ? (((holding.quantity * holding.price) / total) * 100).toFixed(1) : 0}% of your sample account (${money(holding.quantity * holding.price)}). A larger allocation makes your outcome more dependent on that asset. For funds, look inside the holdings to understand overlap. Open the portfolio to compare or preview a sample allocation change.`
            : `Your sample portfolio totals ${money(total)}, including ${money(s.portfolio.cash)} in cash and ${s.portfolio.holdings.length} holdings. Allocation shows how much depends on each asset; it does not tell us whether this portfolio suits you. We can explore concentration or preview a change without altering the original.`;
        message.artifact = "portfolio";
    } else if (/research|proposal/.test(q)) {
        message.text = `${p.name} is in ${p.mode}. Research can propose changes; the independent policy decides whether a version may activate. Current authority permits ${p.authority.assets.join(", ")} with up to ${money(p.authority.capital)}. ${v.report ? "Open the report to inspect this version’s fixture evidence." : "There is no evidence for this version yet; use a predefined research example to see the review workflow."}`;
        message.artifact = "report";
    } else if (/revise|change|tweak|edit|smaller/.test(q)) {
        message.text = `We can refine ${p.name} without changing the running version. It currently allocates ${money(v.rules.capital)}, limits each position to ${v.rules.sizing}%, and checks ${v.rules.timing.toLowerCase()}. Open the strategy to edit these settings; saving creates a new version that needs its own evidence.`;
        message.artifact = "strategy";
    } else if (/idea|strategy|review|trend|invest/.test(q)) {
        message.text = `Let’s make an idea specific. The sample “${p.name}” uses ${v.rules.assets.join(", ")} and a ${v.rules.timing.toLowerCase()} trend rule: ${v.rules.entry} Its exit rule is: ${v.rules.exit} Before proceeding, consider how much capital you want to simulate and how much variation you can tolerate. You can change the rules in the strategy card, then explore predefined reports. Nothing here predicts a return.`;
        message.artifact = "strategy";
    } else {
        message.text =
            "This prototype cannot generate a real answer to that question. It uses scripted conversations, not a model connection. Try “Understand my portfolio,” “Explore an investing idea,” “Revise the strategy,” or “Review the research proposal.”";
    }
    return message;
}
export function safeText(value: string) {
    return value
        .replace(
            /\b(?:sk-[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{12,})\b/g,
            "[credential-shaped text removed]",
        )
        .slice(0, 2000);
}
