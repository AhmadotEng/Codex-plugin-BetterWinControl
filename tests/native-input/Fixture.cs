using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

internal static class Fixture
{
    const uint AppMessage=0x8001;
    static readonly ConcurrentQueue<string> Commands=new();
    static readonly Native.WndProc Procedure=WindowProcedure;
    static readonly Native.WndProc EditProcedure=EditWindowProcedure;
    static readonly List<object> KeyObservations=new();
    static nint MainWindow,Canary,Editor,Dialog,OldEditProcedure,Instructions,PassageFeedback;
    static int Clicks,Doubles,RightClicks,WheelSteps,Moves,HoverEntries,Drags,MenuSelections,DialogClicks,ChordActions,PlainKeyActions,CanaryInputs,EditKeys,BoxX=300,BoxY=80;
    static bool Hovered,Dragging,Pad,InteractivePad,Closing;
    static readonly List<nint> Fonts=new();
    static string LastText="";
    static string LastFeedback="";
    static object? CanaryApi;
    static readonly string ClassName="BetterWinControlAcceptance_"+Environment.ProcessId;
    static readonly object OutputLock=new();

    [STAThread] static int Main(string[] args)
    {
        Pad=args.Contains("--pad")||args.Contains("--interactive-pad");
        InteractivePad=args.Contains("--interactive-pad");
        int watchdogSeconds=60;
        int watchdogIndex=Array.IndexOf(args,"--watchdog-seconds");
        if(watchdogIndex>=0)watchdogSeconds=Math.Clamp(int.Parse(args[watchdogIndex+1]),30,300);
        Native.SetProcessDpiAwarenessContext(new nint(-4));
        var cls=new Native.WndClass{Size=(uint)Marshal.SizeOf<Native.WndClass>(),Style=8,WndProc=Marshal.GetFunctionPointerForDelegate(Procedure),
            Instance=Native.GetModuleHandle(null),Cursor=Native.LoadCursor(0,new nint(32512)),Background=new nint(6),ClassName=ClassName};
        if(Native.RegisterClassEx(ref cls)==0)throw new System.ComponentModel.Win32Exception();
        MainWindow=Create(InteractivePad?"TYPE HERE — Background control co-use":Pad?"AUTOMATED CHECK PAD":"TARGET",0,40,40,620,390);
        int editY=230,editWidth=530,editHeight=75;
        if(InteractivePad)
        {
            if(!Native.SystemParametersInfo(0x30,0,out var workArea,0))
                workArea=new Native.Rect{Right=Native.GetSystemMetrics(0),Bottom=Native.GetSystemMetrics(1)};
            int padWidth=Math.Min(650,Math.Max(460,workArea.Right-workArea.Left-690));
            int padHeight=Math.Min(610,workArea.Bottom-workArea.Top-30);
            int padX=Math.Max(workArea.Left,workArea.Right-padWidth-15),padY=workArea.Top+15;
            // Topmost placement is nonactivating. Only the user's click focuses this pad.
            Native.SetWindowPos(MainWindow,new nint(-1),padX,padY,padWidth,padHeight,0x210);
            Native.GetClientRect(MainWindow,out var client);
            Instructions=Native.CreateWindowEx(0,"STATIC","Waiting for the co-use test. Click the editor below only when instructed.",0x50000000,
                18,16,client.Right-36,Math.Min(280,client.Bottom-190),MainWindow,0,cls.Instance,0);
            var instructionFont=Native.CreateFont(-22,0,0,0,400,0,0,0,1,0,0,5,0,"Segoe UI");Fonts.Add(instructionFont);
            Native.SendMessage(Instructions,0x30,instructionFont,new nint(1));
            int labelY=Math.Min(300,client.Bottom-165);
            var label=Native.CreateWindowEx(0,"STATIC","TYPE HERE ↓  Click the white box",0x50000000,18,labelY,client.Right-36,34,MainWindow,0,cls.Instance,0);
            var labelFont=Native.CreateFont(-24,0,0,0,700,0,0,0,1,0,0,5,0,"Segoe UI");Fonts.Add(labelFont);
            Native.SendMessage(label,0x30,labelFont,new nint(1));
            editY=labelY+40;editWidth=client.Right-40;editHeight=Math.Max(90,client.Bottom-editY-58);
            PassageFeedback=Native.CreateWindowEx(0,"STATIC","Type the displayed phrase; do not press Enter.",0x50000000,
                20,editY+editHeight+10,client.Right-40,34,MainWindow,0,cls.Instance,0);
            var feedbackFont=Native.CreateFont(-18,0,0,0,700,0,0,0,1,0,0,5,0,"Segoe UI");Fonts.Add(feedbackFont);
            Native.SendMessage(PassageFeedback,0x30,feedbackFont,new nint(1));
        }
        if(!Pad)Canary=Create("SAME PROCESS CANARY",0,690,40,330,220);
        Editor=Native.CreateWindowEx(0,"EDIT",Pad?"":"seed",0x50010004|0x00800000,20,editY,editWidth,editHeight,MainWindow,new nint(100),cls.Instance,0);
        if(InteractivePad)
        {
            var editFont=Native.CreateFont(-24,0,0,0,400,0,0,0,1,0,0,5,0,"Segoe UI");Fonts.Add(editFont);
            Native.SendMessage(Editor,0x30,editFont,new nint(1));
        }
        OldEditProcedure=Native.SetWindowLongPtr(Editor,-4,Marshal.GetFunctionPointerForDelegate(EditProcedure));
        Native.ShowWindow(MainWindow,4);if(Canary!=0)Native.ShowWindow(Canary,4);
        Native.SetTimer(MainWindow,1,(uint)watchdogSeconds*1000,0);
        new Thread(()=>{try{string? line;while((line=Console.ReadLine()) is not null){Commands.Enqueue(line);Native.PostMessage(MainWindow!=0?MainWindow:Canary,AppMessage,0,0);}}finally{Native.PostMessage(MainWindow!=0?MainWindow:Canary,0x0010,0,0);}}){IsBackground=true}.Start();
        Emit(new{ready=true,pid=Environment.ProcessId,target=MainWindow.ToInt64(),canary=Canary.ToInt64(),edit=Editor.ToInt64(),pad=Pad});
        while(Native.GetMessage(out var msg,0,0,0)>0){Native.TranslateMessage(ref msg);Native.DispatchMessage(ref msg);}
        foreach(var font in Fonts)if(font!=0)Native.DeleteObject(font);
        return 0;
    }
    static nint Create(string title,nint owner,int x,int y,int width,int height)
    {
        string caption=title.StartsWith("TYPE HERE",StringComparison.Ordinal)?title:"AUTOMATED — DO NOT CLICK — "+title;
        var hwnd=Native.CreateWindowEx(0x80,ClassName,caption,0x00CF0000,x,y,width,height,owner,0,Native.GetModuleHandle(null),0);
        if(hwnd==0)throw new System.ComponentModel.Win32Exception();return hwnd;
    }
    static void Emit(object value){lock(OutputLock)Console.WriteLine(JsonSerializer.Serialize(value));}
    static object State()
    {
        var text=Editor!=0&&Native.IsWindow(Editor)?ReadText(Editor):LastText;
        Native.GetClientRect(MainWindow,out var size);
        return new{pid=Environment.ProcessId,target=MainWindow.ToInt64(),canary=Canary.ToInt64(),edit=Editor.ToInt64(),dialog=Dialog.ToInt64(),
            targetAlive=Native.IsWindow(MainWindow),clicks=Clicks,doubles=Doubles,rightClicks=RightClicks,wheelSteps=WheelSteps,moves=Moves,hoverEntries=HoverEntries,
            hovered=Hovered,drags=Drags,dragging=Dragging,boxX=BoxX,boxY=BoxY,menuSelections=MenuSelections,dialogClicks=DialogClicks,chordActions=ChordActions,
            plainKeyActions=PlainKeyActions,canaryInputs=CanaryInputs,editKeys=EditKeys,text,keyObservations=KeyObservations.ToArray(),clientWidth=size.Right,clientHeight=size.Bottom};
    }
    static string ReadText(nint hwnd){var result=new StringBuilder(4096);Native.GetWindowText(hwnd,result,result.Capacity);return result.ToString();}
    static void DispatchCommands()
    {
        while(Commands.TryDequeue(out var line))
        {
            object? id=null;
            try
            {
                using var document=JsonDocument.Parse(line);var cmd=document.RootElement;
                if(cmd.TryGetProperty("id",out var i))id=i.Clone();var op=cmd.GetProperty("op").GetString();
                if(op=="state"){Emit(new{id,result=State()});continue;}
                if(op=="instructions"&&Pad)
                {
                    Native.SetWindowText(Instructions,cmd.GetProperty("text").GetString()??"");
                    if(cmd.TryGetProperty("title",out var title))Native.SetWindowText(MainWindow,(InteractivePad?"TYPE HERE — ":"AUTOMATED — DO NOT CLICK — ")+(title.GetString()??"Background control co-use test"));
                    Emit(new{id,result=new{updated=true}});continue;
                }
                if(op=="pad_status"&&Pad)
                {
                    // Compare the dedicated disposable passage locally. Never return its contents.
                    string actual=ReadText(Editor).Replace("\r\n","\n");
                    string expected=cmd.TryGetProperty("expected",out var e)?e.GetString()??"":"";
                    if(InteractivePad&&PassageFeedback!=0)
                    {
                        string feedback=string.IsNullOrEmpty(expected)?"Waiting for the test phrase.":actual==expected?"PHRASE MATCHES — ready":
                            actual.Length==0?"Type the displayed phrase; do not press Enter.":expected.StartsWith(actual,StringComparison.Ordinal)?
                            $"Matching so far: {actual.Length} / {expected.Length}":"NOT MATCHED — check spaces, case, or Enter.";
                        if(feedback!=LastFeedback){Native.SetWindowText(PassageFeedback,feedback);LastFeedback=feedback;}
                    }
                    Emit(new{id,result=new{textLength=actual.Length,exactPassage=actual==expected,editKeys=EditKeys,
                        editorFocused=Native.GetFocus()==Editor,foreground=Native.GetForegroundWindow()==MainWindow}});continue;
                }
                if(op=="canary_probe")
                {
                    Native.SendMessage(Canary,AppMessage+1,0,0);Emit(new{id,result=CanaryApi});continue;
                }
                if(op=="close_target")
                {
                    LastText=ReadText(Editor);Native.DestroyWindow(MainWindow);MainWindow=0;Editor=0;
                    Emit(new{id,result=new{closed=true}});continue;
                }
                if(op=="reset")
                {
                    Clicks=Doubles=RightClicks=WheelSteps=Moves=HoverEntries=Drags=MenuSelections=DialogClicks=ChordActions=PlainKeyActions=CanaryInputs=EditKeys=0;
                    Hovered=Dragging=false;BoxX=300;BoxY=80;KeyObservations.Clear();Native.SetWindowText(Editor,Pad?"":"seed");
                    Native.InvalidateRect(MainWindow,0,true);Emit(new{id,result=State()});continue;
                }
                if(op=="cover")
                {
                    var cover=Create("OWN OCCLUSION COVER",0,35,35,635,410);Native.ShowWindow(cover,4);
                    Native.SetWindowPos(cover,new nint(-1),0,0,0,0,0x13);Emit(new{id,result=new{cover=cover.ToInt64()}});continue;
                }
                if(op=="shutdown")
                {
                    Emit(new{id,result=new{closing=true}});Closing=true;
                    Native.EnumThreadWindows(Native.GetCurrentThreadId(),(h,_)=>{Native.DestroyWindow(h);return true;},0);Native.PostQuitMessage(0);return;
                }
                Emit(new{id,error="unsupported fixture operation"});
            }
            catch(Exception ex){Emit(new{id,error=ex.Message});}
        }
    }
    static nint WindowProcedure(nint hwnd,uint message,nint w,nint l)
    {
        if(message==AppMessage){DispatchCommands();return 0;}
        if(hwnd==Canary&&message==AppMessage+1)
        {
            Native.GetCursorPos(out var point);
            CanaryApi=new{controlDown=(Native.GetKeyState(0x11)&0x8000)!=0,shiftDown=(Native.GetKeyState(0x10)&0x8000)!=0,
                asyncControlDown=(Native.GetAsyncKeyState(0x11)&0x8000)!=0,asyncShiftDown=(Native.GetAsyncKeyState(0x10)&0x8000)!=0,
                focus=Native.GetFocus().ToInt64(),capture=Native.GetCapture().ToInt64(),cursorX=point.X,cursorY=point.Y};return 0;
        }
        if(message==0x113){Native.PostQuitMessage(4);return 0;}
        if(message==0x10){if(!Closing){Closing=true;Native.PostQuitMessage(0);}return Native.DefWindowProc(hwnd,message,w,l);}
        bool input=message is >=0x200 and <=0x20E or >=0x100 and <=0x109;
        if(hwnd==Canary&&input){CanaryInputs++;return Native.DefWindowProc(hwnd,message,w,l);}
        if(hwnd==Dialog)
        {
            if(message==0x202){DialogClicks++;Native.DestroyWindow(Dialog);Dialog=0;return 0;}
            return Native.DefWindowProc(hwnd,message,w,l);
        }
        if(hwnd!=MainWindow||Pad)return Native.DefWindowProc(hwnd,message,w,l);
        int x=unchecked((short)(l.ToInt64()&0xFFFF)),y=unchecked((short)((l.ToInt64()>>16)&0xFFFF));
        switch(message)
        {
            case 0x200:
                Moves++;bool hover=x>=20&&x<160&&y>=20&&y<70;if(hover&&!Hovered)HoverEntries++;Hovered=hover;
                if(Dragging){BoxX=x-20;BoxY=y-20;Native.InvalidateRect(hwnd,0,true);}return 0;
            case 0x201:
                if(x>=BoxX&&x<BoxX+60&&y>=BoxY&&y<BoxY+60){Dragging=true;Native.SetCapture(hwnd);}return 0;
            case 0x202:
                if(Dragging){Drags++;Dragging=false;Native.ReleaseCapture();}
                else if(x>=20&&x<160&&y>=20&&y<70)Clicks++;
                else if(x>=180&&x<290&&y>=20&&y<70)
                {
                    Dialog=Create("OWNED DIALOG",MainWindow,80,80,260,180);Native.ShowWindow(Dialog,4);
                }
                Native.InvalidateRect(hwnd,0,true);return 0;
            case 0x203:Doubles++;Native.InvalidateRect(hwnd,0,true);return 0;
            case 0x205:
                RightClicks++;
                if(x>=20&&x<160&&y>=100&&y<150)
                {
                    var menu=Native.CreatePopupMenu();Native.AppendMenu(menu,0,1,"Acceptance item");
                    var point=new Native.Point{X=x,Y=y};Native.ClientToScreen(hwnd,ref point);
                    if(Native.TrackPopupMenu(menu,0x100|0x80,point.X,point.Y,0,hwnd,0)==1)MenuSelections++;
                    Native.DestroyMenu(menu);
                }
                return 0;
            case 0x20A:WheelSteps+=unchecked((short)((w.ToInt64()>>16)&0xFFFF))/120;Native.InvalidateRect(hwnd,0,true);return 0;
            case 0x100:case 0x104:
                int vk=w.ToInt32();bool control=(Native.GetKeyState(0x11)&0x8000)!=0;
                KeyObservations.Add(new{vk,down=true,reportedDown=(Native.GetKeyState(vk)&0x8000)!=0,control,shift=(Native.GetKeyState(0x10)&0x8000)!=0,alt=(Native.GetKeyState(0x12)&0x8000)!=0,
                    asyncControl=(Native.GetAsyncKeyState(0x11)&0x8000)!=0,asyncShift=(Native.GetAsyncKeyState(0x10)&0x8000)!=0});
                if(vk==0x4B&&control)ChordActions++;
                if(vk==0x77&&!control&&(Native.GetKeyState(0x10)&0x8000)==0&&(Native.GetKeyState(0x12)&0x8000)==0)PlainKeyActions++;
                return 0;
            case 0x101:case 0x105:
                KeyObservations.Add(new{vk=w.ToInt32(),down=false,reportedDown=(Native.GetKeyState(w.ToInt32())&0x8000)!=0});return 0;
            case 0x0F:
                var dc=Native.BeginPaint(hwnd,out var paint);Native.GetClientRect(hwnd,out var rect);Native.FillRect(dc,ref rect,new nint(6));
                Draw(dc,20,20,160,70,Hovered?0x70D090u:0xD0D0D0u);Draw(dc,180,20,290,70,0xF0D0B0);Draw(dc,BoxX,BoxY,BoxX+60,BoxY+60,0xD08030);Draw(dc,20,100,160,150,0xA0C0E0);
                var text=$"click={Clicks} double={Doubles} hover={HoverEntries} wheel={WheelSteps} drag={Drags} chord={ChordActions}";
                Native.TextOut(dc,20,180,text,text.Length);Native.EndPaint(hwnd,ref paint);return 0;
        }
        return Native.DefWindowProc(hwnd,message,w,l);
    }
    static void Draw(nint dc,int x,int y,int right,int bottom,uint color){var brush=Native.CreateSolidBrush(color);var r=new Native.Rect{Left=x,Top=y,Right=right,Bottom=bottom};Native.FillRect(dc,ref r,brush);Native.DeleteObject(brush);}
    static nint EditWindowProcedure(nint hwnd,uint message,nint w,nint l)
    {
        if(message is >=0x100 and <=0x109)EditKeys++;
        return Native.CallWindowProc(OldEditProcedure,hwnd,message,w,l);
    }
}
internal static class Native
{
    internal delegate nint WndProc(nint h,uint m,nint w,nint l);
    internal delegate bool EnumWindowsProc(nint h,nint l);
    [StructLayout(LayoutKind.Sequential,CharSet=CharSet.Unicode)]internal struct WndClass{public uint Size,Style;public nint WndProc;public int ClassExtra,WindowExtra;public nint Instance,Icon,Cursor,Background;public string? MenuName;public string ClassName;public nint SmallIcon;}
    [StructLayout(LayoutKind.Sequential)]internal struct Point{public int X,Y;}
    [StructLayout(LayoutKind.Sequential)]internal struct Rect{public int Left,Top,Right,Bottom;}
    [StructLayout(LayoutKind.Sequential)]internal struct Message{public nint Hwnd;public uint Value;public nint WParam,LParam;public uint Time;public Point Point;public uint Private;}
    [StructLayout(LayoutKind.Sequential)]internal struct Paint{public nint DC;public int Erase;public Rect Rect;public int Restore,Incremental;[MarshalAs(UnmanagedType.ByValArray,SizeConst=32)]public byte[] Reserved;}
    [DllImport("kernel32.dll",CharSet=CharSet.Unicode)]internal static extern nint GetModuleHandle(string? name);
    [DllImport("kernel32.dll")]internal static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")]internal static extern bool SetProcessDpiAwarenessContext(nint value);
    [DllImport("user32.dll")]internal static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")]internal static extern bool SystemParametersInfo(uint action,uint parameter,out Rect rectangle,uint flags);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern ushort RegisterClassEx(ref WndClass cls);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern nint CreateWindowEx(uint ex,string cls,string text,uint style,int x,int y,int width,int height,nint parent,nint menu,nint instance,nint param);
    [DllImport("user32.dll")]internal static extern nint LoadCursor(nint instance,nint resource);
    [DllImport("user32.dll")]internal static extern bool ShowWindow(nint hwnd,int command);
    [DllImport("user32.dll")]internal static extern bool SetWindowPos(nint hwnd,nint after,int x,int y,int width,int height,uint flags);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern nint DefWindowProc(nint h,uint m,nint w,nint l);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern nint CallWindowProc(nint proc,nint h,uint m,nint w,nint l);
    [DllImport("user32.dll",EntryPoint="SetWindowLongPtrW")]internal static extern nint SetWindowLongPtr(nint h,int index,nint value);
    [DllImport("user32.dll")]internal static extern bool PostMessage(nint hwnd,uint msg,nint w,nint l);
    [DllImport("user32.dll",EntryPoint="SendMessageW")]internal static extern nint SendMessage(nint hwnd,uint msg,nint w,nint l);
    [DllImport("user32.dll")]internal static extern void PostQuitMessage(int code);
    [DllImport("user32.dll")]internal static extern int GetMessage(out Message msg,nint hwnd,uint min,uint max);
    [DllImport("user32.dll")]internal static extern bool TranslateMessage(ref Message msg);
    [DllImport("user32.dll")]internal static extern nint DispatchMessage(ref Message msg);
    [DllImport("user32.dll")]internal static extern bool DestroyWindow(nint hwnd);
    [DllImport("user32.dll")]internal static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")]internal static extern bool EnumThreadWindows(uint tid,EnumWindowsProc callback,nint param);
    [DllImport("user32.dll")]internal static extern nint SetTimer(nint hwnd,nuint id,uint time,nint callback);
    [DllImport("user32.dll")]internal static extern bool GetClientRect(nint hwnd,out Rect rect);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern int GetWindowText(nint hwnd,StringBuilder text,int count);
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern bool SetWindowText(nint hwnd,string text);
    [DllImport("user32.dll")]internal static extern nint SetCapture(nint hwnd);
    [DllImport("user32.dll")]internal static extern bool ReleaseCapture();
    [DllImport("user32.dll")]internal static extern short GetKeyState(int vk);
    [DllImport("user32.dll")]internal static extern short GetAsyncKeyState(int vk);
    [DllImport("user32.dll")]internal static extern nint GetForegroundWindow();
    [DllImport("user32.dll")]internal static extern nint GetFocus();
    [DllImport("user32.dll")]internal static extern nint GetCapture();
    [DllImport("user32.dll")]internal static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")]internal static extern bool ClientToScreen(nint hwnd,ref Point point);
    [DllImport("user32.dll")]internal static extern nint CreatePopupMenu();
    [DllImport("user32.dll",CharSet=CharSet.Unicode)]internal static extern bool AppendMenu(nint menu,uint flags,nuint id,string text);
    [DllImport("user32.dll")]internal static extern uint TrackPopupMenu(nint menu,uint flags,int x,int y,int reserved,nint hwnd,nint rect);
    [DllImport("user32.dll")]internal static extern bool DestroyMenu(nint menu);
    [DllImport("user32.dll")]internal static extern nint BeginPaint(nint hwnd,out Paint paint);
    [DllImport("user32.dll")]internal static extern bool EndPaint(nint hwnd,ref Paint paint);
    [DllImport("user32.dll")]internal static extern bool FillRect(nint dc,ref Rect rect,nint brush);
    [DllImport("user32.dll")]internal static extern bool InvalidateRect(nint hwnd,nint rect,bool erase);
    [DllImport("gdi32.dll")]internal static extern nint CreateSolidBrush(uint color);
    [DllImport("gdi32.dll",CharSet=CharSet.Unicode)]internal static extern nint CreateFont(int height,int width,int escapement,int orientation,int weight,uint italic,uint underline,uint strikeout,uint charSet,uint outputPrecision,uint clipPrecision,uint quality,uint pitch,string face);
    [DllImport("gdi32.dll")]internal static extern bool DeleteObject(nint value);
    [DllImport("gdi32.dll",CharSet=CharSet.Unicode)]internal static extern bool TextOut(nint dc,int x,int y,string text,int len);
}
