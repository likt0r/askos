// Reads of the resources that failed (components/ReadFailure.vue, ADR 0026).
export default {
  read: {
    failed: 'Could not read {what}.',
    stale: 'Could not be refreshed; what you see may be out of date.',
    retry: 'Retry',
    resources: {
      images: 'the Images',
      githubTokens: 'the GitHub tokens',
      sshConfig: 'the connection help',
      githubApp: 'the GitHub Apps',
      pairings: 'the Paired devices',
      containerfile: 'the Containerfile',
      host: 'the Host\'s history',
    },
  },
}
