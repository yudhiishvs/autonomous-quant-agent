import {
    StrategyEditor,
    ActivationDialog,
    ClosePositionsDialog,
    NewStrategyDialog,
} from "./StrategyDialogs";
import { useState, useEffect } from "react";
import { latest, money } from "./model";
import { useWorkspace } from "./workspace";
import { changeMode, event, activationDecision } from "./operations";
import { AuthorityDialog } from "./AuthorityDialog";
import { Report } from "./Report";
export function Strategies({
    compact = false,
    reportOnly = false,
}: {
    compact?: boolean;
    reportOnly?: boolean;
}) {
    const {
        state,
        set,
        strategyId,
        versionId,
        selectStrategy,
        selectConversation,
        pipeline,
        cancel,
        job,
    } = useWorkspace();
    const p =
        state.strategies.find((x) => x.id === strategyId) ??
        state.strategies[0];
    const [selected, setSelected] = useState(versionId),
        [tab, setTab] = useState<"overview" | "report" | "history">("overview"),
        [modal, setModal] = useState<
            "edit" | "mode" | "activate" | "close" | "new" | null
        >(null);
    const v = p.versions.find((x) => x.id === selected) ?? latest(p),
        previous = p.versions[p.versions.indexOf(v) - 1],
        deployed = p.versions.find((x) => x.id === p.deployment?.versionId),
        decision = activationDecision(state, p, v),
        testReady = !!v.report && decision.outcome !== "blocked";
    useEffect(() => setSelected(versionId), [versionId, strategyId]);
    const choose = (id: string) => {
        selectStrategy(id);
        setSelected("");
    };
    const openEdit = () => setModal("edit");
    if (reportOnly) return <Report version={v} />;
    return (
        <div className={compact ? "artifact-body" : "page"}>
            <div className="section-head">
                <div>
                    <span className="eyebrow">Ideas, made tangible.</span>
                    <h1>{compact ? "Strategy" : "Your strategies."}</h1>
                </div>
                {!compact && (
                    <button
                        onClick={() => {
                            setModal("new");
                        }}
                    >
                        New strategy
                    </button>
                )}
            </div>
            {!compact && (
                <div className="project-tabs">
                    {state.strategies.map((s) => (
                        <button
                            key={s.id}
                            aria-pressed={s.id === p.id}
                            onClick={() => choose(s.id)}
                        >
                            {s.name}
                        </button>
                    ))}
                </div>
            )}
            <section className="card strategy-header">
                <div className="section-head">
                    <div>
                        <span className="eyebrow">
                            Demo account · US stocks & ETFs
                        </span>
                        <h2>{p.name}</h2>
                        <p>{v.description}</p>
                    </div>
                    <span className="status">
                        {deployed
                            ? `${p.deployment?.paused ? "Paused" : "Running"} v${deployed.number}`
                            : "Not activated"}
                    </span>
                </div>
                <div className="row wrap">
                    <span>
                        {p.mode} · {v.review}
                    </span>
                    <button
                        className="text-button"
                        onClick={() => selectConversation(p.conversationId)}
                    >
                        Open conversation ↗
                    </button>
                </div>
            </section>
            <div className="tabs" role="group" aria-label="Strategy sections">
                {(["overview", "report", "history"] as const).map((t) => (
                    <button
                        key={t}
                        aria-pressed={tab === t}
                        onClick={() => setTab(t)}
                    >
                        {t === "overview"
                            ? "Strategy"
                            : t === "report"
                              ? "Evidence"
                              : "Version history"}
                    </button>
                ))}
            </div>
            {tab === "report" ? (
                <section className="card">
                    <Report version={v} />
                </section>
            ) : tab === "history" ? (
                <section className="card">
                    <h2>A record of every revision.</h2>
                    {[...p.versions].reverse().map((ver) => (
                        <button
                            className="list-link"
                            key={ver.id}
                            onClick={() => {
                                setSelected(ver.id);
                                setTab("overview");
                            }}
                        >
                            <span>
                                <strong>
                                    Version {ver.number} · {ver.review}
                                </strong>
                                <small>{ver.description}</small>
                                <small>
                                    {ver.report
                                        ? "Illustrative evidence attached"
                                        : "No report"}
                                    {deployed?.id === ver.id
                                        ? " · Deployed version"
                                        : ""}
                                </small>
                            </span>
                            <span>↗</span>
                        </button>
                    ))}
                </section>
            ) : (
                <>
                    <section className="card">
                        <div className="section-head">
                            <h2>Version {v.number}. In plain language.</h2>
                            <button onClick={openEdit}>Edit strategy</button>
                        </div>
                        <dl className="rules">
                            <dt>Invest in</dt>
                            <dd>{v.rules.assets.join(", ")}</dd>
                            <dt>Enter when</dt>
                            <dd>{v.rules.entry}</dd>
                            <dt>Exit when</dt>
                            <dd>{v.rules.exit}</dd>
                            <dt>Review timing</dt>
                            <dd>{v.rules.timing}</dd>
                            <dt>Capital allocation</dt>
                            <dd>{money(v.rules.capital)}</dd>
                            <dt>Maximum position</dt>
                            <dd>{v.rules.sizing}% of allocated capital</dd>
                            <dt>Drawdown boundary</dt>
                            <dd>
                                {v.rules.maxDrawdown}% · evaluation limit, not
                                loss protection
                            </dd>
                        </dl>
                        <details>
                            <summary>Version identity and changes</summary>
                            <p className="break">{v.id}</p>
                            {previous ? (
                                <>
                                    <p>
                                        Capital: {money(previous.rules.capital)}{" "}
                                        → {money(v.rules.capital)}. Position
                                        size: {previous.rules.sizing}% →{" "}
                                        {v.rules.sizing}%. Timing:{" "}
                                        {previous.rules.timing} →{" "}
                                        {v.rules.timing}.
                                    </p>
                                    <p>
                                        Assets:{" "}
                                        {previous.rules.assets.join(", ")} →{" "}
                                        {v.rules.assets.join(", ")}. Drawdown
                                        limit: {previous.rules.maxDrawdown}% →{" "}
                                        {v.rules.maxDrawdown}%.
                                    </p>
                                    <p>
                                        Entry rule{" "}
                                        {previous.rules.entry === v.rules.entry
                                            ? "unchanged"
                                            : `changed from “${previous.rules.entry}” to “${v.rules.entry}”`}
                                        . Exit rule{" "}
                                        {previous.rules.exit === v.rules.exit
                                            ? "unchanged"
                                            : `changed from “${previous.rules.exit}” to “${v.rules.exit}”`}
                                        .
                                    </p>
                                </>
                            ) : (
                                <p>This is the first version.</p>
                            )}
                            <p>
                                Editing creates a new unreviewed version. The
                                deployed version stays unchanged.
                            </p>
                        </details>
                        <button
                            className="primary"
                            disabled={
                                !testReady ||
                                !!job ||
                                p.deployment?.versionId === v.id
                            }
                            onClick={() => {
                                setModal("activate");
                            }}
                        >
                            Review simulated activation
                        </button>
                        {!v.report && (
                            <p className="small muted">
                                Evidence is required. Explore a predefined
                                evaluation below.
                            </p>
                        )}
                    </section>
                    <section className="card">
                        <div className="section-head">
                            <div>
                                <h2>{p.mode}</h2>
                                <p>
                                    {p.mode === "Collaborative"
                                        ? "Build and refine together."
                                        : p.mode === "Automatic Research"
                                          ? "Research independently. Bring me proposals."
                                          : "Manage within the boundaries I set."}
                                </p>
                            </div>
                            <button onClick={() => setModal("mode")}>
                                Configure mode
                            </button>
                        </div>
                        <p>
                            Allowed: {p.authority.assets.join(", ")} · capital
                            up to {money(p.authority.capital)} · exposure up to{" "}
                            {p.authority.exposure}% · updates at most every{" "}
                            {p.authority.minDays} days.
                        </p>
                        <p className="small">
                            Research used: {p.researchUsed} /{" "}
                            {p.authority.budget} demo evaluations. Pre-existing
                            holdings:{" "}
                            {p.authority.existingHoldings
                                ? "authority granted; fixture leaves unchanged"
                                : "excluded"}
                            .
                        </p>
                        <div className="actions">
                            <button
                                onClick={() => {
                                    cancel();
                                    set((s) => {
                                        const n = structuredClone(s),
                                            t = n.strategies.find(
                                                (x) => x.id === p.id,
                                            )!;
                                        t.researchPaused = !t.researchPaused;
                                        t.generation++;
                                        event(
                                            n,
                                            t.researchPaused
                                                ? "Research paused"
                                                : "Research resumed",
                                            "No change to approved strategy orders or positions.",
                                            t.id,
                                            latest(t).id,
                                        );
                                        return n;
                                    });
                                }}
                            >
                                {p.researchPaused
                                    ? "Resume research"
                                    : "Pause research"}
                            </button>
                            {p.mode !== "Collaborative" && (
                                <button
                                    onClick={() => {
                                        cancel();
                                        set((s) =>
                                            changeMode(
                                                s,
                                                p.id,
                                                "Collaborative",
                                            ),
                                        );
                                    }}
                                >
                                    Stop automatic revisions
                                </button>
                            )}
                            {p.deployment && (
                                <button
                                    onClick={() =>
                                        set((s) => {
                                            const n = structuredClone(s),
                                                t = n.strategies.find(
                                                    (x) => x.id === p.id,
                                                )!;
                                            t.deployment!.paused =
                                                !t.deployment!.paused;
                                            event(
                                                n,
                                                "Simulated orders " +
                                                    (t.deployment!.paused
                                                        ? "paused"
                                                        : "resumed"),
                                                "Positions retained. This does not liquidate holdings.",
                                                t.id,
                                                t.deployment!.versionId,
                                            );
                                            return n;
                                        })
                                    }
                                >
                                    {p.deployment.paused
                                        ? "Resume strategy orders"
                                        : "Pause new strategy orders"}
                                </button>
                            )}
                            <button onClick={() => setModal("close")}>
                                Close simulated positions
                            </button>
                        </div>
                    </section>
                    <section className="card fixture-card">
                        <span className="eyebrow">
                            Explore the complete workflow
                        </span>
                        <h2>Different evidence. Different outcomes.</h2>
                        <p>
                            Each option creates a predefined revision with its
                            own illustrative report. These are not backtests of
                            arbitrary rules.
                        </p>
                        <div className="actions">
                            <button
                                disabled={!!job || p.researchPaused}
                                onClick={() => {
                                    setSelected("");
                                    pipeline(p.id, "pass");
                                }}
                            >
                                Try passing revision
                            </button>
                            <button
                                disabled={!!job || p.researchPaused}
                                onClick={() => {
                                    setSelected("");
                                    pipeline(p.id, "fail");
                                }}
                            >
                                Try failing revision
                            </button>
                            <button
                                disabled={!!job || p.researchPaused}
                                onClick={() => {
                                    setSelected("");
                                    pipeline(p.id, "outside");
                                }}
                            >
                                Try out-of-scope revision
                            </button>
                        </div>
                        <details>
                            <summary>Test recovery controls</summary>
                            <p>
                                Demonstrate a failed evaluation without creating
                                evidence or activation.
                            </p>
                            <button
                                disabled={!!job}
                                onClick={() => {
                                    setSelected("");
                                    pipeline(p.id, "pass", true);
                                }}
                            >
                                Simulate evaluation error
                            </button>
                            <button
                                disabled={!!job}
                                onClick={() => {
                                    setSelected("");
                                    pipeline(p.id, "pass");
                                }}
                            >
                                Retry evaluation
                            </button>
                        </details>
                        <p className="small">
                            Default policy: passing revision activates
                            automatically only in Automatic Management; failed
                            checks block; $3,000 exceeds the default $2,500
                            authority and requests approval. Frequency and
                            budget limits still apply.
                        </p>
                    </section>
                </>
            )}
            {modal === "mode" && (
                <AuthorityDialog strategy={p} onClose={() => setModal(null)} />
            )}
            {modal === "edit" && (
                <StrategyEditor
                    strategy={p}
                    version={v}
                    onClose={() => setModal(null)}
                    onSaved={() => setSelected("")}
                />
            )}
            {modal === "activate" && (
                <ActivationDialog
                    strategy={p}
                    version={v}
                    onClose={() => setModal(null)}
                />
            )}
            {modal === "close" && (
                <ClosePositionsDialog
                    strategy={p}
                    version={v}
                    onClose={() => setModal(null)}
                />
            )}
            {modal === "new" && (
                <NewStrategyDialog
                    onClose={() => setModal(null)}
                    onCreated={choose}
                />
            )}
        </div>
    );
}
