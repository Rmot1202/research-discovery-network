export type Paper = {
  arxiv_id: string
  url: string
  title: string
  abstract: string
  authors: string[]
  published_at: string
  categories: string[]
  primary_category: string
  topic: string
  institution: string | null
  country: string | null
  discovery_score: number | null
  discovery?: Discovery
  summary?: string | null
  key_contribution?: string | null
  application?: string | null
  relevance?: string | null
  topics?: string | null
  methods?: string | null
  context?: string | null
  topic_cluster?: string | null
  affiliation_source_url?: string | null
  affiliation_evidence?: string | null
  affiliation_method?: 'openalex' | 'pdf_text' | 'manual' | null
  affiliation_reviewed?: boolean | null
  institutions_all?: string[] | null
  countries_all?: string[] | null
  has_school_affiliation?: boolean | null
  approval_status: string
}

export type Discovery = {
  discovery_score: number | null
  factors: { topic: number | null; institution: number | null; geography: number | null }
  topic_basis: 'subtopic' | 'category' | null
  missing?: Partial<Record<'topic' | 'institution' | 'geography', 'not_documented' | 'small_field'>>
  weights: { topic: number; institution: number; geography: number }
  coverage: number
  flags: ('topic' | 'institution' | 'geography')[]
  reasons: string[]
  underrepresented: boolean
  measures: 'representation'
}

export type Topic = { code: string; label: string; count: number }
export type TopicLink = { source: string; target: string; shared_papers: number }
export type RelatedPaper = Paper & {
  similarityScore: number
  sharedCategories: string[]
  sharedTerms: string[]
}
export type IngestProgress = {
  status: 'not_started' | 'running' | 'paused' | 'complete' | 'error'
  total_records: number
  records_processed: number
  pages_processed: number
  ingested_papers: number
  error: string | null
}
export type AdminOverview = {
  total: number
  pending: number
  approved: number
  rejected: number
  documented_institutions: number
  documented_countries: number
  embedded: number
  ai_configured: boolean
  publish_mode?: 'review' | 'auto'
}
export type PaperDraft = Pick<Paper, 'summary' | 'key_contribution' | 'application' | 'relevance'>
export type AffiliationDraft = { institution: string; country: string; source_url: string; evidence: string }
export type AffiliationSuggestion = { institution: string; country: string | null; evidence: string; source_url: string }
export type ChatPaper = {
  arxiv_id: string
  title: string
  abstract: string
  authors: string[]
  published_at: string
  primary_category: string
  topic: string
  url: string
  summary?: string | null
  relevance: number
}
export type ChatResponse = {
  answer: string | null
  papers: ChatPaper[]
  retrieval: string
  ai_configured: boolean
  notice?: string
}

export type GapComponentKey = 'scarcity' | 'momentum_gap' | 'synthesis_gap' | 'isolation' | 'concentration'
export type Representation = {
  index: number | null
  verdict: string
  components: { country_concentration: number | null; institution_concentration: number | null }
  documented_papers: number
  documented_pct: number
  min_documented: number
  top_countries: { name: string; papers: number }[]
  top_institutions: { name: string; papers: number }[]
}
export type GapScore = {
  index: number
  verdict: string
  confidence: 'low' | 'medium' | 'high'
  components: Record<GapComponentKey, number>
  drivers: string[]
  representation: Representation
  metrics: {
    papers: number
    share_of_archive_pct: number
    recent_24_months: number
    previous_24_months: number
    growth_ratio: number
    surveys: number
    expected_surveys: number
    cross_listed_pct: number
    top5_author_share_pct: number
    distinct_authors: number
  }
}
export type GapMethodology = {
  weights: Record<GapComponentKey, number>
  component_labels: Record<GapComponentKey, string>
  archive_papers: number
  latest_paper_date: string
  recent_window_start: string
  previous_window_start: string
  archive_survey_rate_pct: number
  coverage_note: string | null
}
export type GapTopic = GapScore & { code: string; label: string }
export type GapReport = { methodology: GapMethodology; topics: GapTopic[] }
export type IdeaGapReport = GapScore & {
  query: string
  keywords: string[]
  category: string | null
  topic_percentile: number | null
  main_topics: { code: string; label: string; papers: number }[]
  sample_papers: { arxiv_id: string; title: string; year: string; topic: string; url: string }[]
  methodology: GapMethodology
}
export type AffiliationScanProgress = {
  status: 'not_started' | 'running' | 'paused' | 'complete' | 'error'
  cursor: string
  checked: number
  found: number
  found_openalex: number
  found_pdf: number
  remaining: number | null
  pdf_fallback: boolean
  error: string | null
  updated_at: string | null
  last_found: string[]
  total_papers: number
  documented_papers: number
  documented_countries: number
  documented_pct: number
  is_running: boolean
}

export type DrillPaper = {
  arxiv_id: string
  title: string
  authors: string[]
  author_count: number
  published_at: string
  institution: string | null
  country: string | null
  url: string
  summary?: string | null
  key_contribution?: string | null
  relevance?: string | null
  abstract: string
}
export type Subtopic = { id: string; label: string; terms: string[]; size: number; papers: DrillPaper[] }
export type TopicDrilldown = {
  code: string
  label: string
  total_papers: number
  source: 'embeddings' | 'keywords'
  clusters: Subtopic[]
}
export type MapCommand = { type: 'select' | 'drill' | 'paper' | 'exit'; code?: string; id: number }
