<template>
  <div class="workbench-page">
    <header class="app-header">
      <a class="brand" href="/investigations" aria-label="EchoTrace 调查工作台">
        <span class="brand-mark">E</span>
        <span>EchoTrace</span>
      </a>
      <div class="header-actions">
        <el-button class="header-action" :icon="Setting" aria-label="项目设置" @click="openSettings">设置</el-button>
        <el-button class="header-action create-action" type="primary" :icon="Plus" aria-label="新建调查" @click="dialogVisible = true">新建调查</el-button>
      </div>
    </header>

    <div class="workspace-layout">
      <aside class="investigation-list" aria-label="调查列表">
        <div class="list-heading">
          <h2>调查</h2>
          <el-button text :icon="Refresh" aria-label="刷新调查列表" :loading="loadingRuns" @click="reloadRuns" />
        </div>
        <el-skeleton v-if="loadingRuns && runs.length === 0" :rows="5" animated />
        <el-empty v-else-if="runs.length === 0" description="暂无调查" :image-size="72" />
        <div v-else class="run-list">
          <button
            v-for="item in runs"
            :key="item.run_id"
            class="run-item"
            :class="{ selected: item.run_id === selectedId }"
            @click="selectRun(item.run_id)"
          >
            <span class="run-item-title">{{ item.event?.title || item.objective }}</span>
            <span class="run-item-meta">
              <i class="status-dot" :class="`status-${item.status}`" />
              {{ runStatusLabel(item) }}
              <span aria-hidden="true">·</span>
              {{ formatDate(item.updated_at) }}
            </span>
          </button>
          <el-button v-if="hasMoreRuns" class="load-more" text :loading="loadingMore" @click="loadMoreRuns">加载更多调查</el-button>
        </div>
      </aside>

      <main class="investigation-main">
        <el-skeleton v-if="loadingDetail" :rows="9" animated />
        <el-empty v-else-if="!investigation" description="选择一项调查，或新建调查开始研究" :image-size="100" />
        <template v-else>
          <header class="investigation-heading">
            <div class="heading-copy">
              <p class="eyebrow">{{ investigationTypeLabel(investigation.investigation_type) }}</p>
              <h1>{{ investigation.event?.title || investigation.objective }}</h1>
              <p class="objective">{{ investigation.objective }}</p>
              <p v-if="investigation.subject" class="subject">调查对象：{{ investigation.subject }}</p>
            </div>
            <div class="heading-actions">
              <el-tag :type="statusType(investigation.status)" effect="light" size="large">{{ investigationStatusLabel }}</el-tag>
              <el-button v-if="['created', 'failed'].includes(investigation.status)" type="primary" :loading="actionLoading" @click="runSelected">开始调查</el-button>
              <el-button v-if="investigation.status === 'running'" type="warning" plain :loading="actionLoading" @click="pauseSelected">暂停</el-button>
              <el-button v-if="investigation.status === 'paused'" type="primary" :loading="actionLoading" @click="resumeSelected">继续调查</el-button>
              <el-dropdown v-if="['created', 'running', 'paused'].includes(investigation.status)" @command="handleRunCommand">
                <el-button :icon="MoreFilled" aria-label="更多调查操作" />
                <template #dropdown>
                  <el-dropdown-menu><el-dropdown-item command="cancel">取消调查</el-dropdown-item></el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </header>

          <section class="progress-overview" aria-label="调查进度概览">
            <div class="progress-title-row">
              <div>
                <strong>{{ progressHeadline }}</strong>
                <span>{{ progressCaption }}</span>
              </div>
              <span class="progress-percent">{{ progressPercent }}%</span>
            </div>
            <el-progress :percentage="progressPercent" :show-text="false" :stroke-width="6" :color="progressColor" />
            <div class="summary-stats">
              <div><strong>{{ completedNodes }}<span>/{{ nodes.length || '—' }}</span></strong><span>节点完成</span></div>
              <div><strong>{{ claimCount }}</strong><span>已归纳主张</span></div>
              <div><strong>{{ sourceCount }}</strong><span>关联来源</span></div>
              <div><strong>{{ evidenceCount }}</strong><span>依据片段</span></div>
            </div>
            <div v-if="reportStageVisible" class="report-progress" aria-live="polite">
              <div class="report-progress-heading">
                <span class="planning-indicator" :class="{ active: !reportGenerationComplete && (reportProgress.status === 'not_started' || reportProgress.status === 'planning' || reportProgress.status === 'writing') }" />
                <div>
                  <strong>{{ reportProgressHeadline }}</strong>
                  <span>{{ reportProgressCaption }}</span>
                </div>
              </div>
              <div v-if="reportProgress.sections.length" class="report-section-progress">
                <div v-for="section in reportProgress.sections" :key="section.chapter_id" class="report-section-item">
                  <span class="report-section-marker" :class="`section-${reportGenerationComplete ? 'completed' : section.status}`">{{ reportGenerationComplete || section.status === 'completed' ? '✓' : '' }}</span>
                  <span>{{ section.title }}</span>
                  <small>{{ reportSectionStatusLabel(reportGenerationComplete ? 'completed' : section.status) }}</small>
                </div>
              </div>
            </div>
          </section>

          <el-alert v-if="investigation.status === 'paused' && investigation.interrupt_reason" class="status-message" type="warning" :closable="false" :title="friendlyError(investigation.interrupt_reason)" />
          <el-alert v-if="investigation.status === 'failed' && investigation.error" class="status-message" type="error" :closable="false" :title="friendlyError(investigation.error)" />
          <el-alert v-if="pollingError" class="status-message" type="warning" :closable="false" title="进度暂时无法更新，系统会继续尝试连接。" />

          <el-tabs v-model="activeTab" class="content-tabs" @tab-change="handleTabChange">
            <el-tab-pane label="调查过程" name="progress">
              <section class="node-progress-section">
                <div class="section-heading">
                  <div><h2>调查节点</h2><p>围绕固定调查脉络逐项研究，每个节点经过三轮审阅。</p></div>
                  <span v-if="nodes.length" class="section-count">{{ nodes.length }} 个节点</span>
                </div>

                <el-skeleton v-if="loadingNodes && nodes.length === 0" :rows="5" animated />
                <div v-else-if="nodes.length === 0" class="planning-state">
                  <span class="planning-indicator" :class="{ active: investigation.status === 'running' }" />
                  <div>
                    <strong>{{ investigation.status === 'running' ? '正在规划调查脉络' : '调查节点尚未生成' }}</strong>
                    <p>{{ investigation.status === 'running' ? '节点就绪后会显示各自的研究进展。' : '开始调查后，系统会按调查类型生成对应节点。' }}</p>
                  </div>
                </div>
                <div v-else class="node-list">
                  <button v-for="node in nodes" :key="node.node_id" class="node-row" @click="openNode(node)">
                    <span class="node-index" :class="nodeStatusClass(node.status)">{{ String(node.node_order).padStart(2, '0') }}</span>
                    <span class="node-content">
                      <span class="node-title-line">
                        <strong>{{ node.title }}</strong>
                        <span class="node-state" :class="nodeStatusClass(node.status)">{{ nodeStatusLabel(node) }}</span>
                      </span>
                      <span class="node-goal">{{ node.goal }}</span>
                      <span class="review-track" aria-label="三轮审稿进度">
                        <span v-for="round in reviewRounds" :key="round.no" class="review-step" :class="reviewStepClass(node, round.no)">
                          <i>{{ reviewExists(node, round.no) ? '✓' : round.no }}</i>
                          <span>{{ round.label }}</span>
                        </span>
                      </span>
                      <span v-if="conclusionPreview(node)" class="node-conclusion">{{ conclusionPreview(node) }}</span>
                    </span>
                    <el-icon class="node-chevron"><ArrowRight /></el-icon>
                  </button>
                </div>
              </section>
            </el-tab-pane>

            <el-tab-pane label="事件脉络" name="trace">
              <div v-if="traceLoading" class="trace-loading"><el-skeleton :rows="8" animated /></div>
              <NodeTracePanel v-else-if="trace" :trace="trace" :review="traceReview" />
              <el-empty v-else :description="traceError || '所有节点完成审阅后，将在这里形成完整事件脉络。'" :image-size="88" />
            </el-tab-pane>

            <el-tab-pane v-if="reportAvailable" label="最终报告" name="report">
              <section class="report-toolbar">
                <div><h2>调查报告</h2><p>由已冻结的事件脉络整理生成。</p></div>
                <div class="report-exports">
                  <a class="export-link" :href="api.reportUrl(investigation.run_id, 'markdown')" download="调查报告.md"><el-icon><Document /></el-icon>Markdown</a>
                  <a class="export-link" :href="api.reportUrl(investigation.run_id, 'pdf')" download="调查报告.pdf"><el-icon><Download /></el-icon>PDF</a>
                  <a class="export-link" :href="api.reportUrl(investigation.run_id, 'html')" target="_blank" rel="noreferrer"><el-icon><View /></el-icon>单独打开</a>
                </div>
              </section>
              <iframe class="report-frame" :src="api.reportUrl(investigation.run_id, 'html')" title="调查报告" loading="lazy" sandbox="allow-same-origin allow-popups" />
            </el-tab-pane>
          </el-tabs>
        </template>
      </main>
    </div>

    <el-drawer v-model="nodeDrawerVisible" :title="selectedNode?.title || '节点详情'" size="min(720px, 94vw)" class="node-drawer">
      <template v-if="selectedNode">
        <p class="drawer-goal">{{ selectedNode.goal }}</p>
        <div class="drawer-meta">
          <el-tag :type="nodeStatusType(selectedNode.status)" effect="light">{{ nodeStatusLabel(selectedNode) }}</el-tag>
          <span>主张 {{ selectedDraft.claims?.length || 0 }}</span>
          <span>依据 {{ selectedDraft.evidence?.length || 0 }}</span>
        </div>
        <section v-if="selectedDraft.conclusion" class="drawer-section conclusion-section">
          <h3>节点综合结论</h3>
          <ConclusionRenderer :ir="selectedDraft.conclusion_ir" :has-source-text="Boolean(selectedDraft.conclusion)" />
        </section>
        <section class="drawer-section">
          <h3>研究主张</h3>
          <el-empty v-if="!selectedDraft.claims?.length" description="主张正在整理中" :image-size="60" />
          <article v-for="(claim, index) in selectedDraft.claims || []" :key="claim.claim_id" class="claim-item">
            <div class="claim-heading"><span>{{ String(index + 1).padStart(2, '0') }}</span><strong>{{ claimTypeLabel(claim.claim_type) }}</strong></div>
            <p>{{ displayText(claim.text) }}</p>
            <div v-for="evidence in evidenceForClaim(selectedDraft, claim)" :key="evidence.evidence_id" class="evidence-quote">
              <blockquote>{{ displayText(evidence.excerpt) }}</blockquote>
              <a v-if="evidence.locator?.url" :href="safeUrl(evidence.locator.url)" target="_blank" rel="noreferrer">
                {{ sourceHost(evidence.locator.url) }}<el-icon><TopRight /></el-icon>
              </a>
              <span v-else>来源片段 · {{ relationLabel(evidence.relation) }}</span>
            </div>
          </article>
        </section>
        <section class="drawer-section review-section">
          <h3>审阅与修订</h3>
          <div v-for="round in reviewRounds" :key="round.no" class="review-detail" :class="{ done: reviewForRound(selectedNode, round.no) }">
            <span class="review-number">{{ reviewForRound(selectedNode, round.no) ? '✓' : round.no }}</span>
            <div>
              <strong>第{{ round.no }}轮 · {{ round.label }}</strong>
              <p v-if="reviewSummary(reviewForRound(selectedNode, round.no))">{{ displayText(reviewSummary(reviewForRound(selectedNode, round.no))) }}</p>
              <p v-else class="review-pending">{{ round.no <= (selectedNode.current_revision || 0) ? '本轮意见已纳入修订' : '等待本轮审阅' }}</p>
            </div>
          </div>
        </section>
        <section v-if="selectedDraft.media?.length" class="drawer-section">
          <h3>图片与视频</h3>
          <div v-for="media in selectedDraft.media" :key="media.media_id" class="media-row">
            <a v-if="media.source_url" :href="safeUrl(media.source_url)" target="_blank" rel="noreferrer">{{ media.caption || '打开相关媒体' }}<el-icon><TopRight /></el-icon></a>
            <span v-else>{{ media.caption }}</span>
            <small>{{ media.why_relevant }}</small>
          </div>
        </section>
      </template>
    </el-drawer>

    <el-dialog v-model="dialogVisible" title="新建调查" width="min(560px, 94vw)" @closed="resetForm">
      <el-form label-position="top" @submit.prevent="create">
        <el-form-item label="调查目标" required><el-input v-model="form.objective" type="textarea" :rows="4" placeholder="例如：调查某公共事件的起因、各方响应和当前结果" /></el-form-item>
        <el-form-item label="调查对象"><el-input v-model="form.subject" placeholder="事件、品牌、产品、机构或人物（可选）" /></el-form-item>
        <el-form-item label="调查类型"><el-select v-model="form.investigation_type" style="width: 100%"><el-option v-for="item in investigationTypes" :key="item.value" :label="item.label" :value="item.value" /></el-select></el-form-item>
        <el-form-item label="补充材料（可选）">
          <div class="material-toolbar">
            <el-button size="small" @click="addMaterial">添加材料</el-button>
            <el-upload :show-file-list="false" :auto-upload="false" accept=".txt,.md,.csv,.json" :on-change="handleMaterialFile"><el-button size="small">导入文本文件</el-button></el-upload>
          </div>
          <div v-for="(material, index) in form.documents" :key="index" class="material-row">
            <div class="material-row-head"><span>材料 {{ index + 1 }}</span><el-button text type="danger" @click="removeMaterial(index)">移除</el-button></div>
            <el-input v-model="material.title" placeholder="材料标题" class="material-title" />
            <el-input v-model="material.url" placeholder="来源网址（可选）" class="material-url" />
            <el-input v-model="material.content" type="textarea" :rows="3" placeholder="粘贴正文、采访记录或备注" />
          </div>
        </el-form-item>
      </el-form>
      <template #footer><el-button @click="dialogVisible = false">取消</el-button><el-button type="primary" :loading="creating" @click="create">开始调查</el-button></template>
    </el-dialog>

    <el-dialog v-model="settingsVisible" title="项目设置" width="min(680px, 94vw)">
      <el-alert type="info" :closable="false" title="设置保存到项目根目录 .env。密钥只显示配置状态，不会回显。保存后运行时将重新加载。" />
      <el-form v-loading="settingsLoading" label-position="top" class="settings-form">
        <section v-for="group in settingGroups" :key="group.category" class="settings-group">
          <h3>{{ group.category }}</h3>
          <el-form-item v-for="item in group.items" :key="item.key" :label="item.label">
            <p class="setting-description">{{ item.description }}</p>
            <el-select v-if="item.control === 'select'" v-model="settingValues[item.key]" style="width: 100%">
              <el-option v-for="option in item.options || []" :key="option" :label="settingOptionLabel(item, option)" :value="option" />
            </el-select>
            <el-input v-else v-model="settingValues[item.key]" :type="item.secret ? 'password' : 'text'" :placeholder="item.secret ? (item.configured ? '已配置，留空则保持不变' : '请输入密钥') : item.value">
              <template #suffix><el-tag v-if="item.configured" type="success" size="small">已配置</el-tag></template>
            </el-input>
          </el-form-item>
        </section>
      </el-form>
      <template #footer><el-button @click="settingsVisible = false">取消</el-button><el-button type="primary" :loading="settingsSaving" @click="saveSettingsForm">保存并重载</el-button></template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { ArrowRight, Document, Download, MoreFilled, Plus, Refresh, Setting, TopRight, View } from '@element-plus/icons-vue'
