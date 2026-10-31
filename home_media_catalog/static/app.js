import { startKodiRemote, stopKodiRemote, showKodiRemote } from './remote.js';
import { startI18n, displayLocale, tr, localizedPrompt, localizedConfirm } from './i18n.js';
import { MediaPlayer } from './player.js';
import { startMusic } from './music.js';
startI18n();
startMusic();
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stateLabels = {watched:tr('Vu'),unwatched:tr('Non vu'),in_progress:tr('En cours'),unknown:tr('État non renseigné')};
const kindLabels = {movie:tr('Film'),tvshow:tr('Série'),episode:tr('Épisode')};
const baseURL = new URL('.', window.location.href);
const url = (path) => new URL(path.replace(/^\/+/, ''), baseURL).href;
const preferenceKey = `kodi_library_preferences_v1:${baseURL.pathname}`;
function readStorage(key) { try { return localStorage.getItem(key); } catch { return null; } }
function writeStorage(key,value) { try { localStorage.setItem(key,value); } catch { /* Browsing also works when storage is unavailable. */ } }
let preferences;
try { preferences=JSON.parse(readStorage(preferenceKey)) || {}; } catch { preferences={}; }
if(typeof preferences!=='object'||Array.isArray(preferences)) preferences={};
const filterIds = ['search','kind','source-type','genre','collection','watch-status','sort'];
let filtersRestored=false;
const savedView = ['grid','list'].includes(preferences.view)?preferences.view:readStorage('kodi_library_view');
const canonicalFormat = value => {
  const label=String(value).trim();
  return /^(4k uhd|4k blu-ray|blu-ray 4k|uhd blu-ray|ultra hd blu-ray)$/i.test(label)?'Blu-ray 4K':/^4k$/i.test(label)?'4K':label;
};
function formatCategory(label) {
  if (/^(dolby atmos|dolby truehd|dolby digital|dts(?:[-:]|$)|aac|flac|pcm|lpcm|ac3|eac3|mp3|opus)/i.test(label)) return 'audio';
  if (/^(blu-ray|dvd|hd dvd|uhd blu-ray|ultra hd blu-ray|4k uhd)/i.test(label)) return 'disc';
  return 'video';
}
// Media formats are always multi-select chips, grouped video → audio → discs.
function updateFormatSelections() {
  const selected=new Set(state.formats);
  const categories={video:'video-formats',audio:'audio-formats',disc:'disc-formats'};
  const values=[...new Set((state.facets?.formats||[]).map(canonicalFormat))]
    .sort((a,b)=>a.localeCompare(b,displayLocale(),{numeric:true}));
  for(const [category,id] of Object.entries(categories)) {
    $(id).innerHTML=values.filter(v=>formatCategory(v)===category).map(label=>{
      const isSelected=selected.has(label);
      return `<button type="button" class="format-option" data-toggle-format="${esc(label)}" aria-label="${esc(label)}" aria-pressed="${isSelected}" title="${esc(label)}">${formatBadges([label])}</button>`;
    }).join('')||'<span class="format-help">Aucun format disponible.</span>';
  }
  $('format-selection-count').textContent=state.formats.length?`${state.formats.length} sélectionné${state.formats.length>1?'s':''} · filtres cumulés (ET)`:'Sélectionnez plusieurs formats pour les cumuler (ET).';
}

