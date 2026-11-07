const path = require('node:path');
const { createRequire } = require('node:module');

const repoRoot = path.resolve(process.env.PROJECT_WORKTREE || process.cwd());
const requireFromFrontend = createRequire(path.join(repoRoot, 'frontend', 'package.json'));
const { defineConfig, firefox } = requireFromFrontend('@playwright/test');
const baseURL = process.env.PLAYWRIGHT_BASE_URL;

if (!baseURL) {
  throw new Error('PLAYWRIGHT_BASE_URL must point to the active worktree frontend URL');
}

const executablePath =
  process.env.PROJECT_FIREFOX_EXECUTABLE || firefox.executablePath();

module.exports = defineConfig({
  testDir: path.join(repoRoot, 'frontend', 'tests', 'e2e'),
  timeout: 30000,
  workers: 1,
  reporter: 'line',
  use: {
    baseURL,
    browserName: 'firefox',
    launchOptions: { executablePath },
    storageState: { cookies: [], origins: [] },
    trace: 'off',
    screenshot: 'only-on-failure',
  },
});
