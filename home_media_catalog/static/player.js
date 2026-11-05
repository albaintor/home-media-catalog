/* Full-window web video player, modelled on the iOS app's on-screen controls:
 * audio tracks, subtitles (text as WebVTT, bitmap burned in by the server),
 * chapters, aspect ratio, speed, technical details, PiP and keyboard control.
 *
 * Two playback modes share one timeline:
 *  - direct: the browser reads the original file with HTTP ranges;
 *  - stream: the server restarts a fragmented MP4 at the requested position
 *    with the chosen audio track (video copied when the browser supports it,
 *    otherwise converted). Displayed time = stream offset + video time.
 */
import { tr, displayLocale } from './i18n.js';

const PREFS_KEY='media_catalog.player.v1:'+new URL('.',location.href).pathname;
const SUBTITLE_SIZES={s:.75,m:1,l:1.3,xl:1.65};
const SPEEDS=[.5,.75,1,1.25,1.5,2];
const QUALITIES=[2160,1080,720,480];
const FITS={contain:'Ajuster',cover:'Remplir',fill:'Étirer'};
const ZOOMS=[1,1.1,1.25,1.33];
const BIBLIOGRAPHIC={fre:'fra',ger:'deu',dut:'nld',chi:'zho',cze:'ces',gre:'ell',per:'fas',rum:'ron',slo:'slk',ice:'isl',arm:'hye',baq:'eus',geo:'kat',mac:'mkd',may:'msa',alb:'sqi',bur:'mya',wel:'cym',tib:'bod',scc:'srp',scr:'hrv'};
const AUDIO_CODECS={truehd:'Dolby TrueHD',eac3:'Dolby Digital Plus',ac3:'Dolby Digital',dts:'DTS',aac:'AAC',mp3:'MP3',opus:'Opus',flac:'FLAC',vorbis:'Vorbis',mp2:'MPEG Audio'};
const AUDIO_MIME={aac:'mp4a.40.2',mp3:'mp4a.6B',opus:'opus',flac:'flac',ac3:'ac-3',eac3:'ec-3'};
const VIDEO_MIME={h264:'avc1.640028',hevc:'hvc1.2.4.L153.B0',vp9:'vp09.00.40.08',av1:'av01.0.08M.08'};
const DIRECT_EXTENSIONS=new Set(['mp4','m4v','mov','webm']);

const ICONS={
  play:'<path d="M8 5.5v13l11-6.5z"/>',
  pause:'<path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z"/>',
  back:'<path d="M12 5V2L7 6l5 4V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z"/><text x="12" y="16.3" font-size="6.4" text-anchor="middle" font-family="system-ui" font-weight="700">10</text>',
  forward:'<path d="M12 5V2l5 4-5 4V7a6 6 0 1 0 6 6h2a8 8 0 1 1-8-8z"/><text x="12" y="16.3" font-size="6.4" text-anchor="middle" font-family="system-ui" font-weight="700">10</text>',
  previous:'<path d="M6 6h2v12H6zM9.5 12 18 18V6z"/>',
  next:'<path d="M16 6h2v12h-2zM6 18l8.5-6L6 6z"/>',
  volume:'<path d="M4 9v6h4l5 5V4L8 9zM15.5 12a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4zM13 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z"/>',
  muted:'<path d="M4 9v6h4l5 5V4L8 9zM16.6 12l2.7-2.7-1.1-1.1-2.7 2.7-2.7-2.7-1.1 1.1 2.7 2.7-2.7 2.7 1.1 1.1 2.7-2.7 2.7 2.7 1.1-1.1z"/>',
  audio:'<path d="M3 10h2v4H3zM7 7h2v10H7zM11 4h2v16h-2zM15 8h2v8h-2zM19 11h2v2h-2z"/>',
  subtitles:'<path d="M4 5h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm0 2v10h16V7zm2 6h5v2H6zm7 0h5v2h-5zM6 10h12v2H6z"/>',
  chapters:'<path d="M4 6h2v2H4zM8 6h12v2H8zM4 11h2v2H4zM8 11h12v2H8zM4 16h2v2H4zM8 16h12v2H8z"/>',
  aspect:'<path d="M3 5h18v14H3zm2 2v10h14V7zm1 1h4v2H8v2H6zm12 8h-4v-2h2v-2h2z"/>',
  settings:'<path d="M19.4 13a7.5 7.5 0 0 0 0-2l2.1-1.6-2-3.5-2.5 1a7.4 7.4 0 0 0-1.7-1L15 3.3h-4l-.4 2.6a7.4 7.4 0 0 0-1.7 1l-2.5-1-2 3.5L6.6 11a7.5 7.5 0 0 0 0 2l-2.2 1.6 2 3.5 2.5-1a7.4 7.4 0 0 0 1.7 1l.4 2.6h4l.4-2.6a7.4 7.4 0 0 0 1.7-1l2.5 1 2-3.5zM13 15.5a3.5 3.5 0 1 1 0-7 3.5 3.5 0 0 1 0 7z" transform="translate(-1 0)"/>',
  info:'<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-6h2zm0-8h-2V7h2z"/>',
  pip:'<path d="M3 5h18v14H3zm2 2v10h14V7zm6 4h7v5h-7z"/>',
  fullscreen:'<path d="M4 4h6v2H6v4H4zM14 4h6v6h-2V6h-4zM4 14h2v4h4v2H4zM18 14h2v6h-6v-2h4z"/>',
  exitFullscreen:'<path d="M8 4h2v6H4V8h4zM14 4h2v4h4v2h-6zM4 14h6v6H8v-4H4zM14 14h6v2h-4v4h-2z"/>',
  close:'<path d="M6.4 5 12 10.6 17.6 5 19 6.4 13.4 12l5.6 5.6-1.4 1.4-5.6-5.6L6.4 19 5 17.6l5.6-5.6L5 6.4z"/>',
};
const icon=(name)=>`<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">${ICONS[name]}</svg>`;
const number=(value,digits=1)=>new Intl.NumberFormat(displayLocale(),{minimumFractionDigits:digits,maximumFractionDigits:digits}).format(value);
const esc=(v)=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function readPrefs() {
  try { const value=JSON.parse(localStorage.getItem(PREFS_KEY)); return value&&typeof value==='object'?value:{}; }
  catch { return {}; }
}
function writePrefs(prefs) {
  try { localStorage.setItem(PREFS_KEY,JSON.stringify(prefs)); } catch { /* Preferences are optional. */ }
}
export function clock(seconds) {
  if(!Number.isFinite(seconds)||seconds<0)return '--:--';
  const s=Math.floor(seconds), h=Math.floor(s/3600), m=Math.floor(s/60)%60, r=String(s%60).padStart(2,'0');
  return h?`${h}:${String(m).padStart(2,'0')}:${r}`:`${m}:${r}`;
}
export function normalizeLanguage(code) {
  const value=String(code||'').toLowerCase();
  return BIBLIOGRAPHIC[value]||value;
}
export function languageName(code) {
  if(!code)return null;
  const normalized=normalizeLanguage(code);
  try {
    const name=new Intl.DisplayNames([displayLocale()],{type:'language'}).of(normalized);
    if(name&&name.toLowerCase()!==normalized)return name.charAt(0).toUpperCase()+name.slice(1);
  } catch { /* Unknown or invalid code. */ }
  return String(code).toUpperCase();
}
function channelsLabel(channels) {
  return {1:'Mono',2:tr('Stéréo'),6:'5.1',7:'6.1',8:'7.1'}[channels]||(channels?`${channels} ch`:'');
}
export function audioLabel(track) {
  const codec=track.codec==='dts'&&track.profile?track.profile:AUDIO_CODECS[track.codec]||(track.codec||'').toUpperCase();
  const name=languageName(track.language)||`${tr('Piste')} ${track.order+1}`;
  const details=[codec,channelsLabel(track.channels)].filter(Boolean).join(' ');
  const title=track.title&&!track.title.toLowerCase().includes((languageName(track.language)||'\u0000').toLowerCase())?track.title:'';
  return {name, details:[details,title].filter(Boolean).join(' · ')};
}
export function subtitleLabel(track) {
  const name=languageName(track.language)||`${tr('Sous-titres')} ${track.order+1}`;
  const flags=[track.forced&&tr('Forcés'),track.hearing_impaired&&tr('Malentendants'),track.title,
    track.kind==='bitmap'?tr('Image (incrustés par le serveur)'):track.kind==='unsupported'?tr('Format non pris en charge'):''].filter(Boolean);
  return {name, details:flags.join(' · ')};
}

