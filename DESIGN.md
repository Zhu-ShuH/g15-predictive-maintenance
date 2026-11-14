---
name: G15 Console
description: Dark, evidence-first device health and report console for the G15 simulation sandbox.
colors:
  ink: "#e1eaf7"
  muted: "#afbed0"
  ground: "#0e1726"
  surface: "#142236"
  line: "#2c405c"
  nav: "#101e32"
  nav-muted: "#adbed6"
  brand: "#8cb0ff"
  brand-soft: "#263e64"
  healthy: "#75d2b1"
  healthy-soft: "#173b32"
  yellow: "#f0c271"
  yellow-soft: "#453823"
  red: "#f199a3"
  red-soft: "#442930"
  focus: "#91b6ff"
  field-focus: "#86aaff"
  nav-text: "#ccdaeb"
  nav-bright: "#f2f6ff"
  nav-hover: "#203a5d"
  nav-current: "#2b4772"
  control-border: "#506b90"
  control-text: "#d3e0f2"
  control-hover: "#1d3253"
  control-hover-border: "#6c8ab5"
  field-ground: "#111e31"
  device-hover: "#1d304b"
  device-selected: "#213a5d"
  device-selected-border: "#5278aa"
  unknown-ground: "#283a51"
  unknown-text: "#afc1d9"
  plot-line: "#91b2f4"
  plot-grid: "#30445f"
  plot-label: "#a5b6ca"
  report-ground: "#203249"
  report-passed: "#8edcb8"
  report-passed-ground: "#193a33"
  report-failed: "#f2a1a6"
  report-unknown: "#b4c4d9"
  report-unknown-ground: "#213249"
typography:
  headline:
    fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: "27px"
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: "-0.025em"
  title:
    fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: "18px"
    fontWeight: 650
    lineHeight: 1.45
  subtitle:
    fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: "16px"
    fontWeight: 650
    lineHeight: 1.5
  body:
    fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: "14px"
  label:
    fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: "11px"
  metric:
    fontFamily: '"Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: "38px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.035em"
  identifier:
    fontFamily: 'ui-monospace, "Cascadia Code", Consolas, monospace'
    fontSize: "11px"
rounded:
  badge: "5px"
  control: "7px"
  item: "8px"
  mobile-panel: "10px"
  panel: "12px"
spacing:
  control-block: "8px"
  control-inline: "13px"
  compact-gap: "7px"
  standard-gap: "16px"
  workspace-gap: "22px"
  workspace-inset: "25px"
  page-inline: "32px"
components:
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.control-text}"
    rounded: "{rounded.control}"
    padding: "8px 13px"
  button-secondary-hover:
    backgroundColor: "{colors.control-hover}"
  button-quiet:
    backgroundColor: "transparent"
    textColor: "{colors.brand}"
    rounded: "{rounded.control}"
    padding: "8px 13px"
  button-quiet-hover:
    backgroundColor: "{colors.brand-soft}"
  search-field:
    backgroundColor: "{colors.field-ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 10px"
    height: "36px"
  nav-item:
    textColor: "{colors.nav-text}"
    rounded: "{rounded.item}"
    padding: "13px 17px"
  nav-item-hover:
    backgroundColor: "{colors.nav-hover}"
    textColor: "{colors.nav-bright}"
  nav-item-current:
    backgroundColor: "{colors.nav-current}"
    textColor: "{colors.nav-bright}"
  status-badge:
    backgroundColor: "{colors.unknown-ground}"
    textColor: "{colors.unknown-text}"
    rounded: "{rounded.badge}"
    padding: "4px 8px"
  status-healthy:
    backgroundColor: "{colors.healthy-soft}"
    textColor: "{colors.healthy}"
  status-yellow:
    backgroundColor: "{colors.yellow-soft}"
    textColor: "{colors.yellow}"
  status-red:
    backgroundColor: "{colors.red-soft}"
    textColor: "{colors.red}"
  device-row:
    rounded: "{rounded.item}"
    padding: "14px 11px"
  device-row-hover:
    backgroundColor: "{colors.device-hover}"
  device-row-selected:
    backgroundColor: "{colors.device-selected}"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
  report-verdict:
    backgroundColor: "{colors.report-ground}"
    rounded: "{rounded.item}"
    padding: "20px 22px"
  report-passed:
    backgroundColor: "{colors.report-passed-ground}"
    textColor: "{colors.report-passed}"
  report-failed:
    backgroundColor: "{colors.red-soft}"
    textColor: "{colors.report-failed}"
  report-unverifiable:
    backgroundColor: "{colors.report-unknown-ground}"
    textColor: "{colors.report-unknown}"
