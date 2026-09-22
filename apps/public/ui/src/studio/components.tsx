import { useEffect, useRef, type ReactNode } from "react";
import { money } from "./model";
export function Dialog({
    title,
    children,
    onClose,
}: {
    title: string;
    children: ReactNode;
    onClose: () => void;
}) {
    const ref = useRef<HTMLDialogElement>(null);
    useEffect(() => {
        const prior = document.activeElement as HTMLElement;
        ref.current?.showModal();
        return () => {
            if (prior?.isConnected) prior.focus();
        };
    }, []);
    return (
        <dialog
            ref={ref}
            onKeyDown={(e) => {
                if (e.key !== "Tab") return;
                const nodes = Array.from(
                    ref.current!.querySelectorAll<HTMLElement>(
                        'button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary,[tabindex="0"]',
                    ),
                ).filter((x) => x.getClientRects().length > 0);
                const first = nodes[0],
                    last = nodes[nodes.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                    e.preventDefault();
                    last?.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault();
                    first?.focus();
                }
            }}
            onCancel={(e) => {
                e.preventDefault();
                onClose();
            }}
            aria-labelledby="dialog-title"
        >
            <div className="dialog-head">
                <h2 id="dialog-title">{title}</h2>
                <button
                    className="icon-button"
                    aria-label="Close dialog"
                    onClick={onClose}
                >
                    ×
                </button>
            </div>
            {children}
        </dialog>
    );
}
export function Chart({
    values,
    benchmark,
    label = "Illustrative portfolio value",
    capital = 10000,
}: {
    values: number[];
    benchmark?: number[];
    label?: string;
    capital?: number;
}) {
    const all = [...values, ...(benchmark ?? [])],
        min = Math.min(...all) - 3,
        max = Math.max(...all) + 3;
    const points = (v: number[]) =>
        v
            .map(
                (n, i) =>
                    `${30 + (i * 540) / (v.length - 1)},${170 - ((n - min) / (max - min)) * 140}`,
            )
            .join(" ");
    return (
        <figure className="chart">
            <svg
                viewBox="0 0 600 205"
                role="img"
                aria-label={`${label}. Starts at ${money((capital * values[0]) / 100)}, ends at ${money((capital * values.at(-1)!) / 100)}. Synthetic data.`}
            >
                <path
                    d="M30 30H570 M30 100H570 M30 170H570"
                    stroke="#e8e8ed"
                    fill="none"
                />
                {benchmark && (
                    <polyline
                        points={points(benchmark)}
                        fill="none"
                        stroke="#7a7a85"
                        strokeWidth="2"
                        strokeDasharray="5 5"
                    />
                )}
                <polyline
                    points={points(values)}
                    fill="none"
                    stroke="#0066cc"
                    strokeWidth="3"
                    strokeLinejoin="round"
                />
                <text x="30" y="198">
                    Start
                </text>
                <text x="535" y="198">
                    End
                </text>
            </svg>
            <figcaption>
                {label} · synthetic illustration{" "}
                {benchmark && "· Blue: strategy · Dashed: benchmark"}
            </figcaption>
        </figure>
    );
}
export function Field({
    label,
    children,
}: {
    label: string;
    children: ReactNode;
}) {
    return (
        <label className="field">
            <span>{label}</span>
            {children}
        </label>
    );
}
export function Empty({
    title,
    children,
}: {
    title: string;
    children: ReactNode;
}) {
    return (
        <div className="empty">
            <h2>{title}</h2>
            <p>{children}</p>
        </div>
    );
}
