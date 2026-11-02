import { useCallback, useEffect, useRef, useState } from 'react'
import { adminRequest, apiRequest } from '../api'
import type { AffiliationScanProgress } from '../types'

type Props = { token: string; onFound?: () => void }

export default function AffiliationScanPanel({ token, onFound }: Props) {
  const [progress, setProgress] = useState<AffiliationScanProgress | null>(null)
  const [pdfFallback, setPdfFallback] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const lastNotified = useRef({ at: 0, found: -1 })

  const refresh = useCallback(async () => {
    try {
      setProgress(await apiRequest<AffiliationScanProgress>('/api/affiliations/progress'))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not load scan progress.')
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])
  useEffect(() => {
    if (!progress?.is_running) return
    const timer = window.setInterval(() => { void refresh() }, 4000)
    return () => window.clearInterval(timer)
  }, [onFound, progress?.is_running, refresh])

  // Refresh gap scores and paper cards at most every 30 s while new affiliations arrive.
  useEffect(() => {
    if (!progress || progress.found === lastNotified.current.found) return
    const now = Date.now()
    if (lastNotified.current.found !== -1 && now - lastNotified.current.at < 30_000 && progress.is_running) return
    const first = lastNotified.current.found === -1
    lastNotified.current = { at: now, found: progress.found }
    if (!first) onFound?.()
  }, [onFound, progress])

  const act = async (path: string) => {
    setBusy(true)
    setError('')
    try {
      setProgress(await adminRequest<AffiliationScanProgress>(token, path, { method: 'POST' }))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not change the scan.')
    } finally {
      setBusy(false)
    }
  }

  const running = progress?.is_running
  const label = running ? 'Scanning…' : progress?.status === 'paused' || progress?.status === 'error' ? 'Resume scan' : 'Scan papers for affiliations'
  return (
    <div className="analysis-controls scan-panel">
      <div>
        <strong>Paper scan · institutions, schools, countries</strong>
        <p>
          Walks every paper missing an affiliation and fills it from OpenAlex (author → institution links, 50 papers per request)
          and, optionally, from the paper's PDF (page 1 and the end of the last page, one paper every 3 s). Each value keeps its
          source and verbatim evidence and is marked unreviewed; existing values are never overwritten.
        </p>
        {progress && (
          <>
            <div className="progress-track"><i style={{ width: `${Math.min(100, progress.documented_pct)}%` }} /></div>
            <p className="scan-stats">
              <b>{progress.documented_papers.toLocaleString()}</b> of {progress.total_papers.toLocaleString()} papers documented ({progress.documented_pct}%)
              {' · '}checked {progress.checked.toLocaleString()} · found {progress.found.toLocaleString()}
              {' '}(OpenAlex {progress.found_openalex.toLocaleString()}, PDF {progress.found_pdf.toLocaleString()})
              {progress.remaining !== null && ` · ${progress.remaining.toLocaleString()} left in this pass`}
              {' · '}status: {running ? 'running' : progress.status}
            </p>
            {progress.error && <p className="scan-error">{progress.error}</p>}
          </>
        )}
        {error && <p className="scan-error" role="alert">{error}</p>}
      </div>
      <div className="scan-actions">
        <label className="scan-toggle">
          <input type="checkbox" checked={pdfFallback} disabled={running} onChange={(event) => setPdfFallback(event.target.checked)} />
          Also read PDFs when OpenAlex has nothing (slow)
        </label>
        {running
          ? <button className="admin-close" disabled={busy} onClick={() => void act('/api/admin/affiliations/pause')}>Pause scan</button>
          : <button className="primary" disabled={busy} onClick={() => void act(`/api/admin/affiliations/scan?pdf_fallback=${pdfFallback}`)}>{label}</button>}
      </div>
    </div>
  )
}
