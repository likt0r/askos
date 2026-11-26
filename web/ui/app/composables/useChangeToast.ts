// The toast for a change without a form that failed (ADR 0026): it stays until dismissed, with
// what failed in the UI's words and the Host's text behind Details.
import { h } from 'vue'
import FailureText from '~/components/FailureText.vue'

export function useChangeToast() {
  const toast = useToast()
  const t = useNuxtApp().$i18n.t
  return (error: unknown, title?: string) => {
    toast.add({
      title: title ?? t('change.failed'),
      description: h(FailureText, { error }),
      color: 'error',
      icon: 'i-lucide-circle-alert',
      duration: 0,
    })
  }
}
