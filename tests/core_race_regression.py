"""Deterministic core races: actual MCP code and linked production C# lifecycle.

No controller UI, native DLL, user applications, physical input, or host registration.
The generated .NET harness replaces only external boundaries (helper/WinEvent/core),
and links the real NativeInputSession.cs unchanged to exercise its lifecycle logic.
"""
from __future__ import annotations
import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from xml.sax.saxutils import escape

ROOT=Path(__file__).resolve().parents[1]
RESULTS=ROOT/"tests"/"results"
checks=[]


def check(name,body):
    start=time.monotonic()
    evidence=body()
    checks.append({"name":name,"passed":True,"elapsedMs":round((time.monotonic()-start)*1000),"evidence":evidence})


def load_mcp():
    spec=importlib.util.spec_from_file_location("race_mcp",ROOT/"scripts"/"mcp_server.py")
    module=importlib.util.module_from_spec(spec)
    previous=sys.dont_write_bytecode;sys.dont_write_bytecode=True
    try:spec.loader.exec_module(module)
    finally:sys.dont_write_bytecode=previous
    atexit.unregister(module.NATIVE.stop)
    return module


class AdapterFixture:
    def __init__(self):self.calls=[];self.cancellations=0
    def call(self,operation,args):self.calls.append((operation,args));return {"ok":True,"fixture":True}
    def cancel(self):self.cancellations+=1


class ProcessFixture:
    def __init__(self,frames=()):self.stdout=iter(frames);self.waits=0
    def poll(self):return 0
    def wait(self,timeout=None):self.waits+=1;return 0


def test_queued_browser_stop(module):
    module.NATIVE=module.Native();adapter=AdapterFixture();module.BROWSER=adapter
    module.NATIVE.on_revoke=adapter.cancel
    entered=threading.Event();outcomes=[]
    original=module.browser_call
    def observed(operation,args,expected_generation=None):
        # This is reached only after tool_call's first generation check; the real
        # browser_call below must re-check after its operation lock becomes free.
        entered.set()
        return original(operation,args,expected_generation)
    module.browser_call=observed
    module.NATIVE.operation.acquire()
    worker=threading.Thread(target=lambda:outcomes.append(module.tool_call("browser",{"operation":"disconnect"},expected_generation=0)),daemon=True)
    try:
        worker.start();assert entered.wait(2),"Queued request did not reach operation lock"
        stop=module.NATIVE.stop()
        assert stop["controllerExited"] and module.NATIVE.generation==1
    finally:
        module.NATIVE.operation.release();module.browser_call=original
    worker.join(2);assert not worker.is_alive(),"Queued request deadlocked"
    assert outcomes and outcomes[0]["isError"],outcomes
    assert "request_cancelled_by_stop" in outcomes[0]["structuredContent"]["error"]["message"],outcomes
    assert adapter.calls==[],adapter.calls
    fresh=module.tool_call("browser",{"operation":"disconnect"},expected_generation=1)
    assert not fresh["isError"] and len(adapter.calls)==1
    return {"oldRequestRejected":True,"oldAdapterCalls":0,"freshGenerationStillWorks":True}


def test_obsolete_controller(module):
    native=module.Native();new=ProcessFixture();old=ProcessFixture([json.dumps({"notification":"revoked","sessionEpoch":7})+"\n"])
    native.process=new;native.stopped=False;adapter=AdapterFixture();native.on_revoke=adapter.cancel
    old_event,new_event=threading.Event(),threading.Event()
    old_responses,new_responses=[],[]
    native.pending={1:(old,old_event,old_responses),2:(new,new_event,new_responses)}
    native.pending_methods={1:"observe",2:"input"}
    native._read(old)
    assert adapter.cancellations==0,"Obsolete controller revoked current browser"
    assert native.process is new and native.generation==0 and not native.stopped
    assert old_event.is_set() and old_responses and "error" in old_responses[0]
    assert not new_event.is_set() and new_responses==[]
    return {"oldNotificationIgnored":True,"oldEOFDoesNotRevokeReplacement":True,"onlyOldPendingRequestSettled":True}


def test_current_controller_revocation(module):
    native=module.Native();current=ProcessFixture([json.dumps({"notification":"revoked","sessionEpoch":8})+"\n"])
    native.process=current;native.stopped=False;adapter=AdapterFixture();native.on_revoke=adapter.cancel
    event=threading.Event();responses=[]
    native.pending={1:(current,event,responses)};native.pending_methods={1:"input"}
    native._read(current)
    assert adapter.cancellations>=1,"Current controller failed to revoke browser"
    assert native.process is None and native.stopped and native.generation==1
    assert event.is_set() and responses and "error" in responses[0]
    return {"activeControllerRevokes":True,"waitingOperationReleased":True,"generationAdvanced":True}


