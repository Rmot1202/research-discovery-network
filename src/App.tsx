import { useEffect, useMemo, useState } from 'react'
import { adminRequest, apiRequest } from './api'
import FeaturedPapers from './components/FeaturedPapers'
import { ArrowIcon, SearchIcon, SparkIcon } from './components/Icons'
import AdminDashboard from './components/AdminDashboard'
import ResearchAssistant from './components/ResearchAssistant'
import ResearchGaps from './components/ResearchGaps'
import TopicNetwork from './components/TopicNetwork'
import { findRelatedPapers } from './paperDiscovery'
import type {
  AdminOverview,
  AffiliationDraft,
  AffiliationSuggestion,
  GapReport,
  IngestProgress,
  Paper,
  PaperDraft,
  RelatedPaper,
  Topic,
  TopicLink,
} from './types'

const pageSize = 50

export default function App() {
  const [topics, setTopics] = useState<Topic[]>([])
  const [topicLinks, setTopicLinks] = useState<TopicLink[]>([])
  const [papers, setPapers] = useState<Paper[]>([])
  const [totalPapers, setTotalPapers] = useState(0)
  const [filterOptions, setFilterOptions] = useState<{
    institutions: string[]
    countries: string[]
    years: number[]
  }>({ institutions: [], countries: [], years: [] })
  const [selectedId, setSelectedId] = useState('cs.AI')
  const [selectedPaper, setSelectedPaper] = useState<Paper | null>(null)
  const [relatedPapers, setRelatedPapers] = useState<RelatedPaper[]>([])
  const [relatedLoading, setRelatedLoading] = useState(false)
  const [relatedError, setRelatedError] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('')
  const [countryFilter, setCountryFilter] = useState('')
  const [institutionFilter, setInstitutionFilter] = useState('')
  const [yearFilter, setYearFilter] = useState('')
  const [sort, setSort] = useState('newest')
  const [page, setPage] = useState(0)
  const [dataRevision, setDataRevision] = useState(0)
  const [filteredTotal, setFilteredTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [apiError, setApiError] = useState('')
  const [ingestProgress, setIngestProgress] = useState<IngestProgress | null>(null)
  const [isStartingIngest, setIsStartingIngest] = useState(false)
  const [featuredPapers, setFeaturedPapers] = useState<Paper[]>([])
  const [isAdminOpen, setIsAdminOpen] = useState(false)
  const [adminTokenInput, setAdminTokenInput] = useState('')
  const [adminToken, setAdminToken] = useState('')
  const [adminOverview, setAdminOverview] = useState<AdminOverview | null>(null)
  const [adminPapers, setAdminPapers] = useState<Paper[]>([])
  const [adminError, setAdminError] = useState('')
  const [adminNotice, setAdminNotice] = useState('')
  const [adminBusyId, setAdminBusyId] = useState('')
  const [analysisBusy, setAnalysisBusy] = useState(false)
  const [paperDrafts, setPaperDrafts] = useState<Record<string, PaperDraft>>({})
  const [affiliationDrafts, setAffiliationDrafts] = useState<Record<string, AffiliationDraft>>({})
  const [affiliationSuggestions, setAffiliationSuggestions] = useState<Record<string, AffiliationSuggestion[]>>({})
  const [assistantScope, setAssistantScope] = useState('')
  const [assistantPrompt, setAssistantPrompt] = useState<{ text: string; id: number } | null>(null)
  const [gapReport, setGapReport] = useState<GapReport | null>(null)
  const [gapError, setGapError] = useState('')
  const [gapRevision, setGapRevision] = useState(0)
  const refreshIngestProgress = async () => {
    try {
      const progress = await apiRequest<IngestProgress>('/api/ingest/progress')
      setIngestProgress(progress)
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not load ingestion progress.')
    }
  }

  useEffect(() => {
    let cancelled = false
    const loadDiscoveryMetadata = async () => {
      try {
        const [topicResult, linkResult, filters] = await Promise.all([
          apiRequest<{ topics: Topic[]; total_papers: number }>('/api/topics'),
          apiRequest<{ links: TopicLink[] }>('/api/topic-links'),
          apiRequest<{ institutions: string[]; countries: string[]; years: number[] }>('/api/filters'),
        ])
        if (cancelled) return
        setTopics(topicResult.topics)
        setTopicLinks(linkResult.links)
        setTotalPapers(topicResult.total_papers)
        setFilterOptions(filters)
        setApiError('')
      } catch (error) {
        if (!cancelled) setApiError(error instanceof Error ? error.message : 'Could not load research topics.')
      }
    }
    void loadDiscoveryMetadata()
    void refreshIngestProgress()
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    let cancelled = false
    void apiRequest<{ papers: Paper[] }>('/api/featured?limit=12')
      .then((result) => { if (!cancelled) setFeaturedPapers(result.papers) })
      .catch((error: unknown) => {
        if (!cancelled) setApiError(error instanceof Error ? error.message : 'Could not load featured papers.')
      })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    let cancelled = false
    void apiRequest<GapReport>('/api/gaps/topics')
      .then((result) => { if (!cancelled) { setGapReport(result); setGapError('') } })
      .catch((error: unknown) => {
        if (!cancelled) setGapError(error instanceof Error ? error.message : 'Could not load research gap scores.')
      })
    return () => { cancelled = true }
  }, [gapRevision, topics.length])

  const gapByCode = useMemo(
    () => Object.fromEntries((gapReport?.topics ?? []).map((topic) => [topic.code, topic])),
    [gapReport],
  )

  const loadAdminDashboard = async (token: string) => {
    setAdminError('')
    try {
      const [overview, queue] = await Promise.all([
        adminRequest<AdminOverview>(token, '/api/admin/overview'),
        adminRequest<{ papers: Paper[] }>(token, '/api/admin/reviews?limit=20'),
      ])
      setAdminOverview(overview)
      setAdminPapers(queue.papers)
      setAdminToken(token)
      setAdminTokenInput('')
      setPaperDrafts(Object.fromEntries(queue.papers.map((paper) => [
        paper.arxiv_id,
        {
          summary: paper.summary ?? '',
          key_contribution: paper.key_contribution ?? '',
          application: paper.application ?? '',
          relevance: paper.relevance ?? '',
        },
      ])))
      setAffiliationDrafts(Object.fromEntries(queue.papers.map((paper) => [
        paper.arxiv_id,
        {
          institution: paper.institution ?? '',
          country: paper.country ?? '',
          source_url: paper.affiliation_source_url ?? '',
          evidence: paper.affiliation_evidence ?? '',
        },
      ])))
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Could not load the admin review queue.')
    }
  }

  const refreshAdminDashboard = async () => {
    if (adminToken) await loadAdminDashboard(adminToken)
  }

  const updateReview = async (paper: Paper, approval_status?: 'approved' | 'rejected' | 'pending') => {
    if (!adminToken) return
    setAdminBusyId(paper.arxiv_id)
    setAdminError('')
    setAdminNotice('')
    try {
      const draft = paperDrafts[paper.arxiv_id]
      await adminRequest(adminToken, `/api/admin/papers/${encodeURIComponent(paper.arxiv_id)}/review`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...draft, ...(approval_status ? { approval_status } : {}) }),
      })
      setAdminNotice(approval_status ? `Paper ${approval_status} successfully.` : 'Summary changes saved.')
      await refreshAdminDashboard()
      if (approval_status === 'approved') {
        const result = await apiRequest<{ papers: Paper[] }>('/api/featured?limit=12')
        setFeaturedPapers(result.papers)
      }
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Could not update the review.')
    } finally {
      setAdminBusyId('')
    }
  }

  const generatePaperSummary = async (paper: Paper) => {
    if (!adminToken) return
    setAdminBusyId(paper.arxiv_id)
    setAdminError('')
    try {
      const summary = await adminRequest<PaperDraft>(adminToken, `/api/admin/papers/${encodeURIComponent(paper.arxiv_id)}/summarize`, { method: 'POST' })
      setPaperDrafts((drafts) => ({ ...drafts, [paper.arxiv_id]: { ...drafts[paper.arxiv_id], ...summary } }))
      setAdminNotice(`AI summary generated for ${paper.arxiv_id}. Review it before approval.`)
      await refreshAdminDashboard()
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Could not generate the paper summary.')
    } finally {
      setAdminBusyId('')
    }
  }

  const saveAffiliation = async (paper: Paper) => {
    if (!adminToken) return
    const draft = affiliationDrafts[paper.arxiv_id]
    setAdminBusyId(paper.arxiv_id)
    setAdminError('')
    try {
      await adminRequest(adminToken, `/api/admin/papers/${encodeURIComponent(paper.arxiv_id)}/affiliation`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      })
      setAdminNotice('Documented affiliation saved with its source evidence.')
      setGapRevision((revision) => revision + 1)
      await refreshAdminDashboard()
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Could not save the affiliation.')
    } finally {
      setAdminBusyId('')
    }
  }

  const extractAffiliation = async (paper: Paper) => {
    if (!adminToken) return
    setAdminBusyId(paper.arxiv_id)
    setAdminError('')
    try {
      const result = await adminRequest<{ suggestions: AffiliationSuggestion[] }>(
        adminToken,
        `/api/admin/papers/${encodeURIComponent(paper.arxiv_id)}/extract-affiliation`,
        { method: 'POST' },
      )
      setAffiliationSuggestions((suggestions) => ({ ...suggestions, [paper.arxiv_id]: result.suggestions }))
      setAdminNotice(result.suggestions.length
        ? `Found ${result.suggestions.length} source-checked affiliation suggestion(s); select one to review.`
        : 'No verifiable affiliation text was found on the first two PDF pages.')
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Could not extract affiliation evidence from the PDF.')
    } finally {
      setAdminBusyId('')
    }
  }

  const runAIAnalysis = async () => {
    if (!adminToken) return
    setAnalysisBusy(true)
    setAdminError('')
    setAdminNotice('')
    try {
      const result = await adminRequest<{ analyzed: number; embedded_papers: number; clusters: number }>(
        adminToken,
        '/api/admin/analyze?limit=20',
        { method: 'POST' },
      )
      setAdminNotice(`Generated ${result.embedded_papers} embeddings and grouped the analyzed collection into ${result.clusters} topic clusters.`)
      await refreshAdminDashboard()
    } catch (error) {
      setAdminError(error instanceof Error ? error.message : 'Could not analyze papers.')
    } finally {
      setAnalysisBusy(false)
    }
  }
  useEffect(() => {
    if (!selectedPaper) {
      setRelatedPapers([])
      setRelatedError('')
      return
    }

    let cancelled = false
    setRelatedLoading(true)
    setRelatedError('')
    const categories = [...new Set([selectedPaper.primary_category, ...selectedPaper.categories])].slice(0, 4)
    Promise.all(categories.map((category) =>
      apiRequest<{ papers: Paper[] }>(`/api/papers?category=${encodeURIComponent(category)}&limit=100&sort=newest`),
    ))
      .then((results) => {
        if (cancelled) return
        const candidates = [...new Map(results.flatMap((result) => result.papers)
          .map((paper) => [paper.arxiv_id, paper])).values()]
        setRelatedPapers(findRelatedPapers(selectedPaper, candidates))
      })
      .catch((error: unknown) => {
        if (!cancelled) setRelatedError(error instanceof Error ? error.message : 'Could not load related papers.')
      })
      .finally(() => {
        if (!cancelled) setRelatedLoading(false)
      })

    return () => { cancelled = true }
  }, [selectedPaper])

  useEffect(() => {
    let cancelled = false
    const timer = window.setTimeout(async () => {
      setIsLoading(true)
      const params = new URLSearchParams({
        limit: String(pageSize),
        offset: String(page * pageSize),
        sort,
      })
      if (searchQuery.trim()) params.set('q', searchQuery.trim())
      if (categoryFilter) params.set('category', categoryFilter)
      if (countryFilter) params.set('country', countryFilter)
      if (institutionFilter) params.set('institution', institutionFilter)
      if (yearFilter) params.set('year', yearFilter)
      try {
        const result = await apiRequest<{
          count: number
          total: number
          papers: Paper[]
        }>(`/api/papers?${params}`)
        if (cancelled) return
        setPapers(result.papers)
        setTotalPapers(result.count)
        setFilteredTotal(result.total)
        setApiError('')
      } catch (error) {
        if (!cancelled) setApiError(error instanceof Error ? error.message : 'Could not load papers.')
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }, searchQuery ? 250 : 0)
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [categoryFilter, countryFilter, dataRevision, institutionFilter, page, searchQuery, sort, yearFilter])

  useEffect(() => {
    if (ingestProgress?.status !== 'running') return
    const interval = window.setInterval(() => {
      void refreshIngestProgress()
      setDataRevision((revision) => revision + 1)
      void Promise.all([
        apiRequest<{ topics: Topic[]; total_papers: number }>('/api/topics'),
        apiRequest<{ links: TopicLink[] }>('/api/topic-links'),
      ])
        .then(([result, linkResult]) => {
          setTopics(result.topics)
          setTopicLinks(linkResult.links)
          setTotalPapers(result.total_papers)
        })
        .catch((error: unknown) => setApiError(error instanceof Error ? error.message : 'Could not refresh topics.'))
    }, 5000)
    return () => window.clearInterval(interval)
  }, [ingestProgress?.status])

  const startFullIngest = async () => {
    if (!adminToken) return
    setIsStartingIngest(true)
    setApiError('')
    try {
      const progress = await adminRequest<IngestProgress>(adminToken, '/api/ingest/full', { method: 'POST' })
      setIngestProgress(progress)
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not start the full arXiv import.')
    } finally {
      setIsStartingIngest(false)
    }
  }

  const pauseFullIngest = async () => {
    if (!adminToken) return
    try {
      setIngestProgress(await adminRequest<IngestProgress>(adminToken, '/api/ingest/pause', { method: 'POST' }))
    } catch (error) {
      setApiError(error instanceof Error ? error.message : 'Could not pause the full arXiv import.')
    }
  }

  const selectTopic = (topic: Topic) => {
    setSelectedId(topic.code)
    setCategoryFilter((current) => current === topic.code ? '' : topic.code)
    setPage(0)
    setSelectedPaper(null)
  }

  const searchResults = useMemo(() => {
    const query = searchQuery.trim().toLowerCase()
    if (!query) return []
    return [
      ...topics
        .filter((topic) => `${topic.code} ${topic.label}`.toLowerCase().includes(query))
        .map((topic) => ({ id: topic.code, type: 'Topic', title: topic.label, meta: `${topic.count.toLocaleString()} papers` })),
      ...papers
        .filter((paper) =>
          [paper.title, paper.topic, paper.institution ?? '', paper.country ?? '', ...paper.authors]
            .some((value) => value.toLowerCase().includes(query)),
        )
        .map((paper) => ({ id: paper.arxiv_id, type: 'Paper', title: paper.title, meta: paper.authors.join(', ') })),
    ].slice(0, 6)
  }, [papers, searchQuery, topics])

  const selectSearchResult = (result: { id: string; type: string }) => {
    if (result.type === 'Topic') {
      const topic = topics.find((item) => item.code === result.id)
      if (topic) selectTopic(topic)
    } else {
      const paper = papers.find((item) => item.arxiv_id === result.id)
      if (paper) {
        setSelectedId(paper.primary_category)
        setCategoryFilter(paper.primary_category)
        setPage(0)
        setSelectedPaper(paper)
      }
    }
    setSearchQuery('')
  }

  const progressPercent = ingestProgress?.status === 'complete'
    ? 100
    : ingestProgress?.total_records
      ? Math.min(99, Math.round((ingestProgress.records_processed / ingestProgress.total_records) * 100))
      : null

  return (
    <main>
      <header className="topbar">
        <button className="brand" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
          <span className="brand-mark" aria-hidden="true"><i /><i /><i /><i /></span>
          <span>Underrepresented<small>Research Discovery</small></span>
        </button>
        <nav aria-label="Primary navigation">
          <a href="#about">About</a>
          <a className="active" href="#explore">Explore</a>
          <a href="#papers">Papers</a>
          <a href="#assistant">Ask</a>
          <a href="#gaps">Gaps</a>
          <a href="#featured">Featured</a>
        </nav>
        <button
          className="admin-nav-button"
          onClick={() => {
            setIsAdminOpen(true)
            window.setTimeout(() => document.getElementById('admin')?.scrollIntoView({ behavior: 'smooth' }), 0)
          }}
        >Admin review</button>
        <div className="search-wrap">
          <label className="search">
            <SearchIcon />
            <input
              aria-label="Search papers, authors, topics, institutions, or countries"
              placeholder="Search papers, authors, topics..."
              value={searchQuery}
              onChange={(event) => { setSearchQuery(event.target.value); setPage(0) }}
            />
            <kbd>⌘ K</kbd>
          </label>
          {searchQuery && (
            <div className="search-results">
              {searchResults.length ? searchResults.map((result) => (
                <button key={`${result.type}-${result.id}`} onClick={() => selectSearchResult(result)}>
                  <span><i>{result.type}</i>{result.title}</span>
                  <small>{result.meta}</small>
                </button>
              )) : <p>Use the filters below to search the imported papers.</p>}
            </div>
          )}
        </div>
      </header>

      <section className="hero" id="explore">
        <div className="hero-copy">
          <div className="eyebrow"><SparkIcon /> Computer science research discovery</div>
          <h1>Explore the full<br /><em>research landscape.</em></h1>
          <p>Browse every arXiv Computer Science research area, find papers, and filter by topic, institution, country, and year.</p>
          <div className="hero-actions">
            <button className="primary" onClick={() => document.getElementById('topics-map')?.scrollIntoView({ behavior: 'smooth' })}>
              Explore topics <ArrowIcon />
            </button>
            <button className="secondary" onClick={() => document.getElementById('papers')?.scrollIntoView({ behavior: 'smooth' })}>
              Find papers
            </button>
          </div>
          <div className="stats">
            <span><strong>{totalPapers.toLocaleString()}</strong> imported papers</span>
            <span><strong>{topics.length}</strong> arXiv CS topics</span>
          </div>
          <div className="full-ingest">
            <p>Archive import uses resumable OAI-PMH paging · controls are in the admin dashboard</p>
            {ingestProgress && ingestProgress.status !== 'not_started' && (
              <div className="ingest-progress">
                <div><strong>{progressPercent === null ? ingestProgress.status : `${progressPercent}%`}</strong> · {ingestProgress.pages_processed.toLocaleString()} pages processed</div>
                <div className={`progress-track ${progressPercent === null ? 'indeterminate' : ''}`}><i style={progressPercent === null ? undefined : { width: `${progressPercent}%` }} /></div>
                <small>{ingestProgress.records_processed.toLocaleString()} archive records scanned · {ingestProgress.ingested_papers.toLocaleString()} unique papers saved{ingestProgress.total_records ? ` · ${ingestProgress.total_records.toLocaleString()} records total` : ''}{ingestProgress.status === 'error' ? ` · ${ingestProgress.error}` : ''}</small>
              </div>
            )}
          </div>
        </div>

        <TopicNetwork
          topics={topics}
          links={topicLinks}
          selectedId={selectedId}
          totalPapers={totalPapers}
          isLoading={isLoading}
          hasPapers={papers.length > 0}
          apiError={apiError}
          onSelect={selectTopic}
          gapByCode={gapByCode}
          onClear={() => {
            setSelectedId('')
            setCategoryFilter('')
            setSelectedPaper(null)
            setPage(0)
          }}
          onAsk={(topic) => {
            setAssistantScope(topic.code)
            setAssistantPrompt({ text: `What are the main research directions in ${topic.label}?`, id: Date.now() })
            document.getElementById('assistant')?.scrollIntoView({ behavior: 'smooth' })
          }}
        />
      </section>

      <ResearchAssistant
        topics={topics}
        scope={assistantScope}
        onScopeChange={setAssistantScope}
        prompt={assistantPrompt}
      />

      <ResearchGaps
        report={gapReport}
        error={gapError}
        topics={topics}
        onSelectTopic={(code) => {
          const topic = topics.find((item) => item.code === code)
          if (topic) {
            setSelectedId(topic.code)
            setCategoryFilter(topic.code)
            setPage(0)
            setSelectedPaper(null)
          }
          document.getElementById('topics-map')?.scrollIntoView({ behavior: 'smooth', block: 'center' })
        }}
      />

      <FeaturedPapers papers={featuredPapers} />

      <AdminDashboard
        isOpen={isAdminOpen}
        token={adminToken}
        tokenInput={adminTokenInput}
        overview={adminOverview}
        papers={adminPapers}
        error={adminError}
        notice={adminNotice}
        busyId={adminBusyId}
        analysisBusy={analysisBusy}
        drafts={paperDrafts}
        affiliationDrafts={affiliationDrafts}
        affiliationSuggestions={affiliationSuggestions}
        ingestProgress={ingestProgress}
        isStartingIngest={isStartingIngest}
        onClose={() => setIsAdminOpen(false)}
        onTokenInputChange={setAdminTokenInput}
        onLogin={(token) => void loadAdminDashboard(token)}
        onStartIngest={() => void startFullIngest()}
        onPauseIngest={() => void pauseFullIngest()}
        onAnalyze={() => void runAIAnalysis()}
        onGenerateSummary={(paper) => void generatePaperSummary(paper)}
        onUpdateReview={(paper, status) => void updateReview(paper, status)}
        onSaveAffiliation={(paper) => void saveAffiliation(paper)}
        onExtractAffiliation={(paper) => void extractAffiliation(paper)}
        onPaperDraftChange={(paperId, field, value) => setPaperDrafts((drafts) => ({
          ...drafts, [paperId]: { ...drafts[paperId], [field]: value },
        }))}
        onAffiliationDraftChange={(paperId, field, value) => setAffiliationDrafts((drafts) => ({
          ...drafts, [paperId]: { ...drafts[paperId], [field]: value },
        }))}
        onAffiliationsChanged={() => { setGapRevision((revision) => revision + 1); setDataRevision((revision) => revision + 1) }}
        onUseAffiliationSuggestion={(paperId, suggestion) => setAffiliationDrafts((drafts) => ({
          ...drafts,
          [paperId]: {
            institution: suggestion.institution,
            country: suggestion.country ?? '',
            source_url: suggestion.source_url,
            evidence: suggestion.evidence,
          },
        }))}
      />

      <section className="paper-explorer" id="papers">
        <div className="paper-explorer-heading">
          <div><span className="eyebrow">Search the collection</span><h2>Find a paper</h2><p>Search titles, abstracts, authors, topics, affiliations, and countries.</p></div>
          <strong>{filteredTotal.toLocaleString()} results</strong>
        </div>
        <div className="filter-bar" aria-label="Paper filters">
          <label>Topic
            <select value={categoryFilter} onChange={(event) => { setCategoryFilter(event.target.value); setSelectedId(event.target.value); setPage(0) }}>
              <option value="">All topics</option>
              {topics.map((topic) => <option key={topic.code} value={topic.code}>{topic.code} · {topic.label} ({topic.count})</option>)}
            </select>
          </label>
          <label>Country
            <select value={countryFilter} onChange={(event) => { setCountryFilter(event.target.value); setPage(0) }}>
              <option value="">{filterOptions.countries.length ? 'All countries' : 'No documented country metadata'}</option>
              {filterOptions.countries.map((country) => <option key={country} value={country}>{country}</option>)}
            </select>
          </label>
          <label>Institution
            <select value={institutionFilter} onChange={(event) => { setInstitutionFilter(event.target.value); setPage(0) }}>
              <option value="">{filterOptions.institutions.length ? 'All institutions' : 'No documented affiliations'}</option>
              {filterOptions.institutions.map((institution) => <option key={institution} value={institution}>{institution}</option>)}
            </select>
          </label>
          <label>Year
            <select value={yearFilter} onChange={(event) => { setYearFilter(event.target.value); setPage(0) }}>
              <option value="">All years</option>
              {filterOptions.years.map((year) => <option key={year} value={year}>{year}</option>)}
            </select>
          </label>
          <label>Sort by
            <select value={sort} onChange={(event) => { setSort(event.target.value); setPage(0) }}>
              <option value="newest">Newest</option>
              <option value="oldest">Oldest</option>
              <option value="score">Topic rarity</option>
              <option value="title">Title A–Z</option>
            </select>
          </label>
        </div>
        <p className="filter-note">Institution and country filters populate only when documented in paper metadata; they are never guessed from author names.</p>
        <div className="paper-results" aria-live="polite">
          {papers.map((paper) => (
            <article className="paper-result" key={paper.arxiv_id}>
              <div>
                <button
                  className="paper-title"
                  aria-expanded={selectedPaper?.arxiv_id === paper.arxiv_id}
                  onClick={() => {
                    setSelectedPaper((current) => current?.arxiv_id === paper.arxiv_id ? null : paper)
                    setSelectedId(paper.primary_category)
                  }}
                >{paper.title}</button>
                <p>{paper.authors.join(' · ') || 'Authors not listed'} · {paper.topic} · {paper.published_at.slice(0, 4)}</p>
                <p className="paper-abstract">{paper.abstract}</p>
                <div className="paper-meta">
                  <span title={paper.affiliation_evidence ?? undefined}>
                    {paper.institution ?? 'Affiliation unavailable'}
                    {paper.institutions_all && paper.institutions_all.length > 1 ? ` +${paper.institutions_all.length - 1} more` : ''}
                    {paper.affiliation_method && paper.affiliation_method !== 'manual' && (
                      <a className="affiliation-source" href={paper.affiliation_source_url ?? undefined} target="_blank" rel="noreferrer">
                        {paper.affiliation_method === 'openalex' ? 'via OpenAlex' : 'from PDF'} · unreviewed
                      </a>
                    )}
                  </span>
                  <span>{paper.country ?? 'Country unavailable'}</span>
                  <span>Topic rarity {paper.score_components.topic_rarity ?? '—'}</span>
                  <span>{paper.approval_status === 'approved' ? 'Featured' : paper.approval_status === 'rejected' ? 'Rejected' : 'Pending review'}</span>
                </div>
                {selectedPaper?.arxiv_id === paper.arxiv_id && (
                  <section className="paper-reader" aria-label={`Paper details for ${paper.title}`}>
                    <div className="reader-heading">
                      <div><span className="eyebrow">Paper overview</span><h3>Abstract</h3></div>
                      <a href={paper.url} target="_blank" rel="noreferrer">Read full paper on arXiv <ArrowIcon /></a>
                    </div>
                    <p className="reader-abstract">{paper.abstract || 'No abstract is available in the imported metadata.'}</p>
                    <div className="related-heading">
                      <div><span className="eyebrow">Explore connections</span><h3>Related papers &amp; potential collaboration leads</h3></div>
                      <p>Match score: 55% topic overlap + 45% title/abstract term overlap. These signals can suggest a lead to explore, not researcher intent or paper quality; documented affiliations are unavailable in this archive.</p>
                    </div>
                    {relatedLoading && <p className="related-status" role="status">Finding papers in overlapping research areas…</p>}
                    {relatedError && <p className="related-status error" role="status">{relatedError}</p>}
                    {!relatedLoading && !relatedError && relatedPapers.length === 0 && (
                      <p className="related-status">No related papers found yet in the imported collection for this paper’s topics.</p>
                    )}
                    <div className="related-list">
                      {relatedPapers.map((related) => (
                        <article className="related-paper" key={related.arxiv_id}>
                          <div className="related-paper-heading">
                            <strong>{related.title}</strong>
                            <span>{related.similarityScore}% match</span>
                          </div>
                          <p>{related.authors.join(' · ') || 'Authors not listed'} · {related.published_at.slice(0, 4)}</p>
                          <p className="related-signals">
                            Shared topics: {related.sharedCategories.join(', ')}
                            {related.sharedTerms.length ? ` · Shared terms: ${related.sharedTerms.join(', ')}` : ' · Topic overlap; no distinctive terms matched'}
                          </p>
                          <a href={related.url} target="_blank" rel="noreferrer">Open on arXiv <ArrowIcon /></a>
                        </article>
                      ))}
                    </div>
                  </section>
                )}
              </div>
              <a href={paper.url} target="_blank" rel="noreferrer">arXiv <ArrowIcon /></a>
            </article>
          ))}
          {!isLoading && papers.length === 0 && <p className="empty-results">No papers match these filters in the imported collection.</p>}
        </div>
        <div className="pagination">
          <button disabled={page === 0 || isLoading} onClick={() => setPage((current) => Math.max(0, current - 1))}>Previous</button>
          <span>Page {page + 1} of {Math.max(1, Math.ceil(filteredTotal / pageSize))}</span>
          <button disabled={isLoading || (page + 1) * pageSize >= filteredTotal} onClick={() => setPage((current) => current + 1)}>Next</button>
        </div>
      </section>
    </main>
  )
}
