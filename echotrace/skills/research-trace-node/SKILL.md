---
name: research-trace-node
description: 根据原始来源研究并填写一个固定 Trace 节点，产出归纳主张、定位证据、节点结论和相关媒体。
roles: [researcher]
---

# 研究一个 Trace 节点

本 Skill 只填写调查模板中的一个节点槽位。模板是节点标题、目标、方面、允许的主张类型、优先来源、研究问题和审稿标准的唯一依据。不得新增节点、改名、调整脉络顺序或把主张移到其它节点。

## 输入边界

运行时会提供以下结构化节点上下文：

- 已选定的 `TraceTemplate` 和 `NodeSpec`；
- 稳定的 `node_id`、`slot_id` 和当前修订号；
- 修订时的上一版 `NodeDraft`；
- 已存在时的上一轮 `NodeReview`；
- 当前修订固定的 `review_focus`，包括审稿方向、目标、rubric 和必需反馈字段；
- `task.report_data_needs`，其中列出分配给当前节点的报告数据集、准确列名、最小样本单位、采集说明和对应图表意图；
- 应用从所提供样本中派生的 `source_analysis_features`，用于描述情感、立场、主题、时间和实体信号；
- 本任务可用的来源记录及准确的工具能力名称。

使用现有来源记录和本任务获准的公开检索工具回答 NodeSpec 的研究问题。来源记录独立存储；输出以 `document_id` 引用来源并保留精确片段。

## 研究步骤

1. 检索前先读 NodeSpec，把每个方面和研究问题转换为少量互补、可核实的事实问题。只能使用列出的 `claim_types`；类型表示主张在本节点中的作用，不表示置信度。
2. 为当前槽位检索最有力的材料。优先使用 NodeSpec 指定的来源类别，必要时增加独立来源或不同立场的材料。分别记录发布时间、事件时间、主体、范围和来源身份。
   对每项 `collection_mode: research` 的 `report_data_needs`，遵循其 `collection_guidance` 检索原始观测，并使用准确的 `purpose` 和 `columns` 组织数据集。`minimum_samples` 是生成有用表格/图表的目标，不是伪造数据行的许可。可用来源不支持所需观测时，省略该数据集，并在节点结论中说明具体边界。每行的 `citation_ids` 必须指向支撑它的证据；不能混用定义、样本单位、时间范围或分母不同的观测。
3. 节点涉及多个主体、行动、时间点或信息方面时，提出多条聚焦且由模型归纳的主张。主张须用有边界的措辞概括或综合来源，不能复制证据片段。一条主张可综合多个文档，一个文档也可支持多条主张。不要把不相关事实合并成宽泛主张，也不要为凑数量而造主张。
4. 每条主张至少绑定一段精确证据。证据须指出来源文档、保留可见原文，并提供可复核定位，例如规范 URL 加章节、段落、时间戳或页码。没有定位片段的 URL 不算证据。
5. `conclusion` 应综合本节点全部主张及证据，直接回答 NodeSpec 目标；在适用时说清不同主体、行动、结果、时间和范围，区分已由材料确认的内容与来源转述或受样本边界限制的内容，不加入证据之外的事实。结论不能只是证据片段的罗列。
6. 只有图片或视频能实质帮助读者理解节点时才添加 `media`。每项媒体须关联来源文档，给出说明和相关性，并且只能提供 `source_url` 或对象存储 `object_key` 其中之一。字段必须严格如下，不能用 `type` 代替 `media_type`：

   ```json
   {
     "media_id": "media-1",
     "media_type": "image",
     "document_id": "doc-1",
     "caption": "材料中图片的内容",
     "why_relevant": "该图片如何帮助理解本节点",
     "source_url": "https://example.org/image.jpg"
   }
   ```

   `media_type` 只能是 `image` 或 `video`。`media_id`、`media_type`、`document_id`、`caption` 和 `why_relevant` 均为必填。`source_url` 与 `object_key` 必须恰好提供一个；没有合适媒体时返回空 `media` 数组。
7. 收到审稿意见后，在保持节点槽位和证据链的前提下逐项处理其中指定的检索和改写任务。遵循 `review_focus`：第 1 轮扩展覆盖面，第 2 轮加强证据，第 3 轮整合主张并重写结论。初稿为 revision `0`，之后三次修订必须依次为 `1`、`2`、`3`。

