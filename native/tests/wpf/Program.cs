using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Shapes;
using System.Windows.Threading;

internal static class Program
{
    static Window target = null!, canary = null!;
    static Button button = null!;
    static TextBox text = null!;
    static ScrollViewer scroll = null!;
    static Canvas drawing = null!;
    static Rectangle box = null!;
    static int clicks, keyChords, pointerDowns, pointerMoves, drags, textChanges, canaryInputs;
    static bool dragging;
    static Point dragOffset;
    static readonly List<object> inputEvidence = [];
    static readonly List<object> pointerEvidence = [];
    static readonly List<object> messageEvidence = [];
    static readonly object outputLock = new();
    [StructLayout(LayoutKind.Sequential)] struct POINT { public int X, Y; }
    [DllImport("user32.dll")] static extern bool ScreenToClient(nint hwnd, ref POINT point);
    [DllImport("user32.dll")] static extern short GetKeyState(int key);
    [DllImport("user32.dll")] static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] static extern nint GetFocus();
    [DllImport("user32.dll")] static extern nint GetCapture();
    static nint TargetHwnd => new WindowInteropHelper(target).Handle;
    static void TracePointer(object sender, MouseEventArgs e) {
        if(pointerEvidence.Count>=180)return;
        var point=e.GetPosition(target);
        pointerEvidence.Add(new { routedEvent=e.RoutedEvent.Name, source=e.OriginalSource?.GetType().Name, x=point.X,y=point.Y,left=e.LeftButton.ToString(), captured=Mouse.Captured?.GetType().Name,nativeCapture=(long)GetCapture(),nativeLeft=(GetKeyState(1)&0x8000)!=0 });
    }
    static void Emit(object data) { lock(outputLock) Console.WriteLine(JsonSerializer.Serialize(data)); }
    static object PointFor(FrameworkElement element, double x = -1, double y = -1)
    {
        var location = element.PointToScreen(new Point(x < 0 ? element.ActualWidth / 2 : x, y < 0 ? element.ActualHeight / 2 : y));
        var point = new POINT { X = (int)Math.Round(location.X), Y = (int)Math.Round(location.Y) };
        ScreenToClient(TargetHwnd, ref point);
        return new { x = point.X, y = point.Y };
    }
    static object State() => new {
        pid = Environment.ProcessId, hwnd = (long)TargetHwnd, canaryHwnd = (long)new WindowInteropHelper(canary).Handle,
        clicks, keyChords, pointerDowns, pointerMoves, drags, textChanges, canaryInputs,
        text = text.Text, scrollOffset = scroll.VerticalOffset, boxX = Canvas.GetLeft(box), boxY = Canvas.GetTop(box), dragging,
        inputEvidence = inputEvidence.ToArray(), pointerEvidence=pointerEvidence.ToArray(),messageEvidence=messageEvidence.ToArray(), keyboardFocusedElement = Keyboard.FocusedElement?.GetType().Name,
        points = new { button = PointFor(button), text = PointFor(text, 30, 15), scroll = PointFor(scroll), box = PointFor(box), dragDestination = PointFor(drawing, 190, 150), background = PointFor((Canvas)target.Content, 650, 60) },
        dpi = VisualTreeHelper.GetDpi(target).DpiScaleX,
        nativeState = new { foreground = (long)GetForegroundWindow(), focus = (long)GetFocus(), capture = (long)GetCapture() }
    };
    [STAThread] public static void Main()
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var canvas = new Canvas { Background = Brushes.White };
        target = new Window { Title = "BetterWinControl owned WPF input fixture", Width = 800, Height = 480, Left = 40, Top = 80, ShowActivated = false, Content = canvas };
        button = new Button { Content = "Count actual WPF clicks", Width = 230, Height = 40 };
        Canvas.SetLeft(button, 20); Canvas.SetTop(button, 20); canvas.Children.Add(button);
        button.Click += (_, _) => clicks++;
        text = new TextBox { Text = "seed", Width = 650, Height = 35 };
        Canvas.SetLeft(text, 20); Canvas.SetTop(text, 80); canvas.Children.Add(text);
        text.TextChanged += (_, _) => textChanges++;
        var items = new StackPanel();
        for(int i=0;i<60;i++) items.Children.Add(new TextBlock { Text = "Owned fixture row " + i, Height = 26 });
        scroll = new ScrollViewer { Width = 280, Height = 200, Content = items, VerticalScrollBarVisibility = ScrollBarVisibility.Visible };
        Canvas.SetLeft(scroll, 20); Canvas.SetTop(scroll, 145); canvas.Children.Add(scroll);
        drawing = new Canvas { Width = 370, Height = 240, Background = Brushes.AliceBlue };
        Canvas.SetLeft(drawing, 340); Canvas.SetTop(drawing, 145); canvas.Children.Add(drawing);
        box = new Rectangle { Width = 65, Height = 65, Fill = Brushes.SteelBlue };
        Canvas.SetLeft(box, 40); Canvas.SetTop(box, 40); drawing.Children.Add(box);
        box.MouseLeftButtonDown += (_, e) => { dragging = true; dragOffset = e.GetPosition(box); box.CaptureMouse(); e.Handled = true; };
        box.MouseMove += (_, e) => { if(dragging) { var point=e.GetPosition(drawing); Canvas.SetLeft(box,point.X-dragOffset.X);Canvas.SetTop(box,point.Y-dragOffset.Y); } };
        box.MouseLeftButtonUp += (_, e) => { if(dragging)drags++;dragging=false;box.ReleaseMouseCapture();e.Handled=true; };
        target.PreviewMouseDown += (_, _) => pointerDowns++;
        target.PreviewMouseMove += (_, _) => pointerMoves++;
        target.AddHandler(Mouse.PreviewMouseDownEvent,new MouseButtonEventHandler(TracePointer),true);
        target.AddHandler(Mouse.PreviewMouseUpEvent,new MouseButtonEventHandler(TracePointer),true);
        target.AddHandler(Mouse.PreviewMouseMoveEvent,new MouseEventHandler(TracePointer),true);
        target.AddHandler(Mouse.PreviewMouseWheelEvent,new MouseWheelEventHandler(TracePointer),true);
        target.AddHandler(Mouse.LostMouseCaptureEvent,new MouseEventHandler((sender,e)=>{
            TracePointer(sender,e);
            if(pointerEvidence.Count<180)pointerEvidence.Add(new { lostCaptureStack=Environment.StackTrace });
        }),true);
        target.PreviewKeyDown += (_, e) => {
            bool control=(Keyboard.Modifiers&ModifierKeys.Control)!=0;
            inputEvidence.Add(new { key=e.Key.ToString(), control, nativeControl=(GetKeyState(17)&0x8000)!=0,leftControl=(GetKeyState(162)&0x8000)!=0,rightControl=(GetKeyState(163)&0x8000)!=0,shift=(Keyboard.Modifiers&ModifierKeys.Shift)!=0,leftShift=(GetKeyState(160)&0x8000)!=0,rightShift=(GetKeyState(161)&0x8000)!=0 });
            if(control&&e.Key==Key.K) { keyChords++;e.Handled=true; }
        };
        canary = new Window { Title = "BetterWinControl owned WPF sibling canary", Width = 330, Height = 180, Left = 880, Top = 80, ShowActivated = false, Content = new TextBox { Text = "Owned sibling canary; no input expected." } };
        canary.PreviewMouseDown += (_, _) => canaryInputs++;
        canary.PreviewKeyDown += (_, _) => canaryInputs++;
        canary.PreviewTextInput += (_, _) => canaryInputs++;
        target.Show();canary.Show();
        HwndSource.FromHwnd(TargetHwnd).AddHook((nint hwnd,int message,nint wParam,nint lParam,ref bool handled) => {
            if(messageEvidence.Count<180&&(message>=0x200&&message<=0x215||message==0x2a3))messageEvidence.Add(new { message, wParam=(long)wParam,lParam=(long)lParam,capture=(long)GetCapture(),left=(GetKeyState(1)&0x8000)!=0 });
            return 0;
        });
        app.Dispatcher.InvokeAsync(() => Emit(new { ready = true, result = State() }), DispatcherPriority.ApplicationIdle);
        _ = Task.Run(async () => {
            string? line;
            while((line=await Console.In.ReadLineAsync())!=null) {
                try {
                    using var document=JsonDocument.Parse(line);var root=document.RootElement;
                    var id=root.GetProperty("id").Clone();string? op=root.GetProperty("op").GetString();
                    if(op=="state")Emit(new { id, result=await app.Dispatcher.InvokeAsync(State,DispatcherPriority.ApplicationIdle) });
                    else if(op=="quit") { Emit(new { id, result=new { closing=true } });await app.Dispatcher.InvokeAsync(app.Shutdown);return; }
                    else Emit(new { id, error=new { code="unsupported",message="Fixture supports state and quit only." } });
                }catch(Exception error){Emit(new { error=new { code="fixture_error",message=error.Message } });}
            }
            await app.Dispatcher.InvokeAsync(app.Shutdown);
        });
        app.Run();
    }
}
