"""Run inside the Agent image. Uses fake model CLIs and a loopback fixture only."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


def check(provider):
    with tempfile.TemporaryDirectory(prefix='ati-container-smoke-') as directory:
        root = Path(directory)
        binary = root / 'bin';binary.mkdir()
        cli = binary / provider
        if provider == 'codex':
            script = '''import sys
from pathlib import Path
if sys.argv[1:]==['login','status']:raise SystemExit(0)
assert sys.argv[1]=='exec'
assert sys.argv[sys.argv.index('--sandbox')+1]=='read-only'
assert 'synthetic fixture prompt' in sys.stdin.read()
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{"fixture":"completed"}')
'''
        else:
            script = '''import json,sys
if sys.argv[1:]==['auth','status']:raise SystemExit(0)
assert '--safe-mode' in sys.argv and '--bare' not in sys.argv
assert sys.argv[sys.argv.index('--tools')+1]==''
assert '--strict-mcp-config' in sys.argv
message=json.load(sys.stdin)
assert 'synthetic fixture prompt' in message['message']['content'][0]['text']
print(json.dumps({'type':'result','subtype':'success','result':'{"fixture":"completed"}','is_error':False}))
'''
        cli.write_text('#!' + sys.executable + '\n' + script);cli.chmod(0o755)
        identity = root / 'identity';identity.mkdir()
        binding = {'installId':'synthetic-install','proof':'synthetic-proof','fingerprint':'synthetic-fingerprint',
                   'environmentId':'synthetic-environment','credential':'synthetic-credential'}
        (identity / 'installation.json').write_text(json.dumps(binding))
        (identity / 'trading').mkdir();grant=identity / 'trading/runtime-grant.json'
        grant.write_text('synthetic-unchanged');(identity / 'trading-enabled').touch()
        packet = {'prompt':'synthetic fixture prompt','variables':{}}
        task = {'taskId':'synthetic-task','attempt':1,'kind':'AI','leaseId':'synthetic-lease',
                'leaseExpiresAt':time.time()+60,'packet':packet,
                'packetHash':hashlib.sha256(json.dumps(packet,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        completed=threading.Event();paths=[];results=[];claimed=False
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal claimed
                paths.append(self.path)
                assert self.headers['Authorization']=='Bearer synthetic-credential'
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/heartbeat'):
                    assert set(payload['components'])=={'AGENT'};body={}
                elif self.path.endswith('/claim'):
                    assert payload['component']=='AGENT'
                    body={'task':None if claimed else task};claimed=True
                elif self.path.endswith('/complete'):
                    results.append(payload);completed.set();body={}
                else:
                    self.send_error(500);return
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        status=root/'status.json'
        env={**os.environ,'HOME':str(root/'home'),'ATI_ENVIRONMENT_DIR':str(identity),
             'ATI_AGENT_STATUS_FILE':str(status),'ATI_AGENT_PROVIDER':provider,
             'ATI_CLOUD_GATEWAY_URL':'http://127.0.0.1:'+str(server.server_port),
             'PATH':str(binary)+os.pathsep+os.environ['PATH']}
        process=subprocess.Popen([sys.executable,'/opt/ati/agent_container.py','run'],env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            if not completed.wait(20):
                detail=status.read_text() if status.is_file() else 'no status file'
                if process.poll() is not None:
                    stdout,stderr=process.communicate(timeout=2)
                    detail+=' '+repr((stdout,stderr))
                raise AssertionError('synthetic container task did not complete: '+detail+' paths='+repr(paths))
            assert results[0]['error'] is None,results
            assert results[0]['result']['json_payload']=={'fixture':'completed'},results
            assert results[0]['result']['model']=='user-'+provider,results
            assert json.loads(status.read_text())['state']=='online'
            assert grant.read_text()=='synthetic-unchanged'
            assert json.loads((identity/'installation.json').read_text())==binding
            assert not any('trading-grant' in path for path in paths)
            assert not Path('/var/run/docker.sock').exists()
            assert os.getuid()!=0
            try:
                Path('/opt/ati/should-not-write').write_text('x')
            except (OSError,PermissionError):pass
            else:raise AssertionError('image root must be read-only')
        finally:
            process.send_signal(signal.SIGTERM)
            stdout,stderr=process.communicate(timeout=10)
            server.shutdown();server.server_close();thread.join()
        assert process.returncode==0,(stdout,stderr)
        assert json.loads(status.read_text())['state']=='stopped'
        print('PASS in-container '+provider+': native Connector → lease guard → adapter → fake CLI → receipt; clean SIGTERM; shared binding and trading grant preserved')


if __name__=='__main__':
    check('codex')
    check('claude')
