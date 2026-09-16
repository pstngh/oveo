export function BrandLogo({ compact = false, decorative = false }: {
  compact?: boolean;
  decorative?: boolean;
}) {
  return <span
    aria-hidden={decorative || undefined}
    aria-label={decorative ? undefined : "Oveo"}
    className={`brand-lockup${compact ? " brand-lockup-compact" : ""}`}
    role={decorative ? undefined : "img"}
  >
    <span className="brand-mark" aria-hidden="true" />
    <span className="brand-wordmark" aria-hidden="true">OVEO</span>
  </span>;
}

export default BrandLogo;
