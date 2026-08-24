import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";
import pngToIco from "png-to-ico";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const svgPath = path.join(root, "resources", "icon.svg");
const pngPath = path.join(root, "resources", "icon.png");
const icoPath = path.join(root, "resources", "icon.ico");

await sharp(svgPath).resize(512, 512).png().toFile(pngPath);
await fs.writeFile(icoPath, await pngToIco(pngPath));
console.log(`Generated ${path.relative(root, pngPath)} and ${path.relative(root, icoPath)}`);
