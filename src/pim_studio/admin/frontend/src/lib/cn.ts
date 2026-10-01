/** Concatenate class names, skipping falsy entries. Lightweight alternative
 *  to `clsx` — we don't need conditional/object syntax here. */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ')
}
