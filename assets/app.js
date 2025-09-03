(async function () {
  const els = {
    list: document.getElementById('list'),
    empty: document.getElementById('empty'),
    errors: document.getElementById('errors'),
    statusGenerated: document.getElementById('status-generated'),
    statusCount: document.getElementById('status-count'),
    statusFetch: document.getElementById('status-fetch'),
    rawLink: document.getElementById('raw-link'),
  };

  // Always point the “raw JSON” link at the current file (works on Pages)
  els.rawLink.href = 'headlines.json';

  const url = `headlines.json?v=${Date.now()}`; // simple cache-buster
  try {
    const res = await fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Header/status
    const gen = data.generated_utc || '';
    els.statusGenerated.textContent = gen
      ? `Last updated: ${formatLocal(gen)} (local)`
      : 'Last updated: —';
    if (gen) {
      els.statusGenerated.title = `UTC: ${gen}`;
    }
    const items = Array.isArray(data.items) ? data.items : [];
    els.statusCount.textContent = `Items: ${items.length}`;
    els.statusFetch.textContent = 'Fetch: ok';

    // Render list
    if (items.length === 0) {
      els.empty.style.display = '';
      return;
    }
    const frag = document.createDocumentFragment();
    for (const it of items) {
      const title = (it.title || '').trim();
      const url = (it.url || '').trim();
      const source = it.source || 'News';
      const iso = it.published_utc || '';
      const when = iso ? formatRelative(iso) : '';

      const region = it.region || null;
      const category = it.category || null;
      const score = typeof it.score === 'number' ? it.score : null;

      const card = document.createElement('article');
      card.className = 'item';

      const h3 = document.createElement('h3');
      const a = document.createElement('a');
      a.href = url || '#';
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = title || '(untitled)';
      h3.appendChild(a);

      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.innerHTML = [
        source ? `<span>${escapeHtml(source)}</span>` : '',
        when ? `<span>${escapeHtml(when)}</span>` : '',
        region ? `<span class="badge">${escapeHtml(region)}</span>` : '',
        category ? `<span class="badge">${escapeHtml(category)}</span>` : '',
        score !== null ? `<span class="badge">score ${score.toFixed(2)}</span>` : '',
      ].filter(Boolean).join('');

      card.appendChild(h3);
      card.appendChild(meta);
      frag.appendChild(card);
    }
    els.list.appendChild(frag);
  } catch (err) {
    console.error(err);
    els.statusFetch.textContent = 'Fetch: error';
    showError(`Could not load headlines.json (${String(err).replace(/^Error:\s*/, '')}).`);
  }

  function showError(msg) {
    const div = document.createElement('div');
    div.className = 'error';
    div.textContent = msg;
    els.errors.appendChild(div);
  }

  function formatLocal(isoStr) {
    try {
      const d = new Date(isoStr);
      // e.g., “Sep 2, 10:14 PM”
      return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    } catch { return isoStr; }
  }

  function formatRelative(isoStr) {
    try {
      const d = new Date(isoStr);
      const now = new Date();
      const sec = Math.floor((now - d) / 1000);
      if (sec < 60) return 'just now';
      const mins = Math.floor(sec / 60);
      if (mins < 60) return `${mins}m ago`;
      const hrs = Math.floor(mins / 60);
      if (hrs < 24) return `${hrs}h ago`;
      const days = Math.floor(hrs / 24);
      return `${days}d ago`;
    } catch { return isoStr; }
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }
})();
