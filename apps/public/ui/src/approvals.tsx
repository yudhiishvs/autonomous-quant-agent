import React, { useEffect, useRef, useState } from "react";
import { api } from "./client";

type Limits = {
    max_order_notional: string;
    max_account_exposure: string;
    max_position_shares: number;
    max_daily_loss: string;
    max_daily_turnover: string;
    max_orders_per_minute: number;
    validity_days: number;
};
type Approval = {
    id: string;
    account_id: string;
    version_id: string;
    binding_hash: string;
    limits: Limits;
    expires_at: string;
    review_until: string;
    state: "draft" | "approved" | "revoked";
    block_reason: string | null;
    execution_available: false;
};
type Account = { id: string; state: string; snapshot: { label: string } };
const reasons: Record<string, string> = {
    approval_draft: "Awaiting your confirmation",
    approval_revoked: "Approval revoked",
    approval_expired: "Approval expired",
    review_expired: "Review expired. Create a new review.",
    account_disconnected: "Paper account disconnected",
    connection_changed: "Account reconnected. A new approval is required.",
    approval_signature_invalid:
        "Approval cannot be verified. Revoke and review again.",
};

export function Approvals({
    versionId,
    versionName,
}: {
    versionId: string;
    versionName: string;
}) {
    const [accounts, setAccounts] = useState<Account[]>([]);
    const [records, setRecords] = useState<Approval[]>([]);
    const [available, setAvailable] = useState(false);
    const [loading, setLoading] = useState(true);
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [review, setReview] = useState<Approval | null>(null);
    const [consent, setConsent] = useState(false);
    const [revoking, setRevoking] = useState<Approval | null>(null);
    const pending = useRef<{ payload: string; id: string } | null>(null);
    const summary = useRef<HTMLDivElement>(null);
    const dialog = useRef<HTMLDialogElement>(null);
    const feedback = useRef<HTMLParagraphElement>(null);

    async function load() {
        setLoading(true);
        setError("");
        try {
            const [accountResult, approvalResult] = await Promise.all([
                api<{ accounts: Account[] }>("/api/v1/accounts"),
                api<{ approval_available: boolean; approvals: Approval[] }>(
                    "/api/v1/approvals",
                ),
            ]);
            setAccounts(accountResult.accounts);
            setRecords(approvalResult.approvals);
            setAvailable(approvalResult.approval_available);
            setReview(null);
            setConsent(false);
        } catch (e) {
            setError(
                e instanceof Error ? e.message : "Unable to load approvals.",
            );
        } finally {
            setLoading(false);
        }
    }
    useEffect(() => {
        void load();
    }, []);
    useEffect(() => {
        if (review) summary.current?.focus();
    }, [review]);
    useEffect(() => {
        if (notice) feedback.current?.focus();
    }, [notice]);
    useEffect(() => {
        if (revoking) dialog.current?.showModal();
    }, [revoking]);
    function update(row: Approval) {
        setRecords((current) => [
            row,
            ...current.filter((item) => item.id !== row.id),
        ]);
    }
    async function preview(event: React.FormEvent<HTMLFormElement>) {
        event.preventDefault();
        setBusy(true);
        setError("");
        setNotice("");
        const data = new FormData(event.currentTarget);
        const limits: Limits = {
            max_order_notional: String(data.get("order_limit")),
            max_account_exposure: String(data.get("exposure")),
            max_position_shares: Number(data.get("position")),
            max_daily_loss: String(data.get("loss")),
            max_daily_turnover: String(data.get("turnover")),
            max_orders_per_minute: Number(data.get("frequency")),
            validity_days: Number(data.get("validity")),
        };
        const payload = JSON.stringify({
            account_id: data.get("account"),
            version_id: versionId,
            limits,
        });
        if (pending.current?.payload !== payload)
            pending.current = { payload, id: crypto.randomUUID() };
        try {
            const row = await api<Approval>("/api/v1/approvals", {
                method: "POST",
                body: JSON.stringify({
                    ...JSON.parse(payload),
                    request_id: pending.current.id,
                }),
            });
            pending.current = null;
            update(row);
            setReview(row);
            setConsent(false);
        } catch (e) {
            setError(
                e instanceof Error ? e.message : "Review failed. Please retry.",
            );
        } finally {
            setBusy(false);
        }
    }
    async function confirm() {
        if (!review || !consent) return;
        setBusy(true);
        setError("");
        try {
            const row = await api<Approval>(
                `/api/v1/approvals/${review.id}/confirm`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        confirm: "approve_paper_strategy_with_reviewed_limits",
                        reviewed_hash: review.binding_hash,
                    }),
                },
            );
            update(row);
            setReview(null);
            setConsent(false);
            setNotice(
                "Configuration approved. Execution is unavailable; no strategy has started.",
            );
        } catch (e) {
            setError(
                e instanceof Error
                    ? e.message
                    : "Approval could not be confirmed.",
            );
        } finally {
            setBusy(false);
        }
    }
    async function revoke() {
        if (!revoking) return;
        setBusy(true);
        setError("");
        try {
            const row = await api<Approval>(
                `/api/v1/approvals/${revoking.id}/revoke`,
                {
                    method: "POST",
                    body: JSON.stringify({
                        confirm: "revoke_without_cancelling_orders",
                    }),
                },
            );
            update(row);
            dialog.current?.close();
            setRevoking(null);
            if (review?.id === row.id) setReview(null);
            setNotice(
                "Approval revoked. No broker orders were cancelled and no positions were closed.",
            );
        } catch (e) {
            setError(
                e instanceof Error
                    ? e.message
                    : "Revocation failed. Retry to revoke.",
            );
        } finally {
            setBusy(false);
        }
    }
    const connected = accounts.filter(
        (account) => account.state === "connected",
    );
    const accountLabel = (id: string) =>
        accounts.find((account) => account.id === id)?.snapshot.label ??
        "Paper account";
    return (
        <section className="approval-section" aria-labelledby="approval-title">
            <h2 id="approval-title">Limits &amp; approval</h2>
            <p>
                Review <strong>{versionName}</strong> for one paper account.
                Changing its version, limits or connection requires a new
                approval.
            </p>
            <p className="muted">
                These limits are ceilings for future risk checks, not guaranteed
                loss protection. Approval alone does not start execution.
            </p>
            {loading && <p role="status">Loading approvals…</p>}
            {error && (
                <p role="alert" className="error">
                    {error}
                </p>
            )}
            {notice && (
                <p
                    role="status"
                    className="notice"
                    tabIndex={-1}
                    ref={feedback}
                >
                    {notice}
                </p>
            )}
            <button
                className="quiet"
                disabled={busy || loading}
                onClick={() => void load()}
            >
                Refresh approvals
            </button>
            {!loading && !available && (
                <p>
                    Approval is unavailable until this installation has a
                    signing key and Alpaca connection configuration.
                </p>
            )}
            {!loading && available && connected.length === 0 && (
                <p>Connect a paper account above, then refresh approvals.</p>
            )}
            {!loading && available && connected.length > 0 && !review && (
                <form onSubmit={(event) => void preview(event)}>
                    <fieldset disabled={busy}>
                        <legend>Risk limits in USD</legend>
                        <label>
                            Paper account
                            <select name="account" required>
                                {connected.map((account) => (
                                    <option key={account.id} value={account.id}>
                                        {account.snapshot.label}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <div className="row">
                            <label>
                                Maximum order (USD)
                                <input
                                    name="order_limit"
                                    type="number"
                                    min="0.01"
                                    max="1000000"
                                    step="0.01"
                                    defaultValue="1000"
                                    required
                                />
                            </label>
                            <label>
                                Maximum account exposure (USD)
                                <input
                                    name="exposure"
                                    type="number"
                                    min="0.01"
                                    max="1000000"
                                    step="0.01"
                                    defaultValue="5000"
                                    required
                                />
                            </label>
                        </div>
                        <div className="row">
                            <label>
                                Maximum position (shares)
                                <input
                                    name="position"
                                    type="number"
                                    min="1"
                                    max="100000"
                                    step="1"
                                    defaultValue="10"
                                    required
                                />
                            </label>
                            <label>
                                Daily loss ceiling (USD)
                                <input
                                    name="loss"
                                    type="number"
                                    min="0.01"
                                    max="1000000"
                                    step="0.01"
                                    defaultValue="100"
                                    required
                                />
                            </label>
                        </div>
                        <div className="row">
                            <label>
                                Daily turnover ceiling (USD)
                                <input
                                    name="turnover"
                                    type="number"
                                    min="0.01"
                                    max="10000000"
                                    step="0.01"
                                    defaultValue="10000"
                                    required
                                />
                            </label>
                            <label>
                                Maximum orders per minute
                                <input
                                    name="frequency"
                                    type="number"
                                    min="1"
                                    max="10"
                                    step="1"
                                    defaultValue="2"
                                    required
                                />
                            </label>
                        </div>
                        <label>
                            Approval duration
                            <select name="validity" defaultValue="1">
                                <option value="1">1 day</option>
                                <option value="7">7 days</option>
                                <option value="30">30 days</option>
                            </select>
                        </label>
                        <button type="submit">
                            {busy ? "Checking account…" : "Review limits"}
                        </button>
                    </fieldset>
                </form>
            )}
            {review && (
                <div
                    className="detail"
                    ref={summary}
                    tabIndex={-1}
                    aria-label="Approval summary"
                >
                    <h3>Review before approving</h3>
                    <p>
                        {versionName} · {accountLabel(review.account_id)}
                    </p>
                    <dl>
                        <dt>Maximum order</dt>
                        <dd>USD {review.limits.max_order_notional}</dd>
                        <dt>Account exposure ceiling</dt>
                        <dd>USD {review.limits.max_account_exposure}</dd>
                        <dt>Position ceiling</dt>
                        <dd>{review.limits.max_position_shares} shares</dd>
                        <dt>Daily loss ceiling</dt>
                        <dd>USD {review.limits.max_daily_loss}</dd>
                        <dt>Daily turnover ceiling</dt>
                        <dd>USD {review.limits.max_daily_turnover}</dd>
                        <dt>Order frequency ceiling</dt>
                        <dd>
                            {review.limits.max_orders_per_minute} per minute
                        </dd>
                        <dt>Approval expires</dt>
                        <dd>{new Date(review.expires_at).toLocaleString()}</dd>
                        <dt>Review deadline</dt>
                        <dd>
                            {new Date(review.review_until).toLocaleString()}
                        </dd>
                    </dl>
                    <details>
                        <summary>Approval fingerprint</summary>
                        <p className="fingerprint">{review.binding_hash}</p>
                    </details>
                    <label className="checkbox">
                        <input
                            type="checkbox"
                            checked={consent}
                            onChange={(event) =>
                                setConsent(event.target.checked)
                            }
                        />
                        I approve this paper strategy, account and these limits.
                        It will not start automatically.
                    </label>
                    <div className="actions">
                        <button
                            disabled={busy || !consent}
                            onClick={() => void confirm()}
                        >
                            {busy ? "Confirming…" : "Approve configuration"}
                        </button>
                        <button
                            className="quiet"
                            disabled={busy}
                            onClick={() => {
                                setReview(null);
                                setConsent(false);
                            }}
                        >
                            Change limits
                        </button>
                    </div>
                </div>
            )}
            <h3>Approval history for this version</h3>
            {records.filter((row) => row.version_id === versionId).length ===
                0 &&
                !loading && <p>No approvals yet.</p>}
            <ul className="versions">
                {records
                    .filter((row) => row.version_id === versionId)
                    .map((row) => (
                        <li key={row.id} className="detail">
                            <strong>{accountLabel(row.account_id)}</strong>
                            <p>
                                {row.block_reason
                                    ? (reasons[row.block_reason] ??
                                      "Approval needs a new review")
                                    : "Approved configuration · Execution unavailable"}
                            </p>
                            <p className="muted">
                                Expires{" "}
                                {new Date(row.expires_at).toLocaleString()}
                            </p>
                            {row.state !== "revoked" && (
                                <button
                                    className="quiet"
                                    disabled={busy}
                                    onClick={() => setRevoking(row)}
                                >
                                    Revoke approval
                                </button>
                            )}
                        </li>
                    ))}
            </ul>
            <p className="help">
                Development limit: 100 approval records per workspace, including
                expired and revoked reviews.
            </p>
            <dialog
                ref={dialog}
                onCancel={() => setRevoking(null)}
                aria-labelledby="revoke-title"
            >
                <h2 id="revoke-title">Revoke this approval?</h2>
                <p>
                    This removes future permission. It does not cancel broker
                    orders or close positions.
                </p>
                <div className="actions">
                    <button
                        className="quiet"
                        disabled={busy}
                        onClick={() => {
                            dialog.current?.close();
                            setRevoking(null);
                        }}
                    >
                        Keep approval
                    </button>
                    <button disabled={busy} onClick={() => void revoke()}>
                        Confirm revocation
                    </button>
                </div>
            </dialog>
        </section>
    );
}
