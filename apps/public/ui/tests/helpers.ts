import { expect, type BrowserContext, type Page } from "@playwright/test";

export async function localOnly(context: BrowserContext) {
    await context.route("**/*", (route) => {
        const url = new URL(route.request().url());
        if (
            url.hostname !== "127.0.0.1" ||
            !["5178", "8188"].includes(url.port)
        )
            return route.abort();
        return route.continue();
    });
}

export async function signIn(page: Page, identity: string) {
    await page.goto("/");
    await page
        .getByRole("link", { name: "Sign in or create an account" })
        .click();
    await page
        .getByLabel("Email", { exact: true })
        .fill(identity + "@example.invalid");
    await page
        .getByLabel("Password", { exact: true })
        .fill("SYNTHETIC-LOCAL-PASSWORD-ONLY");
    await page.getByRole("button", { name: "Sign In", exact: true }).click();
    await expect(
        page.getByRole("heading", { name: "Your strategy versions" }),
    ).toBeVisible();
}