const state = {csrf:null,role:'read',view:savedView==='list'?'list':'grid',order:preferences.order==='asc'?'asc':'desc',limit:60,recent:preferences.recent===true,formats:[],deepSearch:preferences.deepSearch===true,facets:null,players:[],tmdbAvailable:false,jobs:[],activityPollBusy:false,activityWasRunning:false,personalUpdateState:null,personalRemoteFailure:false,personalPollBusy:false,personalSaveCount:0,seq:0,controller:null,detail:null,detailSeq:0,personalSeq:0,personalPending:new Set(),quickPending:new Set(),enrichingIds:new Set(),personSearch:null,seasons:[],playbackDraft:null,playbackMediaId:null,listId:typeof preferences.listId==='string'?preferences.listId:null,listItems:[],listRevision:0,listSort:['position','title','year','date_added','rating','user_rating'].includes(preferences.listSort)?preferences.listSort:'position',listOrder:preferences.listOrder==='desc'?'desc':'asc',listTotal:0,listSeq:0,items:[],ids:new Set(),nextOffset:0,total:0,hasMore:false,loading:false,loadError:false};
function savePreferences() {
  if(!filtersRestored)return;
  writeStorage(preferenceKey,JSON.stringify({filters:Object.fromEntries(filterIds.map(id=>[id,$(id).value])),formats:state.formats,deepSearch:state.deepSearch,order:state.order,recent:state.recent,view:state.view,listId:state.listId,listSort:state.listSort,listOrder:state.listOrder}));
}
function updateSearchClearButton() {
  $('clear-search').hidden=!$('search').value&&!state.personSearch;
}
function restoreFilters() {
  if(filtersRestored)return;
  for(const id of filterIds){
    const value=id==='kind'&&preferences.filters?.kind==='episode'?'':preferences.filters?.[id];
    if(typeof value==='string'&&(id==='search'||[...$(id).options].some(option=>option.value===value)))$(id).value=value;
  }
  const previousFormat=preferences.filters?.format;
  const savedFormats=Array.isArray(preferences.formats)?preferences.formats:
    typeof previousFormat==='string'&&previousFormat?[previousFormat]:[];
  const available=new Set((state.facets?.formats||[]).map(canonicalFormat));
  state.formats=[...new Set(savedFormats.filter(v=>typeof v==='string').map(canonicalFormat))]
    .filter(v=>available.has(v)).slice(0,20);
  updateFormatSelections();
  $('sort-order').textContent=state.order==='asc'?'↑ Croissant':'↓ Décroissant';
  // Always start collapsed, even if an older version persisted the expanded state.
  $('filters-panel').open=false;
  $('deep-search').checked=state.deepSearch;
  $('toggle-filters').setAttribute('aria-expanded','false');
  filtersRestored=true;
  updateSearchClearButton();
}
const moreObserver = 'IntersectionObserver' in window ? new IntersectionObserver(entries=>{
  if(entries.some(entry=>entry.isIntersecting)&&!state.loading&&!state.loadError&&state.hasMore&&!$('app').hidden&&!document.querySelector('dialog[open]')) loadMedia({append:true});
},{rootMargin:'600px 0px'}) : null;
let toastTimer;
// Track-aware player shared by Kodi files and network locations.
async function fetchMedia(address, kind) {
  const response=await fetch(address,{credentials:'same-origin'});
  if(!response.ok) {
    let message=`HTTP ${response.status}`;
    try { const detail=(await response.json()).detail; if(typeof detail==='string')message=detail; } catch { /* Not JSON. */ }
    throw new Error(tr(message));
  }
  return kind==='json'?response.json():response.text();
}
const mediaPlayer=new MediaPlayer({fetchJSON:address=>fetchMedia(address,'json'),fetchText:address=>fetchMedia(address,'text')});
function openPlayer(source) {
  lockPage();
  networkThumbnailPaused=true;
  for(const controller of networkThumbnailControllers)controller.abort();
  return mediaPlayer.open({...source,onClose:(position)=>{
    source.onClose?.(position);
    networkThumbnailPaused=false;
    runNetworkThumbnailQueue();
    setTimeout(releasePage);
  }});
}
function toast(message) { $('toast').textContent=tr(message); $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,5500); }
function error(message) { $('error-banner').textContent=tr(message); $('error-banner').hidden=false; }
// The application shell stays loaded while the API/server is unreachable.
const RETRY_SECONDS=10;
let offlineTimer=null,offlineRemaining=RETRY_SECONDS,offlineBusy=false;
function unavailableError(message) {
  const failure=new Error(message || 'Le serveur ne répond pas.');
  failure.serverUnavailable=true;
  return failure;
}
function stopOfflineTimer() { if(offlineTimer!==null){clearInterval(offlineTimer);offlineTimer=null;} }
function scheduleOfflineRetry() {
  stopOfflineTimer();
  offlineRemaining=RETRY_SECONDS;
  $('retry-server-countdown').textContent='Nouvelle tentative dans '+offlineRemaining+' s…';
  offlineTimer=setInterval(()=>{
    if($('server-unavailable').hidden){stopOfflineTimer();return;}
    offlineRemaining-=1;
    if(offlineRemaining<=0) { stopOfflineTimer();void retryServerConnection(); }
    else $('retry-server-countdown').textContent='Nouvelle tentative dans '+offlineRemaining+' s…';
  },1000);
}
function showServerUnavailable(failure) {
  if($('server-unavailable').hidden) {
    stopKodiRemote(); ++state.seq; state.controller?.abort(); state.loading=false;
    moreObserver?.disconnect();
    document.querySelectorAll('dialog[open]').forEach(dialog=>dialog.close());
    $('app').hidden=true; $('login').hidden=true; $('server-unavailable').hidden=false;
  }
  $('server-unavailable-reason').textContent=failure?.message||'Le serveur ne répond pas.';
  if(!offlineTimer&&!offlineBusy) scheduleOfflineRetry();
}
function showLogin() {
  stopOfflineTimer();stopKodiRemote();++state.seq;state.controller?.abort();state.loading=false;
  moreObserver?.disconnect();$('server-unavailable').hidden=true;$('app').hidden=true;$('login').hidden=false;
}
async function retryServerConnection() {
  if(offlineBusy||$('server-unavailable').hidden)return;
  offlineBusy=true;stopOfflineTimer();
  const button=$('retry-server');button.disabled=true;
  $('retry-server-countdown').textContent='Connexion en cours…';
  try {
    await api('auth/session',{cache:'no-store'});
    await initialize();
  } catch(failure) {
    if(failure.status===401) showLogin();
    else if(failure.serverUnavailable) showServerUnavailable(failure);
    else $('server-unavailable-reason').textContent=failure.message;
  } finally {
    offlineBusy=false;button.disabled=false;
    if(!$('server-unavailable').hidden&&!offlineTimer)scheduleOfflineRetry();
  }
}
$('retry-server').addEventListener('click',()=>void retryServerConnection());
window.addEventListener('online',()=>{if(!$('server-unavailable').hidden)void retryServerConnection();});
if('serviceWorker' in navigator && window.isSecureContext) {
  // Cache only the public static shell, never API responses or credentials.
  navigator.serviceWorker.register(url('service-worker.js'),{scope:baseURL.pathname}).catch(()=>{});
}
async function api(path, options={}) {
  const headers=new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type','application/json');
  if (state.csrf && options.method && options.method !== 'GET') headers.set('X-CSRF-Token',state.csrf);
  let response;
  try {
    response=await fetch(url(`api/v1/${path}`),{...options,headers,credentials:'same-origin'});
  } catch(failure) {
    if(failure.name==='AbortError')throw failure;
    const unavailable=unavailableError('Le serveur ne répond pas ou la connexion réseau est interrompue.');
    showServerUnavailable(unavailable);
    throw unavailable;
  }
  if([502,503,504].includes(response.status)||(response.status===500&&path==='auth/session')) {
    // An individual operation (Kodi playback, writeback, etc.) can return
    // 502/503 while the catalog server itself remains healthy. Distinguish
    // this from an actual server outage before covering the whole screen.
    let serverResponding=false;
    if(path!=='auth/session') {
      const controller=new AbortController();
      const timeout=setTimeout(()=>controller.abort(),3000);
      try {
        const probe=await fetch(url('api/v1/discovery'),{
          cache:'no-store',credentials:'same-origin',signal:controller.signal
        });
        serverResponding=probe.ok;
      } catch { /* The health probe could not reach the server. */ }
      finally { clearTimeout(timeout); }
    }
    if(!serverResponding) {
      const unavailable=unavailableError('Le serveur est momentanément indisponible (HTTP '+response.status+').');
      showServerUnavailable(unavailable);
      throw unavailable;
    }
  }
  if (response.status===204) return null;
  const contentType=response.headers.get('content-type')||'';
  let value=null;
  if(contentType.includes('json')){
    try { value=await response.json(); }
    catch {
      const problem=new Error('Réponse JSON invalide du serveur (HTTP '+response.status+').');
      problem.status=response.status;
      throw problem;
    }
  }else{
    // A reverse proxy or an unhandled Python exception can return plain
    // "Internal Server Error" or an HTML error page instead of JSON.
    // Never pass these responses to JSON.parse().
    const body=(await response.text()).trim();
    if(!response.ok){
      const description=response.status===500
        ? 'Erreur interne du serveur pendant cette opération. Consultez les journaux du conteneur.'
        : body && !body.startsWith('<') && body.length<240 ? body
        : 'Le serveur a répondu avec un contenu non JSON.';
      const problem=new Error(description+' (HTTP '+response.status+').');
      problem.status=response.status;
      throw problem;
    }
    const problem=new Error('Réponse inattendue du serveur : JSON attendu.');
    problem.status=response.status;
    throw problem;
  }
  if (!response.ok) {
    if (response.status===401 && path!=='auth/session') showLogin();
    const detail=typeof value?.detail==='string'?value.detail:
      (typeof value?.error?.message==='string'?value.error.message:null);
    const failure=new Error(detail || `Erreur HTTP ${response.status}`);
    failure.status=response.status;
    throw failure;
  }
  return value;
}
function date(value) { if (!value) return '—'; const d=new Date(value); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString(displayLocale()); }
function dateTime(value) {
  if(!value)return '—';
  const parsed=new Date(value);
  return Number.isNaN(parsed.getTime())?'—':parsed.toLocaleString(displayLocale(),{dateStyle:'short',timeStyle:'short'});
}
function writebackStatusLabel(status){
  return ({updated:'Synchronisé',pending:'En attente',failed:'Échec',unavailable:'Indisponible'})[status]||'Statut inconnu';
}
function writebackHistoryRow(entry, historical=false){
  const timestamp=dateTime(historical?entry.created_at:entry.updated_at);
  const source=entry.source_name||'Source';
  const title=entry.media_title||'Média inconnu';
  const status=writebackStatusLabel(entry.status);
  const method=entry.method||'—';
  const fields=historical?entry.fields:entry.pending_fields;
  const detail=Array.isArray(fields)&&fields.length?'<span>Champs : '+esc(fields.join(', '))+'</span>':'';
  const attempts=historical&&Array.isArray(entry.attempts)&&entry.attempts.length
    ?'<span>Tentatives : '+esc(entry.attempts.map(v=>typeof v==='string'?v:JSON.stringify(v)).join(' · '))+'</span>':'';
  return `<li class="writeback-history-row"><div class="writeback-history-main"><strong>${esc(title)}</strong><span class="writeback-result ${esc(entry.status||'unavailable')}">${esc(status)}</span></div><div class="writeback-history-meta"><span>${esc(source)}</span><time datetime="${esc(historical?entry.created_at:entry.updated_at||'')}">${esc(timestamp)}</time><span>Méthode : ${esc(method)}</span>${detail}${attempts}</div>${entry.message?'<p class="hint">'+esc(entry.message)+'</p>':''}</li>`;
}
function playbackTime(seconds) {
  const total=Math.max(0,Math.floor(Number(seconds)||0));
  const hours=Math.floor(total/3600),minutes=Math.floor(total%3600/60),secs=total%60;
  return hours?`${hours}:${String(minutes).padStart(2,'0')}:${String(secs).padStart(2,'0')}`:`${minutes}:${String(secs).padStart(2,'0')}`;
}
const collectionTypes=['clz','mymovies','filmotech'];
function sourceName(type) { return ({kodi:'Kodi',clz:'CLZ',mymovies:'MyMovies',filmotech:'Filmotech',discogs:'Discogs · musique',clz_music:'CLZ Music',music_csv:'Liste musicale'})[type]||type; }
function sourceIcon(type) {
  const shapes={
    clz:'<path d="m3 7 9-5 9 5-9 5Z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5" fill="none" stroke="currentColor" stroke-width="2"/>',
    mymovies:'<rect x="2" y="4" width="20" height="16" rx="3"/><path d="m10 8 6 4-6 4Z" fill="var(--bg,#121815)"/>',
    filmotech:'<rect x="2" y="3" width="20" height="18" rx="2"/><path d="M7 3v18M17 3v18M2 8h5m-5 8h5M17 8h5m-5 8h5" fill="none" stroke="var(--bg,#121815)" stroke-width="2"/>'
  };
  if(type==='kodi') return `<img class="source-icon" src="${esc(url('static/kodi-logo.svg'))}" alt="" width="18" height="18">`;
  return `<svg class="source-icon source-icon-${esc(type)}" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">${shapes[type]||shapes.clz}</svg>`;
}
// Use the same directional visual language as iOS: down = Kodi → catalog, up = catalog → Kodi.
function transferIcon(direction) {
  const arrow=direction==='up'?'M12 16V8m-4 4 4-4 4 4':'M12 8v8m-4-4 4 4 4-4';
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="${arrow}"/></svg>`;
}
function writebackIndicatorMarkup(item) {
  const states=item.writeback_state||[];
  if(!states.length)return '';
  return '<span class="writeback-indicators" aria-label="Synchronisations vers les référentiels">'+
    states.map(entry=>{
      const mark=entry.status==='updated'?'✓':entry.status==='pending'?transferIcon('up'):'!';
      const label=`${entry.source_name} : ${entry.status==='updated'?'envoi terminé':entry.status==='pending'?'envoi vers la source en attente de rejeu':'échec de l’envoi'}${entry.message?' · '+entry.message:''}`;
      return `<span class="writeback-chip ${esc(entry.status)}" title="${esc(label)}" aria-label="${esc(label)}">${sourceIcon(entry.source_type)}<span class="writeback-mark" aria-hidden="true">${mark}</span></span>`;
    }).join('')+'</span>';
}
function renderWritebackIndicators(){
  const target=$('writeback-indicators');
  if(target&&state.detail)target.innerHTML=writebackIndicatorMarkup(state.detail);
}
async function refreshWritebackQueue(){
  const queue=$('writeback-queue'),summary=$('writeback-summary');
  if(!queue||!summary)return;
  try{
    const data=await api('writebacks/status');
    summary.textContent=data.pending_count?
      `${data.pending_count} synchronisation(s) en attente de rejeu.`:
      'Aucune synchronisation en attente. Les imports ne réécrivent jamais les sources.';
    queue.innerHTML=(data.pending||[]).map(item=>
      `<div class="connection-card"><div><strong>${sourceIcon(item.source_type)} ${esc(item.source_name)}</strong><p>${esc(item.media_title)} · ${esc(Object.keys(item.changes).join(', '))} · ${esc(date(item.updated_at))}</p>${item.last_error?`<p class="error-text">${esc(item.last_error)}</p>`:''}</div><div class="connection-actions"><button type="button" data-retry-writeback="${esc(item.id)}" ${state.role==='admin'?'':'disabled'}>Rejouer</button></div></div>`).join('')||'<p class="hint">Aucun échec à rejouer.</p>';
    const all=$('retry-all-writebacks');if(all)all.disabled=!data.pending_count||state.role!=='admin';
    const recent=$('writeback-recent');
    if(recent){
      const entries=data.recent||[];
      recent.innerHTML=entries.length
        ?'<ul class="writeback-history-list">'+entries.map(item=>writebackHistoryRow(item)).join('')+'</ul>'
        :'<p class="hint">Aucune synchronisation sortante récente.</p>';
    }
  }catch(e){summary.textContent='Chargement impossible : '+e.message;}
}
// Global status remains in the header when a media detail is dismissed.
function renderPersonalStatus(){
  const node=$('personal-update-indicator'),status=state.personalUpdateState;
  node.hidden=!status||$('app').hidden;
  if(!status)return;
  const labels={saving:'Enregistrement…',pending:'Envoi des modifications vers les sources externes en attente',saved:'Modifications enregistrées',failed:'Échec de l’enregistrement',remoteFailed:'Modifications locales enregistrées · échec externe'};
  const icon=$('personal-update-icon');
  if(['saving','pending'].includes(status))icon.innerHTML=transferIcon('up');
  else icon.textContent=status==='saved'?'✓':'!';
  $('personal-update-label').textContent=labels[status];
  node.className='personal-update-indicator '+status;
  node.title=labels[status];
  node.setAttribute('aria-label',labels[status]);
}
function beginPersonalSave(){
  state.personalSaveCount++;
  state.personalUpdateState='saving';
  state.personalRemoteFailure=false;
  renderPersonalStatus();
}
function endPersonalSave(failed=false,checkRemote=false){
  state.personalSaveCount=Math.max(0,state.personalSaveCount-1);
  if(state.personalSaveCount)return;
  state.personalUpdateState=failed?'failed':checkRemote?'pending':'saved';
  renderPersonalStatus();
  if(!failed&&checkRemote)pollPersonalWritebacks();
}
async function pollPersonalWritebacks(){
  if(state.personalPollBusy||state.personalSaveCount||state.personalUpdateState!=='pending'||$('app').hidden)return;
  state.personalPollBusy=true;
  try{
    const result=await api('writebacks/status');
    if(state.personalUpdateState!=='pending'||state.personalSaveCount)return;
    if(result.pending_count===0){
      state.personalUpdateState=state.personalRemoteFailure?'remoteFailed':'saved';
      renderPersonalStatus();
    }
  }catch(e){
    // Unreachable server does not imply a completed synchronization.
  }finally{state.personalPollBusy=false;}
}
$('personal-update-indicator').addEventListener('click',()=>pollPersonalWritebacks());
function sourceBadge(type,label) { return `<span class="source-badge" title="${esc(label||sourceName(type))}">${sourceIcon(type)}<span>${esc(label||sourceName(type))}</span></span>`; }
function badges(item) {
  return (item.media_sources?.length?item.media_sources.map(s=>sourceBadge(s.type,s.type_count>1?s.name:sourceName(s.type))):item.source_types.map(s=>sourceBadge(s))).join('');
}
function seriesProgress(item) {
  if(item.kind!=='tvshow')return null;
  const total=Number(item.episode_count),watched=Number(item.watched_episode_count);
  if(!Number.isFinite(total)||total<=0||!Number.isFinite(watched))return null;
  const count=Math.max(0,Math.min(total,watched));
  return {total,watched:count,partial:count>0&&count<total,complete:count===total};
}
function watchDisplayLabel(item) {
  const progress=seriesProgress(item);
  if(progress?.partial)return `Partiellement vu (${progress.watched}/${progress.total} épisodes)`;
  if(progress?.complete)return `Vu (${progress.watched}/${progress.total} épisodes)`;
  return stateLabels[item.watch_status];
}
function seriesProgressMarkup(item) {
  const progress=seriesProgress(item);
  if(!progress||!progress.watched)return '';
  const label=watchDisplayLabel(item);
  return `<span class="series-progress-chip ${progress.partial?'partial':'complete'}" role="status" title="${esc(label)}" aria-label="${esc(label)}"><span aria-hidden="true">${progress.partial?'◐':'✓'}</span> ${progress.watched}/${progress.total} épisodes vus</span>`;
}
function poster(item, detail=false) {
  const progress=item.total_seconds>0 ? Math.min(100,100*item.resume_seconds/item.total_seconds):0;
  const series=seriesProgress(item);
  const stateIcon=series?.partial
    ? `<span class="state partial" title="${esc(watchDisplayLabel(item))}" aria-label="${esc(watchDisplayLabel(item))}">◐</span>`
    :series?.complete||item.watch_status==='watched'
    ? '<span class="state" title="Vu" aria-label="Vu">✓</span>'
    :item.watch_status==='in_progress'
    ? '<span class="state progress" title="En cours" aria-label="En cours">◐</span>':'';
  return `<div class="poster"><span class="poster-placeholder" aria-hidden="true">${esc(item.title.slice(0,2).toUpperCase())}</span>${item.artwork.poster?`<img src="${esc(url(item.artwork.poster))}" alt="" ${detail?'':'loading="lazy"'}>`:''}${!detail&&item.rating?`<span class="rating">★ ${Number(item.rating).toFixed(1)}</span>`:''}${!detail?stateIcon:''}${(series?.partial||progress>0&&item.watch_status==='in_progress')?`<span class="progress-track ${series?.partial?'series-track':''}"><span style="width:${series?.partial?100*series.watched/series.total:progress}%"></span></span>`:''}</div>`;
}
const formatLogos = {'4K':['4k','video'],'Blu-ray 4K':['bluray4k','disc'],'8K':['8k','video'],'1080p':['1080p','video'],'720p':['720p','video'],'3D':['3d','video'],'Blu-ray 3D':['3d','disc'],'Blu-ray':['bluray','disc'],'DVD':['dvd','disc'],'Dolby Vision':['vision','hdr'],'HDR10':['hdr10','hdr'],'HDR10+':['hdr10plus','hdr'],'HLG':['hlg','hdr'],'Dolby Atmos':['atmos','audio'],'Dolby TrueHD':['truehd','audio'],'DTS-HD MA':['dtshdma','audio'],'DTS-HD':['dtshd','audio'],'DTS:X':['dtsx','audio'],'DTS':['dts','audio'],'Dolby Digital':['dd','audio'],'Dolby Digital+':['ddplus','audio'],'AAC':['aac','audio'],'FLAC':['flac','audio'],'Opus':['opus','audio']};

// Reuse the exact PNG blobs in iOS Assets.xcassets for branded formats.
// New native PDF vector badges use the equivalent SVG symbols below because
// browser <img> does not reliably render PDFs.
const formatImages = {
  '4K':'format-4k','3D':'format-3d','Blu-ray 3D':'format-3d',
  'Blu-ray':'format-blu-ray','Dolby Vision':'format-dolby-vision',
  'HDR10':'format-hdr10','HDR10+':'format-hdr10-plus','HLG':'format-hlg',
  'Dolby Atmos':'format-dolby-atmos','Dolby TrueHD':'format-dolby-truehd',
  'Dolby Digital':'format-dolby-digital','Dolby Digital+':'format-dolby-digital-plus',
  'DTS':'format-dts','DTS-HD':'format-dts-hd','DTS-HD MA':'format-dts-hd-ma',
  'DTS:X':'format-dts-x','AAC':'format-aac','FLAC':'format-flac','Opus':'format-opus'
};
function formatBadges(values, compact=false, provenance=null) {
  const seen=new Set();
  const order={video:0,hdr:0,audio:1,disc:2};
  const labels=values.map(canonicalFormat).filter(label=>{const key=formatLogos[label]?.[0]||label;if(seen.has(key))return false;seen.add(key);return true;})
    .sort((a,b)=>(order[formatLogos[a]?.[1]]??0)-(order[formatLogos[b]?.[1]]??0)||a.localeCompare(b,displayLocale(),{numeric:true}));
  const visible=compact?labels.slice(0,4):labels;
  return visible.map(label=>{
    const logo=formatLogos[label];
    const sources=provenance?.get(label);
    const description=sources?.length?`${label} · ${sources.join(', ')}`:label;
    return logo?`<span class="format-badge ${logo[1]}" title="${esc(description)}" aria-label="${esc(description)}">${formatImages[label]?`<img src="${esc(url('static/format-icons/'+formatImages[label]+'.png'))}" alt="" loading="lazy">`:`<svg aria-hidden="true" viewBox="0 0 112 36"><use href="${esc(url('static/format-logos.svg'))}#${logo[0]}"></use></svg>`}</span>`:`<span class="format-chip" title="${esc(description)}">${esc(label)}</span>`;
  }).join('')+(compact&&labels.length>4?`<span class="format-overflow" title="${esc(labels.slice(4).join(', '))}">+${labels.length-4}</span>`:'');
}
function detailFormatBadges(item) {
  // The detail must reflect editions, never an outdated aggregated works.formats.
  const origins=new Map();
  for(const edition of item.editions||[]) {
    const source=sourceName(edition.source_type);
    const name=edition.source_name?`${source} (${edition.source_name})`:source;
    for(const raw of edition.metadata?.formats||[]) {
      const label=canonicalFormat(raw);
      if(!origins.has(label))origins.set(label,[]);
      if(!origins.get(label).includes(name))origins.get(label).push(name);
    }
  }
  return formatBadges([...origins.keys()].sort((a,b)=>a.localeCompare(b,displayLocale())),false,origins);
}
const personCategories = {cast:'Casting',director:'Réalisation',writer:'Scénario',producer:'Production',composer:'Musique · Composition',photography:'Photographie',crew:'Autres fonctions'};
function castMarkup(people=[]) {
  const members=list=>`<ul class="cast-grid">${list.map(member=>{
    const links=member.links||{};
    const name=esc(member.name);
    return `<li class="cast-member"><button type="button" class="person-photo" data-search-person="${name}" data-person-tmdb="${esc(member.tmdb_id||'')}" data-person-imdb="${esc(member.imdb_id||'')}" aria-label="Rechercher ${name} dans la médiathèque" title="Rechercher les films et séries de ${name}"><span class="cast-initials" aria-hidden="true">${esc(member.name.split(/\s+/).slice(0,2).map(word=>word[0]).join(''))}</span>${member.thumbnail?`<img src="${esc(url(member.thumbnail))}" alt="" loading="lazy">`:''}</button><span><button type="button" class="person-name" data-search-person="${name}" data-person-tmdb="${esc(member.tmdb_id||'')}" data-person-imdb="${esc(member.imdb_id||'')}" title="Rechercher ${name} dans la médiathèque"><strong>${name}</strong></button>${member.role?`<small>${esc(member.role)}</small>`:''}<span class="person-links">${Object.entries(links).map(([site,link])=>`<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${site==='imdb_search'?'Recherche IMDb':site.toUpperCase()} ↗</a>`).join(' · ')}</span></span></li>`;
  }).join('')}</ul>`;
  return Object.entries(personCategories).map(([category,label])=>{
    const group=people.filter(p=>(p.category||'cast')===category); if(!group.length)return '';
    return `<section class="detail-section credits-section"><p class="section-label">${label.toUpperCase()} · ${group.length}</p>${members(group.slice(0,8))}${group.length>8?`<details class="cast-more"><summary>Voir les ${group.length-8} autres personnes</summary>${members(group.slice(8))}</details>`:''}</section>`;
  }).join('');
}
function actionIcon(name) {
  const icons={
    star:'<path d="m12 2.5 2.9 5.9 6.5.9-4.7 4.6 1.1 6.5-5.8-3.1-5.8 3.1 1.1-6.5-4.7-4.6 6.5-.9z"/>',
    youtube:'<rect x="1" y="4" width="22" height="16" rx="5" fill="#ff3838"/><path d="m10 8 6 4-6 4z" fill="white"/>',
    watch:'<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    watched:'<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/><path d="m16 20 2 2 4-5"/>',
    refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 6.1A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.9 5.9"/>',
    playtarget:'<rect x="2.5" y="4" width="19" height="14" rx="2"/><path d="M9.5 8v6l5-3z"/><path d="M9 21h6"/>'
  };
  const filled=['star','youtube'].includes(name);
  return `<svg aria-hidden="true" viewBox="0 0 24 24" fill="${filled?'currentColor':'none'}" ${filled?'':'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"'}>${icons[name]||''}</svg>`;
}
function personalActions(item) {
  const writable=state.role==='admin'&&!state.personalPending.has(item.id);
  const value=item.user_rating;
  return `<div class="rating-control"><button id="rating-button" class="action-button rating-button ${value!=null?'rated':''}" aria-label="Votre note : ${value!=null?esc(value)+'/10':'non renseignée'}${state.role==='admin'?', modifier':''}" aria-haspopup="dialog" aria-controls="rating-popover" aria-expanded="false" ${writable?'':'disabled'}>${actionIcon('star')}<span class="rating-value">${value!=null?esc(value):'—'}</span></button><div id="rating-popover" class="rating-popover" popover="manual" role="dialog" aria-label="Modifier votre note" hidden><div class="rating-popover-heading"><strong>Votre note</strong><output id="rating-preview">${value!=null?esc(value):'—'}<small> / 10</small></output></div><div class="rating-stars" role="group" aria-label="Choisir une note sur dix">${Array.from({length:10},(_,i)=>`<button class="rating-choice" data-rating="${i+1}" aria-label="Donner la note de ${i+1} sur 10" aria-pressed="${value===i+1}" ${writable?'':'disabled'}><span class="rating-glyph">${actionIcon('star')}<span class="rating-choice-fill">${actionIcon('star')}</span></span><small>${i+1}</small></button>`).join('')}</div><label class="rating-slider-label">Par demi-point<input id="rating-slider" type="range" min="0" max="10" step="0.5" value="${value??0}" aria-label="Votre note par demi-point" ${writable?'':'disabled'}></label><button class="text-button rating-reset" data-rating-reset ${writable?'':'disabled'}>Utiliser la note des sources</button><p class="hint">Cliquez sur une étoile pour enregistrer.</p></div></div>`;
}
function watchState(item) {
  if(item.kind==='tvshow'){
    const total=Number(item.episode_count)||0,viewed=Number(item.watched_episode_count)||0;
    const watched=total>0&&viewed===total;
    return {watched,available:total>0,status:watched?'watched':viewed>0?'in_progress':'unwatched',label:watched?'Vu':viewed>0?'Partiellement vu':'Non vu'};
  }
  return {watched:item.watch_status==='watched',available:true,status:item.watch_status,label:stateLabels[item.watch_status]};
}
function watchMarkup(item) {
  const view=watchState(item);
  const label=state.role==='admin'?(view.available?`${view.label} · Marquer comme ${view.watched?'non vu':'vu'}${item.kind==='tvshow'?' tous les épisodes':''}`:'Aucun épisode importé'):`${view.label} · Accès en lecture seule`;
  return `<button id="watch-button" class="status-chip watch-button ${esc(view.status)}" aria-label="${esc(label)}" aria-pressed="${view.watched}" title="${esc(label)}" data-tooltip="${esc(label)}" ${state.role==='admin'&&view.available&&!state.personalPending.has(item.id)?'':'disabled'}>${actionIcon(view.watched?'watched':'watch')}<span class="watch-state">${esc(view.label)}</span></button>`;
}
function detailActions(item) {
  const trailers=[...new Set(item.trailers||[])];
  const link=trailers[0]||'https://www.youtube.com/results?search_query='+encodeURIComponent(item.title+' '+(item.year||'')+' bande annonce');
  const multiple=trailers.length>1;
  const label=multiple?`Choisir une bande-annonce parmi ${trailers.length}`:trailers.length?'Voir la bande-annonce sur YouTube':'Rechercher une bande-annonce sur YouTube';
  const attributes=multiple?'id="open-trailers" aria-haspopup="dialog" aria-controls="trailers-dialog"':`id="trailer-link" href="${esc(link)}" target="_blank" rel="noopener noreferrer"`;
  const trailerButton=`<${multiple?'button':'a'} ${attributes} class="action-button trailer-button" aria-label="${label}" title="${label}">${actionIcon('youtube')}<span>Bande-annonce${multiple?` · ${trailers.length}`:''}</span></${multiple?'button':'a'}>`;
  const playable=item.kind==='tvshow'
    ? (item.source_types||[]).includes('kodi')&&Number(item.episode_count)>0
    : item.editions.some(e=>e.source_type==='kodi');
  return `<div class="detail-actions compact-actions">${trailerButton}${personalActions(item)}${playable?`<button id="open-playback" class="action-button play-target-button" title="Lire sur…" aria-label="Lire sur…" aria-haspopup="dialog" aria-controls="play-dialog">${actionIcon('playtarget')}<span>Lire sur…</span></button>`:''}<span id="writeback-indicators" class="writeback-indicators-container">${writebackIndicatorMarkup(item)}</span><span id="personal-status" class="visually-hidden" role="status" aria-live="polite"></span></div>`;
}
function openTrailers() {
  hideRatingPopover();
  const item=state.detail;if(!item)return;
  $('trailers-content').innerHTML=`<p class="play-media-title">${esc(item.title)}</p><div class="trailer-choices">${[...new Set(item.trailers||[])].map((link,index)=>`<a class="external-button" data-trailer href="${esc(link)}" target="_blank" rel="noopener noreferrer">${actionIcon('youtube')}<span>Bande-annonce ${index+1}<small>YouTube</small></span><span aria-hidden="true">↗</span></a>`).join('')}</div>`;
  showDialog('trailers-dialog');
}
function metadataAction(item) {
  if(item.kind==='episode')return '';
  const configured=item.tmdb_available;
  const label=configured?'Rafraîchir les métadonnées TMDB':'Configurer TMDB dans les paramètres';
  return `<div class="detail-tools"><button ${configured?'id="refresh-metadata"':'data-action="connections"'} class="action-button tmdb-button ${state.enrichingIds.has(item.id)?'is-loading':''}" title="${label}" data-tooltip="${label}" aria-label="${label}" ${state.role==='admin'&&!state.enrichingIds.has(item.id)?'':'disabled'}><img src="${esc(url('static/tmdb-logo.svg'))}" alt="" width="27" height="27">${configured?`<span class="refresh-mark">${actionIcon('refresh')}</span>`:''}</button></div>`;
}
let ratingCloseTimer;
function positionRatingPopover() {
  const popup=$('rating-popover'),button=$('rating-button');if(!popup||popup.hidden||!button)return;
  const rect=button.getBoundingClientRect(),gap=10,edge=12;
  popup.style.left=`${Math.max(edge,Math.min(rect.left,innerWidth-popup.offsetWidth-edge))}px`;
  const below=rect.bottom+gap;
  const top=below+popup.offsetHeight<=innerHeight-edge?below:Math.max(edge,rect.top-popup.offsetHeight-gap);
  popup.style.top=`${top}px`;
}
function showRatingPopover(focus=false) {
  clearTimeout(ratingCloseTimer);
  const popup=$('rating-popover'),button=$('rating-button');if(!popup||!button||button.disabled)return;
  popup.hidden=false;
  if(popup.showPopover&&!popup.matches(':popover-open'))popup.showPopover();
  popup.dataset.open='true';button.setAttribute('aria-expanded','true');positionRatingPopover();
  if(focus)$('rating-slider').focus({preventScroll:true});
}
function hideRatingPopover() {
  clearTimeout(ratingCloseTimer);
  const popup=$('rating-popover');
  if(popup){if(popup.hidePopover&&popup.matches(':popover-open'))popup.hidePopover();popup.hidden=true;delete popup.dataset.open;}
  $('rating-button')?.setAttribute('aria-expanded','false');
}
function scheduleRatingClose() {
  clearTimeout(ratingCloseTimer);
  ratingCloseTimer=setTimeout(()=>{if(!$('rating-popover')?.contains(document.activeElement))hideRatingPopover();},180);
}
function previewRating(value) {
  const output=$('rating-preview');if(output)output.innerHTML=`${value!=null?esc(value):'—'}<small> / 10</small>`;
  document.querySelectorAll('[data-rating]').forEach(button=>{
    const number=Number(button.dataset.rating);
    button.style.setProperty('--star-fill',`${Math.min(100,Math.max(0,((value??0)-number+1)*100))}%`);
  });
  $('rating-slider')?.setAttribute('aria-valuetext',`${value??0} sur 10`);
}
function syncPersonalControls() {
  const item=state.detail;if(!item)return;
  const busy=state.personalPending.has(item.id),writable=state.role==='admin'&&!busy;
  const rating=$('rating-button'),watch=$('watch-button');
  if(rating){rating.disabled=!writable;rating.classList.toggle('rated',item.user_rating!=null);rating.querySelector('.rating-value').textContent=item.user_rating??'—';rating.setAttribute('aria-label',`Votre note : ${item.user_rating!=null?item.user_rating+'/10':'non renseignée'}${state.role==='admin'?', modifier':''}`);}
  if(watch){
    const view=watchState(item);
    const label=state.role==='admin'?(view.available?`${view.label} · Marquer comme ${view.watched?'non vu':'vu'}${item.kind==='tvshow'?' tous les épisodes':''}`:'Aucun épisode importé'):`${view.label} · Accès en lecture seule`;
    watch.disabled=!writable||!view.available;watch.className=`status-chip watch-button ${view.status}`;watch.setAttribute('aria-pressed',view.watched);watch.setAttribute('aria-label',label);watch.title=label;watch.dataset.tooltip=label;
    watch.innerHTML=`${actionIcon(view.watched?'watched':'watch')}<span class="watch-state">${esc(view.label)}</span>`;
  }
  document.querySelectorAll('[data-rating],[data-rating-reset],#rating-slider').forEach(control=>control.disabled=!writable);
  document.querySelectorAll('[data-rating]').forEach(button=>button.setAttribute('aria-pressed',Number(button.dataset.rating)===item.user_rating));
  if($('rating-slider'))$('rating-slider').value=item.user_rating??0;
  previewRating(item.user_rating);
  if($('personal-status')&&busy)$('personal-status').textContent='Enregistrement…';
}
async function savePersonal(values) {
  const item=state.detail;if(!item||state.role!=='admin'||state.personalPending.has(item.id))return;
  const id=item.id,seq=state.detailSeq;
  const previous={personal:{...item.personal},user_rating:item.user_rating,watch_status:item.watch_status};
  state.personalPending.add(id);++state.personalSeq;hideRatingPopover();beginPersonalSave();
  item.personal={...item.personal,...values};
  if(values.user_rating!=null)item.user_rating=values.user_rating;
  if(typeof values.watched==='boolean')item.watch_status=values.watched?'watched':'unwatched';
  syncPersonalControls();
  let saved=false;
  try {
    const response=await api(`media/${encodeURIComponent(id)}/personal`,{method:'PATCH',body:JSON.stringify(values)});
    saved=true;
    const warnings=(response.writeback||[]).filter(result=>result.status!=='updated'||result.message);
    const message=warnings.map(result=>result.source_name+' : '+(result.message||'Synchronisation non effectuée')).join(' · ');
    if(seq===state.detailSeq&&state.detail?.id===id){
      Object.assign(state.detail,{personal:response.personal,user_rating:response.user_rating,watch_status:response.watch_status,writeback_state:response.writeback_state||[]});renderWritebackIndicators();
      if($('personal-status'))$('personal-status').textContent=warnings.length?'Modification locale enregistrée. '+message:'Modification enregistrée.';
    }
    if(warnings.length)toast(message);
    const outbound=(response.writeback||[]);
    state.personalRemoteFailure=outbound.some(result=>['failed','unavailable'].includes(result.status));
    endPersonalSave(false,outbound.some(result=>result.status!=='updated'));
  }catch(e){
    if(seq===state.detailSeq&&state.detail?.id===id){Object.assign(state.detail,previous);if($('personal-status'))$('personal-status').textContent='Échec de l’enregistrement : '+e.message;}
    toast(e.message);
    endPersonalSave(true);
  }finally{state.personalPending.delete(id);if(seq===state.detailSeq&&state.detail?.id===id)syncPersonalControls();}
  if(saved){await loadMedia();if($('lists-dialog').open)await loadList();}
}
async function saveEpisodeWatches(watched,season=null,upToEpisode=null) {
  const item=state.detail;
  if(!item||item.kind!=='tvshow'||state.role!=='admin'||state.personalPending.has(item.id))return;
  const total=Number(item.episode_count)||0;
  if(season===null&&!localizedConfirm(`Attention : ${watched?'marquer comme vus':'marquer comme non vus'} les ${total} épisodes importés de « ${item.title} » (toutes les saisons et les spéciaux) ? Cette action modifie également les états envoyés aux sources configurées.`))return;
  if(upToEpisode!==null&&!localizedConfirm(`Marquer comme vus les épisodes de la saison ${season}, du début jusqu'à l'épisode ${upToEpisode} inclus ?`))return;
  const id=item.id,seq=state.detailSeq,selected=$('season-list')?.querySelector('.season-button[aria-expanded="true"]')?.dataset.season;
  state.personalPending.add(id);++state.personalSeq;syncPersonalControls();beginPersonalSave();
  document.querySelectorAll('[data-mark-season]').forEach(button=>button.disabled=true);
  try {
    const result=await api(`media/${encodeURIComponent(id)}/episodes/watch`,{method:'PATCH',body:JSON.stringify({watched,...(season===null?{}:{season}),...(upToEpisode===null?{}:{up_to_episode:upToEpisode})})});
    // The mutation succeeded. A subsequent catalogue refresh error must not
    // relabel an already persisted edit as a failed save.
    endPersonalSave(false,true);
    if(seq===state.detailSeq&&state.detail?.id===id){
      const updated=await api(`media/${encodeURIComponent(id)}`);
      if(seq===state.detailSeq&&state.detail?.id===id){
        renderDetail(updated);
        await loadSeasons(id,selected===undefined?null:Number(selected));
        if($('personal-status'))$('personal-status').textContent=`${result.updated} épisode(s) modifié(s) localement. État externe dans la barre supérieure.`;
      }
    }
    toast(`${result.updated} épisode(s) mis à jour sur ${result.total}.`);
    await loadMedia();
    if($('lists-dialog').open)await loadList();
  }catch(e){
    toast('Actualisation impossible : '+e.message);
    if(state.personalSaveCount>0)endPersonalSave(true);
  }finally{
    state.personalPending.delete(id);
    if(seq===state.detailSeq&&state.detail?.id===id){
      syncPersonalControls();
      document.querySelectorAll('[data-mark-season]').forEach(button=>button.disabled=false);
    }
  }
}
function metadataMarkup(item) {
  if(item.kind==='episode')return '';
  return `<span id="enrichment-status" class="enrichment-caption" role="status">${state.enrichingIds.has(item.id)?'Récupération des personnes, portraits et bandes-annonces…':item.enriched_at?`Métadonnées TMDB mises en cache le ${date(item.enriched_at)}.`:item.tmdb_available?'Les métadonnées TMDB sont récupérées à l’ouverture de cette fiche.':'TMDB n’est pas configuré. Les métadonnées affichées proviennent de Kodi ou CLZ.'}</span>`;
}
function detailDatesMarkup(item) {
  const dates=[];
  if(item.date_added)dates.push(`Ajouté le ${date(item.date_added)}`);
  if(item.last_played)dates.push(`Dernier visionnage le ${date(item.last_played)}`);
  const metadata=metadataMarkup(item);
  if(!dates.length&&!metadata)return '';
  return `<p class="hint detail-timestamps">${dates.map(esc).join(' · ')}${dates.length&&metadata?' · ':''}${metadata}</p>`;
}
// The browser cannot contact Kodi's private JSON-RPC directly. Probe each
// instance through the server's existing read-only remote snapshot endpoint.
// An idle but reachable instance is online (active:false is not offline).
const playbackKodiConnectivity=new Map();
let playbackKodiStatusTimer=null;
function playbackKodiOption(player) {
  const status=playbackKodiConnectivity.get(player.id);
  const icon=status===true?'🔵':status===false?'🔴':'⚪';
  const detail=status===true?'connectée':status===false?'hors ligne':'vérification…';
  return icon+' '+player.name+' · '+detail;
}
async function refreshPlaybackKodiConnectivity(players) {
  await Promise.allSettled(players.map(async player=>{
    let online=false;
    try {
      await api('players/'+encodeURIComponent(player.id)+'/remote',{cache:'no-store'});
      online=true;
    } catch { /* Reachability is independent of a currently playing media. */ }
    playbackKodiConnectivity.set(player.id,online);
    const select=$('play-target');
    if(!select || !$('play-dialog')?.open) return;
    const option=[...select.options].find(o=>o.value===player.id);
    if(option) {
      option.textContent=playbackKodiOption(player);
      option.title=online?'Kodi connecté':'Kodi hors ligne';
    }
  }));
}
function stopPlaybackKodiStatus() {
  if(playbackKodiStatusTimer!==null)clearInterval(playbackKodiStatusTimer);
  playbackKodiStatusTimer=null;
}
function startPlaybackKodiStatus(players) {
  stopPlaybackKodiStatus();
  void refreshPlaybackKodiConnectivity(players);
  // While the picker is open, reflect power-off/power-on transitions.
  playbackKodiStatusTimer=setInterval(()=>{
    if($('play-dialog')?.open && !document.hidden)
      void refreshPlaybackKodiConnectivity(players);
  },15000);
}
function playbackMarkup(item) {
  const players=state.players.filter(p=>item.editions.some(e=>e.source_type==='kodi'&&e.source_id===p.source_id));
  let controls;
  if(state.role!=='admin')controls='<p class="hint">Un accès administrateur est nécessaire pour lancer la lecture.</p>';
  else if(!players.length)controls='<p class="hint">Ajoutez une instance Kodi associée à la source de ce fichier.</p><button class="secondary" data-action="connections">Configurer une instance Kodi</button>';
  else controls=`<label>Instance Kodi<select id="play-target">${players.map(p=>`<option value="${esc(p.id)}">${esc(playbackKodiOption(p))}</option>`).join('')}</select></label><label>Fichier ou version<select id="play-edition" aria-label="Fichier ou version Kodi"></select></label><label class="play-options"><input id="play-resume" type="checkbox" checked>Reprendre au point de lecture</label><button id="play-button" class="primary">▶ Lire sur Kodi</button><button id="play-browser" class="secondary">▶ Lire dans le navigateur</button><p class="hint">Lecteur web : connexion réseau SMB/NFS/FTP/SFTP configurée privilégiée si son chemin correspond au fichier Kodi, sinon flux Kodi. Le serveur gère les pistes audio, sous-titres, chapitres et la conversion si nécessaire.</p><p id="play-status" class="hint play-status" role="status" aria-live="polite"></p>`;
  return `<p class="play-media-title">${esc(item.title)}</p><div class="playback-controls">${controls}</div><p class="hint">Sur Kodi : lecture par Kodi. Dans le navigateur : accès réseau configuré sur le serveur en priorité, puis Kodi en repli.</p>`;
}
// A series has no single playable file: select an episode before offering
// the same iPhone/browser or Kodi destinations as a film.
function playbackSeriesMarkup(item) {
  if(state.role!=='admin')return '<p class="hint">Un accès administrateur est nécessaire pour lancer la lecture.</p>';
  return `<p class="play-media-title">${esc(item.title)} · Choisissez un épisode pour accéder aux destinations de lecture.</p>
    <div class="playback-controls">
      <label>Saison<select id="play-season" aria-label="Saison à lire" disabled></select></label>
      <label>Épisode<select id="play-episode" aria-label="Épisode à lire" disabled></select></label>
      <button id="play-choose-episode" class="primary" type="button" disabled>Continuer vers les destinations</button>
      <p id="play-series-status" class="hint play-status" role="status" aria-live="polite">Chargement des saisons…</p>
    </div>`;
}
async function loadPlaybackSeriesSeasons(seriesID) {
  if(state.role!=='admin')return;
  try {
    const seasons=await api('media/'+encodeURIComponent(seriesID)+'/seasons');
    if(state.playbackSeriesId!==seriesID||!$('play-dialog').open||!$('play-season'))return;
    const picker=$('play-season');
    const previous=state.playbackSeriesSeason;
    picker.innerHTML=seasons.map(s=>`<option value="${s.season}">${s.season===0?'Spéciaux':'Saison '+s.season} · ${s.episode_count} épisodes</option>`).join('');
    const preferred=seasons.find(s=>String(s.season)===String(previous))
      ||seasons.find(s=>s.season!==0)||seasons[0];
    if(!preferred){
      $('play-series-status').textContent='Aucune saison disponible pour cette série.';
      return;
    }
    picker.value=String(preferred.season);
    picker.disabled=false;
    await loadPlaybackSeriesEpisodes(seriesID);
  }catch(error){
    if(state.playbackSeriesId===seriesID&&$('play-series-status'))
      $('play-series-status').textContent=error.message;
  }
}
async function loadPlaybackSeriesEpisodes(seriesID) {
  const picker=$('play-season'),episodes=$('play-episode'),status=$('play-series-status');
  if(!picker||!episodes||!status||state.playbackSeriesId!==seriesID)return;
  const season=Number(picker.value);
  state.playbackSeriesSeason=season;
  episodes.disabled=true;
  $('play-choose-episode').disabled=true;
  episodes.innerHTML='';
  status.textContent='Chargement des épisodes…';
  try {
    const choices=[];
    let offset=0;
    let more=true;
    // The server returns paginated episodes; never silently omit later pages.
    while(more&&offset<5000){
      const page=await api(`media/${encodeURIComponent(seriesID)}/seasons/${season}/episodes?limit=100&offset=${offset}`);
      if(state.playbackSeriesId!==seriesID||!$('play-dialog').open
        ||$('play-season')?.value!==String(season))return;
      choices.push(...page.items);
      if(!page.has_more||!page.items.length)more=false;
      else offset+=page.items.length;
    }
    if(more)throw new Error('Cette saison dépasse la limite de 5 000 épisodes.');
    episodes.innerHTML=choices.map(e=>`<option value="${esc(e.id)}">E${String(e.episode??0).padStart(2,'0')} · ${esc(e.title)}</option>`).join('');
    episodes.disabled=!choices.length;
    $('play-choose-episode').disabled=!choices.length;
    status.textContent=choices.length?`${choices.length} épisodes disponibles.`:'Aucun épisode dans cette saison.';
  }catch(error){
    if(state.playbackSeriesId===seriesID&&$('play-season')?.value===String(season))
      status.textContent=error.message;
  }
}
async function choosePlaybackSeriesEpisode() {
  const button=$('play-choose-episode');
  const id=$('play-episode')?.value;
  const seriesID=state.playbackSeriesId;
  if(!button||!id||!seriesID)return;
  button.disabled=true;
  try {
    const episode=await api('media/'+encodeURIComponent(id));
    if(state.playbackSeriesId!==seriesID||!$('play-dialog').open)return;
    if(episode.kind!=='episode'||!episode.editions?.some(e=>e.source_type==='kodi'))
      throw new Error('Cet épisode ne possède aucun fichier Kodi lisible.');
    state.playbackItem=episode;
    state.playbackMediaId=episode.id;
    $('play-content').innerHTML=`<button type="button" id="play-back-series" class="text-button">← Choisir un autre épisode</button>${playbackMarkup(episode)}`;
    updatePlayEdition();
    const players=state.players.filter(p=>episode.editions.some(e=>e.source_type==='kodi'&&e.source_id===p.source_id));
    if(players.length)startPlaybackKodiStatus(players);
  }catch(error){
    if($('play-series-status'))$('play-series-status').textContent=error.message;
  }finally {
    if(button.isConnected)button.disabled=false;
  }
}
function returnToPlaybackSeries() {
  const seriesID=state.playbackSeriesId;
  if(!seriesID||state.detail?.id!==seriesID)return;
  stopPlaybackKodiStatus();
  state.playbackItem=null;
  $('play-content').innerHTML=playbackSeriesMarkup(state.detail);
  void loadPlaybackSeriesSeasons(seriesID);
}
function savePlaybackDraft() {
  if(!$('play-target'))return;
  state.playbackDraft={mediaId:state.playbackMediaId,target:$('play-target').value,edition:$('play-edition').value,resume:$('play-resume').checked};
}
function openPlayback() {
  hideRatingPopover();
  const item=state.detail;if(!item)return;
  state.playbackSeriesId=item.kind==='tvshow'?item.id:null;
  state.playbackItem=item.kind==='tvshow'?null:item;
  state.playbackMediaId=item.id;
  if(item.kind==='tvshow'){
    $('play-content').innerHTML=playbackSeriesMarkup(item);
    showDialog('play-dialog');
    void loadPlaybackSeriesSeasons(item.id);
    return;
  }
  const draft=state.playbackDraft?.mediaId===item.id?state.playbackDraft:null;
  $('play-content').innerHTML=playbackMarkup(item);
  if(draft&&$('play-target')&&[...$('play-target').options].some(o=>o.value===draft.target))$('play-target').value=draft.target;
  updatePlayEdition();
  if(draft&&$('play-edition')){
    if([...$('play-edition').options].some(o=>o.value===draft.edition))$('play-edition').value=draft.edition;
    $('play-resume').checked=draft.resume;
  }
  showDialog('play-dialog');
  const players=state.players.filter(p=>item.editions.some(e=>e.source_type==='kodi'&&e.source_id===p.source_id));
  if(players.length)startPlaybackKodiStatus(players);
}
$('play-dialog').addEventListener('close',stopPlaybackKodiStatus);
function card(item) {
  const meta=[item.year,kindLabels[item.kind]].filter(Boolean);
  const roles=state.personSearch&&(item.matched_roles||[]).filter(role=>typeof role==='string'&&role.trim());
  const credit=roles?.length?`<span class="matched-person-role" title="${esc(roles.join(' · '))}">${esc(tr('Rôle'))} : ${esc(roles.join(' · '))}</span>`:'';
  const markup = `<button class="media-card" data-media="${esc(item.id)}" aria-label="${esc(item.title)}, ${esc(watchDisplayLabel(item))}${roles?.length?', rôle : '+esc(roles.join(' · ')):''}">${poster(item)}<span><span class="card-title">${esc(item.title)}</span>${credit}<span class="card-meta">${meta.map(v=>`<span>${esc(v)}</span>`).join('<span aria-hidden="true">·</span>')}</span><span class="format-badges compact">${formatBadges(item.formats,true)}</span><span class="card-meta">${badges(item)}</span></span>${state.view==='list'?`<span class="list-status">${esc(watchDisplayLabel(item))}<br>${item.rating?`★ ${Number(item.rating).toFixed(1)} · `:''}${date(item.date_added)}</span>`:''}</button>`;
  return `<div class="media-card-entry">${markup}<button type="button" class="media-actions-trigger" data-media-actions="${esc(item.id)}" aria-label="Actions pour ${esc(item.title)}" title="Actions pour ${esc(item.title)}" aria-haspopup="dialog" aria-controls="media-actions-popover" aria-expanded="false">⋯</button></div>`;
}

// Explicit catalogue actions are offered through an ellipsis. No listener
// overrides the browser's native context menu on film or series cards.
let mediaActionsTrigger=null;
function closeMediaActions(returnFocus=false) {
  const menu=$('media-actions-popover');
  if(menu.matches(':popover-open'))menu.hidePopover();
  if(returnFocus&&mediaActionsTrigger?.isConnected)mediaActionsTrigger.focus({preventScroll:true});
}
function mediaWatchActions(item) {
  if(item.kind==='tvshow'){
    const progress=seriesProgress(item);
    if(!progress)return '';
    return (progress.complete?'':'<button type="button" data-quick-action="watch" data-watched="true">✓ Marquer toute la série comme vue</button>')+
      (progress.complete||progress.partial?'<button type="button" data-quick-action="watch" data-watched="false">○ Marquer toute la série comme non vue</button>':'');
  }
  return `<button type="button" data-quick-action="watch" data-watched="${item.watch_status==='watched'?'false':'true'}">${item.watch_status==='watched'?'○ Marquer comme non vu':'✓ Marquer comme vu'}</button>`;
}
function positionMediaActions(trigger) {
  const menu=$('media-actions-popover');
  if(!menu.matches(':popover-open')||!trigger?.isConnected)return;
  const rect=trigger.getBoundingClientRect(),margin=8,gap=6;
  const left=Math.max(margin,Math.min(rect.right-menu.offsetWidth,innerWidth-menu.offsetWidth-margin));
  const below=rect.bottom+gap;
  const top=below+menu.offsetHeight<=innerHeight-margin?below:Math.max(margin,rect.top-menu.offsetHeight-gap);
  menu.style.left=left+'px';
  menu.style.top=top+'px';
}
async function openMediaActions(trigger) {
  const menu=$('media-actions-popover'),id=trigger.dataset.mediaActions;
  if(menu.matches(':popover-open')&&menu.dataset.mediaId===id){closeMediaActions(true);return;}
  const item=state.items.find(value=>value.id===id);
  if(!item)return;
  closeMediaActions();
  mediaActionsTrigger=trigger;
  menu.dataset.mediaId=id;
  const admin=state.role==='admin';
  const canPlay=admin&&(item.source_types||[]).includes('kodi')&&(item.kind!=='tvshow'||Number(item.episode_count)>0);
  menu.innerHTML=`<div class="media-actions-title" title="${esc(item.title)}">${esc(item.title)}</div>
    <button type="button" data-quick-action="details">▣ Ouvrir la fiche</button>
    ${admin?mediaWatchActions(item):''}
    ${canPlay?`<button type="button" data-quick-action="play">${actionIcon('playtarget')} Lire sur…</button>`:''}
    <button type="button" data-quick-action="trailer">▷ Bande-annonce YouTube</button>
    ${admin?'<details class="media-actions-lists"><summary>＋ Ajouter à une liste <span aria-hidden="true">›</span></summary><div class="media-actions-list-choices"><button type="button" data-quick-action="new-list">＋ Nouvelle liste</button><p class="hint">Chargement des listes…</p></div></details>':''}`;
  menu.showPopover();
  trigger.setAttribute('aria-expanded','true');
  positionMediaActions(trigger);
  menu.querySelector('button')?.focus({preventScroll:true});
  if(!admin)return;
  try{
    const lists=await api('lists');
    if(menu.dataset.mediaId!==id||!menu.matches(':popover-open'))return;
    menu.querySelector('.media-actions-list-choices').innerHTML=
      '<button type="button" data-quick-action="new-list">＋ Nouvelle liste</button>'+
      lists.map(list=>`<button type="button" class="media-action-list" data-quick-list="${esc(list.id)}">${listThumbnail(list)}<span>${esc(list.name)}</span></button>`).join('');
    attachImageFallbacks(menu);
    positionMediaActions(trigger);
  }catch(e){
    if(menu.dataset.mediaId===id&&menu.matches(':popover-open')){
      const p=menu.querySelector('.media-actions-list-choices .hint');
      if(p)p.textContent='Listes indisponibles : '+e.message;
    }
  }
}
async function quickWatch(id,watched) {
  if(state.role!=='admin'||state.quickPending.has(id)||state.personalPending.has(id))return;
  const item=state.items.find(value=>value.id===id);
  if(!item)return;
  if(item.kind==='tvshow'&&!localizedConfirm('Marquer '+(watched?'tous les épisodes comme vus':'tous les épisodes comme non vus')+' pour ` '+item.title+' » ? Cette action sera synchronisée avec les sources configurées.'))return;
  state.quickPending.add(id);
  beginPersonalSave();
  let saved=false,failed=true;
  try{
    if(item.kind==='tvshow'){
      await api(`media/${encodeURIComponent(id)}/episodes/watch`,{method:'PATCH',body:JSON.stringify({watched})});
      endPersonalSave(false,true);
    }else{
      const response=await api(`media/${encodeURIComponent(id)}/personal`,{method:'PATCH',body:JSON.stringify({watched})});
      const outbound=response.writeback||[];
      state.personalRemoteFailure=outbound.some(result=>['failed','unavailable'].includes(result.status));
      endPersonalSave(false,outbound.some(result=>result.status!=='updated'));
    }
    failed=false;saved=true;
    toast(watched?'Média marqué comme vu.':'Média marqué comme non vu.');
  }catch(e){toast(e.message);}
  finally{
    if(failed)endPersonalSave(true);
    state.quickPending.delete(id);
  }
  if(saved){
    await loadMedia();
    if($('lists-dialog').open)await loadList();
    if(state.detail?.id===id&&$('detail-dialog').open){
      try{renderDetail(await api(`media/${encodeURIComponent(id)}`));}catch(e){toast(e.message);}
    }
  }
}
async function quickAddToList(id,listId) {
  if(state.role!=='admin'||state.quickPending.has(id))return;
  state.quickPending.add(id);
  try{
    await api(`lists/${encodeURIComponent(listId)}/items`,{method:'POST',body:JSON.stringify({media_id:id})});
    toast('Média ajouté à la liste.');
    await refreshHomeListThumbnails();
    if($('lists-dialog').open)await loadList();
  }catch(e){toast(e.message);}
  finally{state.quickPending.delete(id);}
}
async function quickCreateList(id) {
  if(state.role!=='admin'||state.quickPending.has(id))return;
  const name=localizedPrompt('Nom de la nouvelle liste');
  if(!name?.trim())return;
  state.quickPending.add(id);
  try{
    const list=await api('lists',{method:'POST',body:JSON.stringify({name:name.trim()})});
    await api(`lists/${encodeURIComponent(list.id)}/items`,{method:'POST',body:JSON.stringify({media_id:id})});
    toast('Liste créée et média ajouté.');
    await refreshHomeListThumbnails();
    if($('lists-dialog').open)await openLists();
  }catch(e){toast(e.message);}
  finally{state.quickPending.delete(id);}
}
async function quickTrailer(id) {
  try{
    const item=await api(`media/${encodeURIComponent(id)}`);
    const links=[...new Set(item.trailers||[])];
    if(!links.length)links.push('https://www.youtube.com/results?search_query='+encodeURIComponent(item.title+' '+(item.year||'')+' bande annonce'));
    $('trailers-content').innerHTML=`<p class="play-media-title">${esc(item.title)}</p><div class="trailer-choices">${links.map((link,index)=>`<a class="external-button" data-trailer href="${esc(link)}" target="_blank" rel="noopener noreferrer">${actionIcon('youtube')}<span>Bande-annonce ${index+1}<small>YouTube</small></span><span aria-hidden="true">↗</span></a>`).join('')}</div>`;
    showDialog('trailers-dialog');
  }catch(e){toast(e.message);}
}
$('media-actions-popover').addEventListener('toggle',event=>{
  if(event.newState==='closed'){
    if(mediaActionsTrigger?.isConnected)mediaActionsTrigger.setAttribute('aria-expanded','false');
    mediaActionsTrigger=null;
  }
});
document.addEventListener('click',async event=>{
  const trigger=event.target.closest('[data-media-actions]');
  if(trigger){await openMediaActions(trigger);return;}
  const entry=event.target.closest('#media-actions-popover [data-quick-action], #media-actions-popover [data-quick-list]');
  if(!entry)return;
  event.stopPropagation();
  const id=$('media-actions-popover').dataset.mediaId;
  if(!id)return;
  const action=entry.dataset.quickAction, listId=entry.dataset.quickList;
  closeMediaActions();
  if(listId){await quickAddToList(id,listId);return;}
  if(action==='details'){await openDetail(id);return;}
  if(action==='watch'){await quickWatch(id,entry.dataset.watched==='true');return;}
  if(action==='new-list'){await quickCreateList(id);return;}
  if(action==='trailer'){await quickTrailer(id);return;}
  if(action==='play'){
    await openDetail(id);
    if(state.detail?.id===id)openPlayback();
  }
});
window.addEventListener('resize',()=>positionMediaActions(mediaActionsTrigger));
document.addEventListener('scroll',event=>{
  const menu=$('media-actions-popover');
  if(menu.matches(':popover-open')&&event.target!==menu&&!menu.contains(event.target))closeMediaActions();
},{passive:true,capture:true});

