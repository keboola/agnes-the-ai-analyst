/**
 * Metric Modal JavaScript
 * Handles modal open/close, tab switching, and metric data loading
 */

// Global state
let currentMetricPath = null;
let currentMetricData = null;

/**
 * Open metric modal and load data
 * @param {string} metricPath - Path to metric YAML (e.g., 'finance/infra_cost.yml') or catalog FQN (e.g., 'catalog:...')
 */
function openMetricModal(metricPath) {
    currentMetricPath = metricPath;
    const overlay = document.getElementById('metricModalOverlay');
    const body = document.getElementById('metricModalBody');

    // Show modal
    overlay.classList.add('active');
    document.body.style.overflow = 'hidden';

    // Show loading state
    body.innerHTML = '<div class="metric-loading"><div class="metric-loading-spinner"></div><div class="metric-loading-text">Loading metric...</div></div>';

    // Route based on prefix: catalog:FQN uses /api/catalog/metrics, YAML paths use /api/metrics
    const url = metricPath.startsWith('catalog:')
        ? `/api/catalog/metrics/${metricPath.slice(8)}`  // Remove 'catalog:' prefix
        : `/api/metrics/${metricPath}`;

    // Fetch metric data
    fetch(url)
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return response.json();
        })
        .then(data => {
            currentMetricData = data;
            renderMetricModal(data);
        })
        .catch(error => {
            console.error('Error loading metric:', error);
            body.innerHTML = `<div class="metric-error">Failed to load metric: ${error.message}</div>`;
        });
}

/**
 * Close metric modal
 */
function closeMetricModal() {
    const overlay = document.getElementById('metricModalOverlay');
    overlay.classList.remove('active');
    document.body.style.overflow = '';
    currentMetricPath = null;
    currentMetricData = null;
}

/**
 * Switch between tabs
 * @param {string} tabId - Tab identifier
 */
function switchMetricTab(tabId) {
    // Update tab buttons
    document.querySelectorAll('.metric-tab').forEach(tab => {
        tab.classList.remove('active');
    });
    document.querySelector(`[data-tab="${tabId}"]`).classList.add('active');

    // Update tab content
    document.querySelectorAll('.metric-tab-content').forEach(content => {
        content.classList.remove('active');
    });
    document.getElementById(tabId).classList.add('active');
}

/**
 * Render metric modal content
 * @param {Object} data - Metric data from API
 */
function renderMetricModal(data) {
    const modal = document.getElementById('metricModal');
    const titleElement = document.getElementById('metricModalTitle');
    const metadataElement = document.getElementById('metricModalMetadata');
    const body = document.getElementById('metricModalBody');

    // Set category class for tab coloring
    const categoryClass = `category-${data.category}`;
    modal.setAttribute('data-category', data.category);

    // Set title and metadata (with technical name)
    titleElement.innerHTML = `
        <div style="display: flex; flex-direction: column; gap: 4px;">
            <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
                <span>${data.display_name}</span>
                ${data.validation && data.validation.status === 'validated' ? `
                    <span class="metric-validation-badge">
                        <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
                        </svg>
                        Validated
                    </span>
                ` : ''}
            </div>
            <div style="font-size: 14px; font-weight: 400; color: #6B7280; font-family: monospace;">
                ${data.name}
            </div>
        </div>
    `;

    metadataElement.innerHTML = `
        <span class="metric-chip category ${data.category}">${formatCategory(data.category)}</span>
        ${data.metadata.grain ? `<span class="metric-chip grain">${data.metadata.grain}</span>` : ''}
        ${data.metadata.unit ? `<span class="metric-chip unit">${data.metadata.unit}</span>` : ''}
    `;

    // Apply category class to tabs
    document.querySelectorAll('.metric-tab').forEach(tab => {
        tab.className = tab.className.replace(/\bcategory-\w+/g, '');
        tab.classList.add(categoryClass);
    });

    // Render tab contents
    body.innerHTML = `
        ${renderOverviewTab(data)}
        ${renderHowToUseTab(data)}
        ${renderSQLExamplesTab(data)}
        ${renderTechnicalTab(data)}
    `;

    // Activate first tab
    switchMetricTab('tabOverview');

    // Apply syntax highlighting to all code blocks
    if (typeof Prism !== 'undefined') {
        Prism.highlightAll();
    }
}

/**
 * Render Overview tab
 */
