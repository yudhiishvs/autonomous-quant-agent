import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";
import { api, RequestError } from "./client";
import { Accounts } from "./accounts";

type Definition = {
    schema_version: 1;
    symbol: string;
    cadence_seconds: number;
    session: "regular";
    asset_class: "us_equity";
    rule:
        | { kind: "constant_target"; target_shares: number }
        | {
              kind: "moving_average_target";
              target_shares: number;
              fast_minutes: number;
              slow_minutes: number;
          };
    order:
        | { kind: "market"; time_in_force: "day" }
        | { kind: "limit"; time_in_force: "day"; offset_bps: number };
};
type Version = {
    id: string;
    name: string;
    definition: Definition;
    content_hash: string;
    created_at: string;
};

function App() {
    const pendingSave = useRef<{ payload: string; id: string } | null>(null);
    const [state, setState] = useState<
        "loading" | "anonymous" | "ready" | "error"
    >("loading");
    const [versions, setVersions] = useState<Version[]>([]);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [busy, setBusy] = useState(false);
    const [selected, setSelected] = useState<Version | null>(null);
    const [kind, setKind] = useState("moving_average_target");
    const [order, setOrder] = useState("limit");

    async function load() {
        setError("");
        setState("loading");
        try {
            await api("/api/v1/me");
            setVersions(await api<Version[]>("/api/v1/strategy-versions"));
            setState("ready");
        } catch (e) {
            if (e instanceof RequestError && e.status === 401)
                setState("anonymous");
            else {
                setError(
                    e instanceof Error
                        ? e.message
                        : "Unable to load workspace.",
                );
                setState("error");
            }
        }
    }
    useEffect(() => {
        void load();
    }, []);

    async function save(event: React.FormEvent<HTMLFormElement>) {
        event.preventDefault();
        setError("");
        setNotice("");
        const form = event.currentTarget;
        const data = new FormData(form);
        const fast = Number(data.get("fast")),
            slow = Number(data.get("slow"));
        if (kind === "moving_average_target" && fast >= slow) {
            setError("Fast window must be shorter than slow window.");
            return;
        }
        const target = Number(data.get("shares"));
        const definition: Definition = {
            schema_version: 1,
            symbol: String(data.get("symbol")).toUpperCase(),
            cadence_seconds: Number(data.get("cadence")) * 60,
            session: "regular",
            asset_class: "us_equity",
            rule:
                kind === "constant_target"
                    ? { kind: "constant_target", target_shares: target }
                    : {
                          kind: "moving_average_target",
                          target_shares: target,
                          fast_minutes: fast,
                          slow_minutes: slow,
                      },
            order:
                order === "market"
                    ? { kind: "market", time_in_force: "day" }
                    : { kind: "limit", time_in_force: "day", offset_bps: 0 },
        };
        setBusy(true);
        const payload = JSON.stringify({ name: data.get("name"), definition });
        if (pendingSave.current?.payload !== payload) {
            pendingSave.current = { payload, id: crypto.randomUUID() };
        }
        try {
            const version = await api<Version>("/api/v1/strategy-versions", {
                method: "POST",
                body: JSON.stringify({
                    name: data.get("name"),
                    definition,
                    request_id: pendingSave.current.id,
                }),
            });
            pendingSave.current = null;
            setVersions((current) => [
                version,
                ...current.filter((item) => item.id !== version.id),
            ]);
            setSelected(version);
            setNotice("Version saved. It is not approved or running.");
        } catch (e) {
            setError(
                e instanceof Error ? e.message : "Save failed. Please retry.",
            );
        } finally {
            setBusy(false);
        }
    }

    async function logout() {
        setBusy(true);
        try {
            const result = await api<{
                provider_revocation_confirmed: boolean;
            }>("/auth/logout", { method: "POST" });
            setVersions([]);
            setSelected(null);
            setState("anonymous");
            setNotice(
                result.provider_revocation_confirmed
                    ? "Signed out."
                    : "Signed out here. Identity-provider revocation could not be confirmed.",
            );
        } catch (e) {
            setError(e instanceof Error ? e.message : "Sign-out failed.");
        } finally {
            setBusy(false);
        }
    }

    return (
        <>
            <a className="skip" href="#main">
                Skip to content
            </a>
            <header>
                <a className="brand" href="/">
                    Paper workspace
                </a>
                <span className="badge">PAPER ONLY</span>
                {state === "ready" && (
                    <button
                        className="quiet"
                        disabled={busy}
                        onClick={() => void logout()}
                    >
                        Sign out
                    </button>
                )}
            </header>
            <main id="main" tabIndex={-1}>
                {error && (
                    <div role="alert" className="error">
                        {error}
                    </div>
                )}
                {notice && (
                    <p role="status" className="notice">
                        {notice}
                    </p>
                )}
                {state === "loading" && (
                    <p role="status">Loading your workspace…</p>
                )}
                {state === "error" && (
                    <button onClick={() => void load()}>Try again</button>
                )}
                {state === "anonymous" && (
                    <section className="welcome">
                        <p className="eyebrow">
                            YOUR STRATEGIES, CLEARLY DEFINED
                        </p>
                        <h1>
                            A place to prepare
                            <br />
                            your paper strategies.
                        </h1>
                        <p>
                            Save precise, versioned rules for US equities. Keep
                            your configurations in your own workspace.
                        </p>
                        <a className="button" href="/auth/login">
                            Sign in or create an account
                        </a>
                        <p className="muted">
                            Account verification and recovery are handled by our
                            identity provider.
                        </p>
                        <aside>
                            Development release: execution is not available yet.
                            Paper-account connections require installation
                            setup. Saving a strategy does not place an order.
                        </aside>
                    </section>
                )}
                {state === "ready" && (
                    <>
                        <div className="heading">
                            <div>
                                <p className="eyebrow">WORKSPACE</p>
                                <h1>Your strategy versions</h1>
                                <p>
                                    Make the rules explicit. Save a version you
                                    can inspect.
                                </p>
                            </div>
                            <span className="count">
                                {versions.length} / 100 versions
                            </span>
                        </div>
                        <aside className="status">
                            Configuration only · Execution unavailable
                        </aside>
                        <Accounts />
                        <div className="layout">
                            <section aria-labelledby="create-title">
                                <h2 id="create-title">New version</h2>
                                <p className="muted">
                                    Saved versions are immutable. To change a
                                    rule, save another version.
                                </p>
                                <form onSubmit={(event) => void save(event)}>
                                    <label>
                                        Name
                                        <input
                                            name="name"
                                            required
                                            maxLength={80}
                                            placeholder="SPY intraday trend"
                                            autoComplete="off"
                                        />
                                    </label>
                                    <div className="row">
                                        <label>
                                            Symbol
                                            <input
                                                name="symbol"
                                                required
                                                pattern="[A-Z][A-Z0-9.]{0,9}"
                                                maxLength={10}
                                                defaultValue="SPY"
                                                autoCapitalize="characters"
                                            />
                                        </label>
                                        <label>
                                            Target shares
                                            <input
                                                name="shares"
                                                type="number"
                                                min={
                                                    kind === "constant_target"
                                                        ? 0
                                                        : 1
                                                }
                                                max={100000}
                                                step={1}
                                                defaultValue={1}
                                                required
                                            />
                                        </label>
                                    </div>
                                    <label>
                                        Rule
                                        <select
                                            value={kind}
                                            onChange={(e) =>
                                                setKind(e.target.value)
                                            }
                                        >
                                            <option value="moving_average_target">
                                                Moving average target
                                            </option>
                                            <option value="constant_target">
                                                Constant target
                                            </option>
                                        </select>
                                    </label>
                                    {kind === "moving_average_target" ? (
                                        <>
                                            <p className="help">
                                                Propose the target when the fast
                                                average exceeds the slow
                                                average; otherwise propose zero
                                                shares. Missing or stale data
                                                produces no proposal.
                                            </p>
                                            <div className="row">
                                                <label>
                                                    Fast window (minutes)
                                                    <input
                                                        name="fast"
                                                        type="number"
                                                        min={2}
                                                        max={199}
                                                        step={1}
                                                        defaultValue={5}
                                                        required
                                                    />
                                                </label>
                                                <label>
                                                    Slow window (minutes)
                                                    <input
                                                        name="slow"
                                                        type="number"
                                                        min={3}
                                                        max={200}
                                                        step={1}
                                                        defaultValue={20}
                                                        required
                                                    />
                                                </label>
                                            </div>
                                        </>
                                    ) : (
                                        <p className="help">
                                            Propose this total position each
                                            evaluation. This is a target
                                            position, not an instruction to buy
                                            this many shares every minute.
                                        </p>
                                    )}
                                    <label>
                                        Evaluate every (minutes)
                                        <input
                                            name="cadence"
                                            type="number"
                                            min={1}
                                            max={1440}
                                            step={1}
                                            defaultValue={1}
                                            required
                                        />
                                    </label>
                                    <label>
                                        Order policy
                                        <select
                                            value={order}
                                            onChange={(e) =>
                                                setOrder(e.target.value)
                                            }
                                        >
                                            <option value="limit">
                                                Limit at fresh reference price
                                            </option>
                                            <option value="market">
                                                Market
                                            </option>
                                        </select>
                                    </label>
                                    <p className="help">
                                        Whole shares · Regular sessions · DAY
                                        orders. Asset eligibility, approval and
                                        account-wide risk must be checked before
                                        execution.
                                    </p>
                                    <button
                                        type="submit"
                                        disabled={
                                            busy || versions.length >= 100
                                        }
                                    >
                                        {busy ? "Saving…" : "Save version"}
                                    </button>
                                </form>
                            </section>
                            <section aria-labelledby="saved-title">
                                <h2 id="saved-title">Saved versions</h2>
                                {versions.length === 0 ? (
                                    <div className="empty">
                                        <h3>Your first version starts here.</h3>
                                        <p>
                                            Choose a symbol and a rule, then
                                            save it. Nothing will run
                                            automatically.
                                        </p>
                                    </div>
                                ) : (
                                    <ul className="versions">
                                        {versions.map((version) => (
                                            <li key={version.id}>
                                                <button
                                                    className={
                                                        selected?.id ===
                                                        version.id
                                                            ? "version selected"
                                                            : "version"
                                                    }
                                                    onClick={() =>
                                                        setSelected(version)
                                                    }
                                                    aria-pressed={
                                                        selected?.id ===
                                                        version.id
                                                    }
                                                >
                                                    <strong>
                                                        {version.name}
                                                    </strong>
                                                    <span>
                                                        {
                                                            version.definition
                                                                .symbol
                                                        }{" "}
                                                        ·{" "}
                                                        {version.definition.rule
                                                            .kind ===
                                                        "constant_target"
                                                            ? "Constant target"
                                                            : "Moving average"}
                                                    </span>
                                                    <span>
                                                        Saved{" "}
                                                        {new Date(
                                                            version.created_at,
                                                        ).toLocaleString()}
                                                    </span>
                                                </button>
                                            </li>
                                        ))}
                                    </ul>
                                )}
                                {selected && (
                                    <section
                                        className="detail"
                                        aria-label="Selected version"
                                    >
                                        <h3>{selected.name}</h3>
                                        <p>Saved · Not approved</p>
                                        <dl>
                                            <dt>Version ID</dt>
                                            <dd>{selected.id}</dd>
                                            <dt>Configuration hash</dt>
                                            <dd>{selected.content_hash}</dd>
                                        </dl>
                                        <details>
                                            <summary>
                                                View configuration
                                            </summary>
                                            <pre>
                                                {JSON.stringify(
                                                    selected.definition,
                                                    null,
                                                    2,
                                                )}
                                            </pre>
                                        </details>
                                    </section>
                                )}
                            </section>
                        </div>
                    </>
                )}
            </main>
            <footer>
                Paper trading preparation. No live trading, AI or backtesting.
            </footer>
        </>
    );
}

createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
        <App />
    </React.StrictMode>,
);
