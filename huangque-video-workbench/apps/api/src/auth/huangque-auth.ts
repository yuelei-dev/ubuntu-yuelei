export type HuangqueIdentity = {username: string; role: string};
export const AUTH_UPSTREAM_TIMEOUT_MS = 2_000;
export const LEGACY_UNOWNED_USERNAME = '__huangque_legacy_unowned__';

export class HuangqueAuthenticationError extends Error {
  constructor(readonly statusCode: 401 | 503) {
    super(statusCode === 401 ? 'Huangque session is invalid' : 'Huangque authentication is unavailable');
  }
}

const sessionCookie = (cookieHeader: string | undefined): string | undefined => {
  const session = cookieHeader?.split(';').map((cookie) => cookie.trim()).find((cookie) => cookie.startsWith('hq_session='));
  return session && session.length > 'hq_session='.length ? session : undefined;
};

const identityFrom = (payload: unknown): HuangqueIdentity | undefined => {
  const candidate = typeof payload === 'object' && payload !== null && 'user' in payload
    ? (payload as {user: unknown}).user
    : payload;
  if (typeof candidate !== 'object' || candidate === null) return undefined;
  const {username, role} = candidate as Record<string, unknown>;
  return typeof username === 'string' && username.length > 0 && typeof role === 'string' && role.length > 0
    ? {username, role}
    : undefined;
};

/** Validates Huangque's HttpOnly session without forwarding unrelated cookies. */
const authenticationEndpoint = (authBase: string | undefined): string => {
  const base = authBase?.trim() || 'http://127.0.0.1:8095';
  return new URL('api/auth/me', base.endsWith('/') ? base : `${base}/`).toString();
};

export const authenticateHuangque = async (
  cookieHeader: string | undefined, signal: AbortSignal, authBase: string | undefined = process.env.HUANGQUE_AUTH_BASE
): Promise<HuangqueIdentity> => {
  const cookie = sessionCookie(cookieHeader);
  if (!cookie) throw new HuangqueAuthenticationError(401);

  const upstream = new AbortController();
  const abortUpstream = (): void => upstream.abort();
  const timeout = setTimeout(abortUpstream, AUTH_UPSTREAM_TIMEOUT_MS);
  if (signal.aborted) abortUpstream();
  else signal.addEventListener('abort', abortUpstream, {once: true});
  try {
    const response = await fetch(authenticationEndpoint(authBase), {headers: {cookie}, signal: upstream.signal});
    if (response.status === 401 || response.status === 403) throw new HuangqueAuthenticationError(401);
    if (!response.ok) throw new HuangqueAuthenticationError(503);
    const identity = identityFrom(await response.json());
    if (!identity) throw new HuangqueAuthenticationError(503);
    if (identity.username === LEGACY_UNOWNED_USERNAME) throw new HuangqueAuthenticationError(401);
    return identity;
  } catch (error) {
    if (error instanceof HuangqueAuthenticationError) throw error;
    throw new HuangqueAuthenticationError(503);
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', abortUpstream);
  }
};
