import {
    createContext,
    useContext,
    useEffect,
    useRef,
    useState,
    type ReactNode,
} from "react";
import { initialState, fixtureVersion } from "./fixtures";
import { decode, STORAGE_KEY } from "./persistence";
import { event, evaluateAndActivate } from "./operations";
import { latest, uid, type State, type Page } from "./model";
type Job = { label: string; strategyId?: string; versionId?: string };
type Context = {
    state: State;
    set: (f: (s: State) => State) => void;
    page: Page;
    go: (p: Page) => void;
    conversationId: string;
    selectConversation: (id: string) => void;
    strategyId: string;
    versionId: string;
    selectStrategy: (id: string, versionId?: string) => void;
    artifact: "portfolio" | "strategy" | "report" | null;
    setArtifact: (v: Context["artifact"]) => void;
    job: Job | null;
    run: (job: Job, complete: (s: State) => State) => void;
    cancel: () => void;
    pipeline: (
        id: string,
        scenario: "pass" | "fail" | "outside",
        fail?: boolean,
    ) => void;
    error: string;
    clearError: () => void;
    storageWarning: string;
    reset: () => void;
};
const WorkspaceContext = createContext<Context | null>(null);
export const useWorkspace = () => useContext(WorkspaceContext)!;
export function WorkspaceProvider({ children }: { children: ReactNode }) {
    const [storageWarning, setWarning] = useState("");
    const [state, setState] = useState<State>(() => {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            return decode(raw) ?? initialState();
        } catch {
            return initialState();
        }
    });
    const [page, setPage] = useState<Page>("Home"),
        [conversationId, setConversation] = useState("welcome"),
        [strategyId, setStrategy] = useState("monthly"),
        [versionId, setVersion] = useState(""),
        [artifact, setArtifact] = useState<Context["artifact"]>(null),
        [job, setJob] = useState<Job | null>(null),
        [error, setError] = useState("");
    const stateRef = useRef(state),
        jobRef = useRef<Job | null>(null),
        timer = useRef<ReturnType<typeof setTimeout> | null>(null),
        storageLocked = useRef(false);
    const set = (f: (s: State) => State) => {
        const next = f(stateRef.current);
        stateRef.current = next;
        setState(next);
    };
    useEffect(() => {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (raw && !decode(raw)) {
                storageLocked.current = true;
                setWarning(
                    "Saved demo data could not be read. Use Reset demo in Settings to recover. Your other application data is untouched.",
                );
            }
        } catch {
            setWarning(
                "Browser storage is unavailable. Changes will last only in this tab.",
            );
        }
    }, []);
    useEffect(() => {
        if (!storageLocked.current)
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
            } catch {
                setWarning(
                    "Demo data could not be saved. Keep this tab open or export your strategies.",
                );
            }
    }, [state]);
    const cancel = () => {
        if (timer.current) clearTimeout(timer.current);
        timer.current = null;
        const old = jobRef.current;
        jobRef.current = null;
        setJob(null);
        if (old)
            set((s) => {
                const n = structuredClone(s);
                event(
                    n,
                    old.strategyId ? "Test canceled" : "Conversation canceled",
                    "Pending work canceled; no activation or portfolio change.",
                    old.strategyId,
                    old.versionId,
                );
                return n;
            });
    };
    useEffect(() => {
        const changed = (e: StorageEvent) => {
            if (e.key === STORAGE_KEY) {
                cancel();
                storageLocked.current = true;
                setWarning(
                    "Demo data changed in another tab. This tab has stopped saving. Reload to use the latest state, or reset this demo.",
                );
            }
        };
        addEventListener("storage", changed);
        return () => {
            removeEventListener("storage", changed);
            if (timer.current) clearTimeout(timer.current);
        };
    }, []);
    const run = (j: Job, complete: (s: State) => State) => {
        cancel();
        setError("");
        jobRef.current = j;
        setJob(j);
        timer.current = setTimeout(() => {
            if (jobRef.current !== j) return;
            timer.current = null;
            jobRef.current = null;
            setJob(null);
            try {
                set(complete);
            } catch {
                setError(
                    "The demo step could not finish. Your existing portfolio and running strategy are unchanged. Retry the demo step.",
                );
            }
        }, 1000);
    };
    const pipeline = (
        id: string,
        scenario: "pass" | "fail" | "outside",
        fail = false,
    ) => {
        cancel();
        const p = stateRef.current.strategies.find((x) => x.id === id);
        if (!p) return;
        if (p.researchPaused || p.researchUsed >= p.authority.budget) {
            setError(
                "Research is paused or the configured research budget has been used. Update the strategy settings before trying again.",
            );
            return;
        }
        const version = fixtureVersion(
            scenario === "fail" ? "fail" : "pass",
            uid(),
            latest(p).number + 1,
        );
        // The third predefined fixture changes capital only; it is not an evaluation of arbitrary edits.
        if (scenario === "outside") {
            version.rules.capital = 3000;
            version.description =
                "Predefined larger-capital fixture: requires additional authority.";
            version.report!.fingerprint = JSON.stringify([
                version.rules.assets,
                version.rules.entry,
                version.rules.exit,
                version.rules.timing,
                version.rules.sizing,
                version.rules.capital,
                version.rules.maxDrawdown,
            ]);
        }
        const evidence = version.report;
        delete version.report;
        const generation = p.generation;
        set((s) => {
            const n = structuredClone(s),
                t = n.strategies.find((x) => x.id === id)!;
            t.versions.push(version);
            t.researchUsed++;
            event(n, "Strategy revised", version.description, id, version.id);
            event(
                n,
                "Test started",
                "Preparing a predefined illustrative fixture; no market data is being evaluated.",
                id,
                version.id,
            );
            return n;
        });
        const j = {
            label: "Researching a sample revision",
            strategyId: id,
            versionId: version.id,
        };
        jobRef.current = j;
        setJob(j);
        setError("");
        timer.current = setTimeout(() => {
            if (jobRef.current !== j) return;
            setJob({ ...j, label: "Evaluating illustrative checks" });
            timer.current = setTimeout(() => {
                if (jobRef.current !== j) return;
                timer.current = null;
                jobRef.current = null;
                setJob(null);
                set((s) => {
                    const n = structuredClone(s),
                        t = n.strategies.find((x) => x.id === id);
                    if (!t || t.generation !== generation || t.researchPaused) {
                        event(
                            n,
                            "Test canceled",
                            "Authority changed before completion; activation canceled.",
                            id,
                            version.id,
                        );
                        return n;
                    }
                    if (fail) {
                        event(
                            n,
                            "Test failed",
                            "Demonstrated evaluation error. No evidence or approval was created.",
                            id,
                            version.id,
                        );
                        setError(
                            "Illustrative evaluation failed. Retry a fixture to recover.",
                        );
                        return n;
                    }
                    const v = t.versions.find((x) => x.id === version.id)!;
                    v.report = evidence;
                    event(
                        n,
                        "Test completed",
                        "Predefined fixture evidence attached to this exact version.",
                        id,
                        v.id,
                    );
                    const c = n.conversations.find(
                        (x) => x.id === t.conversationId,
                    );
                    c?.messages.push({
                        id: uid(),
                        role: "assistant",
                        text: `A sample revision for ${t.name} is ready. It uses ${v.rules.assets.join(", ")} with $${v.rules.capital} allocated and ${v.rules.sizing}% maximum exposure. The policy result is recorded in Activity. This is illustrative evidence, not a profitability forecast.`,
                        artifact: "report",
                        strategyId: id,
                    });
                    return evaluateAndActivate(n, id, v.id);
                });
            }, 1000);
        }, 700);
    };
    const go = (p: Page) => {
        cancel();
        setPage(p);
        setError("");
    };
    const reset = () => {
        cancel();
        storageLocked.current = false;
        try {
            localStorage.removeItem(STORAGE_KEY);
        } catch {}
        setWarning("");
        set(() => initialState());
        setPage("Home");
        setConversation("welcome");
        setStrategy("monthly");
        setArtifact(null);
    };
    return (
        <WorkspaceContext.Provider
            value={{
                state,
                set,
                page,
                go,
                conversationId,
                selectConversation: (id) => {
                    cancel();
                    setConversation(id);
                    setPage("Conversations");
                },
                strategyId,
                versionId,
                selectStrategy: (id, version) => {
                    setVersion(version ?? "");
                    cancel();
                    setStrategy(id);
                },
                artifact,
                setArtifact,
                job,
                run,
                cancel,
                pipeline,
                error,
                clearError: () => setError(""),
                storageWarning,
                reset,
            }}
        >
            {children}
        </WorkspaceContext.Provider>
    );
}
