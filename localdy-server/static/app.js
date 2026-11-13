/* ============================================================
   0. 常量与基础工具
   ============================================================ */
const { createApp, reactive, onMounted, computed, ref, watch, nextTick, onUnmounted } = Vue

const URL_VIDEO_KEY = 'v'
const URL_TAB_KEY = 't'

const TAB_ID = (() => {
    try {
        let id = sessionStorage.getItem('v_tab_id')
        if (!id) {
            id = Math.random().toString(36).slice(2, 10)
            sessionStorage.setItem('v_tab_id', id)
        }
        return id
    } catch (e) {
        return 'v_' + Math.random().toString(36).slice(2, 10)
    }
})()

// ★ 自动改名的冷却期配置
const AUTO_APPLY_LAST_KEY   = 'auto_apply_tags_last_ts'
const AUTO_APPLY_COOLDOWN_MS = 30 * 60 * 1000    // 30 分钟

/* ============================================================
   1. 后端接口封装
   ============================================================ */
/* ============================================================
   1. 后端接口封装
   ============================================================ */

/* ---------- ★ 拆分模式：服务端注入的配置 ---------- */
const LOCALDY = window.LOCALDY || { api: '', client: 'http://127.0.0.1:18931', download: '/download/localdy_client.py', split: false }
if (LOCALDY.api) axios.defaults.baseURL = LOCALDY.api

// 客户端 API（与服务端不同的 baseURL，需单独实例；配对 token 每次请求都带）
const clientApi = axios.create({ baseURL: LOCALDY.client, timeout: 5000 })
clientApi.interceptors.request.use(c => {
    const t = localStorage.getItem('localdy_client_token')
    if (t) c.headers['X-Client-Token'] = t
    return c
})

// 媒体票据缓存 {hash: {ticket, fetchedAt}}，由 setup 内的响应式 ref 持有。
// 约定 3：票据 5 分钟内可重复使用（绑定单个 hash），不是一次性；
// 浏览器一次播放会发多次 Range 请求，同一票据复用；快过期时刷新。
const TICKET_TTL_MS = 4.5 * 60 * 1000
let ticketStoreRef = null
const ticketFetching = new Set()
async function ensureTickets(hashes) {
    if (!LOCALDY.split) return
    const need = (hashes || []).filter(h => {
        if (!h) return false
        const rec = ticketStoreRef?.value?.[h]
        if (rec && Date.now() - rec.fetchedAt < TICKET_TTL_MS) return false
        if (ticketFetching.has(h)) return false
        return true
    })
    if (!need.length) return
    need.forEach(h => ticketFetching.add(h))
    try {
        const { data } = await clientApi.post('/media-tickets', { hashes: need })
        const map = data?.tickets || data || {}
        for (const h of need) {
            const t = map[h]
            if (t && ticketStoreRef) {
                ticketStoreRef.value = { ...ticketStoreRef.value, [h]: { ticket: t, fetchedAt: Date.now() } }
            }
        }
    } catch (e) {
        // 客户端离线或未配对：静默失败，离线提示由 clientOnline 负责
    } finally {
        need.forEach(h => ticketFetching.delete(h))
    }
}

function getVideoUrl(video) {
    const v = video || {}
    // 非拆分模式，或该视频还没有 hash：走原版服务端流地址
    if (!LOCALDY.split || !v.file_hash) return `/videos/${v.id}/stream`
    const rec = ticketStoreRef?.value?.[v.file_hash]
    if (!rec || Date.now() - rec.fetchedAt >= TICKET_TTL_MS) {
        ensureTickets([v.file_hash])
        return ''
    }
    return `${LOCALDY.client}/media/${v.file_hash}?ticket=${rec.ticket}`
}

async function fetchLatestVideos(state) {
    const params = {
        latest: state.pageSize,
        page: state.page,
        page_size: state.pageSize,
        search: state.searchKeyword,
        score: state.searchScore
    }
    if (state.searchSize && state.searchSize !== 0) {
        params.size = state.searchSize
    }
    if (state.searchWatched && state.searchWatched !== 'all') {
        params.watched = state.searchWatched
    }
    const res = await axios.get('/videos', { params })
    return res.data
}

async function updateScore(videoId, score) {
    const { data } = await axios.post('/videos/update_score', { id: videoId, score: score })
    if (!data.success) throw new Error(data.error || '评分保存失败')
}

async function getTotalCount(pageSize, searchKeyword = '', searchScore = 0,
                             searchSize = 0, searchWatched = 'all') {
    const params = []
    if (searchKeyword) params.push(`search=${encodeURIComponent(searchKeyword)}`)
    if (searchScore && searchScore > 0) params.push(`score=${searchScore}`)
    if (searchSize && searchSize !== 0) params.push(`size=${searchSize}`)
    if (searchWatched && searchWatched !== 'all') params.push(`watched=${searchWatched}`)
    const url = `/videos/count${params.length ? '?' + params.join('&') : ''}`
    const resp = await axios.get(url)
    return resp.data.total || 0
}

/* ============================================================
   2. VideoPlayer
   ============================================================ */
class SearchManager {
    constructor(state, refs) {
        this.state = state
        this.refs = refs
        this.countKey = state.countKey || 'count'
        this.loadVideosCallback = async () => {}
    }

    async doSearch() {
        const start = performance.now()
        this.state.searchKeyword = this.refs.searchKeyword.value.trim()
        this.state.searchScore   = Number(this.refs.searchScore.value) || 0
        this.state.searchWatched = this.refs.searchWatched.value

        if (this.refs.sizeValue.value && this.refs.sizeValue.value > 0) {
            this.state.searchSize = `${this.refs.sizeOperator.value}:${this.refs.sizeValue.value}`
        } else {
            this.state.searchSize = 0
        }

        this.state.page = 1
        this.state.startPage = 1
        this.refs.currentIndex.value = 0
        this.refs.showSearch.value = false

        this.updateUrlParams()
        this.showSearchHint()

        const result = await this.loadVideosCallback(false)

        let count = this.state.totalVideos
        if (count === null) {
            const list = this.state.videos
            if (Array.isArray(list)) count = list.length
        }
        if (count !== null) {
            this.state.searchCount = count
            this.updateCountParam(count)
        }

        const ms = performance.now() - start
        this.refs.searchTimeMsg.value = '搜索完成，用时 ' + (ms / 1000).toFixed(2) + ' 秒'
        setTimeout(() => {
            this.refs.searchTimeMsg.value = ''
        }, 3000)
    }

    parseCountKey(data, key = this.countKey) {
        if (data == null) return null
        if (typeof data === 'number') return isNaN(data) ? null : data
        if (typeof data === 'string') {
            const n = Number(data.trim())
            return data.trim() !== '' && !isNaN(n) ? n : null
        }
        if (typeof data !== 'object') return null
        if (key && data[key] != null && !isNaN(Number(data[key]))) {
            return Number(data[key])
        }
        const fallbackKeys = ['count', 'total', 'totalCount', 'total_count', 'totalNum', 'num', 'size']
        for (const k of fallbackKeys) {
            if (data[k] != null && !isNaN(Number(data[k]))) return Number(data[k])
        }
        for (const k of ['data', 'result', 'res', 'response']) {
            if (data[k] != null && typeof data[k] === 'object') {
                const nested = this.parseCountKey(data[k], key)
                if (nested !== null) return nested
            }
        }
        if (Array.isArray(data)) return data.length
        return null
    }

    updateCountParam(count) {
        const url = new URL(window.location)
        if (count !== null && count !== undefined && !isNaN(count)) {
            url.searchParams.set('count', count)
        } else {
            url.searchParams.delete('count')
        }
        window.history.replaceState({}, '', url)
    }