import * as api from '@/api/investigations'
import ConclusionRenderer from '@/components/trace/ConclusionRenderer.vue'
import NodeTracePanel from '@/components/trace/NodeTracePanel.vue'

const PAGE_SIZE = 30
const reviewRounds = [
  { no: 1, label: '覆盖面' },
  { no: 2, label: '证据深度' },
  { no: 3, label: '综合定稿' },
]
const investigationTypes = [
  { value: 'auto', label: '自动判断' },
  { value: 'public_event', label: '公共事件 / 突发事件' },
  { value: 'public_opinion', label: '公共舆情 / 品牌声誉' },
  { value: 'product_safety', label: '产品安全' },
  { value: 'consumer_dispute', label: '消费纠纷' },
  { value: 'official_claim', label: '官方声明 / 事实核验' },
  { value: 'policy_regulation', label: '政策 / 监管影响' },
]

const runs = ref<api.InvestigationSummary[]>([])
const selectedId = ref('')
const investigation = ref<api.InvestigationDetail>()
const nodes = ref<api.InvestigationNode[]>([])
const trace = ref<api.InvestigationTrace>()
const traceReview = ref<{ findings?: Array<{ kind: string; message: string; recommendation?: string }> } | null>()
const traceLoading = ref(false)
const traceError = ref('')
const loadingRuns = ref(false)
const loadingMore = ref(false)
const loadingDetail = ref(false)
const loadingNodes = ref(false)
const actionLoading = ref(false)
const creating = ref(false)
const dialogVisible = ref(false)
const activeTab = ref('progress')
const nodeDrawerVisible = ref(false)
const selectedNode = ref<api.InvestigationNode>()
const reportAvailable = ref(false)
const reportProgress = ref<api.ReportProgress>({ exists: false, status: 'not_started', completed_sections: 0, total_sections: 0, sections: [] })
const pollingError = ref('')
const settingsVisible = ref(false)
const settingsLoading = ref(false)
const settingsSaving = ref(false)
const settingItems = ref<api.SettingItem[]>([])
const settingValues = reactive<Record<string, string>>({})
const runOffset = ref(0)
const hasMoreRuns = ref(false)
const settingGroups = computed(() => settingItems.value.reduce((groups: Array<{ category: string; items: api.SettingItem[] }>, item) => {
  const group = groups.find(entry => entry.category === item.category)
  if (group) group.items.push(item)
  else groups.push({ category: item.category, items: [item] })
  return groups
}, []))
const form = reactive<{ objective: string; subject: string; investigation_type: string; documents: api.InvestigationDocumentInput[] }>({ objective: '', subject: '', investigation_type: 'auto', documents: [] })
const selectedDraft = computed(() => currentDraft(selectedNode.value))
const completedNodes = computed(() => nodes.value.filter(node => node.status === 'frozen').length)
const reportStageVisible = computed(() => completedNodes.value > 0 && nodes.value.length > 0 && completedNodes.value === nodes.value.length && ['running', 'paused', 'completed', 'failed'].includes(investigation.value?.status || ''))
const progressPercent = computed(() => {
  if (investigation.value?.status === 'completed') return 100
  const nodeProgress = nodes.value.length ? completedNodes.value / nodes.value.length : 0
  if (!nodes.value.length) return 0
  if (completedNodes.value < nodes.value.length) return Math.min(80, Math.round(nodeProgress * 80))
  const reportProgressValue = reportProgress.value.total_sections
    ? reportProgress.value.completed_sections / reportProgress.value.total_sections
    : 0
  return Math.min(99, 80 + Math.round(reportProgressValue * 19))
})
const claimCount = computed(() => nodes.value.reduce((total, node) => total + (currentDraft(node)?.claims?.length || 0), 0))
const evidenceCount = computed(() => nodes.value.reduce((total, node) => total + (currentDraft(node)?.evidence?.length || 0), 0))
const sourceCount = computed(() => new Set(nodes.value.flatMap(node => (currentDraft(node)?.evidence || []).map(item => item.document_id))).size)
const progressColor = computed(() => investigation.value?.status === 'failed' ? '#c05252' : investigation.value?.status === 'completed' ? '#39816c' : '#3d7987')
const progressHeadline = computed(() => {
  if (investigation.value?.status === 'completed') return '调查与报告已完成'
  if (investigation.value?.status === 'failed' && reportStageVisible.value) return '研究节点已完成，报告生成未完成'
  if (investigation.value?.status === 'paused' && reportStageVisible.value) return '调查脉络已完成，报告生成已暂停'
  if (reportStageVisible.value) return '调查脉络已完成，正在生成报告'
  if (investigation.value?.status === 'paused') return '调查已暂停'
  if (investigation.value?.status === 'failed') return '调查未能完成'
  if (investigation.value?.status === 'cancelled') return '调查已取消'
  return '调查正在进行'
})
const investigationStatusLabel = computed(() => reportStageVisible.value && investigation.value?.status === 'running'
  ? '生成报告中'
  : reportStageVisible.value && investigation.value?.status === 'failed'
  ? '报告未完成'
  : statusLabel(investigation.value?.status || ''))