/* Minimal WebVTT parser: cue timing plus a safe subset of inline markup. */
function vttTime(value) {
  const parts=value.trim().split(':').map(Number);
  if(parts.some(n=>!Number.isFinite(n)))return NaN;
  return parts.reduce((total,part)=>total*60+part,0);
}
function cueHTML(text) {
  return esc(text)
    .replace(/&lt;(\/?)(i|b|u)&gt;/g,'<$1$2>')
    .replace(/&lt;[^&]*?&gt;/g,'')
    .replace(/\{\\[^}]*\}/g,'')
    .replace(/\n/g,'<br>');
}
export function parseVTT(source) {
  const cues=[];
  for(const block of String(source).replace(/\r/g,'').split(/\n{2,}/)) {
    const lines=block.split('\n');
    const at=lines.findIndex(line=>line.includes('-->'));
    if(at<0)continue;
    const [from,rest]=lines[at].split('-->');
    const start=vttTime(from), end=vttTime(rest.trim().split(/\s+/)[0]);
    const text=lines.slice(at+1).join('\n').trim();
    if(Number.isFinite(start)&&Number.isFinite(end)&&end>start&&text)cues.push({start,end,html:cueHTML(text)});
  }
  return cues.sort((a,b)=>a.start-b.start);
}

export class MediaPlayer {
  constructor({fetchJSON, fetchText}) {
    this.fetchJSON=fetchJSON; this.fetchText=fetchText;
    this.prefs=readPrefs();
    this.root=null; this.source=null; this.info=null;
    this.mode='direct'; this.offset=0; this.audio=null; this.subtitle=null; this.cues=[];
    this.subtitleDelay=0; this.zoom=1; this.fit='contain'; this.panel=null;
    this.hideTimer=null; this.seekTimer=null; this.scrubbing=false; this.loadToken=0; this.frame=0;
    this.stalls=0;this.waitingStarted=null;this.waitingSeconds=0;this.lastBufferAhead=0;
  }

  /* ----- public API ----- */
  async open(source) {
    this.build();
    this.reset();
    this.source=source;
    this.$('.mp-title').textContent=source.title||'';
    this.$('.mp-subtitle').textContent=source.subtitle||'';
    if(!this.root.open)this.root.showModal();
    document.documentElement.classList.add('media-player-open');
    this.setStatus(tr('Analyse du fichier (pistes, chapitres)…'),true);
    this.showOSD();
    const token=++this.loadToken;
    if(source.probeURL) {
      try { this.info=await this.fetchJSON(source.probeURL); }
      catch(error) { this.info=null; console.info('Media probe unavailable',error); }
    }
    if(token!==this.loadToken)return;
    this.chooseInitialTracks();
    this.renderChapters();
    this.updateButtons();
    this.mediaSession();
    const start=Math.max(0,Number(source.resume)||0);
    if(this.canPlayDirect())this.loadDirect(start);
    else if(this.info&&source.streamURL)this.loadStream(start);
    else this.loadDirect(start);
    if(this.subtitle!=null)void this.loadSubtitle(this.subtitle);
  }
  close() {
    if(this.root?.open)this.root.close();
  }

