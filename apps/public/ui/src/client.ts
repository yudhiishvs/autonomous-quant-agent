export class RequestError extends Error {
    constructor(
        public status: number,
        message: string,
    ) {
        super(message);
    }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
    const csrf =
        document.cookie
            .split("; ")
            .find(
                (value) =>
                    value.startsWith("aqa_csrf=") ||
                    value.startsWith("__Host-aqa_csrf="),
            )
            ?.split("=")[1] ?? "";
    const response = await fetch(path, {
        ...init,
        credentials: "same-origin",
        signal: AbortSignal.timeout(15_000),
        headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf,
            ...init.headers,
        },
    });
    const data = await response.json();
    if (!response.ok)
        throw new RequestError(
            response.status,
            data.detail ?? "Request failed. Please try again.",
        );
    return data as T;
}
