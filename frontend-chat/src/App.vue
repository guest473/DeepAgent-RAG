<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import {
  fetchConversations,
  createConversationApi,
  updateConversationTitle,
  appendMessageApi,
  deleteConversationApi,
  streamChat,
  getUser,
  clearAuth,
  setUnauthorizedHandler,
  apiLogin,
  apiRegister,
  apiMe,
  fetchFiles,
  uploadFiles,
  deleteFile,
  buildVector,
  fetchIndexHealth,
  repairIndexHealth,
  type Conversation,
  type UserInfo,
  type FileItem,
  type HealthReport,
} from './api'

marked.setOptions({ breaks: true, gfm: true })

// ---------- auth state ----------
const user = ref<UserInfo | null>(getUser())
const authMode = ref<'login' | 'register'>('login')
const authUsername = ref('')
const authPassword = ref('')
const authPassword2 = ref('')
const authError = ref('')
const authSuccess = ref('')
const authLoading = ref(false)

// 在途的流式请求：可被主动取消（401、退出登录、组件卸载），
// 取消后 fetch 连接断开，后端会随之停止生成
let streamAbort: AbortController | null = null

// 任意已鉴权接口返回 401（token 过期、账号被删或降权后复核失败）时统一回到登录页，
// 不再依赖各调用点各自处理
setUnauthorizedHandler(() => {
  // 先掐掉在途的流与健康轮询：否则后端仍在生成、定时器还会继续空转
  streamAbort?.abort()
  stopHealthPolling()
  user.value = null
  mode.value = 'chat'
  localStorage.setItem(MODE_KEY, 'chat')
  authMode.value = 'login'
  authError.value = '登录已过期，请重新登录'
})

// ---------- app mode ----------
type AppMode = 'chat' | 'knowledge'
const MODE_KEY = 'deepagent_mode'
function loadMode(): AppMode {
  return localStorage.getItem(MODE_KEY) === 'knowledge' ? 'knowledge' : 'chat'
}
const mode = ref<AppMode>(loadMode())

// ---------- chat state ----------
const conversations = ref<Record<string, Conversation>>({})
const currentId = ref('')
const input = ref('')
const isProcessing = ref(false)
const isThinking = ref(false)
const thinkingSeconds = ref(0)
let thinkingTimer: number | null = null
let thinkingStartTime = 0
const chatArea = ref<HTMLElement | null>(null)
const useOnlineSearch = ref(false)
const showAnnouncement = ref(true)

async function initConversations() {
  if (!user.value) return
  let loaded: Record<string, Conversation> = {}
  try {
    loaded = await fetchConversations()
  } catch {
    loaded = {}
  }
  // 若无会话，由服务端创建一个（会话 id 由服务端生成，本地不再自造 id）
  if (Object.keys(loaded).length === 0) {
    try {
      const conv = await createConversationApi('新对话')
      loaded[conv.id] = conv
    } catch (e) {
      handlePersistError('创建会话', e)
    }
  }
  conversations.value = loaded
  currentId.value = Object.keys(loaded)[0]
}

function dismissAnnouncement() { showAnnouncement.value = false }
function enableSearchFromAnnouncement() { useOnlineSearch.value = true }
watch(useOnlineSearch, (val) => { if (val) showAnnouncement.value = false })

// 思考计时器
function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = (seconds % 60).toFixed(1)
  return m > 0 ? `${m}分${s}秒` : `${s}秒`
}

watch(isThinking, (val) => {
  if (val) {
    thinkingSeconds.value = 0
    const tick = () => {
      thinkingSeconds.value = +((Date.now() - thinkingStartTime) / 1000).toFixed(1)
      thinkingTimer = requestAnimationFrame(tick)
    }
    thinkingTimer = requestAnimationFrame(tick)
  } else {
    if (thinkingTimer != null) { cancelAnimationFrame(thinkingTimer); thinkingTimer = null }
  }
})

const current = computed(() => conversations.value[currentId.value])
const convList = computed(() => Object.values(conversations.value))
const hasEmptyConv = computed(() => convList.value.some(c => c.messages.length === 0))

function scrollToBottom() {
  nextTick(() => {
    if (chatArea.value) chatArea.value.scrollTop = chatArea.value.scrollHeight
  })
}

function renderMarkdown(text: string) { return DOMPurify.sanitize(marked.parse(text) as string) }

async function newConversation() {
  if (isProcessing.value) return
  try {
    const conv = await createConversationApi('新对话')
    conversations.value[conv.id] = conv
    currentId.value = conv.id
  } catch (e) {
    handlePersistError('创建会话', e)
  }
}

function selectConversation(id: string) {
  if (isProcessing.value) return
  currentId.value = id
}

function deleteConversation(id: string) {
  if (isProcessing.value) return
  delete conversations.value[id]
  deleteConversationApi(id).catch(e => handlePersistError('删除会话', e))
  const ids = Object.keys(conversations.value)
  if (ids.length === 0) {
    newConversation()
  } else if (currentId.value === id) {
    currentId.value = ids[0]
  }
}

async function sendMessage() {
  const text = input.value.trim()
  if (!text || isProcessing.value) return
  const conv = conversations.value[currentId.value]
  conv.messages.push({ role: 'user', content: text })
  appendMessageApi(currentId.value, 'user', text).catch(e => handlePersistError('保存用户消息', e))
  if (conv.messages.length === 1) {
    conv.title = text.length > 20 ? text.slice(0, 20) + '...' : text
    updateConversationTitle(currentId.value, conv.title).catch(e => handlePersistError('更新标题', e))
  }
  input.value = ''
  thinkingStartTime = Date.now()
  isProcessing.value = true
  isThinking.value = true
  scrollToBottom()

  const assistantMsg = { role: 'assistant' as const, content: '', status: 'streaming' as const }
  conv.messages.push(assistantMsg)
  const idx = conv.messages.length - 1

  const captureThinkingTime = () => {
    if (conv.messages[idx].thinkingTime == null) {
      conv.messages[idx].thinkingTime = +((Date.now() - thinkingStartTime) / 1000).toFixed(1)
    }
  }

  // 流结束/出错时仅落库一次，避免 streamChat 的 onDone 被多次触发造成重复消息
  let assistantPersisted = false
  const persistAssistant = () => {
    if (assistantPersisted) return
    assistantPersisted = true
    appendMessageApi(
      currentId.value, 'assistant', conv.messages[idx].content, conv.messages[idx].status,
    ).catch(e => handlePersistError('保存回复', e))
  }

  // 上一次可能仍在途（正常情况下已被 isProcessing 挡住），先取消再开启新的
  streamAbort?.abort()
  const ac = new AbortController()
  streamAbort = ac

  try {
    await streamChat(
      text, currentId.value,
      chunk => { if (isThinking.value) { captureThinkingTime(); isThinking.value = false } conv.messages[idx].content += chunk; scrollToBottom() },
      () => { captureThinkingTime(); conv.messages[idx].status = 'completed'; isProcessing.value = false; isThinking.value = false; if (!conv.messages[idx].content) conv.messages[idx].content = '（未收到回复）'; scrollToBottom(); persistAssistant() },
      err => { captureThinkingTime(); conv.messages[idx].status = 'error'; conv.messages[idx].content = `错误: ${err}`; isProcessing.value = false; isThinking.value = false; scrollToBottom(); persistAssistant() },
      useOnlineSearch.value,
      ac.signal,
    )
  } catch {
    captureThinkingTime()
    if (ac.signal.aborted) {
      // 主动取消（401 / 退出登录 / 组件卸载）：连接已断开，不当作网络错误
      conv.messages[idx].status = 'completed'
      conv.messages[idx].content = conv.messages[idx].content
        ? `${conv.messages[idx].content}\n\n（已取消）`
        : '（已取消）'
    } else {
      conv.messages[idx].status = 'error'
      conv.messages[idx].content = '网络错误，请检查连接后重试'
    }
    isProcessing.value = false
    isThinking.value = false
    scrollToBottom()
    persistAssistant()
  } finally {
    if (streamAbort === ac) streamAbort = null
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage() }
}