HARNESS=r'''
using System.Reflection;
using System.Text.Json;
using BackgroundControl;

internal static class Program
{
    static readonly BindingFlags Hidden=BindingFlags.Instance|BindingFlags.NonPublic;
    static readonly List<object> Checks=new();
    static void Assert(bool value,string message){if(!value)throw new Exception(message);}
    static long Generation(NativeInputSession s)=>(long)typeof(NativeInputSession).GetField("generation",Hidden)!.GetValue(s)!;
    static object Json(object value)=>JsonSerializer.SerializeToElement(value);
    static JsonElement Status(NativeInputSession s)=>(JsonElement)Json(s.TeardownStatus());
    static object Start(NativeInputSession s,long expected)
    {
        try{return typeof(NativeInputSession).GetMethod("StartTrackedClient",Hidden)!.Invoke(s,new object[]{123L,expected})!;}
        catch(TargetInvocationException ex){throw ex.InnerException!;}
    }
    static void SetFlag(NativeInputSession s,string name,int value)=>typeof(NativeInputSession).GetField(name,Hidden)!.SetValue(s,value);
    static bool IsVerified(JsonElement result)=>result.TryGetProperty("teardownVerified",out var verified)&&verified.GetBoolean();
    static void Pending(JsonElement result)
    {
        Assert(!IsVerified(result),"Pending startup reported verified teardown");
        Assert(!result.TryGetProperty("teardownRequired",out var required)||required.GetBoolean(),"Pending startup reported no teardown required");
    }
    static void WaitVerified(NativeInputSession s)
    {
        Assert(SpinWait.SpinUntil(()=>IsVerified(Status(s)),2000),"Cleanup acknowledgement was not exposed");
    }
    static async Task StartupCancel(bool priorCompletedTeardown)
    {
        var core=new WindowController();using var session=new NativeInputSession(core);
        if(priorCompletedTeardown)
        {
            NativeInputClient.Reset(block:false);
            Start(session,Generation(session));session.Cancel();
            NativeInputClient.Cleanup.TrySetResult(new {teardownVerified=true,hooksRemoved=true,moduleUnloaded=true,fixture="old"});
            WaitVerified(session);
        }
        NativeInputClient.Reset(block:true);
        long expected=Generation(session);
        var started=Task.Run(()=>Start(session,expected));
        Assert(NativeInputClient.Entered.Wait(2000),"Helper constructor not entered");
        try
        {
            Pending(Status(session));session.Cancel();Pending(Status(session));
        }
        finally{NativeInputClient.Release.Set();}
        try{await started.WaitAsync(TimeSpan.FromSeconds(2));throw new Exception("Cancelled startup returned a usable client");}
        catch(InvalidOperationException ex){Assert(ex.Message=="attachment_cancelled","Unexpected startup error: "+ex.Message);}
        Assert(NativeInputClient.Revocations==1,"Cancelled new helper not revoked exactly once");
        Pending(Status(session));
        NativeInputClient.Cleanup.TrySetResult(new {teardownVerified=true,hooksRemoved=true,moduleUnloaded=true,fixture="current"});
        WaitVerified(session);
        Assert(Status(session).GetProperty("fixture").GetString()=="current","Old teardown result leaked into new generation");
        Checks.Add(new {name=priorCompletedTeardown?"new_pending_start_does_not_reuse_old_teardown":"stop_during_helper_construction_tracks_pending_cleanup",passed=true});
    }
    static void PublishedClientCancel()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);
        NativeInputClient.Reset(block:false);
        var client=Start(session,Generation(session));
        Assert(ReferenceEquals(typeof(NativeInputSession).GetField("client",Hidden)!.GetValue(session),client),"Client not published before capability handshake");
        session.Cancel();Assert(NativeInputClient.Revocations==1,"Published handshake helper was not revoked");Pending(Status(session));
        NativeInputClient.Cleanup.TrySetResult(new {teardownVerified=true,hooksRemoved=true,moduleUnloaded=true});WaitVerified(session);
        Checks.Add(new {name="stop_before_capabilities_handshake_reaches_published_helper",passed=true});
    }
    static async Task RevokePublicationGap()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);
        NativeInputClient.Reset(block:false);
        var lifecycle=typeof(NativeInputSession).GetField("nativeLifecycle",Hidden)!.GetValue(session)!;
        var reachedRevoke=new ManualResetEventSlim(false);var releaseRevoke=new ManualResetEventSlim(false);
        long expected=Generation(session);Task? cancel=null;bool outerHeld=false;
        NativeInputClient.ConstructorHook=()=>
        {
            // Force the real Cancel to advance generation but wait for nativeLifecycle.
            cancel=Task.Run(session.Cancel);
            Assert(SpinWait.SpinUntil(()=>Generation(session)!=expected,2000),"Cancel did not advance generation");
        };
        NativeInputClient.RevokeHook=()=>
        {
            // Startup has left its own publication lock. Release only our scheduling
            // gate, so Cancel/TeardownStatus can run during the real Revoke call.
            Monitor.Exit(lifecycle);outerHeld=false;reachedRevoke.Set();
            Assert(releaseRevoke.Wait(3000),"Fixture Revoke gate timed out");
        };
        var started=Task.Run(()=>
        {
            Monitor.Enter(lifecycle);outerHeld=true;
            try{return Start(session,expected);}
            finally{if(outerHeld){Monitor.Exit(lifecycle);outerHeld=false;}}
        });
        Assert(reachedRevoke.Wait(2000),"Cancelled startup did not reach Revoke barrier");
        try
        {
            await cancel!.WaitAsync(TimeSpan.FromSeconds(2));
            Pending(Status(session));
            try{session.RequireDetached(0);throw new Exception("Transition passed while Revoke had not returned its task");}
            catch(InvalidOperationException ex){Assert(ex.Message.StartsWith("target_transition_teardown_pending"),"Unexpected transition error");}
        }
        finally{releaseRevoke.Set();}
        try{await started.WaitAsync(TimeSpan.FromSeconds(2));throw new Exception("Cancelled startup returned client");}
        catch(InvalidOperationException ex){Assert(ex.Message=="attachment_cancelled","Unexpected startup error");}
        Pending(Status(session));
        NativeInputClient.Cleanup.TrySetResult(new {teardownVerified=true,hooksRemoved=true,moduleUnloaded=true});WaitVerified(session);
        session.RequireDetached(0);
        Checks.Add(new {name="cancelled_start_keeps_pending_authority_during_revoke_publication_gap",passed=true});
    }
    static void TransitionRequiresBothProofs()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);
        session.RequireDetached(0);NativeInputClient.Reset(block:false);Start(session,Generation(session));session.Cancel();
        try{session.RequireDetached(0);throw new Exception("Transition accepted pending cleanup");}
        catch(InvalidOperationException ex){Assert(ex.Message.StartsWith("target_transition_teardown_pending"),"Unexpected pending transition error");}
        NativeInputClient.Cleanup.TrySetResult(new {hooksRemoved=true,moduleUnloaded=false,teardownVerified=false});
        try{session.RequireDetached(0);throw new Exception("Hook removal alone allowed target replacement");}
        catch(InvalidOperationException ex){Assert(ex.Message.StartsWith("target_transition_teardown_pending"),"Unexpected incomplete transition error");}
        Checks.Add(new {name="target_transition_refuses_pending_cleanup_and_hook_only_proof",passed=true});
    }
    static void ConstructorFailsBeforeLaunch()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);NativeInputClient.Reset(block:false);
        NativeInputClient.ConstructorHook=()=>throw new InvalidOperationException("fixture_prelaunch_failure");
        try{Start(session,Generation(session));throw new Exception("Failing helper boundary returned client");}
        catch(InvalidOperationException ex){Assert(ex.Message=="fixture_prelaunch_failure","Unexpected construction error");}
        Assert(NativeInputClient.Revocations==0,"No constructed helper should need revocation in this fixture");
        session.Cancel();session.RequireDetached(0);
        Checks.Add(new {name="known_prelaunch_constructor_failure_leaves_no_pending_child",passed=true});
    }
    static void PendingCleanupPreventsNextConstructor()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);NativeInputClient.Reset(block:false);
        Start(session,Generation(session));session.Cancel();
        var originalCleanup=NativeInputClient.Cleanup.Task;
        Assert(NativeInputClient.Constructors==1,"First helper did not start exactly once");
        try{Start(session,Generation(session));throw new Exception("Pending old cleanup allowed another helper constructor");}
        catch(InvalidOperationException ex){Assert(ex.Message.StartsWith("target_transition_teardown_pending"),"Wrong pending restart rejection: "+ex.Message);}
        Assert(NativeInputClient.Constructors==1,"A refused restart reached the helper constructor");
        Assert(ReferenceEquals(typeof(NativeInputSession).GetField("teardown",Hidden)!.GetValue(session),originalCleanup),"Refused restart overwrote old cleanup evidence");
        Pending(Status(session));
        NativeInputClient.Cleanup.TrySetResult(new {teardownVerified=true,hooksRemoved=true,moduleUnloaded=true});WaitVerified(session);
        Start(session,Generation(session));
        Assert(NativeInputClient.Constructors==2,"Verified cleanup did not permit a subsequent helper start");
        Checks.Add(new {name="pending_cleanup_prevents_next_constructor_and_preserves_evidence",passed=true});
    }
    static void IsolationPolicy()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);
        foreach(var reason in new[]{"target_acquired_foreground","target_acquired_physical_keyboard_focus","target_acquired_physical_mouse_capture"})
            InputIsolationMonitor.Last!.Notify(reason);
        Assert(core.Pauses==0&&!core.Paused,"Idle user interaction blocked toolbar pairing");
        SetFlag(session,"inputInFlight",1);InputIsolationMonitor.Last!.Notify("target_acquired_foreground");
        Assert(core.Pauses==1&&core.Paused,"Foreground acquisition during automated input was not revoked");
        core.Resume();SetFlag(session,"inputInFlight",0);SetFlag(session,"heldState",1);
        InputIsolationMonitor.Last!.Notify("target_acquired_physical_mouse_capture");
        Assert(core.Pauses==2&&core.Paused,"Held virtual state did not retain isolation protection");
        core.Resume();SetFlag(session,"heldState",0);InputIsolationMonitor.Last!.Notify("selected_window_destroyed");
        Assert(core.Stops==1,"Idle target destruction did not revoke selection");
        Checks.Add(new {name="idle_gesture_allowed_but_inflight_and_held_input_revoke",passed=true});
        Checks.Add(new {name="target_destruction_always_revokes",passed=true});
    }
    static void CancelBeforeStart()
    {
        var core=new WindowController();using var session=new NativeInputSession(core);NativeInputClient.Reset(block:false);
        long old=Generation(session);session.Cancel();
        try{Start(session,old);throw new Exception("Stale generation started helper");}
        catch(InvalidOperationException ex){Assert(ex.Message=="input_cancelled","Unexpected generation error");}
        Assert(NativeInputClient.Constructors==0,"Cancelled sequence launched child boundary");
        Checks.Add(new {name="cancel_before_start_prevents_helper_creation",passed=true});
    }
    static async Task Main()
    {
        await StartupCancel(false);await StartupCancel(true);PublishedClientCancel();await RevokePublicationGap();TransitionRequiresBothProofs();ConstructorFailsBeforeLaunch();PendingCleanupPreventsNextConstructor();IsolationPolicy();CancelBeforeStart();
        Console.WriteLine(JsonSerializer.Serialize(new {passed=true,checks=Checks,
            scope="Actual NativeInputSession.cs lifecycle, inert helper/controller/WinEvent boundaries; no native DLL or real OS focus event exercised"}));
    }
}

namespace BackgroundControl
{
    public sealed class WindowController
    {
        public event Action? Revoked;
        public long TargetHwnd {get;private set;}=123;
        public bool Paused {get;private set;}
        public long SessionEpoch {get;private set;}
        public int Pauses {get;private set;}
        public int Stops {get;private set;}
        public void Pause(){Paused=true;Pauses++;SessionEpoch++;Revoked?.Invoke();}
        public void Resume(){Paused=false;SessionEpoch++;}
        public void Stop(){Paused=true;Stops++;TargetHwnd=0;SessionEpoch++;Revoked?.Invoke();}
        public Action CaptureActiveSessionGuard()=>()=>{};
    }
    public sealed record CaptureSnapshot(bool Active,bool Paused,bool IsClosed,int Width,int Height,long FrameVersion,long AgeMs,string? PngBase64,string Status);
    internal sealed class InputIsolationMonitor : IDisposable
    {
        public static InputIsolationMonitor? Last;
        readonly Action<string> violation;
        public InputIsolationMonitor(Func<long> target,Action<string> violation){this.violation=violation;Last=this;}
        public void Notify(string reason)=>violation(reason);
        public void Dispose(){}
    }
    internal sealed class NativeInputClient
    {
        public static ManualResetEventSlim Entered=new(false),Release=new(true);
        public static TaskCompletionSource<object> Cleanup=new(TaskCreationOptions.RunContinuationsAsynchronously);
        public static int Constructors,Revocations;
        public static Action? ConstructorHook,RevokeHook;
        public static void Reset(bool block)
        {Entered=new(false);Release=new(!block);Cleanup=new(TaskCreationOptions.RunContinuationsAsynchronously);Constructors=Revocations=0;ConstructorHook=RevokeHook=null;}
        public NativeInputClient(long hwnd)
        {Interlocked.Increment(ref Constructors);Entered.Set();if(!Release.Wait(3000))throw new TimeoutException("fixture_constructor_gate");ConstructorHook?.Invoke();}
        public static string HelperPath(long hwnd)=>"fixture-not-a-real-helper.exe";
        public JsonElement Request(object request)=>JsonSerializer.SerializeToElement(new {fixture=true});
        public Task<object> Revoke(){Interlocked.Increment(ref Revocations);RevokeHook?.Invoke();return Cleanup.Task;}
    }
}
'''