function attachImageFallbacks(root) { root.querySelectorAll('img').forEach(img=>{img.addEventListener('error',()=>img.classList.add('failed'),{once:true});}); }
function currentParams(offset=0) {
  const params=new URLSearchParams({limit:state.limit,offset,sort:$('sort').value,order:state.order});
  for (const [id,name] of [['search','q'],['kind','kind'],['source-type','source_type'],['genre','genre'],['collection','collection'],['watch-status','watch_status']]) if ($(id).value) params.set(name,$(id).value);
  for (const value of state.formats) params.append('format',value);
  if(state.personSearch) {
    params.delete('q');
    if(state.personSearch.tmdbId)params.set('person_tmdb_id',state.personSearch.tmdbId);
    else if(state.personSearch.imdbId)params.set('person_imdb_id',state.personSearch.imdbId);
    params.set('person_name',state.personSearch.name);
    if(!state.personSearch.tmdbId&&!state.personSearch.imdbId){
      // Old servers ignore person_name without an identity; retain the
      // original cast-name FTS fallback until the server is upgraded.
      params.set('q',state.personSearch.name);
      params.set('deep_search','true');
    }
  }
  if(state.deepSearch) params.set('deep_search','true');
  if (state.recent) params.set('recent_days','30');
  return params;
}
function updateFilterState() {
  let count=['search','kind','source-type','genre','collection','watch-status'].filter(id=>$(id).value).length + state.formats.length + Number(state.recent) + Number(state.deepSearch);
  $('filter-count').textContent=count || '';
  $('reset-filters').hidden=!count;
  document.querySelectorAll('[data-quick]').forEach(b=>b.classList.toggle('active', b.dataset.quick==='recent'?state.recent:b.dataset.quick==='all'?!state.recent&&!$('watch-status').value:!state.recent&&b.dataset.quick===$('watch-status').value));
}
function observeMore() {
  moreObserver?.disconnect();
  if(!state.loading&&!state.loadError&&state.hasMore&&!$('app').hidden&&!document.querySelector('dialog[open]')) moreObserver?.observe($('load-more-trigger'));
}
function updateLoadingState() {
  $('pagination').hidden=!state.items.length&&!state.loadError;
  $('page-info').textContent=`${state.items.length.toLocaleString(displayLocale())} / ${state.total.toLocaleString(displayLocale())} titres affichés`;
  $('load-more').hidden=!state.hasMore&&!state.loadError;
  $('load-more').disabled=state.loading;
  $('load-more').textContent=state.loadError?'Réessayer':'Charger la suite';
  $('load-more-status').textContent=state.loading?'Chargement de la suite…':state.loadError?'Le chargement a échoué. Vous pouvez réessayer.':!state.hasMore&&state.items.length?'Tous les résultats sont affichés.':'';
}
function renderCards(items, append=false) {
  const root=$('results'); root.className=state.view==='grid'?'poster-grid':'list-results';
  const template=document.createElement('template'); template.innerHTML=items.map(card).join('');
  attachImageFallbacks(template.content);
  if(!append) root.replaceChildren();
  root.append(template.content);
}
async function loadMedia({append=false}={}) {
  if(append&&(state.loading||!state.hasMore)) return;
  if(!append)savePreferences();
  if(!append) {
    ++state.seq; state.controller?.abort(); state.items=[]; state.ids=new Set(); state.nextOffset=0; state.total=0; state.hasMore=false;
    $('results').replaceChildren();
  }
  const seq=state.seq; const offset=append?state.nextOffset:0;
  state.controller=new AbortController(); state.loading=true; state.loadError=false;
  moreObserver?.disconnect();
  $('results').dataset.appending=String(append);
  $('results').setAttribute('aria-busy','true'); $('error-banner').hidden=true;
  updateLoadingState();
  updateFilterState();
  try {
    const page=await api(`media?${currentParams(offset)}`,{signal:state.controller.signal});
    if (seq!==state.seq) return;
    const added=page.items.filter(item=>!state.ids.has(item.id));
    for(const item of added) state.ids.add(item.id);
    state.items.push(...added); state.total=page.total; state.nextOffset=offset+page.items.length;
    state.hasMore=page.has_more&&page.items.length>0;
    $('result-count').textContent=`${page.total.toLocaleString(displayLocale())} résultat${page.total>1?'s':''}${state.personSearch?.tmdbId?' · TMDB #'+state.personSearch.tmdbId+' (crédits non identifiés inclus)':state.personSearch?.imdbId?' · IMDb '+state.personSearch.imdbId+' (crédits non identifiés inclus)':''}${state.recent?' · ajoutés depuis 30 jours':''}`;
    if (!append&&!page.items.length) {
      const empty=!state.facets?.sources.length || state.facets.sources.every(s=>!s.item_count);
      $('results').innerHTML=`<div class="empty-state"><span class="empty-icon" aria-hidden="true">▦</span><h2>${empty?'Votre bibliothèque commence ici.':'Aucun titre ne correspond.'}</h2><p>${empty?'Connectez la base Kodi ou importez un export CLZ Movies.':'Modifiez les filtres ou essayez un autre mot.'}</p>${state.role==='admin'&&empty?'<button class="primary" data-action="connections">Ajouter une source →</button>':'<button class="secondary" data-action="reset">Effacer les filtres</button>'}</div>`;
    } else renderCards(added,append);
  } catch (e) { if (e.name!=='AbortError'&&seq===state.seq) {state.loadError=true;error(e.message);} }
  finally { if (seq===state.seq) {state.loading=false;$('results').setAttribute('aria-busy','false');updateLoadingState();observeMore();} }
}
function fillSelect(id, values, label) {
  const selected=$(id).value;
  $(id).innerHTML=`<option value="">${label}</option>`+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
  if (values.includes(selected)) $(id).value=selected;
}
async function refresh() {
  const [facets,status,players]=await Promise.all([api('facets'),api('status'),api('players')]);
  state.facets=facets; state.players=players;
  $('scan-all-kodi').hidden=state.role!=='admin'||!players.length;
  state.tmdbAvailable=status.tmdb_available===true;
  $('server-version').textContent='Serveur '+(status.version||'—')+(status.data_model_version?' · Modèle '+status.data_model_version:'');
  fillSelect('genre',facets.genres,'Tous les genres'); fillSelect('collection',facets.collections,'Toutes les collections');
  if(filtersRestored){
    const available=new Set(facets.formats.map(canonicalFormat));
    state.formats=state.formats.filter(v=>available.has(v));
    updateFormatSelections();
  }
  restoreFilters();
  $('catalogue-status').textContent=`${status.works.toLocaleString(displayLocale())} titres · ${status.editions.toLocaleString(displayLocale())} fichiers et éditions · ${status.sources.length} source${status.sources.length>1?'s':''}`;
  const latest=status.sources.map(s=>s.last_sync).filter(Boolean).sort().at(-1);
  $('sync-status').textContent=latest?`Dernière mise à jour : ${date(latest)}`:'Aucune source importée';
  await loadMedia();
}
// Keep the activity indicator in the main header, including automated jobs
// started by the server scheduler and jobs launched by another client.
const jobPhaseLabels={
  connecting:'Connexion à MariaDB',loading:'Lecture des métadonnées Kodi',parsing_clz:'Analyse du fichier CLZ',parsing_mymovies:'Analyse du fichier MyMovies',parsing_filmotech:'Analyse du fichier Filmotech',
  movies:'Films',series:'Séries',episodes:'Épisodes',
  verifying_library:'Contrôle de la vidéothèque Kodi',
  publishing:'Mise à jour du catalogue',completed:'Terminé',failed:'Échec',
  reading_collection:'Préparation de l’import de collection',
  kodi_scanning:'Analyse de la médiathèque distante Kodi (JSON-RPC)',
  kodi_scan_finished:'Kodi analysé · chargement de MariaDB',
  kodi_sql_fallback:'Kodi injoignable · synchronisation SQL uniquement'
};
function indeterminateKodiPhase(job) {
  return ['kodi_scanning','kodi_scan_finished','kodi_sql_fallback'].includes(job.progress_phase);
}
function renderGlobalScanProgress() {
  const root=$('remote-scan-progress');
  const ids=new Set(state.globalScanIds||[]);
  const jobs=state.jobs.filter(job=>ids.has(job.id));
  root.hidden=!jobs.length;
  root.closest('.remote-scan-dashboard').hidden=!jobs.length;
  if(!jobs.length)return;
  const sources=new Map((state.facets?.sources||[]).map(source=>[source.id,source]));
  root.innerHTML=jobs.map(job=>{
    const name=sources.get(job.source_id)?.name||'Kodi';
    const done=job.status==='completed',failed=job.status==='failed';
    const result=job.result||{};
    const summary=failed?'Échec':done
      ?result.kodi_scan==='sql_only'?'SQL actualisé, analyse Kodi non effectuée':'Actualisation terminée'
      :jobProgressLabel(job);
    const warning=(result.kodi_scan_warnings||[]).join(' · ');
    return `<div class="remote-scan-job ${failed?'is-failed':done?'is-completed':''}">
      <strong>${esc(name)} · ${esc(jobOperationLabel(job))}</strong>
      <div><span class="remote-scan-status">${esc(summary)}</span>
        ${!done&&!failed?jobProgressMarkup(job):''}
        ${failed&&job.error?`<span class="remote-scan-warning">${esc(job.error)}</span>`:''}
        ${warning?`<p class="remote-scan-warning">${esc(warning)}</p>`:''}
      </div>
    </div>`;
  }).join('');
}
function jobOperationLabel(job) {
  const operation=job.operation || (job.result?.kodi_scan ? 'kodi_scan'
    :['kodi_scanning','kodi_scan_finished','kodi_sql_fallback'].includes(job.progress_phase)?'kodi_scan'
    :(state.facets?.sources||[]).find(s=>s.id===job.source_id)?.type==='kodi'?'kodi_sync':'collection_import');
  return ({kodi_scan:'Analyse Kodi puis synchronisation MariaDB',
    kodi_sync:'Synchronisation MariaDB vers le catalogue local',
    collection_import:'Import de collection'})[operation]||'Tâche de catalogue';
}
function jobProgressLabel(job) {
  const phase=job.status==='pending'?'En attente de démarrage':
    jobPhaseLabels[job.progress_phase]||'Initialisation du traitement';
  const current=Math.max(0,Number(job.progress_current)||0);
  const total=Math.max(0,Number(job.progress_total)||0);
  const overall=Number(job.overall_percent);
  const overallText=Number.isFinite(overall)&&!indeterminateKodiPhase(job)&&!job.progress_phase?.startsWith('parsing_')?' · ≈ '+overall+' % global':'';
  return jobOperationLabel(job)+' · '+phase+(total>0?' · '+Math.min(current,total).toLocaleString(displayLocale())+'/'+total.toLocaleString(displayLocale()):'…')+overallText;
}
function jobProgressMarkup(job) {
  const percent=Number(job.overall_percent);
  const showProgress=Number.isFinite(percent)&&!indeterminateKodiPhase(job)&&!job.progress_phase?.startsWith('parsing_');
  return '<div class="sync-phase"><span class="activity-spinner" aria-hidden="true"></span><span>'+esc(jobProgressLabel(job))+'</span></div>'+
    (showProgress?'<progress class="sync-progress" value="'+Math.max(0,Math.min(percent,100))+'" max="100"></progress>':'');
}
function renderSourceJobProgress() {
  const displayed=new Set();
  document.querySelectorAll('button[data-sync],button[data-scan-kodi]').forEach(button=>{
    const id=button.dataset.sync||button.dataset.scanKodi;
    const job=state.jobs.find(item=>item.source_id===id&&['pending','running'].includes(item.status));
    button.disabled=Boolean(job);
    if(displayed.has(id))return;
    displayed.add(id);
    const card=button.closest('.kodi-source-card');
    if(!card)return;
    let target=card.querySelector('.source-progress');
    if(!target) {
      target=document.createElement('div');
      target.className='source-progress';
      target.setAttribute('role','status');
      card.firstElementChild.append(target);
    }
    target.hidden=!job;
    if(job)target.innerHTML=jobProgressMarkup(job);
  });
}
function renderActivity() {
  renderSourceJobProgress();
  const active=state.jobs.filter(j=>['pending','running'].includes(j.status));
  const indicator=$('activity-indicator');
  indicator.hidden=!active.length || $('app').hidden;
  renderGlobalScanProgress();
  if(!active.length)return;
  const sources=new Map((state.facets?.sources||[]).map(s=>[s.id,s]));
  const operations=[...new Set(active.map(job=>jobOperationLabel(job)))];
  const label=operations[0]||'Tâche de catalogue en cours';
  const first=active[0];
  const overall=Number(first.overall_percent);
  const showProgress=Number.isFinite(overall)&&!indeterminateKodiPhase(first)&&!first.progress_phase?.startsWith('parsing_');
  const percent=showProgress?' · ≈'+overall+' %':'';
  $('activity-label').textContent=active.length>1?`${active.length} tâches : ${operations.join(' · ')}`:label+percent;
  $('activity-progress').hidden=!showProgress||active.length!==1;
  if(showProgress&&active.length===1){
    $('activity-progress').max=100;
    $('activity-progress').value=Math.max(0,Math.min(overall,100));
  }
  indicator.title=active.map(j=>`${sources.get(j.source_id)?.name||'Source'} : ${j.status}`).join(' · ');
}
async function pollActivity() {
  if(state.activityPollBusy||$('app').hidden)return;
  state.activityPollBusy=true;
  try {
    const recent=await api('jobs');
    const oldActive=state.jobs.filter(j=>['pending','running'].includes(j.status));
    const finished=oldActive.filter(j=>!recent.some(current=>current.id===j.id&&['pending','running'].includes(current.status)));
    state.jobs=recent;
    renderActivity();
    if(finished.length) {
      const completed=finished.filter(j=>recent.find(r=>r.id===j.id)?.status==='completed');
      const failed=finished.filter(j=>recent.find(r=>r.id===j.id)?.status==='failed');
      if(completed.length) {
        await refresh();
        if($('connections-dialog').open) await connections();
        toast(`${completed.length} synchronisation(s) / import(s) terminé(s). Catalogue actualisé.`);
      }
      if(failed.length) toast('Une synchronisation ou un import a échoué. Consultez les paramètres.');
    }
  } catch(e) {
    // Transient network issues must not leave a stale running badge forever.
    if(e?.status===401)showLogin();
    else console.warn('Échec de lecture des tâches :',e);
  } finally { state.activityPollBusy=false; }
}
$('activity-indicator').addEventListener('click',()=>connections());
$('scan-all-kodi').addEventListener('click',async()=>{
  const button=$('scan-all-kodi');
  button.disabled=true;
  try {
    const jobs=await api('sources/scan-all',{method:'POST'});
    state.globalScanIds=jobs.map(job=>job.id);
    state.jobs=[...jobs,...state.jobs.filter(job=>!state.globalScanIds.includes(job.id))];
    renderActivity();
    toast(`Scan Kodi demandé pour ${jobs.length} source(s). Synchronisation MariaDB ensuite.`);
  } catch(e){toast('Actualisation Kodi impossible : '+e.message);error('Actualisation Kodi impossible : '+e.message);}
  finally {button.disabled=false;}
});