// ---------- knowledge state ----------
const files = ref<FileItem[]>([])
const knowledgeLoading = ref(false)
const knowledgeBuilding = ref(false)
const knowledgeUploading = ref(false)
const knowledgeMsg = ref('')
const knowledgeMsgType = ref<'success' | 'error' | ''>('')
const dragOver = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

// ---------- 对话持久化失败提示 ----------
const persistNotice = ref('')
let persistNoticeTimer: number | null = null
function handlePersistError(op: string, e: unknown) {
  console.error(`[持久化失败] ${op}:`, e)
  persistNotice.value = '消息保存失败，刷新页面可能丢失最新内容'
  if (persistNoticeTimer !== null) clearTimeout(persistNoticeTimer)
  persistNoticeTimer = window.setTimeout(() => { persistNotice.value = '' }, 5000)
}

// ---------- index health state ----------
const health = ref<HealthReport | null>(null)
const healthChecking = ref(false)
const healthRepairing = ref(false)
const isAdmin = computed(() => user.value?.role === 'admin')
const healthIssueCount = ref(0)
let healthPollTimer: number | null = null

function updateHealthBadge() {
  healthIssueCount.value = health.value ? health.value.issues.length : 0
}

async function refreshHealthBadge() {
  if (!isAdmin.value) return
  try {
    const report = await fetchIndexHealth()
    healthIssueCount.value = report.issues.length
  } catch { /* 静默失败，保留原角标值 */ }
}

function startHealthPolling() {
  if (healthPollTimer !== null) { clearInterval(healthPollTimer); healthPollTimer = null }
  if (!isAdmin.value) return
  refreshHealthBadge()
  healthPollTimer = window.setInterval(refreshHealthBadge, 60_000)
}

function stopHealthPolling() {
  if (healthPollTimer !== null) { clearInterval(healthPollTimer); healthPollTimer = null }
  healthIssueCount.value = 0
}

async function loadFiles() {
  knowledgeLoading.value = true
  try { files.value = await fetchFiles() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { knowledgeLoading.value = false }
}

function showKnowledgeMsg(text: string, type: 'success' | 'error') {
  knowledgeMsg.value = text
  knowledgeMsgType.value = type
  setTimeout(() => { knowledgeMsg.value = ''; knowledgeMsgType.value = '' }, 4000)
}

async function handleUpload(fileList: FileList | File[]) {
  const arr = Array.from(fileList)
  if (arr.length === 0) return
  knowledgeUploading.value = true
  try {
    const result = await uploadFiles(arr)
    const uploadedNames = result.uploaded.map(u => u.overwritten ? `${u.name}(覆盖)` : u.name)
    // 落盘成功但入库失败的，与上传错误并列展示，管理员可当场得知未被检索的文件
    const indexFailures = result.uploaded
      .filter(u => !u.indexed)
      .map(u => `${u.name}: ${u.error || '入库失败'}`)
    const errorTexts = [...result.errors.map(e => `${e.name}: ${e.error}`), ...indexFailures]

    if (uploadedNames.length && !errorTexts.length) {
      showKnowledgeMsg(`已上传并入库: ${uploadedNames.join(', ')}`, 'success')
    } else if (errorTexts.length) {
      const prefix = uploadedNames.length ? `已上传: ${uploadedNames.join(', ')}。以下未成功：` : '失败：'
      showKnowledgeMsg(`${prefix}${errorTexts.join('；')}`, 'error')
    } else {
      showKnowledgeMsg('上传失败：没有有效文件', 'error')
    }
    await loadFiles()
  } catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { knowledgeUploading.value = false }
}

function onFileSelect(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files) handleUpload(input.files)
  input.value = ''
}

function onDrop(e: DragEvent) {
  dragOver.value = false
  if (e.dataTransfer?.files) handleUpload(e.dataTransfer.files)
}

