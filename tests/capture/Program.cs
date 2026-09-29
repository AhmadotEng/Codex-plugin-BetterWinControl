using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Automation.Provider;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using System.Windows.Shell;
using BackgroundControl;

internal static class Program
{
    [STAThread] static int Main(string[] args)
    {
        if(args.Length!=1)return 2;
        var app=new Application{ShutdownMode=ShutdownMode.OnExplicitShutdown};
        var watchdog=new DispatcherTimer{Interval=TimeSpan.FromSeconds(18)};
        watchdog.Tick+=(_,_)=>app.Shutdown(3);watchdog.Start();
        app.Startup+=async (_,_)=>await Run(app,Path.GetFullPath(args[0]));
        return app.Run();
    }
    static Window Fixture(double left,double top,int width,int height,Color color)
    {
        var w=new Window{Left=left,Top=top,Width=width,Height=height,Background=new SolidColorBrush(color),Content=new Border(),
            ShowActivated=false,ShowInTaskbar=false,Topmost=true,WindowStyle=WindowStyle.None,ResizeMode=ResizeMode.NoResize,
            WindowStartupLocation=WindowStartupLocation.Manual};
        w.SourceInitialized+=(_,_)=>{var h=new WindowInteropHelper(w).Handle;Native.SetWindowLongPtr(h,-20,new nint(Native.GetWindowLongPtr(h,-20).ToInt64()|0x08000080));};
        return w;
    }
    static async Task Run(Application app,string resultPath)
    {
        var sw=Stopwatch.StartNew();var checks=new List<object>();var commands=new List<string>();var metadata=new ConcurrentQueue<InputState>();
        var target=Fixture(40,40,160,120,Color.FromRgb(32,64,224));Window? cover=null;CapturePreview? preview=null;string? failure=null;
        using var monitorStop=new CancellationTokenSource();
        var monitor=Task.Run(async()=>{try{while(!monitorStop.IsCancellationRequested){metadata.Enqueue(Native.Sample(sw.ElapsedMilliseconds));await Task.Delay(20,monitorStop.Token);}}catch(OperationCanceledException){}});
        try
        {
            target.Show();await Task.Delay(100);
            var targetHwnd=new WindowInteropHelper(target).Handle;
            Native.GetWindowThreadProcessId(targetHwnd,out var owner);
            Require(owner==Environment.ProcessId,"fixture owner");
            preview=new CapturePreview(app.Dispatcher,command=>
            {
                commands.Add(command);
                if(command=="pause")preview!.SetPaused(true);
                if(command=="resume")preview!.SetPaused(false);
                if(command=="stop")preview!.Stop();
            });
            void NoFocus(){if(metadata.Append(Native.Sample(sw.ElapsedMilliseconds)).Any(s=>s.ForegroundPid==Environment.ProcessId||s.FocusPid==Environment.ProcessId))throw new Exception("Own preview/fixture acquired focus; no restoration attempted.");}
            async Task<CaptureSnapshot> Frame(string name,int r,int g,int b,long previousVersion,int? oldWidth=null)
            {
                var wait=Stopwatch.StartNew();
                while(wait.ElapsedMilliseconds<4000)
                {
                    NoFocus();var s=preview.Snapshot(true);
                    if(s.PngBase64 is not null&&s.FrameVersion>previousVersion&&(oldWidth is null||s.Width!=oldWidth))
                    {
                        var png=Convert.FromBase64String(s.PngBase64);using var memory=new MemoryStream(png);
                        var decoded=new PngBitmapDecoder(memory,BitmapCreateOptions.PreservePixelFormat,BitmapCacheOption.OnLoad).Frames[0];
                        var converted=new FormatConvertedBitmap(decoded,PixelFormats.Bgra32,null,0);var pixel=new byte[4];
                        converted.CopyPixels(new Int32Rect(s.Width/2,s.Height/2,1,1),pixel,4,0);
                        if(Math.Abs(pixel[2]-r)<=12&&Math.Abs(pixel[1]-g)<=12&&Math.Abs(pixel[0]-b)<=12)
                        {
                            File.WriteAllBytes(Path.Combine(Path.GetDirectoryName(resultPath)!,name+".png"),png);
                            checks.Add(new {stage=name,s.Width,s.Height,s.FrameVersion,s.AgeMs,rgba=new int[]{pixel[2],pixel[1],pixel[0],pixel[3]},pngSha256=Convert.ToHexString(SHA256.HashData(png))});return s;
                        }
                    }
                    await Task.Delay(40);
                }
                throw new TimeoutException(name+"; last status: "+preview.Snapshot(false).Status);
            }
            async Task Wait(string name,Func<bool> predicate)
            {
                var wait=Stopwatch.StartNew();while(wait.ElapsedMilliseconds<1500){NoFocus();if(predicate()){checks.Add(new{stage=name,passed=true});return;}await Task.Delay(30);}throw new TimeoutException(name);
            }
            (Window Window,Image Image,FrameworkElement Surface,nint Handle) PreviewParts()
            {
                var window=app.Windows.OfType<Window>().Single(w=>w.Title.StartsWith("Background control — ",StringComparison.Ordinal));
                return(window,Field<Image>(preview!,"_image"),Field<FrameworkElement>(preview!,"_surface"),new WindowInteropHelper(window).Handle);
            }
            object RequireFullImage(CaptureSnapshot frame,string stage)
            {
                var parts=PreviewParts();parts.Window.UpdateLayout();var dpi=VisualTreeHelper.GetDpi(parts.Window);
                var source=parts.Image.Source as BitmapSource??throw new InvalidOperationException(stage+": preview image missing; "+preview!.Snapshot(false).Status);
                var origin=parts.Image.TranslatePoint(new System.Windows.Point(),parts.Surface);
                var widthError=Math.Abs(parts.Image.ActualWidth-parts.Surface.ActualWidth)*dpi.DpiScaleX;
                var heightError=Math.Abs(parts.Image.ActualHeight-parts.Surface.ActualHeight)*dpi.DpiScaleY;
                Require(source.PixelWidth==frame.Width&&source.PixelHeight==frame.Height,stage+": preview matches current capture dimensions");
                Require(parts.Image.Stretch==Stretch.Uniform&&parts.Image.Margin==new Thickness(0),stage+": no crop or distortion mode");
                Require(Math.Abs(origin.X)*dpi.DpiScaleX<=1.01&&Math.Abs(origin.Y)*dpi.DpiScaleY<=1.01&&widthError<=1.01&&heightError<=1.01,
                    stage+": image fills both surface dimensions within one physical pixel ("+widthError+", "+heightError+")");
                var outerRead=Native.GetWindowRect(parts.Handle,out var outer);var clientRead=Native.GetClientRect(parts.Handle,out var client);
                Require(outerRead&&clientRead,stage+": own window geometry");
                Require(Math.Abs(client.Width-parts.Surface.ActualWidth*dpi.DpiScaleX)<=1.01&&Math.Abs(client.Height-parts.Surface.ActualHeight*dpi.DpiScaleY)<=1.01,
                    stage+": image surface fills native client");
                return new{stage,passed=true,sourceWidth=source.PixelWidth,sourceHeight=source.PixelHeight,imageWidth=parts.Image.ActualWidth,imageHeight=parts.Image.ActualHeight,
                    surfaceWidth=parts.Surface.ActualWidth,surfaceHeight=parts.Surface.ActualHeight,widthErrorPixels=widthError,heightErrorPixels=heightError,
                    outerWidth=outer.Width,outerHeight=outer.Height,clientWidth=client.Width,clientHeight=client.Height,dpi.DpiScaleX,dpi.DpiScaleY};
            }
            async Task AspectSettled(CaptureSnapshot frame,string stage)
            {
                // A frame may arrive at its new dimensions before the capture restart debounce finishes.
                // Verify geometry after that deliberate refresh has completed, not during ClearImage.
                await Wait(stage+"_live",()=>{var state=preview!.Snapshot(false);return state.Status=="Live preview"&&state.Width==frame.Width&&state.Height==frame.Height&&PreviewParts().Image.Source is not null;});
                await Task.Delay(40);NoFocus();checks.Add(RequireFullImage(frame,stage));
            }
            object RequireBorderlessSession(string stage)
            {
                var access=Field<Task>(preview!,"_borderlessAccess");
                Require(access.IsCompletedSuccessfully,stage+": borderless access request completed successfully");
                var accessStatus=access.GetType().GetProperty("Result")?.GetValue(access)?.ToString();
                Require(accessStatus=="Allowed",stage+": Windows allowed borderless capture");
                var notice=Field<string>(preview!,"_borderlessNotice");
                Require(string.IsNullOrEmpty(notice),stage+": no capture-border fallback or failure notice");
                var grabber=Field<object>(preview!,"_grabber");
                Require(grabber.GetType().FullName=="Microsoft.Windows.SDK.BuildTools.WinApp.UIAutomation.WgcCapture+FrameGrabber",stage+": pinned capture implementation");
                var session=grabber.GetType().GetField("_session",BindingFlags.Instance|BindingFlags.NonPublic)?.GetValue(grabber)
                    ??throw new InvalidOperationException(stage+": pinned WGC session unavailable");
                var property=session.GetType().GetProperty("IsBorderRequired",BindingFlags.Instance|BindingFlags.Public)
                    ??throw new InvalidOperationException(stage+": OS capture-border API unavailable");
                var borderRequired=property.GetValue(session) as bool?;
                Require(borderRequired==false,stage+": IsBorderRequired readback is false");
                checks.Add(new{stage,passed=true,accessStatus,notice,grabberType=grabber.GetType().FullName,sessionType=session.GetType().FullName,isBorderRequired=borderRequired,
                    limitation="Reads the real Windows capture-session configuration. This does not visually verify border removal; Windows policy or another capturing app can still require the border."});
                return session;
            }
            async Task ResizeCases(CaptureSnapshot frame,string label)
            {
                var parts=PreviewParts();var ratio=(double)frame.Width/frame.Height;var dpi=VisualTreeHelper.GetDpi(parts.Window);
                Native.GetWindowThreadProcessId(parts.Handle,out var previewOwner);Require(previewOwner==Environment.ProcessId,"sizing messages target own preview only");
                Native.GetWindowRect(parts.Handle,out var initialOuter);Native.GetClientRect(parts.Handle,out var initialClient);
                var extraWidth=initialOuter.Width-initialClient.Width;var extraHeight=initialOuter.Height-initialClient.Height;
                var baselineWidth=(int)Math.Ceiling(Math.Max(400,Math.Max(parts.Window.MinWidth*dpi.DpiScaleX,parts.Window.MinHeight*dpi.DpiScaleY*ratio)));
                var baselineHeight=(int)Math.Round((baselineWidth-extraWidth)/ratio)+extraHeight;
                var cases=new List<object>();
                foreach(var edge in Enumerable.Range(1,8))
                {
                    Require(Native.SetWindowPos(parts.Handle,0,60,60,baselineWidth,baselineHeight,0x14),"reset own preview dimensions without activation");
                    await Task.Delay(18);parts.Window.UpdateLayout();NoFocus();
                    Native.GetWindowRect(parts.Handle,out var baseline);
                    var proposal=baseline;
                    if(edge is 1 or 4 or 7)proposal.Left-=73;
                    if(edge is 2 or 5 or 8)proposal.Right+=73;
                    if(edge is 3 or 4 or 5)proposal.Top-=41;
                    if(edge is 6 or 7 or 8)proposal.Bottom+=41;
                    var originalProposal=proposal;
                    var handled=Native.SendMessage(parts.Handle,0x0214,new nint(edge),ref proposal);
                    Require(handled!=0,label+": WM_SIZING handled on edge "+edge);
                    var clientWidth=proposal.Width-extraWidth;var clientHeight=proposal.Height-extraHeight;
                    Require(clientWidth>0&&clientHeight>0&&Math.Min(Math.Abs(clientWidth/ratio-clientHeight),Math.Abs(clientHeight*ratio-clientWidth))<=1.01,
                        label+": WM_SIZING ratio on edge "+edge);
                    Require(edge switch
                    {
                        1=>proposal.Right==baseline.Right&&proposal.Top==baseline.Top,
                        2=>proposal.Left==baseline.Left&&proposal.Top==baseline.Top,
                        3=>proposal.Bottom==baseline.Bottom&&proposal.Left==baseline.Left,
                        4=>proposal.Right==baseline.Right&&proposal.Bottom==baseline.Bottom,
                        5=>proposal.Left==baseline.Left&&proposal.Bottom==baseline.Bottom,
                        6=>proposal.Top==baseline.Top&&proposal.Left==baseline.Left,
                        7=>proposal.Right==baseline.Right&&proposal.Top==baseline.Top,
                        8=>proposal.Left==baseline.Left&&proposal.Top==baseline.Top,
                        _=>false
                    },label+": opposite edge/corner remains anchored on edge "+edge);
                    Require(Native.SetWindowPos(parts.Handle,0,proposal.Left,proposal.Top,proposal.Width,proposal.Height,0x14),"apply constrained own preview RECT");
                    await Task.Delay(18);NoFocus();
                    cases.Add(new{edge,proposed=originalProposal.ToArray(),constrained=proposal.ToArray(),fill=RequireFullImage(frame,label+"_edge_"+edge)});
                }
                // A deliberately sub-minimum proposal must retain the ratio after enforcing both minimum dimensions.
                Native.GetWindowRect(parts.Handle,out var beforeMinimum);var minimum=beforeMinimum;
                minimum.Right=minimum.Left+32;minimum.Bottom=minimum.Top+24;
                Require(Native.SendMessage(parts.Handle,0x0214,new nint(8),ref minimum)!=0,label+": minimum WM_SIZING handled");
                Require(minimum.Width+1>=parts.Window.MinWidth*dpi.DpiScaleX&&minimum.Height+1>=parts.Window.MinHeight*dpi.DpiScaleY,label+": both preview minimum dimensions honored");
                Require(minimum.Left==beforeMinimum.Left&&minimum.Top==beforeMinimum.Top,label+": minimum opposite corner anchored");
                Require(Native.SetWindowPos(parts.Handle,0,minimum.Left,minimum.Top,minimum.Width,minimum.Height,0x14),"apply minimum own preview RECT");
                await Task.Delay(18);NoFocus();
                checks.Add(new{stage=label+"_native_sizing_all_edges",passed=true,cases,minimum=minimum.ToArray(),minimumFill=RequireFullImage(frame,label+"_minimum"),
                    input="WM_SIZING and SetWindowPos only for own-process preview; no physical input or user windows changed."});
            }
            void Click(string id)
            {
                var window=app.Windows.OfType<Window>().Single(w=>w.Title.StartsWith("Background control — ",StringComparison.Ordinal));
                var button=FindButton(window,id)??throw new InvalidOperationException("Own preview button missing: "+id);
                var peer=new ButtonAutomationPeer(button);((IInvokeProvider)peer.GetPattern(PatternInterface.Invoke)).Invoke();
            }
            preview.Start(targetHwnd,"Capture fixture");
            var blue=await Frame("initial",32,64,224,-1);
            await AspectSettled(blue,"initial_capture_aspect");
            var firstSession=RequireBorderlessSession("initial_borderless_session_configuration");
            var offThread=await Task.Run(()=>preview.Snapshot(true));Require(offThread.PngBase64 is not null,"off-thread PNG snapshot");checks.Add(new{stage="off_thread_snapshot",passed=true});
            cover=Fixture(34,34,320,240,Colors.Magenta);cover.Show();await Task.Delay(80);
            Native.GetWindowRect(targetHwnd,out var bounds);Require(Native.TestCover(bounds,new WindowInteropHelper(cover).Handle),"own cover");
            checks.Add(new{stage="own_cover_verified",passed=true});
            target.Background=new SolidColorBrush(Color.FromRgb(32,208,64));var green=await Frame("occluded",32,208,64,blue.FrameVersion);
            target.Width=224;target.Height=152;target.Background=new SolidColorBrush(Color.FromRgb(224,48,32));
            var red=await Frame("resized",224,48,32,green.FrameVersion,green.Width);
            Native.GetWindowRect(targetHwnd,out bounds);Require(Native.TestCover(bounds,new WindowInteropHelper(cover).Handle),"resized own cover");
            await AspectSettled(red,"changed_landscape_capture_aspect");
            var resizedSession=RequireBorderlessSession("resized_borderless_session_configuration");
            Require(!ReferenceEquals(firstSession,resizedSession),"source resize starts a new configured capture session");
            await ResizeCases(red,"landscape");
            target.Width=120;target.Height=224;
            var portrait=await Frame("portrait",224,48,32,red.FrameVersion,red.Width);
            await AspectSettled(portrait,"changed_portrait_capture_aspect");
            RequireBorderlessSession("portrait_borderless_session_configuration");
            await ResizeCases(portrait,"portrait");
            target.Width=224;target.Height=152;
            red=await Frame("landscape_restored",224,48,32,portrait.FrameVersion,portrait.Width);
            await AspectSettled(red,"landscape_capture_aspect_restored");
            RequireBorderlessSession("restored_borderless_session_configuration");
            var ownPreview=app.Windows.OfType<Window>().Single(w=>w.Title.StartsWith("Background control — ",StringComparison.Ordinal));
            var chrome=Field<FrameworkElement>(preview,"_chrome");
            var previewImage=Field<Image>(preview,"_image");
            var surface=Field<FrameworkElement>(preview,"_surface");
            var previewHandle=new WindowInteropHelper(ownPreview).Handle;
            var nativeChrome=WindowChrome.GetWindowChrome(ownPreview);
            Require(ownPreview.WindowStyle==WindowStyle.None,"borderless preview");
            Require(!ownPreview.ShowActivated&&(Native.GetWindowLongPtr(previewHandle,-20).ToInt64()&0x08000000L)!=0,"nonactivating preview");
            Require(nativeChrome is not null&&nativeChrome.ResizeBorderThickness.Left>0&&nativeChrome.ResizeBorderThickness.Bottom>0,"native resize edges");
            checks.Add(new{stage="borderless_nonactivating_resize_configuration",passed=true,ownPreview.WindowStyle,ownPreview.ResizeMode,
                resizeBorder=nativeChrome!.ResizeBorderThickness.ToString(),limitation="Configuration inspected; no physical drag or resize input was injected."});
            async Task Hover(bool entered)
            {
                surface.RaiseEvent(new MouseEventArgs(Mouse.PrimaryDevice,Environment.TickCount)
                {RoutedEvent=entered?UIElement.MouseEnterEvent:UIElement.MouseLeaveEvent,Source=surface});
                await Task.Delay(300);ownPreview.UpdateLayout();NoFocus();
            }
            object RenderPreview(string filename)
            {
                ownPreview.UpdateLayout();
                var bitmap=new RenderTargetBitmap((int)Math.Ceiling(ownPreview.ActualWidth),(int)Math.Ceiling(ownPreview.ActualHeight),96,96,PixelFormats.Pbgra32);
                bitmap.Render(ownPreview);
                var encoder=new PngBitmapEncoder();encoder.Frames.Add(BitmapFrame.Create(bitmap));
                using var bytes=new MemoryStream();encoder.Save(bytes);var data=bytes.ToArray();
                File.WriteAllBytes(Path.Combine(Path.GetDirectoryName(resultPath)!,filename),data);
                return new{filename,width=bitmap.PixelWidth,height=bitmap.PixelHeight,sha256=Convert.ToHexString(SHA256.HashData(data))};
            }
            await Hover(false);
            Require((chrome.Opacity<=0.01||chrome.Visibility!=Visibility.Visible)&&!chrome.IsHitTestVisible,"idle overlay hidden and not interactive");
            var imageOrigin=previewImage.TranslatePoint(new System.Windows.Point(0,0),surface);
            Require(Math.Abs(surface.ActualWidth-ownPreview.ActualWidth)<=1&&Math.Abs(surface.ActualHeight-ownPreview.ActualHeight)<=1,"capture surface uses whole borderless client area");
            Require(previewImage.Margin==new Thickness(0)&&previewImage.Stretch==Stretch.Uniform&&VisualTreeHelper.GetParent(previewImage)==surface,"preview image has no title or footer layout inset");
            Require(imageOrigin.X>=-1&&imageOrigin.Y>=-1&&imageOrigin.X+previewImage.ActualWidth<=surface.ActualWidth+1&&imageOrigin.Y+previewImage.ActualHeight<=surface.ActualHeight+1&&
                Math.Abs(previewImage.ActualWidth-surface.ActualWidth)<=1&&Math.Abs(previewImage.ActualHeight-surface.ActualHeight)<=1,"uncropped aspect-correct preview fills both surface dimensions");
            checks.Add(new{stage="idle_hover_controls_hidden",passed=true,chrome.Opacity,chrome.Visibility,chrome.IsHitTestVisible,
                imageWidth=previewImage.ActualWidth,imageHeight=previewImage.ActualHeight,surfaceWidth=surface.ActualWidth,surfaceHeight=surface.ActualHeight,
                render=RenderPreview("preview-idle.png"),input="Synthetic WPF MouseLeave routed event; physical pointer was not moved."});
            await Hover(true);
            Require(chrome.Visibility==Visibility.Visible&&chrome.Opacity>=0.99&&chrome.IsHitTestVisible,"hover overlay visible and interactive");
            Require(FindButton(ownPreview,"preview.pause") is {IsVisible:true}&&FindButton(ownPreview,"preview.stop") is {IsVisible:true},"hover contains working controls");
            checks.Add(new{stage="hover_controls_visible",passed=true,chrome.Opacity,chrome.Visibility,chrome.IsHitTestVisible,
                render=RenderPreview("preview-hover.png"),input="Synthetic WPF MouseEnter routed event; physical pointer was not moved."});
            Require(!File.ReadAllBytes(Path.Combine(Path.GetDirectoryName(resultPath)!,"preview-idle.png")).SequenceEqual(
                File.ReadAllBytes(Path.Combine(Path.GetDirectoryName(resultPath)!,"preview-hover.png"))),"hover changes actual preview render");
            RenderPreview("preview-content.png");
            Click("preview.pause");await Wait("pause_button",()=>preview.Snapshot(false).Paused&&commands.LastOrDefault()=="pause");
            await Hover(false);
            Require((chrome.Opacity<=0.01||chrome.Visibility!=Visibility.Visible)&&!chrome.IsHitTestVisible,"paused overlay hides when pointer leaves");
            checks.Add(new{stage="paused_controls_hidden_on_leave",passed=true,render=RenderPreview("preview-paused-idle.png")});
            await Hover(true);
            Click("preview.pause");await Wait("resume_button",()=>!preview.Snapshot(false).Paused&&commands.LastOrDefault()=="resume");
            preview.SetStatus("Controller status check");Require(preview.Snapshot(false).Status.Contains("Controller status check"),"host status");
            Click("preview.stop");await Wait("stop_button",()=>!preview.Snapshot(false).Active&&commands.LastOrDefault()=="stop");
            preview.Start(targetHwnd,"Capture fixture");await Frame("restarted",224,48,32,-1);
            RequireBorderlessSession("restarted_borderless_session_configuration");
            var beforeClose=commands.Count;app.Windows.OfType<Window>().Single(w=>w.Title.StartsWith("Background control — ",StringComparison.Ordinal)).Close();
            await Wait("preview_close_stops",()=>!preview.Snapshot(false).Active&&commands.Count>beforeClose&&commands.Last()=="stop");
            preview.Start(targetHwnd,"Capture fixture");await Frame("closure_session",224,48,32,-1);
            target.WindowState=WindowState.Minimized;
            await Wait("minimized_label",()=>preview.Snapshot(false).Status.Contains("minimized",StringComparison.OrdinalIgnoreCase)&&preview.Snapshot(true).PngBase64 is null);
            target.Close();cover.Close();await Wait("target_closure",()=>preview.Snapshot(false).IsClosed&&!preview.Snapshot(false).Active);
            NoFocus();
        }
        catch(Exception ex){failure=ex.GetType().Name+": "+ex.Message;}
        finally
        {
            preview?.Dispose();foreach(Window w in app.Windows.OfType<Window>().ToArray())w.Close();monitorStop.Cancel();await monitor;metadata.Enqueue(Native.Sample(sw.ElapsedMilliseconds));
        }
        var samples=metadata.ToArray();var result=new{status=failure is null?"passed":"failed",failure,elapsedMs=sw.ElapsedMilliseconds,processId=Environment.ProcessId,
            checks,commands,remainingOwnWindows=app.Windows.Count,fixtureOrPreviewFocusObserved=samples.Any(s=>s.ForegroundPid==Environment.ProcessId||s.FocusPid==Environment.ProcessId),
            distinctForegroundHandles=samples.Select(s=>s.Foreground).Distinct().Count(),distinctFocusHandles=samples.Select(s=>s.Focus).Distinct().Count(),
            distinctCursorPositions=samples.Select(s=>(s.CursorX,s.CursorY)).Distinct().Count(),samples,
            limitations="Own WPF fixture only. WM_SIZING messages exercise the real native preview hook for all eight edges in landscape and portrait, including minimum dimensions; only this process's preview receives SetWindowPos. No physical resize drag was injected. Hover transitions use synthetic WPF MouseEnter/MouseLeave routed events without moving the physical pointer; real WPF visuals are rendered in each state. Semantic button invocation tests actual callback wiring without physical input. No arbitrary-app, protected-content, concurrent-user, or native Codex UI integration claim."};
        File.WriteAllText(resultPath,JsonSerializer.Serialize(result,new JsonSerializerOptions{WriteIndented=true}));Console.WriteLine(JsonSerializer.Serialize(new{result.status,failure,result.elapsedMs,result.remainingOwnWindows}));app.Shutdown(failure is null?0:1);
    }
    static Button? FindButton(DependencyObject root,string id)
    {
        if(root is Button button&&AutomationProperties.GetAutomationId(button)==id)return button;
        foreach(var child in LogicalTreeHelper.GetChildren(root).OfType<DependencyObject>()){var found=FindButton(child,id);if(found is not null)return found;}return null;
    }
    static T Field<T>(object instance,string name) where T:class=>typeof(CapturePreview).GetField(name,BindingFlags.Instance|BindingFlags.NonPublic)?.GetValue(instance) as T??throw new InvalidOperationException("Missing preview field: "+name);
    static void Require(bool condition,string name){if(!condition)throw new InvalidOperationException("Failed: "+name);}
}
internal record InputState(long ElapsedMs,long Foreground,uint ForegroundPid,long Focus,uint FocusPid,int CursorX,int CursorY);
internal static class Native
{
    [StructLayout(LayoutKind.Sequential)]internal struct Point{public int X,Y;public Point(int x,int y){X=x;Y=y;}}
    [StructLayout(LayoutKind.Sequential)]internal struct Rect{public int Left,Top,Right,Bottom;public int Width=>Right-Left;public int Height=>Bottom-Top;public int[] ToArray()=>new[]{Left,Top,Right,Bottom};}
    [StructLayout(LayoutKind.Sequential)]struct Gui{public int Size,Flags;public nint Active,Focus,Capture,MenuOwner,MoveSize,Caret;public Rect CaretRect;}
    [DllImport("user32.dll")]static extern nint GetForegroundWindow();
    [DllImport("user32.dll")]static extern bool GetGUIThreadInfo(uint thread,ref Gui info);
    [DllImport("user32.dll")]static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")]internal static extern uint GetWindowThreadProcessId(nint hwnd,out uint pid);
    [DllImport("user32.dll")]internal static extern bool GetWindowRect(nint hwnd,out Rect rect);
    [DllImport("user32.dll")]internal static extern bool GetClientRect(nint hwnd,out Rect rect);
    [DllImport("user32.dll")]internal static extern bool SetWindowPos(nint hwnd,nint insertAfter,int x,int y,int width,int height,uint flags);
    [DllImport("user32.dll",EntryPoint="SendMessageW")]internal static extern nint SendMessage(nint hwnd,uint message,nint wParam,ref Rect lParam);
    [DllImport("user32.dll")]static extern nint WindowFromPoint(Point point);
    [DllImport("user32.dll",EntryPoint="GetWindowLongPtrW")]internal static extern nint GetWindowLongPtr(nint hwnd,int index);
    [DllImport("user32.dll",EntryPoint="SetWindowLongPtrW")]internal static extern nint SetWindowLongPtr(nint hwnd,int index,nint value);
    internal static bool TestCover(Rect r,nint h)=>new[]{new Point(r.Left+8,r.Top+8),new Point(r.Right-8,r.Top+8),new Point(r.Left+8,r.Bottom-8),new Point(r.Right-8,r.Bottom-8),new Point((r.Left+r.Right)/2,(r.Top+r.Bottom)/2)}.All(p=>WindowFromPoint(p)==h);
    internal static InputState Sample(long ms){var h=GetForegroundWindow();GetWindowThreadProcessId(h,out var p);var g=new Gui{Size=Marshal.SizeOf<Gui>()};GetGUIThreadInfo(0,ref g);GetWindowThreadProcessId(g.Focus,out var fp);GetCursorPos(out var cursor);return new(ms,h.ToInt64(),p,g.Focus.ToInt64(),fp,cursor.X,cursor.Y);}
}
