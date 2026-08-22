
function app() {
  return {
    view: 'accounts', sidebarOpen: false, configOpen: false, openSelect: null,
    stats: {}, rotationMode: 'round_robin', rotCfg: { mode: 'round_robin', cooldown: 60 },
    accounts: [], rotationAccounts: {}, activeId: '', activeAccount: {},
    apiKeys: [], apiKeyReveal: { open: false, name: '', key: '' },
    apiKeyCreate: { open: false, name: '', saving: false },
    updateInfo: { checking: false, updating: false, checked: false, available: false, dirty: false, error: '' },
    settings: {}, settingsSaving: false,
    models: [], model: '',
    auth: { token: '' },
    authEnabled: false,
    msgs: [], draft: '', selectedImages: [], busy: false,
    cfg: { thinking: 'off', search: 'off', stream: 'on', temperature: 1.0, topP: 0.95, maxTokens: 32768, safety: 'on' },
    toast: { show: false, msg: '', t: null },
    cookieModal: { open: false, cookies: '', name: '', email: '', importing: false },
    accountEdit: { open: false, id: '', name: '', saving: false },
    loginInProgress: false,

    async init() {
      await this.checkAuth();
      this.loadFromCache();
      // Load the server default before model discovery so a clean browser
      // session starts with the configured default instead of alphabetical order.
      await this.loadSettings();
      await Promise.all([
        this.loadModels(),
        this.loadStats(),
        this.loadAccounts(),
        this.loadRotation(),
        this.loadApiKeys(),
      ]);
      this.$watch('cfg', () => this.saveToCache(), { deep: true });
      this.$watch('model', () => this.saveToCache());
      this.$watch('auth.token', () => this.saveToCache());
      document.addEventListener('click', () => this.openSelect = null);
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
    saveToCache() {
      try {
        localStorage.setItem('asp_msgs', JSON.stringify(this.msgs));
        localStorage.setItem('asp_cfg', JSON.stringify(this.cfg));
        localStorage.setItem('asp_model', this.model);
        localStorage.setItem('asp_models', JSON.stringify(this.models));
        if (this.auth.token.trim()) localStorage.setItem('asp_api_token', this.auth.token.trim());
        else localStorage.removeItem('asp_api_token');
      } catch (e) { console.error('Cache save error', e); }
    },
    clearCache() {
      if (!confirm('确定要清理本地缓存（聊天历史和配置）吗？')) return;
      localStorage.removeItem('asp_msgs');
      localStorage.removeItem('asp_cfg');
      localStorage.removeItem('asp_model');
      localStorage.removeItem('asp_models');
      location.reload();
    },
    go(v) {
      this.view = v; this.sidebarOpen = false;
      if (v === 'dashboard') this.loadStats();
      if (v === 'accounts') { this.loadAccounts(); this.loadRotation() }
      if (v === 'api-keys') this.loadApiKeys();
      if (v === 'settings') this.loadSettings();
      if (v === 'update') this.checkUpdate();
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
        if (!ar.ok || !br.ok) throw new Error('account refresh failed');
        const [a, b] = await Promise.all([ar.json(), br.json()]);
        this.accounts = Array.isArray(a) ? a : [];
        this.activeId = b?.id || '';
        this.activeAccount = b || {};
      } catch (e) { console.warn('Account refresh failed', e); }
    },
    async refreshAccountData() { await Promise.all([this.loadAccounts(), this.loadRotation()]); },
    async loadRotation() {
      try {
        const r = await this.apiFetch(`/rotation?t=${Date.now()}`, { cache: 'no-store' });
        const d = await r.json();
        this.rotationMode = d.mode || 'round_robin';
        this.rotCfg.mode = d.mode || 'round_robin';
        this.rotCfg.cooldown = d.cooldown_seconds || 60;
        this.rotationAccounts = d.accounts || {};
      } catch (e) { console.warn('Rotation refresh failed', e); }
    },
    async loadApiKeys() {
      try {
        const r = await this.apiFetch(`/auth/api-keys?t=${Date.now()}`, { cache: 'no-store' });
        if (!r.ok) return;
        const d = await r.json();
        this.apiKeys = d.keys || [];
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
        this.showToast('设置已保存；部分项目需重启反代后生效');
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
      if (!confirm('确定撤销这个 API Key 吗？已使用它的客户端会立即失效。')) return;
      try {
        const r = await this.apiFetch(`/auth/api-keys/${id}/revoke`, { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || '撤销失败'); return; }
        await this.loadApiKeys();
        this.showToast('API Key 已撤销');
      } catch (e) { this.showToast('网络错误'); }
    },
    async checkUpdate() {
      this.updateInfo.checking = true;
      try {
        const r = await this.apiFetch(`/update/check?t=${Date.now()}`, { cache: 'no-store' });
        const d = await r.json().catch(() => ({}));
        this.updateInfo = { ...this.updateInfo, ...d, checking: false, checked: true, error: d.error || '' };
      } catch (e) {
        this.updateInfo.checking = false; this.updateInfo.checked = true; this.updateInfo.error = '检查更新失败';
      }
    },
    async startUpdate() {
      if (!confirm('更新会停止并重启反代，确定继续吗？')) return;
      this.updateInfo.updating = true;
      try {
        const r = await this.apiFetch('/update', { method: 'POST' });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { this.showToast(d.detail || '更新失败'); this.updateInfo.updating = false; return; }
        this.showToast('更新已开始，反代即将重启');
      } catch (e) { this.showToast('更新进程已启动，页面即将断开'); }
    },

    get accountRows() { return this.accounts.map(a => ({ ...a, ...(this.rotationAccounts[a.id] || {}) })) },
    get totalReqs() { return Object.values(this.stats).reduce((s, v) => s + (v.requests || 0), 0) },
    get totalRL() { return Object.values(this.stats).reduce((s, v) => s + (v.rate_limited || 0), 0) },

    async saveRotation() { try { await this.apiFetch('/rotation/mode', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: this.rotCfg.mode, cooldown_seconds: this.rotCfg.cooldown }) }); this.showToast('已保存'); this.loadRotation() } catch (e) { this.showToast('保存失败') } },
    async forceNext() { try { await this.apiFetch('/rotation/next', { method: 'POST' }); await this.refreshAccountData(); this.showToast('已切换账号') } catch (e) { this.showToast('切换失败') } },
    async activateAccount(id) { try { await this.apiFetch(`/accounts/${id}/activate`, { method: 'POST' }); await this.refreshAccountData(); this.showToast('已激活') } catch (e) { this.showToast('激活失败') } },
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
            if (d.account_id) {
              await this.apiFetch(`/accounts/${d.account_id}/activate`, { method: 'POST' });
            }
            await this.refreshAccountData();
            this.showToast(`登录成功${d.email ? ': ' + d.email : ''}`);
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
    async importCookies() {
      const raw = this.cookieModal.cookies.trim();
      if (!raw) { this.showToast('请输入 Cookie'); return }
      // 支持多行：每行一个 cookie 或用分号分隔
      const cookies = raw.split(/[\r\n]+/).map(l => l.trim()).filter(Boolean).join('; ');
      this.cookieModal.importing = true;
      try {
        const body = { cookies };
        if (this.cookieModal.name.trim()) body.name = this.cookieModal.name.trim();
        if (this.cookieModal.email.trim()) body.email = this.cookieModal.email.trim();
        const r = await this.apiFetch('/accounts/import-cookies', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const d = await r.json();
        if (r.ok) {
          this.showToast(`导入成功: ${d.cookie_count} 个 cookie`);
          this.cookieModal.open = false; this.cookieModal.cookies = ''; this.cookieModal.name = ''; this.cookieModal.email = '';
          this.loadAccounts(); this.loadRotation();
        } else {
          this.showToast(d.detail || '导入失败');
        }
      } catch (e) { this.showToast('网络错误') }
      finally { this.cookieModal.importing = false }
    },

    resizeTa() { const el = this.$refs.ta; el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 200) + 'px' },
    scrollDown() { setTimeout(() => { const el = document.getElementById('chat-scroll'); if (el) el.scrollTop = el.scrollHeight }, 50) },

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

      try {
        const r = await this.apiFetch('/v1/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r.ok) { let e = r.statusText; try { const d = await r.json(); if (d.detail) e = JSON.stringify(d.detail) } catch (x) { }; this.msgs.push({ role: 'assistant', content: '', error: `Error ${r.status}: ${e}` }) }
        else if (this.cfg.stream === 'on') {
          const reader = r.body.getReader(); const dec = new TextDecoder(); this.msgs.push({ role: 'assistant', content: '', thinking: '', showThinking: false }); const idx = this.msgs.length - 1; let buf = '';
          while (true) {
            const { done, value } = await reader.read(); if (done) break; buf += dec.decode(value, { stream: true }); const lines = buf.split('\n'); buf = lines.pop();
            for (const ln of lines) {
              if (ln.startsWith('data: ') && ln !== 'data: [DONE]') {
                try {
                  const d = JSON.parse(ln.slice(6)); const delta = d.choices?.[0]?.delta || {};
                  const c = delta.content; if (c) this.msgs[idx].content += c;
                  const th = delta.reasoning_content || delta.thinking || delta.reasoning; if (th) this.msgs[idx].thinking += th;
                } catch (e) { }
              }
            }
            this.scrollDown()
          }
          this.saveToCache();
        } else {
          const d = await r.json(); const msg = d.choices?.[0]?.message || {};
          this.msgs.push({ role: 'assistant', content: msg.content || '(无响应内容)', thinking: msg.reasoning_content || msg.thinking || msg.reasoning || '', showThinking: false })
        }
      }
      catch (e) { this.msgs.push({ role: 'assistant', content: '', error: e.message }) }
      finally { this.busy = false; this.scrollDown(); this.saveToCache() }
    },

    fmtDate(s) { if (!s) return '-'; try { return new Date(s).toLocaleString() } catch (e) { return s } }
  }
}
