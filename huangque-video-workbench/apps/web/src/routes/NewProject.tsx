import {useState, type FormEvent} from 'react';

export type CreateProjectInput = {
  input: {type: 'script'; content: string};
  avatar: {avatarId: string; voiceId: string};
  output: {templateId: string};
};

export type NewProjectApi = {
  createProject(input: CreateProjectInput): Promise<{id: string}>;
};

export const NewProject = ({api, onCreated}: {api: NewProjectApi; onCreated?: (projectId: string) => void}) => {
  const [script, setScript] = useState('');
  const [avatarId, setAvatarId] = useState('');
  const [voiceId, setVoiceId] = useState('');
  const [templateId, setTemplateId] = useState('');
  const [errors, setErrors] = useState<string[]>([]);
  const [submissionError, setSubmissionError] = useState<string>();
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextErrors = [
      !script.trim() && 'Script is required',
      !avatarId.trim() && 'Avatar is required',
      !voiceId.trim() && 'Voice is required',
      !templateId.trim() && 'Template is required'
    ].filter((value): value is string => Boolean(value));
    setErrors(nextErrors);
    setSubmissionError(undefined);
    if (nextErrors.length > 0) return;

    setSubmitting(true);
    try {
      const project = await api.createProject({
        input: {type: 'script', content: script.trim()},
        avatar: {avatarId: avatarId.trim(), voiceId: voiceId.trim()},
        output: {templateId: templateId.trim()}
      });
      onCreated?.(project.id);
    } catch {
      setSubmissionError('Project could not be created. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return <main>
    <h1>New video project</h1>
    <form noValidate onSubmit={submit} aria-describedby={errors.length ? 'project-form-errors' : undefined}>
      {errors.length > 0 && <ul id="project-form-errors" role="alert">{errors.map((error) => <li key={error}>{error}</li>)}</ul>}
      {submissionError && <p role="alert">{submissionError}</p>}
      <p><label htmlFor="project-script">Script</label><textarea id="project-script" value={script} onChange={(event) => setScript(event.target.value)}/></p>
      <p><label htmlFor="project-avatar">Avatar</label><input id="project-avatar" value={avatarId} onChange={(event) => setAvatarId(event.target.value)}/></p>
      <p><label htmlFor="project-voice">Voice</label><input id="project-voice" value={voiceId} onChange={(event) => setVoiceId(event.target.value)}/></p>
      <p><label htmlFor="project-template">Template</label><input id="project-template" value={templateId} onChange={(event) => setTemplateId(event.target.value)}/></p>
      <button type="submit" disabled={submitting}>{submitting ? 'Creating project' : 'Create project'}</button>
    </form>
  </main>;
};
