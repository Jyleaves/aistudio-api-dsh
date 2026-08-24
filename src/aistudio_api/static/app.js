
function app() {
  return {
    view: 'accounts', sidebarOpen: false, configOpen: false, openSelect: null,
    guideProtocol: 'openai',
    stats: {},
    accounts: [], accountMetrics: {}, activeId: '', activeAccount: {},
    apiKeys: [], apiKeysRequestId: 0, apiKeyReveal: { open: false, name: '', key: '' },
    apiKeyCreate: { open: false, name: '', saving: false },
    updateInfo: { checking: false, updating: false, checked: false, available: false, status: 'idle', progress: 0, current: '1.0.2', latest: '', asset_size: 0, downloaded_bytes: 0, resumed: false, message: '', error: '' },
    settings: {}, settingsSaving: false,
    models: [], model: '',
    auth: { token: '' },
    authEnabled: false,
    msgs: [], draft: '', selectedImages: [], busy: false,
    cfg: { thinking: 'off', search: 'off', stream: 'on', temperature: 1.0, topP: 0.95, maxTokens: 32768, safety: 'on' },
    toast: { show: false, msg: '', t: null },
    accountEdit: { open: false, id: '', name: '', saving: false },
    confirmDialog: { open: false, title: '', message: '', confirmText: '确定', danger: false, target: '', targetLabel: '', hint: '', _resolve: null },
    loginInProgress: false,
    loginPreviousActiveId: '',
    accountPreparations: {}, accountInitErrors: {},
    appReady: false, appReadyMessage: '正在初始化...',
    requestPoolStatus: { ready_accounts: [], standby_accounts: [], initializing_accounts: [], failed_accounts: {} },
    accountWarmupMonitoring: false,
    accountStatusTimer: null,
    scrollTimer: null,

    async init() {
      await this.checkAuth();
      this.loadFromCache();
      // Load the server default before model discovery so a clean browser
      // session starts with the configured default instead of alphabetical order.
      await this.loadSettings();
      await this.refreshReadiness();
      await Promise.all([
        this.loadModels(),
        this.loadStats(),
        this.loadAccounts(),
        this.loadAccountMetrics(),
        this.loadApiKeys(),
      ]);
      await this.loadRequestPoolStatus();
      this.monitorAccountWarmup();
      this.accountStatusTimer = setInterval(() => {
        if (document.hidden || this.view !== 'accounts') return;
        Promise.all([
          this.loadAccountMetrics(),
          this.loadRequestPoolStatus(),
        ]).catch(() => {});
      }, 5000);
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden && this.view === 'accounts') {
          Promise.all([this.loadAccountMetrics(), this.loadRequestPoolStatus()]).catch(() => {});
        }
      });
      window.addEventListener('beforeunload', () => {
        if (this.accountStatusTimer) clearInterval(this.accountStatusTimer);
        if (this.scrollTimer) clearTimeout(this.scrollTimer);
      }, { once: true });
      this.$watch('cfg', () => this.savePreferencesToCache(), { deep: true });
      this.$watch('model', () => this.savePreferencesToCache());
      this.$watch('auth.token', () => this.savePreferencesToCache());
      document.addEventListener('click', () => this.openSelect = null);
      // A packaged app checks the official release channel silently. This is
      // intentionally independent of the source checkout and Git.
      this.checkUpdate(true);
    },

    async checkAuth() {
      try {
        const res = await fetch('/auth/check');
        const data = await res.json();
        this.authEnabled = data.auth_enabled;

        if (this.authEnabled) {
          const token = localStorage.getItem('asp_api_token');
          if (data.local_session) {
            this.auth.token = token || '';
            return;
          }
          if (!token) {
            window.location.href = '/static/login.html';
            return;
          }
          // 验证 token 是否有效
          const verifyRes = await fetch('/health', {
            headers: { 'Authorization': `Bearer ${token}` }
          });
          if (!verifyRes.ok) {
            localStorage.removeItem('asp_api_token');
            window.location.href = '/static/login.html';
            return;
          }
          this.auth.token = token;
        }
      } catch (e) {
        console.error('Auth check failed', e);
      }
    },

    async logout() {
      await fetch('/auth/logout', { method: 'POST' }).catch(() => {});
      localStorage.removeItem('asp_api_token');
      this.auth.token = '';
      window.location.href = '/static/login.html';
    },
    loadFromCache() {
      try {
        const msgs = localStorage.getItem('asp_msgs');
        if (msgs) this.msgs = JSON.parse(msgs);
        const cfg = localStorage.getItem('asp_cfg');
        if (cfg) Object.assign(this.cfg, JSON.parse(cfg));
        const model = localStorage.getItem('asp_model');
        if (model) this.model = model;
        const models = localStorage.getItem('asp_models');
        if (models) this.models = JSON.parse(models);
        const token = localStorage.getItem('asp_api_token');
        if (token) this.auth.token = token;
      } catch (e) { console.error('Cache load error', e); }
    },
    savePreferencesToCache() {
      try {
        localStorage.setItem('asp_cfg', JSON.stringify(this.cfg));
        localStorage.setItem('asp_model', this.model);
        localStorage.setItem('asp_models', JSON.stringify(this.models));
        if (this.auth.token.trim()) localStorage.setItem('asp_api_token', this.auth.token.trim());
        else localStorage.removeItem('asp_api_token');
      } catch (e) { console.error('Cache save error', e); }
    },
    saveMessagesToCache() {
      try {
        const cached = [];
        let usedCharacters = 0;
        // localStorage is synchronous. Keep enough recent text for a useful
        // playground history without repeatedly serializing unbounded base64
        // images or multi-megabyte conversations on the UI thread.
        for (const source of this.msgs.slice(-100).reverse()) {
          const item = { ...source };
          delete item.pending;
          if (Array.isArray(item.images)) item.images = item.images.filter(value => !String(value).startsWith('data:'));
          if (typeof item.content === 'string') {
            item.content = item.content
              .replace(/!\[([^\]]*)\]\(data:image\/[^)]+\)/g, '[$1图片未写入本地缓存]')
              .slice(-250000);
          }
          if (typeof item.thinking === 'string') item.thinking = item.thinking.slice(-250000);
          const size = JSON.stringify(item).length;
          if (cached.length && usedCharacters + size > 1000000) break;
          cached.push(item);
          usedCharacters += size;
        }
        localStorage.setItem('asp_msgs', JSON.stringify(cached.reverse()));
      } catch (e) { console.error('Message cache save error', e); }
    },
    saveToCache() {
      this.saveMessagesToCache();
      this.savePreferencesToCache();
    },
    async clearCache() {
      const ok = await this.askConfirm('清理本地缓存', '将清除当前界面的聊天历史和个性化选项。账号与服务设置不会受影响。', {
        confirmText: '确认清理', danger: true,
        hint: '此操作无法撤销。'
      });
      if (!ok) return;
      localStorage.removeItem('asp_msgs');
      localStorage.removeItem('asp_cfg');
      localStorage.removeItem('asp_model');
      localStorage.removeItem('asp_models');
      location.reload();
    },
    go(v) {
      this.view = v; this.sidebarOpen = false; this.configOpen = false;
      if (v === 'dashboard') this.loadStats();
      if (v === 'accounts') { this.loadAccounts(); this.loadAccountMetrics(); this.loadRequestPoolStatus(); this.monitorAccountWarmup() }
      if (v === 'api-keys') this.loadApiKeys();
      if (v === 'settings') this.loadSettings();
      if (v === 'update') this.checkUpdate();
    },
    get serviceOrigin() {
      return window.location.origin;
    },
    async copyText(value) {
      try {
        await navigator.clipboard.writeText(value);
        this.showToast('已复制');
      } catch (e) {
        console.warn('Clipboard write failed', e);
        this.showToast('复制失败，请手动复制');
      }
    },
    async refreshReadiness() {
      try {
        const r = await this.apiFetch(`/health?t=${Date.now()}`, { cache: 'no-store' });
        if (r.ok) {
          const d = await r.json();
          this.appReady = Boolean(d.ready);
          this.appReadyMessage = d.message || (this.appReady ? '已就绪' : '正在初始化...');
        }
      } catch (e) { this.appReady = false; this.appReadyMessage = '正在连接服务...'; }
    },
    async loadRequestPoolStatus() {
      try {
        const r = await this.apiFetch(`/runtime/request-pool?t=${Date.now()}`, { cache: 'no-store' });
        if (r.ok) {
          this.requestPoolStatus = await r.json();
          this.accountInitErrors = { ...(this.requestPoolStatus.failed_accounts || {}) };
        }
      } catch (e) { console.warn('Request pool status failed', e); }
    },
    get hasAccountPreparations() {
      return Object.keys(this.accountPreparations).length > 0;
    },
    get hasInitializingAccounts() {
      return (this.requestPoolStatus.initializing_accounts || []).length > 0;
    },
    async monitorAccountWarmup() {
      if (this.accountWarmupMonitoring) return;
      this.accountWarmupMonitoring = true;
      try {
        while (this.accounts.length && this.hasInitializingAccounts) {
          await new Promise(resolve => setTimeout(resolve, 750));
          await this.loadRequestPoolStatus();
        }
      } finally {
        this.accountWarmupMonitoring = false;
      }
    },
    newChat() { this.msgs = []; this.saveToCache(); this.showToast('已创建新对话') },
    showToast(m) { this.toast.msg = m; this.toast.show = true; if (this.toast.t) clearTimeout(this.toast.t); this.toast.t = setTimeout(() => this.toast.show = false, 3000) },
    toggleSelect(k, e) { e.stopPropagation(); this.openSelect = this.openSelect === k ? null : k },
    selectOpt(k, model, val) { this[model] = val; this.openSelect = null },
    authHeaders(headers = {}) {
      const next = { ...headers };
      const token = this.auth.token.trim();
      if (token && !next.Authorization && !next.authorization) next.Authorization = `Bearer ${token}`;
      return next;
    },
    async apiFetch(url, options = {}) {
      const res = await fetch(url, { ...options, headers: this.authHeaders(options.headers || {}) });
      if (res.status === 401) this.showToast('鉴权失败，请检查 API Token');
      return res;
    },
    renderMarkdown(text) {
      if (!text) return '';
      let html = text;

      // 1. 预处理数学公式，防止被 Marked 误解析
      const mathBlocks = [];
      // 处理块级公式 $$...$$
      html = html.replace(/\$\$([\s\S]+?)\$\$/g, (match, formula) => {
        const id = `__MATH_BLOCK_${mathBlocks.length}__`;
        try {
          mathBlocks.push({ id, html: katex.renderToString(formula.trim(), { displayMode: true, throwOnError: false }) });
          return id;
        } catch (e) { return match; }
      });
      // 处理行内公式 $...$
      html = html.replace(/\$([^\$\n]+?)\$/g, (match, formula) => {
        const id = `__MATH_INLINE_${mathBlocks.length}__`;
        try {
          mathBlocks.push({ id, html: katex.renderToString(formula.trim(), { displayMode: false, throwOnError: false }) });
          return id;
        } catch (e) { return match; }
      });

      // 2. 配置 Marked 并解析
      if (typeof marked !== 'undefined') {
        marked.setOptions({
          highlight: function (code, lang) {
            if (typeof hljs !== 'undefined' && lang && hljs.getLanguage(lang)) {
              try { return hljs.highlight(code, { language: lang }).value; } catch (e) { }
            }
            return code;
          },
          breaks: true,
          gfm: true
        });
        html = marked.parse(html);
      }

      // 3. 将公式替换回来
      mathBlocks.forEach(item => {
        html = html.replace(item.id, item.html);
      });

      // 4. 清洗并返回
      if (typeof DOMPurify !== 'undefined') {
        return DOMPurify.sanitize(html, { ADD_TAGS: ["math", "style"], ADD_ATTR: ["style"] });
      }
      return html;
    },

    async loadModels() { try { const r = await this.apiFetch('/v1/models'); const d = await r.json(); this.models = d.data || []; if (!this.model && this.models.length) { const preferred = this.settings.default_text_model || 'gemini-3.7-flash'; this.model = this.models.some(m => m.id === preferred) ? preferred : this.models[0].id; } this.saveToCache(); } catch (e) { } },
    async loadStats() {
      try {
        const r = await this.apiFetch(`/stats?t=${Date.now()}`, { cache: 'no-store' });
        if (!r.ok) throw new Error(`stats request failed: ${r.status}`);
        const d = await r.json();
        this.stats = d.models || {};
      } catch (e) { console.warn('Stats refresh failed', e); }
    },
    async loadAccounts() {
      try {
        const cacheBust = `?t=${Date.now()}`;
        const [ar, br] = await Promise.all([
          this.apiFetch(`/accounts${cacheBust}`, { cache: 'no-store' }),
          this.apiFetch(`/accounts/active${cacheBust}`, { cache: 'no-store' })
        ]);
        if (!ar.ok) throw new Error('account refresh failed');
        const a = await ar.json();
        // After the last account is removed, /accounts/active correctly
        // returns 404. Treat that as an empty active selection so the UI does
        // not keep rendering the deleted account from its previous state.
        const b = br.ok ? await br.json() : {};
        // Keep account-pool order stable: oldest accounts first, newly added
        // accounts append to the end instead of jumping to the top.
        this.accounts = (Array.isArray(a) ? a : []).sort((left, right) => {
          const leftCreated = String(left.created_at || '');
          const rightCreated = String(right.created_at || '');
          return leftCreated.localeCompare(rightCreated);
        });
        this.activeId = b?.id || '';
        this.activeAccount = b || {};
      } catch (e) { console.warn('Account refresh failed', e); }
    },
    async refreshAccountData() { await Promise.all([this.loadAccounts(), this.loadAccountMetrics()]); },
    async loadAccountMetrics() {
      try {
        const r = await this.apiFetch(`/rotation/accounts?t=${Date.now()}`, { cache: 'no-store' });
        const d = await r.json();
        this.accountMetrics = d || {};
      } catch (e) { console.warn('Account metrics refresh failed', e); }
    },
    async loadApiKeys() {
      const requestId = ++this.apiKeysRequestId;
      try {
        const r = await this.apiFetch(`/auth/api-keys?t=${Date.now()}`, { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        // Ignore an older request that started before a newer refresh (for
        // example, the initial page load racing with a post-delete refresh).
        if (requestId === this.apiKeysRequestId) this.apiKeys = d.keys || [];
      } catch (e) { console.warn('API key list failed', e); }
    },
    async loadSettings() {
      try {
        const r = await this.apiFetch(`/settings?t=${Date.now()}`, { cache: 'no-store' });
        if (r.ok) this.settings = (await r.json()).settings || {};
      } catch (e) { console.warn('Settings load failed', e); }
    },
    async saveSettings() {
      this.settingsSaving = true;
      try {
        const r = await this.apiFetch('/settings', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.settings)
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || '设置保存失败'); return; }
        this.settings = d.settings || this.settings;
        this.showToast('设置已保存；部分项目将在重启 Asteria 后生效');
      } catch (e) { this.showToast('网络错误'); }
      finally { this.settingsSaving = false; }
    },
    async createApiKey() {
      this.apiKeyCreate = { open: true, name: '', saving: false };
      await this.$nextTick();
      document.querySelector('[x-model="apiKeyCreate.name"]')?.focus();
    },
    async submitApiKeyCreate() {
      const name = this.apiKeyCreate.name.trim();
      if (!name) { this.showToast('请输入 API Key 名称'); return; }
      this.apiKeyCreate.saving = true;
      try {
        const r = await this.apiFetch('/auth/api-keys', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name })
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || 'API Key 创建失败'); return; }
        this.apiKeyCreate.open = false;
        this.apiKeyReveal = { open: true, name: d.name || name, key: d.key || '' };
        await this.loadApiKeys();
      } catch (e) { this.showToast('网络错误'); }
      finally { this.apiKeyCreate.saving = false; }
    },
    async revokeApiKey(id) {
      const key = this.apiKeys.find(item => item.id === id);
      const ok = await this.askConfirm('删除 API Key', '删除后，正在使用该密钥的客户端将无法继续访问。', {
        confirmText: '确认删除', danger: true,
        target: key?.name || key?.prefix || id,
        targetLabel: '即将删除的密钥',
        hint: '此操作无法撤销。'
      });
      if (!ok) return;
      try {
        const r = await this.apiFetch(`/auth/api-keys/${id}/revoke`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || '删除失败'); return; }
        // Update the visible list immediately; the server refresh below keeps
        // the UI consistent with the persisted store after the request ends.
        this.apiKeys = this.apiKeys.filter(item => item.id !== id);
        await this.loadApiKeys();
        this.showToast('API Key 已删除');
      } catch (e) { this.showToast('网络错误'); }
    },
    async checkUpdate(silent = false) {
      this.updateInfo.checking = true;
      try {
        const r = await this.apiFetch(`/update/check?t=${Date.now()}`, { cache: 'no-store' });
        const d = await r.json().catch(() => ({}));
        this.updateInfo = { ...this.updateInfo, ...d, checking: false, checked: true, error: d.error || '' };
        if (!silent && d.available) this.showToast(`发现 Asteria ${d.latest}`);
      } catch (e) {
        this.updateInfo.checking = false; this.updateInfo.checked = true; this.updateInfo.error = '检查更新失败';
      }
    },
    async waitForUpdateDownload() {
      for (let i = 0; i < 600; i++) {
        const r = await this.apiFetch(`/update/status?t=${Date.now()}`, { cache: 'no-store' });
        const d = await r.json().catch(() => ({}));
        this.updateInfo = { ...this.updateInfo, ...d, updating: d.status === 'downloading' };
        if (d.status === 'ready') return true;
        if (d.status === 'error') { this.showToast(d.error || '更新下载失败'); return false; }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      this.showToast('更新下载超时');
      return false;
    },
    async startUpdate() {
      const ok = await this.askConfirm('安装更新', '安装期间 Asteria 会短暂关闭，并在完成后重新启动。', {
        confirmText: '开始更新',
        hint: '账号与本机设置会保留。'
      });
      if (!ok) return;
      this.updateInfo.updating = true;
      try {
        if (this.updateInfo.status !== 'ready') {
          const download = await this.apiFetch('/update/download', { method: 'POST' });
          const downloadData = await download.json().catch(() => ({}));
          if (!download.ok) { this.showToast(downloadData.detail || '更新下载失败'); this.updateInfo.updating = false; return; }
          this.updateInfo = { ...this.updateInfo, ...downloadData };
          if (!await this.waitForUpdateDownload()) { this.updateInfo.updating = false; return; }
        }
        const r = await this.apiFetch('/update', { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || '更新失败'); this.updateInfo.updating = false; return; }
        this.updateInfo = { ...this.updateInfo, ...d, updating: true };
        this.showToast('更新已开始，Asteria 即将重启');
      } catch (e) { this.updateInfo.updating = false; this.showToast('更新进程启动失败'); }
    },

    get accountRows() {
      return this.accounts.map(a => ({
        ...a,
        ...(this.accountMetrics[a.id] || {}),
        availability: this.accountInitErrors[a.id]
          ? 'failed'
          : ((this.requestPoolStatus.initializing_accounts || []).includes(a.id)
            ? 'initializing'
            : ((this.requestPoolStatus.ready_accounts || []).includes(a.id) ? 'available' : 'standby')),
      }));
    },
    get totalReqs() { return Object.values(this.stats).reduce((s, v) => s + (v.requests || 0), 0) },
    get totalRL() { return Object.values(this.stats).reduce((s, v) => s + (v.rate_limited || 0), 0) },
    get readyAccountCount() { return (this.requestPoolStatus.ready_accounts || []).length },

    async prepareAccount(id, announce = true) {
      if (this.accountPreparations[id]) return;
      this.accountPreparations = { ...this.accountPreparations, [id]: true };
      const nextErrors = { ...this.accountInitErrors };
      delete nextErrors[id];
      this.accountInitErrors = nextErrors;
      if (announce) this.showToast('正在后台准备账号…');
      try {
        const response = await this.apiFetch(`/accounts/${id}/prepare`, { method: 'POST' });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          const detail = typeof payload.detail === 'string'
            ? payload.detail
            : (payload.detail?.message || `HTTP ${response.status}`);
          throw new Error(detail || '账号初始化失败');
        }
        await this.loadRequestPoolStatus();
        if (announce) this.showToast('账号已可用');
      } catch (e) {
        this.accountInitErrors = { ...this.accountInitErrors, [id]: e.message || '初始化失败' };
        this.showToast(`初始化失败：${e.message || '请稍后重试'}`);
      } finally {
        const nextPreparations = { ...this.accountPreparations };
        delete nextPreparations[id];
        this.accountPreparations = nextPreparations;
      }
    },
    askConfirm(title, message, { confirmText = '确定', danger = false, target = '', targetLabel = '', hint = '' } = {}) {
      return new Promise(resolve => {
        this.confirmDialog = { open: true, title, message, confirmText, danger, target, targetLabel, hint, _resolve: resolve };
      });
    },
    resolveConfirm(value) {
      const resolve = this.confirmDialog._resolve;
      this.confirmDialog.open = false;
      this.confirmDialog._resolve = null;
      if (resolve) resolve(value);
    },
    async logoutAccount(account) {
      const label = account.email || account.name || account.id;
      const ok = await this.askConfirm('退出登录', `确定要退出此账号吗？退出后需要重新登录才能恢复使用。`, {
        confirmText: '确认退出', danger: true, target: label,
        targetLabel: '即将退出的账号',
        hint: '账号本地会话和浏览器缓存将一并清理。'
      });
      if (!ok) return;
      // Give immediate, reversible feedback while Chromium profile cleanup
      // continues on Windows. A failed request restores authoritative state.
      this.accounts = this.accounts.filter(item => item.id !== account.id);
      if (this.activeId === account.id) {
        this.activeId = '';
        this.activeAccount = {};
      }
      this.showToast('正在退出登录并清理本地数据…');
      try {
        const r = await this.apiFetch(`/accounts/${account.id}/logout`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
          await this.refreshAccountData();
          this.showToast(d.detail || '退出登录失败');
          return;
        }
        this.showToast(d.message || '已退出登录');
        await Promise.all([this.refreshAccountData(), this.loadRequestPoolStatus()]);
      } catch (e) {
        await this.refreshAccountData();
        this.showToast('网络错误');
      }
    },
    openAccountEdit(account) {
      this.accountEdit = { open: true, id: account.id, name: account.name || '', saving: false };
    },
    async saveAccountEdit() {
      const edit = this.accountEdit;
      if (!edit.name.trim()) { this.showToast('账号名称不能为空'); return }
      edit.saving = true;
      try {
        const r = await this.apiFetch(`/accounts/${edit.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: edit.name.trim() })
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || '保存失败'); return }
        this.showToast('账号信息已保存');
        edit.open = false;
        await this.refreshAccountData();
      } catch (e) { this.showToast('网络错误') }
      finally { edit.saving = false }
    },
    async addAccount() {
      if (this.loginInProgress) return;
      // Do not let adding an account switch the account serving API traffic.
      // Only a clean installation with no active account may initialize the
      // first account automatically.
      this.loginPreviousActiveId = this.activeId;
      this.loginInProgress = true;
      try {
        const r = await this.apiFetch('/accounts/login/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({})
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok || !d.session_id) {
          this.showToast(d.detail || '启动登录失败');
          return;
        }
        this.showToast('登录已开始，请在弹出的浏览器完成登录');
        await this.pollLoginStatus(d.session_id);
      } catch (e) {
        this.showToast('网络错误');
      } finally {
        this.loginInProgress = false;
      }
    },
    async pollLoginStatus(sessionId) {
      const deadline = Date.now() + 305000;
      let delay = 250;
      let transientFailures = 0;
      while (Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, delay));
        try {
          const r = await this.apiFetch(`/accounts/login/status/${sessionId}?t=${Date.now()}`, { cache: 'no-store' });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) {
            if (r.status >= 500) {
              transientFailures += 1;
              if (transientFailures < 6) { delay = Math.min(2000, delay + 250); continue; }
            }
            this.showToast(d.detail || '查询登录状态失败');
            return;
          }
          transientFailures = 0;
          if (d.status === 'completed') {
            await this.refreshAccountData();
            this.showToast(`登录成功${d.email ? ': ' + d.email : ''}，正在后台准备`);
            if (d.account_id) this.prepareAccount(d.account_id, false);
            this.loginPreviousActiveId = '';
            return;
          }
          if (d.status === 'failed') {
            this.showToast(this.loginErrorMessage(d.error));
            return;
          }
          delay = Math.min(1500, delay + 250);
        } catch (e) {
          transientFailures += 1;
          if (transientFailures >= 6) { this.showToast('查询登录状态失败'); return; }
          delay = Math.min(2000, delay + 250);
        }
      }
      this.showToast('登录仍未完成，请稍后刷新账号列表');
    },
    loginErrorMessage(error) {
      if (!error) return '登录失败';
      if (error.includes('XServer') || error.includes('Missing X server') || error.includes('$DISPLAY')) {
        return '登录浏览器启动失败：Docker 容器没有可用显示服务。请导入 Cookies，或配置 XServer 后重启容器。';
      }
      const msg = `登录失败：${error}`;
      return msg.length > 180 ? `${msg.slice(0, 177)}...` : msg;
    },

    resizeTa() { const el = this.$refs.ta; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 200) + 'px' },
    scrollDown() {
      if (this.scrollTimer) clearTimeout(this.scrollTimer);
      this.scrollTimer = setTimeout(() => {
        const el = document.getElementById('chat-scroll');
        if (el) el.scrollTop = el.scrollHeight;
        this.scrollTimer = null;
      }, 50);
    },

    async handleImageUpload(e) {
      const files = Array.from(e.target.files);
      for (const f of files) {
        if (!f.type.startsWith('image/')) continue;
        const reader = new FileReader();
        reader.onload = (ev) => this.selectedImages.push(ev.target.result);
        reader.readAsDataURL(f);
      }
      e.target.value = '';
    },
    removeImage(idx) { this.selectedImages.splice(idx, 1) },

    async send() {
      const t = this.draft.trim(); const imgs = [...this.selectedImages]; if (!t && !imgs.length) return; if (this.busy || !this.model) return;
      if (!this.appReady) { this.showToast(this.appReadyMessage || '正在初始化，请稍候'); return; }
      this.msgs.push({ role: 'user', content: t, images: imgs }); this.draft = ''; this.selectedImages = []; this.busy = true; this.resizeTa(); this.scrollDown(); this.saveToCache();

      // 生图模型走 /v1/images/edits (支持原始图片编辑)
      if (this.model.includes('image')) {
        try {
          const formData = new FormData();
          formData.append('model', this.model);
          formData.append('prompt', t);
          formData.append('size', '1024x1024');
          // 如果有选中的图片，转换为 File 对象传给 edit 接口（支持多张）
          for (let i = 0; i < imgs.length; i++) {
            const response = await fetch(imgs[i]);
            const blob = await response.blob();
            const file = new File([blob], `image_${i}.png`, { type: 'image/png' });
            formData.append('image', file);
          }
          const r = await this.apiFetch('/v1/images/edits', { method: 'POST', body: formData });
          if (!r.ok) { let e = r.statusText; try { const d = await r.json(); if (d.detail) e = JSON.stringify(d.detail) } catch (x) { }; this.msgs.push({ role: 'assistant', content: '', error: `Error ${r.status}: ${e}` }) }
          else {
            const d = await r.json(); const imgs = d.data || []; let content = ''; imgs.forEach(img => { if (img.b64_json) content += `![image](data:image/png;base64,${img.b64_json})\n`; else if (img.url) content += `![image](${img.url})\n`; if (img.revised_prompt) content += img.revised_prompt + '\n' });
            this.msgs.push({ role: 'assistant', content: content || '(无响应内容)', showThinking: false })
          }
        }
        catch (e) { this.msgs.push({ role: 'assistant', content: '', error: e.message }) }
        finally { this.busy = false; this.scrollDown(); this.saveToCache() }
        return;
      }

      const messages = this.msgs.map(m => {
        if (m.images && m.images.length) {
          const parts = [{ type: 'text', text: m.content || '' }];
          m.images.forEach(img => parts.push({ type: 'image_url', image_url: { url: img } }));
          return { role: m.role, content: parts };
        }
        return { role: m.role, content: m.content };
      });

      const body = { model: this.model, messages };
      if (this.cfg.temperature !== 1) body.temperature = this.cfg.temperature;
      if (this.cfg.topP !== 1) body.top_p = this.cfg.topP;
      if (this.cfg.maxTokens !== 8192) body.max_tokens = this.cfg.maxTokens;
      if (this.cfg.stream === 'on') body.stream = true;
      if (this.cfg.thinking !== 'off') body.thinking = this.cfg.thinking;
      this.saveToCache();
      if (this.cfg.search === 'on') body.google_search = true;
      if (this.cfg.safety === 'off') body.safety_off = true;

      let streamMessageIndex = -1;
      if (this.cfg.stream === 'on') {
        this.msgs.push({ role: 'assistant', content: '正在准备账号和模型…', thinking: '', showThinking: false, pending: true });
        streamMessageIndex = this.msgs.length - 1;
        this.scrollDown();
      }

      try {
        const r = await this.apiFetch('/v1/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r.ok) { let e = r.statusText; try { const d = await r.json(); if (d.detail) e = JSON.stringify(d.detail) } catch (x) { }; const error = `Error ${r.status}: ${e}`; if (streamMessageIndex >= 0) this.msgs[streamMessageIndex] = { role: 'assistant', content: '', error, pending: false }; else this.msgs.push({ role: 'assistant', content: '', error }) }
        else if (this.cfg.stream === 'on') {
          const reader = r.body.getReader(); const dec = new TextDecoder(); const idx = streamMessageIndex; let buf = ''; let received = false;
          let streamedContent = ''; let streamedThinking = ''; let lastPaint = 0;
          const paintStream = (force = false) => {
            const now = performance.now();
            if (!force && now - lastPaint < 50) return false;
            this.msgs[idx] = {
              ...this.msgs[idx],
              content: streamedContent,
              thinking: streamedThinking,
              pending: false,
            };
            lastPaint = now;
            return true;
          };
          this.msgs[idx] = { ...this.msgs[idx], content: '正在等待模型回复…' };
          while (true) {
            const { done, value } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true }); const lines = buf.split('\n'); buf = lines.pop();
            for (const ln of lines) {
              if (ln.startsWith('data: ') && ln !== 'data: [DONE]') {
                try {
                  const d = JSON.parse(ln.slice(6)); const delta = d.choices?.[0]?.delta || {};
                  const c = delta.content; const th = delta.reasoning_content || delta.thinking || delta.reasoning;
                  if (c || th) {
                    streamedContent += c || '';
                    streamedThinking += th || '';
                    received = true;
                  }
                } catch (e) { }
              }
            }
            if (received && paintStream()) this.scrollDown();
          }
          if (received) paintStream(true);
          else this.msgs[idx] = { ...this.msgs[idx], content: '(无响应内容)', pending: false };
          this.scrollDown();
          this.saveToCache();
        } else {
          const d = await r.json(); const msg = d.choices?.[0]?.message || {};
          this.msgs.push({ role: 'assistant', content: msg.content || '(无响应内容)', thinking: msg.reasoning_content || msg.thinking || msg.reasoning || '', showThinking: false })
        }
      }
      catch (e) { if (streamMessageIndex >= 0) this.msgs[streamMessageIndex] = { role: 'assistant', content: '', error: e.message, pending: false }; else this.msgs.push({ role: 'assistant', content: '', error: e.message }) }
      finally { this.busy = false; this.scrollDown(); this.saveToCache() }
    },

    fmtDate(s) { if (!s) return '-'; try { return new Date(s).toLocaleString() } catch (e) { return s } },
    fmtBytes(value) {
      const bytes = Number(value || 0);
      if (!bytes) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB'];
      const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
      return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
    }
  }
}
