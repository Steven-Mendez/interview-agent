import { useCallback, useEffect, useRef, useState } from "react"
import { previewResume } from "@/lib/api"
import type { ResumePreview } from "@/lib/api"

/** Changing the file invalidates both cached text and any in-flight result. */
export function useResumePreview() {
  const [data, setData] = useState<ResumePreview | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const generation = useRef(0)
  const active = useRef<{ file: File; controller: AbortController } | null>(
    null
  )
  const cached = useRef<{ file: File; data: ResumePreview } | null>(null)

  const reset = useCallback(() => {
    generation.current += 1
    active.current?.controller.abort()
    active.current = null
    cached.current = null
    setData(null)
    setError(null)
    setPending(false)
  }, [])

  useEffect(
    () => () => {
      generation.current += 1
      active.current?.controller.abort()
    },
    []
  )

  const load = useCallback(
    async (file: File): Promise<ResumePreview | null> => {
      if (cached.current?.file === file) return cached.current.data
      if (active.current?.file === file) return null
      active.current?.controller.abort()
      const controller = new AbortController()
      const current = ++generation.current
      active.current = { file, controller }
      setData(null)
      setError(null)
      setPending(true)
      try {
        const result = await previewResume(file, controller.signal)
        if (current !== generation.current || controller.signal.aborted)
          return null
        cached.current = { file, data: result }
        setData(result)
        return result
      } catch (cause) {
        if (current === generation.current && !controller.signal.aborted) {
          setError(
            cause instanceof Error
              ? cause
              : new Error("Could not read the resume.")
          )
        }
        return null
      } finally {
        if (current === generation.current) {
          active.current = null
          setPending(false)
        }
      }
    },
    []
  )

  return { data, pending, error, load, reset }
}
