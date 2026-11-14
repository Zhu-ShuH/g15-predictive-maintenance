---
name: G15 Console
description: Cyber Wafuu industrial-rail console with device health, diagnosis and real simulation flow controls.
colors:
  ink: "#FEFFEF"
  muted: "#B9C8D6"
  ground: "#FEFFEF"
  surface: "#28313C"
  component: "#010101"
  line: "#5796D3"
  nav: "#28313C"
  nav-muted: "#B9C8D6"
  brand: "#5796D3"
  brand-soft: "#133E34"
  healthy: "#A5CD2E"
  healthy-soft: "#133E34"
  yellow: "#FF8A00"
  yellow-soft: "#28313C"
  red: "#D32529"
  red-soft: "#28313C"
  focus: "#FF8A00"
  field-focus: "#FF8A00"
  nav-text: "#B9C8D6"
  nav-bright: "#FEFFEF"
  nav-hover: "#133E34"
  nav-current: "#010101"
  control-border: "#80D8CF"
  control-text: "#80D8CF"
  control-hover: "#133E34"
  control-hover-border: "#FF8A00"
  button-primary: "#133E34"
  button-mint: "#80D8CF"
  button-focus: "#FF8A00"
  button-danger: "#FF4400"
  field-ground: "#010101"
  device-hover: "#133E34"
  device-selected: "#133E34"
  device-selected-border: "#A5CD2E"
  unknown-ground: "#28313C"
  unknown-text: "#B9C8D6"
  plot-line: "#5796D3"
  plot-grid: "#28313C"
  plot-label: "#B9C8D6"
  report-ground: "#010101"
  report-passed: "#A5CD2E"
  report-passed-ground: "#133E34"
  report-failed: "#D32529"
  report-unknown: "#B9C8D6"
  report-unknown-ground: "#010101"
  metal-silver: "#B9C1C9"
  metal-gold: "#B89A4A"
  metal-copper: "#A66A43"
  instrument-marker: "#00E1DC"
typography:
  headline:
    fontFamily: '"G15 Display", sans-serif'
    fontSize: "clamp(28px, 2.5vw, 38px)"
    fontWeight: 900
    lineHeight: 1.25
    letterSpacing: "0.015em"
  title:
    fontFamily: '"G15 Instrument", sans-serif'
    fontSize: "18px"
    fontWeight: 700
    lineHeight: 1.45
    letterSpacing: "0.075em"
  subtitle:
    fontFamily: '"G15 Instrument", sans-serif'
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.45
    letterSpacing: "0.075em"
  body:
    fontFamily: '"G15 UI", "Microsoft YaHei UI", sans-serif'
    fontSize: "14px"
  label:
    fontFamily: '"G15 UI", "Microsoft YaHei UI", sans-serif'
    fontSize: "11px"
  metric:
    fontFamily: '"G15 UI", "Microsoft YaHei UI", sans-serif'
    fontSize: "38px"
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.035em"
  identifier:
    fontFamily: '"G15 UI", "Microsoft YaHei UI", sans-serif'
    fontSize: "11px"
rounded:
  badge: "3px"
  control: "0px"
  field: "7px"
  item: "0px"
  mobile-panel: "0px"
  panel: "0px"
  rail: "2px"
spacing:
  control-block: "8px"
  control-inline: "13px"
  compact-gap: "7px"
  standard-gap: "16px"
  workspace-gap: "22px"
  workspace-inset: "25px"
  page-inline: "32px"
  rail-closed-slot: "76px"
  rail-open: "222px"
  rail-mobile-slot: "64px"
components:
  button-secondary:
    backgroundColor: "{colors.button-primary}"
    textColor: "{colors.control-text}"
    rounded: "{rounded.control}"
    padding: "8px 13px"
  button-secondary-hover:
    backgroundColor: "{colors.control-hover}"
  button-quiet:
    backgroundColor: "{colors.component}"
    textColor: "{colors.control-text}"
    rounded: "{rounded.control}"
    padding: "8px 13px"
  button-quiet-hover:
    backgroundColor: "{colors.brand-soft}"
  search-field:
    backgroundColor: "{colors.field-ground}"
    textColor: "{colors.ink}"
    rounded: "{rounded.field}"
    padding: "0 10px"
    height: "36px"
  nav-item:
    textColor: "{colors.nav-text}"
    rounded: "{rounded.item}"
    padding: "10px 11px"
    height: "45px"
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

