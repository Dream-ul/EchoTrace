---
template_id: public_event.report
version: "1.2"
investigation_type: public_event
trace_template_id: public_event.trace
trace_template_version: trace-template.v1
language: zh-CN
datasets:
  - id: event_timeline
    purpose: 按时间整理事件节点、主体和可定位来源
    source_nodes: [origin_early_signals, event_scope, spread_amplification, turning_point, response_treatment, current_outcome]
    columns: [time, event, subject, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 可定位事件记录
      minimum_samples: 3
      merge_strategy: event_rows
      guidance: 从来源证据中逐条整理事件发生时间、事件内容和主体；无法确认事件时间时标为未知并保留原文日期，不得用发布日期或抓取时间代替。每行 citation_ids 必须指向支持该行的 evidence_id。
  - id: impact_metrics
    purpose: 展示冻结节点中有明确口径的影响或处置指标
    source_nodes: [event_scope, response_treatment, current_outcome]
    columns: [name, value, unit, time_scope, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 有明确统计口径的指标观测
      minimum_samples: 2
      guidance: 搜索并提取官方通报、监管文件或原始记录中明确发布的数值，保留指标名称、数值、单位、统计时间和分母/范围；不得把搜索结果条数、转载数或不同口径数值合并。
  - id: spread_trend
    purpose: 展示同口径材料中的传播观测变化
    source_nodes: [spread_amplification, turning_point]
    columns: [time, platform, count, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 同口径平台或时间观测
      minimum_samples: 3
      guidance: 仅提取来源明确报告的传播观测值，并保留时间、平台/等价分组、数量和样本口径；不同平台或不同互动指标不得拼成一条序列。必须有至少两个可比较时间点才能用于趋势图。
visuals:
  - id: event-timeline
    type: timeline
    title: 事件时间线
    dataset: event_timeline
    fallback: table
  - id: spread-trend
    type: line
    title: 传播观测变化
    dataset: spread_trend
    x_field: time
    y_field: count
    fallback: table
chapters:
  - id: summary
    title: 执行摘要
    purpose: 综合全部冻结节点，直接说明事件、关键处置和当前结果。
    node_slots: [origin_early_signals, event_scope, spread_amplification, turning_point, response_treatment, current_outcome]
    recommended_blocks: [paragraph, metric_grid]
    data_needs: [metrics]
    empty_data_behavior: 没有可核实指标时用简洁文字概述，不估算数量或比例。
  - id: origin_scope
    title: 事件起点与范围
    purpose: 说明最早信号、事件事实、涉及主体和可确认范围。
    node_slots: [origin_early_signals, event_scope]
    recommended_blocks: [paragraph, timeline, evidence_card]
    data_needs: [media, sources]
    empty_data_behavior: 对无法确认的起点、时间或范围明确写出材料边界。
  - id: spread_turning
    title: 传播与关键转折
    purpose: 说明信息传播、叙事变化及可观察的转折依据。
    node_slots: [spread_amplification, turning_point]
    recommended_blocks: [paragraph, chart, table]
    data_needs: [metrics, sources]
    empty_data_behavior: 缺少同口径时序数据时按材料描述，不绘制趋势图。
  - id: response_outcome
    title: 多方响应与当前结果
    purpose: 按主体呈现行动、过程、结果和仍在进行的事项。
    node_slots: [response_treatment, current_outcome]
    recommended_blocks: [paragraph, claim_table, image, video]
    data_needs: [media, sources]
    empty_data_behavior: 区分已经完成、正在进行和材料尚未确认的处置。
  - id: sources_limits
    title: 来源与调查边界
    purpose: 汇总关键来源及本次调查能支持的结论范围。
    node_slots: [origin_early_signals, event_scope, spread_amplification, turning_point, response_treatment, current_outcome]
    recommended_blocks: [source_list, callout]
    data_needs: [sources]
    empty_data_behavior: 如来源信息不完整，说明缺少的来源类型和可复核范围。
---
# 公共事件调查报告

面向关注事件全貌的读者。先给出当前可确认的结论，再沿事件进展展开。明确区分事件发生时间、材料发布时间和调查时间；传播量只表示被观测到的传播，不作为事件真实性或公众态度的证明。

## 执行摘要
用一段结论说明事件是什么、目前最重要的行动与结果。若材料支持，可在旁边展示有明确时间范围和口径的关键数字；每个数字都要保留来源引用。
### 核心判断
先给出调查时点下最准确、最有价值的一句话结论，并说明结论适用范围。
### 关键进展
按事实发展顺序概括改变事件状态的主要行动和结果，不重复后文细节。
### 当前边界
点明仍未确认的关键事项，区分材料缺口与已经否定的事实。

## 事件起点与范围
结合“起点与早期信号”和“事件事实与范围”节点，说明最早可定位的材料、主体、时间地点以及已确认影响。早期信息与后续确认结果分开表达。
### 最早信号及时间层次
区分事件发生、首次公开、机构回应和调查取材的时间。
### 事件经过与涉及主体
沿可核实的先后关系说明事件经过、相关主体和已确认影响。
### 多方说法与事实边界
并列呈现存在分歧的定性，并说明哪些行为要素获得材料支持、哪些仍不能确认。
#### 共同确认与争议部分
将多来源共同支持的事实与单方陈述、来源转述和争议判断清楚分开。

## 传播与关键转折
串联传播波次和状态变化。只有输入含同一口径的时间序列时才使用趋势图；转载链条应说明来源依赖，不能把先后关系写成因果。
### 传播路径与阶段
依可定位材料串联首发、扩散节点和主要传播载体，不把样本称为全网覆盖。
### 议题变化与关键转折
说明每次议题或状态变化由什么新材料、裁判或处置触发。
### 传播数据与来源依赖边界
交代指标口径、可比范围和转载依赖；不能比较的数据分开展示。

## 多方响应与当前结果
按机构、企业、平台等主体组织行动，写清采取了什么措施、执行到哪一步以及结果由什么材料支持。承诺、启动和完成应分别表述；相关图片或视频须来自冻结节点媒体。
### 司法程序与机构处置
按程序进展说明司法结论和机构决定，准确表述决定主体、日期和适用范围。
### 各方行动及执行结果
逐一说明相关主体做了什么、执行到哪一步、产生什么可确认结果。
### 仍待确认事项
列出尚缺的一手材料或仍在进行的事项，不把未检索到写成没有发生。

## 来源与调查边界
列出支撑主要结论的来源。说明现有材料无法回答的问题，不从传播热度、单一报道或未核实说法推导结论。
### 关键来源及其用途
优先说明直接支撑主要结论的一手材料，以及媒体来源用于补充的内容。
### 调查范围与证据限制
概述已检索范围、来源局限和仍无法回答的问题。
