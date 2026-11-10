export type OwnedOperation = Readonly<{
  signal: AbortSignal
  isCurrent: () => boolean
  commit: (effect: () => void) => boolean
}>

export class OperationOwner {
  private generation = 0
  private controller: AbortController | null = null

  invalidate() {
    this.generation += 1
    const previous = this.controller
    this.controller = null
    previous?.abort()
  }

  begin(valid: () => boolean): OwnedOperation {
    this.invalidate()
    const generation = this.generation
    const controller = new AbortController()
    this.controller = controller
    const isCurrent = () => this.generation === generation && this.controller === controller && !controller.signal.aborted && valid()
    return {
      signal: controller.signal,
      isCurrent,
      commit: effect => {
        if (!isCurrent()) return false
        effect()
        return true
      },
    }
  }
}
