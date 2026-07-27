export function FormField({
  id,
  label,
  icon: Icon,
  error,
  trailing,
  className = '',
  ...inputProps
}) {
  const errorId = `${id}-error`

  return (
    <div className={`form-control ${className}`.trim()}>
      <label className="field-label" htmlFor={id}>{label}</label>
      <div className={`field ${error ? 'invalid' : ''}`}>
        <Icon size={18} aria-hidden="true" />
        <input
          id={id}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? errorId : undefined}
          {...inputProps}
        />
        {trailing}
      </div>
      {error && <p id={errorId} className="field-error">{error}</p>}
    </div>
  )
}