  /* ----- construction ----- */
  $(selector){ return this.root.querySelector(selector); }
  build() {
    if(this.root)return;
    const button=(action,name,label,extra='')=>`<button type="button" class="mp-button" data-mp="${action}" aria-label="${esc(tr(label))}" title="${esc(tr(label))}" ${extra}>${icon(name)}</button>`;
    const dialog=document.createElement('dialog');
    dialog.className='media-player';
    dialog.setAttribute('aria-label',tr('Lecteur vidéo'));
    dialog.innerHTML=`<div class="mp-stage">
      <video class="mp-video" playsinline preload="auto"></video>
      <div class="mp-captions" aria-live="off"></div>
      <div class="mp-spinner" hidden><span></span></div>
      <div class="mp-osd">
        <div class="mp-top">
          <div class="mp-heading"><strong class="mp-title"></strong><span class="mp-subtitle"></span></div>
          <span class="mp-mode" hidden></span>
          ${button('close','close','Fermer le lecteur','data-mp-close')}
        </div>
        <div class="mp-center">
          ${button('back','back','Reculer de 10 secondes')}
          <button type="button" class="mp-button mp-big" data-mp="toggle" aria-label="${esc(tr('Lecture'))}">${icon('play')}</button>
          ${button('forward','forward','Avancer de 10 secondes')}
        </div>
        <p class="mp-status" role="status" aria-live="polite"></p>
        <div class="mp-bottom">
          <div class="mp-timeline">
            <span class="mp-time mp-current">0:00</span>
            <div class="mp-seek" role="slider" tabindex="0" aria-label="${esc(tr('Position de lecture'))}" aria-valuemin="0" aria-valuemax="0" aria-valuenow="0">
              <div class="mp-rail"><div class="mp-buffered"></div><div class="mp-played"></div><div class="mp-marks"></div></div>
              <div class="mp-thumb"></div><div class="mp-tooltip" hidden></div>
            </div>
            <span class="mp-time mp-duration">--:--</span>
          </div>
          <div class="mp-controls">
            <div class="mp-group">
              ${button('toggle','play','Lecture')}
              ${button('previous','previous','Chapitre précédent','data-mp-chapter')}
              ${button('next','next','Chapitre suivant','data-mp-chapter')}
              <div class="mp-volume">
                ${button('mute','volume','Couper le son')}
                <input type="range" class="mp-volume-range" min="0" max="1" step="0.05" aria-label="${esc(tr('Volume'))}">
              </div>
            </div>
            <div class="mp-group">
              ${button('panel:audio','audio','Pistes audio')}
              ${button('panel:subtitles','subtitles','Sous-titres')}
              ${button('panel:chapters','chapters','Chapitres','data-mp-chapter')}
              ${button('panel:aspect','aspect','Format d’image')}
              ${button('panel:settings','settings','Réglages de lecture')}
              ${button('panel:info','info','Informations techniques')}
              ${button('pip','pip','Image dans l’image','data-mp-pip')}
              ${button('fullscreen','fullscreen','Plein écran')}
            </div>
          </div>
        </div>
      </div>
      <section class="mp-panel" hidden aria-live="polite"></section>
    </div>`;
    document.body.append(dialog);
    this.root=dialog;
    this.video=this.$('.mp-video');
    this.bind();
  }
  bind() {
    const video=this.video, root=this.root;
    root.addEventListener('click',event=>{
      const target=event.target.closest('[data-mp]');
      if(target){ event.stopPropagation(); if(!target.disabled)this.action(target.dataset.mp); return; }
      if(event.target.closest('.mp-panel,.mp-bottom,.mp-top'))return;
      if(this.panel){ this.closePanel(); return; }
      // Touch: first tap reveals the controls; mouse: click toggles playback.
      if(event.pointerType==='touch'&&!root.classList.contains('mp-osd-visible')) { this.showOSD(); return; }
      if(event.target.closest('.mp-stage'))this.toggle();
    });
    root.addEventListener('dblclick',event=>{
      if(!event.target.closest('.mp-bottom,.mp-top,.mp-panel,.mp-center'))this.toggleFullscreen();
    });
    root.addEventListener('pointermove',()=>this.showOSD());
    root.addEventListener('keydown',event=>this.keyboard(event));
    root.addEventListener('cancel',event=>{
      if(this.panel){ event.preventDefault(); this.closePanel(); }
    });
    root.addEventListener('close',()=>this.teardown());
    const sync=()=>this.updateTime();
    for(const name of ['timeupdate','progress','durationchange','loadedmetadata'])video.addEventListener(name,sync);
    video.addEventListener('play',()=>{this.updateButtons();this.scheduleHide();this.loop();});
    video.addEventListener('pause',()=>{this.updateButtons();this.showOSD(true);});
    video.addEventListener('waiting',()=>{
      if(this.waitingStarted===null){this.waitingStarted=performance.now();this.stalls++;}
      this.spinner(true);
    });
    video.addEventListener('stalled',()=>{
      if(this.waitingStarted===null){this.waitingStarted=performance.now();this.stalls++;}
      this.spinner(true);
    });
    video.addEventListener('seeking',()=>this.spinner(true));
    video.addEventListener('playing',()=>{this.finishWaiting();this.spinner(false);this.setStatus('');});
    video.addEventListener('canplay',()=>this.spinner(false));
    video.addEventListener('seeked',()=>this.spinner(false));
    video.addEventListener('ended',()=>{
      if(this.mode==='stream'&&this.duration()-this.currentTime()>5) { this.loadStream(this.currentTime()); return; }
      this.showOSD(true);
    });
    video.addEventListener('volumechange',()=>{
      this.prefs.volume=video.volume; this.prefs.muted=video.muted; writePrefs(this.prefs); this.updateButtons();
    });
    video.addEventListener('error',()=>this.failed());
    video.addEventListener('enterpictureinpicture',()=>this.updateButtons());
    video.addEventListener('leavepictureinpicture',()=>this.updateButtons());
    document.addEventListener('fullscreenchange',()=>this.updateButtons());
    document.addEventListener('webkitfullscreenchange',()=>this.updateButtons());
    this.$('.mp-volume-range').addEventListener('input',event=>{
      video.volume=Number(event.target.value); video.muted=video.volume===0;
    });
    const seek=this.$('.mp-seek');
    const position=event=>{
      const box=seek.getBoundingClientRect();
      return Math.min(1,Math.max(0,(event.clientX-box.left)/box.width))*this.duration();
    };
    seek.addEventListener('pointerdown',event=>{
      if(!this.duration())return;
      seek.setPointerCapture(event.pointerId);
      this.scrubbing=true; this.scrubTime=position(event); this.updateTime();
    });
    seek.addEventListener('pointermove',event=>{
      const time=position(event);
      this.tooltip(time,event);
      if(this.scrubbing){ this.scrubTime=time; this.updateTime(); }
    });
    seek.addEventListener('pointerleave',()=>{ if(!this.scrubbing)this.$('.mp-tooltip').hidden=true; });
    const release=event=>{
      if(!this.scrubbing)return;
      this.scrubbing=false; this.$('.mp-tooltip').hidden=true;
      this.seek(position(event));
    };
    seek.addEventListener('pointerup',release);
    seek.addEventListener('pointercancel',()=>{this.scrubbing=false;this.updateTime();});
    seek.addEventListener('keydown',event=>{
      const step={ArrowLeft:-5,ArrowRight:5,PageDown:-60,PageUp:60}[event.key];
      if(step){ event.preventDefault(); event.stopPropagation(); this.seek(this.currentTime()+step); }
      if(event.key==='Home'){ event.preventDefault(); this.seek(0); }
      if(event.key==='End'){ event.preventDefault(); this.seek(this.duration()-5); }
    });
  }
  reset() {
    clearTimeout(this.seekTimer);
    this.info=null; this.mode='direct'; this.offset=0; this.audio=null; this.subtitle=null; this.cues=[];
    this.stalls=0;this.waitingStarted=null;this.waitingSeconds=0;this.lastBufferAhead=0;
    this.subtitleDelay=0; this.zoom=1; this.fit=FITS[this.prefs.fit]?this.prefs.fit:'contain';
    this.speed=1; this.closePanel(); this.applyFit();
    this.video.volume=Number.isFinite(this.prefs.volume)?this.prefs.volume:1;
    this.video.muted=this.prefs.muted===true;
    this.$('.mp-captions').replaceChildren();
    this.$('.mp-marks').replaceChildren();
    this.$('.mp-mode').hidden=true;
    this.updateTime();
  }
  teardown() {
    // The media timeline is destroyed below; capture the bookmark first.
    const finalPosition=this.currentTime(), finalDuration=this.duration();
    ++this.loadToken;
    cancelAnimationFrame(this.frame);
    clearTimeout(this.hideTimer); clearTimeout(this.seekTimer);
    if(document.pictureInPictureElement===this.video)void document.exitPictureInPicture().catch(()=>{});
    if(document.fullscreenElement)void document.exitFullscreen().catch(()=>{});
    this.video.pause();
    this.video.removeAttribute('src');
    this.video.load();
    document.documentElement.classList.remove('media-player-open');
    if('mediaSession' in navigator)navigator.mediaSession.metadata=null;
    this.source?.onClose?.(finalPosition, finalDuration);
    this.source=null;
  }