async function handleDeleteFile(name: string) {
  if (!confirm(`确定删除 ${name}？`)) return
  try { await deleteFile(name); showKnowledgeMsg(`已删除: ${name}`, 'success'); await loadFiles() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
}

async function handleBuild() {
  knowledgeBuilding.value = true
  try {
    const { message, failures } = await buildVector()
    if (failures && failures.length) {
      // 部分失败必须醒目提示：列出失败文件与原因（路径只显示文件名）
      const detail = failures
        .map(f => `${f.file.split(/[\\/]/).pop()}: ${f.error}`)
        .join('；')
      showKnowledgeMsg(`${message}（${detail}）`, 'error')
    } else {
      showKnowledgeMsg(message, 'success')
    }
    await loadFiles()
    await loadHealth(true)
  }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { knowledgeBuilding.value = false }
}

// ---------- index health actions ----------
async function loadHealth(refresh = false) {
  try { health.value = await fetchIndexHealth(refresh); updateHealthBadge() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
}

async function handleHealthCheck() {
  healthChecking.value = true
  try { health.value = await fetchIndexHealth(true); updateHealthBadge() }
  catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { healthChecking.value = false }
}

async function handleHealthRepair() {
  if (!confirm('确认立即手动修复？系统定时自愈会自动处理，此处将马上清理残留向量、重建入库记录，并重新索引向量丢失的文件。')) return
  healthRepairing.value = true
  try {
    const report = await repairIndexHealth()
    health.value = report
    updateHealthBadge()
    const r = report.repair
    if (r) {
      const parts: string[] = []
      if (r.ghost_deleted.length) parts.push(`清理残留向量 ${r.ghost_deleted.length} 项`)
      if (r.ghost_errors.length) parts.push(`清理异常 ${r.ghost_errors.length} 项`)
      if (r.re_indexed.length) parts.push(`重索引 ${r.re_indexed.length} 个文件`)
      if (r.md5_rebuilt) parts.push('入库记录已重建')
      const hasErrors = r.ghost_errors.length > 0
      showKnowledgeMsg(
        parts.length ? `修复${hasErrors ? '部分失败' : '完成'}：${parts.join('；')}` : (hasErrors ? '修复失败' : '修复完成（无需变更）'),
        hasErrors ? 'error' : 'success'
      )
    } else {
      showKnowledgeMsg('修复完成', 'success')
    }
    await loadFiles()
  } catch (e) { showKnowledgeMsg((e as Error).message, 'error') }
  finally { healthRepairing.value = false }
}

function formatHealthTime(iso: string | null): string {
  if (!iso) return '从未'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

watch(mode, (newMode) => {
  if (newMode === 'knowledge') { loadFiles(); loadHealth() }
})

// ---------- auth actions ----------
async function handleLogin() {
  authError.value = ''
  authSuccess.value = ''
  if (!authUsername.value.trim() || !authPassword.value) {
    authError.value = '请填写用户名和密码'
    return
  }
  authLoading.value = true
  try {
    const u = await apiLogin(authUsername.value.trim(), authPassword.value)
    user.value = u
    initConversations()
    startHealthPolling()
  } catch (e) {
    authError.value = (e as Error).message
  } finally {
    authLoading.value = false
  }
}

async function handleRegister() {
  authError.value = ''
  if (!authUsername.value.trim() || !authPassword.value) {
    authError.value = '请填写用户名和密码'
    return
  }
  if (authPassword.value !== authPassword2.value) {
    authError.value = '两次输入的密码不一致'
    return
  }
  if (authPassword.value.length < 10 || !/\d/.test(authPassword.value) || !/[a-zA-Z]/.test(authPassword.value) || !/[^0-9a-zA-Z\s]/.test(authPassword.value)) {
    authError.value = '密码需至少10个字符，且包含数字、英文字母和特殊符号'
    return
  }
  authLoading.value = true
  try {
    await apiRegister(authUsername.value.trim(), authPassword.value)
    // 注册成功后切换到登录页
    authMode.value = 'login'
    authSuccess.value = '注册成功，请登录'
    authError.value = ''
    authPassword.value = ''
    authPassword2.value = ''
  } catch (e) {
    authError.value = (e as Error).message
  } finally {
    authLoading.value = false
  }
}

function handleLogout() {
  if (!confirm('确定要退出登录吗？')) return
  streamAbort?.abort()
  clearAuth()
  user.value = null
  mode.value = 'chat'
  showGearMenu.value = false
  conversations.value = {}
  currentId.value = ''
  authUsername.value = ''
  authPassword.value = ''
  authPassword2.value = ''
  authError.value = ''
  authSuccess.value = ''
  authMode.value = 'login'
  health.value = null
  stopHealthPolling()
}

// ---------- gear menu ----------
const showGearMenu = ref(false)

// 点击页面其他地方关闭齿轮菜单。抽成具名函数是为了卸载时能移除监听
function onDocumentClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!target.closest('.gear-wrapper')) {
    showGearMenu.value = false
  }
}

function toggleGearMenu() {
  showGearMenu.value = !showGearMenu.value
}

function goToKnowledge() {
  mode.value = 'knowledge'
  localStorage.setItem(MODE_KEY, 'knowledge')
  showGearMenu.value = false
}

function goBackToChat() {
  mode.value = 'chat'
  localStorage.setItem(MODE_KEY, 'chat')
}

// 启动时验证 token 有效性
onMounted(async () => {
  if (user.value) {
    try {
      user.value = await apiMe()
      initConversations()
    }
    catch { user.value = null; clearAuth() }
  }
  // 刷新后恢复上次停留的页面；非管理员不能停留在知识库页
  if (mode.value === 'knowledge' && (!user.value || user.value.role !== 'admin')) {
    mode.value = 'chat'
    localStorage.setItem(MODE_KEY, 'chat')
  }
  // 刷新后直接停在知识库页时，watch(mode) 不会触发（mode 值没变化），需主动拉一次数据，
  // 否则文档列表与索引报告会空白，直到手动点「刷新」
  if (mode.value === 'knowledge') {
    loadFiles()
    loadHealth()
  }
  startHealthPolling()
  document.addEventListener('click', onDocumentClick)
})

// 组件卸载时断开在途流、停掉轮询，并移除文档级监听
onUnmounted(() => {
  streamAbort?.abort()
  stopHealthPolling()
  document.removeEventListener('click', onDocumentClick)
})
</script>

<template>
  <!-- ==================== 登录/注册页 ==================== -->
  <div v-if="!user" class="auth-page">
    <div class="auth-card">
      <svg class="auth-mark seal-svg" width="48" height="48" viewBox="0 0 24 24" aria-hidden="true">
        <rect x="1.5" y="1.5" width="21" height="21" rx="4.5" fill="var(--vermilion)" />
        <text x="12" y="12.5" text-anchor="middle" dominant-baseline="central" font-family="STSong, SimSun, 'Songti SC', serif" font-size="13.5" font-weight="600" fill="#f6f3ec">答</text>
      </svg>
      <h1 class="auth-title">浅问深答</h1>
      <p class="auth-subtitle">言必有据</p>

      <div class="auth-tabs">
        <button :class="{ active: authMode === 'login' }" @click="authMode = 'login'; authError = ''; authSuccess = ''">登录</button>
        <button :class="{ active: authMode === 'register' }" @click="authMode = 'register'; authError = ''; authSuccess = ''">注册</button>
      </div>

      <div v-if="authSuccess" class="auth-success">{{ authSuccess }}</div>
      <div v-if="authError" class="auth-error">{{ authError }}</div>

      <form @submit.prevent="authMode === 'login' ? handleLogin() : handleRegister()" class="auth-form">
        <input
          v-model="authUsername"
          type="text"
          placeholder="用户名"
          autocomplete="username"
          :disabled="authLoading"
        />
        <input
          v-model="authPassword"
          type="password"
          placeholder="密码"
          :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'"
          :disabled="authLoading"
          @keydown.enter="authMode === 'login' ? handleLogin() : handleRegister()"
        />
        <input
          v-if="authMode === 'register'"
          v-model="authPassword2"
          type="password"
          placeholder="确认密码"
          autocomplete="new-password"
          :disabled="authLoading"
          @keydown.enter="handleRegister"
        />
        <button type="submit" class="btn-auth" :disabled="authLoading">
          {{ authLoading ? '处理中...' : (authMode === 'login' ? '登录' : '注册') }}
        </button>
      </form>
    </div>
  </div>

  <!-- ==================== 主界面 ==================== -->
  <div v-else class="layout">
    <!-- 左侧边栏（知识库模式隐藏） -->
    <aside v-if="mode === 'chat'" class="sidebar">
      <div class="sidebar-header">
        <div class="brand">
          <svg class="seal-svg" width="26" height="26" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="1.5" y="1.5" width="21" height="21" rx="4.5" fill="var(--vermilion)" />
            <text x="12" y="12.5" text-anchor="middle" dominant-baseline="central" font-family="STSong, SimSun, 'Songti SC', serif" font-size="13.5" font-weight="600" fill="#f6f3ec">答</text>
          </svg>
          <span class="brand-name">浅问深答</span>
        </div>
        <button class="btn-new" :disabled="isProcessing || hasEmptyConv" @click="newConversation" title="新建对话">
          <span class="plus">＋</span> 新建
        </button>
      </div>

      <div class="sidebar-section-title">对话历史</div>

      <ul class="conv-list">
        <li v-for="c in convList" :key="c.id" :class="{ active: c.id === currentId }">
          <button class="conv-btn" :disabled="isProcessing" @click="selectConversation(c.id)">
            <span v-if="c.id === currentId" class="conv-indicator">▍</span>
            <span class="conv-title">{{ c.title }}</span>
          </button>
          <button class="del-btn" :disabled="isProcessing" @click="deleteConversation(c.id)" title="删除">×</button>
        </li>
      </ul>

      <!-- 左下角：用户标识 + 齿轮菜单 -->
      <div class="sidebar-footer">
        <div class="divider"></div>
        <div class="user-area">
          <div class="user-info">
            <span class="user-avatar">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 12C14.7614 12 17 9.76142 17 7C17 4.23858 14.7614 2 12 2C9.23858 2 7 4.23858 7 7C7 9.76142 9.23858 12 12 12Z" fill="currentColor"/><path d="M12 14C7.58172 14 4 17.5817 4 22H20C20 17.5817 16.4183 14 12 14Z" fill="currentColor"/></svg>
            </span>
            <span class="user-name">{{ user.username }}</span>
            <span class="user-role" :class="user.role">{{ user.role === 'admin' ? '管理员' : '用户' }}</span>
          </div>
          <div class="gear-wrapper">
            <button class="btn-gear" @click="toggleGearMenu" title="设置">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.66a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              <span v-if="healthIssueCount > 0" class="badge">{{ healthIssueCount }}</span>
            </button>
            <div v-if="showGearMenu" class="gear-menu">
              <button v-if="user.role === 'admin'" :disabled="isProcessing" @click="goToKnowledge">
                管理知识库
                <span v-if="healthIssueCount > 0" class="badge menu-badge">{{ healthIssueCount }}</span>
              </button>
              <button class="logout-item" @click="handleLogout">退出登录</button>
            </div>
          </div>
        </div>
      </div>
    </aside>

    <!-- 主区域 -->
    <main class="main">
      <!-- ===== 对话模式 ===== -->
      <template v-if="mode === 'chat'">
        <transition name="banner">
          <div v-if="showAnnouncement" class="announcement">
            <div class="announcement-icon">
              <svg width="11" height="11" viewBox="0 0 10 10" aria-hidden="true"><rect x="1.2" y="1.2" width="7.6" height="7.6" rx="1.8" transform="rotate(45 5 5)" fill="var(--vermilion)" /></svg>
            </div>
            <div class="announcement-content">
              <strong>功能说明：</strong>
              本助手默认仅基于<em>内部知识库</em>回答问题。若您询问最新资讯、实时数据或知识库未涵盖的内容，请点击下方输入框旁的
              <button class="announcement-inline-btn" @click="enableSearchFromAnnouncement">「联网搜索」</button>
              按钮开启联网搜索，以获得更准确的外部信息。
            </div>
            <button class="announcement-close" @click="dismissAnnouncement" title="不再提示">×</button>
          </div>
        </transition>

        <div ref="chatArea" class="chat-area">
          <template v-if="current">
            <div v-if="current.messages.length === 0" class="empty">
              <svg class="seal-svg empty-seal" width="56" height="56" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="1.5" y="1.5" width="21" height="21" rx="4.5" fill="var(--vermilion)" />
                <text x="12" y="12.5" text-anchor="middle" dominant-baseline="central" font-family="STSong, SimSun, 'Songti SC', serif" font-size="13.5" font-weight="600" fill="#f6f3ec">答</text>
              </svg>
              <p class="empty-title">开始一段新对话</p>
              <p class="empty-hint">输入问题，Enter 发送，Shift+Enter 换行</p>
              <p class="empty-tip">
                询问实时信息或知识库外的内容时，记得开启
                <button class="empty-link" @click="useOnlineSearch = true">联网搜索</button>
              </p>
            </div>

            <div v-for="(msg, i) in current.messages" :key="i" class="message" :class="msg.role">
              <div class="avatar" :class="msg.role">
                <template v-if="msg.role === 'user'">
                  <svg class="seal-svg" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
                    <rect x="1.5" y="1.5" width="21" height="21" rx="4.5" fill="var(--vermilion)" />
                    <text x="12" y="12.5" text-anchor="middle" dominant-baseline="central" font-family="STSong, SimSun, 'Songti SC', serif" font-size="13.5" font-weight="600" fill="#f6f3ec">问</text>
                  </svg>
                </template>
                <template v-else>
                  <svg class="seal-svg" width="24" height="24" viewBox="0 0 24 24" aria-hidden="true">
                    <rect x="1.5" y="1.5" width="21" height="21" rx="4.5" fill="var(--vermilion)" />
                    <text x="12" y="12.5" text-anchor="middle" dominant-baseline="central" font-family="STSong, SimSun, 'Songti SC', serif" font-size="13.5" font-weight="600" fill="#f6f3ec">答</text>
                  </svg>
                </template>
              </div>
              <div class="bubble-wrap">
                <div v-if="msg.role === 'assistant' && isThinking && i === current.messages.length - 1" class="thinking">
                  <span class="thinking-dot"></span>
                  <span class="thinking-dot" style="animation-delay:0.15s"></span>
                  <span class="thinking-dot" style="animation-delay:0.3s"></span>
                  <span class="thinking-text">处理中...... {{ formatTime(thinkingSeconds) }}</span>
                </div>
                <div v-if="msg.role === 'assistant' && !(isThinking && !msg.content && i === current.messages.length - 1)" class="bubble markdown-body" v-html="renderMarkdown(msg.content || '')"></div>
                <div v-if="msg.role === 'assistant' && msg.thinkingTime != null && msg.content" class="thinking-time">· 处理耗时 {{ formatTime(msg.thinkingTime) }}</div>
                <div v-if="msg.role === 'user'" class="bubble">{{ msg.content }}</div>
                <span v-if="isProcessing && !isThinking && i === current.messages.length - 1 && msg.role === 'assistant'" class="cursor">▌</span>
              </div>
            </div>
          </template>
        </div>

        <transition name="banner">
          <div v-if="persistNotice" class="persist-notice" @click="persistNotice = ''" title="点击关闭">
            {{ persistNotice }}
          </div>
        </transition>

        <div class="input-bar">
          <div class="input-inner">
            <div class="input-options">
              <button
                class="btn-search" :class="{ active: useOnlineSearch }" :disabled="isProcessing"
                :title="useOnlineSearch ? '已开启联网搜索，点击关闭' : '点击开启联网搜索'"
                @click="useOnlineSearch = !useOnlineSearch"
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2"/><path d="M3 12H21" stroke="currentColor" stroke-width="2"/><path d="M12 3C14.5 5.5 16 8.7 16 12C16 15.3 14.5 18.5 12 21C9.5 18.5 8 15.3 8 12C8 8.7 9.5 5.5 12 3Z" stroke="currentColor" stroke-width="2"/></svg>
                <span>{{ useOnlineSearch ? '联网搜索 · 开' : '联网搜索 · 关' }}</span>
              </button>
            </div>
            <div class="input-row">
              <textarea
                v-model="input" placeholder="请输入您的问题..." rows="1" :disabled="isProcessing"
                @keydown="onKeydown"
                @input="($event.target as HTMLTextAreaElement).style.height='auto';($event.target as HTMLTextAreaElement).style.height=Math.min(($event.target as HTMLTextAreaElement).scrollHeight,120)+'px'"
              />
              <button class="btn-send" :disabled="isProcessing || !input.trim()" @click="sendMessage">
                {{ isProcessing ? '…' : '发送' }}
              </button>
            </div>
          </div>
        </div>
      </template>

      <!-- ===== 文件管理模式 ===== -->
      <template v-if="mode === 'knowledge'">
        <div class="knowledge-page">
          <header class="knowledge-header">
            <button class="btn-back" @click="goBackToChat" title="返回对话">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
            </button>
            <div>
              <h1>知识库管理</h1>
              <p class="subtitle">上传文档自动入库，立即可用于检索</p>
            </div>
          </header>

          <div v-if="knowledgeMsg" class="toast" :class="knowledgeMsgType">{{ knowledgeMsg }}</div>

          <div class="knowledge-cols">
            <div class="knowledge-left">
              <section class="card">
                <h2>上传文件</h2>
                <p class="hint">支持 .txt / .pdf / .docx，上传后自动入库并当场校验。</p>
                <div
                  class="dropzone" :class="{ over: dragOver, busy: knowledgeUploading }"
                  @dragover.prevent="dragOver = true"
                  @dragleave="dragOver = false"
                  @drop.prevent="onDrop"
                  @click="fileInput?.click()"
                >
                  <input ref="fileInput" type="file" accept=".txt,.pdf,.docx" multiple hidden @change="onFileSelect" />
                  <p v-if="knowledgeUploading">上传并入库中...</p>
                  <p v-else>点击或拖拽文件到此处</p>
                </div>
              </section>

              <section class="card">
                <div class="card-header">
                  <h2>索引健康</h2>
                  <div class="health-actions">
                    <button class="btn-ghost" :disabled="healthChecking || healthRepairing" @click="handleHealthCheck">
                      {{ healthChecking ? '检查中...' : '立即检查' }}
                    </button>
                    <button
                      class="btn-ghost" :disabled="healthChecking || healthRepairing" @click="handleHealthRepair"
                      title="系统定时自检发现问题会自动修复；此处可立即手动执行一次"
                    >
                      {{ healthRepairing ? '修复中...' : '手动修复' }}
                    </button>
                  </div>
                </div>
                <p v-if="!health" class="hint">后台定时自检发现问题会自动修复；以下按钮用于手动检查或立即修复。</p>
                <template v-else>
                  <p class="health-summary">
                    <span class="health-dot" :class="health.healthy ? 'ok' : 'bad'"></span>
                    <strong>{{ health.healthy ? '状态良好' : `发现 ${health.issues.length} 类问题` }}</strong>
                    <span class="health-time">最近检查：{{ formatHealthTime(health.checked_at) }}</span>
                  </p>
                  <p class="hint">磁盘文件 {{ health.stats.files }} 个（已正确入库 {{ health.stats.indexed_files }} 个）· 向量块 {{ health.stats.chunks }}</p>
                  <ul v-if="health.issues.length" class="health-issues">
                    <li v-for="(iss, i) in health.issues" :key="i">
                      <span class="issue-sev" :class="iss.severity">{{ iss.severity === 'error' ? '严重' : '警告' }}</span>
                      <span class="issue-text">
                        {{ iss.title }}（{{ iss.count }}）
                        <em v-if="iss.detail" class="issue-detail">{{ iss.detail }}</em>
                      </span>
                    </li>
                  </ul>
                </template>
              </section>
            </div>

            <div class="knowledge-right">
              <section class="card">
                <div class="card-header">
                  <h2>文档列表</h2>
                  <div class="file-header-actions">
                    <button class="btn-ghost" :disabled="knowledgeLoading" @click="loadFiles">刷新</button>
                    <button
                      class="btn-ghost" :disabled="knowledgeLoading || knowledgeBuilding" @click="handleBuild"
                      title="重新扫描知识库目录，补建未入库文件"
                    >
                      {{ knowledgeBuilding ? '补建中...' : '补建向量库' }}
                    </button>
                  </div>
                </div>
                <div v-if="knowledgeLoading" class="empty">加载中...</div>
                <div v-else-if="files.length === 0" class="empty">暂无文档</div>
                <ul v-else class="file-list">
                  <li v-for="f in files" :key="f.name">
                    <span class="file-name">{{ f.name }}</span>
                    <span class="file-actions">
                      <span class="file-status" :class="f.indexed ? 'indexed' : 'pending'">{{ f.indexed ? '已入库' : '未入库' }}</span>
                      <button class="btn-danger" @click="handleDeleteFile(f.name)">删除</button>
                    </span>
                  </li>
                </ul>
              </section>
            </div>
          </div>
        </div>
      </template>
    </main>
  </div>
</template>

<style scoped>
/* ===== 通用动效 ===== */
@keyframes rise {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: none; }
}

