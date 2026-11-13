/* Keep media elements keyed by video id: adjacent videos stay preloaded on a switch. */
class VideoPlayer {
    constructor(state, refs) {
        this.state = state
        this.refs = refs
        this.reloadVideos = async () => {}
        this._switching = false
        this._activeId = null
        this._lastPlayedId = null
        this._generation = 0
        this._shouldPlay = true
        this._seek = null
        this._wheelTotal = 0
        this._lastWheel = 0
        this.touchStart = null
        this.suppressClickUntil = 0
        this.listeners = []
    }

    get activeId() { return this.state.videos[this.refs.currentIndex.value]?.id }
    get media() { return document.querySelector('video.active-video') }
    get blocked() { return this.refs.modalOpen.value || document.hidden }

    updateVideoClass(video = this.media) {
        if (!video?.videoWidth) return
        this.refs.videoClass.value = video.videoWidth / video.videoHeight > 1.1 ? 'video-landscape' : 'video-portrait'
    }

    initializeVideoQueue() {
        const index = this.refs.currentIndex.value
        const current = this.state.videos[index]
        const old = this._activeId
        this._activeId = current?.id ?? null
        this.refs.videoQueue.value = this.state.videos.slice(Math.max(0, index - 1), index + 2)
        this.refs.activeVideoIndex.value = this.refs.videoQueue.value.findIndex(v => v.id === current?.id)
        if (old === this._activeId) return
        this._generation++
        this.endSeek(false, false)
        this.pauseAllVideos()
        Object.assign(this.refs.playback, { currentTime: 0, duration: 0, buffered: 0, paused: true, buffering: !!current, error: '' })
        this.refs.seekPreview.value = null
        this.refs.videoClass.value = 'video-portrait'
        this._shouldPlay = true
        Vue.nextTick().then(() => {
            const video = this.media
            if (!video) return
            video.currentTime = 0
            this.applyPreferences()
            this.updateVideoClass(video)
            this.readMedia(video)
            if (!this.blocked) this.playActiveVideo()
        })
    }

    async navigate(delta) {
        if (this._switching || !this.state.videos.length || !this.state.totalVideos) return
        this._switching = true
        try {
            const globalIndex = (this.state.startPage - 1) * this.state.pageSize + this.refs.currentIndex.value
            const target = (globalIndex + delta + this.state.totalVideos) % this.state.totalVideos
            const targetPage = Math.floor(target / this.state.pageSize) + 1
            if (targetPage !== this.state.startPage) {
                const oldPage = this.state.page
                this.state.page = targetPage
                try {
                    await this.reloadVideos()
                    this.state.startPage = targetPage
                } catch (e) {
                    this.state.page = oldPage
                    this.refs.showToast('切换失败，请检查本地服务是否正常')
                    return
                }
            }
            this.refs.currentIndex.value = Math.min(target % this.state.pageSize, this.state.videos.length - 1)
            this.initializeVideoQueue()
        } finally {
            this._switching = false
        }
    }

    nextVideo() { return this.navigate(1) }
    prevVideo() { return this.navigate(-1) }
    setReloadVideos(callback) { this.reloadVideos = callback }
    setLoadMoreVideos() {} // compatibility with the existing loading wiring

    pauseAllVideos() {
        document.querySelectorAll('video').forEach(v => { v.pause(); v.muted = true })
    }

    applyPreferences() {
        const video = this.media
        if (!video) return
        const p = this.refs.playback
        video.muted = p.muted
        video.volume = p.volume
        video.playbackRate = p.rate
        video.loop = p.loop
    }

