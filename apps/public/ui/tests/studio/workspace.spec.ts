import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
const enter = async (page: any) => {
    await page.goto("/studio.html");
    await page
        .getByRole("button", { name: "Explore the demo", exact: true })
        .click();
};
test("entry, conversations, portfolio and credential-free settings stay connected", async ({
    page,
}) => {
    const calls: string[] = [];
    page.on("request", (r) => {
        if (["fetch", "xhr"].includes(r.resourceType())) calls.push(r.url());
    });
    await enter(page);
    await expect(
        page.getByRole("heading", {
            name: "What would you like to understand?",
        }),
    ).toBeVisible();
    await page
        .getByRole("button", { name: "Understand my portfolio", exact: true })
        .click();
    await expect(page.getByText("Reviewing sample holdings")).toBeVisible();
    await expect(page.getByText(/Your sample portfolio totals/)).toBeVisible();
    await page.getByRole("textbox", { name: "Message" }).fill("keep my draft");
    await page
        .getByRole("button", { name: "New conversation", exact: true })
        .click();
    await page
        .getByRole("button", {
            name: "A clearer picture of my portfolio",
            exact: true,
        })
        .click();
    await expect(page.getByRole("textbox", { name: "Message" })).toHaveValue(
        "keep my draft",
    );
    await page.getByRole("button", { name: "Settings", exact: true }).click();
    await expect(page.getByLabel("API key")).toBeDisabled();
    await page.getByRole("button", { name: "Use demo connection" }).click();
    await expect(page.getByText("Demo connection ready")).toBeVisible();
    expect(calls).toEqual([]);
});
test("management completes policy approval and activation without per-revision confirmation", async ({
    page,
}) => {
    await enter(page);
    await page.getByRole("button", { name: "Strategies", exact: true }).click();
    await page.getByRole("button", { name: "Configure mode" }).click();
    await page
        .getByLabel("Operating mode")
        .selectOption("Automatic Management");
    await page
        .getByLabel("I authorize this mode for this strategy and demo account.")
        .check();
    await page.getByRole("button", { name: "Save authority" }).click();
    await expect(
        page.getByText(/automatically approved and activated in simulation/),
    ).toBeVisible();
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await page
        .getByRole("button", { name: "Stop automatic revisions" })
        .click();
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await page
        .getByRole("button", { name: "Pause new strategy orders" })
        .click();
    await expect(page.getByText("Paused v2", { exact: true })).toBeVisible();
});
test("eligibility cannot be bypassed through onboarding but demo exploration is available", async ({
    page,
}) => {
    await page.goto("/studio.html");
    await page
        .getByRole("button", { name: "Get started", exact: true })
        .click();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("18 or older");
    await page.getByLabel("I am 18 or older.").check();
    await page.getByLabel("I reside in the United States.").check();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByLabel("Start with simulated cash").check();
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await page.getByRole("button", { name: "Open my workspace" }).click();
    await page.getByRole("button", { name: "Portfolio", exact: true }).click();
    await expect(
        page.getByText("$10,000.00", { exact: true }).first(),
    ).toBeVisible();
});
for (const width of [1440, 768, 390])
    test(`accessible responsive workspace at ${width}`, async ({ page }) => {
        await page.setViewportSize({ width, height: 1000 });
        await enter(page);
        for (const route of [
            "Home",
            "Portfolio",
            "Strategies",
            "Activity",
            "Settings",
            "Conversations",
        ]) {
            await page
                .getByRole("button", { name: route, exact: true })
                .click();
            expect(
                (await new AxeBuilder({ page }).analyze()).violations,
            ).toEqual([]);
            expect(
                await page.evaluate(
                    () => document.documentElement.scrollWidth <= innerWidth,
                ),
            ).toBe(true);
            await page.screenshot({
                path: `/tmp/aqa-${route.toLowerCase()}-${width}.png`,
                fullPage: true,
            });
        }
    });