@keyframes bounce {
  0%, 80%, 100% { transform: scale(0); }
  40% { transform: scale(1); }
}

@keyframes blink { 50% { opacity: 0; } }

@keyframes toastIn {
  from { opacity: 0; transform: translate(-50%, -8px); }
  to { opacity: 1; transform: translate(-50%, 0); }
}

.seal-svg { display: block; flex-shrink: 0; }

/* ===== 登录/注册页 ===== */
.auth-page {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background: var(--paper);
  padding: 24px;
}

.auth-card {
  width: 400px;
  padding: 46px 40px 40px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: 0 1px 2px rgba(33, 31, 26, 0.05), 0 18px 52px rgba(33, 31, 26, 0.07);
  animation: rise 0.5s ease both;
}

.auth-mark { margin: 0 auto 18px; }

.auth-title {
  font-family: var(--font-display);
  font-size: 26px;
  font-weight: 600;
  text-align: center;
  color: var(--ink);
  letter-spacing: 0.12em;
}

.auth-subtitle {
  text-align: center;
  color: var(--ink-3);
  font-size: 12.5px;
  margin: 10px 0 30px;
  letter-spacing: 0.04em;
}

.auth-tabs {
  display: flex;
  border-bottom: 1px solid var(--line);
  margin-bottom: 22px;
}

