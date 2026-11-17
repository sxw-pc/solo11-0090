from __future__ import annotations
import http.client
import json
import time
import pytest
from pagedrift.mock import MockCore, Universe, start_mock_server, stop_mock_server
from pagedrift.transport import HttpTransport, InProcessTransport, NonLoopbackError
from helpers import make_records, write_schedule

def _core(tmp_path) -> MockCore:
    from pagedrift.mock import MutationSchedule
    path = write_schedule(tmp_path, "version: 1\nbefore_request: []\n", name="http-empty")
    return MockCore(Universe(make_records(8)), MutationSchedule.load(path))

def test_inprocess_and_http_return_identical_bytes(tmp_path) -> None:
    inproc_core = _core(tmp_path)
    http_core = _core(tmp_path)
    server, thread = start_mock_server(http_core, port=0)
    try:
        http = HttpTransport(f"http://127.0.0.1:{server.bound_port}")
        inprocess = InProcessTransport(inproc_core)
        query = {"offset": 2, "limit": 3}
        direct = inprocess.fetch(query)
        remote = http.fetch(query)
        assert direct.status == remote.status == 200
        assert direct.body == remote.body
        assert json.loads(direct.body)["items"] == json.loads(remote.body)["items"]
        assert inprocess.meta() == http.meta()
    finally:
        stop_mock_server(server, thread)

def test_http_transport_rejects_non_loopback_url() -> None:
    with pytest.raises(NonLoopbackError):
        HttpTransport("http://target.invalid/")
    with pytest.raises(NonLoopbackError):
        HttpTransport("http://public.invalid:8080/")

def test_server_refuses_non_loopback_bind(tmp_path) -> None:
    with pytest.raises(ValueError):
        start_mock_server(_core(tmp_path), host="0.0.0.0", port=0)

def test_non_loopback_host_header_is_rejected_and_audited(tmp_path) -> None:
    core = _core(tmp_path)
    server, thread = start_mock_server(core, port=0)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
        conn.putrequest("GET", "/meta", skip_host=True)
        conn.putheader("Host", "evil.invalid")
        conn.endheaders()
        response = conn.getresponse()
        body = response.read()
        conn.close()
        assert response.status == 403
        payload = json.loads(body)
        assert payload["error"]["code"] == "NON_LOOPBACK_REJECTED"
        assert core.audit_events
        assert core.audit_events[0]["type"] == "NON_LOOPBACK_REJECTED"
        assert core.audit_events[0]["host_header"] == "evil.invalid"
    finally:
        stop_mock_server(server, thread)

def test_loopback_host_header_with_port_is_accepted(tmp_path) -> None:
    core = _core(tmp_path)
    server, thread = start_mock_server(core, port=0)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=5)
        conn.putrequest("GET", "/meta", skip_host=True)
        conn.putheader("Host", f"localhost:{server.bound_port}")
        conn.endheaders()
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
        assert response.status == 200
        assert payload["count"] == 8
        time.sleep(0.01)
        assert core.audit_events == []
    finally:
        stop_mock_server(server, thread)
