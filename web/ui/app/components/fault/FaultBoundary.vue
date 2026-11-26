<script setup lang="ts">
// The page area of the layout. A fault that stops the page (in rendering, setup, a lifecycle
// hook or a watcher) shows in its place, while navigation and host bar stay; going to another
// address clears it. A fault in an event handler leaves the page usable and shows a toast.
// Faults stay in the browser (the console); nothing is sent to the Host.
const route = useRoute()
const showToast = useFaultToast()
const fault = shallowRef<{ message: string } | null>(null)

onErrorCaptured((error, _instance, info) => {
  if (fault.value) return          // the fault view itself failed: the app's handler takes it
  console.error(error)
  if (inEventHandler(info)) showToast(error)
  else fault.value = { message: faultMessage(error) }
  return false
})

watch(() => route.fullPath, () => {
  fault.value = null
})
</script>

<template>
  <FaultView v-if="fault" kind="view" :message="fault.message" />
  <slot v-else />
</template>
