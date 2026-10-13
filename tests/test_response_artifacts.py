import copy
import importlib.util
import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from core_agent.artifacts import InMemoryArtifactStore
from core_agent.errors import CoreError
from core_agent.response_files import ResponseFileService
from core_agent.workspace import ChatWorkspaces, WorkspaceBinding


class ResponseArtifactTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('core_agent.response_artifacts'),
                             'Explicit typed response artifacts are not implemented')
        from core_agent.response_artifacts import ResponseArtifactService

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspaces = ChatWorkspaces(directory.name)
        self.binding = WorkspaceBinding('company', 'owner', 'chat')
        self.workspace = self.workspaces.workspace(self.binding)
        self.store = InMemoryArtifactStore()
        self.files = ResponseFileService(self.workspaces, self.store)
        self.service = ResponseArtifactService(self.files)
        self.options = {'task_id': 'task', 'run_id': 'run', 'limit_bytes': 25_000_000}
        self.publication = 0

    def prepare(self, artifacts, **options):
        self.publication += 1
        publication_id = options.pop('publication_id', f'call-{self.publication}')
        return self.service.prepare(self.binding, artifacts, publication_id=publication_id,
                                    **{**self.options, **options})

    def load(self, manifests, **options):
        return self.service.load(self.binding, manifests, **{**self.options, **options})

    def assert_code(self, code, callback):
        with self.assertRaises(CoreError) as error:
            callback()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_multiple_typed_artifacts_freeze_exact_contents_and_replay_without_workspace(self):
        (self.workspace / 'report.pptx').write_bytes(b'\0zip\xff')
        data = {'z': [True, None, '\u0434\u0430'], 'a': 2}
        inputs = [{'name': 'Report', 'parts': [
            {'type': 'text', 'text': '\u03b1\tline\n', 'media_type': 'text/markdown'},
            {'type': 'data', 'data': data},
            {'type': 'file', 'path': 'report.pptx'},
        ]}, {'parts': [{'type': 'text', 'text': 'Second'}]}]
        manifests = self.prepare(inputs)
        self.assertEqual(len(manifests), 2)
        self.assertEqual(set(manifests[0]), {'schema_version', 'artifact_id', 'name', 'parts'})
        self.assertEqual(manifests[1]['name'], '')
        self.assertEqual([part['type'] for part in manifests[0]['parts']], ['text', 'data', 'file'])
        self.assertEqual(len({item['artifact_id'] for item in manifests}), 2)
        for item in manifests:
            self.assertEqual(uuid.UUID(item['artifact_id']).version, 5)
        data['z'].append('changed')
        (self.workspace / 'report.pptx').unlink()
        with patch.object(self.workspaces, 'open_file', side_effect=AssertionError('workspace replay')):
            original = self.load(manifests)
            replayed = self.load(json.loads(json.dumps(manifests)))
        self.assertEqual(original, replayed)
        self.assertEqual(original[0].name, 'Report')
        self.assertEqual([part.kind for part in original[0].parts], ['text', 'data', 'file'])
        self.assertEqual(original[0].parts[0].data, '\u03b1\tline\n')
        self.assertEqual(original[0].parts[0].media_type, 'text/markdown')
        self.assertEqual(original[0].parts[1].data, {'z': [True, None, '\u0434\u0430'], 'a': 2})
        self.assertEqual(original[0].parts[2].data['bytes'], b'\0zip\xff')
        self.assertEqual(original[0].parts[2].data['filename'], 'report.pptx')
        self.assertEqual(original[0].provenance['core_agent_artifact'],
                         {'version': 1, 'complete': True, 'immutable': True})

    def test_receipts_and_validation_errors_never_reflect_contents_private_refs_or_paths(self):
        manifests = self.prepare([{'parts': [{'type': 'text', 'text': 'private-inline-contents'},
                                             {'type': 'data', 'data': {'token': 'private-json-value'}}]}])
        receipts = self.service.receipts(manifests)
        serialized = json.dumps(receipts)
        for forbidden in ('private-inline-contents', 'private-json-value', 'blob_id', 'tenant_id', 'run_id', 'path'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(receipts[0]['artifact_id'], manifests[0]['artifact_id'])
        self.assertEqual(receipts[0]['parts'][0]['size_bytes'], len(b'private-inline-contents'))
        bad = self.assert_code('INVALID_ARTIFACT_INPUT', lambda: self.prepare([
            {'parts': [{'type': 'text', 'text': 'private-inline-contents', 'path': 'secret-path'}]}]))
        self.assertNotIn('private-inline-contents', str(bad))
        self.assertNotIn('secret-path', str(bad))

    def test_invalid_last_part_rejects_whole_batch_before_any_blob_write(self):
        deep = value = {}
        for _ in range(102):
            value['next'] = {}
            value = value['next']
        invalid = [
            {'type': 'data', 'data': {'nested': [float('nan')]}},
            {'type': 'data', 'data': {'nested': float('inf')}},
            {'type': 'data', 'data': deep},
            {'type': 'data', 'data': []},
            {'type': 'text', 'text': 'x', 'media_type': 'application/json'},
            {'type': 'file', 'path': 'missing', 'data': {}},
            {'type': 'other', 'text': 'x'},
        ]
        for part in invalid:
            with self.subTest(part=part), patch.object(self.store, 'put', wraps=self.store.put) as put:
                self.assert_code('INVALID_ARTIFACT_INPUT', lambda: self.prepare([
                    {'parts': [{'type': 'text', 'text': 'valid'}]}, {'parts': [part]}]))
                put.assert_not_called()
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.assert_code('INVALID_FILE_PATH', lambda: self.prepare([
                {'parts': [{'type': 'text', 'text': 'valid'}, {'type': 'file', 'path': '../private'}]}]))
            put.assert_not_called()

    def test_task_aggregate_counts_utf8_json_and_file_bytes_and_pins_first_limit(self):
        (self.workspace / 'one.bin').write_bytes(b'123')
        first = self.prepare([{'parts': [{'type': 'text', 'text': '\u03b1'}]}], limit_bytes=8)
        second = self.prepare([{'parts': [{'type': 'data', 'data': {}},
                                          {'type': 'file', 'path': 'one.bin'}]}],
                              limit_bytes=1, published=first)
        self.assertEqual({part['ref']['limit_bytes'] for item in second for part in item['parts']}, {8})
        self.assertEqual(len(self.load(first + second, limit_bytes=8)), 2)
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            error = self.assert_code('ATTACHMENTS_TOO_LARGE', lambda: self.prepare([
                {'parts': [{'type': 'text', 'text': '\u03b1'}]}], published=first + second, limit_bytes=100))
            self.assertEqual(error.data, {'allowed_bytes': 8, 'actual_bytes': 9})
            put.assert_not_called()

    def test_counts_are_bounded_across_publications_even_for_empty_text(self):
        first = self.prepare([{'parts': [{'type': 'text', 'text': ''}]} for _ in range(64)])
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.assert_code('INVALID_ARTIFACT_INPUT', lambda: self.prepare([
                {'parts': [{'type': 'text', 'text': ''}]}], published=first))
            put.assert_not_called()
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.assert_code('INVALID_ARTIFACT_INPUT', lambda: self.prepare([
                {'parts': [{'type': 'text', 'text': ''} for _ in range(257)]}]))
            put.assert_not_called()

    def test_every_manifest_scope_is_checked_before_any_blob_lookup(self):
        manifests = self.prepare([{'parts': [{'type': 'text', 'text': 'one'}]},
                                  {'parts': [{'type': 'data', 'data': {'two': 2}}]}])
        for key in ('tenant_id', 'owner_id', 'context_id', 'task_id', 'run_id'):
            changed = copy.deepcopy(manifests)
            changed[-1]['parts'][0]['ref'][key] = 'foreign'
            with self.subTest(key=key), patch.object(self.store, 'get', wraps=self.store.get) as get:
                self.assert_code('ARTIFACT_INTEGRITY_FAILED', lambda: self.load(changed))
                get.assert_not_called()
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.assert_code('ARTIFACT_INTEGRITY_FAILED', lambda: self.service.prepare(
                replace(self.binding, owner_id='foreign'), [{'parts': [{'type': 'text', 'text': 'x'}]}],
                publication_id='call-2', published=manifests, **self.options))
            put.assert_not_called()

    def test_corrupt_last_blob_or_manifest_cannot_return_a_partial_result(self):
        manifests = self.prepare([{'parts': [{'type': 'text', 'text': 'one'}]},
                                  {'parts': [{'type': 'data', 'data': {'two': 2}}]}])
        bad = copy.deepcopy(manifests)
        bad[-1]['parts'][0]['ref']['size_bytes'] = -1
        with patch.object(self.store, 'get', wraps=self.store.get) as get:
            self.assert_code('ARTIFACT_INTEGRITY_FAILED', lambda: self.load(bad))
            get.assert_not_called()
        self.store._content[(self.binding.tenant_id, manifests[-1]['parts'][0]['ref']['blob_id'])] = b'bad'
        self.assert_code('ARTIFACT_INTEGRITY_FAILED', lambda: self.load(manifests))

    def test_publication_ids_are_stable_distinct_and_scope_bound(self):
        inputs = [{'parts': [{'type': 'text', 'text': 'same'}]}]
        first = self.prepare(inputs, publication_id='replay')
        replay = self.prepare(inputs, publication_id='replay')
        other = self.service.prepare(self.binding, inputs, publication_id='call-2', **self.options)
        another_run = self.prepare(inputs, run_id='another')
        self.assertEqual(first[0]['artifact_id'], replay[0]['artifact_id'])
        self.assertNotEqual(first[0]['artifact_id'], other[0]['artifact_id'])
        self.assertNotEqual(first[0]['artifact_id'], another_run[0]['artifact_id'])

    def test_json_that_native_transport_cannot_preserve_never_creates_snapshots(self):
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.assert_code('INVALID_ARTIFACT_INPUT', lambda: self.prepare([
                {'parts': [{'type': 'text', 'text': 'valid'},
                           {'type': 'data', 'data': {'integer': 9007199254740993}}]}]))
            put.assert_not_called()

    def test_native_data_snapshot_counts_the_same_bytes_the_receiver_observes(self):
        from a2a.types import Artifact, Task, TaskState, TaskStatus
        from google.protobuf.json_format import MessageToDict
        from core_agent.database import public_artifact_parts
        from core_agent.remote_agents import _task_event
        from core_agent.remote_artifacts import new_remote_artifacts

        manifests = self.prepare([{'parts': [{'type': 'data', 'data': {'n': 1}}]}], limit_bytes=9)
        artifact = self.load(manifests)[0]
        native = Artifact(artifact_id=artifact.id, name=artifact.name, parts=public_artifact_parts(artifact),
                          metadata=artifact.provenance)
        wire = MessageToDict(Task(id='task', context_id='chat', status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
                                  artifacts=[native]))
        wire_bytes = json.dumps(wire['artifacts'][0]['parts'][0]['data'], sort_keys=True, ensure_ascii=False,
                                separators=(',', ':'), allow_nan=False).encode()
        self.assertEqual(manifests[0]['parts'][0]['ref']['size_bytes'], len(wire_bytes))
        event, receipts = new_remote_artifacts(_task_event({'task': wire}), {'received_artifacts': []}, 9)
        self.assertEqual(len(event.artifacts), 1)
        self.assertEqual(receipts[0]['size_bytes'], len(wire_bytes))
        with patch.object(self.store, 'put', wraps=self.store.put) as put:
            self.assert_code('ATTACHMENTS_TOO_LARGE', lambda: self.prepare([
                {'parts': [{'type': 'data', 'data': {'n': 1}}]}], limit_bytes=7))
            put.assert_not_called()


if __name__ == '__main__':
    unittest.main()
