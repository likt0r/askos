// Browser-Benachrichtigungen und Hinweis auf eine neue Version des Control Room.
export default {
  reload: {
    title: 'Eine neue Version des Control Room ist installiert',
    text: 'Lade die Seite neu, um sie zu nutzen. Bis dahin siehst du evtl. eine ältere Ansicht.',
    now: 'Neu laden',
    later: 'Jetzt nicht',
  },
  notifications: {
    intro: 'Wähle, welche Ereignisse dieser Browser als Benachrichtigung zeigt, auch wenn der Tab im Hintergrund ist. Die Auswahl gilt nur für diesen Browser.',
    unsupported: 'Dieser Browser kann hier keine Benachrichtigungen zeigen (sie brauchen eine sichere Verbindung).',
    denied: 'Benachrichtigungen sind für diese Seite blockiert. Erlaube sie in den Website-Einstellungen des Browsers.',
    enable: 'Benachrichtigungen erlauben',
    enableHint: 'Dein Browser fragt nach der Erlaubnis.',
    granted: 'Dieser Browser darf Benachrichtigungen zeigen.',
    title: {
      section: 'Benachrichtigungen in diesem Browser',
      alert: 'Alert: {vm}',
      'github-refused': 'GitHub-Token abgelehnt: {vm}',
      'build-failed': 'Build gescheitert: {image}',
      'build-rejected': 'Build verworfen: {image}',
      'host-reboot': 'Host-Neustart nötig',
      temperature: 'Temperatur des Hosts',
      other: 'Control Room',
    },
    text: {
      alert: '{vm} wollte {domain} erreichen ({signals})',
      'github-refused': '{vm}: Die Anfrage nach einem GitHub-Token für {request} wurde abgelehnt',
      'build-failed': 'Build {image}-{build} ist gescheitert. Der vorherige Build bleibt in Gebrauch.',
      'build-rejected': 'Build {image}-{build} wurde verworfen: eine Schutzmaßnahme fehlt oder ist abgeschwächt. Der vorherige Build bleibt in Gebrauch.',
      'host-reboot': 'Updates brauchen einen Neustart des Hosts. Die Passphrase der Festplatte muss beim Start eingegeben werden.',
      temperature: '{sensor} liegt bei {value} °C (Schwelle {threshold} °C)',
    },
    kind: {
      alert: { label: 'Alerts', help: 'Eine VM wollte ein verdächtiges Ziel erreichen.' },
      'github-refused': { label: 'Abgelehnte GitHub-Tokens', help: 'Der Token-Dienst hat eine Anfrage einer VM abgelehnt.' },
      'build-failed': { label: 'Gescheiterte Builds', help: 'Ein Build ist gescheitert; der vorherige bleibt in Gebrauch.' },
      'build-rejected': { label: 'Verworfene Builds', help: 'Ein Build wurde verworfen, weil eine Schutzmaßnahme fehlte.' },
      'host-reboot': { label: 'Host-Neustart nötig', help: 'Updates brauchen einen Neustart des Hosts.' },
      temperature: { label: 'Temperatur', help: 'Eine CPU-, GPU- oder NVMe-Temperatur hat ihre Schwelle erreicht.' },
    },
  },
}
