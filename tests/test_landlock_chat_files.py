import os
import tempfile
import unittest
import uuid

from core_agent.chat_files import ChatFileService, MemoryChatFileStore
from core_agent.errors import CoreError
from core_agent.workflow import InMemoryWorkflowStore, WorkflowRecord
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding


class LandlockProtectedInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspaces = ChatWorkspaces(temporary.name)
        self.binding = WorkspaceBinding(str(uuid.uuid4()), 'owner', str(uuid.uuid4()))
        self.workspace = self.workspaces.workspace(self.binding)
        workflow = InMemoryWorkflowStore()
        self.record = workflow.create(WorkflowRecord(
            str(uuid.uuid4()), str(uuid.uuid4()), self.binding.context_id,
            self.binding.tenant_id, self.binding.owner_id, None, 'RUNNING', 1,
            {'prompt': 'files'}, {}))
        self.store = MemoryChatFileStore(workflow, lambda binding: None)
        self.normal = ChatFileService(self.store, self.workspaces)
        self.addCleanup(self.normal.close)
        self.restricted = ChatFileService(self.store, self.workspaces, protected_inputs_enabled=False)
        self.addCleanup(self.restricted.close)

    def prepare(self, service, **options):
        return service.prepare([{'name': 'input.txt', 'raw': b'original bytes'}],
            tenant_id=self.binding.tenant_id, actor_id='actor', message_id=str(uuid.uuid4()),
            request_digest='digest', **({'source': 'upload'} | options))

    def bind(self, service, batch):
        service.bind(batch['batch_id'], self.binding, task_id=self.record.task_id,
            run_id=self.record.run_id, actor_id=batch['actor_id'], message_id=batch['message_id'],
            request_digest=batch['request_digest'], lease_token=batch['lease_token'])

    def accept(self, service, batch):
        self.bind(service, batch)
        service.record_decision(batch['batch_id'], self.binding, decision_ref='allowed', allow=True)

    def assert_disabled(self, operation):
        with self.assertRaises(CoreError) as caught:
            operation()
        self.assertEqual(caught.exception.code, 'CAPABILITY_DISABLED')

    def test_rejected_owner_upload_never_stages_bytes_or_creates_batch(self):
        self.assert_disabled(lambda: self.prepare(self.restricted,
            request_metadata={'destination': {'directory': 'a2a'}}))
        self.assertEqual(self.store.rows, {})
        self.assertEqual(os.listdir(self.restricted.uploads), [])
        self.assertEqual(os.listdir(self.restricted.quarantine), [])
        self.assertFalse((self.workspace / 'attachments').exists())

    def test_default_profile_keeps_protected_uploads(self):
        batch = self.prepare(self.normal)
        self.accept(self.normal, batch)
        manifest = self.normal.publish(batch['batch_id'], self.binding)
        self.assertEqual((self.workspace / manifest['entries'][0]['relative_path']).read_bytes(), b'original bytes')

    def test_a2a_and_remote_destinations_still_publish(self):
        for options in ({'source': 'a2a'}, {'source': 'remote', 'destination_id': 'remote-operation'}):
            with self.subTest(options=options):
                batch = self.prepare(self.restricted, **options)
                self.accept(self.restricted, batch)
                manifest = self.restricted.publish(batch['batch_id'], self.binding)
                self.assertTrue(manifest['entries'][0]['relative_path'].startswith('a2a/'))
                self.assertEqual((self.workspace / manifest['entries'][0]['relative_path']).read_bytes(), b'original bytes')

    def test_legacy_staged_protected_batch_cannot_bind(self):
        batch = self.prepare(self.normal)
        before = self.store.get(batch['batch_id'], self.binding.tenant_id)
        self.assert_disabled(lambda: self.bind(self.restricted, batch))
        self.assertEqual(self.store.get(batch['batch_id'], self.binding.tenant_id), before)

    def test_legacy_accepted_batch_cannot_appear_in_live_writable_workspace(self):
        batch = self.prepare(self.normal)
        self.accept(self.normal, batch)
        before = self.store.get(batch['batch_id'], self.binding.tenant_id)
        self.assert_disabled(lambda: self.restricted.publish(batch['batch_id'], self.binding))
        self.assertEqual(self.store.get(batch['batch_id'], self.binding.tenant_id), before)
        self.assertEqual(list((self.workspace / 'attachments').iterdir()), [])
        material = self.normal.review_material(batch['batch_id'], self.binding,
            run_id=self.record.run_id, task_id=self.record.task_id)
        self.assertEqual(material['manifest']['entries'][0]['sha256'], before['manifest']['entries'][0]['sha256'])


if __name__ == '__main__':
    unittest.main()
