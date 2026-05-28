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

  startPolling(jobId) {
    const progress = document.getElementById('db-migration-progress');
    if (!progress) return;
    progress.style.display = 'block';

    const tick = async () => {
      try {
        const job = await this.fetchJob(jobId);
        progress.innerHTML = `
          <div class="job-status">
            <div>Job <code>${jobId}</code></div>
            <div>Step: <strong>${job.current_step}</strong> (${job.progress_pct}%)</div>
            <div class="progress-bar"><div style="width: ${job.progress_pct}%"></div></div>
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
        if (['success', 'failed', 'cancelled'].includes(job.status)) {
          clearInterval(this._poll);
          setTimeout(() => location.reload(), 2000);
        }
      } catch (e) {
        // Silently swallow transient fetch errors; next tick retries.
      }
    };
    tick();
    this._poll = setInterval(tick, 2000);
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
