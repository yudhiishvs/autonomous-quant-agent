import { randomUUID } from "node:crypto";
import {
    test,
    expect,
    type APIRequestContext,
    type Page,
} from "@playwright/test";
import { localOnly } from "./helpers";

async function actionLink(
    request: APIRequestContext,
    email: string,
    subject: string,
) {
    if (!/^lifecycle-[0-9a-f-]+@example\.invalid$/.test(email))
        throw new Error("Synthetic inbox required");
    let id: string | undefined;
    await expect
        .poll(
            async () => {
                const response = await request.get(
                    "http://127.0.0.1:8128/api/v1/search",
                    {
                        params: { query: `to:${email}`, limit: "5" },
                        maxRedirects: 0,
                        timeout: 5000,
                    },
                );
                if (!response.ok()) return false;
                const data = await response.json();
                const message = data.messages.find(
                    (item: { Subject: string; To: { Address: string }[] }) =>
                        item.Subject === subject &&
                        item.To.some(
                            (recipient) => recipient.Address === email,
                        ),
                );
                id = message?.ID;
                return typeof id === "string" && /^[A-Za-z0-9_-]+$/.test(id);
            },
            {
                timeout: 15000,
                message: "Expected email in the synthetic local inbox",
            },
        )
        .toBe(true);
    const response = await request.get(
        `http://127.0.0.1:8128/api/v1/message/${id}`,
        { maxRedirects: 0, timeout: 5000 },
    );
    const message = await response.json();
    if (
        !message.To.some(
            (recipient: { Address: string }) => recipient.Address === email,
        )
    )
        throw new Error("Inbox recipient mismatch");
    const link = String(message.Text).match(
        /http:\/\/127\.0\.0\.1:8188\/realms\/paper\/login-actions\/action-token\?[^\s<>"]+/,
    )?.[0];
    if (!link || link.length > 8192)
        throw new Error("Expected a bounded local identity action link");
    return link;
}

async function followAction(page: Page, link: string) {
    try {
        await page.goto(link);
    } catch {
        throw new Error("Local identity action navigation failed");
    }
}

test("registration verifies email, sets a password and recovers the same workspace", async ({
    page,
    context,
    request,
}) => {
    await localOnly(context);
    const email = `lifecycle-${randomUUID()}@example.invalid`;
    await page.goto("/");
    await page
        .getByRole("link", { name: "Sign in or create an account" })
        .click();
    await page.getByRole("link", { name: "Register", exact: true }).click();
    await page.getByLabel("Email", { exact: false }).fill(email);
    await page.getByLabel("First name", { exact: false }).fill("Synthetic");
    await page.getByLabel("Last name", { exact: false }).fill("Lifecycle");
    await page.getByRole("button", { name: "Register", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Email verification" }),
    ).toBeVisible();
    expect(
        (await context.request.get("http://127.0.0.1:5178/api/v1/me")).status(),
    ).toBe(401);
    const verification = await actionLink(request, email, "Verify email");
    await followAction(page, verification);
    await expect(
        page.getByRole("heading", { name: "Update password" }),
    ).toBeVisible();
    await page
        .getByLabel("New Password", { exact: false })
        .fill("SYNTHETIC-INITIAL-PASSWORD-ONLY");
    await page
        .getByLabel("Confirm password", { exact: false })
        .fill("SYNTHETIC-INITIAL-PASSWORD-ONLY");
    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Your strategy versions" }),
    ).toBeVisible();
    const owner = (
        await (
            await context.request.get("http://127.0.0.1:5178/api/v1/me")
        ).json()
    ).user_id;
    await page
        .getByLabel("Name", { exact: true })
        .fill("Recovery keeps my strategy");
    await page
        .getByRole("button", { name: "Save version", exact: true })
        .click();
    await expect(
        page.getByRole("status", { name: "Workspace status" }),
    ).toContainText("Version saved");
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await page
        .getByRole("link", { name: "Sign in or create an account" })
        .click();
    await page
        .getByRole("link", { name: "Forgot Password?", exact: true })
        .click();
    // Keycloak recognizes its existing SSO session and sends the recovery email directly.
    const recovery = await actionLink(request, email, "Reset password");
    await followAction(page, recovery);
    await page
        .getByLabel("New Password", { exact: false })
        .fill("SYNTHETIC-RECOVERED-PASSWORD-ONLY");
    await page
        .getByLabel("Confirm password", { exact: false })
        .fill("SYNTHETIC-RECOVERED-PASSWORD-ONLY");
    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Your strategy versions" }),
    ).toBeVisible();
    expect(
        (
            await (
                await context.request.get("http://127.0.0.1:5178/api/v1/me")
            ).json()
        ).user_id,
    ).toBe(owner);
    await expect(
        page.getByRole("button", { name: /Recovery keeps my strategy/ }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
    await page
        .getByRole("link", { name: "Sign in or create an account" })
        .click();
    const restart = page.getByRole("button", {
        name: "Restart login",
        exact: true,
    });
    if (await restart.isVisible()) await restart.click();
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page
        .getByLabel("Password", { exact: true })
        .fill("SYNTHETIC-INITIAL-PASSWORD-ONLY");
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
    await expect(
        page.getByText("Invalid username or password.", { exact: true }),
    ).toBeVisible();
    await page
        .getByLabel("Password", { exact: true })
        .fill("SYNTHETIC-RECOVERED-PASSWORD-ONLY");
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Your strategy versions" }),
    ).toBeVisible();
    expect(
        (
            await (
                await context.request.get("http://127.0.0.1:5178/api/v1/me")
            ).json()
        ).user_id,
    ).toBe(owner);
    await page.getByRole("button", { name: "Sign out", exact: true }).click();
});
