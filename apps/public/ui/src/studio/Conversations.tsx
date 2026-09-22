import { useState } from "react";
import { useWorkspace } from "./workspace";
import { uid } from "./model";
import { answer, safeText } from "./conversation";
import { Dialog, Field } from "./components";
import { Portfolio } from "./Portfolio";
import { Strategies } from "./Strategies";
export function Conversations() {
    const {
        state,
        set,
        conversationId,
        selectConversation,
        strategyId,
        selectStrategy,
        artifact,
        setArtifact,
        run,
        job,
        cancel,
        go,
    } = useWorkspace();
    const c =
        state.conversations.find((x) => x.id === conversationId) ??
        state.conversations[0];
    const [modal, setModal] = useState<"rename" | "delete" | null>(null),
        [name, setName] = useState(""),
        [failed, setFailed] = useState(false),
        [retry, setRetry] = useState("");
    const create = () => {
        const id = uid();
        set((s) => ({
            ...s,
            conversations: [
                ...s.conversations,
                {
                    id,
                    name: `New conversation ${s.conversations.length + 1}`,
                    draft: "",
                    messages: [],
                    strategyId,
                },
            ],
        }));
        selectConversation(id);
        setArtifact(null);
    };
    const send = (text: string, simulateFailure = false) => {
        if (!c || !text.trim()) return;
        const clean = safeText(text.trim());
        setFailed(false);
        setRetry(clean);
        set((s) => ({
            ...s,
            conversations: s.conversations.map((x) =>
                x.id === c.id
                    ? {
                          ...x,
                          draft: "",
                          messages: [
                              ...x.messages,
                              { id: uid(), role: "user", text: clean },
                          ],
                      }
                    : x,
            ),
        }));
        run(
            {
                label: /portfolio|holding/i.test(clean)
                    ? "Reviewing sample holdings"
                    : "Preparing an illustrative comparison",
            },
            (s) => {
                if (simulateFailure) {
                    setFailed(true);
                    return s;
                }
                const m = answer(s, c.id, clean);
                return {
                    ...s,
                    conversations: s.conversations.map((x) =>
                        x.id === c.id
                            ? { ...x, messages: [...x.messages, m] }
                            : x,
                    ),
                };
            },
        );
    };
    return (
        <div className="conversation-layout">
            <aside
                className="conversation-list"
                aria-label="Local conversations"
            >
                <div className="section-head">
                    <h2>Conversations</h2>
                    <button
                        className="icon-button"
                        aria-label="New conversation"
                        onClick={create}
                    >
                        ＋
                    </button>
                </div>
                <p className="small muted">
                    A little more clarity, every time.
                </p>
                {state.conversations.map((x) => (
                    <button
                        className="conversation-link"
                        key={x.id}
                        aria-current={x.id === c?.id ? "true" : undefined}
                        onClick={() => {
                            selectConversation(x.id);
                            setArtifact(null);
                            setFailed(false);
                            if (x.strategyId) selectStrategy(x.strategyId);
                        }}
                    >
                        {x.name}
                    </button>
                ))}
                {!c && <button onClick={create}>Start a conversation</button>}
            </aside>
            <div className="conversation-main">
                {c && (
                    <>
                        <header className="conversation-header">
                            <div>
                                <span className="eyebrow">
                                    Scripted assistant · Sample context
                                </span>
                                <h1>{c.name}</h1>
                            </div>
                            <div className="actions">
                                <button
                                    className="text-button"
                                    onClick={() => {
                                        setName(c.name);
                                        setModal("rename");
                                    }}
                                >
                                    Rename
                                </button>
                                <button
                                    className="text-button"
                                    onClick={() => setModal("delete")}
                                >
                                    Delete
                                </button>
                            </div>
                        </header>
                        <div className="messages" aria-live="polite">
                            {!c.messages.length && (
                                <div className="chat-empty">
                                    <span className="assistant-mark">a</span>
                                    <h2>Let’s find your starting point.</h2>
                                    <p>
                                        Choose a question below, or describe
                                        what you’d like to explore.
                                    </p>
                                </div>
                            )}
                            {c.messages.map((m) => (
                                <article
                                    className={`message ${m.role}`}
                                    key={m.id}
                                >
                                    <span className="message-author">
                                        {m.role === "assistant"
                                            ? "AQA · scripted demo"
                                            : "You"}
                                    </span>
                                    <p>{m.text}</p>
                                    {m.artifact && (
                                        <button
                                            className="artifact-link"
                                            onClick={() => {
                                                if (m.strategyId)
                                                    selectStrategy(
                                                        m.strategyId,
                                                    );
                                                setArtifact(m.artifact!);
                                            }}
                                        >
                                            Open{" "}
                                            {m.artifact === "portfolio"
                                                ? "portfolio"
                                                : m.artifact === "report"
                                                  ? "illustrative report"
                                                  : "strategy"}{" "}
                                            <span>↗</span>
                                        </button>
                                    )}
                                </article>
                            ))}
                        </div>
                        {failed && (
                            <div role="alert" className="error">
                                The scripted response could not finish.{" "}
                                <button onClick={() => send(retry)}>
                                    Retry response
                                </button>
                            </div>
                        )}
                        <div className="composer-area">
                            <div className="suggestions">
                                {[
                                    "Understand my portfolio",
                                    "Explore an investing idea",
                                    "Revise the strategy",
                                    "Review the research proposal",
                                ].map((q) => (
                                    <button
                                        disabled={!!job}
                                        key={q}
                                        onClick={() => send(q)}
                                    >
                                        {q}
                                    </button>
                                ))}
                            </div>
                            <form
                                className="composer"
                                onSubmit={(e) => {
                                    e.preventDefault();
                                    send(c.draft);
                                }}
                            >
                                <textarea
                                    aria-label="Message"
                                    maxLength={2000}
                                    placeholder="What would you like to explore?"
                                    value={c.draft}
                                    onChange={(e) =>
                                        set((s) => ({
                                            ...s,
                                            conversations: s.conversations.map(
                                                (x) =>
                                                    x.id === c.id
                                                        ? {
                                                              ...x,
                                                              draft: safeText(
                                                                  e.target
                                                                      .value,
                                                              ),
                                                          }
                                                        : x,
                                            ),
                                        }))
                                    }
                                />
                                <button
                                    className="primary"
                                    disabled={!!job || !c.draft.trim()}
                                    type="submit"
                                >
                                    Send ↑
                                </button>
                            </form>
                            <div className="composer-caption">
                                <span>
                                    Scripted responses. No personal information
                                    or API keys, please.
                                </span>
                                <details>
                                    <summary>Demo recovery</summary>
                                    <button
                                        disabled={!!job}
                                        onClick={() =>
                                            send(
                                                c.draft || "Review my strategy",
                                                true,
                                            )
                                        }
                                    >
                                        Simulate response error
                                    </button>
                                </details>
                            </div>
                        </div>
                    </>
                )}
            </div>
            {artifact && (
                <aside
                    className="artifact-panel"
                    aria-label="Conversation artifact"
                >
                    <div className="artifact-toolbar">
                        <span>In this conversation</span>
                        <button
                            aria-label="Close artifact"
                            onClick={() => setArtifact(null)}
                        >
                            Close ×
                        </button>
                    </div>
                    <div className="artifact-tabs">
                        {(["portfolio", "strategy", "report"] as const).map(
                            (t) => (
                                <button
                                    key={t}
                                    aria-pressed={artifact === t}
                                    onClick={() => setArtifact(t)}
                                >
                                    {t === "report"
                                        ? "Evidence"
                                        : t[0].toUpperCase() + t.slice(1)}
                                </button>
                            ),
                        )}
                    </div>
                    {artifact === "portfolio" ? (
                        <Portfolio
                            compact
                            ask={(symbol) =>
                                send(
                                    symbol
                                        ? `Explain ${symbol} holding`
                                        : "Understand my portfolio",
                                )
                            }
                        />
                    ) : (
                        <Strategies
                            compact
                            reportOnly={artifact === "report"}
                        />
                    )}
                </aside>
            )}
            {modal && c && (
                <Dialog
                    title={
                        modal === "rename"
                            ? "Name this conversation"
                            : "Delete this conversation?"
                    }
                    onClose={() => setModal(null)}
                >
                    {modal === "rename" ? (
                        <>
                            <Field label="Conversation name">
                                <input
                                    value={name}
                                    maxLength={80}
                                    onChange={(e) => setName(e.target.value)}
                                />
                            </Field>
                            <button
                                className="primary"
                                disabled={!name.trim()}
                                onClick={() => {
                                    set((s) => ({
                                        ...s,
                                        conversations: s.conversations.map(
                                            (x) =>
                                                x.id === c.id
                                                    ? {
                                                          ...x,
                                                          name: safeText(
                                                              name.trim(),
                                                          ),
                                                      }
                                                    : x,
                                        ),
                                    }));
                                    setModal(null);
                                }}
                            >
                                Save name
                            </button>
                        </>
                    ) : (
                        <>
                            <p>
                                This removes the local messages and draft.
                                Associated strategy versions and evidence stay
                                available; a fresh project conversation is
                                created if needed.
                            </p>
                            <button
                                className="danger"
                                onClick={() => {
                                    cancel();
                                    let next = "";
                                    set((s) => {
                                        const n = structuredClone(s);
                                        n.conversations =
                                            n.conversations.filter(
                                                (x) => x.id !== c.id,
                                            );
                                        for (const p of n.strategies.filter(
                                            (x) => x.conversationId === c.id,
                                        )) {
                                            const id = uid();
                                            n.conversations.push({
                                                id,
                                                name: p.name,
                                                draft: "",
                                                messages: [],
                                                strategyId: p.id,
                                            });
                                            p.conversationId = id;
                                        }
                                        next = n.conversations[0]?.id ?? "";
                                        return n;
                                    });
                                    selectConversation(next);
                                    setArtifact(null);
                                    setModal(null);
                                }}
                            >
                                Delete local conversation
                            </button>
                        </>
                    )}
                </Dialog>
            )}
        </div>
    );
}