const progressCaption = computed(() => reportStageVisible.value
  ? `研究节点 ${completedNodes.value} / ${nodes.value.length} 已完成`
  : nodes.value.length
  ? `已完成 ${completedNodes.value} / ${nodes.value.length} 个节点`
  : investigation.value?.status === 'running' ? '正在生成本次调查的节点结构' : '等待开始调查')
const reportGenerationComplete = computed(() => reportProgress.value.status === 'completed'
  || reportAvailable.value
  || investigation.value?.status === 'completed')
const reportProgressHeadline = computed(() => {
  if (reportGenerationComplete.value) return '调查报告已生成'
  if (investigation.value?.status === 'paused') return '调查已暂停，报告生成等待恢复'
  if (reportProgress.value.status === 'planning' || reportProgress.value.status === 'not_started') return '正在整理调查脉络并规划报告章节'
  if (reportProgress.value.status === 'failed') return '报告生成遇到问题'
  if (reportProgress.value.total_sections > 0 && reportProgress.value.completed_sections === reportProgress.value.total_sections) return '报告章节已完成，正在汇总与发布'
  return '正在撰写报告章节'
})
const reportProgressCaption = computed(() => {
  if (reportGenerationComplete.value) {
    return reportProgress.value.total_sections
      ? `已完成 ${reportProgress.value.total_sections} / ${reportProgress.value.total_sections} 个章节，可在“最终报告”中查看`
      : '最终报告已保存，可在“最终报告”中查看'
  }
  return reportProgress.value.total_sections
    ? `已完成 ${reportProgress.value.completed_sections} / ${reportProgress.value.total_sections} 个章节`
    : '报告任务准备中，研究结果已保存'
})

