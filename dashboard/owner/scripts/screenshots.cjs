// Screenshots of the owner page and /evidence, and a check that every element rendered with payload data.
//   node scripts/screenshots.cjs http://127.0.0.1:8080 ../../eval/demo/screenshots
const { chromium } = require("playwright");
const path = require("node:path");

const base = process.argv[2] ?? "http://127.0.0.1:8080";
const out = path.resolve(process.argv[3] ?? "../../eval/demo/screenshots");

(async () => {
  const browser = await chromium.launch();
  const checks = [];
  for (const [name, vp] of [["laptop", { width: 1280, height: 900 }], ["phone", { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport: vp, deviceScaleFactor: 2 });
    const api = [];
    page.on("response", (r) => { if (r.url().includes("/api/")) api.push(`${r.status()} ${r.url()}`); });
    await page.goto(`${base}/`, { waitUntil: "networkidle" });
    await page.waitForSelector("#drivers svg");
    const got = await page.evaluate(() => ({
      balance: document.querySelector(".balance")?.textContent,
      risks: [...document.querySelectorAll(".risk-num")].map((e) => e.textContent),
      runway: document.querySelector(".runway")?.textContent,
      drivers_sentence: document.querySelector(".headline-text")?.textContent,
      fan_paths: document.querySelectorAll("#outlook svg path").length,
      risk_paths: document.querySelectorAll("#risk svg path").length,
      driver_bars: document.querySelectorAll("#drivers svg rect").length,
      plan_steps: document.querySelectorAll("#plan li").length,
      what_ifs: document.querySelectorAll("#what-ifs button").length,
      owed_rows: document.querySelectorAll("#owed li").length,
      source: document.querySelector(".foot")?.textContent,
    }));
    checks.push({ name, api, ...got });
    await page.screenshot({ path: `${out}/owner-${name}.png`, fullPage: true });
    if (name === "laptop") {
      await page.click("#what-ifs button >> nth=0");
      await page.waitForTimeout(300);
      const overlay = await page.evaluate(() => ({
        breaking: document.querySelector(".breaking")?.textContent,
        fan_paths: document.querySelectorAll("#outlook svg path").length,
      }));
      checks.push({ name: "what-if selected", ...overlay });
      await page.screenshot({ path: `${out}/owner-${name}-what-if.png`, fullPage: true });
      await page.click("#what-ifs button >> nth=0");
      await page.waitForTimeout(300);
      checks.push({ name: "what-if cleared", fan_paths: await page.evaluate(() => document.querySelectorAll("#outlook svg path").length) });
    }
    await page.goto(`${base}/evidence/`, { waitUntil: "networkidle" });
    await page.waitForSelector(".chart svg");
    checks.push({ name: `evidence ${name}`, lines: await page.evaluate(() => document.querySelectorAll(".chart svg path").length),
      sentences: await page.evaluate(() => [...document.querySelectorAll(".evidence-text")].map((e) => e.textContent)) });
    await page.screenshot({ path: `${out}/evidence-${name}.png`, fullPage: true });
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify(checks, null, 1));
})();
