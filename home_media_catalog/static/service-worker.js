/* Offline fallback for the public application shell; API and media remain network-only. */
const SHELL_CACHE='media-catalog-shell-v5';
const SHELL=['./','static/app.css','static/app.js','static/i18n.js','static/remote.js','static/player.js','static/music.js','static/music.css','static/cinema.svg','static/kodi-logo.svg'];
self.addEventListener('install',event=>{
  event.waitUntil(caches.open(SHELL_CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting()));
});
self.addEventListener('activate',event=>{
  event.waitUntil(Promise.all([
    caches.keys().then(keys=>Promise.all(keys.filter(key=>key.startsWith('media-catalog-shell-')&&key!==SHELL_CACHE).map(key=>caches.delete(key)))),
    self.clients.claim()
  ]));
});
async function shellResponse(request,fallback) {
  const cache=await caches.open(SHELL_CACHE);
  try {
    const controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),6000);
    let response;
    try { response=await fetch(request,{signal:controller.signal}); }
    finally { clearTimeout(timer); }
    if(response.ok) {
      try { await cache.put(fallback,response.clone()); } catch { /* Storage may be disabled. */ }
      return response;
    }
    if(response.status<500) return response;
  } catch { /* The server is unreachable. */ }
  return await cache.match(fallback) || Response.error();
}
self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET')return;
  const requestURL=new URL(event.request.url);
  const scopeURL=new URL(self.registration.scope);
  if(requestURL.origin!==scopeURL.origin||!requestURL.pathname.startsWith(scopeURL.pathname))return;
  const relative=requestURL.pathname.slice(scopeURL.pathname.length);
  if(event.request.mode==='navigate'&&relative==='') {
    event.respondWith(shellResponse(event.request,'./'));
  } else if(SHELL.includes(relative)) {
    event.respondWith(shellResponse(event.request,relative));
  }
});
