"""Owner UI projects durable public output without treating artifacts as replies."""
import subprocess
import unittest
from pathlib import Path


class PublicOutputUITests(unittest.TestCase):
    def test_messages_and_artifact_rows_survive_refresh_without_duplicates(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
import assert from "node:assert/strict";
import { publicMessageText, publicHistoryItems, responseArtifacts } from "./ui/src/types.ts";
const message = (id, text) => ({messageId:id,role:"ROLE_AGENT",taskId:"task",contextId:"chat",parts:[{text}]});
const intermediate = message("task:message:call", "Working update");
const final = message("task:final", "Final reply");
const task = {id:"task",contextId:"chat",status:{state:"TASK_STATE_COMPLETED",message:final},
  history:[intermediate, final],artifacts:[{artifactId:"blob",parts:[{text:"Never a reply"}]}]};
const ref = {type:"data",file_id:"file",name:"part-1.json",media_type:"application/json",size_bytes:2,sha256:"a".repeat(64)};
const artifact = {artifact_id:"artifact",name:"Report",parts:[ref]};
const published = {id:"tool",task_id:"task",kind:"tool_result",status:"available",text:"{}",
  public_message:{message_id:intermediate.messageId,text:"Working update"},response_artifacts:[artifact]};
const terminal = {id:"final",task_id:"task",kind:"result",status:"available",text:"Final reply",
  public_message_id:final.messageId,response_artifacts:[artifact],outcome:{state:"COMPLETED"}};
const history = [terminal,published];
assert.equal(publicMessageText({...terminal,text:"Stale final text"},task),"Final reply");
assert.equal(publicHistoryItems([]).length,0);
const rows = publicHistoryItems(history);
assert.equal(rows.filter(i=>i.kind==="agent_message").length,1);
assert.equal(rows.flatMap(i=>i.response_artifacts??[]).length,1);
assert.equal(rows.find(i=>i.response_artifacts?.length).kind,"result");
assert.equal(rows.find(i=>i.response_artifacts?.length).outcome,undefined);
assert.equal(rows.filter(i=>i.kind==="tool_result").length,1);
assert.deepEqual(publicHistoryItems(history),rows);
const withheld = {...terminal,status:"rejected",text:"[Unavailable]"};
assert.equal(publicMessageText(withheld,task),"");
assert.equal(publicHistoryItems([{...published,status:"pending_guardrail"}]).length,1);
assert.equal(publicHistoryItems([{...published,status:"pending_guardrail"}])[0].response_artifacts,undefined);
assert.equal(publicMessageText({...terminal,text:"Reviewed text"},{...task,status:{state:"TASK_STATE_WORKING",message:{...final,metadata:{partial:true}}}}),"Reviewed text");
assert.deepEqual(responseArtifacts([{...artifact,parts:[{...ref,size_bytes:-1}]}]),[]);
assert.deepEqual(responseArtifacts([{...artifact,parts:[{...ref,type:"unknown"}]}]),[]);
assert.equal(responseArtifacts([artifact])[0].parts[0].type,"data");
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_interaction_and_reply_activity(self):
        root = Path(__file__).resolve().parents[1]
        script = r'''
import assert from "node:assert/strict";
import { matchingActionWait, awaitingModelReply } from "./ui/src/toolPresentation.ts";
const wait = {wait_id:"wait", source_id:"call", subject:{}, kind:"tool_approval"};
assert.equal(matchingActionWait("call",[wait]),wait);
assert.equal(matchingActionWait("other",[wait]),undefined);
assert.equal(matchingActionWait(undefined,[wait]),undefined);
assert.equal(matchingActionWait("call",[{...wait,outcome:{reason:"rejected"}}]),undefined);
for (const subject of [{tool_call_id:"call"},{call:{id:"call"}}, {source_id:"arguments:call"},{source_id:"result:call"}]) {
  const linked = {...wait,source_id:"unrelated",subject};
  assert.equal(matchingActionWait("call",[linked]),linked);
}
for (const state of ["TASK_STATE_WORKING","TASK_STATE_SUBMITTED"]) {
  assert.equal(awaitingModelReply({status:{state}},false),true);
  assert.equal(awaitingModelReply({status:{state}},true),false);
}
for (const state of ["TASK_STATE_COMPLETED","TASK_STATE_FAILED","TASK_STATE_CANCELED","TASK_STATE_REJECTED","TASK_STATE_INPUT_REQUIRED","TASK_STATE_AUTH_REQUIRED"]) {
  assert.equal(awaitingModelReply({status:{state}},false),false);
}
assert.equal(awaitingModelReply(undefined,false),false);
'''
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
