// Local dev talks to uvicorn on :8001; a production build calls the same origin that served the page.
const apiUrl = (import.meta.env.VITE_API_URL ?? (import.meta.env.PROD ? '' : 'http://127.0.0.1:8001')).replace(/\/$/, '')

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiUrl}${path}`, init)
  const result = await response.json()
  if (!response.ok) {
    throw new Error(result.detail ?? `API returned ${response.status}`)
  }
  return result as T
}

export function adminRequest<T>(token: string, path: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(path, {
    ...init,
    headers: { ...init?.headers, Authorization: `Bearer ${token}` },
  })
}