.auth-tabs button {
  flex: 1;
  padding: 10px 0 12px;
  font-size: 14.5px;
  color: var(--ink-3);
  background: none;
  border: none;
  cursor: pointer;
  position: relative;
  transition: color 0.2s;
}

.auth-tabs button.active { color: var(--ink); font-weight: 500; }

.auth-tabs button.active::after {
  content: '';
  position: absolute;
  left: 32%;
  right: 32%;
  bottom: -1px;
  height: 2px;
  background: var(--ink);
  border-radius: 1px;
}

.auth-error {
  padding: 10px 14px;
  background: var(--terra-bg);
  color: var(--terra);
  border: 1px solid var(--terra-border);
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 16px;
}

.auth-success {
  padding: 10px 14px;
  background: var(--sage-bg);
  color: var(--sage);
  border: 1px solid var(--sage-border);
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 16px;
}

.auth-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.auth-form input {
  padding: 11px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  font-size: 14.5px;
  background: var(--paper);
  color: var(--ink);
  transition: border-color 0.15s, box-shadow 0.15s;
}

.auth-form input:focus {
  border-color: var(--ink-2);
  box-shadow: 0 0 0 3px rgba(33, 31, 26, 0.06);
}

.btn-auth {
  padding: 11px 0;
  background: var(--vermilion);
  color: var(--paper);
  border-radius: 8px;
  font-size: 14.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s;
  margin-top: 4px;
}

