(function () {
    "use strict";

    const STORAGE_KEY = "musicPlayerState";
    const root = document.getElementById("persistentPlayer");
    const title = document.getElementById("persistentTitle");
    const previousButton = document.getElementById("miniPrevious");
    const playButton = document.getElementById("miniPlayPause");
    const nextButton = document.getElementById("miniNext");
    const autoplayButton = document.getElementById("miniAutoplay");
    const loopButton = document.getElementById("miniLoop");
    const shuffleButton = document.getElementById("miniShuffle");
    const slider = document.getElementById("miniProgress");
    const currentTimeLabel = document.getElementById("miniCurrentTime");
    const durationLabel = document.getElementById("miniDuration");
    const audio = document.getElementById("persistentAudio");

    if (!root || !title || !previousButton || !playButton || !nextButton ||
        !autoplayButton || !loopButton || !shuffleButton || !slider ||
        !currentTimeLabel || !durationLabel || !audio) {
        return;
    }

    let state = readState() || {
        queue: [],
        currentIndex: 0,
        currentTime: 0,
        playing: false,
        autoplay: true,
        loop: false,
        shuffle: false,
        history: []
    };

    let intentionalPause = false;
    let interruptedPlayback = false;
    let resumeTimer = null;
    let loadedUrl = "";

    function readState() {
        try {
            const value = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
            return value && typeof value === "object" ? value : null;
        } catch (_) {
            return null;
        }
    }

    function writeState(updates) {
        state = Object.assign({}, state, updates);
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }

    function formatTime(seconds) {
        if (!Number.isFinite(seconds) || seconds < 0) {
            return "0:00";
        }
        const minutes = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60).toString().padStart(2, "0");
        return `${minutes}:${secs}`;
    }

    function updateControls() {
        const hasQueue = Array.isArray(state.queue) && state.queue.length > 0;
        const hasMultiple = hasQueue && state.queue.length > 1;

        playButton.textContent = audio.paused ? "▶" : "⏸";
        playButton.setAttribute("aria-label", audio.paused ? "Play" : "Pause");
        previousButton.disabled = !hasMultiple;
        nextButton.disabled = !hasMultiple;

        autoplayButton.setAttribute("aria-pressed", String(Boolean(state.autoplay)));
        loopButton.setAttribute("aria-pressed", String(Boolean(state.loop)));
        shuffleButton.setAttribute("aria-pressed", String(Boolean(state.shuffle)));

        autoplayButton.classList.toggle("active", Boolean(state.autoplay));
        loopButton.classList.toggle("active", Boolean(state.loop));
        shuffleButton.classList.toggle("active", Boolean(state.shuffle));

        audio.loop = Boolean(state.loop);
    }

    function updateProgress() {
        const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
        const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;

        currentTimeLabel.textContent = formatTime(current);
        durationLabel.textContent = formatTime(duration);
        slider.max = duration > 0 ? String(duration) : "0";
        slider.value = duration > 0 ? String(Math.min(current, duration)) : "0";
        slider.disabled = duration <= 0;
    }

    function updateStateFromAudio() {
        if (!audio.src) {
            return;
        }

        const queue = Array.isArray(state.queue) ? state.queue : [];
        const track = queue[state.currentIndex];

        writeState({
            url: audio.src,
            title: track?.title || state.title || "Unknown song",
            path: track?.path || state.path || "",
            drive_index: track?.drive_index ?? state.drive_index ?? null,
            currentTime: Number.isFinite(audio.currentTime) ? audio.currentTime : 0,
            playing: !audio.paused
        });
    }

    function syncCurrentTrackState() {
        const queue = Array.isArray(state.queue) ? state.queue : [];
        const track = queue[state.currentIndex];
        if (!track) {
            return;
        }

        state.url = track.url;
        state.title = track.title || "Unknown song";
        state.path = track.path || "";
        state.drive_index = track.drive_index ?? null;
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    }

    function showCurrentTrack() {
        const queue = Array.isArray(state.queue) ? state.queue : [];
        const index = Number.isInteger(state.currentIndex) ? state.currentIndex : 0;
        const track = queue[index];

        if (!track || !track.url) {
            root.hidden = true;
            return;
        }

        root.hidden = false;
        title.textContent = "Now playing: " + (track.title || "Unknown song");
        syncCurrentTrackState();
        updateControls();
        updateProgress();
    }

    function loadTrack(track, shouldPlay, restorePosition) {
        if (!track || !track.url) {
            return;
        }

        root.hidden = false;
        title.textContent = "Now playing: " + (track.title || "Unknown song");
        loadedUrl = track.url;

        if ("mediaSession" in navigator && "MediaMetadata" in window) {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: track.title || "Unknown song",
                artist: "Local Music Library",
                album: state.playlist || "Music Library"
            });
        }

        const startTime = restorePosition && Number.isFinite(state.currentTime)
            ? Math.max(0, state.currentTime)
            : 0;

        if (audio.src !== track.url) {
            audio.src = track.url;
        }

        audio.addEventListener("loadedmetadata", function restoreAndPlay() {
            if (startTime > 0 && Number.isFinite(audio.duration)) {
                try {
                    audio.currentTime = Math.min(startTime, audio.duration);
                } catch (_) {}
            }

            updateProgress();

            if (shouldPlay) {
                audio.play().catch(() => {
                    interruptedPlayback = true;
                    writeState({ playing: true });
                });
            }
        }, { once: true });

        updateControls();
    }

    function setQueue(queue, index, playlist, shouldPlay) {
        const cleanQueue = Array.isArray(queue)
            ? queue.filter(item => item && item.url)
            : [];

        if (!cleanQueue.length) {
            return;
        }

        const safeIndex = Number.isInteger(index)
            ? Math.max(0, Math.min(index, cleanQueue.length - 1))
            : 0;

        state = Object.assign({}, state, {
            queue: cleanQueue,
            currentIndex: safeIndex,
            playlist: playlist || "",
            currentTime: 0,
            playing: Boolean(shouldPlay),
            history: [safeIndex]
        });

        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        playIndex(safeIndex, Boolean(shouldPlay));
    }

    function playIndex(index, shouldPlay) {
        const queue = Array.isArray(state.queue) ? state.queue : [];
        if (!queue.length) {
            return;
        }

        const safeIndex = Math.max(0, Math.min(index, queue.length - 1));
        const track = queue[safeIndex];
        const wasDifferent = safeIndex !== state.currentIndex || audio.src !== track.url;

        state.currentIndex = safeIndex;
        state.currentTime = wasDifferent ? 0 : state.currentTime;
        state.playing = Boolean(shouldPlay);
        state.url = track.url;
        state.title = track.title || "Unknown song";
        state.path = track.path || "";
        state.drive_index = track.drive_index ?? null;

        if (wasDifferent && state.shuffle) {
            const history = Array.isArray(state.history) ? state.history.slice() : [];
            if (history[history.length - 1] !== safeIndex) {
                history.push(safeIndex);
            }
            state.history = history.slice(-100);
        }

        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        loadTrack(track, Boolean(shouldPlay), !wasDifferent);
        updateControls();
    }

    function randomNextIndex() {
        const queue = state.queue || [];
        if (queue.length <= 1) {
            return state.currentIndex || 0;
        }

        const choices = queue
            .map((_, index) => index)
            .filter(index => index !== state.currentIndex);

        return choices[Math.floor(Math.random() * choices.length)];
    }

    function nextTrack() {
        const queue = state.queue || [];
        if (!queue.length) {
            return;
        }

        let nextIndex;
        if (state.shuffle) {
            nextIndex = randomNextIndex();
        } else {
            nextIndex = (state.currentIndex + 1) % queue.length;
        }

        playIndex(nextIndex, true);
    }

    function previousTrack() {
        const queue = state.queue || [];
        if (!queue.length) {
            return;
        }

        if (audio.currentTime > 3) {
            audio.currentTime = 0;
            writeState({ currentTime: 0 });
            return;
        }

        let previousIndex;
        if (state.shuffle && Array.isArray(state.history) && state.history.length > 1) {
            const history = state.history.slice();
            history.pop();
            previousIndex = history[history.length - 1];
            state.history = history;
            localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
        } else {
            previousIndex = (state.currentIndex - 1 + queue.length) % queue.length;
        }

        playIndex(previousIndex, true);
    }

    function attemptResume() {
        if (!interruptedPlayback || !audio.src || !audio.paused ||
            document.visibilityState !== "visible") {
            return;
        }

        if (resumeTimer !== null) {
            clearTimeout(resumeTimer);
        }

        resumeTimer = setTimeout(() => {
            resumeTimer = null;
            if (!interruptedPlayback || !audio.paused) {
                return;
            }

            audio.play().catch(() => {});
        }, 300);
    }

    previousButton.addEventListener("click", previousTrack);
    nextButton.addEventListener("click", nextTrack);

    playButton.addEventListener("click", () => {
        if (audio.paused) {
            intentionalPause = false;
            interruptedPlayback = false;
            audio.play().catch(() => {});
        } else {
            intentionalPause = true;
            interruptedPlayback = false;
            audio.pause();
        }
    });

    autoplayButton.addEventListener("click", () => {
        writeState({ autoplay: !state.autoplay });
        updateControls();
    });

    loopButton.addEventListener("click", () => {
        writeState({ loop: !state.loop });
        audio.loop = state.loop;
        updateControls();
    });

    shuffleButton.addEventListener("click", () => {
        writeState({
            shuffle: !state.shuffle,
            history: state.shuffle ? [] : [state.currentIndex]
        });
        updateControls();
    });

    slider.addEventListener("input", () => {
        const value = Number(slider.value);
        if (Number.isFinite(value)) {
            audio.currentTime = value;
            writeState({ currentTime: value });
            updateProgress();
        }
    });

    audio.addEventListener("play", () => {
        intentionalPause = false;
        interruptedPlayback = false;
        writeState({ playing: true, currentTime: audio.currentTime });
        if ("mediaSession" in navigator) {
            navigator.mediaSession.playbackState = "playing";
        }
        updateControls();
    });

    audio.addEventListener("pause", () => {
        updateProgress();

        if (!intentionalPause && audio.src && audio.currentTime > 0 &&
            (!Number.isFinite(audio.duration) || audio.currentTime < audio.duration - 0.25)) {
            interruptedPlayback = true;
        }

        writeState({
            playing: interruptedPlayback,
            currentTime: Number.isFinite(audio.currentTime) ? audio.currentTime : 0
        });
        if ("mediaSession" in navigator) {
            navigator.mediaSession.playbackState = "paused";
        }

        intentionalPause = false;
        updateControls();
    });

    audio.addEventListener("timeupdate", () => {
        updateProgress();
        updateStateFromAudio();
    });

    audio.addEventListener("loadedmetadata", updateProgress);

    audio.addEventListener("ended", () => {
        if (state.loop) {
            return;
        }

        if (state.autoplay) {
            nextTrack();
        } else {
            writeState({ playing: false, currentTime: audio.duration || 0 });
            updateControls();
        }
    });

    if ("mediaSession" in navigator) {
        try {
            navigator.mediaSession.setActionHandler("play", () => {
                intentionalPause = false;
                audio.play().catch(() => {});
            });
        } catch (_) {}

        try {
            navigator.mediaSession.setActionHandler("pause", () => {
                intentionalPause = true;
                interruptedPlayback = false;
                audio.pause();
            });
        } catch (_) {}

        try {
            navigator.mediaSession.setActionHandler("nexttrack", nextTrack);
        } catch (_) {}

        try {
            navigator.mediaSession.setActionHandler("previoustrack", previousTrack);
        } catch (_) {}
    }

    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
            attemptResume();
        }
    });

    window.addEventListener("focus", attemptResume);
    window.addEventListener("pageshow", attemptResume);

    function navigate(url, replace) {
        fetch(url, { headers: { "X-Requested-With": "SPA-Navigation" } })
            .then(response => {
                if (!response.ok) {
                    throw new Error("Navigation failed");
                }
                return response.text();
            })
            .then(html => {
                const parser = new DOMParser();
                const documentFromResponse = parser.parseFromString(html, "text/html");
                const nextMain = documentFromResponse.querySelector("main");
                const currentMain = document.querySelector("main");

                if (!nextMain || !currentMain) {
                    window.location.href = url;
                    return;
                }

                // Capture the nonce from the currently loaded document BEFORE
                // replacing <main>. The fetched page has a different nonce, and
                // using that new nonce would cause Firefox CSP to reject the
                // dynamically executed inline scripts.
                const activeNonceScript = document.querySelector("script[nonce]");
                const activeNonce = activeNonceScript
                    ? activeNonceScript.getAttribute("nonce")
                    : null;

                currentMain.replaceWith(nextMain);
                document.title = documentFromResponse.title;

                for (const oldScript of Array.from(nextMain.querySelectorAll("script"))) {
                    const replacement = document.createElement("script");
                    for (const attribute of oldScript.attributes) {
                        replacement.setAttribute(attribute.name, attribute.value);
                    }
                    if (!replacement.src) {
                        if (activeNonce) {
                            replacement.setAttribute("nonce", activeNonce);
                        } else {
                            replacement.removeAttribute("nonce");
                        }
                    }
                    replacement.textContent = oldScript.textContent
                        ? `(function () {\n${oldScript.textContent}\n}());`
                        : "";
                    oldScript.replaceWith(replacement);
                }

                if (replace) {
                    history.replaceState({}, "", url);
                } else {
                    history.pushState({}, "", url);
                }

                window.scrollTo(0, 0);
                showCurrentTrack();
            })
            .catch(() => {
                window.location.href = url;
            });
    }

    document.addEventListener("click", event => {
        const link = event.target.closest("a[href]");
        if (!link || link.target || link.hasAttribute("download") || event.defaultPrevented) {
            return;
        }

        const url = new URL(link.href, window.location.href);
        if (url.origin !== window.location.origin || url.hash || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }

        if (url.pathname === "/login") {
            return;
        }

        event.preventDefault();
        navigate(url.href, false);
    });

    window.addEventListener("popstate", () => {
        navigate(window.location.href, true);
    });

    window.PersistentPlayer = {
        setQueue,
        playIndex,
        nextTrack,
        previousTrack,
        getState: () => readState()
    };

    if (Array.isArray(state.queue) && state.queue.length) {
        const current = state.queue[state.currentIndex] || {
            title: state.title,
            url: state.url,
            path: state.path,
            drive_index: state.drive_index
        };

        if (current && current.url) {
            syncCurrentTrackState();
            showCurrentTrack();
            loadTrack(current, Boolean(state.playing), true);
        }
    }

    updateControls();
    updateProgress();
}());
