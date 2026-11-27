// Fehlgeschlagenes Lesen der Ressourcen (components/ReadFailure.vue, ADR 0026).
export default {
  read: {
    failed: 'Konnte {what} nicht lesen.',
    stale: 'Konnte nicht aktualisiert werden; was du siehst, ist evtl. nicht mehr aktuell.',
    retry: 'Erneut versuchen',
    resources: {
      images: 'die Images',
      githubTokens: 'die GitHub-Tokens',
      sshConfig: 'die Verbindungshilfe',
      githubApp: 'die GitHub-Apps',
      pairings: 'die gekoppelten Geräte',
      vmLog: 'das Log der VM',
      buildLog: 'das Protokoll des Builds',
      containerfile: 'das Containerfile',
      host: 'den Verlauf des Hosts',
    },
  },
}
