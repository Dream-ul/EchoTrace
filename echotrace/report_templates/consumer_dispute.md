---
template_id: consumer_dispute.report
version: "1.2"
investigation_type: consumer_dispute
trace_template_id: consumer_dispute.trace
trace_template_version: trace-template.v1
language: zh-CN
datasets:
  - id: response_timeline
    purpose: 按时间和主体整理回应、承诺与执行状态
    source_nodes: [first_feedback, merchant_platform_response, escalation_intervention]
    columns: [time, actor, action, status, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 有来源定位的沟通或处置记录
      minimum_samples: 2
      merge_strategy: event_rows
      guidance: 从订单、平台记录、当事人公开材料或监管记录整理时间、主体、实际行动和状态；承诺与履约分开，材料没有明确时间时保留未知。
  - id: remedy_status
    purpose: 对照退款、换货、赔偿等方案与实际履行情况
    source_nodes: [remedy_fulfillment, unresolved_items]
    columns: [stage, status, amount, unit, citation_ids]
    required: false
    collection:
      mode: research
      sample_unit: 有明确金额口径的履约阶段
      minimum_samples: 2
      guidance: 仅提取有凭据的退款、赔偿、换货金额及履约状态；区分提出金额、协商金额、实际到账金额，不把不同币种、不同当事人或未履行方案相加。
visuals:
  - id: response-timeline
    type: timeline
    title: 处理过程时间线
    dataset: response_timeline
    fallback: table
  - id: remedy-status
    type: bar
    title: 履约金额对比
    dataset: remedy_status
    x_field: stage
    y_field: amount
    fallback: table
chapters:
  - id: summary
    title: 纠纷摘要
    purpose: 直接交代交易关系、争议焦点、处理进展和当前结果。
    node_slots: [transaction_service, first_feedback, merchant_platform_response, escalation_intervention, remedy_fulfillment, unresolved_items]
    recommended_blocks: [paragraph, metric_grid]
    data_needs: [metrics]
    empty_data_behavior: 没有可核实金额或时长时不估算，并明确材料范围。
  - id: transaction_feedback
    title: 交易关系与首次反馈
    purpose: 还原交易或服务条件以及消费者最初反馈和诉求。
    node_slots: [transaction_service, first_feedback]
    recommended_blocks: [paragraph, timeline, evidence_card, image]
    data_needs: [metrics, media, sources]
    empty_data_behavior: 区分当事人陈述、订单记录和合同规则，未提供材料处标注待核。
  - id: initial_response
    title: 商家与平台的处理过程
    purpose: 并列说明商家、平台各自的回应、动作、承诺和执行结果。
    node_slots: [merchant_platform_response]
    recommended_blocks: [claim_table, timeline, callout]
    data_needs: [metrics, sources]
    empty_data_behavior: 无法确认承诺履行时写明尚无完成凭据，不推定一方已解决。
  - id: escalation
    title: 升级、监管或司法介入
    purpose: 说明争议升级节点、介入主体、程序状态和阶段性结果。
    node_slots: [escalation_intervention]
    recommended_blocks: [paragraph, timeline, evidence_card]
    data_needs: [media, sources]
    empty_data_behavior: 区分受理、调解、立案和实体裁决，不把程序启动写成责任认定。
  - id: remedy_unresolved
    title: 履约结果与未解决事项
    purpose: 总结退款、换货、赔偿或其他履约情况，并列出尚未完成内容。
    node_slots: [remedy_fulfillment, unresolved_items]
    recommended_blocks: [paragraph, table, callout]
    data_needs: [metrics, sources]
    empty_data_behavior: 分开陈述方案、实际履行和双方确认；没有结果凭据时保留不确定性。
---
# 消费纠纷调查报告

以可核对的交易材料和沟通记录为主线。保持消费者陈述、商家或平台回应、订单凭据、监管记录和处理结果之间的来源区别，不代替司法或监管机关作责任认定。

## 纠纷摘要
用简洁文字说明交易对象、争议焦点、当前处理状态及双方已确认的结果。金额、日期和时长均须来自节点材料并标出对应口径。
### 交易与争议焦点
说明交易对象、核心争议和双方已确认的事实。
### 当前处理结果
概括已履行事项、未完成事项和处理状态。
### 结论适用范围
明确报告根据哪些材料描述纠纷，不替代监管或司法认定。

## 交易关系与首次反馈
说明交易或服务条件、付款和履约状态，再呈现消费者最早反馈的问题和诉求。将陈述与订单、合同或平台记录分开标注。
### 交易条件与履约记录
呈现订单、合同、付款和服务交付等可核对记录。
### 消费者首次反馈与诉求
说明首次反馈时间、内容及消费者提出的解决要求。
### 双方确认与分歧
将凭据可确认的内容与双方陈述中的不同口径分开。

## 商家与平台的处理过程
分别描述商家和平台回应了什么、实际采取了什么行动、承诺是否履行。不要把自动回复或处理承诺写成问题已解决。
### 商家回应与行动
说明商家表态、实际采取的措施及履行凭据。
### 平台介入与处理流程
说明平台受理、调查、调解或作出的决定及材料依据。
### 承诺与实际履行差异
明确哪些是承诺、哪些已完成、哪些没有结果凭据。

## 升级、监管或司法介入
按时间整理投诉升级、监管调解或司法程序。明确程序所处阶段及决定的适用范围，不从立案或受理推导实体责任。
### 投诉升级与监管处理
按时间说明投诉、受理、调解或监管决定。
### 司法程序与适用范围
准确说明程序阶段、裁判结论和适用主体。
### 外部介入仍未解决的事项
指出介入后仍未解决的争议和材料空白。

## 履约结果与未解决事项
逐项说明退款、换货、赔偿等方案是否实际完成、由谁确认，并列出仍待处理的事项和欠缺的材料。
### 退款、换货或赔偿结果
按具体方案列明执行状态、完成时间和确认来源。
### 尚未完成的义务与争议
列出未履行部分及其责任主体陈述。
### 后续处理和证据缺口
说明还需哪些材料才能确认纠纷解决。
