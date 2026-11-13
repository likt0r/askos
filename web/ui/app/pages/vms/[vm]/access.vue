<script setup lang="ts">
// A VM's Access tab: its GitHub owners, its Claude token and OpenCode key, the GitHub tokens it got and the
// refused requests.
const route = useRoute()
const live = useLive()
const name = computed(() => String(route.params.vm))
const vm = computed(() => live.state.vms.find(v => v.name === name.value))
</script>

<template>
  <div v-if="vm" class="space-y-4" data-testid="access-tab">
    <div class="grid items-start gap-4 lg:grid-cols-2">
      <AccessGithubOwners :key="`owners-${vm.name}`" :vm="vm" />
      <AccessClaudeToken :key="`claude-${vm.name}`" :vm="vm" />
      <AccessOpencodeKey :key="`opencode-${vm.name}`" :vm="vm" />
    </div>
    <AccessGithubTokens :vm="vm.name" />
  </div>
</template>
