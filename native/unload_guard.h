#pragma once
#include <windows.h>
#include <tlhelp32.h>
#include <array>
#include <cstdint>
#include <cstring>

namespace bwc {
struct CodeRange { uintptr_t begin=0,end=0; };
inline bool InCode(uintptr_t address,const CodeRange* ranges,size_t count){
    for(size_t i=0;i<count;i++)if(address>=ranges[i].begin&&address<ranges[i].end)return true;
    return false;
}

// This is a conservative reclamation check, not a stack unwinder. False
// positives retain the inert module. Call only after entry points have been
// removed and the target-thread Windows-hook barrier has completed.
class FrozenThreadAudit {
    static constexpr size_t MaxThreads=512,MaxStack=2*1024*1024;
    struct Thread { HANDLE handle=nullptr;bool suspended=false; };
    struct BasicInformation {
        LONG exitStatus; void* teb; struct { HANDLE process,thread; } clientId;
        ULONG_PTR affinity; LONG priority,basePriority;
    };
    using QueryThread=LONG (NTAPI*)(HANDLE,ULONG,void*,ULONG,ULONG*);
    std::array<Thread,MaxThreads> threads{};
    size_t count=0;
    QueryThread query=nullptr;
    const char* problem="Thread audit not completed.";
public:
    FrozenThreadAudit()=default;
    FrozenThreadAudit(const FrozenThreadAudit&)=delete;
    ~FrozenThreadAudit(){
        for(size_t i=0;i<count;i++)if(threads[i].suspended)ResumeThread(threads[i].handle);
        for(size_t i=0;i<count;i++)if(threads[i].handle)CloseHandle(threads[i].handle);
    }
    const char* Reason()const{return problem;}
    bool Freeze(){
        query=(QueryThread)GetProcAddress(GetModuleHandleW(L"ntdll.dll"),"NtQueryInformationThread");
        if(!query){problem="Thread information API unavailable.";return false;}
        HANDLE snapshot=CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD,0);
        if(snapshot==INVALID_HANDLE_VALUE){problem="Thread enumeration unavailable.";return false;}
        THREADENTRY32 entry{};entry.dwSize=sizeof(entry);
        bool valid=Thread32First(snapshot,&entry)!=FALSE;
        if(!valid)problem="Thread enumeration failed.";
        while(valid){
            if(entry.th32OwnerProcessID==GetCurrentProcessId()&&entry.th32ThreadID!=GetCurrentThreadId()){
                if(count==MaxThreads){problem="Thread audit exceeds 512 threads.";valid=false;break;}
                HANDLE h=OpenThread(THREAD_SUSPEND_RESUME|THREAD_GET_CONTEXT|THREAD_QUERY_INFORMATION|SYNCHRONIZE,FALSE,entry.th32ThreadID);
                if(!h){problem="A target thread cannot be inspected.";valid=false;break;}
                threads[count++].handle=h;
            }
            entry.dwSize=sizeof(entry);
            if(!Thread32Next(snapshot,&entry)){valid=GetLastError()==ERROR_NO_MORE_FILES;break;}
        }
        CloseHandle(snapshot);
        if(!valid)return false;
        // All allocations, module lookups and handle opens precede suspension.
        for(size_t i=0;i<count;i++){
            auto& t=threads[i];
            if(WaitForSingleObject(t.handle,0)==WAIT_OBJECT_0)continue;
            if(SuspendThread(t.handle)==DWORD(-1)){problem="A target thread could not be suspended.";return false;}
            t.suspended=true;
        }
        return true;
    }
    bool Outside(const CodeRange* ranges,size_t rangeCount){
        alignas(uintptr_t) std::array<unsigned char,4096> bytes{};
        ULONGLONG deadline=GetTickCount64()+100;
        for(size_t i=0;i<count;i++){
            auto& t=threads[i];if(!t.suspended)continue;
            CONTEXT context{};context.ContextFlags=CONTEXT_CONTROL;
            if(!GetThreadContext(t.handle,&context)){problem="A target context could not be read.";return false;}
#if defined(__x86_64__) || defined(_M_X64)
            uintptr_t ip=context.Rip,sp=context.Rsp;
#else
            uintptr_t ip=context.Eip,sp=context.Esp;
#endif
            if(InCode(ip,ranges,rangeCount)){problem="A thread is still executing helper code.";return false;}
            BasicInformation basic{};
            if(query(t.handle,0,&basic,sizeof(basic),nullptr)<0||!basic.teb){problem="A target TEB could not be inspected.";return false;}
            NT_TIB tib{};SIZE_T read=0;
            if(!ReadProcessMemory(GetCurrentProcess(),basic.teb,&tib,sizeof(tib),&read)||read!=sizeof(tib)){problem="A target stack boundary could not be read.";return false;}
            uintptr_t top=(uintptr_t)tib.StackBase,bottom=(uintptr_t)tib.StackLimit;
            if(sp<bottom||sp>top||top-sp>MaxStack){problem="Active stack exceeds the bounded audit.";return false;}
            uintptr_t cursor=sp&~(sizeof(uintptr_t)-1);
            while(cursor<top){
                if(GetTickCount64()>deadline){problem="Stack inspection exceeded 100 milliseconds.";return false;}
                SIZE_T amount=(SIZE_T)((top-cursor)<bytes.size()?top-cursor:bytes.size());
                if(!ReadProcessMemory(GetCurrentProcess(),(void*)cursor,bytes.data(),amount,&read)||read!=amount){problem="An active stack page could not be read.";return false;}
                for(size_t j=0;j+sizeof(uintptr_t)<=amount;j+=sizeof(uintptr_t)){
                    uintptr_t candidate=0;memcpy(&candidate,bytes.data()+j,sizeof(candidate));
                    if(InCode(candidate,ranges,rangeCount)){problem="An active stack still references helper code.";return false;}
                }
                cursor+=amount;
            }
        }
        problem="";return true;
    }
};
}
