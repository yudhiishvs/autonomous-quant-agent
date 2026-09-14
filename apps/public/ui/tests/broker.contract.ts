import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { localOnly, signIn } from "./helpers";

test("paper consent, durable account snapshot and explicit disconnect", async ({
    page,
    context,
}) => {
    await localOnly(context);
    await context.route(
        "https://app.alpaca.markets/oauth/authorize**",
        async (route) => {
            // No request reaches Alpaca: only its documented browser redirect is simulated.
            const request = new URL(route.request().url());
            expect(request.searchParams.get("env")).toBe("paper");
            expect(request.searchParams.get("scope")).toBe("trading");
            const callback = new URL(request.searchParams.get("redirect_uri")!);
            expect(callback.origin).toBe("http://127.0.0.1:5178");
            callback.searchParams.set(
                "state",
                request.searchParams.get("state")!,
            );
            callback.searchParams.set("code", "synthetic-browser-consent");
            await route.fulfill({
                status: 302,
                headers: { location: callback.href },
                body: "",
            });
        },
    );
    await signIn(page, "fixture-one");
    const connect = page.getByRole("button", {
        name: "Continue to Alpaca paper consent",
    });
    await expect(connect).toBeDisabled();
    await page.getByLabel("I want to connect my Alpaca paper account.").check();
    await connect.click();
    await expect(
        page.getByText("Paper access verified", { exact: true }),
    ).toBeVisible();
    await page.reload();
    await expect(
        page.getByText("Paper access verified", { exact: true }),
    ).toBeVisible();
    await expect(
        page.getByText("10000.123456789", { exact: true }),
    ).toHaveCount(2);
    await page.getByRole("button", { name: "Refresh Alpaca · …1234" }).click();
    await expect(
        page.getByText("Paper account snapshot refreshed.", { exact: true }),
    ).toBeVisible();
    for (const width of [390, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth,
            ),
        ).toBe(true);
        expect(
            (
                await new AxeBuilder({ page })
                    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
                    .analyze()
            ).violations,
        ).toEqual([]);
        await page.screenshot({
            path: `test-results/accounts-${width}.png`,
            fullPage: true,
        });
    }
    await page
        .getByRole("button", { name: "Disconnect Alpaca · …1234" })
        .click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("does not cancel open orders");
    expect(
        (
            await new AxeBuilder({ page })
                .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
                .analyze()
        ).violations,
    ).toEqual([]);
    await dialog.getByRole("button", { name: "Keep connection" }).click();
    await expect(
        page.getByText("Paper access verified", { exact: true }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Disconnect Alpaca · …1234" })
        .click();
    await dialog
        .getByRole("button", { name: "Disconnect account", exact: true })
        .click();
    await expect(dialog).not.toBeVisible();
    await expect(page.getByText("Disconnected", { exact: true })).toBeVisible();
    await page.reload();
    await expect(page.getByText("Disconnected", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
});
