import { defineConfig } from "@playwright/test";
export default defineConfig({
    testDir: "./tests/studio",
    workers: 1,
    use: {
        baseURL: "http://127.0.0.1:5178",
        headless: true,
        channel: "chrome",
    },
    reporter: "list",
});
