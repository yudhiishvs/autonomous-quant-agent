import { curves } from "./fixtures";
import { money, type Version } from "./model";
import { Chart, Empty } from "./components";
export function Report({ version }: { version: Version }) {
    const r = version.report;
    if (!r)
        return (
            <Empty title="This version has no evidence">
                Edits need their own evaluation. Choose a predefined
                illustrative revision to explore the complete report workflow.
                Arbitrary rules are not backtested in this prototype.
            </Empty>
        );
    const pass = r.scenario === "pass";
    return (
        <div className="report">
            <span className="eyebrow">
                Illustrative report · Version {version.number}
            </span>
            <h2>
                {pass
                    ? "The demonstrated checks pass."
                    : "A required check did not pass."}
            </h2>
            <div className="note">
                Predefined fixture results. These numbers were not computed from
                market data or your edited strategy. Passing checks establishes
                policy eligibility, not profitability.
            </div>
            <div className="metric-grid">
                <div>
                    <span>Illustrative return</span>
                    <strong>{pass ? "+12.0%" : "+5.0%"}</strong>
                </div>
                <div>
                    <span>Maximum drawdown</span>
                    <strong>{pass ? "−1.8%" : "−28.2%"}</strong>
                </div>
                <div>
                    <span>Benchmark return</span>
                    <strong>+10.0%</strong>
                </div>
            </div>
            <Chart
                values={curves[r.scenario]}
                benchmark={curves.benchmark}
                capital={version.rules.capital}
                label="Strategy and broad-market benchmark"
            />
            <p className="small muted">
                Predefined thresholds: 100% sample data coverage; drawdown at
                most {version.rules.maxDrawdown}%; nonnegative holdout return;
                nonnegative return with doubled transaction costs.
            </p>
            <div className="report-checks">
                {Object.entries(r.checks).map(([name, ok]) => (
                    <div className="row" key={name}>
                        <span>{name}</span>
                        <strong className={ok ? "positive" : "negative"}>
                            {ok ? "Pass" : "Fail"}
                        </strong>
                    </div>
                ))}
            </div>
            <details open>
                <summary>What this illustration assumes</summary>
                <dl>
                    <dt>Date range</dt>
                    <dd>Jan 2 – Dec 31, 2025 (synthetic)</dd>
                    <dt>Starting capital</dt>
                    <dd>{money(version.rules.capital)}</dd>
                    <dt>Assets / timing</dt>
                    <dd>
                        {version.rules.assets.join(", ")} /{" "}
                        {version.rules.timing}
                    </dd>
                    <dt>Data / execution</dt>
                    <dd>
                        Invented month-end observations; next-session fills; no
                        leverage; fractional shares.
                    </dd>
                    <dt>Costs</dt>
                    <dd>
                        0.05% fee plus 0.10% slippage per side, included in
                        fixture returns.
                    </dd>
                    <dt>Identity</dt>
                    <dd className="break">{version.id}</dd>
                </dl>
            </details>
            <details>
                <summary>Illustrative trade examples</summary>
                <p>
                    Examples only; not a complete ledger or inputs used to
                    calculate the displayed curve.
                </p>
                <table>
                    <thead>
                        <tr>
                            <th>Date</th>
                            <th>Action</th>
                            <th>Assumption</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>Jan 3, 2025</td>
                            <td>Buy {version.rules.assets[0]}</td>
                            <td>
                                {money(
                                    (version.rules.capital *
                                        version.rules.sizing) /
                                        100,
                                )}{" "}
                                exposure
                            </td>
                        </tr>
                        <tr>
                            <td>Apr 2, 2025</td>
                            <td>Exit to cash</td>
                            <td>Trend turned negative</td>
                        </tr>
                    </tbody>
                </table>
            </details>
            <details>
                <summary>Out-of-sample and robustness</summary>
                <p>
                    Jan–Aug is the illustrative development window; Sep–Dec is a
                    separate holdout. The fixture demonstrates checking holdout
                    stability and doubled transaction costs. No parameter search
                    was performed.
                </p>
                <p>
                    Illustrative paper observation: {r.paperDays} days. This is
                    a fixture, not elapsed live observation.
                </p>
            </details>
            <details>
                <summary>Limitations and interpretation</summary>
                <p>
                    Invented prices cannot establish future returns, real
                    liquidity, data quality, execution reliability or tax
                    consequences. A favorable historical result can still
                    reflect chance. Real strategies require independent
                    evaluation on appropriate licensed data and ongoing
                    monitoring.
                </p>
            </details>
        </div>
    );
}