setInterval(()=>{if(!$('app').hidden){pollActivity();pollPersonalWritebacks();}},3000);
async function refreshStaleOnOpen() {
  try {
    const jobs=await api('sources/sync-stale',{method:'POST'});
    if(!jobs.length)return; // Already recent, or another client started it.
    const ids=new Set(jobs.map(job=>job.id));
    state.jobs=[...jobs,...state.jobs.filter(job=>!ids.has(job.id))];
    renderActivity();
  } catch(e) {
    // A failed automatic check must not prevent browsing or sign-out.
    console.warn('Automatic 24-hour catalogue refresh unavailable:',e.message);
  }
}
async function initialize() {
  try {
    const session=await api('auth/session'); state.csrf=session.csrf_token; state.role=session.role;
    stopOfflineTimer(); $('server-unavailable').hidden=true;
    $('login').hidden=true; $('app').hidden=false; $('connections-button').hidden=state.role!=='admin'; $('logout-button').hidden=session.access_method!=='session';
    $('create-list-button').hidden=state.role!=='admin';
    setView(state.view,false); renderPersonalStatus(); await refresh(); await refreshHomeListThumbnails(); await pollActivity(); void refreshNetworkHome();
    // Only once on authenticated entry; the server enforces the 24h threshold.
    void refreshStaleOnOpen();
    void startKodiRemote({api,url,role:state.role});
  } catch (e) {
    if(e.serverUnavailable)showServerUnavailable(e);
    else if(e.status===401||$('app').hidden)showLogin();
    else error(e.message);
  }
}
// A cast credit searches the local catalogue (including indexed TMDB credits),
// not TMDB's remote person page. Clear unrelated filters before searching.
function searchLocalPerson(name,tmdbId='',imdbId='') {
  const person=String(name||'').trim();
  if(!person)return;
  state.personSearch={
    name:person,
    tmdbId:/^[0-9]+$/.test(tmdbId)?tmdbId:null,
    imdbId:/^nm[0-9]+$/.test(imdbId)?imdbId:null
  };
  if($('detail-dialog').open)$('detail-dialog').close();
  for(const id of ['kind','source-type','genre','collection','watch-status']) $(id).value='';
  state.formats=[];
  state.recent=false;
  state.deepSearch=true;
  $('deep-search').checked=true;
  $('search').value=person;
  updateSearchClearButton();
  $('sort').value='title';
  state.order='asc';
  $('sort-order').textContent='↑ Croissant';
  updateFormatSelections();
  loadMedia();
  $('search').focus({preventScroll:true});
  $('search').scrollIntoView({block:'center',behavior:'smooth'});
}
function reset() { state.personSearch=null; ['search','kind','source-type','genre','collection','watch-status'].forEach(id=>$(id).value=''); state.formats=[];state.deepSearch=false;$('deep-search').checked=false;updateFormatSelections(); state.recent=false; updateSearchClearButton(); loadMedia(); }
function setView(view, render=true) { state.view=view; writeStorage('kodi_library_view',view);savePreferences(); $('grid-view').setAttribute('aria-pressed',view==='grid'); $('list-view').setAttribute('aria-pressed',view==='list'); if (render&&state.items.length) {renderCards(state.items);observeMore();} }
let pagePosition=null;
function lockPage() {
  if(pagePosition)return;
  pagePosition={x:window.scrollX,y:window.scrollY};
  document.body.style.top=`-${pagePosition.y}px`;
  document.body.style.left=`-${pagePosition.x}px`;
  document.documentElement.classList.add('modal-open');
  moreObserver?.disconnect();
}
function releasePage() {
  if(document.querySelector('dialog[open]'))return;
  if(pagePosition){
    const position=pagePosition;pagePosition=null;
    document.documentElement.classList.remove('modal-open');
    document.body.style.top='';document.body.style.left='';
    window.scrollTo(position.x,position.y);
  }
  observeMore();
}
function showDialog(id) { if (!$(id).open) {lockPage();$(id).showModal();if(id==='connections-dialog')$(id).scrollTop=0;} }
function editionMarkup(e) {
  const m=e.metadata;
  const fields=[['Format',m.formats?.join(', ')],['Vidéo',m.video_streams?.map(v=>[v.width&&v.height?`${v.width} × ${v.height}`:'',v.codec,v.hdr_type,v.hdr_detail,v.stereo_mode].filter(Boolean).join(' · ')).join(' / ')],['Édition',m.edition||m.version],['Code-barres',m.barcode],['Disques',m.disc_count],['Quantité',m.quantity],['Coffret',m.boxset],['Collection',m.collections?.join(', ')],['Régions',m.regions?.join(', ')],['Audio',m.audio_tracks?.join(', ')],['Sous-titres',m.subtitles?.join(', ')],['Dernier visionnage',e.last_played?date(e.last_played):null],['Historique CLZ',m.seen_dates?.map(date).join(', ')],['Lectures',e.play_count],['Reprise',e.resume_seconds?`${Math.floor(e.resume_seconds/60)} min`:null],['Fichier',m.file],['Ajout',e.date_added?date(e.date_added):null],['Identifiants',Object.entries(e.identifiers).map(([k,v])=>`${k}: ${v}`).join(' · ')]];
  const label=collectionTypes.includes(e.source_type)?(m.formats?.join(' / ')||'Édition physique'):(m.version||'Fichier Kodi');
  return `<details class="edition"><summary><b>${esc(label)}</b> <span class="muted">· ${sourceIcon(e.source_type)} ${esc(e.source_name)}</span></summary><div class="format-badges edition-formats">${formatBadges(m.formats||[])}</div><dl class="edition-grid">${fields.filter(([,v])=>v!==null&&v!==undefined&&v!=='').map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></details>`;
}
function tmdbAttribution(item) {
  if(!item.enriched_at)return '';
  return `<section class="detail-section credits-attribution"><p class="section-label">CRÉDITS DES MÉTADONNÉES</p><a href="https://www.themoviedb.org/" target="_blank" rel="noopener noreferrer"><img width="80" src="${esc(url('static/tmdb-logo.svg'))}" alt="TMDB"></a><p class="hint">This product uses the TMDB API but is not endorsed or certified by TMDB.</p></section>`;
}
function updatePlayEdition() {
  const target=$('play-target'); const selector=$('play-edition');if(!target||!selector)return;
  const previous=selector.value;
  const player=state.players.find(p=>p.id===target.value);if(!player)return;
  const item=state.playbackItem||state.detail;
  const editions=item.editions.filter(e=>e.source_type==='kodi'&&e.source_id===player.source_id).sort((a,b)=>Number(!a.metadata.is_default)-Number(!b.metadata.is_default));
  selector.innerHTML=editions.map(e=>`<option value="${esc(e.id)}">${esc(e.metadata.version||e.metadata.file?.split('/').pop()||'Fichier Kodi')}${e.metadata.is_default?' · Par défaut':''}</option>`).join('');
  if([...selector.options].some(option=>option.value===previous))selector.value=previous;
}
function resumeProgress(item) {
  if(!(item.total_seconds>0&&item.resume_seconds>0)) return '';
  const percent=Math.min(100,Math.max(0,100*item.resume_seconds/item.total_seconds));
  return `<div class="resume-progress" aria-label="Position de lecture : ${esc(playbackTime(item.resume_seconds))}">
    <time class="resume-time">${esc(playbackTime(item.resume_seconds))}</time>
    <progress value="${percent}" max="100" aria-label="Progression de lecture"></progress>
  </div>`;
}
// A media currently exposes a poster and, optionally, fanart. Keep unique available images.
function mediaArtworkImages(item) {
  const images=[{path:item.artwork?.poster,label:tr('Affiche')},{path:item.artwork?.fanart,label:tr('Fond d’écran')}];
  return images.filter((image,index)=>typeof image.path==='string'&&image.path.trim()&&
    images.findIndex(candidate=>candidate.path===image.path)===index);
}
let artworkGalleryImages=[],artworkGalleryIndex=0,artworkGalleryTitle='';
function renderArtworkGallery() {
  if(!artworkGalleryImages.length)return;
  const asset=artworkGalleryImages[artworkGalleryIndex];
  const image=$('artwork-gallery-image');
  const fullURL=url(asset.path);
  image.hidden=false;
  image.onerror=()=>{if(image.src===fullURL){image.hidden=true;$('artwork-gallery-caption').textContent=tr('Image indisponible.');}};
  image.alt=`${asset.label} · ${artworkGalleryTitle}`;
  image.src=fullURL;
  $('artwork-gallery-caption').textContent=asset.label;
  $('artwork-gallery-title').textContent=artworkGalleryTitle;
  $('artwork-gallery-counter').textContent=`${artworkGalleryIndex+1} / ${artworkGalleryImages.length}`;
  const multiple=artworkGalleryImages.length>1;
  $('artwork-gallery-prev').hidden=!multiple;
  $('artwork-gallery-next').hidden=!multiple;
  $('artwork-gallery-counter').hidden=!multiple;
}
function openArtworkGallery() {
  const item=state.detail;if(!item)return;
  artworkGalleryImages=mediaArtworkImages(item);
  if(!artworkGalleryImages.length)return;
  artworkGalleryIndex=0;
  artworkGalleryTitle=item.title;
  renderArtworkGallery();
  showDialog('artwork-gallery-dialog');
}
function stepArtworkGallery(offset) {
  if(!artworkGalleryImages.length||!$('artwork-gallery-dialog').open)return;
  artworkGalleryIndex=(artworkGalleryIndex+offset+artworkGalleryImages.length)%artworkGalleryImages.length;
  renderArtworkGallery();
}
async function copyMediaTitle() {
  const title=state.detail?.title;
  if(!title)return;
  if(navigator.clipboard?.writeText&&window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(title);
      toast('Titre copié.');
      return;
    }catch { /* Older browsers may deny clipboard access. */ }
  }
  // Keep the fallback inside the open modal; elements outside it are inert.
  const field=document.createElement('textarea');
  field.value=title;
  field.setAttribute('readonly','');
  field.style.cssText='position:fixed;opacity:0;pointer-events:none';
  $('detail-dialog').appendChild(field);
  field.select();
  let copied=false;
  try {copied=document.execCommand('copy');}catch { /* Show manual fallback below. */ }
  field.remove();
  if(copied)toast('Titre copié.');
  else localizedPrompt('Copiez le titre',title);
}
function renderDetail(item) {
  closeEpisodeContextMenu();
  const previousScroll=state.detail?.id===item.id?$('detail-dialog').scrollTop:0;
  hideRatingPopover();
  state.detail=item;
  $('detail-content').innerHTML=`${metadataAction(item)}<div class="detail-layout"><div class="detail-poster"><button type="button" id="open-artwork-gallery" class="detail-poster-trigger" title="${esc(tr('Agrandir les images du média'))}" aria-label="${esc(tr('Agrandir les images du média'))}" ${mediaArtworkImages(item).length?'':'disabled'}>${poster(item,true)}</button></div><div class="detail-intro"><p class="eyebrow">${esc(kindLabels[item.kind])}</p><div class="detail-heading"><h2><button type="button" id="copy-media-title" class="detail-copy-title" title="${esc(tr('Copier le titre'))}" aria-label="${esc(tr('Copier le titre'))}">${esc(item.title)}</button></h2>${item.original_title&&item.original_title!==item.title?`<p class="original-title">${esc(item.original_title)}</p>`:''}</div><div class="detail-info">${item.year?`<span>${item.year}</span>`:''}${item.runtime_minutes?`<span>${item.runtime_minutes} min</span>`:''}${item.rating?`<span class="score" title="Note publique : ${Number(item.rating).toFixed(1)}/10">★ ${Number(item.rating).toFixed(1)}</span>`:''}</div>${detailActions(item)}<div class="format-badges" aria-label="Formats disponibles selon les éditions">${detailFormatBadges(item)}</div> ${resumeProgress(item)}<div class="detail-meta-row">${watchMarkup(item)}${seriesProgressMarkup(item)}<div class="card-meta">${badges(item)}</div></div>${!item.source_types.includes('kodi')?'<p class="hint">Cette fiche provient uniquement de collections importées. Aucun fichier Kodi ne lui est actuellement associé dans le catalogue.</p>':''}<button id="add-to-list" class="secondary" ${state.role==='admin'?'':'disabled title="Un accès administrateur est nécessaire pour modifier les listes."'}>+ Ajouter à une liste</button>${item.lists?.length?`<p class="hint">Listes : ${item.lists.map(l=>`<button class="text-button" data-open-list="${esc(l.id)}">${esc(l.name)} ↗</button>`).join(' · ')}</p>`:''}</div><div class="detail-body">${item.kind==='episode'&&item.parent_id?`<button class="text-button" type="button" data-return-to-series="${esc(item.parent_id)}" data-return-season="${item.season??0}">← Série · Saison ${item.season??0} · Épisode ${item.episode??0}</button>`:''}<div class="genre-chips">${item.genres.map(g=>`<button data-genre="${esc(g)}">${esc(g)}</button>`).join('')}</div><p class="synopsis">${esc(item.plot||'Synopsis non renseigné.')}</p>${detailDatesMarkup(item)}${item.kind==='tvshow'?'<section class="detail-section"><p class="section-label">SAISONS</p><div id="season-list" class="season-links">Chargement…</div><div id="episode-list" class="episode-links"></div></section>':''}${castMarkup(item.people||item.cast)}<section class="detail-section"><p class="section-label">FICHIERS & ÉDITIONS · ${item.editions.length}</p>${item.editions.map(editionMarkup).join('')}${state.role==='admin'&&item.kind==='movie'?'<button id="diagnose-kodi" class="secondary" type="button">Vérifier la présence dans la vidéothèque</button><div id="kodi-diagnostic" class="metadata-status" role="status" aria-live="polite" hidden></div>':''}</section>${tmdbAttribution(item)}</div></div>`;
  attachImageFallbacks($('detail-content'));syncPersonalControls();$('detail-dialog').scrollTop=previousScroll;
}
async function openDetail(id) {
  const seq=++state.detailSeq;
  try {
    const item=await api(`media/${encodeURIComponent(id)}`); if(seq!==state.detailSeq)return;
    renderDetail(item); showDialog('detail-dialog');
    if(item.tmdb_available&&state.role==='admin'&&item.kind!=='episode'&&(!item.enriched_at||Date.now()-Date.parse(item.enriched_at)>30*86400000||(item.kind==='tvshow'&&!item.season_posters_cached))) enrichDetail(false);
    if (item.kind==='tvshow') await loadSeasons(item.id);

  } catch(e) {toast(e.message);}
}
async function loadSeasons(id,selectedSeason=null) {
  const seasons=await api(`media/${encodeURIComponent(id)}/seasons`);
  if(state.detail?.id!==id||!$('season-list'))return;
  state.seasons=seasons;
  const seriesPoster=state.detail.artwork?.poster;
  $('season-list').innerHTML=seasons.length?seasons.map(season=>{
    const label=season.season===0?'Spéciaux':`Saison ${season.season}`;
    const artwork=season.poster||seriesPoster;
    return `<div class="season-entry"><button type="button" class="season-button" data-season="${season.season}" aria-expanded="${selectedSeason===season.season}" aria-label="${esc(label)}, ${season.episode_count} épisodes, ${season.watched_count} vus${season.watched_count>0&&season.watched_count<season.episode_count?', partiellement vu':''}">
      <span class="season-cover">${artwork?`<img loading="lazy" src="${esc(url(artwork))}" alt="">`:'<span aria-hidden="true">▦</span>'}${season.watched_count>0?`<span class="season-watched-flag ${season.watched_count<season.episode_count?'partial':'complete'}" title="${season.watched_count<season.episode_count?'Partiellement vu':'Vu'} : ${season.watched_count}/${season.episode_count} épisodes" aria-label="${season.watched_count<season.episode_count?'Partiellement vu':'Vu'} : ${season.watched_count}/${season.episode_count} épisodes">${season.watched_count<season.episode_count?'◐':'✓'}</span>`:''}</span>
      <span class="season-label">${esc(label)}</span>
      <span class="season-count">${season.episode_count} épisodes · ${season.watched_count} vus</span>
    </button></div>`;
  }).join(''):'Aucune saison importée pour cette série.';
  attachImageFallbacks($('season-list'));
  if(selectedSeason!==null)loadSeasonEpisodes(selectedSeason);
}
async function loadSeasonEpisodes(season,offset=0,scrollToEpisodes=false) {
  const detail=state.detail;
  if(!detail||detail.kind!=='tvshow'||!$('episode-list'))return;
  const parent=detail.id;
  const node=$('episode-list');
  node.dataset.season=String(season);
  if(!offset)node.innerHTML='<p class="hint">Chargement des épisodes…</p>';
  try{
    const page=await api(`media/${encodeURIComponent(parent)}/seasons/${season}/episodes?limit=100&offset=${offset}`);
    if(state.detail?.id!==parent||node.dataset.season!==String(season))return;
    const markup=page.items.map(e=>{
      const watched=e.watch_status==='watched';
      const inProgress=e.watch_status==='in_progress';
      const statusLabel=watched?'Vu':inProgress?'En cours':'Non vu';
      const statusIcon=watched?'✓':inProgress?'◐':'○';
      return `<div class="season-episode-entry"><button type="button" class="season-episode" data-media="${esc(e.id)}" data-episode-season="${season}" data-episode-number="${Number(e.episode??0)}" title="Ouvrir la fiche de cet épisode" aria-label="Épisode ${esc(e.episode??'?')} : ${esc(e.title)}, ${statusLabel}">
        <span class="season-episode-number">E${String(e.episode??0).padStart(2,'0')}</span>
        <span class="season-episode-description"><span class="season-episode-title">${esc(e.title)}</span>${e.plot?`<small class="episode-synopsis">${esc(e.plot)}</small>`:''}</span>
        ${e.runtime_minutes?`<span class="season-episode-duration">${e.runtime_minutes} min</span>`:''}
        <span class="season-episode-state ${watched?'is-watched':inProgress?'is-progress':'is-unwatched'}" title="${statusLabel}" aria-label="${statusLabel}">${statusIcon}</span>
      </button>${state.role==='admin'? `<button type="button" class="season-episode-actions" data-episode-action data-episode-season="${season}" data-episode-number="${Number(e.episode??0)}" title="Marquer les épisodes jusqu’ici comme vus" aria-label="Actions pour l’épisode ${esc(e.episode??'?')}">⋯</button>` : ''}</div>`;
    }).join('');

    if(!offset){
      const seasonData=state.seasons?.find(s=>s.season===season);
      const actions=state.role==='admin'?`<div class="selected-season-watch-actions"><button type="button" data-mark-season="${season}" data-watched="true" ${state.personalPending.has(parent)?'disabled':''}>Tout vu</button><button type="button" data-mark-season="${season}" data-watched="false" ${state.personalPending.has(parent)?'disabled':''}>Tout non vu</button></div>`:'';
      node.innerHTML=`<div class="selected-season-header"><p class="section-label">${season===0?'SPÉCIAUX':'SAISON '+season} · ${page.total} ÉPISODES${seasonData?' · '+seasonData.watched_count+' vus':''}</p>${actions}</div>${markup||'<p class="hint">Aucun épisode.</p>'}`;
    }
    else {node.querySelector('[data-load-season]')?.remove();node.insertAdjacentHTML('beforeend',markup);}
    if(page.has_more)node.insertAdjacentHTML('beforeend',`<button class="secondary" data-load-season="${season}" data-offset="${offset+page.items.length}">Afficher plus d’épisodes</button>`);
    // Only a user-initiated season choice scrolls; refreshes/pagination keep
    // the current reading position in the detail modal.
    if(scrollToEpisodes&&!offset&&$('detail-dialog').open) {
      requestAnimationFrame(()=>{
        if(node.isConnected&&state.detail?.id===parent&&node.dataset.season===String(season))
          node.scrollIntoView({behavior:'smooth',block:'start'});
      });
    }
  }catch(e){if(state.detail?.id===parent&&node.dataset.season===String(season))node.textContent='Chargement des épisodes impossible : '+e.message;}
}
// An explicit episode action button retains the inclusive "mark through"
// command without intercepting the browser's native right-click menu.
function closeEpisodeContextMenu() {
  const menu=document.getElementById('episode-context-menu');
  if(!menu)return;
  if(menu.matches(':popover-open'))menu.hidePopover();
  menu.remove();
}
function openEpisodeContextMenu(button,x,y) {
  if(state.role!=='admin'||!button||state.personalPending.has(state.detail?.id))return;
  const season=Number(button.dataset.episodeSeason);
  const number=Number(button.dataset.episodeNumber);
  if(!Number.isInteger(season)||!Number.isInteger(number)||number<0)return;
  closeEpisodeContextMenu();
  const menu=document.createElement('div');
  menu.id='episode-context-menu';
  menu.className='episode-context-menu';
  menu.setAttribute('role','menu');
  // A fixed z-index alone cannot overtake a modal <dialog> in the top
  // layer. The menu must be a popover owned by that dialog.
  menu.setAttribute('popover','manual');
  menu.innerHTML=`<button role="menuitem" type="button" data-watch-through-season="${season}" data-watch-through-episode="${number}">✓ Marquer comme vus jusqu'à l'épisode ${number} inclus</button>`;
  $('detail-dialog').append(menu);
  menu.showPopover();
  menu.style.left=Math.max(8,Math.min(x,window.innerWidth-menu.offsetWidth-8))+'px';
  menu.style.top=Math.max(8,Math.min(y,window.innerHeight-menu.offsetHeight-8))+'px';
  menu.querySelector('button')?.focus({preventScroll:true});
}
document.addEventListener('pointerdown',event=>{
  if(!event.target.closest('#episode-context-menu,[data-episode-action]'))closeEpisodeContextMenu();
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape')closeEpisodeContextMenu();
});
async function diagnoseKodi() {
  const item=state.detail;
  const display=$('kodi-diagnostic');
  const button=$('diagnose-kodi');
  if(!item||!display||!button)return;
  display.hidden=false;
  display.textContent='Contrôle du parcours de vidéothèque via les tables MariaDB…';
  button.disabled=true;
  try {
    const data=await api('media/'+encodeURIComponent(item.id)+'/diagnostics');
    if(state.detail?.id!==item.id||!$('kodi-diagnostic'))return;
    const paragraphs=[];
    for(const source of data.sources){
      paragraphs.push('<p><strong>'+esc(source.source_name)+'</strong> ('+(sourccollectionTypes.includes(e.source_type)?'CLZ':'Kodi')+')</p>');
      if(collectionTypes.includes(source.source_type)){
        paragraphs.push('<p class="hint">Édition CLZ toujours présente : cette fiche est conservée.</p>');
        continue;
      }
      paragraphs.push('<p class="hint">Base configurée : '+esc(source.configured_database||'Automatique')+' · Base interrogée : <strong>'+esc(source.queried_database||'non vérifiée')+'</strong></p>');
      if(source.library_rule)paragraphs.push('<p class="hint">Critère de la vidéothèque : <strong>'+esc(source.library_rule)+'</strong></p>');
      if(source.error)paragraphs.push('<p class="error-text">Échec du contrôle : '+esc(source.error)+'</p>');
      for(const edition of source.editions){
        const status=edition.in_library===true?'PRÉSENT dans la vidéothèque Kodi (SQL)':
          edition.in_library===false?'ABSENT du parcours Kodi (version par défaut non trouvée)':
          edition.exists_in_movie===true?'Présent dans movie (schéma ancien ou contrôle incomplet)':
          edition.exists_in_movie===false?'ABSENT de movie':'Non vérifié';
        paragraphs.push('<p class="'+(edition.in_library===false||edition.exists_in_movie===false?'error-text':'hint')+'">'+esc(edition.external_key)+' · idMovie='+esc(edition.kodi_id??'—')+' · '+esc(status)+'</p>');
        const links=edition.sql_links;
        if(links){
          paragraphs.push('<p class="hint">Fichier par défaut (ID '+esc(links.default_file_id??'—')+') : '+(links.file_row_exists?'ligne files présente':'<strong>ligne files absente</strong>')+' · '+(links.path_row_exists?'chemin SQL présent':'<strong>chemin SQL absent</strong>')+'</p>');
          if(links.file)paragraphs.push('<p class="hint">Chemin enregistré : <code>'+esc(links.file)+'</code></p>');
          if(links.excluded_from_scan)paragraphs.push('<p class="hint">Répertoire exclu du scan (path.exclude)</p>');
          if(links.scan_disabled)paragraphs.push('<p class="hint">Mise à jour du répertoire désactivée (path.noUpdate)</p>');
          if(links.versions?.length)paragraphs.push('<p class="hint">Versions dans videoversion : '+links.versions.map(v=>'ID '+esc(v.file_id??'—')+' ('+(v.file_row_exists?'lien valide':'fichier SQL absent')+')').join(' · ')+'</p>');
        }
      }
      paragraphs.push('<p class="hint">La présence dans files/path atteste un lien SQL, pas l’existence physique du fichier NAS. exclude/noUpdate contrôlent le scan, ce ne sont pas des indicateurs de suppression.</p>');
    }
    $('kodi-diagnostic').innerHTML=paragraphs.join('')||'<p>Aucune édition répertoriée.</p>';
  }catch(e){
    if(state.detail?.id===item.id&&$('kodi-diagnostic')) $('kodi-diagnostic').textContent=e.message;
  }finally{
    if(state.detail?.id===item.id&&$('diagnose-kodi')) $('diagnose-kodi').disabled=false;
  }
}
document.addEventListener('click',event=>{
  if(event.target.closest('#diagnose-kodi'))diagnoseKodi();
});
async function pollJob(job) {
  state.jobs=[job,...state.jobs.filter(j=>j.id!==job.id)];renderActivity();
  const target=$('job-status'); if (target) {target.hidden=false;target.textContent=jobProgressLabel(job);}
  for(let i=0;i<600;i++) {
    const value=await api(`jobs/${encodeURIComponent(job.id)}`);
    state.jobs=[value,...state.jobs.filter(j=>j.id!==value.id)];renderActivity();
    if(target && ['pending','running'].includes(value.status)){
      target.innerHTML=jobProgressMarkup(value);
    }
    if(value.status==='failed') throw new Error(value.error||'Import échoué.');
    if(value.status==='completed') {
      const result=value.result||{};
      const details=[jobOperationLabel(value),`${result.imported||0} entrées synchronisées`];
      if(result.database) details.unshift(`Base Kodi : ${result.database}`);
      if(result.created_editions) details.push(`${result.created_editions} nouvelles éditions`);
      if(result.updated_editions) details.push(`${result.updated_editions} éditions reconnues`);
      if(result.unchanged_editions) details.push(`${result.unchanged_editions} éditions inchangées (sans réindexation)`);
      if(result.rebuilt_works!=null) details.push(`${result.rebuilt_works} fiches réindexées`);
      if(result.library_verified)details.push(`Parcours SQL Kodi vérifié : ${result.library_movie_count} films · ${result.library_excluded_editions} éditions écartées`);

      if(result.kodi_scan==='finished') details.unshift('Analyse Kodi terminée, catalogue local mis à jour');
      if(result.kodi_scan==='sql_only') details.unshift('Kodi non analysé : seule la base SQL existante a été synchronisée.');
      if(result.kodi_scan_warnings?.length) details.push(...result.kodi_scan_warnings.map(msg=>'Attention : '+msg));
       if(result.removed_editions) details.push(`${result.removed_editions} éditions retirées de cette source`);
      else if(result.database) details.push('Aucune suppression Kodi détectée');
      if(result.removed_works) details.push(`${result.removed_works} œuvres supprimées du catalogue`);
      if(result.orphaned_movie_view_rows) details.push(`${result.orphaned_movie_view_rows} lignes obsolètes dans movie_view ignorées`);
      if(result.movie_table_count!=null) details.push(`${result.movie_table_count} films présents dans movie`);
      if(result.sqlite_publish_ms!=null) details.push(`Publication SQLite : ${result.sqlite_publish_ms} ms`);
      if(result.sql_view_fetch_ms) details.push(`Lecture vues SQL : ${Object.entries(result.sql_view_fetch_ms).map(([kind,ms])=>kind+' '+ms+' ms').join(', ')}`);
      if(result.kept_clz_works) details.push(`${result.kept_clz_works} œuvres conservées via CLZ`);
      if(result.kept_other_sources) details.push(`${result.kept_other_sources} œuvres conservées par une autre source`);
      if(result.matching_conflicts) details.push(`${result.matching_conflicts} rapprochements ambigus conservés séparément`);
      const summary=details.join(' · ')+'.';
      const removedTitles=result.removed_titles||[];
      const more=Math.max(0,(result.removed_titles_total||removedTitles.length)-removedTitles.length);
      const removalReport=removedTitles.length
        ? `\nRetirés de cette source : ${removedTitles.join(' · ')}${more?` · et ${more} autre(s)`:''}.`
        : '';
      const report=summary+removalReport+(result.database&&result.removed_editions===0?
        '\nSi un film supprimé de Kodi reste affiché, vérifiez que la base MyVideos indiquée est bien celle utilisée par Kodi et que la fiche ne provient pas de CLZ.':'');
      if(target) target.textContent=report;
      toast(summary);
      await refresh();
      if($('connections-dialog').open) {
        await connections();
        const status=$('job-status');
        if(status) {status.hidden=false;status.textContent=report;}
      }
      return;
    }
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  toast('L’import continue en arrière-plan. Consultez les paramètres pour son résultat.');
}
// Shared explorer UI: the same folder/back/list/grid conventions apply to
// SMB, NFS, FTP and cloud connectors. Only adapters that can stream expose Play.
const networkExplorer={
  locations:[],selected:null,stack:[],entries:[],shown:0,
  view:'grid',busy:false,revision:0,menuIndex:null,
  searchActive:false,searchEntries:[],searchNextOffset:null,searchRevision:0,
  searchBusy:false,searchTruncated:false,
  sort:readStorage('network-browser-sort')||'name',descending:readStorage('network-browser-descending')==='true'
};
function networkPathLabel() {
  if(!networkExplorer.selected)return 'Sources enregistrées';
  const nodes=networkExplorer.stack;
  return networkExplorer.selected.name+
    (nodes.length?' / '+nodes.map(x=>x.name).join(' / '):'');
}
const browsableLocationKinds=new Set(['smb','nfs','ftp','sftp','onedrive']);
function networkHomeMarkup(locations) {
  const allowed=locations.filter(l=>browsableLocationKinds.has(l.kind));
  return allowed.slice(0,12).map(loc=>
    '<button class="network-home-entry" type="button" data-network-home-open="'+esc(loc.id)+'">'+
      '<span aria-hidden="true">'+(loc.kind==='onedrive'?'☁':'▣')+'</span>'+
      '<strong>'+esc(loc.name)+'</strong>'+
      '<small>'+esc(loc.kind.toUpperCase())+'</small>'+
    '</button>'
  ).join('')||'<span class="hint">Aucune source enregistrée. Ajoutez un emplacement dans Paramètres → Réseau et cloud.</span>';
}
async function refreshNetworkHome() {
  try {
    networkExplorer.locations=(await api('playback/locations')).filter(
      loc=>browsableLocationKinds.has(loc.kind));
    $('network-home-locations').innerHTML=networkHomeMarkup(networkExplorer.locations);
  }catch(e){
    $('network-home-locations').textContent='Sources indisponibles : '+e.message;
  }
}
function networkBrowseError(message) {
  $('network-browser-feedback').textContent=message||'';
}
function networkMedia(filename) {
  return /\.(mp4|m4v|mov|mkv|webm|avi|ts|m2ts|mpg|mpeg|mp3|m4a|wav)$/i.test(filename||'');
}
// Never launch dozens of remote FFmpeg thumbnail extractions together:
// a folder may contain hundreds of MKVs on the same SMB session.
let networkThumbnailBusy=0;
let networkThumbnailPaused=false;
const networkThumbnailControllers=new Set();
let networkThumbnailWarningShown=false;
const networkThumbnailQueue=[];
async function fetchNetworkThumbnail(image){
  const controller=new AbortController();
  networkThumbnailControllers.add(controller);
  const timeout=setTimeout(()=>controller.abort(),15000);
  try{
    const response=await fetch(image.dataset.thumbnailUrl,{
      credentials:'same-origin',signal:controller.signal
    });
    if(!response.ok)throw new Error('HTTP '+response.status);
    const blob=await response.blob();
    if(!blob.type.startsWith('image/'))throw new Error('Type de réponse non image');
    if(!image.isConnected)return;
    const object=URL.createObjectURL(blob);
    image.addEventListener('load',()=>URL.revokeObjectURL(object),{once:true});
    image.addEventListener('error',()=>URL.revokeObjectURL(object),{once:true});
    image.src=object;
  }catch(error){
    if(networkThumbnailPaused&&error.name==='AbortError'){
      if(image.isConnected)networkThumbnailQueue.push(image);
      return;
    }
    if(!image.isConnected)return;
    const parent=image.parentElement;
    if(parent){
      parent.title='Vignette indisponible : '+error.message;
      parent.textContent='🎞';
    }
    if(!networkThumbnailWarningShown){
      networkThumbnailWarningShown=true;
      networkBrowseError('Certaines vignettes ne peuvent pas être extraites ('+
        error.message+'). Consultez les journaux serveur si toutes sont absentes.');
    }
  }finally{
    clearTimeout(timeout);
    networkThumbnailControllers.delete(controller);
    networkThumbnailBusy--;
    runNetworkThumbnailQueue();
  }
}
function runNetworkThumbnailQueue(){
  if(networkThumbnailPaused)return;
  while(networkThumbnailBusy<2 && networkThumbnailQueue.length){
    const image=networkThumbnailQueue.shift();
    if(!image.isConnected)continue;
    networkThumbnailBusy++;
    void fetchNetworkThumbnail(image);
  }
}
const networkThumbnailObserver=typeof IntersectionObserver==='function' ? new IntersectionObserver(entries=>{
  for(const entry of entries){
    if(!entry.isIntersecting)continue;
    const image=entry.target;
    networkThumbnailObserver.unobserve(image);
    if(image.isConnected)networkThumbnailQueue.push(image);
  }
  runNetworkThumbnailQueue();
},{root:$('network-browser-items'),rootMargin:'120px'}) : null;
function observeNetworkThumbnails(){
  document.querySelectorAll('#network-browser-items img[data-thumbnail-url]').forEach(image=>{
    if(networkThumbnailObserver)networkThumbnailObserver.observe(image);
    else networkThumbnailQueue.push(image);
  });
  if(!networkThumbnailObserver)runNetworkThumbnailQueue();
}
function networkThumbnail(entry){
  const loc=networkExplorer.selected;
  if(!loc || entry.is_dir || !/\.(jpg|jpeg|png|webp|gif|bmp|tif|tiff|heic|heif|avif|mp4|m4v|mov|mkv|avi|ts|m2ts|webm|mpg|mpeg)$/i.test(entry.name||''))return '';
  const src=url('api/v1/playback/thumbnail/'+encodeURIComponent(loc.id)+
    '?path='+encodeURIComponent(entry.path)+'&filename='+encodeURIComponent(entry.name));
  return '<img class="network-browser-thumbnail" loading="lazy" decoding="async" data-thumbnail-url="'+esc(src)+'" alt="">';
}
function networkCurrentPath(){
  return networkExplorer.stack.at(-1)?.path||'';
}
function networkDisplayedEntries(browser){
  return browser.searchActive?browser.searchEntries:browser.entries;
}
function sortedNetworkEntries(browser) {
  const key=browser.sort, multiplier=browser.descending?-1:1;
  return networkDisplayedEntries(browser).map((entry,index)=>({...entry,_originalIndex:index})).sort((a,b)=>{
    if(Boolean(a.is_dir)!==Boolean(b.is_dir))return a.is_dir?-1:1;
    let result=0;
    if(key==='size')result=(Number(a.size)||0)-(Number(b.size)||0);
    else if(key==='type')result=(a.name.split('.').pop()||'').localeCompare(b.name.split('.').pop()||'',displayLocale(),{numeric:true});
    else if(key==='modified'||key==='created'){
      const timestamp=value=>typeof value==='number'?value*1000:
        (typeof value==='string'&&value?Date.parse(value)||0:0);
      const l=timestamp(a[key]),r=timestamp(b[key]);
      result=l&&r?l-r:0;
    }
    if(!result)result=a.name.localeCompare(b.name,displayLocale(),{numeric:true,sensitivity:'base'});
    return result*multiplier||a._originalIndex-b._originalIndex;
  });
}
function renderNetworkBrowser() {
  const browser=networkExplorer;
  const box=$('network-browser-items');
  const display=networkDisplayedEntries(browser);
  const count=Math.min(browser.shown,display.length);
  $('network-browser-search').disabled=!browser.selected;
  $('network-browser-recursive').disabled=!browser.selected;
  box.className='network-browser-items '+(browser.view==='grid'?'is-grid':'is-list');
  $('network-browser-path').textContent=networkPathLabel();
  $('network-browser-back').disabled=!browser.selected;
  $('network-browser-grid').setAttribute('aria-pressed',String(browser.view==='grid'));
  $('network-browser-list').setAttribute('aria-pressed',String(browser.view==='list'));
  $('network-browser-sort').value=browser.sort;
  $('network-browser-sort-order').textContent=browser.descending?'↓':'↑';
  $('network-browser-sort-order').setAttribute('aria-pressed',String(browser.descending));
  if(!browser.selected){
    box.innerHTML=browser.locations.filter(loc=>browsableLocationKinds.has(loc.kind)).map(loc=>
      '<button type="button" class="network-browser-tile" data-network-source="'+esc(loc.id)+'">'+
      '<span class="network-browser-tile-icon">'+
      (loc.kind==='onedrive'?'☁':'▣')+
      '</span><strong>'+esc(loc.name)+'</strong><small>'+esc(loc.kind.toUpperCase())+
      '</small></button>'
    ).join('')||'<p class="hint">Aucune source enregistrée.</p>';
  }else{
    box.innerHTML=sortedNetworkEntries(browser).slice(0,count).map((item,i)=>
      '<div class="network-browser-tile" data-entry="'+i+'">'+
      '<button class="network-browser-item-main" data-network-entry="'+i+'" type="button">'+
      '<span class="network-browser-tile-icon">'+(networkThumbnail(item)|| (item.is_dir?'📁':networkMedia(item.name)?'🎞':'📄'))+'</span>'+
      '<span class="network-browser-item-name">'+esc(item.name)+'</span>'+
      (browser.searchActive?'<small class="network-browser-relative-path">'+esc(item.path)+'</small>':'')+
      '<small>'+(item.is_dir?'Dossier':formatBytes(item.size||0))+'</small></button>'+
      (!item.is_dir?'<button type="button" class="network-browser-download" data-network-download="'+i+'" title="Télécharger" aria-label="Télécharger '+esc(item.name)+'">↓</button>':'')+
      '<button type="button" class="network-browser-more-actions" data-network-menu="'+i+
      '" aria-haspopup="menu" aria-expanded="'+String(browser.menuIndex===i)+
      '" aria-label="Actions '+esc(item.name)+'">···</button>'+
      (browser.menuIndex===i?
        '<div class="network-browser-action-menu" role="menu">'+
        (!item.is_dir&&networkMedia(item.name)?'<button type="button" role="menuitem" data-network-action="play" data-entry-index="'+i+'">Lire</button>':'')+
        (!item.is_dir?'<button type="button" role="menuitem" data-network-action="download" data-entry-index="'+i+'">Télécharger</button>':'')+
        '<button type="button" role="menuitem" data-network-action="copy-path" data-entry-index="'+i+'">Copier le chemin</button>'+
        '<small>Modifications indisponibles sur le Web</small></div>':'')+
      '</div>'
    ).join('')||(!browser.busy?'<p class="hint">Aucun résultat.</p>':'');
  }
  $('network-browser-more').hidden=!browser.selected ||
    (browser.searchActive ?
      (browser.searchNextOffset===null && display.length<=count) :
      display.length<=count);
  $('network-browser-more').textContent=browser.searchActive && browser.searchNextOffset!==null
    ? 'Charger les résultats suivants' : 'Afficher la suite';
  observeNetworkThumbnails();
}
function formatBytes(bytes){
  const n=Number(bytes)||0;
  if(n<1024)return n+' o';
  if(n<1048576)return (n/1024).toFixed(0)+' Ko';
  if(n<1073741824)return (n/1048576).toFixed(1)+' Mo';
  return (n/1073741824).toFixed(2)+' Go';
}
function networkClearSearch(){
  const browser=networkExplorer;
  browser.searchRevision++;
  browser.searchActive=false;
  browser.searchEntries=[];
  browser.searchNextOffset=null;
  browser.searchBusy=false;
  browser.searchTruncated=false;
  browser.menuIndex=null;
}
async function networkSearch(append=false){
  const browser=networkExplorer;
  const query=$('network-browser-search').value.trim();
  if(!browser.selected || query.length<2){
    networkClearSearch();
    if(query.length===1)networkBrowseError('Saisir au moins 2 caractères.');
    else networkBrowseError('');
    renderNetworkBrowser();
    return;
  }
  const recursive=$('network-browser-recursive').checked;
  const revision=append?browser.searchRevision:++browser.searchRevision;
  if(!append){
    browser.searchEntries=[];
    browser.searchNextOffset=null;
    browser.shown=0;
    browser.menuIndex=null;
  }
  browser.searchActive=true;
  if(!recursive){
    browser.searchEntries=browser.entries.filter(
      entry=>entry.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())
    );
    browser.searchNextOffset=null;
    browser.shown=browser.searchEntries.length;
    networkBrowseError(browser.searchEntries.length+' résultat(s) dans ce dossier.');
    renderNetworkBrowser();
    return;
  }
  if(append && browser.searchBusy)return;
  browser.searchBusy=true;
  networkBrowseError('Recherche dans les sous-dossiers…');
  try{
    const offset=append?browser.searchNextOffset:0;
    if(offset===null)return;
    const params=new URLSearchParams({q:query,path:networkCurrentPath(),offset:String(offset),limit:'80'});
    const response=await api('playback/search/'+encodeURIComponent(browser.selected.id)+'?'+params);
    if(revision!==browser.searchRevision)return;
    browser.searchEntries.push(...response.items);
    browser.searchNextOffset=response.next_offset;
    browser.searchTruncated=response.truncated===true;
    browser.shown=browser.searchEntries.length;
    networkBrowseError(browser.searchEntries.length+' résultat(s)'+
      (browser.searchTruncated?' · limite de parcours atteinte (160 dossiers / 8 000 éléments).':'')+
      (browser.searchNextOffset!==null?' · autres résultats disponibles.':''));
  }catch(e){
    if(revision===browser.searchRevision)networkBrowseError(e.message);
  }finally{
    if(revision===browser.searchRevision){
      browser.searchBusy=false;
      renderNetworkBrowser();
    }
  }
}
$('network-browser-search-form').addEventListener('submit',event=>{
  event.preventDefault();
  void networkSearch();
});
$('network-browser-search').addEventListener('search',()=>void networkSearch());
$('network-browser-recursive').addEventListener('change',()=>{
  if($('network-browser-search').value.trim().length>=2)void networkSearch();
});