**Creative North Star: "Cyber Wafuu Industrial Slide Rails"**

象牙网格底承托深青工作面，银、金、铜金属框与铆钉延续既有组件语言。左侧工业滑轨收为图标，悬停展开；用户提供的 G15 原图标识位于左上，流光响应悬停和键盘焦点。厚重标题、仪表黑体组件标题和方棱小字构成三个自托管字形层级。

这是一套用于 G15 教学沙盘的界面。实际查询与生成报告决定内容，视觉层保持未知、失败和未判定可辨。该设计记录不构成生产环境、真实工业诊断或 Gate0 的验收结论。实现为原生 HTML、CSS 和 ES modules；交付用户提供的品牌 PNG 与本地 OFL 字体，参考字形图仅作为风格依据，不声称精确复刻原字体。

**Key Characteristics:**

- 象牙网格底、深青工作面、银金铜金属框和铆钉。
- 窄工业滑轨、悬停展开标签、点击 Logo 固定与恢复折叠。
- 健康、告警、异常与过期状态始终配文字。
- 三层自托管字体；读数和时间以等宽数字对齐。
- 图表检查落在实际存储窗口，数据范围与来源保持可见。

## Colors

前置 token 是颜色的规范值，继承 `cyber-wafuu.css` 并由末层 `industrial.css` 细化导航与字体。状态派生色与交互色保留实现中的细微差异；金属色只表达结构，不替代告警语义。

### Primary

- **Light blue / 浅蓝**：`brand` 用于链接、轻按钮、检查读数和范围输入；`brand-soft` 为轻按钮悬停底色。
- **Orientation blue / 导向蓝**：`nav`、`nav-hover`、`nav-current` 区分导航底、悬停与当前页。`device-selected` 和对应边框突出当前设备。
- **Plot blue / 曲线蓝**：`plot-line` 仅承担健康曲线的连续读数；曲线不随活动告警级别变色。

### Secondary

- **Lime / 黄绿**：`healthy` 与 `healthy-soft` 表示无活动告警；已生成报告的通过状态使用独立的 `report-passed` 配对。
- **Amber / 琥珀**：`yellow` 与 `yellow-soft` 表示黄色告警。
- **Alarm red / 告警红**：`red` 与 `red-soft` 表示红色告警；读取失败与报告未通过保留其各自文字和配色。

### Neutral

- **Ivory ground / 象牙底**：`ground` 为网格页面底，`surface` 为深青工作面、顶栏和轮廓按钮底。
- **Metal structure / 金属结构**：`metal-silver`、`metal-gold`、`metal-copper` 分别用于银、金、铜框；`instrument-marker` 用于组件标题的青色短标。
- **Pale ink / 浅墨文字**：`ink` 为主要文字，`muted` 为辅助说明；导航与控制拥有前置 token 中各自的文字色。
- **Blue slate / 蓝灰分隔**：`line` 分隔容器与章节；图表使用独立的 `plot-grid`、`plot-label`。
- **Unknown slate / 未知蓝灰**：`unknown-ground`、`unknown-text` 和报告未判定配对，不借用通过状态。
- **Focus orange / 焦点橙**：`focus` 用于全局键盘轮廓，`field-focus` 用于搜索框的焦点容器；滑轨使用内收的暖橙轮廓以免被窄容器裁掉。

**The Labeled State Rule.** 状态色必须与明确的状态文字共同出现；连接提示、活动告警、历史告警和报告结论各自保留名称。

## Typography

**Display Font:** `G15 Display`，由思源黑体 Heavy 900 派生，用于页面大标题。

**Component Font:** `G15 Instrument`，由思源黑体 Bold 700 派生，用于二、三级组件标题。

**Body / Label Font:** `G15 UI`，由思源宋体 Heavy 派生，用于标签、正文、小字与数字；未覆盖字符回退到 Microsoft YaHei UI / sans-serif。三套 WOFF2 随页面本地提供，采用 OFL 1.1，保留版权与许可；详细来源见 `src/g15/console/static/fonts/SOURCES.md`。使用 `font-display:swap` 与 `font-synthesis:none`，不依赖运行时外网。参考图的定制切角和金属效果由材质、颜色与层级接近表达，字体并非原图同款。

### Hierarchy

