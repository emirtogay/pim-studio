/**
 * Copy text to the clipboard, on this panel's own origin.
 *
 * `navigator.clipboard` exists only in a secure context, and the panel is
 * served over plain HTTP on an IP address — `http://localhost:8003`. So in
 * production here the async Clipboard API is `undefined` and the
 * `execCommand` path below is the normal one, not the fallback. Written the
 * other way round, a copy button would work on localhost during development
 * and silently do nothing on the server, which is the worst of the two
 * failures: it looks implemented.
 *
 * Returns whether the text actually made it, so the caller can say "copied"
 * only when it did. `document.execCommand` is deprecated and still the only
 * thing that works on an insecure origin; when the panel moves to HTTPS the
 * first branch takes over and this one stops being reached.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false

  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Permission denied or a browser that lied about the API. Fall through.
    }
  }

  try {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    // Off-screen rather than hidden: `display:none` and `visibility:hidden`
    // are not selectable, so the copy would silently produce nothing.
    area.style.position = 'fixed'
    area.style.top = '0'
    area.style.left = '-9999px'
    document.body.appendChild(area)
    area.select()
    area.setSelectionRange(0, area.value.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}
