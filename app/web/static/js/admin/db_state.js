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
    const el = document.getElementById('db-state-card');
    if (!el) return;
    const backend = data.backend;
    const transitionBtns = data.allowed_transitions.map(t => {
      let label;
      if (t === 'side_car') {
        // From DuckDB this is the first cutover; from cloud it's a DR
        // move back to the local container PG.
        label = (backend === 'cloud')
          ? 'Move back to side-car Postgres'
          : 'Enable side-car Postgres';
      } else {  // t === 'cloud'
        label = (backend === 'duckdb')
          ? 'Migrate straight to managed Postgres'
          : 'Migrate to managed Postgres';
      }
      return `<button class="btn btn-primary" data-target="${t}">${label}</button>`;
    }).join(' ');

    el.innerHTML = `
      <div class="card">
        <h3>Database backend</h3>
        <p><strong>Current:</strong> ${backend}</p>
        <p><strong>URL:</strong> ${data.url_redacted || '(none — DuckDB)'}</p>
        <div class="actions">${transitionBtns}</div>
      </div>
    `;

    el.querySelectorAll('button[data-target]').forEach(btn => {
      btn.addEventListener('click', () => this.handleTransitionClick(btn.dataset.target));
    });

    if (data.current_job_id) {
      this.startPolling(data.current_job_id);
    }
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