---

# Design System: G15 Console

## Overview

**Creative North Star: "University Laboratory Exhibition Panels"**

深墨色工作面、深蓝导航与可核查的图表构成这套控制台。视觉语气安静、明确，系统字体承担操作阅读，数字通过对齐和层级突出；颜色用于选中、方向和有文字的状态。此文记录已经实现的视觉系统，方向来自已选候选 7（`seed:e51c490a6e88c8c1`）。

这是一套用于 G15 教学沙盘的界面。实际查询与生成报告决定内容，视觉层保持未知、失败和未判定可辨。该设计记录不构成生产环境、真实工业诊断或 Gate0 的验收结论。实现为原生 HTML、CSS 和 ES modules；没有获批的栅格设计稿，也没有随界面交付的栅格资产。

**Key Characteristics:**

- 深墨背景、深蓝导航、浅蓝选择与曲线。
- 扁平分区、细线分隔、紧凑但有明确分组的记录。
- 青绿、琥珀与柔红状态始终配文字。
- 系统无衬线用于操作；等宽仅用于运行标识、规则和采样位置等数据。
- 图表检查落在实际存储窗口，数据范围与来源保持可见。

## Colors

前置 token 是颜色的规范值，取自 `src/g15/console/static/app.css`；不增加新的语义色。状态派生色与交互色保留实现中的细微差异。

### Primary

- **Light blue / 浅蓝**：`brand` 用于链接、轻按钮、检查读数和范围输入；`brand-soft` 为轻按钮悬停底色。
- **Orientation blue / 导向蓝**：`nav`、`nav-hover`、`nav-current` 区分导航底、悬停与当前页。`device-selected` 和对应边框突出当前设备。
- **Plot blue / 曲线蓝**：`plot-line` 仅承担健康曲线的连续读数；曲线不随活动告警级别变色。

### Secondary

- **Teal / 青绿**：`healthy` 与 `healthy-soft` 表示无活动告警；已生成报告的通过状态使用独立的 `report-passed` 配对。
- **Amber / 琥珀**：`yellow` 与 `yellow-soft` 表示黄色告警。
- **Soft red / 柔红**：`red` 与 `red-soft` 表示红色告警或读取错误；报告未通过保留其专用文字色。

### Neutral

- **Ink ground / 深墨底**：`ground` 为页面底，`surface` 为工作面、顶栏和轮廓按钮底。
- **Pale ink / 浅墨文字**：`ink` 为主要文字，`muted` 为辅助说明；导航与控制拥有前置 token 中各自的文字色。
- **Blue slate / 蓝灰分隔**：`line` 分隔容器与章节；图表使用独立的 `plot-grid`、`plot-label`。
- **Unknown slate / 未知蓝灰**：`unknown-ground`、`unknown-text` 和报告未判定配对，不借用通过状态。
- **Focus blue / 焦点蓝**：`focus` 用于全局键盘轮廓，`field-focus` 用于搜索框的焦点容器。

**The Labeled State Rule.** 状态色必须与明确的状态文字共同出现；连接提示、活动告警、历史告警和报告结论各自保留名称。

## Typography

**Body Font:** Segoe UI → Microsoft YaHei UI → PingFang SC → sans-serif。字体来自本机系统，无运行时字体下载。

**Label/Mono Font:** ui-monospace → Cascadia Code → Consolas → monospace，仅用于运行 ID、规则、采样区间和序号等数据。设备名称及健康分使用无衬线。

### Hierarchy