def test_csharp_lifecycle():
    directory=(RESULTS/"core-race-harness").resolve()
    assert directory.is_relative_to(RESULTS.resolve())
    directory.mkdir(parents=True,exist_ok=True)
    source=ROOT/"controller"/"NativeInputSession.cs"
    parser_source=ROOT/"controller"/"KeyChordParser.cs"
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    parser_digest=hashlib.sha256(parser_source.read_bytes()).hexdigest()
    target_framework="net10.0-windows10.0.19041.0"
    project=f'''<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>{target_framework}</TargetFramework><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable></PropertyGroup><ItemGroup><Compile Include="{escape(str(source))}" Link="NativeInputSession.cs" /><Compile Include="{escape(str(parser_source))}" Link="KeyChordParser.cs" /><PackageReference Include="Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation" Version="0.7.0" /></ItemGroup></Project>'''
    (directory/"RaceHarness.csproj").write_text(project,encoding="utf-8")
    (directory/"Program.cs").write_text(HARNESS,encoding="utf-8")
    (directory/"NuGet.Config").write_text('<configuration><packageSources><clear /></packageSources></configuration>',encoding="utf-8")
    env={**os.environ,"DOTNET_CLI_TELEMETRY_OPTOUT":"1","DOTNET_NOLOGO":"1","DOTNET_GENERATE_ASPNET_CERTIFICATE":"false","DOTNET_CLI_HOME":str(directory/"dotnet-home"),
         "NUGET_PACKAGES":os.environ.get("NUGET_PACKAGES",str(Path.home()/".nuget"/"packages"))}
    build=subprocess.run(["dotnet","build",str(directory/"RaceHarness.csproj"),"-c","Release","--configfile",str(directory/"NuGet.Config"),"--verbosity","quiet"],cwd=directory,env=env,text=True,capture_output=True,timeout=60,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert build.returncode==0,build.stdout+build.stderr
    run=subprocess.run(["dotnet",str(directory/"bin"/"Release"/target_framework/"RaceHarness.dll")],cwd=directory,env=env,text=True,capture_output=True,timeout=20,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    assert run.returncode==0,run.stdout+run.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest()==digest,"Production source changed during test; rerun against final source"
    assert hashlib.sha256(parser_source.read_bytes()).hexdigest()==parser_digest,"Key parser source changed during test; rerun against final source"
    result=json.loads(run.stdout.strip())
    assert result["passed"] and len(result["checks"])==10
    return {**result,"sourceSha256":digest,"keyParserSha256":parser_digest,"externalBoundariesAreFixtures":True}


def main():
    module=load_mcp()
    error=None
    try:
        check("queued_browser_request_rechecked_after_operation_lock",lambda:test_queued_browser_stop(module))
        check("obsolete_controller_notification_and_eof_cannot_revoke_replacement",lambda:test_obsolete_controller(module))
        check("current_controller_revokes_and_releases_pending_call",lambda:test_current_controller_revocation(module))
        check("production_csharp_startup_and_isolation_races",test_csharp_lifecycle)
    except Exception:
        error=traceback.format_exc()
    RESULTS.mkdir(parents=True,exist_ok=True)
    report={"passed":error is None,"scope":"Real production MCP and NativeInputSession lifecycle with inert OS/helper boundaries; no user apps or native DLL loaded", "checks":checks,
        "limitations":["Does not prove native DLL unload or actual Windows focus events.","Does not exercise real browser pairing or WGC frames.","C# lifecycle calls use reflection to reach the actual private startup transaction."],
        "source":{"mcpSha256":hashlib.sha256((ROOT/"scripts"/"mcp_server.py").read_bytes()).hexdigest()},"error":error}
    (RESULTS/"core-race-regression.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
    print(json.dumps(report,indent=2))
    if error:raise SystemExit(1)


if __name__=="__main__":main()