    showSearchHint() {
        const conditions = []
        if (this.state.searchKeyword) {
            conditions.push(`关键词: "${this.state.searchKeyword}"`)
        }
        if (this.state.searchScore > 0) {
            conditions.push(`评分: ${this.state.searchScore}分`)
        }
        if (this.state.searchSize && this.state.searchSize !== 0) {
            const [operator, value] = this.state.searchSize.split(':')
            const operatorText = operator === 'lte' ? '≤' : operator === 'gte' ? '≥' : '='
            conditions.push(`大小: ${operatorText}${value}MB`)
        }
        if (this.state.searchWatched && this.state.searchWatched !== 'all') {
            const map = { yes: '已看', no: '未看', stale: '最近1个月未看' }
            conditions.push(`观看: ${map[this.state.searchWatched]}`)
        }
        if (conditions.length > 0) {
            this.refs.searchTimeMsg.value = `应用筛选条件: ${conditions.join(', ')}`
        }
    }

    updateUrlParams() {
        const url = new URL(window.location)
        url.searchParams.delete(URL_VIDEO_KEY)

        if (this.state.searchKeyword) url.searchParams.set('search', this.state.searchKeyword)
        else url.searchParams.delete('search')

        if (this.state.searchScore && this.state.searchScore > 0) url.searchParams.set('score', this.state.searchScore)
        else url.searchParams.delete('score')

        if (this.state.searchSize && this.state.searchSize !== 0) url.searchParams.set('size', this.state.searchSize)
        else url.searchParams.delete('size')

        if (this.state.searchWatched && this.state.searchWatched !== 'all')
            url.searchParams.set('watched', this.state.searchWatched)
        else
            url.searchParams.delete('watched')

        window.history.pushState({}, '', url)
    }

    loadFromUrlParams() {
        const urlParams = new URLSearchParams(window.location.search)
        this.refs.searchKeyword.value = this.state.searchKeyword = ''
        this.refs.searchScore.value = this.state.searchScore = 0
        this.refs.searchWatched.value = this.state.searchWatched = 'all'
        this.refs.sizeValue.value = ''
        this.refs.sizeOperator.value = 'lte'
        this.state.searchSize = 0

        const searchParam  = urlParams.get('search')
        const scoreParam   = urlParams.get('score')
        const sizeParam    = urlParams.get('size')
        const countParam   = urlParams.get('count')
        const watchedParam = urlParams.get('watched')

        if (searchParam) {
            this.refs.searchKeyword.value = searchParam
            this.state.searchKeyword = searchParam
        }

        if (scoreParam) {
            const score = parseInt(scoreParam)
            if (!isNaN(score) && score > 0) {
                this.refs.searchScore.value = score
                this.state.searchScore = score
            }
        }

        if (sizeParam) {
            if (sizeParam.includes(':')) {
                const [operator, value] = sizeParam.split(':')
                const sizeValue = parseInt(value)
                if (!isNaN(sizeValue) && sizeValue > 0) {
                    this.refs.sizeOperator.value = operator
                    this.refs.sizeValue.value = sizeValue
                    this.state.searchSize = sizeParam
                }
            } else {
                const size = parseInt(sizeParam)
                if (!isNaN(size) && size > 0) {
                    this.refs.sizeOperator.value = 'lte'
                    this.refs.sizeValue.value = size
                    this.state.searchSize = `lte:${size}`
                }
            }
        }

        if (watchedParam && ['yes', 'no', 'stale'].includes(watchedParam)) {
            this.refs.searchWatched.value = watchedParam
            this.state.searchWatched = watchedParam
        }

        if (countParam !== null) {
            const count = parseInt(countParam)
            if (!isNaN(count)) this.state.searchCount = count
        }

        return !!(searchParam || scoreParam || sizeParam || watchedParam)
    }

    clearSearch() {
        this.refs.searchKeyword.value = ''
        this.refs.searchScore.value = 0
        this.refs.sizeOperator.value = 'lte'
        this.refs.sizeValue.value = ''
        this.refs.searchWatched.value = 'all'
        this.state.searchKeyword = ''
        this.state.searchScore = 0
        this.state.searchSize = 0
        this.state.searchWatched = 'all'
        this.state.searchCount = 0
        this.state.page = 1
        this.state.startPage = 1
        this.refs.currentIndex.value = 0

        const url = new URL(window.location)
        url.searchParams.delete('search')
        url.searchParams.delete('score')
        url.searchParams.delete('size')
        url.searchParams.delete('count')
        url.searchParams.delete('watched')
        window.history.pushState({}, '', url)
    }

    setLoadVideosCallback(callback) { this.loadVideosCallback = callback }

    initPopstateListener() {
        window.addEventListener('popstate', () => {
            const shouldSearch = this.loadFromUrlParams()
            if (shouldSearch) {
                this.doSearch()
            } else {
                this.clearSearch()
                this.loadVideosCallback(false)
            }
        })
    }
}

/* ============================================================
   4. ConfigManager
   ============================================================ */
class ConfigManager {
    constructor(refs) { this.refs = refs; this.disposed = false }

    async fetchPaths() {
        try {
            const resp = await axios.get('/video-paths')
            this.refs.pathList.value = resp.data.paths || []
        } catch (e) { this.refs.showToast('读取文件夹配置失败') }
    }

    async run(action) {
        if (this.refs.configBusy.value) return
        this.refs.configBusy.value = true
        try { await action() }
        catch (e) { this.refs.showToast(e.response?.data?.error || e.message || '操作失败') }
        finally { this.refs.configBusy.value = false }
    }

    async trackTask(id) {
        while (!this.disposed) {
            const { data } = await axios.get('/tasks/' + id)
            this.refs.indexProgress.value = data
            if (data.status === 'error') throw new Error(data.message)
            if (data.status === 'done') {
                this.refs.showToast(data.message)
                await this.refs.refreshLibrary()
                return
            }
            await new Promise(resolve => setTimeout(resolve, 800))
        }
    }

    async addPath() {
        const path = this.refs.newPath.value.trim()
        if (!path) return this.refs.showToast('请先输入视频文件夹路径')
        await this.run(async () => {
            const { data } = await axios.post('/video-paths?async=1', { path })
            this.refs.newPath.value = ''
            await this.fetchPaths()
            if (data.task_id) await this.trackTask(data.task_id)
            else await this.refs.refreshLibrary()
        })
    }

    async indexPath(path) {
        await this.run(async () => {
            const { data } = await axios.post('/video-paths/index/async', { path })
            await this.trackTask(data.task_id)
        })
    }

    async indexPath_del(path) {
        if (!window.confirm('从视频库移除此文件夹？原始视频文件会保留。')) return
        await this.run(async () => {
            await axios.post('/video-paths/index?del=1', { path })
            await this.fetchPaths()
            await this.refs.refreshLibrary()
            this.refs.showToast('已移除文件夹，原始文件已保留')
        })
    }
}

