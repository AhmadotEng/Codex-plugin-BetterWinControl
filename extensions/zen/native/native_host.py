"""Browser-owned persistent native messaging host; no service or startup registration."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import threading

from bridge import EXTENSION_ID, OPS, authenticate_client, connect_pipe, read_config, read_native, write_native, with_deadline


class PersistentHost:
    """One stdin reader for the browser lifetime, fresh authenticated broker generations."""
    def __init__(self,pipe,secret,browser_input,browser_output):
        self.pipe,self.secret=pipe,secret
        self.browser_input,self.browser_output=browser_input,browser_output
        self.lock=threading.RLock();self.output_lock=threading.Lock()
        self.ended=threading.Event();self.hello_ready=threading.Event()
        self.hello=None;self.channel=None;self.connecting=None
        self.generation=0;self.pending={}

    def emit(self,message):
        with self.output_lock:write_native(self.browser_output,message)

    def close(self):
        with self.lock:
            self.ended.set();self.hello_ready.set();self.generation+=1;self.pending.clear()
            active,connecting=self.channel,self.connecting
            self.channel=None;self.connecting=None
        for channel in [active,connecting]:
            if channel:
                try:channel.close()
                except OSError:pass

    def read_browser(self):
        """Never restart this reader on broker reconnection."""
        try:
            hello=read_native(self.browser_input)
            if hello.get("type")!="hello" or hello.get("v")!=1 or hello.get("extensionId")!=EXTENSION_ID:
                raise ValueError("extension_identity_required")
            with self.lock:self.hello=hello
            self.hello_ready.set()
            while not self.ended.is_set():
                message=read_native(self.browser_input)
                if message.get("v")!=1 or message.get("type") not in {"reply","event"}:
                    raise ValueError("invalid_extension_message")
                with self.lock:
                    channel=self.channel;generation=self.generation
                    if channel is None:continue
                    if message["type"]=="reply":
                        expected=self.pending.pop(message.get("id"),None)
                        if expected is None or expected[0]!=generation:continue
                        # Error replies have only the correlated ID. Normal replies
                        # must also carry the exact session and epoch.
                        if "error" not in message and (message.get("sessionId"),message.get("epoch"))!=expected[1:]:continue
                    elif message.get("event") not in {"window_closed","permissions_revoked","user_stop"}:
                        continue
                # Captured channel is never replaced by a later generation. A late
                # reply can fail on its old pipe but cannot reach a new controller.
                try:channel.send(message)
                except (OSError,EOFError,ValueError):pass
        except (EOFError,OSError,ValueError):pass
        finally:self.close()

    def run(self):
        self.emit({"v":1,"type":"transport","state":"waiting"})
        reader=threading.Thread(target=self.read_browser,daemon=True)
        reader.start()
        self.hello_ready.wait()
        try:
            while not self.ended.is_set():
                channel=None;published=False
                try:
                    # No spin or process spawning while no controller is active.
                    channel=connect_pipe(self.pipe,timeout=.2)
                    with self.lock:
                        if self.ended.is_set():channel.close();break
                        self.connecting=channel
                    with_deadline(channel,lambda:authenticate_client(channel,self.secret,self.pipe))
                    with self.lock:
                        if self.ended.is_set():channel.close();break
                        self.generation+=1;self.pending.clear()
                        self.channel=channel;self.connecting=None;published=True
                    self.emit({"v":1,"type":"transport","state":"connected"})
                    channel.send(self.hello)
                    while not self.ended.is_set():
                        request=channel.read()
                        if request.get("v")!=1 or request.get("op") not in OPS or not isinstance(request.get("id"),str):
                            raise ValueError("invalid_broker_command")
                        with self.lock:
                            if self.channel is not channel or self.ended.is_set():break
                            if len(self.pending)>=128:raise ValueError("too_many_pending_requests")
                            self.pending[request["id"]]=(self.generation,request.get("sessionId"),request.get("epoch"))
                        self.emit(request)
                except (EOFError,OSError,TimeoutError,ValueError):
                    pass
                finally:
                    with self.lock:
                        if self.channel is channel:
                            self.channel=None;self.generation+=1;self.pending.clear()
                        if self.connecting is channel:self.connecting=None
                    if channel:channel.close()
                    if published and not self.ended.is_set():
                        # Revoke extension state before any subsequent broker command.
                        self.emit({"v":1,"type":"transport","state":"waiting"})
                self.ended.wait(.15)
        finally:self.close()


def main():
    if os.name!="nt":raise RuntimeError("windows_required")
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(),os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(),os.O_BINARY)
    path=Path(os.environ.get("BWC_ZEN_CONFIG",str(Path(os.environ["LOCALAPPDATA"])/"BetterWinControl"/"zen"/"native.json")))
    pipe,secret=read_config(path)
    # Browser EOF wakes the sole reader and cancels authentication and active IO.
    browser_input=os.fdopen(os.dup(sys.stdin.fileno()),"rb",buffering=0)
    browser_output=os.fdopen(os.dup(sys.stdout.fileno()),"wb",buffering=0)
    PersistentHost(pipe,secret,browser_input,browser_output).run()


if __name__=="__main__":
    try:main()
    except EOFError:sys.exit(0)
    except Exception:
        sys.stderr.write("BetterWinControl native bridge closed. Check local broker configuration.\n")
        sys.exit(1)
