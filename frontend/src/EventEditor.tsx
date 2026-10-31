import { useEffect, useRef, useState } from "react";
import { buildEventPatch, eventDraft } from "./eventEditing";
import type { EventPatch, ScheduleMode } from "./eventEditing";
import type { EventRecord } from "./types";

interface Props {
  event: EventRecord;
  saving: boolean;
  onSave: (id: string, patch: EventPatch) => Promise<void>;
  onClose: () => void;
}

export function EventEditor({ event, saving, onSave, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState(() => eventDraft(event));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const node = dialog.current;
    if (node && !node.open) node.showModal();
    return () => node?.close();
  }, []);
  async function submit(e: React.SubmitEvent<HTMLFormElement>) {
    e.preventDefault();
    if (saving) return;
    setError(null);
    try {
      const patch = buildEventPatch(event, draft);
      if (Object.keys(patch).length === 0) onClose();
      else await onSave(event.id, patch);
    } catch (failure: unknown) {
      setError(failure instanceof Error ? failure.message : "Не удалось сохранить изменения.");
    }
  }
  return <dialog ref={dialog} className="event-editor" aria-labelledby="editor-heading"
    onCancel={(e) => { e.preventDefault(); if (!saving) onClose(); }}>
    <form onSubmit={submit}>
      <h2 id="editor-heading">Редактировать запись</h2>
      <fieldset disabled={saving}>
        <label>Название<input name="name" value={draft.name} maxLength={200} required
          onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></label>
        {event.event_type === "task" && <label>Срок<select name="schedule" value={draft.mode}
          onChange={(e) => setDraft({ ...draft, mode: e.target.value as ScheduleMode })}>
          <option value="none">Без срока</option><option value="date">Только дата</option>
          <option value="time">Дата и время</option>
        </select></label>}
        {draft.mode !== "none" && <div className="editor-fields">
          <label>Дата<input name="date" type="date" required value={draft.date}
            onChange={(e) => setDraft({ ...draft, date: e.target.value })} /></label>
          {draft.mode === "time" && <label>Время<input name="time" type="time" required value={draft.time}
            onChange={(e) => setDraft({ ...draft, time: e.target.value })} /></label>}
        </div>}
        {draft.mode === "time" && event.event_type !== "reminder" && <>
          <label className="editor-checkbox"><input name="range" type="checkbox" checked={draft.range}
            onChange={(e) => setDraft({ ...draft, range: e.target.checked })} />Указать окончание</label>
          {draft.range && <div className="editor-fields">
            <label>Дата окончания<input name="endDate" type="date" required value={draft.endDate}
              onChange={(e) => setDraft({ ...draft, endDate: e.target.value })} /></label>
            <label>Время окончания<input name="endTime" type="time" required value={draft.endTime}
              onChange={(e) => setDraft({ ...draft, endTime: e.target.value })} /></label>
          </div>}
        </>}
      </fieldset>
      <p className="editor-timezone">Время указано по часовому поясу устройства.</p>
      {error && <p className="status-error" role="alert">{error}</p>}
      <div className="editor-buttons">
        <button type="button" disabled={saving} onClick={onClose}>Отмена</button>
        <button className="editor-save" type="submit" disabled={saving}>{saving ? "Сохранение…" : "Сохранить"}</button>
      </div>
    </form>
  </dialog>;
}