- **Headline**：前置 `headline` 用于页面标题；宽屏按视口伸缩，窄屏降至（27px）。标题为深青字与细金色底缘，不把金属材质处理套到正文。
- **Title**：前置 `title` 为二级标题；设备标题在桌面采用（21px），设备列表标题采用（14px）。指定组件标题前附青色短标（4px）。
- **Subtitle**：前置 `subtitle` 为章节标题；窄屏采用（14px）。
- **Body**：根字号为前置 `body`；实际正文、按钮与解释多在（11–13px），不把所有文字放大为标题。
- **Label**：前置 `label` 为字段说明与辅助数据；辅助说明采用明确的行高（1.65–1.9）。
- **Metric**：前置 `metric` 为最新窗口健康分；在中屏和窄屏分别采用（34px）和（33px）。报告指标采用（21px），窄屏采用（19px）。

读数、计数、表格和时间使用 `tabular-nums`；不是等宽字体替代。图表刻度保持实际屏幕字号（10px），由测量后的 SVG 视口绘制。

**The Data Type Rule.** 保留数值、单位、标识与解释的字形层级。未知不替换为零；检查某个历史窗口不改写标题处的最新窗口健康分。

## Layout

桌面左侧轨道可见宽（64px），为主工作区预留（76px）；固定于视口左侧，上下各留（8px）。悬停展开到前置 `rail-open`，默认覆盖内容；宽度至少（1100px）且已固定时，主区让出（238px）。主区域最大宽度（1700px），仿真页为（1800px）。设备目录与详情保留双列，详情通过共享标题与分隔线组织内容，不将每个数值包成单独卡片。

响应断点来自实际 CSS，按层叠应用：

| 条件 | 实际布局行为 |
| --- | --- |
| 宽度至少 1540px | 目录扩至 252px，列间隙 26px，详情左右内边距 31px，图表高 270px。 |
| 宽度不超过 1180px | 保持窄左轨道；页面左右 24px，目录 200px、列间隙 16px；范围滑块独占一行，报告指标由三列变两列。 |
| 宽度不超过 900px | 左轨道继续纵向排列，主区左右 18px；设备标题与运行信息上下排列，图表高 220px；仿真趋势与观测区堆叠。 |
| 宽度不超过 650px | 轨道可见宽 52px、占位 64px，展开仍为 222px；主区左右 12px。设备目录在详情上方横向滚动，图表高 205px；仿真链路以两行呈现，设备表明确支持横向查看。 |

窄屏目录提供横向滑动提示，选择或尺寸改变时让已选设备进入可视范围；普通图表检查不会覆盖用户主动横向浏览的位置。告警表在窄屏重排为有字段标签的记录，保留级别、仿真位置、规则、北京时间与可展开原因；报告的逐事件表保留横向滚动。桌面表格保持列对齐。长规则、原因与采样值允许换行，运行 ID 在紧凑标题区以省略号收束，完整值仍以实际标识参与数据选择。

持续模式的目录在桌面随视口限高并纵向滚动，设备列表最大高度为 `calc(100vh - 260px)`；已选设备在选择或尺寸改变时进入可视范围。窄屏继续使用横向设备条。

间距沿用已实现的组件值：控件内边距、目录项间隙、面板内边距和列间隙分别在前置 token 中记录，不要求所有测量都服从单一倍数。

## Elevation & Depth

界面继承深青毛玻璃工作面、黑色组件和金属框。银金铜边框、铆钉、内侧暗线与局部投影表达装配层次；滑轨横向渐变模拟金属导轨，展开不抬升其他组件。焦点轮廓是交互反馈，不以流光替代可见焦点。

**The Metal Frame Rule.** 新增同类组件继承金属框与内部细分隔；材质不承担业务状态，也不改变数据范围。

导航颜色、按钮和目录项保持短状态过渡；轨道宽度使用（260ms，`cubic-bezier(.16,1,.3,1)`），标签使用淡入与短位移。Logo 在支持悬停的指针停留或键盘焦点时，以原图透明轮廓和亮度作为蒙版，在图案与字形的金属表面循环流动银白／青色高光（2.8s，linear）；图形间隙与底板不参与发光。移开或失焦即停止，隐藏页面暂停。仿真链路只在新鲜的真实计数增长时播放流向线，图表按实际采样更新。减少动态偏好关闭流光、滑轨过渡与流向动画。

## Shapes

