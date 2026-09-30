#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <cstdio>
#ifdef BWC_AUDIT_DLL
static volatile LONG callbacks=0;
extern "C" __declspec(dllexport) LONG WINAPI Counter(){return InterlockedCompareExchange(&callbacks,0,0);}
extern "C" __declspec(dllexport) void WINAPI HoldAfterCounter(HANDLE entered,HANDLE release){
    InterlockedIncrement(&callbacks);
    InterlockedDecrement(&callbacks);
    // Deliberately mimic a post-destructor epilogue calling an external helper:
    // zero callbacks and IP in KERNELBASE/NTDLL, with a return into this DLL.
    SetEvent(entered);
    WaitForSingleObject(release,INFINITE);
    InterlockedExchange(&callbacks,0); // Prevent a tail call that removes the DLL return.
}
BOOL WINAPI DllMain(HINSTANCE,DWORD,LPVOID){return TRUE;}
#else
#include "../unload_guard.h"
using Hold=void (WINAPI*)(HANDLE,HANDLE);
struct Work { Hold hold;HANDLE entered,release; };
DWORD WINAPI Run(void* parameter){auto work=(Work*)parameter;work->hold(work->entered,work->release);return 0;}
int wmain(int argc,wchar_t** argv){
    if(argc!=2)return 2;
    HMODULE module=LoadLibraryW(argv[1]);if(!module)return 3;
    auto count=(LONG (WINAPI*)())GetProcAddress(module,"Counter");
    auto hold=(Hold)GetProcAddress(module,"HoldAfterCounter");if(!count||!hold)return 4;
    auto dos=(PIMAGE_DOS_HEADER)module;auto nt=(PIMAGE_NT_HEADERS)((BYTE*)module+dos->e_lfanew);
    bwc::CodeRange range{(uintptr_t)module,(uintptr_t)module+nt->OptionalHeader.SizeOfImage};
    Work work{hold,CreateEventW(nullptr,TRUE,FALSE,nullptr),CreateEventW(nullptr,TRUE,FALSE,nullptr)};
    HANDLE thread=CreateThread(nullptr,0,Run,&work,0,nullptr);if(!thread)return 5;
    bool entered=WaitForSingleObject(work.entered,2000)==WAIT_OBJECT_0,zero=entered&&count()==0,rejected=false;
    const char* reason="";
    {
        bwc::FrozenThreadAudit audit;
        if(audit.Freeze()){rejected=!audit.Outside(&range,1);reason=audit.Reason();}
    }
    SetEvent(work.release);bool returned=WaitForSingleObject(thread,2000)==WAIT_OBJECT_0;
    CloseHandle(thread);CloseHandle(work.entered);CloseHandle(work.release);
    bool clear=false;
    {bwc::FrozenThreadAudit audit;clear=audit.Freeze()&&audit.Outside(&range,1);}
    bool freed=clear&&FreeLibrary(module)!=FALSE;
    bool absent=freed&&GetModuleHandleW(argv[1])==nullptr;
    bool stackRejected=rejected&&strcmp(reason,"An active stack still references helper code.")==0;
    printf("{\"passed\":%s,\"counterZero\":%s,\"pendingReturnRejected\":%s,\"reason\":\"%s\",\"threadReturned\":%s,\"clearAfterReturn\":%s,\"moduleAbsent\":%s}\n",zero&&stackRejected&&returned&&clear&&absent?"true":"false",zero?"true":"false",stackRejected?"true":"false",reason,returned?"true":"false",clear?"true":"false",absent?"true":"false");
    return zero&&stackRejected&&returned&&clear&&absent?0:1;
}
#endif
