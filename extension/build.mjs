// Build: bundle TS entry points and copy static panel assets into dist/.
import { build } from "esbuild";
import { cp, mkdir } from "node:fs/promises";

await mkdir("dist", { recursive: true });
await build({
  entryPoints: { background: "src/background.ts", panel: "src/panel/panel.ts" },
  outdir: "dist",
  bundle: true,
  format: "esm",
  target: "chrome120",
  sourcemap: false,
  minify: false,
});
// Content scripts cannot be ES modules; bundle them as plain IIFEs.
await build({
  entryPoints: { "espn-tap": "src/espn-tap.ts", "espn-bridge": "src/espn-bridge.ts" },
  outdir: "dist",
  bundle: true,
  format: "iife",
  target: "chrome120",
  sourcemap: false,
  minify: false,
});
await cp("src/panel/panel.html", "dist/panel.html");
await cp("src/panel/panel.css", "dist/panel.css");
console.log("built extension -> dist/");
