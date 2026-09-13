# 水墨风界面

配色调整为宣纸白、墨灰、淡赭，移除大面积青绿色。生成真实水墨山水底图，作为全窗口固定背景和首页画面；正文采用浅色纸面覆盖，保留清晰阅读层次。页面切换仍不使用整页淡入。

生成方式：内置 image_gen 工具。没有使用 CLI 或用户的模型密钥。

项目底图：`apps/desktop/src/assets/ink-landscape-v1.png`。原始生成图已复制进项目，通过 Vite 一起打包，不依赖临时目录或在线图片。

最终提示词：

```
Use case: stylized-concept. Asset type: actual full-window background artwork for a Chinese personal diary and reflective chat desktop application, NOT a UI mockup. Create a refined Chinese shuimo ink-wash landscape on warm ivory xuan rice paper, wide landscape 16:10 composition. Misty mountains rendered with delicate diluted graphite ink, pale layered distant ridgelines, soft lake reflections, a few restrained dry brush reeds near lower edges. Keep the upper and central 70 percent predominantly open, luminous paper and mist for readable interface overlays. Clearly visible but quiet mountain forms concentrated in the bottom quarter and outer right edge, subtle natural paper fibre throughout. Monochrome warm gray and charcoal, minute pale earthy wash, absolutely no teal, green or saturated colour. Elegantly sparse, contemplative, atmospheric, authentic brush bleeding and water marks, sophisticated contemporary ink painting. No text, calligraphy, stamps, logos, borders, people, buildings, UI panels or controls.
```

样式：`apps/desktop/src/ChineseTheme.css`。

交付目录：`apps/desktop/release-ink-wash/win-unpacked/`。

TypeScript 和 Vite 构建通过。界面检查截图保存在 `.data/release-evidence/ink-theme/`。

桌面验证通过：底图解码加载、会话保留、30 帧稳定切换、刷新保留页面。已目视检查首页和对话页截图。
