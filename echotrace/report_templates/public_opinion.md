---
template_id: public_opinion.report
version: "1.2"
investigation_type: public_opinion
trace_template_id: public_opinion.trace
trace_template_version: trace-template.v1
language: zh-CN
datasets:
  - id: discussion_trend
    purpose: 观察讨论样本随时间和平台的变化
    source_nodes: [trigger_content, initial_spread, narrative_shift]
    columns: [time, platform, count, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 同口径平台或时间观测
      minimum_samples: 3
      guidance: 只提取来源明确发布的讨论量或互动量，注明平台、时间、指标口径和样本范围；转载篇数、搜索结果数不能代表讨论热度，也不能据此推断全网规模。
  - id: sentiment_distribution
    purpose: 展示样本中的情感标签及其口径
    source_nodes: [core_positions, current_opinion_state]
    columns: [document_id, label, count, share, citation_ids]
    required: false
    collection:
      mode: analysis_feature
      sample_unit: 篇公开来源文本
      minimum_samples: 5
      feature_type: sentiment
      aggregation: count_by_label
      guidance: 由应用层汇总每篇被节点证据引用的公开来源文本情感特征。仅用于描述来源文本信号，绝不能解释为平台评论倾向或公众总体态度。
  - id: topic_positions
    purpose: 连接主题、立场、情感和样本数量
    source_nodes: [core_positions, narrative_shift]
    columns: [topic, stance, sentiment, count, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 有来源定位的独立观点样本
      minimum_samples: 3
      guidance: 仅当来源正文确实包含可定位的用户/参与者观点时，按互不重复的主题、立场和情感归纳观点样本；注明采集平台、时间窗和样本量，不把新闻报道文本当作评论样本。
  - id: source_topic_distribution
    purpose: 展示被引用公开来源文本中的主题信号
    source_nodes: [core_positions, narrative_shift]
    columns: [document_id, label, count, share, citation_ids]
    required: false
    collection:
      mode: analysis_feature
      sample_unit: 篇公开来源文本
      minimum_samples: 3
      feature_type: topic
      aggregation: count_by_label
      guidance: 由应用层汇总规则主题特征，只描述采集来源文本覆盖的主题，不代表公众讨论频率；一篇文本可匹配多个主题。
  - id: source_stance_distribution
    purpose: 展示被引用公开来源文本中的立场信号
    source_nodes: [core_positions, current_opinion_state]
    columns: [document_id, label, count, share, citation_ids]
    required: false
    collection:
      mode: analysis_feature
      sample_unit: 篇公开来源文本
      minimum_samples: 5
      feature_type: stance
      aggregation: count_by_label
      guidance: 由应用层汇总每篇被节点证据引用的公开来源文本立场特征。仅描述文本信号，不代表平台评论或公众总体立场。
  - id: comment_sentiment_samples
    purpose: 展示可定位评论样本中的情感标签
    source_nodes: [core_positions, current_opinion_state]
    columns: [sample_id, label, count, share, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 去重且可定位的用户评论文本
      minimum_samples: 10
      aggregation: count_by_label
      guidance: 仅从来源正文中确实可见的公开用户评论提取；每条去重评论单独成行，count 填 1，label 标注情感类别，citation_ids 指向包含该评论原文的 evidence。注明平台和采样时间，不能把新闻正文、机构表态或分析特征当评论，不能把便利样本比例推广为公众总体。
visuals:
  - id: discussion-trend
    type: line
    title: 讨论热度变化
    dataset: discussion_trend
    x_field: time
    y_field: count
    fallback: table
  - id: sentiment-distribution
    type: pie
    title: 公开来源文本情感信号
    dataset: sentiment_distribution
    x_field: label
    y_field: count
    fallback: table
  - id: source-topic-distribution
    type: bar
    title: 公开来源文本主题覆盖
    dataset: source_topic_distribution
    x_field: label
    y_field: count
    fallback: table
  - id: source-stance-distribution
    type: bar
    title: 公开来源文本立场信号
    dataset: source_stance_distribution
    x_field: label
    y_field: count
    fallback: table
  - id: comment-sentiment-distribution
    type: pie
    title: 可定位评论样本情感分布
    dataset: comment_sentiment_samples
    x_field: label
    y_field: count
    fallback: table
chapters:
  - id: summary
    title: 舆情摘要
    purpose: 汇总讨论对象、主要观点、可观察变化和当前状态。
    node_slots: [trigger_content, initial_spread, core_positions, narrative_shift, response_repair, current_opinion_state]
    recommended_blocks: [paragraph, metric_grid]
    data_needs: [metrics, sentiment, stance, topic]
    empty_data_behavior: 没有可说明样本的数据时只给定性总结，并写明样本限制。
  - id: trigger_spread
    title: 触发内容与传播样本
    purpose: 解释讨论由什么内容触发，以及样本中的扩散路径。
    node_slots: [trigger_content, initial_spread]
    recommended_blocks: [paragraph, timeline, table, image, video]
    data_needs: [metrics, media, sources]
    empty_data_behavior: 缺少平台或时间分布数据时不推断全网传播规模。
  - id: positions
    title: 核心观点、情感与立场
    purpose: 呈现讨论主题、情感倾向、立场分歧和代表性样本。
    node_slots: [core_positions]
    recommended_blocks: [paragraph, chart, table, evidence_card]
    data_needs: [sentiment, stance, topic, sources]
    empty_data_behavior: 未接入或样本不足时明确标注未分析或样本不足，不补造比例。
  - id: narrative_response
    title: 讨论变化与主体回应
    purpose: 连接讨论状态变化、触发材料和相关主体回应。
    node_slots: [narrative_shift, response_repair]
    recommended_blocks: [paragraph, timeline, chart, claim_table]
    data_needs: [metrics, sentiment, topic, media]
    empty_data_behavior: 无可比时间窗口时只描述已观察到的材料，不声称回应改变了舆情。
  - id: current_state
    title: 当前状态与样本边界
    purpose: 总结仍在持续的议题、未解决争议和本次样本的适用范围。
    node_slots: [current_opinion_state]
    recommended_blocks: [paragraph, callout, source_list]
    data_needs: [sentiment, stance, topic, sources]
    empty_data_behavior: 将结论限定于实际采集的平台、时间窗和样本，不代表全部公众。
---
# 公共舆情调查报告

报告对象是可观察到的讨论与表达。情感、立场和主题来自样本分析，不等同于外部事实；所有比例必须同时交代样本量、时间范围、平台范围和分析方法。

## 舆情摘要
先概括触发议题、主要分歧、讨论变化与当前状态。来源文本情感/立场/主题图仅描述被引用的公开来源文档，必须呈现样本数和分析边界；只有实际评论样本才能支持对评论倾向的描述。
### 主要议题与分歧
归纳当前讨论集中在哪些问题，区分事实争议、价值判断和利益诉求。
### 讨论变化与当前状态
概括有材料支持的变化，以及调查时点下仍在持续的讨论。
### 样本结论边界
明确本报告观察到的文本或评论样本及其覆盖限制。

## 触发内容与传播样本
引用原始触发内容，说明发布主体和语境。按可核实的首发、转发或转载材料描述传播，不把少量采集记录称为全网传播。
### 触发内容及发布语境
说明原始内容、发布时间、发布主体和当时语境。
### 可观察传播路径
按来源记录串联传播节点，并标明转述关系和采样范围。
### 传播指标及口径
只有存在同口径样本时才比较趋势，注明时间窗、平台和指标定义。

## 核心观点、情感与立场
归纳互不重复的主要观点，给出可定位的代表性样本。区分公开来源文本分析特征与用户评论样本；不得把情感极性改写成事实判断，也不得把来源文本比例写成总体民意。
### 主要观点与诉求
归纳互不重复的观点类别，并以可定位样本说明其实际表达。
### 情感与立场信号
描述分析样本中的情感和立场分布，不能替代事实核验。
### 主题关系与代表性材料
呈现议题之间的关系及能够支撑判断的代表性材料。

## 讨论变化与主体回应
分别呈现讨论主题或立场变化，以及机构、企业、平台的回应内容和实际行动。只有存在可比较的时间窗口和样本口径时才讨论变化幅度或回应效果。
### 讨论主题或立场变化
按可比较的时间窗口描述变化，说明无法比较的部分。
### 相关主体回应与行动
区分回应表态、实际行动和已验证结果。
### 回应效果的证据范围
只报告能够由样本和时间口径支撑的观察，不据此推断因果。

## 当前状态与样本边界
说明持续争议、讨论是否仍在发生及采样覆盖限制。结论明确适用于哪些平台和时间段，哪些群体或内容没有被覆盖。
### 当前讨论状态
总结调查时点可以确认的讨论状态和后续变化。
### 平台、时间与样本覆盖
列明实际观察的平台、时间窗、样本类型和未覆盖部分。
