// Remove build output directories (a portable `rm -rf` for the npm scripts).
import { rmSync } from "node:fs";

for (const dir of process.argv.slice(2)) {
  rmSync(new URL(`../${dir}`, import.meta.url), { recursive: true, force: true });
}
