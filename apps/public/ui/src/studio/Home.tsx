import { useWorkspace } from "./workspace";
import { money, latest, uid } from "./model";
import { portfolioTotal } from "./operations";
import { Chart } from "./components";
export function Home({
    start,
}: {
    start: (kind: "portfolio" | "idea" | "review") => void;
}) {
    const { state, go, selectConversation, selectStrategy } = useWorkspace();
    const total = portfolioTotal(state.portfolio),
        pending = state.strategies.filter(
            (p) => latest(p).review !== "approved",
        );
    return (
        <div className="page home-page">
            <div className="page-intro">
                <span className="eyebrow">Your investing workspace</span>
                <h1>
                    What would you
                    <br />
                    like to understand?
                </h1>
                <p>
                    Big questions. Small steps. Let’s work through it together.
                </p>
            </div>
            <div className="prompt-grid">
                {[
                    [
                        "portfolio",
                        "Understand my portfolio",
                        "See the bigger picture of what you own.",
                        "◔",
                    ],
                    [
                        "idea",
                        "Explore an investing idea",
                        "Turn a thought into something you can evaluate.",
                        "↗",
                    ],
                    [
                        "review",
                        "Review a strategy",
                        "Understand the rules and what the evidence says.",
                        "≋",
                    ],
                ].map(([kind, title, copy, icon]) => (
                    <button
                        className="prompt-card"
                        aria-label={title}
                        key={kind}
                        onClick={() =>
                            start(kind as "portfolio" | "idea" | "review")
                        }
                    >
                        <span className="tile-icon" aria-hidden="true">
                            {icon}
                        </span>
                        <strong>{title}</strong>
                        <span>{copy}</span>
                        <span className="card-arrow" aria-hidden="true">
                            ↗
                        </span>
                    </button>
                ))}
            </div>
            <div className="home-bottom">
                <section className="card overview">
                    <div className="section-head">
                        <h2>Your sample portfolio</h2>
                        <button
                            className="text-button"
                            onClick={() => go("Portfolio")}
                        >
                            View portfolio ↗
                        </button>
                    </div>
                    <div className="balance">{money(total)}</div>
                    <p className="muted">
                        {state.portfolio.holdings.length} holdings ·{" "}
                        {money(state.portfolio.cash)} cash
                    </p>
                    <Chart
                        values={[92, 94, 93, 96, 95, 97, 96, 99, 98, 100]}
                        capital={total}
                    />
                    <span className="small muted">
                        {state.portfolio.timestamp}
                    </span>
                </section>
                <section className="card">
                    <div className="section-head">
                        <h2>Pick up where you left off.</h2>
                    </div>
                    {state.conversations.slice(0, 3).map((c) => (
                        <button
                            className="list-link"
                            key={c.id}
                            onClick={() => selectConversation(c.id)}
                        >
                            <span>
                                <strong>{c.name}</strong>
                                <small>
                                    {c.messages.length} messages · local demo
                                </small>
                            </span>
                            <span aria-hidden="true">↗</span>
                        </button>
                    ))}
                    <h3 className="review-heading">A moment for review</h3>
                    {pending.length ? (
                        pending.slice(0, 2).map((p) => (
                            <button
                                className="review-card"
                                key={p.id}
                                onClick={() => {
                                    selectStrategy(p.id);
                                    go("Strategies");
                                }}
                            >
                                <strong>{p.name}</strong>
                                <span>
                                    Version {latest(p).number} ·{" "}
                                    {latest(p).review} →
                                </span>
                            </button>
                        ))
                    ) : (
                        <p>No revisions need your attention.</p>
                    )}
                </section>
            </div>
            <div className="quiet-note">
                A space to learn, not a promise of returns. All figures and
                assistant responses are illustrative.
            </div>
        </div>
    );
}
