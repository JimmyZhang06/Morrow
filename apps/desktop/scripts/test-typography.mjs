import { _electron as electron } from "@playwright/test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const evidence = resolve("../../.data/release-evidence/typography");
await mkdir(evidence, { recursive: true });
for (const scale of [1, 1.5, 2]) {
  const directory = await mkdtemp(join(tmpdir(), "morrow-type-test-"));
  const app = await electron.launch({ executablePath: resolve("release/win-unpacked/Morrow.exe"),
    args: [`--user-data-dir=${directory}`, `--force-device-scale-factor=${scale}`], timeout: 180000 });
  try {
    const page = await app.firstWindow({ timeout: 180000 });
    await page.getByPlaceholder("写下一句话、一个感受，或刚刚发生的片段……").waitFor({ timeout: 60000 });
    for (const width of [1380, 980]) {
      await app.evaluate(({ BrowserWindow }, size) => BrowserWindow.getAllWindows()[0].setContentSize(size, 760), width);
      for (const name of ["今天", "记录", "认识", "尝试", "主线", "设置与隐私"]) {
        const button = name === "设置与隐私" ? page.getByRole("button", { name, exact: true })
          : page.getByRole("navigation", { name: "主导航" }).getByRole("button", { name, exact: true });
        await button.click();
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const result = await page.evaluate(() => {
          const visible = el => {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
          };
          const small = [...document.querySelectorAll("body *")].filter(el =>
            visible(el) && [...el.childNodes].some(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim()) &&
            parseFloat(getComputedStyle(el).fontSize) < 13.9).map(el => ({ tag: el.tagName, class: el.className, size: getComputedStyle(el).fontSize }));
          const clippedButtons = [...document.querySelectorAll(".primary-nav button, .settings-actions button")]
            .filter(el => visible(el) && el.scrollHeight > el.clientHeight + 2).map(el => el.textContent);
          return { small, clippedButtons, overflow: document.documentElement.scrollWidth > innerWidth,
            inputSize: document.querySelector(".settings-field input") ? getComputedStyle(document.querySelector(".settings-field input")).fontSize : null };
        });
        assert.deepEqual(result.small, [], `${name}, width ${width}, DPI ${scale}: tiny text`);
        assert.deepEqual(result.clippedButtons, [], `${name}, width ${width}, DPI ${scale}: clipped buttons`);
        assert.equal(result.overflow, false, `${name}: horizontal page overflow`);
        if (result.inputSize) assert.equal(result.inputSize, "16px");
        if (name === "今天" || name === "设置与隐私") {
          await page.screenshot({ path: join(evidence, `${scale}-${width}-${name}.png`) });
        }
      }
    }
    console.log(`Typography: DPI ${scale}, regular/compact windows, all six pages readable without clipped navigation or action buttons`);
  } finally { await app.close(); }
}
