import { useState, type FormEvent } from 'react'
import { apiRequest } from '../api'
import type { GapComponentKey, GapReport, GapScore, GapTopic, IdeaGapReport, Topic } from '../types'
import { ArrowIcon, SearchIcon } from './Icons'

type ResearchGapsProps = {
  report: GapReport | null
  error: string
  topics: Topic[]
  onSelectTopic: (code: string) => void
}

const COMPONENT_ORDER: GapComponentKey[] = ['scarcity', 'momentum_gap', 'synthesis_gap', 'isolation', 'concentration']
const COMPONENT_HELP: Record<GapComponentKey, string> = {
  scarcity: 'How few papers exist, on a log scale against the whole archive.',
  momentum_gap: 'Papers in the last 24 months vs. the 24 before. Flat or shrinking scores high.',
  synthesis_gap: 'Surveys and reviews compared with the archive-wide rate.',
  isolation: 'Share of papers that are not cross-listed in another CS area.',
  concentration: 'Share of papers that include one of the 5 most frequent author names.',
}
const ideaExamples = ['camera-based 3D printer security', 'pose estimation ergonomics', 'PLC anomaly detection', 'LSTM crop yield forecasting']

/** Green (well developed) -> amber -> red (under-researched). */
export function gapColor(index: number | null | undefined): string {
  if (index === null || index === undefined) return '#6c8587'
  const t = Math.max(0, Math.min(1, (index - 20) / 60))
  const hue = 145 - 145 * t
  return `hsl(${hue.toFixed(0)}, 78%, ${(52 + 6 * (1 - Math.abs(t - 0.5) * 2)).toFixed(0)}%)`
}

function Bar({ value, color }: { value: number; color?: string }) {
  return <div className="gap-bar"><i style={{ width: `${Math.max(2, value)}%`, background: color ?? gapColor(value) }} /></div>
}

function UnderResearchedCard({ score, labels, weights }: { score: GapScore; labels: GapReport['methodology']['component_labels']; weights: GapReport['methodology']['weights'] }) {
  const m = score.metrics
  return (
    <article className="gap-card">
      <header>
        <span className="gap-kicker">Score 1 · Under-researched</span>
        <div className="gap-headline">
          <strong style={{ color: gapColor(score.index) }}>{score.index.toFixed(0)}</strong>
          <div><b>{score.verdict}</b><small>{score.confidence} confidence · 0 = well developed, 100 = barely studied</small></div>
        </div>
      </header>
      <ul className="gap-components">
        {COMPONENT_ORDER.map((key) => (
          <li key={key} title={COMPONENT_HELP[key]}>
            <span>{labels[key]} <em>× {weights[key].toFixed(2)}</em></span>
            <Bar value={score.components[key]} />
            <small>{score.components[key].toFixed(0)}</small>
          </li>
        ))}
      </ul>
      {score.metrics.papers > 0 && <p className="gap-drivers">Main drivers: {score.drivers.join(' and ')}</p>}
      <dl className="gap-metrics">
        <div><dt>Papers</dt><dd>{m.papers.toLocaleString()} <small>({m.share_of_archive_pct}% of archive)</small></dd></div>
        <div><dt>Last 24 months / prior 24</dt><dd>{m.recent_24_months.toLocaleString()} / {m.previous_24_months.toLocaleString()} <small>(×{m.growth_ratio})</small></dd></div>
        <div><dt>Surveys / expected</dt><dd>{m.surveys} / {m.expected_surveys}</dd></div>
        <div><dt>Cross-listed</dt><dd>{m.cross_listed_pct}%</dd></div>
        <div><dt>Top-5 author share</dt><dd>{m.top5_author_share_pct}% <small>of {m.distinct_authors.toLocaleString()} authors</small></dd></div>
      </dl>
    </article>
  )
}