async function networkOpenRoot(id=null){
  networkClearSearch();
  $('network-browser-search').value='';
  networkExplorer.selected=id?networkExplorer.locations.find(x=>x.id===id)||null:null;
  networkExplorer.stack=[];
  networkExplorer.entries=[];
  networkExplorer.shown=0;
  renderNetworkBrowser();
  if(networkExplorer.selected)await networkOpenFolder('',networkExplorer.selected.name);
}
async function networkOpenFolder(path,name){
  networkClearSearch();
  $('network-browser-search').value='';
  const browser=networkExplorer;
  if(!browser.selected)return;
  const revision=++browser.revision;
  browser.busy=true;
  networkThumbnailWarningShown=false;
  networkBrowseError('Ouverture du dossier…');
  const target=path||'';
  try {
    const items=await api('playback/browse/'+encodeURIComponent(browser.selected.id)+
      '?path='+encodeURIComponent(target));
    if(revision!==browser.revision)return;
    browser.stack.push({path:target,name});
    browser.entries=items;
    browser.shown=Math.min(80,items.length);
    networkBrowseError(items.length>=2000?'Affichage limité aux 2 000 premiers éléments.':'');
  }catch(e) {
    if(revision!==browser.revision)return;
    const detail=e.status===501?'Ce protocole nécessite un connecteur ou un montage disponible sur le serveur.':e.message;
    networkBrowseError(detail);
  }finally{
    if(revision===browser.revision){browser.busy=false;renderNetworkBrowser();}
  }
}
async function networkGoBack(){
  const browser=networkExplorer;
  if(!browser.selected)return;
  if(browser.stack.length<=1){await networkOpenRoot();return;}
  const parent=browser.stack.at(-2);
  browser.stack.pop();
  browser.stack.pop();
  await networkOpenFolder(parent.path,parent.name);
}
function networkPlay(entry){
  const browser=networkExplorer;
  if(!browser.selected || !networkMedia(entry.name))return;
  const kind=browser.selected.kind;
  if(!['smb','ftp','sftp','nfs','onedrive'].includes(kind)) {
    networkBrowseError('La lecture de ce fournisseur n’est pas encore disponible.');
    return;
  }
  const query='?path='+encodeURIComponent(entry.path);
  const base='api/v1/playback/media/'+encodeURIComponent(browser.selected.id);
  networkBrowseError('');
  void openPlayer({
    title:entry.name.replace(/\.[^.]+$/,''),
    subtitle:browser.selected.name||'',
    extension:entry.name.split('.').pop(),
    directURL:url('api/v1/playback/stream/'+encodeURIComponent(browser.selected.id)+query),
    probeURL:url(base+'/probe'+query),
    subtitleURL:index=>url(`${base}/subtitles/${index}.vtt${query}`),
    streamURL:params=>url(`${base}/stream.mp4${query}&${new URLSearchParams(params)}`),
  });
}
async function openNetworkBrowser(id=null){
  showDialog('network-browser-dialog');
  if(networkExplorer.locations.length===0)await refreshNetworkHome();
  await networkOpenRoot(id);
}
$('network-explorer-button').addEventListener('click',()=>void openNetworkBrowser());
$('network-home-locations').addEventListener('click',event=>{
  const button=event.target.closest('[data-network-home-open]');
  if(button)void openNetworkBrowser(button.dataset.networkHomeOpen);
});
$('network-browser-back').addEventListener('click',()=>void networkGoBack());
$('network-browser-grid').addEventListener('click',()=>{
  networkExplorer.view='grid';renderNetworkBrowser();
});
$('network-browser-list').addEventListener('click',()=>{
  networkExplorer.view='list';renderNetworkBrowser();
});
$('network-browser-sort').addEventListener('change',event=>{
  networkExplorer.sort=event.target.value;
  writeStorage('network-browser-sort',networkExplorer.sort);
  renderNetworkBrowser();
});
$('network-browser-sort-order').addEventListener('click',()=>{
  networkExplorer.descending=!networkExplorer.descending;
  writeStorage('network-browser-descending',String(networkExplorer.descending));
  renderNetworkBrowser();
});
$('network-browser-refresh').addEventListener('click',()=>{
  if(networkExplorer.selected){
    const current=networkExplorer.stack.at(-1);
    networkExplorer.stack.pop();
    void networkOpenFolder(current?.path||'',current?.name||networkExplorer.selected.name);
  } else void refreshNetworkHome().then(()=>renderNetworkBrowser());
});
$('network-browser-more').addEventListener('click',()=>{
  if(networkExplorer.searchActive&&networkExplorer.searchNextOffset!==null){
    void networkSearch(true);
  }else{
    networkExplorer.shown+=80;renderNetworkBrowser();
  }
});
// Missing/unsupported frames retain a normal file icon; do not display a broken image.
$('network-browser-items').addEventListener('error',event=>{
  const image=event.target.closest?.('.network-browser-thumbnail');
  if(!image)return;
  const parent=image.parentElement;
  if(parent)parent.textContent='🎞';
},true);
$('network-browser-items').addEventListener('click',event=>{
  const source=event.target.closest('[data-network-source]');
  if(source){void networkOpenRoot(source.dataset.networkSource);return;}
  const action=event.target.closest('[data-network-action]');
  if(action){
    const item=sortedNetworkEntries(networkExplorer)[Number(action.dataset.entryIndex)];
    if(!item)return;
    const choice=action.dataset.networkAction;
    networkExplorer.menuIndex=null;
    renderNetworkBrowser();
    if(choice==='play')networkPlay(item);
    else if(choice==='download')networkDownload(item);
    else if(choice==='copy-path'&&navigator.clipboard?.writeText){
      void navigator.clipboard.writeText(item.path);
    }
    return;
  }
  const button=event.target.closest('[data-network-entry],[data-network-menu],[data-network-download]');
  if(!button)return;
  const index=Number(button.dataset.networkEntry??button.dataset.networkMenu??button.dataset.networkDownload);
  const item=sortedNetworkEntries(networkExplorer)[index];
  if(!item)return;
  if(button.hasAttribute('data-network-download')) {
    networkDownload(item);
    return;
  }
  if(button.hasAttribute('data-network-menu')) {
    networkExplorer.menuIndex=networkExplorer.menuIndex===index?null:index;
    renderNetworkBrowser();
    return;
  }
  networkExplorer.menuIndex=null;
  if(item.is_dir)void networkOpenFolder(item.path,item.name);
  else networkPlay(item);
});
function networkDownload(item){
  if(!networkExplorer.selected || item.is_dir)return;
  const link=document.createElement('a');
  link.href=url('api/v1/playback/stream/'+encodeURIComponent(networkExplorer.selected.id)+'?path='+encodeURIComponent(item.path));
  link.download=item.name.replace(/[\\/]/g,'_');
  document.body.append(link);link.click();link.remove();
}

const playbackCloudProviders=[['onedrive','OneDrive']];
async function renderPlaybackCloudSettings() {
  const container=$('connection-content');
  if(!container)return;
  const section=document.createElement('section');
  section.className='metadata-status';
  section.id='playback-cloud-web-settings';
  section.innerHTML='<h3>Services cloud · navigateur web</h3>'+
    '<p class="hint">Pour OneDrive, le code de connexion Microsoft fonctionne sur réseau local, sans domaine ni popup OAuth. Le serveur conserve les jetons chiffrés pour la navigation et la lecture.</p>'+
    '<div id="playback-cloud-web-list"></div>'+
    '<details id="playback-cloud-developer-options"><summary>Configuration développeur des applications cloud (avancé)</summary>'+
    '<p class="hint">OneDrive : seul un identifiant public Entra est nécessaire avec le code de connexion ; activez les flux de clients publics.</p>'+
    '<div id="playback-cloud-developer-list"></div>'+
    '<form id="playback-cloud-app-form" class="connection-form" hidden>'+
    '<strong class="wide" id="playback-cloud-app-title"></strong>'+
    '<input name="provider" type="hidden">'+
    '<label class="wide">Client ID<input name="client_id" required autocomplete="off"></label>'+
    '<label class="wide">Client secret<input name="client_secret" type="password" autocomplete="new-password" placeholder="Laisser vide pour conserver"></label>'+
    '<label class="wide">URL de retour HTTPS<input name="redirect_url" required type="url"></label>'+
    '<button class="primary" type="submit">Enregistrer l’application OAuth</button>'+
    '<button type="button" class="secondary" data-cloud-app-cancel>Annuler</button>'+
    '</form></details>'+
    '<div id="playback-cloud-device-panel" role="region" aria-live="polite"></div>'+
    '<p id="playback-cloud-web-status" role="status" aria-live="polite"></p>'+
    '<p id="playback-cloud-web-link"></p>';
  $('playback-cloud-web-settings')?.remove();
  container.prepend(section);
  const message=section.querySelector('#playback-cloud-web-status');
  const link=section.querySelector('#playback-cloud-web-link');
  const list=section.querySelector('#playback-cloud-web-list');
  const form=section.querySelector('#playback-cloud-app-form');
  const developerList=section.querySelector('#playback-cloud-developer-list');
  const devicePanel=section.querySelector('#playback-cloud-device-panel');
  let locations=[],statuses=[],apps=[],providers=[];
  let deviceRun=0;
  let activeDeviceCode='';
  // The UI may be served at http://nas.local while the OAuth callback is at
  // https://media.example.org. Trust ONLY the redirect origin returned by
  // our authenticated authorization API, never arbitrary popup senders.
  const allowedCallbackOrigins=new Set([window.location.origin]);
  const onCloudAuthorized=event=>{
    if(!allowedCallbackOrigins.has(event.origin)||
       event.data?.type!=='homekodi-cloud-oauth')return;
    if(!section.isConnected){window.removeEventListener('message',onCloudAuthorized);return;}
    // Reload the server-side connected state; do not erase the outcome while
    // refreshing the list, and retain provider errors for diagnosis.
    void load({preserveMessage:true}).then(()=>{
      message.textContent=event.data.success?'Compte connecté.':
        (event.data.error||'L’autorisation a échoué.');
    });
  };
  window.addEventListener('message',onCloudAuthorized);
  async function load({preserveMessage=false}={}) {
    if(!preserveMessage)message.textContent='';
    link.replaceChildren();
    try {
      const outputs=await Promise.all([
        api('playback/locations'),api('playback/cloud/status'),
        state.role==='admin'?api('playback/cloud/apps'):Promise.resolve([]),
        api('playback/cloud/providers')
      ]);
      [locations,statuses,apps,providers]=outputs;
    } catch(e) {message.textContent=e.message;return;}
    const byId=new Map(statuses.map(row=>[row.id,row]));
    const providerByKind=new Map(providers.map(row=>[row.provider,row]));
    list.innerHTML=playbackCloudProviders.map(([kind,label])=>{
      const accounts=locations.filter(row=>row.kind===kind);
      const provider=providerByKind.get(kind);
      const configured=provider?.configured===true;
      const deviceReady=kind==='onedrive'&&provider?.device_configured===true;
      const note=deviceReady?'Connexion sans domaine disponible':
        configured?'Application OAuth configurée':
        (provider?.device_reason||provider?.reason||'Configuration administrateur requise');
      const disabled=state.role!=='admin';
      const accountCards=accounts.map(place=>{
        const status=byId.get(place.id);
        const displayName=status?.account_email||place.username||place.name||label;
        const active=status?.connected===true;
        return '<div class="connection-card"><div><strong>'+esc(displayName)+'</strong>'+
          '<p class="hint">'+esc(label)+' · '+(active?'Connecté':'Non connecté')+
          (displayName!==place.name?' · '+esc(place.name):'')+'</p></div>'+
          '<div class="connection-actions">'+
          (kind==='onedrive'&&deviceReady
            ? '<button type="button" class="secondary" data-web-cloud-device="onedrive" data-cloud-place-id="'+esc(place.id)+'"'+
              (disabled?' disabled':'')+'>Reconnecter</button>'
            : '<button type="button" class="secondary" data-web-cloud-connect="'+esc(kind)+'" data-cloud-place-id="'+esc(place.id)+'"'+
              (disabled||!configured?' disabled':'')+'>Reconnecter</button>')+
          (state.role==='admin'?'<button class="secondary" type="button" data-web-cloud-disconnect="'+
            esc(place.id)+'"'+(active?'':' disabled')+'>Déconnecter</button>'+
            '<button class="secondary" type="button" data-web-cloud-remove="'+
            esc(place.id)+'">Supprimer ce compte</button>':'')+
          '</div></div>';
      }).join('');
      const addButtons=kind==='onedrive'
        ? '<button type="button" data-web-cloud-device="onedrive"'+
          (disabled||!deviceReady?' disabled':'')+'>Ajouter un compte OneDrive</button>'+
          (configured?'<button type="button" class="secondary" data-web-cloud-connect="onedrive"'+
            (disabled?' disabled':'')+'>Ajouter via OAuth HTTPS</button>':'')
        : '<button type="button" data-web-cloud-connect="'+esc(kind)+'"'+
          (disabled||!configured?' disabled':'')+'>Ajouter un compte '+esc(label)+'</button>';
      return '<div class="connection-card"><div><strong>'+esc(label)+'</strong>'+
        '<p class="hint">'+esc(note)+' · '+accounts.length+' compte(s)</p></div>'+
        '<div class="connection-actions">'+addButtons+'</div></div>'+
        (accountCards||'<p class="hint">Aucun compte de ce service enregistré.</p>');
    }).join('')+'<p><button class="secondary" type="button" data-web-cloud-refresh>Actualiser les connexions</button></p>';
    developerList.innerHTML=state.role==='admin'
      ? playbackCloudProviders.map(([kind,label])=>'<button type="button" class="secondary" data-cloud-app-edit="'+
        esc(kind)+'">Configurer '+esc(label)+'</button>').join(' ')
      : '';
  }
  section.addEventListener('click',async event=>{
    const button=event.target.closest('button');
    if(!button||button.disabled)return;
    if(button.hasAttribute('data-cloud-app-cancel')){form.hidden=true;return;}
    const edit=button.dataset.cloudAppEdit;
    if(edit){
      const app=apps.find(row=>row.provider===edit);
      form.hidden=false;
      form.elements.provider.value=edit;
      form.elements.client_id.value=app?.client_id||
        providers.find(row=>row.provider===edit)?.device_client_id||'';
      form.elements.client_secret.value='';
      const origin=location.origin==='null'?'':location.origin;
      form.elements.redirect_url.value=app?.redirect_url||
        (edit==='onedrive'?'':origin+baseURL.pathname+'api/v1/playback/cloud/callback');
      form.elements.redirect_url.required=edit!=='onedrive';
      section.querySelector('#playback-cloud-app-title').textContent=
        'Application OAuth · '+(playbackCloudProviders.find(x=>x[0]===edit)?.[1]||edit);
      form.scrollIntoView({block:'nearest'});
      return;
    }
    if(button.hasAttribute('data-web-cloud-refresh')){await load();return;}
    if(button.hasAttribute('data-cloud-device-copy')){
      if(!activeDeviceCode)return;
      try{
        await navigator.clipboard.writeText(activeDeviceCode);
        message.textContent='Code copié.';
      }catch(_){message.textContent='Sélectionnez le code affiché pour le copier.';}
      return;
    }
    if(button.hasAttribute('data-cloud-device-cancel')){
      ++deviceRun;
      activeDeviceCode='';
      devicePanel.replaceChildren();
      message.textContent='Connexion OneDrive annulée dans cette interface.';
      return;
    }
    const deviceKind=button.dataset.webCloudDevice;
    if(deviceKind==='onedrive'){
      const run=++deviceRun;
      activeDeviceCode='';
      devicePanel.replaceChildren();
      button.disabled=true;
      message.textContent='Demande du code de connexion Microsoft…';
      try{
        const existingId=button.dataset.cloudPlaceId;
        let place=existingId?locations.find(row=>row.id===existingId&&row.kind==='onedrive'):null;
        if(existingId&&!place)throw new Error('Compte OneDrive introuvable.');
        if(!place){
          place=await api('playback/locations',{
            method:'POST',body:JSON.stringify({
              name:'OneDrive '+(locations.filter(row=>row.kind==='onedrive').length+1),
              kind:'onedrive',target:''
            })
          });
        }
        const auth=await api('playback/cloud/'+encodeURIComponent(place.id)+'/device/start',{
          method:'POST'
        });
        if(run!==deviceRun||!section.isConnected)return;
        // Microsoft documents a stable device-code entry point. The server
        // owns the device_code, so opening this fixed URL with user_code
        // avoids rejecting valid Microsoft regional verification_uri hosts
        // and prevents redirecting the user to arbitrary response URLs.
        const verification=new URL('https://microsoft.com/devicelogin');
        activeDeviceCode=auth.user_code;
        devicePanel.innerHTML='<div class="connection-card"><div>'+
          '<strong>Autorisation Microsoft OneDrive</strong>'+
          '<p>1. Ouvrez le site Microsoft, puis connectez-vous.</p>'+
          '<p>2. Saisissez ce code : <strong class="pairing-code">'+esc(auth.user_code)+'</strong></p>'+
          '<p>3. Acceptez l’accès en lecture à vos fichiers OneDrive.</p>'+
          '<p><a href="'+esc(verification.href)+'" target="_blank" rel="noopener noreferrer">'+
            'Ouvrir microsoft.com/devicelogin</a></p>'+
          '</div><div class="connection-actions">'+
          '<button type="button" data-cloud-device-copy>Copier le code</button>'+
          '<button type="button" class="secondary" data-cloud-device-cancel>Annuler</button>'+
          '</div></div>';
        message.textContent='En attente de la validation sur Microsoft…';
        const deadline=Date.now()+Number(auth.expires_in||900)*1000;
        let retry=Math.max(5,Number(auth.interval)||5);
        while(run===deviceRun&&section.isConnected&&Date.now()<deadline){
          await new Promise(resolve=>setTimeout(resolve,retry*1000));
          if(run!==deviceRun||!section.isConnected)return;
          const state=await api('playback/cloud/'+encodeURIComponent(place.id)+'/device/poll',{
            method:'POST',body:JSON.stringify({request_id:auth.request_id})
          });
          if(run!==deviceRun||!section.isConnected)return;
          if(state.status==='connected'){
            devicePanel.replaceChildren();
            activeDeviceCode='';
            await load({preserveMessage:true});
            message.textContent='OneDrive connecté. Vous pouvez parcourir vos fichiers.';
            return;
          }
          if(state.status!=='pending')
            throw new Error(state.message||'Autorisation OneDrive interrompue.');
          retry=Math.max(2,Math.min(120,Number(state.retry_after)||5));
        }
        if(run===deviceRun)throw new Error('Code OneDrive expiré. Recommencez.');
      }catch(e){
        if(run===deviceRun)message.textContent=e.message||String(e);
      }finally{button.disabled=false;}
      return;
    }
    const remove=button.dataset.webCloudRemove;
    if(remove){
      const place=locations.find(row=>row.id===remove);
      if(!place||!window.confirm('Supprimer le compte cloud « '+place.name+' » et son autorisation sur ce serveur ?'))return;
      button.disabled=true;
      try{
        await api('playback/locations/'+encodeURIComponent(remove),{method:'DELETE'});
        await load();
        message.textContent='Compte cloud supprimé du serveur.';
      }catch(e){message.textContent=e.message||String(e);button.disabled=false;}
      return;
    }
    const kind=button.dataset.webCloudConnect;
    const disconnect=button.dataset.webCloudDisconnect;
    if(!kind&&!disconnect)return;
    button.disabled=true;
    message.textContent='Connexion au fournisseur…';
    link.replaceChildren();
    const readiness=providers.find(row=>row.provider===kind);
    // Do not display a meaningless white window when the OAuth application
    // has not even been registered (the most common failure).
    if(kind && readiness?.configured!==true){
      message.textContent=readiness?.reason||'Connecteur cloud non configuré sur le serveur.';
      section.querySelector('#playback-cloud-developer-options').open=true;
      button.disabled=false;
      return;
    }
    // Browsers block windows opened after an awaited API request. Open now,
    // with meaningful progress instead of a blank page, then redirect when
    // the authorization URL has been validated by the backend.
    const popup=kind?window.open('about:blank','_blank',
      'popup,width=620,height=760'):null;
    if(popup){
      try {
        popup.document.open();
        popup.document.write('<!doctype html><html lang="fr"><meta charset="utf-8">'+
          '<title>Connexion cloud</title><body style="font:16px system-ui;padding:2rem">'+
          '<h2>Connexion sécurisée</h2><p>Préparation de la page du fournisseur…</p>'+
          '<p>Cette fenêtre affichera le site de connexion officiel.</p></body></html>');
        popup.document.close();
      } catch(_) { /* Never abort OAuth because the opener is cross-origin. */ }
    }
    try {
      if(disconnect){
        await api('playback/cloud/'+encodeURIComponent(disconnect),{method:'DELETE'});
        await load();
        return;
      }
      const label=playbackCloudProviders.find(([value])=>value===kind)?.[1]||kind;
      const existingId=button.dataset.cloudPlaceId;
      let place=existingId?locations.find(row=>row.id===existingId&&row.kind===kind):null;
      if(existingId&&!place)throw new Error('Compte cloud introuvable.');
      if(!place){
        place=await api('playback/locations',{
          method:'POST',body:JSON.stringify({
            name:label+' '+(locations.filter(row=>row.kind===kind).length+1),
            kind,target:''
          })
        });
      }
      const auth=await api('playback/cloud/'+encodeURIComponent(place.id)+'/authorize',{method:'POST'});
      const target=new URL(auth.authorization_url);
      if(target.protocol!=='https:')throw new Error('URL OAuth non sécurisée.');
      const callbackURL=target.searchParams.get('redirect_uri');
      const callback=callbackURL?new URL(callbackURL):null;
      if(!callback||callback.protocol!=='https:')
        throw new Error('URL HTTPS de retour OAuth absente ou incorrecte.');
      allowedCallbackOrigins.add(callback.origin);
      if(popup && !popup.closed) {
        popup.location.replace(target.href);
        message.textContent='Autorisez votre compte dans la fenêtre '+label+'. La connexion sera détectée automatiquement.';
      } else {
        const anchor=document.createElement('a');
        anchor.href=target.href;
        anchor.target='_blank';
        anchor.rel='noopener noreferrer';
        anchor.textContent='Ouvrir la connexion '+label;
        link.replaceChildren(anchor);
        message.textContent='Autorisez le compte, puis actualisez les connexions.';
      }
    } catch(e) {
      const explanation=e.message||String(e);
      message.textContent=explanation;
      // A failed login is actionable. Keep the popup open with the error,
      // rather than closing it so fast that the user sees only a flash.
      if(popup && !popup.closed){
        try {
          popup.document.open();
          popup.document.write('<!doctype html><html lang="fr"><meta charset="utf-8">'+
            '<title>Connexion impossible</title><body style="font:16px system-ui;padding:2rem">'+
            '<h2>Connexion impossible</h2><p>'+esc(explanation)+'</p>'+
            '<p>Corrigez la configuration, puis réessayez.</p></body></html>');
          popup.document.close();
        } catch(_) { /* A cross-origin provider page may deny access. */ }
      }
      if(/OAuth|configuration|client|retour/i.test(explanation)) {
        section.querySelector('#playback-cloud-developer-options').open=true;
      }
    }
    finally {button.disabled=false;}
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    const data=new FormData(form),provider=String(data.get('provider')||'');
    const payload={
      client_id:String(data.get('client_id')||'').trim(),
      client_secret:String(data.get('client_secret')||''),
      redirect_url:String(data.get('redirect_url')||'').trim()
    };
    try{
      await api('playback/cloud/apps/'+encodeURIComponent(provider),{
        method:'PUT',body:JSON.stringify(payload)
      });
      form.hidden=true;
      await load();
      message.textContent='Application OAuth enregistrée. Vous pouvez maintenant connecter votre compte.';
    }catch(e){message.textContent=e.message;}
  });
  await load();
}

// Preserve the existing settings forms and their event handlers while presenting
// them in four accessible panes. A rerender keeps the currently selected pane.

const playbackNetworkKinds=[
  ['smb','Samba / SMB'],['nfs','NFS'],['ftp','FTP'],['sftp','SFTP'],
  ['upnp','UPnP / DLNA'],['http','HTTP / HTTPS']
];
async function renderPlaybackNetworkSettings(){
  const root=$('connection-content');
  if(!root)return;
  const section=document.createElement('section');
  section.id='playback-network-web-settings';
  section.className='metadata-status';
  section.innerHTML='<h3>Emplacements réseau enregistrés</h3>'+
    '<p class="hint">Les adresses des sources sont copiées vers iOS. Les mots de passe web restent dans le coffre chiffré du serveur et ne sont jamais envoyés à l’iPhone.</p>'+
    '<div id="playback-network-web-list"></div>'+
    '<form id="playback-network-web-form" class="connection-form">'+
    '<input type="hidden" name="id">'+
    '<label>Nom<input name="name" required maxlength="120" placeholder="NAS · Films"></label>'+
    '<label>Protocole<select name="kind">'+playbackNetworkKinds.map(([k,n])=>'<option value="'+esc(k)+'">'+esc(n)+'</option>').join('')+'</select></label>'+
    '<label data-network-field="host"><span data-network-host-label>Adresse IP</span><input name="host" maxlength="255" placeholder="192.168.1.20"></label>'+
    '<label data-network-field="hostname">Nom d’hôte (alias Kodi, facultatif)<input name="hostname" maxlength="255" placeholder="MON-NAS" autocomplete="off"><small class="hint">Nom utilisé dans les chemins SMB/NFS Kodi ; la connexion utilise l’adresse IP ci-dessus.</small></label>'+
    '<label data-network-field="port">Port<input name="port" type="number" min="1" max="65535" placeholder="445"></label>'+
    '<label data-network-field="share">Partage / dossier<input name="share" placeholder="Films"></label>'+
    '<label data-network-field="username">Utilisateur<input name="username" autocomplete="off"></label>'+
    '<label data-network-field="workgroup">Domaine / WORKGROUP<input name="workgroup" placeholder="WORKGROUP"></label>'+
    '<label data-network-field="password">Mot de passe serveur<input name="password" type="password" autocomplete="new-password" placeholder="Vide : conserver"></label>'+
    '<label data-network-field="target" class="wide">URL<input name="target" placeholder="https://nas.local/video.mp4"></label>'+
    '<p class="hint wide">Pour SFTP, renseignez un utilisateur et un mot de passe sur ce serveur : les identifiants du trousseau iOS ne sont pas synchronisés. Utilisez HTTPS pour accéder aux réglages web. Laisser le mot de passe vide conserve la valeur déjà enregistrée.</p>'+
    '<div class="wide connection-actions"><button type="submit" class="primary">Enregistrer l’emplacement</button>'+
    '<button type="button" class="secondary" data-network-reset>Nouvel emplacement</button></div>'+
    '<p id="playback-network-web-result" class="wide" role="status" aria-live="polite"></p>'+
    '</form>';
  root.prepend(section);
  const list=section.querySelector('#playback-network-web-list');
  const form=section.querySelector('form');
  const feedback=section.querySelector('#playback-network-web-result');
  const fields=form.elements;
  let locations=[];
  function adapt(){
    const kind=fields.kind.value;
    for(const row of form.querySelectorAll('[data-network-field]')){
      const field=row.dataset.networkField;
      row.hidden=field==='target'?!['http','upnp'].includes(kind):
        field==='hostname'?!['smb','nfs'].includes(kind):
        field==='host'||field==='port'?['http','upnp'].includes(kind):
        field==='workgroup'?kind!=='smb':
        field==='username'||field==='password'?kind==='nfs':false;
    }
    if(kind==='smb'&&!fields.port.value)fields.port.value='445';
    form.querySelector('[data-network-host-label]').textContent=['smb','nfs'].includes(kind)?'Adresse IP':'Hôte ou adresse IP';
    fields.host.required=!['http','upnp'].includes(kind);
    fields.target.required=['http','upnp'].includes(kind);
  }
  function reset(){
    form.reset();
    fields.id.value='';
    feedback.textContent='';
    adapt();
  }
  async function load(){
    try{locations=await api('playback/locations');}
    catch(e){feedback.textContent=e.message;return;}
    const network=locations.filter(x=>playbackNetworkKinds.some(([kind])=>kind===x.kind));
    list.innerHTML=network.map(loc=>
      '<div class="connection-card"><div><strong>'+esc(loc.name)+'</strong>'+
      '<p class="hint">'+esc(loc.kind.toUpperCase())+' · '+esc(loc.target||loc.host||'')+
      (loc.hostname?' · Nom d’hôte Kodi : '+esc(loc.hostname):'')+
      (loc.has_password?' · Mot de passe serveur enregistré':loc.kind==='sftp'?' · Mot de passe serveur manquant':'')+'</p></div>'+
      '<div class="connection-actions">'+
      '<button type="button" data-network-edit="'+esc(loc.id)+'">Modifier</button>'+
      '<button type="button" class="secondary" data-network-delete="'+esc(loc.id)+'">Supprimer</button>'+
      '</div></div>'
    ).join('')||'<p class="hint">Aucun emplacement réseau enregistré.</p>';
    for(const button of list.querySelectorAll('button')){
      button.disabled=state.role!=='admin';
    }
  }
  fields.kind.addEventListener('change',adapt);
  form.querySelector('[data-network-reset]').addEventListener('click',reset);
  list.addEventListener('click',async event=>{
    const edit=event.target.closest('[data-network-edit]');
    const remove=event.target.closest('[data-network-delete]');
    if(edit){
      const loc=locations.find(x=>x.id===edit.dataset.networkEdit);
      if(!loc)return;
      reset();
      for(const key of ['id','name','kind','host','hostname','port','share','username','workgroup','target']){
        if(fields[key])fields[key].value=loc[key]??'';
      }
      adapt();
      form.scrollIntoView({behavior:'smooth',block:'nearest'});
    }else if(remove){
      const id=remove.dataset.networkDelete;
      if(!localizedConfirm('Supprimer cet emplacement réseau ? Aucun fichier multimédia ne sera supprimé.'))return;
      try{await api('playback/locations/'+encodeURIComponent(id),{method:'DELETE'});reset();await load();}
      catch(e){feedback.textContent=e.message;}
    }
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    feedback.textContent='Enregistrement…';
    if(state.role!=='admin')return;
    const id=fields.id.value;
    const kind=fields.kind.value;
    const payload={
      name:fields.name.value.trim(),kind,
      target:fields.target.value.trim(),
      host:fields.host.value.trim(),
      hostname:['smb','nfs'].includes(kind)?fields.hostname.value.trim():'',
      port:fields.port.value?Number(fields.port.value):null,
      share:fields.share.value.trim(),
      username:fields.username.value.trim(),
      workgroup:fields.workgroup.value.trim()
    };
    if(!['http','upnp'].includes(kind)){
      const host=payload.host.includes(':')&&!payload.host.startsWith('[')
        ? '['+payload.host+']':payload.host;
      payload.target=kind+'://'+host+(payload.port?':'+payload.port:'')+
        '/'+payload.share.replace(/^\/+|\/+$/g,'');
    }
    if(fields.password.value)payload.password=fields.password.value;
    try{
      await api(id?'playback/locations/'+encodeURIComponent(id):'playback/locations',{
        method:id?'PUT':'POST',body:JSON.stringify(payload)
      });
      reset();
      await load();
      feedback.textContent='Emplacement enregistré.';
    }catch(e){feedback.textContent=e.message;}
  });
  reset();
  if(state.role!=='admin')form.querySelector('button[type=submit]').disabled=true;
  await load();
}

