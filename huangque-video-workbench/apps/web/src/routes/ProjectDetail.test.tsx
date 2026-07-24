// @vitest-environment jsdom
import {act, cleanup, fireEvent, render, screen} from '@testing-library/react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {ProjectDetail} from './ProjectDetail';

describe('ProjectDetail', () => {
  afterEach(() => cleanup());
  it('regenerates one scene without submitting the full project', async () => {
    const regenerateScene = vi.fn().mockResolvedValue({jobId: 'job-2'});
    const api = {
      createProject: vi.fn(),
      getProject: vi.fn(),
      updateScene: vi.fn(),
      regenerateScene,
      renderProject: vi.fn(),
      subscribeToProjectEvents: vi.fn(() => ({close: vi.fn()}))
    };

    render(<ProjectDetail api={api} project={{
      id: 'project_1',
      title: 'Fixture project',
      status: 'GENERATING_ASSETS',
      scenes: [
        {id: 'scene_001', order: 1, status: 'READY', script: 'First', visual: {}, locked: true},
        {id: 'scene_002', order: 2, status: 'FAILED', script: 'Second', visual: {}, failureReason: 'Asset generation failed'}
      ],
      assetVersions: []
    }}/>);

    await screen.getByRole('button', {name: 'Regenerate scene_002'}).click();

    expect(regenerateScene).toHaveBeenCalledWith('project_1', 'scene_002');
    expect(api.renderProject).not.toHaveBeenCalled();
  });

  it('renders ordered locked cards, asset versions, scene errors, and editable scene scripts', async () => {
    const updateScene = vi.fn().mockResolvedValue(undefined);
    render(<ProjectDetail api={{
      createProject: vi.fn(), getProject: vi.fn(), updateScene,
      regenerateScene: vi.fn(), renderProject: vi.fn(),
      subscribeToProjectEvents: vi.fn(() => ({close: vi.fn()}))
    } as any} project={{
      id: 'project_1', title: 'Fixture project', status: 'PARTIALLY_FAILED',
      scenes: [
        {id: 'scene_002', order: 2, status: 'FAILED', script: 'Second', visual: {}, failureReason: 'Provider timeout'},
        {id: 'scene_001', order: 1, status: 'READY', script: 'First', visual: {}, locked: true}
      ],
      assetVersions: [{id: 'asset_1', sceneId: 'scene_001', version: 3, uri: '/asset.mp4'}]
    }}/>);

    expect(screen.getAllByTestId('storyboard-card').map((card) => card.getAttribute('data-scene-id'))).toEqual(['scene_001', 'scene_002']);
    expect(screen.getByText('Locked')).toBeTruthy();
    expect(screen.getByText('Asset version 3')).toBeTruthy();
    expect(screen.getByText('FAILED')).toBeTruthy();
    expect(screen.getByRole('alert', {name: 'Scene scene_002 failed'}).textContent).toContain('Provider timeout');

    fireEvent.change(screen.getByLabelText('Script for scene_002'), {target: {value: 'Changed script'}});
    await screen.getByRole('button', {name: 'Save scene_002'}).click();
    expect(updateScene).toHaveBeenCalledWith('project_1', 'scene_002', {script: 'Changed script'});
  });

  it('shows preview and final download controls accessibly', () => {
    render(<ProjectDetail api={{regenerateScene: vi.fn()}} project={{
      id: 'project_1', title: 'Completed project', status: 'COMPLETED', scenes: [], assetVersions: [],
      previewUrl: '/preview.mp4', downloadUrl: '/final.mp4'
    } as any}/>);

    expect(screen.getByLabelText('Project preview').getAttribute('src')).toBe('/preview.mp4');
    expect(screen.getByRole('link', {name: 'Download final video'}).getAttribute('href')).toBe('/final.mp4');
  });

  it('falls back to five-second polling after two SSE disconnects and cleans it up at terminal state', async () => {
    vi.useFakeTimers();
    const errors: Array<() => void> = [];
    const close = vi.fn();
    const getProject = vi.fn().mockResolvedValue({
      id: 'project_1', title: 'Completed project', status: 'COMPLETED', scenes: [], assetVersions: []
    });
    const api = {
      getProject, regenerateScene: vi.fn(), renderProject: vi.fn(), updateScene: vi.fn(), createProject: vi.fn(),
      subscribeToProjectEvents: vi.fn((_projectId: string, _onEvent: unknown, onError: () => void) => {
        errors.push(onError);
        return {close};
      })
    };
    const tree = render(<ProjectDetail api={api as any} project={{
      id: 'project_1', title: 'Processing project', status: 'RENDERING', scenes: [], assetVersions: []
    }}/>);

    act(() => { errors[0](); errors[0](); });
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });

    expect(getProject).toHaveBeenCalledWith('project_1');
    expect(close).toHaveBeenCalled();
    tree.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(getProject).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it('reports malformed and missing project data instead of throwing', () => {
    const {rerender} = render(<ProjectDetail api={{regenerateScene: vi.fn()}} project={null as any}/>);
    expect(screen.getByRole('alert').textContent).toContain('Project data is unavailable.');
    rerender(<ProjectDetail api={{regenerateScene: vi.fn()}} project={{id: 'project_1'} as any}/>);
    expect(screen.getByRole('alert').textContent).toContain('Project data is unavailable.');
  });

  it.each([
    {label: 'null scene', project: {id: 'project_1', title: 'Bad', status: 'RENDERING', scenes: [null], assetVersions: []}},
    {label: 'null visual', project: {id: 'project_1', title: 'Bad', status: 'RENDERING', scenes: [{id: 'scene_001', order: 1, status: 'READY', script: 'Text', visual: null}], assetVersions: []}},
    {label: 'malformed asset version', project: {id: 'project_1', title: 'Bad', status: 'RENDERING', scenes: [], assetVersions: [{id: 'asset_1', sceneId: 'scene_001', version: 'one', uri: '/asset.mp4'}]}}
  ])('renders a visible error for $label', ({project}) => {
    render(<ProjectDetail api={{regenerateScene: vi.fn()}} project={project as any}/>);
    expect(screen.getByRole('alert').textContent).toContain('Project data is unavailable.');
  });

  it('renders a visible error when an SSE update has malformed JSON shape', () => {
    let onProject: ((project: any) => void) | undefined;
    render(<ProjectDetail api={{
      regenerateScene: vi.fn(),
      subscribeToProjectEvents: vi.fn((_id, listener) => { onProject = listener; return {close: vi.fn()}; })
    }} project={{id: 'project_1', title: 'Live project', status: 'RENDERING', scenes: [], assetVersions: []}}/>);

    act(() => onProject?.({id: 'project_1', title: 'Broken update', status: 'RENDERING', scenes: [null], assetVersions: []}));

    expect(screen.getByRole('alert').textContent).toContain('Project update data is unavailable.');
  });
});