function renderOverviewTab(data) {
    const keyInsights = data.notes.key_insights || data.notes.all.slice(0, 5);

    return `
        <div id="tabOverview" class="metric-tab-content">
            <div class="metric-section">
                <h3 class="metric-section-header">What it measures</h3>
                <div class="metric-section-content">
                    <p>${data.overview.description}</p>
                </div>
            </div>

            ${keyInsights.length > 0 ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Key Insights</h3>
                    <div class="metric-section-content">
                        <ul>
                            ${keyInsights.map(note => `<li>${highlightTechnicalTerms(escapeHtml(note))}</li>`).join('')}
                        </ul>
                    </div>
                </div>
            ` : ''}

            ${data.validation ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Validation</h3>
                    <div class="metric-section-content">
                        <p><strong>${data.validation.method}</strong></p>
                        <p>${data.validation.result}</p>
                        ${data.validation.last_updated ? `<p style="color: #6B7280; font-size: 14px;">Last updated: ${data.validation.last_updated}</p>` : ''}
                    </div>
                </div>
            ` : ''}
        </div>
    `;
}

/**
 * Render How to Use tab
 */
function renderHowToUseTab(data) {
    return `
        <div id="tabHowToUse" class="metric-tab-content">
            ${data.dimensions.length > 0 ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Dimensions</h3>
                    <div class="metric-section-content">
                        <p style="margin-bottom: 12px;">Filter and group this metric by:</p>
                        <div class="metric-dimensions">
                            ${data.dimensions.map(dim => `
                                <button class="metric-dimension-pill" onclick="copyDimension(this, '${dim}')" title="Click to copy">
                                    ${dim}
                                </button>
                            `).join('')}
                        </div>
                    </div>
                </div>
            ` : ''}

            ${data.notes.all.length > 0 ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Important Notes</h3>
                    <div class="metric-section-content">
                        <ul>
                            ${data.notes.all.map(note => `<li>${highlightTechnicalTerms(escapeHtml(note))}</li>`).join('')}
                        </ul>
                    </div>
                </div>
            ` : ''}

            ${data.special_sections && data.special_sections.cost_allocation_guide ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Cost Allocation Guide</h3>
                    <div class="metric-expandable">
                        <button class="metric-expandable-trigger" onclick="toggleExpandable(this)">
                            <span>How to allocate infrastructure cost to customers</span>
                            <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                                <path stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/>
                            </svg>
                        </button>
                        <div class="metric-expandable-content">
                            ${renderMarkdown(data.special_sections.cost_allocation_guide)}
                        </div>
                    </div>
                </div>
            ` : ''}
        </div>
    `;
}

/**
 * Render SQL Examples tab
 */
function renderSQLExamplesTab(data) {
    const sqlExamples = data.sql_examples || {};
    const simpleQueries = [];
    const advancedQueries = [];

    // Categorize queries by complexity
    Object.entries(sqlExamples).forEach(([key, example]) => {
        if (example.complexity === 'advanced') {
            advancedQueries.push([key, example]);
        } else {
            simpleQueries.push([key, example]);
        }
    });

    return `
        <div id="tabSQLExamples" class="metric-tab-content">
            ${simpleQueries.map(([key, example]) => renderCodeBlock(example.title, example.query, key)).join('')}

            ${advancedQueries.length > 0 ? `
                <div class="metric-expandable">
                    <button class="metric-expandable-trigger" onclick="toggleExpandable(this)">
                        <span>Show advanced queries</span>
                        <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" d="M19 9l-7 7-7-7"/>
                        </svg>
                    </button>
                    <div class="metric-expandable-content">
                        ${advancedQueries.map(([key, example]) => renderCodeBlock(example.title, example.query, key)).join('')}
                    </div>
                </div>
            ` : ''}
        </div>
    `;
}

/**
 * Render Technical Details tab
 */
function renderTechnicalTab(data) {
    return `
        <div id="tabTechnical" class="metric-tab-content">
            <div class="metric-section">
                <h3 class="metric-section-header">Metric Configuration</h3>
                <table class="metric-details-table">
                    <tr>
                        <td>Name</td>
                        <td>${data.name}</td>
                    </tr>
                    <tr>
                        <td>Type</td>
                        <td>${data.metadata.type}</td>
                    </tr>
                    <tr>
                        <td>Expression</td>
                        <td>${data.technical.expression}</td>
                    </tr>
                    <tr>
                        <td>Table</td>
                        <td>${data.technical.table}</td>
                    </tr>
                    <tr>
                        <td>Time Column</td>
                        <td>${data.metadata.time_column}</td>
                    </tr>
                    <tr>
                        <td>Grain</td>
                        <td>${data.metadata.grain}</td>
                    </tr>
                </table>
            </div>

            ${data.technical.data_sources && data.technical.data_sources.length > 0 ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Data Sources</h3>
                    <div class="metric-section-content">
                        <p><strong>Primary:</strong> ${data.technical.table}</p>
                        ${data.technical.data_sources.filter(ds => ds.type === 'join').length > 0 ? `
                            <p><strong>Joins:</strong></p>
                            <ul>
                                ${data.technical.data_sources.filter(ds => ds.type === 'join').map(ds =>
                                    `<li>${ds.table}${ds.via ? ` (via ${ds.via})` : ''}</li>`
                                ).join('')}
                            </ul>
                        ` : ''}
                    </div>
                </div>
            ` : ''}

            ${data.technical.synonyms && data.technical.synonyms.length > 0 ? `
                <div class="metric-section">
                    <h3 class="metric-section-header">Synonyms</h3>
                    <div class="metric-section-content">
                        <div class="metric-dimensions">
                            ${data.technical.synonyms.map(syn => `
                                <span class="metric-dimension-pill">${escapeHtml(syn)}</span>
                            `).join('')}
                        </div>
                    </div>
                </div>
            ` : ''}
        </div>
    `;
}

