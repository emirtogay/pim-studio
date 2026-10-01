/**
 * Small pill label for a product category. Used on product cards and in
 * filter chip groups. Plain span — carries no interactive behaviour itself.
 */

import { cn } from '../lib/cn'

export default function CategoryChip({
  category,
  className,
}: {
  category: string | null | undefined
  className?: string
}) {
  if (!category) return null

  return (
    <span
      className={cn(
        'inline-flex items-center rounded-[3px] bg-paper-sunk px-1.5 py-0.5',
        'font-mono text-[10.5px] uppercase tracking-[0.1em] text-ink-500',
        className,
      )}
    >
      {category}
    </span>
  )
}