const iconPaths = {
    play: 'M8 5l11 7-11 7V5z',
    pause: 'M6 5h4v14H6zM14 5h4v14h-4z',
    heart: 'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z',
    'thumb-down': 'M7 14V3H3v11h4zm0-10h10a3 3 0 0 1 3 3l-1 6a2 2 0 0 1-2 2h-4l1 5a2 2 0 0 1-3 1l-4-7',
    discover: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm4 5-2 6-6 2 2-6 6-2z',
    clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 4v5l3 2',
    folder: 'M3 7V5h6l2 2h10v13H3V7z',
    sliders: 'M4 6h7m4 0h5M4 12h2m4 0h10M4 18h10m4 0h2M11 3v6M6 9v6m8 0v6',
    settings: 'M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1 1-3zm3 5a4 4 0 1 0 0 8 4 4 0 0 0 0-8z',
    keyboard: 'M3 5h18v14H3V5zm4 4h.01M11 9h.01M15 9h.01M18 9h.01M7 12h.01M11 12h.01M15 12h.01M18 12h.01M8 16h8',
    search: 'M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM16 16l5 5',
    shuffle: 'M3 5h3l12 14h3M3 19h3l4-5m4-4 4-5h3m-3-3 3 3-3 3m0 8 3 3-3 3',
    'arrow-right': 'M4 12h16m-6-6 6 6-6 6',
    'chevron-right': 'm9 5 7 7-7 7',
    'chevron-down': 'm5 9 7 7 7-7',
    'chevron-up': 'm5 15 7-7 7 7',
    'volume': 'M11 4 6 8H3v8h3l5 4V4zm4 4a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14',
    'volume-off': 'M11 4 6 8H3v8h3l5 4V4zm5 5 5 6m0-6-5 6',
    eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
    'eye-off': 'm3 3 18 18M9 5a12 12 0 0 1 3 0c6 0 10 7 10 7a17 17 0 0 1-4 5M6 6a19 19 0 0 0-4 6s4 7 10 7a14 14 0 0 0 5-1M9 9a4 4 0 0 0 6 6',
    maximize: 'M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5',
    minimize: 'M3 8h5V3m8 0v5h5M8 21v-5H3m18 0h-5v5',
    repeat: 'M4 11V7h16m-3-3 3 3-3 3M20 13v4H4m3-3-3 3 3 3',
    note: 'M5 3h14v18H5V3zm4 5h6m-6 4h6m-6 4h4',
    hash: 'M9 3 7 21M17 3l-2 18M4 8h17M3 16h17',
    download: 'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',
    close: 'm6 6 12 12M18 6 6 18',
    check: 'm5 12 4 4L19 6',
    mouse: 'M12 2a6 6 0 0 0-6 6v8a6 6 0 0 0 12 0V8a6 6 0 0 0-6-6zm0 3v4',
    alert: 'M12 3 2 21h20L12 3zm0 6v5m0 3h.01',
    palette: 'M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1.5-3.3A1.5 1.5 0 0 1 16 15h2a3 3 0 0 0 3-3 9 9 0 0 0-9-9zM7 10h.01M10 7h.01M15 7h.01M18 10h.01'
}
const AppIcon = {
    props: ['name'],
    render() { return Vue.h('svg', { class: 'app-icon', viewBox: '0 0 24 24', 'aria-hidden': 'true' }, [Vue.h('path', { d: iconPaths[this.name] || iconPaths.play })]) }
}

/* ============================================================
   5. 应用主体
   ============================================================ */
