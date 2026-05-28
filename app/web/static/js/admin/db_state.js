// app/web/static/js/admin/db_state.js
// /admin/server-config "Database backend" section — current state card,
// transition buttons, modal for cloud URL, progress polling.
//
// Spec: docs/superpowers/specs/2026-05-27-db-backend-state-machine-design.md

const DBState = {
  async fetchState() {
    const r = await fetch('/api/admin/db/state');
    if (!r.ok) throw new Error(`state fetch ${r.status}`);
    return r.json();
  },

  async startMigration(target, cloudUrl) {
    const body = { target };
    if (cloudUrl) body.cloud_url = cloudUrl;
    const r = await fetch('/api/admin/db/migrate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (r.status === 409) {
      const e = await r.json();
      throw new Error(`Migration already running: ${e.detail}`);
    }
    if (!r.ok) {
      const e = await r.json();
      throw new Error(e.detail || `migrate fetch ${r.status}`);
    }
    return r.json();
  },

  async fetchJob(jobId) {
    const r = await fetch(`/api/admin/db/job/${jobId}`);
    if (!r.ok) throw new Error(`job fetch ${r.status}`);
    return r.json();
  },

  async cancelJob(jobId) {
    const r = await fetch(`/api/admin/db/cancel/${jobId}`, { method: 'POST' });
    if (!r.ok) {
      const e = await r.json();
      throw new Error(e.detail || `cancel ${r.status}`);
    }
    return r.json();
  },

  // -------------------------------------------------------------------------
  // Phase 5.1 — localStorage cache helpers
  // Persist the last known job state so that during the ~30-90s applier
  // restart window (when fetch errors replace successful responses) the UI
  // can show "last known state" instead of a blank yellow box.
  // -------------------------------------------------------------------------

  _cacheJobState(jobId, job) {
    try {
      localStorage.setItem(`db-job-${jobId}`, JSON.stringify({
        ts: Date.now(),
        job,
      }));
    } catch (e) {
      // Quota / private mode — non-fatal; polling still works, just no cache.
    }
  },

  _loadCachedJobState(jobId) {
    try {
      const raw = localStorage.getItem(`db-job-${jobId}`);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (e) {
      return null;
    }
  },

  renderState(data) {
    const backend = data.backend;

    // Status header — hero strip on /admin/database, or the legacy
    // single-card view (#db-state-card) that older calls still render.
    const valueEl = document.getElementById('db-backend-value');
    const urlEl   = document.getElementById('db-url-value');
    const actionsEl = document.getElementById('db-actions');
    const helpEl    = document.getElementById('db-actions-help');

    if (valueEl) {
      // Drop the loading class + any previous backend-* state class,
      // then add the one matching this read so the colored dot picks
      // up the right hue.
      valueEl.className = `backend-value backend-${backend}`;
      valueEl.textContent = this._friendlyBackend(backend);
    }
    if (urlEl) {
      urlEl.textContent = data.url_redacted || '— (DuckDB file on the VM, no URL)';
    }

    // Render transition buttons. /admin/database has #db-actions
    // (preferred); fall back to the legacy #db-state-card.actions
    // container for any old embedded view.
    const transitionBtns = data.allowed_transitions.map(t => ({
      target: t,
      label: this._transitionLabel(backend, t),
    }));

    if (actionsEl) {
      if (transitionBtns.length === 0) {
        actionsEl.innerHTML = '';
        if (helpEl) {
          helpEl.innerHTML = `<em>No transitions available from <code>${backend}</code> —
            this is a terminal state.</em>`;
        }
      } else {
        actionsEl.innerHTML = transitionBtns
          .map(b => `<button class="btn" data-target="${b.target}">${b.label}</button>`)
          .join(' ');
        if (helpEl) {
          helpEl.textContent = `Pick a target to start a migration. The
            host applier will copy data, restart the app on the new backend,
            and verify row counts. Progress shows below.`;
        }
        actionsEl.querySelectorAll('button[data-target]').forEach(btn => {
          btn.addEventListener('click', () => this.handleTransitionClick(btn.dataset.target));
        });
      }
    } else {
      // Legacy single-card view (kept for the old /admin/server-config
      // section in case any installation still has it embedded).
      const legacyEl = document.getElementById('db-state-card');
      if (legacyEl) {
        const html = transitionBtns
          .map(b => `<button class="btn btn-primary" data-target="${b.target}">${b.label}</button>`)
          .join(' ');
        legacyEl.innerHTML = `
          <div class="card">
            <h3>Database backend</h3>
            <p><strong>Current:</strong> ${backend}</p>
            <p><strong>URL:</strong> ${data.url_redacted || '(none — DuckDB)'}</p>
            <div class="actions">${html}</div>
          </div>
        `;
        legacyEl.querySelectorAll('button[data-target]').forEach(btn => {
          btn.addEventListener('click', () => this.handleTransitionClick(btn.dataset.target));
        });
      }
    }

    if (data.current_job_id) {
      this.startPolling(data.current_job_id);
    }
  },

  _friendlyBackend(b) {
    switch (b) {
      case 'duckdb':                return 'DuckDB (on-VM file)';
      case 'side_car':              return 'Side-car Postgres (container)';
      case 'cloud':                 return 'Managed cloud Postgres';
      case 'side_car_in_progress':  return 'Side-car cutover in progress…';
      case 'cloud_in_progress':     return 'Cloud cutover in progress…';
      default:                      return b;
    }
  },

  _transitionLabel(backend, target) {
    if (target === 'side_car') {
      return (backend === 'cloud')
        ? 'Move back to side-car Postgres'
        : 'Enable side-car Postgres';
    }
    if (target === 'cloud') {
      return (backend === 'duckdb')
        ? 'Migrate straight to managed Postgres'
        : 'Migrate to managed Postgres';
    }
    return `Migrate to ${target}`;
  },

  async handleTransitionClick(target) {
    let cloudUrl = null;
    if (target === 'cloud') {
      cloudUrl = prompt('Cloud PG connection string (postgresql+psycopg://user:pass@host:5432/db):');
      if (!cloudUrl) return;
    }
    try {
      const { job_id } = await this.startMigration(target, cloudUrl);
      this.startPolling(job_id);
    } catch (e) {
      alert(`Migration failed to start: ${e.message}`);
    }
  },

  // -------------------------------------------------------------------------
  // _renderJob(job, isStale, progress, jobId)
  //
  // Extracted from the old inline tick callback so the same render path
  // is shared between the live-fetch success path and the cache-fallback
  // stale path (Phase 5.1).
  //
  // Phase 5.3: renders job.table_progress when present.
  // -------------------------------------------------------------------------
  _renderJob(job, isStale, progress, jobId) {
    // Phase 5.3 — per-table progress block, only shown when the migrator
    // has called update_table_progress (field is absent on early steps).
    const tableProgressHtml = job.table_progress
      ? `<div class="table-progress">Table ${job.table_progress.tables_done}/${job.table_progress.tables_total}: <code>${job.table_progress.current_table}</code></div>`
      : '';

    // Phase 5.1 — stale subtitle shown when cache is used during outage.
    const staleHtml = isStale
      ? `<div class="stale-notice">(connection lost — retrying…)</div>`
      : '';

    progress.innerHTML = `
      <div class="job-status">
        <div>Job <code>${jobId}</code>${staleHtml}</div>
        <div>Step: <strong>${job.current_step}</strong> (${job.progress_pct}%)</div>
        <div class="progress-bar"><div style="width: ${job.progress_pct}%"></div></div>
        ${tableProgressHtml}
        ${job.error ? `<div class="error">Error: ${job.error.message}</div>` : ''}
        <button class="btn btn-secondary" id="db-cancel-btn">Cancel</button>
      </div>
    `;
    document.getElementById('db-cancel-btn')?.addEventListener('click', async () => {
      try {
        await this.cancelJob(jobId);
        alert('Cancelled');
      } catch (e) {
        alert(`Cancel failed: ${e.message}`);
      }
    });
  },

  startPolling(jobId) {
    const progress = document.getElementById('db-migration-progress');
    if (!progress) return;
    progress.style.display = 'block';

    // Phase 5.1 — hydrate from cache immediately before the first fetch.
    // Eliminates the blank-box flash on page-reload mid-migration, and
    // gives operators something to read during the applier restart window.
    // Cache TTL guard: an entry older than 5 minutes is treated as stale
    // (operator may have reopened the page much later, the job has long
    // since completed, the cached state should not be shown as live).
    const CACHE_TTL_MS = 5 * 60 * 1000;
    const cached = this._loadCachedJobState(jobId);
    if (cached && (Date.now() - cached.ts) < CACHE_TTL_MS) {
      this._renderJob(cached.job, /*isStale=*/false, progress, jobId);
    }

    // Phase 5.2 — exponential backoff state.
    // While the app container is restarting (fetch errors) we back off
    // from 2s → 4s → 8s → … → 30s max, then reset to 2s on first
    // successful fetch. This avoids spamming connection-refused errors
    // during the ~30-90s applier restart window.
    let consecutiveFailures = 0;
    const BASE_INTERVAL = 2000;
    const MAX_INTERVAL = 30000;

    // Phase 5.2 — recursive setTimeout instead of setInterval so each
    // tick can choose its own next-delay after seeing whether the fetch
    // succeeded or failed.
    const tick = async () => {
      try {
        const job = await this.fetchJob(jobId);
        // Successful fetch — reset backoff and update cache.
        consecutiveFailures = 0;
        this._cacheJobState(jobId, job);
        this._renderJob(job, /*isStale=*/false, progress, jobId);
        if (['success', 'failed', 'cancelled'].includes(job.status)) {
          // Terminal state — stop polling, reload after 2s so the state
          // card refreshes to the new backend. Also drop the cache entry
          // so it doesn't linger in the operator's localStorage forever.
          try { localStorage.removeItem(`db-job-${jobId}`); } catch (e) {}
          setTimeout(() => location.reload(), 2000);
          return;
        }
      } catch (e) {
        // Fetch failed (app restarting or transient network issue).
        // Phase 5.1: render last known state with a stale subtitle.
        consecutiveFailures += 1;
        const staleCache = this._loadCachedJobState(jobId);
        if (staleCache) {
          this._renderJob(staleCache.job, /*isStale=*/true, progress, jobId);
        }
      }
      // Phase 5.2 — schedule next tick with exponential backoff.
      const interval = Math.min(BASE_INTERVAL * Math.pow(2, consecutiveFailures), MAX_INTERVAL);
      this._poll = setTimeout(tick, interval);
    };

    tick();
  },

  async init() {
    try {
      const data = await this.fetchState();
      this.renderState(data);
    } catch (e) {
      console.error('DBState init failed', e);
    }
  },
};

document.addEventListener('DOMContentLoaded', () => DBState.init());
