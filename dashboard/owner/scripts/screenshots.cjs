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
    await page.waitForSelector("#outlook svg");
    const got = await page.evaluate(() => ({
      status: document.querySelector(".status-label")?.textContent,
      headline: document.querySelector(".headline-text")?.textContent,
      tasks: document.querySelectorAll("#todo > ol li").length,
      optional: document.querySelectorAll("#todo details li").length,
      buyers: document.querySelectorAll("#owes > ul > li").length,
      more_buyers: document.querySelectorAll("#owes details li").length,
      chart_paths: document.querySelectorAll("#outlook svg path").length,
      ready: [...document.querySelectorAll("#ready li")].map((e) => e.textContent),
      foot: document.querySelector(".foot")?.textContent,
    }));
    checks.push({ name, api, ...got });
    await page.screenshot({ path: `${out}/owner-${name}.png`, fullPage: true });
    if (name === "laptop") {
      await page.evaluate(() => document.querySelectorAll("details").forEach((d) => { d.open = true; }));
      await page.waitForTimeout(200);
      await page.screenshot({ path: `${out}/owner-${name}-expanded.png`, fullPage: true });
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