let selectedConnectionTab='ios';
function organizeConnectionSettingsTabs() {
  const root=$('connection-content');
  if(!root)return;
  const tabDefinitions=[
    ['ios','Appairage / iOS'],
    ['sources','Sources et synchronisation'],
    ['ai','IA'],
    ['network','Réseau et cloud']
  ];
  const navigation=document.createElement('div');
  navigation.className='connection-tab-nav';
  navigation.setAttribute('role','tablist');
  navigation.setAttribute('aria-label','Catégories des paramètres');
  const panels=new Map();
  for(const [id,label] of tabDefinitions) {
    const button=document.createElement('button');
    button.type='button';
    button.className='connection-tab-button';
    button.id='connection-tab-'+id;
    button.setAttribute('role','tab');
    button.setAttribute('aria-controls','connection-panel-'+id);
    button.textContent=label;
    navigation.append(button);
    const panel=document.createElement('section');
    panel.className='connection-tab-panel';
    panel.id='connection-panel-'+id;
    panel.setAttribute('role','tabpanel');
    panel.setAttribute('aria-labelledby',button.id);
    panel.tabIndex=0;
    panels.set(id,panel);
  }
  let category='ios';
  for(const element of [...root.children]) {
    if(element.id==='playback-cloud-web-settings' ||
       element.id==='playback-network-web-settings') {
      panels.get('network').append(element);
      continue;
    }
    if(element.matches('h3.connection-subheading')) {
      const heading=element.textContent.toLocaleLowerCase('fr');
      if(heading.includes('appairage')) category='ios';
      else if(heading.includes('intelligente')||heading.includes('métadonnées et portraits')) category='ai';
      else if(heading.includes('catalogue')||heading.includes('synchronisation')||
              heading.includes('collection')||heading.includes('sauvegarde')||
              heading.includes('sources')) category='sources';
    }
    panels.get(category).append(element);
  }
  function selectTab(id,focus=false) {
    if(!panels.has(id))return;
    selectedConnectionTab=id;
    for(const [key] of tabDefinitions) {
      const button=navigation.querySelector('#connection-tab-'+key);
      const active=key===id;
      button.setAttribute('aria-selected',String(active));
      button.tabIndex=active?0:-1;
      panels.get(key).hidden=!active;
      if(focus&&active)button.focus();
    }
  }
  const buttons=[...navigation.querySelectorAll('[role="tab"]')];
  for(let i=0;i<buttons.length;i++) {
    buttons[i].addEventListener('click',()=>selectTab(tabDefinitions[i][0]));
    buttons[i].addEventListener('keydown',event=>{
      let next=null;
      if(event.key==='ArrowRight')next=(i+1)%buttons.length;
      if(event.key==='ArrowLeft')next=(i+buttons.length-1)%buttons.length;
      if(event.key==='Home')next=0;
      if(event.key==='End')next=buttons.length-1;
      if(next===null)return;
      event.preventDefault();
      selectTab(tabDefinitions[next][0],true);
    });
  }
  root.append(navigation,...panels.values());
  selectTab(selectedConnectionTab);
}

