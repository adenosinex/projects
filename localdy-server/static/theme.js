/* Apply the saved appearance before the page paints. Themes only change the UI. */
const LocaldyThemes = (() => {
    const choices = Object.freeze([
        { id: 'light', name: '亮色', description: '浅灰界面，清爽明亮', bg: '#e3e6eb', surface: '#f5f6f8', player: '#ccd2da', accent: '#c7355b' },
        { id: 'dark', name: '全黑', description: '保留原本的深黑界面', bg: '#111114', surface: '#19191e', player: '#070709', accent: '#ff365f' }
    ])
    // Keep existing appearance preferences when simplifying the theme choices.
    const normalize = id => id === 'dark' || id === 'graphite' ? 'dark' : 'light'
    function load() {
        try {
            const saved = localStorage.getItem('localdy_theme')
            return normalize(saved)
        } catch { return 'light' }
    }
    function apply(id, persist = true) {
        const selected = normalize(id)
        document.documentElement.dataset.theme = selected
        const theme = choices.find(item => item.id === selected)
        document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme.bg)
        if (persist) {
            try { localStorage.setItem('localdy_theme', selected) } catch {}
        }
        return selected
    }
    apply(load(), false)
    return Object.freeze({ choices, load, apply })
})()