    async playActiveVideo() {
        const video = this.media
        if (!video || this.blocked || this.refs.playback.error) return
        if (video.readyState >= 1 && video.duration && !video.videoWidth) {
            this.refs.playback.error = '浏览器未能解码视频画面。这个文件可能使用 H.265 / HEVC 编码，请换用支持该编码的浏览器，或转换为 H.264 MP4。'
            this.pause()
            return
        }
        const generation = this._generation
        this._shouldPlay = true
        this.applyPreferences()
        document.querySelectorAll('video.hidden-video').forEach(v => { v.pause(); v.muted = true })
        try {
            await video.play()
        } catch (e) {
            if (generation !== this._generation || !this._shouldPlay || this.blocked) return
            if (e.name === 'NotAllowedError' && !video.muted) {
                this.refs.playback.muted = true
                this.refs.playback.soundBlocked = true
                video.muted = true
                try { await video.play() } catch {
                    this.refs.playback.paused = true
                    this.refs.playback.buffering = false
                }
            } else if (e.name !== 'AbortError') {
                this.refs.playback.paused = true
                this.refs.playback.buffering = false
                if (e.name === 'NotSupportedError') this.refs.playback.error = '浏览器无法解码这个文件。请使用支持的 MP4（H.264）或 WebM 视频。'
            }
        }
    }

    pause() {
        this._shouldPlay = false
        this.media?.pause()
        this.refs.playback.paused = true
        this.refs.playback.buffering = false
    }

    toggle() {
        if (this.media?.paused) this.playActiveVideo()
        else this.pause()
    }

    seekTo(seconds) {
        const video = this.media
        const duration = video?.duration
        if (!video || !Number.isFinite(duration) || duration <= 0 || !Number.isFinite(seconds)) return false
        const target = Math.max(0, Math.min(duration, seconds))
        video.currentTime = target
        // Sync before clearing the preview: Vue must not restore the range's old value.
        this.refs.playback.currentTime = target
        return true
    }

    beginSeek(event) {
        const video = this.media
        if (event.button !== 0 || event.isPrimary === false || this._seek || !video || !this.refs.playback.duration) return
        const range = event.currentTarget
        event.preventDefault()
        this._seek = { id: this.activeId, video, range, pointerId: event.pointerId, wasPlaying: !video.paused }
        range.focus({ preventScroll: true })
        range.setPointerCapture(event.pointerId)
        this.pause()
        this.previewPointerSeek(event)
    }

