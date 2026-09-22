import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { STORAGE_KEY } from "../../src/studio/persistence";
const enter = async (page: Page) => {
    await page.goto("/studio.html");
    await page
        .getByRole("button", { name: "Explore the demo", exact: true })
        .click();
};
const nav = async (page: Page, name: string) =>
    page.getByRole("button", { name, exact: true }).click();
const mode = async (page: Page, name: string) => {
    await nav(page, "Configure mode");
    await page.getByLabel("Operating mode").selectOption(name);
    if (name !== "Collaborative")
        await page
            .getByLabel(
                "I authorize this mode for this strategy and demo account.",
            )
            .check();
    await nav(page, "Save authority");
};
const read = async (page: Page) =>
    page.evaluate((key) => JSON.parse(localStorage.getItem(key)!), STORAGE_KEY);

test("review is version-specific; editing invalidates evidence and leaves deployment unchanged", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Strategies");
    await nav(page, "Try passing revision");
    await expect(
        page.getByRole("button", { name: "Review simulated activation" }),
    ).toBeEnabled();
    await nav(page, "Review simulated activation");
    await nav(page, "Approve and activate simulation");
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await nav(page, "Edit strategy");
    await page.getByLabel("Maximum position (%)").fill("101");
    await nav(page, "Save new version");
    await expect(page.getByRole("alert")).toContainText("numeric limits");
    await page.getByLabel("Maximum position (%)").fill("10");
    await nav(page, "Save new version");
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await nav(page, "Evidence");
    await expect(page.getByText("This version has no evidence")).toBeVisible();
    await nav(page, "Version history");
    await page.getByRole("button", { name: /Version 2/ }).click();
    await nav(page, "Evidence");
    await expect(page.getByText("The demonstrated checks pass.")).toBeVisible();
    const s = await read(page);
    expect(s.strategies[0].versions).toHaveLength(3);
    expect(s.strategies[0].versions[2].report).toBeUndefined();
    expect(s.strategies[0].deployment.versionId).toBe(
        s.strategies[0].versions[1].id,
    );
});
test("new strategy and conversation input render safely, support rename/delete and export", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Strategies");
    await nav(page, "New strategy");
    await page.keyboard.press("Escape");
    await expect(
        page.getByRole("button", { name: "New strategy", exact: true }),
    ).toBeFocused();
    await nav(page, "New strategy");
    await expect(
        page.getByRole("button", { name: "Create strategy" }),
    ).toBeDisabled();
    await page.getByLabel("Strategy name").fill("A calmer portfolio");
    await nav(page, "Create strategy");
    await nav(page, "Open conversation ↗");
    await page
        .getByLabel("Message", { exact: true })
        .fill('<script>alert("test")</script>');
    await nav(page, "Send ↑");
    await expect(page.getByText(/cannot generate a real answer/)).toBeVisible();
    await expect(page.locator(".messages script")).toHaveCount(0);
    await nav(page, "Rename");
    await page.getByLabel("Conversation name").fill("A thoughtful start");
    await nav(page, "Save name");
    await expect(
        page.getByRole("heading", { name: "A thoughtful start" }),
    ).toBeVisible();
    await nav(page, "Delete");
    await expect(page.getByRole("dialog")).toContainText("messages and draft");
    await nav(page, "Delete local conversation");
    await nav(page, "Strategies");
    await expect(
        page.getByRole("button", { name: "A calmer portfolio", exact: true }),
    ).toBeVisible();
    await nav(page, "Settings");
    const download = page.waitForEvent("download");
    await nav(page, "Export demo strategies");
    expect((await download).suggestedFilename()).toBe(
        "aqa-demo-strategies.json",
    );
});
test("failed checks block; out-of-scope revision escalates without changing authority", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Strategies");
    await mode(page, "Automatic Management");
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await nav(page, "Try failing revision");
    await expect(
        page.getByText("Automatic Management · blocked"),
    ).toBeVisible();
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await nav(page, "Evidence");
    await expect(
        page.getByText("A required check did not pass."),
    ).toBeVisible();
    await nav(page, "Strategy");
    await nav(page, "Try out-of-scope revision");
    await expect(
        page.getByText("Automatic Management · needs approval"),
    ).toBeVisible();
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    const s = await read(page);
    expect(s.strategies[0].authority.capital).toBe(2500);
    expect(s.events.some((e: any) => e.action === "Policy rejection")).toBe(
        true,
    );
    expect(s.events.some((e: any) => e.action === "Policy escalation")).toBe(
        true,
    );
});
test("research proposes without activation; revoking while pending prevents activation", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Strategies");
    await mode(page, "Automatic Research");
    await expect(
        page.getByText("Automatic Research · needs approval"),
    ).toBeVisible();
    expect((await read(page)).strategies[0].deployment).toBeUndefined();
    await mode(page, "Automatic Management");
    await nav(page, "Stop automatic revisions");
    await page.waitForTimeout(1900);
    expect((await read(page)).strategies[0].deployment).toBeUndefined();
    await expect(page.getByText("Collaborative · unreviewed")).toBeVisible();
    await nav(page, "Activity");
    await expect(
        page.getByRole("heading", { name: "Test canceled" }),
    ).toBeVisible();
});
test("cancellation and failure retry preserve evidence boundaries", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Strategies");
    await nav(page, "Try passing revision");
    await nav(page, "Cancel");
    await page.waitForTimeout(1900);
    expect((await read(page)).strategies[0].versions[1].report).toBeUndefined();
    await page.getByText("Test recovery controls").click();
    await nav(page, "Simulate evaluation error");
    await expect(page.getByRole("alert")).toContainText("evaluation failed");
    await nav(page, "Retry evaluation");
    await expect(
        page.getByRole("button", { name: "Review simulated activation" }),
    ).toBeEnabled();
    await nav(page, "Try passing revision");
    await nav(page, "Portfolio");
    await page.waitForTimeout(1900);
    const s = await read(page);
    expect(s.strategies[0].versions.at(-1).report).toBeUndefined();
    expect(s.events.some((e: any) => e.action === "Test failed")).toBe(true);
});
test("portfolio preview does not apply early and manual input validates", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Portfolio");
    await nav(page, "Preview an allocation change");
    await page.getByLabel("Target allocation (%)").fill("101");
    await nav(page, "Calculate sample change");
    await expect(page.getByRole("alert")).toContainText("0 to 100");
    await page.getByLabel("Target allocation (%)").fill("10");
    await nav(page, "Calculate sample change");
    await expect(page.getByRole("dialog")).toContainText("$5,500.00");
    expect((await read(page)).portfolio.cash).toBe(2500);
    await nav(page, "Apply to simulated portfolio");
    expect((await read(page)).portfolio.cash).toBe(8000);
    await nav(page, "Build a sample portfolio");
    await page.getByLabel("Quantity", { exact: true }).fill("-1");
    await nav(page, "Add or replace holding");
    await expect(page.getByRole("alert")).toContainText("greater than zero");
    await page.getByLabel("Quantity", { exact: true }).fill("5");
    await page.getByLabel("Sample price ($)").fill("100");
    await nav(page, "Add or replace holding");
    await page.getByLabel("Sample cash ($)").fill("1000");
    await nav(page, "Replace simulated portfolio");
    const p = (await read(page)).portfolio;
    expect(p.cash).toBe(1000);
    expect(p.holdings.find((h: any) => h.symbol === "VTI").quantity).toBe(5);
    expect(p.source).toBe("manual");
});
test("pause does not liquidate; closing positions is a separate confirmation", async ({
    page,
}) => {
    await enter(page);
    await nav(page, "Strategies");
    await mode(page, "Automatic Management");
    await expect(page.getByText("Running v2", { exact: true })).toBeVisible();
    await nav(page, "Pause new strategy orders");
    expect((await read(page)).portfolio.holdings).toHaveLength(4);
    await nav(page, "Close simulated positions");
    await page.keyboard.press("Escape");
    expect((await read(page)).portfolio.holdings).toHaveLength(4);
    await nav(page, "Close simulated positions");
    await nav(page, "Confirm close simulated positions");
    const s = await read(page);
    expect(s.portfolio.holdings).toEqual([]);
    expect(s.portfolio.cash).toBe(25000);
    expect(s.strategies[0].deployment.paused).toBe(true);
});
test("persistence malformed recovery reset and unrelated storage preservation", async ({
    page,
}) => {
    await page.goto("/studio.html");
    await page.evaluate((key) => {
        localStorage.setItem("unrelated-test", "keep");
        localStorage.setItem(key, '{"schema":2,"strategies":[{}]}');
    }, STORAGE_KEY);
    await page.reload();
    await expect(page.getByRole("alert")).toContainText("could not be read");
    await nav(page, "Reset unreadable demo");
    await nav(page, "Explore the demo");
    await nav(page, "Settings");
    await nav(page, "Use demo connection");
    await page.reload();
    await nav(page, "Settings");
    await expect(page.getByText("Demo connection ready")).toBeVisible();
    await nav(page, "Reset demo data");
    await page.keyboard.press("Escape");
    await expect(page.getByText("Demo connection ready")).toBeVisible();
    await nav(page, "Reset demo data");
    await nav(page, "Confirm reset demo");
    await expect(
        page.getByRole("button", { name: "Get started", exact: true }),
    ).toBeVisible();
    expect(
        await page.evaluate(() => localStorage.getItem("unrelated-test")),
    ).toBe("keep");
});
test("dialogs and artifacts support keyboard focus, responsive layout, and accessibility", async ({
    page,
}) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await enter(page);
    await nav(page, "Conversations");
    await page.getByRole("button", { name: "Open portfolio ↗" }).click();
    await expect(
        page.getByRole("complementary", { name: "Conversation artifact" }),
    ).toBeVisible();
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
        path: "/tmp/aqa-artifact-390.png",
        fullPage: true,
    });
    await nav(page, "Close artifact");
    await nav(page, "Strategies");
    await nav(page, "Configure mode");
    await page
        .getByLabel("Operating mode")
        .selectOption("Automatic Management");
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.getByRole("button", { name: "Cancel", exact: true }).focus();
    await page.keyboard.press("Tab");
    await expect(
        page.getByRole("button", { name: "Close dialog" }),
    ).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(
        page.getByRole("button", { name: "Configure mode" }),
    ).toBeFocused();
    await nav(page, "Try passing revision");
    await expect(
        page.getByRole("button", { name: "Review simulated activation" }),
    ).toBeEnabled();
    await nav(page, "Evidence");
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    expect(
        await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
        ),
    ).toBe(true);
    await page.screenshot({ path: "/tmp/aqa-report-390.png", fullPage: true });
});
