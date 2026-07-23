// @vitest-environment jsdom
import {fireEvent, render, screen} from '@testing-library/react';
import {describe, expect, it, vi} from 'vitest';
import {NewProject} from './NewProject';

describe('NewProject', () => {
  it('requires script, avatar, voice, and template before creating a project', async () => {
    const createProject = vi.fn().mockResolvedValue({id: 'project_1'});
    render(<NewProject api={{createProject}}/>);

    await screen.getByRole('button', {name: 'Create project'}).click();

    expect(screen.getByText('Script is required')).toBeTruthy();
    expect(screen.getByText('Avatar is required')).toBeTruthy();
    expect(screen.getByText('Voice is required')).toBeTruthy();
    expect(screen.getByText('Template is required')).toBeTruthy();
    expect(createProject).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText('Script'), {target: {value: 'A script'}});
    fireEvent.change(screen.getByLabelText('Avatar'), {target: {value: 'avatar_1'}});
    fireEvent.change(screen.getByLabelText('Voice'), {target: {value: 'voice_1'}});
    fireEvent.change(screen.getByLabelText('Template'), {target: {value: 'vertical_knowledge_v1'}});
    await screen.getByRole('button', {name: 'Create project'}).click();

    expect(createProject).toHaveBeenCalledWith({
      input: {type: 'script', content: 'A script'},
      avatar: {avatarId: 'avatar_1', voiceId: 'voice_1'},
      output: {templateId: 'vertical_knowledge_v1'}
    });
  });
});