主面板与设备组件为直角金属框（4px），滑轨框为银色（3px）与微圆角（2px）；按钮沿用 JRPG 切角。状态前的小点仅作为文字补充。行内图标采用无填充 SVG、圆端和圆连接；常规图标（18px），滑轨图标（20px）。品牌使用用户原图 `g15-logo.png`：折叠时显示左侧标记，展开后呈现完整横向标识。

## Components

### Buttons

轮廓按钮负责刷新、继续加载与报告下载；轻按钮负责趋势范围切换和 JSON 下载。共用高度至少（36px）、字重（650）、字号（12px）与前置 padding，图表内紧凑按钮至少（32px）。按钮使用 JRPG 切角、薄荷细边框和深绿底，轻按钮为黑底；悬停变为深绿与橙边并轻移（-1px），按下回落（2px）。禁用时透明度（0.45），同时阻止交互。链接下载仅在报告就绪后获得 URL。

全局键盘焦点为轮廓（3px）、外偏移（3px）；保留跳到主要内容链接，不以透明度替代焦点。

### Inputs / Fields

搜索框以深色底、细边框和搜索图标组成，输入高（36px）；聚焦容器为轮廓（2px）、外偏移（1px）。所有输入保留可访问名称。告警级别使用原生 select，错误解释保留在可恢复消息中。窄屏隐藏设备搜索框，告警搜索保留并铺满一行。

### Navigation

滑轨默认仅显示图标，悬停或键盘进入时展开到全宽并显示文字标签；离开后自动收起。点击 Logo 固定，再次点击立即恢复折叠；固定偏好存于本机 localStorage。触屏通过点击 Logo 展开并固定。Logo 用原生按钮，提供 `aria-pressed`、`aria-expanded`、`aria-controls`；Escape 可收起未固定滑轨。导航保留可访问名称与 title，当前页使用 `aria-current="page"`。各断点均使用不换列的纵向布局，导航列表可独立滚动，页脚保持末位，避免窄屏图标被旧布局挤出。设备选择仍为独立按钮组，用 `aria-pressed` 表达当前选择，不把设备状态与选中状态混为一体。

### Chips / Status badges

徽标以标签、同色圆点和低亮底组成，正文不使用纯颜色编码。目录中的徽标更紧凑、背景透明。活动状态来自已返回的规则状态：红色优先于黄色；有状态且全部为 normal/null 时显示“无活动告警”，其余为未知。不要由健康分在前端推导告警阈值。

### Cards / Containers

目录和详情是共享工作面的两个容器，保持金属边框与铆钉。详情内部用细分隔线组织最新窗口、曲线和最近告警；报告指标用定义列表与横线形成数据网格。加载、空结果、认证不可用和部分失败显示实际文案，保留重试或刷新入口。

### Inspectable health plot

健康图使用实际存储窗口，纵轴固定（0–100），标记（0、50、100）；横轴在采样率一致且有效时换算为仿真相对秒，否则显示窗口起始序号。鼠标指针和可键盘操作的原生范围滑块均选择最近的真实窗口，读数包含区间、健康分与仿真位置。选择点和虚线检查线不改变最新健康分。

竖向虚线标记已读取范围内的历史告警，并配红/黄图例。只绘制已成功读取的窗口，不补造数据；分页未耗尽时明确显示“部分范围”。窗口与告警各用自己的 cursor，单流分页不追加另一流重放的记录。全程读取受到已有页数/窗口数保护，未完成时保留部分范围说明。

### Alarm records

桌面表格保留级别、仿真位置、规则、北京时间和原因五列。窄屏按记录重排，并给数据加可见字段名。原因使用原生 details/summary 展开，完整原因允许任意长词换行。筛选与搜索仅作用于已读取的历史记录；匹配数、已读取总数和仍有更早记录分别呈现。最近告警展示最近三条已读取记录。

### Report verdict and evidence

报告面板从同一运行的已生成 summary 显示通过、未通过或未判定，保留其独立的色调。指标、原因、逐事件结果和下载不在浏览器重算。`passed === true`、`passed === false` 与其他值分别显示对应结论；检出率为 null 且无受控故障事件时显示不适用，有事件但未计算时保留未计算。全程最小健康分与最新窗口健康分分别标注；负匹配时延不解释为提前预测。场景通过不表示 Gate0 或真实工业能力验收通过。

### Continuous simulation status

