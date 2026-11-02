import type { FormEvent } from 'react'
import type {
  AdminOverview,
  AffiliationDraft,
  AffiliationSuggestion,
  IngestProgress,
  Paper,
  PaperDraft,
} from '../types'
import AffiliationScanPanel from './AffiliationScanPanel'
import { ArrowIcon } from './Icons'
import { DiscoveryBreakdown } from './DiscoveryScore'

type AdminDashboardProps = {
  isOpen: boolean
  token: string
  tokenInput: string
  overview: AdminOverview | null
  papers: Paper[]
  error: string
  notice: string
  busyId: string
  analysisBusy: boolean
  drafts: Record<string, PaperDraft>
  affiliationDrafts: Record<string, AffiliationDraft>
  affiliationSuggestions: Record<string, AffiliationSuggestion[]>
  ingestProgress: IngestProgress | null
  isStartingIngest: boolean
  onClose: () => void
  onTokenInputChange: (value: string) => void
  onLogin: (token: string) => void
  onStartIngest: () => void
  onPauseIngest: () => void
  onAnalyze: () => void
  onGenerateSummary: (paper: Paper) => void
  onUpdateReview: (paper: Paper, status?: 'approved' | 'rejected' | 'pending') => void
  onSaveAffiliation: (paper: Paper) => void
  onExtractAffiliation: (paper: Paper) => void
  onPaperDraftChange: (paperId: string, field: keyof PaperDraft, value: string) => void
  onAffiliationDraftChange: (paperId: string, field: keyof AffiliationDraft, value: string) => void
  onUseAffiliationSuggestion: (paperId: string, suggestion: AffiliationSuggestion) => void
  onAffiliationsChanged?: () => void
}

