import type { Discovery } from '../types'

const FACTOR_NAMES: Record<keyof Discovery['factors'], string> = {
  topic: 'Topic',
  institution: 'Institution',
  geography: 'Country / region',
}

const FACTOR_HELP: Record<keyof Discovery['factors'], string> = {
  topic: 'How rare this paper\'s subtopic is among the subtopics of its field.',
  institution: 'How rarely this institution appears in this field.',
  geography: 'How rarely this country appears in this field.',
}

function scoreColor(value: number | null) {
  if (value === null) return '#9fb0b1'
  const t = Math.max(0, Math.min(1, value / 100))
  return `hsl(${(200 - 170 * t).toFixed(0)}, 62%, ${(48 - 6 * t).toFixed(0)}%)`
}

export function DiscoveryPill({ discovery }: { discovery?: Discovery }) {
  if (!discovery || discovery.discovery_score === null) return <span className="discovery-pill muted">Discovery Score —</span>
  return (
    <span className={`discovery-pill${discovery.underrepresented ? ' flagged' : ''}`} title={discovery.reasons.join(' · ') || undefined}>
      Discovery Score <b style={{ color: scoreColor(discovery.discovery_score) }}>{discovery.discovery_score.toFixed(0)}</b>
      {discovery.underrepresented && <em>{discovery.reasons[0]}</em>}
    </span>
  )
}

export function DiscoveryBreakdown({ discovery, field }: { discovery?: Discovery; field: string }) {
  if (!discovery) return null
  const keys = Object.keys(FACTOR_NAMES) as (keyof Discovery['factors'])[]
  const measured = keys.filter((key) => discovery.factors[key] !== null).length
  return (
    <section className="discovery-card" aria-label="Discovery Score breakdown">
      <header>
        <div>
          <span className="eyebrow">Discovery Score</span>
          <strong style={{ color: scoreColor(discovery.discovery_score) }}>
            {discovery.discovery_score === null ? '—' : discovery.discovery_score.toFixed(0)}
          </strong>
          <small>out of 100 · based on {measured} of 3 factors</small>
        </div>
        {discovery.reasons.length > 0 && (
          <ul className="discovery-reasons">{discovery.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
        )}
      </header>
      <div className="discovery-factors">
        {keys.map((key) => {
          const value = discovery.factors[key]
          return (
            <div key={key} className={value === null ? 'missing' : ''} title={FACTOR_HELP[key]}>
              <span>{FACTOR_NAMES[key]} <small>{Math.round(discovery.weights[key] * 100)}%</small></span>
              <div className="gap-bar"><i style={{ width: `${value === null ? 0 : Math.max(2, value)}%`, background: scoreColor(value) }} /></div>
              <b>{value === null ? 'Not documented' : value.toFixed(0)}</b>
            </div>
          )
        })}
      </div>
      <p className="discovery-note">
        Compared with other {field} papers in this arXiv collection
        {discovery.topic_basis === 'category' ? ' (topic compares the whole field with other CS fields until its subtopics are grouped)' : ''}.
        100 = least common, 0 = most common. This measures representation in the dataset, not visibility: citations and downloads are not included.
        A paper only needs one strong factor to be flagged.
      </p>
    </section>
  )
}
