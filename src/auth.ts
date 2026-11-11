// Admin sign-in with Firebase Authentication (Google accounts).
// The Firebase web settings come from the API (/api/auth/config), so nothing is baked into the build.
// Firebase only proves who you are; the API decides who is an admin (ADMIN_EMAILS).
import type { Auth, User } from 'firebase/auth'
import { apiRequest } from './api'

export const FIREBASE_SESSION = 'firebase-session'

export type AuthConfig = {
  firebase: { apiKey: string; authDomain: string; projectId: string } | null
  token_login: boolean
}

let authPromise: Promise<Auth | null> | null = null

export function getAuthConfig(): Promise<AuthConfig> {
  return apiRequest<AuthConfig>('/api/auth/config')
}

async function getFirebaseAuth(): Promise<Auth | null> {
  if (!authPromise) {
    authPromise = (async () => {
      const config = await getAuthConfig()
      if (!config.firebase) return null
      const [{ initializeApp, getApps }, { getAuth }] = await Promise.all([
        import('firebase/app'),
        import('firebase/auth'),
      ])
      const app = getApps()[0] ?? initializeApp(config.firebase)
      return getAuth(app)
    })()
  }
  return authPromise
}

export async function signInWithGoogle(): Promise<User> {
  const auth = await getFirebaseAuth()
  if (!auth) throw new Error('Firebase sign-in is not configured on the server.')
  const { GoogleAuthProvider, signInWithPopup } = await import('firebase/auth')
  const result = await signInWithPopup(auth, new GoogleAuthProvider())
  return result.user
}

export async function signOutFirebase(): Promise<void> {
  const auth = await getFirebaseAuth()
  if (auth) await (await import('firebase/auth')).signOut(auth)
}

/** Waits for Firebase to restore a previous session (if any). */
export async function currentUser(): Promise<User | null> {
  const auth = await getFirebaseAuth()
  if (!auth) return null
  await auth.authStateReady()
  return auth.currentUser
}

/** A fresh ID token; Firebase refreshes it automatically when it is close to expiring (1 hour). */
export async function firebaseIdToken(): Promise<string> {
  const user = await currentUser()
  if (!user) throw new Error('Your admin sign-in expired. Sign in again.')
  return user.getIdToken()
}
