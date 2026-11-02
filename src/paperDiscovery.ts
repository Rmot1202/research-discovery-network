import type { Paper, RelatedPaper } from './types'

const stopWords = new Set([
  'about', 'above', 'after', 'again', 'against', 'all', 'also', 'among', 'and',
  'any', 'are', 'because', 'been', 'before', 'being', 'below', 'between', 'both',
  'but', 'can', 'could', 'did', 'does', 'doing', 'down', 'during', 'each', 'few',
  'for', 'from', 'further', 'had', 'has', 'have', 'having', 'here', 'how', 'into',
  'its', 'itself', 'just', 'more', 'most', 'not', 'now', 'off', 'once', 'only',
  'other', 'over', 'own', 'same', 'should', 'some', 'such', 'than', 'that', 'the',
  'their', 'them', 'then', 'there', 'these', 'they', 'this', 'those', 'through',
  'too', 'under', 'until', 'very', 'was', 'were', 'what', 'when', 'where', 'which',
  'while', 'who', 'whom', 'why', 'will', 'with', 'within', 'without', 'would',
  'approach', 'based', 'data', 'method', 'methods', 'model', 'models', 'paper',
  'propose', 'proposed', 'result', 'results', 'show', 'study', 'task', 'use', 'used',
  'using', 'work', 'across', 'already', 'different', 'existing', 'first', 'given',
  'however', 'including', 'instead', 'large', 'many', 'may', 'new', 'often', 'one',
  'provide', 'several', 'small', 'structure', 'structures', 'well', 'costly',
  'evaluated', 'evaluation', 'families', 'improve', 'improved', 'improving', 'make',
  'making', 'shared', 'whether',
])

function paperTerms(paper: Paper): Set<string> {
  return new Set(
    `${paper.title} ${paper.abstract}`
      .toLowerCase()
      .match(/[a-z][a-z0-9-]{2,}/g)
      ?.filter((term) => !stopWords.has(term)) ?? [],
  )
}

export function findRelatedPapers(paper: Paper, candidates: Paper[]): RelatedPaper[] {
  const paperCategories = new Set(paper.categories)
  const terms = paperTerms(paper)
  return candidates
    .filter((candidate) => candidate.arxiv_id !== paper.arxiv_id)
    .map((candidate) => {
      const sharedCategories = candidate.categories.filter((category) => paperCategories.has(category))
      const categoryUnion = new Set([...paperCategories, ...candidate.categories])
      const candidateTerms = paperTerms(candidate)
      const sharedTerms = [...terms].filter((term) => candidateTerms.has(term))
      const categorySimilarity = categoryUnion.size ? sharedCategories.length / categoryUnion.size : 0
      const termSimilarity = terms.size + candidateTerms.size
        ? (2 * sharedTerms.length) / (terms.size + candidateTerms.size)
        : 0
      return {
        ...candidate,
        similarityScore: Math.round((categorySimilarity * 0.55 + termSimilarity * 0.45) * 100),
        sharedCategories,
        sharedTerms: sharedTerms.slice(0, 4),
      }
    })
    .filter((candidate) => candidate.sharedCategories.length > 0)
    .sort((left, right) => right.similarityScore - left.similarityScore || right.published_at.localeCompare(left.published_at))
    .slice(0, 5)
}
