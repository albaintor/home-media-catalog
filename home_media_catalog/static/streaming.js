// Web streaming catalogue, read from the same reconciled NAS state as iOS.
// No streaming credentials are copied here; discovery keys remain device-local.
import { tr, displayLocale } from './i18n.js';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const root = new URL('.', window.location.href);
const providerNames = {
  netflix: 'Netflix', disney_plus: 'Disney+', prime_video: 'Prime Video',
  apple_tv_plus: 'Apple TV+', hbo_max: 'HBO Max', paramount_plus: 'Paramount+'
};
const providerHosts = {
  netflix: ['netflix.com'], disney_plus: ['disneyplus.com'],
  prime_video: ['primevideo.com'], apple_tv_plus: ['tv.apple.com'],
  hbo_max: ['max.com', 'hbomax.com'], paramount_plus: ['paramountplus.com']
};
const state = { rows: [], service: '', kind: '', query: '', loading: false, loaded: false };

function safePoster(raw) {
  return typeof raw === 'string' && /^\/[a-z0-9_.-]{1,150}$/i.test(raw)
    ? 'https://image.tmdb.org/t/p/w342' + raw : '';
}
function safeProviderLink(raw, service) {
  if (!raw || !providerHosts[service]) return '';
  try {
    const parsed = new URL(raw);
    if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return '';
    const host = parsed.hostname.toLowerCase();
    if (!providerHosts[service].some(allowed =>
      host === allowed || host.endsWith('.' + allowed))) return '';
    return parsed.href;
  } catch { return ''; }
}
function providerLink(item, service) {
  const payload = item.payload || {};
  const kind = payload.kind || (item.key.split(':')[1] || '');
  const id = payload.tmdb_id || (item.key.split(':')[2] || '');
  // France uses the original key; regional overrides use region-scoped keys.
  const candidates = ['link:' + kind + ':' + id + ':' + service];
  for (const row of state.rows) {
    if (row.deleted || !row.key.startsWith('link:')) continue;
    if (row.key === candidates[0] || (
      row.key.endsWith(':' + kind + ':' + id + ':' + service) &&
      row.key.split(':').length === 5 &&
      row.key.split(':')[1] === String(navigator.language || 'fr').slice(-2).toUpperCase()
    )) return safeProviderLink(row.payload?.url, service);
  }
  return '';
}
function syncedItems() {
  return state.rows.filter(row => !row.deleted &&
    /^item:(movie|tv):[0-9]+$/.test(row.key) &&
    row.payload && (row.payload.status === 'saved' || row.payload.status === 'pending'));
}
function renderFilters() {
  const subscriptionRow = state.rows.find(row => row.key === 'prefs:subscriptions' && !row.deleted);
  const subscribed = new Set((subscriptionRow?.payload?.value || '')
    .split(',').filter(key => providerNames[key]));
  const services = new Set(syncedItems().flatMap(row =>
    (row.payload.services || '').split(',').filter(key => providerNames[key])));
  const keys = Object.keys(providerNames).filter(key =>
    subscribed.has(key) || services.has(key));
  const picker = $('streaming-service');
  picker.innerHTML = '<option value="">' + esc(tr('Tous les services')) + '</option>' +
    keys.map(key => '<option value="' + key + '">' + esc(providerNames[key]) + '</option>').join('');
  if (!keys.includes(state.service)) state.service = '';
  picker.value = state.service;
}
function render() {
  const all = syncedItems();
  const query = state.query.trim().normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase();
  const entries = all.filter(row => {
    const p = row.payload;
    return (!state.kind || p.kind === state.kind) &&
      (!state.service || (p.services || '').split(',').includes(state.service)) &&
      (!query || (p.title || '').normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().includes(query));
  }).sort((a, b) => (a.payload.title || '').localeCompare(
    b.payload.title || '', displayLocale(), {numeric:true}));
  $('streaming-summary').textContent = all.length.toLocaleString(displayLocale()) + ' ' +
    tr('titres enregistrés') + ' · ' + entries.length.toLocaleString(displayLocale()) + ' ' + tr('affichés');
  $('streaming-results').innerHTML = entries.map(row => {
    const p = row.payload;
    const id = String(p.work_id || '');
    const title = p.title || tr('Titre sans nom');
    const poster = safePoster(p.poster_path);
    const serviceNames = [...new Set((p.services || '').split(',').filter(x => providerNames[x]))];
    const services = serviceNames.map(key => {
      const destination = providerLink(row, key);
      return destination
        ? '<a class="streaming-provider streaming-provider-link" href="' + esc(destination) +
            '" target="_blank" rel="noopener noreferrer" title="' + esc(tr('Ouvrir sur') + ' ' + providerNames[key]) + '">' +
            esc(providerNames[key]) + '</a>'
        : '<span class="streaming-provider">' + esc(providerNames[key]) + '</span>';
    }).join('');
    const status = p.status === 'pending' ? '<span class="streaming-pending">' +
      esc(tr('Publication en attente')) + '</span>' : '';
    const meta = [p.kind === 'tv' ? tr('Série') : tr('Film'), p.year].filter(Boolean).join(' · ');
    return '<article class="streaming-card"><div class="streaming-poster">' +
      (poster ? '<img loading="lazy" decoding="async" src="' + esc(poster) + '" alt="">' :
        '<span aria-hidden="true">' + esc(title.slice(0, 2).toUpperCase()) + '</span>') +
      '</div><div class="streaming-card-body"><h3>' + esc(title) + '</h3>' +
      '<p class="streaming-meta">' + esc(meta) + '</p><div class="streaming-providers">' + services +
      '</div>' + status +
      (id && /^[a-z0-9_-]{1,128}$/i.test(id)
        ? '<button type="button" class="secondary streaming-open" data-streaming-media="' +
          esc(id) + '">' + esc(tr('Ouvrir la fiche')) + '</button>'
        : '') + '</div></article>';
  }).join('') || '<div class="empty-state"><h2>' + esc(tr('Aucun titre streaming')) +
    '</h2><p>' + esc(tr('Ajoutez des titres sur iOS puis synchronisez-les avec le serveur.')) +
    '</p></div>';
}
async function load() {
  if (state.loading) return;
  state.loading = true;
  $('streaming-refresh').disabled = true;
  $('streaming-error').hidden = true;
  $('streaming-summary').textContent = tr('Chargement du streaming…');
  try {
    const response = await fetch(new URL('api/v1/streaming/state', root),
      {credentials:'same-origin', cache:'no-store'});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    if (!data || !Array.isArray(data.rows)) throw new Error(tr('Réponse serveur invalide'));
    state.rows = data.rows;
    state.loaded = true;
    renderFilters();
    render();
  } catch (error) {
    $('streaming-error').textContent = tr('Impossible de charger le streaming : ') + error.message;
    $('streaming-error').hidden = false;
    $('streaming-summary').textContent = state.loaded
      ? tr('Données précédemment chargées') : tr('Streaming indisponible');
    if (state.loaded) render();
  } finally {
    state.loading = false;
    $('streaming-refresh').disabled = false;
  }
}
function route() {
  const streaming = window.location.hash === '#streaming';
  $('streaming-page').hidden = !streaming;
  $('streaming-link').setAttribute('aria-current', streaming ? 'page' : 'false');
  if (streaming && !$('app').hidden) void load();
}
export function startStreaming() {
  if (!$('streaming-page')) return;
  $('streaming-search').addEventListener('input', event => {
    state.query = event.target.value;
    if (state.loaded) render();
  });
  $('streaming-service').addEventListener('change', event => {
    state.service = event.target.value;
    if (state.loaded) render();
  });
  $('streaming-kind').addEventListener('change', event => {
    state.kind = event.target.value;
    if (state.loaded) render();
  });
  $('streaming-refresh').addEventListener('click', () => void load());
  $('streaming-results').addEventListener('click', event => {
    const button = event.target.closest('[data-streaming-media]');
    if (!button) return;
    window.dispatchEvent(new CustomEvent('streaming-open-media', {
      detail: {mediaID: button.dataset.streamingMedia}
    }));
  });
  window.addEventListener('hashchange', route);
  window.addEventListener('media-app-ready', route);
  route();
}