.btn-auth:hover:not(:disabled) { background: var(--vermilion-deep); }
.btn-auth:disabled { opacity: 0.45; cursor: not-allowed; }

/* ===== 主布局 ===== */
.layout {
  display: flex;
  height: 100vh;
  background: var(--paper);
}

.sidebar {
  width: 264px;
  background: var(--paper-deep);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  padding: 1.25rem 0.85rem 1rem;
  flex-shrink: 0;
  animation: rise 0.4s ease both;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 0.5rem;
  margin-bottom: 1.25rem;
}

.brand {
  display: flex;
  align-items: center;
  gap: 9px;
  min-width: 0;
}

.brand-name {
  font-family: var(--font-display);
  font-size: 16.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: var(--ink);
  white-space: nowrap;
}

.btn-new {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 5px 11px;
  background: var(--vermilion);
  color: var(--paper);
  border-radius: 6px;
  font-size: 12.5px;
  font-weight: 500;
  transition: background 0.15s, opacity 0.15s;
  flex-shrink: 0;
}
.btn-new:hover:not(:disabled) { background: var(--vermilion-deep); }
.btn-new:disabled { opacity: 0.4; cursor: not-allowed; }
.plus { font-size: 11px; font-weight: 700; }

.sidebar-section-title {
  padding: 0 0.5rem;
  font-size: 11px;
  letter-spacing: 0.14em;
  color: var(--ink-3);
  margin-bottom: 0.6rem;
}

.conv-list {
  list-style: none;
  flex: 1;
  overflow-y: auto;
  padding: 0;
}

.conv-list li {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-bottom: 2px;
  border-radius: 7px;
  border: 1px solid transparent;
  transition: background 0.12s, border-color 0.12s;
}

.conv-list li.active {
  background: var(--surface);
  border-color: var(--line);
  box-shadow: 0 1px 2px rgba(33, 31, 26, 0.04);
}

.conv-list li:hover:not(.active) { background: rgba(33, 31, 26, 0.045); }

.conv-btn {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  background: none;
  color: var(--ink-2);
  font-size: 13.5px;
  border-radius: 6px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  text-align: left;
  min-width: 0;
}
.conv-list li.active .conv-btn { color: var(--ink); }
.conv-indicator { font-size: 12px; color: var(--vermilion); flex-shrink: 0; line-height: 1; }
.conv-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.del-btn {
  width: 26px;
  height: 26px;
  background: none;
  color: var(--ink-3);
  font-size: 15px;
  border-radius: 5px;
  flex-shrink: 0;
  opacity: 0;
  transition: color 0.15s, background 0.15s, opacity 0.15s;
}
.conv-list li:hover .del-btn, .conv-list li.active .del-btn { opacity: 1; }
.del-btn:hover:not(:disabled) { background: var(--terra-bg); color: var(--terra); }

/* 侧边栏底部：用户区域 */
.sidebar-footer { padding: 0 0.5rem; }
.divider { height: 1px; background: var(--line); margin: 0.85rem 0; }

.user-area {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 13px;
  color: var(--ink);
  min-width: 0;
  flex: 1;
}

.user-avatar {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  background: var(--vermilion);
  color: var(--paper);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.user-name { font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.user-role {
  font-size: 10px;
  padding: 1px 7px;
  border-radius: 4px;
  font-weight: 500;
  flex-shrink: 0;
  letter-spacing: 0.03em;
}
.user-role.admin, .user-role.user { background: var(--sage-bg); color: var(--sage); }

/* 齿轮菜单 */
.gear-wrapper {
  position: relative;
  flex-shrink: 0;
}

.btn-gear {
  position: relative;
  width: 30px;
  height: 30px;
  border-radius: 7px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ink-3);
  transition: background 0.15s, color 0.15s;
}
.btn-gear:hover { background: rgba(33, 31, 26, 0.06); color: var(--ink); }

.badge {
  position: absolute;
  top: -5px;
  right: -6px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--terra);
  color: var(--paper);
  font-size: 10px;
  font-weight: 600;
  line-height: 16px;
  text-align: center;
  pointer-events: none;
}
.gear-menu .menu-badge {
  position: static;
  float: right;
  margin-top: 1px;
  margin-left: 8px;
}

.gear-menu {
  position: absolute;
  bottom: 100%;
  right: 0;
  margin-bottom: 6px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 9px;
  box-shadow: 0 2px 4px rgba(33, 31, 26, 0.05), 0 14px 36px rgba(33, 31, 26, 0.1);
  padding: 5px;
  min-width: 146px;
  z-index: 50;
  animation: rise 0.18s ease both;
}

