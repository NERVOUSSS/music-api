(() => {
  const $ = (s) => document.querySelector(s);
  const state = {
    source: "netease",
    strategy: localStorage.getItem("sound-island-playback-source") || "auto",
    quality: localStorage.getItem("sound-island-player-quality") || "320k",
    results: [],
    queue: [],
    current: null,
    liked: read("sound-island-liked"),
    playlists: [],
    activePlaylist: null,
    playlistSearchPlaylistId: null,
    playlistSearchQuery: "",
    key: localStorage.getItem("sound-island-key") || "",
    theme: localStorage.getItem("sound-island-theme") || "purple",
    user: null,
    lyricLines: [],
    activeLyricIndex: -1,
    resolvedBy: "",
    resolvedKind: "",
    resolvedSong: null,
    playbackRetrySources: new Set(),
    recoveryInProgress: false,
    playbackAttempt: 0,
    resolveController: null,
    lyricController: null,
    nextResolveController: null,
    nextPlayback: null,
    audioAttempt: 0,
    audioUrl: "",
    repeat: localStorage.getItem("sound-island-repeat") || "all",
    shuffle: localStorage.getItem("sound-island-shuffle") === "true",
    connectedAccounts: [],
    failedQueueIds: new Set(),
    skippingUnavailable: false,
    keepAlive: false,
    keepAliveTimer: null,
    switchingPlayback: false,
    handoffWatchdog: null,
    prefetchRefreshed: false,
    pendingResume: false,
    accountQrTimer: null,
    accountQrPolling: false,
    accountQrPlatform: "netease",
    dailyBatch: 0,
    dailySongs: [],
    dailyLoaded: false,
    dailyPlaylistBatch: 0,
    dailyPlaylists: [],
    dailyPlaylistsLoaded: false,
    activeDailyPlaylist: null,
    dailyPlaylistTracks: [],
    presenceTimer: null,
  };
  const audioA = $("#audio");
  const audioB = $("#audio-b");
  audioA.preload = "auto";
  audioA.setAttribute("playsinline", "");
  audioA.volume = 0.8;
  audioB.preload = "auto";
  audioB.setAttribute("playsinline", "");
  audioB.volume = 0.8;
  let audio = audioA;
  // 网页内悬浮歌词：复用当前播放器的歌词状态，不创建 audio，也不介入回源链路。
  const inlineLyricsSettingsKey = "sound-island-inline-lyrics-settings";
  const inlineLyricsPositionKey = "sound-island-inline-lyrics-position";
  const inlineLyricsDefaults = { offset: 0, font: "system", fontSize: 28, normalColor: "", activeColor: "", locked: false };
  let inlineLyricsSettings = readInlineLyricsSettings();
  let inlineLyricsPosition = readInlineLyricsPosition();
  let inlineLyricsDrag = null;
  function getIdleAudio() { return audio === audioA ? audioB : audioA; }
  function switchAudio() { audio = getIdleAudio(); return audio; }
  applyTheme(state.theme);

  function read(key) { try { return JSON.parse(localStorage.getItem(key) || "[]"); } catch { return []; } }
  function save(key, value) { localStorage.setItem(key, JSON.stringify(value)); }
  function applyTheme(theme) {
    state.theme = theme === "sage" ? "sage" : "purple";
    document.body.dataset.theme = state.theme;
    const select = $("#theme-select");
    if (select) select.value = state.theme;
    if ($("#hero-art")) art();
    // 页面内悬浮歌词继承当前主题的默认颜色；用户手动指定颜色时保持不变。
    applyInlineLyricsSettings();
    renderInlineLyrics();
  }

  function esc(text) { return String(text || "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" })[c]); }
  function label(source) { return ({ netease: "\u7f51\u6613\u4e91\u97f3\u4e50", qq: "QQ\u97f3\u4e50", kugou: "\u9177\u72d7\u97f3\u4e50", kugou_concept: "\u9177\u72d7-\u6982\u5ff5", migu: "\u54aa\u5495\u97f3\u4e50", qishui: "\u6c7d\u6c34\u97f3\u4e50", third: "\u81ea\u5b9a\u4e49\u97f3\u6e90", aggregate: "\u805a\u5408\u641c\u7d22" })[source] || source; }
  function id(song) { return `${song.source || "netease"}:${song.id}`; }
  function defaultLiked() { return state.playlists.find((item) => item.id === "liked"); }
  function isLiked(song) {
    const liked = defaultLiked();
    return (liked ? liked.songs : state.liked).some((item) => id(item) === id(song));
  }
  function time(ms) { const s = Math.floor((ms || 0) / 1000); return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "--:--"; }
  function audioTime(s) { s = Math.floor(s || 0); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
  function query(data) { return new URLSearchParams(data).toString() + (state.key ? `&key=${encodeURIComponent(state.key)}` : ""); }
  function cover(song, cls = "row-cover") { return `<div class="${cls}">${song.cover ? `<img src="${esc(song.cover)}" alt="">` : "S"}</div>`; }
  function playerIcon(playing, loading = false) {
    if (loading) return `<span class="control-symbol" aria-hidden="true">...</span><span class="sr-only">加载中</span>`;
    return playing
      ? `<span class="control-symbol" aria-hidden="true">Ⅱ</span><span class="sr-only">暂停</span>`
      : `<span class="control-symbol" aria-hidden="true">▶</span><span class="sr-only">播放</span>`;
  }
  function sourceText(song = state.current) {
    if (state.resolvedKind === "cross_platform" && state.resolvedBy) return `官方补源 · ${label(state.resolvedBy)}`;
    if (state.resolvedBy) return `回源 · ${label(state.resolvedBy)}`;
    return label(song?.source || state.source);
  }
  function parseLyrics(text) {
    const lines = [];
    String(text || "").replace(/\r/g, "").split("\n").forEach((raw) => {
      const matches = [...raw.matchAll(/\[(\d+):(\d+(?:\.\d+)?)\]/g)];
      const content = raw.replace(/\[(\d+):(\d+(?:\.\d+)?)\]/g, "").trim();
      if (matches.length) {
        matches.forEach((match) => lines.push({ time: Number(match[1]) * 60 + Number(match[2]), text: content || "\u00a0" }));
      } else if (content) {
        lines.push({ time: null, text: content });
      }
    });
    return lines.sort((a, b) => (a.time ?? Infinity) - (b.time ?? Infinity));
  }
  function readInlineLyricsSettings() {
    try { return { ...inlineLyricsDefaults, ...JSON.parse(localStorage.getItem(inlineLyricsSettingsKey) || "{}") }; }
    catch (_) { return { ...inlineLyricsDefaults }; }
  }
  function saveInlineLyricsSettings() { localStorage.setItem(inlineLyricsSettingsKey, JSON.stringify(inlineLyricsSettings)); }
  function readInlineLyricsPosition() {
    try {
      const value = JSON.parse(localStorage.getItem(inlineLyricsPositionKey) || "null");
      return Number.isFinite(value?.x) && Number.isFinite(value?.y) ? value : null;
    } catch (_) { return null; }
  }
  function inlineLyricsFont(font) {
    return ({ system: 'system-ui,-apple-system,"Microsoft YaHei",sans-serif', "microsoft-yahei": '"Microsoft YaHei",sans-serif', pingfang: '"PingFang SC","Microsoft YaHei",sans-serif', serif: '"Noto Serif SC",STSong,serif', mono: 'ui-monospace,Consolas,monospace' })[font] || 'system-ui,-apple-system,"Microsoft YaHei",sans-serif';
  }
  function inlineLyricsThemeColor(kind) {
    if (kind === "normal") return state.theme === "sage" ? "#447d6e" : "#c9bde6";
    return state.theme === "sage" ? "#0b6352" : "#fffaff";
  }
  function applyInlineLyricsSettings() {
    const overlay = $("#inline-floating-lyrics");
    if (!overlay) return;
    overlay.style.setProperty("--inline-lyrics-font", inlineLyricsFont(inlineLyricsSettings.font));
    overlay.style.setProperty("--inline-lyrics-size", `${inlineLyricsSettings.fontSize}px`);
    overlay.style.setProperty("--inline-lyrics-normal", inlineLyricsSettings.normalColor || inlineLyricsThemeColor("normal"));
    overlay.style.setProperty("--inline-lyrics-active", inlineLyricsSettings.activeColor || inlineLyricsThemeColor("active"));
    overlay.classList.toggle("is-locked", Boolean(inlineLyricsSettings.locked));
    const lock = $("#inline-lyrics-lock");
    lock.setAttribute("aria-pressed", String(Boolean(inlineLyricsSettings.locked)));
    lock.textContent = inlineLyricsSettings.locked ? "🔒" : "🔓";
    lock.title = inlineLyricsSettings.locked ? "解锁歌词位置" : "锁定歌词位置";
    $("#inline-lyrics-offset").textContent = `${inlineLyricsSettings.offset > 0 ? "+" : ""}${Number(inlineLyricsSettings.offset || 0).toFixed(1)} 秒`;
    applyInlineLyricsPosition();
  }
  function defaultInlineLyricsPosition() {
    return { x: Math.round(window.innerWidth / 2), y: Math.round(Math.max(110, Math.min(window.innerHeight - 150, window.innerHeight * .4))) };
  }
  function clampInlineLyricsPosition(position) {
    return {
      x: Math.round(Math.min(Math.max(Number(position?.x) || 0, 80), Math.max(80, window.innerWidth - 80))),
      y: Math.round(Math.min(Math.max(Number(position?.y) || 0, 52), Math.max(70, window.innerHeight - 116)))
    };
  }
  function applyInlineLyricsPosition() {
    const overlay = $("#inline-floating-lyrics");
    if (!overlay) return;
    const position = clampInlineLyricsPosition(inlineLyricsPosition || defaultInlineLyricsPosition());
    overlay.style.left = `${position.x}px`;
    overlay.style.top = `${position.y}px`;
  }
  function inlineLyricIndex() {
    const effectiveTime = Math.max(0, Number(audio.currentTime || 0) + Number(inlineLyricsSettings.offset || 0));
    let index = -1;
    state.lyricLines.forEach((line, lineIndex) => { if (Number.isFinite(line.time) && line.time <= effectiveTime) index = lineIndex; });
    return index;
  }
  function renderInlineLyrics() {
    const current = $("#inline-lyrics-current");
    const next = $("#inline-lyrics-next");
    const meta = $("#inline-lyrics-meta");
    if (!current || !next || !meta) return;
    meta.textContent = state.current ? `${state.current.title || "未命名歌曲"} · ${state.current.artist || "未知歌手"}` : "等待播放歌曲";
    if (!state.current) {
      current.textContent = "从播放栏点击“歌词”打开悬浮歌词";
      current.classList.add("is-empty");
      next.textContent = "";
      return;
    }
    if (!state.lyricLines.length) {
      current.textContent = "这首歌曲暂时没有可用歌词";
      current.classList.add("is-empty");
      next.textContent = "";
      return;
    }
    const active = inlineLyricIndex();
    const index = active >= 0 ? active : 0;
    current.textContent = state.lyricLines[index]?.text || "　";
    current.classList.toggle("is-empty", active < 0);
    next.textContent = state.lyricLines.slice(index + 1).find((line) => String(line.text || "").trim())?.text || "";
  }
  function openInlineLyrics() {
    const overlay = $("#inline-floating-lyrics");
    overlay.hidden = false;
    applyInlineLyricsSettings();
    renderInlineLyrics();
  }
  function closeInlineLyrics() { $("#inline-floating-lyrics").hidden = true; }
  function updateInlineLyricsOffset(delta) {
    inlineLyricsSettings.offset = Math.max(-10, Math.min(10, Math.round((Number(inlineLyricsSettings.offset || 0) + delta) * 10) / 10));
    saveInlineLyricsSettings();
    applyInlineLyricsSettings();
    renderInlineLyrics();
  }
  function fillInlineLyricsSettingsForm() {
    $("#inline-setting-offset").value = inlineLyricsSettings.offset;
    $("#inline-setting-font").value = inlineLyricsSettings.font;
    $("#inline-setting-font-size").value = inlineLyricsSettings.fontSize;
    $("#inline-setting-font-size-output").textContent = `${inlineLyricsSettings.fontSize}px`;
    $("#inline-setting-normal-color").value = inlineLyricsSettings.normalColor || inlineLyricsThemeColor("normal");
    $("#inline-setting-active-color").value = inlineLyricsSettings.activeColor || inlineLyricsThemeColor("active");
  }
  function saveInlineLyricsSettingsForm() {
    inlineLyricsSettings.offset = Math.max(-10, Math.min(10, Number($("#inline-setting-offset").value) || 0));
    inlineLyricsSettings.font = $("#inline-setting-font").value;
    inlineLyricsSettings.fontSize = Number($("#inline-setting-font-size").value) || inlineLyricsDefaults.fontSize;
    inlineLyricsSettings.normalColor = $("#inline-setting-normal-color").value;
    inlineLyricsSettings.activeColor = $("#inline-setting-active-color").value;
    saveInlineLyricsSettings();
    applyInlineLyricsSettings();
    renderInlineLyrics();
  }
  function renderLyrics() {
    const target = $("#now-lyrics");
    const status = $("#lyrics-status");
    state.activeLyricIndex = -1;
    if (!state.lyricLines.length) {
      target.innerHTML = `<p class="lyrics-empty">暂时没有可用歌词</p>`;
      status.textContent = "暂无歌词";
      renderInlineLyrics();
        return;
    }
    target.innerHTML = state.lyricLines.map((line, index) =>
      `<p data-lyric-index="${index}">${esc(line.text)}</p>`).join("");
    status.textContent = `${state.lyricLines.length} 行歌词`;
    syncActiveLyric();
    renderInlineLyrics();
  }
  function syncActiveLyric() {
    const timed = state.lyricLines.some((line) => line.time !== null);
    if (!timed) return;
    let index = -1;
    state.lyricLines.forEach((line, lineIndex) => {
      if (line.time !== null && line.time <= audio.currentTime) index = lineIndex;
    });
    if (index === state.activeLyricIndex) return;
    state.activeLyricIndex = index;
    $("#now-lyrics").querySelectorAll("[data-lyric-index]").forEach((line) => {
      line.classList.toggle("active", Number(line.dataset.lyricIndex) === index);
    });
    const lyricsList = $("#now-lyrics");
    const active = lyricsList.querySelector(`[data-lyric-index="${index}"]`);
    if (active && !$("#now-playing-view").hidden) {
      // `scrollIntoView()` also scrolls outer ancestors (including the whole
      // music page). Calculate within the dedicated lyrics scroller instead,
      // so lyric following never pulls the immersive view back to page top.
      const listRect = lyricsList.getBoundingClientRect();
      const activeRect = active.getBoundingClientRect();
      const targetTop = lyricsList.scrollTop + activeRect.top - listRect.top
        - (lyricsList.clientHeight - activeRect.height) / 2;
      lyricsList.scrollTo({ top: Math.max(0, targetTop), behavior: "smooth" });
    }
  }
  function syncLikeUi(song = state.current) {
    const liked = Boolean(song && isLiked(song));
    const compact = $("#like-button");
    compact.classList.toggle("liked", liked);
    compact.title = liked ? "取消喜欢" : "喜欢";
    compact.innerHTML = `<span aria-hidden="true">${liked ? "♥" : "♡"}</span><span class="sr-only">${liked ? "取消喜欢" : "喜欢"}</span>`;
    const nowLike = $("#now-like");
    nowLike.classList.toggle("liked", liked);
    nowLike.innerHTML = `<span aria-hidden="true">${liked ? "♥" : "♡"}</span><span>${liked ? "已喜欢" : "喜欢"}</span>`;
  }
  function syncNowPlaying(song = state.current) {
    const hasSong = Boolean(song);
    $("#now-title").textContent = song?.title || "还没有正在播放的歌曲";
    $("#now-artist").textContent = song?.artist || (song ? "未知歌手" : "从搜索结果中选择一首歌");
    $("#now-album").textContent = song?.album || "声屿音乐";
    $("#now-source").textContent = song ? sourceText(song) : "等待选择歌曲";
    $("#now-playing-cover").innerHTML = song?.cover ? `<img src="${esc(song.cover)}" alt="">` : "S";
    $("#now-playing-trigger").disabled = !hasSong;
    $("#like-button").disabled = !hasSong;
    $("#player-add").disabled = !hasSong;
    $("#now-like").disabled = !hasSong;
    $("#now-add").disabled = !hasSong;
    syncLikeUi(song);
  }
  function syncPlaybackUi(loading = false) {
    const hasSong = Boolean(state.current);
    const playing = hasSong && !audio.paused && !state.pendingResume;
    ["#play-button", "#now-play-button"].forEach((selector) => {
      const button = $(selector);
      button.disabled = !hasSong || loading;
      button.title = loading ? "加载中" : playing ? "暂停" : "播放";
      button.innerHTML = playerIcon(playing, loading);
    });
    ["#previous-button", "#next-button", "#now-previous-button", "#now-next-button"].forEach((selector) => {
      $(selector).disabled = !hasSong || loading;
    });
    syncPlaybackModes();
    $("#now-playing-view").classList.toggle("is-playing", playing);
  }
  function syncPlaybackModes() {
    ["#repeat-list-button", "#now-repeat-list-button"].forEach((selector) => {
      const button = $(selector);
      button.classList.toggle("active", state.repeat === "all");
      button.setAttribute("aria-pressed", state.repeat === "all" ? "true" : "false");
    });
    ["#repeat-one-button", "#now-repeat-one-button"].forEach((selector) => {
      const button = $(selector);
      button.classList.toggle("active", state.repeat === "one");
      button.setAttribute("aria-pressed", state.repeat === "one" ? "true" : "false");
    });
    ["#shuffle-button", "#now-shuffle-button"].forEach((selector) => {
      const button = $(selector);
      button.classList.toggle("active", state.shuffle);
      button.title = state.shuffle ? "关闭随机播放" : "随机播放";
      button.querySelector(".sr-only").textContent = button.title;
    });
  }
  function syncProgress() {
    if (state.keepAlive) return;
    const current = audioTime(audio.currentTime);
    const duration = audioTime(audio.duration);
    const value = audio.duration ? audio.currentTime / audio.duration * 100 : 0;
    $("#current-time").textContent = current;
    $("#duration-time").textContent = duration;
    $("#progress-input").value = value;
    $("#now-current-time").textContent = current;
    $("#now-duration-time").textContent = duration;
    $("#now-progress-input").value = value;
    $("#now-progress-input").style.setProperty("--progress-percent", `${value}%`);
    syncActiveLyric();
    // 独立的网页内歌词层不能依赖“正在播放”详情页是否打开，随音频进度单独刷新。
    renderInlineLyrics();
    syncMediaSessionPosition();
    maybeRefreshNextPlayback();
  }
  function openNowPlaying() {
    if (!state.current) return;
    $("#now-playing-view").hidden = false;
    document.body.classList.add("now-playing-open");
    syncNowPlaying();
    syncProgress();
  }
  function closeNowPlaying() {
    $("#now-playing-view").hidden = true;
    document.body.classList.remove("now-playing-open");
  }
  async function loadAnnouncement() {
    const bar = $("#announcement-bar");
    const track = $("#announcement-track");
    try {
      const response = await fetch("/api/site/announcement");
      const data = await response.json();
      const message = String(data?.announcement || "").trim();
      if (!response.ok || !message) {
        bar.hidden = true;
        return;
      }
      const copies = Math.max(4, Math.ceil(160 / Math.max(message.length, 1)));
      const count = copies % 2 === 0 ? copies : copies + 1;
      const items = Array.from({ length: count }, () => {
        const item = document.createElement("span");
        item.className = "announcement-message";
        item.textContent = message;
        return item;
      });
      track.replaceChildren(...items);
      track.style.setProperty("--announcement-duration", `${Math.max(18, Math.min(72, Math.ceil(message.length * 0.46)))}s`);
      bar.hidden = false;
    } catch (_) {
      bar.hidden = true;
    }
  }
  function playlistSongs() {
    const playlist = state.playlists.find((item) => item.id === state.activePlaylist);
    return playlist ? playlist.songs || [] : [];
  }
  function renderQueue() {
    const target = $("#queue-list");
    const queue = state.queue || [];
    $("#queue-count").textContent = `${queue.length} 首歌曲`;
    $("#queue-source").textContent = state.current ? `当前来源 · ${sourceText(state.current)}` : "等待播放";
    target.innerHTML = queue.length ? queue.map((song, index) => `<div class="queue-item ${state.current && id(song) === id(state.current) ? "active" : ""}" data-queue-id="${esc(id(song))}">
      <button class="queue-item-main" type="button" title="播放 ${esc(song.title)}">
        ${cover(song, "row-cover queue-cover")}<span class="queue-item-copy"><strong>${esc(song.title)}</strong><small>${esc(song.artist || "未知歌手")}</small></span><span class="queue-item-index">${index + 1}</span>
      </button>
      <button class="queue-item-remove" type="button" title="从播放列表移除" aria-label="移除 ${esc(song.title)}">×</button>
    </div>`).join("") : `<p class="queue-empty">当前播放列表为空</p>`;
    target.querySelectorAll(".queue-item-main").forEach((button) => button.addEventListener("click", () => {
      const song = queue.find((item) => id(item) === button.closest("[data-queue-id]").dataset.queueId);
      if (song) play(song, state.queue);
    }));
    target.querySelectorAll(".queue-item-remove").forEach((button) => button.addEventListener("click", (event) => {
      event.stopPropagation();
      removeFromQueue(button.closest("[data-queue-id]").dataset.queueId);
    }));
  }
  function removeFromQueue(songId) {
    const queue = state.queue || [];
    const index = queue.findIndex((song) => id(song) === songId);
    if (index < 0) return;
    const removed = queue[index];
    const removingCurrent = state.current && id(state.current) === songId;
    state.queue = queue.filter((_, itemIndex) => itemIndex !== index);
    state.failedQueueIds.delete(songId);
    if (!removingCurrent) {
      renderQueue();
      return;
    }
    if (!state.queue.length) {
      state.current = null;
      state.playbackAttempt += 1;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      setPlayer({ title: "还没有正在播放的歌曲", artist: "", cover: "" });
      renderQueue();
      return;
    }
    const next = state.shuffle
      ? state.queue[Math.floor(Math.random() * state.queue.length)]
      : state.queue[index % state.queue.length];
    renderQueue();
    play(next, state.queue, true);
  }
  function syncQueueButtons(open) {
    ["#queue-button", "#now-queue-button"].forEach((selector) => {
      const button = $(selector);
      const label = open ? "最小化播放列表" : "打开播放列表";
      button.title = label;
      button.querySelector(".sr-only").textContent = label;
    });
  }
  function openQueue() {
    renderQueue();
    $("#queue-panel").classList.remove("is-minimized");
    $("#queue-panel").hidden = false;
    document.body.classList.add("queue-open");
    syncQueueButtons(true);
  }
  function minimizeQueue() {
    const panel = $("#queue-panel");
    panel.classList.add("is-minimized");
    panel.hidden = true;
    document.body.classList.remove("queue-open");
    syncQueueButtons(false);
  }
  function toggleQueue() {
    const panel = $("#queue-panel");
    if (panel.hidden || panel.classList.contains("is-minimized")) openQueue();
    else minimizeQueue();
  }
  function clearQueue() {
    state.queue = state.current ? [state.current] : [];
    renderQueue();
  }

  function counts() {
    $("#liked-count").textContent = (defaultLiked()?.songs || state.liked).length;
  }
  function render(songs, target, emptyMessage = "这个歌单还没有歌曲。") {
    target.innerHTML = songs.length ? songs.map((song, index) => `<div class="track-row song" data-id="${esc(id(song))}">
      <span class="index">${index + 1}</span><div class="song-info">${cover(song)}<div class="song-copy"><span class="song-title">${esc(song.title)}</span><span class="song-artist">${esc(song.artist || "未知歌手")}</span></div></div>
      <span class="album-name">${esc(song.album || "--")}</span><span class="duration">${time(song.duration)}</span>
      <div class="row-actions"><button data-action="like">${isLiked(song) ? "已喜欢" : "喜欢"}</button><button data-action="add">加入</button><button data-action="play">播放</button></div></div>`).join("") : `<div class="state-panel">${esc(emptyMessage)}</div>`;
    target.querySelectorAll(".song").forEach((row) => {
      const song = songs.find((item) => id(item) === row.dataset.id);
      row.addEventListener("dblclick", () => play(song, songs));
      row.querySelector('[data-action="play"]').addEventListener("click", () => play(song, songs));
      row.querySelector('[data-action="like"]').addEventListener("click", () => like(song));
      row.querySelector('[data-action="add"]').addEventListener("click", () => openAddDialog(song));
    });
  }
  function resetPlaylistSearch() {
    state.playlistSearchPlaylistId = null;
    state.playlistSearchQuery = "";
    const input = $("#playlist-search-input");
    const panel = $("#playlist-search-panel");
    const toggle = $("#playlist-search-toggle");
    if (input) input.value = "";
    if (panel) panel.hidden = true;
    if (toggle) {
      toggle.classList.remove("active");
      toggle.setAttribute("aria-expanded", "false");
    }
  }
  function setPlaylistSearchOpen(open) {
    const panel = $("#playlist-search-panel");
    const toggle = $("#playlist-search-toggle");
    panel.hidden = !open;
    toggle.classList.toggle("active", open);
    toggle.setAttribute("aria-expanded", String(open));
    if (open) $("#playlist-search-input").focus();
  }
  function renderLibrary(filter = "liked") {
    const isPlaylist = filter === "playlist";
    const songs = isPlaylist ? playlistSongs() : (defaultLiked()?.songs || state.liked);
    if (isPlaylist && state.playlistSearchPlaylistId !== state.activePlaylist) {
      resetPlaylistSearch();
      state.playlistSearchPlaylistId = state.activePlaylist;
    }
    const queryText = isPlaylist ? state.playlistSearchQuery.trim() : "";
    const query = queryText.toLocaleLowerCase();
    const visibleSongs = query ? songs.filter((song) => `${song.title || ""}\n${song.artist || ""}`.toLocaleLowerCase().includes(query)) : songs;
    $("#library-title").textContent = isPlaylist ? (state.playlists.find((p) => p.id === state.activePlaylist)?.name || "我的歌单") : "我喜欢的音乐";
    $("#rename-playlist").hidden = !isPlaylist;
    $("#playlist-search-toggle").hidden = !isPlaylist;
    $("#delete-playlist").hidden = !isPlaylist;
    $("#playlist-search-result").textContent = queryText ? `找到 ${visibleSongs.length} 首，共 ${songs.length} 首` : `${songs.length} 首歌曲`;
    $("#playlist-search-clear").hidden = !queryText;
    render(visibleSongs, $("#library-list"), queryText ? `没有找到与“${queryText}”匹配的歌曲或歌手。` : "这个歌单还没有歌曲。");
  }

  function platformLabel(platform) { return ({ netease: "网易云", qq: "QQ", kugou: "酷狗", kuwo: "酷我", migu: "咪咕", qishui: "汽水" })[platform] || platform || ""; }
  function playlistTooltip() {
    let tooltip = $("#playlist-tooltip");
    if (tooltip) return tooltip;
    tooltip = document.createElement("div");
    tooltip.id = "playlist-tooltip";
    tooltip.className = "playlist-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    document.body.appendChild(tooltip);
    return tooltip;
  }
  function hidePlaylistTooltip() {
    const tooltip = $("#playlist-tooltip");
    if (!tooltip) return;
    tooltip.hidden = true;
    tooltip.classList.remove("is-visible");
  }
  function showPlaylistTooltip(button) {
    const tooltip = playlistTooltip();
    const name = button.dataset.tooltipName || "";
    const platform = button.dataset.tooltipPlatform || "";
    tooltip.innerHTML = `<span class="playlist-tooltip-name">${esc(name)}</span>${platform ? `<span class="playlist-tooltip-platform playlist-tooltip-platform-${esc(platform)}"><i aria-hidden="true"></i>${esc(platformLabel(platform))}</span>` : ""}`;
    tooltip.hidden = false;
    tooltip.classList.add("is-visible");
    button.setAttribute("aria-describedby", tooltip.id);
    const rect = button.getBoundingClientRect();
    const gap = 8;
    const margin = 12;
    const maxWidth = Math.min(320, window.innerWidth - margin * 2);
    tooltip.style.maxWidth = `${maxWidth}px`;
    let left = Math.max(margin, Math.min(rect.left + 8, window.innerWidth - maxWidth - margin));
    let top = rect.bottom + gap;
    if (top + tooltip.offsetHeight > window.innerHeight - margin) top = rect.top - tooltip.offsetHeight - gap;
    tooltip.style.left = `${Math.round(left)}px`;
    tooltip.style.top = `${Math.round(Math.max(margin, top))}px`;
  }
  function openPlaylistRename(playlistId) {
    const playlist = state.playlists.find((item) => item.id === playlistId);
    if (!playlist || playlist.id === "liked") return;
    state.activePlaylist = playlist.id;
    $("#name-dialog-title").textContent = "\u91cd\u547d\u540d\u6b4c\u5355";
    $("#name-form").dataset.mode = "rename";
    $("#playlist-name-input").value = playlist.name;
    $("#name-dialog").showModal();
  }
  async function deleteLocalPlaylist(playlistId) {
    const playlist = state.playlists.find((item) => item.id === playlistId);
    if (!playlist || playlist.id === "liked") return;
    const origin = playlist.account_platform ? `\n\n\u8fd9\u53ea\u4f1a\u5220\u9664\u58f0\u5c7f\u97f3\u4e50\u91cc\u7684\u672c\u5730\u540c\u6b65\u526f\u672c\uff0c\u4e0d\u4f1a\u5220\u9664${platformLabel(playlist.account_platform)}\u4e2d\u7684\u539f\u6b4c\u5355\u3002` : "";
    if (!confirm(`\u786e\u5b9a\u5220\u9664\u201c${playlist.name}\u201d\u5417\uff1f${origin}`)) return;
    try {
      await playlistRequest(`/api/playlists/${encodeURIComponent(playlist.id)}`, "DELETE");
      if (state.activePlaylist === playlist.id) state.activePlaylist = "liked";
      await refreshPlaylists();
      if (!state.playlists.some((item) => item.id === playlistId)) show("liked");
    } catch (error) { alert(error.message); }
  }
  function renderPlaylistNav() {
    const target = $("#playlist-nav");
    target.innerHTML = state.playlists.map((playlist) => {
      const sourcePlatform = playlist.account_platform || playlist.remote_platform || "";
      const origin = sourcePlatform ? `<small class="playlist-origin playlist-origin-${esc(sourcePlatform)}">${esc(platformLabel(sourcePlatform))}</small>` : "";
      const actions = playlist.id === "liked" ? "" : `<div class="playlist-nav-actions"><button class="playlist-nav-action" type="button" data-playlist-action="rename" data-playlist-id="${esc(playlist.id)}" title="\u91cd\u547d\u540d\u6b4c\u5355" aria-label="\u91cd\u547d\u540d\u6b4c\u5355">\u7f16\u8f91</button><button class="playlist-nav-action playlist-nav-delete" type="button" data-playlist-action="delete" data-playlist-id="${esc(playlist.id)}" title="\u5220\u9664\u672c\u5730\u6b4c\u5355" aria-label="\u5220\u9664\u672c\u5730\u6b4c\u5355">\u5220\u9664</button></div>`;
      const tooltip = `${playlist.name}${sourcePlatform ? ` · ${platformLabel(sourcePlatform)}` : ""}`;
      return `<div class="playlist-nav-row ${playlist.id === state.activePlaylist ? "active" : ""}"><button class="playlist-nav-item ${playlist.id === state.activePlaylist ? "active" : ""}" data-playlist="${esc(playlist.id)}" data-tooltip-name="${esc(playlist.name)}" data-tooltip-platform="${esc(sourcePlatform)}" aria-label="${esc(tooltip)}"><span>${esc(playlist.name)}${origin}</span><b>${(playlist.songs || []).length}</b></button>${actions}</div>`;
    }).join("");
    target.querySelectorAll("[data-playlist]").forEach((button) => button.addEventListener("click", () => {
      hidePlaylistTooltip();
      state.activePlaylist = button.dataset.playlist;
      renderPlaylistNav();
      show("playlist");
      closeMobileMenu();
    }));
    target.querySelectorAll("[data-playlist]").forEach((button) => {
      button.addEventListener("pointerenter", () => showPlaylistTooltip(button));
      button.addEventListener("pointerleave", hidePlaylistTooltip);
      button.addEventListener("focus", () => showPlaylistTooltip(button));
      button.addEventListener("blur", hidePlaylistTooltip);
    });
    target.querySelectorAll("[data-playlist-action]").forEach((button) => button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (button.dataset.playlistAction === "rename") openPlaylistRename(button.dataset.playlistId);
      if (button.dataset.playlistAction === "delete") deleteLocalPlaylist(button.dataset.playlistId);
    }));
  }
  async function loadPlaylists() {
    try {
      const response = await fetch("/api/playlists");
      const data = await response.json();
      if (data.code !== 0) throw new Error(data.msg || "歌单加载失败");
      state.playlists = data.playlists || [];
      state.activePlaylist = state.activePlaylist || "liked";
      state.liked = defaultLiked()?.songs || state.liked;
      renderPlaylistNav();
      counts();
    } catch (error) {
      console.warn(error);
    }
  }
  async function playlistRequest(url, method, body) {
    const response = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const raw = await response.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch { /* Reverse proxies may produce a non-JSON error page. */ }
    if (!data) {
      throw new Error(response.status >= 500 ? "服务器暂时无法处理导入请求，请稍后重试" : `歌单操作失败（HTTP ${response.status}）`);
    }
    if (!response.ok || data.code !== 0) throw new Error(data.msg || "歌单操作失败");
    return data;
  }
  async function refreshPlaylists() {
    const response = await fetch("/api/playlists");
    const data = await response.json();
    if (data.code === 0) {
      state.playlists = data.playlists || [];
      state.liked = defaultLiked()?.songs || [];
      renderPlaylistNav();
      counts();
    }
  }
  async function search() {
    const keyword = $("#search-input").value.trim();
    if (!keyword) return $("#search-input").focus();
    $("#state-panel").hidden = false; $("#track-table").hidden = true; $("#state-panel").className = "state-panel"; $("#state-panel").textContent = "正在搜索...";
    $("#result-title").textContent = `“${keyword}” 的搜索结果`; $("#result-meta").textContent = `${label(state.source)} · 正在加载`;
    try {
      const response = await fetch(`/api/player/search?${query({ keyword, source: state.source, limit: 20 })}`);
      const data = await response.json();
      if (!response.ok || data.code !== 0) throw new Error(data.msg || "搜索失败");
      state.results = data.data || []; $("#result-meta").textContent = `${label(state.source)} · ${state.results.length} 首结果`;
      if (!state.results.length) { $("#state-panel").textContent = "没有找到匹配的歌曲。"; return; }
      render(state.results, $("#track-list")); $("#state-panel").hidden = true; $("#track-table").hidden = false;
    } catch (error) {
      $("#state-panel").className = "state-panel error"; $("#state-panel").textContent = error.message || "搜索失败，请检查服务配置。"; $("#result-meta").textContent = "请求未完成";
    }
  }
  function resolveParams(song, exclude = []) {
    return {
      song_id: song.id,
      source: song.source,
      strategy: state.strategy,
      title: song.title,
      artist: song.artist,
      album: song.album,
      duration: song.duration || 0,
      extra: song.extra || "",
      exclude: exclude.join(","),
      quality: state.quality,
    };
  }
  function cancelPendingPlayback() {
    if (state.resolveController) state.resolveController.abort();
    if (state.lyricController) state.lyricController.abort();
    if (state.nextResolveController) state.nextResolveController.abort();
    state.resolveController = null;
    state.lyricController = null;
    state.nextResolveController = null;
    state.nextPlayback = null;
  }
  async function loadLyrics(lyricSong, attempt) {
    // `lyricSong` can be the actual official fallback match (for example a
    // QQ song matched while the queue entry remains a Kugou song). Keep the
    // current queue entry separately for the stale-response guard: comparing
    // it with `lyricSong` would discard every cross-platform lyric response.
    const currentSong = state.current;
    const controller = new AbortController();
    state.lyricController = controller;
    try {
      const response = await fetch(`/api/player/lyric?${query(resolveParams(lyricSong))}`, { signal: controller.signal });
      const data = await response.json();
      if (attempt !== state.playbackAttempt || state.current !== currentSong || controller.signal.aborted) return;
      if (response.ok && data.code === 0 && data.lyric) {
        state.lyricLines = parseLyrics(data.lyric);
        state.activeLyricIndex = -1;
        renderLyrics();
      }
    } catch (error) {
      if (error.name !== "AbortError") console.warn("歌词加载失败", error);
    } finally {
      if (state.lyricController === controller) state.lyricController = null;
    }
  }
  function queuedNextSong() {
    if (!state.current || !state.queue.length) return null;
    if (state.shuffle && state.queue.length > 1) {
      const candidates = state.queue.filter((song) => id(song) !== id(state.current));
      return candidates[Math.floor(Math.random() * candidates.length)] || null;
    }
    const at = state.queue.findIndex((song) => id(song) === id(state.current));
    return state.queue[(at + 1 + state.queue.length) % state.queue.length] || null;
  }

  function applyResolvedSong(song, data) {
    state.resolvedBy = data.resolved_by || "";
    state.resolvedKind = data.resolved_kind || "native";
    state.resolvedSong = data.resolved_song || null;
    state.lyricLines = parseLyrics(data.lyric || song.lyric || "");
    syncNowPlaying(song);
    renderLyrics();
  }

  async function prefetchNextPlayback(song, attempt) {
    const next = queuedNextSong();
    if (!next || next === song || state.repeat === "one") return;
    if (state.nextResolveController) state.nextResolveController.abort();
    const controller = new AbortController();
    state.nextResolveController = controller;
    try {
      const response = await fetch(`/api/player/resolve?${query(resolveParams(next))}`, { signal: controller.signal });
      const data = await response.json();
      if (!response.ok || data.code !== 0 || !data.url) return;
      if (controller.signal.aborted || state.current !== song || state.playbackAttempt !== attempt) return;
      state.nextPlayback = { song: next, data };
      const idle = getIdleAudio();
      idle.src = data.url;
      idle.load();
    } catch (error) {
      if (error.name !== "AbortError") console.debug("下一首预解析失败", error);
    } finally {
      if (state.nextResolveController === controller) state.nextResolveController = null;
    }
  }

  function clearHandoffWatchdog() {
    if (state.handoffWatchdog) window.clearTimeout(state.handoffWatchdog);
    state.handoffWatchdog = null;
  }

  async function startPrefetchedPlayback(prefetched) {
    const { song, data } = prefetched;
    cancelPendingPlayback();
    clearHandoffWatchdog();
    const attempt = ++state.playbackAttempt;
    state.queue = Array.isArray(state.queue) ? state.queue : [];
    state.current = song;
    state.playbackRetrySources.clear();
    state.recoveryInProgress = false;
    state.lyricLines = [];
    state.activeLyricIndex = -1;
    state.skippingUnavailable = false;
    state.prefetchRefreshed = false;
    // Switch to the idle audio element which already has data preloaded.
    const prev = audio;
    switchAudio();
    state.audioAttempt = attempt;
    state.audioUrl = data.url;
    audio.volume = prev.volume;
    audio.loop = false;
    // A refreshed prefetch, a silent-bridge takeover or a cleared element can
    // leave the idle player pointing at a different URL than the one we just
    // committed to.  Without this the browser would keep the stale buffer and
    // the queue stalls with no error event at all.
    if (audio.src !== data.url) {
      audio.src = data.url;
      audio.load();
    }
    setPlayer(song);
    renderQueue();
    renderLyrics();
    $("#hero-song").textContent = song.title;
    $("#hero-artist").textContent = song.artist || "未知歌手";
    applyResolvedSong(song, data);
    state.switchingPlayback = true;
    // audio.play() on an element without usable data can stay pending forever,
    // emitting neither "ended" nor "error".  Fall back to a full resolve so the
    // queue always keeps moving.
    state.handoffWatchdog = window.setTimeout(() => {
      state.handoffWatchdog = null;
      if (attempt !== state.playbackAttempt || state.current !== song) return;
      if (!audio.paused && audio.currentTime > 0) return;
      state.switchingPlayback = false;
      play(song, state.queue, true);
    }, 5000);
    try {
      await audio.play();
    } catch (error) {
      if (attempt !== state.playbackAttempt || state.current !== song) return;
      // The preloaded element refused to start (expired or dropped source).
      // Go back to the element that still owns the audio session and resolve
      // the track normally instead of leaving the queue stuck.
      audio = prev;
      return play(song, state.queue, true);
    } finally {
      state.switchingPlayback = false;
    }
    clearHandoffWatchdog();
    // Only stop the previous player AFTER the new one is confirmed playing
    prev.pause();
    prev.removeAttribute("src");
    prev.load();
    if (attempt !== state.playbackAttempt || state.current !== song) return;
    setMediaSessionPlaybackState();
    syncMediaSessionPosition();
    $("#source-label").textContent = sourceText(song);
    if (!data.lyric) loadLyrics(state.resolvedSong || song, attempt);
    prefetchNextPlayback(song, attempt);
  }

  async function resolveAndStart(song, exclude = []) {
    const attempt = ++state.playbackAttempt;
    if (state.resolveController) state.resolveController.abort();
    const controller = new AbortController();
    state.resolveController = controller;
    try {
      const response = await fetch(`/api/player/resolve?${query(resolveParams(song, exclude))}`, { signal: controller.signal });
      const data = await response.json();
      if (!response.ok || data.code !== 0 || !data.url) throw new Error("未获取到播放地址");
      if (attempt !== state.playbackAttempt || state.current !== song || controller.signal.aborted) return;
      applyResolvedSong(song, data);
      stopKeepAlive();
      state.audioAttempt = attempt;
      state.audioUrl = data.url;
      audio.src = data.url;
      audio.load();
      // audio.play() can stay pending forever when the resolved URL never
      // delivers any data (dead or very slow CDN node): the element emits
      // neither "playing" nor "error", so the track hangs with no message at
      // all.  Arm the same kind of watchdog startPrefetchedPlayback uses.
      // The probe is readyState/currentTime, never paused: a locked iOS screen
      // reports paused === true on a perfectly good source, so using paused
      // here would break lock-screen playback.
      let watchdogFired = false;
      let watchdogTimer = null;
      const clearOwnWatchdog = () => {
        if (watchdogTimer === null) return;
        window.clearTimeout(watchdogTimer);
        if (state.handoffWatchdog === watchdogTimer) state.handoffWatchdog = null;
        watchdogTimer = null;
      };
      const armWatchdog = (delay, allowGrace) => {
        watchdogTimer = window.setTimeout(() => {
          if (state.handoffWatchdog === watchdogTimer) state.handoffWatchdog = null;
          watchdogTimer = null;
          if (attempt !== state.playbackAttempt || state.current !== song) return;
          // Healthy either way: the clock is moving, or the element already
          // buffered enough to keep playing (paused with data means an
          // autoplay/lock-screen block, which visibilitychange handles).
          if (audio.currentTime > 0 || audio.readyState >= 3) return;
          // Not playing yet.  Grant one longer grace period when the page is
          // hidden (a locked screen throttles the initial media fetch) or when
          // the source did deliver some data and is merely slow.  A source that
          // produced nothing at all (readyState 0) is treated as dead.
          if (allowGrace && (document.hidden || audio.readyState > 0)) {
            armWatchdog(12000, false);
            return;
          }
          watchdogFired = true;
          // recoverPlayback bumps state.playbackAttempt on every retry, so the
          // only meaningful guard left here is "is this still the live track".
          recoverPlayback(song).then((recovered) => {
            if (recovered || state.current !== song) return;
            handleUnavailable(song, new Error("播放地址无响应"));
          }).catch(() => {
            if (state.current === song) handleUnavailable(song, new Error("播放地址无响应"));
          });
        }, delay);
        state.handoffWatchdog = watchdogTimer;
      };
      clearHandoffWatchdog();
      armWatchdog(8000, true);
      try {
        await audio.play();
      } catch (error) {
        clearOwnWatchdog();
        if (watchdogFired || attempt !== state.playbackAttempt || state.current !== song) return;
        // Backgrounded tabs and locked iOS screens reject with NotAllowedError
        // or AbortError.  The source is fine in that case, so keep the track
        // and let the visibilitychange handler resume it on unlock.
        const blocked = error && (error.name === "NotAllowedError" || error.name === "AbortError");
        // Extra safety for locked iOS screens: if the element already buffered
        // this source, the rejection is an autoplay restriction whatever its
        // name is, so never treat it as a broken URL.
        if (blocked || (document.hidden && audio.readyState >= 3)) {
          state.pendingResume = true;
          syncPlaybackUi();
          return;
        }
        // Anything else means this URL is unusable.  Surface it so play() can
        // fall back to another resolver or skip the track with a message,
        // instead of silently freezing on a dead source.
        throw error;
      }
      clearOwnWatchdog();
      if (attempt !== state.playbackAttempt || state.current !== song) return;
      setMediaSessionPlaybackState();
      syncMediaSessionPosition();
      $("#source-label").textContent = sourceText(song);
      if (!data.lyric) loadLyrics(state.resolvedSong || song, attempt);
      prefetchNextPlayback(song, attempt);
    } finally {
      if (state.resolveController === controller) state.resolveController = null;
    }
  }
  function cancelCurrentAudio() {
    stopKeepAlive();
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    const idle = getIdleAudio();
    idle.pause();
    idle.removeAttribute("src");
    idle.load();
    state.audioUrl = "";
    state.audioAttempt = 0;
  }
  async function recoverPlayback(song) {
    if (!song || state.current !== song || state.recoveryInProgress) return false;
    if (state.strategy !== "auto" || !state.resolvedBy) return false;
    state.recoveryInProgress = true;
    try {
      while (state.resolvedBy && !state.playbackRetrySources.has(state.resolvedBy)) {
        state.playbackRetrySources.add(state.resolvedBy);
        try {
          await resolveAndStart(song, [...state.playbackRetrySources]);
          return true;
        } catch (_) {
          // The returned URL failed in the browser. Exclude that resolver and continue.
        }
      }
      return false;
    } finally {
      state.recoveryInProgress = false;
    }
  }
  async function play(song, queue = state.results, autoSkip = false) {
    if (!song) return;
    cancelPendingPlayback();
    clearHandoffWatchdog();
    ++state.playbackAttempt;
    if (autoSkip && !state.nextPlayback) {
      // No prefetched track: keep audio session alive with silent bridge
      startKeepAlive();
    } else if (!autoSkip) {
      cancelCurrentAudio();
    }
    state.audioUrl = "";
    state.audioAttempt = 0;
    if (!autoSkip) state.failedQueueIds.clear();
    state.queue = Array.isArray(queue) ? queue : [];
    state.current = song;
    state.resolvedBy = "";
    state.resolvedKind = "";
    state.resolvedSong = null;
    state.playbackRetrySources.clear();
    state.recoveryInProgress = false;
    state.lyricLines = [];
    state.activeLyricIndex = -1;
    state.skippingUnavailable = false;
    state.prefetchRefreshed = false;
    setPlayer(song);
    renderQueue();
    renderLyrics();
    $("#hero-song").textContent = song.title;
    $("#hero-artist").textContent = song.artist || "未知歌手";
    $("#source-label").textContent = label(song.source);
    syncPlaybackUi(true);
    // Keep autoplay enabled while resolving the next URL so mobile browsers treat
    // the transition as the same active audio session.
    audio.autoplay = true;
    try {
      await resolveAndStart(song);
    } catch (error) {
      if (error.name === "AbortError" || state.current !== song) return;
      // The audio "error" listener may already be recovering this same song;
      // skipping here in parallel would fight with it.
      if (state.recoveryInProgress) return;
      if (!await recoverPlayback(song)) handleUnavailable(song, error);
    } finally {
      if (state.current === song) audio.autoplay = false;
    }
  }
  function handleUnavailable(song = state.current, error = new Error("播放失败")) {
    if (!song || state.skippingUnavailable) return;
    state.skippingUnavailable = true;
    state.failedQueueIds.add(id(song));
    syncPlaybackUi();
    const candidates = state.queue.filter((item) => !state.failedQueueIds.has(id(item)));
    if (!candidates.length) {
      $("#state-panel").hidden = false; $("#state-panel").className = "state-panel error";
      $("#state-panel").textContent = "该播放列表暂无可用播放源";
      stopKeepAlive();
      audio.pause(); audio.removeAttribute("src"); audio.load();
      state.skippingUnavailable = false;
      return;
    }
    startKeepAlive();
    const currentIndex = state.queue.findIndex((item) => id(item) === id(song));
    const next = state.shuffle ? candidates[Math.floor(Math.random() * candidates.length)] : [...state.queue.slice(currentIndex + 1), ...state.queue.slice(0, Math.max(currentIndex + 1, 0))].find((item) => !state.failedQueueIds.has(id(item))) || candidates[0];
    $("#state-panel").hidden = false; $("#state-panel").className = "state-panel error";
    $("#state-panel").textContent = `“${song.title}”无可用播放源，正在跳过…`;
    window.setTimeout(() => play(next, state.queue, true), 550);
  }
  const SILENT_WAV = "/static/silence.wav";

  // Mobile browsers suspend a locked page as soon as no audio is playing, which
  // stops the queue between two songs.  Looping a short silent track keeps the
  // audio session (and the page) alive while the next URL is resolved.
  function startKeepAlive() {
    if (state.keepAliveTimer) window.clearTimeout(state.keepAliveTimer);
    state.keepAliveTimer = window.setTimeout(abandonKeepAlive, 25000);
    if (state.keepAlive) return;
    state.keepAlive = true;
    try {
      audio.loop = true;
      audio.autoplay = true;
      audio.src = SILENT_WAV;
      audio.load();
      const started = audio.play();
      if (started && started.catch) started.catch(() => {});
    } catch (_) {}
  }

  function stopKeepAlive() {
    if (state.keepAliveTimer) window.clearTimeout(state.keepAliveTimer);
    state.keepAliveTimer = null;
    state.keepAlive = false;
    audio.loop = false;
  }

  function abandonKeepAlive() {
    if (!state.keepAlive) return;
    stopKeepAlive();
    audio.pause();
  }

  function syncMediaSessionPosition() {
    if (!("mediaSession" in navigator) || typeof navigator.mediaSession.setPositionState !== "function") return;
    if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
    try {
      navigator.mediaSession.setPositionState({
        duration: audio.duration,
        playbackRate: audio.playbackRate || 1,
        position: Math.min(Math.max(audio.currentTime, 0), audio.duration),
      });
    } catch (_) {}
  }

  // Signed playback URLs expire, so refresh the prefetched next song shortly
  // before the current one ends instead of relying on the initial prefetch.
  function maybeRefreshNextPlayback() {
    if (state.keepAlive || audio.paused || state.repeat === "one" || !state.current) return;
    const remaining = audio.duration - audio.currentTime;
    if (!Number.isFinite(remaining)) return;
    if (remaining > 40) {
      state.prefetchRefreshed = false;
      return;
    }
    if (state.prefetchRefreshed) return;
    state.prefetchRefreshed = true;
    prefetchNextPlayback(state.current, state.playbackAttempt);
  }

  function syncMediaSession(song = state.current) {
    if (!song || !("mediaSession" in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: song.title || "未知歌曲",
        artist: song.artist || "未知歌手",
        album: song.album || "声屿音乐",
        artwork: song.cover ? [{ src: song.cover, sizes: "300x300", type: "image/jpeg" }] : [],
      });
    } catch (_) {}
  }

  function setMediaSessionPlaybackState() {
    if ("mediaSession" in navigator) {
      try { navigator.mediaSession.playbackState = audio.paused ? "paused" : "playing"; } catch (_) {}
    }
  }

  function setPlayer(song) {
    $("#player-title").textContent = song.title;
    $("#player-artist").textContent = song.artist || "未知歌手";
    $("#player-cover").innerHTML = song.cover ? `<img src="${esc(song.cover)}" alt="">` : "S";
    syncMediaSession(song);
    syncNowPlaying(song);
    syncPlaybackUi();
  }
  async function like(song) {
    if (!song) return;
    try {
      if (isLiked(song)) await playlistRequest(`/api/playlists/liked/songs/${encodeURIComponent(song.source)}/${encodeURIComponent(song.id)}`, "DELETE");
      else await playlistRequest("/api/playlists/liked/songs", "POST", { song });
      await refreshPlaylists();
      if (state.current && id(state.current) === id(song)) setPlayer(state.current);
      renderLibrary(state.activePlaylist && state.activePlaylist !== "liked" ? "playlist" : "liked");
      if (!$("#track-table").hidden) render(state.results, $("#track-list"));
    } catch (error) { alert(error.message); }
  }
  function openAddDialog(song) {
    if (!song) return;
    $("#playlist-select").innerHTML = state.playlists.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
    $("#playlist-select").value = state.activePlaylist || "liked";
    $("#playlist-dialog").dataset.song = JSON.stringify(song);
    $("#playlist-dialog").showModal();
  }
  async function addCurrentToPlaylist(event) {
    event.preventDefault();
    try {
      const song = JSON.parse($("#playlist-dialog").dataset.song || "null");
      await playlistRequest(`/api/playlists/${encodeURIComponent($("#playlist-select").value)}/songs`, "POST", { song });
      $("#playlist-dialog").close();
      await refreshPlaylists();
      if (state.activePlaylist && state.activePlaylist !== "liked") renderLibrary("playlist");
    } catch (error) { alert(error.message); }
  }
  function move(offset, continuous = false) {
    if (!state.current || !state.queue.length) return;
    if (continuous && offset > 0 && state.nextPlayback) {
      const prefetched = state.nextPlayback;
      state.nextPlayback = null;
      return startPrefetchedPlayback(prefetched).catch((error) => handleUnavailable(prefetched.song, error));
    }
    if (state.shuffle && offset > 0 && state.queue.length > 1) {
      const candidates = state.queue.filter((song) => id(song) !== id(state.current));
      const randomSong = candidates[Math.floor(Math.random() * candidates.length)];
      return play(randomSong, state.queue, continuous);
    }
    const at = state.queue.findIndex((song) => id(song) === id(state.current));
    play(state.queue[(at + offset + state.queue.length) % state.queue.length], state.queue, continuous);
  }
  function refreshNextPlaybackPrefetch() {
    if (state.nextResolveController) state.nextResolveController.abort();
    state.nextResolveController = null;
    state.nextPlayback = null;
    if (state.current && !audio.paused && state.repeat !== "one") {
      prefetchNextPlayback(state.current, state.playbackAttempt);
    }
  }
  function setRepeatMode(mode) {
    state.repeat = mode === "one" ? "one" : "all";
    localStorage.setItem("sound-island-repeat", state.repeat);
    syncPlaybackModes();
    refreshNextPlaybackPrefetch();
  }
  function toggleShuffle() {
    state.shuffle = !state.shuffle;
    localStorage.setItem("sound-island-shuffle", String(state.shuffle));
    syncPlaybackModes();
    refreshNextPlaybackPrefetch();
  }
  function handleEnded() {
    if (state.keepAlive || !state.current) return;
    if (state.repeat === "one" && !state.failedQueueIds.has(id(state.current))) {
      audio.currentTime = 0;
      audio.play().catch((error) => handleUnavailable(state.current, error));
      return;
    }
    if (state.nextPlayback) {
      // Idle audio already has the next song loaded — switch instantly
      move(1, true);
    } else {
      // No prefetch ready; start silent bridge to keep session alive
      startKeepAlive();
      move(1, true);
    }
  }

  async function loadDailyRecommendations(nextBatch = false) {
    if (nextBatch) state.dailyBatch += 1;
    const target = $("#daily-list");
    target.innerHTML = '<div class="state-panel">正在准备今日推荐...</div>';
    $("#daily-refresh").disabled = true;
    try {
      const response = await fetch(`/api/player/daily-recommendations?batch=${state.dailyBatch}`, { cache: "no-store" });
      const data = await response.json();
      if (!response.ok || data.code !== 0) throw new Error(data.msg || "今日推荐加载失败");
      state.dailySongs = data.data || [];
      state.dailyLoaded = true;
      $("#daily-description").textContent = `${data.date} · 已为你挑选 ${data.total || state.dailySongs.length} 首可播放热歌`;
      render(state.dailySongs, target);
    } catch (error) {
      target.innerHTML = `<div class="state-panel error">${esc(error.message || "今日推荐加载失败")}</div>`;
    } finally {
      $("#daily-refresh").disabled = false;
    }
  }

  function dailyPlaylistShareUrl(playlist) {
    if (!playlist) return "";
    if (playlist.source === "qq") return `https://y.qq.com/n/ryqq/playlist/${encodeURIComponent(playlist.id)}`;
    if (playlist.source === "kugou") return `https://activity.kugou.com/mfanxing-home/v1/all_share/index.html?global_specialid=${encodeURIComponent(playlist.id)}`;
    return `https://music.163.com/playlist?id=${encodeURIComponent(playlist.id)}`;
  }
  function playCountText(value) {
    const count = Number(value) || 0;
    if (count >= 100000000) return `${(count / 100000000).toFixed(1)}亿`;
    if (count >= 10000) return `${(count / 10000).toFixed(count >= 1000000 ? 0 : 1)}万`;
    return String(count);
  }
  function renderDailyPlaylists() {
    const target = $("#daily-playlist-grid");
    if (!state.dailyPlaylists.length) {
      target.innerHTML = '<div class="state-panel">今天还没有可推荐的歌单。</div>';
      return;
    }
    target.innerHTML = state.dailyPlaylists.map((playlist) => `<button class="playlist-card" type="button" data-playlist-source="${esc(playlist.source)}" data-playlist-remote="${esc(playlist.id)}">
      <span class="playlist-card-cover">${playlist.cover ? `<img src="${esc(playlist.cover)}" alt="" loading="lazy">` : "S"}<span class="playlist-card-play" aria-hidden="true">▶</span></span>
      <span class="playlist-card-body">
        <span class="playlist-card-title">${esc(playlist.name)}</span>
        <span class="playlist-card-meta"><em class="playlist-origin playlist-origin-${esc(playlist.source)}">${esc(label(playlist.source))}</em>${playlist.track_count ? `<i>${playlist.track_count} 首</i>` : ""}${playlist.play_count ? `<i>${playCountText(playlist.play_count)} 播放</i>` : ""}</span>
      </span></button>`).join("");
    target.querySelectorAll("[data-playlist-remote]").forEach((card) => card.addEventListener("click", () => {
      const playlist = state.dailyPlaylists.find((item) => item.source === card.dataset.playlistSource && item.id === card.dataset.playlistRemote);
      if (playlist) openDailyPlaylist(playlist);
    }));
  }
  async function loadDailyPlaylists(nextBatch = false) {
    if (nextBatch) state.dailyPlaylistBatch += 1;
    closeDailyPlaylistDetail();
    const target = $("#daily-playlist-grid");
    target.innerHTML = '<div class="state-panel">正在准备今日歌单...</div>';
    $("#daily-playlist-refresh").disabled = true;
    try {
      const response = await fetch(`/api/player/daily-playlists?batch=${state.dailyPlaylistBatch}`, { cache: "no-store" });
      const data = await response.json();
      if (!response.ok || data.code !== 0) throw new Error(data.msg || "今日歌单加载失败");
      state.dailyPlaylists = data.data || [];
      state.dailyPlaylistsLoaded = true;
      $("#daily-playlist-description").textContent = `${data.date} · 今日精选 ${data.total || state.dailyPlaylists.length} 张公开歌单`;
      renderDailyPlaylists();
    } catch (error) {
      target.innerHTML = `<div class="state-panel error">${esc(error.message || "今日歌单加载失败")}</div>`;
    } finally {
      $("#daily-playlist-refresh").disabled = false;
    }
  }
  function closeDailyPlaylistDetail() {
    state.activeDailyPlaylist = null;
    state.dailyPlaylistTracks = [];
    $("#daily-playlist-detail").hidden = true;
    $("#daily-playlist-grid").hidden = false;
  }
  async function openDailyPlaylist(playlist) {
    state.activeDailyPlaylist = playlist;
    state.dailyPlaylistTracks = [];
    $("#daily-playlist-grid").hidden = true;
    $("#daily-playlist-detail").hidden = false;
    $("#daily-playlist-detail-title").textContent = playlist.name;
    $("#daily-playlist-detail-meta").textContent = `${label(playlist.source)}${playlist.creator ? ` · ${playlist.creator}` : ""}`;
    const target = $("#daily-playlist-tracks");
    target.innerHTML = '<div class="state-panel">正在读取歌单曲目...</div>';
    $("#daily-playlist-play-all").disabled = true;
    $("#daily-playlist-save").disabled = true;
    try {
      const params = new URLSearchParams({ source: playlist.source, id: playlist.id });
      const response = await fetch(`/api/player/playlist-tracks?${params.toString()}`, { cache: "no-store" });
      const data = await response.json();
      if (!response.ok || data.code !== 0) throw new Error(data.msg || "歌单曲目读取失败");
      state.dailyPlaylistTracks = data.data || [];
      render(state.dailyPlaylistTracks, target);
      $("#daily-playlist-play-all").disabled = !state.dailyPlaylistTracks.length;
      $("#daily-playlist-save").disabled = false;
    } catch (error) {
      target.innerHTML = `<div class="state-panel error">${esc(error.message || "歌单曲目读取失败")}</div>`;
    }
  }
  async function saveDailyPlaylist() {
    const playlist = state.activeDailyPlaylist;
    if (!playlist) return;
    const button = $("#daily-playlist-save");
    button.disabled = true;
    button.textContent = "正在收藏";
    try {
      const result = await playlistRequest("/api/playlists/import-link", "POST", { url: dailyPlaylistShareUrl(playlist), name: playlist.name });
      state.activePlaylist = result.playlist.id;
      await refreshPlaylists();
      alert(result.msg || "歌单已收藏到我的歌单");
    } catch (error) {
      alert(`收藏失败：${error.message}`);
    } finally {
      button.disabled = false;
      button.textContent = "收藏到我的歌单";
    }
  }

  function isMobileViewport() { return window.matchMedia("(max-width: 860px)").matches; }
  function setMobileMenu(open) {
    const shouldOpen = Boolean(open) && isMobileViewport();
    document.body.classList.toggle("mobile-menu-open", shouldOpen);
    $("#mobile-sidebar-backdrop").hidden = !shouldOpen;
    $("#mobile-menu-button").setAttribute("aria-expanded", String(shouldOpen));
  }
  function openMobileMenu() { setMobileMenu(true); }
  function closeMobileMenu() { setMobileMenu(false); }

  function show(view) {
    const library = view === "liked" || view === "playlist";
    const accounts = view === "accounts";
    const daily = view === "daily";
    const dailyPlaylists = view === "daily-playlists";
    if (view !== "playlist" && (state.playlistSearchPlaylistId !== null || state.playlistSearchQuery)) resetPlaylistSearch();
    $("#discover-view").hidden = library || accounts || daily || dailyPlaylists;
    $("#library-view").hidden = !library;
    $("#accounts-view").hidden = !accounts;
    $("#daily-view").hidden = !daily;
    $("#daily-playlists-view").hidden = !dailyPlaylists;
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.view === view));
    if (library) renderLibrary(view);
    if (accounts) loadConnectedAccounts();
    if (daily && !state.dailyLoaded) loadDailyRecommendations();
    if (dailyPlaylists && !state.dailyPlaylistsLoaded) loadDailyPlaylists();
  }
  function art() {
    const canvas = $("#hero-art"), ctx = canvas.getContext("2d"), w = canvas.width, h = canvas.height;
    const palette = state.theme === "sage"
      ? { base: "#d9f4ed", grid: "rgba(19,116,96,.10)", outer: "#28a98d", inner: "#81d5c2", line: "rgba(17,111,92,.30)", center: "#fafffd", bar: "#a9ded1", barFill: "#e97968" }
      : { base: "#070d29", grid: "rgba(164,185,255,.06)", outer: "#6f72ec", inner: "#151a48", line: "rgba(192,204,255,.24)", center: "#ecebff", bar: "#0a1031", barFill: "#a58aff" };
    ctx.fillStyle = palette.base; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = palette.grid; ctx.lineWidth = 1;
    for (let x = 0; x < w; x += 78) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke(); }
    for (let y = 0; y < h; y += 78) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
    ctx.fillStyle = palette.outer; ctx.beginPath(); ctx.arc(w * .82, h * .42, h * .31, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = palette.inner; ctx.beginPath(); ctx.arc(w * .82, h * .42, h * .23, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = palette.line; ctx.lineWidth = 2;
    for (let radius = h * .08; radius < h * .22; radius += h * .035) { ctx.beginPath(); ctx.arc(w * .82, h * .42, radius, 0, Math.PI * 2); ctx.stroke(); }
    ctx.fillStyle = palette.center; ctx.beginPath(); ctx.arc(w * .82, h * .42, h * .034, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = palette.bar; ctx.fillRect(w * .56, h * .74, w * .34, 5);
    ctx.fillStyle = palette.barFill; ctx.fillRect(w * .56, h * .74, w * .18, 5);
  }

  function stopAccountQr() {
    if (state.accountQrTimer) window.clearInterval(state.accountQrTimer);
    state.accountQrTimer = null;
    state.accountQrPolling = false;
  }
  async function startAccountQr(platform = state.accountQrPlatform || "netease") {
    stopAccountQr();
    state.accountQrPlatform = platform;
    const info = accountPlatforms.find((item) => item.id === platform);
    const platformName = info?.name || "\u97f3\u4e50\u5e73\u53f0";
    const dialog = $("#account-qr-dialog");
    const title = $("#account-qr-title");
    const status = $("#account-qr-status");
    const image = $("#account-qr-image");
    title.textContent = `${platformName}\u626b\u7801\u767b\u5f55`;
    image.alt = `${platformName}\u767b\u5f55\u4e8c\u7ef4\u7801`;
    status.textContent = `\u6b63\u5728\u751f\u6210${platformName}\u6388\u6743\u7801`;
    image.removeAttribute("src");
    if (!dialog.open) dialog.showModal();
    try {
      const data = await accountRequest(`/api/accounts/${encodeURIComponent(platform)}/qr/start`, "POST");
      if (typeof data.qr_image !== "string" || !data.qr_image.startsWith("data:image/png;base64,")) {
        throw new Error("\u4e8c\u7ef4\u7801\u6570\u636e\u4e0d\u5b8c\u6574\uff0c\u8bf7\u5237\u65b0\u540e\u91cd\u8bd5");
      }
      const imageLoaded = new Promise((resolve, reject) => {
        image.onload = () => resolve();
        image.onerror = () => reject(new Error("\u4e8c\u7ef4\u7801\u56fe\u7247\u52a0\u8f7d\u5931\u8d25\uff0c\u8bf7\u70b9\u51fb\u5237\u65b0\u4e8c\u7ef4\u7801\u91cd\u8bd5"));
      });
      image.src = data.qr_image;
      await imageLoaded;
      status.textContent = platform === "qishui" ? "\u8bf7\u4f7f\u7528\u5df2\u767b\u5f55\u7684\u6c7d\u6c34\u97f3\u4e50 App \u626b\u7801\u786e\u8ba4" : `\u8bf7\u4f7f\u7528${platformName} App \u626b\u7801\uff0c\u5e76\u5728 App \u4e2d\u786e\u8ba4\u6388\u6743`;
      const poll = async () => {
        if (state.accountQrPolling || !dialog.open) return;
        state.accountQrPolling = true;
        try {
          const result = await accountRequest(`/api/accounts/${encodeURIComponent(platform)}/qr/${encodeURIComponent(data.token)}`);
          if (result.status === "authorized") {
            stopAccountQr();
            status.textContent = "\u6388\u6743\u6210\u529f\uff0c\u6b63\u5728\u66f4\u65b0\u8d26\u6237";
            dialog.close();
            await loadConnectedAccounts();
            alert(`${platformName}\u8d26\u6237\u5df2\u8fde\u63a5\uff0c\u73b0\u5728\u53ef\u4ee5\u540c\u6b65\u6b4c\u5355\u3002`);
          } else if (result.status === "expired") {
            stopAccountQr();
            status.textContent = "\u4e8c\u7ef4\u7801\u5df2\u8fc7\u671f\uff0c\u8bf7\u70b9\u51fb\u5237\u65b0";
          } else {
            status.textContent = result.message || "\u7b49\u5f85\u626b\u7801\u6388\u6743";
          }
        } catch (error) {
          stopAccountQr();
          status.textContent = error.message;
        } finally { state.accountQrPolling = false; }
      };
      await poll();
      if (dialog.open && state.accountQrTimer === null) state.accountQrTimer = window.setInterval(poll, 2500);
    } catch (error) {
      status.textContent = error.message;
    }
  }
  $("#account-qr-close").addEventListener("click", () => $("#account-qr-dialog").close());
  $("#account-qr-dialog").addEventListener("close", stopAccountQr);
  $("#account-qr-refresh").addEventListener("click", () => startAccountQr(state.accountQrPlatform));
  $("#close-account-connect").addEventListener("click", () => $("#account-connect-dialog").close());
  $("#account-connect-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = $("#account-connect-form");
    const method = form.dataset.method || "cookie";
    const platform = form.dataset.platform;
    const submit = $("#account-connect-submit");
    submit.disabled = true; submit.textContent = "正在连接";
    try {
      const result = await accountRequest(`/api/accounts/${encodeURIComponent(platform)}/connect`, "POST", {
        method, cookie: $("#account-connect-cookie").value.trim(), username: $("#account-connect-username").value.trim(), password: $("#account-connect-password").value,
      });
      $("#account-connect-dialog").close(); alert(result.msg); await loadConnectedAccounts();
    } catch (error) { alert(error.message); } finally { submit.disabled = false; submit.textContent = "保存连接"; }
  });
  $("#player-quality-trigger").addEventListener("click", (event) => {
    event.stopPropagation();
    const menu = $("#player-quality-menu");
    const open = menu.hidden;
    menu.hidden = !open;
    event.currentTarget.setAttribute("aria-expanded", String(open));
  });
  $("#player-quality-menu").addEventListener("click", async (event) => {
    const option = event.target.closest("[data-quality]");
    if (!option) return;
    await setPlayerQuality(option.dataset.quality);
  });
  document.addEventListener("click", (event) => {
    const picker = $(".player-quality-picker");
    if (!picker.contains(event.target)) {
      $("#player-quality-menu").hidden = true;
      $("#player-quality-trigger").setAttribute("aria-expanded", "false");
    }
  });
  $("#player-quality-trigger").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      $("#player-quality-menu").hidden = true;
      event.currentTarget.setAttribute("aria-expanded", "false");
    }
  });
  $("#search-form").addEventListener("submit", (event) => { event.preventDefault(); search(); });
  $("#hero-search").addEventListener("click", () => $("#search-input").focus());
  document.querySelectorAll(".source-tab").forEach((button) => button.addEventListener("click", () => { state.source = button.dataset.source; document.querySelectorAll(".source-tab").forEach((tab) => tab.classList.toggle("active", tab === button)); $("#source-label").textContent = label(state.source); }));
  $("#mobile-menu-button").addEventListener("click", () => {
    const open = !document.body.classList.contains("mobile-menu-open");
    setMobileMenu(open);
  });
  $("#mobile-sidebar-backdrop").addEventListener("click", closeMobileMenu);
  window.matchMedia("(max-width: 860px)").addEventListener("change", (event) => { if (!event.matches) closeMobileMenu(); });
  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
    show(button.dataset.view);
    closeMobileMenu();
  }));
  $("#daily-refresh").addEventListener("click", () => loadDailyRecommendations(true));
  $("#daily-playlist-refresh").addEventListener("click", () => loadDailyPlaylists(true));
  $("#daily-playlist-close").addEventListener("click", closeDailyPlaylistDetail);
  $("#daily-playlist-save").addEventListener("click", saveDailyPlaylist);
  $("#daily-playlist-play-all").addEventListener("click", () => {
    if (state.dailyPlaylistTracks.length) play(state.dailyPlaylistTracks[0], state.dailyPlaylistTracks);
  });
  $("#play-button").addEventListener("click", () => { if (state.current) audio.paused ? audio.play() : audio.pause(); });
  $("#previous-button").addEventListener("click", () => move(-1));
  $("#next-button").addEventListener("click", () => move(1));
  $("#shuffle-button").addEventListener("click", toggleShuffle);
  $("#repeat-list-button").addEventListener("click", () => setRepeatMode("all"));
  $("#repeat-one-button").addEventListener("click", () => setRepeatMode("one"));
  $("#queue-button").addEventListener("click", toggleQueue);
  $("#queue-close").addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    minimizeQueue();
  });
  $("#queue-panel").addEventListener("click", (event) => {
    if (event.target.closest("#queue-close")) minimizeQueue();
  });
  $("#queue-clear").addEventListener("click", clearQueue);
  $("#like-button").addEventListener("click", () => state.current && like(state.current));
  $("#player-add").addEventListener("click", () => openAddDialog(state.current));
  $("#now-playing-trigger").addEventListener("click", openNowPlaying);
  $("#now-close").addEventListener("click", closeNowPlaying);
  $("#now-queue-button").addEventListener("click", toggleQueue);
  $("#now-play-button").addEventListener("click", () => { if (state.current) audio.paused ? audio.play() : audio.pause(); });
  $("#now-previous-button").addEventListener("click", () => move(-1));
  $("#now-next-button").addEventListener("click", () => move(1));
  $("#now-shuffle-button").addEventListener("click", toggleShuffle);
  $("#now-repeat-list-button").addEventListener("click", () => setRepeatMode("all"));
  $("#now-repeat-one-button").addEventListener("click", () => setRepeatMode("one"));
  $("#now-like").addEventListener("click", () => state.current && like(state.current));
  $("#now-add").addEventListener("click", () => openAddDialog(state.current));
  $("#volume-input").addEventListener("input", (event) => { const v = event.target.value / 100; audioA.volume = v; audioB.volume = v; });
  $("#progress-input").addEventListener("input", (event) => { if (audio.duration) audio.currentTime = audio.duration * event.target.value / 100; });
  $("#now-progress-input").addEventListener("input", (event) => { if (audio.duration) audio.currentTime = audio.duration * event.target.value / 100; });
  function bindAudioEvents(el) {
    el.addEventListener("play", () => { if (el !== audio || state.keepAlive) return; state.pendingResume = false; syncPlaybackUi(); setMediaSessionPlaybackState(); });
    el.addEventListener("pause", () => { if (el !== audio || state.keepAlive) return; syncPlaybackUi(); setMediaSessionPlaybackState(); });
    el.addEventListener("loadedmetadata", () => { if (el === audio) { syncProgress(); syncMediaSessionPosition(); } });
    el.addEventListener("timeupdate", () => { if (el === audio) syncProgress(); });
    el.addEventListener("ended", () => {
      if (state.keepAlive || el.loop) return;
      if (el !== audio) {
        // The active pointer flips before the new element confirms playback, so
        // a late "ended" from the retired element must still advance the queue
        // when nothing else is playing.  Re-adopt it so the prefetch handoff
        // keeps using the correct idle counterpart.
        if (state.switchingPlayback || !audio.paused) return;
        audio = el;
      }
      handleEnded();
    });
    el.addEventListener("error", async () => {
      if (el !== audio) return;
      if (state.keepAlive) return;
      const song = state.current;
      const attempt = state.audioAttempt;
      if (!song || !audio.src || state.recoveryInProgress || attempt !== state.playbackAttempt || audio.src !== state.audioUrl) return;
      const recovered = await recoverPlayback(song);
      if (attempt !== state.playbackAttempt || state.current !== song) return;
      if (!recovered) handleUnavailable(song, new Error("音频地址加载失败"));
    });
  }
  bindAudioEvents(audioA);
  bindAudioEvents(audioB);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    if (!state.current || state.keepAlive || !state.pendingResume) return;
    if (audio.paused && state.audioUrl && audio.src) {
      audio.play().then(() => {
        state.pendingResume = false;
        syncPlaybackUi();
        setMediaSessionPlaybackState();
      }).catch(() => {});
    }
  });
  if ("mediaSession" in navigator) {
    const mediaActions = {
      play: () => audio.play().catch(() => {}),
      pause: () => audio.pause(),
      stop: () => audio.pause(),
      nexttrack: () => move(1),
      previoustrack: () => move(-1),
      seekbackward: (details) => { audio.currentTime = Math.max(0, audio.currentTime - ((details && details.seekOffset) || 10)); },
      seekforward: (details) => { if (Number.isFinite(audio.duration)) audio.currentTime = Math.min(audio.duration, audio.currentTime + ((details && details.seekOffset) || 10)); },
      seekto: (details) => { if (details && Number.isFinite(details.seekTime)) audio.currentTime = details.seekTime; },
    };
    Object.entries(mediaActions).forEach(([action, handler]) => {
      try { navigator.mediaSession.setActionHandler(action, handler); } catch (_) {}
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (document.body.classList.contains("mobile-menu-open")) {
      closeMobileMenu();
      return;
    }
    if (!$("#now-playing-view").hidden) closeNowPlaying();
    if (!$("#queue-panel").hidden) minimizeQueue();
  });
  $("#lyrics-window-button").addEventListener("click", () => { openInlineLyrics(); });
  $("#inline-lyrics-close").addEventListener("click", closeInlineLyrics);
  $("#inline-lyrics-lock").addEventListener("click", () => { inlineLyricsSettings.locked = !inlineLyricsSettings.locked; saveInlineLyricsSettings(); applyInlineLyricsSettings(); });
  $("#inline-lyrics-offset-decrease").addEventListener("click", () => updateInlineLyricsOffset(-.1));
  $("#inline-lyrics-offset-increase").addEventListener("click", () => updateInlineLyricsOffset(.1));
  $("#inline-lyrics-settings").addEventListener("click", () => {
    if (inlineLyricsSettings.locked) return;
    fillInlineLyricsSettingsForm();
    $("#inline-lyrics-settings-dialog").showModal();
  });
  $("#inline-lyrics-settings-form").addEventListener("submit", saveInlineLyricsSettingsForm);
  $("#inline-setting-font-size").addEventListener("input", (event) => { $("#inline-setting-font-size-output").textContent = `${event.target.value}px`; });
  $("#inline-lyrics-reset-settings").addEventListener("click", () => { inlineLyricsSettings = { ...inlineLyricsDefaults, locked: inlineLyricsSettings.locked }; fillInlineLyricsSettingsForm(); });
  $("#inline-floating-lyrics").addEventListener("pointerdown", (event) => {
    if (inlineLyricsSettings.locked || event.button !== 0 || event.target.closest("button,input,select,label")) return;
    const overlay = $("#inline-floating-lyrics");
    const rect = overlay.getBoundingClientRect();
    inlineLyricsDrag = { pointerId: event.pointerId, originX: event.clientX, originY: event.clientY, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    overlay.classList.add("is-dragging");
    overlay.setPointerCapture?.(event.pointerId);
    event.preventDefault();
  });
  $("#inline-floating-lyrics").addEventListener("pointermove", (event) => {
    if (!inlineLyricsDrag || event.pointerId !== inlineLyricsDrag.pointerId) return;
    inlineLyricsPosition = clampInlineLyricsPosition({ x: inlineLyricsDrag.x + event.clientX - inlineLyricsDrag.originX, y: inlineLyricsDrag.y + event.clientY - inlineLyricsDrag.originY });
    applyInlineLyricsPosition();
  });
  function finishInlineLyricsDrag(event) {
    if (!inlineLyricsDrag || event.pointerId !== inlineLyricsDrag.pointerId) return;
    inlineLyricsDrag = null;
    $("#inline-floating-lyrics").classList.remove("is-dragging");
    try { localStorage.setItem(inlineLyricsPositionKey, JSON.stringify(inlineLyricsPosition)); } catch (_) {}
  }
  $("#inline-floating-lyrics").addEventListener("pointerup", finishInlineLyricsDrag);
  $("#inline-floating-lyrics").addEventListener("pointercancel", finishInlineLyricsDrag);
  window.addEventListener("resize", () => { if (!$("#inline-floating-lyrics").hidden) applyInlineLyricsPosition(); });
  $("#settings-button").addEventListener("click", () => { $("#api-key-input").value = state.key; $("#theme-select").value = state.theme; $("#settings-dialog").showModal(); });
  $("#save-settings").addEventListener("click", () => {
    state.key = $("#api-key-input").value.trim();
    localStorage.setItem("sound-island-key", state.key);
    applyTheme($("#theme-select").value);
    localStorage.setItem("sound-island-theme", state.theme);
  });
  $("#playback-source").value = state.strategy;
  $("#playback-source").addEventListener("change", (event) => { state.strategy = event.target.value; localStorage.setItem("sound-island-playback-source", state.strategy); });
  $("#playlist-form").addEventListener("submit", addCurrentToPlaylist);
  $("#new-playlist").addEventListener("click", () => {
    $("#name-dialog-title").textContent = "新建歌单"; $("#name-form").dataset.mode = "create"; $("#playlist-name-input").value = ""; $("#name-dialog").showModal();
  });
  $("#rename-playlist").addEventListener("click", () => {
    const playlist = state.playlists.find((p) => p.id === state.activePlaylist);
    if (!playlist) return;
    $("#name-dialog-title").textContent = "重命名歌单"; $("#name-form").dataset.mode = "rename"; $("#playlist-name-input").value = playlist.name; $("#name-dialog").showModal();
  });
  $("#playlist-search-toggle").addEventListener("click", () => {
    const panel = $("#playlist-search-panel");
    if (panel.hidden) setPlaylistSearchOpen(true);
    else {
      resetPlaylistSearch();
      state.playlistSearchPlaylistId = state.activePlaylist;
      renderLibrary("playlist");
    }
  });
  $("#playlist-search-input").addEventListener("input", (event) => {
    state.playlistSearchQuery = event.target.value;
    renderLibrary("playlist");
  });
  $("#playlist-search-input").addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    resetPlaylistSearch();
    state.playlistSearchPlaylistId = state.activePlaylist;
    renderLibrary("playlist");
    $("#playlist-search-toggle").focus();
  });
  $("#playlist-search-clear").addEventListener("click", () => {
    state.playlistSearchQuery = "";
    $("#playlist-search-input").value = "";
    renderLibrary("playlist");
    $("#playlist-search-input").focus();
  });
  $("#name-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("#playlist-name-input").value.trim();
    try {
      const mode = $("#name-form").dataset.mode;
      if (mode === "rename") await playlistRequest(`/api/playlists/${encodeURIComponent(state.activePlaylist)}`, "PATCH", { name });
      else {
        const data = await playlistRequest("/api/playlists", "POST", { name });
        state.activePlaylist = data.playlist.id;
      }
      $("#name-dialog").close(); await refreshPlaylists(); show("playlist");
    } catch (error) { alert(error.message); }
  });
  $("#delete-playlist").addEventListener("click", async () => {
    if (!state.activePlaylist || !confirm("确定删除这个歌单吗？")) return;
    try {
      await playlistRequest(`/api/playlists/${encodeURIComponent(state.activePlaylist)}`, "DELETE");
      state.activePlaylist = "liked"; await refreshPlaylists(); show("liked");
    } catch (error) { alert(error.message); }
  });
  $("#import-link-playlist").addEventListener("click", () => {
    $("#import-link-input").value = "";
    $("#import-link-name").value = "";
    $("#import-link-dialog").showModal();
    $("#import-link-input").focus();
  });
  $("#close-import-link").addEventListener("click", () => $("#import-link-dialog").close());
  $("#import-link-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = $("#import-link-input").value.trim();
    const name = $("#import-link-name").value.trim();
    const submit = $("#import-link-submit");
    if (!url) return $("#import-link-input").focus();
    submit.disabled = true;
    submit.textContent = "正在导入";
    try {
      const result = await playlistRequest("/api/playlists/import-link", "POST", { url, name });
      state.activePlaylist = result.playlist.id;
      $("#import-link-dialog").close();
      await refreshPlaylists();
      show("playlist");
      alert(result.msg || "歌单导入完成");
    } catch (error) {
      alert(`导入失败：${error.message}`);
    } finally {
      submit.disabled = false;
      submit.textContent = "导入";
    }
  });
  $("#import-playlist").addEventListener("click", () => $("#playlist-file").click());
  $("#playlist-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const result = await playlistRequest("/api/playlists/import", "POST", { data });
      state.playlists = result.playlists || state.playlists;
      renderPlaylistNav(); counts(); alert(result.msg || "导入完成");
    } catch (error) { alert(`导入失败：${error.message}`); }
    event.target.value = "";
  });
  const accountPlatforms = [
    { id: "netease", name: "\u7f51\u6613\u4e91\u97f3\u4e50", detail: "\u53ef\u626b\u7801\u6388\u6743\u5e76\u540c\u6b65\u8d26\u6237\u6b4c\u5355", qr: true },
    { id: "qq", name: "QQ \u97f3\u4e50", detail: "\u53ef\u626b\u7801\u6388\u6743\u5e76\u540c\u6b65\u8d26\u6237\u6b4c\u5355", qr: true },
    { id: "kugou", name: "\u9177\u72d7\u97f3\u4e50", detail: "\u53ef\u626b\u7801\u6388\u6743\u5e76\u540c\u6b65\u8d26\u6237\u6b4c\u5355", qr: true },
    { id: "qishui", name: "\u6c7d\u6c34\u97f3\u4e50", detail: "\u4f7f\u7528\u6c7d\u6c34\u97f3\u4e50 App \u626b\u7801\u6388\u6743\u5e76\u540c\u6b65\u8d26\u6237\u6b4c\u5355", qr: true },
  ];
  async function accountRequest(url, method = "GET", body) {
    const response = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined, cache: "no-store" });
    let data = {};
    try { data = await response.json(); } catch (_) {}
    if (response.status === 401) {
      location.replace("/login?next=/music");
      throw new Error("\u767b\u5f55\u72b6\u6001\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55");
    }
    if (!response.ok || data.code !== 0) throw new Error(data.msg || data.detail || `\u8d26\u6237\u64cd\u4f5c\u5931\u8d25\uff08${response.status}\uff09`);
    return data;
  }
  function accountStatus(account) {
    const statusText = ({ active: "已连接", needs_reauth: "需要重新授权", unsupported: "暂不可用" })[account?.status] || "未连接";
    return `<span class="account-status ${esc(account?.status || "empty")}">${esc(statusText)}</span>`;
  }
  function renderAccounts() {
    const target = $("#accounts-grid");
    target.innerHTML = accountPlatforms.map((platform) => {
      const account = state.connectedAccounts.find((item) => item.platform === platform.id);
      const lastSynced = account?.last_synced_at ? ` · 上次同步 ${new Date(account.last_synced_at).toLocaleString()}` : "";
      const summary = account ? `${esc(account.nickname || platform.name)}${lastSynced}` : esc(platform.detail);
      const actions = account
        ? `<button class="text-button" data-account-action="sync" data-account-id="${esc(account.id)}">\u540c\u6b65\u6b4c\u5355</button><button class="text-button danger" data-account-action="disconnect" data-account-id="${esc(account.id)}">\u65ad\u5f00</button>`
        : `<button class="text-button" data-account-action="cookie" data-platform="${platform.id}">\u5bfc\u5165\u4f1a\u8bdd</button>${platform.qr ? `<button class="text-button" data-account-action="qr" data-platform="${platform.id}">\u626b\u7801\u767b\u5f55</button>` : ""}`;
      return `<article class="account-card"><div class="account-card-head"><div><span class="account-platform-mark">${esc(platform.name.slice(0, 1))}</span><div><h3>${esc(platform.name)}</h3><p>${summary}</p></div></div>${accountStatus(account)}</div><p class="account-card-message">${esc(account?.status_message || "授权后将第三方歌单同步到当前声屿账户")}</p><div class="account-actions-row">${actions}</div></article>`;
    }).join("");
    target.querySelectorAll("[data-account-action]").forEach((button) => button.addEventListener("click", () => runAccountAction(button)));
  }
  async function loadConnectedAccounts() {
    try {
      const data = await accountRequest("/api/accounts");
      state.connectedAccounts = data.accounts || [];
      $("#accounts-meta").textContent = state.connectedAccounts.length ? `已连接 ${state.connectedAccounts.length} 个音乐账户` : "连接音乐平台后，同步歌单到本账户";
    } catch (error) {
      state.connectedAccounts = [];
      $("#accounts-meta").textContent = error.message;
    }
    renderAccounts();
  }
  function openAccountConnect(platform, method = "cookie") {
    const info = accountPlatforms.find((item) => item.id === platform);
    const form = $("#account-connect-form");
    form.dataset.platform = platform;
    form.dataset.method = method;
    $("#account-connect-heading").textContent = `${info?.name || "音乐"}${method === "password" ? "账号登录" : "账户连接"}`;
    $("#account-connect-cookie").value = "";
    $("#account-connect-username").value = "";
    $("#account-connect-password").value = "";
    document.querySelectorAll(".account-password-field").forEach((item) => { item.hidden = method !== "password"; });
    $(".account-cookie-field").hidden = method === "password";
    $("#account-connect-help").textContent = method === "password" ? "平台未提供稳定可验证的网页登录协议，提交后不会保存密码，并会提示可用授权方式。" : "请粘贴已完成扫码或网页登录的完整 Cookie 会话。";
    $("#account-connect-dialog").showModal();
  }
  async function runAccountAction(button) {
    const action = button.dataset.accountAction;
    if (action === "cookie" || action === "password") return openAccountConnect(button.dataset.platform, action);
    if (action === "qr") return startAccountQr(button.dataset.platform);
    if (action === "sync") {
      button.disabled = true; button.textContent = "同步中";
      try { const data = await accountRequest(`/api/accounts/${encodeURIComponent(button.dataset.accountId)}/sync`, "POST"); alert(data.msg); await refreshPlaylists(); await loadConnectedAccounts(); }
      catch (error) { alert(error.message); await loadConnectedAccounts(); }
      return;
    }
    if (action === "disconnect") {
      if (!confirm("断开后已同步的本地歌单会保留，确定继续吗？")) return;
      try { const data = await accountRequest(`/api/accounts/${encodeURIComponent(button.dataset.accountId)}`, "DELETE"); alert(data.msg); await loadConnectedAccounts(); }
      catch (error) { alert(error.message); }
    }
  }

  const playerQualityOptions = [
    { id: "128k", name: "128k 标准" },
    { id: "320k", name: "320k 高品质" },
    { id: "flac", name: "FLAC 无损" },
    { id: "hires", name: "Hi-Res（可用时）" },
  ];
  function updatePlayerQualityControl(value) {
    const select = $("#player-quality");
    const triggerValue = $("#player-quality-value");
    const option = playerQualityOptions.find((item) => item.id === value) || { id: value, name: value };
    if (select) select.value = option.id;
    if (triggerValue) triggerValue.textContent = option.name;
    $("#player-quality-menu")?.querySelectorAll("[data-quality]").forEach((item) => {
      const active = item.dataset.quality === option.id;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
  }
  function renderPlayerQualityOptions(options) {
    const menu = $("#player-quality-menu");
    const select = $("#player-quality");
    const normalized = (options || playerQualityOptions).filter((item) => ["128k", "320k", "flac", "hires"].includes(item.id));
    const usable = normalized.length ? normalized : playerQualityOptions;
    menu.innerHTML = usable.map((item) => `<button type="button" role="option" data-quality="${esc(item.id)}" aria-selected="false"><span>${esc(item.name)}</span><span class="player-quality-check" aria-hidden="true">✓</span></button>`).join("");
    select.innerHTML = usable.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
    updatePlayerQualityControl(state.quality);
  }
  async function setPlayerQuality(nextQuality) {
    if (!["128k", "320k", "flac", "hires"].includes(nextQuality)) return;
    state.quality = nextQuality;
    localStorage.setItem("sound-island-player-quality", nextQuality);
    updatePlayerQualityControl(nextQuality);
    $("#player-quality-menu").hidden = true;
    $("#player-quality-trigger").setAttribute("aria-expanded", "false");
    if (!state.current) return;
    const song = state.current;
    cancelPendingPlayback();
    cancelCurrentAudio();
    state.resolvedBy = "";
    state.resolvedKind = "";
    state.resolvedSong = null;
    state.playbackRetrySources.clear();
    try { await resolveAndStart(song); }
    catch (error) { if (error.name !== "AbortError" && state.current === song) handleUnavailable(song, error); }
  }
  async function loadPlaybackSources() {
    try {
      const response = await fetch(`/api/player/sources?${query({})}`);
      const data = await response.json();
      const options = data?.sources?.playback || [];
      const qualities = data?.qualities || [];
      const configuredQuality = data?.quality || "320k";
      renderPlayerQualityOptions(qualities.length ? qualities : playerQualityOptions);
      state.quality = ["128k", "320k", "flac", "hires"].some((item) => item === state.quality) ? state.quality : configuredQuality;
      updatePlayerQualityControl(state.quality);
      const select = $("#playback-source");
      select.innerHTML = options.map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
      select.value = options.some((item) => item.id === state.strategy) ? state.strategy : "auto";
      state.strategy = select.value;
    } catch (_) {}
  }
  async function loadSession() {
    const response = await fetch("/api/auth/me");
    const data = await response.json();
    if (!response.ok || !data.user) {
      location.replace("/login?next=/music");
      return false;
    }
    state.user = data.user;
    $("#account-name").textContent = data.user.display_name || data.user.username;
    const admin = data.user.role === "admin";
    $("#admin-sidebar-link").hidden = !admin;
    $("#admin-settings-link").hidden = !admin;
    return true;
  }
  const presenceClientId = (() => {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll("-", "");
    if (globalThis.crypto?.getRandomValues) {
      const bytes = new Uint8Array(18);
      globalThis.crypto.getRandomValues(bytes);
      return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
    }
    return `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}fallback`;
  })();
  function renderOnlineCount(value) {
    const count = $("#online-count");
    const presence = $("#online-presence");
    if (!count || !presence) return;
    const online = Number(value);
    count.textContent = Number.isFinite(online) && online >= 0 ? String(Math.floor(online)) : "--";
    presence.classList.toggle("is-unavailable", !Number.isFinite(online));
  }
  async function sendPresenceHeartbeat() {
    try {
      const response = await fetch("/api/presence/heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: presenceClientId }),
        cache: "no-store",
      });
      const data = await response.json();
      if (!response.ok || data.code !== 0) throw new Error(data.detail || data.msg || "在线状态更新失败");
      renderOnlineCount(data.online);
    } catch (_) {
      renderOnlineCount(NaN);
    }
  }
  function leaveOnlinePresence() {
    if (state.presenceTimer !== null) {
      window.clearInterval(state.presenceTimer);
      state.presenceTimer = null;
    }
    const url = `/api/presence/leave?client_id=${encodeURIComponent(presenceClientId)}`;
    if (navigator.sendBeacon) navigator.sendBeacon(url, new Blob([], { type: "text/plain" }));
    else fetch(url, { method: "POST", keepalive: true }).catch(() => {});
  }
  function startOnlinePresence() {
    sendPresenceHeartbeat();
    if (state.presenceTimer === null) state.presenceTimer = window.setInterval(sendPresenceHeartbeat, 30000);
  }
  window.addEventListener("pagehide", leaveOnlinePresence);
  window.addEventListener("pageshow", () => { if (state.user) startOnlinePresence(); });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && state.user) sendPresenceHeartbeat(); });
  $("#logout-button").addEventListener("click", async () => {
    try {
      await fetch(`/api/presence/leave?client_id=${encodeURIComponent(presenceClientId)}`, { method: "POST", keepalive: true });
      await fetch("/api/auth/logout", { method: "POST" });
    } finally { location.replace("/login"); }
  });
  async function bootstrap() {
    if (!await loadSession()) return;
    localStorage.removeItem("sound-island-recent");
    counts();
    renderLibrary("liked");
    art();
    syncNowPlaying();
    syncPlaybackUi();
    syncProgress();
    renderQueue();
    await loadPlaylists();
    loadPlaybackSources();
    loadAnnouncement();
    startOnlinePresence();
  }
  bootstrap();
})();

