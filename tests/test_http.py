import pytest
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import time
from utils.http import get_with_retries
import requests

class RetryMockServer(BaseHTTPRequestHandler):
    request_count = 0
    
    def do_GET(self):
        RetryMockServer.request_count += 1
        if RetryMockServer.request_count <= 2:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"Service Unavailable")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
    
    def log_message(self, format, *args):
        pass # Suppress logging

def test_get_with_retries_recovers_from_503():
    RetryMockServer.request_count = 0
    server = HTTPServer(('127.0.0.1', 0), RetryMockServer)
    port = server.server_port
    
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    
    try:
        response = get_with_retries(f"http://127.0.0.1:{port}", backoff_factor=0.01)
        assert response.status_code == 200
        assert response.text == "OK"
        assert RetryMockServer.request_count == 3
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=1)

def test_get_with_retries_max_retries_exceeded():
    class FailServer(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(502)
            self.end_headers()
            
        def log_message(self, format, *args):
            pass
            
    server = HTTPServer(('127.0.0.1', 0), FailServer)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever)
    t.daemon = True
    t.start()
    
    try:
        with pytest.raises(requests.exceptions.RetryError):
            get_with_retries(f"http://127.0.0.1:{port}", retries=2, backoff_factor=0.01)
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=1)