    previewPointerSeek(event) {
        const gesture = this._seek
        if (!gesture || event.pointerId !== gesture.pointerId) return
        const rect = gesture.range.getBoundingClientRect()
        if (!rect.width) return
        const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width))
        const target = Math.min(gesture.video.duration, Math.round(ratio * gesture.video.duration * 10) / 10)
        this.refs.seekPreview.value = target
        gesture.range.value = target
    }

    commitSeek(event) {
        // Pointer gestures commit once on release. Input/change also cover keyboard and assistive controls.
        if (this._seek) return
        const target = Number(event.currentTarget.value)
        if (target !== this.refs.playback.currentTime) this.seekTo(target)
    }

    finishSeek(event) {
        if (event?.pointerId != null && event.pointerId !== this._seek?.pointerId) return
        if (event?.type === 'pointerup') this.previewPointerSeek(event)
        this.endSeek(true)
    }

    cancelSeek(event) {
        if (event?.pointerId != null && event.pointerId !== this._seek?.pointerId) return
        this.endSeek(false)
    }

    endSeek(commit, resume = true) {
        const gesture = this._seek
        if (!gesture) return
        this._seek = null
        const sameVideo = gesture.id === this.activeId && gesture.video === this.media
        if (commit && sameVideo && this.refs.seekPreview.value !== null) this.seekTo(this.refs.seekPreview.value)
        this.refs.seekPreview.value = null
        if (gesture.range.hasPointerCapture(gesture.pointerId)) gesture.range.releasePointerCapture(gesture.pointerId)
        if (resume && sameVideo && gesture.wasPlaying && !this.blocked) this.playActiveVideo()
    }

    readMedia(video = this.media) {
        if (!video || Number(video.dataset.videoId) !== this.activeId) return
        const p = this.refs.playback
        p.currentTime = Number.isFinite(video.currentTime) ? video.currentTime : 0
        p.duration = Number.isFinite(video.duration) ? video.duration : 0
        p.paused = video.paused
        p.buffered = 0
        for (let i = 0; i < video.buffered.length; i++) {
            if (video.buffered.start(i) <= video.currentTime + .5 && video.buffered.end(i) >= video.currentTime) {
                p.buffered = video.buffered.end(i)
                break
            }
        }
    }

    onMediaEvent(event, id) {
        if (id !== this.activeId) return
        this.readMedia(event.target)
        if (event.type === 'loadedmetadata') {
            this.updateVideoClass(event.target)
            if (!event.target.videoWidth && event.target.duration) {
                this.refs.playback.error = '浏览器未能解码视频画面。这个文件可能使用 H.265 / HEVC 编码，请换用支持该编码的浏览器，或转换为 H.264 MP4。'
                this.pause()
            }
        }
        if (event.type === 'waiting') this.refs.playback.buffering = this._shouldPlay
        if (event.type === 'playing') {
            if (this.blocked) { this.pause(); return }
            this.refs.playback.buffering = false
            if (id !== this._lastPlayedId) {
                this._lastPlayedId = id
                axios.post('/videos/' + id + '/play').catch(() => {})
            }
        }
        if (event.type === 'pause') this.refs.playback.buffering = false
    }

    onVideoLoaded(index) {
        if (index === this.refs.activeVideoIndex.value) this.readMedia()
    }

    onVideoReady(index) {
        if (index !== this.refs.activeVideoIndex.value) return
        this.refs.playback.buffering = false
        this.readMedia()
        // A user pause must survive later canplay events.
        if (this._shouldPlay && !this.blocked && this.media?.paused) this.playActiveVideo()
    }

    onMediaError(event, id) {
        if (id !== this.activeId) return
        const code = event.target.error?.code
        this.refs.playback.error = code === 2
            ? '读取视频失败，请检查文件位置或网络共享连接，再试一次。'
            : code === 3
                ? '视频解码失败，文件可能损坏或使用了浏览器不支持的编码。'
                : '文件可能已移动、离线，或格式不受浏览器支持。可以重试或浏览下一个。'
        this.refs.playback.buffering = false
        this.refs.playback.paused = true
    }

    handleTouchStart(e) {
        if (this.blocked || e.touches.length !== 1) { this.touchStart = null; return }
        const t = e.touches[0]
        this.touchStart = { x: t.clientX, y: t.clientY }
    }

    handleTouchEnd(e) {
        if (!this.touchStart || this.blocked) return
        const t = e.changedTouches[0]
        const y = t.clientY - this.touchStart.y
        const x = t.clientX - this.touchStart.x
        this.touchStart = null
        if (Math.abs(y) > 55 && Math.abs(y) > Math.abs(x) * 1.3) {
            this.suppressClickUntil = Date.now() + 450
            if (y < 0) this.nextVideo()
            else this.prevVideo()
        }
    }

    initEventListeners() {
        const wheel = e => {
            if (this.blocked || e.ctrlKey || e.target.closest('.player-controls') || !e.target.closest('.player-stage')) return
            if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return
            e.preventDefault()
            const now = Date.now()
            if (now - this._lastWheel < 650) return
            if (now - this._wheelAt > 200) this._wheelTotal = 0
            this._wheelAt = now
            this._wheelTotal += e.deltaY * (e.deltaMode === 1 ? 16 : 1)
            if (Math.abs(this._wheelTotal) < 50) return
            this._lastWheel = now
            const direction = this._wheelTotal > 0 ? 1 : -1
            this._wheelTotal = 0
            this.navigate(direction)
        }
        window.addEventListener('wheel', wheel, { passive: false })
        this.listeners.push(() => window.removeEventListener('wheel', wheel))
    }

    destroy() {
        this._generation++
        this.endSeek(false, false)
        this.pauseAllVideos()
        this.listeners.forEach(remove => remove())
    }
}
