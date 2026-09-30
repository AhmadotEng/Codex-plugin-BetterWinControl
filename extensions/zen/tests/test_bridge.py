"""Protocol and actual Windows local-pipe tests. No browser/add-on registration."""
from pathlib import Path
import ctypes
import hashlib
import importlib.util
import io
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "native"))
import bridge
import native_host


class BridgeTests(unittest.TestCase):
    def test_native_framing_unicode_and_limits(self):
        pipe = io.BytesIO()
        value = {"value":"مرحبا 👋"}
        bridge.write_native(pipe, value)
        pipe.seek(0)
        self.assertEqual(bridge.read_native(pipe), value)
        with self.assertRaises(ValueError):
            bridge.read_native(io.BytesIO(struct.pack("<I", bridge.MAX_MESSAGE + 1)))
        with self.assertRaises(ValueError):
            bridge.read_native(io.BytesIO(struct.pack("<I", 4) + b"{}"))

    def test_pipe_discovery_is_local_and_namespaced(self):
        bridge.validate_pipe(r"\\.\pipe\BetterWinControl-Zen-test")
        for invalid in [r"\\remote\pipe\BetterWinControl-Zen-test", r"\\.\pipe\other", r"\\.\pipe\BetterWinControl-Zen-test\sub"]:
            with self.assertRaises(ValueError):
                bridge.validate_pipe(invalid)

    def test_auth_domain_role_endpoint_separation(self):
        key = bytes(range(32))
        values = {bridge.proof(key, role, pipe, "a"*64, "b"*64) for role in ["client","server"] for pipe in ["pipe-one","pipe-two"]}
        self.assertEqual(len(values), 4)

    def test_authentication_roundtrip_and_wrong_key(self):
        for valid in [True, False]:
            left, right = socket.socketpair()
            left.settimeout(3); right.settimeout(3)
            server = bridge.JsonStream(left.makefile("rwb",buffering=0))
            client = bridge.JsonStream(right.makefile("rwb",buffering=0))
            failures=[]
            def worker():
                try: bridge.authenticate_server(server,b"a"*32,"pipe")
                except Exception as ex: failures.append(type(ex).__name__)
                finally: server.close(); left.close()
            t=threading.Thread(target=worker,daemon=True);t.start()
            try:
                if valid: bridge.authenticate_client(client,b"a"*32,"pipe")
                else:
                    with self.assertRaises((ValueError,EOFError)): bridge.authenticate_client(client,b"b"*32,"pipe")
                t.join(3); self.assertFalse(t.is_alive())
                self.assertEqual(bool(failures),not valid)
            finally: client.close();right.close()

    @unittest.skipUnless(os.name == "nt", "Windows native security")
    def test_current_user_dpapi(self):
        sid=bridge.current_user_sid()
        self.assertTrue(sid.startswith("S-1-5-"))
        secret=os.urandom(32)
        cipher=bridge.dpapi(secret)
        self.assertNotEqual(secret,cipher)
        self.assertEqual(bridge.dpapi(cipher,decrypt=True),secret)

    @unittest.skipUnless(os.name == "nt", "Windows local pipe")
    def test_actual_native_host_and_broker(self):
        name=r"\\.\pipe\BetterWinControl-Zen-test-"+uuid.uuid4().hex
        secret=os.urandom(32)
        directory=ROOT/"tests"/"results"/("fixture-"+uuid.uuid4().hex)
        config=directory/"native.json"
        bridge.write_config(config,name,secret)
        self.assertEqual(bridge.read_config(config),(name,secret))
        proc=subprocess.Popen([sys.executable,"-u",str(ROOT/"native"/"native_host.py")],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            env={**os.environ,"BWC_ZEN_CONFIG":str(config)},creationflags=subprocess.CREATE_NO_WINDOW)
        connection=None;messages=queue.Queue();errors=[]
        def read_output():
            try:
                while True:messages.put(bridge.read_native(proc.stdout))
            except EOFError:pass
            except Exception as ex:errors.append(repr(ex))
        reader=threading.Thread(target=read_output,daemon=True);reader.start()
        try:
            bridge.write_native(proc.stdin,{"v":1,"type":"hello","extensionId":bridge.EXTENSION_ID,"version":"test"})
            self.assertEqual(messages.get(timeout=3),{"v":1,"type":"transport","state":"waiting"})
            time.sleep(.3);self.assertIsNone(proc.poll()) # Browser host predates broker.
            stale=None
            for epoch in range(1,4):
                accepted=[]
                server=bridge.BrokerServer(name,secret,lambda pid:{"verifiedTestProcess":True} if pid==proc.pid else None)
                def accept():
                    try:accepted.append(server.accept(timeout=3))
                    except Exception as ex:errors.append(repr(ex))
                waiter=threading.Thread(target=accept,daemon=True);waiter.start();waiter.join(4)
                self.assertFalse(waiter.is_alive());self.assertEqual(errors,[])
                connection=accepted[0]
                self.assertEqual(messages.get(timeout=3),{"v":1,"type":"transport","state":"connected"})
                self.assertEqual(connection.peer["clientPid"],proc.pid)
                result=[]
                def request():
                    try:result.append(connection.request("fixture-"+str(epoch),epoch,"capabilities",timeout=3))
                    except Exception as ex:errors.append(repr(ex))
                worker=threading.Thread(target=request,daemon=True);worker.start()
                current=messages.get(timeout=3)
                if stale:
                    bridge.write_native(proc.stdin,{"v":1,"type":"reply","id":stale["id"],"sessionId":stale["sessionId"],"epoch":stale["epoch"],"result":{"stale":True}})
                bridge.write_native(proc.stdin,{"v":1,"type":"reply","id":current["id"],"sessionId":current["sessionId"],"epoch":current["epoch"],"result":{"epoch":epoch}})
                worker.join(3);self.assertEqual(result,[{"epoch":epoch}]);self.assertEqual(errors,[])
                # A real in-flight request times out. Its later reply must not be
                # replayed into the next authenticated controller generation.
                timeout_errors=[]
                def unanswered():
                    try:connection.request("fixture-"+str(epoch),epoch,"observe",timeout=.1)
                    except Exception as ex:timeout_errors.append(ex)
                pending=threading.Thread(target=unanswered,daemon=True);pending.start()
                stale=messages.get(timeout=3);pending.join(3)
                self.assertIsInstance(timeout_errors[0],TimeoutError)
                self.assertEqual(messages.get(timeout=3),{"v":1,"type":"transport","state":"waiting"})
                self.assertIsNone(proc.poll())
                connection.close();connection=None
            proc.stdin.close();proc.wait(timeout=3);reader.join(1)
            self.assertEqual(proc.returncode,0);self.assertEqual(errors,[])
        finally:
            if proc.poll() is None:proc.kill();proc.wait(timeout=3)
            if connection:connection.close()
            for stream in [proc.stdin,proc.stdout,proc.stderr]:stream.close()
        # Only an encrypted fixture secret is retained under this test folder. No host is registered.
        self.assertNotIn(secret.hex(),config.read_text())

    def test_persistent_host_drops_unknown_old_and_duplicate_reply_ids(self):
        class Channel:
            def __init__(self):self.sent=[]
            def send(self,value):self.sent.append(value)
            def close(self):pass
        host=native_host.PersistentHost("fixture",b"s"*32,None,None)
        channel=Channel();host.channel=channel;host.generation=2
        host.pending={"new":(2,"new-session",3)}
        old={"v":1,"type":"reply","id":"old","sessionId":"old-session","epoch":1,"result":{"old":True}}
        current={"v":1,"type":"reply","id":"new","sessionId":"new-session","epoch":3,"result":{"current":True}}
        frames=[{"v":1,"type":"hello","extensionId":bridge.EXTENSION_ID},old,current,current,EOFError()]
        with mock.patch.object(native_host,"read_native",side_effect=frames):host.read_browser()
        self.assertEqual(channel.sent,[current])
        self.assertTrue(host.ended.is_set())

    @unittest.skipUnless(os.name=="nt","Windows local pipe")
    def test_closed_server_never_creates_a_listener(self):
        name=r"\\.\pipe\BetterWinControl-Zen-closed-"+uuid.uuid4().hex
        server=bridge.BrokerServer(name,b"s"*32,lambda pid:{"fixture":True})
        server.close()
        with self.assertRaisesRegex(EOFError,"server_closed"):server.accept(timeout=.1)
        with self.assertRaises(TimeoutError):bridge.connect_pipe(name,timeout=.1)

    @unittest.skipUnless(os.name=="nt","Windows local pipe")
    def test_close_during_stream_creation_releases_fixed_pipe(self):
        name=r"\\.\pipe\BetterWinControl-Zen-start-cancel-"+uuid.uuid4().hex
        server=bridge.BrokerServer(name,b"s"*32,lambda pid:{"fixture":True})
        original=bridge.WinPipeStream
        def stream(handle):
            result=original(handle);server.close();return result
        with mock.patch.object(bridge,"WinPipeStream",side_effect=stream):
            with self.assertRaisesRegex(EOFError,"server_closed"):server.accept(timeout=.1)
        replacement=bridge.BrokerServer(name,b"s"*32,lambda pid:{"fixture":True})
        with self.assertRaises(TimeoutError):replacement.accept(timeout=.05)

    @unittest.skipUnless(os.name=="nt","Windows local pipe")
    def test_immediate_cancel_and_reopen_three_times(self):
        name=r"\\.\pipe\BetterWinControl-Zen-rapid-"+uuid.uuid4().hex
        for _ in range(3):
            server=bridge.BrokerServer(name,b"s"*32,lambda pid:{"fixture":True})
            errors=[]
            def accept():
                try:server.accept(timeout=2)
                except Exception as ex:errors.append(ex)
            worker=threading.Thread(target=accept,daemon=True);worker.start()
            end=time.monotonic()+1
            while server.stream is None and time.monotonic()<end:time.sleep(.001)
            self.assertIsNotNone(server.stream)
            server.close()
            # Must create immediately, without waiting for/joining the old worker.
            replacement=bridge.BrokerServer(name,b"s"*32,lambda pid:{"fixture":True})
            with self.assertRaises(TimeoutError):replacement.accept(timeout=.02)
            worker.join(1);self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0],EOFError)

    @unittest.skipUnless(os.name == "nt", "Windows local pipe")
    def test_pipe_rejects_unauthorized_os_pid(self):
        name=r"\\.\pipe\BetterWinControl-Zen-deny-"+uuid.uuid4().hex
        observed=[];errors=[]
        def deny(pid):
            observed.append(pid)
            return None
        server=bridge.BrokerServer(name,b"s"*32,deny)
        def accept():
            try: server.accept(timeout=1)
            except Exception as ex: errors.append(ex)
        t=threading.Thread(target=accept,daemon=True);t.start()
        client=bridge.connect_pipe(name)
        try:
            with self.assertRaises(EOFError): client.read()
            t.join(2)
            self.assertFalse(t.is_alive())
            self.assertEqual(observed,[os.getpid()])
            self.assertIsInstance(errors[0],PermissionError)
        finally: client.close()

    @unittest.skipUnless(os.name == "nt", "Windows local pipe")
    def test_pipe_accept_deadline_cancels_wait(self):
        name=r"\\.\pipe\BetterWinControl-Zen-empty-"+uuid.uuid4().hex
        server=bridge.BrokerServer(name,b"s"*32,lambda pid:None)
        start=time.monotonic()
        with self.assertRaises(TimeoutError): server.accept(timeout=.1)
        self.assertLess(time.monotonic()-start,1)

    @unittest.skipUnless(os.name == "nt", "Windows local pipe")
    def test_auth_deadline_closes_silent_peer(self):
        name=r"\\.\pipe\BetterWinControl-Zen-silent-"+uuid.uuid4().hex
        accepted=[];errors=[]
        server=bridge.BrokerServer(name,b"s"*32,lambda pid:{"fixture":True})
        def accept():
            try: accepted.append(server.accept(timeout=1))
            except Exception as ex: errors.append(ex)
        t=threading.Thread(target=accept,daemon=True);t.start()
        client=bridge.connect_pipe(name)
        try:
            self.assertEqual(client.read()["type"],"auth_challenge")
            with self.assertRaises(EOFError):
                bridge.with_deadline(client,client.read,timeout=.1)
        finally:client.close()
        t.join(2);self.assertFalse(t.is_alive());self.assertEqual(accepted,[])

    def test_reproducible_xpi(self):
        spec=importlib.util.spec_from_file_location("build_xpi",ROOT/"scripts"/"build_xpi.py")
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        output=ROOT/"tests"/"results";output.mkdir(parents=True,exist_ok=True)
        first=module.build(output/"first.xpi");second=module.build(output/"second.xpi")
        self.assertEqual(first["sha256"],second["sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
