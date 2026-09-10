import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";
import pngToIco from "png-to-ico";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(root, "resources", "morrow-icon-source.png");
const pngPath = path.join(root, "resources", "icon.png");
const icoPath = path.join(root, "resources", "icon.ico");

await sharp(sourcePath).resize(512, 512).png().toFile(pngPath);
await fs.writeFile(icoPath, await pngToIco(pngPath));
await fs.mkdir(path.join(root, "public"), { recursive: true });
await sharp(sourcePath).resize(64, 64).png().toFile(path.join(root, "public", "morrow-icon.png"));
console.log(`Generated ${path.relative(root, pngPath)} and ${path.relative(root, icoPath)}`);