  /* ----- track and mode selection ----- */
  chooseInitialTracks() {
    const info=this.info;
    if(!info)return;
    const audioLang=this.prefs.audioLanguage, subtitleLang=this.prefs.subtitleLanguage;
    const preferred=audioLang&&info.audio.find(a=>normalizeLanguage(a.language)===audioLang);
    this.audio=(preferred||info.audio.find(a=>a.default)||info.audio[0])?.index??null;
    const usable=info.subtitles.filter(s=>s.kind!=='unsupported');
    let subtitle=null;
    if(subtitleLang&&subtitleLang!=='off')subtitle=usable.find(s=>normalizeLanguage(s.language)===subtitleLang&&!s.forced)||null;
    // Like most players: forced subtitles in the audio language are always on.
    const spoken=normalizeLanguage(info.audio.find(a=>a.index===this.audio)?.language);
    if(!subtitle)subtitle=usable.find(s=>s.forced&&s.kind==='text'&&(!s.language||normalizeLanguage(s.language)===spoken))||null;
    // Bitmap subtitles require conversion: only enable them on explicit choice.
    this.subtitle=subtitle&&subtitle.kind==='text'?subtitle.index:null;
  }
  selectedAudio(){ return this.info?.audio.find(a=>a.index===this.audio)||null; }
  selectedSubtitle(){ return this.info?.subtitles.find(s=>s.index===this.subtitle)||null; }
  canPlayDirect() {
    const source=this.source;
    if(!source.directURL)return false;
    if(!this.info)return true;
    if(!DIRECT_EXTENSIONS.has(String(source.extension||'').toLowerCase()))return false;
    if(this.selectedSubtitle()?.kind==='bitmap')return false;
    const video=this.info.video, audio=this.selectedAudio();
    const container=String(source.extension).toLowerCase()==='webm'?'video/webm':'video/mp4';
    const codecs=[video&&VIDEO_MIME[video.codec],audio&&AUDIO_MIME[audio.codec]];
    if(video&&!codecs[0])return false;
    if(audio&&!codecs[1])return false;
    if(!this.video.canPlayType(`${container}; codecs="${codecs.filter(Boolean).join(', ')}"`))return false;
    // Only the default audio track plays in direct mode, unless the browser
    // exposes AudioTrackList (Safari) to switch tracks natively.
    const first=this.info.audio.find(a=>a.default)||this.info.audio[0];
    return !audio||audio===first||'audioTracks' in this.video;
  }
  copyCodecs() {
    const supported=[];
    if(this.video.canPlayType('video/mp4; codecs="avc1.640028"'))supported.push('h264');
    if(this.video.canPlayType('video/mp4; codecs="hvc1.2.4.L153.B0"'))supported.push('hevc');
    return this.prefs.forceConvert?'':supported.join(',');
  }
  loadDirect(start) {
    this.mode='direct'; this.offset=0;
    const video=this.video;
    video.src=this.source.directURL;
    video.playbackRate=this.speed;
    video.addEventListener('loadedmetadata',()=>{
      if(start>0&&(!Number.isFinite(video.duration)||video.duration>start))video.currentTime=start;
      this.applyNativeAudio();
    },{once:true});
    this.setMode(tr('Direct'),tr('Fichier original lu directement par le navigateur.'));
    this.setStatus(tr('Ouverture du flux…'),true);
    this.play();
  }
  loadStream(start) {
    if(!this.source?.streamURL)return;
    const duration=this.duration();
    start=Math.max(0,duration?Math.min(start,duration-1):start);
    this.mode='stream'; this.offset=start;
    const burn=this.selectedSubtitle()?.kind==='bitmap'?this.subtitle:null;
    const height=QUALITIES.includes(this.prefs.quality)?this.prefs.quality:1080;
    const copy=this.copyCodecs();
    const video=this.video;
    const params={start:start.toFixed(3),copy,height};
    if(this.audio!=null)params.audio=this.audio;
    if(burn!=null)params.subtitle=burn;
    video.src=this.source.streamURL(params);
    video.playbackRate=this.speed;
    const codec=this.info?.video?.codec;
    const converted=burn!=null||!copy.split(',').includes(codec)||(codec==='h264'&&!/420p$/.test(this.info?.video?.pix_fmt||''));
    this.setMode(converted?tr('Conversion'):tr('Remux'),
      converted?tr('Vidéo convertie en H.264 par le serveur.'):tr('Vidéo copiée sans perte, audio converti en AAC stéréo.'));
    this.setStatus(converted?tr('Démarrage de la conversion sur le serveur…'):tr('Préparation du flux…'),true);
    this.play();
  }
  applyNativeAudio() {
    const tracks=this.video.audioTracks;
    if(!tracks||!this.info||this.mode!=='direct')return;
    const order=this.selectedAudio()?.order??0;
    for(let i=0;i<tracks.length;i++)tracks[i].enabled=i===order;
  }
  play() {
    void this.video.play().catch(error=>{
      if(error?.name==='NotAllowedError')this.setStatus(tr('Appuyez sur lecture pour démarrer.'));
    });
  }
  failed() {
    if(!this.source||!this.video.getAttribute('src'))return;
    const error=this.video.error;
    if(this.mode==='direct'&&this.info&&this.source.streamURL) {
      this.loadStream(this.currentTime());
      return;
    }
    this.spinner(false);
    this.setStatus(error?.code===4
      ? tr('Format non pris en charge ou serveur occupé. Réessayez, ou lancez la lecture sur Kodi.')
      : tr('Lecture interrompue (erreur réseau ou serveur).'));
    this.showOSD(true);
  }
  setAudio(index) {
    if(index===this.audio)return this.closePanel();
    this.audio=index;
    this.prefs.audioLanguage=normalizeLanguage(this.selectedAudio()?.language)||undefined; writePrefs(this.prefs);
    const time=this.currentTime();
    if(this.mode==='direct'&&this.canPlayDirect())this.applyNativeAudio();
    else this.loadStream(time);
    this.renderPanel();
  }
  async setSubtitle(index) {
    const previous=this.selectedSubtitle();
    this.subtitle=index;
    const track=this.selectedSubtitle();
    this.prefs.subtitleLanguage=track?normalizeLanguage(track.language)||undefined:'off'; writePrefs(this.prefs);
    this.cues=[]; this.renderCaptions();
    // Bitmap subtitles are burned into a converted stream; leaving them needs a new stream too.
    if(track?.kind==='bitmap'||previous?.kind==='bitmap') {
      const time=this.currentTime();
      if(track?.kind!=='bitmap'&&this.source.directURL&&this.canPlayDirect())this.loadDirect(time);
      else this.loadStream(time);
    }
    this.renderPanel();
    if(track?.kind==='text')await this.loadSubtitle(index);
  }
  async loadSubtitle(index) {
    const url=this.source?.subtitleURL?.(index);
    if(!url)return;
    this.setStatus(tr('Extraction des sous-titres… (lecture complète du fichier sur le serveur, cela peut prendre une minute)'),false);
    try {
      const text=await this.fetchText(url);
      if(this.subtitle!==index)return;
      this.cues=parseVTT(text);
      this.setStatus('');
      this.renderCaptions();
    } catch(error) {
      if(this.subtitle===index)this.setStatus(tr('Sous-titres indisponibles : ')+error.message);
    }
  }