.gear-menu button {
  display: block;
  width: 100%;
  padding: 8px 12px;
  text-align: left;
  font-size: 13px;
  color: var(--ink);
  border-radius: 6px;
  transition: background 0.12s;
}
.gear-menu button:hover:not(:disabled) { background: rgba(33, 31, 26, 0.05); }
.gear-menu button:disabled { opacity: 0.4; cursor: not-allowed; }
.gear-menu .logout-item { color: var(--terra); }
.gear-menu .logout-item:hover { background: var(--terra-bg); }

/* ===== 主区域 ===== */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--paper);
}

.announcement {
  display: flex;
  align-items: flex-start;
  gap: 11px;
  margin: 1rem 1.5rem 0;
  padding: 12px 16px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 9px;
  color: var(--ink-2);
  font-size: 13px;
  line-height: 1.7;
}
.announcement-icon { flex-shrink: 0; display: flex; align-items: center; padding-top: 7px; }
.announcement-content { flex: 1; }
.announcement-content strong { color: var(--ink); font-weight: 600; }
.announcement-content em { font-style: normal; font-weight: 600; color: var(--ink); }
.announcement-inline-btn {
  display: inline;
  color: var(--vermilion);
  font-weight: 600;
  text-decoration: underline;
  text-underline-offset: 2px;
  padding: 0;
  background: none;
  cursor: pointer;
}
.announcement-inline-btn:hover { color: var(--vermilion-deep); }
.announcement-close {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 5px;
  color: var(--ink-3);
  font-size: 15px;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: color 0.15s, background 0.15s;
}
.announcement-close:hover { background: rgba(33, 31, 26, 0.06); color: var(--ink); }

.banner-enter-active, .banner-leave-active { transition: all 0.25s ease; overflow: hidden; }
.banner-enter-from, .banner-leave-to { opacity: 0; max-height: 0; margin-top: 0; padding-top: 0; padding-bottom: 0; }

.chat-area {
  flex: 1;
  overflow-y: auto;
  padding: 2.5rem 2rem;
  scroll-behavior: smooth;
}

.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 12px;
}
.empty-seal { margin-bottom: 10px; animation: rise 0.6s ease both; }
.empty-title {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 600;
  color: var(--ink);
  letter-spacing: 0.08em;
}
.empty-hint { font-size: 13.5px; color: var(--ink-3); }
.empty-tip { font-size: 13px; color: var(--ink-3); margin-top: 4px; }
.empty-link {
  display: inline;
  color: var(--vermilion);
  font-weight: 500;
  text-decoration: underline;
  text-underline-offset: 2px;
  padding: 0;
  background: none;
  cursor: pointer;
}
.empty-link:hover { color: var(--vermilion-deep); }

.message {
  display: flex;
  gap: 14px;
  margin: 0 auto;
  margin-bottom: 30px;
  max-width: 720px;
  animation: rise 0.3s ease both;
}
.message.user { flex-direction: row-reverse; }

.avatar {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
}
.message.user .avatar, .message.assistant .avatar { background: transparent; }

.bubble-wrap { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; }
.message.user .bubble-wrap { align-items: flex-end; flex: 0 1 auto; }

.bubble {
  padding: 10px 15px;
  border-radius: 9px;
  font-size: 15px;
  line-height: 1.75;
  word-break: break-word;
  max-width: 100%;
}
.message .bubble {
  background: transparent;
  border: none;
  padding: 3px 0;
}

.thinking {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 7px 2px;
  color: var(--ink-3);
  font-size: 13.5px;
}
.thinking-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--ink-3);
  display: inline-block;
  animation: bounce 1.4s infinite ease-in-out both;
}
.thinking-text { margin-left: 5px; }

.thinking-time {
  font-size: 11.5px;
  color: var(--ink-3);
  margin-top: 2px;
  letter-spacing: 0.02em;
}

.cursor {
  display: inline-block;
  margin-left: 2px;
  animation: blink 1s step-end infinite;
  color: var(--ink-3);
}

/* 持久化失败提示 */
.persist-notice {
  margin: 0 16px 10px;
  padding: 8px 12px;
  font-size: 13px;
  color: #9a3412;
  background: #fef3c7;
  border: 1px solid #f3c14b;
  border-radius: 8px;
  cursor: pointer;
}

/* 输入区 */
.input-bar {
  padding: 0.85rem 1.5rem 1.35rem;
  border-top: 1px solid var(--line);
  background: var(--paper);
}

.input-inner {
  max-width: 720px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 9px;
}

.input-options { display: flex; align-items: center; }

.btn-search {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 13px;
  background: var(--surface);
  color: var(--ink-2);
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 12.5px;
  font-weight: 500;
  transition: all 0.15s;
  cursor: pointer;
}
.btn-search:hover:not(:disabled) { border-color: var(--ink-2); color: var(--ink); }
.btn-search.active {
  background: var(--vermilion);
  color: var(--paper);
  border-color: var(--vermilion);
}
.btn-search:disabled { opacity: 0.5; cursor: not-allowed; }

.input-row { display: flex; gap: 9px; align-items: flex-end; }

.input-row textarea {
  flex: 1;
  padding: 11px 15px;
  border: 1px solid var(--line);
  border-radius: 10px;
  resize: none;
  font-size: 15px;
  line-height: 1.6;
  max-height: 120px;
  background: var(--surface);
  color: var(--ink);
  transition: border-color 0.15s, box-shadow 0.15s;
}
.input-row textarea:focus {
  border-color: var(--ink-2);
  box-shadow: 0 0 0 3px rgba(33, 31, 26, 0.06);
}
.input-row textarea:disabled { opacity: 0.6; cursor: not-allowed; }

.btn-send {
  padding: 0 21px;
  height: 42px;
  background: var(--vermilion);
  color: var(--paper);
  border-radius: 10px;
  font-size: 14.5px;
  font-weight: 500;
  letter-spacing: 0.04em;
  transition: background 0.15s, opacity 0.15s;
  flex-shrink: 0;
}
.btn-send:hover:not(:disabled) { background: var(--vermilion-deep); }
.btn-send:disabled { opacity: 0.35; cursor: not-allowed; }

/* ===== 知识库管理 ===== */
.knowledge-page {
  flex: 1;
  overflow-y: auto;
  padding: 44px 24px;
  max-width: 1120px;
  margin: 0 auto;
  width: 100%;
  animation: rise 0.4s ease both;
}

.knowledge-cols {
  display: grid;
  grid-template-columns: 360px minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
}
.knowledge-left,
.knowledge-right {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}
.knowledge-cols .card { margin-bottom: 0; }
.knowledge-left .card:last-child { margin-top: auto; }
.knowledge-right .card {
  flex: 1;
  display: flex;
  flex-direction: column;
}
.knowledge-right .empty { flex: 1; }
@media (max-width: 900px) {
  .knowledge-cols { grid-template-columns: 1fr; }
}

.knowledge-header {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 32px;
}