export default function AdminDashboard(props: AdminDashboardProps) {
  const {
    isOpen, token, tokenInput, overview, papers, error, notice, busyId, analysisBusy,
    drafts, affiliationDrafts, affiliationSuggestions, ingestProgress, isStartingIngest,
    onClose, onTokenInputChange, onLogin, onStartIngest, onPauseIngest, onAnalyze,
    onGenerateSummary, onUpdateReview, onSaveAffiliation, onExtractAffiliation,
    onPaperDraftChange, onAffiliationDraftChange, onUseAffiliationSuggestion, onAffiliationsChanged,
  } = props
  if (!isOpen) return null

  const login = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    onLogin(tokenInput)
  }

  return (
    <section className="admin-dashboard" id="admin">
      <div className="paper-explorer-heading">
        <div><span className="eyebrow">Internal workspace</span><h2>Research review dashboard</h2><p>Inspect transparent scores, verify sourced metadata, generate summaries, and decide what becomes public.</p></div>
        <button className="admin-close" onClick={onClose}>Close dashboard</button>
      </div>
      {!token ? (
        <form className="admin-login" onSubmit={login}>
          <label>Admin access token<input type="password" autoComplete="current-password" value={tokenInput} onChange={(event) => onTokenInputChange(event.target.value)} placeholder="Token from your local ADMIN_API_TOKEN setting" /></label>
          <button className="primary" type="submit" disabled={!tokenInput}>Open review queue <ArrowIcon /></button>
        </form>
      ) : (
        <>
          {overview && (
            <div className="admin-stats">
              <span><strong>{overview.pending.toLocaleString()}</strong> pending</span>
              <span><strong>{overview.approved.toLocaleString()}</strong> {overview.publish_mode === 'auto' ? 'featured (auto-publish on)' : 'approved'}</span>
              <span><strong>{overview.embedded.toLocaleString()}</strong> embedded</span>
              <span><strong>{overview.documented_institutions.toLocaleString()}</strong> documented institutions</span>
              <span><strong>{overview.documented_countries.toLocaleString()}</strong> documented countries</span>
            </div>
          )}
          <div className="analysis-controls">
            <div><strong>Archive ingestion</strong><p>{ingestProgress?.records_processed.toLocaleString() ?? '0'} records scanned · {ingestProgress?.ingested_papers.toLocaleString() ?? '0'} unique papers saved · Status: {ingestProgress?.status ?? 'unknown'}{ingestProgress?.error ? ` · ${ingestProgress.error}` : ''}</p></div>
            {ingestProgress?.status !== 'complete' && (
              <button className="primary" onClick={onStartIngest} disabled={isStartingIngest || ingestProgress?.status === 'running'}>
                {isStartingIngest ? 'Starting…' : ingestProgress?.status === 'running' ? 'Import running' : ingestProgress?.status === 'error' || ingestProgress?.status === 'paused' ? 'Resume from saved cursor' : 'Ingest all arXiv CS papers'}
              </button>
            )}
            {ingestProgress?.status === 'running' && <button className="admin-close" onClick={onPauseIngest}>Pause import</button>}
          </div>
          <div className="analysis-controls">
            <div><strong>AI analysis</strong><p>{overview?.ai_configured ? 'Provider configured. Embedding and clustering are available.' : 'Configure the AI provider, chat model, and embedding model in your local environment to enable AI.'}</p></div>
            <button className="primary" onClick={onAnalyze} disabled={analysisBusy || !overview?.ai_configured}>
              {analysisBusy ? 'Embedding and clustering…' : 'Analyze next 20 papers'}
            </button>
          </div>
          <AffiliationScanPanel token={token} onFound={onAffiliationsChanged} />
          {notice && <p className="admin-notice" role="status">{notice}</p>}
          {error && <p className="admin-error" role="alert">{error}</p>}
          <div className="admin-queue">
            {papers.map((paper) => {
              const draft = drafts[paper.arxiv_id] ?? {}
              const affiliation = affiliationDrafts[paper.arxiv_id] ?? { institution: '', country: '', source_url: '', evidence: '' }
              const isBusy = busyId === paper.arxiv_id
              return (
                <article className="admin-paper" key={paper.arxiv_id}>
                  <div className="admin-paper-heading">
                    <div><span>{paper.topic} · {paper.arxiv_id}</span><h3>{paper.title}</h3><p>{paper.authors.join(' · ')} · {paper.published_at.slice(0, 4)}</p></div>
                    <strong>{paper.discovery_score === null ? '—' : paper.discovery_score.toFixed(1)}<small> Discovery Score</small></strong>
                  </div>
                  <p className="admin-abstract">{paper.abstract}</p>
                  <DiscoveryBreakdown discovery={paper.discovery} field={paper.primary_category} />
                  <p className="score-explanation">Weights: topic 40%, institution 30%, country 30%. Factors without documented data are left out and the rest are rescaled. This is a representation score, not a quality or visibility score.</p>
                  {(paper.topics || paper.methods || paper.context) && (
                    <div className="ai-tags">
                      {paper.topics && <span>Research topics: {paper.topics}</span>}
                      {paper.methods && <span>Methods: {paper.methods}</span>}
                      {paper.context && <span>Context: {paper.context}</span>}
                    </div>
                  )}
                  <div className="admin-summary-fields">
                    {([
                      ['summary', 'Plain-language summary'],
                      ['key_contribution', 'Key contribution'],
                      ['application', 'Potential application'],
                      ['relevance', 'Why it was surfaced'],
                    ] as const).map(([field, label]) => (
                      <label key={field}>{label}
                        <textarea value={draft[field] ?? ''} onChange={(event) => onPaperDraftChange(paper.arxiv_id, field, event.target.value)} rows={field === 'summary' ? 3 : 2} />
                      </label>
                    ))}
                  </div>
                  <div className="admin-actions">
                    <button onClick={() => onGenerateSummary(paper)} disabled={isBusy || !overview?.ai_configured}>{isBusy ? 'Working…' : 'Generate AI analysis'}</button>
                    <button onClick={() => onUpdateReview(paper)} disabled={isBusy}>Save summary edits</button>
                    <button className="approve-action" onClick={() => onUpdateReview(paper, 'approved')} disabled={isBusy}>{overview?.publish_mode === 'auto' ? 'Feature on homepage' : 'Approve for public site'}</button>
                    <button className="reject-action" onClick={() => onUpdateReview(paper, 'rejected')} disabled={isBusy}>Reject</button>
                  </div>
                  <details className="affiliation-editor">
                    <summary>Document affiliation evidence</summary>
                    <p>Only enter institution or country details supported by the paper or a cited source. Do not infer them from author names.</p>
                    <button onClick={() => onExtractAffiliation(paper)} disabled={isBusy || !overview?.ai_configured}>Suggest from arXiv PDF</button>
                    {(affiliationSuggestions[paper.arxiv_id] ?? []).map((suggestion, index) => (
                      <div className="affiliation-suggestion" key={`${suggestion.institution}-${index}`}>
                        <span><strong>{suggestion.institution}</strong>{suggestion.country ? ` · ${suggestion.country}` : ''}</span>
                        <q>{suggestion.evidence}</q>
                        <button onClick={() => onUseAffiliationSuggestion(paper.arxiv_id, suggestion)}>Review this suggestion</button>
                      </div>
                    ))}
                    <div>
                      <label>Institution<input value={affiliation.institution} onChange={(event) => onAffiliationDraftChange(paper.arxiv_id, 'institution', event.target.value)} /></label>
                      <label>Country<input value={affiliation.country} onChange={(event) => onAffiliationDraftChange(paper.arxiv_id, 'country', event.target.value)} /></label>
                      <label>Source URL<input type="url" value={affiliation.source_url} onChange={(event) => onAffiliationDraftChange(paper.arxiv_id, 'source_url', event.target.value)} /></label>
                      <label>Evidence excerpt<textarea value={affiliation.evidence} onChange={(event) => onAffiliationDraftChange(paper.arxiv_id, 'evidence', event.target.value)} rows={2} /></label>
                    </div>
                    <button onClick={() => onSaveAffiliation(paper)} disabled={isBusy}>Save sourced affiliation</button>
                    {paper.affiliation_source_url && <a href={paper.affiliation_source_url} target="_blank" rel="noreferrer">Current evidence source</a>}
                  </details>
                </article>
              )
            })}
            {!papers.length && <p className="featured-empty">The review queue is empty.</p>}
          </div>
        </>
      )}
      {error && !token && <p className="admin-error" role="alert">{error}</p>}
    </section>
  )
}
