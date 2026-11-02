import client from './client'

export interface InvestigationSummary {
  run_id: string
  objective: string
  investigation_type: string
  subject?: string | null
  status: string
  checkpoint?: string | null
  created_at: string
  updated_at: string
  event: { event_id: string; title: string; stage: string; risk_score: number }
}

export interface InvestigationDetail extends InvestigationSummary {
  scope: Record<string, unknown>
  error?: string | null
  interrupt_reason?: string | null
  state_node?: string | null
  template_id?: string | null
  template_version?: string | null
}

export interface InvestigationPage {
  success: boolean
  runs: InvestigationSummary[]
  limit: number
  offset: number
  has_more: boolean
  next_offset?: number | null
}

export interface InvestigationDocumentInput {
  source?: string
  title: string
  content: string
  url?: string
}

export interface NodeEvidence {
  evidence_id: string
  claim_id: string
  document_id: string
  excerpt: string
  locator: { url?: string; section?: string; [key: string]: unknown }
  relation: string
}

export interface NodeClaim {
  claim_id: string
  claim_type: string
  text: string
  evidence_ids: string[]
  source_document_ids?: string[]
}

export type ConclusionBlock =
  | { type: 'heading'; level: 1 | 2 | 3; text: string }
  | { type: 'paragraph'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'table'; title?: string; columns: string[]; rows: string[][] }

export interface ConclusionIR {
  schema_version: 'conclusion-ir.v1'
  blocks: ConclusionBlock[]
  source_hash?: string
}

export interface NodeDraft {
  conclusion: string
  conclusion_ir?: ConclusionIR | null
  claims: NodeClaim[]
  evidence: NodeEvidence[]
  media: Array<{
    media_id: string
    media_type: string
    caption: string
    why_relevant: string
    source_url?: string
    object_key?: string
  }>
}

export interface NodeReview {
  revision: number
  status: string
  covered_aspects?: string[]
  missing_aspects?: string[]
  evidence_followups?: string[]
  rewrite_instructions?: string[]
  next_research_tasks?: string[]
  final_editor_note?: string | null
}

export interface NodeRevision {
  revision: number
  conclusion: string
  draft: NodeDraft
  review?: NodeReview | null
  created_at?: string | null
}

export interface InvestigationNode {
  node_id: string
  run_id: string
  template_id: string
  template_version: string
  slot_id: string
  node_order: number
  instance_no: number
  status: string
  current_revision: number
  frozen_revision?: number | null
  current_review_id?: string | null
  title: string
  goal: string
  aspects: string[]
  revision_history: NodeRevision[]
}

export interface InvestigationTraceNode {
  node_id: string
  slot_id: string
  node_order: number
  title: string
  goal: string
  status: string
  frozen_revision: number
  draft: NodeDraft
  source_document_ids: string[]
}

export interface InvestigationTrace {
  trace_id: string
  run_id: string
  investigation_type: string
  subject: string
  template_id: string
  template_version: string
  nodes: InvestigationTraceNode[]
  edges: Array<{
    from_slot_id: string
    to_slot_id: string
    relation: string
    label_goal: string
  }>
}

export interface SettingItem {
  key: string
  label: string
  category: string
  description: string
  secret: boolean
  control?: 'text' | 'select'
  options?: string[]
  configured: boolean
  value: string
}

export interface ReportProgress {
  exists: boolean
  status: 'not_started' | 'planning' | 'writing' | 'completed' | 'failed'
  planning_status?: string | null
  completed_sections: number
  total_sections: number
  sections: Array<{ chapter_id: string; title: string; status: 'queued' | 'generating' | 'completed' | 'failed' | 'cancelled' }>
}

export function listInvestigations(limit = 30, offset = 0, suppressErrorMessage = false) {
  return client.get<InvestigationPage>('/api/investigations', {
    params: { limit, offset },
    suppressErrorMessage,
  })
}

export function createInvestigation(payload: {
  objective: string
  event_title?: string
  investigation_type: string
  subject?: string
  scope?: Record<string, unknown>
  documents?: InvestigationDocumentInput[]
}) {
  return client.post<{ success: boolean; run: InvestigationSummary }>('/api/investigations', payload)
}

export function getInvestigation(runId: string, suppressErrorMessage = false) {
  return client.get<{ success: boolean; run: InvestigationDetail }>(`/api/investigations/${runId}`, { suppressErrorMessage })
}

export function getInvestigationNodes(runId: string, suppressErrorMessage = false) {
  return client.get<{ success: boolean; run_id: string; nodes: InvestigationNode[] }>(`/api/investigations/${runId}/nodes`, { suppressErrorMessage })
}

export function reformatFrozenNode(runId: string, slotId: string) {
  return client.post<{ success: boolean; run_id: string; slot_id: string; revision: number; conclusion_ir: ConclusionIR }>(
    `/api/investigations/${runId}/nodes/${encodeURIComponent(slotId)}/reformat`,
  )
}

export function getTrace(runId: string, suppressErrorMessage = false) {
  return client.get<{ success: boolean; trace: InvestigationTrace; review?: { findings?: Array<{ kind: string; message: string; recommendation?: string }> } | null }>(`/api/investigations/${runId}/trace`, { suppressErrorMessage })
}

export function getReport(runId: string, format: 'json' | 'markdown' | 'html' | 'pdf' = 'json', suppressErrorMessage = false) {
  return client.get<{ success: boolean; report: Record<string, unknown> }>(`/api/investigations/${runId}/report`, {
    params: { format },
    suppressErrorMessage,
    responseType: format === 'pdf' || format === 'html' || format === 'markdown' ? 'text' : 'json',
  })
}

export function getReportProgress(runId: string, suppressErrorMessage = false) {
  return client.get<{ success: boolean } & ReportProgress>(`/api/investigations/${runId}/report/progress`, { suppressErrorMessage })
}

export function reportUrl(runId: string, format: 'html' | 'markdown' | 'pdf') {
  return `/api/investigations/${encodeURIComponent(runId)}/report?format=${format}`
}

export function getSettings() {
  return client.get<{ success: boolean; settings: SettingItem[] }>('/api/settings')
}

export function saveSettings(values: Record<string, string>) {
  return client.put<{ success: boolean; settings: SettingItem[]; restarted: boolean }>('/api/settings', { values })
}

export function runInvestigation(runId: string) {
  return client.post(`/api/investigations/${runId}/run`)
}

export function pauseInvestigation(runId: string) {
  return client.post(`/api/investigations/${runId}/pause`)
}

export function resumeInvestigation(runId: string) {
  return client.post(`/api/investigations/${runId}/resume`)
}

export function cancelInvestigation(runId: string) {
  return client.post(`/api/investigations/${runId}/cancel`)
}
