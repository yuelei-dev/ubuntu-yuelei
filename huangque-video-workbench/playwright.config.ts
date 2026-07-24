import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
import {defineConfig} from '@playwright/test';

const chromeCandidates = [
  process.env.HUANGQUE_CHROME_PATH,
  process.env.LOCALAPPDATA && resolve(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  process.env.PROGRAMFILES && resolve(process.env.PROGRAMFILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
  process.env['PROGRAMFILES(X86)'] && resolve(process.env['PROGRAMFILES(X86)'], 'Google', 'Chrome', 'Application', 'chrome.exe')
].filter((candidate): candidate is string => Boolean(candidate));
const chromeExecutable = chromeCandidates.find((candidate) => existsSync(candidate));

if (!chromeExecutable) throw new Error('Google Chrome was not found; set HUANGQUE_CHROME_PATH to chrome.exe');

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 150_000,
  expect: {timeout: 120_000},
  workers: 1,
  outputDir: './tests/output/playwright',
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    launchOptions: {executablePath: chromeExecutable},
    trace: 'retain-on-failure'
  },
  webServer: {
    command: 'npm.cmd run dev',
    url: 'http://127.0.0.1:4173/projects/new',
    timeout: 30_000,
    reuseExistingServer: false
  }
});
