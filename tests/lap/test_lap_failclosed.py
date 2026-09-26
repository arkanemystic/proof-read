"""Fail closed: worker down, timeout, malformed JSON, error results, missing decide marker."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proofread.contracts import FAILCLOSED_POLICY, Action
from proofread.verify.lap_client import LapVerifier

ACTS = [Action(episode_id="e", step=1, kind="write", path="/workspace/func.py", added_lines=["x = 1"]),
        Action(episode_id="e", step=2, kind="exec", cmd="ls")]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _serve(verify_body, compile_body=None, delay=0.0):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            if delay:
                time.sleep(delay)
            body = compile_body if self.path == "/compile-policy" else verify_body
            if body is None:
                body = json.dumps({"success": True, "error": None, "module_name": "PROOFREAD"})
            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _all_failclosed(vs):
    assert len(vs) == len(ACTS)
    for v in vs:
        assert not v.ok and v.failed_policies == [FAILCLOSED_POLICY] and v.source == "failclosed"
        assert v.detail.startswith("LAP:")


def test_worker_down():
    v = LapVerifier(url=f"http://127.0.0.1:{_free_port()}", timeout_s=2)
    _all_failclosed(asyncio.run(v.check_batch(ACTS)))


@pytest.mark.parametrize("body", [
    "not json",
    json.dumps([1, 2]),
    json.dumps({"result": "maybe", "trace": ""}),
    json.dumps({"result": "proved"}),  # trace missing
    json.dumps({"result": "error", "trace": "lean timed out after 30s"}),
    json.dumps({"result": "refuted", "trace": "type mismatch"}),  # refuted without decide marker
])
def test_malformed_or_undecided(body):
    srv, url = _serve(body)
    try:
        _all_failclosed(asyncio.run(LapVerifier(url=url, timeout_s=5).check_batch(ACTS)))
    finally:
        srv.shutdown()


def test_registration_failure():
    srv, url = _serve(json.dumps({"result": "proved", "trace": ""}),
                      compile_body=json.dumps({"success": False, "error": "boom", "module_name": None}))
    try:
        _all_failclosed(asyncio.run(LapVerifier(url=url, timeout_s=5).check_batch(ACTS)))
    finally:
        srv.shutdown()


def test_timeout():
    srv, url = _serve(json.dumps({"result": "proved", "trace": ""}), delay=2.0)
    try:
        v = LapVerifier(url=url, timeout_s=0.5, register=False)
        _all_failclosed(asyncio.run(v.check_batch(ACTS)))
    finally:
        srv.shutdown()


def test_refuted_without_policy_explanation():
    trace = "Tactic `decide` proved that the proposition\n x = true\nis false"
    srv, url = _serve(json.dumps({"result": "refuted", "trace": trace}))
    try:
        # every conjecture refuted -> all six policies "fail"; that is a decision, not fail closed
        vs = asyncio.run(LapVerifier(url=url, timeout_s=5).check_batch(ACTS[:1]))
        assert not vs[0].ok and len(vs[0].failed_policies) == 6
    finally:
        srv.shutdown()


def test_empty_batch():
    assert asyncio.run(LapVerifier(url="http://127.0.0.1:1").check_batch([])) == []
