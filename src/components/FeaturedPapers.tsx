import type { Paper } from '../types'
import { ArrowIcon } from './Icons'

export default function FeaturedPapers({ papers, publishMode = 'review' }: { papers: Paper[]; publishMode?: 'review' | 'auto' }) {
  const auto = publishMode === 'auto'
  return (
    <section className="featured-section" id="featured">
      <div className="paper-explorer-heading">
        <div><span className="eyebrow">{auto ? 'Latest research' : 'Human-reviewed research'}</span><h2>Featured papers</h2><p>{auto ? 'Editor picks appear first, followed by the newest papers in the collection.' : 'Only papers approved by a reviewer appear in this public selection.'}</p></div>
        <strong>{papers.length.toLocaleString()} {auto ? 'shown' : 'featured'}</strong>
      </div>
      {papers.length ? (
        <div className="featured-grid">
          {papers.map((paper) => (
            <article className="featured-card" key={paper.arxiv_id}>
              <span>{paper.topic} · {paper.published_at.slice(0, 4)}</span>
              <h3>{paper.title}</h3>
              <p className="featured-authors">{paper.authors.join(' · ') || 'Authors not listed'}</p>
              <p>{paper.summary || paper.abstract}</p>
              {paper.key_contribution && <div><strong>Key contribution</strong><p>{paper.key_contribution}</p></div>}
              {paper.application && <div><strong>Potential application</strong><p>{paper.application}</p></div>}
              {paper.relevance && <div><strong>Why it was surfaced</strong><p>{paper.relevance}</p></div>}
              {(paper.topics || paper.methods || paper.context) && (
                <div className="featured-analysis">
                  {paper.topics && <span>Topics: {paper.topics}</span>}
                  {paper.methods && <span>Methods: {paper.methods}</span>}
                  {paper.context && <span>Context: {paper.context}</span>}
                </div>
              )}
              <a href={paper.url} target="_blank" rel="noreferrer">Read on arXiv <ArrowIcon /></a>
            </article>
          ))}
        </div>
      ) : (
        <p className="featured-empty">No papers have been approved for public featuring yet. Review candidates in the admin dashboard.</p>
      )}
    </section>
  )
}
