# G15 自托管字体

这三套字体按参考图的三个层级做相近匹配，并非参考图中定制字形的精确复刻。三者均实际随控制台提供，不依赖用户安装字体或运行时外网。

| 用途 | CSS family | 文件 | 原始字体 |
| --- | --- | --- | --- |
| 大标题：厚重、方正的机械面板感 | `G15 Display` | `g15-display.woff2` | Source Han Sans SC Heavy 2.004，900 |
| 组件标题：清晰的仪表黑体 | `G15 Instrument` | `g15-instrument.woff2` | Source Han Sans SC Bold 2.004，700 |
| 标签、小字：方形端点与衬线的方棱风格 | `G15 UI` | `g15-ui.woff2` | Source Han Serif CN Heavy 1.000，700 |

原始字体来自本机已安装文件；具体版本、原始版权、内嵌许可证与源文件 SHA-256，以及输出 SHA-256、大小和覆盖数，见 `font-manifest.json`。仅采用字体自身明确写有 SIL Open Font License 1.1 的版本；没有打包本机商用字库，也没有采用许可范围不明的站酷酷黑/高端黑。

## 原始项目与许可来源

- Adobe Source Han Sans 官方项目：https://github.com/adobe-fonts/source-han-sans
- Adobe Source Han Sans 官方 OFL：https://github.com/adobe-fonts/source-han-sans/blob/release/LICENSE.txt
- Adobe Source Han Serif 官方项目：https://github.com/adobe-fonts/source-han-serif
- Adobe Source Han Serif 官方 OFL：https://github.com/adobe-fonts/source-han-serif/blob/release/LICENSE.txt
- 随附完整许可：`OFL.txt`。原字体版权和许可元数据同时保留在字体中。

来源与许可于 2026-11-14 核实。OFL 允许字体嵌入、修改和随软件分发；字体本身仍受 OFL 约束，不单独出售。

## 派生处理

用 FontTools 4.68.0 / Brotli 1.2.1 转换为 WOFF2；保留原字形轮廓和基本横排连字/字距特性，覆盖 GB2312 全部可用字符（含 6763 常用汉字）、界面 HTML/JS/CSS 中全部已有字符，以及数字、拉丁、标点和全角符号。未打包罕见扩展字与竖排变体，以控制本地加载体积；运行 ID、设备名中没有覆盖的特殊字仍可由后续字体回退显示。

为遵守原始保留名称 `Source`，派生字体的 name 表与 CFF 主名称已统一改为 G15 Display / G15 Instrument / G15 UI；保留原始版权与许可。参考图及本轮界面要求中的中文、`LIVE-001`、`600 秒`、`Token` 已逐字符确认存在。每个输出小于控制台静态文件的 8 MiB 上限。

## 接入建议

使用 `@font-face` 的 `font-display: swap`。三个 family 分别声明 900 / 700 / 700 字重，避免使用浏览器额外伪粗体。大标题可配浅银到青色的细微金属渐变；组件标题维持纯色并辅以青色侧标。密集数据和较长说明需要更平稳的字形时，可使用 G15 Instrument 或系统 sans-serif 作后续回退。

字体参考图包含定制切角与金属表面，这些应由界面的颜色/材质层呈现，而非声称原始字体就是同款。
