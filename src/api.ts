// Local dev talks to uvicorn on :8001; a production build calls the same origin that served the page.
const apiUrl = (import.meta.env.VITE_API_URL ?? (import.meta.env.PROD ? '' : 'http://127.0.0.1:8001')).replace(/\/$/, '')

const RESTARTING = 'The server is busy or restarting. Wait a minute, then try again.'

// When the free Render instance restarts, the host answers with an HTML error page or an empty
// body instead of JSON. GET requests retry once after a short wait; anything else gets a readable
// message instead of "Unexpected token '<'".
export async function apiRequest<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const isGet = !init?.method || init.method.toUpperCase() === 'GET'
  let response: Response
  try {
    response = await fetch(`${apiUrl}${path}`, init)
  } catch {
    if (isGet && !retried) return retryLater<T>(path, init)
    throw new Error(RESTARTING)
  }
  const body = await response.text()
  let result: { detail?: string } | null = null
  try {
    result = body ? JSON.parse(body) : null
  } catch {
    result = null
  }
  if (result === null) {
    if (isGet && !retried && (response.status >= 500 || response.ok)) return retryLater<T>(path, init)
    throw new Error(response.status >= 500 || response.ok ? RESTARTING : `API returned ${response.status}`)
  }
  if (!response.ok) {
    throw new Error(result.detail ?? `API returned ${response.status}`)
  }
  return result as T
}

async function retryLater<T>(path: string, init?: RequestInit): Promise<T> {
  await new Promise((resolve) => setTimeout(resolve, 4000))
  return apiRequest<T>(path, init, true)
}

// `token` is either the admin token typed into the form or FIREBASE_SESSION, in which case a
// fresh Firebase ID token is fetched for every request (they expire after an hour).
export async function adminRequest<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  let bearer = token
  if (token === 'firebase-session') {
    const { firebaseIdToken } = await import('./auth')
    bearer = await firebaseIdToken()
  }
  return apiRequest<T>(path, {
    ...init,
    headers: { ...init?.headers, Authorization: `Bearer ${bearer}` },
  })
}
