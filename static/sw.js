const CACHE='seat-phone-shell-v3';
const SHELL=['/','/app.js','/device-auth.js','/styles.css','/icon.svg','/manifest.webmanifest'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{const url=new URL(event.request.url);if(event.request.method!=='GET'||url.origin!==self.location.origin||!SHELL.includes(url.pathname))return;event.respondWith(fetch(event.request).catch(()=>caches.match(event.request)));});
self.addEventListener('push',event=>{let value={title:'席取りサポート',body:'アプリを開いて状況を確認してください。',url:'/'};try{value={...value,...event.data.json()};}catch{}event.waitUntil(self.registration.showNotification(value.title,{body:value.body,icon:'/icon.svg',tag:value.tag||'seat-status',data:{url:'/'}}));});
self.addEventListener('notificationclick',event=>{event.notification.close();event.waitUntil(self.clients.matchAll({type:'window',includeUncontrolled:true}).then(clients=>{for(const client of clients)if(new URL(client.url).origin===self.location.origin)return client.focus();return self.clients.openWindow('/');}));});
