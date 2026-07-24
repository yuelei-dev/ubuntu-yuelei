import {createServer} from 'node:http';

const server = createServer((request, response) => {
  if (request.method === 'GET' && request.url === '/api/auth/me') {
    response.writeHead(200, {'content-type': 'application/json'});
    response.end(JSON.stringify({user: {username: 'localdev', role: 'editor'}}));
    return;
  }
  response.writeHead(404).end();
});

server.listen(8095, '0.0.0.0');
