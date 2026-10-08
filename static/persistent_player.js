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
    const shufflePlaylistButton = document.getElementById("miniShufflePlaylist");
    const shuffleMenu = document.getElementById("miniShuffleMenu");
    const shufflePlaylistOptions = document.getElementById("miniShufflePlaylistOptions");
    const shufflePlaylistSelectAll = document.getElementById("miniShufflePlaylistSelectAll");
    const shufflePlaylistClear = document.getElementById("miniShufflePlaylistClear");
    const shufflePlaylistApply = document.getElementById("miniShufflePlaylistApply");
    const shuffleStatus = document.getElementById("miniShuffleStatus");
    const volumeButton = document.getElementById("miniVolume");
    const volumeMenu = document.getElementById("miniVolumeMenu");
    const volumeSlider = document.getElementById("miniVolumeSlider");
    const volumeValue = document.getElementById("miniVolumeValue");
    const volumeReset = document.getElementById("miniVolumeReset");
    const slider = document.getElementById("miniProgress");
    const bufferedBar = document.getElementById("miniBuffered");
    const currentTimeLabel = document.getElementById("miniCurrentTime");
    const durationLabel = document.getElementById("miniDuration");
    const audio = document.getElementById("persistentAudio");

    if (!root || !title || !previousButton || !playButton || !nextButton ||
        !autoplayButton || !loopButton || !shuffleButton || !shufflePlaylistButton ||
        !shuffleMenu || !shufflePlaylistOptions || !shufflePlaylistSelectAll || !shufflePlaylistClear ||
        !shufflePlaylistApply || !shuffleStatus ||
        !volumeButton || !volumeMenu || !volumeSlider || !volumeValue || !volumeReset || !slider ||
        !currentTimeLabel || !durationLabel || !bufferedBar || !audio) {
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
        shufflePlaylist: "",
        shufflePlaylists: [],
        history: [],
        volume: 1
    };

    if (!Object.prototype.hasOwnProperty.call(state, "shufflePlaylist")) {
        state.shufflePlaylist = "";
    }
    if (!Array.isArray(state.shufflePlaylists)) {
        state.shufflePlaylists = state.shufflePlaylist ? [state.shufflePlaylist] : [];
    }
    state.shufflePlaylists = state.shufflePlaylists.filter(name => typeof name === "string" && name.length > 0);
    if (!Number.isFinite(Number(state.volume))) {
        state.volume = 1;
    }
    state.volume = Math.max(0, Math.min(2, Number(state.volume)));

    let playlistNamesLoaded = false;
    let playlistNamesLoading = null;
    let intentionalPause = false;
    let switchingTrack = false;
    let interruptedPlayback = false;
    let resumeTimer = null;
    let loadedUrl = "";
    let audioContext = null;
    let mediaSourceNode = null;
    let gainNode = null;

    function ensureAudioGraph() {
        // Do not create a Web Audio graph for normal 0-100% playback.
        // iOS Safari handles a plain <audio> element much more reliably for
        // background/Lock Screen playback. The graph is only needed once the
        // user explicitly asks for amplification above 100%.
        if (Number(state.volume) <= 1 && !audioContext) {
            return false;
        }

        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
            return false;
        }

        try {
            if (!audioContext) {
                audioContext = new AudioContextClass();
                mediaSourceNode = audioContext.createMediaElementSource(audio);
                gainNode = audioContext.createGain();
                mediaSourceNode.connect(gainNode);
                gainNode.connect(audioContext.destination);
            }

            if (audioContext.state === "suspended") {
                audioContext.resume().catch(() => {});
            }

            applyGain();
            return true;
        } catch (_) {
            return false;
        }
    }

    function preparePlaybackAudio() {
        if (Number(state.volume) > 1) {
            ensureAudioGraph();
        } else if (!audioContext) {
            audio.volume = Math.max(0, Math.min(1, Number(state.volume)));
        } else {
            applyGain();
        }
    }

    function applyGain() {
        const volume = Math.max(0, Math.min(2, Number(state.volume)));
        if (gainNode) {
            gainNode.gain.value = volume;
        }
        // Keep the native element at 100% once Web Audio is active. The GainNode
        // handles both normal volume and the 100%-200% amplification range.
        audio.volume = 1;
    }

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

    function updateVolumeUi() {
        const volume = Math.max(0, Math.min(2, Number(state.volume)));
        volumeSlider.value = String(volume);
        volumeValue.textContent = `${Math.round(volume * 100)}%`;
        volumeSlider.setAttribute("aria-valuetext", `${Math.round(volume * 100)}%${volume > 1 ? " amplified" : ""}`);
        if (volume === 0) {
            volumeButton.textContent = "🔇";
            volumeButton.setAttribute("aria-label", "Volume muted");
        } else if (volume <= 0.5) {
            volumeButton.textContent = "🔉";
            volumeButton.setAttribute("aria-label", "Volume");
        } else if (volume <= 1) {
            volumeButton.textContent = "🔊";
            volumeButton.setAttribute("aria-label", "Volume");
        } else {
            volumeButton.textContent = "📢";
            volumeButton.setAttribute("aria-label", "Volume amplified");
        }
    }

    function setVolume(value) {
        const volume = Math.max(0, Math.min(2, Number(value)));
        writeState({ volume });
        if (volume > 1) {
            if (ensureAudioGraph()) {
                applyGain();
            } else {
                audio.volume = 1;
            }
        } else if (audioContext) {
            applyGain();
        } else {
            // Keep ordinary playback on the native media element. This is
            // important for reliable iOS Safari background playback.
            audio.volume = volume;
        }
        updateVolumeUi();
    }

    function closePopovers() {
        shuffleMenu.hidden = true;
        volumeMenu.hidden = true;
        shufflePlaylistButton.setAttribute("aria-expanded", "false");
        volumeButton.setAttribute("aria-expanded", "false");
    }

    function openShuffleMenu() {
        volumeMenu.hidden = true;
        volumeButton.setAttribute("aria-expanded", "false");
        shuffleMenu.hidden = false;
        shufflePlaylistButton.setAttribute("aria-expanded", "true");
        loadShufflePlaylists();
    }

    function openVolumeMenu() {
        shuffleMenu.hidden = true;
        shufflePlaylistButton.setAttribute("aria-expanded", "false");
        volumeMenu.hidden = false;
        volumeButton.setAttribute("aria-expanded", "true");
        volumeSlider.focus();
    }

    function getSelectedShufflePlaylists() {
        return Array.from(
            shufflePlaylistOptions.querySelectorAll('input[type="checkbox"][data-playlist]')
        )
            .filter(input => input.checked)
            .map(input => input.dataset.playlist);
    }

    function renderShufflePlaylistOptions(names) {
        shufflePlaylistOptions.replaceChildren();
        const selected = new Set(Array.isArray(state.shufflePlaylists) ? state.shufflePlaylists : []);

        for (const name of names) {
            const label = document.createElement("label");
            label.className = "mini-shuffle-option";

            const input = document.createElement("input");
            input.type = "checkbox";
            input.dataset.playlist = name;
            input.value = name;
            input.checked = selected.has(name);

            const text = document.createElement("span");
            text.textContent = name;

            label.appendChild(input);
            label.appendChild(text);
            shufflePlaylistOptions.appendChild(label);
        }

        updateShuffleSelectionStatus();
    }

    function updateShuffleSelectionStatus() {
        const selected = getSelectedShufflePlaylists();
        shuffleStatus.textContent = selected.length
            ? `${selected.length} playlist${selected.length === 1 ? "" : "s"} selected.`
            : "No playlists selected. Shuffle uses the current queue.";
    }

    async function loadShufflePlaylists() {
        if (playlistNamesLoaded) {
            updateShuffleSelectionStatus();
            return;
        }
        if (playlistNamesLoading) {
            return playlistNamesLoading;
        }

        shuffleStatus.textContent = "Loading playlists…";
        playlistNamesLoading = fetch("/api/playlists", {
            credentials: "same-origin",
            headers: { "Accept": "application/json" }
        })
            .then(response => {
                if (!response.ok) {
                    throw new Error("Could not load playlists");
                }
                return response.json();
            })
            .then(data => {
                const names = Array.isArray(data.playlists)
                    ? data.playlists.filter(name => typeof name === "string")
                    : [];
                renderShufflePlaylistOptions(names);
                playlistNamesLoaded = true;
            })
            .catch(() => {
                shuffleStatus.textContent = "Could not load playlists.";
            })
            .finally(() => {
                playlistNamesLoading = null;
            });

        return playlistNamesLoading;
    }

    async function applyShufflePlaylist() {
        const selected = getSelectedShufflePlaylists();
        if (!selected.length) {
            writeState({ shufflePlaylists: [], shufflePlaylist: "", shuffle: false });
            shuffleStatus.textContent = "Using the current queue.";
            updateControls();
            closePopovers();
            return;
        }

        shuffleStatus.textContent = `Loading ${selected.length} playlist${selected.length === 1 ? "" : "s"}…`;
        shufflePlaylistApply.disabled = true;

        try {
            const results = await Promise.all(selected.map(async name => {
                const response = await fetch(`/api/library/${encodeURIComponent(name)}`, {
                    credentials: "same-origin",
                    headers: { "Accept": "application/json" }
                });
                if (!response.ok) {
                    throw new Error(`Playlist load failed: ${name}`);
                }
                const data = await response.json();
                return { name, songs: Array.isArray(data.songs) ? data.songs : [] };
            }));

            const combined = [];
            const seen = new Set();
            for (const result of results) {
                for (const song of result.songs) {
                    if (!song || !song.url) {
                        continue;
                    }
                    const key = `${song.drive_index ?? ""}|${song.path || song.url}`;
                    if (seen.has(key)) {
                        continue;
                    }
                    seen.add(key);
                    combined.push(song);
                }
            }

            if (!combined.length) {
                shuffleStatus.textContent = "The selected playlists have no playable songs.";
                return;
            }

            const randomIndex = Math.floor(Math.random() * combined.length);
            const label = selected.length === 1
                ? selected[0]
                : `${selected.length} playlists`;

            setQueue(combined, randomIndex, label, true);
            writeState({
                shufflePlaylists: selected,
                shufflePlaylist: label,
                shuffle: true,
                history: [randomIndex]
            });
            updateControls();
            closePopovers();
        } catch (_) {
            shuffleStatus.textContent = "Could not load one or more playlists.";
        } finally {
            shufflePlaylistApply.disabled = false;
        }
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
        const selectedShuffleCount = Array.isArray(state.shufflePlaylists) ? state.shufflePlaylists.length : 0;
        shufflePlaylistButton.classList.toggle("active", selectedShuffleCount > 0);
        shufflePlaylistButton.setAttribute("aria-label", selectedShuffleCount > 0
            ? `Choose shuffle playlists: ${selectedShuffleCount} selected`
            : "Choose shuffle playlists");

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

        // Show how much of the current track the browser has buffered.
        // Media buffers are exposed as time ranges, so use the range that
        // contains the current playback position when possible.
        let bufferedEnd = 0;
        if (duration > 0 && audio.buffered.length > 0) {
            for (let index = 0; index < audio.buffered.length; index += 1) {
                try {
                    const start = audio.buffered.start(index);
                    const end = audio.buffered.end(index);
                    if (current >= start - 0.25 && current <= end + 0.25) {
                        bufferedEnd = end;
                        break;
                    }
                    bufferedEnd = Math.max(bufferedEnd, end);
                } catch (_) {}
            }
        }

        const bufferedPercent = duration > 0
            ? Math.max(0, Math.min(100, (bufferedEnd / duration) * 100))
            : 0;
        bufferedBar.style.width = `${bufferedPercent}%`;
        bufferedBar.setAttribute("aria-valuenow", String(Math.round(bufferedPercent)));
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

    function releaseMediaResource() {
        // Clear the media element's resource selection. This does not control
        // Safari's global HTTP cache, but it releases this page's active media
        // resource/buffer so an old song is no longer attached to the player.
        try {
            audio.pause();
            audio.removeAttribute("src");
            audio.preload = "none";
            audio.load();
        } catch (_) {}
        loadedUrl = "";
        updateProgress();
    }

    function loadTrack(track, shouldPlay, restorePosition) {
        if (!track || !track.url) {
            return;
        }

        root.hidden = false;
        title.textContent = "Now playing: " + (track.title || "Unknown song");

        // HTMLMediaElement.src returns an absolute URL, while Flask url_for()
        // commonly supplies a relative URL. Normalize before comparing so we do
        // not unnecessarily reload the same MP3 every time it is selected.
        const sourceUrl = new URL(track.url, window.location.href).href;
        const sourceChanged = loadedUrl !== sourceUrl;
        const startTime = restorePosition && Number.isFinite(state.currentTime)
            ? Math.max(0, state.currentTime)
            : 0;

        if ("mediaSession" in navigator && "MediaMetadata" in window) {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: track.title || "Unknown song",
                artist: "Local Music Library",
                album: state.playlist || "Music Library"
            });
        }

        function restoreAndPlay() {
            // A previous track may have had a pending metadata callback when
            // the user skipped quickly. Ignore callbacks for any older source.
            if (loadedUrl !== sourceUrl) {
                return;
            }

            if (startTime > 0 && Number.isFinite(audio.duration)) {
                try {
                    audio.currentTime = Math.min(startTime, audio.duration);
                } catch (_) {}
            }

            updateProgress();

            if (shouldPlay) {
                preparePlaybackAudio();
                audio.play().catch(() => {
                    interruptedPlayback = true;
                    writeState({ playing: true });
                });
            }
        }

        if (sourceChanged) {
            // Explicitly release the previous media resource before attaching a
            // new track. This is particularly helpful for Safari on iOS, which
            // can otherwise retain old media buffers while a long queue plays.
            if (!audio.paused) {
                intentionalPause = true;
                switchingTrack = true;
                audio.pause();
            }
            releaseMediaResource();

            loadedUrl = sourceUrl;
            audio.preload = "metadata";
            audio.addEventListener("loadedmetadata", restoreAndPlay, { once: true });
            audio.src = sourceUrl;
            audio.load();
        } else if (audio.readyState >= HTMLMediaElement.HAVE_METADATA) {
            restoreAndPlay();
        } else {
            // Avoid stacking one-shot listeners if metadata is not ready yet.
            audio.addEventListener("loadedmetadata", restoreAndPlay, { once: true });
        }

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
            shufflePlaylist: "",
            shufflePlaylists: [],
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
        const trackUrl = track.url ? new URL(track.url, window.location.href).href : "";
        const wasDifferent = safeIndex !== state.currentIndex || loadedUrl !== trackUrl;

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

            preparePlaybackAudio();
            audio.play().catch(() => {});
        }, 300);
    }

    shufflePlaylistButton.addEventListener("click", () => {
        if (shuffleMenu.hidden) {
            openShuffleMenu();
        } else {
            closePopovers();
        }
    });

    shufflePlaylistApply.addEventListener("click", applyShufflePlaylist);

    shufflePlaylistSelectAll.addEventListener("click", () => {
        shufflePlaylistOptions.querySelectorAll('input[type="checkbox"][data-playlist]').forEach(input => {
            input.checked = true;
        });
        updateShuffleSelectionStatus();
    });

    shufflePlaylistClear.addEventListener("click", () => {
        shufflePlaylistOptions.querySelectorAll('input[type="checkbox"][data-playlist]').forEach(input => {
            input.checked = false;
        });
        updateShuffleSelectionStatus();
    });

    shufflePlaylistOptions.addEventListener("change", event => {
        if (event.target.matches('input[type="checkbox"][data-playlist]')) {
            updateShuffleSelectionStatus();
        }
    });

    volumeButton.addEventListener("click", () => {
        if (volumeMenu.hidden) {
            openVolumeMenu();
        } else {
            closePopovers();
        }
    });

    volumeSlider.addEventListener("input", () => {
        setVolume(volumeSlider.value);
    });

    volumeReset.addEventListener("click", () => {
        setVolume(1);
        volumeSlider.focus();
    });

    document.addEventListener("click", event => {
        if (!event.target.closest(".mini-popover-wrap")) {
            closePopovers();
        }
    });

    document.addEventListener("keydown", event => {
        if (event.key === "Escape") {
            closePopovers();
        }
    });

    previousButton.addEventListener("click", previousTrack);
    nextButton.addEventListener("click", nextTrack);

    playButton.addEventListener("click", () => {
        if (audio.paused) {
            intentionalPause = false;
            interruptedPlayback = false;
            preparePlaybackAudio();
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

        if (switchingTrack) {
            switchingTrack = false;
            intentionalPause = false;
            updateControls();
            return;
        }

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
    audio.addEventListener("progress", updateProgress);
    audio.addEventListener("canplay", updateProgress);
    audio.addEventListener("durationchange", updateProgress);

    audio.addEventListener("ended", () => {
        if (state.loop) {
            return;
        }

        if (state.autoplay) {
            nextTrack();
        } else {
            writeState({ playing: false, currentTime: audio.duration || 0 });
            updateControls();
            // No next track is being loaded, so release the completed media
            // resource instead of keeping its buffer attached indefinitely.
            releaseMediaResource();
        }
    });

    if ("mediaSession" in navigator) {
        try {
            navigator.mediaSession.setActionHandler("play", () => {
                intentionalPause = false;
                ensureAudioGraph();
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
                // Browsers intentionally hide nonce values from getAttribute("nonce").
                // Use the DOM nonce property so dynamically created scripts receive
                // the same nonce authorized by the current response CSP.
                const activeNonce = activeNonceScript
                    ? activeNonceScript.nonce
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

    // Leave the audio element on native HTMLMediaElement playback until the
    // user explicitly requests >100% amplification. Creating AudioContext at
    // page load can interfere with iOS Safari background playback.
    if (Number(state.volume) <= 1) {
        audio.volume = Math.max(0, Math.min(1, Number(state.volume)));
    }
    updateVolumeUi();
    updateControls();
    updateProgress();
}());
