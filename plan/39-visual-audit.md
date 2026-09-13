# Morrow 视觉检查与改版

检查覆盖今天、记录、对话、认识、尝试、主线和设置七个主页面，以及记录详情、聊天内容与引用、回信、初始空状态、模型未配置/已配置状态。使用独立测试数据，检查常规窗口、980×660 小窗口和 150% 放大对话流程。

## 主要发现与已完成调整

| 原先的问题 | 调整 |
| --- | --- |
| 标题字体、字号、按钮和留白在页面间变化，层级不稳定 | 统一界面标题、主按钮、侧栏选中态和页面边距；日记与信件保留阅读字体 |
| 首页重复介绍「保留原话、依据、用户判断」，同时反复提示配置 AI | 合并为简短初始提示，移除与写记录无关的配置横幅 |
| 记录页的大搜索面板在页面标题上方，且与列表搜索争夺注意力 | 移到列表工具区；历史搜索及索引维护合为可展开入口 |
| 对话设置同时有自动检索卡片、手动查找、手选日记，卡片层层堆叠 | 自动查找变为轻量选项；查找与手选合并成「选择参考日记」；减少介绍区高度 |
| 第一次聊天没有历史，却显示重复的新建按钮和空历史栏 | 首次聊天收起空历史栏；已有会话后恢复列表 |
| 模型设置未创建配置时重复显示新增提示、空列表、编辑标题 | 首次直接显示表单；有配置后再显示模型库；字段成组排版 |
| 设置页的说明、密码、服务和排错工具持续占据主界面 | 接口说明、备份恢复、连接与诊断按用途收起；授权选项仍明确展示 |
| 主线和回忆录尚无内容时出现两块重复空状态 | 合并为一个起步区域，保留两类生成操作 |
| 空状态图标有多重装饰边框，文字区域暴露 ETag 等实现词 | 去掉多余边框，修订说明改用日常语言 |

## 图片的使用

使用内置 imagegen 生成一张植物光影纸面素材，仅用于首页欢迎区域。文字所在左侧保留大面积低对比留白，正文、搜索、表单与聊天不铺背景图片。图标仍沿用现有 Lucide 系统。

素材：[morrow-light-v1.png](C:/Users/Zhang/Desktop/开发项目/自我认知/apps/desktop/src/assets/morrow-light-v1.png)

生成方式：内置 image_gen（未使用 CLI/API Key 回退）。原始生成图保留，项目使用独立副本。

最终生成提示词：

> Create a premium understated decorative background asset for a calm Chinese personal journaling desktop app called Morrow. Landscape composition, 1536x1024. Warm ivory matte paper surface (#faf9f5), very soft diffuse late afternoon window light, subtle organic shadows cast by a small out-of-frame olive branch, a few airy sage green translucent leaves confined to the far right and upper right 35% of image. The entire left 65% and central area must be almost empty ivory negative space for UI text, with absolutely no dark shapes there. Editorial botanical photography meets delicate handmade paper, refined and quiet, not sentimental, no people, no objects, no vases, no books, no text, no lettering, no logos, no borders. Very low contrast with naturally fading edges into warm ivory, no strong gradients or spotlight. This will be a header background, never a full-page wallpaper. Generate the bitmap image asset only.

## 实际界面

[新版首页](C:/Users/Zhang/Desktop/开发项目/自我认知/.data/release-evidence/visual-audit/after/今天.png) · [记录页](C:/Users/Zhang/Desktop/开发项目/自我认知/.data/release-evidence/visual-audit/after/记录.png) · [对话页](C:/Users/Zhang/Desktop/开发项目/自我认知/.data/release-evidence/visual-audit/after/对话.png) · [设置页](C:/Users/Zhang/Desktop/开发项目/自我认知/.data/release-evidence/visual-audit/after/设置-top.png) · [小窗口首页](C:/Users/Zhang/Desktop/开发项目/自我认知/.data/release-evidence/visual-audit/after/今天-compact.png)

全部改版前后截图位于 `.data/release-evidence/visual-audit/before` 与 `after`。截图为测试数据，不是用户记录。

## 验证

TypeScript 检查与 Vite 构建通过。真实桌面对话流程通过，覆盖选取日记、认识纠正、引用、来源跳转、删除、回信和 150% 布局。主要页面与小窗口截图已检查，无窗口横向溢出。最终 EXE 的本地搜索（开启、补建、搜索、撤回及 150% 布局）、模型配置（新增、切换、删除）、加密备份/恢复、数据保存、容量异常保护和重启验证均通过。

发布包：[Morrow.exe](C:/Users/Zhang/Desktop/开发项目/自我认知/apps/desktop/release-visual-refresh/win-unpacked/Morrow.exe)
