// Browser notifications and the reload prompt for a new Control Room version.
export default {
  reload: {
    title: 'A new version of the Control Room is installed',
    text: 'Reload this page to use it. Until then you may see an older view.',
    now: 'Reload',
    later: 'Not now',
  },
  notifications: {
    intro: 'Choose which events this browser shows as notifications, also while the tab is in the background. The choice is kept in this browser only.',
    unsupported: 'This browser cannot show notifications here (they need a secure connection).',
    denied: 'Notifications are blocked for this site. Allow them in the browser\'s site settings to use them.',
    enable: 'Allow notifications',
    enableHint: 'Your browser will ask for permission.',
    granted: 'This browser may show notifications.',
    title: {
      section: 'Notifications in this browser',
      alert: 'Alert: {vm}',
      'github-refused': 'GitHub token refused: {vm}',
      'build-failed': 'Build failed: {image}',
      'build-rejected': 'Build rejected: {image}',
      'host-reboot': 'Host reboot needed',
      temperature: 'Host temperature',
      other: 'Control Room',
    },
    text: {
      alert: '{vm} tried to reach {domain} ({signals})',
      'github-refused': '{vm}: the GitHub token request for {request} was refused',
      'build-failed': 'Build {image}-{build} failed. The previous Build stays in use.',
      'build-rejected': 'Build {image}-{build} was rejected: a safeguard is missing or weakened. The previous Build stays in use.',
      'host-reboot': 'Updates need a Host reboot. The disk passphrase must be entered at boot.',
      temperature: '{sensor} is at {value} °C (threshold {threshold} °C)',
    },
    kind: {
      alert: { label: 'Alerts', help: 'A VM tried to reach a suspicious destination.' },
      'github-refused': { label: 'Refused GitHub tokens', help: 'The token service refused a request of a VM.' },
      'build-failed': { label: 'Failed Builds', help: 'A Build failed; the previous one stays in use.' },
      'build-rejected': { label: 'Rejected Builds', help: 'A Build was discarded because a safeguard was missing.' },
      'host-reboot': { label: 'Host reboot needed', help: 'Updates need a reboot of the Host.' },
      temperature: { label: 'Temperature', help: 'A CPU, GPU or NVMe temperature reached its threshold.' },
    },
  },
}
