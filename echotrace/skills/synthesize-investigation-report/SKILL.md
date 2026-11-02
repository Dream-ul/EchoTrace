---
name: synthesize-investigation-report
description: 根据已冻结的 NodeTrace 和指定调查类型 Markdown 模板生成中文 ReportDocument。
roles: [report_composer]
---

# 生成调查报告

只接收已冻结的 NodeTrace、调查类型对应的 Markdown 模板和模板要求的数据。模板文件规定章节顺序、章节目的、节点映射、推荐 block、数据集采集规格和可用图表；代码契约规定 ReportDocument JSON 的结构与引用规则。章节上下文中的 `template_datasets` 是应用依据模板规格从冻结节点确定性整理的只读数据，不得重算或改写其中的数值、样本范围、限制和引用。

## 工作规则

章节生成时，`recommended_blocks` 仅用于提示常见表达方式，不是白名单。报告 agent 可以从 `ReportBlock` 支持的类型中按本章语义自主选择；所有图表、表格、时间线和媒体仍必须绑定冻结 Trace 中真实存在的数据集或媒体，不能补造数据。

1. 先读取模板，按模板规划章节，再逐章生成结构化 block。
2. `chapter.report_context` 是全篇叙事约束：先理解报告总摘要、全部章节重点及前后章职责，再写当前章。按时间、主体或问题递进；前章已经说明的事实只在必要时简短承接，不整段复述；不把当前章写成彼此无关的材料摘录。
3. 当前章节的 `subheadings` 来自调查类型 Markdown 模板，是固定的小节目录。必须按给定顺序逐一输出 `heading` block，标题和 `level` 原样保留，不增加、删除、合并或改写标题。每个小节标题之后应紧跟该小节的分析、证据、表格或图表；材料不足时仍保留该小节，并按模板的 `empty_data_behavior` 说明边界。标题不自行添加数字，渲染器会生成 `1`、`1.1`、`1.1.1` 层级编号。
4. 只综合输入中已有的节点结论、主张、证据、媒体、指标和数据集，不搜索、不调用工具、不补造事实。
5. 事实 block 在 `citations` 数组中引用实际存在的 claim、evidence、document 或 media ID；图表和表格在 `dataset_ids` 中引用已有数据集。稳定 ID 只用于结构化引用字段，不得写进任何面向读者的文字。
6. `recommended_blocks` 只是模板建议。根据现有数据的比较关系自主判断是否使用 `metric_grid`、`timeline`、`chart` 或 `table`；图表必须引用真实数据集并给出对应 VisualizationSpec。时间变化用折线/面积，对比用柱状，构成占比用饼/环，相关性用散点，同一对象的多维度用雷达。数据不具备可比性时用有信息量的文字说明，不估算或拼接不相容数值。
7. 情感、立场和主题图必须说明分析样本是什么。由来源全文计算的信号只描述被引用的公开文本，不能表述成社交平台评论倾向或总体民意；只有实际评论样本能支持对评论的结论。应用没有生成某个模板数据集时，不得自行补数据或伪造“零值”。
8. 所有面向读者的标题、段落、表格列名、图表标题、图注和来源标签使用简体中文。URL 只出现在来源链接字段；原文引用可作为 evidence card 内容。
9. `paragraph.text` 必须是自然的中文段落。严禁把 Python 字典、JSON 对象、字段名和值拼接、序列化的数组、`time:...; event:...`、`name/unit/value/metric_id` 记录写入段落。任何结构化内容必须放在对应 block 的 `items`、`rows` 或数据集内。
10. 表格不得展示 `node_id`、`claim_id`、`evidence_id`、`metric_id`、`dataset_id`、`citation_ids`、`source_document_id` 等内部字段；应使用中文语义列名。来源引用由渲染层映射为可读来源标题和链接。
11. 格式由语义决定：连续叙事使用 `paragraph`；并列理由、发现、影响、行动或结果使用 `bullet_list`；存在明确执行顺序或阶段顺序时使用 `ordered_list`。检查“第一、第二、第三”“其一、其二”等内容，按完整语义提取为列表项，不把编号和解释挤在一个长段落中。避免连续把每个段落框成卡片。
11. 格式由语义决定：连续叙事使用 `paragraph`；并列理由、发现、影响、行动或结果使用 `bullet_list`；存在明确执行顺序或阶段顺序时使用 `ordered_list`。检查“第一、第二、第三”“其一、其二”等内容，按完整语义提取为列表项，不把编号和解释挤在一个长段落中。避免连续把每个段落框成卡片。

## 业务输出与 Agent 协议

报告使用两个阶段的业务 JSON。`phase=plan` 返回 `report_plan`，包含中文标题、
副标题、摘要和按模板顺序排列的章节重点；`phase=section` 只返回当前章节的
`chapter_id`、`summary`、`blocks` 以及本章实际使用的指标、数据集、图表和媒体。
代码会把所有章节统一装订为符合 `ReportDocument` 的 JSON，语言固定为 `zh-CN`。
章节顺序必须与模板一致，所有 block 类型、数据集引用、媒体引用和来源引用都必须
通过代码契约校验。

顶层核心字段为 `report_id`、`run_id`、`trace_id`、`investigation_type`、
`template_id`、`template_version`、`template_content_hash`、`title`、`subtitle`、
`summary`、`toc` 和 `chapters`；
可选字段为 `source_index`、`language`、`metrics`、`datasets` 和
`visualizations`。每个章节包含 `chapter_id`、`title`、连续的 `order`、
`purpose`、`summary` 和 `blocks`。`heading` block 使用 `payload.level`（3 对应 `1.1`，4 对应 `1.1.1`）及 `payload.text`；其标题序列必须与模板 `subheadings` 一致。每个 block 包含 `type`、`payload`、
`citations` 和 `dataset_ids`；图表、表格、指标网格和时间线 block 必须
引用已输入的数据集。`type` 只能使用项目 `ReportBlock` 契约支持的类型。
报告 ID、调查 ID、trace ID、模板 ID 和版本必须取自输入上下文，不得编造。

报告可以丰富，但不能为丰富而复制整份 Trace。围绕模板章节目的挑选关键
主张、证据卡、节点媒体和真实指标；相同事实只在最适合的位置完整呈现，
其他位置可交叉引用。数据不足时保留有信息量的文字分析，不制造统计值。

该 JSON 不是 AgentDecision。JSON 模式下，将当前阶段 JSON 序列化后放入最终
`AgentDecision.answer`；Native 模式下只调用
`agent_control(action="final", answer="<当前阶段 JSON 字符串>")`。不要把业务 JSON
直接写入普通 assistant content，不要输出 Markdown 围栏或额外说明。
