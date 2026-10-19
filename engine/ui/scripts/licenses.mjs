// Writes public/THIRD-PARTY-LICENSES.txt: the licence of every package bundled into
// the built app (everything in package-lock.json that isn't dev-only). Runs before build.
import { readFileSync, readdirSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const lock = JSON.parse(readFileSync("package-lock.json", "utf8"));
const out = ["Third-party software bundled in this web app", "=".repeat(46), ""];
for (const [path, info] of Object.entries(lock.packages).sort()) {
  if (!path || info.dev || info.devOptional) continue;
  const name = path.replace(/^.*node_modules\//, "");
  const dir = path;
  const file = existsSync(dir) ? readdirSync(dir).find((f) => /^(licen[cs]e|copying)/i.test(f)) : null;
  out.push(`${name} ${info.version} (${info.license || "see licence"})`, "-".repeat(60));
  out.push(file ? readFileSync(join(dir, file), "utf8").trim() : `Licence: ${info.license || "unknown"}`, "", "");
}
writeFileSync("public/THIRD-PARTY-LICENSES.txt", out.join("\n"));
console.log(`licences: ${out.filter((l) => l.startsWith("-".repeat(60))).length} packages`);
