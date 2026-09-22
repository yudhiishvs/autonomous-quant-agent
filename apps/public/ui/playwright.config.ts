import { defineConfig } from "@playwright/test";

export default defineConfig({
    testDir: "./tests",
    testIgnore: ["**/studio/**"],
    timeout: 45_000,
    workers: 1,
    retries: 0,
    use: {
        baseURL: "http://127.0.0.1:5178",
        headless: true,
        trace: "off",
        screenshot: "off",
    },
    reporter: "list",
});
