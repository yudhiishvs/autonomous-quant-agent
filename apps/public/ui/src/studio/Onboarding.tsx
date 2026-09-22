import { useState } from "react";
import { useWorkspace } from "./workspace";
import { Dialog } from "./components";
export function Onboarding() {
    const { set } = useWorkspace();
    const [step, setStep] = useState(0),
        [age, setAge] = useState(false),
        [us, setUs] = useState(false),
        [cash, setCash] = useState(false),
        [error, setError] = useState("");
    const enter = (demo: boolean) =>
        set((s) => ({
            ...s,
            entered: true,
            eligible: !demo,
            portfolio:
                cash && !demo
                    ? {
                          cash: 10000,
                          holdings: [],
                          source: "cash",
                          timestamp: "User-selected demo starting cash",
                      }
                    : s.portfolio,
        }));
    return (
        <main className="welcome">
            <header>
                <a
                    className="brand"
                    href="/studio.html"
                    aria-label="AQA prototype home"
                >
                    AQA
                    <span className="brand-dot" />
                </a>
                <span className="muted">
                    Interactive prototype · Sample data
                </span>
            </header>
            <section className="welcome-hero">
                <span className="eyebrow">
                    A little clarity. A better starting point.
                </span>
                <h1>
                    Your next chapter.
                    <br />
                    <span>Invest in understanding.</span>
                </h1>
                <p>
                    A workspace to explore what you own, turn ideas into clear
                    strategies, and see the evidence. One conversation at a
                    time.
                </p>
                <div className="actions">
                    <button className="primary" onClick={() => setStep(1)}>
                        Get started
                    </button>
                    <button onClick={() => enter(true)}>
                        Explore the demo <span aria-hidden="true">↗</span>
                    </button>
                </div>
                <p className="small muted">
                    No sign-up, credentials, or real money. Your demo stays in
                    this browser.
                </p>
            </section>
            <div className="welcome-cards">
                <article>
                    <span className="number">01</span>
                    <h2>Start where you are.</h2>
                    <p>
                        Understand a sample portfolio, or begin with simulated
                        cash.
                    </p>
                </article>
                <article>
                    <span className="number">02</span>
                    <h2>Make your idea tangible.</h2>
                    <p>
                        Work through rules, trade-offs, and illustrative
                        evidence together.
                    </p>
                </article>
                <article>
                    <span className="number">03</span>
                    <h2>Stay in control.</h2>
                    <p>
                        Choose what happens automatically. Set boundaries you
                        can change.
                    </p>
                </article>
            </div>
            <footer>
                US stocks & ETFs · Paper-first design · Live execution
                unavailable
            </footer>
            {step > 0 && (
                <Dialog
                    title={
                        step === 1
                            ? "First, a little context."
                            : step === 2
                              ? "Where would you like to start?"
                              : "Your intelligence. Your choice."
                    }
                    onClose={() => setStep(0)}
                >
                    <p className="eyebrow">Step {step} of 3</p>
                    {step === 1 ? (
                        <>
                            <p>
                                The intended service is for US residents aged 18
                                or older. These demo declarations are not
                                identity verification.
                            </p>
                            <label className="check">
                                <input
                                    type="checkbox"
                                    checked={age}
                                    onChange={(e) => setAge(e.target.checked)}
                                />
                                I am 18 or older.
                            </label>
                            <label className="check">
                                <input
                                    type="checkbox"
                                    checked={us}
                                    onChange={(e) => setUs(e.target.checked)}
                                />
                                I reside in the United States.
                            </label>
                            {error && (
                                <p role="alert" className="error">
                                    {error}
                                </p>
                            )}
                            <button
                                className="primary"
                                onClick={() =>
                                    age && us
                                        ? (setError(""), setStep(2))
                                        : setError(
                                              "The simulated service requires US residency and age 18 or older. You can still explore the synthetic demo.",
                                          )
                                }
                            >
                                Continue
                            </button>
                            <button onClick={() => enter(true)}>
                                Explore the demo
                            </button>
                        </>
                    ) : step === 2 ? (
                        <>
                            <label className="choice">
                                <input
                                    type="radio"
                                    name="start"
                                    checked={!cash}
                                    onChange={() => setCash(false)}
                                />
                                <span>
                                    <strong>
                                        Use a sample existing portfolio
                                    </strong>
                                    <small>
                                        Explore $25,000 across stocks, ETFs and
                                        cash.
                                    </small>
                                </span>
                            </label>
                            <label className="choice">
                                <input
                                    type="radio"
                                    name="start"
                                    checked={cash}
                                    onChange={() => setCash(true)}
                                />
                                <span>
                                    <strong>Start with simulated cash</strong>
                                    <small>
                                        Begin with $10,000 and no holdings.
                                    </small>
                                </span>
                            </label>
                            <button
                                className="primary"
                                onClick={() => setStep(3)}
                            >
                                Continue
                            </button>
                        </>
                    ) : (
                        <>
                            <p>
                                The intended service uses your chosen provider’s
                                API key. API usage is billed separately; a
                                consumer chat subscription should not be assumed
                                to include API usage.
                            </p>
                            <div className="note">
                                This prototype uses scripted responses. It
                                collects no API keys and makes no provider
                                requests.
                            </div>
                            <p>
                                You’ll begin in <strong>Collaborative</strong>{" "}
                                mode. Demo work is saved locally in this
                                browser.
                            </p>
                            <button
                                className="primary"
                                onClick={() => enter(false)}
                            >
                                Open my workspace
                            </button>
                        </>
                    )}
                </Dialog>
            )}
        </main>
    );
}
