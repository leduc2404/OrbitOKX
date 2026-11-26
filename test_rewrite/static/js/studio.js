/* ==========================================================================
   OKXSquare Prompt Tuning Lab — Frontend Controller (v3 Final)
   ========================================================================== */
(() => {
  "use strict";

  const CLIENT_SENSITIVE_RE = [
    /\b(trump|biden|harris|putin|zelensky|netanyahu|xi jinping|israel|iran|gaza|palestine|hamas|ukraine|russia|taiwan)\b/gi,
    /\b(president|prime minister|white house|kremlin|election|elections|parliament|congress|senate|geopolitical|geopolitics|central bank|fed)\b/gi,
    /\b(tổng thống|bầu cử|chính phủ|nghị viện|quốc hội|chính trị|địa chính trị|chiến tranh|xung đột|ngân hàng trung ương|trung quốc|nước mỹ|hoa kỳ)\b/gi,
    /\b(ico|ido|ieo|airdrop|giveaway|huy động vốn|đa cấp|rửa tiền|lừa đảo|bơm xả|pump\s*&\s*dump)\b/gi,
  ];

  const SAMPLES = {
    crypto: `BTC/USDT Technical Update:
Bitcoin is testing key liquidity near 91,450 after sweeping stops below 89,800 on the 4H chart. Spot CVD shows aggressive bid absorption while perp funding cooled to +0.008%.
Watching zone: 90,200 - 90,800
Targets: 93,400 / 95,100
Invalidation: 88,900
Meanwhile $SOL reclaimed 184 with 2.4x average hourly volume as rotation flows back into major Layer 1 tokens.`,
    sensitive: `Breaking: President Trump commented today on Fed interest rate cuts and US-China trade tariffs, triggering sharp volatility across crypto markets and gold reserves as central banks reassess inflation risks. Plus join our huge token airdrop giveaway at https://t.me/example!`,
  };

  const CONCURRENCY_OPTIONS = [1, 2, 3, 5, 8, 10, 15, 20];

  const state = {
    mode: "single", // single | matrix | rotate
    config: null,
    prompts: null,
    promptsById: {},
    promptDrafts: {},
    checkedPrompts: new Set(),
    promptLastScores: {},

    storage: null,
    posts: [],
    selectedPostId: null,
    selectedPostFull: null,
    activeHistoryIndex: 0,
    singleVariantId: "",
    origDirty: false,

    // Mode 2 (1 post x N prompts)
    matrixPostId: "",
    matrixSourceText: "",
    matrixSourceLabel: "",
    matrixItems: [],
    matrixAnalytics: null,
    matrixFilter: "all",
    matrixSort: "order",
    matrixCols: "1",
    matrixHighlightPair: null,

    // Mode 3 (N posts x rotate 20 prompts)
    rotateItems: [],
    rotateAnalytics: null,
    rotateFilter: "all",

    // Slide-over prompt editor state
    drawerTarget: null,
    drawerTab: "result",
    drawerPrevOutput: "",

    // Jobs & Logs
    activeJobId: null,
    pollTimer: null,
    isRewriting: false,
    logs: [],
    logFilterLevel: "ALL",
    logSearch: "",
    eventSource: null,
  };

  const $ = (id) => document.getElementById(id);

  // =========================================================================
  // Helpers
  // =========================================================================
  async function api(url, opts = {}) {
    const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts });
    let data = null;
    try { data = await res.json(); }
    catch { data = { ok: false, error: `HTTP ${res.status}` }; }
    return { status: res.status, data };
  }

  function toast(msg, kind = "ok") {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    $("toastStack").appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  function esc(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function highlightTickers(text) {
    return esc(text).replace(/(\$[A-Z0-9]{2,12})/g, '<span class="ticker-hl">$1</span>');
  }

  function wc(text) {
    const t = (text || "").trim();
    return t ? t.split(/\s+/).length : 0;
  }

  function fmtBytes(b) {
    if (!b) return "0 B";
    if (b < 1024) return `${b} B`;
    return `${(b / 1024).toFixed(1)} KB`;
  }

  function getAllPromptsOrdered() {
    if (!state.prompts) return [];
    return [...(state.prompts.group1 || []), ...(state.prompts.group2 || [])];
  }

  function getPromptContent(vid) {
    if (vid in state.promptDrafts) return state.promptDrafts[vid];
    return state.promptsById[vid]?.content || "";
  }

  function isPromptDirty(vid) {
    if (!(vid in state.promptDrafts)) return false;
    return state.promptDrafts[vid] !== (state.promptsById[vid]?.content || "");
  }

  function checkSafetyText(text, badgeEl, statsEl) {
    statsEl.textContent = `${wc(text)} từ · ${text.length} ký tự`;
    if (!text.trim()) {
      badgeEl.className = "safety-badge neutral";
      badgeEl.textContent = "Chưa có nội dung";
      return;
    }
    const hits = new Set();
    CLIENT_SENSITIVE_RE.forEach((re) => {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(text)) !== null) hits.add(m[0].toLowerCase());
    });
    if (hits.size > 0) {
      badgeEl.className = "safety-badge warn";
      badgeEl.textContent = `⚠ Pre-screen nhạy cảm: ${[...hits].slice(0, 5).join(", ")}`;
    } else {
      badgeEl.className = "safety-badge clean";
      badgeEl.textContent = "✓ Pre-screen sạch";
    }
  }

  // =========================================================================
  // Mode Switching
  // =========================================================================
  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll("#modeNav .mode-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    document.querySelectorAll(".mode-ctrl, .mode-cta").forEach((el) => el.classList.toggle("active", el.dataset.forMode === mode));
    $("viewModeSingle").classList.toggle("active", mode === "single");
    $("viewModeMatrix").classList.toggle("active", mode === "matrix");
    $("viewModeRotate").classList.toggle("active", mode === "rotate");

    if (mode === "matrix") syncMatrixSourcePreview();
    if (mode === "rotate") {
      updateRotateReadiness();
      if (!state.rotateItems.length) renderRotateView();
    }
  }

  // =========================================================================
  // Config & Concurrency Sync
  // =========================================================================
  function setConcurrencyValue(rawVal) {
    const val = Math.max(1, Math.min(20, Number(rawVal) || 5));
    $("cfgConcurrency").value = String(val);

    // Pick exact or nearest option in #ribbonConcurrency
    const sel = $("ribbonConcurrency");
    let bestOpt = CONCURRENCY_OPTIONS[0];
    let minDiff = Infinity;
    for (const opt of CONCURRENCY_OPTIONS) {
      const diff = Math.abs(opt - val);
      if (diff < minDiff) {
        minDiff = diff;
        bestOpt = opt;
      }
    }
    sel.value = String(bestOpt);
  }

  async function loadConfigAndPrompts() {
    const [cfgRes, prRes] = await Promise.all([api("/api/config"), api("/api/prompts")]);
    if (cfgRes.data.ok) {
      state.config = cfgRes.data.config;
      populateProviderUI(state.config.active_provider);
    } else {
      toast(`Lỗi đọc config: ${cfgRes.data.error}`, "err");
    }
    if (prRes.data.ok) {
      applyPromptsCatalog(prRes.data.prompts, true);
    }
  }

  function populateProviderUI(providerName) {
    if (!state.config) return;
    const prov = state.config.providers[providerName] || state.config.providers[state.config.active_provider];
    if (!prov) return;

    $("cfgProvider").value = prov.name;
    $("cfgModel").value = prov.model || "";
    $("cfgEndpoint").value = prov.endpoint || "";
    $("cfgEndpoint").disabled = prov.protocol !== "openai_compatible";

    const g = prov.generation || {};
    setGenNumbers(g.temperature ?? 0.65, g.top_p ?? 0.8, g.top_k ?? 40, g.max_output_tokens ?? 2048);
    $("cfgTimeout").value = prov.processing?.request_timeout ?? 60;

    // Pick smart concurrency default from YAML
    const yamlWorkers = Number(prov.processing?.max_workers);
    const defaultConc = yamlWorkers > 0 ? Math.min(yamlWorkers, 20) : (prov.name === "gemma" ? 8 : 5);
    setConcurrencyValue(defaultConc);

    const dl = $("modelSuggestions");
    dl.innerHTML = "";
    const suggested = new Set([prov.model]);
    (prov.models || []).forEach((m) => m.name && suggested.add(m.name));
    suggested.forEach((name) => {
      if (!name) return;
      const opt = document.createElement("option");
      opt.value = name;
      dl.appendChild(opt);
    });

    const keySel = $("cfgKeyIndex");
    keySel.innerHTML = "";
    if (prov.name === "gemini_proxy") {
      const opt = document.createElement("option");
      opt.value = "0";
      opt.textContent = prov.api_key_masked ? `YAML Proxy Key (${prov.api_key_masked})` : "(Thiếu api_key)";
      keySel.appendChild(opt);
      $("cfgKeyCountTag").textContent = prov.has_api_key ? "1 key" : "0 key";
    } else {
      const meta = prov.api_keys_meta || [];
      $("cfgKeyCountTag").textContent = `${meta.length} keys`;
      const autoOpt = document.createElement("option");
      autoOpt.value = "auto";
      autoOpt.textContent = meta.length ? `Auto Round-Robin (${meta.length} keys)` : "(Không có key)";
      keySel.appendChild(autoOpt);
      meta.forEach((k) => {
        const opt = document.createElement("option");
        opt.value = String(k.index);
        opt.textContent = `#${k.index + 1} · ${k.label} (${k.masked})`;
        keySel.appendChild(opt);
      });
    }

    const isYamlActive = prov.name === state.config.active_provider;
    $("yamlActiveTag").textContent = isYamlActive
      ? "★ active_provider trong gemini_config.yaml"
      : `${prov.name} (${prov.enabled ? "enabled" : "disabled"} trong YAML)`;
    $("yamlMetaInline").textContent = `${prov.protocol} · retry ${prov.processing?.retry_attempts ?? 3}x`;

    $("headerProviderLabel").textContent = `${prov.name} · ${prov.model || "?"}`;
    $("pingDot").className = "status-dot idle";
    $("headerPingLatency").textContent = "";
    updateRibbonParamSummary();
  }

  function setGenNumbers(temp, topP, topK, maxTok) {
    $("cfgTemp").value = temp;
    $("rangeTemp").value = temp;
    $("valTempLabel").textContent = Number(temp).toFixed(2);
    $("cfgTopP").value = topP;
    $("rangeTopP").value = topP;
    $("valTopPLabel").textContent = Number(topP).toFixed(2);
    $("cfgTopK").value = topK;
    $("cfgMaxTokens").value = maxTok;
    updateRibbonParamSummary();
  }

  function updateRibbonParamSummary() {
    $("ribbonParamSummary").textContent = `T=${$("cfgTemp").value} · P=${$("cfgTopP").value}`;
    $("headerProviderLabel").textContent = `${$("cfgProvider").value} · ${$("cfgModel").value || "?"}`;
  }

  function collectRuntimePayload() {
    return {
      provider: $("cfgProvider").value,
      enable_prescreen: $("cfgEnablePrescreen").checked,
      enable_clean_output: $("cfgEnableCleanOutput").checked,
      concurrency: Math.max(1, Math.min(20, Number($("cfgConcurrency").value) || 5)),
      overrides: {
        model: $("cfgModel").value.trim(),
        endpoint: $("cfgEndpoint").value.trim(),
        key_index: $("cfgKeyIndex").value,
        api_key_override: $("cfgApiKeyOverride").value.trim(),
        temperature: $("cfgTemp").value,
        top_p: $("cfgTopP").value,
        top_k: $("cfgTopK").value,
        max_output_tokens: $("cfgMaxTokens").value,
        request_timeout: $("cfgTimeout").value,
      },
    };
  }

  function collectActiveDraftOverrides() {
    const out = {};
    for (const [vid, txt] of Object.entries(state.promptDrafts)) {
      if (isPromptDirty(vid)) out[vid] = txt;
    }
    return out;
  }

  // =========================================================================
  // Prompt Explorer Rail & Direct Inline Editor
  // =========================================================================
  function applyPromptsCatalog(catalog, initSelection = false) {
    state.prompts = catalog;
    state.promptsById = {};
    const all = getAllPromptsOrdered();
    all.forEach((p) => { state.promptsById[p.id] = p; });

    if (initSelection) {
      state.checkedPrompts = new Set(all.map((p) => p.id));
      if (!state.singleVariantId && all.length) state.singleVariantId = all[0].id;
    }

    populateSinglePromptDropdown();
    renderPromptRail();
    loadInlinePromptEditor(state.singleVariantId);
    updateCheckedPromptLabels();
    renderRotateCoverageDots();
  }

  function populateSinglePromptDropdown() {
    const sel = $("singlePromptSelect");
    sel.innerHTML = "";
    const addGroup = (label, list) => {
      if (!list?.length) return;
      const og = document.createElement("optgroup");
      og.label = label;
      list.forEach((p) => {
        const opt = document.createElement("option");
        opt.value = p.id;
        opt.textContent = `${isPromptDirty(p.id) ? "● " : ""}${p.code} · ${p.angle}`;
        og.appendChild(opt);
      });
      sel.appendChild(og);
    };
    addGroup("Group 1 — Trader thực chiến (75–105t)", state.prompts?.group1);
    addGroup("Group 2 — Dữ liệu & Tâm lý (90–125t)", state.prompts?.group2);
    if (state.singleVariantId && state.promptsById[state.singleVariantId]) {
      sel.value = state.singleVariantId;
    }
  }

  function renderPromptRail() {
    const container = $("promptRailList");
    if (!state.prompts) return;

    const dirtyVids = Object.keys(state.promptDrafts).filter(isPromptDirty);
    $("sidebarDraftCount").textContent = dirtyVids.length > 0 ? `20 · ${dirtyVids.length}*` : String(state.prompts.total || 20);
    const saveAllBtn = $("btnSaveAllDrafts");
    saveAllBtn.classList.toggle("hidden", dirtyVids.length === 0);
    saveAllBtn.textContent = `💾 Lưu nháp (${dirtyVids.length})`;

    const renderGroup = (title, list) => {
      if (!list?.length) return "";
      return `
        <div class="prompt-group-hdr">${esc(title)}</div>
        ${list
          .map((p) => {
            const active = p.id === state.singleVariantId;
            const dirty = isPromptDirty(p.id);
            const checked = state.checkedPrompts.has(p.id);
            const sc = state.promptLastScores[p.id];
            let scoreHtml = "";
            if (sc) {
              if (sc.status === "ok") {
                const cls = sc.score >= 85 ? "good" : sc.score >= 65 ? "mid" : "bad";
                scoreHtml = `<span class="pr-score ${cls}">${sc.score}đ</span>`;
              } else {
                scoreHtml = `<span class="pr-score bad">${sc.status === "rejected" ? "REJ" : "ERR"}</span>`;
              }
            }
            return `
              <div class="prompt-rail-item ${active ? "active" : ""}" data-pvid="${esc(p.id)}">
                <input type="checkbox" data-pchk="${esc(p.id)}" ${checked ? "checked" : ""} />
                <div class="pr-main">
                  <div class="pr-code-line">
                    <span class="pr-code">${esc(p.code)}</span>
                    ${dirty ? '<span class="pr-dirty-dot" title="Có sửa nháp chưa lưu"></span>' : ""}
                    ${p.has_backup ? '<span class="pr-bak" title="Đã sửa trên disk, có backup gốc">bak</span>' : ""}
                  </div>
                  <div class="pr-angle" title="${esc(p.angle)}">${esc(p.angle)}</div>
                </div>
                ${scoreHtml}
              </div>`;
          })
          .join("")}
      `;
    };

    const g1List = state.prompts?.group1 || [];
    const g2List = state.prompts?.group2 || [];
    container.innerHTML =
      renderGroup(`GROUP 1 · TRADER THỰC CHIẾN (${g1List.length})`, g1List) +
      renderGroup(`GROUP 2 · DỮ LIỆU & TÂM LÝ (${g2List.length})`, g2List);
  }

  function updateCheckedPromptLabels() {
    const n = state.checkedPrompts.size;
    const mOpt = $("matrixScopeSelect").querySelector('option[value="checked"]');
    const rOpt = $("rotateScopeSelect").querySelector('option[value="checked"]');
    if (mOpt) mOpt.textContent = `Prompt đang tick chọn (${n})`;
    if (rOpt) rOpt.textContent = `Prompt đang tick chọn (${n})`;
    updateMatrixCtaLabel();
    updateRotateReadiness();
  }

  function resolveSelectedScopeIds(scopeVal) {
    if (scopeVal === "group1") return (state.prompts?.group1 || []).map((p) => p.id);
    if (scopeVal === "group2") return (state.prompts?.group2 || []).map((p) => p.id);
    if (scopeVal === "checked") return getAllPromptsOrdered().filter((p) => state.checkedPrompts.has(p.id)).map((p) => p.id);
    return getAllPromptsOrdered().map((p) => p.id);
  }

  function updateMatrixCtaLabel() {
    const n = resolveSelectedScopeIds($("matrixScopeSelect").value).length;
    $("btnRunMatrix").textContent = `▶ Chạy 1 Bài × ${n} Prompt`;
  }

  function loadInlinePromptEditor(vid) {
    const p = state.promptsById[vid];
    if (!p) return;
    state.singleVariantId = vid;
    $("singlePromptSelect").value = vid;
    $("inlinePeCode").textContent = p.code;
    $("inlinePeAngle").textContent = p.angle;
    const text = getPromptContent(vid);
    $("inlinePeTextarea").value = text;
    updateInlinePeBadges(vid, text);
    renderPromptRail();
    syncSingleOutputForPrompt();
  }

  function syncSingleOutputForPrompt() {
    if (!state.selectedPostFull) return;
    const hist = state.selectedPostFull.rewrite_history || [];
    if (!hist.length) return;
    // Prefer a history item matching currently chosen singleVariantId
    const matchIdx = hist.findIndex((h) => h.variant_id === state.singleVariantId);
    if (matchIdx >= 0) {
      state.activeHistoryIndex = matchIdx;
      renderSingleResult(hist[matchIdx], hist);
    } else {
      const shown = hist[state.activeHistoryIndex] || hist[0];
      renderSingleResult(shown, hist);
    }
  }

  function updateInlinePeBadges(vid, text) {
    const p = state.promptsById[vid];
    if (!p) return;
    const dirty = text !== p.content;
    const hasPlaceholder = text.includes("{text}");
    $("inlinePeDirtyBadge").classList.toggle("hidden", !dirty);
    $("inlinePePlaceholderErr").classList.toggle("hidden", hasPlaceholder);
    $("btnInlineRestoreBackup").classList.toggle("hidden", !p.has_backup);
    $("btnInlineRevertDraft").disabled = !dirty;
    $("btnInlineSavePrompt").disabled = !hasPlaceholder || !dirty;
    $("inlinePeMeta").textContent = `${p.filename} · ${wc(text)} từ · ~${Math.max(1, Math.round(text.length / 4))} tok`;
  }

  async function savePromptToDisk(vid, content) {
    if (!content.includes("{text}")) {
      toast("Prompt bắt buộc phải chứa placeholder {text}!", "err");
      return false;
    }
    const { data } = await api(`/api/prompts/${encodeURIComponent(vid)}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
    if (!data.ok) {
      toast(`Lỗi lưu prompt: ${data.error}`, "err");
      return false;
    }
    delete state.promptDrafts[vid];
    applyPromptsCatalog(data.prompts, false);
    toast(`Đã lưu ${data.prompt.filename} (đã tạo backup gốc tự động)`, "ok");
    return true;
  }

  async function saveAllDirtyDrafts() {
    const dirtyEntries = Object.entries(state.promptDrafts).filter(([vid]) => isPromptDirty(vid));
    if (!dirtyEntries.length) return;
    for (const [vid, txt] of dirtyEntries) {
      if (!txt.includes("{text}")) {
        toast(`Prompt ${state.promptsById[vid]?.code || vid} thiếu {text}, không thể lưu!`, "err");
        return;
      }
    }
    let saved = 0;
    for (const [vid, txt] of dirtyEntries) {
      const { data } = await api(`/api/prompts/${encodeURIComponent(vid)}`, {
        method: "PUT",
        body: JSON.stringify({ content: txt }),
      });
      if (data.ok) {
        delete state.promptDrafts[vid];
        state.prompts = data.prompts;
        saved++;
      }
    }
    applyPromptsCatalog(state.prompts, false);
    toast(`Đã lưu ${saved} prompt nháp xuống ổ cứng`, "ok");
  }

  async function restorePromptFromBackup(vid) {
    if (!confirm(`Khôi phục ${vid} về bản gốc ban đầu trước khi sửa?`)) return false;
    const { data } = await api(`/api/prompts/${encodeURIComponent(vid)}/restore`, { method: "POST" });
    if (!data.ok) {
      toast(`Khôi phục lỗi: ${data.error}`, "err");
      return false;
    }
    delete state.promptDrafts[vid];
    applyPromptsCatalog(data.prompts, false);
    toast(`Đã khôi phục ${data.prompt.filename} về bản gốc`, "ok");
    return true;
  }

  // =========================================================================
  // Storage, Posts Queue & Saved Sessions
  // =========================================================================
  function renderStorageState(info) {
    if (!info) return;
    state.storage = info;
    const mode = info.mode;
    document.querySelectorAll("#storageModeSwitch .seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("headerPostCount").textContent = `${info.total_posts} bài`;
    $("sidebarPostCount").textContent = String(info.total_posts);

    $("storageSummaryBox").innerHTML = `
      <div><strong>Chế độ:</strong> ${mode.toUpperCase()} · <strong>Tổng bài:</strong> ${info.total_posts}</div>
      <div><strong>File ổ cứng:</strong> ${esc(info.disk_path)}</div>
      <div><strong>Kích thước file:</strong> ${info.disk_exists ? fmtBytes(info.disk_bytes) : "Chưa tạo"}</div>
      <div><strong>Production DB:</strong> ${info.prod_db_exists ? "Có sẵn (data/okx_square.db)" : "Không có"}</div>
    `;
    $("btnImportProdDb").disabled = !info.prod_db_exists;
    updateRotateReadiness();
  }

  async function loadPosts(keepSelection = true) {
    const params = new URLSearchParams({
      source: $("filterSource").value,
      q: $("queueSearch").value.trim(),
    });
    const { data } = await api(`/api/posts?${params.toString()}`);
    if (!data.ok) return;
    state.posts = data.posts || [];
    renderStorageState(data.storage);
    renderQueueList();

    if (keepSelection && state.selectedPostId && state.posts.some((p) => p.id === state.selectedPostId)) {
      await selectPost(state.selectedPostId, false);
    } else if (!state.selectedPostId && state.posts.length) {
      await selectPost(state.posts[0].id, true);
    }
    if (!state.rotateItems.length && state.mode === "rotate") {
      renderRotateView();
    }
  }

  function renderQueueList() {
    const container = $("queueList");
    if (!state.posts.length) {
      container.innerHTML = `
        <div class="empty-box">
          <p>Chưa có bài viết.</p>
          <span>Nhấn <strong>Crawl bài</strong> (KuCoin / SoSoValue) hoặc <strong>+ Nhập tay</strong>.</span>
        </div>`;
      return;
    }

    container.innerHTML = state.posts
      .map((p) => {
        const isSel = p.id === state.selectedPostId;
        const rw = p.latest_rewrite;
        let badge = "";
        if (rw?.status === "ok" && rw.audit) badge = `<span class="pr-score ${rw.audit.score >= 85 ? "good" : "mid"}">${rw.audit.score}đ</span>`;
        else if (rw?.status === "rejected") badge = `<span class="pr-score mid">REJ</span>`;
        else if (rw?.status === "error") badge = `<span class="pr-score bad">ERR</span>`;
        return `
          <div class="post-card ${isSel ? "selected" : ""}" data-id="${esc(p.id)}">
            <div class="post-card-top">
              <div class="post-card-left">
                <span class="src-badge ${esc(p.source)}">${esc(p.source)}</span>
                ${badge}
              </div>
              <button type="button" class="post-del-btn" data-del-id="${esc(p.id)}" title="Xóa bài">✕</button>
            </div>
            <p class="post-snippet">${esc(p.text)}</p>
            <div class="post-card-foot">
              <span>${esc(p.crawled_from || p.source)}</span>
              <span>${wc(p.text)} từ</span>
            </div>
          </div>`;
      })
      .join("");
  }

  async function selectPost(postId, resetHistory = true) {
    state.selectedPostId = postId;
    document.querySelectorAll(".post-card").forEach((el) => el.classList.toggle("selected", el.dataset.id === postId));

    const { data } = await api(`/api/posts/${encodeURIComponent(postId)}`);
    if (!data.ok) return;
    state.selectedPostFull = data.post;
    if (resetHistory) state.activeHistoryIndex = 0;

    // Mode 1 Original pane
    $("origMetaLine").textContent = `${data.post.source.toUpperCase()} · ${data.post.crawled_from || "--"} · ${data.post.id}`;
    if (data.post.url) {
      $("origExternalLink").href = data.post.url;
      $("origExternalLink").classList.remove("hidden");
    } else {
      $("origExternalLink").classList.add("hidden");
    }
    $("originalTextArea").value = data.post.text || "";
    setOrigDirty(false);
    checkSafetyText(data.post.text || "", $("origSafetyBadge"), $("origStats"));

    const hist = data.post.rewrite_history || [];
    $("historyCountBadge").textContent = String(hist.length);

    // Auto-sync Mode 1 prompt to latest_rewrite.variant_id when user hasn't unsaved draft
    const latestVid = data.post.latest_rewrite?.variant_id;
    if (resetHistory && latestVid && state.promptsById[latestVid] && !isPromptDirty(state.singleVariantId)) {
      state.singleVariantId = latestVid;
      loadInlinePromptEditor(latestVid);
    } else {
      renderSingleResult(hist[state.activeHistoryIndex] || data.post.latest_rewrite || null, hist);
    }

    syncMatrixSourcePreview();
  }

  function setOrigDirty(dirty) {
    state.origDirty = dirty;
    $("btnSaveOrigText").disabled = !dirty || !state.selectedPostId;
    $("btnResetOrigText").disabled = !state.selectedPostFull;
  }

  async function loadSavedSessions() {
    const { data } = await api("/api/sessions");
    if (!data.ok || !data.sessions) return;
    const { matrix, rotate } = data.sessions;

    if (matrix?.items?.length) {
      state.matrixPostId = matrix.post_id || "";
      state.matrixSourceText = matrix.original_text || "";
      state.matrixSourceLabel = `${(matrix.source || "POST").toUpperCase()} · ${wc(matrix.original_text)} từ`;
      state.matrixItems = matrix.items;
      state.matrixAnalytics = matrix.analytics;
      matrix.items.forEach((it) => {
        if (it.variant_id) state.promptLastScores[it.variant_id] = { status: it.status, score: it.audit?.score ?? 0 };
      });
      syncMatrixSourcePreview();
      renderMatrixView();
    }

    if (rotate?.items?.length) {
      state.rotateItems = rotate.items;
      state.rotateAnalytics = rotate.analytics;
      rotate.items.forEach((it) => {
        if (it.variant_id) state.promptLastScores[it.variant_id] = { status: it.status, score: it.audit?.score ?? 0 };
      });
      renderRotateView();
    }
    renderPromptRail();
  }

  // =========================================================================
  // Mode 1: Single Rewrite & Rendering
  // =========================================================================
  function renderSingleResult(res, history = []) {
    const pill = $("rwStatusPill");
    const callout = $("rwErrorCallout");
    const emptyEl = $("rwEmptyState");
    const renderedEl = $("rwRenderedText");
    const auditBar = $("rwAuditBar");
    const syncBtn = $("btnSyncPromptToOutput");

    if (!res) {
      pill.className = "status-pill idle";
      pill.textContent = "CHỜ CHẠY";
      $("rwMetaLine").textContent = "--";
      $("rwStats").textContent = "0 từ";
      callout.classList.add("hidden");
      syncBtn.classList.add("hidden");
      emptyEl.classList.remove("hidden");
      renderedEl.classList.add("hidden");
      auditBar.classList.add("hidden");
      $("rwIterDiffOutput").innerHTML = `<span class="muted-tag">Chạy ít nhất 2 lần (trước và sau khi sửa prompt) để xem khác biệt từng từ.</span>`;
      $("rwRawOutput").textContent = "";
      $("rwCleanOutput").textContent = "";
      renderHistoryList([]);
      return;
    }

    const st = res.status || "error";
    const mapPill = { ok: ["ok", "HOÀN TẤT"], rejected: ["rejected", "REJECTED"], error: ["error", "LỖI API"] };
    const [cls, txt] = mapPill[st] || ["error", st.toUpperCase()];
    pill.className = `status-pill ${cls}`;
    pill.textContent = txt;

    const lat = res.latency_ms ? `${(res.latency_ms / 1000).toFixed(2)}s` : "0s";
    const draftTag = res.is_draft ? " [NHÁP]" : "";
    $("rwMetaLine").textContent = `${res.variant_id || "--"}${draftTag} · ${res.provider}/${res.model} · ${lat} · T=${res.params?.temperature ?? "?"}`;

    // Show 1-click prompt sync button if viewed output came from a different prompt than current editor
    if (res.variant_id && res.variant_id !== state.singleVariantId && state.promptsById[res.variant_id]) {
      syncBtn.textContent = `Mở ${state.promptsById[res.variant_id].code}`;
      syncBtn.dataset.syncVid = res.variant_id;
      syncBtn.classList.remove("hidden");
    } else {
      syncBtn.classList.add("hidden");
    }

    const outText = res.output_text || "";
    $("rwStats").textContent = `${wc(outText)} từ · ${outText.length} ký tự`;

    if (st === "error" || st === "rejected") {
      callout.classList.remove("hidden");
      callout.classList.toggle("reject", st === "rejected");
      $("rwErrorTitle").textContent = st === "rejected" ? "Bị từ chối ([REJECT])" : "Lỗi gọi API Rewrite";
      $("rwErrorMessage").textContent = st === "rejected" ? (res.reject_reason || "Từ chối") : (res.error || "Unknown error");
      $("btnToggleTraceback").classList.toggle("hidden", !res.traceback);
      $("rwErrorTrace").textContent = res.traceback || "";
      $("rwErrorTrace").classList.add("hidden");
    } else {
      callout.classList.add("hidden");
    }

    if (outText) {
      emptyEl.classList.add("hidden");
      renderedEl.classList.remove("hidden");
      renderedEl.innerHTML = highlightTickers(outText);
    } else {
      emptyEl.classList.remove("hidden");
      renderedEl.classList.add("hidden");
    }

    const prevRun = history[state.activeHistoryIndex + 1];
    if (prevRun?.output_text && outText) {
      $("rwIterDiffOutput").innerHTML = buildWordDiffHtml(prevRun.output_text, outText);
    } else {
      $("rwIterDiffOutput").innerHTML = `<span class="muted-tag">Chưa có bản chạy trước đó cho bài này. Hãy sửa prompt ở khung dưới và bấm <strong>Test nháp</strong> để so sánh trực tiếp trước ↔ sau!</span>`;
    }

    $("rwRawOutput").textContent = res.raw_output || "(trống)";
    $("rwCleanOutput").textContent = res.cleaned_output || "(trống)";

    if (st === "ok" && res.audit?.checks?.length) {
      auditBar.classList.remove("hidden");
      $("auditScoreVal").textContent = `${res.audit.score}/100`;
      $("auditScoreVal").style.color = res.audit.score >= 85 ? "var(--accent)" : res.audit.score >= 65 ? "var(--amber)" : "var(--red)";
      $("auditChecksList").innerHTML = res.audit.checks
        .map((c) => {
          const ic = c.status === "pass" ? "✓" : c.status === "warn" ? "!" : "✕";
          return `<span class="audit-pill ${c.status}" title="${esc(c.detail)}">${ic} ${esc(c.label)}</span>`;
        })
        .join("");
    } else {
      auditBar.classList.add("hidden");
    }

    renderHistoryList(history);
  }

  function renderHistoryList(history) {
    const el = $("rwHistoryList");
    if (!history.length) {
      el.innerHTML = `<div class="muted-tag">Chưa có lịch sử chạy.</div>`;
      return;
    }
    el.innerHTML = history
      .map((item, idx) => `
        <div class="history-item" data-hidx="${idx}" style="${idx === state.activeHistoryIndex ? "border-color: var(--accent);" : ""}">
          <div class="history-head">
            <span>#${history.length - idx} · ${esc(item.variant_id)}${item.is_draft ? " (nháp)" : ""} · ${esc(item.model)}</span>
            <span>T=${item.params?.temperature} · ${((item.latency_ms || 0) / 1000).toFixed(2)}s · ${item.audit?.score ?? "--"}đ</span>
          </div>
          <div class="history-preview">${esc(item.output_text || item.error || "")}</div>
        </div>`)
      .join("");
  }

  async function runSingleRewrite(forceUseDraft = false) {
    if (state.isRewriting) return;
    const text = $("originalTextArea").value.trim();
    if (!text) {
      toast("Hãy chọn 1 bài hoặc dán bài viết gốc trước!", "warn");
      return;
    }
    const vid = state.singleVariantId || $("singlePromptSelect").value;
    const draftText = $("inlinePeTextarea").value;
    const useDraft = forceUseDraft || isPromptDirty(vid);
    if (useDraft && !draftText.includes("{text}")) {
      toast("Prompt nháp đang thiếu placeholder {text}!", "err");
      return;
    }

    state.isRewriting = true;
    $("btnRunSingle").disabled = true;
    $("btnInlineTestDraft").disabled = true;
    $("rwStatusPill").className = "status-pill running";
    $("rwStatusPill").textContent = "ĐANG CHẠY...";

    try {
      const payload = {
        ...collectRuntimePayload(),
        post_id: state.selectedPostId || "",
        text,
        source: state.selectedPostFull?.source || "manual",
        variant_id: vid,
        custom_prompt_template: useDraft ? draftText : null,
        is_draft: useDraft,
      };
      const { data } = await api("/api/rewrite", { method: "POST", body: JSON.stringify(payload) });
      const res = data.result;
      if (res?.variant_id) {
        state.promptLastScores[res.variant_id] = { status: res.status, score: res.audit?.score ?? 0 };
        renderPromptRail();
      }
      if (data.post) {
        state.selectedPostFull = data.post;
        state.activeHistoryIndex = 0;
        renderStorageState(data.storage);
        renderSingleResult(res, data.post.rewrite_history || [res]);
        $("historyCountBadge").textContent = String((data.post.rewrite_history || []).length);
        await loadPosts(false);
      } else if (res) {
        renderSingleResult(res, [res]);
      }

      if (res?.status === "ok") toast(`Xong [${res.variant_id}] · ${res.audit?.score ?? 0}đ (${(res.latency_ms / 1000).toFixed(2)}s)`, "ok");
      else if (res?.status === "rejected") toast(`Rejected: ${res.reject_reason}`, "warn");
      else toast(`Lỗi: ${res?.error || data.error}`, "err");
    } finally {
      state.isRewriting = false;
      $("btnRunSingle").disabled = false;
      $("btnInlineTestDraft").disabled = false;
    }
  }

  // =========================================================================
  // Mode 2: Sticky Split-Desk (1 Bài × 20 Prompt)
  // =========================================================================
  function syncMatrixSourcePreview() {
    const selText = $("originalTextArea").value.trim() || state.selectedPostFull?.text || "";
    const hasMismatch =
      state.matrixItems.length > 0 &&
      state.matrixPostId &&
      state.selectedPostId &&
      state.selectedPostId !== state.matrixPostId;

    $("matrixMismatchBanner").classList.toggle("hidden", !hasMismatch);

    // If mismatch, keep showing the text that actually produced the results OR selected text?
    // Keep matrixSourceText if results belong to another post and user hasn't manually overridden
    const displayText = hasMismatch ? state.matrixSourceText : selText;
    $("matrixSourceEditor").value = displayText;
    $("matrixOrigMeta").textContent = hasMismatch
      ? `${state.matrixSourceLabel} (đang khóa theo kết quả)`
      : state.selectedPostFull
      ? `${state.selectedPostFull.source.toUpperCase()} · ${state.selectedPostFull.crawled_from || "--"}`
      : "Nhập hoặc chọn bài bên trái";
    checkSafetyText(displayText, $("matrixOrigSafety"), $("matrixOrigStats"));
    $("matrixTargetPostHint").textContent = displayText
      ? `${state.selectedPostFull?.source?.toUpperCase() || "TEXT"} · ${wc(displayText)} từ`
      : "Chưa có bài gốc";
  }

  async function runMatrixAllPrompts(overridePostText = null, overridePostId = null) {
    const text = (overridePostText !== null ? overridePostText : ($("matrixSourceEditor").value || $("originalTextArea").value || "")).trim();
    if (!text) {
      toast("Hãy chọn 1 bài viết ở cột trái hoặc dán bài gốc trước!", "warn");
      return;
    }
    const variantIds = resolveSelectedScopeIds($("matrixScopeSelect").value);
    if (!variantIds.length) {
      toast("Chưa tick chọn prompt nào!", "warn");
      return;
    }

    const postId = overridePostId !== null ? overridePostId : (state.selectedPostId || "");
    const source = state.selectedPostFull?.source || "manual";
    state.matrixPostId = postId;
    state.matrixSourceText = text;
    state.matrixSourceLabel = `${source.toUpperCase()} · ${wc(text)} từ`;
    state.matrixItems = [];
    state.matrixAnalytics = null;
    state.matrixHighlightPair = null;
    $("matrixMismatchBanner").classList.add("hidden");
    renderMatrixView();

    const payload = {
      ...collectRuntimePayload(),
      post_id: postId,
      text,
      source,
      variant_ids: variantIds,
      draft_overrides: collectActiveDraftOverrides(),
    };

    const { data } = await api("/api/test/all-prompts", { method: "POST", body: JSON.stringify(payload) });
    if (!data.ok) {
      toast(data.error || "Không khởi chạy được Mode 1×20", "err");
      return;
    }

    pollAsyncJob(
      data.job.id,
      (job) => {
        state.matrixItems = (job.items || []).slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
        state.matrixItems.forEach((it) => {
          if (it.variant_id) state.promptLastScores[it.variant_id] = { status: it.status, score: it.audit?.score ?? 0 };
        });
        if (job.analytics) state.matrixAnalytics = job.analytics;
        renderMatrixView();
        renderPromptRail();
      },
      (job) => {
        if (job.analytics) {
          state.matrixAnalytics = job.analytics;
          renderMatrixView();
          toast(`Xong ${job.analytics.ok_count}/${job.analytics.total} prompt · TB ${job.analytics.avg_score}đ · Đa dạng ${job.analytics.diversity_score}%`, "ok");
        } else if (job.status === "error") {
          toast(`Lỗi job: ${job.error}`, "err");
        }
      }
    );
  }

  function renderRadar(panelId, metricsId, insightsId, analytics, onPairClick) {
    const panel = $(panelId);
    if (!analytics || !analytics.total) {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");

    const scCls = analytics.avg_score >= 85 ? "good" : analytics.avg_score >= 70 ? "warn" : "bad";
    const dvCls = analytics.diversity_score >= 75 ? "good" : analytics.diversity_score >= 55 ? "warn" : "bad";
    const wInfo = [
      analytics.avg_words_g1 != null ? `G1:${analytics.avg_words_g1}t` : "",
      analytics.avg_words_g2 != null ? `G2:${analytics.avg_words_g2}t` : "",
    ].filter(Boolean).join(" / ");

    $(metricsId).innerHTML = `
      <div class="rm-item"><span class="rm-label">HOÀN THÀNH</span><span class="rm-val">${analytics.ok_count}/${analytics.total}</span></div>
      <div class="rm-item"><span class="rm-label">ĐIỂM TB</span><span class="rm-val ${scCls}">${analytics.avg_score}đ</span></div>
      <div class="rm-item"><span class="rm-label">ĐA DẠNG</span><span class="rm-val ${dvCls}">${analytics.diversity_score}%</span></div>
      ${wInfo ? `<div class="rm-item"><span class="rm-label">TB ĐỘ DÀI</span><span class="rm-val">${wInfo}</span></div>` : ""}
      ${analytics.rejected_count ? `<div class="rm-item"><span class="rm-label">REJECT</span><span class="rm-val warn">${analytics.rejected_count}</span></div>` : ""}
      ${analytics.error_count ? `<div class="rm-item"><span class="rm-label">LỖI</span><span class="rm-val bad">${analytics.error_count}</span></div>` : ""}
    `;

    const chips = [];
    (analytics.overlap_pairs || []).slice(0, 4).forEach((pair) => {
      const ca = state.promptsById[pair.a]?.code || pair.a;
      const cb = state.promptsById[pair.b]?.code || pair.b;
      chips.push(`<span class="insight-chip overlap" data-pair-a="${esc(pair.a)}" data-pair-b="${esc(pair.b)}" title="Click để đánh dấu 2 prompt trùng lặp này">⚠ Trùng ${pair.similarity}%: ${esc(ca)} ↔ ${esc(cb)}</span>`);
    });
    (analytics.repeated_openings || []).slice(0, 2).forEach((ro) => {
      chips.push(`<span class="insight-chip overlap" title="Mở bài giống nhau: ${esc(ro.variants.join(", "))}">⚠ Trùng mở bài (${ro.variants.length}): "${esc(ro.phrase)}..."</span>`);
    });
    (analytics.top_issues || []).slice(0, 4).forEach((iss) => {
      chips.push(`<span class="insight-chip issue" title="Gặp ở: ${esc(iss.variants.join(", "))}">✕ ${iss.count} prompt: ${esc(iss.label)}</span>`);
    });
    if (!chips.length && analytics.ok_count > 0) {
      chips.push(`<span class="insight-chip ok">✓ Không phát hiện trùng lặp văn phong hay vi phạm nổi bật</span>`);
    }

    const box = $(insightsId);
    box.innerHTML = chips.join("");
    if (onPairClick) {
      box.querySelectorAll("[data-pair-a]").forEach((el) => {
        el.addEventListener("click", () => onPairClick(el.dataset.pairA, el.dataset.pairB));
      });
    }
  }

  function renderMatrixView() {
    renderRadar("matrixRadar", "matrixRadarMetrics", "matrixRadarInsights", state.matrixAnalytics, (a, b) => {
      state.matrixHighlightPair = new Set([a, b]);
      state.matrixFilter = "all";
      document.querySelectorAll("#matrixFilterPills .fpill").forEach((x) => x.classList.toggle("active", x.dataset.mf === "all"));
      renderMatrixView();
      const firstCard = document.querySelector(`.mcard[data-vid="${a}"]`);
      if (firstCard) firstCard.scrollIntoView({ behavior: "smooth", block: "center" });
    });

    const grid = $("matrixGrid");
    grid.className = `matrix-grid cols-${state.matrixCols}`;

    if (!state.matrixItems.length) {
      const totalPrompts = getAllPromptsOrdered().length || 10;
      grid.innerHTML = `
        <div class="stage-empty">
          <div class="empty-glyph">⊞</div>
          <h3>So sánh song song 1 Bài gốc (trái) ↔ ${totalPrompts} Prompt (phải)</h3>
          <p>Chọn số <strong>LUỒNG</strong> ở thanh trên rồi nhấn <strong>▶ Chạy 1 Bài × ${totalPrompts} Prompt</strong>.</p>
          <span>Bài gốc luôn giữ cố định bên trái để bạn đối chiếu từng câu từng chữ với toàn bộ ${totalPrompts} phiên bản rewrite bên phải.</span>
        </div>`;
      return;
    }

    const simMap = state.matrixAnalytics?.per_variant_max_sim || {};
    let list = state.matrixItems.slice();

    if (state.matrixFilter === "g1") list = list.filter((x) => x.group === 1);
    else if (state.matrixFilter === "g2") list = list.filter((x) => x.group === 2);
    else if (state.matrixFilter === "low") list = list.filter((x) => x.status === "ok" && (x.audit?.score ?? 100) < 85);
    else if (state.matrixFilter === "overlap") list = list.filter((x) => (simMap[x.variant_id] || 0) >= 24);
    else if (state.matrixFilter === "err") list = list.filter((x) => x.status !== "ok");

    if (state.matrixSort === "score_asc") list.sort((a, b) => (a.audit?.score ?? -1) - (b.audit?.score ?? -1));
    else if (state.matrixSort === "score_desc") list.sort((a, b) => (b.audit?.score ?? -1) - (a.audit?.score ?? -1));
    else if (state.matrixSort === "overlap_desc") list.sort((a, b) => (simMap[b.variant_id] || 0) - (simMap[a.variant_id] || 0));
    else list.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));

    if (!list.length) {
      grid.innerHTML = `<div class="stage-empty"><p>Không có prompt nào khớp bộ lọc hiện tại.</p></div>`;
      return;
    }

    grid.innerHTML = list.map((item) => buildMatrixCardHtml(item, simMap)).join("");
  }

  function buildAuditMiniPills(item) {
    if (item.status !== "ok" || !item.audit?.checks) return "";
    const bads = item.audit.checks.filter((c) => c.status !== "pass");
    if (!bads.length) return `<span class="audit-pill pass">✓ Đạt mọi luật</span>`;
    return bads
      .map((c) => `<span class="audit-pill ${c.status}" title="${esc(c.detail)}">${c.status === "fail" ? "✕" : "!"} ${esc(c.label)}</span>`)
      .join("");
  }

  function buildMatrixCardHtml(item, simMap) {
    const vid = item.variant_id || "";
    const p = state.promptsById[vid];
    const code = p?.code || vid.toUpperCase();
    const angle = item.angle || p?.angle || "";
    const gCls = item.group === 1 ? "g1" : "g2";
    const hl = state.matrixHighlightPair?.has(vid) ? "highlight" : "";
    const stCls = item.status === "error" ? "card-err" : item.status === "rejected" ? "card-rej" : "";

    const score = item.audit?.score ?? 0;
    const scoreBadge =
      item.status === "ok"
        ? `<span class="pr-score ${score >= 85 ? "good" : score >= 65 ? "mid" : "bad"}">${score}đ</span>`
        : `<span class="pr-score bad">${item.status === "rejected" ? "REJECT" : "LỖI"}</span>`;

    const maxSim = simMap[vid] || 0;
    const simBadge = maxSim >= 24 ? `<span class="sim-badge" title="Độ trùng lặp cao nhất với prompt khác">trùng ${maxSim}%</span>` : "";
    const draftBadge = item.is_draft || isPromptDirty(vid) ? `<span class="pe-badge">nháp</span>` : "";

    const bodyHtml =
      item.status === "ok"
        ? highlightTickers(item.output_text || "")
        : `<div class="mcard-err-msg">${esc(item.status === "rejected" ? item.reject_reason : item.error)}</div>`;

    return `
      <article class="mcard ${hl} ${stCls}" data-vid="${esc(vid)}">
        <header class="mcard-head">
          <div class="mcard-id-wrap">
            <span class="g-tag ${gCls}">${esc(code)}</span>
            <span class="mcard-angle" title="${esc(angle)}">${esc(angle)}</span>
            ${draftBadge}
          </div>
          <div class="mcard-badges">
            ${simBadge}
            <span class="mono muted-tag">${item.audit?.word_count || 0}t · ${((item.latency_ms || 0) / 1000).toFixed(1)}s</span>
            ${scoreBadge}
          </div>
        </header>
        <div class="mcard-body">${bodyHtml}</div>
        <footer class="mcard-foot">
          <div class="mcard-audits">${buildAuditMiniPills(item)}</div>
          <div class="mcard-btns">
            <button type="button" class="btn-xs" data-m-edit="${esc(vid)}">✎ Sửa Prompt</button>
            <button type="button" class="btn-xs" data-m-rerun="${esc(vid)}">↻ Chạy lại</button>
            <button type="button" class="btn-xs" data-m-focus="${esc(vid)}" title="Mở trong Mode 1×1">Mở 1×1</button>
            <button type="button" class="btn-xs mono" data-m-json="${esc(vid)}" title="Copy JSON của card này (model + bài gốc + prompt + bài xử lý)">{ } JSON</button>
            <button type="button" class="btn-xs" data-m-copy="${esc(vid)}">Copy</button>
          </div>
        </footer>
      </article>`;
  }

  // =========================================================================
  // Mode 3: N Bài × Xoay đủ 20 Prompt (1-to-1 Rotation)
  // =========================================================================
  function updateRotateReadiness() {
    const scopeIds = resolveSelectedScopeIds($("rotateScopeSelect").value);
    const need = scopeIds.length || 20;
    const have = state.storage?.total_posts ?? state.posts.length;
    const pill = $("rotateReadinessPill");
    const fillBtn = $("btnCrawlToFill20");

    if (have >= need) {
      pill.className = "mono readiness-pill ready";
      pill.textContent = `Đủ bài: ${have} / ${need} prompt`;
      fillBtn.classList.add("hidden");
    } else {
      const fillAll = $("chkRotateFillAll")?.checked;
      pill.className = `mono readiness-pill ${fillAll ? "ready" : "short"}`;
      pill.textContent = fillAll
        ? `${have} bài → phủ ${need}/${need} prompt (lặp vòng)`
        : `${have} bài / ${need} prompt`;
      fillBtn.classList.remove("hidden");
    }
  }

  function renderRotateCoverageDots() {
    const all = getAllPromptsOrdered();
    const byVid = {};
    state.rotateItems.forEach((it) => { byVid[it.variant_id] = it; });

    const covered = Object.keys(byVid).length;
    $("rotateCoverageLabel").textContent = `${covered} / ${all.length} prompt đã chạy`;

    $("rotateCoverageDots").innerHTML = all
      .map((p) => {
        const it = byVid[p.id];
        let cls = "";
        if (it) {
          if (it.status !== "ok") cls = "bad";
          else if ((it.audit?.score ?? 0) >= 85) cls = "good";
          else cls = "mid";
        }
        return `<button type="button" class="cov-dot ${cls}" data-cov-vid="${esc(p.id)}" title="${esc(p.code)} · ${esc(p.angle)}${it ? ` (${it.audit?.score ?? it.status})` : " (chưa chạy)"}">${esc(p.code)}</button>`;
      })
      .join("");
  }

  async function runRotatePrompts() {
    const variantIds = resolveSelectedScopeIds($("rotateScopeSelect").value);
    if (!variantIds.length) {
      toast("Chưa có prompt nào được chọn!", "warn");
      return;
    }
    if (!state.posts.length) {
      toast("Chưa có bài viết nào! Hãy bấm Crawl trước.", "warn");
      return;
    }

    state.rotateItems = [];
    state.rotateAnalytics = null;
    renderRotateView();

    const payload = {
      ...collectRuntimePayload(),
      variant_ids: variantIds,
      fill_all_variants: $("chkRotateFillAll").checked,
      draft_overrides: collectActiveDraftOverrides(),
    };

    const { data } = await api("/api/test/rotate-prompts", { method: "POST", body: JSON.stringify(payload) });
    if (!data.ok) {
      toast(data.error || "Không chạy được Mode Xoay 1-1", "err");
      return;
    }

    pollAsyncJob(
      data.job.id,
      (job) => {
        state.rotateItems = (job.items || []).slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
        state.rotateItems.forEach((it) => {
          if (it.variant_id) state.promptLastScores[it.variant_id] = { status: it.status, score: it.audit?.score ?? 0 };
        });
        if (job.analytics) state.rotateAnalytics = job.analytics;
        renderRotateView();
        renderPromptRail();
      },
      async (job) => {
        if (job.analytics) {
          state.rotateAnalytics = job.analytics;
          renderRotateView();
          toast(`Xoay 1-1 xong: ${job.analytics.ok_count}/${job.analytics.total} cặp · TB ${job.analytics.avg_score}đ`, "ok");
        }
        await loadPosts(true);
      }
    );
  }

  function renderRotateView() {
    renderRotateCoverageDots();
    renderRadar("rotateRadar", "rotateRadarMetrics", "rotateRadarInsights", state.rotateAnalytics);

    const box = $("rotateList");
    if (!state.rotateItems.length) {
      if (!state.posts.length) {
        const totalPrompts = getAllPromptsOrdered().length || 10;
        box.innerHTML = `
          <div class="stage-empty">
            <div class="empty-glyph">⟳</div>
            <h3>Chế độ Xoay vòng 1-1: 1 Bài — 1 Prompt phủ đủ ${totalPrompts} Prompt</h3>
            <p>Bài 1 → <code>P01</code>, Bài 2 → <code>P02</code>, …, Bài ${totalPrompts} → <code>P${totalPrompts}</code>. So sánh trực quan từng cặp Bài gốc ↔ Bài đã xử lý.</p>
            <span>Hãy Crawl bài viết trước rồi nhấn <strong>▶ Chạy Xoay 1-1</strong>.</span>
          </div>`;
        return;
      }

      // Live Pre-run Pairing Preview
      const variantIds = resolveSelectedScopeIds($("rotateScopeSelect").value);
      const fillAll = $("chkRotateFillAll").checked;
      const count = fillAll ? variantIds.length : Math.min(state.posts.length, variantIds.length);
      const previewRows = [];
      for (let i = 0; i < count; i++) {
        const post = state.posts[i % state.posts.length];
        const p = state.promptsById[variantIds[i]];
        const gCls = p?.group === 1 ? "g1" : "g2";
        previewRows.push(`
          <div class="pairing-row">
            <span class="mono muted-tag">#${i + 1}</span>
            <span class="g-tag ${gCls}">${esc(p?.code || variantIds[i])}</span>
            <span class="src-badge ${esc(post.source)}">${esc(post.source)}</span>
            <span class="pairing-post">${esc(post.text)}</span>
          </div>`);
      }
      box.innerHTML = `
        <div class="pairing-preview-wrap">
          <div class="pairing-preview-head">
            <div>
              <strong>Bản ghép cặp dự kiến (${count} cặp)</strong>
              <span class="muted-tag"> · Bấm <strong>▶ Chạy Xoay 1-1</strong> ở góc trên để chạy với ${$("cfgConcurrency").value} luồng đồng thời</span>
            </div>
          </div>
          <div class="pairing-preview-grid">${previewRows.join("")}</div>
        </div>`;
      return;
    }

    let list = state.rotateItems.slice();
    if (state.rotateFilter === "g1") list = list.filter((x) => x.group === 1);
    else if (state.rotateFilter === "g2") list = list.filter((x) => x.group === 2);
    else if (state.rotateFilter === "low") list = list.filter((x) => x.status === "ok" && (x.audit?.score ?? 100) < 85);
    else if (state.rotateFilter === "err") list = list.filter((x) => x.status !== "ok");

    if (!list.length) {
      box.innerHTML = `<div class="stage-empty"><p>Không có cặp nào khớp bộ lọc.</p></div>`;
      return;
    }

    box.innerHTML = list
      .map((item) => {
        const vid = item.variant_id || "";
        const p = state.promptsById[vid];
        const code = p?.code || vid.toUpperCase();
        const angle = item.angle || p?.angle || "";
        const gCls = item.group === 1 ? "g1" : "g2";
        const score = item.audit?.score ?? 0;
        const scoreBadge =
          item.status === "ok"
            ? `<span class="pr-score ${score >= 85 ? "good" : score >= 65 ? "mid" : "bad"}">${score}đ</span>`
            : `<span class="pr-score bad">${item.status === "rejected" ? "REJECT" : "LỖI"}</span>`;

        const outHtml =
          item.status === "ok"
            ? highlightTickers(item.output_text || "")
            : `<div class="mcard-err-msg">${esc(item.status === "rejected" ? item.reject_reason : item.error)}</div>`;

        return `
          <article class="pair-card" id="pair-${esc(vid)}" data-vid="${esc(vid)}" data-pid="${esc(item.post_id || "")}">
            <header class="mcard-head">
              <div class="mcard-id-wrap">
                <span class="mono muted-tag">#${(item.order ?? 0) + 1}</span>
                <span class="src-badge ${esc(item.post_source || "manual")}">${esc(item.post_source || "post")}</span>
                <span class="muted-tag">→</span>
                <span class="g-tag ${gCls}">${esc(code)}</span>
                <span class="mcard-angle">${esc(angle)}</span>
              </div>
              <div class="mcard-badges">
                <span class="mono muted-tag">${item.audit?.word_count || 0}t · ${((item.latency_ms || 0) / 1000).toFixed(1)}s</span>
                ${scoreBadge}
              </div>
            </header>
            <div class="pair-grid">
              <div class="pair-col orig">${esc(item.original_text || "")}</div>
              <div class="pair-col out">${outHtml}</div>
            </div>
            <footer class="mcard-foot">
              <div class="mcard-audits">${buildAuditMiniPills(item)}</div>
              <div class="mcard-btns">
                <button type="button" class="btn-xs" data-r-edit="${esc(vid)}">✎ Sửa Prompt</button>
                <button type="button" class="btn-xs" data-r-rerun="${esc(vid)}">↻ Chạy lại cặp này</button>
                <button type="button" class="btn-xs" data-r-focus="${esc(vid)}" data-r-pid="${esc(item.post_id || "")}">Mở 1×1</button>
                <button type="button" class="btn-xs mono" data-r-json="${esc(vid)}" title="Copy JSON của cặp này (model + bài gốc + prompt + bài xử lý)">{ } JSON</button>
                <button type="button" class="btn-xs" data-r-copy="${esc(vid)}">Copy</button>
              </div>
            </footer>
          </article>`;
      })
      .join("");
  }

  // =========================================================================
  // Slide-Over Prompt Editor & In-Place Re-Run (Mode 2 & Mode 3)
  // =========================================================================
  function openPromptDrawer(context, vid) {
    const p = state.promptsById[vid];
    if (!p) return;
    const item =
      context === "matrix"
        ? state.matrixItems.find((x) => x.variant_id === vid)
        : state.rotateItems.find((x) => x.variant_id === vid);

    const origText =
      context === "rotate"
        ? (item?.original_text || "")
        : (state.matrixSourceText || $("matrixSourceEditor").value || $("originalTextArea").value || "");

    state.drawerTarget = {
      context,
      variantId: vid,
      postId: item?.post_id || state.matrixPostId || state.selectedPostId || "",
      originalText: origText,
      order: item?.order ?? 0,
    };
    state.drawerPrevOutput = item?.output_text || "";
    state.drawerTab = "result";
    document.querySelectorAll("#drawerPreviewTabs .vtab").forEach((b) => b.classList.toggle("active", b.dataset.dtab === "result"));

    const text = getPromptContent(vid);
    $("drawerPeCode").textContent = `${p.code} · ${p.angle}`;
    $("drawerPePath").textContent = p.file_path;
    $("drawerPeTextarea").value = text;
    updateDrawerBadges(vid, text);
    renderDrawerPreview(item);
    $("promptDrawerBackdrop").classList.remove("hidden");
  }

  function updateDrawerBadges(vid, text) {
    const p = state.promptsById[vid];
    if (!p) return;
    const dirty = text !== p.content;
    const hasPh = text.includes("{text}");
    $("drawerPeDirty").classList.toggle("hidden", !dirty);
    $("drawerPePlaceholderErr").classList.toggle("hidden", hasPh);
    $("btnDrawerRevert").disabled = !dirty;
    $("btnDrawerRestore").classList.toggle("hidden", !p.has_backup);
    $("btnDrawerSave").disabled = !hasPh || !dirty;
  }

  function renderDrawerPreview(item) {
    const tab = state.drawerTab;
    const textBox = $("drawerPreviewText");
    const checksBox = $("drawerPreviewChecks");

    if (tab === "original") {
      $("drawerPreviewMeta").textContent = `BÀI VIẾT GỐC (${wc(state.drawerTarget?.originalText)} từ)`;
      textBox.textContent = state.drawerTarget?.originalText || "(Chưa có bài viết gốc)";
      checksBox.innerHTML = "";
      return;
    }

    if (tab === "diff") {
      $("drawerPreviewMeta").textContent = "SO SÁNH TRƯỚC ↔ SAU KHI SỬA PROMPT";
      if (state.drawerPrevOutput && item?.output_text && state.drawerPrevOutput !== item.output_text) {
        textBox.innerHTML = buildWordDiffHtml(state.drawerPrevOutput, item.output_text);
      } else {
        textBox.innerHTML = `<span class="muted-tag">Sửa prompt ở trên rồi nhấn <strong>▶ Test lại ô này</strong> để thấy rõ từng từ thay đổi.</span>`;
      }
      checksBox.innerHTML = "";
      return;
    }

    if (!item) {
      $("drawerPreviewMeta").textContent = "CHƯA CÓ KẾT QUẢ";
      textBox.textContent = "(Bấm ▶ Test lại ô này để chạy thử)";
      checksBox.innerHTML = "";
      return;
    }
    $("drawerPreviewMeta").textContent = `${item.audit?.score ?? "--"}đ · ${item.audit?.word_count ?? 0} từ · ${((item.latency_ms || 0) / 1000).toFixed(2)}s`;
    textBox.innerHTML = item.status === "ok"
      ? highlightTickers(item.output_text || "")
      : `<span class="mcard-err-msg">${esc(item.reject_reason || item.error)}</span>`;
    checksBox.innerHTML = (item.audit?.checks || [])
      .map((c) => `<span class="audit-pill ${c.status}" title="${esc(c.detail)}">${c.status === "pass" ? "✓" : c.status === "warn" ? "!" : "✕"} ${esc(c.label)}</span>`)
      .join("");
  }

  async function rerunSingleCardInPlace(context, vid, explicitDraft = null) {
    const arr = context === "matrix" ? state.matrixItems : state.rotateItems;
    const idx = arr.findIndex((x) => x.variant_id === vid);
    const existing = idx >= 0 ? arr[idx] : null;
    if (existing?.output_text) state.drawerPrevOutput = existing.output_text;

    const text =
      context === "rotate"
        ? (existing?.original_text || "")
        : (state.matrixSourceText || $("matrixSourceEditor").value || $("originalTextArea").value || "").trim();

    if (!text) {
      toast("Không có nội dung gốc để chạy lại!", "warn");
      return null;
    }

    const draft = explicitDraft !== null ? explicitDraft : (isPromptDirty(vid) ? state.promptDrafts[vid] : null);
    if (draft && !draft.includes("{text}")) {
      toast("Prompt nháp thiếu {text}!", "err");
      return null;
    }

    toast(`Đang chạy lại ${state.promptsById[vid]?.code || vid}...`, "ok");
    const { data } = await api("/api/rewrite", {
      method: "POST",
      body: JSON.stringify({
        ...collectRuntimePayload(),
        post_id: existing?.post_id || (context === "matrix" ? (state.matrixPostId || state.selectedPostId || "") : ""),
        text,
        source: existing?.post_source || state.selectedPostFull?.source || "manual",
        variant_id: vid,
        custom_prompt_template: draft,
        is_draft: Boolean(draft),
        record_to_post: context === "rotate",
      }),
    });

    const res = data.result;
    if (!res) return null;
    res.order = existing?.order ?? 0;
    if (context === "rotate") {
      res.original_text = text;
      res.post_source = existing?.post_source || "";
      res.post_author = existing?.post_author || "";
    }
    if (idx >= 0) arr[idx] = res;
    else arr.push(res);

    state.promptLastScores[vid] = { status: res.status, score: res.audit?.score ?? 0 };

    const anaRes = await api("/api/test/analyze", { method: "POST", body: JSON.stringify({ items: arr }) });
    if (context === "matrix") {
      if (anaRes.data.ok) state.matrixAnalytics = anaRes.data.analytics;
      renderMatrixView();
      api("/api/sessions/matrix", {
        method: "PUT",
        body: JSON.stringify({
          session: {
            post_id: state.matrixPostId,
            source: state.selectedPostFull?.source || "manual",
            original_text: text,
            items: arr,
            analytics: state.matrixAnalytics,
            updated_at: Date.now() / 1000,
          },
        }),
      });
    } else {
      if (anaRes.data.ok) state.rotateAnalytics = anaRes.data.analytics;
      renderRotateView();
      api("/api/sessions/rotate", {
        method: "PUT",
        body: JSON.stringify({
          session: {
            items: arr,
            analytics: state.rotateAnalytics,
            updated_at: Date.now() / 1000,
          },
        }),
      });
    }
    renderPromptRail();
    if (state.drawerTarget?.variantId === vid) renderDrawerPreview(res);

    if (res.status === "ok") toast(`Cập nhật ${state.promptsById[vid]?.code}: ${res.audit?.score ?? 0}đ`, "ok");
    else toast(`${vid}: ${res.reject_reason || res.error}`, "err");
    return res;
  }

  // =========================================================================
  // Crawl & Async Job Polling
  // =========================================================================
  function setAsyncButtonsDisabled(disabled) {
    $("btnStartCrawl").disabled = disabled;
    $("btnRunMatrix").disabled = disabled;
    $("btnRunRotate").disabled = disabled;
  }

  function collectSelectedOkxHashtags() {
    const tags = new Set();
    document.querySelectorAll("#okxPresetChips .tag-chip.active, #okxTrendingChips .tag-chip.active").forEach((btn) => {
      const t = (btn.dataset.okxTag || "").trim();
      if (t) tags.add(t);
    });
    const manual = ($("crawlOkxHashtags")?.value || "").trim();
    if (manual) {
      manual.split(/[\s,;]+/).forEach((tok) => {
        const clean = tok.trim().replace(/^[#$]+/, "");
        if (clean) tags.add(clean);
      });
    }
    return [...tags];
  }

  let okxTopicsLoadedOnce = false;
  async function loadOkxTrendingTopics(force = false) {
    const btn = $("btnLoadOkxTopics");
    if (btn) { btn.disabled = true; btn.textContent = "Đang tải..."; }
    try {
      const { data } = await api(`/api/okx/trending-topics${force ? "?force=1" : ""}`);
      const box = $("okxTrendingChips");
      if (data.ok && Array.isArray(data.trending_topics) && data.trending_topics.length) {
        okxTopicsLoadedOnce = true;
        box.innerHTML = data.trending_topics
          .slice(0, 6)
          .map((tp) => {
            const lbl = tp.en_title ? `🔥 #${tp.en_title}` : `🔥 ${tp.title.slice(0, 24)}`;
            return `<button type="button" class="tag-chip trending" data-okx-tag="${esc(tp.id)}" title="${esc(tp.title)} (${esc(tp.sub_text || "")})">${esc(lbl)}</button>`;
          })
          .join("");
        box.classList.remove("hidden");
        if (force) toast(`Đã tải ${data.trending_topics.length} Hot Topic từ OKX Orbit`, "ok");
      } else if (force) {
        toast(data.error || "Không tải được Hot Topic OKX", "warn");
      }
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "↻ Hot Topic"; }
    }
  }

  async function startCrawl(customOpts = null) {
    const sources = customOpts?.sources || [...document.querySelectorAll("#crawlSourcePills input:checked")].map((i) => i.value);
    if (!sources.length) {
      toast("Hãy chọn ít nhất 1 nguồn crawl!", "warn");
      return;
    }
    setAsyncButtonsDisabled(true);
    const { data } = await api("/api/crawl", {
      method: "POST",
      body: JSON.stringify({
        sources,
        max_posts_per_source: customOpts?.max_posts_per_source || Number($("crawlMaxPosts").value) || 10,
        lookback_hours: customOpts?.lookback_hours || Number($("crawlLookback").value) || 48,
        ignore_watermark: $("crawlIgnoreWatermark").checked,
        binance_authors: $("crawlBinanceAuthors")?.value || "",
        binance_post_ids: $("crawlBinancePostIds")?.value || "",
        binance_keywords: $("crawlBinanceKeywords")?.value || "",
        okx_hashtags: collectSelectedOkxHashtags(),
        okx_timeline_type: $("crawlOkxType")?.value || "1",
        okx_include_trending: Boolean($("crawlOkxTrending")?.checked),
        okx_include_feed: Boolean($("crawlOkxFeed")?.checked),
        okx_authors: $("crawlOkxAuthors")?.value || "",
      }),
    });
    if (!data.ok) {
      setAsyncButtonsDisabled(false);
      toast(`Crawl lỗi: ${data.error}`, "err");
      return;
    }
    pollAsyncJob(data.job.id, null, async (job) => {
      if (job.status === "done") {
        const res = job.result || {};
        const n = res.total_new ?? 0;
        const bySrc = res.by_source || {};
        const parts = Object.entries(bySrc).map(([k, v]) => `${k}: +${v.new ?? 0}`);
        const warnings = Object.entries(bySrc).filter(([, v]) => v.warning || v.error).map(([k, v]) => `${k}: ${v.warning || v.error}`);
        const detail = parts.length ? ` (${parts.join(", ")})` : "";
        toast(`Crawl xong: +${n} bài mới${detail}`, n > 0 ? "ok" : "warn");
        if (warnings.length) toast(warnings[0], "warn");
        if (n > 0) $("crawlBox").classList.add("hidden");
        await loadPosts(true);
      } else {
        toast(`Crawl lỗi: ${job.error}`, "err");
      }
    });
  }

  function pollAsyncJob(jobId, onProgress, onDone) {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    state.activeJobId = jobId;
    setAsyncButtonsDisabled(true);
    $("jobBanner").classList.remove("hidden");
    $("btnCancelJob").classList.remove("hidden");

    const tick = async () => {
      if (state.activeJobId !== jobId) return;
      const { data } = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (!data.ok || !data.job || state.activeJobId !== jobId) return;
      const job = data.job;
      const pct = job.total > 0 ? Math.min(100, Math.round((job.progress / job.total) * 100)) : 50;
      $("jobBannerText").textContent = `${job.label} — ${job.message || ""}`;
      $("jobBarFill").style.width = `${job.status === "done" ? 100 : pct}%`;
      $("jobPct").textContent = `${job.status === "done" ? 100 : pct}%`;

      if (onProgress && job.items?.length) onProgress(job);

      if (job.status === "running") {
        state.pollTimer = setTimeout(tick, 550);
      } else {
        state.pollTimer = null;
        setAsyncButtonsDisabled(false);
        $("btnCancelJob").classList.add("hidden");
        setTimeout(() => {
          if (state.activeJobId === jobId) $("jobBanner").classList.add("hidden");
        }, 900);
        if (onDone) onDone(job);
      }
    };
    tick();
  }

  // =========================================================================
  // Smart Context-Aware JSON Export (Copy / Download)
  // =========================================================================
  function getPromptTextForItem(res, fallbackVid = "") {
    if (res?.prompt_template) return res.prompt_template;
    const vid = res?.variant_id || fallbackVid;
    if (vid && state.promptsById[vid]) return getPromptContent(vid);
    return "";
  }

  function extractAuditIssues(res) {
    return (res?.audit?.checks || [])
      .filter((c) => c.status !== "pass")
      .map((c) => ({ rule: c.id, status: c.status, label: c.label, detail: c.detail }));
  }

  function buildPromptMeta(res, fallbackVid, includePrompt, minimal) {
    const vid = res?.variant_id || fallbackVid || "";
    const p = state.promptsById[vid];
    const obj = {
      prompt_id: vid,
      prompt_code: p?.code || (vid ? vid.toUpperCase() : ""),
      group: res?.group ?? p?.group ?? null,
      angle: res?.angle || p?.angle || "",
    };
    if (!minimal && res?.is_draft) obj.is_draft = true;
    if (includePrompt) obj.prompt_text = getPromptTextForItem(res, fallbackVid);
    return obj;
  }

  function buildSingleItemJson(res, originalText, meta = {}, opts = {}) {
    const includePrompt = opts.includePrompt !== false;
    const minimal = Boolean(opts.minimal);
    const vid = res?.variant_id || meta.fallbackVid || "";
    const model = res?.model || $("cfgModel").value.trim() || "";
    const provider = res?.provider || $("cfgProvider").value || "";

    const out = {
      export_type: "single_article",
      exported_at: new Date().toISOString(),
      model,
      provider,
    };
    if (!minimal && res?.params) out.generation_params = res.params;
    if (meta.post_id) out.post_id = meta.post_id;
    if (meta.source) out.source = meta.source;
    if (!minimal && meta.author) out.author = meta.author;
    if (!minimal && meta.url) out.url = meta.url;

    out.original_text = originalText || "";
    out.prompt = buildPromptMeta(res, vid, includePrompt, minimal);
    out.status = res?.status || "not_run";
    out.rewritten_text = res?.output_text || "";

    if (res?.status === "rejected") out.reject_reason = res.reject_reason || "";
    if (res?.status === "error") out.error = res.error || "";

    if (!minimal && res) {
      out.word_count = res.audit?.word_count ?? wc(res.output_text || "");
      out.audit_score = res.audit?.score ?? null;
      out.issues = extractAuditIssues(res);
      out.latency_ms = res.latency_ms ?? null;
      if (res.usage) out.usage = res.usage;
    }
    return out;
  }

  function getFilteredMatrixItems() {
    const simMap = state.matrixAnalytics?.per_variant_max_sim || {};
    let list = state.matrixItems.slice();
    if (state.matrixFilter === "g1") list = list.filter((x) => x.group === 1);
    else if (state.matrixFilter === "g2") list = list.filter((x) => x.group === 2);
    else if (state.matrixFilter === "low") list = list.filter((x) => x.status === "ok" && (x.audit?.score ?? 100) < 85);
    else if (state.matrixFilter === "overlap") list = list.filter((x) => (simMap[x.variant_id] || 0) >= 24);
    else if (state.matrixFilter === "err") list = list.filter((x) => x.status !== "ok");

    if (state.matrixSort === "score_asc") list.sort((a, b) => (a.audit?.score ?? -1) - (b.audit?.score ?? -1));
    else if (state.matrixSort === "score_desc") list.sort((a, b) => (b.audit?.score ?? -1) - (a.audit?.score ?? -1));
    else if (state.matrixSort === "overlap_desc") list.sort((a, b) => (simMap[b.variant_id] || 0) - (simMap[a.variant_id] || 0));
    else list.sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    return list;
  }

  function getFilteredRotateItems() {
    let list = state.rotateItems.slice();
    if (state.rotateFilter === "g1") list = list.filter((x) => x.group === 1);
    else if (state.rotateFilter === "g2") list = list.filter((x) => x.group === 2);
    else if (state.rotateFilter === "low") list = list.filter((x) => x.status === "ok" && (x.audit?.score ?? 100) < 85);
    else if (state.rotateFilter === "err") list = list.filter((x) => x.status !== "ok");
    return list;
  }

  function buildSmartExportPayload(scope, opts = {}) {
    const includePrompt = opts.includePrompt !== false;
    const minimal = Boolean(opts.minimal);
    const respectFilter = Boolean(opts.respectFilter);
    const includeSummary = !minimal && opts.includeSummary !== false;
    const nowIso = new Date().toISOString();
    const curModel = $("cfgModel").value.trim();
    const curProvider = $("cfgProvider").value;

    if (scope === "single") {
      const post = state.selectedPostFull;
      const hist = post?.rewrite_history || [];
      const activeRes = hist[state.activeHistoryIndex] || post?.latest_rewrite || null;
      const origText = $("originalTextArea").value || post?.text || "";
      const payload = buildSingleItemJson(
        activeRes,
        origText,
        {
          post_id: post?.id || "",
          source: post?.source || "manual",
          author: post?.crawled_from || "",
          url: post?.url || "",
          fallbackVid: state.singleVariantId,
        },
        { includePrompt, minimal }
      );
      const code = payload.prompt?.prompt_code || "single";
      return { payload, itemCount: 1, label: `Đơn bài (${code})`, fileSlug: `single_${code}` };
    }

    if (scope === "single_history") {
      const post = state.selectedPostFull;
      const hist = post?.rewrite_history || [];
      const origText = $("originalTextArea").value || post?.text || "";
      const runs = hist.map((res, idx) => {
        const row = {
          run_index: hist.length - idx,
          model: res.model || curModel,
          provider: res.provider || curProvider,
          prompt: buildPromptMeta(res, res.variant_id, includePrompt, minimal),
          status: res.status || "error",
          rewritten_text: res.output_text || "",
        };
        if (res.status === "rejected") row.reject_reason = res.reject_reason || "";
        if (res.status === "error") row.error = res.error || "";
        if (!minimal) {
          row.generation_params = res.params || null;
          row.word_count = res.audit?.word_count ?? wc(res.output_text || "");
          row.audit_score = res.audit?.score ?? null;
          row.issues = extractAuditIssues(res);
          row.latency_ms = res.latency_ms ?? null;
        }
        return row;
      });
      const payload = {
        export_type: "single_article_history",
        exported_at: nowIso,
        post_id: post?.id || "",
        source: post?.source || "manual",
        original_text: origText,
        total_runs: runs.length,
        runs,
      };
      return { payload, itemCount: runs.length, label: `Lịch sử 1 bài (${runs.length} lần)`, fileSlug: `single_history_${runs.length}runs` };
    }

    if (scope === "matrix") {
      const list = respectFilter ? getFilteredMatrixItems() : state.matrixItems.slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
      const origText = state.matrixSourceText || $("matrixSourceEditor").value || $("originalTextArea").value || "";
      const first = list[0];
      const results = list.map((res, idx) => {
        const row = {
          index: idx + 1,
          model: res.model || curModel,
          provider: res.provider || curProvider,
          prompt: buildPromptMeta(res, res.variant_id, includePrompt, minimal),
          status: res.status || "error",
          rewritten_text: res.output_text || "",
        };
        if (res.status === "rejected") row.reject_reason = res.reject_reason || "";
        if (res.status === "error") row.error = res.error || "";
        if (!minimal) {
          row.word_count = res.audit?.word_count ?? wc(res.output_text || "");
          row.audit_score = res.audit?.score ?? null;
          row.issues = extractAuditIssues(res);
          row.latency_ms = res.latency_ms ?? null;
        }
        return row;
      });
      const payload = {
        export_type: "single_article_multi_prompt",
        exported_at: nowIso,
        model: first?.model || curModel,
        provider: first?.provider || curProvider,
      };
      if (!minimal && first?.params) payload.generation_params = first.params;
      payload.original_article = {
        post_id: state.matrixPostId || state.selectedPostId || "",
        original_text: origText,
      };
      if (includeSummary && state.matrixAnalytics) {
        payload.summary = {
          total: state.matrixAnalytics.total,
          ok_count: state.matrixAnalytics.ok_count,
          avg_score: state.matrixAnalytics.avg_score,
          diversity_score: state.matrixAnalytics.diversity_score,
          avg_words_g1: state.matrixAnalytics.avg_words_g1,
          avg_words_g2: state.matrixAnalytics.avg_words_g2,
          overlap_pairs: state.matrixAnalytics.overlap_pairs,
          top_issues: state.matrixAnalytics.top_issues,
        };
      }
      payload.total_results = results.length;
      payload.results = results;
      return { payload, itemCount: results.length, label: `1 Bài × ${results.length} Prompt`, fileSlug: `matrix_${results.length}prompts` };
    }

    if (scope === "rotate") {
      const list = respectFilter ? getFilteredRotateItems() : state.rotateItems.slice().sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
      const first = list[0];
      const items = list.map((res, idx) => {
        const row = {
          index: idx + 1,
          post_id: res.post_id || "",
          source: res.post_source || "",
          model: res.model || curModel,
          provider: res.provider || curProvider,
          original_text: res.original_text || "",
          prompt: buildPromptMeta(res, res.variant_id, includePrompt, minimal),
          status: res.status || "error",
          rewritten_text: res.output_text || "",
        };
        if (res.status === "rejected") row.reject_reason = res.reject_reason || "";
        if (res.status === "error") row.error = res.error || "";
        if (!minimal) {
          row.word_count = res.audit?.word_count ?? wc(res.output_text || "");
          row.audit_score = res.audit?.score ?? null;
          row.issues = extractAuditIssues(res);
          row.latency_ms = res.latency_ms ?? null;
        }
        return row;
      });
      const payload = {
        export_type: "multi_article_rotate_prompts",
        exported_at: nowIso,
        model: first?.model || curModel,
        provider: first?.provider || curProvider,
      };
      if (!minimal && first?.params) payload.generation_params = first.params;
      if (includeSummary && state.rotateAnalytics) {
        payload.summary = {
          total: state.rotateAnalytics.total,
          ok_count: state.rotateAnalytics.ok_count,
          avg_score: state.rotateAnalytics.avg_score,
          diversity_score: state.rotateAnalytics.diversity_score,
          overlap_pairs: state.rotateAnalytics.overlap_pairs,
          top_issues: state.rotateAnalytics.top_issues,
        };
      }
      payload.total_items = items.length;
      payload.items = items;
      return { payload, itemCount: items.length, label: `Xoay 1-1 (${items.length} cặp)`, fileSlug: `rotate_${items.length}pairs` };
    }

    // Scope: all_rewritten — all posts in current storage that have latest_rewrite
    const rewrittenPosts = state.posts.filter((p) => p.latest_rewrite);
    const items = rewrittenPosts.map((p, idx) => {
      const res = p.latest_rewrite;
      const row = {
        index: idx + 1,
        post_id: p.id,
        source: p.source,
        model: res.model || curModel,
        provider: res.provider || curProvider,
        original_text: p.text || "",
        prompt: buildPromptMeta(res, res.variant_id, includePrompt, minimal),
        status: res.status || "error",
        rewritten_text: res.output_text || "",
      };
      if (res.status === "rejected") row.reject_reason = res.reject_reason || "";
      if (res.status === "error") row.error = res.error || "";
      if (!minimal) {
        row.author = p.crawled_from || "";
        row.url = p.url || "";
        row.word_count = res.audit?.word_count ?? wc(res.output_text || "");
        row.audit_score = res.audit?.score ?? null;
        row.issues = extractAuditIssues(res);
        row.latency_ms = res.latency_ms ?? null;
      }
      return row;
    });
    const payload = {
      export_type: "multi_article_all_rewritten",
      exported_at: nowIso,
      storage_mode: state.storage?.mode || "ram",
      total_items: items.length,
      items,
    };
    return { payload, itemCount: items.length, label: `Toàn kho (${items.length} bài)`, fileSlug: `all_rewritten_${items.length}posts` };
  }

  function makeExportFilename(slug) {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}`;
    return `okxsquare_${slug}_${stamp}.json`;
  }

  function getDefaultScopeForCurrentMode() {
    if (state.mode === "matrix") return "matrix";
    if (state.mode === "rotate") return "rotate";
    return "single";
  }

  function updateExportModalPreview() {
    const scope = $("expScope").value;
    const minimal = $("expMinimal").checked;
    $("expIncludeSummary").disabled = minimal;
    $("expSummaryWrap").style.opacity = minimal ? "0.45" : "1";

    const built = buildSmartExportPayload(scope, {
      includePrompt: $("expIncludePrompt").checked,
      minimal,
      respectFilter: $("expRespectFilter").checked,
      includeSummary: $("expIncludeSummary").checked,
    });
    const jsonStr = JSON.stringify(built.payload, null, 2);
    const bytes = new Blob([jsonStr]).size;
    const estTok = Math.max(1, Math.round(jsonStr.length / 4));
    $("expMetaStats").textContent = `${built.itemCount} mục · ${fmtBytes(bytes)} · ~${estTok.toLocaleString("vi-VN")} tok`;
    $("expFilenameHint").textContent = makeExportFilename(built.fileSlug);

    // Truncate DOM preview safely for huge exports while keeping copy/download 100% full
    if (jsonStr.length > 120000) {
      $("exportJsonPreview").textContent = jsonStr.slice(0, 120000) + `\n\n... [Đã rút gọn hiển thị — Copy / Tải file vẫn chứa đủ 100% ${fmtBytes(bytes)}]`;
    } else {
      $("exportJsonPreview").textContent = jsonStr;
    }
    return { ...built, jsonStr, bytes, filename: makeExportFilename(built.fileSlug) };
  }

  function openExportJsonModal() {
    const defaultScope = getDefaultScopeForCurrentMode();
    $("expScope").value = defaultScope;
    const hasFilter =
      (defaultScope === "matrix" && state.matrixFilter !== "all") ||
      (defaultScope === "rotate" && state.rotateFilter !== "all");
    $("expRespectFilter").checked = hasFilter;
    updateExportModalPreview();
    $("exportJsonModal").classList.remove("hidden");
  }

  function downloadJsonString(jsonStr, filename) {
    const blob = new Blob([jsonStr], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function copyJsonText(jsonStr, label) {
    try {
      await navigator.clipboard.writeText(jsonStr);
      const bytes = new Blob([jsonStr]).size;
      toast(`Đã copy JSON · ${label} (${fmtBytes(bytes)})`, "ok");
    } catch {
      toast("Không thể truy cập clipboard — hãy mở nút Xuất để tải file .json", "err");
    }
  }

  // =========================================================================
  // Ping Provider
  // =========================================================================
  async function pingProvider(silent = false) {
    $("pingDot").className = "status-dot busy";
    $("headerPingLatency").textContent = "...";
    const p = collectRuntimePayload();
    const { data } = await api("/api/config/ping", {
      method: "POST",
      body: JSON.stringify({ provider: p.provider, overrides: p.overrides }),
    });
    if (data.ok) {
      $("pingDot").className = "status-dot ok";
      $("headerPingLatency").textContent = `${data.latency_ms}ms`;
      if (!silent) toast(`Ping ${data.provider} OK (${data.latency_ms}ms)`, "ok");
    } else {
      $("pingDot").className = "status-dot err";
      $("headerPingLatency").textContent = "ERR";
      if (!silent) toast(`Ping lỗi: ${data.error}`, "err");
    }
  }

  // =========================================================================
  // Word Diff
  // =========================================================================
  function buildWordDiffHtml(oldStr, newStr) {
    const a = (oldStr || "").split(/(\s+)/);
    const b = (newStr || "").split(/(\s+)/);
    if (a.length * b.length > 160000) {
      return `<div class="diff-del">${esc(oldStr)}</div><br/><div class="diff-ins">${esc(newStr)}</div>`;
    }
    const m = a.length, n = b.length;
    const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
    for (let i = m - 1; i >= 0; i--) {
      for (let j = n - 1; j >= 0; j--) {
        dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    let i = 0, j = 0;
    const out = [];
    while (i < m && j < n) {
      if (a[i] === b[j]) { out.push(esc(b[j])); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) {
        out.push(a[i].trim() ? `<span class="diff-del">${esc(a[i])}</span>` : esc(a[i])); i++;
      } else {
        out.push(b[j].trim() ? `<span class="diff-ins">${esc(b[j])}</span>` : esc(b[j])); j++;
      }
    }
    while (i < m) { out.push(a[i].trim() ? `<span class="diff-del">${esc(a[i])}</span>` : esc(a[i])); i++; }
    while (j < n) { out.push(b[j].trim() ? `<span class="diff-ins">${esc(b[j])}</span>` : esc(b[j])); j++; }
    return out.join("");
  }

  // =========================================================================
  // Real-time Log Dock (SSE)
  // =========================================================================
  async function initLogs() {
    const { data } = await api("/api/logs?limit=200");
    if (data.ok && Array.isArray(data.logs)) {
      state.logs = data.logs;
      renderAllLogs();
    }
    const es = new EventSource("/api/logs/stream");
    state.eventSource = es;
    es.onopen = () => $("sseDot").classList.remove("disconnected");
    es.onerror = () => $("sseDot").classList.add("disconnected");
    es.onmessage = (ev) => {
      try {
        const entry = JSON.parse(ev.data);
        if (state.logs.some((x) => x.id === entry.id)) return;
        state.logs.push(entry);
        if (state.logs.length > 1500) state.logs.shift();
        updateLogHeader(entry);
        if (matchesLogFilter(entry)) appendLogRow(entry);
      } catch { /* heartbeat */ }
    };
  }

  function updateLogHeader(latestEntry = null) {
    const errCount = state.logs.filter((e) => e.level === "ERROR").length;
    $("logErrorCount").textContent = String(errCount);
    const errBtn = document.querySelector('#logLevelPills .lpill[data-level="ERROR"]');
    if (errBtn) errBtn.classList.toggle("has-errors", errCount > 0);
    const last = latestEntry || state.logs[state.logs.length - 1];
    if (last) {
      $("logLatestPreview").textContent = `[${last.source}] ${last.message}`;
      $("logLatestPreview").style.color = last.level === "ERROR" ? "#fca5a5" : "var(--ink-muted)";
    }
  }

  function matchesLogFilter(e) {
    if (state.logFilterLevel !== "ALL" && e.level !== state.logFilterLevel) return false;
    if (state.logSearch) {
      const q = state.logSearch.toLowerCase();
      if (!`${e.message} ${e.source} ${e.details || ""}`.toLowerCase().includes(q)) return false;
    }
    return true;
  }

  function createLogRow(e) {
    const row = document.createElement("div");
    row.className = `log-row ${e.level}${e.details ? " has-details" : ""}`;
    row.innerHTML = `
      <span class="log-ts">${esc(e.time)}</span>
      <span class="log-lvl ${esc(e.level)}">[${esc(e.level)}]</span>
      <span class="log-src">${esc(e.source)}</span>
      <span class="log-msg">${esc(e.message)}${e.details ? ' <span class="muted-tag">[+trace]</span>' : ""}</span>`;
    if (e.details) {
      const pre = document.createElement("pre");
      pre.className = "log-detail-pre hidden";
      pre.textContent = e.details;
      row.appendChild(pre);
      row.addEventListener("click", () => pre.classList.toggle("hidden"));
    }
    return row;
  }

  function appendLogRow(e) {
    const s = $("logStream");
    s.appendChild(createLogRow(e));
    s.scrollTop = s.scrollHeight;
  }

  function renderAllLogs() {
    updateLogHeader();
    const s = $("logStream");
    s.innerHTML = "";
    const frag = document.createDocumentFragment();
    state.logs.filter(matchesLogFilter).forEach((e) => frag.appendChild(createLogRow(e)));
    s.appendChild(frag);
    s.scrollTop = s.scrollHeight;
  }

  // =========================================================================
  // Event Wiring
  // =========================================================================
  function bindEvents() {
    // 3 Mode tabs
    document.querySelectorAll("#modeNav .mode-btn").forEach((b) => {
      b.addEventListener("click", () => setMode(b.dataset.mode));
    });

    // Sidebar tabs
    document.querySelectorAll("#sidebarTabs .stab").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#sidebarTabs .stab").forEach((x) => x.classList.remove("active"));
        document.querySelectorAll(".stab-pane").forEach((p) => p.classList.remove("active"));
        b.classList.add("active");
        $(b.dataset.stab === "prompts" ? "stabPrompts" : "stabPosts").classList.add("active");
      });
    });

    // Config drawer & two-way Concurrency Sync
    const toggleCfg = () => $("configDrawer").classList.toggle("hidden");
    $("btnToggleConfig").addEventListener("click", toggleCfg);
    $("btnCloseConfig").addEventListener("click", () => $("configDrawer").classList.add("hidden"));
    $("ribbonParamSummary").addEventListener("click", toggleCfg);
    $("headerProviderPill").addEventListener("click", () => pingProvider(false));
    $("btnPingProvider").addEventListener("click", () => pingProvider(false));

    $("cfgProvider").addEventListener("change", (e) => populateProviderUI(e.target.value));
    $("cfgModel").addEventListener("input", updateRibbonParamSummary);
    $("ribbonConcurrency").addEventListener("change", (e) => {
      $("cfgConcurrency").value = e.target.value;
    });
    $("cfgConcurrency").addEventListener("input", (e) => setConcurrencyValue(e.target.value));
    $("rangeTemp").addEventListener("input", (e) => { $("cfgTemp").value = e.target.value; $("valTempLabel").textContent = Number(e.target.value).toFixed(2); updateRibbonParamSummary(); });
    $("cfgTemp").addEventListener("input", (e) => { $("rangeTemp").value = e.target.value; $("valTempLabel").textContent = Number(e.target.value || 0).toFixed(2); updateRibbonParamSummary(); });
    $("rangeTopP").addEventListener("input", (e) => { $("cfgTopP").value = e.target.value; $("valTopPLabel").textContent = Number(e.target.value).toFixed(2); updateRibbonParamSummary(); });
    $("cfgTopP").addEventListener("input", (e) => { $("rangeTopP").value = e.target.value; $("valTopPLabel").textContent = Number(e.target.value || 0).toFixed(2); updateRibbonParamSummary(); });

    $("btnReloadConfig").addEventListener("click", async () => {
      const { data } = await api("/api/config/reload", { method: "POST" });
      if (data.ok) {
        state.config = data.config;
        populateProviderUI(state.config.active_provider);
        applyPromptsCatalog(data.prompts, false);
        toast("Đã reload gemini_config.yaml & 10 prompts", "ok");
      }
    });
    $("btnResetYamlDefaults").addEventListener("click", () => {
      $("cfgApiKeyOverride").value = "";
      populateProviderUI($("cfgProvider").value);
      toast("Đã khôi phục thông số YAML", "ok");
    });

    // Storage mode & modal
    document.querySelectorAll("#storageModeSwitch .seg-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const mode = btn.dataset.mode;
        if (state.storage?.mode === mode) return;
        const { data } = await api("/api/storage/mode", { method: "POST", body: JSON.stringify({ mode, carry_over: true }) });
        if (data.ok) {
          renderStorageState(data.storage);
          await loadPosts(true);
          toast(`Chuyển lưu trữ sang ${mode.toUpperCase()}`, "ok");
        }
      });
    });
    $("btnStorageMenu").addEventListener("click", async () => {
      const { data } = await api("/api/storage");
      if (data.ok) renderStorageState(data.storage);
      $("storageModal").classList.remove("hidden");
    });
    $("btnSyncToDisk").addEventListener("click", async () => {
      const { data } = await api("/api/storage/sync", { method: "POST", body: JSON.stringify({ direction: "to_disk" }) });
      if (data.ok) { renderStorageState(data.storage); toast(`Đã lưu ${data.count} bài xuống ổ cứng`, "ok"); }
    });
    $("btnLoadFromDisk").addEventListener("click", async () => {
      const { data } = await api("/api/storage/sync", { method: "POST", body: JSON.stringify({ direction: "from_disk" }) });
      if (data.ok) {
        renderStorageState(data.storage);
        await Promise.all([loadPosts(true), loadSavedSessions()]);
        toast(`Đã nạp ${data.count} bài từ ổ cứng`, "ok");
      }
    });
    $("btnImportProdDb").addEventListener("click", async () => {
      const { data } = await api("/api/storage/import-prod", { method: "POST", body: JSON.stringify({ limit: 25 }) });
      if (data.ok) { renderStorageState(data.storage); await loadPosts(true); $("storageModal").classList.add("hidden"); toast(`Import +${data.result.imported} bài`, "ok"); }
      else toast(data.error, "err");
    });
    $("btnClearRewritesOnly").addEventListener("click", async () => {
      const { data } = await api("/api/storage/clear", { method: "POST", body: JSON.stringify({ scope: "rewrites_only" }) });
      if (data.ok) {
        state.matrixItems = []; state.matrixAnalytics = null;
        state.rotateItems = []; state.rotateAnalytics = null;
        state.promptLastScores = {};
        renderMatrixView(); renderRotateView(); renderPromptRail();
        await loadPosts(true);
        toast(`Đã reset rewrite (${data.removed} bài)`, "warn");
      }
    });
    $("btnClearAllPosts").addEventListener("click", async () => {
      if (!confirm("Xóa toàn bộ bài viết trong bộ nhớ test?")) return;
      const { data } = await api("/api/storage/clear", { method: "POST", body: JSON.stringify({ scope: "all" }) });
      if (data.ok) {
        state.selectedPostId = null; state.selectedPostFull = null;
        state.matrixItems = []; state.matrixAnalytics = null; state.matrixPostId = ""; state.matrixSourceText = "";
        state.rotateItems = []; state.rotateAnalytics = null;
        state.promptLastScores = {};
        $("originalTextArea").value = "";
        checkSafetyText("", $("origSafetyBadge"), $("origStats"));
        renderSingleResult(null, []);
        renderMatrixView(); renderRotateView(); renderPromptRail();
        await loadPosts(false);
        $("storageModal").classList.add("hidden");
        toast(`Đã xóa ${data.removed} bài`, "warn");
      }
    });

    // Crawl box
    $("btnToggleCrawlBox").addEventListener("click", () => $("crawlBox").classList.toggle("hidden"));
    document.querySelectorAll("#crawlSourcePills input").forEach((inp) => {
      inp.addEventListener("change", () => {
        inp.closest(".source-pill").classList.toggle("active", inp.checked);
        const binOn = document.querySelector('#crawlSourcePills input[value="binance"]')?.checked;
        const okxOn = document.querySelector('#crawlSourcePills input[value="okx"]')?.checked;
        $("binanceExtraBox").classList.toggle("hidden", !binOn);
        $("okxExtraBox").classList.toggle("hidden", !okxOn);
        if (okxOn && !okxTopicsLoadedOnce) loadOkxTrendingTopics(false);
      });
    });
    $("okxPresetChips").addEventListener("click", (e) => {
      const chip = e.target.closest(".tag-chip");
      if (chip) chip.classList.toggle("active");
    });
    $("okxTrendingChips").addEventListener("click", (e) => {
      const chip = e.target.closest(".tag-chip");
      if (chip) chip.classList.toggle("active");
    });
    $("btnLoadOkxTopics").addEventListener("click", () => loadOkxTrendingTopics(true));
    $("btnStartCrawl").addEventListener("click", () => startCrawl());
    $("btnCrawlToFill20").addEventListener("click", () => startCrawl({ sources: ["kucoin", "sosovalue"], max_posts_per_source: 12, lookback_hours: 72 }));

    // Queue list
    $("filterSource").addEventListener("change", () => loadPosts(true));
    let qTimer = null;
    $("queueSearch").addEventListener("input", () => { clearTimeout(qTimer); qTimer = setTimeout(() => loadPosts(true), 200); });
    $("queueList").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-del-id]");
      if (del) {
        e.stopPropagation();
        await api(`/api/posts/${encodeURIComponent(del.dataset.delId)}`, { method: "DELETE" });
        if (state.selectedPostId === del.dataset.delId) state.selectedPostId = null;
        await loadPosts(true);
        return;
      }
      const card = e.target.closest(".post-card");
      if (card) await selectPost(card.dataset.id, true);
    });

    // 20 Prompts Rail interactions
    document.querySelectorAll(".prompt-select-bar [data-psel]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const t = btn.dataset.psel;
        if (t === "all") state.checkedPrompts = new Set(getAllPromptsOrdered().map((p) => p.id));
        else if (t === "g1") state.checkedPrompts = new Set((state.prompts?.group1 || []).map((p) => p.id));
        else if (t === "g2") state.checkedPrompts = new Set((state.prompts?.group2 || []).map((p) => p.id));
        else state.checkedPrompts.clear();
        renderPromptRail();
        updateCheckedPromptLabels();
      });
    });
    $("btnSaveAllDrafts").addEventListener("click", saveAllDirtyDrafts);

    $("promptRailList").addEventListener("click", (e) => {
      const chk = e.target.closest("[data-pchk]");
      if (chk) {
        e.stopPropagation();
        if (chk.checked) state.checkedPrompts.add(chk.dataset.pchk);
        else state.checkedPrompts.delete(chk.dataset.pchk);
        updateCheckedPromptLabels();
        if (state.mode === "rotate" && !state.rotateItems.length) renderRotateView();
        return;
      }
      const item = e.target.closest("[data-pvid]");
      if (!item) return;
      const vid = item.dataset.pvid;
      if (state.mode === "single") loadInlinePromptEditor(vid);
      else if (state.mode === "matrix") openPromptDrawer("matrix", vid);
      else if (state.mode === "rotate") openPromptDrawer("rotate", vid);
    });

    // Mode 1 controls
    $("singlePromptSelect").addEventListener("change", (e) => loadInlinePromptEditor(e.target.value));
    $("btnSyncPromptToOutput").addEventListener("click", (e) => {
      const vid = e.target.dataset.syncVid;
      if (vid) loadInlinePromptEditor(vid);
    });
    $("btnToggleInlineEditor").addEventListener("click", () => {
      const hidden = $("inlinePromptEditor").classList.toggle("hidden");
      $("btnToggleInlineEditor").classList.toggle("on", !hidden);
    });
    $("btnToggleInlineEditor").classList.add("on");

    $("originalTextArea").addEventListener("input", (e) => {
      checkSafetyText(e.target.value, $("origSafetyBadge"), $("origStats"));
      setOrigDirty(e.target.value !== (state.selectedPostFull?.text || ""));
      if (!state.matrixItems.length) syncMatrixSourcePreview();
    });
    $("btnResetOrigText").addEventListener("click", () => {
      if (!state.selectedPostFull) return;
      const orig = state.selectedPostFull.original_text_initial ?? state.selectedPostFull.text ?? "";
      $("originalTextArea").value = orig;
      checkSafetyText(orig, $("origSafetyBadge"), $("origStats"));
      setOrigDirty(orig !== (state.selectedPostFull.text || ""));
      toast("Đã hoàn tác về nội dung gốc ban đầu", "ok");
    });
    $("btnSaveOrigText").addEventListener("click", async () => {
      if (!state.selectedPostId) return;
      const text = $("originalTextArea").value;
      const { data } = await api(`/api/posts/${encodeURIComponent(state.selectedPostId)}`, {
        method: "PUT",
        body: JSON.stringify({ text }),
      });
      if (data.ok) {
        state.selectedPostFull = data.post;
        setOrigDirty(false);
        await loadPosts(false);
        toast("Đã lưu nội dung gốc", "ok");
      }
    });
    $("btnCopyOriginal").addEventListener("click", () => { navigator.clipboard.writeText($("originalTextArea").value || ""); toast("Đã copy bài gốc", "ok"); });
    $("btnCopyRewritten").addEventListener("click", () => { navigator.clipboard.writeText($("rwRenderedText").textContent || ""); toast("Đã copy bài đã xử lý", "ok"); });
    $("btnCopySingleJson").addEventListener("click", () => {
      const built = buildSmartExportPayload("single", { includePrompt: true, minimal: false });
      copyJsonText(JSON.stringify(built.payload, null, 2), built.label);
    });

    // Smart Context-Aware JSON Export (Action Ribbon + Modal)
    $("btnQuickCopyJson").addEventListener("click", () => {
      const scope = getDefaultScopeForCurrentMode();
      const respectFilter =
        (scope === "matrix" && state.matrixFilter !== "all") ||
        (scope === "rotate" && state.rotateFilter !== "all");
      const built = buildSmartExportPayload(scope, {
        includePrompt: true,
        minimal: false,
        respectFilter,
        includeSummary: true,
      });
      if (built.itemCount === 0) {
        toast("Chưa có kết quả rewrite ở chế độ này để copy JSON!", "warn");
        return;
      }
      copyJsonText(JSON.stringify(built.payload, null, 2), built.label);
    });
    $("btnOpenExportJson").addEventListener("click", openExportJsonModal);
    ["expScope", "expIncludePrompt", "expMinimal", "expRespectFilter", "expIncludeSummary"].forEach((id) => {
      $(id).addEventListener("change", updateExportModalPreview);
    });
    $("btnModalCopyJson").addEventListener("click", () => {
      const built = updateExportModalPreview();
      copyJsonText(built.jsonStr, built.label);
    });
    $("btnModalDownloadJson").addEventListener("click", () => {
      const built = updateExportModalPreview();
      downloadJsonString(built.jsonStr, built.filename);
      toast(`Đã tải ${built.filename}`, "ok");
    });

    $("inlinePeTextarea").addEventListener("input", (e) => {
      const vid = state.singleVariantId;
      if (!vid) return;
      state.promptDrafts[vid] = e.target.value;
      updateInlinePeBadges(vid, e.target.value);
      populateSinglePromptDropdown();
      renderPromptRail();
    });
    $("btnInlineRevertDraft").addEventListener("click", () => {
      const vid = state.singleVariantId;
      delete state.promptDrafts[vid];
      loadInlinePromptEditor(vid);
      populateSinglePromptDropdown();
      toast("Đã hủy bản nháp", "ok");
    });
    $("btnInlineRestoreBackup").addEventListener("click", () => restorePromptFromBackup(state.singleVariantId));
    $("btnInlineTestDraft").addEventListener("click", () => runSingleRewrite(true));
    $("btnInlineSavePrompt").addEventListener("click", () => savePromptToDisk(state.singleVariantId, $("inlinePeTextarea").value));

    $("btnRunSingle").addEventListener("click", () => runSingleRewrite(false));
    $("btnSpineRewrite").addEventListener("click", () => runSingleRewrite(false));
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && state.mode === "single") {
        e.preventDefault();
        runSingleRewrite(false);
      }
    });

    document.querySelectorAll("#rwViewTabs .vtab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#rwViewTabs .vtab").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".rw-view").forEach((v) => v.classList.remove("active"));
        btn.classList.add("active");
        const map = { rendered: "viewRendered", iterdiff: "viewIterDiff", raw: "viewRaw", history: "viewHistory" };
        $(map[btn.dataset.view]).classList.add("active");
      });
    });
    $("btnToggleTraceback").addEventListener("click", () => $("rwErrorTrace").classList.toggle("hidden"));
    $("rwHistoryList").addEventListener("click", (e) => {
      const item = e.target.closest("[data-hidx]");
      if (!item || !state.selectedPostFull) return;
      state.activeHistoryIndex = Number(item.dataset.hidx);
      const hist = state.selectedPostFull.rewrite_history || [];
      renderSingleResult(hist[state.activeHistoryIndex], hist);
      document.querySelector('#rwViewTabs .vtab[data-view="rendered"]').click();
    });

    // Mode 2 Split-Desk controls
    $("matrixScopeSelect").addEventListener("change", updateMatrixCtaLabel);
    $("btnRunMatrix").addEventListener("click", () => runMatrixAllPrompts());
    $("btnCopyMatrixOrig").addEventListener("click", () => { navigator.clipboard.writeText($("matrixSourceEditor").value || ""); toast("Đã copy bài gốc", "ok"); });
    $("btnCollapseMatrixOrig").addEventListener("click", () => {
      $("matrixSplit").classList.add("orig-collapsed");
      $("btnShowMatrixOrig").classList.remove("hidden");
    });
    $("btnShowMatrixOrig").addEventListener("click", () => {
      $("matrixSplit").classList.remove("orig-collapsed");
      $("btnShowMatrixOrig").classList.add("hidden");
    });
    $("btnMatrixViewOutputOrig").addEventListener("click", () => {
      if (state.matrixPostId) selectPost(state.matrixPostId, true);
      else {
        $("matrixSourceEditor").value = state.matrixSourceText;
        $("matrixMismatchBanner").classList.add("hidden");
      }
    });
    $("btnMatrixRerunNewPost").addEventListener("click", () => {
      const newText = $("originalTextArea").value.trim() || state.selectedPostFull?.text || "";
      runMatrixAllPrompts(newText, state.selectedPostId || "");
    });
    $("matrixSourceEditor").addEventListener("input", (e) => {
      state.matrixSourceText = e.target.value;
      checkSafetyText(e.target.value, $("matrixOrigSafety"), $("matrixOrigStats"));
      if (!state.matrixItems.length) {
        $("originalTextArea").value = e.target.value;
        checkSafetyText(e.target.value, $("origSafetyBadge"), $("origStats"));
      }
    });

    document.querySelectorAll("#matrixColSwitch .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#matrixColSwitch .seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.matrixCols = btn.dataset.cols;
        $("matrixGrid").className = `matrix-grid cols-${state.matrixCols}`;
      });
    });

    document.querySelectorAll("#matrixFilterPills .fpill").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#matrixFilterPills .fpill").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.matrixFilter = b.dataset.mf;
        state.matrixHighlightPair = null;
        renderMatrixView();
      });
    });
    $("matrixSortSelect").addEventListener("change", (e) => { state.matrixSort = e.target.value; renderMatrixView(); });

    $("matrixGrid").addEventListener("click", (e) => {
      const editBtn = e.target.closest("[data-m-edit]");
      if (editBtn) return openPromptDrawer("matrix", editBtn.dataset.mEdit);
      const rerunBtn = e.target.closest("[data-m-rerun]");
      if (rerunBtn) return rerunSingleCardInPlace("matrix", rerunBtn.dataset.mRerun);
      const focusBtn = e.target.closest("[data-m-focus]");
      if (focusBtn) {
        loadInlinePromptEditor(focusBtn.dataset.mFocus);
        setMode("single");
        return;
      }
      const jsonBtn = e.target.closest("[data-m-json]");
      if (jsonBtn) {
        const it = state.matrixItems.find((x) => x.variant_id === jsonBtn.dataset.mJson);
        if (it) {
          const origText = state.matrixSourceText || $("matrixSourceEditor").value || "";
          const obj = buildSingleItemJson(it, origText, { post_id: state.matrixPostId, source: state.selectedPostFull?.source || "" });
          copyJsonText(JSON.stringify(obj, null, 2), `${state.promptsById[it.variant_id]?.code || it.variant_id}`);
        }
        return;
      }
      const copyBtn = e.target.closest("[data-m-copy]");
      if (copyBtn) {
        const it = state.matrixItems.find((x) => x.variant_id === copyBtn.dataset.mCopy);
        if (it?.output_text) { navigator.clipboard.writeText(it.output_text); toast("Đã copy output", "ok"); }
      }
    });

    // Mode 3 (Rotate 1-1) controls
    $("rotateScopeSelect").addEventListener("change", () => { updateRotateReadiness(); if (!state.rotateItems.length) renderRotateView(); });
    $("chkRotateFillAll").addEventListener("change", () => { updateRotateReadiness(); if (!state.rotateItems.length) renderRotateView(); });
    $("btnRunRotate").addEventListener("click", runRotatePrompts);
    document.querySelectorAll("#rotateFilterPills .fpill").forEach((b) => {
      b.addEventListener("click", () => {
        document.querySelectorAll("#rotateFilterPills .fpill").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        state.rotateFilter = b.dataset.rf;
        renderRotateView();
      });
    });
    $("rotateCoverageDots").addEventListener("click", (e) => {
      const dot = e.target.closest("[data-cov-vid]");
      if (!dot) return;
      const card = $(`pair-${dot.dataset.covVid}`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
      else openPromptDrawer("rotate", dot.dataset.covVid);
    });
    $("rotateList").addEventListener("click", async (e) => {
      const editBtn = e.target.closest("[data-r-edit]");
      if (editBtn) return openPromptDrawer("rotate", editBtn.dataset.rEdit);
      const rerunBtn = e.target.closest("[data-r-rerun]");
      if (rerunBtn) return rerunSingleCardInPlace("rotate", rerunBtn.dataset.rRerun);
      const focusBtn = e.target.closest("[data-r-focus]");
      if (focusBtn) {
        const vid = focusBtn.dataset.rFocus;
        const pid = focusBtn.dataset.rPid;
        if (pid) await selectPost(pid, true);
        loadInlinePromptEditor(vid);
        setMode("single");
        return;
      }
      const jsonBtn = e.target.closest("[data-r-json]");
      if (jsonBtn) {
        const it = state.rotateItems.find((x) => x.variant_id === jsonBtn.dataset.rJson);
        if (it) {
          const obj = buildSingleItemJson(it, it.original_text || "", {
            post_id: it.post_id || "",
            source: it.post_source || "",
            author: it.post_author || "",
            url: it.post_url || "",
          });
          copyJsonText(JSON.stringify(obj, null, 2), `${state.promptsById[it.variant_id]?.code || it.variant_id}`);
        }
        return;
      }
      const copyBtn = e.target.closest("[data-r-copy]");
      if (copyBtn) {
        const it = state.rotateItems.find((x) => x.variant_id === copyBtn.dataset.rCopy);
        if (it?.output_text) { navigator.clipboard.writeText(it.output_text); toast("Đã copy output", "ok"); }
      }
    });

    // Slide-Over Prompt Drawer
    const closeDrawer = () => $("promptDrawerBackdrop").classList.add("hidden");
    $("btnClosePromptDrawer").addEventListener("click", closeDrawer);
    $("promptDrawerBackdrop").addEventListener("click", (e) => { if (e.target === $("promptDrawerBackdrop")) closeDrawer(); });
    document.querySelectorAll("#drawerPreviewTabs .vtab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#drawerPreviewTabs .vtab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.drawerTab = btn.dataset.dtab;
        if (!state.drawerTarget) return;
        const arr = state.drawerTarget.context === "matrix" ? state.matrixItems : state.rotateItems;
        renderDrawerPreview(arr.find((x) => x.variant_id === state.drawerTarget.variantId));
      });
    });
    $("drawerPeTextarea").addEventListener("input", (e) => {
      if (!state.drawerTarget) return;
      const vid = state.drawerTarget.variantId;
      state.promptDrafts[vid] = e.target.value;
      updateDrawerBadges(vid, e.target.value);
      if (vid === state.singleVariantId) loadInlinePromptEditor(vid);
      renderPromptRail();
    });
    $("btnDrawerRevert").addEventListener("click", () => {
      if (!state.drawerTarget) return;
      const vid = state.drawerTarget.variantId;
      delete state.promptDrafts[vid];
      const orig = state.promptsById[vid]?.content || "";
      $("drawerPeTextarea").value = orig;
      updateDrawerBadges(vid, orig);
      if (vid === state.singleVariantId) loadInlinePromptEditor(vid);
      renderPromptRail();
    });
    $("btnDrawerRestore").addEventListener("click", async () => {
      if (!state.drawerTarget) return;
      const vid = state.drawerTarget.variantId;
      if (await restorePromptFromBackup(vid)) {
        const orig = state.promptsById[vid]?.content || "";
        $("drawerPeTextarea").value = orig;
        updateDrawerBadges(vid, orig);
      }
    });
    $("btnDrawerTestOne").addEventListener("click", async () => {
      if (!state.drawerTarget) return;
      $("btnDrawerTestOne").disabled = true;
      await rerunSingleCardInPlace(state.drawerTarget.context, state.drawerTarget.variantId, $("drawerPeTextarea").value);
      $("btnDrawerTestOne").disabled = false;
    });
    $("btnDrawerSave").addEventListener("click", async () => {
      if (!state.drawerTarget) return;
      const vid = state.drawerTarget.variantId;
      if (await savePromptToDisk(vid, $("drawerPeTextarea").value)) {
        updateDrawerBadges(vid, $("drawerPeTextarea").value);
      }
    });

    // Job Cancel
    $("btnCancelJob").addEventListener("click", async () => {
      if (!state.activeJobId) return;
      await api(`/api/jobs/${encodeURIComponent(state.activeJobId)}/cancel`, { method: "POST" });
      toast("Đang gửi lệnh dừng...", "warn");
    });

    // Manual post modal
    $("btnOpenManualModal").addEventListener("click", () => { $("manualModal").classList.remove("hidden"); $("manualText").focus(); });
    $("btnSampleCrypto").addEventListener("click", () => { $("manualSource").value = "binance"; $("manualText").value = SAMPLES.crypto; });
    $("btnSampleSensitive").addEventListener("click", () => { $("manualSource").value = "kucoin"; $("manualText").value = SAMPLES.sensitive; });
    $("btnManualSave").addEventListener("click", async () => {
      const text = $("manualText").value.trim();
      if (!text) return toast("Nhập nội dung bài viết!", "warn");
      const { data } = await api("/api/posts/manual", {
        method: "POST",
        body: JSON.stringify({ text, source: $("manualSource").value, author: $("manualAuthor").value.trim() || "manual_test" }),
      });
      if (!data.ok) return toast(data.error, "err");
      $("manualModal").classList.add("hidden");
      $("manualText").value = "";
      await loadPosts(false);
      await selectPost(data.post.id, true);
    });

    document.querySelectorAll("[data-close]").forEach((btn) => btn.addEventListener("click", () => $(btn.dataset.close).classList.add("hidden")));
    document.querySelectorAll(".modal-backdrop").forEach((bd) => bd.addEventListener("click", (e) => { if (e.target === bd) bd.classList.add("hidden"); }));

    // Log Dock
    document.querySelectorAll("#logLevelPills .lpill").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#logLevelPills .lpill").forEach((x) => x.classList.remove("active"));
        btn.classList.add("active");
        state.logFilterLevel = btn.dataset.level;
        if ($("logDock").dataset.size === "min") setLogDockSize("normal");
        renderAllLogs();
      });
    });
    $("logSearch").addEventListener("input", (e) => { state.logSearch = e.target.value.trim(); renderAllLogs(); });
    $("btnClearLogs").addEventListener("click", async () => { await api("/api/logs/clear", { method: "POST" }); state.logs = []; renderAllLogs(); });
    $("btnCopyLogs").addEventListener("click", () => {
      const txt = state.logs.filter(matchesLogFilter).map((e) => `${e.iso} [${e.level}] [${e.source}] ${e.message}${e.details ? "\n" + e.details : ""}`).join("\n");
      navigator.clipboard.writeText(txt);
      toast("Đã copy log", "ok");
    });

    function setLogDockSize(sz) {
      $("logDock").dataset.size = sz;
      $("btnCycleLogSize").textContent = sz === "min" ? "▲ Mở Log" : sz === "normal" ? "▲ Phóng to" : "▼ Thu gọn";
    }
    $("btnCycleLogSize").addEventListener("click", () => {
      const cur = $("logDock").dataset.size;
      setLogDockSize(cur === "min" ? "normal" : cur === "normal" ? "max" : "min");
    });
  }

  async function init() {
    bindEvents();
    await Promise.all([loadConfigAndPrompts(), loadPosts(false), loadSavedSessions(), initLogs()]);
    pingProvider(true);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
