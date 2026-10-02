/** The product mark: the header and the chip of the search loading scene share it. */
export function BrandMark({ className }: { className?: string }) {
  return <svg className={className} viewBox="0 0 64 64" fill="none" aria-hidden="true">
    <path d="M32 3 60 32 32 61 4 32Z" stroke="currentColor" strokeWidth="2" />
    <path d="M18 44V21l14 17 14-17v23M14 44h9m18 0h9" stroke="currentColor" strokeWidth="3" strokeLinejoin="round" />
  </svg>;
}
