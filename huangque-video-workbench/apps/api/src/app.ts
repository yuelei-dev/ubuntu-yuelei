import Fastify, {type FastifyInstance, type FastifyReply, type FastifyRequest} from 'fastify';
import {projectRoutes} from './routes/projects.js';
import {ProjectService, type ProjectRepository} from './services/project-service.js';
import type {QueueAdapter} from './queue.js';
import {InMemoryProjectEventBroker, openProjectEventStream, type ProjectEventBroker} from './events.js';
import {authenticateHuangque, HuangqueAuthenticationError, type HuangqueIdentity} from './auth/huangque-auth.js';
import {ObjectReadTimeoutError, outputObjectKey, projectForClient, type ObjectReader} from './routes/output.js';

export type AppDependencies = {
  repository: ProjectRepository;
  queue: QueueAdapter;
  idFactory: () => string;
  projectEvents?: ProjectEventBroker;
  heartbeatMs?: number;
  authenticate?: (cookieHeader: string | undefined, signal: AbortSignal) => Promise<HuangqueIdentity>;
  objectReader?: ObjectReader;
  publicApiBasePath?: string;
};
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
  const authenticate = dependencies.authenticate ?? authenticateHuangque;
  const events = dependencies.projectEvents ?? new InMemoryProjectEventBroker();
  const heartbeatMs = dependencies.heartbeatMs ?? 15_000;
  const publicApiBasePath = dependencies.publicApiBasePath ?? '/api';
  const publish = async (projectId: string, ownerUsername: string): Promise<void> => {
    const project = await service.get(projectId, ownerUsername);
    if (project) events.publish(project);
  };
  const identityFor = async (request: FastifyRequest): Promise<HuangqueIdentity | {statusCode: 401 | 503; payload: {error: string}}> => {
    const cancellation = new AbortController();
    const abort = (): void => cancellation.abort();
    if (request.raw.aborted) abort();
    else request.raw.once('aborted', abort);
    try {
      return await authenticate(request.headers.cookie, cancellation.signal);
    } catch (error) {
      if (error instanceof HuangqueAuthenticationError && error.statusCode === 503) {
        return {statusCode: 503, payload: {error: 'authentication_unavailable'}};
      }
      return {statusCode: 401, payload: {error: 'unauthorized'}};
    } finally {
      request.raw.off('aborted', abort);
    }
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
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const response = await routes.create(request.body, identity.username);
    if (response.statusCode < 400 && response.payload && typeof response.payload === 'object' && typeof (response.payload as {id?: unknown}).id === 'string') {
      await publish((response.payload as {id: string}).id, identity.username);
    }
    replyWith(reply, response);
  });
  app.get<{Params: IdParams}>('/api/projects/:id', async (request, reply) => {
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const response = await routes.get(request.params.id, identity.username);
    if (response.statusCode === 200 && response.payload && typeof response.payload === 'object') {
      replyWith(reply, {
        ...response,
        payload: projectForClient(response.payload as import('./services/project-service.js').ProjectDetail, publicApiBasePath)
      });
      return;
    }
    replyWith(reply, response);
  });
  app.patch<{Params: SceneParams}>('/api/projects/:id/scenes/:sceneId', async (request, reply) => {
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const response = await routes.patchScene(request.params.id, request.params.sceneId, request.body, identity.username);
    if (response.statusCode < 400) await publish(request.params.id, identity.username);
    replyWith(reply, response);
  });
  app.post<{Params: SceneParams}>('/api/projects/:id/scenes/:sceneId/regenerate', async (request, reply) => {
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const response = await routes.regenerate(request.params.id, request.params.sceneId, identity.username);
    if (response.statusCode < 400) await publish(request.params.id, identity.username);
    replyWith(reply, response);
  });
  app.post<{Params: IdParams}>('/api/projects/:id/render', async (request, reply) => {
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const response = await routes.render(request.params.id, identity.username);
    if (response.statusCode < 400) await publish(request.params.id, identity.username);
    replyWith(reply, response);
  });
  app.get<{Params: IdParams}>('/api/projects/:id/events', async (request, reply) => {
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const project = await service.get(request.params.id, identity.username);
    if (!project) return replyWith(reply, {statusCode: 404, payload: {error: 'not_found', resource: 'project'}});
    reply.hijack();
    reply.raw.writeHead(200, {'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache', connection: 'keep-alive'});
    openProjectEventStream({raw: reply.raw, broker: events, project, heartbeatMs, publicApiBasePath});
  });
  const output = async (request: FastifyRequest<{Params: IdParams}>, reply: FastifyReply) => {
    const identity = await identityFor(request);
    if ('statusCode' in identity) return replyWith(reply, identity);
    const project = await service.get(request.params.id, identity.username);
    if (!project) return replyWith(reply, {statusCode: 404, payload: {error: 'not_found', resource: 'project'}});
    const objectKey = outputObjectKey(project);
    if (!objectKey || !dependencies.objectReader) return replyWith(reply, {statusCode: 404, payload: {error: 'not_found', resource: 'output'}});
    try {
      return reply.type('video/mp4').header('cache-control', 'no-store').send(await dependencies.objectReader.open(objectKey));
    } catch (error) {
      if (error instanceof ObjectReadTimeoutError) {
        return replyWith(reply, {statusCode: 504, payload: {error: 'output_storage_timeout'}});
      }
      return replyWith(reply, {statusCode: 404, payload: {error: 'not_found', resource: 'output'}});
    }
  };
  app.get<{Params: IdParams}>('/api/projects/:id/output', output);
  app.get<{Params: IdParams}>('/projects/:id/output', output);
  return app;
};

/** A runnable Fastify application with all external state injected. */
export const createApp = (dependencies: AppDependencies): FastifyInstance => createFastifyApp(() => Fastify({
  bodyLimit: 256 * 1024,
  routerOptions: {
    onBadUrl: (_path, _request, response) => {
      response.statusCode = 400;
      response.setHeader('content-type', 'application/json');
      response.end(JSON.stringify({error: 'validation_error', issues: [{path: 'id', message: 'must be URI encoded'}]}));
    }
  }
}), dependencies);
