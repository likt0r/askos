// Faults of the UI itself: an unknown address, a view or the shell that stopped (#213).
export default {
  fault: {
    notFound: {
      nav: 'Not found',
      title: 'This page does not exist',
      text: 'The Control Room has no page at this address.',
      back: 'Back to the VMs',
    },
    view: {
      title: 'Something went wrong in this view',
      text: 'A fault in the Control Room UI stopped this view. The other pages still work; reload the page to start this one again.',
    },
    shell: {
      title: 'Something went wrong in the Control Room',
      text: 'A fault in the Control Room UI stopped it. Reload the page to start it again.',
    },
    toast: 'Something went wrong in the Control Room UI',
    reload: 'Reload',
    details: 'Details',
    copy: 'Copy',
    copied: 'Copied',
  },
}
