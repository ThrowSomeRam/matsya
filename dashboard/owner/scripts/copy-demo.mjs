// Copy the precomputed demo payloads (eval/demo) into public/demo, the fallback when the API is down.
import { copyFileSync, existsSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const src = resolve(here, "../../../eval/demo");
const dst = resolve(here, "../public/demo");
mkdirSync(dst, { recursive: true });
for (const f of ["owner_view.json", "naive_vs_ours.json", "obligations.json"]) {
  if (existsSync(resolve(src, f))) copyFileSync(resolve(src, f), resolve(dst, f));
  else console.warn(`missing ${f} in eval/demo: the screen needs the API for it`);
}
