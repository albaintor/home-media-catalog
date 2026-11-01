// Music library: albums (digital + physical editions), artists, tracks and
// music sources. Uses the same /api/v1 endpoints as the iOS app.
import { tr, displayLocale } from './i18n.js';

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const baseURL = new URL('.', window.location.href);
const url = (path) => new URL(path.replace(/^\/+/, ''), baseURL).href;
const FORMAT_LABELS = {Digital:'Fichiers', CD:'CD', SACD:'SACD', Vinyl:'Vinyle', Cassette:'Cassette', MiniDisc:'MiniDisc', 'DVD-Audio':'DVD-Audio', 'Blu-ray Audio':'Blu-ray Audio', 'Reel-to-reel':'Bande', Streaming:'Streaming', Other:'Autre'};
const SOURCE_LABELS = {kodi:'Kodi', discogs:'Discogs (CSV)', clz_music:'CLZ Music (XML)', music_csv:'Liste CSV / JSON'};
const state = {csrf:null, role:'read', tab:'albums', query:'', format:'', offset:0, total:0, items:[], loading:false, seq:0, players:[], queue:[], queueIndex:-1, canonicalQueue:[], shuffle:false, repeatMode:0, playbackToken:0, artistAlbums:new Map(), artistObserver:null, debounce:null};

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
    if (state.tab === 'artists') observeArtistCards(results);
    if (!state.items.length) results.innerHTML = `<p class="empty-state">${esc(tr(state.query ? 'Aucun résultat.' : 'Aucun élément. Ajoutez une source musicale dans l’onglet Sources.'))}</p>`;
    $('music-more').hidden = state.offset >= state.total;
  } catch (error) {
    $('music-results').innerHTML = `<p class="error-banner">${esc(error.message)}</p>`;
  } finally { state.loading = false; }
}

const albumCard = (album) => `<button type="button" class="music-card" data-album="${esc(album.id)}">${artwork('albums', album.id, album.title)}<span class="card-title">${esc(album.title)}</span><span class="card-meta">${esc(album.artist)}${album.year ? ' · ' + album.year : ''}</span><span class="music-formats">${formatBadges(album.formats)}</span></button>`;
const artistCard = (artist) => `<button type="button" class="music-card artist" data-artist="${esc(artist.id)}"><span class="music-cover music-artist-mosaic" data-mosaic-artist="${esc(artist.id)}"><span class="music-cover-placeholder">♫</span></span><span class="card-title">${esc(artist.name)}</span><span class="card-meta">${artist.album_count} ${esc(tr('albums'))}</span></button>`;

function artistMosaic(albums) {
  const selected = albums.slice(0, 4);
  if (!selected.length) return '<span class="music-cover-placeholder">♫</span>';
  const cells = selected.map(album => `<img alt="" loading="lazy" decoding="async" src="${url(`api/v1/music/albums/${encodeURIComponent(album.id)}/artwork/thumb`)}" onerror="this.style.visibility='hidden'">`);
  while (cells.length < 4 && selected.length > 2) cells.push('<span class="music-mosaic-empty">♫</span>');
  return `<span class="music-mosaic-cells ${selected.length === 1 ? 'single' : selected.length === 2 ? 'double' : ''}">${cells.join('')}</span>`;
}