- **Headline**：前置 `headline` 用于页面标题；窄屏降至（24px）。
- **Title**：前置 `title` 为二级标题；设备标题在桌面采用（21px），设备列表标题采用（14px）。
- **Subtitle**：前置 `subtitle` 为章节标题；窄屏采用（14px）。
- **Body**：根字号为前置 `body`；实际正文、按钮与解释多在（11–13px），不把所有文字放大为标题。
- **Label**：前置 `label` 为字段说明与辅助数据；辅助说明采用明确的行高（1.65–1.9）。
- **Metric**：前置 `metric` 为最新窗口健康分；在中屏和窄屏分别采用（34px）和（33px）。报告指标采用（21px），窄屏采用（19px）。

读数、计数、表格和时间使用 `tabular-nums`；不是等宽字体替代。图表刻度保持实际屏幕字号（10px），由测量后的 SVG 视口绘制。

**The Data Type Rule.** 保留数值、单位、标识与解释的字形层级。未知不替换为零；检查某个历史窗口不改写标题处的最新窗口健康分。

## Layout

桌面为左侧导航（204px）与主工作区；导航固定在滚动视口顶部，高度为整个视口。主区域最大宽度（1700px），两侧内边距（32px）。设备目录与详情形成双列：目录（232px）、间隙（22px）、详情填满剩余宽度。详情通过共享标题与分隔线组织内容，不将每个数值包成单独卡片。

响应断点来自实际 CSS，按层叠应用：

| 条件 | 实际布局行为 |
| --- | --- |
| 宽度至少 1540px | 目录扩至 252px，列间隙 26px，详情左右内边距 31px，图表高 270px。 |
| 宽度不超过 1180px | 导航缩至 178px，页面左右 24px，目录 200px、列间隙 16px；范围滑块独占一行，报告指标由三列变两列。 |
| 宽度不超过 900px | 左导航变成顶部横向导航，目录仍为 203px 的左列；设备标题与运行信息上下排列，图表高 220px。 |
| 宽度不超过 650px | 页面左右 16px，导航按钮三等分；设备目录在详情上方，设备项横向滚动，每项 164px；详情左右 17px，图表高 205px。 |

窄屏目录提供横向滑动提示，选择或尺寸改变时让已选设备进入可视范围；普通图表检查不会覆盖用户主动横向浏览的位置。告警表在窄屏重排为有字段标签的记录，保留级别、仿真位置、规则、北京时间与可展开原因；报告的逐事件表保留横向滚动。桌面表格保持列对齐。长规则、原因与采样值允许换行，运行 ID 在紧凑标题区以省略号收束，完整值仍以实际标识参与数据选择。

间距沿用已实现的组件值：控件内边距、目录项间隙、面板内边距和列间隙分别在前置 token 中记录，不要求所有测量都服从单一倍数。

## Elevation & Depth

界面没有投影、玻璃模糊或渐变。页面底、导航底、工作面、选中面以色调分层；边框与横向分隔线负责结构。悬停改变底色或边框，不抬升组件。焦点轮廓是交互反馈，不作装饰光晕。

**The Flat Surface Rule.** 新增同类组件沿用色调与分隔线的层级，保持静态布局稳定。

导航、按钮和目录项的状态过渡为（140–150ms ease）；没有周期性图表运动。系统请求减少动态时关闭动画、过渡与平滑滚动。

## Shapes

主面板使用前置 `panel` 圆角，窄屏为 `mobile-panel`；控件、导航/设备项、状态徽标分别使用前置圆角。薄边框承担轮廓，状态前的小圆点仅作为文字的补充。行内图标采用无填充 SVG、圆端和圆连接；常规图标（18px），按钮图标（15px）。品牌为内联线条图形，不依赖外部图片。

## Components

### Buttons

轮廓按钮负责刷新、继续加载与报告下载；轻按钮负责趋势范围切换和 JSON 下载。共用高度至少（36px）、字重（550）、字号（12px）与前置 padding，图表内紧凑按钮至少（32px）。轮廓按钮使用细边框，悬停同时改变背景和边框；轻按钮悬停使用柔蓝底。禁用时透明度（0.5），同时阻止交互。链接下载仅在报告就绪后获得 URL。

