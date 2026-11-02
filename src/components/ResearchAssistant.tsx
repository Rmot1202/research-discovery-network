import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { apiRequest } from '../api'
import type { ChatPaper, ChatResponse, Topic } from '../types'
import { ArrowIcon, SparkIcon } from './Icons'

type Message = {
  role: 'user' | 'assistant'
  content: string
  papers?: ChatPaper[]
  notice?: string
  retrieval?: string
}

type ResearchAssistantProps = {
  topics: Topic[]
  scope: string
  onScopeChange: (code: string) => void
  prompt: { text: string; id: number } | null
}

const starterQuestions = [
  'What research exists on 3D printer security?',
  'Summarize recent work on pose estimation for robotics.',
  'Which papers use LSTM or GRU models for forecasting?',
  'Find papers about digital forensics tools.',
]

function renderInline(text: string, papers: ChatPaper[] = []): ReactNode[] {
  const byId = new Map(papers.map((paper) => [paper.arxiv_id, paper]))
  const parts = text.split(/(\*\*[^*]+\*\*|\[arXiv:[^\]]+\])/g)
  return parts.map((part, index) => {
    const citation = part.match(/^\[arXiv:([^\]]+)\]$/)
    if (citation) {
      const ids = citation[1].split(/[,;]\s*(?:arXiv:)?/).map((id) => id.trim()).filter(Boolean)
      return (
        <span key={index}>
          {ids.map((id) => {
            const paper = byId.get(id)
            return (
              <a key={id} className="cite" href={paper?.url ?? `https://arxiv.org/abs/${id}`} target="_blank" rel="noreferrer" title={paper?.title ?? id}>
                {id}
              </a>
            )
          })}
        </span>
      )
    }
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>
    return <span key={index}>{part}</span>
  })
}

function AnswerText({ content, papers }: { content: string; papers?: ChatPaper[] }) {
  const blocks: ReactNode[] = []
  let bullets: string[] = []
  const flush = () => {
    if (!bullets.length) return
    blocks.push(<ul key={`list-${blocks.length}`}>{bullets.map((item, index) => <li key={index}>{renderInline(item, papers)}</li>)}</ul>)
    bullets = []
  }
  for (const rawLine of content.split('\n')) {
    const line = rawLine.trim()
    const bullet = line.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/)
    if (bullet) { bullets.push(bullet[1]); continue }
    flush()
    if (!line) continue
    const heading = line.match(/^#{1,4}\s+(.*)$/)
    blocks.push(heading
      ? <h4 key={`h-${blocks.length}`}>{renderInline(heading[1], papers)}</h4>
      : <p key={`p-${blocks.length}`}>{renderInline(line, papers)}</p>)
  }
  flush()
  return <div className="answer-text">{blocks}</div>
}

export default function ResearchAssistant({ topics, scope, onScopeChange, prompt }: ResearchAssistantProps) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isAsking, setIsAsking] = useState(false)
  const [error, setError] = useState('')
  const [status, setStatus] = useState<{ ai_configured: boolean; chat_model: string | null } | null>(null)
  const logRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    void apiRequest<{ ai_configured: boolean; chat_model: string | null }>('/api/chat/status')
      .then(setStatus)
      .catch(() => setStatus(null))
  }, [])

  useEffect(() => {
    if (!prompt) return
    setInput(prompt.text)
    inputRef.current?.focus()
  }, [prompt])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages, isAsking])

  const ask = async (question: string) => {
    const trimmed = question.trim()
    if (trimmed.length < 2 || isAsking) return
    const history = messages.map(({ role, content }) => ({ role, content }))
    setMessages((current) => [...current, { role: 'user', content: trimmed }])
    setInput('')
    setIsAsking(true)
    setError('')
    try {
      const result = await apiRequest<ChatResponse>('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: trimmed, history, category: scope || null }),
      })
      setMessages((current) => [...current, {
        role: 'assistant',
        content: result.answer ?? (result.papers.length
          ? 'Here are the best-matching papers in the archive:'
          : 'No papers in the archive matched that question. Try different keywords or a broader topic.'),
        papers: result.papers,
        notice: result.notice,
        retrieval: result.retrieval,
      }])
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'The research assistant could not answer.')
    } finally {
      setIsAsking(false)
    }
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void ask(input)
  }

  const scopeTopic = topics.find((topic) => topic.code === scope)

  return (
    <section className="assistant-section" id="assistant">
      <div className="paper-explorer-heading">
        <div>
          <span className="eyebrow"><SparkIcon /> Research assistant</span>
          <h2>Ask the archive</h2>
          <p>Questions are answered from the imported arXiv papers, with every claim linked back to its paper.</p>
        </div>
        <strong className={`assistant-status ${status?.ai_configured ? 'is-on' : ''}`}>
          {status === null ? 'API offline' : status.ai_configured ? `Model · ${status.chat_model}` : 'Search-only mode'}
        </strong>
      </div>

      <div className="assistant-shell">
        <div className="assistant-log" ref={logRef} aria-live="polite">
          {messages.length === 0 && (
            <div className="assistant-empty">
              <p>Try one of these, or ask your own question:</p>
              <div className="starter-list">
                {starterQuestions.map((question) => (
                  <button key={question} onClick={() => void ask(question)}>{question}</button>
                ))}
              </div>
            </div>
          )}
          {messages.map((message, index) => (
            <article key={index} className={`chat-message ${message.role}`}>
              {message.role === 'user' ? <p>{message.content}</p> : <AnswerText content={message.content} papers={message.papers} />}
              {message.notice && <p className="chat-notice">{message.notice}</p>}
              {message.papers && message.papers.length > 0 && (
                <details className="chat-sources" open={!message.notice ? undefined : true}>
                  <summary>{message.papers.length} papers retrieved{message.retrieval ? ` · ${message.retrieval} search` : ''}</summary>
                  <ol>
                    {message.papers.map((paper) => (
                      <li key={paper.arxiv_id}>
                        <a href={paper.url} target="_blank" rel="noreferrer">{paper.title} <ArrowIcon /></a>
                        <small>{paper.arxiv_id} · {paper.topic} · {paper.published_at.slice(0, 4)} · {paper.authors.slice(0, 3).join(', ')}{paper.authors.length > 3 ? ' et al.' : ''}</small>
                      </li>
                    ))}
                  </ol>
                </details>
              )}
            </article>
          ))}
          {isAsking && <div className="chat-message assistant thinking"><span /><span /><span /> Searching the archive…</div>}
        </div>
        {error && <p className="admin-error">{error}</p>}
        <form className="assistant-form" onSubmit={submit}>
          <label className="assistant-scope">Scope
            <select value={scope} onChange={(event) => onScopeChange(event.target.value)}>
              <option value="">All topics</option>
              {topics.map((topic) => <option key={topic.code} value={topic.code}>{topic.code} · {topic.label}</option>)}
            </select>
          </label>
          <textarea
            ref={inputRef}
            rows={2}
            value={input}
            maxLength={1000}
            placeholder={scopeTopic ? `Ask about ${scopeTopic.label} papers…` : 'Ask about any paper, method, or topic in the archive…'}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void ask(input) }
            }}
          />
          <button className="primary" type="submit" disabled={isAsking || input.trim().length < 2}>Ask <ArrowIcon /></button>
        </form>
        <p className="filter-note">Answers are AI drafts grounded in the retrieved paper abstracts. Check the linked papers before relying on a result.</p>
      </div>
    </section>
  )
}