function RepresentationCard({ score }: { score: GapScore }) {
  const r = score.representation
  return (
    <article className="gap-card">
      <header>
        <span className="gap-kicker">Score 2 · Underrepresented</span>
        <div className="gap-headline">
          <strong style={{ color: r.index === null ? '#7f9798' : gapColor(r.index) }}>{r.index === null ? 'n/a' : r.index.toFixed(0)}</strong>
          <div><b>{r.verdict}</b><small>Who is visible: concentration of documented institutions and countries</small></div>
        </div>
      </header>
      {r.index === null ? (
        <p className="gap-empty">
          Only {r.documented_papers.toLocaleString()} of these papers ({r.documented_pct}%) have a documented affiliation; at least {r.min_documented} are needed.
          Run the paper scan in the admin dashboard to fill affiliations from OpenAlex and the papers' PDFs. Names are never used to guess affiliation or identity.
        </p>
      ) : (
        <>
          <ul className="gap-components">
            <li><span>Country concentration</span><Bar value={r.components.country_concentration ?? 0} /><small>{r.components.country_concentration?.toFixed(0) ?? '—'}</small></li>
            <li><span>Institution concentration</span><Bar value={r.components.institution_concentration ?? 0} /><small>{r.components.institution_concentration?.toFixed(0) ?? '—'}</small></li>
          </ul>
          <div className="gap-places">
            <div><span>Top countries</span>{r.top_countries.map((item) => <p key={item.name}>{item.name}<small>{item.papers}</small></p>)}</div>
            <div><span>Top institutions</span>{r.top_institutions.map((item) => <p key={item.name}>{item.name}<small>{item.papers}</small></p>)}</div>
          </div>
          <p className="gap-drivers">Based on {r.documented_papers.toLocaleString()} papers with documented affiliations ({r.documented_pct}%).</p>
        </>
      )}
    </article>
  )
}

