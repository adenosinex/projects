/* Reuse the active decoder to paint a small, blurred copy of the local video. */
class VideoBackdrop {
    constructor(canvas) {
        this.canvas = canvas
        this.context = canvas.getContext('2d', { alpha: false })
        this.video = null
        this.frameId = null
        this.timerId = null
        this.lastFrameAt = -Infinity
        this.listeners = []
        this.resizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(() => this.draw()) : null
        this.resizeObserver?.observe(canvas)
    }

    attach(video) {
        if (video === this.video) { this.draw(); return }
        this.stop()
        this.listeners.forEach(remove => remove())
        this.listeners = []
        this.video = video
        this.lastFrameAt = -Infinity
        this.canvas.dataset.ready = 'false'
        if (!video) return
        const listen = (name, handler) => {
            video.addEventListener(name, handler)
            this.listeners.push(() => video.removeEventListener(name, handler))
        }
        const refresh = () => { this.draw(); this.start() }
        ;['loadeddata', 'seeked', 'playing'].forEach(name => listen(name, refresh))
        listen('play', () => this.start())
        listen('pause', () => { this.stop(); this.draw() })
        listen('ended', () => { this.stop(); this.draw() })
        ;['emptied', 'error'].forEach(name => listen(name, () => {
            this.stop()
            this.canvas.dataset.ready = 'false'
        }))
        refresh()
    }

    draw() {
        const video = this.video
        if (!this.context || !video || video.readyState < 2 || !video.videoWidth || !video.videoHeight || video.error || document.hidden) return false
        // The background follows the stage's cover crop; the foreground stays contain.
        const stageWidth = this.canvas.clientWidth || 320
        const stageHeight = this.canvas.clientHeight || 180
        const scale = Math.min(1, 256 / Math.max(stageWidth, stageHeight))
        const width = Math.max(1, Math.round(stageWidth * scale))
        const height = Math.max(1, Math.round(stageHeight * scale))
        if (this.canvas.width !== width || this.canvas.height !== height) {
            this.canvas.width = width
            this.canvas.height = height
        }
        const cover = Math.max(width / video.videoWidth, height / video.videoHeight)
        const cropWidth = width / cover, cropHeight = height / cover
        try {
            this.context.drawImage(video, (video.videoWidth - cropWidth) / 2, (video.videoHeight - cropHeight) / 2, cropWidth, cropHeight, 0, 0, width, height)
            this.canvas.dataset.ready = 'true'
            return true
        } catch {
            this.canvas.dataset.ready = 'false'
            return false
        }
    }

    start() {
        const video = this.video
        if (!video || video.paused || video.ended || video.error || document.hidden || this.frameId !== null || this.timerId !== null) return
        if (typeof video.requestVideoFrameCallback === 'function') {
            this.frameId = video.requestVideoFrameCallback(now => {
                if (video !== this.video) return
                this.frameId = null
                if (now - this.lastFrameAt >= 200) {
                    this.draw()
                    this.lastFrameAt = now
                }
                this.start()
            })
        } else {
            this.timerId = setTimeout(() => {
                if (video !== this.video) return
                this.timerId = null
                this.draw()
                this.start()
            }, 200)
        }
    }

    stop() {
        if (this.frameId !== null) this.video?.cancelVideoFrameCallback?.(this.frameId)
        clearTimeout(this.timerId)
        this.frameId = this.timerId = null
    }

    destroy() { this.attach(null); this.resizeObserver?.disconnect() }
}
