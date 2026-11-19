// Shows iNaturalist's computer-vision scores next to species suggestions.
//
// Runs in the page's own JavaScript world at document creation, before the
// site's code: iNaturalist's API client keeps a reference to window.fetch when
// it loads, so the wrapper must be in place first. Requests and responses are
// passed through unchanged; responses of computervision/score_image and
// score_observation are only read (a clone) to remember their scores.
//
// The uploader's species field asks for vision suggestions when it is focused
// and renders them as `.ac.vision[data-taxon-id]` rows in a jQuery UI menu.
// Each photo card caches its own response, so a row is matched to a response
// by taxon id and by the row's own vision score where jQuery exposes it.
(() => {
  const host = location.hostname;
  if (host !== 'inaturalist.org' && !host.endsWith('.inaturalist.org')) { return; }
  if (window.__naturelogVisionScores) { return; }
  const state = window.__naturelogVisionScores = { responses: [] };
  const KEEP_RESPONSES = 50;
  const VISION_URL = /\/computervision\/score_(image|observation)/;

  const number = value => (typeof value === 'number' && Number.isFinite(value) ? value : null);

  function remember(data) {
    if (!data || !Array.isArray(data.results)) { return; }
    const byTaxon = new Map();
    let max = 0;
    for (const r of data.results) {
      const id = r && r.taxon && r.taxon.id;
      if (id == null) { continue; }
      const entry = {
        combined: number(r.combined_score) ?? number(r.score),
        vision: number(r.vision_score),
        nearby: number(r.frequency_score)
      };
      byTaxon.set(String(id), entry);
      max = Math.max(max, entry.combined ?? 0, entry.vision ?? 0);
    }
    const ancestor = data.common_ancestor;
    if (ancestor && ancestor.taxon && ancestor.taxon.id != null) {
      const score = number(ancestor.score);
      byTaxon.set(String(ancestor.taxon.id), { combined: score, vision: null, nearby: null, ancestor: true });
      max = Math.max(max, score ?? 0);
    }
    // API versions differ: scores are either percentages or fractions of 1.
    state.responses.unshift({ byTaxon, percent: max > 1 });
    state.responses.length = Math.min(state.responses.length, KEEP_RESPONSES);
    annotateAll();
  }

  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    const result = originalFetch.apply(this, arguments);
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    if (VISION_URL.test(url)) {
      result.then(response => (response.ok ? response.clone().json() : null))
        .then(remember)
        .catch(() => {});
    }
    return result;
  };

  // The suggestion's own data (a Taxon with visionScore), if the page's jQuery holds it.
  function itemData(row) {
    const $ = window.jQuery;
    const li = row.closest('li');
    if (!$ || !li) { return null; }
    try { return $(li).data('ui-autocomplete-item') || null; } catch (e) { return null; }
  }

  function lookup(row) {
    const id = row.getAttribute('data-taxon-id');
    const item = itemData(row);
    const vision = item ? number(item.visionScore) : null;
    let fallback = null;
    for (const response of state.responses) {
      const entry = response.byTaxon.get(id);
      if (!entry) { continue; }
      if (vision == null || entry.ancestor || entry.vision === vision) {
        return { ...entry, percent: response.percent };
      }
      fallback = fallback || { ...entry, percent: response.percent };
    }
    return vision == null ? fallback : null;
  }

  const format = (value, percent) => (percent ? value : value * 100).toFixed(1);

  function annotate(row) {
    const scores = lookup(row);
    const label = row.querySelector('.ac-label > div') || row.querySelector('.ac-label');
    if (!scores || scores.combined == null || !label) { return; }
    let badge = label.querySelector('.naturelog-score');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'naturelog-score';
      label.appendChild(badge);
    }
    const p = scores.percent;
    badge.textContent = scores.ancestor
      ? `Common ancestor · ${format(scores.combined, p)}`
      : `Score ${format(scores.combined, p)}`;
    const details = [`Score: ${format(scores.combined, p)}`];
    if (scores.vision != null) { details.push(`Visually similar: ${format(scores.vision, p)}`); }
    if (scores.nearby != null) { details.push(`Expected nearby: ${format(scores.nearby, p)}`); }
    details.push('iNaturalist computer vision scores (relative, not probabilities)');
    badge.title = details.join('\n');
  }

  function annotateAll(root) {
    (root || document).querySelectorAll('.ac.vision[data-taxon-id]').forEach(annotate);
  }

  function start() {
    const style = document.createElement('style');
    style.textContent = `
      .naturelog-score {
        display: inline-block; margin-left: 6px; padding: 0 6px;
        border-radius: 9px; background: #74ac00; color: #fff;
        font-size: 11px; font-weight: 600; line-height: 17px; white-space: nowrap;
      }`;
    document.head.appendChild(style);
    new MutationObserver(mutations => {
      for (const m of mutations) {
        for (const node of m.addedNodes) {
          if (node.nodeType !== Node.ELEMENT_NODE) { continue; }
          if (node.matches('.ac.vision[data-taxon-id]')) { annotate(node); } else { annotateAll(node); }
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
    annotateAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
