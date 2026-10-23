/**
 * Whose terminal a symbol comes from (ADR-0032). Two brokers can list the same ticker — `GOLD` is
 * the metal at one and a mining share at the other — so the name alone does not say which market
 * a row is. Nothing is drawn for a server nobody registered.
 */
export function BrokerTag(props: { broker: string | null | undefined }): React.JSX.Element | null {
  if (!props.broker) return null
  return (
    <span
      className="shrink-0 rounded bg-slate-800 px-1 text-[10px] text-slate-300"
      title={`listed by ${props.broker}`}
    >
      {props.broker}
    </span>
  )
}