export default function ResearchGaps({ report, error, topics, onSelectTopic }: ResearchGapsProps) {
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState('')
  const [idea, setIdea] = useState<IdeaGapReport | null>(null)
  const [ideaError, setIdeaError] = useState('')
  const [busy, setBusy] = useState(false)
  const [sortKey, setSortKey] = useState<'index' | 'representation' | 'papers'>('index')

  const check = async (text: string, category = scope) => {
    if (text.trim().length < 2) return
    setBusy(true)
    setIdeaError('')
    try {
      const params = new URLSearchParams({ q: text.trim() })
      if (category) params.set('category', category)
      setIdea(await apiRequest<IdeaGapReport>(`/api/gaps/idea?${params}`))
    } catch (failure) {
      setIdeaError(failure instanceof Error ? failure.message : 'Could not score this research idea.')
    } finally {
      setBusy(false)
    }
  }
  const submit = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void check(query) }

  const methodology = idea?.methodology ?? report?.methodology
  const sorted: GapTopic[] = [...(report?.topics ?? [])].sort((a, b) => {
    if (sortKey === 'papers') return a.metrics.papers - b.metrics.papers
    if (sortKey === 'representation') return (b.representation.index ?? -1) - (a.representation.index ?? -1)
    return Number(b.metrics.papers > 0) - Number(a.metrics.papers > 0) || b.index - a.index
  })

  return (
    <section className="research-gaps" id="gaps">
      <div className="paper-explorer-heading">
        <div>
          <span className="eyebrow">Research gap finder</span>
          <h2>Is this research under-researched or underrepresented?</h2>
          <p>Two separate, explainable scores from the archive's metadata. Under-researched asks how little work exists. Underrepresented asks whether the visible work comes from only a few institutions or countries.</p>
        </div>
      </div>

      <form className="gap-form" onSubmit={submit}>
        <label className="gap-input">
          <SearchIcon />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Describe a research idea, e.g. camera-based 3D printer security" aria-label="Research idea" />
        </label>
        <select value={scope} onChange={(event) => setScope(event.target.value)} aria-label="Limit to a topic">
          <option value="">All CS topics</option>
          {topics.map((topic) => <option key={topic.code} value={topic.code}>{topic.code} · {topic.label}</option>)}
        </select>
        <button className="primary" type="submit" disabled={busy || query.trim().length < 2}>{busy ? 'Scoring…' : 'Check idea'} <ArrowIcon /></button>
      </form>
      <div className="gap-examples">
        {ideaExamples.map((example) => (
          <button key={example} type="button" onClick={() => { setQuery(example); void check(example) }}>{example}</button>
        ))}
      </div>
      {ideaError && <p className="related-status error" role="alert">{ideaError}</p>}

      {idea && methodology && (
        <div className="gap-result" aria-live="polite">
          <p className="gap-query">
            Papers matching <b>all</b> of: {idea.keywords.map((keyword) => <code key={keyword}>{keyword}</code>)}
            {idea.topic_percentile !== null && idea.metrics.papers > 0 && <> · more under-researched than {idea.topic_percentile}% of CS topics</>}
          </p>
          <div className="gap-cards">
            <UnderResearchedCard score={idea} labels={methodology.component_labels} weights={methodology.weights} />
            <RepresentationCard score={idea} />
          </div>
          {idea.main_topics.length > 0 && (
            <div className="gap-topics-chips">
              <span>Where this work lives:</span>
              {idea.main_topics.map((topic) => (
                <button key={topic.code} type="button" onClick={() => onSelectTopic(topic.code)}>{topic.label}<small>{topic.papers}</small></button>
              ))}
            </div>
          )}
          {idea.sample_papers.length > 0 && (
            <div className="gap-samples">
              <span className="panel-label">Newest matching papers</span>
              {idea.sample_papers.map((paper) => (
                <a key={paper.arxiv_id} href={paper.url} target="_blank" rel="noreferrer">
                  <b>{paper.title}</b><small>{paper.year} · {paper.topic} · arXiv:{paper.arxiv_id}</small>
                </a>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="gap-table-wrap">
        <div className="gap-table-head">
          <h3>All CS topics ranked</h3>
          <label>Sort by
            <select value={sortKey} onChange={(event) => setSortKey(event.target.value as typeof sortKey)}>
              <option value="index">Most under-researched</option>
              <option value="representation">Most underrepresented</option>
              <option value="papers">Fewest papers</option>
            </select>
          </label>
        </div>
        {error && <p className="related-status error">{error}</p>}
        {!report && !error && <p className="related-status">Calculating gap scores…</p>}
        {report && (
          <table className="gap-table">
            <thead>
              <tr><th>Topic</th><th>Under-researched</th><th>Verdict</th><th>Papers</th><th>Growth</th><th>Underrepresented</th></tr>
            </thead>
            <tbody>
              {sorted.map((topic) => (
                <tr key={topic.code} onClick={() => onSelectTopic(topic.code)} tabIndex={0} onKeyDown={(event) => { if (event.key === 'Enter') onSelectTopic(topic.code) }}>
                  <td><b>{topic.label}</b><small>{topic.code}</small></td>
                  <td>{topic.metrics.papers === 0
                    ? <small>Not in the imported sample yet</small>
                    : <div className="gap-cell"><Bar value={topic.index} /><span>{topic.index.toFixed(0)}</span></div>}</td>
                  <td>{topic.metrics.papers === 0 ? '—' : <>{topic.verdict}<small>{topic.confidence} confidence</small></>}</td>
                  <td>{topic.metrics.papers.toLocaleString()}</td>
                  <td>{topic.metrics.papers === 0 ? '—' : `×${topic.metrics.growth_ratio}`}</td>
                  <td>{topic.representation.index === null
                    ? <small>n/a · {topic.representation.documented_pct}% documented</small>
                    : <div className="gap-cell"><Bar value={topic.representation.index} /><span>{topic.representation.index.toFixed(0)}</span></div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {methodology && (
        <details className="gap-method">
          <summary>How the scores are calculated</summary>
          <p><b>Under-researched index</b> = weighted average of five 0–100 signals (higher = less developed):
            {' '}{COMPONENT_ORDER.map((key) => `${methodology.component_labels[key]} ${Math.round(methodology.weights[key] * 100)}%`).join(', ')}.
            {' '}Growth compares {methodology.previous_window_start} → {methodology.recent_window_start} with {methodology.recent_window_start} → {methodology.latest_paper_date}. Archive survey rate: {methodology.archive_survey_rate_pct}%.</p>
          <ul>{COMPONENT_ORDER.map((key) => <li key={key}><b>{methodology.component_labels[key]}:</b> {COMPONENT_HELP[key]}</li>)}</ul>
          <p><b>Underrepresentation index</b> = average of country and institution concentration (1 − normalised Shannon entropy) among papers with documented affiliations. It is shown only when at least 10 papers are documented.</p>
          <p>Verdicts: under-researched ≥ 62 (“growing” if output rose ≥ 30%), developing ≥ 45, otherwise well developed. Underrepresented ≥ 60, somewhat concentrated ≥ 40. These are signals to guide review, not measures of quality or of the people doing the research.</p>
          {methodology.coverage_note && <p className="gap-note">{methodology.coverage_note}</p>}
        </details>
      )}
    </section>
  )
}
