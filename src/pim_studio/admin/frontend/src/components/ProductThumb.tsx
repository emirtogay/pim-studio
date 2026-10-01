/**
 * Product thumbnail with a diagonal-stripe placeholder when no image is
 * available. Used across all pages that show product cards.
 *
 * The `size` prop maps to a fixed px square to keep thumbnails consistent
 * within a given context (48px in table rows, 64px in update cards, etc.).
 */

import { cn } from '../lib/cn'

type ThumbSize = 48 | 56 | 64 | 80 | 120

const SIZE_CLASS: Record<ThumbSize, string> = {
  48: 'h-12 w-12',
  56: 'h-14 w-14',
  64: 'h-16 w-16',
  80: 'h-20 w-20',
  120: 'h-[120px] w-[120px]',
}

export default function ProductThumb({
  src,
  alt,
  size = 56,
  className,
}: {
  src: string | null | undefined
  alt?: string
  size?: ThumbSize
  className?: string
}) {
  const base = cn('rounded-xs overflow-hidden flex-shrink-0 object-cover', SIZE_CLASS[size], className)

  if (!src) {
    return <div className={cn(base, 'ph-img')} aria-hidden="true" />
  }

  return (
    <img
      src={src}
      alt={alt ?? ''}
      className={cn(base, 'object-cover')}
      loading="lazy"
      onError={(e) => {
        // Replace broken image with the placeholder pattern.
        const el = e.currentTarget
        el.style.display = 'none'
        const ph = document.createElement('div')
        ph.className = el.className + ' ph-img'
        el.parentNode?.insertBefore(ph, el)
      }}
    />
  )
}