全局键盘焦点为轮廓（3px）、外偏移（3px）；保留跳到主要内容链接，不以透明度替代焦点。

### Inputs / Fields

搜索框以深色底、细边框和搜索图标组成，输入高（36px）；聚焦容器为轮廓（2px）、外偏移（1px）。所有输入保留可访问名称。告警级别使用原生 select，错误解释保留在可恢复消息中。窄屏隐藏设备搜索框，告警搜索保留并铺满一行。

### Navigation

导航为图标加完整文字的原生按钮，当前页使用 `aria-current="page"`。深蓝选中底明显区别于悬停底；顶部导航在窄屏均分。设备选择是独立按钮组，用 `aria-pressed` 表达当前选择，不把设备状态与选中状态混为一体。

### Chips / Status badges

徽标以标签、同色圆点和低亮底组成，正文不使用纯颜色编码。目录中的徽标更紧凑、背景透明。活动状态来自已返回的规则状态：红色优先于黄色；有状态且全部为 normal/null 时显示“无活动告警”，其余为未知。不要由健康分在前端推导告警阈值。

### Cards / Containers

目录和详情是共享工作面的两个容器，保持细边框与扁平圆角。详情内部用分隔线组织最新窗口、曲线和最近告警；报告指标用定义列表与横线形成数据网格。加载、空结果、认证不可用和部分失败显示实际文案，保留重试或刷新入口。

### Inspectable health plot

健康图使用实际存储窗口，纵轴固定（0–100），标记（0、50、100）；横轴在采样率一致且有效时换算为仿真相对秒，否则显示窗口起始序号。鼠标指针和可键盘操作的原生范围滑块均选择最近的真实窗口，读数包含区间、健康分与仿真位置。选择点和虚线检查线不改变最新健康分。

竖向虚线标记已读取范围内的历史告警，并配红/黄图例。只绘制已成功读取的窗口，不补造数据；分页未耗尽时明确显示“部分范围”。窗口与告警各用自己的 cursor，单流分页不追加另一流重放的记录。全程读取受到已有页数/窗口数保护，未完成时保留部分范围说明。

### Alarm records

桌面表格保留级别、仿真位置、规则、北京时间和原因五列。窄屏按记录重排，并给数据加可见字段名。原因使用原生 details/summary 展开，完整原因允许任意长词换行。筛选与搜索仅作用于已读取的历史记录；匹配数、已读取总数和仍有更早记录分别呈现。最近告警展示最近三条已读取记录。

### Report verdict and evidence

报告面板从同一运行的已生成 summary 显示通过、未通过或未判定，保留其独立的色调。指标、原因、逐事件结果和下载不在浏览器重算。`passed === true`、`passed === false` 与其他值分别显示对应结论；检出率为 null 且无受控故障事件时显示不适用，有事件但未计算时保留未计算。全程最小健康分与最新窗口健康分分别标注；负匹配时延不解释为提前预测。场景通过不表示 Gate0 或真实工业能力验收通过。

## Do's and Don'ts

### Do:

- **Do** 延用深墨工作面与浅蓝选择，使当前设备和当前页面易于识别。
- **Do** 让状态色与文字共同表达含义，并分开标明活动状态、历史告警和报告结论。
- **Do** 保留实际数据范围、单位、来源与未知值，使图表和报告可核查。
- **Do** 在窄屏保留已选设备可见性，并用有字段标签的告警记录保障完整阅读。
- **Do** 使用原生按钮、输入、select、details 和可见键盘焦点。

### Don't:

- **Don't** 把仿真结果、场景通过或接口可读取写成真实设备联网、生产就绪或 Gate0 通过。
- **Don't** 把未知、未判定、null 或失败画成通过、零或满分。
- **Don't** 从健康分另造告警阈值，或用历史告警代替当前活动状态。
- **Don't** 为图表添加模拟实时跳动、补造缺失窗口或省略部分范围提示。
- **Don't** 用栅格装饰、外部字体、投影或玻璃效果改变这套已实现的扁平系统。