  /* ----- time ----- */
  duration() {
    const probed=this.info?.duration;
    if(probed>0)return probed;
    const native=this.video?.duration;
    return Number.isFinite(native)?native:0;
  }
  currentTime() {
    if(this.scrubbing)return this.scrubTime;
    return this.offset+(this.video?.currentTime||0);
  }
  seek(time) {
    const duration=this.duration();
    time=Math.max(0,duration?Math.min(time,duration-.5):time);
    this.showOSD();
    if(this.mode==='direct'){ this.video.currentTime=time; this.updateTime(); return; }
    // In a converted stream, positions already buffered stay instant;
    // anything else restarts the server stream at the new position.
    const relative=time-this.offset;
    const buffered=this.video.buffered;
    for(let i=0;i<buffered.length;i++) {
      if(relative>=buffered.start(i)&&relative<=buffered.end(i)-.5){ this.video.currentTime=relative; this.updateTime(); return; }
    }
    this.offset=time; this.video.pause(); this.updateTime();
    clearTimeout(this.seekTimer);
    this.seekTimer=setTimeout(()=>this.loadStream(time),250);
  }
  finishWaiting(){
    if(this.waitingStarted!==null){
      this.waitingSeconds+=(performance.now()-this.waitingStarted)/1000;
      this.waitingStarted=null;
    }
  }
  updateTime() {
    if(!this.root)return;
    const duration=this.duration(), current=Math.min(this.currentTime(),duration||Infinity);
    this.$('.mp-current').textContent=clock(current);
    this.$('.mp-duration').textContent=duration?clock(duration):'--:--';
    const ratio=duration?current/duration:0;
    this.$('.mp-played').style.width=`${ratio*100}%`;
    this.$('.mp-thumb').style.left=`${ratio*100}%`;
    let bufferedEnd=0;
    const buffered=this.video.buffered;
    for(let i=0;i<buffered.length;i++)if(buffered.start(i)<=this.video.currentTime+1)bufferedEnd=Math.max(bufferedEnd,buffered.end(i));
    this.$('.mp-buffered').style.left=`${duration?this.offset/duration*100:0}%`;
    this.$('.mp-buffered').style.width=`${duration?Math.max(0,bufferedEnd)/duration*100:0}%`;
    this.lastBufferAhead=Math.max(0,bufferedEnd-this.video.currentTime);
    const diagnostics=this.$('.mp-live-telemetry');
    if(diagnostics){
      const elapsed=this.waitingSeconds+(this.waitingStarted!==null?(performance.now()-this.waitingStarted)/1000:0);
      diagnostics.textContent='Tampon : '+this.lastBufferAhead.toFixed(1)+' s · Arrêts : '+this.stalls+
        ' · Attente : '+elapsed.toFixed(1)+' s';
    }
    const seek=this.$('.mp-seek');
    seek.setAttribute('aria-valuemax',String(Math.round(duration)));
    seek.setAttribute('aria-valuenow',String(Math.round(current)));
    seek.setAttribute('aria-valuetext',`${clock(current)} / ${clock(duration)}`);
    this.renderCaptions();
    this.highlightChapter();
    if('mediaSession' in navigator&&duration&&navigator.mediaSession.setPositionState) {
      try { navigator.mediaSession.setPositionState({duration,position:Math.min(current,duration),playbackRate:this.video.playbackRate||1}); }
      catch { /* Ignore transient invalid states while loading. */ }
    }
  }
  loop() {
    cancelAnimationFrame(this.frame);
    const tick=()=>{
      if(!this.source||this.video.paused)return;
      this.renderCaptions();
      this.frame=requestAnimationFrame(tick);
    };
    this.frame=requestAnimationFrame(tick);
  }
  tooltip(time,event) {
    const tip=this.$('.mp-tooltip'), box=this.$('.mp-seek').getBoundingClientRect();
    const chapter=this.chapterAt(time);
    tip.textContent=chapter?.title?`${clock(time)} · ${chapter.title}`:clock(time);
    tip.hidden=false;
    const x=Math.min(box.width-tip.offsetWidth/2,Math.max(tip.offsetWidth/2,event.clientX-box.left));
    tip.style.left=`${x}px`;
  }

  /* ----- subtitles rendering ----- */
  renderCaptions() {
    const layer=this.$('.mp-captions');
    const scale=SUBTITLE_SIZES[this.prefs.subtitleSize]||1;
    layer.style.setProperty('--mp-caption-scale',String(scale));
    if(!this.cues.length||this.subtitle==null){ if(layer.childElementCount)layer.replaceChildren(); return; }
    const time=this.currentTime()-this.subtitleDelay;
    const active=this.cues.filter(cue=>cue.start<=time&&cue.end>time);
    const key=active.map(cue=>cue.start).join(',');
    if(layer.dataset.key===key)return;
    layer.dataset.key=key;
    layer.innerHTML=active.map(cue=>`<span class="mp-cue">${cue.html}</span>`).join('');
  }

