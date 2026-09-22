import { useWorkspace } from "./workspace";
import { Empty } from "./components";
export function Activity() {
    const { state, go, selectStrategy } = useWorkspace();
    return (
        <div className="page">
            <div className="page-intro">
                <span className="eyebrow">Nothing happens quietly.</span>
                <h1>A clear record.</h1>
                <p>Local demo history. Every decision has a reason.</p>
            </div>
            {!state.events.length ? (
                <Empty title="A fresh start">
                    Strategy changes, evaluations and simulated actions will
                    appear here.
                </Empty>
            ) : (
                <div className="activity-list">
                    {state.events.map((e) => (
                        <article className="activity-item" key={e.id}>
                            <span className="timeline-dot" />
                            <div>
                                <div className="section-head">
                                    <h2>{e.action}</h2>
                                    <time
                                        dateTime={new Date(e.at).toISOString()}
                                    >
                                        {new Date(e.at).toLocaleString()}
                                    </time>
                                </div>
                                <p>{e.reason}</p>
                                {e.versionId && (
                                    <p className="small break">
                                        Exact version: {e.versionId}
                                    </p>
                                )}
                                {e.strategyId && (
                                    <button
                                        className="text-button"
                                        onClick={() => {
                                            selectStrategy(
                                                e.strategyId!,
                                                e.versionId,
                                            );
                                            go("Strategies");
                                        }}
                                    >
                                        Open associated strategy ↗
                                    </button>
                                )}
                            </div>
                        </article>
                    ))}
                </div>
            )}
            <p className="quiet-note">
                Stored only in this browser. This is not a production or
                tamper-proof audit log.
            </p>
        </div>
    );
}