function currentDraft(node?: api.InvestigationNode): api.NodeDraft {
  const history = node?.revision_history || []
  return history.find(item => item.revision === node?.current_revision)?.draft || history.at(-1)?.draft || { conclusion: '', claims: [], evidence: [], media: [] }
}

function conclusionPreview(node: api.InvestigationNode) {
  const ir = currentDraft(node).conclusion_ir
  if (!ir || ir.schema_version !== 'conclusion-ir.v1') return ''

  const parts = ir.blocks.flatMap(block => {
    if (block.type === 'heading' || block.type === 'paragraph') return [block.text]
    if (block.type === 'list') {
      return block.items.map((item, index) => block.ordered ? `${index + 1}. ${item}` : `• ${item}`)
    }
    const tableLabel = block.title || block.columns.join('、')
    const firstRow = block.rows[0]?.join('；')
    return [firstRow ? `${tableLabel}：${firstRow}` : tableLabel]
  })
  const preview = parts.map(displayText).filter(Boolean).join(' · ')
  return preview.length > 320 ? `${preview.slice(0, 320).trimEnd()}…` : preview
}

async function loadRuns(reset = true) {
  if (reset) {
    loadingRuns.value = true
    runOffset.value = 0
  } else loadingMore.value = true
  try {
    const result = await api.listInvestigations(PAGE_SIZE, runOffset.value, true)
    if (reset) runs.value = result.data.runs
    else runs.value = [...runs.value, ...result.data.runs]
    hasMoreRuns.value = result.data.has_more
    runOffset.value = result.data.next_offset ?? result.data.offset + result.data.runs.length
  } finally {
    loadingRuns.value = false
    loadingMore.value = false
  }
}

async function reloadRuns() {
  await loadRuns()
  if (selectedId.value && !runs.value.some(item => item.run_id === selectedId.value)) await selectRun(runs.value[0]?.run_id || '')
}

async function loadMoreRuns() { await loadRuns(false) }

async function selectRun(runId: string) {
  stopPolling()
  selectedId.value = runId
  investigation.value = undefined
  nodes.value = []
  trace.value = undefined
  traceReview.value = undefined
  reportAvailable.value = false
  reportProgress.value = { exists: false, status: 'not_started', completed_sections: 0, total_sections: 0, sections: [] }
  traceError.value = ''
  if (!runId) return
  loadingDetail.value = true
  loadingNodes.value = true
  try {
    const [detailResult, nodeResult] = await Promise.all([
      api.getInvestigation(runId),
      api.getInvestigationNodes(runId, true),
    ])
    if (selectedId.value !== runId) return
    investigation.value = detailResult.data.run
    nodes.value = nodeResult.data.nodes
    syncRunSummary(detailResult.data.run)
    if (nodesAreFrozen()) {
      await refreshReportProgress(runId)
      await checkReport(runId)
      if (activeTab.value === 'trace') await loadTrace(runId, true)
    }
    if (detailResult.data.run.status === 'running') startPolling(runId)
  } catch (error: any) {
    if (selectedId.value === runId) ElMessage.error(error?.response?.data?.detail || '调查信息加载失败')
  } finally {
    if (selectedId.value === runId) {
      loadingDetail.value = false
      loadingNodes.value = false
    }
  }
}

function syncRunSummary(detail: api.InvestigationSummary) {
  runs.value = runs.value.map(item => item.run_id === detail.run_id ? { ...item, ...detail } : item)
}