createApp({
    setup() {
        const state = reactive({
            videos: [],
            page: 1,
            pageSize: 10,
            searchKeyword: '',
            searchScore: 0,
            searchSize: 0,
            searchWatched: 'all',
            totalVideos: 0,
            startPage: 1
        })
        const currentIndex = ref(0)
        const showDetail = ref(false)
        const showSearch = ref(false)
        const showConfig = ref(false)
        const showQuickTag = ref(false)
        const searchKeyword = ref('')
        const searchScore = ref(0)
        const searchSize = ref(0)
        const searchWatched = ref('all')
        const sizeOperator = ref('lte')
        const sizeValue = ref('')
        const newPath = ref('')
        const pathList = ref([])
        const searchTimeMsg = ref('')
        const jumpIndex = ref('')

        const likeLoading = ref(0)
        const burstScore = ref(0)
        const shakeScore = ref(0)

        const videoQueue = ref([])

        // ★ 拆分模式：票据缓存（响应式，getVideoUrl 渲染时读取）
        const ticketMap = ref({})
        ticketStoreRef = ticketMap
        const ticketRefreshed = new Set()
        // ★ 拆分模式：客户端在线状态 + 配对
        const clientOnline = ref(true)
        const clientChecked = ref(false)
        const pairingNeeded = ref(false)
        const pairingCode = ref('')
        async function checkClient() {
            if (!LOCALDY.split) return
            try {
                await clientApi.get('/health')
                clientOnline.value = true
                pairingNeeded.value = !localStorage.getItem('localdy_client_token')
            } catch (e) {
                clientOnline.value = false
            } finally {
                clientChecked.value = true
            }
        }
        async function savePairing() {
            const code = (pairingCode.value || '').trim()
            if (!code) return
            localStorage.setItem('localdy_client_token', code)
            try {
                // 同时把 token 交给服务端保存，服务端用它校验客户端推来的索引/任务
                await axios.post('/api/pair-client', { token: code })
            } catch (e) {
                console.warn('服务端配对失败，客户端推送仍未被服务端信任', e)
                pairingNeeded.value = true
                return
            }
            pairingCode.value = ''
            pairingNeeded.value = false
            checkClient()
            ensureTickets(videoQueue.value.map(v => v.file_hash).filter(Boolean))
        }
        // 队列变化时，提前给当前 + 相邻视频取票（预加载）
        watch(videoQueue, (q) => {
            if (!LOCALDY.split || !q?.length) return
            ensureTickets(q.map(v => v.file_hash).filter(Boolean))
        })

        const activeVideoIndex = ref(1)
        const videoLoadStates = ref([false, false, false])
        const isLoading = ref(true)
        const videoClass = ref('video-portrait')
        const showHelp = ref(false)
        const showAppearance = ref(false)
        const themeChoices = LocaldyThemes.choices
        const activeTheme = ref(LocaldyThemes.load())
        const currentTheme = computed(() => themeChoices.find(theme => theme.id === activeTheme.value) || themeChoices[0])
        function setTheme(id) { activeTheme.value = LocaldyThemes.apply(id) }
        function openAppearance() {
            showDetail.value = showSearch.value = showConfig.value = showQuickTag.value = showHelp.value = false
            showAppearance.value = true
        }
        watch(showAppearance, async open => {
            if (open) {
                await nextTick()
                document.querySelector('.theme-card.selected')?.focus()
            }
        })
        const cleanMode = ref(false)
        const playerStage = ref(null)
        const isFullscreen = ref(false)
        const loadError = ref('')
        const heartBurst = ref(false)
        const seekPreview = ref(null)
        const configBusy = ref(false)
        const indexProgress = ref(null)
        const noteSaving = ref(false)
        const modalOpen = computed(() => showDetail.value || showSearch.value || showConfig.value || showQuickTag.value || showHelp.value || showAppearance.value)
        let savedPreferences = {}
        try { savedPreferences = JSON.parse(localStorage.getItem('localdy_player') || '{}') || {} } catch {}
        const playback = reactive({
            paused: true, buffering: false, error: '', duration: 0, currentTime: 0, buffered: 0,
            volume: Number.isFinite(savedPreferences.volume) ? Math.max(0, Math.min(1, savedPreferences.volume)) : .8,
            muted: savedPreferences.muted === true,
            rate: [.5, .75, 1, 1.25, 1.5, 2].includes(savedPreferences.rate) ? savedPreferences.rate : 1,
            loop: savedPreferences.loop !== false, soundBlocked: false
        })

        // ★ 备注
        const noteText = ref('')
        const noteSaved = ref(false)

        // ★ 备注标签 → 文件名
        const pendingTags = ref([])
        const pendingTagsLoading = ref(false)
        const applyingTags = ref(false)

        // ★ 上次执行结果（在配置弹窗里展示）
        const lastApplyReport = ref(null)

        // ★ 自动改名开关（持久化，默认开启）
        const AUTO_TAG_KEY = 'auto_apply_tags_enabled'
        const autoApplyEnabled = ref(true)
        try {
            const savedAuto = localStorage.getItem(AUTO_TAG_KEY)
            if (savedAuto !== null) autoApplyEnabled.value = savedAuto === '1'
        } catch (e) {}

        // ★ 冷却期剩余秒数（用于 UI 展示，每秒刷新）
        const cooldownRemainSec = ref(0)
        let cooldownTimer = null

        function loadAutoApplyLastTs() {
            try {
                const v = parseInt(localStorage.getItem(AUTO_APPLY_LAST_KEY) || '0', 10)
                return Number.isFinite(v) ? v : 0
            } catch (e) { return 0 }
        }

        function markAutoApplyNow() {
            try {
                localStorage.setItem(AUTO_APPLY_LAST_KEY, String(Date.now()))
            } catch (e) {}
            refreshCooldown()
        }

        function remainingCooldownMs() {
            const last = loadAutoApplyLastTs()
            if (!last) return 0
            return Math.max(0, AUTO_APPLY_COOLDOWN_MS - (Date.now() - last))
        }

        function inCooldown() {
            return remainingCooldownMs() > 0
        }

        function refreshCooldown() {
            cooldownRemainSec.value = Math.ceil(remainingCooldownMs() / 1000)
        }

        const cooldownText = computed(() => {
            let s = cooldownRemainSec.value
            if (s <= 0) return ''
            const m = Math.floor(s / 60)
            const sec = s % 60
            if (m > 0) return `${m} 分 ${sec} 秒`
            return `${sec} 秒`
        })

        // ★ 全局轻提示（仅用于保存备注失败等关键情况）
        const toastMsg = ref('')
        let toastTimer = null
        function showToast(msg, duration = 2400) {
            if (!msg) return
            toastMsg.value = msg
            if (toastTimer) clearTimeout(toastTimer)
            toastTimer = setTimeout(() => { toastMsg.value = '' }, duration)
        }

        // ★ 快捷标签
        const LOCAL_TAG_KEY = 'quick_tags_history'
        const historyTags = ref([])
        const newTagInput = ref('')

        const refs = {
            currentIndex,
            videoQueue,
            activeVideoIndex,
            videoLoadStates,
            videoClass,
            showSearch,
            searchKeyword,
            searchScore,
            searchSize,
            searchWatched,
            sizeOperator,
            sizeValue,
            searchTimeMsg,
            newPath,
            pathList,
            playback,
            modalOpen,
            seekPreview,
            configBusy,
            indexProgress,
            showToast,
            refreshLibrary: async () => {
                await refreshTotalCount()
                const id = currentVideo.value?.id
                const previousIndex = currentIndex.value
                state.page = Math.max(1, Math.min(state.page, Math.ceil(state.totalVideos / state.pageSize) || 1))
                state.startPage = state.page
                await loadVideos(false, false)
                const index = state.videos.findIndex(v => v.id === id)
                currentIndex.value = index >= 0 ? index : Math.max(0, Math.min(previousIndex, state.videos.length - 1))
                videoPlayer.initializeVideoQueue()
            }
        }

        const videoPlayer = new VideoPlayer(state, refs)
        const searchManager = new SearchManager(state, refs)
        const configManager = new ConfigManager(refs)

        const currentVideo = computed(() => {
            return state.videos[currentIndex.value] || {}
        })

        const ambientCanvas = ref(null)
        let backdrop = null
        watch([ambientCanvas, () => currentVideo.value?.id, isLoading], async () => {
            await nextTick()
            if (!ambientCanvas.value) return
            if (!backdrop) backdrop = new VideoBackdrop(ambientCanvas.value)
            backdrop.attach(isLoading.value ? null : videoPlayer.media)
        }, { flush: 'post' })

        const libraryTab = computed(() => state.searchScore == 5 ? 'liked' : state.searchWatched === 'no' ? 'unwatched' : 'all')
        const libraryHeading = computed(() => ({ all: '随心看', liked: '我的喜欢', unwatched: '还没看过' })[libraryTab.value])
        const hasFilters = computed(() => !!(state.searchKeyword || state.searchScore || state.searchSize || state.searchWatched !== 'all'))
        const filterSummary = computed(() => [
            state.searchKeyword, state.searchScore ? state.searchScore + ' 分' : '',
            state.searchSize ? state.searchSize.replace('lte:', '≤ ').replace('gte:', '≥ ').replace('eq:', '= ') + ' MB' : '',
            ({ yes: '已看', no: '未看', stale: '最近一个月未看' })[state.searchWatched]
        ].filter(Boolean).join(' · '))
        const currentFolder = computed(() => {
            const parts = (currentVideo.value.detail || '').split(/[\\/]/).filter(Boolean)
            return parts.length > 1 ? parts[parts.length - 2] : '本地视频库'
        })
        const progressPercent = computed(() => playback.duration ? Math.min(100, (seekPreview.value ?? playback.currentTime) / playback.duration * 100) : 0)
        const bufferedPercent = computed(() => playback.duration ? Math.max(progressPercent.value, Math.min(100, playback.buffered / playback.duration * 100)) : 0)

        function formatTime(seconds) {
            seconds = Math.max(0, Math.floor(Number(seconds) || 0))
            const h = Math.floor(seconds / 3600)
            const m = Math.floor(seconds % 3600 / 60)
            const s = String(seconds % 60).padStart(2, '0')
            return h ? h + ':' + String(m).padStart(2, '0') + ':' + s : m + ':' + s
        }
        function formatSize(bytes) {
            if (!bytes) return '本地文件'
            const mb = bytes / 1024 / 1024
            return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB'
        }
        function applyPlaybackPreferences() {
            videoPlayer.applyPreferences()
            try { localStorage.setItem('localdy_player', JSON.stringify({ volume: playback.volume, muted: playback.muted, rate: playback.rate, loop: playback.loop })) } catch {}
        }
        function togglePlayback() { videoPlayer.toggle() }
        function toggleMute() {
            playback.muted = !playback.muted
            playback.soundBlocked = false
            if (!playback.muted && !playback.volume) playback.volume = .8
            applyPlaybackPreferences()
        }
        function enableSound() {
            playback.muted = false
            playback.soundBlocked = false
            if (!playback.volume) playback.volume = .8
            applyPlaybackPreferences()
            videoPlayer.playActiveVideo()
        }
        function setVolume(e) {
            playback.volume = Number(e.target.value)
            playback.muted = playback.volume === 0
            playback.soundBlocked = false
            applyPlaybackPreferences()
        }
        function toggleLoop() {
            playback.loop = !playback.loop
            applyPlaybackPreferences()
            showToast(playback.loop ? '已切换为单条循环' : '已切换为自动连播')
        }
        async function toggleFullscreen() {
            try {
                if (document.fullscreenElement) await document.exitFullscreen()
                else if (playerStage.value?.requestFullscreen) await playerStage.value.requestFullscreen()
                else if (videoPlayer.media?.webkitEnterFullscreen) videoPlayer.media.webkitEnterFullscreen()
                else showToast('当前浏览器暂不支持全屏')
            } catch { showToast('无法进入全屏，请再试一次') }
        }
        function syncFullscreen() { isFullscreen.value = !!document.fullscreenElement }
        function beginSeek(e) { videoPlayer.beginSeek(e) }
        function previewPointerSeek(e) { videoPlayer.previewPointerSeek(e) }
        function commitSeek(e) { videoPlayer.commitSeek(e) }
        function finishSeek(e) { videoPlayer.finishSeek(e) }
        function cancelSeek(e) { videoPlayer.cancelSeek(e) }
        function seekBy(delta) {
            if (videoPlayer.media) videoPlayer.seekTo(videoPlayer.media.currentTime + delta)
        }
        function onMediaEvent(e, id) { videoPlayer.onMediaEvent(e, id) }
        function onMediaError(e, id) {
            videoPlayer.onMediaError(e, id)
            // ★ 拆分模式：票据过期也会表现为加载失败，刷新票据重试一次
            // （每个 hash 只自动刷新一次，避免解码错误时无限循环）
            if (LOCALDY.split && !ticketRefreshed.has(id)) {
                const v = state.videos.find(x => x.id === id)
                if (v?.file_hash && ticketMap.value[v.file_hash]) {
                    ticketRefreshed.add(id)
                    const next = { ...ticketMap.value }
                    delete next[v.file_hash]
                    ticketMap.value = next
                    ensureTickets([v.file_hash])
                }
            }
        }
        function onEnded(id) { if (id === currentVideo.value.id && !playback.loop && !modalOpen.value) nextVideo() }
        function retryVideo() {
            playback.error = ''
            playback.buffering = true
            videoPlayer.media?.load()
            videoPlayer.playActiveVideo()
        }
        let tapTimer = null
        let heartTimer = null
        function handleStageClick(e) {
            if (Date.now() < videoPlayer.suppressClickUntil || modalOpen.value) return
            if (e.detail >= 2) {
                clearTimeout(tapTimer)
                if (currentVideo.value.score !== 5) submitScore(5)
                heartBurst.value = true
                clearTimeout(heartTimer)
                heartTimer = setTimeout(() => { heartBurst.value = false }, 700)
            } else {
                clearTimeout(tapTimer)
                tapTimer = setTimeout(togglePlayback, 230)
            }
        }
        function cancelTouch() { videoPlayer.touchStart = null }
        async function selectLibrary(tab) {
            searchKeyword.value = ''
            searchScore.value = tab === 'liked' ? 5 : 0
            searchWatched.value = tab === 'unwatched' ? 'no' : 'all'
            sizeValue.value = ''
            await doSearch()
        }
        async function resetFilters() { await selectLibrary('all') }
        async function shuffleVideo() {
            if (totalVideos.value < 2) return
            let target = Math.floor(Math.random() * totalVideos.value) + 1
            if (target === globalVideoIndex.value) target = target % totalVideos.value + 1
            await jumpToIndex(target)
        }

        const currentVideoIndex = computed(() => {
            const id = currentVideo.value?.id
            if (id !== undefined && id !== null) {
                const idx = state.videos.findIndex(v => v.id === id)
                if (idx !== -1) return idx + 1
            }
            return Math.min(currentIndex.value + 1, state.videos.length || 1)
        })

        const totalVideos = computed(() => state.totalVideos || 0)

        const globalVideoIndex = computed(() => {
            if (!currentVideo.value?.id) return 0
            return (state.startPage - 1) * state.pageSize + currentIndex.value + 1
        })

        const videoIndexLabel = computed(() => {
            if (!currentVideo.value?.id || !globalVideoIndex.value) return ''
            if (!totalVideos.value) return `[${globalVideoIndex.value}] `
            return `[${globalVideoIndex.value}/${totalVideos.value}] `
        })

        const currentVideoTitle = computed(() => {
            const title = currentVideo.value?.title || ''
            if (!currentVideo.value?.id) return title
            return `${videoIndexLabel.value}${title}`
        })

        /* ---------- ★ 标题 #标签解析 ---------- */
        function parseTags(text) {
            const parts = []
            if (!text) return parts
            const re = /#([^\s#]+)/g
            let last = 0
            let m
            while ((m = re.exec(text)) !== null) {
                const tag = m[1].replace(/[,，.。;；:：!！?？、)）\]】"'’”、]+$/, '')
                const matchedLen = m[0].length

                if (!tag) {
                    if (m.index > last) parts.push({ type: 'text', value: text.slice(last, m.index) })
                    parts.push({ type: 'text', value: m[0] })
                } else {
                    if (m.index > last) parts.push({ type: 'text', value: text.slice(last, m.index) })
                    parts.push({ type: 'tag', value: tag })
                }
                last = m.index + matchedLen
            }
            if (last < text.length) parts.push({ type: 'text', value: text.slice(last) })
            return parts
        }

        const titleParts = computed(() =>
            parseTags((currentVideo.value?.filename || currentVideo.value?.title || '').replace(/\.[a-z0-9]{2,5}$/i, ''))
        )

        /* ---------- ★ 点击标签 → 搜索该关键词 ---------- */
        async function searchByTag(tag) {
            if (!tag) return
            searchKeyword.value = tag
            state.searchKeyword = tag
            showSearch.value = false
            await doSearch()
        }

        async function refreshTotalCount() {
            const filters = JSON.stringify([state.searchKeyword, state.searchScore, state.searchSize, state.searchWatched])
            try {
                const total = await getTotalCount(
                    state.pageSize,
                    state.searchKeyword,
                    state.searchScore,
                    state.searchSize,
                    state.searchWatched
                )
                if (filters === JSON.stringify([state.searchKeyword, state.searchScore, state.searchSize, state.searchWatched])) state.totalVideos = total
            } catch (e) {
                console.error('获取总数失败:', e)
                throw e
            }
        }

        function updateUrlWithIndex() {
            const idx = globalVideoIndex.value
            if (!idx || idx < 1) return
            try {
                const url = new URL(window.location.href)
                const curV = url.searchParams.get(URL_VIDEO_KEY)
                const curT = url.searchParams.get(URL_TAB_KEY)
                if (curV === String(idx) && curT === TAB_ID) return
                url.searchParams.set(URL_TAB_KEY, TAB_ID)
                url.searchParams.set(URL_VIDEO_KEY, String(idx))
                window.history.replaceState(window.history.state, '', url.toString())
            } catch (e) {}
        }

        function readIndexFromUrl() {
            try {
                const params = new URLSearchParams(window.location.search)
                const v = parseInt(params.get(URL_VIDEO_KEY), 10)
                if (!Number.isFinite(v) || v <= 0) return null
                const urlTab = params.get(URL_TAB_KEY)
                if (urlTab && urlTab !== TAB_ID) return null
                return v
            } catch (e) {}
            return null
        }

        function clearForeignUrlParams() {
            try {
                const url = new URL(window.location.href)
                const urlTab = url.searchParams.get(URL_TAB_KEY)
                if (urlTab && urlTab !== TAB_ID) {
                    url.searchParams.delete(URL_VIDEO_KEY)
                    url.searchParams.delete(URL_TAB_KEY)
                    window.history.replaceState(window.history.state, '', url.toString())
                }
            } catch (e) {}
        }

        async function jumpToIndex(v, skipUrl = false) {
            if (!v || v < 1) return false
            if (state.totalVideos && v > state.totalVideos) {
                v = state.totalVideos
            }

            const page = Math.ceil(v / state.pageSize)
            const localIdx = (v - 1) % state.pageSize

            state.page = page
            state.startPage = page

            await loadVideos(false, false)

            const maxIdx = Math.max(0, state.videos.length - 1)
            currentIndex.value = Math.min(localIdx, maxIdx)

            videoPlayer.initializeVideoQueue()

            if (!skipUrl) {
                setTimeout(() => updateUrlWithIndex(), 0)
            }
            return true
        }

        async function doJump() {
            const v = parseInt(jumpIndex.value, 10)
            if (!Number.isFinite(v) || v < 1) return
            await jumpToIndex(v)
            jumpIndex.value = ''
        }

        watch(
            () => [currentVideo.value?.id, state.startPage],
            ([id]) => {
                if (id) updateUrlWithIndex()
            }
        )

        async function handlePopstate() {
            videoPlayer.pause()
            searchManager.loadFromUrlParams()
            await refreshTotalCount()
            const v = readIndexFromUrl()
            await jumpToIndex(v || 1, true)
        }

        let loadGeneration = 0
        async function loadVideos(append = false, initialize = true) {
            const generation = ++loadGeneration
            const newVideos = await fetchLatestVideos(state)
            if (generation !== loadGeneration) return 0
            if (!Array.isArray(newVideos)) throw new Error('视频列表返回格式不正确')
            loadError.value = ''

            if (append) {
                state.videos.push(...newVideos)
            } else {
                state.videos = newVideos
                currentIndex.value = 0
            }
            if (initialize) videoPlayer.initializeVideoQueue()
            return newVideos.length
        }

        videoPlayer.setLoadMoreVideos(async () => {
            return await loadVideos(true)
        })

        videoPlayer.setReloadVideos(async () => {
            return await loadVideos(false, false)
        })

        searchManager.setLoadVideosCallback(async (append = false) => {
            await refreshTotalCount()
            await loadVideos(append, false)
            state.startPage = state.page
            return state.totalVideos
        })

        async function initialLoad() {
            try {
                isLoading.value = true
                clearForeignUrlParams()
                searchManager.loadFromUrlParams()
                await refreshTotalCount()

                const urlIndex = readIndexFromUrl()

                if (urlIndex) {
                    await jumpToIndex(urlIndex, true)
                } else {
                    const totalPages = Math.max(1, Math.ceil(state.totalVideos / state.pageSize))
                    state.page = Math.floor(Math.random() * totalPages) + 1
                    state.startPage = state.page
                    await loadVideos(false)
                }

                showSearch.value = false
                showConfig.value = false

                isLoading.value = false
                videoPlayer.initializeVideoQueue()
                await nextTick()
                videoPlayer.playActiveVideo()
                scheduleAutoApply(2500)
            } catch (error) {
                console.error('Initial load failed:', error)
                loadError.value = '请检查本地服务是否启动，然后重新加载。'
                isLoading.value = false
            }
        }

        const prevVideo = () => videoPlayer.prevVideo()
        const nextVideo = () => videoPlayer.nextVideo()

        function toggleDetail() {
            showDetail.value = !showDetail.value
            if (showDetail.value) {
                showQuickTag.value = false
            }
        }

        const handleTouchStart = (e) => videoPlayer.handleTouchStart(e)
        const handleTouchEnd = (e) => videoPlayer.handleTouchEnd(e)
        const onVideoLoaded = (index) => videoPlayer.onVideoLoaded(index)
        const onVideoReady = (index) => videoPlayer.onVideoReady(index)

        async function submitScore(newScore) {
            if (!currentVideo.value?.id) return
            if (likeLoading.value) return

            const video = currentVideo.value
            const prevScore = video.score || 0
            const videoId = video.id
            likeLoading.value = newScore || prevScore || -1
            video.score = newScore

            try {
                await updateScore(videoId, newScore)
                state.videos.filter(v => v.id === videoId).forEach(v => { v.score = newScore })
                if (currentVideo.value.id === videoId) burstScore.value = newScore
                showToast(newScore === 0 ? (prevScore === 5 ? '已撤回喜欢' : prevScore === 1 ? '已撤回点踩' : '已清除评分') : newScore === 5 ? '已加入我的喜欢' : newScore === 1 ? '已点踩，再次点击可撤回' : '评分已保存')
                if (state.searchScore > 0 && newScore !== state.searchScore) {
                    await refs.refreshLibrary().catch(() => showToast('评分已保存，刷新筛选列表失败'))
                }
                setTimeout(() => {
                    if (burstScore.value === newScore) burstScore.value = 0
                }, 700)
            } catch (err) {
                console.error('评分提交失败:', err)
                video.score = prevScore
                state.videos.filter(v => v.id === videoId).forEach(v => { v.score = prevScore })
                if (currentVideo.value.id === videoId) shakeScore.value = newScore
                showToast('评分保存失败，已恢复原评分')
                setTimeout(() => {
                    if (shakeScore.value === newScore) shakeScore.value = 0
                }, 500)
            } finally {
                likeLoading.value = 0
            }
        }

        const likeVideo = (newScore = 5) => submitScore(currentVideo.value.score === newScore ? 0 : newScore)
        const updateScoreHandler = (newScore) => submitScore(newScore)
        let searchGeneration = 0
        const doSearch = async () => {
            const generation = ++searchGeneration
            videoPlayer.pause()
            isLoading.value = true
            try { await searchManager.doSearch() }
            catch (e) {
                loadError.value = '搜索失败，请检查本地服务后重试。'
                showToast(loadError.value)
            } finally {
                if (generation === searchGeneration) {
                    isLoading.value = false
                    videoPlayer.initializeVideoQueue()
                    await nextTick()
                    videoPlayer.playActiveVideo()
                }
            }
        }

        const fetchPaths = () => configManager.fetchPaths()
        const addPath = () => configManager.addPath()
        const indexPath = (path) => configManager.indexPath(path)
        const indexPath_del = (path) => configManager.indexPath_del(path)

        const setQuickSize = (operator, size) => {
            sizeOperator.value = operator
            sizeValue.value = size
            updateSizeFilter()
        }

        const clearSizeFilter = () => {
            sizeOperator.value = 'lte'
            sizeValue.value = ''
            state.searchSize = 0
        }

        const updateSizeFilter = () => {
            if (sizeValue.value && sizeValue.value > 0) {
                state.searchSize = `${sizeOperator.value}:${sizeValue.value}`
            } else {
                state.searchSize = 0
            }
        }

        // ★ 备注
        async function loadNote() {
            noteText.value = ''
            const id = currentVideo.value?.id
            if (!id) return
            try {
                const r = await axios.get(`/videos/${id}/note`)
                if (currentVideo.value.id === id && !noteText.value) noteText.value = r.data.note || ''
            } catch (e) {
                if (currentVideo.value.id === id) showToast('备注读取失败，请重试')
            }
        }

        async function saveNote() {
            const id = currentVideo.value?.id
            if (!id || noteSaving.value) return
            const savedText = noteText.value
            noteSaving.value = true
            try {
                const r = await axios.post(`/videos/${id}/note`, { note: savedText })
                const data = r.data || {}
                if ((savedText || '').trim() && data.cleared) {
                    showToast('备注保存失败：后端未能识别该视频的文件。\n常见原因：物理文件已被移动/删除。', 4000)
                    noteSaved.value = false
                    return
                }
                if (currentVideo.value.id === id) noteSaved.value = true
                setTimeout(() => { noteSaved.value = false }, 1500)

                // ★ 备注中含 #tag → 尝试自动改名（受冷却期约束）
                if (autoApplyEnabled.value && /#[^\s#]+/.test(savedText || '')) {
                    scheduleAutoApply(700)
                }
            } catch (e) {
                console.error('保存备注失败:', e)
                showToast('保存备注失败: ' + (e.response?.data?.error || e.message))
            } finally {
                noteSaving.value = false
            }
        }

        async function clearNote() {
            noteText.value = ''
            await saveNote()
        }

        // ★ 快捷标签
        function loadLocalTags() {
            try {
                return JSON.parse(localStorage.getItem(LOCAL_TAG_KEY) || '[]') || []
            } catch (e) {
                return []
            }
        }

        function saveLocalTags(tag) {
            if (!tag) return
            try {
                const local = loadLocalTags().filter(t => t !== tag)
                local.unshift(tag)
                localStorage.setItem(LOCAL_TAG_KEY, JSON.stringify(local.slice(0, 60)))
            } catch (e) {}
        }

        async function loadHistoryTags() {
            const local = loadLocalTags()
            let server = []
            try {
                const r = await axios.get('/notes/history-tags')
                server = r.data.tags || []
            } catch (e) {
                console.warn('获取历史标签失败:', e)
            }
            const seen = new Set()
            const merged = []
            for (const t of [...local, ...server]) {
                if (t && !seen.has(t)) {
                    seen.add(t)
                    merged.push(t)
                }
            }
            historyTags.value = merged.slice(0, 60)
        }

        function isTagInNote(tag) {
            if (!tag || !noteText.value) return false
            const matches = noteText.value.match(/#([^\s#]+)/g) || []
            return matches.some(x => x.slice(1) === tag)
        }

        async function quickAddTag(tag) {
            if (!tag || noteSaving.value) return
            const id = currentVideo.value?.id
            if (!id) return

            saveLocalTags(tag)

            if (!isTagInNote(tag)) {
                const base = (noteText.value || '').replace(/\s+$/, '')
                noteText.value = base ? `${base} #${tag}` : `#${tag}`
            }
            await saveNote()
            await loadHistoryTags()
        }

        async function addNewTag() {
            const t = (newTagInput.value || '').trim().replace(/^#/, '')
            if (!t) return
            if (/\s/.test(t)) {
                showToast('标签内不能含空格')
                return
            }
            newTagInput.value = ''
            await quickAddTag(t)
        }

        async function toggleQuickTag() {
            if (showQuickTag.value) {
                showQuickTag.value = false
                return
            }
            showQuickTag.value = true
            await loadNote()
            await loadHistoryTags()
        }

        function openFullDetail() {
            showQuickTag.value = false
            showDetail.value = true
        }

        // ★ 备注标签 → 文件名（配置弹窗）
        async function loadPendingTags() {
            pendingTagsLoading.value = true
            try {
                const r = await axios.get('/notes/pending-tags')
                pendingTags.value = r.data.items || []
            } catch (e) {
                console.error('获取待处理标签失败:', e)
                pendingTags.value = []
            } finally {
                pendingTagsLoading.value = false
            }
        }

        /* ---------- ★ 将改名结果同步到本地列表（不重新加载，不跳转） ---------- */
        function applyRenamedToLocal(renamed) {
            if (!renamed || !renamed.length) return

            const basename = (p) => {
                if (!p) return ''
                const s = String(p)
                const idx1 = s.lastIndexOf('/')
                const idx2 = s.lastIndexOf('\\')
                const idx = Math.max(idx1, idx2)
                return idx >= 0 ? s.slice(idx + 1) : s
            }

            const map = {}
            for (const r of renamed) {
                if (r && r.video_id != null && r.new) {
                    map[r.video_id] = r
                }
            }
            if (!Object.keys(map).length) return

            const updateOne = (v) => {
                if (!v || v.id == null) return
                const info = map[v.id]
                if (info && info.new) {
                    v.filename = basename(info.new)
                    v.detail   = info.new
                }
            }

            state.videos.forEach(updateOne)
            videoQueue.value.forEach(updateOne)
        }

        /* ---------- ★ 应用标签（完全静默，结果写入 lastApplyReport；执行后刷新冷却） ---------- */
        async function applyNoteTags(tags, options = {}) {
            if (applyingTags.value) return null

            applyingTags.value = true
            // ★ 只要真的发起了执行（手动或自动），就重置冷却
            markAutoApplyNow()

            try {
                const r = await axios.post('/notes/apply-tags',
                    { tags: tags || null, dry_run: false })

                // ★ 拆分模式：服务端只记任务，不直接改名
                if (r.data.queued) {
                    lastApplyReport.value = {
                        time: new Date().toLocaleString(),
                        scope: tags && tags.length ? `#${tags.join(' #')}` : '全部标签',
                        renamed: 0,
                        queued: true,
                        job_id: r.data.job_id,
                        files: r.data.files || 0,
                        errors: [],
                    }
                    showToast(`已提交给客户端执行（${r.data.files || 0} 个文件），任务 ${r.data.job_id}`)
                    return { queued: true, job_id: r.data.job_id }
                }

                const rep = r.data.report || {}
                const renamed = rep.renamed || []

                lastApplyReport.value = {
                    time: new Date().toLocaleString(),
                    scope: tags && tags.length ? `#${tags.join(' #')}` : '全部标签',
                    renamed: renamed.length,
                    skipped: rep.skipped || [],
                    notes_updated: rep.notes_updated || 0,
                    notes_cleared: rep.notes_cleared || 0,
                    errors: rep.errors || [],
                    recovered: rep.recovered || [],
                }

                if (showConfig.value) await loadPendingTags()
                await loadHistoryTags()

                applyRenamedToLocal(renamed)

                const ae = document.activeElement
                const editing = ae && ae.classList && ae.classList.contains('note-textarea')
                if (!editing && currentVideo.value?.id && (showDetail.value || showQuickTag.value)) {
                    await loadNote()
                }

                return rep
            } catch (e) {
                console.error('应用标签失败:', e)
                lastApplyReport.value = {
                    time: new Date().toLocaleString(),
                    scope: tags && tags.length ? `#${tags.join(' #')}` : '全部标签',
                    error: (e.response?.data?.error || e.message),
                }
                return null
            } finally {
                applyingTags.value = false
            }
        }

        function clearApplyReport() {
            lastApplyReport.value = null
        }

        /* ---------- ★ 自动改名：定时器 + 冷却期 + 静默执行 ---------- */
        let autoApplyTimer = null

        function scheduleAutoApply(delay = 1200) {
            if (!autoApplyEnabled.value) return
            // ★ 冷却期内不调度
            if (inCooldown()) return

            if (autoApplyTimer) clearTimeout(autoApplyTimer)
            autoApplyTimer = setTimeout(() => {
                autoApplyTimer = null
                autoApplyNoteTags()
            }, delay)
        }

        async function autoApplyNoteTags() {
            if (!autoApplyEnabled.value) return
            if (inCooldown()) return
            if (applyingTags.value) {
                // 正在处理，稍后再试（此时会被冷却期过滤）
                scheduleAutoApply(1500)
                return
            }
            try {
                const r = await axios.get('/notes/pending-tags')
                const items = r.data.items || []
                pendingTags.value = items
                if (!items.length) return     // 没东西可处理，不刷新冷却
                // applyNoteTags 内部会 markAutoApplyNow()
                await applyNoteTags(null)
            } catch (e) {
                console.warn('自动应用标签失败:', e)
            }
        }

        // 开关持久化；重新打开时若已过冷却期立即尝试一次
        watch(autoApplyEnabled, (v) => {
            try { localStorage.setItem(AUTO_TAG_KEY, v ? '1' : '0') } catch (e) {}
            if (v) scheduleAutoApply(500)
        })

        // 打开详情时加载备注；切换视频时若详情开着，也重新加载
        watch(showDetail, (v) => { if (v) loadNote() })
        watch(() => currentVideo.value?.id, () => {
            clearTimeout(tapTimer)
            burstScore.value = shakeScore.value = 0
            heartBurst.value = false
            noteSaved.value = false
            if (showDetail.value) loadNote()
            if (showQuickTag.value) loadNote()
        })

        // 打开配置弹窗时刷新待处理标签 + 刷新冷却显示
        watch(showConfig, (v) => {
            if (v) {
                loadPendingTags()
                refreshCooldown()
            }
        })

        /* ---------- ★ 键盘快捷键：Z 点赞 / X 点踩 ---------- */
        function handleGlobalKey(e) {
            if (e.key === 'Escape') {
                showHelp.value = showSearch.value = showConfig.value = showDetail.value = showQuickTag.value = showAppearance.value = false
                return
            }
            if (modalOpen.value) {
                if (e.key === 'Tab') {
                    const dialog = document.querySelector('.search-modal [role="dialog"]')
                    const items = dialog?.querySelectorAll('button:not(:disabled), input:not(:disabled), select, textarea, a[href]')
                    if (items?.length) {
                        const first = items[0], last = items[items.length - 1]
                        if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) { e.preventDefault(); last.focus() }
                        else if (!e.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) { e.preventDefault(); first.focus() }
                    }
                }
                return
            }
            const t = e.target
            const tag = (t && t.tagName ? t.tagName : '').toUpperCase()
            if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return
            if (e.ctrlKey || e.metaKey || e.altKey) return
            if (e.isComposing) return
            if (e.repeat) return

            const k = (e.key || '').toLowerCase()
            if (k === 'z') {
                e.preventDefault()
                likeVideo(5)
            } else if (k === 'x') {
                e.preventDefault()
                likeVideo(1)
            } else if (k === 'arrowdown') { e.preventDefault(); nextVideo()
            } else if (k === 'arrowup') { e.preventDefault(); prevVideo()
            } else if (k === 'arrowright') { e.preventDefault(); seekBy(5)
            } else if (k === 'arrowleft') { e.preventDefault(); seekBy(-5)
            } else if (k === ' ' && tag !== 'BUTTON') { e.preventDefault(); togglePlayback()
            } else if (k === 'm') { e.preventDefault(); toggleMute()
            } else if (k === 'f') { e.preventDefault(); toggleFullscreen()
            } else if (k === 'c') { e.preventDefault(); cleanMode.value = !cleanMode.value
            } else if (k === '/') { e.preventDefault(); showSearch.value = true
            }
        }

        let resumeAfterModal = false
        let modalVideoId = null
        let previousFocus = null
        watch(modalOpen, async open => {
            if (open) {
                resumeAfterModal = !playback.paused || videoPlayer._shouldPlay
                modalVideoId = currentVideo.value.id
                previousFocus = document.activeElement
                clearTimeout(tapTimer)
                videoPlayer.pause()
                await nextTick()
                const dialog = document.querySelector('.search-modal [role="dialog"]')
                ;(dialog?.querySelector('.theme-card.selected') || dialog?.querySelector('.search-input') || dialog?.querySelector('button'))?.focus()
            } else {
                if (!document.hidden && (resumeAfterModal || modalVideoId !== currentVideo.value.id)) videoPlayer.playActiveVideo()
                previousFocus?.focus?.()
            }
        })
        let resumeAfterVisibility = false
        function handleVisibility() {
            if (document.hidden) {
                resumeAfterVisibility = !playback.paused
                videoPlayer.pause()
            } else if (resumeAfterVisibility && !modalOpen.value) {
                videoPlayer.playActiveVideo()
                resumeAfterVisibility = false
            }
        }
        const safePopstate = () => handlePopstate().catch(() => showToast('恢复浏览位置失败'))
        onMounted(() => {
            initialLoad()
            fetchPaths()
            videoPlayer.initEventListeners()
            loadHistoryTags()
            checkClient()
            window.addEventListener('keydown', handleGlobalKey)
            window.addEventListener('popstate', safePopstate)
            document.addEventListener('visibilitychange', handleVisibility)
            document.addEventListener('fullscreenchange', syncFullscreen)

            // ★ 冷却期剩余时间每秒刷新
            refreshCooldown()
            cooldownTimer = setInterval(refreshCooldown, 1000)
        })

        onUnmounted(() => {
            backdrop?.destroy()
            videoPlayer.destroy()
            configManager.disposed = true
            clearInterval(cooldownTimer)
            ;[autoApplyTimer, toastTimer, tapTimer, heartTimer].forEach(clearTimeout)
            window.removeEventListener('keydown', handleGlobalKey)
            window.removeEventListener('popstate', safePopstate)
            document.removeEventListener('visibilitychange', handleVisibility)
            document.removeEventListener('fullscreenchange', syncFullscreen)
        })
        return {
            showAppearance, themeChoices, activeTheme, currentTheme, setTheme, openAppearance,
            playback, modalOpen, showHelp, cleanMode, playerStage, ambientCanvas, isFullscreen,
            loadError, initialLoad, heartBurst, seekPreview, configBusy, indexProgress, noteSaving,
            libraryTab, libraryHeading, hasFilters, filterSummary, currentFolder,
            progressPercent, bufferedPercent, formatTime, formatSize,
            applyPlaybackPreferences, togglePlayback, toggleMute, enableSound, setVolume,
            toggleLoop, toggleFullscreen, beginSeek, previewPointerSeek, commitSeek, finishSeek, cancelSeek,
            onMediaEvent, onMediaError, onEnded, retryVideo, handleStageClick, cancelTouch,
            selectLibrary, resetFilters, shuffleVideo, submitScore,
            state,
            currentIndex,
            currentVideo,
            currentVideoIndex,
            globalVideoIndex,
            totalVideos,
            videoIndexLabel,
            currentVideoTitle,
            jumpIndex,
            doJump,
            jumpToIndex,
            refreshTotalCount,
            likeLoading,
            burstScore,
            shakeScore,
            showDetail,
            getVideoUrl,
            // ★ 拆分模式：客户端状态 + 配对 + 下载链接
            splitMode: LOCALDY.split,
            clientOnline, clientChecked, pairingNeeded, pairingCode,
            savePairing, checkClient, downloadUrl: LOCALDY.download,
            prevVideo,
            nextVideo,
            toggleDetail,
            handleTouchStart,
            handleTouchEnd,
            updateScore: updateScoreHandler,
            showSearch,
            searchKeyword,
            searchScore,
            searchSize,
            searchWatched,
            sizeOperator,
            sizeValue,
            setQuickSize,
            clearSizeFilter,
            updateSizeFilter,
            doSearch,
            likeVideo,
            showConfig,
            newPath,
            pathList,
            addPath,
            indexPath,
            indexPath_del,
            searchTimeMsg,
            videoClass,
            videoQueue,
            activeVideoIndex,
            onVideoLoaded,
            onVideoReady,
            isLoading,
            // ★ 标签
            titleParts,
            searchByTag,
            // ★ 备注
            noteText,
            noteSaved,
            saveNote,
            clearNote,
            // ★ 备注标签 → 文件名
            pendingTags,
            pendingTagsLoading,
            applyingTags,
            loadPendingTags,
            applyNoteTags,
            // ★ 自动改名 + 冷却
            autoApplyEnabled,
            cooldownRemainSec,
            cooldownText,
            // ★ 执行结果展示
            lastApplyReport,
            clearApplyReport,
            // ★ 轻提示
            toastMsg,
            // ★ 快捷标签
            showQuickTag,
            historyTags,
            newTagInput,
            toggleQuickTag,
            openFullDetail,
            quickAddTag,
            addNewTag,
            isTagInNote,
            loadHistoryTags,
        }
    }
}).component('app-icon', AppIcon).mount('#app')
