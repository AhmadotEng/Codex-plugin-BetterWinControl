"""Controller-facing optional Zen adapter. Importing does not install anything."""
from __future__ import annotations
import ctypes
import contextvars
from contextlib import contextmanager
from ctypes import wintypes
import hashlib
import importlib.util
import math
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import uuid

import bridge

ROOT = Path(__file__).resolve().parents[1]
DOTNET_FILETIME_OFFSET = 504911232000000000
CONNECTION_WINDOW_SECONDS = 30
CONNECT_WAIT_SECONDS = 5


class AdapterError(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise AdapterError(code)


def canonical(path):
    return os.path.normcase(os.path.realpath(path))


def gecko_batch_command(launcher):
    """Exact shipped Gecko Windows .bat launch form, including native-host args.

    NativeMessaging supplies manifest path and extension ID; the subprocess worker
    prepends the launcher, quotes whitespace-containing args, then wraps for /s/c.
    This is deliberately not a general cmd.exe parser or arbitrary-tail allowance.
    """
    manifest = Path(launcher).with_name("local.betterwincontrol.zen.json")
    args = [str(launcher), str(manifest), bridge.EXTENSION_ID]
    require(all(not any(char in arg for char in '\"%\r\n\0') for arg in args), "native_launcher_path_not_supported")
    quoted = ['"' + arg + '"' if any(char.isspace() for char in arg) else arg for arg in args]
    return 'cmd.exe /s/c "' + ' '.join(quoted) + '"'


class WindowsIdentity:
    """Read-only OS identity. Never infer PID or native window from a title."""
    def window(self, hwnd):
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.IsWindow.argtypes = [wintypes.HWND]
        require(bool(user.IsWindow(hwnd)), "selected_window_gone")
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        thread = user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = wintypes.RECT()
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        require(bool(user.GetWindowRect(hwnd, ctypes.byref(rect))), "window_geometry_unavailable")
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetClassNameW(hwnd, name, len(name))
        user.GetDpiForWindow.argtypes = [wintypes.HWND]
        return {"hwnd":int(hwnd),"pid":pid.value,"threadId":thread,"className":name.value,
                "left":rect.left,"top":rect.top,"width":rect.right-rect.left,"height":rect.bottom-rect.top,
                "dpi":user.GetDpiForWindow(hwnd) or 96}

    def windows(self, pid):
        """Read-only native siblings, including covered windows; no focus changes."""
        user=ctypes.WinDLL("user32",use_last_error=True)
        callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        user.EnumWindows.argtypes=[callback_type,wintypes.LPARAM]
        user.IsWindowVisible.argtypes=[wintypes.HWND]
        user.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
        values=[];failures=[]
        def collect(hwnd,_):
            owner=wintypes.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
            if owner.value==pid and user.IsWindowVisible(hwnd):
                try:
                    value=self.window(hwnd)
                    if value["className"]=="MozillaWindowClass":values.append(value)
                except Exception as ex:failures.append(ex)
            return True
        require(bool(user.EnumWindows(callback_type(collect),0)),"native_windows_unavailable")
        require(not failures,"native_window_changed_during_mapping")
        return values

    def process(self, pid):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000,False,pid)
        require(bool(handle), "process_identity_unavailable")
        try:
            path=ctypes.create_unicode_buffer(32768);length=wintypes.DWORD(len(path))
            kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
            require(bool(kernel.QueryFullProcessImageNameW(handle,0,path,ctypes.byref(length))),"process_image_unavailable")
            created,exited,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
            kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
            require(bool(kernel.GetProcessTimes(handle,*[ctypes.byref(v) for v in [created,exited,kernel_time,user_time]])),"process_start_unavailable")
            start=((created.dwHighDateTime<<32)|created.dwLowDateTime)+DOTNET_FILETIME_OFFSET
            ntdll=ctypes.WinDLL("ntdll")
            ntdll.NtQueryInformationProcess.argtypes=[wintypes.HANDLE,ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong)]
            size=ctypes.c_ulong()
            ntdll.NtQueryInformationProcess(handle,60,None,0,ctypes.byref(size))
            require(0<size.value<=131072,"process_command_unavailable")
            data=ctypes.create_string_buffer(size.value)
            require(ntdll.NtQueryInformationProcess(handle,60,data,len(data),ctypes.byref(size))==0,"process_command_unavailable")
            class UnicodeString(ctypes.Structure):
                _fields_=[("length",wintypes.USHORT),("maximum",wintypes.USHORT),("buffer",ctypes.c_void_p)]
            value=UnicodeString.from_buffer(data)
            require(bool(value.buffer) and ctypes.addressof(data)<=value.buffer and value.buffer+value.length<=ctypes.addressof(data)+len(data),"process_command_invalid")
            command=ctypes.wstring_at(value.buffer,value.length//2)
        finally:kernel.CloseHandle(handle)
        class Entry(ctypes.Structure):
            _fields_=[("size",wintypes.DWORD),("usage",wintypes.DWORD),("pid",wintypes.DWORD),("heap",ctypes.c_size_t),("module",wintypes.DWORD),("threads",wintypes.DWORD),("parent",wintypes.DWORD),("priority",wintypes.LONG),("flags",wintypes.DWORD),("exe",wintypes.WCHAR*260)]
        kernel.CreateToolhelp32Snapshot.argtypes=[wintypes.DWORD,wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype=wintypes.HANDLE
        snapshot=kernel.CreateToolhelp32Snapshot(2,0)
        require(snapshot!=ctypes.c_void_p(-1).value,"process_parent_unavailable")
        parent=None
        try:
            entry=Entry();entry.size=ctypes.sizeof(entry)
            for name in ["Process32FirstW","Process32NextW"]:getattr(kernel,name).argtypes=[wintypes.HANDLE,ctypes.POINTER(Entry)]
            more=kernel.Process32FirstW(snapshot,ctypes.byref(entry))
            while more:
                if entry.pid==pid:parent=entry.parent;break
                more=kernel.Process32NextW(snapshot,ctypes.byref(entry))
        finally:kernel.CloseHandle(snapshot)
        require(parent is not None,"process_parent_unavailable")
        return {"pid":pid,"parentPid":parent,"path":path.value,"startTicks":start,"commandLine":command}

    def argv(self, command):
        shell=ctypes.WinDLL("shell32",use_last_error=True)
        shell.CommandLineToArgvW.argtypes=[wintypes.LPCWSTR,ctypes.POINTER(ctypes.c_int)]
        shell.CommandLineToArgvW.restype=ctypes.POINTER(wintypes.LPWSTR)
        count=ctypes.c_int();values=shell.CommandLineToArgvW(command,ctypes.byref(count))
        require(bool(values),"invalid_process_command")
        try:return [values[i] for i in range(count.value)]
        finally:
            kernel=ctypes.WinDLL("kernel32");kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree(values)


class CoreAdapter:
    def __init__(self, repo_root: Path, read_core_state, *, _identity=None, trace=None):
        self.repo_root=Path(repo_root).resolve()
        self.directory=self.repo_root/"runtime"/"zen-bridge"
        self.config=self.directory/"config.json"
        self.launcher=self.directory/"native-host"/"betterwincontrol-zen-native.bat"
        self.read_core_state=read_core_state
        self.trace=trace
        self.identity=_identity or WindowsIdentity()
        self.python=canonical(sys.executable)
        self.host=canonical(ROOT/"native"/"native_host.py")
        self.lock=threading.RLock()
        self.generation=0
        self.connection=None;self.server=None;self.error=None;self.connection_deadline=None
        self.connection_ready=threading.Event()
        self.snapshot=None;self.session_id=None;self.bound=False;self.pairs=[]

    def _trace(self, name, **metadata):
        bridge.trace_event(self.trace, name, **metadata)

    @contextmanager
    def _stage(self, name, **metadata):
        # Preserve application exceptions and results even if a diagnostic sink fails.
        context=None
        if self.trace is not None:
            try:
                candidate=self.trace.span(name, **metadata)
                candidate.__enter__()
                context=candidate
            except Exception:pass
        try:
            yield
        except BaseException as exc:
            if context is not None:
                try:context.__exit__(type(exc),exc,exc.__traceback__)
                except Exception:pass
            raise
        else:
            if context is not None:
                try:context.__exit__(None,None,None)
                except Exception:pass

    def _request(self, connection, session_id, epoch, operation, args, **options):
        # This span includes pipe/native-host/browser execution together. It does
        # not pretend to know extension-internal timing without extension telemetry.
        with self._stage("zen.roundtrip", operation=operation, sessionId=session_id, epoch=epoch, arguments=args):
            result=connection.request(session_id,epoch,operation,args,**options)
            self._trace("zen.response",operation=operation,sessionId=session_id,epoch=epoch,result=result)
            return result

    def cancel(self):
        with self.lock:
            revoked_session,revoked_generation=self.session_id,self.generation
            self.generation+=1
            connection,server=self.connection,self.server
            self.connection=None;self.server=None;self.snapshot=None;self.bound=False;self.pairs=[];self.connection_deadline=None
            self.connection_ready.set()
        self._trace("zen.session.cancelled",sessionId=revoked_session,generation=revoked_generation,
                    nextGeneration=revoked_generation+1,hadConnection=connection is not None,hadServer=server is not None)
        if connection:connection.close()
        if server:server.close()

    def _state(self):
        with self._stage("browser.core_state"):
            state=self.read_core_state()
        require(isinstance(state,dict) and state.get("attached") and not state.get("paused"),"core_not_active")
        win=state.get("window") or {}
        require(all(isinstance(win.get(key),int) and win[key]>0 for key in ["hwnd","pid","startTicks","threadId"]),"core_target_identity_missing")
        require(isinstance(state.get("sessionEpoch"),int) and state["sessionEpoch"]>=0,"core_epoch_missing")
        require(win.get("className")=="MozillaWindowClass","selected_window_is_not_zen")
        with self._stage("browser.window_identity",hwnd=win["hwnd"]):
            actual=self.identity.window(win["hwnd"])
        require(all(actual[key]==win[key] for key in ["pid","threadId","className"]),"selected_window_identity_changed")
        with self._stage("browser.process_identity",pid=win["pid"]):
            process=self.identity.process(win["pid"])
        require(process["startTicks"]==win["startTicks"] and Path(process["path"]).name.lower()=="zen.exe","selected_browser_identity_changed")
        return {"epoch":state["sessionEpoch"],"window":dict(win),"actual":actual,"browser":process}

    def _check(self):
        try:
            state=self._state()
            with self.lock:
                saved=self.snapshot
                require(saved is not None,"adapter_not_connected")
                require(state["epoch"]==saved["epoch"] and all(state["window"][key]==saved["window"][key] for key in ["hwnd","pid","startTicks","threadId"]),"core_selection_changed")
        except Exception:
            self.cancel()
            raise
        return state

    def _authorize(self,pid):
        try:
            state=self._check();child=self.identity.process(pid)
            require(canonical(child["path"])==self.python,"native_host_image_mismatch")
            args=self.identity.argv(child["commandLine"])
            require(len(args)==3 and canonical(args[0])==self.python and args[1]=="-u" and canonical(args[2])==self.host,"native_host_command_mismatch")
            browser=state["browser"]
            require(child["startTicks"]>=browser["startTicks"],"native_host_ancestry_invalid")
            parent=self.identity.process(child["parentPid"])
            if parent["pid"]!=browser["pid"]:
                expected_cmd=canonical(Path(os.environ["SystemRoot"])/"System32"/"cmd.exe")
                require(canonical(parent["path"])==expected_cmd,"native_host_parent_mismatch")
                if parent["commandLine"] != gecko_batch_command(self.launcher):
                    cmd_args=self.identity.argv(parent["commandLine"])
                    # Retain only the previously supported explicit fixed-launcher
                    # form. Real Gecko's nested /s/c command must match exactly above,
                    # including the expected manifest and fixed extension ID.
                    require(len(cmd_args)>=3 and canonical(cmd_args[0])==expected_cmd,"native_launcher_mismatch")
                    tail=[];seen_c=False
                    for arg in cmd_args[1:]:
                        if not seen_c:
                            require(arg.lower() in ["/d","/s","/q","/c"],"native_launcher_command_mismatch")
                            seen_c=arg.lower()=="/c"
                        else:tail.append(arg)
                    require(seen_c and len(tail)==1 and canonical(tail[0].strip('"'))==canonical(self.launcher),"native_launcher_command_mismatch")
                require(browser["startTicks"]<=parent["startTicks"]<=child["startTicks"],"native_launcher_ancestry_invalid")
                parent=self.identity.process(parent["parentPid"])
            require(parent["pid"]==browser["pid"] and parent["startTicks"]==browser["startTicks"] and canonical(parent["path"])==canonical(browser["path"]),"native_host_browser_mismatch")
            self._check()
            return {"hostStartTicks":child["startTicks"],"browserPid":browser["pid"],"browserStartTicks":browser["startTicks"],"selectedHwnd":state["window"]["hwnd"]}
        except Exception as exc:
            self._trace("zen.authorization.rejected",candidatePid=pid,sessionId=self.session_id,
                        generation=self.generation,errorType=type(exc).__name__,error=str(exc))
            return None

    def _event(self,message,peer):
        self._trace("zen.event",message=message,peer=peer,sessionId=self.session_id,generation=self.generation)
        with self.lock:
            if message.get("event")=="pair_requested":
                self.pairs=[entry for entry in self.pairs if entry[0].get("windowId")!=message.get("windowId")]
                self.pairs.append((message,peer,time.monotonic()))
                self.pairs=self.pairs[-8:]
                return
        if message.get("event") in ["window_closed","permissions_revoked","user_stop"]:
            self.cancel()

    def _prepare(self):
        self.cancel()
        if self.config.exists():pipe,secret=bridge.read_config(self.config)
        else:
            suffix=hashlib.sha256((bridge.current_user_sid()+str(self.directory)).encode()).hexdigest()[:24]
            pipe=r"\\.\pipe\BetterWinControl-Zen-"+suffix
            bridge.write_config(self.config,pipe,secrets.token_bytes(32))
        spec=importlib.util.spec_from_file_location("bwc_zen_prepare",ROOT/"scripts"/"prepare_native_host.py")
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        staged=module.prepare(self.python,self.config,self.launcher.parent)
        return {"ok":True,"prepared":True,"registered":False,"extensionInstalled":False,"signing":"browser_policy","config":str(self.config),"manifest":staged["manifest"],"launcher":staged["launcher"]}

    def _waiting(self, status):
        """The browser-owned host reconnects automatically; no toolbar gesture."""
        remaining=max(0,math.ceil((self.connection_deadline or 0)-time.monotonic()))
        return {"ok":True,"connected":bool(self.connection and not self.connection.closed),"bound":False,
                "pending":True,"status":status,"connectionWindowSeconds":CONNECTION_WINDOW_SECONDS,
                "remainingSeconds":remaining,"retryAfterMilliseconds":500,
                "requiresUserAction":False,
                "requires":"The enabled companion connects automatically. Poll pair while pending; do not request a toolbar click or restart connect."}

    def _connect(self):
        try:state=self._state()
        except Exception:
            self.cancel()
            raise
        with self.lock:
            saved=self.snapshot
            same=saved is not None and state["epoch"]==saved["epoch"] and all(state["window"][key]==saved["window"][key] for key in ["hwnd","pid","startTicks","threadId"])
            if same and self.connection is not None and not self.connection.closed and self.bound:
                return {"ok":True,"connected":True,"bound":True,"pending":False,"status":"bound"}
            reuse=same and self.error is None and self.server is not None and self.connection_deadline is not None and time.monotonic()<self.connection_deadline and (self.connection is None or not self.connection.closed)
        if not reuse:
            self.cancel()
            require(self.config.is_file() and self.launcher.is_file(),"prepare_required")
            pipe,secret=bridge.read_config(self.config)
            with self.lock:
                generation=self.generation
                self.snapshot=state;self.session_id=uuid.uuid4().hex;self.error=None
                self.connection_deadline=time.monotonic()+CONNECTION_WINDOW_SECONDS
                self.connection_ready=threading.Event()
                server=bridge.BrokerServer(pipe,secret,self._authorize,trace=self.trace);self.server=server
                ready=self.connection_ready
            def on_event(message,peer):
                with self.lock:
                    if generation!=self.generation:
                        self._trace("zen.event.ignored",message=message,peer=peer,sessionId=self.session_id,
                                    generation=generation,currentGeneration=self.generation,reason="stale_generation")
                        return
                    self._event(message,peer)
            def accept():
                try:
                    with self._stage("zen.accept_authentication"):
                        connection=server.accept(on_event,timeout=CONNECTION_WINDOW_SECONDS)
                    with self.lock:
                        if generation!=self.generation:connection.close();return
                        self.connection=connection
                except Exception as ex:
                    with self.lock:
                        if generation==self.generation:
                            self.error="native_host_connection_timed_out" if isinstance(ex,TimeoutError) else "native_host_connection_failed"
                            self.connection_deadline=None
                finally:ready.set()
            context=contextvars.copy_context()
            threading.Thread(target=context.run,args=(accept,),daemon=True).start()
        deadline=time.monotonic()+CONNECT_WAIT_SECONDS
        with self.lock:
            generation=self.generation;ready=self.connection_ready
        with self._stage("zen.connection_wait"):
            ready.wait(CONNECT_WAIT_SECONDS)
        with self.lock:require(generation==self.generation,"session_cancelled")
        return self._pair(timeout=max(.05,deadline-time.monotonic()))

    @staticmethod
    def _geometry_matches(geometry,actual):
        scale=actual["dpi"]/96
        return any(all(abs(geometry[key]*factor-actual[key])<=max(16,16*scale) for key in ["left","top","width","height"]) for factor in {1,scale})

    def _unique_candidate(self,candidates,state):
        require(isinstance(candidates,list) and 0<len(candidates)<=256,"browser_windows_unavailable")
        now=int(time.time()*1000);ids=set();nonces=set()
        for item in candidates:
            require(isinstance(item,dict),"invalid_pairing_candidate")
            geometry=item.get("window") or {};ident=item.get("windowId");nonce=item.get("pairingNonce")
            require(isinstance(ident,int) and not isinstance(ident,bool) and ident>0 and ident not in ids,"invalid_pairing_candidate")
            require(isinstance(nonce,str) and 1<=len(nonce)<=128 and nonce not in nonces,"invalid_pairing_candidate")
            require(isinstance(item.get("expiresAt"),int) and now<item["expiresAt"]<=now+31000,"pairing_candidate_expired")
            require(geometry.get("id")==ident and geometry.get("type")=="normal" and geometry.get("incognito") is False,"invalid_pairing_candidate")
            require(all(isinstance(geometry.get(key),(int,float)) and not isinstance(geometry[key],bool) and math.isfinite(geometry[key]) for key in ["left","top","width","height"]),"invalid_pairing_candidate")
            require(geometry["width"]>0 and geometry["height"]>0,"invalid_pairing_candidate")
            ids.add(ident);nonces.add(nonce)
        natives=self.identity.windows(state["window"]["pid"])
        selected=[win for win in natives if win["hwnd"]==state["window"]["hwnd"]]
        require(len(selected)==1,"selected_window_not_visible")
        require(all(selected[0][key]==state["actual"][key] for key in ["pid","threadId","className","left","top","width","height","dpi"]),"window_geometry_changed_during_pairing")
        matches={item["windowId"]:[win["hwnd"] for win in natives if self._geometry_matches(item["window"],win)] for item in candidates}
        target=state["window"]["hwnd"]
        options=[item for item in candidates if target in matches[item["windowId"]]]
        require(len(options)==1 and matches[options[0]["windowId"]]==[target],"browser_window_mapping_ambiguous")
        return options[0]

    def _pair(self,timeout=5):
        state=self._check()
        with self.lock:
            connection=self.connection;generation=self.generation
            pending=self.error is None and self.server is not None and self.connection_deadline is not None and time.monotonic()<self.connection_deadline
            if connection is None:
                if pending:return self._waiting("waiting_for_native_host")
                if self.error is not None:raise AdapterError(self.error)
                if self.connection_deadline is not None:
                    self.error="native_host_connection_timed_out"
                    self.cancel()
                    raise AdapterError(self.error)
            require(connection is not None and not connection.closed,"native_host_not_connected")
            if self.bound:return {"ok":True,"connected":True,"bound":True,"pending":False,"status":"bound"}
        peer=connection.peer;win=state["window"]
        require(peer.get("browserPid")==win["pid"] and peer.get("browserStartTicks")==win["startTicks"] and peer.get("selectedHwnd")==win["hwnd"],"native_host_browser_mismatch")
        deadline=time.monotonic()+timeout
        reply=self._request(connection,self.session_id,state["epoch"],"pair_candidates",{},timeout=max(.05,deadline-time.monotonic()))
        require(isinstance(reply,dict),"invalid_pairing_candidates")
        candidates=reply.get("candidates")
        candidate=self._unique_candidate(candidates,state)
        before=self._check()
        require(all(before["actual"][key]==state["actual"][key] for key in ["left","top","width","height","dpi"]),"window_geometry_changed_during_pairing")
        require(self._unique_candidate(candidates,before)["windowId"]==candidate["windowId"],"browser_window_mapping_changed")
        try:
            result=self._request(connection,self.session_id,state["epoch"],"bind",{"windowId":candidate["windowId"],"pairingNonce":candidate["pairingNonce"]},timeout=max(.05,deadline-time.monotonic()))
            after=self._check()
            require(isinstance(result,dict) and result.get("bound") is True and result.get("windowId")==candidate["windowId"] and result.get("epoch")==state["epoch"],"binding_response_mismatch")
            require(all(after["actual"][key]==state["actual"][key] for key in ["left","top","width","height","dpi"]),"window_geometry_changed_during_pairing")
            checked=[{**item,"window":result.get("window")} if item["windowId"]==candidate["windowId"] else item for item in candidates]
            require(self._unique_candidate(checked,after)["windowId"]==candidate["windowId"],"browser_window_mapping_changed")
            with self.lock:
                require(generation==self.generation and self.connection is connection,"session_cancelled")
                self.bound=True;self.pairs=[];self.connection_deadline=None
        except Exception:
            self.cancel()
            raise
        return {"ok":True,"connected":True,"bound":True,"pending":False,"status":"bound","window":state["window"],"browser":result,"mapping":"unique_authenticated_window_geometry"}

    def call(self,operation,args=None):
        args={} if args is None else args
        try:
            require(isinstance(args,dict),"arguments_must_be_object")
            require(operation in {"prepare","connect","pair","capabilities","observe","input","verify","disconnect"},"unsupported_browser_operation")
            if operation in {"prepare","connect","pair","capabilities","disconnect"}:require(not args,"unexpected_arguments")
            if operation=="prepare":return self._prepare()
            if operation=="disconnect":self.cancel();return {"ok":True,"disconnected":True,"inFlightMayFinish":True}
            if operation=="connect":return self._connect()
            if operation=="pair":return self._pair()
            with self._stage("browser.preflight"):
                state=self._check()
            with self.lock:connection=self.connection;bound=self.bound
            require(connection is not None and not connection.closed,"native_host_not_connected")
            if operation!="capabilities":require(bound,"browser_pairing_required")
            result=self._request(connection,self.session_id,state["epoch"],operation,args)
            with self._stage("browser.postflight"):
                self._check()
            return {"ok":True,"backend":"zen-webextension-semantic","result":result}
        except bridge.AdapterResponseError as ex:
            return {"ok":False,"error":str(ex),"bound":self.bound,"requiresFreshObservation":True}
        except AdapterError as ex:
            if str(ex) in {"core_not_active","core_selection_changed","selected_window_identity_changed","selected_browser_identity_changed"}:self.cancel()
            return {"ok":False,"error":str(ex),"bound":self.bound,"connectionError":self.error,"nativeHostInstalledByThisAdapter":False}
        except Exception as exc:
            self._trace("zen.adapter.error",operation=operation,arguments=args,sessionId=self.session_id,
                        generation=self.generation,errorType=type(exc).__name__,error=str(exc))
            self.cancel()
            return {"ok":False,"error":"browser_adapter_failed_or_outcome_unknown","bound":False,"inFlightMayFinish":True}
