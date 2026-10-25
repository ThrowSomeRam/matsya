// Screenshots of the owner dashboard and /evidence, and a check that every element rendered with payload data.
//   node scripts/screenshots.cjs http://127.0.0.1:8080 ../../eval/demo/screenshots
const { chromium } = require("playwright");
const path = require("node:path");

const base = process.argv[2] ?? "http://127.0.0.1:8080";
const out = path.resolve(process.argv[3] ?? "../../eval/demo/screenshots");

(async () => {
  const browser = await chromium.launch();
  const checks = [];
  for (const [name, vp] of [["laptop", { width: 1440, height: 900 }], ["phone", { width: 390, height: 844 }]]) {
    const page = await browser.newPage({ viewport: vp, deviceScaleFactor: 2 });
    const api = [];
    page.on("response", (r) => { if (r.url().includes("/api/")) api.push(`${r.status()} ${r.url()}`); });
    await page.goto(`${base}/`, { waitUntil: "networkidle" });
    await page.waitForSelector(".chart-card svg");
    const got = await page.evaluate(() => ({
      kpis: [...document.querySelectorAll(".kpi")].map((e) => `${e.querySelector(".kpi-label")?.textContent}: ${e.querySelector(".kpi-value")?.textContent}`),
      badge: document.querySelector(".badge")?.textContent,
      tasks: document.querySelectorAll(".todo-card > ol .task").length,
      optional: document.querySelectorAll(".todo-card details .task").length,
      buyers: document.querySelectorAll(".table-card > table tbody tr").length,
      more_buyers: document.querySelectorAll(".table-card details tbody tr").length,
      chart_paths: document.querySelectorAll(".chart-card svg path").length,
      ready: [...document.querySelectorAll(".ready li")].map((e) => e.textContent),
    }));
    checks.push({ name, api, ...got });
    await page.screenshot({ path: `${out}/owner-${name}.png`, fullPage: true });
    if (name === "laptop") {
      await page.click(".chip >> nth=1");
      await page.evaluate(() => {
        const s = document.querySelector(".slider");
        s.value = "60";
        s.dispatchEvent(new Event("input", { bubbles: true }));
      });
      await page.waitForTimeout(200);
      checks.push({ name: "simulation", scenario: await page.textContent(".chip[aria-pressed=true]"),
        date: await page.textContent(".sim-date"),
        kpis: await page.$$eval(".kpi", (e) => e.map((x) => `${x.querySelector(".kpi-label")?.textContent}: ${x.querySelector(".kpi-value")?.textContent}`)),
        planNote: await page.textContent(".todo-card .scenario-note") });
      await page.screenshot({ path: `${out}/owner-${name}-what-if.png`, fullPage: true });
      await page.click(".chip >> nth=0");
      await page.evaluate(() => document.querySelectorAll("details").forEach((d) => { d.open = true; }));
      await page.waitForTimeout(200);
      await page.screenshot({ path: `${out}/owner-${name}-expanded.png`, fullPage: true });
    }
    await page.goto(`${base}/evidence/`, { waitUntil: "networkidle" });
    await page.waitForSelector(".chart svg");
    checks.push({ name: `evidence ${name}`, lines: await page.evaluate(() => document.querySelectorAll(".chart svg path").length) });
    await page.screenshot({ path: `${out}/evidence-${name}.png`, fullPage: true });
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify(checks, null, 1));
})();