  /* ----- chapters ----- */
  chapters(){ return this.info?.chapters||[]; }
  chapterAt(time) {
    const list=this.chapters();
    for(let i=list.length-1;i>=0;i--)if(time>=list[i].start)return list[i];
    return null;
  }
  chapterTitle(chapter,index){ return chapter.title||`${tr('Chapitre')} ${index+1}`; }
  renderChapters() {
    const marks=this.$('.mp-marks'), duration=this.duration(), list=this.chapters();
    marks.innerHTML=duration?list.slice(1).map(c=>`<span style="left:${c.start/duration*100}%"></span>`).join(''):'';
    for(const element of this.root.querySelectorAll('[data-mp-chapter]'))element.hidden=list.length<2;
  }
  jumpChapter(direction) {
    const list=this.chapters(), time=this.currentTime();
    if(list.length<2)return;
    let index=list.findLastIndex(c=>c.start<=time+.5);
    if(direction<0&&index>=0&&time-list[index].start>3)direction=0;
    index=Math.max(0,Math.min(list.length-1,index+direction));
    this.seek(list[index].start+.01);
  }
  highlightChapter() {
    if(this.panel!=='chapters')return;
    const current=this.chapterAt(this.currentTime());
    for(const row of this.root.querySelectorAll('[data-chapter]'))
      row.setAttribute('aria-current',String(Number(row.dataset.chapter)===current?.start));
  }

  /* ----- panels ----- */
  togglePanel(name) {
    if(this.panel===name)return this.closePanel();
    this.panel=name; this.renderPanel(); this.showOSD(true);
  }
  closePanel() {
    this.panel=null;
    if(this.root){ this.$('.mp-panel').hidden=true; this.$('.mp-panel').replaceChildren(); }
    this.scheduleHide();
  }
  renderPanel() {
    if(!this.panel)return;
    const panel=this.$('.mp-panel');
    const option=(attrs,checked,title,details='')=>`<button type="button" class="mp-option" ${attrs} aria-pressed="${checked}"><span class="mp-check" aria-hidden="true">${checked?'✓':''}</span><span><b>${esc(title)}</b>${details?`<small>${esc(details)}</small>`:''}</span></button>`;
    const heading=title=>`<header><h3>${esc(tr(title))}</h3><button type="button" class="mp-button" data-mp="panel-close" aria-label="${esc(tr('Fermer'))}">${icon('close')}</button></header>`;
    let html='';
    const info=this.info;
    if(this.panel==='audio') {
      html=heading('Pistes audio');
      if(!info?.audio.length)html+=`<p class="mp-hint">${esc(tr('Aucune information de piste : seule la piste par défaut est lue.'))}</p>`;
      else html+=info.audio.map(track=>{const label=audioLabel(track);return option(`data-mp="audio:${track.index}"`,track.index===this.audio,label.name,label.details);}).join('');
      if(info?.audio.length>1&&!this.source.streamURL)html+=`<p class="mp-hint">${esc(tr('Le changement de piste n’est pas disponible pour cette source.'))}</p>`;
    } else if(this.panel==='subtitles') {
      html=heading('Sous-titres')+option('data-mp="subtitle:off"',this.subtitle==null,tr('Désactivés'));
      for(const track of info?.subtitles||[]) {
        const label=subtitleLabel(track);
        const disabled=track.kind==='unsupported'||(track.kind==='bitmap'&&!this.source.streamURL)||(track.kind==='text'&&!this.source.subtitleURL);
        html+=option(`data-mp="subtitle:${track.index}" ${disabled?'disabled':''}`,track.index===this.subtitle,label.name,label.details);
      }
      if(!info)html+=`<p class="mp-hint">${esc(tr('Pistes non analysées pour cette source.'))}</p>`;
      const size=this.prefs.subtitleSize||'m';
      html+=`<div class="mp-subsection"><p>${esc(tr('Taille'))}</p><div class="mp-segmented">${Object.keys(SUBTITLE_SIZES).map(key=>`<button type="button" data-mp="subsize:${key}" aria-pressed="${key===size}">${key.toUpperCase()}</button>`).join('')}</div></div>`;
      html+=`<div class="mp-subsection"><p>${esc(tr('Décalage'))} <output>${this.subtitleDelay>0?'+':''}${number(this.subtitleDelay)} s</output></p><div class="mp-segmented"><button type="button" data-mp="subdelay:-0.5">−0,5 s</button><button type="button" data-mp="subdelay:-0.1">−0,1 s</button><button type="button" data-mp="subdelay:0">0</button><button type="button" data-mp="subdelay:0.1">+0,1 s</button><button type="button" data-mp="subdelay:0.5">+0,5 s</button></div></div>`;
    } else if(this.panel==='chapters') {
      const current=this.chapterAt(this.currentTime());
      html=heading('Chapitres')+'<ol class="mp-chapters">'+this.chapters().map((c,i)=>`<li><button type="button" class="mp-option" data-mp="chapter:${i}" data-chapter="${c.start}" aria-current="${c===current}"><span class="mp-chapter-time">${clock(c.start)}</span><span><b>${esc(this.chapterTitle(c,i))}</b></span></button></li>`).join('')+'</ol>';
    } else if(this.panel==='aspect') {
      html=heading('Format d’image')+Object.entries(FITS).map(([key,label])=>option(`data-mp="fit:${key}"`,this.fit===key,tr(label))).join('');
      html+=`<div class="mp-subsection"><p>${esc(tr('Zoom'))}</p><div class="mp-segmented">${ZOOMS.map(z=>`<button type="button" data-mp="zoom:${z}" aria-pressed="${z===this.zoom}">${Math.round(z*100)} %</button>`).join('')}</div></div>`;
    } else if(this.panel==='settings') {
      const quality=QUALITIES.includes(this.prefs.quality)?this.prefs.quality:1080;
      html=heading('Réglages de lecture');
      html+=`<div class="mp-subsection"><p>${esc(tr('Vitesse'))}</p><div class="mp-segmented">${SPEEDS.map(s=>`<button type="button" data-mp="speed:${s}" aria-pressed="${s===this.speed}">${number(s,s%1?2:0).replace(/0$/,'')}×</button>`).join('')}</div></div>`;
      if(this.source.streamURL) {
        html+=`<div class="mp-subsection"><p>${esc(tr('Qualité de conversion'))}</p><div class="mp-segmented">${QUALITIES.map(q=>`<button type="button" data-mp="quality:${q}" aria-pressed="${q===quality}">${q===2160?'4K':q+'p'}</button>`).join('')}</div></div>`;
        html+=option('data-mp="force-convert"',this.prefs.forceConvert===true,tr('Toujours convertir la vidéo'),tr('Pour les navigateurs qui affichent une image noire en mode Remux.'));
        html+=option('data-mp="restart-stream"',false,tr('Relancer le flux serveur à cette position'),tr('Force le mode Remux/Conversion avec la piste audio choisie.'));
      }
    } else if(this.panel==='info') {
      html=heading('Informations techniques')+this.technicalDetails();
    }
    panel.innerHTML=html;
    panel.hidden=false;
    panel.querySelector('[aria-pressed="true"],[aria-current="true"]')?.scrollIntoView({block:'nearest'});
  }
  technicalDetails() {
    const info=this.info, video=info?.video, rows=[];
    const add=(label,value)=>{ if(value)rows.push(`<dt>${esc(tr(label))}</dt><dd>${esc(value)}</dd>`); };
    add('Mode de lecture',this.$('.mp-mode').textContent+' — '+this.$('.mp-mode').title);
    add('Conteneur',[this.source.extension?.toUpperCase(),info?.container].filter(Boolean).join(' · '));
    add('Durée',this.duration()?clock(this.duration()):'');
    if(info?.bit_rate)add('Débit',`${number(info.bit_rate/1e6)} Mb/s`);
    if(info?.size)add('Taille',info.size>=1e9?`${number(info.size/1e9,2)} ${tr('Go')}`:`${number(info.size/1e6,0)} ${tr('Mo')}`);
    if(video)add('Vidéo',[(video.codec||'').toUpperCase(),video.profile,video.width&&`${video.width} × ${video.height}`,video.frame_rate&&`${number(video.frame_rate,video.frame_rate%1?3:0)} ${tr('i/s')}`,video.hdr,video.pix_fmt].filter(Boolean).join(' · '));
    const audio=this.selectedAudio();
    if(audio){ const l=audioLabel(audio); add('Audio',`${l.name} · ${l.details}`); }
    const subtitle=this.selectedSubtitle();
    if(subtitle){ const l=subtitleLabel(subtitle); add('Sous-titres',[l.name,l.details].filter(Boolean).join(' · ')); }
    add('Pistes',info?`${info.audio.length} ${tr('audio')} · ${info.subtitles.length} ${tr('sous-titres')} · ${info.chapters.length} ${tr('chapitres')}`:tr('Non analysées'));
    add('Image affichée',this.video.videoWidth?`${this.video.videoWidth} × ${this.video.videoHeight}`:'');
    add('Tampon en avance',this.lastBufferAhead.toFixed(1)+' s');
    add('Interruptions de lecture',String(this.stalls));
    const quality=this.video.getVideoPlaybackQuality?.();
    if(quality)add('Images perdues',`${quality.droppedVideoFrames} / ${quality.totalVideoFrames}`);
    return `<dl class="mp-details">${rows.join('')}</dl><p class="mp-live-telemetry" role="status" aria-live="off">Tampon : ${this.lastBufferAhead.toFixed(1)} s · Arrêts : ${this.stalls} · Attente : ${this.waitingSeconds.toFixed(1)} s</p><p class="mp-hint">Ces mesures viennent du navigateur. Le débit NAS et les reconnexions SMB/NFS nécessitent une télémétrie distincte sur le serveur.</p>`;
  }