.btn-back {
  width: 38px;
  height: 38px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ink-2);
  border: 1px solid var(--line);
  background: var(--surface);
  flex-shrink: 0;
  margin-top: 3px;
  transition: border-color 0.15s, color 0.15s;
}
.btn-back:hover { border-color: var(--ink-2); color: var(--ink); }

.knowledge-header h1 {
  font-family: var(--font-display);
  font-size: 24px;
  font-weight: 600;
  margin-bottom: 6px;
  letter-spacing: 0.06em;
}
.subtitle { color: var(--ink-3); font-size: 13.5px; }

.toast {
  /* 悬浮提示：脱离文档流固定于视口顶部，出现/消失不引起页面上下跳动 */
  position: fixed;
  top: 20px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 100;
  max-width: min(720px, calc(100vw - 48px));
  padding: 13px 18px;
  border-radius: 9px;
  font-size: 14px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 8px;
  box-shadow: 0 8px 28px rgba(33, 31, 26, 0.14);
  animation: toastIn 0.3s ease;
}
.toast::before { font-size: 16px; flex-shrink: 0; }
.toast.success { background: var(--sage-bg); color: var(--sage); border: 1px solid var(--sage-border); }
.toast.success::before { content: '✓'; }
.toast.error { background: var(--terra-bg); color: var(--terra); border: 1px solid var(--terra-border); }
.toast.error::before { content: '✕'; }

.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 26px;
  margin-bottom: 20px;
  box-shadow: 0 1px 2px rgba(33, 31, 26, 0.04);
}
.card h2 {
  font-family: var(--font-display);
  font-size: 16.5px;
  font-weight: 600;
  margin-bottom: 8px;
  letter-spacing: 0.04em;
}

.card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.card-header h2 { margin-bottom: 0; }

.hint { font-size: 13px; color: var(--ink-3); margin-bottom: 16px; }

.dropzone {
  border: 1.5px dashed var(--line-strong);
  border-radius: 10px;
  padding: 42px;
  text-align: center;
  color: var(--ink-3);
  background: var(--paper);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
  margin-bottom: 16px;
}
.dropzone:hover { border-color: var(--ink-3); color: var(--ink-2); }
.dropzone.over { border-color: var(--vermilion); color: var(--vermilion); background: #faf3ec; }
.dropzone.busy { opacity: 0.6; pointer-events: none; }

.file-header-actions { display: flex; align-items: center; gap: 8px; }

.btn-ghost {
  padding: 6px 15px;
  background: none;
  color: var(--ink-2);
  border: 1px solid var(--line);
  border-radius: 7px;
  font-size: 12.5px;
  transition: border-color 0.15s, color 0.15s;
}
.btn-ghost:hover:not(:disabled) { border-color: var(--ink-2); color: var(--ink); }

.btn-danger {
  padding: 6px 15px;
  background: none;
  color: var(--terra);
  border: 1px solid var(--terra-border);
  border-radius: 7px;
  font-size: 12.5px;
  transition: background 0.15s;
}
.btn-danger:hover { background: var(--terra-bg); }

.file-list {
  list-style: none;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  max-height: calc(100vh - 290px);
  padding-right: 4px;
}
.file-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 0;
  border-bottom: 1px solid var(--line);
}
.file-list li:last-child { border-bottom: none; }
.file-name { font-size: 14px; word-break: break-all; padding-right: 16px; }
.file-actions { display: flex; align-items: center; gap: 12px; flex-shrink: 0; }
.file-status {
  font-size: 11.5px;
  line-height: 1;
  padding: 4px 10px;
  border-radius: 999px;
  white-space: nowrap;
  border: 1px solid transparent;
}
.file-status.indexed { color: var(--sage); background: var(--sage-bg); border-color: var(--sage-border); }
.file-status.pending { color: var(--terra); background: var(--terra-bg); border-color: var(--terra-border); }

/* ===== 索引健康 ===== */
.health-actions { display: flex; gap: 8px; }
.health-summary { display: flex; align-items: center; gap: 8px; font-size: 14px; margin-bottom: 2px; }
.health-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.health-dot.ok { background: var(--sage); }
.health-dot.bad { background: var(--terra); }
.health-time { margin-left: auto; font-size: 12px; color: var(--ink-3); }
.health-issues { list-style: none; margin-top: 8px; }
.health-issues li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
}
.health-issues li:last-child { border-bottom: none; }
.issue-sev {
  flex-shrink: 0;
  font-size: 11px;
  line-height: 1.6;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
}
.issue-sev.warn { color: var(--ink-2); background: var(--paper-deep); border-color: var(--line-strong); }
.issue-sev.error { color: var(--terra); background: var(--terra-bg); border-color: var(--terra-border); }
.issue-detail { display: block; color: var(--ink-3); font-size: 12px; font-style: normal; }

/* ===== markdown ===== */
.markdown-body :deep(h1), .markdown-body :deep(h2), .markdown-body :deep(h3), .markdown-body :deep(h4) {
  margin-top: 0.8em;
  margin-bottom: 0.4em;
  font-family: var(--font-display);
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: 0.02em;
}
.markdown-body :deep(h1) { font-size: 1.45em; }
.markdown-body :deep(h2) { font-size: 1.28em; }
.markdown-body :deep(h3) { font-size: 1.14em; }
.markdown-body :deep(h4) { font-size: 1.05em; }
.markdown-body :deep(p) { margin: 0.5em 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 1.6em; margin: 0.5em 0; }
.markdown-body :deep(li) { margin: 0.2em 0; }
.markdown-body :deep(strong) { font-weight: 600; }
.markdown-body :deep(em) { font-style: italic; }
.markdown-body :deep(code) {
  background: var(--code-inline-bg);
  color: var(--ink);
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 0.88em;
  font-family: var(--font-mono);
}
.markdown-body :deep(pre) {
  background: var(--code-bg);
  color: var(--code-text);
  border-radius: 9px;
  padding: 14px 16px;
  overflow-x: auto;
  margin: 0.9em 0;
}
.markdown-body :deep(pre code) { background: none; color: inherit; padding: 0; font-size: 0.88em; line-height: 1.6; }
.markdown-body :deep(blockquote) {
  border-left: 3px solid var(--line-strong);
  padding-left: 13px;
  color: var(--ink-2);
  margin: 0.9em 0;
}
.markdown-body :deep(hr) { border: none; border-top: 1px solid var(--line); margin: 1.1em 0; }
.markdown-body :deep(a) { color: var(--vermilion); text-decoration: none; }
.markdown-body :deep(a:hover) { text-decoration: underline; text-underline-offset: 2px; }
.markdown-body :deep(table) { border-collapse: collapse; margin: 0.9em 0; width: 100%; }
.markdown-body :deep(th), .markdown-body :deep(td) { border: 1px solid var(--line); padding: 7px 11px; text-align: left; }
.markdown-body :deep(th) { background: var(--paper-deep); font-weight: 600; }
</style>