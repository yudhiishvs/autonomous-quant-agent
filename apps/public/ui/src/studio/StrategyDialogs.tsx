import { useState } from "react";
import {
    assets,
    latest,
    money,
    uid,
    type Rules,
    type Strategy,
    type Version,
} from "./model";
import { baseRules, defaultAuthority } from "./fixtures";
import { useWorkspace } from "./workspace";
import {
    saveVersion,
    portfolioTotal,
    activationDecision,
    event,
} from "./operations";
import { Dialog, Field } from "./components";
type VersionProps = {
    strategy: Strategy;
    version: Version;
    onClose: () => void;
};

export function StrategyEditor({
    strategy: p,
    version: v,
    onClose,
    onSaved,
}: VersionProps & { onSaved: () => void }) {
    const { set, cancel } = useWorkspace();
    const [rules, setRules] = useState<Rules>(structuredClone(v.rules)),
        [description, setDescription] = useState(v.description),
        [error, setError] = useState("");
    return (
        <Dialog title="Refine your strategy" onClose={() => onClose()}>
            <Field label="Description">
                <textarea
                    value={description}
                    maxLength={300}
                    onChange={(e) => setDescription(e.target.value)}
                />
            </Field>
            <fieldset>
                <legend>Selected assets</legend>
                {assets.map((a) => (
                    <label className="check" key={a}>
                        <input
                            type="checkbox"
                            checked={rules.assets.includes(a)}
                            onChange={(e) =>
                                setRules({
                                    ...rules,
                                    assets: e.target.checked
                                        ? [...rules.assets, a]
                                        : rules.assets.filter((x) => x !== a),
                                })
                            }
                        />
                        {a}
                    </label>
                ))}
            </fieldset>
            <Field label="Entry rule">
                <textarea
                    maxLength={300}
                    value={rules.entry}
                    onChange={(e) =>
                        setRules({ ...rules, entry: e.target.value })
                    }
                />
            </Field>
            <Field label="Exit rule">
                <textarea
                    maxLength={300}
                    value={rules.exit}
                    onChange={(e) =>
                        setRules({ ...rules, exit: e.target.value })
                    }
                />
            </Field>
            <div className="form-grid">
                <Field label="Timing">
                    <select
                        value={rules.timing}
                        onChange={(e) =>
                            setRules({
                                ...rules,
                                timing: e.target.value as Rules["timing"],
                            })
                        }
                    >
                        <option>Monthly</option>
                        <option>Weekly</option>
                    </select>
                </Field>
                {(
                    [
                        ["capital", "Capital allocation ($)"],
                        ["sizing", "Maximum position (%)"],
                        ["maxDrawdown", "Drawdown boundary (%)"],
                    ] as const
                ).map(([k, label]) => (
                    <Field key={k} label={label}>
                        <input
                            type="number"
                            value={rules[k]}
                            onChange={(e) =>
                                setRules({
                                    ...rules,
                                    [k]: Number(e.target.value),
                                })
                            }
                        />
                    </Field>
                ))}
            </div>
            {error && (
                <p role="alert" className="error">
                    {error}
                </p>
            )}
            <p>
                Saving invalidates review and report eligibility for the new
                version. Existing evidence and the running version are
                preserved.
            </p>
            <button
                className="primary"
                onClick={() => {
                    try {
                        cancel();
                        set((s) => saveVersion(s, p.id, rules, description));
                        onSaved();
                        onClose();
                    } catch (e) {
                        setError((e as Error).message);
                    }
                }}
            >
                Save new version
            </button>
        </Dialog>
    );
}

