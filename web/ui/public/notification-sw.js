// Only shows notifications and focuses the Control Room when one is clicked (Android's
// Chrome allows notifications through a service worker only). No caching, no fetch handler.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = (event.notification.data && event.notification.data.url) || '/activity'
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windows) => {
    for (const client of windows) {
      if ('focus' in client) return client.focus()
    }
    return self.clients.openWindow(url)
  }))
})