/**
 * Render code block with copy button and syntax highlighting
 */
function renderCodeBlock(title, code, id) {
    return `
        <div class="metric-code-block">
            <div class="metric-code-header">
                <div class="metric-code-title">${title}</div>
                <button class="metric-code-copy-btn" onclick="copyCode('code-${id}', this)">
                    <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/>
                    </svg>
                    Copy
                </button>
            </div>
            <pre class="metric-code-pre"><code id="code-${id}" class="language-sql">${escapeHtml(code)}</code></pre>
        </div>
    `;
}

/**
 * Copy code to clipboard
 */
function copyCode(elementId, button) {
    const code = document.getElementById(elementId).textContent;
    copyToClipboard(code, button);
}

/**
 * Copy dimension name to clipboard
 */
function copyDimension(button, text) {
    const originalText = button.textContent;
    navigator.clipboard.writeText(text).then(() => {
        button.textContent = '✓ Copied!';
        button.style.background = '#D1FAE5';
        button.style.color = '#065F46';
        setTimeout(() => {
            button.textContent = originalText;
            button.style.background = '';
            button.style.color = '';
        }, 1500);
    }).catch(err => {
        console.error('Failed to copy:', err);
    });
}

/**
 * Copy text to clipboard with visual feedback
 */
function copyToClipboard(text, button = null) {
    navigator.clipboard.writeText(text).then(() => {
        if (button) {
            const originalHTML = button.innerHTML;
            button.classList.add('copied');
            button.innerHTML = `
                <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7"/>
                </svg>
                Copied!
            `;
            setTimeout(() => {
                button.classList.remove('copied');
                button.innerHTML = originalHTML;
            }, 2000);
        }
    }).catch(err => {
        console.error('Failed to copy:', err);
    });
}

/**
 * Toggle expandable section
 */
function toggleExpandable(trigger) {
    const expandable = trigger.closest('.metric-expandable');
    expandable.classList.toggle('expanded');
}

/**
 * Format category name
 */
function formatCategory(category) {
    const map = {
        'finance': 'Finance',
        'product_usage': 'Product Usage',
        'sales_revenue': 'Sales & Revenue',
        'weekly_leadership_kpis': 'Weekly Leadership KPIs',
        'revenue': 'Revenue',
        'customers': 'Customers',
        'marketing': 'Marketing',
        'support': 'Support'
    };
    return map[category] || category.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
}

/**
 * Simple markdown-to-HTML renderer (for cost_allocation_guide)
 */
function renderMarkdown(text) {
    return text
        .replace(/### (.*)/g, '<h4 style="font-weight: 600; margin: 16px 0 8px;">$1</h4>')
        .replace(/## (.*)/g, '<h3 style="font-weight: 700; margin: 20px 0 12px;">$1</h3>')
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\n\n/g, '</p><p>')
        .replace(/\n/g, '<br>');
}

/**
 * Highlight technical terms in text (snake_case, table names, etc.)
 */
function highlightTechnicalTerms(text) {
    // Pattern: snake_case words, table names, technical identifiers
    // Match: gross_mrr, net_mrr, product_revenue, mrr_aggregated, etc.
    const pattern = /\b([a-z][a-z0-9]*_[a-z0-9_]+)\b/g;

    return text.replace(pattern, '<code>$1</code>');
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
    // Close modal on overlay click
    document.getElementById('metricModalOverlay')?.addEventListener('click', (e) => {
        if (e.target.id === 'metricModalOverlay') {
            closeMetricModal();
        }
    });

    // Close modal on Escape key
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && document.getElementById('metricModalOverlay')?.classList.contains('active')) {
            closeMetricModal();
        }
    });
});