  /* ----- actions ----- */
  action(name) {
    const [command,value]=name.split(/:(.*)/s);
    this.showOSD();
    switch(command) {
      case 'close': return this.close();
      case 'toggle': return this.toggle();
      case 'back': return this.seek(this.currentTime()-10);
      case 'forward': return this.seek(this.currentTime()+10);
      case 'previous': return this.jumpChapter(-1);
      case 'next': return this.jumpChapter(1);
      case 'mute': this.video.muted=!this.video.muted; if(!this.video.muted&&this.video.volume===0)this.video.volume=.5; return;
      case 'fullscreen': return this.toggleFullscreen();
      case 'pip': return this.togglePiP();
      case 'panel': return this.togglePanel(value);
      case 'panel-close': return this.closePanel();
      case 'audio': return this.setAudio(Number(value));
      case 'subtitle': return void this.setSubtitle(value==='off'?null:Number(value));
      case 'subsize': this.prefs.subtitleSize=value; writePrefs(this.prefs); this.$('.mp-captions').dataset.key=''; this.renderCaptions(); return this.renderPanel();
      case 'subdelay': this.subtitleDelay=Number(value)===0?0:Math.round((this.subtitleDelay+Number(value))*10)/10; this.$('.mp-captions').dataset.key=''; this.renderCaptions(); return this.renderPanel();
      case 'chapter': { const c=this.chapters()[Number(value)]; if(c)this.seek(c.start+.01); return this.highlightChapter(); }
      case 'fit': this.fit=value; this.prefs.fit=value; writePrefs(this.prefs); this.applyFit(); return this.renderPanel();
      case 'zoom': this.zoom=Number(value); this.applyFit(); return this.renderPanel();
      case 'speed': this.speed=Number(value); this.video.playbackRate=this.speed; return this.renderPanel();
      case 'quality': this.prefs.quality=Number(value); writePrefs(this.prefs); if(this.mode==='stream')this.loadStream(this.currentTime()); return this.renderPanel();
      case 'force-convert': this.prefs.forceConvert=!this.prefs.forceConvert; writePrefs(this.prefs); if(this.mode==='stream')this.loadStream(this.currentTime()); return this.renderPanel();
      case 'restart-stream': this.loadStream(this.currentTime()); return this.closePanel();
    }
  }
  toggle() {
    if(this.video.paused||this.video.ended) {
      if(this.mode==='stream'&&this.video.ended)return this.loadStream(this.currentTime());
      this.play();
    } else this.video.pause();
  }
  applyFit() {
    if(!this.video)return;
    this.video.style.objectFit=this.fit;
    this.video.style.transform=this.zoom===1?'':`scale(${this.zoom})`;
  }
  toggleFullscreen() {
    const stage=this.$('.mp-stage');
    if(document.fullscreenElement||document.webkitFullscreenElement) {
      (document.exitFullscreen||document.webkitExitFullscreen).call(document);
    } else if(stage.requestFullscreen) {
      void stage.requestFullscreen().catch(()=>{});
    } else if(stage.webkitRequestFullscreen) stage.webkitRequestFullscreen();
    else if(this.video.webkitEnterFullscreen) this.video.webkitEnterFullscreen();
  }
  async togglePiP() {
    try {
      if(document.pictureInPictureElement)await document.exitPictureInPicture();
      else await this.video.requestPictureInPicture();
    } catch(error){ this.setStatus(tr('Image dans l’image indisponible : ')+error.message); }
  }
  keyboard(event) {
    if(event.target.closest?.('input')&&!['Escape'].includes(event.key))return;
    if(event.metaKey||event.ctrlKey||event.altKey)return;
    const key=event.key.length===1?event.key.toLowerCase():event.key;
    const actions={
      ' ':()=>this.toggle(), k:()=>this.toggle(),
      ArrowLeft:()=>this.seek(this.currentTime()-10), j:()=>this.seek(this.currentTime()-10),
      ArrowRight:()=>this.seek(this.currentTime()+10), l:()=>this.seek(this.currentTime()+10),
      ArrowUp:()=>{this.video.muted=false;this.video.volume=Math.min(1,this.video.volume+.1);},
      ArrowDown:()=>{this.video.volume=Math.max(0,this.video.volume-.1);},
      m:()=>{this.video.muted=!this.video.muted;},
      f:()=>this.toggleFullscreen(),
      c:()=>this.cycleSubtitles(),
      a:()=>this.cycleAudio(),
      i:()=>this.togglePanel('info'),
      PageUp:()=>this.jumpChapter(-1), p:()=>this.jumpChapter(-1),
      PageDown:()=>this.jumpChapter(1), n:()=>this.jumpChapter(1),
    };
    if(event.target.closest?.('.mp-seek')&&['ArrowLeft','ArrowRight','PageUp','PageDown'].includes(key))return;
    if(key===' '&&event.target.closest?.('button'))return;
    const run=actions[key];
    if(!run)return;
    event.preventDefault();
    run();
    this.showOSD();
  }
  cycleSubtitles() {
    const list=(this.info?.subtitles||[]).filter(s=>s.kind==='text'||(s.kind==='bitmap'&&this.source.streamURL));
    const ids=[null,...list.map(s=>s.index)];
    const next=ids[(ids.indexOf(this.subtitle)+1)%ids.length];
    void this.setSubtitle(next);
    this.flash(next==null?tr('Sous-titres désactivés'):`${tr('Sous-titres')} : ${subtitleLabel(this.selectedSubtitle()).name}`);
  }
  cycleAudio() {
    const list=this.info?.audio||[];
    if(list.length<2)return;
    const position=list.findIndex(a=>a.index===this.audio);
    const next=list[(position+1)%list.length];
    this.setAudio(next.index);
    this.flash(`${tr('Audio')} : ${audioLabel(next).name}`);
  }