export function ActivationDialog({
    strategy: p,
    version: v,
    onClose,
}: VersionProps) {
    const { state, set } = useWorkspace();
    const [error, setError] = useState("");
    const deployed = p.versions.find((x) => x.id === p.deployment?.versionId),
        decision = activationDecision(state, p, v);
    return (
        <Dialog title="Review simulated activation" onClose={() => onClose()}>
            <p>
                <strong>Demo account · Paper simulation</strong>
            </p>
            <p>
                Exact version: v{v.number}{" "}
                <span className="break">({v.id})</span>
            </p>
            <p>
                {v.rules.assets.join(", ")} · {money(v.rules.capital)} allocated
                · position limit {v.rules.sizing}% · drawdown boundary{" "}
                {v.rules.maxDrawdown}%.
            </p>
            <p>
                Evidence: all required illustrative checks pass;{" "}
                {v.report?.paperDays} sample observation days. Authority:{" "}
                {decision.outcome === "review"
                    ? "additional manual authorization required"
                    : "manual approval for this exact version"}
                .
            </p>
            <p>
                Replaces{" "}
                {deployed ? `v${deployed.number}` : "no running version"}.
                Existing holdings are untouched. Pending orders: 0. Activation
                enables simulated rules but does not generate real fills.
            </p>
            <p className="note">
                This one-time approval does not expand automatic-management
                authority. Live execution remains unavailable.
            </p>
            {error && (
                <p role="alert" className="error">
                    {error}
                </p>
            )}
            <button
                className="primary"
                onClick={() => {
                    if (v.rules.capital > portfolioTotal(state.portfolio)) {
                        setError(
                            "Capital exceeds the simulated account total. Revise the allocation first.",
                        );
                        return;
                    }
                    set((s) => {
                        const n = structuredClone(s),
                            t = n.strategies.find((x) => x.id === p.id)!,
                            ver = t.versions.find((x) => x.id === v.id)!;
                        if (activationDecision(n, t, ver).outcome === "blocked")
                            return n;
                        ver.review = "approved";
                        t.deployment = {
                            versionId: ver.id,
                            paused: false,
                            positions: t.deployment?.positions ?? [],
                            activatedAt: Date.now(),
                        };
                        event(
                            n,
                            "Manual approval",
                            "User approved this exact version without extending automatic authority.",
                            t.id,
                            ver.id,
                        );
                        event(
                            n,
                            "Simulated activation",
                            "No real order. Existing positions unchanged; zero pending orders.",
                            t.id,
                            ver.id,
                        );
                        return n;
                    });
                    onClose();
                }}
            >
                Approve and activate simulation
            </button>
        </Dialog>
    );
}

export function ClosePositionsDialog({
    strategy: p,
    version: v,
    onClose,
}: VersionProps) {
    const { state, set, cancel } = useWorkspace();
    return (
        <Dialog title="Close simulated positions?" onClose={() => onClose()}>
            <p>
                This separately sells every sample holding at its assumed price
                and moves the value to cash. No real order or tax estimate is
                created.
            </p>
            <p>
                {state.portfolio.holdings.map((h) => h.symbol).join(", ") ||
                    "No holdings"}{" "}
                ·{" "}
                {money(portfolioTotal(state.portfolio) - state.portfolio.cash)}{" "}
                to sample cash. Strategy orders will be paused.
            </p>
            <button
                className="danger"
                onClick={() => {
                    cancel();
                    set((s) => {
                        const n = structuredClone(s);
                        n.portfolio = {
                            ...n.portfolio,
                            cash: portfolioTotal(n.portfolio),
                            holdings: [],
                        };
                        n.strategies.forEach((t) => {
                            t.generation++;
                            t.researchPaused = true;
                            if (t.deployment) t.deployment.paused = true;
                        });
                        event(
                            n,
                            "Simulated positions closed",
                            "User separately confirmed closing all sample holdings to cash. Orders and research paused.",
                            p.id,
                            v.id,
                        );
                        return n;
                    });
                    onClose();
                }}
            >
                Confirm close simulated positions
            </button>
            <button onClick={() => onClose()}>Keep positions</button>
        </Dialog>
    );
}

export function NewStrategyDialog({
    onClose,
    onCreated,
}: {
    onClose: () => void;
    onCreated: (id: string) => void;
}) {
    const { set } = useWorkspace();
    const [name, setName] = useState("");
    return (
        <Dialog title="Start a strategy project" onClose={() => onClose()}>
            <Field label="Strategy name">
                <input
                    maxLength={80}
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                />
            </Field>
            <button
                disabled={!name.trim()}
                className="primary"
                onClick={() => {
                    const id = uid(),
                        cid = uid();
                    set((s) => {
                        const n = structuredClone(s);
                        n.strategies.push({
                            id,
                            name: name.trim(),
                            conversationId: cid,
                            accountId: "demo-account",
                            mode: "Collaborative",
                            authority: structuredClone(defaultAuthority),
                            versions: [
                                {
                                    id: uid(),
                                    number: 1,
                                    description:
                                        "A new idea to develop together.",
                                    rules: structuredClone(baseRules),
                                    createdAt: Date.now(),
                                    review: "unreviewed",
                                },
                            ],
                            generation: 0,
                            researchPaused: false,
                            researchUsed: 0,
                        });
                        n.conversations.push({
                            id: cid,
                            name: name.trim(),
                            draft: "",
                            strategyId: id,
                            messages: [],
                        });
                        event(
                            n,
                            "Strategy created",
                            "Collaborative mode; no approval or report.",
                            id,
                            n.strategies.at(-1)!.versions[0].id,
                        );
                        return n;
                    });
                    onCreated(id);
                    onClose();
                }}
            >
                Create strategy
            </button>
        </Dialog>
    );
}
