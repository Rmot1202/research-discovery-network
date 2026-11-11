import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import type {
  AdminOverview,
  AdminToday,
  AffiliationDraft,
  AffiliationSuggestion,
  IngestProgress,
  Paper,
  PaperDraft,
} from '../types'
import AffiliationScanPanel from './AffiliationScanPanel'
import { FIREBASE_SESSION, currentUser, getAuthConfig, signInWithGoogle } from '../auth'
import type { AuthConfig } from '../auth'
import { ArrowIcon } from './Icons'
import { DiscoveryBreakdown } from './DiscoveryScore'

type AdminDashboardProps = {
  isOpen: boolean
  token: string
  tokenInput: string
  overview: AdminOverview | null
  papers: Paper[]
  papersTotal: number
  today: AdminToday | null
  day: string
  source: '' | 'arxiv' | 'openalex'
  onDayChange: (day: string) => void
  onSourceChange: (source: '' | 'arxiv' | 'openalex') => void
  onLogout: () => void
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
    isOpen, token, tokenInput, overview, papers, papersTotal, today, day, source,
    onDayChange, onSourceChange, onLogout, error, notice, busyId, analysisBusy,
    drafts, affiliationDrafts, affiliationSuggestions, ingestProgress, isStartingIngest,
    onClose, onTokenInputChange, onLogin, onStartIngest, onPauseIngest, onAnalyze,
    onGenerateSummary, onUpdateReview, onSaveAffiliation, onExtractAffiliation,
    onPaperDraftChange, onAffiliationDraftChange, onUseAffiliationSuggestion, onAffiliationsChanged,
  } = props
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null)
  const [signingIn, setSigningIn] = useState(false)
  const [signInError, setSignInError] = useState('')
  useEffect(() => {
    if (!isOpen || authConfig) return
    getAuthConfig().then(async (config) => {
      setAuthConfig(config)
      // Already signed in with Firebase on this browser? Open the dashboard straight away.
      if (config.firebase && !token && (await currentUser())) onLogin(FIREBASE_SESSION)
    }).catch(() => setAuthConfig({ firebase: null, token_login: true }))
  }, [isOpen, authConfig, token, onLogin])
  if (!isOpen) return null

  const googleSignIn = async () => {
    setSigningIn(true)
    setSignInError('')
    try {
      await signInWithGoogle()
      onLogin(FIREBASE_SESSION)
    } catch (signInFailure) {
      setSignInError(signInFailure instanceof Error ? signInFailure.message : 'Google sign-in failed.')
    } finally {
      setSigningIn(false)
    }
  }

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
        <div className="admin-login-options">
          {authConfig?.firebase && (
            <div className="admin-login">
              <button className="primary" onClick={() => void googleSignIn()} disabled={signingIn}>
                {signingIn ? 'Signing in…' : 'Sign in with Google'} <ArrowIcon />
              </button>
              <p>Only accounts on the admin list can open the dashboard.</p>
              {signInError && <p className="admin-error" role="alert">{signInError}</p>}
            </div>
          )}
          {(authConfig?.token_login ?? true) && (
            <details className="affiliation-editor" open={!authConfig?.firebase}>
              <summary>{authConfig?.firebase ? 'Use the admin token instead' : 'Admin access token'}</summary>
              <form className="admin-login" onSubmit={login}>
                <label>Admin access token<input type="password" autoComplete="current-password" value={tokenInput} onChange={(event) => onTokenInputChange(event.target.value)} placeholder="ADMIN_API_TOKEN from the Render environment" /></label>
                <button className="primary" type="submit" disabled={!tokenInput}>Open review queue <ArrowIcon /></button>
              </form>
            </details>
          )}
        </div>
      ) : (
        <>
          <div className="analysis-controls">
            <div>
              <strong>Papers loaded {day ? `on ${day}` : 'today'}</strong>
              <p>
                {today ? `${today.loaded.toLocaleString()} new papers · arXiv ${today.by_source.arxiv.toLocaleString()} · OpenAlex ${today.by_source.openalex.toLocaleString()}` : 'Loading…'}
                {today?.database_mb != null ? ` · database ${today.database_mb.toLocaleString()} MB of ${today.storage_limit_mb.toLocaleString()} MB` : ''}
              </p>
            </div>
            <label>Day<input type="date" value={day} onChange={(event) => onDayChange(event.target.value)} /></label>
            <label>Source
              <select value={source} onChange={(event) => onSourceChange(event.target.value as '' | 'arxiv' | 'openalex')}>
                <option value="">All</option><option value="arxiv">arXiv</option><option value="openalex">OpenAlex</option>
              </select>
            </label>
            {day && <button className="admin-close" onClick={() => onDayChange('')}>Back to today</button>}
            <button className="admin-close" onClick={onLogout}>Sign out</button>
          </div>
          {!!today?.runs.length && (
            <details className="affiliation-editor">
              <summary>Scheduled import runs ({today.runs.length})</summary>
              <ul className="import-runs">
                {today.runs.map((run) => (
                  <li key={`${run.source}-${run.finished_at}`}>
                    <strong>{run.source === 'openalex' ? 'OpenAlex' : 'arXiv'}</strong> · {new Date(run.finished_at).toLocaleTimeString()} · {run.inserted.toLocaleString()} new
                    {typeof run.details.stopped === 'string' ? ` · stopped: ${run.details.stopped.replace(/_/g, ' ')}` : ''}
                    {typeof run.details.credits_used === 'number' ? ` · ${run.details.credits_used.toLocaleString()} OpenAlex credits` : ''}
                  </li>
                ))}
              </ul>
            </details>
          )}
          {overview && (
            <div className="admin-stats">
              <span><strong>{overview.total.toLocaleString()}</strong> papers in archive</span>
              <span><strong>{overview.pending.toLocaleString()}</strong> pending</span>
              <span><strong>{overview.approved.toLocaleString()}</strong> {overview.publish_mode === 'auto' ? 'featured (auto-publish on)' : 'approved'}</span>
              <span><strong>{overview.embedded.toLocaleString()}</strong> embedded</span>
              <span><strong>{overview.documented_institutions.toLocaleString()}</strong> documented institutions</span>
            </div>
          )}
          <details className="affiliation-editor">
            <summary>Manual tools (imports now run automatically every day)</summary>
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
          </details>
          <p className="admin-queue-count">Showing {papers.length.toLocaleString()} of {papersTotal.toLocaleString()} papers loaded {day ? `on ${day}` : 'today'}.</p>
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
                    <div><span>{paper.topic} · {paper.source === 'openalex' ? `OpenAlex ${paper.arxiv_id}${paper.venue ? ` · ${paper.venue}` : ''}` : `arXiv ${paper.arxiv_id}`}</span><h3>{paper.title}</h3><p>{paper.authors.join(' · ')} · {paper.published_at.slice(0, 4)}</p></div>
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
            {!papers.length && <p className="featured-empty">No papers were loaded {day ? `on ${day}` : 'today yet'}. The daily import runs at 06:30 UTC.</p>}
          </div>
        </>
      )}
      {error && !token && <p className="admin-error" role="alert">{error}</p>}
    </section>
  )
}
