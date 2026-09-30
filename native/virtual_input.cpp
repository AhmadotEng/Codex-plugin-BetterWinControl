#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include "wire.h"
#include "vendor/minhook-1.3.4/include/MinHook.h"
#include <atomic>
#include <array>
#include <cstring>
#include <algorithm>
#include <string>
#include <vector>
#include <windowsx.h>
#include "unload_guard.h"

namespace {
using namespace bwc;
HMODULE g_module=nullptr;
struct Session {
    Config config{};
    HWND root=nullptr, focus=nullptr, capture=nullptr, hover=nullptr;
    HANDLE pipe=INVALID_HANDLE_VALUE, host=nullptr;
    HANDLE revokeEvent=nullptr, monitor=nullptr, targetThread=nullptr;
    std::atomic<bool> monitorDone{false};
    HHOOK before=nullptr, after=nullptr;
    UINT dispatchMessage=0;
    std::atomic<bool> enabled{false}, pending{false};
    std::atomic<bool> destroyed{false};
    std::atomic<uint32_t> dispatching{0};
    Command command{};
    Reply reply{};
    POINT pointer{};
    RECT clip{};
    bool clipped=false;
    std::array<BYTE,256> keys{};
    std::array<bool,256> asyncPressed{};
    std::array<HWND,64> leaveTracking{};
    uint32_t buttons=0;
    bool hooksBarrier=false,everThreadHook=false;
    const char* unloadReason="Quiescence has not been established.";
};
std::array<CodeRange,64> g_codeRanges{};
size_t g_codeRangeCount=0;
std::atomic<Session*> g_session{nullptr};
std::atomic<uint32_t> g_callbacks{0};
struct Count { Count(){g_callbacks.fetch_add(1);} ~Count(){g_callbacks.fetch_sub(1);} };
thread_local Session* tlsSession=nullptr;
thread_local HWND tlsWindow=nullptr;
thread_local HWND tlsStack[128]{};
thread_local unsigned tlsDepth=0;
thread_local bool tlsReleasing=false;

bool Identity(Session* s) {
    DWORD pid=0;
    return !s->destroyed.load() && IsWindow(s->root) && GetWindowThreadProcessId(s->root,&pid)==s->config.targetThread && pid==s->config.targetPid;
}
bool Scope(Session* s, HWND hwnd) {
    if(!hwnd || !IsWindow(hwnd)) return false;
    DWORD pid=0; GetWindowThreadProcessId(hwnd,&pid);
    if(pid!=s->config.targetPid) return false;
    if(hwnd==s->root || IsChild(s->root,hwnd)) return true;
    HWND top=GetAncestor(hwnd,GA_ROOT);
    for(int i=0;top && i<32;i++,top=GetWindow(top,GW_OWNER)) if(top==s->root) return true;
    return false;
}
Session* Virtual() {
    auto s=tlsSession;
    // Keep physical-input APIs intercepted until an in-flight synthetic callback returns,
    // even after cancellation. Falling through here on cancellation could move the real cursor.
    return s && Scope(s,tlsWindow) ? s : nullptr;
}
bool SameThread(Session* s,HWND hwnd) { return GetWindowThreadProcessId(hwnd,nullptr)==s->config.targetThread; }
void Fail(Reply& r,Error e,const char* text){r.error=e;strncpy_s(r.message,text,_TRUNCATE);}
void Snapshot(Session* s,Reply& r){
    r.x=s->pointer.x;r.y=s->pointer.y;r.focus=(uint64_t)(uintptr_t)s->focus;r.capture=(uint64_t)(uintptr_t)s->capture;
    memcpy(r.keys,s->keys.data(),256);r.buttons=s->buttons;r.revoked=!s->enabled.load();
}
POINT ScreenPoint(Session* s){POINT p=s->pointer;ClientToScreen(s->root,&p);return p;}
HWND Hit(Session* s,POINT screen){
    HWND current=s->root;
    for(int i=0;i<32;i++){
        POINT point=screen;ScreenToClient(current,&point);
        HWND next=ChildWindowFromPointEx(current,point,CWP_SKIPINVISIBLE|CWP_SKIPDISABLED|CWP_SKIPTRANSPARENT);
        if(!next || next==current || !Scope(s,next)) break;
        current=next;
    }
    return current;
}
LRESULT Deliver(Session* s,HWND target,UINT message,WPARAM w,LPARAM l){
    if((!s->enabled.load()&&!tlsReleasing) || !Scope(s,target) || !SameThread(s,target)) return 0;
    HWND old=tlsWindow;tlsWindow=target;
    LRESULT result=SendMessageW(target,message,w,l);
    tlsWindow=old;
    return result;
}
void Focus(Session* s,HWND hwnd){
    if(s->focus==hwnd) return;
    HWND previous=s->focus;s->focus=hwnd;
    if(previous && Scope(s,previous)) Deliver(s,previous,WM_KILLFOCUS,(WPARAM)hwnd,0);
    if(hwnd) Deliver(s,hwnd,WM_SETFOCUS,(WPARAM)previous,0);
}
unsigned MouseFlags(Session* s){
    unsigned flags=0;
    if(s->keys[VK_CONTROL]&0x80)flags|=MK_CONTROL;
    if(s->keys[VK_SHIFT]&0x80)flags|=MK_SHIFT;
    if(s->buttons&1)flags|=MK_LBUTTON;
    if(s->buttons&2)flags|=MK_RBUTTON;
    if(s->buttons&4)flags|=MK_MBUTTON;
    return flags;
}
void Clear(Session* s){s->keys.fill(0);s->asyncPressed.fill(false);s->buttons=0;s->capture=nullptr;s->clipped=false;}
void EndPointerGesture(Session* s){
    // Some built-in controls release capture through an internal kernel path
    // after consulting the real queue's (empty) capture state. Complete our
    // synthetic pointer gesture explicitly when its last button is released.
    if(!s->buttons&&s->capture){HWND previous=s->capture;s->capture=nullptr;Deliver(s,previous,WM_CAPTURECHANGED,0,0);}
}
void Leave(Session* s,HWND hwnd){
    for(auto& tracked:s->leaveTracking)if(tracked==hwnd)tracked=nullptr;
    Deliver(s,hwnd,WM_MOUSELEAVE,0,0);
}

decltype(&GetCursorPos) realGetCursorPos;
decltype(&SetCursorPos) realSetCursorPos;
decltype(&GetKeyState) realGetKeyState;
decltype(&GetAsyncKeyState) realGetAsyncKeyState;
decltype(&GetKeyboardState) realGetKeyboardState;
decltype(&GetFocus) realGetFocus;
decltype(&SetFocus) realSetFocus;
decltype(&GetCapture) realGetCapture;
decltype(&SetCapture) realSetCapture;
decltype(&ReleaseCapture) realReleaseCapture;
decltype(&GetForegroundWindow) realGetForegroundWindow;
decltype(&GetActiveWindow) realGetActiveWindow;
decltype(&SetActiveWindow) realSetActiveWindow;
decltype(&SetForegroundWindow) realSetForegroundWindow;
decltype(&ClipCursor) realClipCursor;
decltype(&GetClipCursor) realGetClipCursor;
decltype(&WindowFromPoint) realWindowFromPoint;
decltype(&TrackMouseEvent) realTrackMouseEvent;
using NativeWindowSetter=HWND (WINAPI*)(HWND);
using NativeForegroundSetter=BOOL (WINAPI*)(HWND);
using NativeCursorSetter=BOOL (WINAPI*)(LONG,LONG);
NativeWindowSetter realNtSetCapture,realNtSetFocus,realNtSetActiveWindow;
NativeForegroundSetter realNtSetForegroundWindow;
NativeCursorSetter realNtSetCursorPos;

BOOL WINAPI HookGetCursorPos(LPPOINT p){Count n;if(auto s=Virtual()){if(!p)return FALSE;*p=ScreenPoint(s);return TRUE;}return realGetCursorPos(p);}
BOOL WINAPI HookSetCursorPos(int x,int y){Count n;if(auto s=Virtual()){POINT p{x,y};ScreenToClient(s->root,&p);s->pointer=p;return TRUE;}return realSetCursorPos(x,y);}
SHORT WINAPI HookGetKeyState(int vk){Count n;if(auto s=Virtual()){if(!s->enabled.load()&&!tlsReleasing)return 0;return vk>=0&&vk<256 ? (SHORT)(((s->keys[vk]&0x80)?0x8000:0)|(s->keys[vk]&1)) : 0;}return realGetKeyState(vk);}
SHORT WINAPI HookGetAsyncKeyState(int vk){Count n;if(auto s=Virtual()){if(vk<0||vk>255||(!s->enabled.load()&&!tlsReleasing))return 0;SHORT r=(SHORT)(((s->keys[vk]&0x80)?0x8000:0)|(s->asyncPressed[vk]?1:0));s->asyncPressed[vk]=false;return r;}return realGetAsyncKeyState(vk);}
BOOL WINAPI HookGetKeyboardState(PBYTE p){Count n;if(auto s=Virtual()){if(!p)return FALSE;if(!s->enabled.load()&&!tlsReleasing)memset(p,0,256);else memcpy(p,s->keys.data(),256);return TRUE;}return realGetKeyboardState(p);}
HWND WINAPI HookGetFocus(){Count n;if(auto s=Virtual())return s->focus;return realGetFocus();}
HWND WINAPI HookSetFocus(HWND h){Count n;if(auto s=Virtual()){HWND old=s->focus;if(!h || (Scope(s,h)&&SameThread(s,h)))Focus(s,h);return old;}return realSetFocus(h);}
HWND WINAPI HookGetCapture(){Count n;if(auto s=Virtual())return s->capture;return realGetCapture();}
HWND WINAPI HookSetCapture(HWND h){Count n;if(auto s=Virtual()){HWND old=s->capture;if(Scope(s,h)&&SameThread(s,h))s->capture=h;return old;}return realSetCapture(h);}
BOOL WINAPI HookReleaseCapture(){Count n;if(auto s=Virtual()){HWND old=s->capture;s->capture=nullptr;if(old)Deliver(s,old,WM_CAPTURECHANGED,0,0);return TRUE;}return realReleaseCapture();}
HWND WINAPI HookGetForegroundWindow(){Count n;if(auto s=Virtual())return s->root;return realGetForegroundWindow();}
HWND WINAPI HookGetActiveWindow(){Count n;if(auto s=Virtual())return s->root;return realGetActiveWindow();}
HWND WINAPI HookSetActiveWindow(HWND h){Count n;if(auto s=Virtual())return s->root;return realSetActiveWindow(h);}
BOOL WINAPI HookSetForegroundWindow(HWND h){Count n;if(auto s=Virtual())return Scope(s,h);return realSetForegroundWindow(h);}
BOOL WINAPI HookClipCursor(const RECT* r){Count n;if(auto s=Virtual()){s->clipped=r!=nullptr;if(r)s->clip=*r;return TRUE;}return realClipCursor(r);}
BOOL WINAPI HookGetClipCursor(LPRECT r){Count n;if(auto s=Virtual()){if(!r)return FALSE;if(s->clipped)*r=s->clip;else GetWindowRect(s->root,r);return TRUE;}return realGetClipCursor(r);}
HWND WINAPI HookWindowFromPoint(POINT p){Count n;if(auto s=Virtual())return Hit(s,p);return realWindowFromPoint(p);}
BOOL WINAPI HookTrackMouseEvent(LPTRACKMOUSEEVENT request){
    Count n;auto s=Virtual();if(!s)return realTrackMouseEvent(request);
    if(!request||request->cbSize!=sizeof(TRACKMOUSEEVENT)){SetLastError(ERROR_INVALID_PARAMETER);return FALSE;}
    // A real TME_LEAVE request immediately posts WM_MOUSELEAVE when the user's
    // physical pointer is elsewhere. Keep synthetic registrations independent.
    if(request->dwFlags&TME_QUERY){
        HWND tracked=nullptr;for(auto h:s->leaveTracking)if(h&&(!request->hwndTrack||request->hwndTrack==h)){tracked=h;break;}
        request->hwndTrack=tracked;request->dwFlags=tracked?TME_LEAVE:0;request->dwHoverTime=0;return TRUE;
    }
    if(!Scope(s,request->hwndTrack)||!SameThread(s,request->hwndTrack)){SetLastError(ERROR_ACCESS_DENIED);return FALSE;}
    if(request->dwFlags&~(TME_LEAVE|TME_CANCEL)){SetLastError(ERROR_NOT_SUPPORTED);return FALSE;}
    if(request->dwFlags&TME_CANCEL){for(auto& h:s->leaveTracking)if(h==request->hwndTrack)h=nullptr;return TRUE;}
    if(!(request->dwFlags&TME_LEAVE))return TRUE;
    for(auto h:s->leaveTracking)if(h==request->hwndTrack)return TRUE;
    for(auto& h:s->leaveTracking)if(!h){h=request->hwndTrack;return TRUE;}
    SetLastError(ERROR_NOT_ENOUGH_MEMORY);return FALSE;
}
// Standard USER32 controls can call WIN32U directly, bypassing the public setter
// exports. These five same-signature user-mode entry points guard that path too.
// No syscall numbers, kernel hooks, privilege changes, or direct syscalls are used.
HWND WINAPI HookNtSetCapture(HWND h){Count n;if(auto s=Virtual()){HWND old=s->capture;if(!h||(Scope(s,h)&&SameThread(s,h)))s->capture=h;return old;}return realNtSetCapture(h);}
HWND WINAPI HookNtSetFocus(HWND h){Count n;if(auto s=Virtual()){HWND old=s->focus;if(!h||(Scope(s,h)&&SameThread(s,h)))Focus(s,h);return old;}return realNtSetFocus(h);}
HWND WINAPI HookNtSetActiveWindow(HWND h){Count n;if(auto s=Virtual())return s->root;return realNtSetActiveWindow(h);}
BOOL WINAPI HookNtSetForegroundWindow(HWND h){Count n;if(auto s=Virtual())return Scope(s,h);return realNtSetForegroundWindow(h);}
BOOL WINAPI HookNtSetCursorPos(LONG x,LONG y){Count n;if(auto s=Virtual()){if(s->enabled.load()){POINT p{x,y};ScreenToClient(s->root,&p);s->pointer=p;}return TRUE;}return realNtSetCursorPos(x,y);}

bool RememberTrampoline(void* address){
    MEMORY_BASIC_INFORMATION memory{};if(!VirtualQuery(address,&memory,sizeof(memory)))return false;
    CodeRange range{(uintptr_t)memory.BaseAddress,(uintptr_t)memory.BaseAddress+memory.RegionSize};
    for(size_t i=0;i<g_codeRangeCount;i++)if(g_codeRanges[i].begin==range.begin)return true;
    if(g_codeRangeCount==g_codeRanges.size())return false;g_codeRanges[g_codeRangeCount++]=range;return true;
}
bool InstallHooks(){
    auto dos=(PIMAGE_DOS_HEADER)g_module;auto nt=(PIMAGE_NT_HEADERS)((BYTE*)g_module+dos->e_lfanew);
    g_codeRangeCount=1;g_codeRanges[0]={(uintptr_t)g_module,(uintptr_t)g_module+nt->OptionalHeader.SizeOfImage};
    if(MH_Initialize()!=MH_OK)return false;
    HMODULE user=GetModuleHandleW(L"user32.dll");
#define ADD(name) if(MH_CreateHook((LPVOID)GetProcAddress(user,#name),(LPVOID)&Hook##name,(LPVOID*)&real##name)!=MH_OK||!RememberTrampoline((void*)real##name))return false;
    ADD(GetCursorPos) ADD(SetCursorPos) ADD(GetKeyState) ADD(GetAsyncKeyState) ADD(GetKeyboardState)
    ADD(GetFocus) ADD(SetFocus) ADD(GetCapture) ADD(SetCapture) ADD(ReleaseCapture)
    ADD(GetForegroundWindow) ADD(GetActiveWindow) ADD(SetActiveWindow) ADD(SetForegroundWindow)
    ADD(ClipCursor) ADD(GetClipCursor) ADD(WindowFromPoint) ADD(TrackMouseEvent)
#undef ADD
    HMODULE native=GetModuleHandleW(L"win32u.dll");
#define NATIVE(name) if(!native||MH_CreateHook((LPVOID)GetProcAddress(native,"NtUser" #name),(LPVOID)&HookNt##name,(LPVOID*)&realNt##name)!=MH_OK||!RememberTrampoline((void*)realNt##name))return false;
    NATIVE(SetCapture) NATIVE(SetFocus) NATIVE(SetActiveWindow) NATIVE(SetForegroundWindow) NATIVE(SetCursorPos)
#undef NATIVE
    return MH_EnableHook(MH_ALL_HOOKS)==MH_OK;
}
unsigned SpecificKey(unsigned vk){return vk==VK_SHIFT?VK_LSHIFT:vk==VK_CONTROL?VK_LCONTROL:vk==VK_MENU?VK_LMENU:vk;}
unsigned MessageKey(unsigned vk){return vk==VK_LSHIFT||vk==VK_RSHIFT?VK_SHIFT:vk==VK_LCONTROL||vk==VK_RCONTROL?VK_CONTROL:vk==VK_LMENU||vk==VK_RMENU?VK_MENU:vk;}
void UpdateKey(Session* s,unsigned vk,bool down){
    // Generic modifier commands operate the left key. Aggregate state still
    // reflects either side, so releasing generic Ctrl cannot release right Ctrl.
    vk=SpecificKey(vk);
    bool previous=(s->keys[vk]&0x80)!=0;
    BYTE toggle=s->keys[vk]&1;
    if(down&&!previous&&(vk==VK_CAPITAL||vk==VK_NUMLOCK||vk==VK_SCROLL))toggle^=1;
    s->keys[vk]=(down?0x80:0)|toggle;
    if(down)s->asyncPressed[vk]=true;
    unsigned aggregate=MessageKey(vk);
    if(aggregate!=vk){
        unsigned left=SpecificKey(aggregate);
        s->keys[aggregate]=(s->keys[left]|s->keys[left+1])&0x80;
        if(down)s->asyncPressed[aggregate]=true;
    }
}
bool Extended(unsigned vk){return vk==VK_RCONTROL||vk==VK_RMENU||(vk>=VK_PRIOR&&vk<=VK_DOWN)||vk==VK_INSERT||vk==VK_DELETE||vk==VK_DIVIDE||vk==VK_NUMLOCK;}
void SendKey(Session* s,HWND keyboard,unsigned requested,bool down,bool translate){
    unsigned vk=SpecificKey(requested),messageVk=MessageKey(vk);
    bool previous=(s->keys[vk]&0x80)!=0;
    bool alt=(s->keys[VK_MENU]&0x80)!=0||messageVk==VK_MENU;
    HKL layout=GetKeyboardLayout(s->config.targetThread);
    unsigned scan=MapVirtualKeyExW(vk,MAPVK_VK_TO_VSC_EX,layout);
    LPARAM bits=1|((scan&255)<<16)|(Extended(vk)?(1<<24):0)|(alt?(1<<29):0)|(previous?(1u<<30):0)|(!down?(1u<<31):0);
    UpdateKey(s,vk,down);
    Deliver(s,keyboard,down?(alt?WM_SYSKEYDOWN:WM_KEYDOWN):(alt?WM_SYSKEYUP:WM_KEYUP),messageVk,bits);
    if(translate&&down&&!(s->keys[VK_CONTROL]&0x80)&&!alt){
        WCHAR chars[8]{};int count=ToUnicodeEx(messageVk,scan&255,s->keys.data(),chars,8,4,layout);
        if(count>0)for(int i=0;i<std::min(count,8);i++)Deliver(s,keyboard,WM_CHAR,chars[i],bits);
    }
}
void Dispatch(Session* s){
    Command& c=s->command;Reply& r=s->reply;r={};r.sequence=c.sequence;
    if((!s->enabled.load()&&c.op!=Op::Release)||!Identity(s)){Fail(r,Error::Revoked,"Target identity revoked.");return;}
    if(c.hasPoint && (c.op==Op::Button||c.op==Op::DoubleClick||c.op==Op::Wheel)){
        Op original=c.op;c.op=Op::Move;Dispatch(s);c.op=original;
        if(r.error!=Error::None)return;
        r={};r.sequence=c.sequence;
    }
    HWND keyboard=(s->focus&&Scope(s,s->focus))?s->focus:s->root;
    if(!SameThread(s,keyboard)){Fail(r,Error::Unsupported,"Cross-thread virtual focus is not implemented.");return;}
    if(c.op==Op::Move){
        RECT rect{};GetClientRect(s->root,&rect);
        if(!s->capture && (c.x<0||c.y<0||c.x>=rect.right||c.y>=rect.bottom)){Fail(r,Error::OutOfScope,"Pointer lies outside selected client bounds without virtual capture.");return;}
        s->pointer={c.x,c.y};
        if(s->clipped){POINT p=ScreenPoint(s);p.x=std::clamp(p.x,s->clip.left,s->clip.right-1);p.y=std::clamp(p.y,s->clip.top,s->clip.bottom-1);ScreenToClient(s->root,&p);s->pointer=p;}
        POINT screen=ScreenPoint(s);HWND target=s->capture?s->capture:Hit(s,screen);
        if(!SameThread(s,target)){Fail(r,Error::Unsupported,"Cross-thread pointer recipient is not implemented.");return;}
        if(s->hover!=target){if(s->hover)Leave(s,s->hover);s->hover=target;}
        POINT local=screen;ScreenToClient(target,&local);Deliver(s,target,WM_MOUSEMOVE,MouseFlags(s),MAKELPARAM(local.x,local.y));r.delivered=1;
    } else if(c.op==Op::Button){
        if(c.button>2){Fail(r,Error::InvalidCommand,"Unknown button.");return;}
        POINT screen=ScreenPoint(s);HWND target=s->capture?s->capture:Hit(s,screen);
        if(!SameThread(s,target)){Fail(r,Error::Unsupported,"Cross-thread pointer recipient is not implemented.");return;}
        static const unsigned vks[]={VK_LBUTTON,VK_RBUTTON,VK_MBUTTON};
        static const UINT downs[]={WM_LBUTTONDOWN,WM_RBUTTONDOWN,WM_MBUTTONDOWN};
        static const UINT ups[]={WM_LBUTTONUP,WM_RBUTTONUP,WM_MBUTTONUP};
        unsigned mask=1u<<c.button;if(c.down)s->buttons|=mask;else s->buttons&=~mask;
        UpdateKey(s,vks[c.button],c.down!=0);
        if(c.down)Focus(s,target);
        POINT local=screen;ScreenToClient(target,&local);Deliver(s,target,c.down?downs[c.button]:ups[c.button],MouseFlags(s),MAKELPARAM(local.x,local.y));if(!c.down)EndPointerGesture(s);r.delivered=1;
    } else if(c.op==Op::DoubleClick){
        if(c.button>2){Fail(r,Error::InvalidCommand,"Unknown button.");return;}
        POINT screen=ScreenPoint(s);HWND target=s->capture?s->capture:Hit(s,screen);
        if(!SameThread(s,target)){Fail(r,Error::Unsupported,"Cross-thread pointer recipient is not implemented.");return;}
        static const unsigned vks[]={VK_LBUTTON,VK_RBUTTON,VK_MBUTTON};
        static const UINT downs[]={WM_LBUTTONDOWN,WM_RBUTTONDOWN,WM_MBUTTONDOWN};
        static const UINT doubles[]={WM_LBUTTONDBLCLK,WM_RBUTTONDBLCLK,WM_MBUTTONDBLCLK};
        static const UINT ups[]={WM_LBUTTONUP,WM_RBUTTONUP,WM_MBUTTONUP};
        POINT local=screen;ScreenToClient(target,&local);Focus(s,target);
        for(unsigned phase=0;phase<4;phase++){
            bool down=phase==0||phase==2;unsigned mask=1u<<c.button;
            if(down)s->buttons|=mask;else s->buttons&=~mask;UpdateKey(s,vks[c.button],down);
            Deliver(s,target,phase==0?downs[c.button]:phase==2?doubles[c.button]:ups[c.button],MouseFlags(s),MAKELPARAM(local.x,local.y));
            if(!down)EndPointerGesture(s);
        }
        r.delivered=1;
    } else if(c.op==Op::Wheel){
        POINT screen=ScreenPoint(s);HWND target=s->capture?s->capture:Hit(s,screen);
        if(!SameThread(s,target)){Fail(r,Error::Unsupported,"Cross-thread wheel recipient is not implemented.");return;}
        Deliver(s,target,WM_MOUSEWHEEL,MAKEWPARAM(MouseFlags(s),(SHORT)c.delta),MAKELPARAM(screen.x,screen.y));r.delivered=1;
    } else if(c.op==Op::Key){
        if(c.vk<1||c.vk>255){Fail(r,Error::InvalidCommand,"Virtual key must be 1..255.");return;}
        SendKey(s,keyboard,c.vk,c.down!=0,true);
        r.delivered=1;
    } else if(c.op==Op::Text){
        if(c.textLength>MaxText){Fail(r,Error::InvalidCommand,"Text exceeds protocol bound.");return;}
        for(unsigned i=0;i<c.textLength;i++)Deliver(s,keyboard,WM_CHAR,c.text[i],1);
        r.delivered=1;
    } else if(c.op==Op::Release){
        // Release only synthetic state. Never send physical key-up or call system capture APIs.
        for(unsigned vk=1;vk<256;vk++)if((s->keys[vk]&0x80)&&vk!=VK_LBUTTON&&vk!=VK_RBUTTON&&vk!=VK_MBUTTON&&SpecificKey(vk)==vk)SendKey(s,keyboard,vk,false,false);
        POINT screen=ScreenPoint(s);HWND target=s->capture?s->capture:Hit(s,screen);POINT local=screen;ScreenToClient(target,&local);
        static const UINT ups[]={WM_LBUTTONUP,WM_RBUTTONUP,WM_MBUTTONUP};
        for(unsigned i=0;i<3;i++)if(s->buttons&(1u<<i)){s->buttons&=~(1u<<i);Deliver(s,target,ups[i],MouseFlags(s),MAKELPARAM(local.x,local.y));}
        Clear(s);if(s->hover){HWND previous=s->hover;s->hover=nullptr;Leave(s,previous);}s->leaveTracking.fill(nullptr);r.delivered=1;
    } else if(c.op!=Op::State&&c.op!=Op::Capabilities&&c.op!=Op::Ping){Fail(r,Error::InvalidCommand,"Unknown operation.");}
    Snapshot(s,r);
}
LRESULT CALLBACK Before(int code,WPARAM w,LPARAM l){
    Count count;
    auto s=g_session.load();
    if(code>=0&&s){
        auto p=(CWPSTRUCT*)l;
        // HWND values may be reused on this same process/thread. Latch closure
        // before the handle can be recycled instead of relying on polling.
        if(p->hwnd==s->root&&p->message==WM_NCDESTROY){s->destroyed=true;s->enabled=false;s->pending=false;}
        if(p->hwnd==s->root&&p->message==s->dispatchMessage&&p->wParam==(WPARAM)s->config.token&&p->lParam==(LPARAM)s->command.sequence&&s->pending.exchange(false)){
            s->dispatching.fetch_add(1);Session* old=tlsSession;HWND oldWindow=tlsWindow;bool oldReleasing=tlsReleasing;tlsSession=s;tlsWindow=s->root;tlsReleasing=s->command.op==Op::Release;
            Dispatch(s);tlsReleasing=oldReleasing;tlsWindow=oldWindow;tlsSession=old;s->dispatching.fetch_sub(1);
        }else if(tlsSession==s){if(tlsDepth<128)tlsStack[tlsDepth++]=tlsWindow;tlsWindow=p->hwnd;}
    }
    return CallNextHookEx(nullptr,code,w,l);
}
LRESULT CALLBACK After(int code,WPARAM w,LPARAM l){
    Count count;
    if(code>=0&&tlsSession&&tlsDepth){tlsWindow=tlsStack[--tlsDepth];}
    return CallNextHookEx(nullptr,code,w,l);
}
bool Execute(Session* s,const Command& c,Reply& r){
    s->command=c;s->reply={};s->reply.sequence=c.sequence;s->pending=true;
    DWORD_PTR result=0;
    BOOL ok=SendMessageTimeoutW(s->root,s->dispatchMessage,(WPARAM)s->config.token,(LPARAM)c.sequence,SMTO_ABORTIFHUNG|SMTO_BLOCK,1500,&result);
    if(!ok||s->pending.load()||s->dispatching.load()){
        s->pending=false;s->enabled=false;r={};r.sequence=c.sequence;r.revoked=1;Fail(r,Error::Timeout,"Target dispatch timed out; session revoked. Detach waits for any running callback.");return false;
    }
    r=s->reply;return r.error==Error::None;
}
bool WriteReply(Session* s,const Reply& r){DWORD written=0;return WriteFile(s->pipe,&r,sizeof(r),&written,nullptr)&&written==sizeof(r);}
BOOL CALLBACK FindBarrierWindow(HWND hwnd,LPARAM parameter){
    auto selected=(HWND*)parameter;DWORD pid=0;GetWindowThreadProcessId(hwnd,&pid);
    if(pid!=GetCurrentProcessId())return TRUE;*selected=hwnd;return FALSE;
}
bool ThreadHookBarrier(Session* s){
    if(s->hooksBarrier)return true;
    if(!s->everThreadHook){s->hooksBarrier=true;return true;}
    if(!s->targetThread){s->unloadReason="Target UI-thread lifetime could not be inspected.";return false;}
    bool exited=WaitForSingleObject(s->targetThread,0)==WAIT_OBJECT_0;
    if(exited){s->hooksBarrier=true;return true;}
    HWND hwnd=Identity(s)?s->root:nullptr;
    if(!hwnd)EnumThreadWindows(s->config.targetThread,FindBarrierWindow,(LPARAM)&hwnd);
    if(!hwnd){s->unloadReason="No responsive window can establish the UI-thread hook barrier.";return false;}
    DWORD_PTR ignored=0;
    if(!SendMessageTimeoutW(hwnd,WM_NULL,0,0,SMTO_ABORTIFHUNG|SMTO_BLOCK,1500,&ignored)){
        s->unloadReason="The UI-thread hook barrier has not completed.";return false;
    }
    s->hooksBarrier=true;return true;
}
bool RemoveHooks(Session* s){
    s->enabled=false;s->pending=false;
    ULONGLONG dispatchDeadline=GetTickCount64()+2000;
    while(s->dispatching.load()&&GetTickCount64()<dispatchDeadline)Sleep(5);
    if(s->dispatching.load()){s->unloadReason="A synthetic target dispatch is still running.";return false;}
    bool threadExited=s->targetThread&&WaitForSingleObject(s->targetThread,0)==WAIT_OBJECT_0;
    if(s->before){if(!UnhookWindowsHookEx(s->before)&&!threadExited){s->unloadReason="The pre-dispatch Windows hook was not removed.";return false;}s->before=nullptr;}
    if(s->after){if(!UnhookWindowsHookEx(s->after)&&!threadExited){s->unloadReason="The post-dispatch Windows hook was not removed.";return false;}s->after=nullptr;}
    if(!ThreadHookBarrier(s))return false;
    MH_STATUS status=MH_DisableHook(MH_ALL_HOOKS);
    if(status!=MH_OK&&status!=MH_ERROR_DISABLED&&status!=MH_ERROR_NOT_INITIALIZED)return false;
    ULONGLONG deadline=GetTickCount64()+2000;
    while((g_callbacks.load()||s->dispatching.load())&&GetTickCount64()<deadline)Sleep(5);
    if(g_callbacks.load()||s->dispatching.load()){s->unloadReason="A hook callback remains active.";return false;}
    {
        // Count can reach zero before a C++ epilogue has returned. MinHook only
        // relocates trampoline IPs, so establish code AND stack quiescence.
        FrozenThreadAudit audit;
        if(!audit.Freeze()||!audit.Outside(g_codeRanges.data(),g_codeRangeCount)){s->unloadReason=audit.Reason();return false;}
        if(g_callbacks.load()||s->dispatching.load()){s->unloadReason="A callback entered during the audit.";return false;}
    }
    Clear(s);
    status=MH_Uninitialize();
    return status==MH_OK||status==MH_ERROR_NOT_INITIALIZED;
}
DWORD WINAPI Monitor(void* parameter){
    auto s=(Session*)parameter;
    while(!s->monitorDone.load()){
        if((s->host&&WaitForSingleObject(s->host,0)!=WAIT_TIMEOUT)||
           (s->revokeEvent&&WaitForSingleObject(s->revokeEvent,0)==WAIT_OBJECT_0)){
            s->enabled=false;s->pending=false;break;
        }
        Sleep(20);
    }
    return 0;
}
DWORD WINAPI Worker(void* parameter){
    auto s=(Session*)parameter;
    s->host=OpenProcess(SYNCHRONIZE,FALSE,s->config.hostPid);
    s->targetThread=OpenThread(SYNCHRONIZE|THREAD_QUERY_LIMITED_INFORMATION,FALSE,s->config.targetThread);
    s->revokeEvent=OpenEventW(SYNCHRONIZE,FALSE,s->config.revokeEventName);
    if(s->host){
        for(unsigned i=0;i<60;i++){
            s->pipe=CreateFileW(s->config.pipeName,GENERIC_READ|GENERIC_WRITE,0,nullptr,OPEN_EXISTING,0,nullptr);
            if(s->pipe!=INVALID_HANDLE_VALUE)break;
            if(WaitForSingleObject(s->host,0)!=WAIT_TIMEOUT)break;
            Sleep(25);
        }
    }
    ULONG serverPid=0;
    bool connected=s->revokeEvent&&s->pipe!=INVALID_HANDLE_VALUE&&GetNamedPipeServerProcessId(s->pipe,&serverPid)&&serverPid==s->config.hostPid;
    Reply hello{};
    if(!connected||!Identity(s)||!s->targetThread||GetProcessIdOfThread(s->targetThread)!=s->config.targetPid)Fail(hello,Error::InvalidTarget,"Target identity or host pipe authentication failed.");
    else if(!InstallHooks())Fail(hello,Error::Internal,"Installing native detours failed.");
    else{
        s->dispatchMessage=RegisterWindowMessageW(s->config.pipeName);
        // This DLL already runs in the target process. Supplying hMod here asks
        // USER32 to load it as an injected hook module and adds loader references.
        s->before=SetWindowsHookExW(WH_CALLWNDPROC,Before,nullptr,s->config.targetThread);
        s->after=SetWindowsHookExW(WH_CALLWNDPROCRET,After,nullptr,s->config.targetThread);
        s->everThreadHook=s->before||s->after;
        if(!s->before||!s->after)Fail(hello,Error::Internal,"Installing target-thread dispatch hooks failed.");
        else{s->enabled=true;s->focus=s->root;s->monitor=CreateThread(nullptr,0,Monitor,s,0,nullptr);if(!s->monitor)Fail(hello,Error::Internal,"Watchdog creation failed.");Snapshot(s,hello);}
    }
    if(connected)WriteReply(s,hello);
    ULONGLONG last=GetTickCount64();bool detachRequested=false;Command lastCommand{};
    while(hello.error==Error::None&&s->enabled.load()){
        if(WaitForSingleObject(s->host,0)!=WAIT_TIMEOUT||!Identity(s)||GetTickCount64()-last>5000)break;
        DWORD available=0;if(!PeekNamedPipe(s->pipe,nullptr,0,nullptr,&available,nullptr))break;
        if(available<sizeof(Command)){Sleep(10);continue;}
        Command c{};DWORD read=0;if(!ReadFile(s->pipe,&c,sizeof(c),&read,nullptr)||read!=sizeof(c))break;
        last=GetTickCount64();lastCommand=c;
        if(c.magic!=Magic||c.version!=Version)break;
        if(c.op==Op::Detach){detachRequested=true;break;}
        Reply reply{};Execute(s,c,reply);if(!WriteReply(s,reply))break;
    }
    // Deliver synthetic releases before revocation while the target remains responsive.
    if(Identity(s)&&!s->dispatching.load()){Command release{};release.op=Op::Release;release.sequence=lastCommand.sequence+1;Reply ignored;Execute(s,release,ignored);}
    s->monitorDone=true;
    if(s->monitor&&WaitForSingleObject(s->monitor,2000)==WAIT_OBJECT_0){CloseHandle(s->monitor);s->monitor=nullptr;}
    bool removed=RemoveHooks(s);
    if(detachRequested){Reply bye{};bye.sequence=lastCommand.sequence;bye.hooksRemoved=removed;bye.revoked=1;if(!removed)Fail(bye,Error::Timeout,s->unloadReason);WriteReply(s,bye);}
    if(s->pipe!=INVALID_HANDLE_VALUE){CloseHandle(s->pipe);s->pipe=INVALID_HANDLE_VALUE;}
    if(s->monitor){WaitForSingleObject(s->monitor,INFINITE);CloseHandle(s->monitor);s->monitor=nullptr;}
    if(s->host){CloseHandle(s->host);s->host=nullptr;}
    if(s->revokeEvent){CloseHandle(s->revokeEvent);s->revokeEvent=nullptr;}
    if(!removed){
        // Keep the inert DLL resident until executing target code returns; unloading it sooner is unsafe.
        while(!RemoveHooks(s))Sleep(100);
    }
    if(s->targetThread){CloseHandle(s->targetThread);s->targetThread=nullptr;}
    g_session=nullptr;delete s;
    FreeLibraryAndExitThread(g_module,0);
    return 0;
}
}
extern "C" __declspec(dllexport) DWORD WINAPI BwcStart(void* configPointer){
    if(!configPointer)return 1;Config config=*(Config*)configPointer;
    if(config.magic!=Magic||config.version!=Version||config.targetPid!=GetCurrentProcessId())return 2;
    auto s=new Session();s->config=config;s->root=(HWND)(uintptr_t)config.hwnd;
    Session* expected=nullptr;if(!g_session.compare_exchange_strong(expected,s)){delete s;return 3;}
    HANDLE thread=CreateThread(nullptr,0,Worker,s,0,nullptr);
    if(!thread){g_session=nullptr;delete s;return 4;}CloseHandle(thread);return 0;
}
BOOL WINAPI DllMain(HINSTANCE module,DWORD reason,LPVOID){if(reason==DLL_PROCESS_ATTACH){g_module=module;DisableThreadLibraryCalls(module);}return TRUE;}
