#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include "wire.h"
#include "vendor/json/json.hpp"
#include <windows.h>
#include <tlhelp32.h>
#include <sddl.h>
#include <bcrypt.h>
#include <atomic>
#include <iostream>
#include <string>
#include <vector>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <deque>
#include <stdexcept>
#include <sstream>
#include <iomanip>

using json=nlohmann::json;
using namespace bwc;
struct NativeError:std::runtime_error{std::string code;NativeError(std::string c,std::string m):runtime_error(m),code(std::move(c)){};};
void Check(bool value,const char* code,const char* message){if(!value)throw NativeError(code,std::string(message)+" (Win32 "+std::to_string(GetLastError())+")");}
std::wstring Utf16(const std::string& text){
    if(text.find('\0')!=std::string::npos)throw NativeError("invalid_text","NUL is not supported.");
    int n=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),(int)text.size(),nullptr,0);
    if(!n&&!text.empty())throw NativeError("invalid_text","Text must be valid UTF-8.");
    std::wstring value(n,L'\0');if(n)MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),(int)text.size(),value.data(),n);return value;
}
std::wstring OwnDirectory(){wchar_t path[32768];DWORD count=GetModuleFileNameW(nullptr,path,32768);std::wstring s(path,count);return s.substr(0,s.find_last_of(L"\\/"));}
std::wstring Sid(){
    HANDLE token=nullptr;Check(OpenProcessToken(GetCurrentProcess(),TOKEN_QUERY,&token),"ipc_security","Cannot open own token.");
    DWORD size=0;GetTokenInformation(token,TokenUser,nullptr,0,&size);std::vector<BYTE> data(size);
    bool ok=GetTokenInformation(token,TokenUser,data.data(),size,&size);CloseHandle(token);Check(ok,"ipc_security","Cannot read own user SID.");
    LPWSTR sid=nullptr;Check(ConvertSidToStringSidW(((TOKEN_USER*)data.data())->User.Sid,&sid),"ipc_security","Cannot format SID.");std::wstring result(sid);LocalFree(sid);return result;
}
DWORD Integrity(HANDLE process){
    HANDLE token=nullptr;Check(OpenProcessToken(process,TOKEN_QUERY,&token),"target_access_denied","Cannot read process integrity.");
    DWORD size=0;GetTokenInformation(token,TokenIntegrityLevel,nullptr,0,&size);std::vector<BYTE> data(size);
    bool ok=GetTokenInformation(token,TokenIntegrityLevel,data.data(),size,&size);CloseHandle(token);Check(ok,"target_access_denied","Cannot read integrity label.");
    auto sid=((TOKEN_MANDATORY_LABEL*)data.data())->Label.Sid;return *GetSidSubAuthority(sid,*GetSidSubAuthorityCount(sid)-1);
}
std::wstring DesktopName(HDESK desk){wchar_t name[256]{};DWORD used=0;Check(GetUserObjectInformationW(desk,UOI_NAME,name,sizeof(name),&used),"desktop_scope","Cannot inspect desktop.");return name;}
uintptr_t ModuleBase(DWORD pid,const std::wstring& path){
    HANDLE snapshot=INVALID_HANDLE_VALUE;
    for(unsigned i=0;i<5;i++){snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPMODULE|TH32CS_SNAPMODULE32,pid);if(snapshot!=INVALID_HANDLE_VALUE||GetLastError()!=ERROR_BAD_LENGTH)break;Sleep(5);}
    Check(snapshot!=INVALID_HANDLE_VALUE,"module_query_failed","Cannot verify target native modules.");
    MODULEENTRY32W entry{};entry.dwSize=sizeof(entry);uintptr_t base=0;
    if(!Module32FirstW(snapshot,&entry)){DWORD error=GetLastError();CloseHandle(snapshot);SetLastError(error);throw NativeError("module_query_failed","Cannot enumerate target native modules.");}
    do{if(_wcsicmp(entry.szExePath,path.c_str())==0){base=(uintptr_t)entry.modBaseAddr;break;}}while(Module32NextW(snapshot,&entry));
    CloseHandle(snapshot);return base;
}
struct Host{
    HANDLE process=nullptr,owner=nullptr,pipe=INVALID_HANDLE_VALUE,revoke=nullptr;
    HWND root=nullptr;DWORD targetPid=0,targetThread=0;uint64_t sequence=0;
    std::wstring dllPath,revokeEventName;bool attached=false,unloaded=false;
    std::atomic<bool> eof{false};
    ~Host(){if(revoke)SetEvent(revoke);if(pipe!=INVALID_HANDLE_VALUE)CloseHandle(pipe);if(process)CloseHandle(process);if(owner)CloseHandle(owner);if(revoke)CloseHandle(revoke);}
    bool OwnerAlive(){return owner&&WaitForSingleObject(owner,0)==WAIT_TIMEOUT;}
    bool TargetAlive(){DWORD pid=0;return process&&WaitForSingleObject(process,0)==WAIT_TIMEOUT&&IsWindow(root)&&GetWindowThreadProcessId(root,&pid)==targetThread&&pid==targetPid;}
    bool Io(bool writing,void* data,DWORD bytes,DWORD timeout,bool honorEof=true){
        OVERLAPPED operation{};operation.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);if(!operation.hEvent)return false;
        DWORD count=0;BOOL ok=writing?WriteFile(pipe,data,bytes,&count,&operation):ReadFile(pipe,data,bytes,&count,&operation);
        if(!ok&&GetLastError()!=ERROR_IO_PENDING){CloseHandle(operation.hEvent);return false;}
        ULONGLONG deadline=GetTickCount64()+timeout;
        while(!ok){
            DWORD status=WaitForSingleObject(operation.hEvent,20);
            if(status==WAIT_OBJECT_0){ok=GetOverlappedResult(pipe,&operation,&count,FALSE);break;}
            if(GetTickCount64()>=deadline||!OwnerAlive()||(honorEof&&eof.load())){
                if(revoke)SetEvent(revoke);CancelIoEx(pipe,&operation);WaitForSingleObject(operation.hEvent,1000);CloseHandle(operation.hEvent);return false;
            }
        }
        CloseHandle(operation.hEvent);return ok&&count==bytes;
    }
    void Attach(uint64_t hwnd,DWORD ownerPid){
        root=(HWND)(uintptr_t)hwnd;targetThread=GetWindowThreadProcessId(root,&targetPid);
        Check(root&&IsWindow(root)&&IsWindowVisible(root)&&targetThread&&targetPid&&GetAncestor(root,GA_ROOT)==root,"invalid_target","Select an existing visible top-level application window.");
        Check(targetPid!=GetCurrentProcessId()&&targetPid!=ownerPid,"invalid_target","Controller cannot target itself.");
        owner=OpenProcess(SYNCHRONIZE,FALSE,ownerPid);Check(OwnerAlive(),"owner_gone","Controller owner is not running.");
        DWORD ownSession=0,targetSession=0;ProcessIdToSessionId(GetCurrentProcessId(),&ownSession);ProcessIdToSessionId(targetPid,&targetSession);Check(ownSession==targetSession,"desktop_scope","Target must be on the same session.");
        HDESK input=OpenInputDesktop(0,FALSE,DESKTOP_READOBJECTS);Check(input!=nullptr,"desktop_scope","Cannot inspect input desktop.");
        std::wstring targetDesk=DesktopName(GetThreadDesktop(targetThread)),inputDesk=DesktopName(input);CloseDesktop(input);
        Check(targetDesk==inputDesk&&targetDesk==DesktopName(GetThreadDesktop(GetCurrentThreadId()))&&_wcsicmp(targetDesk.c_str(),L"Default")==0,"desktop_scope","Only the current ordinary desktop is supported.");
        process=OpenProcess(PROCESS_QUERY_INFORMATION|PROCESS_CREATE_THREAD|PROCESS_VM_OPERATION|PROCESS_VM_WRITE|PROCESS_VM_READ|SYNCHRONIZE,FALSE,targetPid);
        Check(process!=nullptr,"target_access_denied","Cannot attach to this process; no protection bypass is attempted.");
        Check(Integrity(process)<=Integrity(GetCurrentProcess()),"target_access_denied","Higher-integrity targets require a separate explicitly elevated design.");
        wchar_t processPath[32768]{};DWORD pathSize=32768;Check(QueryFullProcessImageNameW(process,0,processPath,&pathSize),"target_access_denied","Cannot identify target executable.");
        std::wstring name(processPath,pathSize);name=name.substr(name.find_last_of(L"\\/")+1);for(auto& c:name)c=(wchar_t)towlower(c);
        const wchar_t* blocked[]={L"lsass.exe",L"winlogon.exe",L"logonui.exe",L"consent.exe",L"credentialuibroker.exe",L"secure system",L"system",L"csrss.exe",L"smss.exe"};
        for(auto item:blocked)if(name==item)throw NativeError("protected_target","Security and credential processes are not supported.");
        BOOL ownWow=FALSE,targetWow=FALSE;IsWow64Process(GetCurrentProcess(),&ownWow);IsWow64Process(process,&targetWow);Check(ownWow==targetWow,"architecture_mismatch","Host architecture must match target process.");
        BYTE random[24]{};Check(BCryptGenRandom(nullptr,random,sizeof(random),BCRYPT_USE_SYSTEM_PREFERRED_RNG)==0,"ipc_security","Random generation failed.");
        std::wstringstream nonce;for(BYTE b:random)nonce<<std::hex<<std::setw(2)<<std::setfill(L'0')<<(unsigned)b;
        Config config{};config.hwnd=hwnd;config.targetPid=targetPid;config.targetThread=targetThread;config.hostPid=GetCurrentProcessId();memcpy(&config.token,random,sizeof(config.token));
        std::wstring pipeName=L"\\\\.\\pipe\\BetterWinControl-"+nonce.str(),eventName=L"Local\\BetterWinControl-Revoke-"+nonce.str();
        revokeEventName=eventName;
        wcsncpy_s(config.pipeName,pipeName.c_str(),_TRUNCATE);wcsncpy_s(config.revokeEventName,eventName.c_str(),_TRUNCATE);
        std::wstring sddl=L"D:P(A;;GA;;;"+Sid()+L")";PSECURITY_DESCRIPTOR descriptor=nullptr;
        Check(ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(),SDDL_REVISION_1,&descriptor,nullptr),"ipc_security","Cannot create private IPC ACL.");
        SECURITY_ATTRIBUTES attributes{sizeof(attributes),descriptor,FALSE};
        pipe=CreateNamedPipeW(pipeName.c_str(),PIPE_ACCESS_DUPLEX|FILE_FLAG_OVERLAPPED|FILE_FLAG_FIRST_PIPE_INSTANCE,PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT|PIPE_REJECT_REMOTE_CLIENTS,1,sizeof(Command)*2,sizeof(Reply)*2,0,&attributes);
        revoke=CreateEventW(&attributes,TRUE,FALSE,eventName.c_str());LocalFree(descriptor);
        Check(pipe!=INVALID_HANDLE_VALUE&&revoke,"ipc_security","Cannot create private IPC.");
        dllPath=OwnDirectory()+L"\\VirtualInput.dll";
        Check(GetFileAttributesW(dllPath.c_str())!=INVALID_FILE_ATTRIBUTES,"native_missing","VirtualInput.dll is missing.");
        Check(ModuleBase(targetPid,dllPath)==0,"target_busy","This process already has a native input session or pending detach.");
        HMODULE local=LoadLibraryExW(dllPath.c_str(),nullptr,DONT_RESOLVE_DLL_REFERENCES);Check(local!=nullptr,"native_missing","Cannot inspect native DLL.");
        FARPROC start=GetProcAddress(local,"BwcStart");if(!start)start=GetProcAddress(local,"BwcStart@4");
        uintptr_t startRva=(uintptr_t)start-(uintptr_t)local;FreeLibrary(local);Check(start!=nullptr,"native_missing","Native DLL entry point is missing.");
        size_t pathBytes=(dllPath.size()+1)*sizeof(wchar_t);void* remotePath=VirtualAllocEx(process,nullptr,pathBytes,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);Check(remotePath!=nullptr,"attach_failed","Cannot allocate DLL path.");
        bool write=WriteProcessMemory(process,remotePath,dllPath.c_str(),pathBytes,nullptr);Check(write,"attach_failed","Cannot write DLL path.");
        auto load=(LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"LoadLibraryW");
        HANDLE thread=CreateRemoteThread(process,nullptr,0,load,remotePath,0,nullptr);Check(thread!=nullptr,"attach_failed","Cannot start target DLL loader.");
        DWORD wait=WaitForSingleObject(thread,3000);CloseHandle(thread);if(wait!=WAIT_OBJECT_0){SetEvent(revoke);throw NativeError("attach_pending","Target loader did not finish; no target thread is terminated.");}
        VirtualFreeEx(process,remotePath,0,MEM_RELEASE);
        uintptr_t remoteBase=ModuleBase(targetPid,dllPath);Check(remoteBase!=0,"attach_failed","Target did not load native DLL.");
        void* remoteConfig=VirtualAllocEx(process,nullptr,sizeof(config),MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE);Check(remoteConfig!=nullptr,"attach_failed","Cannot allocate attach configuration.");
        Check(WriteProcessMemory(process,remoteConfig,&config,sizeof(config),nullptr),"attach_failed","Cannot write attach configuration.");
        thread=CreateRemoteThread(process,nullptr,0,(LPTHREAD_START_ROUTINE)(remoteBase+startRva),remoteConfig,0,nullptr);Check(thread!=nullptr,"attach_failed","Cannot initialize target helper.");
        wait=WaitForSingleObject(thread,3000);DWORD exitCode=1;if(wait==WAIT_OBJECT_0)GetExitCodeThread(thread,&exitCode);CloseHandle(thread);
        if(wait!=WAIT_OBJECT_0){SetEvent(revoke);throw NativeError("attach_pending","Target initialization is pending.");}VirtualFreeEx(process,remoteConfig,0,MEM_RELEASE);
        Check(exitCode==0,exitCode==3?"target_busy":"attach_failed","Native initialization rejected attachment.");
        OVERLAPPED connect{};connect.hEvent=CreateEventW(nullptr,TRUE,FALSE,nullptr);BOOL connected=ConnectNamedPipe(pipe,&connect);DWORD error=GetLastError();
        if(!connected&&error==ERROR_IO_PENDING){connected=WaitForSingleObject(connect.hEvent,3000)==WAIT_OBJECT_0;DWORD unused=0;if(connected)connected=GetOverlappedResult(pipe,&connect,&unused,FALSE);}
        else if(!connected&&error==ERROR_PIPE_CONNECTED)connected=TRUE;
        if(!connected){CancelIoEx(pipe,&connect);WaitForSingleObject(connect.hEvent,1000);}CloseHandle(connect.hEvent);Check(connected,"attach_failed","Native IPC handshake timed out.");
        ULONG clientPid=0;Check(GetNamedPipeClientProcessId(pipe,&clientPid)&&clientPid==targetPid,"ipc_security","Native IPC peer identity mismatch.");
        Reply hello{};Check(Io(false,&hello,sizeof(hello),3000,false),"attach_failed","Native initialization acknowledgement missing.");
        if(hello.error!=Error::None)throw NativeError("attach_failed",hello.message);
        attached=true;
    }
    bool WaitUnloaded(DWORD timeout){ULONGLONG deadline=GetTickCount64()+timeout;while(GetTickCount64()<deadline){if(process&&WaitForSingleObject(process,0)==WAIT_OBJECT_0){unloaded=true;return true;}try{if(ModuleBase(targetPid,dllPath)==0){unloaded=true;return true;}}catch(...){return false;}Sleep(20);}return false;}
    Reply Call(Command command,bool shutdown=false){
        if(command.op==Op::Detach&&revoke&&WaitForSingleObject(revoke,0)==WAIT_OBJECT_0){
            if(!WaitUnloaded(3000))throw NativeError("detach_pending","Revocation acknowledged, but target callbacks have not quiesced and DLL remains loaded.");
            attached=false;Reply gone{};gone.revoked=1;gone.hooksRemoved=1;return gone;
        }
        if(!attached)throw NativeError("not_attached","Native session is not attached.");
        if(!TargetAlive())throw NativeError("target_closed","Target identity is no longer live.");
        command.sequence=++sequence;Reply reply{};
        HWND before=GetForegroundWindow();
        if(!Io(true,&command,sizeof(command),2000,!shutdown)||!Io(false,&reply,sizeof(reply),5000,!shutdown))throw NativeError(eof.load()?"revoked":"native_timeout","Native reply unavailable; session revoked.");
        Check(reply.magic==Magic&&reply.version==Version&&reply.sequence==command.sequence,"native_protocol","Native reply identity mismatch.");
        HWND after=GetForegroundWindow();
        if(command.op!=Op::Detach&&before!=root&&after==root){SetEvent(revoke);throw NativeError("focus_changed","Target gained foreground during native dispatch; session revoked without restoring focus.");}
        if(reply.error!=Error::None){
            const char* code=reply.error==Error::Unsupported?"unsupported":reply.error==Error::OutOfScope?"out_of_scope":reply.error==Error::Revoked?"revoked":reply.error==Error::Timeout?(command.op==Op::Detach?"detach_pending":"target_timeout"):"native_error";
            throw NativeError(code,reply.message);
        }
        if(command.op==Op::Detach){attached=false;if(!reply.hooksRemoved||!WaitUnloaded(2500))throw NativeError("detach_pending","Hooks were removed but DLL unload has not been confirmed.");}
        return reply;
    }
};
json State(const Reply& r){
    json keys=json::array(),buttons=json::array();for(unsigned i=1;i<256;i++)if((r.keys[i]&0x80)&&i!=VK_LBUTTON&&i!=VK_RBUTTON&&i!=VK_MBUTTON)keys.push_back(i);
    const char* names[]={"left","right","middle"};for(unsigned i=0;i<3;i++)if(r.buttons&(1u<<i))buttons.push_back(names[i]);
    return {{"pointer",{{"x",r.x},{"y",r.y}}},{"heldKeys",keys},{"heldButtons",buttons},{"virtualFocusHwnd",std::to_string(r.focus)},{"virtualCaptureHwnd",std::to_string(r.capture)},{"delivered",r.delivered!=0},{"effectVerified",false},{"revoked",r.revoked!=0},{"mechanism","scoped_in_process_virtual_input"}};
}
Command Parse(const json& j){
    Command c;std::string op=j.at("op").get<std::string>();
    if(op=="capabilities")c.op=Op::Capabilities;else if(op=="state")c.op=Op::State;else if(op=="release")c.op=Op::Release;else if(op=="detach")c.op=Op::Detach;
    else if(op=="move"){c.op=Op::Move;c.x=j.at("x").get<int>();c.y=j.at("y").get<int>();}
    else if(op=="button"||op=="double_click"){c.op=op=="button"?Op::Button:Op::DoubleClick;if(c.op==Op::Button)c.down=j.at("down").get<bool>();std::string b=j.at("button").get<std::string>();if(b=="left")c.button=0;else if(b=="right")c.button=1;else if(b=="middle")c.button=2;else throw NativeError("invalid_button","Button must be left, right or middle.");}
    else if(op=="wheel"){c.op=Op::Wheel;c.delta=j.at("delta").get<int>();if(c.delta<-32768||c.delta>32767)throw NativeError("invalid_delta","Wheel delta must fit signed 16 bits.");}
    else if(op=="key"){c.op=Op::Key;c.vk=j.at("vk").get<unsigned>();c.down=j.at("down").get<bool>();if(c.vk<1||c.vk>255)throw NativeError("invalid_key","Virtual key must be 1..255.");}
    else if(op=="text"){c.op=Op::Text;std::wstring t=Utf16(j.at("text").get<std::string>());if(t.size()>MaxText)throw NativeError("text_too_long","Text exceeds 2048 UTF-16 units.");c.textLength=(uint32_t)t.size();memcpy(c.text,t.data(),t.size()*2);}
    else throw NativeError("unsupported","Unknown native operation.");
    if(c.op==Op::Button||c.op==Op::Wheel||c.op==Op::DoubleClick){
        if(j.contains("x")!=j.contains("y"))throw NativeError("invalid_coordinates","Provide x and y together.");
        if(j.contains("x")){c.hasPoint=1;c.x=j.at("x").get<int>();c.y=j.at("y").get<int>();}
    }
    return c;
}
int wmain(int argc,wchar_t** argv){
    Host host;std::string attachCode,attachMessage;uint64_t hwnd=0;DWORD ownerPid=0;
    try{
        for(int i=1;i+1<argc;i+=2){std::wstring arg=argv[i];if(arg==L"--hwnd")hwnd=std::stoull(argv[i+1]);else if(arg==L"--owner-pid")ownerPid=std::stoul(argv[i+1]);else throw NativeError("invalid_arguments","Unknown launch argument.");}
        Check(hwnd&&ownerPid,"invalid_arguments","Use --hwnd and --owner-pid.");host.Attach(hwnd,ownerPid);
    }catch(const NativeError& e){attachCode=e.code;attachMessage=e.what();}catch(const std::exception& e){attachCode="attach_failed";attachMessage=e.what();}
    struct Inbox{std::mutex mutex;std::condition_variable changed;std::deque<std::string> lines;};
    // Process lifetime bounds this reader. It alone detects EOF and revokes independently of target calls.
    auto inbox=new Inbox();
    std::thread([&host,inbox]{std::string line;while(std::getline(std::cin,line)){
        if(line.size()>32768)line="{\"op\":\"invalid_oversize\"}";
        {std::lock_guard lock(inbox->mutex);inbox->lines.push_back(std::move(line));}inbox->changed.notify_one();
    }host.eof=true;if(host.revoke)SetEvent(host.revoke);inbox->changed.notify_one();}).detach();
    ULONGLONG lastPing=GetTickCount64();
    while(true){
        std::string line;
        {std::unique_lock lock(inbox->mutex);inbox->changed.wait_for(lock,std::chrono::milliseconds(50),[&]{return !inbox->lines.empty()||host.eof.load();});if(!inbox->lines.empty()){line=std::move(inbox->lines.front());inbox->lines.pop_front();}}
        if(line.empty()){
            if(host.eof.load()||(host.owner&&!host.OwnerAlive()))break;
            if(host.attached&&GetTickCount64()-lastPing>1000){try{Command c;c.op=Op::Ping;host.Call(c);}catch(...){if(host.revoke)SetEvent(host.revoke);host.attached=false;}lastPing=GetTickCount64();}
            continue;
        }
        json id=nullptr;bool shouldExit=false;
        try{
            json request=json::parse(line);if(request.contains("id"))id=request["id"];
            if(!attachCode.empty())throw NativeError(attachCode,attachMessage);
            Command c=Parse(request);Reply reply=host.Call(c);json result=State(reply);
            if(c.op==Op::Capabilities){
                result["capabilities"]={{"move",true},{"button",true},{"wheel",true},{"key",true},{"text",true},{"release",true},{"doubleClick",true},{"nativeMenus",false},{"ownedDialogPointer",false},{"crossThreadWindows",false},{"rawInput",false},{"ime",false},{"scope","synchronous_dispatch_selected_window_and_same_thread_children"},{"maxTextUtf16Units",MaxText},{"concurrentTargetsPerProcess",1}};
                result["revocationEventName"]=std::string(host.revokeEventName.begin(),host.revokeEventName.end());
            }
            if(c.op==Op::Detach){result["hooksRemoved"]=reply.hooksRemoved!=0;result["moduleUnloaded"]=host.unloaded;result["detached"]=true;shouldExit=true;}
            result["target"]={{"hwnd",std::to_string((uintptr_t)host.root)},{"pid",host.targetPid},{"threadId",host.targetThread}};
            std::cout<<json({{"id",id},{"result",result}}).dump()<<std::endl;
        }catch(const NativeError& e){std::cout<<json({{"id",id},{"error",{{"code",e.code},{"message",e.what()}}}}).dump()<<std::endl;}
        catch(const std::exception& e){std::cout<<json({{"id",id},{"error",{{"code","invalid_command"},{"message",e.what()}}}}).dump()<<std::endl;}
        if(shouldExit)break;
    }
    if(host.revoke)SetEvent(host.revoke);
    if(host.attached)host.WaitUnloaded(3000);
    // ExitProcess avoids destruction racing the blocked stdin reader; it only terminates this host.
    ExitProcess(0);
}