持续模式沿用同一套金属工作面、内部细分隔线和有文字的状态徽标。页面顶部显示仿真设备数、采样率、窗口长度及采集样本、已处理样本、已写窗口和待分析批次；设备项保留实际查询得到的健康预览，并补充样本、窗口、积压与最近提交。详情上方的采集计数来自当前持续状态，图表中的保留快照仍按其已读取范围解释。

状态与概览在本轮完成后约（2 秒）继续读取，后台概览并发上限（3）；隐藏页面暂停，销毁时取消请求和计时器。请求超过（6 秒）标记超时；持续状态、概览或设备提交超过（10 秒）未更新，使用中性过期标签。停止、异常、暂停和积压分别写明，不让绿色暗示仍在更新。这些是界面读取/新鲜度策略，不是业务健康阈值。

最新页可跟随已提交窗口。历史分页、全程读取、手动窗口检查和报告不被自动更新重置；有新数据时显示沿用蓝色选中底的提示，用户点击“回到最新”后恢复。展开的告警原因与报告内容不因无关的状态轮询重建。持续摘要标为“持续运行摘要 · 未判定”，没有受控真值对照时不造检出率或通过结论。旧服务的 `/api/live` 返回 404 时不显示持续模块或错误。

### Fleet screen and operations views

大屏 `/screen` 以真实状态分布环、异常优先设备矩阵、所选设备趋势和来源明确的维护建议构成三栏布局；窄屏依次堆叠。运营看板 `/operations` 使用指标卡、同一真实趋势和可搜索设备表。两页只用现有查询数据，状态色始终配文字，未知、过期和读取失败单独计数。页面间使用普通链接，可进入仿真控制页；不增加租户、工单或会诊操作。

诊断工作台 `/diagnosis` 使用双栏分析布局：真实设备选择、当前健康分、规则版本、最新窗口的五维风险与最近证据链。五维固定为电平、强度、波动、冲击和频谱，使用原生 progress 与真实持久化风险绘制；任一维度未知时不绘制完整雷达。历史告警来源与当前状态分别标注，不补造温度/电流工业诊断、SLA 或在线会诊。

### Cyber Wafuu shared theme

页面先继承 `cyber-wafuu.css` 的象牙网格底、深青看板、黑色毛玻璃组件、银金铜框与铆钉，再以 `industrial.css` 作为末层统一字体和滑轨。按钮沿用 JRPG 切角、深绿与薄荷配色，焦点为橙色。小屏保留窄纵向滑轨，主区按内容重排；不得以隐藏内容冒充适配。

### Simulation flow and device controls

`/simulation` 用四段链路呈现仿真采集、分析队列、特征分析、结果入库，计数来自同源 `/api/live`。采集与处理速率由相邻有效读取的累计计数差除以实际时间差计算，保留最近（60 次）速率读数；会话切换、计数倒退、设备集合改变或间隔过长不能连成有效速率。延迟 P95、反压事件和延迟周期保留其实际来源与单位。

单台及全体启停操作仅暂停/恢复当前仿真会话的新增采集，已有批次继续处理，恢复后沿用运行与样本序号。请求期间禁用相关操作；旧会话、过期快照、不可用控制与异常均明确提示。全暂停时仍校验服务心跳，不把持续成功读取的旧快照当作新鲜。设备异常、会话异常、已暂停、排空中和会话结束分别显示，不以状态色代替文字。

## Do's and Don'ts

### Do:

- **Do** 延用象牙网格、金属工作面与窄滑轨，使当前设备和当前页面易于识别。
- **Do** 让状态色与文字共同表达含义，并分开标明活动状态、历史告警和报告结论。
- **Do** 保留实际数据范围、单位、来源与未知值，使图表和报告可核查。
- **Do** 在窄屏保留已选设备可见性，并用有字段标签的告警记录保障完整阅读。
- **Do** 使用原生按钮、输入、select、details 和可见键盘焦点。

### Don't:

- **Don't** 把仿真结果、场景通过或接口可读取写成真实设备联网、生产就绪或 Gate0 通过。
- **Don't** 把未知、未判定、null 或失败画成通过、零或满分。
- **Don't** 从健康分另造告警阈值，或用历史告警代替当前活动状态。
- **Don't** 为图表添加模拟实时跳动、补造缺失窗口或省略部分范围提示。
- **Don't** 替换用户提供的品牌原图，或将相近的自托管字体声称为参考图精确同款。
- **Don't** 把仿真采集启停写成物理设备控制，或用装饰动画暗示没有发生的数据传输。
