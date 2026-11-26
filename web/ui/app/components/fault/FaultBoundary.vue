<script setup lang="ts">
// The page area of the layout. A fault that stops the page (in rendering, setup, a lifecycle
// hook or a watcher) shows in its place, while navigation and host bar stay; going to another
// address clears it. A fault in an event handler leaves the page usable and shows a toast. An
// ended session or a Control Room that cannot be reached is the sign-in page's and the connection
// notice's to say. Faults stay in the browser (the console); nothing is sent to the Host.
import type { ComponentPublicInstance } from 'vue'
import FaultView from './FaultView.vue'

// the router's route: the layout's useRoute() follows only pages that finished loading, and the
// page that stopped is not shown
const router = useRouter()
const showToast = useFaultToast()
const fault = shallowRef<{ message: string } | null>(null)

function inFaultView(instance: ComponentPublicInstance | null) {
  for (let i = instance; i; i = i.$parent) if (i.$.type === FaultView) return true
  return false
}

onErrorCaptured((error, instance, info) => {
  // the fault view itself failed: the app's handler takes it (app/error.vue)
  if (fault.value && inFaultView(instance)) return
  console.error(error)
  // another fault of the page that stopped: the first one is shown
  if (fault.value || shownElsewhere(error)) return false
  if (inEventHandler(info)) showToast(error)
  else fault.value = { message: faultMessage(error) }
  return false
})

watch(() => router.currentRoute.value.fullPath, () => {
  fault.value = null
})
</script>

<template>
  <FaultView v-if="fault" kind="view" :message="fault.message" />
  <slot v-else />
</template>