async function refreshSelected(runId = selectedId.value) {
  if (!runId || selectedId.value !== runId) return
  try {
    const [detailResult, nodeResult] = await Promise.all([
      api.getInvestigation(runId, true),
      api.getInvestigationNodes(runId, true),
    ])
    if (selectedId.value !== runId) return
    investigation.value = detailResult.data.run
    nodes.value = nodeResult.data.nodes
    syncRunSummary(detailResult.data.run)
    pollingError.value = ''
    if (nodesAreFrozen() || reportProgress.value.exists) await refreshReportProgress(runId)
    if (['completed', 'failed', 'cancelled', 'paused'].includes(detailResult.data.run.status)) {
      stopPolling()
      if (detailResult.data.run.status === 'completed') await checkReport(runId)
    }
    if (activeTab.value === 'trace' && nodesAreFrozen() && !trace.value) await loadTrace(runId, true)
  } catch {
    pollingError.value = '进度暂时无法更新，系统会继续尝试连接。'
  }
}

async function checkReport(runId: string) {
  try {
    const result = await api.getReport(runId, 'json', true)
    if (selectedId.value === runId) reportAvailable.value = Boolean(result.data?.report)
  } catch {
    if (selectedId.value === runId) reportAvailable.value = false
  }
}

async function refreshReportProgress(runId: string) {
  try {
    const result = await api.getReportProgress(runId, true)
    if (selectedId.value === runId) reportProgress.value = result.data
  } catch {
    // Keep node progress visible while report task details reconnect.
  }
}

async function loadTrace(runId = selectedId.value, silent = false) {
  if (!runId) return
  traceLoading.value = true
  traceError.value = ''
  try {
    const result = await api.getTrace(runId, true)
    if (selectedId.value === runId) {
      trace.value = result.data.trace
      traceReview.value = result.data.review
    }
  } catch (error: any) {
    const message = error?.response?.data?.detail || ''
    if (selectedId.value === runId && !message.includes('no completed frozen node Trace')) {
      traceError.value = message || '事件脉络暂时无法加载。'
    }
    if (!silent && message) ElMessage.error(message)
  } finally {
    if (selectedId.value === runId) traceLoading.value = false
  }
}

async function create() {
  if (!form.objective.trim()) return ElMessage.warning('请输入调查目标')
  creating.value = true
  try {
    const objective = form.objective.trim()
    const documents = form.documents.filter(item => item.content.trim()).map((item, index) => ({
      source: item.source?.trim() || 'user-supplied',
      title: item.title?.trim() || `用户材料 ${index + 1}`,
      content: item.content.trim(),
      ...(item.url?.trim() ? { url: item.url.trim() } : {}),
    }))
    const result = await api.createInvestigation({
      objective,
      investigation_type: form.investigation_type,
      subject: form.subject.trim() || undefined,
      documents,
    })
    dialogVisible.value = false
    await loadRuns()
    const created = result.data.run
    await selectRun(created.run_id)
    await api.runInvestigation(created.run_id)
    await refreshSelected(created.run_id)
    startPolling(created.run_id)
    ElMessage.success('调查已开始')
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || '调查启动失败')
  } finally {
    creating.value = false
  }
}

async function performAction(action: () => Promise<unknown>, message: string) {
  if (!selectedId.value) return
  actionLoading.value = true
  try {
    await action()
    await refreshSelected()
    if (investigation.value?.status === 'running') startPolling(selectedId.value)
    ElMessage.success(message)
  } finally {
    actionLoading.value = false
  }
}

async function runSelected() { await performAction(() => api.runInvestigation(selectedId.value), '调查已开始') }
async function pauseSelected() { await performAction(() => api.pauseInvestigation(selectedId.value), '调查已暂停') }
async function resumeSelected() { await performAction(() => api.resumeInvestigation(selectedId.value), '调查已恢复') }
async function cancelSelected() { await performAction(() => api.cancelInvestigation(selectedId.value), '调查已取消') }
async function handleRunCommand(command: string) { if (command === 'cancel') await cancelSelected() }

function startPolling(runId: string) {
  stopPolling()
  pollingError.value = ''
  pollTimer = setInterval(() => {
    if (selectedId.value === runId && !pollInFlight) {
      pollInFlight = true
      void refreshSelected(runId).finally(() => { pollInFlight = false })
    }
  }, 5000)
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = undefined
  pollInFlight = false
}

function openNode(node: api.InvestigationNode) {
  selectedNode.value = node
  nodeDrawerVisible.value = true
}

function reviewForRound(node: api.InvestigationNode, roundNo: number) {
  return node.revision_history.find(item => item.revision === roundNo)?.review || null
}

function reviewExists(node: api.InvestigationNode, roundNo: number) { return Boolean(reviewForRound(node, roundNo)) }

function reviewStepClass(node: api.InvestigationNode, roundNo: number) {
  if (reviewExists(node, roundNo)) return 'complete'
  const nextRound = Math.min((node.current_revision || 0) + 1, 3)
  if (node.status !== 'pending' && node.status !== 'frozen' && roundNo === nextRound) return 'current'
  return ''
}

function nodeStatusLabel(node: api.InvestigationNode) {
  if (node.status === 'frozen') return '已完成'
  if (node.status === 'pending') return '待开始'
  if (node.status === 'researching') return '研究中'
  if (node.status === 'reviewing') return node.current_revision === 0 ? '初稿待审' : '修订待审'
  if (node.status === 'revise') return `第${Math.min(node.current_revision + 1, 3)}轮补充中`
  if (node.status === 'blocked') return '等待处理'
  if (node.status === 'failed') return '未完成'
  return node.status
}

function nodeStatusClass(status: string) {
  if (status === 'frozen') return 'node-complete'
  if (status === 'failed' || status === 'blocked') return 'node-problem'
  if (status === 'pending') return 'node-pending'
  return 'node-active'
}

function nodeStatusType(status: string) {
  return status === 'frozen' ? 'success' : status === 'failed' || status === 'blocked' ? 'danger' : status === 'pending' ? 'info' : 'warning'
}

function evidenceForClaim(draft: api.NodeDraft, claim: api.NodeClaim) {
  const links = new Set(claim.evidence_ids || [])
  return (draft.evidence || []).filter(item => links.has(item.evidence_id))
}

function displayText(value?: unknown) {
  let text = String(value || '')
  for (const node of nodes.value) {
    if (node.slot_id) text = text.replaceAll(node.slot_id, node.title)
  }
  return text
    .replace(/\b(?:n?claim|doc(?:ument)?|evidence|node|run|trace|template|revision|review|event)[_-][A-Za-z0-9][A-Za-z0-9_-]{5,}\b/gi, '')
    .replace(/\s{2,}/g, ' ')
    .replace(/\s+([，。；：、,.!?])/g, '$1')
    .trim()
}

