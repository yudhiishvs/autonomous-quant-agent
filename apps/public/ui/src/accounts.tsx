import { useEffect, useRef, useState } from "react";
import { api } from "./client";

type Account = {
    id: string;
    state: "connected" | "disconnected" | "reconnect_required";
    revision: number;
    snapshot: {
        label: string;
        currency: "USD";
        cash: string;
        equity: string;
        checked_at: string;
    };
};
type AccountsResponse = { connection_available: boolean; accounts: Account[] };

export function Accounts({ onChange }: { onChange: () => void }) {
    const [data, setData] = useState<AccountsResponse | null>(null);
    const [error, setError] = useState("");
    const [notice, setNotice] = useState("");
    const [busy, setBusy] = useState(false);
    const [consent, setConsent] = useState(false);
    const [disconnecting, setDisconnecting] = useState<Account | null>(null);
    const dialog = useRef<HTMLDialogElement>(null);

    async function load() {
        try {
            setData(await api<AccountsResponse>("/api/v1/accounts"));
        } catch (e) {
            setError(
                e instanceof Error
                    ? e.message
                    : "Accounts could not be loaded.",
            );
        }
    }
    useEffect(() => {
        void load();
    }, []);
    useEffect(() => {
        if (disconnecting) dialog.current?.showModal();
    }, [disconnecting]);

    async function connect() {
        if (!consent) return;
        setBusy(true);
        setError("");
        try {
            const result = await api<{ authorization_url: string }>(
                "/api/v1/accounts/connect",
                {
                    method: "POST",
                    body: JSON.stringify({ consent: "paper_only" }),
                },
            );
            const url = new URL(result.authorization_url);
            if (
                url.origin !== "https://app.alpaca.markets" ||
                url.pathname !== "/oauth/authorize" ||
                url.searchParams.get("env") !== "paper" ||
                url.username ||
                url.password
            )
                throw new Error(
                    "Paper consent destination could not be verified.",
                );
            window.location.assign(url.href);
        } catch (e) {
            setError(
                e instanceof Error ? e.message : "Connection could not start.",
            );
            setBusy(false);
        }
    }
    async function refresh(account: Account) {
        setBusy(true);
        setError("");
        setNotice("");
        try {
            await api("/api/v1/accounts/" + account.id + "/refresh", {
                method: "POST",
            });
            setNotice("Paper account snapshot refreshed.");
        } catch (e) {
            setError(
                e instanceof Error ? e.message : "Refresh was not confirmed.",
            );
        } finally {
            await load();
            onChange();
            setBusy(false);
        }
    }
    async function disconnect() {
        if (!disconnecting) return;
        setBusy(true);
        setError("");
        setNotice("");
        try {
            const result = await api<{ disconnected: boolean }>(
                "/api/v1/accounts/" + disconnecting.id + "/disconnect",
                {
                    method: "POST",
                    body: JSON.stringify({
                        confirm: "disconnect_without_cancelling_orders",
                    }),
                },
            );
            if (!result.disconnected)
                throw new Error("Disconnect was not confirmed.");
            setNotice(
                "Disconnected here. Existing broker orders were not cancelled. Revoke app access in Alpaca if you also want to remove the provider grant.",
            );
            dialog.current?.close();
            setDisconnecting(null);
        } catch (e) {
            setError(
                e instanceof Error
                    ? e.message
                    : "Disconnect was not confirmed. Try again.",
            );
        } finally {
            await load();
            onChange();
            setBusy(false);
        }
    }

    return (
        <section className="accounts" aria-labelledby="accounts-title">
            <h2 id="accounts-title">Paper accounts</h2>
            {error && (
                <p role="alert" className="error">
                    {error}
                </p>
            )}
            {notice && (
                <p role="status" className="notice">
                    {notice}
                </p>
            )}
            {!data && !error && <p>Loading paper accounts…</p>}
            {!data && error && (
                <button
                    onClick={() => {
                        setError("");
                        void load();
                    }}
                >
                    Retry accounts
                </button>
            )}
            {data && (
                <>
                    {!data.connection_available && (
                        <p className="muted">
                            Alpaca connections are not configured on this
                            installation. You can still prepare strategy
                            versions.
                        </p>
                    )}
                    {data.accounts.length === 0 && (
                        <p>No paper account connected.</p>
                    )}
                    <ul className="account-list">
                        {data.accounts.map((account) => (
                            <li key={account.id}>
                                <div className="account-heading">
                                    <h3>{account.snapshot.label}</h3>
                                    <span>
                                        {account.state === "connected"
                                            ? "Paper access verified"
                                            : account.state ===
                                                "reconnect_required"
                                              ? "Reconnect required"
                                              : "Disconnected"}
                                    </span>
                                </div>
                                <dl className="account-balances">
                                    <div>
                                        <dt>Cash (USD)</dt>
                                        <dd>{account.snapshot.cash}</dd>
                                    </div>
                                    <div>
                                        <dt>Equity (USD)</dt>
                                        <dd>{account.snapshot.equity}</dd>
                                    </div>
                                </dl>
                                <p className="muted">
                                    Last checked{" "}
                                    {new Date(
                                        account.snapshot.checked_at,
                                    ).toLocaleString()}
                                    . Saved snapshot; refresh to check current
                                    access and balances.
                                </p>
                                <div className="account-actions">
                                    {account.state === "connected" && (
                                        <button
                                            disabled={
                                                busy ||
                                                !data.connection_available
                                            }
                                            onClick={() =>
                                                void refresh(account)
                                            }
                                        >
                                            Refresh {account.snapshot.label}
                                        </button>
                                    )}
                                    {account.state !== "disconnected" && (
                                        <button
                                            className="secondary"
                                            disabled={busy}
                                            onClick={() =>
                                                setDisconnecting(account)
                                            }
                                        >
                                            Disconnect {account.snapshot.label}
                                        </button>
                                    )}
                                </div>
                            </li>
                        ))}
                    </ul>
                    {data.connection_available && (
                        <div className="connect-form">
                            <label className="checkbox">
                                <input
                                    type="checkbox"
                                    checked={consent}
                                    onChange={(event) =>
                                        setConsent(event.target.checked)
                                    }
                                />
                                I want to connect my Alpaca paper account.
                            </label>
                            <p className="help">
                                Alpaca will ask for paper trading permission.
                                Connecting does not approve a strategy or place
                                an order. Execution is unavailable in this
                                development release. No real-money account is
                                supported.
                            </p>
                            <button
                                disabled={busy || !consent}
                                onClick={() => void connect()}
                            >
                                Continue to Alpaca paper consent
                            </button>
                        </div>
                    )}
                </>
            )}
            <dialog
                ref={dialog}
                onCancel={() => setDisconnecting(null)}
                aria-labelledby="disconnect-title"
            >
                <h2 id="disconnect-title">
                    Disconnect {disconnecting?.snapshot.label}?
                </h2>
                <p>
                    This removes the saved access token and invalidates pending
                    connections in this workspace. It does not cancel open
                    orders, close positions, or revoke access at Alpaca.
                </p>
                <p>
                    Manage existing broker orders and provider app access
                    directly in Alpaca. Reconnecting will require fresh consent.
                </p>
                <div className="account-actions">
                    <button
                        className="secondary"
                        disabled={busy}
                        onClick={() => {
                            dialog.current?.close();
                            setDisconnecting(null);
                        }}
                    >
                        Keep connection
                    </button>
                    <button disabled={busy} onClick={() => void disconnect()}>
                        Disconnect account
                    </button>
                </div>
            </dialog>
        </section>
    );
}
