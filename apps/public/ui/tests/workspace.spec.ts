import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { localOnly, signIn } from "./helpers";

test("two verified identities save isolated versions; CSRF and guessed IDs fail", async ({
    browser,
}) => {
    const first = await browser.newContext();
    const second = await browser.newContext();
    await localOnly(first);
    await localOnly(second);
    try {
        const a = await first.newPage();
        const b = await second.newPage();
        const errors: string[] = [];
        a.on("pageerror", (error) => errors.push(error.message));
        b.on("pageerror", (error) => errors.push(error.message));
        await signIn(a, "fixture-one");
        await signIn(b, "fixture-two");
        const name = "Browser isolation " + Date.now();
        await a.getByLabel("Name", { exact: true }).fill(name);
        await a.getByLabel("Fast window (minutes)").fill("20");
        await a.getByRole("button", { name: "Save version" }).click();
        await expect(a.getByRole("alert")).toHaveText(
            "Fast window must be shorter than slow window.",
        );
        await a.getByLabel("Fast window (minutes)").fill("5");
        const saved = a.waitForResponse(
            (response) =>
                response.url().endsWith("/api/v1/strategy-versions") &&
                response.request().method() === "POST",
        );
        await a.getByRole("button", { name: "Save version" }).click();
        const response = await saved;
        expect(response.status()).toBe(201);
        const version = await response.json();
        await expect(a.getByRole("status")).toContainText(
            "not approved or running",
        );
        await a.reload();
        await expect(
            a.getByRole("button", { name: new RegExp(name) }),
        ).toBeVisible();
        await b.reload();
        await expect(
            b.getByRole("button", { name: new RegExp(name) }),
        ).toHaveCount(0);
        const guessed = await second.request.get(
            "http://127.0.0.1:5178/api/v1/strategy-versions/" + version.id,
        );
        expect(guessed.status()).toBe(404);
        const csrf = await first.request.post(
            "http://127.0.0.1:5178/api/v1/strategy-versions",
            {
                headers: { Origin: "http://127.0.0.1:5178" },
                data: { name, definition: version.definition },
            },
        );
        expect(csrf.status()).toBe(403);
        await a.getByRole("button", { name: "Sign out" }).click();
        await expect(a.getByRole("status")).toHaveText("Signed out.");
        expect(
            (
                await first.request.get("http://127.0.0.1:5178/api/v1/me")
            ).status(),
        ).toBe(401);
        await b.getByRole("button", { name: "Sign out" }).click();
        expect(errors).toEqual([]);
    } finally {
        await first.close();
        await second.close();
    }
});

test("retry after a lost save response returns one durable version", async ({
    page,
    context,
}) => {
    await localOnly(context);
    await signIn(page, "fixture-two");
    const name = "Lost response " + Date.now();
    await page.getByLabel("Name", { exact: true }).fill(name);
    let savedId = "";
    let attempts = 0;
    const requestIds: string[] = [];
    await page.route("**/api/v1/strategy-versions", async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        requestIds.push(route.request().postDataJSON().request_id);
        attempts++;
        if (attempts === 1) {
            const response = await route.fetch();
            expect(response.status()).toBe(201);
            savedId = (await response.json()).id;
            return route.abort("failed");
        }
        return route.continue();
    });
    await page.getByRole("button", { name: "Save version" }).click();
    await expect(page.getByRole("alert")).toBeVisible();
    await page.getByRole("button", { name: "Save version" }).click();
    await expect(page.getByRole("status")).toContainText(
        "not approved or running",
    );
    expect(requestIds).toHaveLength(2);
    expect(requestIds[0]).toBe(requestIds[1]);
    const response = await context.request.get(
        "http://127.0.0.1:5178/api/v1/strategy-versions",
    );
    const matching = (await response.json()).filter(
        (version: { name: string }) => version.name === name,
    );
    expect(matching).toHaveLength(1);
    expect(matching[0].id).toBe(savedId);
    await page.reload();
    await expect(
        page.getByRole("button", { name: new RegExp(name) }),
    ).toHaveCount(1);
    await page.getByRole("button", { name: "Sign out" }).click();
});

for (const width of [390, 768, 1440]) {
    test(`workspace keyboard, overflow and accessibility at ${width}px`, async ({
        page,
        context,
    }) => {
        await localOnly(context);
        await page.setViewportSize({ width, height: 900 });
        await signIn(page, "fixture-one");
        await page.keyboard.press("Tab");
        await expect(
            page.getByRole("link", { name: "Skip to content" }),
        ).toBeFocused();
        expect(
            await page.evaluate(
                () => document.documentElement.scrollWidth <= innerWidth,
            ),
        ).toBe(true);
        const audit = await new AxeBuilder({ page })
            .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
            .analyze();
        expect(
            audit.violations.map((v) => ({
                id: v.id,
                impact: v.impact,
                nodes: v.nodes.map((n) => ({
                    target: n.target,
                    summary: n.failureSummary,
                })),
            })),
        ).toEqual([]);
        await page.screenshot({
            path: `test-results/workspace-${width}.png`,
            fullPage: true,
        });
        await page.getByRole("button", { name: "Sign out" }).click();
    });
}
