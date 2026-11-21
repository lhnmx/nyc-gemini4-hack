import { useEffect, useRef, useState } from 'react';
import { apiUrl } from './project.js';

export default function Builder({ initial, health, onClose, onCreated, onSubmitting }) {
  const dialog = useRef(null);
  const goalInput = useRef(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const node = dialog.current;
    if (submitting) return;
    node.showModal();
    goalInput.current.focus();
    return () => node.close();
  }, [submitting]);
  async function submit(event) {
    event.preventDefault();
    if (submitting || health.blocked === true) return;
    const body = new FormData(event.currentTarget);
    const goal = String(body.get('goal')).trim();
    if (goal.length < 2) {
      goalInput.current.setCustomValidity('Enter a project with at least two characters.');
      goalInput.current.reportValidity();
      return;
    }
    body.set('goal', goal);
    const ownedItems = String(body.get('owned_items') || '').split(',').map(item => item.trim()).filter(Boolean);
    onSubmitting({ goal, owned_items: ownedItems, status: 'submitting', media: {} });
    setSubmitting(true);
    setError('');
    try {
      const response = await fetch(apiUrl('/api/projects'), { method: 'POST', body });
      if (!response.ok) {
        const failure = await response.json().catch(() => null);
        throw new Error(typeof failure?.detail === 'string' ? failure.detail : 'Could not start this guide. Please try again.');
      }
      const data = await response.json();
      if (!data.project_id) throw new Error('The server did not return a guide. Please try again.');
      onCreated({
        project_id: data.project_id, goal, owned_items: ownedItems,
        status: data.status || 'queued', media: {}, plan: null,
      });
    } catch (problem) {
      setError(problem instanceof TypeError ? 'Could not reach the server. Check your connection and try again.' : problem.message);
      onSubmitting(null);
      setSubmitting(false);
    }
  }
  return <dialog id="builder" ref={dialog} aria-labelledby="builder-title"
    onCancel={event => { event.preventDefault(); if (!submitting) onClose(); }}>
    <div className="dialog-top"><h2 id="builder-title">Create a guide</h2><button type="button" className="close-dialog" aria-label="Close new guide" onClick={onClose} disabled={submitting}>×</button></div>
    <p className="intro">Turn a project into a narrated film and a step-by-step guide.</p>
    <form onSubmit={submit}>
      <label htmlFor="goal">Your project</label>
      <textarea id="goal" name="goal" ref={goalInput} rows="3" minLength="2" maxLength="300" required
        placeholder="Fold a paper plane" defaultValue={initial?.goal ?? ''}
        onChange={event => event.target.setCustomValidity('')} />
      <label htmlFor="owned">Supplies you have <span className="optional">(optional)</span></label>
      <input id="owned" name="owned_items" aria-describedby="owned-hint" placeholder="Origami paper, ruler, fine-tip markers" defaultValue={(initial?.owned_items || []).join(', ')} />
      <small id="owned-hint">Separate items with commas.</small>
      <p id="form-status" className="form-status" role="status">{error || (submitting ? 'Starting your guide…' : (health.blocked || health.reconnecting) ? health.detail : '')}</p>
      <button className="primary" type="submit" disabled={submitting || health.blocked === true}>Generate guide <span aria-hidden="true">→</span></button>
    </form>
  </dialog>;
}