async function connections() {
  try {
    const [sources,players,status,devices,aiSettings]=await Promise.all([api('sources'),api('players'),api('status'),api('pairing/devices'),state.role==='admin'?api('ai/settings'):Promise.resolve(null)]);state.facets.sources=sources;state.players=players;state.tmdbAvailable=status.tmdb_available===true;
    const collections=sources.filter(s=>collectionTypes.includes(s.type)); const kodi=sources.filter(s=>s.type==='kodi');
    const sourceJobs=new Map();
    for(const job of state.jobs)if(!sourceJobs.has(job.source_id))sourceJobs.set(job.source_id,job);
    $('connection-content').innerHTML=`<section class="metadata-status"><strong>Version du serveur : ${esc(status.version||'—')}</strong>${status.data_model_version?' · Modèle de données '+esc(status.data_model_version):''}</section><h3 class="connection-subheading">Appairage de l'application iOS</h3><section class="metadata-status"><p>Générez un code à 8 chiffres, valable 5 minutes et utilisable une seule fois. Il remplace la saisie du jeton API dans l'application.</p><label>Autorisations du nouvel appareil <select id="pairing-role"><option value="admin">Administrateur</option><option value="read">Lecture seule</option></select></label><p><button id="pairing-start" class="primary" type="button">Générer un code d'appairage</button></p><p id="pairing-code" class="pairing-code" role="status" aria-live="polite"></p><div id="pairing-qr" hidden><img id="pairing-qr-image" alt="QR code sécurisé d'appairage HTTPS" width="220" height="220"><p class="hint">Sur iOS, appuyez sur « Scanner le QR HTTPS ». Le code et l'identité TLS sont vérifiés automatiquement. Aucun certificat à installer.</p><p class="hint" id="pairing-fingerprint"></p></div><p class="hint">Le code manuel à 8 chiffres continue de fonctionner sur les anciens clients. Scannez le QR pour activer l'épinglage TLS sécurisé.</p>${devices.length?`<p class="hint">Appareils appairés</p><div class="paired-devices">${devices.map(device=>`<div class="connection-card"><span><strong>${esc(device.name)}</strong> · ${device.role==='admin'?'Administrateur':'Lecture seule'}</span><button type="button" class="secondary" data-revoke-device="${esc(device.token_hash)}">Révoquer</button></div>`).join('')}</div>`:''}</section><h3 class="connection-subheading">Recherche intelligente · IA</h3><section class="metadata-status"><form id="ai-settings-form" class="connection-form">
<label>Fournisseur IA<select name="provider" id="ai-language-provider">${aiProviderOptions(aiSettings?.provider||'openai',false)}</select></label>
<span data-ai-language-model>${aiSettingsModelPicker('model',aiSettings?.model||'gpt-4o-mini',aiSettings?.provider||'openai')}</span>
<p class="hint wide">OpenAI : recherche vectorielle hybride inchangée. Claude et Gemini : recherche locale FTS5 et vérification des résultats par le seul modèle sélectionné, sans embeddings tiers.</p>
<span class="wide" data-openai-vectors ${(aiSettings?.provider||'openai')==='openai'?'':'hidden'}>
  ${aiEmbeddingModelPicker(aiSettings?.embedding_model||'text-embedding-3-small')}
  <p class="hint">L'index de vecteurs OpenAI est conservé. Changer de modèle d'embeddings déclenche une réindexation des résumés concernés.</p>
  <p class="hint">Index vectoriel : <strong id="ai-index-progress">${aiSettings?.index?.indexed??0} / ${aiSettings?.index?.total??0}</strong> médias · ${aiSettings?.index?.running?'indexation en cours':'à jour ou en attente'}.</p>
  <button type="button" class="secondary" id="ai-reindex">Indexer / actualiser</button>
</span>
<label class="wide" id="ai-language-key-label">Clé API · ${esc(aiProviderName(aiSettings?.provider||'openai'))}<input name="api_key" type="password" autocomplete="new-password" placeholder="${aiSettings?.has_api_key?'Clé enregistrée · vide pour conserver':'Clé API requise'}"></label>
<label class="check-label wide"><input type="checkbox" name="clear_api_key">Supprimer la clé du fournisseur sélectionné</label>
<p class="hint wide">Les clés restent sur le serveur. Les vecteurs OpenAI existants sont réutilisés si le modèle d'embeddings est inchangé.</p>
<label class="wide">Plafond mensuel IA (USD)<input name="monthly_budget_usd" type="number" min="0" max="10000" step="0.01" required value="${aiSettings?.monthly_budget_usd??5}"></label>
<p class="hint wide">Plafond de consommation estimé. Les modèles personnalisés sans tarif connu sont bloqués pour éviter les dépenses non maîtrisées.</p>
<div class="wide metadata-status" id="ai-usage-summary">${aiUsageMarkup(aiSettings?.usage)}</div>
<button type="button" class="secondary wide" id="ai-refresh-usage">Actualiser la consommation</button>
<button type="submit" class="primary wide">Enregistrer l’IA</button>
<p class="hint wide" id="ai-settings-result" role="status"></p>
<button type="button" class="secondary wide" id="ai-test-connection" ${aiSettings?.configured?'':'disabled'}>Tester le modèle IA</button>
<p class="hint wide">Teste le modèle sélectionné, et aussi les embeddings si OpenAI est actif. Enregistrez les changements avant le test.</p>
<div class="wide" id="ai-connection-result" role="status" aria-live="polite"></div></form></section><h3 class="connection-subheading">Métadonnées et portraits · TMDB</h3><section class="metadata-status"><p>${state.tmdbAvailable?'TMDB est configuré. Les fiches récupèrent automatiquement le casting, les portraits et les bandes-annonces disponibles.':'TMDB n’est pas configuré. Renseignez TMDB_API_TOKEN dans .env, puis relancez rebuild.sh.'}</p><p class="hint">Utilisez le jeton de lecture API ou la clé API v3 depuis <a href="https://www.themoviedb.org/settings/api" target="_blank" rel="noopener noreferrer">les paramètres API de votre compte TMDB ↗</a>. Le secret reste sur le serveur. Il n’est pas nécessaire de réimporter la bibliothèque pour activer TMDB.</p><button id="test-tmdb" class="secondary" ${state.tmdbAvailable?'':'disabled'}>Tester la connexion TMDB</button><p id="tmdb-test-result" class="hint play-status" role="status"></p></section><h3 class="connection-subheading">Sauvegarde JSON de la bibliothèque</h3><section class="metadata-status"><p>Exportez les médias, notes, visionnages et listes pour restaurer le catalogue sur ce serveur ou un autre. Les mots de passe et jetons d’appairage ne sont pas exportés.</p><div class="connection-actions"><button type="button" class="secondary" id="backup-export">Exporter en JSON</button><label class="secondary backup-file-picker" for="backup-import-file">Importer un JSON</label><input type="file" id="backup-import-file" accept=".json,application/json" hidden></div><p class="hint">L’import remplace tout le catalogue et ses listes. Les appareils appairés sur ce serveur restent autorisés. Les vignettes sont régénérées.</p></section><h3 class="connection-subheading">Maintenance de la base du catalogue</h3><section class="metadata-status"><p>Analyse et nettoyage des doublons de sources (connexion MariaDB/Kodi ou type CLZ, My Movies, Filmotech) et des enregistrements orphelins. Kodi MyVideos n’est jamais modifié.</p><div class="connection-actions"><button type="button" class="secondary" id="catalogue-db-preview">Analyser la base</button><button type="button" class="secondary" id="kodi-catalogue-clean" ${state.role==='admin'?'':'disabled'}>Nettoyer Kodi puis le catalogue</button><button type="button" class="secondary" id="catalogue-db-clean" ${state.role==='admin'?'':'disabled'}>Nettoyer la base</button><button type="button" id="catalogue-db-reset" ${state.role==='admin'?'':'disabled'}>Réinitialiser la base</button></div><p id="catalogue-db-preview-result" class="hint" role="status" aria-live="polite"></p><p class="hint">La réinitialisation efface le catalogue du serveur, ses sources, listes, notes et historiques de synchronisation. Les autorisations d’accès et emplacements de lecture réseau sont conservés.</p></section><h3 class="connection-subheading">Sources du catalogue</h3>${sources.map(s=>`<div class="connection-card${s.type==='kodi'?' kodi-source-card':''}"><div><strong>${sourceIcon(s.type)} ${esc(s.name)}</strong><p>${s.type==='kodi'?'Kodi JSON-RPC · MariaDB facultatif':sourceName(s.type)} · ${['discogs','clz_music','music_csv'].includes(s.type)?`${s.music_count||0} albums`:`${s.item_count} entrées`}${s.last_sync?` · ${date(s.last_sync)}`:''}${!s.enabled?' · Désactivée':''}</p>${sourceJobs.get(s.id)?.result?.database?`<p class="hint">Base synchronisée : <strong>${esc(sourceJobs.get(s.id).result.database)}</strong> · ${s.config?.database?'sélection explicite':'détection automatique'}</p>`:''}${sourceJobs.get(s.id)&&['pending','running'].includes(sourceJobs.get(s.id).status)?`<div class="source-progress">${jobProgressMarkup(sourceJobs.get(s.id))}</div>`:''}${s.last_error && String(s.last_error).trim()!==String(sourceJobs.get(s.id)?.error||'').trim()?`<p class="error-text">${esc(s.last_error)}</p>`:''}</div><div class="connection-actions">${s.type==='kodi'?`<button type="button" data-sync="${esc(s.id)}">Synchroniser la médiathèque</button><button type="button" data-scan-kodi="${esc(s.id)}">Analyser Kodi + synchroniser</button><button data-edit-source="${esc(s.id)}">Modifier</button><button data-writeback-history="${esc(s.id)}">Historique des écritures</button>`:s.type==='mymovies'?`<button data-configure-mymovies="${esc(s.id)}">Configurer l’API</button>`:''}<button data-remove-source="${esc(s.id)}">Retirer</button></div>${s.type==='kodi'?`<section class="source-kodi-instances" data-kodi-instances-for="${esc(s.id)}"><div class="source-kodi-instances-heading"><strong>Instances Kodi · JSON-RPC</strong><button type="button" class="secondary" data-add-player-source="${esc(s.id)}">+ Ajouter une instance</button></div>${players.filter(p=>p.source_id===s.id).map(p=>`<div class="connection-card kodi-instance-row"><div><strong>${esc(p.name)}</strong><p>${esc(p.host)}:${esc(p.port)} · HTTP JSON-RPC</p><p id="player-result-${esc(p.id)}" class="hint play-status" role="status"></p></div><div class="connection-actions"><button type="button" data-test-player="${esc(p.id)}">Tester</button><button type="button" data-edit-player="${esc(p.id)}">Modifier</button><button type="button" data-remove-player="${esc(p.id)}">Retirer</button></div></div>`).join('')||'<p class="hint">Aucune instance associée. Ajoutez un lecteur Kodi pour utiliser la télécommande.</p>'}</section>`:''}</div>`).join('')||'<p class="hint">Aucune source configurée.</p>'}<details><summary class="text-button">+ Connecter une instance Kodi</summary><form id="kodi-form" class="connection-form"><input type="hidden" name="id"><input type="hidden" name="player_id"><label class="wide">Nom<input name="name" required placeholder="Kodi — Salon"></label><p class="hint wide">Connexion principale : JSON-RPC. Kodi peut utiliser sa base locale SQLite. MariaDB est facultatif.</p><label>Hôte Kodi JSON-RPC (facultatif)<input name="kodi_host" placeholder="192.168.1.20"></label><label>Port HTTP Kodi<input name="kodi_port" type="number" min="1" max="65535" value="8080" required></label><label>Utilisateur HTTP<input name="kodi_username" autocomplete="off"></label><label>Mot de passe HTTP<input name="kodi_password" type="password" autocomplete="new-password" placeholder="Vide : conserver le mot de passe"></label><label>Port WebSocket<input name="websocket_port" type="number" min="1" max="65535" value="9090"></label><label>Adresse MAC · Wake-on-LAN<input name="kodi_mac" placeholder="AA:BB:CC:DD:EE:FF"></label><label class="check-label"><input name="kodi_tls" type="checkbox">HTTPS Kodi</label><button type="button" class="secondary wide" data-test-primary-kodi>Tester Kodi JSON-RPC</button><p id="kodi-primary-result" class="connection-result wide" role="status" aria-live="polite" hidden></p><details class="wide mariadb-fallback-settings"><summary>Accès MariaDB facultatif (Kodi éteint)</summary><p class="hint">Lorsque Kodi ne répond pas, le serveur lit cette base après une seconde maximum. Une autorisation d'écriture séparée est nécessaire pour mettre à jour les états distants.</p><div class="connection-form"><label>Hôte MariaDB<input name="host" placeholder="192.168.1.10"></label><label>Port SQL<input name="port" type="number" min="1" max="65535" value="3306"></label><label>Base MyVideos<input name="database" placeholder="Automatique, ou MyVideos…"></label><div class="wide mariadb-database-picker"><button type="button" class="secondary" data-discover-mariadb>Rechercher les bases MyVideos</button><select id="mariadb-database-choices" aria-label="Bases MyVideos disponibles" hidden><option value="">Sélectionner une base…</option></select><p id="mariadb-database-result" class="hint" role="status" aria-live="polite"></p></div><label>Utilisateur MariaDB<input name="username" autocomplete="off" placeholder="home_media_catalog"></label><label>Mot de passe<input name="password" type="password" autocomplete="new-password"></label><label class="check-label wide"><input name="tls" type="checkbox">Connexion MariaDB avec TLS</label><label class="check-label wide"><input name="mariadb_write_enabled" type="checkbox">Autoriser le repli en écriture MariaDB si Kodi JSON-RPC est inaccessible</label><p class="hint wide">Le repli en lecture est disponible dès que MariaDB est configuré. Le repli en écriture reste désactivé par défaut et nécessite des droits UPDATE. Une base vide utilise automatiquement le MyVideos le plus récent, y compris pour les écritures. Choisissez explicitement la bonne base si plusieurs sont présentes. Une écriture SQL ne produit pas de notification Kodi.</p><p class="hint wide">Le repli MariaDB ne nécessite qu'un compte SELECT pour importer lorsque Kodi est éteint.</p><button class="secondary wide" type="button" data-test-connection="kodi-form">Tester le repli MariaDB</button><p id="kodi-form-result" class="connection-result wide" role="status" aria-live="polite" hidden></p></div></details><button class="primary wide" type="submit">Enregistrer et synchroniser</button></form></details><h3 class="connection-subheading">Importer une collection</h3><form id="collection-form" class="upload-row"><label>Type de collection<select name="collection_type">${collectionTypes.map(type=>`<option value="${type}">${sourceName(type)}</option>`).join('')}</select></label><label>Collection<select name="source_id" aria-label="Collection"><option value="">Nouvelle collection…</option></select></label><input name="file" type="file" accept=".xml,application/xml,text/xml" required aria-label="Export XML de la collection"><button class="secondary" type="submit">Importer l’export XML</button><p class="hint">Un nouvel import remplace uniquement la collection choisie et conserve les autres sources. Utilisez un export XML Filmotech ou My Movies Collection Management.</p></form><details id="mymovies-api-details"><summary class="text-button">+ My Movies : synchronisation des visionnages (API professionnelle)</summary><form id="mymovies-api-form" class="connection-form"><label>Collection My Movies<select name="source_id" required>${collections.filter(s=>s.type==='mymovies').map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')}</select></label><label>Hôte du serveur My Movies<input name="remote_host" placeholder="192.168.1.10"></label><label>Port Remote Data API<input name="remote_port" type="number" min="1" max="65535" value="51414"></label><label>Clé API<input name="remote_api_key" type="password" autocomplete="new-password" placeholder="Vide = conserver la clé existante"></label><label class="check-label wide"><input type="checkbox" name="remote_enabled">Activer l’écriture vu/non vu</label><p class="hint wide">API Remote Server professionnelle de My Movies, réservée aux installations autorisées. L’export XML est conservé en lecture seule. Aucun transfert des notes personnelles n’est possible via cette API. La clé transite via HTTP : à utiliser sur un réseau de confiance uniquement.</p><button type="submit" class="primary wide">Enregistrer la configuration</button></form></details><h3 class="connection-subheading">Synchronisations vers Kodi et services tiers</h3><section class="metadata-status"><p id="writeback-summary" role="status">Chargement des synchronisations...</p><p class="hint">Seules les modifications explicites d’une fiche déclenchent une écriture distante. Les imports et actualisations de catalogue restent en lecture seule.</p><div id="writeback-queue"></div><details class="writeback-recent-details"><summary>Synchronisations distantes récentes</summary><div id="writeback-recent" aria-live="polite"></div></details><div id="writeback-source-history" class="writeback-source-history" hidden></div><button id="retry-all-writebacks" class="secondary" type="button" ${state.role==='admin'?'':'disabled'}>Rejouer toutes les synchronisations en échec</button></section><div id="job-status" class="job-status" role="status" hidden></div>${kodi.length?`<details id="player-details" class="source-player-editor" hidden><summary class="text-button">+ Ajouter une instance Kodi</summary><form id="player-form" class="connection-form"><input type="hidden" name="id"><label>Nom<input name="name" required placeholder="Kodi — Salon"></label><label>Médiathèque Kodi associée<select name="source_id">${kodi.map(s=>`<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('')}</select></label><p class="hint wide">Cette connexion JSON-RPC appartient à l’instance Kodi. La source associée définit l’accès MariaDB, sans dupliquer les identifiants HTTP. Plusieurs instances peuvent partager la même source.</p><label>Hôte Kodi<input name="host" required placeholder="192.168.1.20"></label><label>Port HTTP<input name="port" type="number" value="8080" min="1" max="65535" required></label><label>Port WebSocket Kodi (notifications de scan)<input name="websocket_port" type="number" value="9090" min="1" max="65535" required></label><label>Adresse MAC · Wake-on-LAN<input name="mac" placeholder="AA:BB:CC:DD:EE:FF"></label><label>Utilisateur HTTP<input name="username" autocomplete="off"></label><label>Mot de passe HTTP<input name="password" type="password" autocomplete="new-password"></label><label class="check-label wide"><input name="tls" type="checkbox">HTTPS sur Kodi</label><p class="hint wide">Ce formulaire contacte l’API HTTP de Kodi, pas MariaDB. Kodi doit être démarré, avec le contrôle distant HTTP activé, et utiliser la base MyVideos choisie.</p><button class="secondary wide" type="button" data-test-connection="player-form">Tester la connexion Kodi</button><p id="player-form-result" class="connection-result wide" role="status" aria-live="polite" hidden></p><button type="submit" class="primary wide">Enregistrer l’instance</button></form></details>`:''}`;
    void renderPlaybackCloudSettings();
    void renderPlaybackNetworkSettings();
    organizeConnectionSettingsTabs();
    const importForm=$('collection-form');
    const updateCollections=()=>{
      const type=importForm.elements.collection_type.value;
      importForm.elements.source_id.innerHTML=collections.filter(source=>source.type===type).map(source=>`<option value="${esc(source.id)}">${esc(source.name)}</option>`).join('')+'<option value="">Nouvelle collection…</option>';
    };
    await refreshWritebackQueue();
    importForm.elements.collection_type.addEventListener('change',updateCollections);
    updateCollections();
    showDialog('connections-dialog');
    renderActivity();
  } catch(e) {toast(e.message);}
}
// A single editor moves below the source to which the instance belongs.
function openKodiPlayerEditor(sourceID,player=null) {
  const container=[...document.querySelectorAll('[data-kodi-instances-for]')]
    .find(element=>element.dataset.kodiInstancesFor===sourceID);
  const details=$('player-details'), form=$('player-form');
  if(!container||!details||!form)return;
  const source=state.facets.sources.find(s=>s.id===sourceID&&s.type==='kodi');
  if(!source)return;
  details.hidden=false;
  container.append(details);
  form.reset();
  details.querySelector('summary').textContent=player
    ? 'Modifier une instance Kodi' : '+ Ajouter une instance Kodi';
  fillForm('player-form',player
    ? {...player,password:''}
    : {id:'',name:source.name,source_id:sourceID,host:'',port:8080,
       websocket_port:9090,username:'',password:'',mac:'',tls:false});
  const result=$('player-form-result');
  if(result){result.hidden=true;result.textContent='';}
}
function primaryKodiInput(form) {
  const data=new FormData(form);
  return {name:String(data.get('name')||'').trim(),source_id:data.get('id')||'',
    host:String(data.get('kodi_host')||'').trim(),port:Number(data.get('kodi_port')||8080),
    websocket_port:Number(data.get('websocket_port')||9090),
    username:String(data.get('kodi_username')||'').trim(),password:data.get('kodi_password')||null,
    tls:data.has('kodi_tls'),mac:String(data.get('kodi_mac')||'').trim()};
}
async function testPrimaryKodi(form) {
  const status=$('kodi-primary-result');
  status.hidden=false;status.className='connection-result wide';status.textContent='Connexion JSON-RPC…';
  try{
    const data=primaryKodiInput(form);
    const payload={host:data.host,port:data.port,username:data.username,password:data.password,
      tls:data.tls,websocket_port:data.websocket_port,player_id:form.elements.player_id.value||null};
    const result=await api('connections/kodi/test',{method:'POST',body:JSON.stringify(payload)});
    status.classList.add('success-text');status.textContent=result.message;return result;
  }catch(e){status.classList.add('error-text');status.textContent=e.message;throw e;}
}
function connectionInput(form) {
  const data=new FormData(form);
  const config={host:String(data.get('host')||'').trim(),port:Number(data.get('port')),username:String(data.get('username')||'').trim(),password:data.get('password')||null,tls:data.has('tls')};
  if(form.getAttribute('id')==='kodi-form') return {config:{...config,database:String(data.get('database')||'').trim(),mariadb_write_enabled:data.has('mariadb_write_enabled')},source_id:data.get('id')||null};
  return {...config,websocket_port:Number(data.get('websocket_port')||9090),
     mac:String(data.get('mac')||'').trim(),player_id:data.get('id')||null};
}
async function discoverMariaDBDatabases(form) {
  const feedback=$('mariadb-database-result'),picker=$('mariadb-database-choices');
  const button=form.querySelector('[data-discover-mariadb]');
  const input=connectionInput(form);
  if(!form.elements.namedItem('host').reportValidity()||
     !form.elements.namedItem('username').reportValidity()||
     !form.elements.namedItem('port').reportValidity())return;
  feedback.textContent='Recherche des bases accessibles…';
  picker.hidden=true;
  button.disabled=true;
  try {
    const result=await api('connections/mariadb/databases',{method:'POST',body:JSON.stringify(input)});
    const databases=result.databases||[];
    picker.innerHTML='<option value="">Choisir une base MyVideos…</option>'+
      databases.map(item=>`<option value="${esc(item.name)}">${esc(item.name)} · version ${Number(item.version)}</option>`).join('');
    const current=form.elements.namedItem('database').value;
    if(databases.some(item=>item.name===current))picker.value=current;
    picker.hidden=databases.length===0;
    feedback.textContent=databases.length
      ?`${databases.length} base(s) trouvée(s). Sans choix explicite, l’application utilise ${result.automatic_database} (numéro le plus élevé, non vérifié par JSON-RPC). Choisissez la base de votre instance Kodi.`
      :'Aucune base MyVideos visible pour ce compte MariaDB. Vérifiez les droits d’accès.';
  }catch(e){feedback.textContent='Recherche impossible : '+e.message;}
  finally{button.disabled=false;}
}
async function testConnection(form) {
  const input=connectionInput(form); const formId=form.getAttribute('id');
  const result=$(formId+'-result'); result.hidden=false; result.className='connection-result wide';
  result.textContent='Vérification de la connexion…';
  const controls=[...form.elements].map(control=>({control,disabled:control.disabled}));
  controls.forEach(({control})=>control.disabled=true);
  try {
    const tested=await api(`connections/${formId==='kodi-form'?'mariadb':'kodi'}/test`,{method:'POST',body:JSON.stringify(input)});
    result.textContent=tested.message+(tested.version?` Version ${tested.version}.`:'');
    result.classList.add('success-text'); return tested;
  } catch(e) {result.textContent=e.message;result.classList.add('error-text');throw e;}
  finally {controls.forEach(({control,disabled})=>control.disabled=disabled);}
}
document.addEventListener('input',event=>{
  const form=event.target.closest('#kodi-form,#player-form');
  if(form) {
    const result=$(form.getAttribute('id')+'-result');if(result)result.hidden=true;
    if(form.id==='kodi-form'&&$('kodi-primary-result'))$('kodi-primary-result').hidden=true;
    if(form.id==='kodi-form'&&event.target.name==='database') {
      const picker=$('mariadb-database-choices');
      if(picker&&!picker.hidden&&picker.value!==event.target.value)picker.value='';
    }
  }
});
function fillForm(id,data) {
  const form=$(id); form.closest('details').open=true;
  for(const [key,value] of Object.entries(data)) {
    const el=form.elements.namedItem(key); if(!el) continue;
    if(el.type==='checkbox') el.checked=Boolean(value); else el.value=value??'';
  }
  if(id==='kodi-form'){
    const picker=$('mariadb-database-choices'),feedback=$('mariadb-database-result');
    if(picker){picker.hidden=true;picker.value='';}
    if(feedback)feedback.textContent='';
  }
  form.scrollIntoView({block:'nearest',behavior:'smooth'});
}
document.addEventListener('submit',async event=>{
  const form=event.target; const formId=form.getAttribute('id');
  if(!['login-form','kodi-form','collection-form','player-form','mymovies-api-form'].includes(formId)) return;
  event.preventDefault(); const button=form.querySelector('button[type="submit"]'); button.disabled=true;
  try {
    const data=new FormData(form);
    if(formId==='login-form') {
      await api('auth/session',{method:'POST',body:JSON.stringify({token:data.get('token')})}); form.reset(); $('login-error').textContent=''; await initialize();
    } else if(formId==='kodi-form') {
      const fallback=connectionInput(form).config;
      if(Boolean(fallback.host)!==Boolean(fallback.username))throw new Error('Le repli MariaDB nécessite un hôte et un utilisateur.');
      if(fallback.mariadb_write_enabled&&!fallback.host)throw new Error('Écriture MariaDB : configurez un hôte MariaDB. Une base vide utilisera automatiquement le MyVideos le plus récent.');
      const body={name:data.get('name'),type:'kodi',config:fallback};
      const id=data.get('id');
      if(id) {
        // A source owns its MariaDB fallback; player credentials and endpoint
        // belong exclusively to independent Kodi instance records.
        await api(`sources/${encodeURIComponent(id)}`,{method:'PUT',body:JSON.stringify(body)});
        toast('Source Kodi et fallback MariaDB enregistrés. Instances JSON-RPC inchangées.');
        await connections();
      } else {
        const primary=primaryKodiInput(form);
        // Zero or more Kodi instances per source. Initial player creation
        // is a convenience, not a required relationship.
        if(primary.host)await testPrimaryKodi(form);
        const source=await api('sources',{method:'POST',body:JSON.stringify(body)});
        if(primary.host){
          await api('players',{method:'POST',body:JSON.stringify({...primary,source_id:source.id})});
          await startKodiRemote({api,url,role:state.role});
        }
        if(primary.host||fallback.host){
          await pollJob(await api(`sources/${source.id}/sync`,{method:'POST'}));
        }else{
          toast('Médiathèque Kodi créée. Ajoutez une instance JSON-RPC ou un fallback MariaDB pour synchroniser.');
          await connections();
        }
      }
    } else if(formId==='mymovies-api-form') {
      const s=state.facets.sources.find(x=>x.id===data.get('source_id')&&x.type==='mymovies');
      if(!s) throw new Error('Choisissez une collection My Movies existante.');
      const config={remote_host:String(data.get('remote_host')||'').trim(),remote_port:Number(data.get('remote_port')||51414),remote_api_key:data.get('remote_api_key')||null,remote_enabled:data.has('remote_enabled')};
      if(config.remote_enabled&&(!config.remote_host||(!config.remote_api_key&&!s.config.has_remote_api_key)))throw new Error('Hôte et clé API nécessaires pour activer la synchronisation.');
      await api(`sources/${encodeURIComponent(s.id)}`,{method:'PUT',body:JSON.stringify({name:s.name,type:'mymovies',enabled:s.enabled,config})});
      toast('Configuration de l’API My Movies enregistrée.');await connections();
    } else if(formId==='collection-form') {
      let id=data.get('source_id');
      const type=data.get('collection_type');
      if(!collectionTypes.includes(type)) throw new Error('Unsupported collection type');
      if(!id) { const name=localizedPrompt('Nom de cette collection',sourceName(type)); if(!name) return; const source=await api('sources',{method:'POST',body:JSON.stringify({name,type})}); id=source.id; }
      const body=new FormData(); body.set('source_id',id); body.set('file',data.get('file'));
      await pollJob(await api('imports/'+type,{method:'POST',body}));
    } else {
      const id=data.get('id'); const {player_id,...config}=connectionInput(form); const body={name:data.get('name'),source_id:data.get('source_id'),...config};
      if(!id) await testConnection(form);
      await api(id?`players/${id}`:'players',{method:id?'PUT':'POST',body:JSON.stringify(body)});
      toast('Instance enregistrée.');
      await connections();
      // Rebuild the remote's player/socket subscriptions after a configuration
      // change; the one-shot initial list is otherwise stale until page reload.
      await startKodiRemote({api,url,role:state.role});
    }
  } catch(e) {if(formId==='login-form') $('login-error').textContent=e.message; else toast(e.message);}
  finally {button.disabled=false;}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('button'); if(!button) return;
  if(button.id==='play-choose-episode'){await choosePlaybackSeriesEpisode();return;}
  if(button.id==='play-back-series'){returnToPlaybackSeries();return;}
  if(button.id==='copy-media-title'){await copyMediaTitle();return;}
  if(button.id==='open-artwork-gallery'){openArtworkGallery();return;}
  if(button.hasAttribute('data-artwork-step')){stepArtworkGallery(Number(button.dataset.artworkStep));return;}
  if(button.hasAttribute('data-test-primary-kodi')){await testPrimaryKodi($('kodi-form'));return;}
  if(button.hasAttribute('data-discover-mariadb')){await discoverMariaDBDatabases($('kodi-form'));return;}
  if(button.dataset.searchPerson){searchLocalPerson(button.dataset.searchPerson,button.dataset.personTmdb,button.dataset.personImdb);return;}
  if(button.hasAttribute('data-episode-action')){
    const rect=button.getBoundingClientRect();
    openEpisodeContextMenu(button,rect.right,rect.bottom+6);
    return;
  }
  if(button.hasAttribute('data-watch-through-episode')){
    const season=Number(button.dataset.watchThroughSeason),episode=Number(button.dataset.watchThroughEpisode);
    closeEpisodeContextMenu();
    await saveEpisodeWatches(true,season,episode);
    return;
  }
  if(button.hasAttribute('data-mark-season')){await saveEpisodeWatches(button.dataset.watched==='true',Number(button.dataset.markSeason));return;}
  if(button.dataset.close) $(button.dataset.close).close();
  if(button.dataset.media) openDetail(button.dataset.media);
  if(button.hasAttribute('data-season')) {
    document.querySelectorAll('.season-button').forEach(b=>b.setAttribute('aria-expanded',String(b===button)));
    loadSeasonEpisodes(Number(button.dataset.season),0,true);
    return;
  }
  if(button.hasAttribute('data-load-season')) {
    loadSeasonEpisodes(Number(button.dataset.loadSeason),Number(button.dataset.offset)||0);
    return;
  }
  if(button.dataset.returnToSeries){
    const seriesId=button.dataset.returnToSeries,season=Number(button.dataset.returnSeason)||0;
    await openDetail(seriesId);
    if(state.detail?.id===seriesId)loadSeasonEpisodes(season,0,true);
    return;
  }
  if(button.dataset.genre) {$('genre').value=button.dataset.genre; $('detail-dialog').close();  loadMedia();}
  if(button.hasAttribute('data-toggle-format')) {
    const value=canonicalFormat(button.dataset.toggleFormat);
    if(state.formats.includes(value))state.formats=state.formats.filter(v=>v!==value);
    else if(state.formats.length<20)state.formats.push(value);
    else {toast('20 formats maximum.');return;}
    updateFormatSelections();loadMedia();return;
  }
  if(button.dataset.action==='connections') connections(); if(button.dataset.action==='reset') reset();
  if(button.dataset.action==='lists')openLists();
  if(button.dataset.openList)openLists(button.dataset.openList);
  if(button.id==='pairing-start') {
    button.disabled=true;
    try {
      const response=await api('pairing/start',{method:'POST',body:JSON.stringify({role:$('pairing-role').value})});
      $('pairing-code').textContent=`${response.code.slice(0,4)} ${response.code.slice(4)} · Valable 5 minutes`;
      if(response.qr_png){
        $('pairing-qr-image').src='data:image/png;base64,'+response.qr_png;
        $('pairing-fingerprint').textContent='Certificat TLS SHA-256 : '+response.fingerprint;
        $('pairing-qr').hidden=false;
      }
    }catch(e){toast(e.message);}finally{button.disabled=false;}
    return;
  }
  if(button.dataset.revokeDevice) {
    if(localizedConfirm('Révoquer cet appareil iOS ? Il devra être appairé de nouveau.')) {
      try {await api('pairing/devices/'+encodeURIComponent(button.dataset.revokeDevice),{method:'DELETE'});await connections();}
      catch(e){toast(e.message);}
    }
    return;
  }
  if(button.dataset.retryWriteback||button.id==='retry-all-writebacks') {
    button.disabled=true;
    try{
      const path=button.dataset.retryWriteback?'writebacks/pending/'+encodeURIComponent(button.dataset.retryWriteback)+'/retry':'writebacks/retry';
      const result=await api(path,{method:'POST'});
      const failures=result.results.filter(entry=>entry.result.status!=='updated').length;
      toast(failures?`${failures} synchronisation(s) restent en échec.`:'Synchronisation(s) rejouée(s).');
      await refreshWritebackQueue();
      if(state.detail){
        const detail=await api('media/'+encodeURIComponent(state.detail.id));
        state.detail.writeback_state=detail.writeback_state||[];
        renderWritebackIndicators();
      }
    }catch(e){toast(e.message);}finally{button.disabled=false;}
    return;
  }
  if(button.dataset.closeWritebackHistory){
    const target=$('writeback-source-history');target.hidden=true;target.replaceChildren();return;
  }
  if(button.dataset.quick) {state.recent=button.dataset.quick==='recent'; $('watch-status').value=['all','recent'].includes(button.dataset.quick)?'':button.dataset.quick;  if(state.recent){$('sort').value='date_added';state.order='desc';$('sort-order').textContent='↓ Décroissant';}loadMedia();}
  if(!button.dataset.testConnection&&!button.dataset.sync&&!button.dataset.removeSource&&!button.dataset.removePlayer&&!button.dataset.editSource&&!button.dataset.editPlayer&&!button.dataset.addPlayerSource&&!button.dataset.configureMymovies&&!button.dataset.scanKodi&&!button.dataset.writebackHistory&&!button.dataset.testPlayer&&!['play-button','play-browser','test-tmdb'].includes(button.id))return;
  const result=['play-button','play-browser'].includes(button.id)?$('play-status'):button.id==='test-tmdb'?$('tmdb-test-result'):button.dataset.testPlayer?$(`player-result-${button.dataset.testPlayer}`):null;
  if(result){result.className='hint play-status';result.textContent=button.id==='play-button'?'Envoi de la lecture à Kodi…':button.id==='play-browser'?'Chargement du flux vidéo via Kodi…':'Vérification de la connexion…';}
  try {
    if(button.dataset.testConnection) {const form=$(button.dataset.testConnection); const fields=['host','port',...(form.getAttribute('id')==='kodi-form'?['username']:[])];if(fields.every(name=>form.elements.namedItem(name).reportValidity())) await testConnection(form);}
    if(button.dataset.sync) {button.disabled=true;toast('Synchronisation Kodi demandée (JSON-RPC, ou MariaDB si indisponible).');await pollJob(await api(`sources/${button.dataset.sync}/sync`,{method:'POST'}));}
    if(button.dataset.scanKodi) {button.disabled=true;const job=await api(`sources/${button.dataset.scanKodi}/scan`,{method:'POST'});toast('Scan Kodi demandé. Synchronisation MariaDB à la fin du scan.');await pollJob(job);}
    if(button.dataset.writebackHistory) {
      const data=await api(`sources/${encodeURIComponent(button.dataset.writebackHistory)}/writeback`);
      const target=$('writeback-source-history');
      const source=state.facets.sources.find(s=>s.id===button.dataset.writebackHistory);
      target.hidden=false;
      target.innerHTML=`<div class="writeback-history-heading"><h4>Historique des écritures · ${esc(source?.name||'Source')}</h4><button type="button" class="secondary" data-close-writeback-history>Fermer</button></div>${data.length?'<ul class="writeback-history-list">'+data.map(item=>writebackHistoryRow(item,true)).join('')+'</ul>':'<p class="hint">Aucune écriture enregistrée pour cette source.</p>'}`;
      target.scrollIntoView({block:'nearest',behavior:'smooth'});
    }
    if(button.dataset.removeSource && localizedConfirm('Retirer cette source et ses données importées ?')) {
      await api(`sources/${button.dataset.removeSource}`,{method:'DELETE'});
      await refresh();
      await connections();
      // SQLite removes linked Kodi players through its ON DELETE CASCADE FK.
      await startKodiRemote({api,url,role:state.role});
    }
    if(button.dataset.removePlayer && localizedConfirm('Retirer cette instance Kodi ?')) {
      await api(`players/${button.dataset.removePlayer}`,{method:'DELETE'});
      await connections();
      await startKodiRemote({api,url,role:state.role});
    }
    if(button.dataset.editSource) {
      const s=state.facets.sources.find(s=>s.id===button.dataset.editSource);
      if(!s||s.type!=='kodi')return;
      const form=$('kodi-form');
      fillForm('kodi-form',{id:s.id,name:s.name,...s.config,player_id:''});
      // Editing a source must never edit or create a Kodi JSON-RPC instance.
      // Those live in the nested per-source player editor.
      for(const field of ['kodi_host','kodi_port','kodi_username','kodi_password','websocket_port','kodi_tls']) {
        const input=form.elements.namedItem(field);
        if(input) {
          input.required=false;
          input.closest('label').hidden=true;
        }
      }
      const primaryHint=form.querySelector('p.hint.wide');
      if(primaryHint)primaryHint.hidden=true;
      const primaryTest=form.querySelector('[data-test-primary-kodi]');
      if(primaryTest)primaryTest.hidden=true;
      const primaryResult=$('kodi-primary-result');
      if(primaryResult)primaryResult.hidden=true;
      form.querySelector('button[type="submit"]').textContent='Enregistrer la source';
      form.querySelector('.mariadb-fallback-settings').open=true;
    }
    if(button.dataset.configureMymovies) {const s=state.facets.sources.find(s=>s.id===button.dataset.configureMymovies);if(s){fillForm('mymovies-api-form',{source_id:s.id,...s.config});}}
    if(button.dataset.addPlayerSource) {
      openKodiPlayerEditor(button.dataset.addPlayerSource);
    }
    if(button.dataset.editPlayer) {
      const player=state.players.find(p=>p.id===button.dataset.editPlayer);
      if(player)openKodiPlayerEditor(player.source_id,player);
    }
    if(button.id==='test-tmdb'){button.disabled=true;const tested=await api('connections/tmdb/test',{method:'POST'});result.textContent=tested.message;result.className='success-text play-status';}
    if(button.dataset.testPlayer){
      button.disabled=true;const p=state.players.find(p=>p.id===button.dataset.testPlayer);
      if(!p)throw new Error('Choisissez une instance Kodi.');
      const tested=await api('connections/kodi/test',{method:'POST',body:JSON.stringify({player_id:p.id,host:p.host,port:p.port,username:p.username,tls:p.tls})});
      result.textContent=tested.message;result.className='success-text play-status';
    }
    if(button.id==='play-browser') {
      const playbackItem=state.playbackItem||state.detail;
      savePlaybackDraft();
      button.disabled=true;
      const startingAt=performance.now();
      const playerId=$('play-target').value;
      const ticket=await api(`players/${encodeURIComponent(playerId)}/local-play`,{
        method:'POST',
        body:JSON.stringify({
          media_id:playbackItem.id,
          edition_id:$('play-edition').value||null,
          resume:$('play-resume').checked
        })
      });
      if(typeof ticket.path!=='string' ||
         !ticket.path.startsWith(`/api/v1/players/${playerId}/local-stream/`)) {
        throw new Error('Le serveur a renvoyé une URL de lecture invalide.');
      }
      const streamURL=url(ticket.path);
      if(new URL(streamURL).origin!==location.origin)throw new Error('URL de streaming non autorisée.');
      const mediaBase=ticket.path.replace(/\/local-stream\/([^/]+)\/[^/]+$/,'/local-media/$1');
      const item=playbackItem;
      const resumeAt=$('play-resume')?.checked?Number(ticket.resume_seconds)||0:0;
      console.info('Browser playback ticket ms',Math.round(performance.now()-startingAt),
        'catalogue ms',Number(ticket.resolution_ms)||0,
        'preferred transport',ticket.preferred_transport||'kodi');
      result.textContent=ticket.preferred_transport&&ticket.preferred_transport!=='kodi'
        ? 'Connexion réseau '+ticket.preferred_transport.toUpperCase()+' privilégiée ; repli sur Kodi si indisponible.'
        : 'Lecture via Kodi (aucune connexion réseau configurée correspondante).';
      if($('play-dialog').open)$('play-dialog').close();
      void openPlayer({
        title:item.title,
        subtitle:item.kind==='episode'?`S${item.season??0} · E${item.episode??0}`:[item.year,$('play-edition')?.selectedOptions[0]?.textContent].filter(Boolean).join(' · '),
        artwork:item.artwork?.poster?url(item.artwork.poster):null,
        extension:ticket.path.split('.').pop(),
        resume:resumeAt,
        directURL:streamURL,
        probeURL:url(mediaBase+'/probe'),
        subtitleURL:index=>url(`${mediaBase}/subtitles/${index}.vtt`),
        streamURL:params=>url(`${mediaBase}/stream.mp4?${new URLSearchParams(params)}`),
      });
      return;
    }
    if(button.id==='play-button') {
      const playbackItem=state.playbackItem||state.detail;
      savePlaybackDraft();
      button.disabled=true;
      const target=$('play-target').value;
      await api(`players/${target}/play`,{
        method:'POST',
        body:JSON.stringify({media_id:playbackItem.id,edition_id:$('play-edition').value,resume:$('play-resume').checked})
      });
      toast('Lecture envoyée à Kodi.');
      // Player.Open was acknowledged: close both media dialogs before opening
      // the same Now Playing remote used by the header Kodi button.
      if($('play-dialog').open)$('play-dialog').close();
      if($('detail-dialog').open)$('detail-dialog').close();
      showKodiRemote(target);
    }
  }catch(e){if(result){result.textContent=e.message;result.className='error-text play-status';}if(button.dataset.sync||button.dataset.scanKodi){const status=$('job-status');if(status){status.hidden=false;status.textContent='Échec : '+e.message;}}toast(e.message);}finally{button.disabled=false;}
});
$('connections-button').addEventListener('click',connections);
$('lists-button').addEventListener('click',()=>openLists());
$('create-list-button').addEventListener('click',()=>openLists(state.listId,{create:true}));
$('logout-button').addEventListener('click',async()=>{try{await api('auth/session',{method:'DELETE'});showLogin();}catch(e){toast(e.message);}});
$('grid-view').addEventListener('click',()=>setView('grid')); $('list-view').addEventListener('click',()=>setView('list'));
// Native <details> handles opening/closing without JavaScript; the event keeps
// accessible state synchronized while retaining filter values independently.
$('filters-panel').addEventListener('toggle',()=>{
  $('toggle-filters').setAttribute('aria-expanded',String($('filters-panel').open));
});
$('reset-filters').addEventListener('click',reset);
let searchTimer;
$('search').addEventListener('input',()=>{
  state.personSearch=null;
  updateSearchClearButton();
  savePreferences();
  clearTimeout(searchTimer);
  searchTimer=setTimeout(()=>{loadMedia();},250);
});
$('clear-search').addEventListener('click',()=>{
  clearTimeout(searchTimer);
  state.personSearch=null;
  $('search').value='';
  updateSearchClearButton();
  loadMedia();
  $('search').focus({preventScroll:true});
});
['kind','source-type','genre','collection','watch-status','sort'].forEach(id=>$(id).addEventListener('change',()=>{loadMedia();}));
$('deep-search').addEventListener('change',()=>{state.deepSearch=$('deep-search').checked;loadMedia();});
$('sort-order').addEventListener('click',()=>{state.order=state.order==='asc'?'desc':'asc';$('sort-order').textContent=state.order==='asc'?'↑ Croissant':'↓ Décroissant';loadMedia();});
$('load-more').addEventListener('click',()=>loadMedia({append:state.items.length>0}));
document.addEventListener('keydown',event=>{if(event.key==='/'&&!['INPUT','SELECT','TEXTAREA'].includes(event.target.tagName)&&!$('app').hidden&&!document.querySelector('dialog[open]')){event.preventDefault();$('search').focus();}});
for(const dialog of document.querySelectorAll('dialog')) {
  dialog.addEventListener('close',releasePage);
  dialog.addEventListener('click',event=>{if(event.target===dialog){const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();}});
}
async function enrichDetail(force) {
  const id=state.detail?.id;if(!id||state.enrichingIds.has(id))return;
  state.enrichingIds.add(id);
  const seq=state.detailSeq; const personalSeq=state.personalSeq; const status=$('enrichment-status');
  if(status)status.textContent='Récupération des personnes, portraits et bandes-annonces…';
  const button=$('refresh-metadata');if(button){button.disabled=true;button.classList.add('is-loading');}
  let enriched=false;
  try {
    const item=await api(`media/${encodeURIComponent(id)}/enrich?refresh=${force}`,{method:'POST'});
    enriched=true;
    if(seq===state.detailSeq&&$('detail-dialog').open){
      const expandedSeason=$('season-list')?.querySelector('[aria-expanded="true"]')?.dataset.season;
      if((state.personalSeq||0)!==personalSeq)Object.assign(item,{personal:state.detail.personal,user_rating:state.detail.user_rating,watch_status:state.detail.watch_status});
      const episodes=$('episode-list')?.innerHTML;
      const ratingDraft=$('rating-popover')?.dataset.open?{value:$('rating-slider').value,focused:$('rating-popover').contains(document.activeElement)}:null;
      renderDetail(item);
      if(ratingDraft){showRatingPopover(ratingDraft.focused);$('rating-slider').value=ratingDraft.value;previewRating(Number(ratingDraft.value));}
      if(state.detail?.kind==='tvshow') {
        loadSeasons(id,expandedSeason===undefined?null:Number(expandedSeason)).catch(()=>{});
      } else if(episodes&&$('episode-list'))$('episode-list').innerHTML=episodes;
      if($('play-dialog').open)updatePlayEdition();
    }
  } catch(e) {if(seq===state.detailSeq){if($('enrichment-status'))$('enrichment-status').textContent=e.message;toast(e.message);}}
  finally {state.enrichingIds.delete(id);if(enriched&&seq===state.detailSeq&&$('enrichment-status'))$('enrichment-status').textContent=`Métadonnées TMDB mises en cache le ${date(state.detail.enriched_at)}.`;if(seq===state.detailSeq&&$('refresh-metadata')){$('refresh-metadata').disabled=state.role!=='admin';$('refresh-metadata').classList.remove('is-loading');}}
}
// Thumbnail URLs include list revisions, so a changed mosaic bypasses the
// browser cache and always matches the latest ordering.
function listThumbnail(list,extraClass=''){
  const image=list.thumbnail?`<img src="${esc(url(list.thumbnail))}" alt="" loading="lazy">`:'<span aria-hidden="true">▦</span>';
  return `<span class="list-mosaic ${extraClass}" aria-hidden="true">${image}</span>`;
}
function renderHomeListThumbnails(lists){
  const container=$('home-list-thumbnails');
  if(!container)return;
  container.innerHTML=lists.map(list=>`<button type="button" class="home-list-thumb" data-open-home-list="${esc(list.id)}" title="${esc(list.name)}" aria-label="${esc(list.name)}">${listThumbnail(list)}</button>`).join('');
  attachImageFallbacks(container);
}
async function refreshHomeListThumbnails(){
  try{renderHomeListThumbnails(await api('lists'));}catch(e){/* Lists may be temporarily unavailable; keep catalogue visible. */}
}
async function chooseList() {
  try {
    const lists=await api('lists');const item=state.detail;
    $('choose-list-content').innerHTML=`<p class="muted">${esc(item.title)}</p><div class="choose-list-buttons">${lists.map(l=>`<button class="list-choice-row" data-choose-list="${esc(l.id)}" ${item.lists.some(current=>current.id===l.id)?'disabled':''}>${listThumbnail(l)}<span><strong>${esc(l.name)}</strong><small class="muted">${l.item_count} médias${item.lists.some(current=>current.id===l.id)?' · Déjà ajouté':''}</small></span></button>`).join('')||'<p class="hint">Créez votre première liste ci-dessous.</p>'}</div><form id="choose-new-list" class="inline-form"><label>Nouvelle liste<input name="name" required maxlength="120" placeholder="Films à voir"></label><button type="submit" class="primary">+ Créer et ajouter</button></form>`;
    showDialog('choose-list-dialog');
  } catch(e){toast(e.message);}
}
async function openLists(selected=state.listId,{create=false}={}) {
  try {
    const lists=await api('lists');renderHomeListThumbnails(lists);state.listId=lists.some(l=>l.id===selected)?selected:lists[0]?.id;
    savePreferences();
    const current=lists.find(l=>l.id===state.listId);const admin=state.role==='admin';
    const newListForm=admin?`<form id="new-list-form" class="inline-form" ${current&&!create?'hidden':''}><label>Nom de la liste<input name="name" required maxlength="120" placeholder="Films à voir"></label><button class="primary" type="submit">Créer la liste</button></form>`:'';
    $('lists-content').innerHTML=`<div class="list-toolbar"><label>Liste<select id="selected-list">${lists.map(l=>`<option data-name="${esc(l.name)}" value="${esc(l.id)}" ${l.id===state.listId?'selected':''}>${esc(l.name)} · ${l.item_count}</option>`).join('')||'<option>Aucune liste</option>'}</select></label>${admin?'<button id="new-list-toggle" class="secondary">+ Nouvelle liste</button>':''}</div><div class="list-selector-cards list-cover-grid" role="group" aria-label="Choix de la liste">${lists.map(l=>`<button type="button" class="list-choice-row ${l.id===state.listId?'is-selected':''}" data-select-list="${esc(l.id)}" aria-pressed="${l.id===state.listId}">${listThumbnail(l)}<span class="list-grid-caption"><strong>${esc(l.name)}</strong><small class="muted">${l.watched_count??0} / ${l.item_count} vus</small></span></button>`).join('')}</div>${!admin?'<p class="metadata-status hint">Accès en lecture seule : utilisez le jeton administrateur pour créer une liste ou modifier son contenu.</p>':''}<p class="hint">Ajoutez des films et des séries, puis choisissez leur ordre dans chaque liste. Ces sélections sont conservées après les synchronisations.</p>${newListForm}${current?`<div class="list-toolbar"><p class="hint">${esc(current.description||'Votre sélection, dans l’ordre que vous choisissez.')}</p>${admin?'<button id="rename-list" class="text-button">Renommer</button><button id="delete-list" class="text-button">Supprimer</button>':''}</div><div class="list-toolbar"><label>Tri<select id="list-sort"><option value="position">Ordre manuel</option><option value="title">Titre</option><option value="year">Année</option><option value="date_added">Date d’ajout</option><option value="rating">Note</option><option value="user_rating">Votre note</option></select></label><button id="list-sort-order" class="secondary">${state.listOrder==='asc'?'↑ Croissant':'↓ Décroissant'}</button>${admin?'<button id="list-add-button" class="primary">+ Ajouter des médias</button>':''}</div>${admin?'<section id="list-search-area" class="list-search-area" hidden><label>Rechercher dans le catalogue<input id="list-search" type="search" placeholder="Titre, personne, genre…" autocomplete="off"></label><div id="list-search-results" aria-live="polite"></div></section>':''}<p id="list-count" class="hint"></p><div id="list-items" class="saved-list-items" aria-live="polite"></div><button id="list-load-more" class="secondary" hidden>Charger la suite</button>`:'<p class="empty-state">Créez une liste pour organiser vos prochains visionnages.</p>'}`;
    attachImageFallbacks($('lists-content'));
    showDialog('lists-dialog');
    if(current){$('list-sort').value=state.listSort;await loadList();}
    if(admin&&(create||!current))$('new-list-form').elements.name.focus();
  } catch(e){toast(e.message);}
}
function listRow(item,index) {
  const manual=state.listSort==='position'&&state.listOrder==='asc'&&state.role==='admin';
  return `<div class="saved-list-row"><span class="list-position">${index+1}</span><button class="saved-media" data-media="${esc(item.id)}">${poster(item)}<span><strong>${esc(item.title)}</strong><small>${[item.year,kindLabels[item.kind],stateLabels[item.watch_status]].filter(Boolean).map(esc).join(' · ')}${item.user_rating!=null?' · Votre note : '+item.user_rating+'/10':''}</small></span></button><div class="saved-row-actions">${manual?`<button data-move-list="${esc(item.id)}" data-direction="-1" aria-label="Monter ${esc(item.title)}" ${index===0?'disabled':''}>↑</button><button data-move-list="${esc(item.id)}" data-direction="1" aria-label="Descendre ${esc(item.title)}" ${index===state.listTotal-1?'disabled':''}>↓</button>`:''}${state.role==='admin'?`<button data-remove-list-media="${esc(item.id)}" aria-label="Retirer ${esc(item.title)} de la liste">×</button>`:''}</div></div>`;
}
async function loadList({append=false}={}) {
  const seq=++state.listSeq;const id=state.listId;
  if(!id||!$('list-items'))return;
  const offset=append?state.listItems.length:0;
  try {
    const page=await api(`lists/${id}/items?sort=${state.listSort}&order=${state.listOrder}&offset=${offset}&limit=60`);
    if(seq!==state.listSeq||id!==state.listId)return;
    if(append&&page.revision!==state.listRevision){toast('La liste a changé, elle a été rechargée.');return loadList();}
    state.listItems=append?[...state.listItems,...page.items]:page.items;state.listRevision=page.revision;state.listTotal=page.total;
    const option=$('selected-list')?.selectedOptions[0];if(option?.dataset.name)option.textContent=`${option.dataset.name} · ${page.total}`;
    $('list-items').innerHTML=state.listItems.map(listRow).join('')||'<p class="hint">Cette liste est vide. Ajoutez des médias avec le bouton +.</p>';
    $('list-count').textContent=`${state.listItems.length} / ${page.total} médias`;
    $('list-load-more').hidden=!page.has_more;attachImageFallbacks($('list-items'));
  } catch(e){toast(e.message);}
}
let listSearchSeq=0,listSearchController;
async function searchList() {
  if(!$('list-search'))return;
  const query=$('list-search').value.trim();const seq=++listSearchSeq;const listId=state.listId;
  listSearchController?.abort();listSearchController=new AbortController();
  if(!query){$('list-search-results').innerHTML='';return;}
  $('list-search-results').textContent='Recherche…';
  try {
    const page=await api(`media?q=${encodeURIComponent(query)}&limit=20&sort=title&order=asc`,{signal:listSearchController.signal});
    if(seq!==listSearchSeq||listId!==state.listId||!$('list-search-results'))return;
    $('list-search-results').innerHTML=page.items.map(item=>`<div class="search-list-result"><span><strong>${esc(item.title)}</strong><small>${item.year||''} · ${esc(kindLabels[item.kind])}</small></span><button data-add-list-media="${esc(item.id)}" aria-label="Ajouter ${esc(item.title)}" ${state.listItems.some(i=>i.id===item.id)?'disabled':''}>+</button></div>`).join('')+(page.has_more?'<p class="hint">20 premiers résultats. Précisez votre recherche pour retrouver les autres.</p>':'')||'<p class="hint">Aucun résultat.</p>';
  } catch(e){if(e.name!=='AbortError'&&seq===listSearchSeq)toast(e.message);}
}
async function moveList(id,direction) {
  const listId=state.listId;const ids=[];let offset=0;let revision=null;
  // Obtain a full consistent revision before sending the complete permutation.
  while(true){
    const page=await api(`lists/${listId}/items?sort=position&order=asc&limit=200&offset=${offset}`);
    if(revision!==null&&revision!==page.revision)throw new Error('La liste a changé. Rechargez-la avant de la réordonner.');
    revision=page.revision;ids.push(...page.items.map(item=>item.id));offset+=page.items.length;
    if(!page.has_more)break;
    if(ids.length>10000)throw new Error('Cette liste dépasse la limite de 10 000 médias pour le réordonnancement.');
  }
  const index=ids.indexOf(id);const other=index+direction;
  if(index<0||other<0||other>=ids.length)return;
  [ids[index],ids[other]]=[ids[other],ids[index]];
  await api(`lists/${listId}/order`,{method:'PUT',body:JSON.stringify({media_ids:ids,revision})});
  await loadList();
}
document.addEventListener('submit',async event=>{
  const form=event.target;const id=form.getAttribute('id');
  if(!['new-list-form','choose-new-list'].includes(id))return;
  event.preventDefault();const button=form.querySelector('button[type="submit"]');button.disabled=true;
  try {
    const data=new FormData(form);
    {
      const name=String(data.get('name')).trim();if(!name)return;
      const list=await api('lists',{method:'POST',body:JSON.stringify({name})});
      if(id==='choose-new-list'){
        const mid=state.detail.id;await api(`lists/${list.id}/items`,{method:'POST',body:JSON.stringify({media_id:mid})});
        $('choose-list-dialog').close();renderDetail(await api(`media/${mid}`));await refreshHomeListThumbnails();toast('Liste créée et média ajouté.');
      }else await openLists(list.id);
    }
  }catch(e){toast(e.message);}finally{button.disabled=false;}
});
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  if(!['refresh-metadata','add-to-list','new-list-toggle','list-add-button','rename-list','delete-list','list-load-more','list-sort-order'].includes(button.id)&&!button.dataset.openHomeList&&!button.dataset.selectList&&!button.dataset.chooseList&&!button.dataset.addListMedia&&!button.dataset.removeListMedia&&!button.dataset.moveList&&!button.dataset.copyFile)return;
  try {
    if(button.dataset.openHomeList){await openLists(button.dataset.openHomeList);return;}
    if(button.dataset.selectList){await openLists(button.dataset.selectList);return;}
    if(button.id==='refresh-metadata'){hideRatingPopover();await enrichDetail(true);}
    if(button.id==='add-to-list')await chooseList();
    if(button.dataset.chooseList){
      button.disabled=true;const mid=state.detail.id;
      await api(`lists/${button.dataset.chooseList}/items`,{method:'POST',body:JSON.stringify({media_id:mid})});
      $('choose-list-dialog').close();renderDetail(await api(`media/${mid}`));if($('lists-dialog').open)await openLists();else await refreshHomeListThumbnails();toast('Média ajouté à la liste.');
    }
    if(button.id==='new-list-toggle'){$('new-list-form').hidden=false;$('new-list-form').elements.name.focus();}
    if(button.id==='list-add-button'){$('list-search-area').hidden=!$('list-search-area').hidden;if(!$('list-search-area').hidden)$('list-search').focus();}
    if(button.id==='rename-list'){
      const name=localizedPrompt('Nom de la liste',$('selected-list').selectedOptions[0].textContent.replace(/ · \d+$/,''));
      if(name?.trim()){const existing=(await api('lists')).find(l=>l.id===state.listId);await api(`lists/${state.listId}`,{method:'PUT',body:JSON.stringify({name:name.trim(),description:existing.description})});await openLists();}
    }
    if(button.id==='delete-list'&&localizedConfirm('Supprimer cette liste ? Les médias restent dans le catalogue.')){await api(`lists/${state.listId}`,{method:'DELETE'});await openLists();}
    if(button.id==='list-load-more'){button.disabled=true;await loadList({append:true});}
    if(button.id==='list-sort-order'){state.listOrder=state.listOrder==='asc'?'desc':'asc';button.textContent=state.listOrder==='asc'?'↑ Croissant':'↓ Décroissant';savePreferences();await loadList();}
    if(button.dataset.addListMedia){button.disabled=true;await api(`lists/${state.listId}/items`,{method:'POST',body:JSON.stringify({media_id:button.dataset.addListMedia})});await loadList();await refreshHomeListThumbnails();await searchList();}
    if(button.dataset.removeListMedia){button.disabled=true;await api(`lists/${state.listId}/items/${button.dataset.removeListMedia}`,{method:'DELETE'});await loadList();await refreshHomeListThumbnails();if($('list-search')?.value)await searchList();}
    if(button.dataset.moveList){button.disabled=true;await moveList(button.dataset.moveList,Number(button.dataset.direction));await refreshHomeListThumbnails();}
    if(button.dataset.copyFile){
      const file=state.detail.editions.find(e=>e.id===button.dataset.copyFile).metadata.file;
      if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(file);toast('Chemin copié.');}
      else localizedPrompt('Copiez le chemin du fichier',file);
    }
  }catch(e){toast(e.message);}finally{button.disabled=false;}
});
document.addEventListener('change',event=>{
  if(event.target.id==='play-target'){updatePlayEdition();if($('play-status'))$('play-status').textContent='';}
  if(['play-target','play-edition','play-resume'].includes(event.target.id))savePlaybackDraft();
  if(event.target.id==='mariadb-database-choices'&&event.target.value){
    const form=$('kodi-form');
    form.elements.namedItem('database').value=event.target.value;
    $('mariadb-database-result').textContent='Base sélectionnée : '+event.target.value+'. Testez la connexion avant de synchroniser.';
    $('kodi-form-result').hidden=true;
  }
  if(event.target.id==='selected-list')openLists(event.target.value);
  if(event.target.id==='list-sort'){state.listSort=event.target.value;savePreferences();loadList();}
});
let listSearchTimer;
document.addEventListener('input',event=>{if(event.target.id==='list-search'){clearTimeout(listSearchTimer);listSearchTimer=setTimeout(searchList,250);}});

// The rating editor opens on hover/focus, and remains usable on touch screens.
document.addEventListener('pointerover',event=>{
  if(event.target.closest('#rating-button'))showRatingPopover();
  if(event.target.closest('#rating-popover'))clearTimeout(ratingCloseTimer);
});
document.addEventListener('pointerout',event=>{
  if(!event.target.closest('#rating-button,#rating-popover'))return;
  const next=event.relatedTarget;
  if(next&&($('rating-button')?.contains(next)||$('rating-popover')?.contains(next)))return;
  scheduleRatingClose();
});
document.addEventListener('focusin',event=>{
  if(event.target.id==='rating-button')showRatingPopover();
  if(event.target.closest('#rating-popover'))clearTimeout(ratingCloseTimer);
});
document.addEventListener('focusout',event=>{
  if(!event.target.closest('#rating-button,#rating-popover'))return;
  const next=event.relatedTarget;
  if(!next||!($('rating-button')?.contains(next)||$('rating-popover')?.contains(next)))hideRatingPopover();
});
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&$('rating-popover')?.dataset.open){event.preventDefault();event.stopPropagation();$('rating-button').focus({preventScroll:true});hideRatingPopover();}
},true);
document.addEventListener('click',event=>{
  if(!event.target.closest('#rating-button,#rating-popover'))hideRatingPopover();
  if(event.target.closest('[data-trailer]'))$('trailers-dialog').close();
  const button=event.target.closest('button');if(!button||button.disabled)return;
  if(button.id==='rating-button')showRatingPopover(true);
  if(button.dataset.rating)savePersonal({user_rating:Number(button.dataset.rating)});
  if(button.hasAttribute('data-rating-reset'))savePersonal({user_rating:null});
  if(button.id==='watch-button'){
    if(state.detail?.kind==='tvshow')saveEpisodeWatches(!watchState(state.detail).watched);
    else savePersonal({watched:state.detail.watch_status!=='watched'});
  }
  if(button.id==='open-playback')openPlayback();
  if(button.id==='open-trailers')openTrailers();
});
document.addEventListener('input',event=>{if(event.target.id==='rating-slider')previewRating(Number(event.target.value));});
document.addEventListener('change',event=>{
  if(event.target.id==='rating-slider')savePersonal({user_rating:Number(event.target.value)});
  if(event.target.id==='play-season'&&state.playbackSeriesId)
    void loadPlaybackSeriesEpisodes(state.playbackSeriesId);
});
$('detail-dialog').addEventListener('scroll',hideRatingPopover);
$('detail-dialog').addEventListener('close',()=>{hideRatingPopover();if($('play-dialog').open)$('play-dialog').close();if($('trailers-dialog').open)$('trailers-dialog').close();if($('artwork-gallery-dialog').open)$('artwork-gallery-dialog').close();});
// Mobile swipes and desktop drags. Vertical scrolling does not change images.
let artworkSwipeStart=null;
$('artwork-gallery-image').addEventListener('pointerdown',event=>{
  if(event.button!==0)return;
  artworkSwipeStart={x:event.clientX,y:event.clientY,id:event.pointerId};
  event.currentTarget.setPointerCapture(event.pointerId);
});
$('artwork-gallery-image').addEventListener('pointerup',event=>{
  if(!artworkSwipeStart||artworkSwipeStart.id!==event.pointerId)return;
  const dx=event.clientX-artworkSwipeStart.x,dy=event.clientY-artworkSwipeStart.y;
  artworkSwipeStart=null;
  if(Math.abs(dx)>45&&Math.abs(dx)>Math.abs(dy)*1.25)stepArtworkGallery(dx<0?1:-1);
});
$('artwork-gallery-image').addEventListener('pointercancel',()=>{artworkSwipeStart=null;});
document.addEventListener('keydown',event=>{
  if(!$('artwork-gallery-dialog').open)return;
  if(event.key==='ArrowRight'||event.key==='ArrowLeft'){
    event.preventDefault();
    stepArtworkGallery(event.key==='ArrowRight'?1:-1);
  }
});
$('artwork-gallery-dialog').addEventListener('close',()=>{artworkSwipeStart=null;});
$('play-dialog').addEventListener('close',savePlaybackDraft);
window.addEventListener('resize',positionRatingPopover);

initialize();


// JSON catalogue backups: the server is the authoritative library store.
document.addEventListener('click',async event=>{
  const button=event.target.closest('#backup-export');
  if(!button)return;
  button.disabled=true;
  try {
    const backup=await api('backup');
    const objectURL=URL.createObjectURL(new Blob([JSON.stringify(backup,null,2)],{type:'application/json'}));
    const link=document.createElement('a');
    link.href=objectURL;
    link.download='media-catalog-'+new Date().toISOString().slice(0,10)+'.json';
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(()=>URL.revokeObjectURL(objectURL),30000);
    toast('Export JSON téléchargé.');
  }catch(e){toast(e.message);}
  finally{button.disabled=false;}
});
document.addEventListener('change',async event=>{
  if(event.target.id!=='backup-import-file')return;
  const file=event.target.files?.[0];
  event.target.value='';
  if(!file)return;
  if(state.role!=='admin'){toast('Accès administrateur nécessaire.');return;}
  if(file.size>100*1024*1024){toast('Le fichier JSON dépasse 100 Mio.');return;}
  if(!localizedConfirm('Importer cette sauvegarde et remplacer tout le catalogue, ses notes, visionnages et listes ?'))return;
  const button=$('backup-export');
  if(button)button.disabled=true;
  try {
    const content=await file.text();
    const backup=JSON.parse(content);
    const result=await api('backup',{method:'POST',body:JSON.stringify(backup)});
    toast('Catalogue restauré : '+result.media+' médias et '+result.lists+' listes. Vignettes en régénération.');
    await refresh();
    await refreshHomeListThumbnails();
    await connections();
    await startKodiRemote({api,url,role:state.role});
  }catch(e){toast(e.message);}
  finally{if(button)button.disabled=false;}
});




function aiUsageMarkup(report){
  if(!report)return '<p>Consommation IA non disponible.</p>';
  const money=n=>(Number(n)||0).toLocaleString(undefined,{style:'currency',currency:'USD',minimumFractionDigits:4,maximumFractionDigits:6});
  const labels={indexing:'Indexation',search:'Recherches',connection_test:'Tests de connexion'};
  const categories=Object.entries(report.categories||{}).map(([category,v])=>
    '<tr><td>'+esc(labels[category]||category)+'</td><td>'+Number(v.requests||0)+'</td>'+
    '<td>'+Number(v.input_tokens||0).toLocaleString()+'</td><td>'+Number(v.output_tokens||0).toLocaleString()+'</td>'+
    '<td>'+money(v.cost_usd)+'</td></tr>'
  ).join('');
  return '<h4>Consommation IA · '+esc(report.month)+'</h4>'+
    '<p>Coût estimé : <strong>'+money(report.cost_usd)+'</strong> · Plafond : '+
    (report.monthly_budget_usd==null?'Aucun':money(report.monthly_budget_usd))+
    (report.remaining_usd!=null?' · Restant : '+money(report.remaining_usd):'')+'</p>'+
    '<table><thead><tr><th>Opération</th><th>Appels</th><th>Tokens entrants</th><th>Tokens sortants</th><th>Coût estimé</th></tr></thead><tbody>'+
    (categories||'<tr><td colspan="5">Aucun appel enregistré ce mois-ci.</td></tr>')+
    '</tbody></table><p class="hint">Montants indicatifs calculés à partir des tokens signalés par les fournisseurs. Hors taxes et éventuels autres usages du compte.</p>';
}
document.addEventListener('click',async event=>{
  if(!event.target.closest('#ai-refresh-usage'))return;
  const button=event.target.closest('button');
  button.disabled=true;
  try{
    const report=await api('ai/usage');
    if($('ai-usage-summary'))$('ai-usage-summary').innerHTML=aiUsageMarkup(report);
  }catch(e){toast(e.message)}
  finally{button.disabled=false}
});

const aiLanguageModels={
  openai:[['gpt-6-luna','GPT-6 Luna'],['gpt-6.1-sol','GPT-6.1 Sol'],['gpt-4o-mini','GPT-4o mini']],
  anthropic:[['claude-haiku-5-5','Claude Haiku 5.5 · Recommandé'],['claude-sonnet-5-5','Claude Sonnet 5.5'],['claude-haiku-4-5','Claude Haiku 4.5']],
  gemini:[['gemini-3.5-flash-lite','Gemini 3.5 Flash-Lite · Conseillé'],['gemini-3.8-flash','Gemini 3.8 Flash'],['gemini-2.5-flash-lite','Gemini 2.5 Flash-Lite · Ancien']]
};
function aiProviderName(provider){return {openai:'OpenAI',anthropic:'Claude (Anthropic)',gemini:'Gemini (Google)'}[provider]||provider;}
function aiProviderOptions(selected){
  const names=['openai','anthropic','gemini'];
  return names.map(id=>'<option value="'+id+'"'+(id===selected?' selected':'')+'>'+esc(aiProviderName(id))+'</option>').join('');
}
function aiSettingsModelPicker(name,selected,provider='openai'){
  const choices=aiLanguageModels[provider]||[];
  const custom=!choices.some(([id])=>id===selected);
  const label="Modèle IA";
  const options=choices.map(([id,title])=>
    '<option value="'+esc(id)+'"'+(id===selected?' selected':'')+'>'+esc(title)+'</option>'
  ).join('');
  return '<label>'+label+'<select name="'+name+'_choice" data-ai-model-selector="'+name+'">'+
    options+'<option value="custom"'+(custom?' selected':'')+'>Autre modèle…</option></select></label>'+
    '<label class="wide" data-ai-custom="'+name+'"'+(custom?'':' hidden')+'>'+
    'Identifiant personnalisé<input name="'+name+'_custom" maxlength="100" autocomplete="off" value="'+esc(custom?selected:'')+'" placeholder="'+esc(selected)+'"></label>';
}
function aiEmbeddingModelPicker(selected){
  const models=[
    ['text-embedding-3-small','text-embedding-3-small · Conseillé'],
    ['text-embedding-3-large','text-embedding-3-large · Précision renforcée']
  ];
  const custom=!models.some(([id])=>id===selected);
  return '<label>Modèle d\'embeddings OpenAI<select name="embedding_model_choice" data-ai-model-selector="embedding_model">'+
    models.map(([id,label])=>'<option value="'+esc(id)+'"'+(selected===id?' selected':'')+'>'+esc(label)+'</option>').join('')+
    '<option value="custom"'+(custom?' selected':'')+'>Autre modèle…</option></select></label>'+
    '<label data-ai-custom="embedding_model"'+(custom?'':' hidden')+'>Identifiant personnalisé'+
    '<input name="embedding_model_custom" maxlength="100" autocomplete="off" value="'+esc(custom?selected:'')+'"></label>';
}
function formAIModelValue(data,name){
  const choice=String(data.get(name+'_choice')||'');
  const value=(choice==='custom'?data.get(name+'_custom'):choice)||'';
  if(!String(value).trim())throw new Error("Sélectionnez un modèle ou saisissez son identifiant.");
  return String(value).trim();
}
document.addEventListener('change',event=>{
  const select=event.target.closest('#ai-language-provider');
  if(!select)return;
  const form=$('ai-settings-form');
  form.querySelector('[data-ai-language-model]').innerHTML=aiSettingsModelPicker('model',aiLanguageModels[select.value][0][0],select.value);
  $('ai-language-key-label').firstChild.textContent='Clé API · '+aiProviderName(select.value);
  const vectorSettings=form.querySelector('[data-openai-vectors]');
  if(vectorSettings)vectorSettings.hidden=select.value!=='openai';
  if($('ai-reindex'))$('ai-reindex').disabled=true;
  $('ai-test-connection').disabled=true;
});
document.addEventListener('change',event=>{
  const select=event.target.closest('[data-ai-model-selector]');
  if(!select)return;
  const custom=$('ai-settings-form')?.querySelector('[data-ai-custom="'+select.dataset.aiModelSelector+'"]');
  if(custom)custom.hidden=select.value!=='custom';
  if($('ai-test-connection'))$('ai-test-connection').disabled=true;
  if($('ai-reindex')&&select.dataset.aiModelSelector==='embedding_model')$('ai-reindex').disabled=true;
});

let aiPreviousPlan=null;
let aiConversation=[];
let aiResultIDs=[];
const aiRefinementHistory=[];
function updateAIResultActions(){
  $('ai-undo-search').disabled=aiRefinementHistory.length===0;
  $('ai-add-results-list').disabled=state.role!=='admin'||aiResultIDs.length===0;
}
async function refreshAIStatus(){
  try {
    const status=await api('ai/status');
    $('ai-toggle').title=status.configured?'Recherche intelligente':'Configurer l’IA dans Paramètres';
    $('ai-search-feedback').textContent=status.configured?'Interroge uniquement votre catalogue. Résultats fondés sur les filtres reconnus.':'IA non configurée : demandez à un administrateur de renseigner la clé API dans Paramètres. La recherche habituelle fonctionne toujours.';
    $('ai-search-form').hidden=!status.configured;
  } catch(e) { $('ai-search-feedback').textContent='Recherche IA indisponible sur ce serveur.'; $('ai-search-form').hidden=true; }
}
$('ai-toggle').addEventListener('click',()=>{
  $('ai-search-area').hidden=!$('ai-search-area').hidden;
  if(!$('ai-search-area').hidden){void refreshAIStatus();$('ai-prompt').focus();}
});
document.addEventListener('submit',async event=>{
  if(event.target.id==='ai-settings-form'){
    event.preventDefault();
    const form=event.target, button=form.querySelector('button[type=submit]'), data=new FormData(form);
    button.disabled=true;
    try{
      const saved=await api('ai/settings',{method:'PUT',body:JSON.stringify({
        provider:String(data.get('provider')),model:formAIModelValue(data,'model'),
        ...(data.get('provider')==='openai'?{embedding_model:formAIModelValue(data,'embedding_model')}:{ }),
        api_key:String(data.get('api_key')||'')||null,clear_api_key:data.has('clear_api_key'),
        monthly_budget_usd:Number(data.get('monthly_budget_usd'))
      })});
      $('ai-settings-result').textContent=saved.configured?'IA configurée.':'IA désactivée.';
      if($('ai-usage-summary'))$('ai-usage-summary').innerHTML=aiUsageMarkup(saved.usage);
      form.elements.api_key.value='';form.elements.clear_api_key.checked=false;
      await refreshAIStatus();
      $('ai-test-connection').disabled=!saved.configured;
      if($('ai-reindex'))$('ai-reindex').disabled=!saved.configured||saved.provider!=='openai';
      if(saved.index&&$('ai-index-progress'))$('ai-index-progress').textContent=saved.index.indexed+' / '+saved.index.total;
      $('ai-connection-result').replaceChildren();
    }catch(e){$('ai-settings-result').textContent=e.message;}
    finally{button.disabled=false;}
  }
  if(event.target.id==='ai-search-form'){
    event.preventDefault();
    const button=event.target.querySelector('button[type=submit]'), target=$('ai-search-results');
    button.disabled=true;$('ai-search-feedback').textContent='Interprétation et recherche sémantique…';
    try{
      const userPrompt=$('ai-prompt').value.trim();
      const result=await api('ai/search',{method:'POST',body:JSON.stringify({
        prompt:userPrompt,previous_plan:aiPreviousPlan,
        ...(aiResultIDs.length?{previous_ids:aiResultIDs}:{})
      })});
      aiRefinementHistory.push({plan:aiPreviousPlan,conversation:aiConversation.slice(),
        html:target.innerHTML,feedback:$('ai-search-feedback').textContent,ids:aiResultIDs.slice()});
      aiPreviousPlan=result.plan;
      aiResultIDs=[...new Set(result.items.map(item=>item.id))];
      updateAIResultActions();
      $('ai-bulk-panel').hidden=true;
      aiConversation.push({role:'user',text:userPrompt},{role:'assistant',text:result.assistant_message||'Critères appliqués.'});
      $('ai-prompt').value='';$('ai-prompt').placeholder='Affiner : uniquement les non vus, plutôt des années 90…';
      $('ai-chat-history').innerHTML=aiConversation.map(turn=>
        '<div class="ai-chat-turn '+turn.role+'">'+esc(turn.text)+'</div>'
      ).join('');
      const p=result.plan;
      const filters=[p.kind,p.watch_status,p.genre,p.format,p.year_min&&'Depuis '+p.year_min,p.year_max&&'Jusqu’à '+p.year_max,p.rating_min!=null&&'Note ≥ '+p.rating_min,p.keywords].filter(Boolean);
      $('ai-search-feedback').textContent=result.total+' résultat(s) · '+(filters.join(' · ')||'Recherche sémantique')+
        (result.has_more?' · 60 premiers affichés':'')+
        
        (result.note?' · '+result.note:'');
      target.innerHTML=result.items.length
        ? '<div class="ai-results-grid">'+result.items.map(item=>
            '<button class="ai-result-card" type="button" data-ai-media="'+esc(item.id)+'">'+
            poster(item)+'<strong class="card-title">'+esc(item.title)+'</strong>'+
            '<small>'+esc([item.year,item.kind==='tvshow'?'Série':'Film'].filter(Boolean).join(' · '))+'</small>'+
            (result.relevance_reasons?.[item.id]
              ? '<span class="ai-relevance-reason">'+esc(result.relevance_reasons[item.id])+'</span>' : '')+
            (result.episode_matches?.[item.id]||[]).map(hit=>
              '<span class="ai-episode-hit" data-ai-media="'+esc(hit.id)+'">'+
              '<b>S'+String(hit.season??0).padStart(2,'0')+'E'+String(hit.episode??0).padStart(2,'0')+'</b> '+esc(hit.title)+
              '<small>'+esc(hit.plot||'')+'</small></span>'
            ).join('')+
            '</button>'
          ).join('')+'</div>'
        : '<p class="hint">Aucun résultat correspondant dans le catalogue.</p>';
      attachImageFallbacks(target);
    }catch(e){$('ai-search-feedback').textContent=e.message;}
    finally{button.disabled=false;}
  }
});
document.addEventListener('click',event=>{
  const b=event.target.closest('[data-ai-media]');
  if(b){void openDetail(b.dataset.aiMedia);}
});


document.addEventListener('click',async event=>{
  if(event.target.closest('#ai-reindex')){
    const button=$('ai-reindex');if(!button||button.disabled)return;
    button.disabled=true;
    try{
      const result=await api('ai/index/refresh',{method:'POST'});
      if(result.index && $('ai-index-progress')){
        $('ai-index-progress').textContent=result.index.indexed+' / '+result.index.total;
      }
      toast('Indexation OpenAI lancée. Seules les fiches modifiées seront recalculées.');
    }catch(e){toast(e.message)}finally{button.disabled=false}
    return;
  }
  if(event.target.closest('#ai-undo-search')){
    const previous=aiRefinementHistory.pop();
    if(!previous)return;
    aiPreviousPlan=previous.plan;
    aiConversation=previous.conversation;
    aiResultIDs=previous.ids;
    $('ai-chat-history').innerHTML=aiConversation.map(turn=>
      '<div class="ai-chat-turn '+turn.role+'">'+esc(turn.text)+'</div>').join('');
    $('ai-search-feedback').textContent=previous.feedback;
    $('ai-search-results').innerHTML=previous.html;
    $('ai-bulk-panel').hidden=true;
    updateAIResultActions();
    return;
  }
  if(event.target.closest('#ai-add-results-list')){
    if(state.role!=='admin'||!aiResultIDs.length)return;
    const panel=$('ai-bulk-panel');
    try {
      const lists=await api('lists');
      panel.innerHTML='<label>Liste<select id="ai-bulk-list">'+
        lists.map(l=>'<option value="'+esc(l.id)+'">'+esc(l.name)+'</option>').join('')+
        '<option value="new">＋ Nouvelle liste…</option></select></label> '+
        '<button id="ai-bulk-submit" type="button" class="primary">Ajouter '+aiResultIDs.length+' média(s)</button> '+
        '<button id="ai-bulk-close" type="button" class="secondary">Annuler</button> '+
        '<span id="ai-bulk-feedback" role="status"></span>';
      panel.hidden=false;
    }catch(e){toast(e.message)}
    return;
  }
  if(event.target.closest('#ai-bulk-close')){$('ai-bulk-panel').hidden=true;return;}
  if(event.target.closest('#ai-bulk-submit')){
    const ids=aiResultIDs.slice();
    if(!ids.length||state.role!=='admin')return;
    const button=$('ai-bulk-submit'),feedback=$('ai-bulk-feedback');
    button.disabled=true;feedback.textContent='Enregistrement…';
    try {
      let id=$('ai-bulk-list').value;
      if(id==='new'){
        const name=localizedPrompt('Nom de la nouvelle liste', 'Résultats IA');
        if(!name?.trim()){feedback.textContent='Création annulée.';return;}
        const list=await api('lists',{method:'POST',body:JSON.stringify({name:name.trim()})});
        id=list.id;
      }
      const added=await api('lists/'+encodeURIComponent(id)+'/items/bulk',{
        method:'POST',body:JSON.stringify({media_ids:ids})
      });
      feedback.textContent=added.added+' ajout(s) · '+added.already_present+' déjà présent(s).';
      await refreshHomeListThumbnails();
      toast(feedback.textContent);
    }catch(e){feedback.textContent=e.message}
    finally{button.disabled=false;}
    return;
  }
  if(event.target.closest('#ai-new-search')){
    aiPreviousPlan=null;aiConversation=[];aiResultIDs=[];aiRefinementHistory.length=0;
    updateAIResultActions();$('ai-bulk-panel').hidden=true;
    $('ai-chat-history').replaceChildren();
    $('ai-search-results').replaceChildren();$('ai-prompt').value='';
    $('ai-prompt').placeholder='Décris les films ou épisodes recherchés…';
    await refreshAIStatus();$('ai-prompt').focus();return;
  }

});

document.addEventListener('click',async event=>{
  if(!event.target.closest('#ai-test-connection'))return;
  const button=$('ai-test-connection'), output=$('ai-connection-result');
  if(!button||!output||button.disabled)return;
  button.disabled=true;
  output.textContent='Test du modèle IA en cours…';
  const messages={
    invalid_key:'Clé API invalide.',
    access_denied:'Accès refusé pour cette clé ou ce projet.',
    model_unavailable:'Modèle introuvable ou inaccessible avec cette clé.',
    quota_or_rate_limit:'Quota fournisseur dépassé ou limite de débit atteinte.',
    model_or_request_incompatible:'Le modèle ne prend pas en charge cette opération API.',
    timeout:'Le fournisseur IA ne répond pas dans le délai imparti.',
    network_error:'Le serveur ne parvient pas à joindre le fournisseur IA.',
    provider_unavailable:'Service IA temporairement indisponible.',
    invalid_response:'Réponse du fournisseur IA inattendue. Consultez les journaux serveur.'
  };
  try{
    const result=await api('ai/connection/test',{method:'POST'});
    output.innerHTML=[
      ['Modèle IA',result.language],
      ...(result.embedding?[["Embeddings OpenAI",result.embedding]]:[])
    ].map(([name,part])=>
      '<p class="'+(part.ok?'success-text':'error-text')+'">'+
      (part.ok?'✓ ':'✗ ')+esc(name)+' ('+esc(part.model)+') : '+
      (part.ok?'Connexion validée.':esc(messages[part.reason]||'Erreur fournisseur.'))+'</p>'
    ).join('');
  }catch(e){output.textContent='Test impossible : '+e.message}
  finally{button.disabled=false}
});


// Coordinated maintenance is explicitly initiated by an administrator.
// Kodi's clean operation must finish before the SQLite catalogue is normalized.
document.addEventListener('click',async event=>{
  const button=event.target.closest('#kodi-catalogue-clean');
  if(!button||state.role!=='admin')return;
  if(state.jobs.some(x=>['pending','running'].includes(x.status))){
    toast('Attendez la fin des synchronisations Kodi actives.');return;
  }
  if(!localizedConfirm('Vérifiez que tous les partages NAS sont accessibles. Nettoyer les bibliothèques Kodi peut retirer les fichiers déclarés absents. Continuer ?'))return;
  const approval=localizedPrompt('Saisissez NETTOYER pour confirmer :','');
  if(approval!=='NETTOYER')return;
  button.disabled=true;
  const output=$('catalogue-db-preview-result'),problems=[];
  try{
    const completed=new Set(),instances=new Set();
    for(const source of state.facets.sources.filter(x=>x.type==='kodi'&&x.enabled!==false)){
      const player=state.players.find(p=>p.source_id===source.id);
      if(!player){problems.push(source.name+' : pas de lecteur Kodi');continue;}
      const endpoint=player.host.toLowerCase()+':'+player.port;
      if(instances.has(endpoint))continue;
      instances.add(endpoint);
      try{
        const job=await api('sources/'+encodeURIComponent(source.id)+'/clean',{method:'POST'});
        let finished=false;
        for(let attempt=0;attempt<600;attempt++){
          const current=await api('jobs/'+encodeURIComponent(job.id));
          if(output)output.textContent=source.name+' : '+(current.progress_phase||current.status);
          if(current.status==='completed'){completed.add(source.id);finished=true;break;}
          if(current.status==='failed'){problems.push(source.name+' : '+(current.error||'échec'));finished=true;break;}
          await new Promise(resolve=>setTimeout(resolve,2000));
        }
        if(!finished)throw new Error('Kodi travaille encore : nettoyage SQLite non lancé.');
      }catch(e){if(e.message.includes('travaille encore'))throw e;problems.push(source.name+' : '+e.message);}
    }
    let report;
    for(let attempt=0;attempt<10;attempt++){
      try{
        report=await api('catalogue/maintenance/clean',{
          method:'POST',headers:{'X-Confirm-Catalogue-Maintenance':'clean'}
        });break;
      }catch(e){
        if(e.status!==409||attempt===9)throw e;
        await new Promise(resolve=>setTimeout(resolve,2000));
      }
    }
    if(output)output.textContent='Kodi : '+completed.size+'/'+instances.size+
      ' · Catalogue : '+report.merged_sources+' sources et '+report.merged_records+
      ' enregistrements fusionnés.'+(problems.length?' Attention : '+problems.join(' | '):'');
    await refresh();
  }catch(e){if(output)output.textContent='Maintenance interrompue : '+e.message;}
  finally{button.disabled=false;}
});

// Maintenance affects only this server's SQLite catalogue, never Kodi MyVideos.
// The reset requires both an explicit warning and the typed confirmation.
document.addEventListener('click',async event=>{
  const button=event.target.closest('#catalogue-db-preview,#catalogue-db-clean,#catalogue-db-reset');
  if(!button)return;
  const result=$('catalogue-db-preview-result');
  const operation=button.id==='catalogue-db-preview'?'preview':
    (button.id==='catalogue-db-clean'?'clean':'reset');
  if(state.role!=='admin'){if(result)result.textContent='Accès administrateur nécessaire.';return;}
  if(operation==='clean'&&!localizedConfirm(
    'Nettoyer les doublons et les orphelins du catalogue serveur ? La base Kodi MyVideos restera inchangée.'))return;
  if(operation==='reset'){
    if(!localizedConfirm('ATTENTION : supprimer TOUS les médias, sources, notes, listes et écritures en attente du catalogue serveur ? Cette opération est irréversible sans export JSON préalable.'))return;
    const token=window.prompt('Pour confirmer la réinitialisation, saisissez RÉINITIALISER :');
    if(token!=='RÉINITIALISER'){if(result)result.textContent='Réinitialisation annulée.';return;}
  }
  button.disabled=true;
  if(result)result.textContent=operation==='preview'?'Analyse en cours…':'Maintenance en cours…';
  try{
    if(operation==='preview'){
      const data=await api('catalogue/maintenance/preview');
      if(result)result.textContent=
        'Sources : '+data.sources+' · Médias : '+data.works+
        ' · Sources en double : '+data.duplicate_sources+
        ' · Entrées en double : '+data.duplicate_records+
        ' · Médias orphelins : '+data.orphan_works+
        ' · Connexions orphelines : '+data.orphan_players+
        ' · Enregistrements orphelins : '+data.orphan_records;
    }else{
      const data=await api('catalogue/maintenance/'+operation,{
        method:'POST',headers:{'X-Confirm-Catalogue-Maintenance':operation}
      });
      if(result)result.textContent=(operation==='clean'?'Nettoyage terminé. ':'Base réinitialisée. ')+
        (operation==='clean'?' · Sources fusionnées : '+data.merged_sources+
        ' · Enregistrements fusionnés : '+data.merged_records:'');
      toast(operation==='clean'?'Base nettoyée.':'Base serveur réinitialisée.');
      await refresh();
      // This view may be re-rendered by connections(). The toast contains the result.
      await connections();
    }
  }catch(error){if(result)result.textContent='Erreur : '+error.message;toast(error.message);}
  finally{button.disabled=false;}
});
