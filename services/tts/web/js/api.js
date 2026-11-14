// API client: /api/* is forwarded in-process to the TTS API with the session cookie (the key stays on the server).
export class ApiError extends Error {
  constructor(message, status = 0, detail = '') {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

let unauthorized = () => {};
export const onUnauthorized = (fn) => { unauthorized = fn; };
export const apiUrl = (path) => `/api${path}`;

function detailText(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && d.msg ? `${(d.loc || []).slice(1).join('.')}${d.loc && d.loc.length > 1 ? ': ' : ''}${d.msg}` : String(d))).join('; ');
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return detail ? String(detail) : '';
}

export async function errorFrom(res) {
  let detail = '';
  try {
    const ct = res.headers.get('content-type') || '';
    detail = ct.includes('json') ? detailText((await res.json()).detail) : (await res.text()).slice(0, 300);
  } catch { /* empty body */ }
  switch (res.status) {
    case 401: unauthorized(); return new ApiError('Сесія завершилась — увійдіть знову.', 401, detail);
    case 429: return new ApiError('Сервер зайнятий, спробуйте за хвилину.', 429, detail);
    case 413: return new ApiError(`Текст задовгий. ${detail}`.trim(), 413, detail);
    case 404: return new ApiError(detail && detail !== 'Not Found' ? `Не знайдено: ${detail}` : 'Не знайдено (можливо, вже видалено).', 404, detail);
    default:
      if (res.status >= 500) return new ApiError(`Помилка сервера (${res.status})${detail ? `: ${detail}` : ''}`, res.status, detail);
      return new ApiError(detail || `Помилка ${res.status}`, res.status, detail);
  }
}

/** api('/v1/voices'), api('/v1/jobs', {method: 'POST', json: {...}}), {raw: true} returns the Response. */
export async function api(path, { method = 'GET', json, body, signal, raw = false, headers = {} } = {}) {
  const opts = { method, signal, credentials: 'same-origin', headers: { 'X-Requested-With': 'ozvuchka', ...headers } };
  if (json !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(json);
  } else if (body !== undefined) opts.body = body;
  let res;
  try {
    res = await fetch(apiUrl(path), opts);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError('Немає з’єднання з сервером. Перевірте мережу й спробуйте ще раз.', 0);
  }
  if (!res.ok) throw await errorFrom(res);
  if (raw) return res;
  const ct = res.headers.get('content-type') || '';
  return ct.includes('json') ? res.json() : res.text();
}

// ---------- login session (/auth/* lives on the UI server itself)
async function authCall(path, method = 'GET', json) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: { 'X-Requested-With': 'ozvuchka', ...(json ? { 'Content-Type': 'application/json' } : {}) },
    body: json ? JSON.stringify(json) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* ignore */ }
  return { ok: res.ok, status: res.status, data };
}

export const auth = {
  me: () => authCall('/auth/me'),
  login: (username, password) => authCall('/auth/login', 'POST', { username, password }),
  logout: () => authCall('/auth/logout', 'POST'),
};
