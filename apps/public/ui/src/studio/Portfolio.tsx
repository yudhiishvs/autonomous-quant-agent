import { useState } from "react";
import {
    assets,
    money,
    type Asset,
    type Portfolio as PortfolioType,
} from "./model";
import { portfolioTotal, previewAllocation, event } from "./operations";
import { useWorkspace } from "./workspace";
import { Chart, Dialog, Field, Empty } from "./components";
export function Portfolio({
    compact = false,
    ask,
}: {
    compact?: boolean;
    ask: (symbol?: string) => void;
}) {
    const { state, set } = useWorkspace(),
        p = state.portfolio,
        total = portfolioTotal(p);
    const [modal, setModal] = useState<"manual" | "allocation" | null>(null),
        [symbol, setSymbol] = useState<Asset>("VTI"),
        [quantity, setQuantity] = useState("10"),
        [price, setPrice] = useState("200"),
        [cash, setCash] = useState(String(p.cash)),
        [percent, setPercent] = useState("20"),
        [error, setError] = useState(""),
        [preview, setPreview] = useState<{
            trade: number;
            portfolio: PortfolioType;
        } | null>(null);
    const [holdings, setHoldings] = useState(p.holdings);
    const startManual = () => {
        setHoldings(p.holdings);
        setCash(String(p.cash));
        setError("");
        setModal("manual");
    };
    const add = () => {
        const q = Number(quantity),
            v = Number(price);
        if (
            !Number.isFinite(q) ||
            q <= 0 ||
            q > 1000000 ||
            !Number.isFinite(v) ||
            v <= 0 ||
            v > 1000000
        ) {
            setError(
                "Quantity and sample price must be greater than zero and at most 1,000,000.",
            );
            return;
        }
        setHoldings((h) => [
            ...h.filter((x) => x.symbol !== symbol),
            { symbol, quantity: q, price: v },
        ]);
        setError("");
    };
    return (
        <div className={compact ? "artifact-body" : "page"}>
            <div className="section-head">
                <div>
                    <span className="eyebrow">
                        One portfolio. A clearer picture.
                    </span>
                    <h1>What you own.</h1>
                </div>
                {!compact && (
                    <button onClick={startManual}>
                        Build a sample portfolio
                    </button>
                )}
            </div>
            <p className="muted">
                Demo account · {p.timestamp}. No brokerage connected.
            </p>
            <section className="card">
                <div className="section-head">
                    <div>
                        <span className="muted">Total account value</span>
                        <div className="balance">{money(total)}</div>
                    </div>
                    <button className="text-button" onClick={() => ask()}>
                        Ask about my portfolio ↗
                    </button>
                </div>
                <Chart
                    values={[92, 94, 93, 96, 95, 97, 96, 99, 98, 100]}
                    capital={total}
                />
                <p className="small muted">
                    Illustrative history ending September 18, 2026. Scaled to
                    the current demo total; not a record of market performance.
                </p>
            </section>
            <section className="card">
                <h2>Your allocation</h2>
                <div
                    className="allocation-bar"
                    role="img"
                    aria-label="Portfolio allocation"
                >
                    {p.holdings.map((h, i) => (
                        <span
                            key={h.symbol}
                            style={{
                                width: `${total ? ((h.quantity * h.price) / total) * 100 : 0}%`,
                                background: [
                                    "#0066cc",
                                    "#397abd",
                                    "#6c94bb",
                                    "#414854",
                                    "#a3aab4",
                                ][i],
                            }}
                        />
                    ))}
                    <span style={{ flex: 1, background: "#d4d4db" }} />
                </div>
                <div className="table-wrap">
                    <table>
                        <caption className="sr-only">
                            Sample holdings including cash
                        </caption>
                        <thead>
                            <tr>
                                <th>Asset</th>
                                <th>Value</th>
                                <th>Allocation</th>
                                {!compact && <th>Explore</th>}
                            </tr>
                        </thead>
                        <tbody>
                            {p.holdings.map((h) => (
                                <tr key={h.symbol}>
                                    <th>
                                        {h.symbol}
                                        <small>
                                            {h.quantity.toLocaleString(
                                                "en-US",
                                                { maximumFractionDigits: 4 },
                                            )}{" "}
                                            shares · {money(h.price)} assumed
                                            price
                                        </small>
                                    </th>
                                    <td>{money(h.quantity * h.price)}</td>
                                    <td>
                                        {total
                                            ? (
                                                  ((h.quantity * h.price) /
                                                      total) *
                                                  100
                                              ).toFixed(1)
                                            : "0"}
                                        %
                                    </td>
                                    {!compact && (
                                        <td>
                                            <button
                                                className="text-button"
                                                onClick={() => ask(h.symbol)}
                                                aria-label={`Ask about ${h.symbol}`}
                                            >
                                                Ask ↗
                                            </button>
                                        </td>
                                    )}
                                </tr>
                            ))}
                            <tr>
                                <th>Cash</th>
                                <td>{money(p.cash)}</td>
                                <td>
                                    {total
                                        ? ((p.cash / total) * 100).toFixed(1)
                                        : "0"}
                                    %
                                </td>
                                {!compact && <td>Available</td>}
                            </tr>
                        </tbody>
                    </table>
                </div>
                {!p.holdings.length && (
                    <Empty title="Room to explore">
                        Your simulated cash is waiting. Create a sample holding
                        or start a conversation.
                    </Empty>
                )}
                {!compact && p.holdings.length > 0 && (
                    <button
                        onClick={() => {
                            setSymbol(p.holdings[0].symbol);
                            setPreview(null);
                            setError("");
                            setModal("allocation");
                        }}
                    >
                        Preview an allocation change
                    </button>
                )}
            </section>
            <div className="note">
                <strong>Spread out doesn’t always mean diversified.</strong>
                <p>
                    Different funds can hold the same companies. A large
                    position ties more of your outcome to one asset. Cash is
                    included in these percentages; funds can overlap. This demo
                    does not evaluate your personal circumstances.
                </p>
            </div>
            {modal === "manual" && (
                <Dialog
                    title="Build a sample portfolio"
                    onClose={() => setModal(null)}
                >
                    <p>
                        Replace the demo portfolio with your assumptions. Do not
                        enter account numbers or other personal information.
                        Supported US stock and ETF symbols only.
                    </p>
                    <Field label="Symbol">
                        <select
                            value={symbol}
                            onChange={(e) => setSymbol(e.target.value as Asset)}
                        >
                            {assets.map((a) => (
                                <option key={a}>{a}</option>
                            ))}
                        </select>
                    </Field>
                    <div className="form-grid">
                        <Field label="Quantity">
                            <input
                                type="number"
                                value={quantity}
                                onChange={(e) => setQuantity(e.target.value)}
                            />
                        </Field>
                        <Field label="Sample price ($)">
                            <input
                                type="number"
                                value={price}
                                onChange={(e) => setPrice(e.target.value)}
                            />
                        </Field>
                    </div>
                    <button onClick={add}>Add or replace holding</button>
                    {holdings.map((h) => (
                        <div className="row" key={h.symbol}>
                            <span>
                                {h.symbol} · {h.quantity} × {money(h.price)}
                            </span>
                            <button
                                aria-label={`Remove ${h.symbol}`}
                                onClick={() =>
                                    setHoldings((x) =>
                                        x.filter((y) => y.symbol !== h.symbol),
                                    )
                                }
                            >
                                Remove
                            </button>
                        </div>
                    ))}
                    <Field label="Sample cash ($)">
                        <input
                            type="number"
                            value={cash}
                            onChange={(e) => setCash(e.target.value)}
                        />
                    </Field>
                    {error && (
                        <p role="alert" className="error">
                            {error}
                        </p>
                    )}
                    <p>
                        Applying replaces all current sample holdings and cash.
                        Strategy versions are preserved.
                    </p>
                    <button
                        className="primary"
                        onClick={() => {
                            const c = Number(cash);
                            if (!Number.isFinite(c) || c < 0 || c > 100000000) {
                                setError(
                                    "Cash must be between $0 and $100,000,000.",
                                );
                                return;
                            }
                            set((s) => {
                                const n = structuredClone(s);
                                n.portfolio = {
                                    cash: Math.round(c * 100) / 100,
                                    holdings,
                                    source: "manual",
                                    timestamp: "User-supplied demo assumptions",
                                };
                                event(
                                    n,
                                    "Portfolio replaced",
                                    "User confirmed replacement with manual sample holdings and cash.",
                                );
                                return n;
                            });
                            setModal(null);
                        }}
                    >
                        Replace simulated portfolio
                    </button>
                </Dialog>
            )}
            {modal === "allocation" && (
                <Dialog
                    title="Preview an allocation change"
                    onClose={() => setModal(null)}
                >
                    <p>
                        Illustrative trade amounts use your sample prices. No
                        tax calculation or real order.
                    </p>
                    <Field label="Holding">
                        <select
                            value={symbol}
                            onChange={(e) => {
                                setSymbol(e.target.value as Asset);
                                setPreview(null);
                            }}
                        >
                            {p.holdings.map((h) => (
                                <option key={h.symbol}>{h.symbol}</option>
                            ))}
                        </select>
                    </Field>
                    <Field label="Target allocation (%)">
                        <input
                            type="number"
                            value={percent}
                            onChange={(e) => {
                                setPercent(e.target.value);
                                setPreview(null);
                            }}
                        />
                    </Field>
                    <button
                        onClick={() => {
                            try {
                                setPreview(
                                    previewAllocation(
                                        p,
                                        symbol,
                                        Number(percent),
                                    ),
                                );
                                setError("");
                            } catch (e) {
                                setError((e as Error).message);
                            }
                        }}
                    >
                        Calculate sample change
                    </button>
                    {error && (
                        <p role="alert" className="error">
                            {error}
                        </p>
                    )}
                    {preview && (
                        <>
                            <div className="note">
                                <p>
                                    {symbol}:{" "}
                                    {(
                                        ((p.holdings.find(
                                            (h) => h.symbol === symbol,
                                        )!.quantity *
                                            p.holdings.find(
                                                (h) => h.symbol === symbol,
                                            )!.price) /
                                            total) *
                                        100
                                    ).toFixed(1)}
                                    % → {Number(percent).toFixed(1)}%
                                </p>
                                <p>
                                    {preview.trade >= 0 ? "Buy" : "Sell"}{" "}
                                    {money(Math.abs(preview.trade))} in
                                    simulation.
                                </p>
                                <p>
                                    Cash: {money(p.cash)} →{" "}
                                    {money(preview.portfolio.cash)}
                                </p>
                                <p>
                                    Account total remains {money(total)}. Fees
                                    assumed $0 for this allocation illustration.
                                </p>
                            </div>
                            <button
                                className="primary"
                                onClick={() => {
                                    set((s) => {
                                        const n = structuredClone(s);
                                        n.portfolio = preview.portfolio;
                                        event(
                                            n,
                                            "Simulated portfolio change",
                                            `${symbol} target ${percent}%; sample trade ${money(preview.trade)}. User applied the preview.`,
                                        );
                                        return n;
                                    });
                                    setModal(null);
                                }}
                            >
                                Apply to simulated portfolio
                            </button>
                        </>
                    )}
                </Dialog>
            )}
        </div>
    );
}