## 研究进度与停止条件

每轮都应把 `context_pack.research_progress` 当作工作清单：阅读检索结果后、决定是否继续搜索前，检查固定方面、当前审稿任务、已尝试查询、新发现文档和诊断计数。计数不代表研究完整，也不会撤销来源工具权限。

只有当前草稿含有相关且有边界的主张，并由可定位来源片段支持时，才能标记某方面已覆盖。只有审稿任务要求的研究或编辑已经反映在主张、证据或结论中，才能标记任务已完成。不能因为运行过搜索、来源提及了主题或预算已消耗就标记完成。运行时会检查 checkpoint 标签是否对应 NodeSpec 和审稿任务，但不会判断主张实质质量；由 Verifier 负责编辑审稿。

通过 `agent_control` 调用 `research_checkpoint` 控制动作。其 `answer` 是以下结构的 JSON 字符串：

```json
{
  "status": "continue",
  "covered_aspects": ["已由主张和证据覆盖的 NodeSpec 原文方面"],
  "completed_tasks": ["已实际完成的 review task 原文"],
  "next_queries": ["针对尚未解决问题的新检索词"],
  "reason": "仍缺少能够确认处置结果的原始材料"
}
```

选择 `continue` 时，只列出已处理的方面和审稿任务，并为未解决项给出具体、全新的 `next_queries`。运行时会拒绝已经尝试过的查询，并在 checkpoint 后只允许声明的查询；不能只换几个词重复查询。

只有每个必需方面都有证据支持，且每项指定的审稿任务都已反映在草稿中，才可选择 `status: "ready"`。方面和任务必须逐字取自上下文。随后停止检索并提交完整 NodeDraft。若运行时返回 `finalize_only: true`，表示检索已结束且来源工具不可用，立即通过 `agent_control(action="final")` 提交 NodeDraft。`force_finalize: true` 表示运行保护提前结束研究；不得把缺失方面标为已覆盖，也不得将该综合稿说成质量通过，需保留剩余证据边界。主动选择 `ready` 也会进入 `finalize_only`，但 `force_finalize` 为 false。任一状态下非最终动作若被拒绝，就不要再次调用工具或 checkpoint，直接提交 NodeDraft。固定的 Verifier 轮次仍负责编辑审稿。

revision `3` 通常侧重编辑统合；若第三轮审稿或现有草稿暴露出具体事实缺口，仍可使用搜索和来源工具。修改主张、证据关系或结论而现有材料不足时，先重新检索。搜索次数、没有新来源或截止时间提示都不能证明节点已完成。只有每个固定方面和审稿任务都有证据支持时才可 `ready`；供应商限流和任务截止时间是执行保护，触发后运行时会报告失败。

## 节点输出合同

下面的对象是节点的**业务载荷**，不是 AgentDecision 协议本身。JSON 模式下，把它序列化为 JSON 字符串，放入一个 `{"action":"final", ...}` AgentDecision 的 `answer`。Native 模式下，通过 `agent_control(action="final", answer="<JSON 字符串>")` 提交；不要把业务对象直接写进普通 assistant content，也不要用 Markdown 包裹。

载荷必须包含以下必需字段。只有来源确实支持相关数值，或同一统计粒度的数据表能增加信息时，才附加可选的 `metrics` 和 `datasets`。`analysis_features` 由应用生成，模型不得创建。

```json
{
  "node_id": "node-...",
  "slot_id": "origin_early_signals",
  "revision": 0,
  "claims": [
    {
      "claim_id": "claim-...",
      "claim_type": "observation",
      "text": "...",
      "subject": "...",
      "predicate": "...",
      "object_value": "...",
      "time_scope": "...",
      "source_document_ids": ["doc-..."],
      "evidence_ids": ["evidence-..."]
    }
  ],
  "evidence": [
    {
      "evidence_id": "evidence-...",
      "claim_id": "claim-...",
      "document_id": "doc-...",
      "excerpt": "原文中可定位的片段",
      "locator": {"url": "https://...", "section": "..."},
      "relation": "supports"
    }
  ],
  "conclusion": "面向用户的节点综合结论。",
  "media": []
}
```

