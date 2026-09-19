"""Contract checks for tool-free incremental knowledge extraction.

Run: python tests/knowledge_extraction_test.py (from the server/ directory)
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from knowledge_extractor import (
    CopilotKnowledgeExtractor, KnowledgeExtractionError, parse_extraction_output,
    parse_mind_map_output,
)
import webchat


class StubConfig:
    CHAT_API_TOKEN = ""
    MAX_ATTACHMENT_MB = 25
    MAX_ATTACHMENTS = 8
    SESSION_WATCHER_ENABLED = False
    ONEDRIVE_SYNC_ENABLED = False
    KNOWLEDGE_EXTRACTION_MAX_ITEMS = 12
    KNOWLEDGE_EXTRACTION_MAX_CHARS = 45000
    KNOWLEDGE_EXTRACTION_MAX_MESSAGES = 120

    def __init__(self, sessions_dir: str):
        self.SESSIONS_DIR = sessions_dir


class StubRunner:
    name = "copilot"

    def __init__(self, workdir: str):
        self.workdir = workdir


class StubExtractor:
    available = True
    provider_name = "stub"
    extractor_version = "knowledge-v2.5-stub-v1"
    map_extractor_version = "knowledge-map-v2.6-stub-v1"
    max_map_input_chars = 22000
    max_map_nodes = 48
    reason = ""

    def __init__(self):
        self.calls = []
        self.map_calls = []
        self.map_failures_remaining = 0
        self.bad_output = False
        self.empty_output = False
        self.block_extract = False
        self.extract_started = asyncio.Event()
        self.extract_release = asyncio.Event()

    async def extract(self, messages, existing_items):
        self.calls.append({"messages": messages, "existing": existing_items})
        if self.block_extract:
            self.extract_started.set()
            await self.extract_release.wait()
        if self.empty_output:
            return '{"items":[]}'
        if self.bad_output:
            return json.dumps({
                "items": [{
                    "type": "decision",
                    "title": "Bad evidence",
                    "bodyMarkdown": "Must not be stored.",
                    "evidenceMessageIds": ["outside-the-batch"],
                    "confidence": "high",
                    "project": "",
                    "labels": [],
                }]
            })
        return json.dumps({
            "items": [{
                "type": "decision",
                "title": "Use immutable packages",
                "bodyMarkdown": "Batch says: " + " | ".join(
                    message["text"] for message in messages
                ),
                "evidenceMessageIds": [message["id"] for message in messages],
                "confidence": "high",
                "project": "Copilot Bridge",
                "labels": ["Sync"],
            }]
        })

    async def extract_map(self, conversations):
        self.map_calls.append(conversations)
        first = conversations[0]["messages"][0]["messageId"]
        last = conversations[-1]["messages"][-1]["messageId"]
        if self.map_failures_remaining:
            self.map_failures_remaining -= 1
            return json.dumps({
                "title": "Invalid map",
                "summary": "This output must fail strict evidence validation.",
                "nodes": [{
                    "id": "invalid", "parentId": None, "label": "Invalid",
                    "summary": "References evidence outside the prepared history.",
                    "kind": "topic", "evidenceMessageIds": ["outside-the-batch"],
                }],
            })
        return json.dumps({
            "title": "Bridge knowledge map",
            "summary": "Decisions and practices found across conversation history.",
            "nodes": [
                {
                    "id": "bridge", "parentId": None, "label": "Copilot Bridge",
                    "summary": "The main product and architecture theme.",
                    "kind": "project", "evidenceMessageIds": [first],
                },
                {
                    "id": "sync", "parentId": "bridge", "label": "Immutable sync",
                    "summary": "Never overwrite peer event packages.",
                    "kind": "decision", "evidenceMessageIds": [last],
                },
            ],
        })

    def describe(self):
        return {
            "provider": self.provider_name,
            "available": self.available,
            "extractorVersion": self.extractor_version,
            "tools": "disabled",
        }


async def main() -> None:
    copilot = CopilotKnowledgeExtractor(StubConfig("unused"))
    args = copilot.build_args("prompt")
    assert "--available-tools=" in args
    assert "--no-custom-instructions" in args
    assert "--disable-builtin-mcps" in args
    assert copilot.max_input_chars == 12000
    assert not any(value in args for value in (
        "--allow-all", "--allow-all-tools", "--add-dir", "--attachment",
    ))
    copilot.exe = r"C:\Program Files\Copilot\copilot.bat"
    try:
        copilot._launcher()
        raise AssertionError("insecure batch launcher was accepted")
    except KnowledgeExtractionError as exc:
        assert "no secure sibling" in str(exc)
    with tempfile.TemporaryDirectory() as launcher_tmp:
        batch_path = Path(launcher_tmp) / "copilot.bat"
        powershell_path = batch_path.with_suffix(".ps1")
        batch_path.write_text("@echo off\n", encoding="ascii")
        powershell_path.write_text("exit 0\n", encoding="ascii")
        copilot.exe = str(batch_path)
        assert copilot._launcher()[-2:] == ["-File", str(powershell_path)]

    mind_map = parse_mind_map_output(json.dumps({
        "title": "Bridge knowledge",
        "summary": "Evidence-backed architecture knowledge.",
        "nodes": [
            {
                "id": "architecture", "parentId": None,
                "label": "Architecture", "summary": "Core design decisions.",
                "kind": "topic", "evidenceMessageIds": ["message-1"],
            },
            {
                "id": "sync", "parentId": "architecture",
                "label": "Immutable sync", "summary": "Never overwrite peer packages.",
                "kind": "decision", "evidenceMessageIds": ["message-1"],
            },
        ],
    }), {"message-1"})
    assert mind_map["nodes"][1]["parentId"] == "architecture"
    for invalid_map in (
        '{"title":"Map","summary":"Summary","nodes":[{"id":"a",'
        '"parentId":"missing","label":"A","summary":"A summary",'
        '"kind":"topic","evidenceMessageIds":["message-1"]}]}',
        '{"title":"Map","summary":"Summary","nodes":[{"id":"child",'
        '"parentId":"parent","label":"Child","summary":"Child summary",'
        '"kind":"topic","evidenceMessageIds":["message-1"]},{"id":"parent",'
        '"parentId":"missing","label":"Parent","summary":"Parent summary",'
        '"kind":"topic","evidenceMessageIds":["message-1"]}]}',
        '{"title":"Map","summary":"Summary","nodes":[{"id":"a",'
        '"parentId":null,"label":"A","summary":"A summary","kind":"topic",'
        '"evidenceMessageIds":["outside"]}]}',
    ):
        try:
            parse_mind_map_output(invalid_map, {"message-1"})
            raise AssertionError("invalid mind map output was accepted")
        except KnowledgeExtractionError:
            pass

    allowed = {"message-1"}
    assert parse_extraction_output(json.dumps({
        "items": [{
            "type": "fact", "title": "Fact", "bodyMarkdown": "Body",
            "evidenceMessageIds": ["message-1"], "confidence": "high",
            "project": "", "labels": [],
        }]
    }), allowed)[0]["title"] == "Fact"
    for invalid in (
        '{"items":[],"items":[]}',
        '{"items":[{"type":"fact","title":"Fact","bodyMarkdown":"Body",'
        '"evidenceMessageIds":["outside"],"confidence":"high","project":"",'
        '"labels":[]}]}',
        '{"items":[{"type":"fact","title":"Fact","bodyMarkdown":"Body",'
        '"evidenceMessageIds":["message-1"],"project":"","labels":[]}]}',
        '{"items":[{"type":"fact","title":"Fact","bodyMarkdown":"Body",'
        '"evidenceMessageIds":["message-1"],"confidence":"high","project":"",'
        '"labels":["123456789012345678901234567890123"]}]}',
    ):
        try:
            parse_extraction_output(invalid, allowed)
            raise AssertionError("invalid extractor output was accepted")
        except KnowledgeExtractionError:
            pass

    with tempfile.TemporaryDirectory() as tmp:
        workdir = os.path.join(tmp, "workspace")
        os.makedirs(workdir)
        extractor = StubExtractor()
        app = web.Application()
        webchat.setup_web_routes(
            app, StubConfig(os.path.join(tmp, "sessions")), StubRunner(workdir),
            knowledge_extractor=extractor,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = await (await client.post(
                "/api/sessions", json={"title": "Extract this"}
            )).json()
            store = app["session_store"]
            store.append_message(session["id"], "user", "Use immutable packages")
            store.append_message(session["id"], "assistant", "They avoid overwrites")
            store.update_organization(session["id"], {"project": "Copilot Bridge"})

            extractor.map_failures_remaining = 1
            map_response = await client.post(
                "/api/knowledge/map/generate",
                json={"focusConversationId": session["id"]},
            )
            assert map_response.status == 200
            generated_map = await map_response.json()
            assert generated_map["cached"] is False
            assert generated_map["map"]["nodes"][0]["evidence"]
            assert len(extractor.map_calls) == 2
            assert extractor.map_calls[0][0]["conversationId"] == session["id"]
            cached_map = await (await client.post(
                "/api/knowledge/map/generate",
                json={"focusConversationId": session["id"]},
            )).json()
            assert cached_map["cached"] is True
            assert len(extractor.map_calls) == 2
            loaded_map = await (await client.get("/api/knowledge/map")).json()
            assert loaded_map["map"]["title"] == "Bridge knowledge map"
            invalid_force = await client.post(
                "/api/knowledge/map/generate", json={"force": "false"}
            )
            assert invalid_force.status == 400
            assert (await invalid_force.json())["error"] == "force must be a boolean"
            assert len(extractor.map_calls) == 2
            invalid_focus = await client.post(
                "/api/knowledge/map/generate", json={"focusConversationId": 7}
            )
            assert invalid_focus.status == 400
            assert (await invalid_focus.json())["error"] == (
                "focusConversationId must be a string"
            )

            empty_session = await (await client.post(
                "/api/sessions", json={"title": "Empty focus"}
            )).json()
            empty_focus = await client.post(
                "/api/knowledge/map/generate",
                json={"focusConversationId": empty_session["id"]},
            )
            assert empty_focus.status == 400
            assert (await empty_focus.json())["error"] == (
                "focused conversation has no eligible messages"
            )
            assert len(extractor.map_calls) == 2

            cached_json = store._conn.execute(
                "SELECT map_json FROM knowledge_maps WHERE id='history'"
            ).fetchone()[0]
            extractor.map_failures_remaining = 2
            rejected_map = await client.post(
                "/api/knowledge/map/generate", json={"force": True}
            )
            assert rejected_map.status == 502
            assert "out-of-scope evidence" in (
                await rejected_map.json()
            )["error"]
            assert len(extractor.map_calls) == 4
            assert store._conn.execute(
                "SELECT map_json FROM knowledge_maps WHERE id='history'"
            ).fetchone()[0] == cached_json

            response = await client.post(
                f"/api/sessions/{session['id']}/knowledge/extract"
            )
            assert response.status == 200
            first = await response.json()
            assert first["cached"] is False
            assert first["processedMessageCount"] == 2
            assert first["remainingMessageCount"] == 0
            assert len(first["items"]) == 1
            item_id = first["items"][0]["id"]
            assert first["items"][0]["status"] == "draft"
            assert len(extractor.calls) == 1

            repeated = await (await client.post(
                f"/api/sessions/{session['id']}/knowledge/extract"
            )).json()
            assert repeated["cached"] is True
            assert repeated["noNewMessages"] is True
            assert len(extractor.calls) == 1

            store.append_message(session["id"], "user", "Never overwrite peer files")
            store.append_message(session["id"], "assistant", "Use one device namespace")
            incremental = await (await client.post(
                f"/api/sessions/{session['id']}/knowledge/extract"
            )).json()
            assert len(extractor.calls) == 2
            assert len(extractor.calls[1]["messages"]) == 2
            assert incremental["items"][0]["id"] == item_id
            assert incremental["items"][0]["versionNumber"] == 2
            assert len(incremental["items"][0]["versions"]) == 2
            assert store._conn.execute(
                "SELECT COUNT(*) FROM knowledge_extraction_runs"
            ).fetchone()[0] == 2

            other = await (await client.post(
                "/api/sessions", json={"title": "Same knowledge elsewhere"}
            )).json()
            store.append_message(other["id"], "user", "Use immutable packages here too")
            cross_session = await (await client.post(
                f"/api/sessions/{other['id']}/knowledge/extract"
            )).json()
            assert cross_session["items"][0]["id"] == item_id
            assert cross_session["items"][0]["versionNumber"] == 3
            assert len(store.list_knowledge_items()) == 1

            empty_session = await (await client.post(
                "/api/sessions", json={"title": "Nothing durable"}
            )).json()
            store.append_message(empty_session["id"], "user", "Hello")
            extractor.empty_output = True
            calls_before = len(extractor.calls)
            runs_before = store._conn.execute(
                "SELECT COUNT(*) FROM knowledge_extraction_runs"
            ).fetchone()[0]
            empty = await (await client.post(
                f"/api/sessions/{empty_session['id']}/knowledge/extract"
            )).json()
            assert empty["items"] == []
            assert len(extractor.calls) == calls_before + 1
            assert store._conn.execute(
                "SELECT COUNT(*) FROM knowledge_extraction_runs"
            ).fetchone()[0] == runs_before + 1
            empty_again = await (await client.post(
                f"/api/sessions/{empty_session['id']}/knowledge/extract"
            )).json()
            assert empty_again["noNewMessages"] is True
            assert len(extractor.calls) == calls_before + 1
            extractor.empty_output = False

            store.append_message(session["id"], "user", "A malicious new input")
            extractor.bad_output = True
            before_items = len(store.list_knowledge_items())
            before_runs = store._conn.execute(
                "SELECT COUNT(*) FROM knowledge_extraction_runs"
            ).fetchone()[0]
            rejected = await client.post(
                f"/api/sessions/{session['id']}/knowledge/extract"
            )
            assert rejected.status == 502
            assert len(store.list_knowledge_items()) == before_items
            assert store._conn.execute(
                "SELECT COUNT(*) FROM knowledge_extraction_runs"
            ).fetchone()[0] == before_runs
        finally:
            await client.close()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = os.path.join(tmp, "workspace")
        os.makedirs(workdir)
        extractor = StubExtractor()
        extractor.block_extract = True
        app = web.Application()
        webchat.setup_web_routes(
            app, StubConfig(os.path.join(tmp, "sessions")), StubRunner(workdir),
            knowledge_extractor=extractor,
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = await (await client.post(
                "/api/sessions", json={"title": "Background knowledge"}
            )).json()
            store = app["session_store"]
            store.append_message(session["id"], "user", "Persist task progress")
            store.append_message(session["id"], "assistant", "Use a SQLite job row")
            endpoint = f"/api/sessions/{session['id']}/knowledge/generate"

            started_response = await client.post(
                endpoint, json={"maxConversations": 24}
            )
            assert started_response.status == 202
            started = await started_response.json()
            assert started["accepted"] is True
            assert started["job"]["status"] == "queued"
            await asyncio.wait_for(extractor.extract_started.wait(), timeout=1)

            duplicate_response = await client.post(endpoint, json={})
            assert duplicate_response.status == 200
            duplicate = await duplicate_response.json()
            assert duplicate["accepted"] is False
            assert duplicate["job"]["status"] == "running"
            assert len(app["knowledge_generation_tasks"]) == 1

            progress = await (await client.get(
                f"/api/sessions/{session['id']}/knowledge/generation"
            )).json()
            assert progress["job"]["phase"] == "extracting"
            jobs = await (await client.get(
                "/api/knowledge/generation-jobs"
            )).json()
            assert jobs["jobs"][0]["conversationId"] == session["id"]
            assert jobs["jobs"][0]["conversationTitle"] == "Background knowledge"

            extractor.extract_release.set()
            completed = None
            for _ in range(100):
                completed = await (await client.get(
                    f"/api/sessions/{session['id']}/knowledge/generation"
                )).json()
                if completed["job"]["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert completed["job"]["status"] == "completed"
            assert completed["job"]["progressPercent"] == 100
            assert completed["job"]["generatedItemCount"] == 1
            assert completed["job"]["resultMapId"] == (
                "conversation-" + session["id"]
            )
            focused_map = await (await client.get(
                "/api/knowledge/map?conversationId=" + session["id"]
            )).json()
            assert focused_map["map"]["id"] == "conversation-" + session["id"]
            session_items = await (await client.get(
                f"/api/sessions/{session['id']}/knowledge"
            )).json()
            assert session_items["count"] == 1
        finally:
            extractor.extract_release.set()
            await client.close()

    with tempfile.TemporaryDirectory() as tmp:
        workdir = os.path.join(tmp, "workspace")
        os.makedirs(workdir)
        app = web.Application()
        webchat.setup_web_routes(
            app, StubConfig(os.path.join(tmp, "sessions")), StubRunner(workdir)
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            session = await (await client.post("/api/sessions", json={})).json()
            response = await client.post(
                f"/api/sessions/{session['id']}/knowledge/extract"
            )
            assert response.status == 503
        finally:
            await client.close()

    webapp = Path(_SERVER_DIR) / "webapp"
    product_js = (webapp / "v23.js").read_text(encoding="utf-8")
    assert "v25ExtractKnowledge" in product_js
    assert "v27GenerateConversationKnowledge" in product_js
    assert "v27-generate-knowledge" in product_js
    assert "v28KnowledgeJobs" in product_js
    assert "v28KnowledgeJobActive" in product_js
    assert "v28RefreshGenerateButtons" in product_js
    assert "v28LoadKnowledgeJobs" in product_js
    assert "v28OpenKnowledgeJob" in product_js
    assert "Generating…" in product_js
    assert "v23ShowView('knowledge', false)" in product_js
    assert "v26RenderKnowledgeMap(true)" in product_js
    assert "const request = ++v23State.knowledgeMapRequest" in product_js
    assert "if (request !== v23State.knowledgeMapRequest) return" in product_js
    assert product_js.count("v23State.knowledgeMapRequest += 1") == 2
    generate_start = product_js.index("async function v27GenerateConversationKnowledge")
    generate_end = product_js.index("function v23SettingSection", generate_start)
    generate_code = product_js[generate_start:generate_end]
    assert "'/api/chat'" not in generate_code
    assert "'/api/chat-sync'" not in generate_code
    assert "/knowledge/extract" not in generate_code
    assert "/api/knowledge/map/generate" not in generate_code
    assert "/knowledge/generate" in generate_code
    assert "maxConversations" in generate_code
    assert "/knowledge/extract" in product_js
    assert "v26GenerateKnowledgeMap" in product_js
    assert "/api/knowledge/map/generate" in product_js
    assert "/api/knowledge/generation-jobs" in product_js
    assert "/api/knowledge/map?conversationId=" in product_js
    assert "knowledgeMode: 'map'" in product_js
    assert "v26MapNode" in product_js
    assert "v26RevealSelectedMapNode" in product_js
    assert "viewport.scrollTo" in product_js
    assert "window.setTimeout" in product_js
    assert "toggle.disabled = searchActive" in product_js
    assert "window.addEventListener('resize'" in product_js
    assert "timeline.setAttribute('aria-hidden', 'true')" in product_js
    assert "item.messageId" in product_js
    assert "v23PreserveInitialView" in product_js
    assert "navigationRevision === v23State.navigationRevision" in product_js
    detail_start = product_js.index("async function v24RenderKnowledgeDetail")
    reuse_start = product_js.index(
        "const reuse = v23Action(v23T('在当前会话中引用'", detail_start
    )
    reuse_end = product_js.index("const remove =", reuse_start)
    reuse_code = product_js[reuse_start:reuse_end]
    assert "8000 - header.length - footer.length" in reuse_code
    assert "v23ShowView('conversation')" in reuse_code
    assert "input.dispatchEvent(new Event('input'))" in reuse_code
    assert "item.bodyMarkdown" in reuse_code
    assert "Continue based on the following personal knowledge" in reuse_code
    assert "newSession" not in reuse_code
    assert "send(" not in reuse_code

    print("ALL KNOWLEDGE EXTRACTION TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