function observeArtistCards(root) {
  if (!state.artistObserver) {
    state.artistObserver = new IntersectionObserver(entries => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        state.artistObserver.unobserve(entry.target);
        const element = entry.target, id = element.dataset.mosaicArtist;
        const cached = state.artistAlbums.get(id);
        if (cached) { element.innerHTML = artistMosaic(cached); continue; }
        api(`music/artists/${encodeURIComponent(id)}`).then(artist => {
          const albums = artist.albums || [];
          state.artistAlbums.set(id, albums);
          if (element.isConnected && element.dataset.mosaicArtist === id) element.innerHTML = artistMosaic(albums);
        }).catch(() => { /* retain placeholder when artist detail is unavailable */ });
      }
    }, {rootMargin:'200px'});
  }
  for (const element of root.querySelectorAll('[data-mosaic-artist]')) {
    const cached = state.artistAlbums.get(element.dataset.mosaicArtist);
    if (cached) element.innerHTML = artistMosaic(cached);
    else state.artistObserver.observe(element);
  }
}

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
  $('music-detail').innerHTML = `<div class="music-detail-head"><span class="music-cover music-artist-mosaic">${artistMosaic(artist.albums || [])}</span><div>
    <p class="eyebrow">${esc(tr('ARTISTE'))}</p><h2>${esc(artist.name)}</h2><p class="muted">${esc(artist.genres.join(', '))}</p>
    ${artist.biography ? `<p class="synopsis">${esc(artist.biography)}</p>` : ''}</div></div>
    <div class="detail-section"><p class="section-label">${esc(tr('ALBUMS'))} · ${artist.albums.length}</p><div class="music-grid">${artist.albums.map(albumCard).join('')}</div></div>
    ${artist.appears_on.length ? `<div class="detail-section"><p class="section-label">${esc(tr('APPARAÎT SUR'))}</p><div class="music-grid">${artist.appears_on.map(albumCard).join('')}</div></div>` : ''}`;
  $('music-detail-dialog').showModal();
}

// ---------------------------------------------------------------- playback
// Unlike a POST ticket followed by audio.play(), this authenticated GET
// can be attached synchronously during the user's click. Safari otherwise
// rejects play() after an asynchronous network round trip.
function streamURL(track) {
  return url(`api/v1/players/music/tracks/${encodeURIComponent(track.id)}/stream`);
}

function playableTracks(tracks) {
  return tracks.filter(t => t.playable && t.file);
}

function updateQueueUI() {
  const next = $('music-next'), prev = $('music-prev');
  if (next) next.disabled = !(state.queueIndex + 1 < state.queue.length || (state.repeatMode === 1 && state.queue.length));
  if (prev) prev.disabled = state.queueIndex < 0;
  const shuffle = $('music-shuffle'), repeat = $('music-repeat');
  if (shuffle) shuffle.setAttribute('aria-pressed', String(state.shuffle));
  if (repeat) {
    repeat.setAttribute('aria-label', ['Ne pas répéter', 'Répéter la file', 'Répéter le titre'][state.repeatMode]);
    repeat.textContent = state.repeatMode === 2 ? '🔂' : '🔁';
    repeat.setAttribute('aria-pressed', String(state.repeatMode !== 0));
  }
  const list = $('music-queue-list');
  if (list) list.innerHTML = state.queue.map((track, index) =>
    `<li><button type="button" data-music-queue-index="${index}" ${index === state.queueIndex ? 'aria-current="true"' : ''}><span>${index === state.queueIndex ? '♫ ' : ''}${esc(track.title)}</span><small>${esc(track.artist)}</small></button></li>`).join('');
  if ('mediaSession' in navigator) {
    navigator.mediaSession.setPositionState && updateMediaPosition();
  }
}

function updateMediaPosition() {
  const audio = $('music-audio');
  if (!audio || !('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
  const durationValue = Number.isFinite(audio.duration) ? audio.duration : state.queue[state.queueIndex]?.duration_seconds;
  if (!(durationValue > 0)) return;
  try {
    navigator.mediaSession.setPositionState({
      duration: durationValue,
      position: Math.max(0, Math.min(durationValue, audio.currentTime || 0)),
      playbackRate: audio.playbackRate || 1
    });
  } catch { /* browser may not support position updates */ }
}

async function playQueue(tracks, index) {
  const playable = playableTracks(tracks);
  const selected = tracks[index];
  if (!playable.length) return toast('Aucun morceau lisible.');
  state.canonicalQueue = playable.slice();
  if (state.shuffle) {
    const current = playable.find(t => t.id === selected?.id) || playable[0];
    const remaining = playable.filter(t => t !== current);
    for (let i = remaining.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [remaining[i], remaining[j]] = [remaining[j], remaining[i]];
    }
    state.queue = [current, ...remaining];
    state.queueIndex = 0;
  } else {
    state.queue = playable.slice();
    state.queueIndex = Math.max(0, playable.findIndex(t => t.id === selected?.id));
  }
  updateQueueUI();
  await playCurrent();
}

async function playCurrent() {
  const track = state.queue[state.queueIndex];
  if (!track) return;
  const token = ++state.playbackToken;
  const audio = $('music-audio');
  $('music-player').hidden = false;
  $('music-now-title').textContent = track.title;
  $('music-now-artist').textContent = [track.artist, track.album].filter(Boolean).join(' · ');
  updateQueueUI();
  if ('mediaSession' in navigator) {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title, artist: track.artist, album: track.album,
      artwork: track.album_id ? [{src: url(`api/v1/music/albums/${encodeURIComponent(track.album_id)}/artwork/thumb`), sizes:'512x512'}] : []
    });
    navigator.mediaSession.playbackState = 'paused';
  }
  try {
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    if (token !== state.playbackToken) return;
    audio.src = streamURL(track);
    await audio.play();
  } catch (error) { if (token === state.playbackToken) toast(error.message); }
}