function reviewSummary(review: api.NodeReview | null) {
  if (!review) return ''
  const notes = [review.final_editor_note, ...(review.rewrite_instructions || []), ...(review.evidence_followups || []), ...(review.next_research_tasks || [])]
    .map(item => String(item || '').trim())
    .filter(Boolean)
  return [...new Set(notes)].slice(0, 3).join(' ')
}

function claimTypeLabel(value: string) {
  const labels: Record<string, string> = { observation: '观察', attribution: '主体归属', uncertainty: '不确定性', fact: '事实', scope: '范围', spread: '传播', viewpoint: '观点', metric: '指标', action: '行动', response: '响应', result: '结果', status: '状态', statement: '声明', comparison: '对照', boundary: '边界', change: '变化' }
  return labels[value] || '研究主张'
}

function relationLabel(value: string) {
  return ({ supports: '支持该主张', contradicts: '与主张存在差异', qualifies: '限定主张范围' } as Record<string, string>)[value] || '相关依据'
}

function sourceHost(value: string) {
  try { return new URL(value).hostname.replace(/^www\./, '') } catch { return '查看来源' }
}

function safeUrl(value?: unknown) {
  const text = String(value || '')
  try { return ['http:', 'https:'].includes(new URL(text).protocol) ? text : undefined } catch { return undefined }
}

function statusLabel(value: string) {
  return ({ created: '待开始', running: '进行中', paused: '已暂停', completed: '已完成', failed: '未完成', cancelled: '已取消' } as Record<string, string>)[value] || value
}

function runStatusLabel(item: api.InvestigationSummary) {
  if (item.run_id !== selectedId.value || !reportStageVisible.value) return statusLabel(item.status)
  if (item.status === 'running') return '生成报告中'
  if (item.status === 'failed') return '报告未完成'
  if (item.status === 'paused') return '报告已暂停'
  return statusLabel(item.status)
}

function statusType(value: string) {
  return value === 'completed' ? 'success' : value === 'failed' || value === 'cancelled' ? 'danger' : value === 'running' ? 'primary' : value === 'paused' ? 'warning' : 'info'
}

function reportSectionStatusLabel(status: string) {
  return ({ queued: '排队中', generating: '撰写中', completed: '已完成', failed: '待重试', cancelled: '已取消' } as Record<string, string>)[status] || '等待中'
}

function investigationTypeLabel(value: string) {
  return investigationTypes.find(item => item.value === value)?.label || '调查'
}

function friendlyError(value: string) {
  const text = String(value || '').trim()
  if (!text) return ''
  if (/model_content_rejected|context_length|timeout|provider|redis|mysql|traceback|exception/i.test(text)) return '本次调查遇到运行问题，已有节点和研究结果已保留。可稍后继续调查或联系管理员检查运行状态。'
  return text
}

function formatDate(value?: string) {
  if (!value) return '刚刚'
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) ? value : `${value}+08:00`
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function settingOptionLabel(item: api.SettingItem, option: string) {
  if (item.key === 'ECHOTRACE_PERSISTENCE' && option === 'mysql') return 'MySQL：正式持久化'
  if (['ECHOTRACE_PUBLIC_FETCH_ENABLED', 'ECHOTRACE_REDIS_TLS', 'S3_USE_TLS'].includes(item.key) && option === '1') return '开启'
  if (['ECHOTRACE_PUBLIC_FETCH_ENABLED', 'ECHOTRACE_REDIS_TLS', 'S3_USE_TLS'].includes(item.key) && option === '0') return '关闭'
  if (item.key === 'ECHOTRACE_LLM_THINKING' && option === '1') return '开启（更深入）'
  if (item.key === 'ECHOTRACE_LLM_THINKING' && option === '0') return '关闭（更快）'
  return option
}

async function openSettings() {
  settingsVisible.value = true
  settingsLoading.value = true
  try {
    const result = await api.getSettings()
    settingItems.value = result.data.settings
    Object.keys(settingValues).forEach(key => delete settingValues[key])
    result.data.settings.forEach(item => { settingValues[item.key] = item.secret ? '' : item.value })
  } finally { settingsLoading.value = false }
}

async function saveSettingsForm() {
  settingsSaving.value = true
  try {
    const values = Object.fromEntries(settingItems.value
      .filter(item => !item.secret || settingValues[item.key]?.trim())
      .map(item => [item.key, settingValues[item.key] || '']))
    await api.saveSettings(values)
    settingsVisible.value = false
    ElMessage.success('设置已保存，运行时已重载')
  } finally { settingsSaving.value = false }
}

function addMaterial() { form.documents.push({ source: 'user-supplied', title: '', content: '', url: '' }) }
function removeMaterial(index: number) { form.documents.splice(index, 1) }
async function handleMaterialFile(file: any) {
  if (!file?.raw) return
  try { form.documents.push({ source: 'user-upload', title: file.name || '导入材料', content: await file.raw.text(), url: '' }) }
  catch { ElMessage.error('无法读取材料文件') }
}
function resetForm() { form.objective = ''; form.subject = ''; form.investigation_type = 'auto'; form.documents = [] }
async function handleTabChange(name: string | number) {
  if (String(name) === 'trace' && !trace.value && nodesAreFrozen()) await loadTrace(selectedId.value, true)
}

function nodesAreFrozen() {
  return nodes.value.length > 0 && nodes.value.every(node => node.status === 'frozen')
}

let pollTimer: ReturnType<typeof setInterval> | undefined
let pollInFlight = false

onMounted(async () => {
  await loadRuns()
  if (runs.value[0]) await selectRun(runs.value[0].run_id)
})
onUnmounted(stopPolling)
</script>

