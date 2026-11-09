<script setup lang="ts">
// The VM's GitHub owners: the accounts and organisations it may get tokens for. Marks owners
// without an Installation of the GitHub App (when GitHub can be asked); edit and save.
import type { GithubAppOverview } from '~/utils/access'
import type { Vm } from '~/utils/live'

const props = defineProps<{ vm: Vm }>()
const { t } = useI18n()
const live = useLive()
const errorText = useErrorText()
const toast = useToast()

const editing = ref(false)
const draft = ref<string[]>([])
const input = ref('')
const busy = ref(false)
const error = ref<unknown>(null)
const missing = ref<Set<string> | null>(null)

const owners = computed(() => props.vm.github_owners ?? [])
const inputValid = computed(() => !input.value.trim() || input.value.trim().split(/[\s,]+/).every(looksLikeGithubOwner))
const changed = computed(() => draft.value.join(',') !== owners.value.join(','))

onMounted(async () => {
  try {
    const app = await api<GithubAppOverview>('/github-app')
    missing.value = new Set(app.missing.filter(m => m.vms.includes(props.vm.name)).map(m => m.owner.toLowerCase()))
  }
  catch {
    // without GitHub the owners are shown without that mark
  }
})

function edit() {
  draft.value = [...owners.value]
  input.value = ''
  error.value = null
  editing.value = true
}

function addTyped() {
  const names = input.value.trim().split(/[\s,]+/).filter(Boolean)
  if (!names.length || !names.every(looksLikeGithubOwner)) return
  for (const n of names) if (!draft.value.some(d => d.toLowerCase() === n.toLowerCase())) draft.value.push(n)
  input.value = ''
}

async function save() {
  addTyped()
  if (!draft.value.length) return
  busy.value = true
  error.value = null
  try {
    await api(`/vms/${props.vm.name}/github-owners`, { method: 'PUT', body: { owners: draft.value } })
    toast.add({ color: 'success', icon: 'i-lucide-circle-check', title: t('access.owners.saved', { vm: props.vm.name }) })
    editing.value = false
    void live.refetchVms()
  }
  catch (e) {
    error.value = e
  }
  finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="space-y-3 rounded-lg border border-default p-4" data-testid="github-owners">
    <div class="flex flex-wrap items-start gap-2">
      <div class="min-w-0 flex-1">
        <h3 class="font-medium text-highlighted">
          {{ t('access.owners.title') }}
        </h3>
        <p class="text-sm text-muted">
          {{ t('access.owners.intro') }}
        </p>
      </div>
      <UButton v-if="!editing" color="neutral" variant="outline" size="sm" icon="i-lucide-pencil" :label="t('access.owners.edit')" data-testid="owners-edit" @click="edit" />
    </div>

    <template v-if="!editing">
      <p v-if="!owners.length" class="text-sm text-muted">
        {{ t('access.owners.none') }}
      </p>
      <ul v-else class="flex flex-wrap gap-2" data-testid="owners-list">
        <li v-for="o in owners" :key="o" class="flex items-center gap-1" :data-owner="o">
          <UBadge color="neutral" variant="subtle" icon="i-lucide-building-2" :label="o" />
          <UTooltip v-if="missing?.has(o.toLowerCase())" :text="t('access.owners.missingHint')">
            <UBadge color="warning" variant="soft" size="sm" :label="t('access.owners.missing')" data-testid="owner-missing" />
          </UTooltip>
        </li>
      </ul>
      <p v-if="missing?.size" class="text-sm text-muted">
        {{ t('access.owners.missingText') }}
        <ULink to="/system" class="text-primary">
          {{ t('access.owners.toSystem') }}
        </ULink>
      </p>
    </template>

    <form v-else class="space-y-3" novalidate @submit.prevent="addTyped">
      <ul class="flex flex-wrap gap-2">
        <li v-for="(o, i) in draft" :key="o">
          <UBadge color="neutral" variant="subtle" :label="o">
            <template #trailing>
              <button type="button" class="ms-1 rounded hover:text-error" :aria-label="t('access.owners.remove', { owner: o })" data-testid="owner-remove" @click="draft.splice(i, 1)">
                <UIcon name="i-lucide-x" class="size-3" />
              </button>
            </template>
          </UBadge>
        </li>
      </ul>
      <div class="flex flex-wrap gap-2">
        <UInput
          v-model="input"
          :placeholder="t('access.owners.placeholder')"
          autocomplete="off"
          spellcheck="false"
          class="min-w-0 flex-1"
          :color="inputValid ? undefined : 'error'"
          :aria-label="t('access.owners.add')"
          data-testid="owner-input"
        />
        <UButton type="submit" color="neutral" variant="outline" icon="i-lucide-plus" :label="t('access.owners.add')" :disabled="!input.trim() || !inputValid" />
      </div>
      <p v-if="!inputValid" class="text-sm text-error">
        {{ t('errors.invalid_github_owner') }}
      </p>
      <p v-if="!draft.length" class="text-sm text-warning">
        {{ t('errors.github_owner_required') }}
      </p>
      <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-circle-alert" :title="errorText(error)" role="alert" />
      <div class="flex flex-wrap justify-end gap-2">
        <UButton color="neutral" variant="outline" :label="t('access.common.cancel')" :disabled="busy" @click="editing = false" />
        <UButton icon="i-lucide-save" :label="t('access.common.save')" :loading="busy" :disabled="(!draft.length && !input.trim()) || !inputValid || (!changed && !input.trim())" data-testid="owners-save" @click="save" />
      </div>
    </form>
  </section>
</template>
