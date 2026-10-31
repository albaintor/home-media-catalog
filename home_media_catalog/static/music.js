// Music library: albums (digital + physical editions), artists, tracks and
// music sources. Uses the same /api/v1 endpoints as the iOS app.
import { tr, displayLocale } from './i18n.js';

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const baseURL = new URL('.', window.location.href);
const url = (path) => new URL(path.replace(/^\/+/, ''), baseURL).href;
const FORMAT_LABELS = {Digital:'Fichiers', CD:'CD', SACD:'SACD', Vinyl:'Vinyle', Cassette:'Cassette', MiniDisc:'MiniDisc', 'DVD-Audio':'DVD-Audio', 'Blu-ray Audio':'Blu-ray Audio', 'Reel-to-reel':'Bande', Streaming:'Streaming', Other:'Autre'};
const SOURCE_LABELS = {kodi:'Kodi', discogs:'Discogs (CSV)', clz_music:'CLZ Music (XML)', music_csv:'Liste CSV / JSON'};
const state = {csrf:null, role:'read', tab:'albums', query:'', format:'', offset:0, total:0, items:[], loading:false, seq:0, players:[], queue:[], queueIndex:-1, debounce:null};

async function api(path, options={}) {
  const headers = new Headers(options.headers || {});
  if (state.csrf && options.method && options.method !== 'GET') headers.set('X-CSRF-Token', state.csrf);
  if (options.json !== undefined) { headers.set('Content-Type','application/json'); options.body = JSON.stringify(options.json); }
  const response = await fetch(url('api/v1/' + path), {...options, headers, credentials:'same-origin'});
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try { message = (await response.json()).error?.message || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function duration(seconds) {
  if (!seconds) return '';
  const total = Math.round(seconds), h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60), s = total % 60;
  return h ? `${h} h ${String(m).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
}
const formatBadges = (formats=[]) => formats.map(f => `<span class="music-format ${f==='Digital'?'digital':'physical'}">${esc(tr(FORMAT_LABELS[f] || f))}</span>`).join('');
const toast = (message) => { const node = $('toast'); if (!node) return; node.textContent = tr(message); node.hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => { node.hidden = true; }, 3500); };

function artwork(kind, id, label) {
  const initials = esc(String(label || '?').trim().slice(0, 2).toUpperCase());
  return `<span class="music-cover"><span class="music-cover-placeholder">${initials}</span><img loading="lazy" alt="" src="${url(`api/v1/music/${kind}/${encodeURIComponent(id)}/artwork/thumb`)}" onerror="this.remove()"></span>`;
}

async function open() {
  $('music-dialog').showModal();
  try {
    const session = await api('auth/session'); state.csrf = session.csrf_token; state.role = session.role;
  } catch { /* the catalogue login handles expired sessions */ }
  try { state.players = await api('players'); } catch { state.players = []; }
  await loadStatus();
  switchTab(state.tab);
}

async function loadStatus() {
  try {
    const status = await api('music/status');
    $('music-summary').textContent = `${status.albums.toLocaleString(displayLocale())} ${tr('albums')} · ${status.artists.toLocaleString(displayLocale())} ${tr('artistes')} · ${status.tracks.toLocaleString(displayLocale())} ${tr('titres')}`;
    const facets = await api('music/facets');
    $('music-format').innerHTML = `<option value="">${esc(tr('Tous les supports'))}</option>` + facets.formats.map(f => `<option value="${esc(f)}">${esc(tr(FORMAT_LABELS[f] || f))}</option>`).join('');
    $('music-format').value = state.format;
  } catch (error) { $('music-summary').textContent = tr('Bibliothèque musicale indisponible : ') + error.message; }
}

function switchTab(tab) {
  state.tab = tab;
  for (const button of document.querySelectorAll('[data-music-tab]')) button.setAttribute('aria-selected', String(button.dataset.musicTab === tab));
  $('music-browse').hidden = tab === 'sources';
  $('music-sources').hidden = tab !== 'sources';
  $('music-format').hidden = tab !== 'albums';
  if (tab === 'sources') renderSources(); else reload();
}

async function reload() {
  state.offset = 0; state.items = []; $('music-results').innerHTML = '';
  await loadMore();
}

async function loadMore() {
  if (state.loading) return;
  state.loading = true;
  const seq = ++state.seq;
  const params = new URLSearchParams({offset: state.offset, limit: 120});
  if (state.query) params.set('q', state.query);
  if (state.tab === 'albums' && state.format) params.set('format', state.format);
  try {
    const page = await api(`music/${state.tab}?${params}`);
    if (seq !== state.seq) return;
    state.total = page.total; state.offset += page.items.length; state.items.push(...page.items);
    const html = page.items.map(state.tab === 'albums' ? albumCard : state.tab === 'artists' ? artistCard : trackRow).join('');
    const results = $('music-results');
    results.className = state.tab === 'tracks' ? 'music-track-list' : 'music-grid';
    results.insertAdjacentHTML('beforeend', html);
    if (!state.items.length) results.innerHTML = `<p class="empty-state">${esc(tr(state.query ? 'Aucun résultat.' : 'Aucun élément. Ajoutez une source musicale dans l’onglet Sources.'))}</p>`;
    $('music-more').hidden = state.offset >= state.total;
  } catch (error) {
    $('music-results').innerHTML = `<p class="error-banner">${esc(error.message)}</p>`;
  } finally { state.loading = false; }
}

const albumCard = (album) => `<button type="button" class="music-card" data-album="${esc(album.id)}">${artwork('albums', album.id, album.title)}<span class="card-title">${esc(album.title)}</span><span class="card-meta">${esc(album.artist)}${album.year ? ' · ' + album.year : ''}</span><span class="music-formats">${formatBadges(album.formats)}</span></button>`;
const artistCard = (artist) => `<button type="button" class="music-card artist" data-artist="${esc(artist.id)}">${artwork('artists', artist.id, artist.name)}<span class="card-title">${esc(artist.name)}</span><span class="card-meta">${artist.album_count} ${esc(tr('albums'))}</span></button>`;

function trackRow(track, index) {
  const number = track.position || (track.track ? String(track.track) : '');
  return `<div class="music-track${track.playable ? '' : ' not-playable'}"><span class="music-track-number">${esc(number)}</span>
    <span class="music-track-title"><strong>${esc(track.title)}</strong><small>${esc(track.artist)}${track.album ? ' · ' + esc(track.album) : ''}</small></span>
    <span class="music-track-duration">${duration(track.duration_seconds)}</span>
    <span class="music-track-actions">${track.playable ? `<button type="button" class="action-button" data-play-track="${esc(track.id)}" data-index="${index ?? ''}" aria-label="${esc(tr('Écouter dans le navigateur'))}" title="${esc(tr('Écouter dans le navigateur'))}">▶</button>` : `<span class="hint">${esc(tr(FORMAT_LABELS[track.format] || track.format))}</span>`}</span></div>`;
}

async function showAlbum(id) {
  const album = await api(`music/albums/${encodeURIComponent(id)}`);
  const playable = album.tracks.filter(t => t.playable);
  const kodiPlayers = state.players.filter(p => album.editions.some(e => e.source_id === p.source_id));
  const editions = album.editions.map(e => `<details class="edition"><summary><strong>${esc(tr(FORMAT_LABELS[e.format] || e.format))}</strong> · ${esc(e.source_name)}${e.format_detail ? ' · ' + esc(e.format_detail) : ''}</summary><dl class="edition-grid">${[
    ['Label', e.label], ['Référence', e.catalog_number], ['Code-barres', e.barcode], ['Pays', e.country], ['Sortie', e.release_date || e.year],
    ['État du disque', e.media_condition], ['État de la pochette', e.sleeve_condition], ['Achat', e.purchase_date], ['Notes', e.notes],
    ['Discogs', e.discogs_id ? `<a href="https://www.discogs.com/release/${encodeURIComponent(e.discogs_id)}" target="_blank" rel="noopener">${esc(e.discogs_id)}</a>` : null],
  ].filter(([, v]) => v).map(([k, v]) => `<dt>${esc(tr(k))}</dt><dd>${k === 'Discogs' ? v : esc(v)}</dd>`).join('')}</dl></details>`).join('');
  $('music-detail').innerHTML = `<div class="music-detail-head">${artwork('albums', album.id, album.title)}<div>
    <p class="eyebrow">${esc(tr(album.compilation ? 'COMPILATION' : 'ALBUM'))}</p><h2>${esc(album.title)}</h2>
    <p class="music-detail-artists">${album.artists.map(a => `<button type="button" class="text-button" data-artist="${esc(a.id)}">${esc(a.name)}</button>`).join(' / ') || esc(album.artist)}</p>
    <p class="muted">${[album.year, album.label, album.genres.join(', '), album.track_count ? `${album.track_count} ${tr('titres')}` : '', duration(album.duration_seconds)].filter(Boolean).map(esc).join(' · ')}</p>
    <div class="music-formats">${formatBadges(album.formats)}</div>
    <div class="detail-actions">${playable.length ? `<button type="button" class="primary" data-play-album>▶ ${esc(tr('Écouter'))}</button>` : ''}
    ${kodiPlayers.map(p => `<button type="button" class="secondary" data-kodi-album="${esc(p.id)}">${esc(tr('Lire sur'))} ${esc(p.name)}</button>`).join('')}</div></div></div>
    <div class="detail-section"><p class="section-label">${esc(tr('ÉDITIONS'))} · ${album.editions.length}</p>${editions}</div>
    ${album.tracks.length ? `<div class="detail-section"><p class="section-label">${esc(tr('PISTES'))}</p><div class="music-track-list">${album.tracks.map((t, i) => trackRow(t, i)).join('')}</div></div>` : ''}`;
  state.detailTracks = album.tracks;
  $('music-detail').dataset.album = album.id;
  $('music-detail-dialog').showModal();
}

async function showArtist(id) {
  const artist = await api(`music/artists/${encodeURIComponent(id)}`);
  $('music-detail').innerHTML = `<div class="music-detail-head">${artwork('artists', artist.id, artist.name)}<div>
    <p class="eyebrow">${esc(tr('ARTISTE'))}</p><h2>${esc(artist.name)}</h2><p class="muted">${esc(artist.genres.join(', '))}</p>
    ${artist.biography ? `<p class="synopsis">${esc(artist.biography)}</p>` : ''}</div></div>
    <div class="detail-section"><p class="section-label">${esc(tr('ALBUMS'))} · ${artist.albums.length}</p><div class="music-grid">${artist.albums.map(albumCard).join('')}</div></div>
    ${artist.appears_on.length ? `<div class="detail-section"><p class="section-label">${esc(tr('APPARAÎT SUR'))}</p><div class="music-grid">${artist.appears_on.map(albumCard).join('')}</div></div>` : ''}`;
  $('music-detail-dialog').showModal();
}

// ---------------------------------------------------------------- playback
async function streamURL(track) {
  const player = state.players.find(p => p.source_id === track.source_id);
  if (!player) throw new Error(tr('Aucune instance Kodi n’est associée à la source de ce titre.'));
  const ticket = await api(`players/${encodeURIComponent(player.id)}/music-stream`, {method:'POST', json:{track_id: track.id}});
  return url(ticket.path.replace(/^\/+/, ''));
}

async function playQueue(tracks, index) {
  state.queue = tracks.filter(t => t.playable);
  state.queueIndex = Math.max(0, state.queue.findIndex(t => t.id === tracks[index]?.id));
  await playCurrent();
}

async function playCurrent() {
  const track = state.queue[state.queueIndex];
  if (!track) return;
  const audio = $('music-audio');
  try {
    audio.src = await streamURL(track);
    await audio.play();
  } catch (error) { toast(error.message); }
  $('music-player').hidden = false;
  $('music-now-title').textContent = track.title;
  $('music-now-artist').textContent = [track.artist, track.album].filter(Boolean).join(' · ');
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({title: track.title, artist: track.artist, album: track.album,
      artwork: track.album_id ? [{src: url(`api/v1/music/albums/${track.album_id}/artwork/thumb`)}] : []});
  }
}

function step(delta) {
  const next = state.queueIndex + delta;
  if (next < 0 || next >= state.queue.length) return;
  state.queueIndex = next; playCurrent();
}

// ----------------------------------------------------------------- sources
async function renderSources() {
  const container = $('music-sources');
  container.innerHTML = `<p class="hint">${esc(tr('Chargement…'))}</p>`;
  let sources = [];
  try { sources = await api('sources'); } catch (error) { container.innerHTML = `<p class="error-banner">${esc(error.message)}</p>`; return; }
  const admin = state.role === 'admin';
  const cards = sources.filter(s => SOURCE_LABELS[s.type]).map(source => {
    const isKodi = source.type === 'kodi';
    const actions = !admin ? '' : isKodi
      ? `<label class="check-label"><input type="checkbox" data-music-auto="${esc(source.id)}" ${source.config.music_enabled ? 'checked' : ''}> ${esc(tr('Inclure la musique dans chaque synchronisation'))}</label>
         <button type="button" class="secondary" data-music-sync="${esc(source.id)}">↻ ${esc(tr('Synchroniser la musique'))}</button>`
      : `<label class="secondary music-upload">${esc(tr('Importer un export…'))}<input type="file" data-music-import="${esc(source.id)}" data-type="${esc(source.type)}" accept=".csv,.tsv,.txt,.json,.xml" hidden></label>
         <button type="button" class="text-button" data-music-delete="${esc(source.id)}">${esc(tr('Supprimer'))}</button>`;
    return `<div class="connection-card"><div><strong class="source-name">${esc(source.name)}</strong><p>${esc(tr(SOURCE_LABELS[source.type]))} · ${source.music_count} ${esc(tr('albums'))}${source.last_error ? ' · ⚠ ' + esc(source.last_error) : ''}</p></div><div class="connection-actions">${actions}</div></div>`;
  }).join('');
  container.innerHTML = `<p class="muted">${esc(tr('Synchronisez la bibliothèque musicale de Kodi (JSON-RPC, avec repli MariaDB MyMusic) ou importez vos collections physiques : CD, vinyles, cassettes…'))}</p>${cards || ''}
    ${admin ? `<form id="music-source-form" class="connection-form"><label>${esc(tr('Nom'))}<input name="name" required maxlength="120" placeholder="${esc(tr('Mes vinyles'))}"></label>
    <label>${esc(tr('Type'))}<select name="type"><option value="discogs">${esc(tr(SOURCE_LABELS.discogs))}</option><option value="clz_music">${esc(tr(SOURCE_LABELS.clz_music))}</option><option value="music_csv">${esc(tr(SOURCE_LABELS.music_csv))}</option></select></label>
    <p class="hint wide">${esc(tr('Liste CSV : colonnes Artiste, Album/Titre et, au choix, Année, Support (CD, Vinyle…), Label, Référence, Code-barres, Genre, Notes, Pistes.'))}</p>
    <button class="primary wide" type="submit">${esc(tr('Ajouter la collection'))}</button></form>` : ''}
    <div id="music-job" class="job-status" hidden></div>`;
}

async function watchJob(job) {
  const node = $('music-job');
  if (node) { node.hidden = false; node.textContent = tr('Import en cours…'); }
  for (let attempt = 0; attempt < 1800; attempt++) {
    const current = await api(`jobs/${job.id}`);
    if (current.status === 'completed' || current.status === 'failed') {
      const message = current.status === 'failed' ? tr('Échec : ') + current.error
        : current.result?.music_error ? tr('Vidéo synchronisée, musique en échec : ') + current.result.music_error
        : `${tr('Terminé')} · ${current.result?.music_albums ?? 0} ${tr('albums')} · ${current.result?.music_tracks ?? 0} ${tr('titres')}`;
      if ($('music-job')) $('music-job').textContent = message;
      toast(message); await loadStatus(); if (state.tab === 'sources') renderSources();
      return;
    }
    if ($('music-job')) $('music-job').textContent = `${tr('En cours')} · ${current.overall_percent} %`;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
}

async function toggleAuto(sourceId, enabled) {
  const sources = await api('sources');
  const source = sources.find(s => s.id === sourceId);
  if (!source) return;
  const config = {...source.config, music_enabled: enabled};
  delete config.has_password; delete config.has_remote_api_key;
  await api(`sources/${encodeURIComponent(sourceId)}`, {method:'PUT', json:{name: source.name, type: source.type, enabled: source.enabled, config}});
  toast(enabled ? 'La musique sera synchronisée avec la vidéothèque.' : 'Synchronisation musicale automatique désactivée.');
}

// ----------------------------------------------------------------- events
export function startMusic() {
  if (!$('music-button')) return;
  $('music-button').addEventListener('click', open);
  for (const button of document.querySelectorAll('[data-music-tab]')) button.addEventListener('click', () => switchTab(button.dataset.musicTab));
  $('music-search').addEventListener('input', event => {
    clearTimeout(state.debounce);
    state.debounce = setTimeout(() => { state.query = event.target.value.trim(); if (state.tab !== 'sources') reload(); }, 250);
  });
  $('music-format').addEventListener('change', event => { state.format = event.target.value; reload(); });
  $('music-more').addEventListener('click', loadMore);
  const audio = $('music-audio');
  audio.addEventListener('ended', () => step(1));
  $('music-prev').addEventListener('click', () => step(-1));
  $('music-next').addEventListener('click', () => step(1));
  if ('mediaSession' in navigator) {
    navigator.mediaSession.setActionHandler('previoustrack', () => step(-1));
    navigator.mediaSession.setActionHandler('nexttrack', () => step(1));
  }
  document.addEventListener('click', async event => {
    const target = event.target.closest('[data-album],[data-artist],[data-play-track],[data-play-album],[data-kodi-album],[data-music-sync],[data-music-delete]');
    if (!target || !target.closest('#music-dialog,#music-detail-dialog')) return;
    try {
      if (target.dataset.album) await showAlbum(target.dataset.album);
      else if (target.dataset.artist) await showArtist(target.dataset.artist);
      else if (target.dataset.playTrack) {
        const inDetail = target.closest('#music-detail-dialog');
        const list = inDetail ? state.detailTracks : state.items;
        await playQueue(list, list.findIndex(t => t.id === target.dataset.playTrack));
      } else if (target.hasAttribute('data-play-album')) await playQueue(state.detailTracks, 0);
      else if (target.dataset.kodiAlbum) {
        await api(`players/${encodeURIComponent(target.dataset.kodiAlbum)}/music/play`, {method:'POST', json:{album_id: $('music-detail').dataset.album}});
        toast('Lecture lancée sur Kodi.');
      } else if (target.dataset.musicSync) {
        await watchJob(await api(`sources/${encodeURIComponent(target.dataset.musicSync)}/music/sync`, {method:'POST'}));
      } else if (target.dataset.musicDelete) {
        if (!window.confirm(tr('Supprimer cette collection musicale ?'))) return;
        await api(`sources/${encodeURIComponent(target.dataset.musicDelete)}`, {method:'DELETE'});
        await loadStatus(); renderSources();
      }
    } catch (error) { toast(error.message); }
  });
  document.addEventListener('change', async event => {
    const input = event.target;
    try {
      if (input.dataset?.musicAuto) await toggleAuto(input.dataset.musicAuto, input.checked);
      else if (input.dataset?.musicImport && input.files?.length) {
        const body = new FormData();
        body.append('source_id', input.dataset.musicImport);
        body.append('file', input.files[0]);
        await watchJob(await api(`imports/music/${encodeURIComponent(input.dataset.type)}`, {method:'POST', body}));
      }
    } catch (error) { toast(error.message); }
  });
  document.addEventListener('submit', async event => {
    if (event.target.id !== 'music-source-form') return;
    event.preventDefault();
    const data = new FormData(event.target);
    try {
      await api('sources', {method:'POST', json:{name: data.get('name'), type: data.get('type'), config:{}}});
      renderSources();
    } catch (error) { toast(error.message); }
  });
}
