// Streaming web parity: personal heart shelves plus TMDB-backed discovery
// via the NAS. User state is synced with the iOS local-first journal.
import { tr, displayLocale } from './i18n.js';

const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const root = new URL('.', window.location.href);
const names = {
  netflix:'Netflix',disney_plus:'Disney+',prime_video:'Prime Video',
  apple_tv_plus:'Apple TV+',hbo_max:'HBO Max',paramount_plus:'Paramount+'
};
const genreOptions = [
  ['action','Action & adventure',28,10759],
  ['comedy','Comedy',35,35],
  ['drama','Drama',18,18],
  ['crime','Crime',80,80],
  ['science_fiction','Science fiction & fantasy',878,10765],
  ['family','Family',10751,10751],
  ['animation','Animation',16,16],
  ['documentary','Documentary',99,99]
];
const state = {
  rows:[], visible:[],subscriptions:[], kind:'both', region:'FR',
  query:'',loaded:false,loading:false,session:null,
  cache:new Map(),titles:new Map(),shelves:new Map(),
  observer:null,version:0,subview:null,selected:null,searchTimer:null
};
function preference(key,fallback) {
  const row=state.rows.find(r=>r.key==='prefs:'+key && !r.deleted);
  return typeof row?.payload?.value==='string'?row.payload.value:fallback;
}
function personalRows(kind) {
  return state.rows.filter(row=>!row.deleted &&
    /^item:(movie|tv):[0-9]+$/.test(row.key) &&
    (!kind || row.payload?.kind===kind) &&
    ['saved','pending'].includes(row.payload?.status) &&
    row.payload?.title)
    .sort((a,b)=>b.modified_at.localeCompare(a.modified_at));
}
function keyFor(title){return title.kind+':'+title.id;}
function savedRow(title){
  return state.rows.find(row=>!row.deleted&&row.key==='item:'+keyFor(title) &&
    ['saved','pending'].includes(row.payload?.status));
}
function normalizeTitle(row){
  const p=row.payload;
  return {id:Number(p.tmdb_id),kind:p.kind,title:p.title,
    original_title:p.original_title||p.title,year:Number(p.year)||null,
    poster_path:p.poster_path||null,backdrop_path:p.backdrop_path||null,
    overview:p.overview||'',rating:Number(p.rating)||0,
    service_keys:(p.services||'').split(',').filter(x=>names[x]),
    work_id:p.work_id||null};
}
function posterURL(raw,base='w342'){
  return typeof raw==='string'&&/^\/[a-z0-9_.-]{1,150}$/i.test(raw)
    ? 'https://image.tmdb.org/t/p/'+base+raw:'';
}
function validRegion(){
  const v=state.region.toUpperCase();
  return /^[A-Z]{2}$/.test(v)?v:'FR';
}
async function api(route,options={}){
  const response=await fetch(new URL('api/v1/'+route,root),{
    credentials:'same-origin',cache:'no-store',...options
  });
  if(!response.ok){
    let message='HTTP '+response.status;
    try{const data=await response.json();message=data.detail||data.error?.message||message;}catch{}
    throw new Error(typeof message==='string'?message:JSON.stringify(message));
  }
  return response.json();
}
function failure(text){$('streaming-error').textContent=text;$('streaming-error').hidden=false;}
function servicesFor(title){
  const saved=savedRow(title);
  const source=title.service_keys||saved?.payload?.services?.split(',')||[];
  return [...new Set(source)].filter(s=>names[s]);
}
function providerSearchURL(key,title) {
  const q=encodeURIComponent(title);
  switch(key){
    case 'netflix':return 'https://www.netflix.com/search?q='+q;
    case 'disney_plus':return 'https://www.disneyplus.com/search/'+q;
    case 'prime_video':return 'https://www.primevideo.com/search/?phrase='+q;
    case 'apple_tv_plus':return 'https://tv.apple.com/'+validRegion().toLowerCase()+'/search?term='+q;
    case 'hbo_max':return 'https://play.max.com/search?q='+q;
    case 'paramount_plus':return 'https://www.paramountplus.com/search/?q='+q;
    default:return '';
  }
}
function directURL(title,key){
  const pattern='link:'+title.kind+':'+title.id+':'+key;
  const scoped='link:'+validRegion()+':'+title.kind+':'+title.id+':'+key;
  const row=state.rows.find(r=>!r.deleted&&(r.key===scoped||
    (validRegion()==='FR'&&r.key===pattern)));
  const raw=row?.payload?.url;
  if(!raw)return '';
  try{
    const u=new URL(raw),host=u.hostname.toLowerCase();
    const allowed={netflix:['netflix.com'],disney_plus:['disneyplus.com'],
      prime_video:['primevideo.com'],apple_tv_plus:['tv.apple.com'],
      hbo_max:['max.com','hbomax.com'],paramount_plus:['paramountplus.com']}[key]||[];
    return u.protocol==='https:'&&!u.username&&!u.password&&
      allowed.some(h=>host===h||host.endsWith('.'+h))?u.href:'';
  }catch{return ''}
}
function card(title){
  const key=keyFor(title);
  state.titles.set(key,title);
  const poster=posterURL(title.poster_path),isSaved=!!savedRow(title);
  const badges=servicesFor(title).slice(0,4).map(s=>'<span title="'+esc(names[s])+'">'+esc(
    s==='netflix'?'N':s==='prime_video'?'Prime':s==='disney_plus'?'D+':s==='apple_tv_plus'?'Apple':s==='hbo_max'?'Max':'P+')+'</span>').join('');
  const year=title.year||'',rating=Number(title.rating)||0;
  return '<article class="streaming-tile"><div class="streaming-tile-art">'+
    '<button class="streaming-tile-poster" type="button" data-streaming-card="'+esc(key)+'" aria-label="'+esc(title.title)+'">'+
    (poster?'<img loading="lazy" decoding="async" src="'+esc(poster)+'" alt="">':
      '<span>'+esc((title.title||'?').slice(0,2).toUpperCase())+'</span>')+'</button>'+
    '<button type="button" class="streaming-heart'+(isSaved?' saved':'')+
    '" aria-label="'+esc(isSaved?'In my catalog':'Add to catalog')+
    '" title="'+esc(isSaved?'In my catalog':'Add to catalog')+
    '" data-streaming-heart="'+esc(key)+'">'+(isSaved?'♥':'♡')+'</button>'+
    (rating>0?'<span class="streaming-rating">★ '+rating.toFixed(1)+'</span>':'')+
    (badges?'<span class="streaming-tile-services">'+badges+'</span>':'')+'</div>'+
    '<div class="streaming-tile-title" title="'+esc(title.title)+'">'+esc(title.title)+'</div>'+
    '<div class="streaming-tile-year">'+esc(year)+'</div></article>';
}
function shelf(id,heading,content,notes='',more=true) {
  return '<section class="streaming-shelf" data-streaming-shelf="'+esc(id)+'">'+
    '<div class="streaming-shelf-header"><h3>'+esc(heading)+'</h3>'+
    (notes?'<span class="streaming-shelf-note">'+esc(notes)+'</span>':'')+'</div>'+
    '<div class="streaming-shelf-track">'+content+'</div>'+
    (more?'<button type="button" class="streaming-see-more" data-streaming-more="'+
      esc(id)+'">'+esc('See more titles')+'</button>':'')+'</section>';
}
function renderSaved(){
  const groups=[['movie','My Movies'],['tv','My Series']];
  $('streaming-saved').innerHTML=groups.map(([kind,heading])=>{
    const titles=personalRows(kind).map(normalizeTitle);
    if(!titles.length)return '';
    state.shelves.set('saved-'+kind,{heading,items:titles,kind,saved:true});
    return shelf('saved-'+kind,heading,titles.map(card).join(''));
  }).join('');
}
function subscribedServices(){
  const all=Object.keys(names);
  const subs=preference('subscriptions',all.join(',')).split(',').filter(s=>names[s]);
  return all.filter(s=>subs.includes(s));
}
function filterChips(){
  const subs=state.subscriptions;
  const fullySelected=subs.length>0&&state.visible.length===subs.length;
  $('streaming-services').innerHTML=
    '<button type="button" aria-pressed="'+fullySelected+'" data-streaming-service="all">'+
    esc('All')+'</button>'+
    subs.map(s=>'<button type="button" data-streaming-service="'+esc(s)+
      '" aria-pressed="'+state.visible.includes(s)+'"><span class="streaming-service-dot '+
      esc(s)+'"></span>'+esc(names[s])+'</button>').join('');
}
function computeShelves(){
  const kinds=state.kind==='both'?['tv','movie']:[state.kind];
  const selections=preference('genres','action,drama,comedy').split(',');
  const genres=genreOptions.filter(g=>selections.includes(g[0]));
  const result=[];
  if(preference('show_tops','true')==='true')
    for(const kind of kinds) result.push({kind,sort:'top',genre:null,
      heading:kind==='tv'?'Popular series across my services':'Popular movies across my services'});
  if(preference('show_recent','true')==='true')
    for(const kind of kinds) result.push({kind,sort:'recent',genre:null,
      heading:kind==='tv'?'Recently available · series':'Recently available · movies'});
  for(const g of genres)for(const kind of kinds)
    result.push({kind,sort:'popularity',genre:kind==='tv'?g[3]:g[2],
      heading:g[1]+' · '+(kind==='tv'?'series':'movies')});
  return result;
}
let activeLoads=0;
const queue=[];
function limited(operation) {
  return new Promise((resolve,reject)=>{
    queue.push({operation,resolve,reject});
    pump();
  });
}
function pump() {
  while(activeLoads<3&&queue.length){
    const job=queue.shift();
    activeLoads++;
    Promise.resolve().then(job.operation).then(job.resolve,job.reject).finally(()=>{
      activeLoads--;pump();
    });
  }
}
function cacheKey(def,page=1) {
  return [def.kind,def.sort,def.genre||'',state.visible.join(','),
    validRegion(),page].join(':');
}
async function retrieve(def,page=1){
  const key=cacheKey(def,page);
  if(state.cache.has(key))return state.cache.get(key);
  const params=new URLSearchParams({
    service:state.visible.join(','),kind:def.kind,region:validRegion()
  });
  let endpoint;
  if(def.sort==='top'&&page===1){
    endpoint='streaming/top';
  }else{
    endpoint='streaming/discover';
    params.set('page',String(page));
    params.set('sort',def.sort==='top'?'popularity':def.sort);
    if(def.genre)params.set('genre',String(def.genre));
  }
  const value=await limited(()=>api(endpoint+'?'+params));
  state.cache.set(key,value);
  return value;
}
function setupDiscover(){
  state.version++;
  if(state.observer)state.observer.disconnect();
  const list=$('streaming-discover');
  list.innerHTML='';
  const version=state.version;
  if(!state.visible.length){
    list.innerHTML='<p class="streaming-shelf-message">'+esc(
      'Choose a subscription service to discover titles.')+'</p>';
    return;
  }
  for(const [i,def] of computeShelves().entries()){
    def.id='discovery-'+i+'-'+def.kind+'-'+def.sort;
    state.shelves.set(def.id,{...def,items:[],page:1,total:1,loaded:false});
    const element=document.createElement('div');
    element.dataset.discoveryKey=def.id;
    element.innerHTML=shelf(def.id,def.heading,
      '<div class="streaming-shelf-loading">Loading…</div>','',false);
    list.append(element);
  }
  async function populate(element) {
    const id=element.dataset.discoveryKey;
    const def=state.shelves.get(id);
    if(!def||def.loaded)return;
    def.loaded=true;
    try{
      const result=await retrieve(def);
      if(version!==state.version)return;
      def.items=Array.isArray(result.items)?result.items:[];
      def.total=result.total_pages||1;
      if(def.sort==='top'&&result.ranking_type==='official_weekly')
        def.heading=def.kind==='tv'?'Top 10 series · Netflix (weekly)':
          'Top 10 movies · Netflix (weekly)';
      const note=def.sort==='top'&&result.ranking_type==='official_weekly'?'Official':
        def.sort==='top'?'TMDB':'';
      element.innerHTML=def.items.length
        ?shelf(id,def.heading,def.items.slice(0,20).map(card).join(''),note,
          def.items.length>0)
        :shelf(id,def.heading,'<p class="streaming-shelf-message">'+
          esc('No titles available for this selection.')+'</p>','',false);
    }catch(e){
      if(version!==state.version)return;
      def.loaded=false;
      element.innerHTML=shelf(id,def.heading,
        '<p class="streaming-shelf-message">'+esc(e.message)+'</p>','',false);
    }
  }
  if('IntersectionObserver' in window){
    state.observer=new IntersectionObserver(entries=>{
      for(const entry of entries)if(entry.isIntersecting){
        state.observer.unobserve(entry.target);
        void populate(entry.target);
      }
    },{rootMargin:'380px'});
    for(const el of list.querySelectorAll('[data-discovery-key]'))
      state.observer.observe(el);
  } else {
    for(const el of list.querySelectorAll('[data-discovery-key]'))
      void populate(el);
  }
}
let searchGeneration=0;
async function performSearch(){
  const q=state.query.trim(),gen=++searchGeneration;
  if(q.length<2){
    setupDiscover();
    return;
  }
  state.version++;
  if(state.observer)state.observer.disconnect();
  $('streaming-discover').innerHTML='<div class="streaming-shelf-loading">Searching…</div>';
  if(!state.visible.length){
    $('streaming-discover').innerHTML='';
    return;
  }
  try{
    const params=new URLSearchParams({
      q:q,service:state.visible.join(','),kind:state.kind,region:validRegion()
    });
    const result=await api('streaming/search?'+params);
    if(gen!==searchGeneration)return;
    $('streaming-discover').innerHTML='';
    for(const kind of (state.kind==='both'?['tv','movie']:[state.kind])){
      const items=(result.items||[]).filter(x=>x.kind===kind);
      if(!items.length)continue;
      const id='search-'+kind;
      const heading=kind==='tv'?'Series · search results':'Movies · search results';
      state.shelves.set(id,{id,kind,heading,items,saved:true});
      $('streaming-discover').insertAdjacentHTML('beforeend',
        shelf(id,heading,items.map(card).join('')));
    }
    if(!$('streaming-discover').childElementCount)
      $('streaming-discover').innerHTML='<p class="streaming-shelf-message">No titles found.</p>';
  }catch(e){if(gen===searchGeneration)failure(e.message);}
}
function renderBrowser(){
  state.titles.clear();
  state.shelves.clear();
  filterChips();
  renderSaved();
  if(state.query.trim().length>=2)void performSearch();
  else setupDiscover();
  const saved=personalRows().length;
  $('streaming-summary').textContent=saved+' '+tr('titres enregistrés')+
    ' · '+state.visible.length+' '+tr('services');
}
function showSubview(id){
  const def=state.shelves.get(id);
  if(!def)return;
  state.subview={id};
  $('streaming-browser').hidden=true;
  $('streaming-subview').hidden=false;
  $('streaming-subview-title').textContent=def.heading;
  $('streaming-subview-grid').innerHTML=def.items.map(card).join('');
  $('streaming-subview-more').hidden=!!def.saved||def.page>=def.total;
  $('streaming-subview').scrollIntoView({block:'start'});
}
function closeSubview(){
  state.subview=null;
  $('streaming-subview').hidden=true;
  $('streaming-browser').hidden=false;
}
async function loadNext(){
  const def=state.shelves.get(state.subview?.id);
  if(!def||def.saved||def.page>=def.total)return;
  const button=$('streaming-subview-more');
  button.disabled=true;
  try{
    const response=await retrieve(def,def.page+1);
    const present=new Set(def.items.map(keyFor));
    const next=(response.items||[]).filter(x=>!present.has(keyFor(x)));
    def.items.push(...next);
    def.page++;
    def.total=response.total_pages||def.total;
    $('streaming-subview-grid').insertAdjacentHTML('beforeend',next.map(card).join(''));
    button.hidden=def.page>=def.total;
  }catch(e){failure(e.message);}finally{button.disabled=false;}
}
function details(key){
  const title=state.titles.get(key);
  if(!title)return;
  state.selected=key;
  const poster=posterURL(title.poster_path,'w500');
  const available=servicesFor(title);
  const links=available.map(s=>{
    const direct=directURL(title,s);
    const fallback=providerSearchURL(s,title.title);
    const destination=direct||fallback;
    return destination?'<a href="'+esc(destination)+'" target="_blank" rel="noopener noreferrer">'+
      esc((direct?'Open on ':'Search on ')+names[s])+'</a>':'';
  }).join('');
  $('streaming-title-content').innerHTML=
    '<div class="streaming-detail-layout">'+
    (poster?'<img src="'+esc(poster)+'" alt="">':'<div></div>')+
    '<div><h2 id="streaming-detail-heading">'+esc(title.title)+'</h2>'+
    '<p>'+esc([title.kind==='tv'?'Series':'Movie',title.year].filter(Boolean).join(' · '))+'</p>'+
    '<div class="streaming-detail-actions"><button type="button" class="secondary" data-streaming-save-detail="'+
      esc(key)+'">'+esc(savedRow(title)?'♥ In my catalog':'♡ Add to catalog')+'</button>'+
    (savedRow(title)?.payload?.work_id?
      '<button type="button" class="secondary" data-streaming-work="'+
      esc(savedRow(title).payload.work_id)+'">Open catalog entry</button>':'')+
    '</div><div class="streaming-detail-services">'+links+'</div></div></div>'+
    (title.overview?'<p class="synopsis">'+esc(title.overview)+'</p>':'');
  $('streaming-title-dialog').showModal();
}
async function save(title){
  if(savedRow(title))return;
  try{
    if(!state.session)state.session=await api('auth/session');
    if(state.session.role!=='admin')throw new Error('Administrator access required to save streaming titles.');
    const selected=servicesFor(title).filter(x=>names[x]);
    const membership=selected.length?selected:state.visible;
    const key='item:'+keyFor(title);
    const original=state.rows.find(row=>row.key===key);
    const nextTime=new Date(Math.max(Date.now(),Date.parse(original?.modified_at||'')+2||0)).toISOString();
    const payload={
      tmdb_id:String(title.id),kind:title.kind,title:title.title,
      year:title.year?String(title.year):'',poster_path:title.poster_path||'',
      backdrop_path:title.backdrop_path||'',services:membership.join(','),
      status:'pending'
    };
    const result=await api('streaming/state/sync',{
      method:'POST',headers:{
        'Content-Type':'application/json','X-CSRF-Token':state.session.csrf_token
      },body:JSON.stringify({changes:[{key,payload,modified_at:nextTime,deleted:false}]})
    });
    state.rows=result.rows;
    renderBrowser();
    if(state.subview)showSubview(state.subview.id);
    if(state.selected===keyFor(title))details(state.selected);
  }catch(e){failure(e.message);}
}
async function load(){
  if(state.loading)return;
  state.loading=true;
  $('streaming-refresh').disabled=true;
  $('streaming-error').hidden=true;
  try{
    const [response,session]=await Promise.all([
      api('streaming/state'),api('auth/session')
    ]);
    if(!Array.isArray(response.rows))throw new Error('Invalid streaming state.');
    state.rows=response.rows;
    state.session=session;
    state.subscriptions=subscribedServices();
    state.visible=preference('visible_services',state.subscriptions.join(','))
      .split(',').filter(s=>state.subscriptions.includes(s));
    state.kind=preference('kinds','both');
    if(!['both','tv','movie'].includes(state.kind))state.kind='both';
    $('streaming-kind').value=state.kind;
    state.loaded=true;
    renderBrowser();
  }catch(e){failure(e.message);}finally{
    state.loading=false;$('streaming-refresh').disabled=false;
  }
}
function route(){
  const active=window.location.hash==='#streaming';
  $('streaming-page').hidden=!active;
  $('streaming-link').setAttribute('aria-current',active?'page':'false');
  if(active&&!$('app').hidden)void load();
  if(!active&&state.observer)state.observer.disconnect();
}
export function startStreaming(){
  if(!$('streaming-page'))return;
  try{
    const cached=localStorage.getItem('hmc-streaming-region');
    const language=navigator.language||'fr-FR';
    const guessed=language.split('-').slice(-1)[0].toUpperCase();
    state.region=cached||(/^[A-Z]{2}$/.test(guessed)?guessed:'FR');
  }catch{state.region='FR';}
  if(![...$('streaming-region').options].some(x=>x.value===state.region))state.region='FR';
  $('streaming-region').value=state.region;
  $('streaming-region').addEventListener('change',e=>{
    state.region=e.target.value;
    try{localStorage.setItem('hmc-streaming-region',state.region);}catch{}
    state.cache.clear();renderBrowser();
  });
  $('streaming-search').addEventListener('input',e=>{
    state.query=e.target.value;
    clearTimeout(state.searchTimer);
    state.searchTimer=setTimeout(()=>renderBrowser(),350);
  });
  $('streaming-kind').addEventListener('change',e=>{
    state.kind=e.target.value;renderBrowser();
  });
  $('streaming-refresh').addEventListener('click',()=>void load());
  $('streaming-services').addEventListener('click',event=>{
    const button=event.target.closest('[data-streaming-service]');
    if(!button)return;
    const key=button.dataset.streamingService;
    if(key==='all')state.visible=[...state.subscriptions];
    else if(state.visible.includes(key))state.visible=state.visible.filter(x=>x!==key);
    else if(state.subscriptions.includes(key))state.visible.push(key);
    renderBrowser();
  });
  $('streaming-page').addEventListener('click',event=>{
    const heart=event.target.closest('[data-streaming-heart],[data-streaming-save-detail]');
    if(heart){
      const title=state.titles.get(heart.dataset.streamingHeart||heart.dataset.streamingSaveDetail);
      if(title)void save(title);
      return;
    }
    const tile=event.target.closest('[data-streaming-card]');
    if(tile){details(tile.dataset.streamingCard);return;}
    const more=event.target.closest('[data-streaming-more]');
    if(more){showSubview(more.dataset.streamingMore);return;}
    const work=event.target.closest('[data-streaming-work]');
    if(work){
      $('streaming-title-dialog').close();
      window.dispatchEvent(new CustomEvent('streaming-open-media',{detail:{mediaID:work.dataset.streamingWork}}));
    }
  });
  $('streaming-back').addEventListener('click',closeSubview);
  $('streaming-subview-more').addEventListener('click',()=>void loadNext());
  $('streaming-detail-close').addEventListener('click',()=>$('streaming-title-dialog').close());
  window.addEventListener('hashchange',route);
  window.addEventListener('media-app-ready',route);
  route();
}
