import { useState } from "react";
import { useWorkspace } from "./workspace";
import { Dialog, Field } from "./components";
export function Settings() {
    const { state, set, reset } = useWorkspace(),
        [confirm, setConfirm] = useState(false),
        [exported, setExported] = useState(false);
    const settings = state.settings;
    return (
        <div className="page settings-page">
            <div className="page-intro">
                <span className="eyebrow">Make it yours.</span>
                <h1>A few thoughtful settings.</h1>
                <p>Your choices. Clearly explained.</p>
            </div>
            <section className="card">
                <h2>Your workspace</h2>
                <dl>
                    <dt>Region</dt>
                    <dd>United States only · intended service</dd>
                    <dt>Eligibility</dt>
                    <dd>
                        {state.eligible
                            ? "US residency and age 18+ declared in onboarding. Not verified."
                            : "Exploring synthetic information without a service eligibility declaration."}
                    </dd>
                    <dt>Account</dt>
                    <dd>Demo account · Paper simulation</dd>
                </dl>
            </section>
            <section className="card">
                <h2>Bring your preferred intelligence.</h2>
                <p>
                    The intended service uses your own provider API key. These
                    are preview selections, not implemented integrations.
                </p>
                <Field label="AI provider preference">
                    <select
                        value={settings.provider}
                        onChange={(e) =>
                            set((s) => ({
                                ...s,
                                settings: {
                                    ...s.settings,
                                    provider: e.target
                                        .value as typeof settings.provider,
                                    connected: false,
                                },
                            }))
                        }
                    >
                        <option>OpenAI</option>
                        <option>Anthropic</option>
                        <option>Google</option>
                    </select>
                </Field>
                <Field label="API key">
                    <input
                        disabled
                        value="Unavailable in this prototype"
                        readOnly
                    />
                </Field>
                <div className="actions">
                    <button
                        onClick={() =>
                            set((s) => ({
                                ...s,
                                settings: {
                                    ...s.settings,
                                    connected: !s.settings.connected,
                                },
                            }))
                        }
                    >
                        {settings.connected
                            ? "Disconnect demo"
                            : "Use demo connection"}
                    </button>
                    <span role="status">
                        {settings.connected
                            ? "Demo connection ready"
                            : "No provider connected"}
                    </span>
                </div>
                <p className="small">
                    Provider API usage is billed separately. A consumer chat
                    subscription should not be assumed to include API usage. No
                    key is collected or transmitted here.
                </p>
                <Field label="Monthly API budget preview ($)">
                    <input
                        type="number"
                        min="0"
                        max="1000"
                        value={settings.budget}
                        onChange={(e) => {
                            const n = Number(e.target.value);
                            if (Number.isFinite(n) && n >= 0 && n <= 1000)
                                set((s) => ({
                                    ...s,
                                    settings: { ...s.settings, budget: n },
                                }));
                        }}
                    />
                </Field>
                <p>
                    Preview budget: ${settings.budget}. Actual prototype usage:
                    $0. This is not provider billing enforcement or a price
                    quote.
                </p>
            </section>
            <section className="card">
                <h2>Brokerage and notifications</h2>
                <p>
                    Brokerage: <strong>Not connected</strong>. Only local
                    simulated execution is available.
                </p>
                <button disabled>Live execution unavailable</button>
                <label className="check">
                    <input
                        type="checkbox"
                        checked={settings.notifications}
                        onChange={(e) =>
                            set((s) => ({
                                ...s,
                                settings: {
                                    ...s.settings,
                                    notifications: e.target.checked,
                                },
                            }))
                        }
                    />
                    Show optional demo notification summaries.
                </label>
                <p className="small">
                    Activation decisions always appear in Activity and in the
                    workspace. No email or push messages are sent.
                </p>
            </section>
            <section className="card">
                <h2>Your demo, on this browser.</h2>
                <p>
                    Only local demo state is saved under a dedicated namespace.
                    No account credentials are read. Clearing this site’s
                    storage removes your demo; it is not a production audit
                    record.
                </p>
                <div className="actions">
                    <button
                        onClick={() => {
                            const blob = new Blob(
                                    [
                                        JSON.stringify(
                                            {
                                                prototype: true,
                                                sampleData: true,
                                                strategies: state.strategies,
                                                settings: state.settings,
                                            },
                                            null,
                                            2,
                                        ),
                                    ],
                                    { type: "application/json" },
                                ),
                                url = URL.createObjectURL(blob),
                                a = document.createElement("a");
                            a.href = url;
                            a.download = "aqa-demo-strategies.json";
                            a.click();
                            URL.revokeObjectURL(url);
                            setExported(true);
                        }}
                    >
                        Export demo strategies
                    </button>
                    <button
                        className="danger-text"
                        onClick={() => setConfirm(true)}
                    >
                        Reset demo data
                    </button>
                </div>
                {exported && (
                    <p role="status">
                        Demo export prepared. It contains strategy configuration
                        and synthetic evidence, no credentials.
                    </p>
                )}
            </section>
            {confirm && (
                <Dialog
                    title="Reset this demo?"
                    onClose={() => setConfirm(false)}
                >
                    <p>
                        This removes this prototype’s local conversations,
                        drafts, strategies, permissions, portfolio changes,
                        settings and activity. It does not clear authentication
                        or other application storage.
                    </p>
                    <button className="danger" onClick={reset}>
                        Confirm reset demo
                    </button>
                    <button onClick={() => setConfirm(false)}>
                        Keep my demo
                    </button>
                </Dialog>
            )}
        </div>
    );
}
