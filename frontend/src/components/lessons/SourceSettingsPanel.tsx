export type SourceSettingsPatch = { include_untagged_sources?: boolean; sources_only?: boolean };

/** What this subject may draw on (S26, V05). Presentational: the Lessons page owns the
 * request, so the switches can be tested without a server. */
export function SourceSettingsPanel({
  includeUntagged,
  sourcesOnly,
  onChange,
  disabled,
}: {
  includeUntagged: boolean;
  sourcesOnly: boolean;
  onChange: (patch: SourceSettingsPatch) => void;
  disabled?: boolean;
}) {
  return (
    <section className="flex max-w-2xl flex-col gap-3">
      <h2 className="text-h3">Sources</h2>
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          className="toggle toggle-sm mt-0.5"
          checked={includeUntagged}
          disabled={disabled}
          onChange={(e) => onChange({ include_untagged_sources: e.target.checked })}
        />
        <span className="flex flex-col">
          <span className="text-body">Also use my untagged materials</span>
          <span className="text-caption text-base-content/60">
            Uploads without a subject are left out unless you turn this on.
          </span>
        </span>
      </label>
      <label className="flex items-start gap-3">
        <input
          type="checkbox"
          className="toggle toggle-sm mt-0.5"
          checked={sourcesOnly}
          disabled={disabled}
          onChange={(e) => onChange({ sources_only: e.target.checked })}
        />
        <span className="flex flex-col">
          <span className="text-body">Teach only from my sources</span>
          <span className="text-caption text-base-content/60">
            Guru says when your sources don't cover something instead of filling in from general
            knowledge.
          </span>
        </span>
      </label>
    </section>
  );
}