  /* ----- chrome ----- */
  flash(message){ this.setStatus(message); clearTimeout(this.flashTimer); this.flashTimer=setTimeout(()=>{ if(this.$('.mp-status').textContent===message)this.setStatus(''); },2500); }
  setStatus(message,busy=false) {
    if(!this.root)return;
    this.$('.mp-status').textContent=message||'';
    if(busy)this.spinner(true);
  }
  setMode(label,detail) {
    const mode=this.$('.mp-mode');
    mode.textContent=label; mode.title=detail; mode.hidden=false;
  }
  spinner(visible){ this.$('.mp-spinner').hidden=!visible; }
  showOSD(sticky=false) {
    if(!this.root)return;
    this.root.classList.add('mp-osd-visible');
    clearTimeout(this.hideTimer);
    if(!sticky)this.scheduleHide();
  }
  scheduleHide() {
    clearTimeout(this.hideTimer);
    this.hideTimer=setTimeout(()=>{
      if(this.video.paused||this.panel||this.scrubbing||this.root.querySelector('.mp-bottom:hover,.mp-top:hover'))return;
      this.root.classList.remove('mp-osd-visible');
    },3200);
  }
  updateButtons() {
    if(!this.root)return;
    const paused=this.video.paused;
    for(const button of this.root.querySelectorAll('[data-mp="toggle"]')) {
      button.innerHTML=icon(paused?'play':'pause');
      button.setAttribute('aria-label',tr(paused?'Lecture':'Pause'));
      button.title=tr(paused?'Lecture':'Pause');
    }
    this.root.classList.toggle('mp-paused',paused);
    const mute=this.$('[data-mp="mute"]'), silent=this.video.muted||this.video.volume===0;
    mute.innerHTML=icon(silent?'muted':'volume');
    mute.setAttribute('aria-label',tr(silent?'Rétablir le son':'Couper le son'));
    this.$('.mp-volume-range').value=String(this.video.muted?0:this.video.volume);
    const full=Boolean(document.fullscreenElement||document.webkitFullscreenElement);
    const fullscreen=this.$('[data-mp="fullscreen"]');
    fullscreen.innerHTML=icon(full?'exitFullscreen':'fullscreen');
    fullscreen.setAttribute('aria-label',tr(full?'Quitter le plein écran':'Plein écran'));
    this.$('[data-mp-pip]').hidden=!document.pictureInPictureEnabled;
    const hasSubs=Boolean(this.info?.subtitles.length), hasAudio=(this.info?.audio.length||0)>1;
    this.$('[data-mp="panel:subtitles"]').classList.toggle('mp-active',this.subtitle!=null);
    this.$('[data-mp="panel:subtitles"]').classList.toggle('mp-dim',!hasSubs);
    this.$('[data-mp="panel:audio"]').classList.toggle('mp-dim',!hasAudio);
  }
  mediaSession() {
    if(!('mediaSession' in navigator))return;
    try {
      navigator.mediaSession.metadata=new MediaMetadata({title:this.source.title||'',artist:this.source.subtitle||'',artwork:this.source.artwork?[{src:this.source.artwork}]:[]});
      const handlers={
        play:()=>this.play(), pause:()=>this.video.pause(),
        seekbackward:()=>this.seek(this.currentTime()-10), seekforward:()=>this.seek(this.currentTime()+10),
        seekto:details=>this.seek(details.seekTime),
        previoustrack:this.chapters().length>1?()=>this.jumpChapter(-1):null,
        nexttrack:this.chapters().length>1?()=>this.jumpChapter(1):null,
      };
      for(const [name,handler] of Object.entries(handlers)) {
        try { navigator.mediaSession.setActionHandler(name,handler); } catch { /* Unsupported action. */ }
      }
    } catch { /* Media Session is optional. */ }
  }
}
