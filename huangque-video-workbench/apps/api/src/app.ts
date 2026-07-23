import Fastify, {type FastifyInstance, type FastifyReply} from 'fastify';
import {projectRoutes} from './routes/projects.js';
import {ProjectService, type ProjectRepository} from './services/project-service.js';
import type {QueueAdapter} from './queue.js';
import {InMemoryProjectEventBroker, openProjectEventStream, type ProjectEventBroker} from './events.js';

export type AppDependencies = {repository: ProjectRepository; queue: QueueAdapter; idFactory: () => string; projectEvents?: ProjectEventBroker; heartbeatMs?: number};
export type FastifyFactory = () => FastifyInstance;

type IdParams = {id: string};
type SceneParams = {id: string; sceneId: string};

const replyWith = (reply: FastifyReply, response: {statusCode: number; payload: unknown}): void => {
  reply.code(response.statusCode).send(response.payload);
};

/**
 * Registers Task 4 routes on a Fastify instance. Production code supplies the
 * standard Fastify factory; tests may supply a separately configured factory.
 */
export const createFastifyApp = (factory: FastifyFactory, dependencies: AppDependencies): FastifyInstance => {
  const app = factory();
  const service = new ProjectService(dependencies.repository, dependencies.queue, dependencies.idFactory);
  const routes = projectRoutes(service);
  const events = dependencies.projectEvents ?? new InMemoryProjectEventBroker();
  const heartbeatMs = dependencies.heartbeatMs ?? 15_000;
  const publish = async (projectId: string): Promise<void> => {
    const project = await service.get(projectId);
    if (project) events.publish(project);
  };

  app.setErrorHandler((error, _request, reply) => {
    const failure = error as {statusCode?: number; message?: string};
    if (failure.statusCode === 400) {
      reply.code(400).send({error: 'validation_error', issues: [{path: 'request', message: failure.message ?? 'invalid request'}]});
      return;
    }
    reply.send(error);
  });

  app.post('/api/projects', async (request, reply) => {
    const response = await routes.create(request.body);
    if (response.statusCode < 400 && response.payload && typeof response.payload === 'object' && typeof (response.payload as {id?: unknown}).id === 'string') {
      await publish((response.payload as {id: string}).id);
    }
    replyWith(reply, response);
  });
  app.get<{Params: IdParams}>('/api/projects/:id', async (request, reply) => replyWith(reply, await routes.get(request.params.id)));
  app.patch<{Params: SceneParams}>('/api/projects/:id/scenes/:sceneId', async (request, reply) => {
    const response = await routes.patchScene(request.params.id, request.params.sceneId, request.body);
    if (response.statusCode < 400) await publish(request.params.id);
    replyWith(reply, response);
  });
  app.post<{Params: SceneParams}>('/api/projects/:id/scenes/:sceneId/regenerate', async (request, reply) => {
    const response = await routes.regenerate(request.params.id, request.params.sceneId);
    if (response.statusCode < 400) await publish(request.params.id);
    replyWith(reply, response);
  });
  app.post<{Params: IdParams}>('/api/projects/:id/render', async (request, reply) => {
    const response = await routes.render(request.params.id);
    if (response.statusCode < 400) await publish(request.params.id);
    replyWith(reply, response);
  });
  app.get<{Params: IdParams}>('/api/projects/:id/events', async (request, reply) => {
    const project = await service.get(request.params.id);
    if (!project) return replyWith(reply, {statusCode: 404, payload: {error: 'not_found', resource: 'project'}});
    reply.hijack();
    reply.raw.writeHead(200, {'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive'});
    openProjectEventStream({raw: reply.raw, broker: events, project, heartbeatMs});
  });
  return app;
};

/** A runnable Fastify application with all external state injected. */
export const createApp = (dependencies: AppDependencies): FastifyInstance => createFastifyApp(() => Fastify({
  routerOptions: {
    onBadUrl: (_path, _request, response) => {
      response.statusCode = 400;
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify({error: 'validation_error', issues: [{path: 'id', message: 'must be URI encoded'}]}));
    }
  }
}), dependencies);
