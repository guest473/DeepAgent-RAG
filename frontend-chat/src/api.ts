export type MessageStatus = 'streaming' | 'completed' | 'error'

export interface Message {
  role: 'user' | 'assistant'
  content: string
  thinkingTime?: number
  status?: MessageStatus
}

export interface Conversation {
  id: string
  title: string
  messages: Message[]
}

export interface UserInfo {
  id: number
  username: string
  role: 'user' | 'admin'
}

export interface FileItem {
  name: string
  path: string
  /** 是否已入库（已构建进向量库） */
  indexed: boolean
}

// ---------- auth helpers ----------

const TOKEN_KEY = 'deepagent_token'
const USER_KEY = 'deepagent_user'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function getUser(): UserInfo | null {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as UserInfo
  } catch {
    // localStorage 被写坏时不能让应用初始化整体崩掉（会白屏）：清掉坏值按未登录处理
    localStorage.removeItem(USER_KEY)
    return null
  }
}

function saveAuth(token: string, user: UserInfo) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

function authHeaders(): Record<string, string> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// 401 统一回调：由应用层注册（清空登录态并展示登录页）
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(fn: () => void) {
  unauthorizedHandler = fn
}

/**
 * 已鉴权接口的请求封装：任一请求返回 401 即视为登录态失效，
 * 统一清除本地凭证并通知应用层回到登录页。
 * 登录接口不使用它——那里的 401 表示账号密码错误，而非会话过期。
 */
async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(input, init)
  if (res.status === 401) {
    clearAuth()
    unauthorizedHandler?.()
  }
  return res
}

/**
 * 取服务端错误文案：FastAPI 的 detail 在业务错误时是字符串，
 * 在 422 校验失败时是数组/对象，只有字符串可直接展示，否则回落到兜底文案。
 */
async function errorMessage(res: Response, fallback: string): Promise<string> {
  const data = (await res.json().catch(() => null)) as { detail?: unknown } | null
  const detail = data?.detail
  return typeof detail === 'string' && detail ? detail : fallback
}

// ---------- auth API ----------

export async function apiRegister(username: string, password: string): Promise<void> {
  const res = await fetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || '注册失败')
  }
}

export async function apiLogin(username: string, password: string): Promise<UserInfo> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  if (!res.ok) {
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || '登录失败')
  }
  const data = await res.json()
  saveAuth(data.token, data.user)
  return data.user
}

export async function apiMe(): Promise<UserInfo> {
  const res = await apiFetch('/api/auth/me', { headers: authHeaders() })
  if (!res.ok) throw new Error('未登录')
  const data = await res.json()
  return data.user
}

// ---------- conversation API ----------

export async function fetchConversations(): Promise<Record<string, Conversation>> {
  const res = await apiFetch('/api/conversations', { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取会话列表失败'))
  const data = await res.json()
  const convs: Record<string, Conversation> = {}
  for (const c of data.conversations as Conversation[]) {
    convs[c.id] = c
  }
  return convs
}

export async function createConversationApi(title: string): Promise<Conversation> {
  const res = await apiFetch('/api/conversations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '创建会话失败'))
  return res.json()
}

export async function updateConversationTitle(id: string, title: string): Promise<void> {
  const res = await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ title }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '更新标题失败'))
}

export async function appendMessageApi(
  conversationId: string,
  role: 'user' | 'assistant',
  content: string,
  status?: MessageStatus,
): Promise<void> {
  const res = await apiFetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ role, content, status }),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '保存消息失败'))
}

export async function deleteConversationApi(id: string): Promise<void> {
  const res = await apiFetch(`/api/conversations/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
  if (!res.ok) throw new Error(await errorMessage(res, '删除会话失败'))
}

// ---------- chat API ----------

export async function streamChat(
  message: string,
  threadId: string,
  onChunk: (text: string) => void,
  onDone: () => void,
  onError: (err: string) => void,
  useOnlineSearch: boolean = false,
  signal?: AbortSignal,
) {
  const res = await apiFetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ message, thread_id: threadId, use_online_search: useOnlineSearch }),
    signal,
  })

  if (!res.ok) {
    if (res.status === 401) {
      onError('登录已过期，请重新登录')
      return
    }
    // 422 由 Pydantic 校验产生（detail 为数组），给可读提示而非裸状态码
    if (res.status === 422) {
      onError('输入内容不合法（可能超出长度上限）')
      return
    }
    // 400 / 429 等：服务端的 detail 已是可直接展示的文案
    onError(await errorMessage(res, `请求失败: ${res.status}`))
    return
  }

  const reader = res.body?.getReader()
  if (!reader) {
    onError('无法读取响应流')
    return
  }

  const decoder = new TextDecoder()
  let buffer = ''
  // 流中已通过 done/error 事件收尾时，循环结束后的兜底 onDone 不再触发，
  // 避免把 error 状态覆盖回 completed
  let finished = false

  while (true) {
    const { done, value } = await reader.read()
    if (done) break

    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue
      try {
        const data = JSON.parse(line.slice(6))
        if (data.content) onChunk(data.content)
        if (data.done) { finished = true; onDone() }
        if (data.error) { finished = true; onError(data.error) }
      } catch {
        /* skip malformed */
      }
    }
  }
  if (!finished) onDone()
}

// ---------- knowledge API ----------

export async function fetchFiles(): Promise<FileItem[]> {
  const res = await apiFetch('/api/files', { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取文件列表失败'))
  const data = await res.json()
  return data.files
}

export async function uploadFiles(files: File[]): Promise<{
  uploaded: { name: string; overwritten: boolean; indexed: boolean; error: string | null }[]
  errors: { name: string; error: string }[]
}> {
  const form = new FormData()
  files.forEach(f => form.append('files', f))
  const res = await apiFetch('/api/files/upload', { method: 'POST', headers: authHeaders(), body: form })
  if (!res.ok) throw new Error(await errorMessage(res, '上传失败'))
  return res.json()
}

export async function deleteFile(name: string): Promise<void> {
  const res = await apiFetch(`/api/files/${encodeURIComponent(name)}`, { method: 'DELETE', headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '删除失败'))
}

export async function buildVector(): Promise<{ message: string; failures: { file: string; error: string }[] }> {
  const res = await apiFetch('/api/vector/build', { method: 'POST', headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '构建失败'))
  return res.json()
}

export interface HealthIssue {
  type: string
  severity: 'warn' | 'error'
  title: string
  detail?: string
  count: number
  items: string[]
}

export interface HealthReport {
  checked_at: string | null
  healthy: boolean
  milvus_error?: string | null
  stats: { files: number; indexed_files: number; chunks: number }
  issues: HealthIssue[]
  repair?: {
    ghost_deleted: string[]
    ghost_errors: string[]
    md5_rebuilt: boolean
    re_indexed: string[]
    re_index_failures: { file: string; error: string }[]
  }
}

export async function fetchIndexHealth(refresh = false): Promise<HealthReport> {
  const res = await apiFetch(`/api/index/health${refresh ? '?refresh=1' : ''}`, { headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '获取索引健康状态失败'))
  return res.json()
}

export async function repairIndexHealth(): Promise<HealthReport> {
  const res = await apiFetch('/api/index/health/repair', { method: 'POST', headers: authHeaders() })
  if (!res.ok) throw new Error(await errorMessage(res, '修复失败'))
  return res.json()
}