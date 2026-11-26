// The toast for a fault the UI can go on from (#213): it stays until dismissed, with the
// fault's message behind Details.
import { h } from 'vue'
import FaultDetails from '~/components/fault/FaultDetails.vue'

export function useFaultToast() {
  const toast = useToast()
  const t = useNuxtApp().$i18n.t
  return (error: unknown) => {
    toast.add({
      title: t('fault.toast'),
      description: h(FaultDetails, { message: faultMessage(error) }),
      color: 'error',
      icon: 'i-lucide-octagon-alert',
      duration: 0,
    })
  }
}
