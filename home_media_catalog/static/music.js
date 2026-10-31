// Music library view: albums and artists from Kodi and physical collections.
// Kept separate from app.js; it only uses the shared helpers passed in.

const $ = (id) => document.getElementById(id);

export function setupMusic(ctx) {
  const { api, url, esc, toast, tr, pollJob } = ctx;
  const state = { tab: 'albums', artistId: null, artistName: '', items: [], offset: 0, hasMore: false, seq: 0, loaded: false };
  const button = $('music-button');
  const view = $('music-view');
  if (!button || !view) return;

  const duration = (seconds) => {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  };
  const cover = (item, className = 'music-cover') => item.artwork?.poster
    ? `<img class="${className}" loading="lazy" src="${esc(url(item.artwork.poster))}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:this.className+' music-cover-empty',textContent:'♪'}))">`
    : `<div class="${className} music-cover-empty" aria-hidden="true">♪</div>`;
  const badges = (formats) => (formats || []).map((f) => `<span class="music-badge">${esc(f)}</span>`).join('');

  function showMusic(visible) {
    view.hidden = !visible;
    document.querySelector('.catalogue').hidden = visible;
    button.setAttribute('aria-pressed', String(visible));
    if (visible && !state.loaded) { state.loaded = true; void loadFacets(); void reload(); }
  }
  button.addEventListener('click', () => showMusic(view.hidden));

  async function loadFacets() {
    try {
      const [facets, status] = await Promise.all([api('music/facets'), api('music/status')]);
      $('music-format').innerHTML = `<option value="">${esc(tr('Tous les supports'))}</option>` +
        facets.formats.map((f) => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
      renderStatus(status);
    } catch (error) { toast(error.message); }
  }
  function renderStatus(status) {
    const n = (v) => Number(v || 0).toLocaleString();
    $('music-status').textContent = status.albums
      ? `${n(status.artists)} artistes · ${n(status.albums)} albums (${n(status.physical_albums)} physiques) · ${n(status.songs)} titres`
      : tr('Aucune musique. Activez la bibliothèque musicale sur une source Kodi, ou importez votre liste de CD/vinyles (Discogs, CLZ Music, CSV) dans les paramètres.');
  }

  function query() {
    const params = new URLSearchParams({ limit: '60', offset: String(state.offset) });
    const q = $('music-search').value.trim();
    if (q) params.set('q', q);
    if (state.tab === 'artists') return `music/artists?${params}`;
    const origin = $('music-origin').value;
    if (origin === 'physical') params.set('physical', 'true');
    if (origin === 'digital') params.set('physical', 'false');
    if ($('music-format').value) params.set('format', $('music-format').value);
    params.set('sort', $('music-sort').value);
    if (['year', 'date_added'].includes($('music-sort').value)) params.set('order', 'desc');
    if (state.artistId) { params.set('artist_id', state.artistId); params.set('sort', 'year'); params.set('order', 'asc'); }
    return `music/albums?${params}`;
  }

  async function reload() { state.offset = 0; state.items = []; await load(); }
  async function load() {
    const seq = ++state.seq;
    try {
      const page = await api(query());
      if (seq !== state.seq) return;
      state.items = [...state.items, ...page.items];
      state.offset += page.items.length;
      state.hasMore = page.has_more;
      render();
    } catch (error) { toast(error.message); }
  }

  function render() {
    const results = $('music-results');
    $('music-back').hidden = !state.artistId;
    $('music-more').hidden = !state.hasMore;
    for (const id of ['music-origin', 'music-format', 'music-sort']) $(id).hidden = state.tab === 'artists';
    if (state.tab === 'artists') {
      results.className = 'music-grid music-artists';
      results.innerHTML = state.items.map((a) => `<button type="button" class="music-card" data-music-artist="${esc(a.id)}" data-music-artist-name="${esc(a.name)}">${cover(a, 'music-cover music-artist-cover')}<strong>${esc(a.name)}</strong><span class="muted">${a.album_count} album${a.album_count > 1 ? 's' : ''}</span></button>`).join('') || `<p class="hint">${esc(tr('Aucun artiste.'))}</p>`;
      return;
    }
    results.className = 'music-grid';
    const heading = state.artistId ? `<h3 class="music-artist-heading">${esc(state.artistName)}</h3>` : '';
    results.innerHTML = heading + (state.items.map((album) => `<button type="button" class="music-card" data-music-album="${esc(album.id)}">${cover(album)}<strong>${esc(album.title)}</strong><span class="muted">${esc(album.artist)}${album.year ? ' · ' + album.year : ''}</span><span class="music-badges">${album.physical ? '<span class="music-badge physical">Physique</span>' : ''}${badges(album.formats)}</span></button>`).join('') || `<p class="hint">${esc(tr('Aucun album.'))}</p>`);
  }

  async function openAlbum(id) {
    try {
      const album = await api(`music/albums/${encodeURIComponent(id)}`);
      const players = (ctx.players() || []).filter((p) => album.editions.some((e) => e.source_type === 'kodi' && e.source_id === p.source_id));
      const tracks = album.songs.length
        ? album.songs.map((s) => `<li><span class="music-track-no">${s.disc && s.disc > 1 ? s.disc + '-' : ''}${s.track ?? ''}</span><span class="music-track-title">${esc(s.title)}${s.artist && s.artist !== album.artist ? ` <span class="muted">· ${esc(s.artist)}</span>` : ''}</span><span class="muted">${s.duration_seconds ? duration(s.duration_seconds) : ''}</span>${players.length ? `<button type="button" class="icon-button" data-music-play="${esc(s.id)}" aria-label="${esc(tr('Lire sur Kodi'))}">▶</button>` : ''}</li>`).join('')
        : album.physical_tracks.map((t) => `<li><span class="music-track-no">${t.track ?? ''}</span><span class="music-track-title">${esc(t.title)}</span><span class="muted">${t.duration_seconds ? duration(t.duration_seconds) : ''}</span></li>`).join('');
      const editions = album.editions.map((e) => `<li>${badges([...(e.formats || [])])} <strong>${esc(e.source_name)}</strong>${e.label ? ' · ' + esc(e.label) : ''}${e.catalog_number ? ' · ' + esc(e.catalog_number) : ''}${e.media_condition ? ' · ' + esc(e.media_condition) : ''}${e.barcode ? ` · <span class="muted">${esc(e.barcode)}</span>` : ''}</li>`).join('');
      const playControls = players.length
        ? `<div class="music-play"><select id="music-player">${players.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')}</select><button type="button" class="primary" data-music-play="${esc(album.id)}">${esc(tr('Lire l’album sur Kodi'))}</button></div>`
        : '';
      $('music-detail').innerHTML = `<div class="music-detail">${cover(album, 'music-detail-cover')}<div><p class="eyebrow">${esc(album.compilation ? tr('COMPILATION') : tr('ALBUM'))}</p><h2>${esc(album.title)}</h2><p><button type="button" class="text-button" data-music-artist="${esc(album.artist_id || '')}" data-music-artist-name="${esc(album.artist)}">${esc(album.artist)}</button>${album.year ? ' · ' + album.year : ''}${album.track_count ? ' · ' + album.track_count + ' titres' : ''}${album.duration_seconds ? ' · ' + duration(album.duration_seconds) : ''}</p><p>${(album.genres || []).map(esc).join(', ')}</p>${playControls}<h3>${esc(tr('Éditions'))}</h3><ul class="music-editions">${editions}</ul></div></div>${album.plot ? `<p class="music-review">${esc(album.plot)}</p>` : ''}${tracks ? `<h3>${esc(tr('Titres'))}</h3><ol class="music-tracks">${tracks}</ol>` : ''}`;
      $('music-dialog').showModal();
    } catch (error) { toast(error.message); }
  }

  let searchTimer;
  $('music-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => void reload(), 250); });
  for (const id of ['music-origin', 'music-format', 'music-sort']) $(id).addEventListener('change', () => void reload());
  $('music-more').addEventListener('click', () => void load());
  $('music-back').addEventListener('click', () => { state.artistId = null; void reload(); });
  for (const tab of ['albums', 'artists']) {
    $(`music-tab-${tab}`).addEventListener('click', () => {
      state.tab = tab; state.artistId = null;
      $('music-tab-albums').setAttribute('aria-selected', String(tab === 'albums'));
      $('music-tab-artists').setAttribute('aria-selected', String(tab === 'artists'));
      void reload();
    });
  }

  document.addEventListener('click', async (event) => {
    const target = event.target.closest('button');
    if (!target) return;
    if (target.dataset.musicAlbum) { await openAlbum(target.dataset.musicAlbum); return; }
    if (target.dataset.musicArtist !== undefined && target.dataset.musicArtist) {
      $('music-dialog').close();
      showMusic(true);
      state.tab = 'albums'; state.artistId = target.dataset.musicArtist; state.artistName = target.dataset.musicArtistName || '';
      $('music-tab-albums').setAttribute('aria-selected', 'true');
      $('music-tab-artists').setAttribute('aria-selected', 'false');
      await reload();
      return;
    }
    if (target.dataset.musicPlay) {
      const player = $('music-player')?.value;
      if (!player) return;
      target.disabled = true;
      try {
        await api(`players/${encodeURIComponent(player)}/play-music`, { method: 'POST', body: JSON.stringify({ media_id: target.dataset.musicPlay }) });
        toast(tr('Lecture lancée sur Kodi.'));
      } catch (error) { toast(error.message); } finally { target.disabled = false; }
      return;
    }
    if (target.dataset.syncMusic) {
      target.disabled = true;
      try {
        toast(tr('Synchronisation de la musique Kodi demandée.'));
        await pollJob(await api(`sources/${encodeURIComponent(target.dataset.syncMusic)}/sync-music`, { method: 'POST' }));
        state.loaded = false;
        if (!view.hidden) { state.loaded = true; await loadFacets(); await reload(); }
      } catch (error) { toast(error.message); } finally { target.disabled = false; }
    }
  });
}
