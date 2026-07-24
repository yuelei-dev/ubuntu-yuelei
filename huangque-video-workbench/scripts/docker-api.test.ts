import Fastify from 'fastify';
import {describe, expect, it} from 'vitest';
import {PassThrough} from 'node:stream';
import {browserShell, openMinioObjectWithinDeadline, registerDockerBrowserRoutes} from './docker-api.js';

describe('Docker browser routes', () => {
  it('bootstraps an HttpOnly SameSite Huangque session only for the development browser entry', async () => {
    const development = Fastify();
    registerDockerBrowserRoutes(development, {clientBundle: 'console.log(1)', html: '<!doctype html>', developmentMode: true});
    const production = Fastify();
    registerDockerBrowserRoutes(production, {clientBundle: 'console.log(1)', html: '<!doctype html>', developmentMode: false});

    const [developmentResponse, productionResponse] = await Promise.all([
      development.inject({method: 'GET', url: '/projects/new'}),
      production.inject({method: 'GET', url: '/projects/new'})
    ]);

    expect(developmentResponse.headers['set-cookie']).toMatch(/hq_session=localdev; Path=\/; HttpOnly; SameSite=Lax/);
    expect(productionResponse.headers['set-cookie']).toBeUndefined();
    await Promise.all([development.close(), production.close()]);
  });

  it('serves a production shell and bundle entirely below the configured UI mount', async () => {
    const html = browserShell({uiBasePath: '/video-workbench', apiBasePath: '/api/video-workbench'});
    expect(html).toContain('src="/video-workbench/workbench-client.js"');
    expect(html).toContain('name="workbench-api-base" content="/api/video-workbench"');
    expect(html).not.toContain('src="/workbench-client.js"');
    const app = Fastify();
    registerDockerBrowserRoutes(app, {
      clientBundle: 'console.log(1)',
      html,
      developmentMode: false
    });
    const bundle = await app.inject({method: 'GET', url: '/workbench-client.js'});
    expect(bundle.statusCode).toBe(200);
    expect(bundle.body).toContain('console.log(1)');
    await app.close();
  });
});

describe('private output first-byte deadline', () => {
  it('destroys a black-hole object stream and returns a controlled timeout', async () => {
    const stream = new PassThrough();
    await expect(openMinioObjectWithinDeadline(async () => stream, 10))
      .rejects.toMatchObject({name: 'ObjectReadTimeoutError'});
    expect(stream.destroyed).toBe(true);
  });

  it('terminates both sides when an output stalls after its first byte', async () => {
    const upstream = new PassThrough();
    upstream.write(Buffer.from([1]));
    const downstream = await openMinioObjectWithinDeadline(async () => upstream, 100, 10);
    const error = await new Promise<Error>((resolveError) => {
      downstream.once('error', resolveError);
      downstream.resume();
    });
    expect(error).toMatchObject({name: 'ObjectReadTimeoutError'});
    expect(upstream.destroyed).toBe(true);
    expect(downstream.destroyed).toBe(true);
  });
});
