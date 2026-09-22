import React from "react";
import { createRoot } from "react-dom/client";
import { WorkspaceProvider, useWorkspace } from "./workspace";
import { Onboarding } from "./Onboarding";
import { Home } from "./Home";
import { Conversations } from "./Conversations";
import { Portfolio } from "./Portfolio";
import { Strategies } from "./Strategies";
import { Activity } from "./Activity";
import { Settings } from "./Settings";
import { uid, type Page } from "./model";
import { answer } from "./conversation";
import "./studio.css";
const destinations: Page[] = [
    "Home",
    "Conversations",
    "Portfolio",
    "Strategies",
    "Activity",
    "Settings",
];
const icons = ["⌂", "◯", "◔", "≋", "↗", "⚙"];
function App() {
    const w = useWorkspace();
    const {
        state,
        set,
        page,
        go,
        selectConversation,
        job,
        cancel,
        error,
        clearError,
        storageWarning,
    } = w;
    const start = (kind: "portfolio" | "idea" | "review", symbol?: string) => {
        const text =
            kind === "portfolio"
                ? symbol
                    ? `Explain my ${symbol} holding`
                    : "Understand my portfolio"
                : kind === "idea"
                  ? "Explore an investing idea"
                  : "Review a strategy";
        let id = state.conversations[0]?.id;
        if (!id) {
            id = uid();
            const cid = id;
            set((s) => ({
                ...s,
                conversations: [
                    {
                        id: cid,
                        name: "A new starting point",
                        draft: "",
                        messages: [],
                        strategyId: s.strategies[0].id,
                    },
                ],
            }));
        }
        selectConversation(id);
        const cid = id;
        set((s) => ({
            ...s,
            conversations: s.conversations.map((c) =>
                c.id === cid
                    ? {
                          ...c,
                          messages: [
                              ...c.messages,
                              { id: uid(), role: "user", text },
                          ],
                      }
                    : c,
            ),
        }));
        w.run(
            {
                label:
                    kind === "portfolio"
                        ? "Reviewing sample holdings"
                        : "Preparing an illustrative comparison",
            },
            (s) => ({
                ...s,
                conversations: s.conversations.map((c) =>
                    c.id === cid
                        ? {
                              ...c,
                              messages: [...c.messages, answer(s, cid, text)],
                          }
                        : c,
                ),
            }),
        );
    };
    if (!state.entered)
        return (
            <>
                <Onboarding />
                {storageWarning && (
                    <p role="alert" className="storage-warning">
                        {storageWarning}
                        <button onClick={w.reset}>Reset unreadable demo</button>
                    </p>
                )}
            </>
        );
    return (
        <div className="app">
            <a className="skip-link" href="#main-content">
                Skip to content
            </a>
            <aside className="sidebar">
                <button
                    className="brand"
                    onClick={() => go("Home")}
                    aria-label="AQA home"
                >
                    AQA
                    <span className="brand-dot" />
                </button>
                <div className="workspace-label">
                    <span className="workspace-avatar">D</span>
                    <span>
                        My workspace<small>Demo account</small>
                    </span>
                </div>
                <nav aria-label="Main navigation">
                    {destinations.map((p, i) => (
                        <button
                            key={p}
                            className={page === p ? "active" : ""}
                            aria-current={page === p ? "page" : undefined}
                            onClick={() => go(p)}
                        >
                            <span className="nav-icon" aria-hidden="true">
                                {icons[i]}
                            </span>
                            {p}
                        </button>
                    ))}
                </nav>
                <div className="sidebar-bottom">
                    <span className="paper-dot" /> Paper simulation
                    <p>
                        Room to explore.
                        <br />
                        Boundaries to rely on.
                    </p>
                    <span className="small">Live execution unavailable</span>
                </div>
            </aside>
            <div className="app-content">
                <header className="topbar">
                    <span>{page}</span>
                    <span className="prototype-label">
                        Interactive prototype · Sample data
                    </span>
                    <span className="avatar" aria-label="Demo workspace">
                        D
                    </span>
                </header>
                {storageWarning && (
                    <div role="alert" className="storage-warning">
                        {storageWarning}
                    </div>
                )}
                {state.notice && (
                    <div className="notice" role="status">
                        <span>{state.notice}</span>
                        <button
                            onClick={() => set((s) => ({ ...s, notice: "" }))}
                            aria-label="Dismiss notification"
                        >
                            ×
                        </button>
                    </div>
                )}
                {job && (
                    <div className="job-bar" role="status">
                        <span className="spinner" />
                        <span>
                            {job.label}
                            <small>Scripted demo · no external execution</small>
                        </span>
                        <button onClick={cancel}>Cancel</button>
                    </div>
                )}
                {error && (
                    <div className="error global-error" role="alert">
                        {error}
                        <button onClick={clearError}>Dismiss</button>
                    </div>
                )}
                <main id="main-content" tabIndex={-1}>
                    {page === "Home" ? (
                        <Home start={start} />
                    ) : page === "Conversations" ? (
                        <Conversations />
                    ) : page === "Portfolio" ? (
                        <Portfolio
                            ask={(symbol) => start("portfolio", symbol)}
                        />
                    ) : page === "Strategies" ? (
                        <Strategies />
                    ) : page === "Activity" ? (
                        <Activity />
                    ) : (
                        <Settings />
                    )}
                </main>
            </div>
        </div>
    );
}
createRoot(document.getElementById("root")!).render(
    <WorkspaceProvider>
        <App />
    </WorkspaceProvider>,
);
