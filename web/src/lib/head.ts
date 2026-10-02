export const PRODUCT_NAME = "Interviewer Agent"

/** A route's document title: "<page> · Interviewer Agent". */
export function pageHead(page?: string) {
  return {
    meta: [{ title: page ? `${page} · ${PRODUCT_NAME}` : PRODUCT_NAME }],
  }
}
