"""Optional PC-side Tianji relay. Both ends bind loopback; SSH carries traffic.

No browser, public listener, GUI window, Bilibili cookies or SSH password needed.
The narrow forwarding key cannot open a shell or arbitrary remote listeners.
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading
import time

import httpx


def serve(config_path):
    path=Path(config_path).resolve()
    config=json.loads(path.read_text(encoding='utf-8'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            if self.path!='/v1/chat/completions':self.send_error(404);return
            supplied=self.headers.get('Authorization','')
            if not hmac.compare_digest(supplied,'Bearer '+config['bridge_token']):self.send_error(401);return
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=256000:raise ValueError('payload_size')
                payload=json.loads(self.rfile.read(size))
                if payload.get('model') not in ('gpt-6.1-sol','gpt-5.6-sol'):raise ValueError('model')
                if not 1<=int(payload.get('max_tokens',0))<=1800:raise ValueError('max_tokens')
                with httpx.Client(timeout=httpx.Timeout(100,connect=15),trust_env=True,
                                  proxy=config.get('proxy') or None) as client:
                    result=client.post('https://yujianwudi.top/v1/chat/completions',
                        headers={'Authorization':'Bearer '+config['api_key']},json=payload)
                body=result.content
                (path.parent/'health.json').write_text(json.dumps({'at':time.time(),'upstream_http':result.status_code}),encoding='utf-8')
                self.send_response(result.status_code)
                self.send_header('Content-Type','application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except Exception as exc:
                error_type=type(exc).__name__
                (path.parent/'health.json').write_text(json.dumps({'at':time.time(),'error_type':error_type}),encoding='utf-8')
                body=json.dumps({'error':{'code':'bridge_'+error_type,'message':'Temporary relay failure'}}).encode()
                try:
                    self.send_response(502);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                except OSError:pass
    server=ThreadingHTTPServer(('127.0.0.1',31416),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    args=[config['ssh_exe'],'-NT','-i',str(path.parent/'forward_key'),
          '-o','IdentitiesOnly=yes','-o','StrictHostKeyChecking=yes',
          '-o','UserKnownHostsFile='+str(path.parent/'known_hosts'),
          '-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30','-o','ServerAliveCountMax=3',
          '-o','ConnectTimeout=15','-R','127.0.0.1:31415:127.0.0.1:31416',config['ssh_target']]
    failures=0
    while True:
        try:
            process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            process.wait()
        except OSError:pass
        failures=min(failures+1,6);time.sleep(min(120,5*2**failures))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    serve(parser.parse_args().config)
