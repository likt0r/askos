// Images and Builds of the System page: the resource `images` (utils/resources.ts), rebuilds and
// Update now (#114); a Build's log is the resource `buildLog` (components/system/SystemBuildLog).
// Whether an update of every Image runs comes from the `services` event (`updating`), before
// that from the overview.
import { computed, onScopeDispose, ref } from 'vue'
import type { BuildEvent } from '~/utils/images'

export function useImages() {
  const live = useLive()
  const resource = useResource('images')
  const overview = resource.data
  const { t } = useI18n()
  const starting = ref<string | null>(null)
  // whether the update runs by the last `services` event (the backend decides, also while its
  // unit still stops); null before one
  const systemUpdating = useSystem().updating
  const updateUnit = computed<boolean | null>(() => (typeof systemUpdating.value === 'boolean' ? systemUpdating.value : null))
  const updating = computed(() => updateUnit.value ?? overview.value?.updating ?? false)

  // a rebuild from its button: a failure is a toast, except a Build or an update that runs
  // already, which the page says beside its buttons
  const RUNNING = ['build_running', 'update_running']
  const rebuild = useChange('rebuild', { own: RUNNING, failed: () => t('system.images.rebuildFailed') })
  // an Update of one Image and Update now, from their dialogs: a failure shows there
  const update = useChange('updateImage', { form: true })
  const updateNow = useChange('updateNow', { form: true })
  /** a rebuild refused because a Build or an update runs already */
  const startError = computed(() => {
    const error = rebuild.error.value
    return error instanceof ApiError && RUNNING.includes(error.code) ? error : null
  })

  /** Start a Build of an Image (or "all"). Whether it started. */
  async function start(target: string) {
    starting.value = target
    try {
      return (await rebuild.run(target)).ok
    }
    finally {
      starting.value = null
    }
  }

  /** An Update: a Build of the Image, and when it succeeds, its unit resets these VMs onto it.
   * Whether it started. */
  async function startImageUpdate(image: string, reset: string[]) {
    starting.value = image
    try {
      return (await update.run({ image, reset })).ok
    }
    finally {
      starting.value = null
    }
  }

  /** Update now: the version check, a Build of every Image with an update, then a Reset of
   * these VMs onto the newest Build of their Image. Whether it started. */
  async function startUpdate(reset: string[] = []) {
    starting.value = 'update'
    try {
      return (await updateNow.run(reset)).ok
    }
    finally {
      starting.value = null
    }
  }

  // the overview follows `build` events in the resources module; here the start error
  const offBuild = live.on('build', (event: BuildEvent) => {
    if (!event?.image || !event.build) return
    rebuild.reset()             // what it said (a Build running already) is out of date
  })

  onScopeDispose(offBuild)

  return { resource, startError, starting, updating, update, updateNow, start, startImageUpdate, startUpdate }
}