`claims`、`evidence` 和 `media` 都必须是数组。若提供 `metrics` 或 `datasets`，也必须是符合项目合同的数组。不要混入其它图表或分析结构的字段。`MetricObservation` 只能包含 `metric_id`、`name`、`value`、`unit`、`time_scope`、
`source_document_id`, `evidence_id`, `locator`, `direct_or_derived`, `formula`,
`geography`, `subject`, `population`, `denominator`, `limitation`, and
`confidence`; use `direct_or_derived: "derived"` only with a non-empty formula.
When `confidence` is present, it must be a JSON number from `0` through `1` or
`null`; omit it when the source does not support a numeric confidence value.
An `AnalysisDataset` may contain only `dataset_id`, `purpose`, `columns`,
`rows`, `grain`, `sample_scope`, `time_scope`, `method`, `input_metric_ids`,
`citation_ids`, `source_dataset_ids`, `source_feature_ids`, and `limitations`.
The collection metadata in `report_data_needs` (`collection_mode`,
`merge_strategy`, `minimum_samples`, and `collection_guidance`) guides research;
it is not part of an `AnalysisDataset` and must not be copied into one. When a
contract error concerns only a field name or an evidence ID link, repair the
payload using the evidence already present. Search again only when the source
material itself is missing or cannot support the claim.
`source_feature_ids` 只用于应用派生的分析数据集，不得编造 feature ID。Evidence 对象必须且只能包含
`evidence_id`、`claim_id`、`document_id`、`excerpt`、`locator` 和 `relation`；不要添加 `source_title` 或其它字段。`relation` 只能是
`supports`、`contradicts` 或 `mentions`，不能替换成同义词。节点内的主张 ID 和证据 ID 必须唯一。每条主张至少引用一个 evidence ID，且每条证据都必须引用本输出中的主张。包括修订在内，NodeDraft 至少包含一条主张和一条证据。指标必须引用其来源文档中的证据；派生指标必须提供公式。数据集行只能使用已声明的列；适用时须说明统计粒度、样本范围、时间范围、方法和限制。

如果原始材料包含两个或以上统计含义相同的观测，应保留为同一统计粒度的数据集，不要只写在段落里。传播序列必须包含可比较的 `time`、平台或等效分组字段和数值 `count`；情感或立场分布必须说明样本范围，并包含 `label` 与 `count` 或 `share`；雷达图数据必须是同一对象的多个数值维度。每行保留原单位、时间范围、样本边界和证据引用。不能把定义不同的指标、搜索结果数、转发数或近似范围拼成图表。来源不支持所需字段时，不输出该数据集，并在节点结论中说明具体边界。

载荷要完整且精简：只保留补充独立方面或主体的主张；每条主张使用最短且足以支撑它的原文片段，不要复制整篇文章；避免在结论和 evidence 中重复相同原文。每条主张优先绑定一到两条直接相关的证据；若需独立印证或说明实质冲突，可增加证据。不能为了压缩输出而省略必需字段。

提交前由 NodeController 和领域合同校验 NodeDraft。若收到校验错误，应修正完整载荷，不能因此省略主张或用结论代替证据。AgentDecision 外层协议由运行时独立校验。应用会分配稳定的 node、claim、evidence 和 media ID；模型提供的 ID 只是临时关联键，应保持简短且在本载荷内一致。不要声称已运行本地 Python 校验脚本，Agent 没有 shell 执行能力。

## 注意事项

- 不要返回来源上下文使用的 `documents` / 全局 `claims` / 全局 `evidence` 包装；本节点的交付物是 NodeDraft。
- 不要返回 `PASS`、`GAP`、`CONFLICT` 或 `POLICY_BLOCKED`；节点由独立的编辑审稿 Skill 评阅。
- 不要把重复转载、情感倾向或搜索结果数量当作事实证明。
- `source_analysis_features` 只描述当前样本，可辅助节点结论，但不能替代事实证据。
- 不要把抓取时间当成事件发生时间，也不要只凭先后顺序推断因果。
- 不得编造媒体 URL 或来源片段；没有相关媒体时返回空 `media` 数组。
- 不要把来源原文片段直接当作主张；片段是依据，主张应是研究 Agent 的跨来源归纳。
- 节点内 ID 应简短且稳定，便于之后的审稿引用。
