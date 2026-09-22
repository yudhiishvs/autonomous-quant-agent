import { useState } from "react";
import {
    assets,
    modes,
    latest,
    type Strategy,
    type Mode,
    type Authority,
    validAuthority,
} from "./model";
import { checks } from "./fixtures";
import { Dialog, Field } from "./components";
import { useWorkspace } from "./workspace";
import { changeMode, event } from "./operations";
export function AuthorityDialog({
    strategy,
    onClose,
}: {
    strategy: Strategy;
    onClose: () => void;
}) {
    const { set, cancel, pipeline } = useWorkspace();
    const [mode, setMode] = useState<Mode>(strategy.mode),
        [a, setA] = useState<Authority>(structuredClone(strategy.authority)),
        [consent, setConsent] = useState(false),
        [error, setError] = useState("");
    const number = (
        key:
            | "capital"
            | "exposure"
            | "minSizing"
            | "maxSizing"
            | "minDays"
            | "budget"
            | "paperDays",
        label: string,
    ) => (
        <Field label={label}>
            <input
                type="number"
                value={a[key]}
                onChange={(e) => setA({ ...a, [key]: Number(e.target.value) })}
            />
        </Field>
    );
    return (
        <Dialog title="Choose how we work together." onClose={onClose}>
            <p>{strategy.name} · Demo account · Paper simulation only</p>
            <Field label="Operating mode">
                <select
                    value={mode}
                    onChange={(e) => {
                        setMode(e.target.value as Mode);
                        setConsent(false);
                    }}
                >
                    {modes.map((m) => (
                        <option key={m}>{m}</option>
                    ))}
                </select>
            </Field>
            <div className="note">
                {mode === "Collaborative"
                    ? "Build and refine together. You approve strategy activations."
                    : mode === "Automatic Research"
                      ? "Research independently. Bring me proposals. Activations still need your approval."
                      : "Manage within the boundaries I set. Qualifying revisions are approved and activated automatically, without a confirmation for each revision."}
            </div>
            <p className="small">
                An approved strategy can follow its approved rules in any mode.
                These modes control research and revisions.
            </p>
            {mode !== "Collaborative" && (
                <>
                    <h3>Authority for this strategy only</h3>
                    <fieldset>
                        <legend>Allowed assets</legend>
                        <div className="checks-inline">
                            {assets.map((asset) => (
                                <label key={asset}>
                                    <input
                                        type="checkbox"
                                        checked={a.assets.includes(asset)}
                                        onChange={(e) =>
                                            setA({
                                                ...a,
                                                assets: e.target.checked
                                                    ? [...a.assets, asset]
                                                    : a.assets.filter(
                                                          (x) => x !== asset,
                                                      ),
                                            })
                                        }
                                    />
                                    {asset}
                                </label>
                            ))}
                        </div>
                    </fieldset>
                    <div className="form-grid">
                        {number("capital", "Capital limit ($)")}
                        {number("exposure", "Exposure limit (%)")}
                        {number("minSizing", "Minimum sizing (%)")}
                        {number("maxSizing", "Maximum sizing (%)")}
                        {number("minDays", "Minimum days between updates")}
                        {number("budget", "Research budget (demo evaluations)")}
                        {number("paperDays", "Required paper-observation days")}
                    </div>
                    <p>
                        Only sizing within this range and allocation up to the
                        capital limit may change automatically. Entry/exit logic
                        and risk limits require human approval.
                    </p>
                    <label className="check">
                        <input
                            type="checkbox"
                            checked={a.timingChange}
                            onChange={(e) =>
                                setA({ ...a, timingChange: e.target.checked })
                            }
                        />
                        Allow timing to change between weekly and monthly.
                    </label>
                    <label className="check">
                        <input
                            type="checkbox"
                            checked={a.existingHoldings}
                            onChange={(e) =>
                                setA({
                                    ...a,
                                    existingHoldings: e.target.checked,
                                })
                            }
                        />
                        Grant authority over pre-existing holdings.
                    </label>
                    <p className="small">
                        Pre-existing holdings are excluded by default. The
                        supplied revision fixtures leave them unchanged even
                        when permission is granted. Closing them always requires
                        a separate review.
                    </p>
                    <fieldset>
                        <legend>Required checks · cannot be removed</legend>
                        {checks.map((c) => (
                            <label className="check" key={c}>
                                <input type="checkbox" checked disabled />
                                {c}
                            </label>
                        ))}
                    </fieldset>
                    <p className="small">
                        Locked risk boundary:{" "}
                        {latest(strategy).rules.maxDrawdown}%. Entry and exit
                        rules are those shown in the current version; changing
                        them requires approval.
                    </p>
                    <h3>Always requires human approval</h3>
                    <ul>
                        <li>
                            Assets, exposure or capital outside these limits.
                        </li>
                        <li>Changes to entry/exit logic or risk limits.</li>
                        <li>
                            Closing pre-existing holdings or changing authority.
                        </li>
                    </ul>
                    <label className="check consent">
                        <input
                            type="checkbox"
                            checked={consent}
                            onChange={(e) => setConsent(e.target.checked)}
                        />
                        I authorize this mode for this strategy and demo
                        account.
                    </label>
                    <p className="small">
                        Saving starts one bounded sample research cycle. The
                        prototype does not run in the background when this page
                        is closed.
                    </p>
                </>
            )}
            {error && (
                <p role="alert" className="error">
                    {error}
                </p>
            )}
            <div className="actions">
                <button
                    className="primary"
                    onClick={() => {
                        if (
                            mode !== "Collaborative" &&
                            (!consent ||
                                !validAuthority({
                                    ...a,
                                    baseline: latest(strategy).rules,
                                }))
                        ) {
                            setError(
                                "Confirm authority and check limits: capital $100–100,000; sizing 1–100% within exposure; update interval 1–365 days; budget 1–100; observation 5–365 days.",
                            );
                            return;
                        }
                        cancel();
                        set((s) => {
                            const n = changeMode(s, strategy.id, mode),
                                p = n.strategies.find(
                                    (x) => x.id === strategy.id,
                                )!;
                            p.authority = {
                                ...a,
                                baseline: structuredClone(
                                    latest(strategy).rules,
                                ),
                            };
                            p.researchPaused = false;
                            event(
                                n,
                                "Authority configured",
                                "User granted account-specific limits. Existing deployment and holdings preserved.",
                                p.id,
                                latest(p).id,
                            );
                            return n;
                        });
                        onClose();
                        if (mode !== "Collaborative")
                            pipeline(strategy.id, "pass");
                    }}
                >
                    Save authority
                </button>
                <button onClick={onClose}>Cancel</button>
            </div>
        </Dialog>
    );
}
