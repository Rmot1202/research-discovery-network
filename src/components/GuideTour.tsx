import { useCallback, useEffect, useLayoutEffect, useState } from 'react'
import type { MapCommand } from '../types'

type Step = {
  target: string
  title: string
  body: string
  // What the demo does on the live page when this step opens.
  action?: MapCommand['type']
}

const STEPS: Step[] = [
  {
    target: '#topics-map',
    title: '1. The research map',
    body: 'Every sphere is one arXiv Computer Science area. Bigger spheres have more papers, and lines join areas that share papers. Drag to rotate and scroll to zoom.',
  },
  {
    target: '#topics-map',
    title: '2. Click a topic once',
    body: 'The camera travels to it, and a panel shows its paper count, gap scores and connected topics.',
    action: 'select',
  },
  {
    target: '#topics-map',
    title: '3. Click it again to look inside',
    body: 'A second click opens the topic. The big spheres are its subtopics (papers grouped by meaning). The small dots around each one are papers.',
    action: 'drill',
  },
  {
    target: '#topics-map',
    title: '4. Click a paper dot',
    body: 'You get a plain-language summary, why it matters, and links to the paper on arXiv. Use "← All topics" to go back.',
    action: 'paper',
  },
  {
    target: '.search-wrap',
    title: '5. Search anything',
    body: 'Type a title, author, topic, school or country to jump straight to it.',
    action: 'exit',
  },
  {
    target: '#assistant',
    title: '6. Ask the archive',
    body: 'Ask a question in plain words. The assistant answers from the papers in the collection and cites each one.',
  },
  {
    target: '#gaps',
    title: '7. Find research gaps',
    body: 'See which areas are under-researched, and which have few papers from less-represented schools or countries.',
  },
  {
    target: '#papers',
    title: '8. Filter the papers',
    body: 'Filter by topic, country, institution and year, then open any paper. You can replay this guide any time from "How to use".',
  },
]

const SEEN_KEY = 'rdn-guide-seen-v1'

type Props = {
  open: boolean
  onClose: () => void
  onCommand: (command: MapCommand) => void
}

export function shouldAutoOpenGuide() {
  try {
    return !window.localStorage.getItem(SEEN_KEY) && !new URLSearchParams(window.location.search).has('noguide')
  } catch {
    return false
  }
}

export default function GuideTour({ open, onClose, onCommand }: Props) {
  const [index, setIndex] = useState(0)
  const [rect, setRect] = useState<DOMRect | null>(null)
  const step = STEPS[index]

  const measure = useCallback(() => {
    const element = document.querySelector(step.target)
    setRect(element ? element.getBoundingClientRect() : null)
  }, [step.target])

  const finish = useCallback(() => {
    try { window.localStorage.setItem(SEEN_KEY, '1') } catch { /* private mode */ }
    setIndex(0)
    onCommand({ type: 'exit', id: Date.now() })
    onClose()
  }, [onClose, onCommand])

  useEffect(() => { if (open) setIndex(0) }, [open])

  // Scroll the step's target into view and run its live demo action.
  useEffect(() => {
    if (!open) return
    const element = document.querySelector(step.target)
    element?.scrollIntoView({ behavior: 'smooth', block: step.target === '.search-wrap' ? 'start' : 'center' })
    if (step.action) onCommand({ type: step.action, id: Date.now() })
    const timers = [150, 500, 900].map((delay) => window.setTimeout(measure, delay))
    return () => timers.forEach((timer) => window.clearTimeout(timer))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, index])

  useLayoutEffect(() => {
    if (!open) return
    measure()
    window.addEventListener('resize', measure)
    window.addEventListener('scroll', measure, true)
    return () => {
      window.removeEventListener('resize', measure)
      window.removeEventListener('scroll', measure, true)
    }
  }, [open, measure])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') finish()
      if (event.key === 'ArrowRight') setIndex((value) => Math.min(STEPS.length - 1, value + 1))
      if (event.key === 'ArrowLeft') setIndex((value) => Math.max(0, value - 1))
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [finish, open])

  if (!open) return null
  const pad = 8
  const viewportHeight = window.innerHeight
  const box = rect && {
    top: Math.max(4, rect.top - pad),
    left: Math.max(4, rect.left - pad),
    width: Math.min(window.innerWidth - 8, rect.width + pad * 2),
    height: Math.min(viewportHeight - 8, rect.height + pad * 2),
  }
  // Put the card below the highlight if there is room, otherwise inside its top-left corner.
  const cardTop = box ? (box.top + box.height + 190 < viewportHeight ? box.top + box.height + 12 : Math.max(12, box.top + 12)) : viewportHeight / 2 - 90
  const cardLeft = box ? Math.min(Math.max(12, box.left + 12), window.innerWidth - 352) : window.innerWidth / 2 - 170

  return (
    <div className="guide-layer" role="dialog" aria-modal="true" aria-labelledby="guide-title">
      {box ? <div className="guide-spotlight" style={box} /> : <div className="guide-dim" />}
      <div className="guide-card" style={{ top: cardTop, left: cardLeft }}>
        <div className="guide-progress">{STEPS.map((item, position) => <i key={item.title} className={position <= index ? 'on' : ''} />)}</div>
        <h3 id="guide-title">{step.title}</h3>
        <p>{step.body}</p>
        <div className="guide-actions">
          <button className="guide-skip" onClick={finish}>{index === STEPS.length - 1 ? 'Close' : 'Skip guide'}</button>
          <span>{index + 1} / {STEPS.length}</span>
          {index > 0 && <button onClick={() => setIndex(index - 1)}>Back</button>}
          {index < STEPS.length - 1
            ? <button className="guide-next" onClick={() => setIndex(index + 1)}>Next</button>
            : <button className="guide-next" onClick={finish}>Start exploring</button>}
        </div>
      </div>
    </div>
  )
}