function step(delta) {
  if (!state.queue.length) return;
  const next = state.queueIndex + delta;
  if (next < 0) {
    $('music-audio').currentTime = 0;
    return;
  }
  if (next >= state.queue.length) {
    if (state.repeatMode !== 1) return;
    state.queueIndex = 0;
  } else state.queueIndex = next;
  void playCurrent();
}

function completed() {
  if (state.repeatMode === 2) {
    const audio = $('music-audio');
    audio.currentTime = 0;
    void audio.play().catch(error => toast(error.message));
  } else if (state.queueIndex + 1 < state.queue.length || state.repeatMode === 1) {
    step(1);
  } else if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
}

function toggleShuffle() {
  state.shuffle = !state.shuffle;
  if (state.queue.length > 1 && state.queueIndex >= 0) {
    if (state.shuffle) {
      const previous = state.queue.slice(0, state.queueIndex + 1);
      const remaining = state.queue.slice(state.queueIndex + 1);
      for (let i = remaining.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [remaining[i], remaining[j]] = [remaining[j], remaining[i]];
      }
      state.queue = [...previous, ...remaining];
    } else {
      const id = state.queue[state.queueIndex]?.id;
      state.queue = state.canonicalQueue.slice();
      state.queueIndex = Math.max(0, state.queue.findIndex(t => t.id === id));
    }
  }
  updateQueueUI();
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
  audio.addEventListener('ended', completed);
  audio.addEventListener('error', () => {
    if (state.queue[state.queueIndex] && audio.getAttribute('src')) {
      const errorCode = audio.error?.code;
      toast(errorCode === 4
        ? 'Format audio non pris en charge ou flux réseau inaccessible.'
        : 'Impossible de lire ce titre. Vérifiez la connexion réseau ou Kodi.');
    }
  });
  audio.addEventListener('play', () => { if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing'; });
  audio.addEventListener('pause', () => { if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused'; });
  audio.addEventListener('seeked', updateMediaPosition);
  audio.addEventListener('loadedmetadata', updateMediaPosition);
  $('music-prev').addEventListener('click', () => step(-1));
  $('music-next').addEventListener('click', () => step(1));
  $('music-shuffle')?.addEventListener('click', toggleShuffle);
  $('music-repeat')?.addEventListener('click', () => { state.repeatMode = (state.repeatMode + 1) % 3; updateQueueUI(); });
  $('music-queue-button')?.addEventListener('click', () => {
    const list = $('music-queue-panel');
    list.hidden = !list.hidden;
    $('music-queue-button').setAttribute('aria-expanded', String(!list.hidden));
    updateQueueUI();
  });
  $('music-queue-list')?.addEventListener('click', event => {
    const button = event.target.closest('[data-music-queue-index]');
    if (!button) return;
    state.queueIndex = Number(button.dataset.musicQueueIndex);
    void playCurrent();
  });
  if ('mediaSession' in navigator) {
    const handlers = {
      play: () => void audio.play(), pause: () => audio.pause(),
      previoustrack: () => step(-1), nexttrack: () => step(1),
      seekto: details => { if (Number.isFinite(details.seekTime)) audio.currentTime = details.seekTime; },
      seekbackward: details => { audio.currentTime = Math.max(0, audio.currentTime - (details.seekOffset || 10)); },
      seekforward: details => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + (details.seekOffset || 10)); }
    };
    for (const [action, handler] of Object.entries(handlers)) {
      try { navigator.mediaSession.setActionHandler(action, handler); } catch { /* unsupported action */ }
    }
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