<style scoped>
.workbench-page { min-height: 100vh; overflow-x: hidden; background: #f5f7f6; color: #1e2b2b; }
.app-header { position: relative; height: 60px; padding: 0 28px; display: flex; align-items: center; justify-content: space-between; background: #fff; border-bottom: 1px solid #e4e9e6; }
.brand { display: inline-flex; align-items: center; gap: 10px; color: #183b3b; font-size: 15px; font-weight: 700; text-decoration: none; }
.brand-mark { width: 28px; height: 28px; display: grid; place-items: center; border-radius: 6px; background: #2d6f69; color: #fff; font-size: 15px; }
.header-actions, .heading-actions, .report-exports { display: flex; align-items: center; gap: 8px; }
.header-action { flex: 0 0 auto; }
.workspace-layout { display: grid; grid-template-columns: 268px minmax(0, 1fr); min-height: calc(100vh - 60px); max-width: 1600px; margin: 0 auto; }
.investigation-list { padding: 22px 14px; background: #fff; border-right: 1px solid #e4e9e6; }
.list-heading { display: flex; align-items: center; justify-content: space-between; padding: 0 8px 13px; }
.list-heading h2 { margin: 0; font-size: 14px; }
.run-list { display: grid; gap: 2px; }
.run-item { width: 100%; min-width: 0; padding: 12px 10px; border: 0; border-radius: 5px; background: transparent; text-align: left; cursor: pointer; }
.run-item:hover { background: #f4f7f5; }
.run-item.selected { background: #eaf3f0; box-shadow: inset 3px 0 #3c7b6d; }
.run-item-title { display: block; overflow: hidden; color: #263837; font-size: 13px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.run-item-meta { display: flex; align-items: center; gap: 6px; margin-top: 6px; color: #7b8986; font-size: 11px; }
.status-dot { width: 7px; height: 7px; border-radius: 50%; background: #a5b0ad; }
.status-running { background: #3c8290; box-shadow: 0 0 0 3px #e3f0f1; }
.status-completed { background: #39816c; }
.status-failed, .status-cancelled { background: #c05252; }
.status-paused { background: #c18a35; }
.load-more { width: 100%; margin-top: 8px; }
.investigation-main { min-width: 0; padding: 30px clamp(20px, 4vw, 58px) 56px; }
.investigation-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding-bottom: 23px; border-bottom: 1px solid #dfe6e2; }
.heading-copy { min-width: 0; }
.eyebrow { margin: 0 0 8px; color: #518077; font-size: 11px; font-weight: 700; }
.investigation-heading h1 { margin: 0; color: #1d3030; font-size: 25px; line-height: 1.35; overflow-wrap: anywhere; }
.objective { max-width: 820px; margin: 10px 0 0; color: #586965; font-size: 14px; line-height: 1.65; }
.subject { margin: 7px 0 0; color: #7a8985; font-size: 12px; }
.heading-actions { flex-shrink: 0; }
.progress-overview { margin: 22px 0 12px; padding: 17px 0 15px; border-bottom: 1px solid #e1e7e3; }
.progress-title-row { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.progress-title-row > div { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px; }
.progress-title-row strong { color: #253c39; font-size: 14px; }
.progress-title-row span { color: #7a8985; font-size: 12px; }
.progress-title-row .progress-percent { color: #376f66; font-size: 13px; font-weight: 700; }
.summary-stats { display: flex; flex-wrap: wrap; gap: clamp(20px, 5vw, 58px); margin-top: 17px; }
.summary-stats div { display: flex; align-items: baseline; gap: 7px; }
.summary-stats strong { color: #273a38; font-size: 18px; }
.summary-stats strong span { color: #87938f; font-size: 12px; font-weight: 500; }
.summary-stats div > span { color: #788580; font-size: 12px; }
.report-progress { margin-top: 18px; padding: 13px 14px; border-left: 3px solid #4a8b79; background: #f0f6f3; }
.report-progress-heading { display: flex; align-items: flex-start; gap: 10px; }
.report-progress-heading .planning-indicator { flex: 0 0 auto; margin-top: 3px; }
.report-progress-heading > div { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 12px; }
.report-progress-heading strong { color: #315d50; font-size: 13px; }
.report-progress-heading span:not(.planning-indicator) { color: #70827a; font-size: 12px; }
.report-section-progress { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 7px 18px; margin: 12px 0 0 21px; }
.report-section-item { display: grid; grid-template-columns: 16px minmax(0, 1fr) auto; align-items: center; gap: 7px; min-width: 0; color: #485c54; font-size: 11px; }
.report-section-marker { width: 14px; height: 14px; display: grid; place-items: center; border: 1px solid #b7c9c0; border-radius: 50%; color: #34775b; font-size: 9px; }
.report-section-marker.section-generating { border-color: #4b8790; border-right-color: transparent; animation: spin 0.9s linear infinite; }
.report-section-marker.section-completed { border-color: #92bca8; background: #e0efe7; }
.report-section-item > span:nth-child(2) { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.report-section-item small { color: #86938d; font-size: 10px; }
.status-message { margin: 15px 0; }
.content-tabs { margin-top: 12px; }
.node-progress-section { padding: 8px 0 0; }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin: 10px 0 15px; }
.section-heading h2, .report-toolbar h2 { margin: 0; color: #263a37; font-size: 16px; }
.section-heading p, .report-toolbar p { margin: 5px 0 0; color: #788580; font-size: 12px; }
.section-count { color: #85918c; font-size: 12px; }
.node-list { border-top: 1px solid #e1e7e3; }
.node-row { display: grid; grid-template-columns: 36px minmax(0, 1fr) 20px; align-items: start; gap: 14px; width: 100%; padding: 18px 6px; border: 0; border-bottom: 1px solid #e5eae7; background: transparent; text-align: left; cursor: pointer; }
.node-row:hover { background: #fbfcfb; }
.node-index { width: 30px; height: 30px; display: grid; place-items: center; border: 1px solid #d9e2dd; border-radius: 50%; color: #7d8a85; font-size: 11px; }
.node-index.node-complete { border-color: #9bc5b4; background: #eaf4ef; color: #36785d; }
.node-index.node-active { border-color: #9fc3c4; background: #edf6f5; color: #397580; }
.node-index.node-problem { border-color: #e0b3ac; background: #fcf0ee; color: #ad504b; }
.node-content { display: grid; gap: 7px; min-width: 0; }
.node-title-line { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; }
.node-title-line strong { color: #253a37; font-size: 14px; }
.node-state { color: #798680; font-size: 11px; }
.node-state.node-complete { color: #34775b; }
.node-state.node-active { color: #3e7c84; }
.node-state.node-problem { color: #b34d48; }
.node-goal { overflow: hidden; color: #77847f; font-size: 12px; line-height: 1.5; text-overflow: ellipsis; }
.review-track { display: flex; flex-wrap: wrap; gap: 6px; margin: 2px 0; }
.review-step { display: inline-flex; align-items: center; gap: 5px; color: #98a29e; font-size: 10px; }
.review-step i { width: 16px; height: 16px; display: grid; place-items: center; border: 1px solid #d8dfdb; border-radius: 50%; font-size: 9px; font-style: normal; }
.review-step.complete { color: #3e7c64; }
.review-step.complete i { border-color: #9cc4ae; background: #eaf4ee; }
.review-step.current { color: #3f7780; }
.review-step.current i { border-color: #80b1b7; background: #eaf4f5; }
.node-conclusion { display: -webkit-box; overflow: hidden; color: #53645f; font-size: 12px; line-height: 1.6; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.node-chevron { margin-top: 6px; color: #9aa59f; }
.planning-state { display: flex; align-items: center; gap: 13px; padding: 30px 10px; border-top: 1px solid #e3e9e5; border-bottom: 1px solid #e3e9e5; }
.planning-state strong { color: #374944; font-size: 13px; }
.planning-state p { margin: 5px 0 0; color: #7d8984; font-size: 12px; }
.planning-indicator { width: 11px; height: 11px; border: 2px solid #c3cec8; border-radius: 50%; }
.planning-indicator.active { border-color: #3c7b83; border-right-color: transparent; animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.trace-loading { padding-top: 20px; }
.report-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 18px; padding: 15px 0; }
.report-exports a { text-decoration: none; }
.export-link { display: inline-flex; align-items: center; gap: 6px; padding: 7px 10px; border: 1px solid #d7e0db; border-radius: 4px; color: #405750; font-size: 12px; }
.export-link:hover { border-color: #86ada1; background: #f4f8f5; color: #2d6e63; }
.report-frame { display: block; width: 100%; min-height: 76vh; border: 1px solid #dfe6e2; border-radius: 4px; background: #fff; }
.drawer-goal { margin: -12px 0 16px; color: #73817c; font-size: 13px; line-height: 1.6; }
.drawer-meta { display: flex; align-items: center; gap: 15px; padding-bottom: 15px; border-bottom: 1px solid #e5eae7; color: #788580; font-size: 12px; }
.drawer-section { margin-top: 24px; }
.drawer-section h3 { margin: 0 0 12px; color: #354944; font-size: 14px; }
.conclusion-section :deep(.conclusion-renderer) { margin-top: 4px; }
.claim-item { padding: 14px 0; border-top: 1px solid #e7ece9; }
.claim-heading { display: flex; align-items: center; gap: 8px; color: #52736b; font-size: 11px; }
.claim-heading > span { color: #a0aaa5; font-variant-numeric: tabular-nums; }
.claim-item > p { margin: 7px 0 0; color: #2e413c; font-size: 13px; line-height: 1.7; }
.evidence-quote { margin: 10px 0 0 20px; padding: 9px 12px; border-left: 2px solid #aac9bd; background: #f5f8f6; }
.evidence-quote blockquote { margin: 0; color: #56655f; font-size: 12px; line-height: 1.65; }
.evidence-quote a, .evidence-quote > span, .media-row a { display: inline-flex; align-items: center; gap: 5px; margin-top: 6px; color: #34766e; font-size: 11px; text-decoration: none; }
.review-detail { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 10px; padding: 11px 0; border-top: 1px solid #e7ece9; }
.review-number { width: 22px; height: 22px; display: grid; place-items: center; border: 1px solid #d8e0dc; border-radius: 50%; color: #89958f; font-size: 10px; }
.review-detail.done .review-number { border-color: #9cc4ae; background: #eaf4ee; color: #39775d; }
.review-detail strong { color: #435750; font-size: 12px; }
.review-detail p { margin: 5px 0 0; color: #687771; font-size: 12px; line-height: 1.6; }
.review-detail .review-pending { color: #a0aaa5; }
.media-row { padding: 10px 0; border-top: 1px solid #e7ece9; }
.media-row small { display: block; margin-top: 4px; color: #7e8a85; }
.material-toolbar { display: flex; gap: 8px; margin-bottom: 10px; }
.material-row { margin-top: 10px; padding: 10px; border: 1px solid #e2e8e4; border-radius: 5px; }
.material-row-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; color: #65736d; font-size: 12px; }
.material-title, .material-url { margin-bottom: 8px; }
.settings-form { max-height: 60vh; overflow: auto; margin-top: 18px; }
.settings-group { margin-top: 18px; padding-top: 18px; border-top: 1px solid #e5eae7; }
.settings-group:first-child { margin-top: 0; padding-top: 0; border-top: 0; }
.settings-group h3 { margin: 0 0 12px; font-size: 14px; }
.setting-description { margin: -4px 0 7px; color: #7b8882; font-size: 12px; }
@media (max-width: 900px) {
  .workspace-layout { grid-template-columns: 225px minmax(0, 1fr); }
  .investigation-main { padding: 24px 22px 40px; }
  .investigation-heading { flex-direction: column; }
}
@media (max-width: 680px) {
  .app-header { height: 54px; padding: 0 12px; }
  .brand > span:last-child { display: none; }
  .header-actions { position: fixed; z-index: 10; top: 9px; left: 80px; right: auto; gap: 6px; flex-shrink: 0; }
  .header-action { width: 36px; min-width: 36px; height: 36px; padding: 0; font-size: 0; }
  .header-action :deep(.el-icon) { margin: 0; }
  .header-action :deep(.el-icon) { font-size: 16px; }
  .header-action :deep(span) { display: none; }
  .workspace-layout { display: flex; flex-direction: column; min-height: calc(100vh - 54px); }
  .investigation-list { padding: 10px 12px; border-right: 0; border-bottom: 1px solid #e4e9e6; }
  .list-heading { padding: 0 3px 8px; }
  .run-list { display: flex; overflow-x: auto; gap: 6px; padding-bottom: 2px; }
  .run-item { flex: 0 0 210px; width: 210px; padding: 9px; border: 1px solid #e5ebe7; }
  .run-item.selected { border-color: #9dbeb1; box-shadow: inset 0 -2px #3c7b6d; }
  .load-more { flex: 0 0 130px; }
  .investigation-main { padding: 21px 15px 34px; }
  .investigation-heading h1 { font-size: 21px; }
  .heading-actions { flex-wrap: wrap; }
  .summary-stats { gap: 13px 22px; }
  .summary-stats div { min-width: calc(50% - 12px); }
  .node-row { grid-template-columns: 30px minmax(0, 1fr) 16px; gap: 9px; padding: 15px 2px; }
  .node-index { width: 27px; height: 27px; }
  .node-title-line { align-items: flex-start; }
  .review-track { gap: 7px 11px; }
  .review-step span { font-size: 9px; }
  .report-toolbar { align-items: flex-start; flex-direction: column; }
  .report-exports { flex-wrap: wrap; }
  .report-frame { min-height: 70vh; }
}
</style>
