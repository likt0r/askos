<script setup lang="ts">
// The page area of the layout. A fault that stops the page (in rendering, setup, a lifecycle
// hook or a watcher) shows in its place, while navigation and host bar stay; going to another
// address clears it. A fault in an event handler leaves the page usable and shows a toast.
// Faults stay in the browser (the console); nothing is sent to the Host.
import type { ComponentPublicInstance } from 'vue'

// the router's route: the layout's useRoute() follows only pages that finished loading, and the
// page that stopped is not shown
const router = useRouter()
const showToast = useFaultToast()
const fault = shallowRef<{ message: string } | null>(null)
const view = shallowRef<ComponentPublicInstance | null>(null)

function within(instance: ComponentPublicInstance | null, root: ComponentPublicInstance | null) {
  for (let i = instance; i; i = i.$parent) if (i === root) return true
  return false
}

onErrorCaptured((error, instance, info) => {
  if (fault.value) {
    // the fault view itself failed: the app's handler takes it (app/error.vue)
    if (view.value && within(instance, view.value)) return
    // another fault of the page that stopped: the first one is shown
    return false
  }
  console.error(error)
  if (inEventHandler(info)) showToast(error)
  else fault.value = { message: faultMessage(error) }
  return false
})

watch(() => router.currentRoute.value.fullPath, () => {
  fault.value = null
})
</script>

<template>
  <FaultView v-if="fault" ref="view" kind="view" :message="fault.message" />
  <slot v-else />
</template>
