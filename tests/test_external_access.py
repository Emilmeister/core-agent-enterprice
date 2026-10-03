"""ENT-AC-85: owner HTTP boundary and a controlled Keycloak wire transport."""
import json
import time
import uuid
from urllib.parse import parse_qs

import httpx

from tests.test_auth import AuthAppTestCase, ISSUER
from core_agent.model import ModelResponse, ScriptedModel, ToolRequest


class ExternalAccessTests(AuthAppTestCase):
    durable_blobs = True

    async def asyncSetUp(self):
        self.clients = {}
        self.admin_calls = []
        self.admin_status = 200
        self.issued = 0
        self.failure_path = ""
        self.malformed = False
        self.malformed_after_create = False
        self.role_names = ['agent-external']
        self.delete_fault = ''
        await super().asyncSetUp()

    def introspect(self, request):
        if request.url.path.endswith('/token/introspect'):
            form = parse_qs(request.content.decode())
            claims = dict(self.tokens.get(form['token'][0], {'active': False}))
            if form['token'][0].startswith('issued-'):
                claims['active'] = False
            for client in self.clients.values():
                if claims.get('sub') == 'service-' + client['id']:
                    claims['active'] = client['enabled'] and claims.get('iat', 0) >= client.get('notBefore', 0)
            return httpx.Response(200, json=claims)
        if request.url.path.endswith('/token'):
            form = parse_qs(request.content.decode())
            client = next(row for row in self.clients.values() if row['clientId'] == form['client_id'][0])
            self.assertEqual(form['client_secret'], ['never-expose-secret'])
            self.assertEqual(form['scope'], ['offline_access'])
            self.issued += 1
            token = 'issued-' + str(self.issued)
            now = int(time.time())
            duration = int(client['attributes']['access.token.lifespan'])
            self.tokens[token] = {'active': True, 'iss': ISSUER, 'sub': 'service-' + client['id'],
                'aud': ['company-agent'], 'exp': now + duration, 'iat': now, 'token_type': 'Bearer',
                'realm_access': {'roles': ['agent-external']}}
            return httpx.Response(200, json={'access_token': token, 'token_type': 'Bearer', 'expires_in': duration})
        self.admin_calls.append(request)
        self.assertIn(request.headers['authorization'], ('Bearer owner-a', 'Bearer owner-b'))
        if self.admin_status != 200:
            return httpx.Response(self.admin_status, json={'secret': 'never-expose-secret'})
        path = request.url.path.removeprefix('/admin/realms/company')
        if path == self.failure_path:
            return httpx.Response(500, json={'secret': 'never-expose-secret'})
        payload = json.loads(request.content) if request.content else None
        if path == '/clients':
            if request.method == 'POST':
                if any(row['clientId'] == payload['clientId'] for row in self.clients.values()):
                    return httpx.Response(409)
                identifier = str(uuid.uuid4())
                self.clients[identifier] = {**payload, 'id': identifier}
                return httpx.Response(201)
            rows = list(self.clients.values())
            if 'clientId' in request.url.params:
                rows = [row for row in rows if row['clientId'] == request.url.params['clientId']]
            if self.malformed_after_create and rows:
                return httpx.Response(200, json={})
            return httpx.Response(200, json=['unexpected'] if self.malformed else rows[int(request.url.params.get('first', 0)):][:100])
        if path.startswith('/roles/'):
            name = path.rsplit('/', 1)[1]
            return httpx.Response(200, json={'id': name, 'name': name})
        if path.startswith('/users/'):
            if request.method == 'POST':
                return httpx.Response(204)
            return httpx.Response(200, json=[{'id': 'role', 'name': name} for name in self.role_names])
        if path.startswith('/clients/'):
            identifier = path.split('/')[2]
            if identifier not in self.clients:
                return httpx.Response(404)
            if path.endswith('/service-account-user'):
                return httpx.Response(200, json={'id': 'service-' + identifier})
            if path.endswith('/client-secret'):
                return httpx.Response(200, json={'value': 'never-expose-secret'})
            if path.endswith('/default-client-scopes'):
                return httpx.Response(200, json=[])
            if path.endswith('/scope-mappings/realm'):
                return httpx.Response(204)
            if request.method == 'PUT':
                self.clients[identifier] = payload
                return httpx.Response(204)
            if request.method == 'DELETE':
                if self.delete_fault == 'unavailable':
                    return httpx.Response(500, json={'secret': 'never-expose-secret'})
                del self.clients[identifier]
                if self.delete_fault == 'lost':
                    raise httpx.ReadTimeout('never-expose-secret', request=request)
                if self.delete_fault == 'concurrent':
                    return httpx.Response(404)
                return httpx.Response(204)
            return httpx.Response(200, json=self.clients[identifier])
        raise AssertionError((request.method, path))

    async def create_access(self, **overrides):
        return await self.http.post('/api/external-access', headers=self.headers('owner-a'),
            json={'name': 'Поставщик', 'days': 30, 'request_id': str(uuid.uuid4()), **overrides})

    async def test_owner_issue_list_duplicate_and_revoke(self):
        request_id = str(uuid.uuid4())
        response = await self.create_access(request_id=request_id)
        self.assertEqual(response.status_code, 201, response.text)
        issued = response.json()
        self.assertEqual(issued['expires_in'], 30 * 86400)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        actor = await self.app.state.authenticator.authenticate(issued['access_token'])
        self.assertTrue(actor.is_external)
        self.assertFalse(actor.is_owner)
        listing = await self.http.get('/api/external-access', headers=self.headers('owner-a'))
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()['total'], 1)
        self.assertNotIn(issued['access_token'], listing.text)
        self.assertNotIn('never-expose-secret', listing.text)
        repeated = await self.create_access(request_id=request_id)
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(repeated.json()['error']['code'], 'EXTERNAL_ACCESS_ALREADY_ISSUED')
        self.assertEqual(len(self.clients), 1)
        identifier = issued['account']['id']
        revoked = await self.http.delete('/api/external-access/' + identifier, headers=self.headers('owner-a'))
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()['account']['status'], 'revoked')
        denied = await self.http.get('/a2a/external/.well-known/agent-card.json', headers=self.headers(issued['access_token']))
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(len(self.clients), 1)

    async def test_replacement_preserves_identity_and_invalidates_old_token(self):
        first = (await self.create_access()).json()
        replaced = await self.http.post('/api/external-access/' + first['account']['id'] + '/token',
            headers=self.headers('owner-a'), json={'days': 1})
        self.assertEqual(replaced.status_code, 200, replaced.text)
        second = replaced.json()
        self.assertEqual(second['account']['id'], first['account']['id'])
        self.assertEqual(self.tokens[first['access_token']]['sub'], self.tokens[second['access_token']]['sub'])
        old = await self.http.get('/a2a/external/.well-known/agent-card.json', headers=self.headers(first['access_token']))
        new = await self.http.get('/a2a/external/.well-known/agent-card.json', headers=self.headers(second['access_token']))
        self.assertEqual(old.status_code, 401)
        self.assertEqual(new.status_code, 200)

    async def test_delete_retains_tasks_for_owners_without_reassigning_external_identity(self):
        first = (await self.create_access()).json()
        await self.submit(first['access_token'], 'initial', 'external-history')
        agent = self.app.state.core_agent
        admission = agent.tool_runtime.environment_manager.validate_workspace_scope.__self__
        tenant = self.app.state.authenticator.settings.tenant
        binding, _ = await admission.workspace_scope(tenant, 'external-history')
        workspace = agent.response_files_service.workspaces.workspace(binding)
        (workspace / 'result.txt').write_bytes(b'retained result')
        agent.model = ScriptedModel([
            ModelResponse(tool_requests=(ToolRequest('publish', 'core_response_files', {'paths': ['result.txt']}),)),
            ModelResponse(message='File ready'),
        ])
        task = await self.submit(first['access_token'], 'before-deletion', 'external-history')
        record = agent.workflow_store.lookup_task(task['id'])
        file_id = record.result['outgoing_files'][0]['file_id']
        identifier = first['account']['id']
        response = await self.http.delete('/api/external-access/' + identifier + '/account',
            headers=self.headers('owner-b'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'deleted': True, 'account_id': identifier})
        deletion = next(call for call in self.admin_calls if call.method == 'DELETE')
        self.assertEqual(deletion.headers['authorization'], 'Bearer owner-b')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn(identifier, self.clients)
        listing = await self.http.get('/api/external-access', headers=self.headers('owner-a'))
        self.assertEqual(listing.json()['total'], 0)
        old = await self.http.get('/a2a/external/.well-known/agent-card.json',
            headers=self.headers(first['access_token']))
        self.assertEqual(old.status_code, 401)
        owner = await self.http.get('/a2a/owner/tasks/' + task['id'], headers=self.headers('owner-a'))
        self.assertEqual(owner.status_code, 200, owner.text)
        file = await self.http.get(f"/api/chats/external-history/tasks/{task['id']}/files/{file_id}",
            headers=self.headers('owner-b'))
        self.assertEqual(file.status_code, 200, file.text)
        self.assertEqual(file.content, b'retained result')
        self.assertEqual(agent.workflow_store.lookup_task(task['id']), record)
        second = (await self.create_access()).json()
        self.assertNotEqual(self.tokens[first['access_token']]['sub'], self.tokens[second['access_token']]['sub'])
        stranger = await self.http.get('/a2a/external/tasks/' + task['id'],
            headers=self.headers(second['access_token']))
        self.assertEqual(stranger.status_code, 404, stranger.text)
        missing = await self.http.delete('/api/external-access/' + identifier + '/account',
            headers=self.headers('owner-a'))
        self.assertEqual(missing.status_code, 404)

    async def test_delete_validates_scope_authority_and_input_before_native_mutation(self):
        issued = (await self.create_access()).json()
        identifier = issued['account']['id']
        path = '/api/external-access/' + identifier + '/account'
        for token, url, content, expected in [
            ('external-a', path, None, 403),
            ('owner-a', path + '?unexpected=1', None, 400),
            ('owner-a', path, '{}', 400),
            ('owner-a', '/api/external-access/not-a-uuid/account', None, 404),
        ]:
            response = await self.http.request('DELETE', url, headers=self.headers(token), content=content)
            self.assertEqual(response.status_code, expected, response.text)
        client = self.clients[identifier]
        client['attributes']['agent.access.tenant'] = 'another-company'
        self.assertEqual((await self.http.delete(path, headers=self.headers('owner-a'))).status_code, 404)
        client['attributes']['agent.access.tenant'] = self.app.state.authenticator.settings.tenant
        self.role_names.append('agent-owner')
        self.assertEqual((await self.http.delete(path, headers=self.headers('owner-a'))).status_code, 404)
        self.assertEqual((await self.http.get('/api/external-access', headers=self.headers('owner-a'))).json()['total'], 0)
        self.assertFalse(any(call.method == 'DELETE' for call in self.admin_calls))
        self.role_names = []
        client['attributes']['agent.access.state'] = 'pending'
        self.assertEqual((await self.http.delete(path, headers=self.headers('owner-a'))).status_code, 200)

    async def test_delete_upstream_errors_do_not_confirm_or_expose_credentials(self):
        issued = (await self.create_access()).json()
        path = '/api/external-access/' + issued['account']['id'] + '/account'
        for upstream, expected in [(403, 403), (500, 503)]:
            self.admin_status = upstream
            response = await self.http.delete(path, headers=self.headers('owner-a'))
            self.assertEqual(response.status_code, expected, response.text)
            self.assertNotIn('never-expose-secret', response.text)
            self.assertNotIn('owner-a', response.text)
            self.assertIn(issued['account']['id'], self.clients)
        self.admin_status = 200
        self.delete_fault = 'unavailable'
        response = await self.http.delete(path, headers=self.headers('owner-a'))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIn(issued['account']['id'], self.clients)
        before = sum(call.method == 'DELETE' for call in self.admin_calls)
        self.delete_fault = 'lost'
        response = await self.http.delete(path, headers=self.headers('owner-a'))
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn('never-expose-secret', response.text)
        self.assertNotIn('deleted', response.text)
        listing = await self.http.get('/api/external-access', headers=self.headers('owner-a'))
        self.assertEqual(listing.json()['total'], 0)
        self.assertEqual(sum(call.method == 'DELETE' for call in self.admin_calls), before + 1)
        concurrent = (await self.create_access()).json()
        self.delete_fault = 'concurrent'
        response = await self.http.delete('/api/external-access/' + concurrent['account']['id'] + '/account',
            headers=self.headers('owner-a'))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['deleted'])

    async def test_external_and_invalid_requests_never_reach_admin(self):
        for method, path, payload in [('GET', '/api/external-access', None),
            ('POST', '/api/external-access', {'name': 'x', 'days': 30, 'request_id': str(uuid.uuid4())}),
            ('DELETE', '/api/external-access/' + str(uuid.uuid4()), None)]:
            result = await self.http.request(method, path, json=payload, headers=self.headers('external-a'))
            self.assertEqual(result.status_code, 403)
        for values in [{'days': True}, {'days': 0}, {'days': 366}, {'name': ''},
                       {'name': 'x' * 101}, {'request_id': '../elsewhere'}, {'name': 'x\n'}]:
            result = await self.create_access(**values)
            self.assertEqual(result.status_code, 400, result.text)
        self.assertEqual(self.admin_calls, [])

    async def test_keycloak_errors_are_safe_and_do_not_fallback_to_admin_credentials(self):
        for status, expected in [(403, 403), (401, 403), (500, 503)]:
            self.admin_status = status
            result = await self.create_access()
            self.assertEqual(result.status_code, expected, result.text)
            self.assertNotIn('never-expose-secret', result.text)
            self.assertNotIn('owner-a', result.text)
        self.assertFalse(self.clients)

    async def test_partial_create_resumes_without_duplicate_and_conflicting_input_is_rejected(self):
        request_id = str(uuid.uuid4())
        self.failure_path = '/roles/agent-external'
        failed = await self.create_access(request_id=request_id)
        self.assertEqual(failed.status_code, 503, failed.text)
        self.assertEqual(len(self.clients), 1)
        self.failure_path = ''
        conflict = await self.create_access(request_id=request_id, name='Другой')
        self.assertEqual(conflict.status_code, 409)
        retried = await self.create_access(request_id=request_id)
        self.assertEqual(retried.status_code, 201, retried.text)
        self.assertEqual(len(self.clients), 1)

    async def test_foreign_account_and_malformed_keycloak_reply_are_safe(self):
        granted = (await self.create_access()).json()
        identifier = granted['account']['id']
        self.clients[identifier]['attributes']['agent.access.tenant'] = 'another-company'
        result = await self.http.delete('/api/external-access/' + identifier, headers=self.headers('owner-a'))
        self.assertEqual(result.status_code, 404)
        listing = await self.http.get('/api/external-access', headers=self.headers('owner-a'))
        self.assertEqual(listing.json()['total'], 0)
        self.malformed = True
        result = await self.http.get('/api/external-access', headers=self.headers('owner-a'))
        self.assertEqual(result.status_code, 503, result.text)

    async def test_malformed_roles_and_post_creation_lookup_are_safe(self):
        granted = (await self.create_access()).json()
        client = self.clients[granted['account']['id']]
        client['attributes'] = {}
        self.role_names = [['unexpected']]
        result = await self.http.get('/api/external-access', headers=self.headers('owner-a'))
        self.assertEqual(result.status_code, 503, result.text)
        self.clients.clear()
        self.malformed_after_create = True
        result = await self.create_access()
        self.assertEqual(result.status_code, 503, result.text)
