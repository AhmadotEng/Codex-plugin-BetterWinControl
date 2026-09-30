#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <atomic>
#include <iostream>
#include <string>
#include <thread>
#include "../vendor/json/json.hpp"

using json=nlohmann::json;
HWND root=nullptr,edit=nullptr;
HANDLE ready=nullptr;
std::atomic<int> clicks{0},chords{0};
LRESULT CALLBACK WindowProc(HWND hwnd,UINT msg,WPARAM w,LPARAM l){
    if(msg==WM_LBUTTONDOWN){clicks++;SetCapture(hwnd);return 0;}
    if(msg==WM_LBUTTONUP){ReleaseCapture();return 0;}
    if(msg==WM_KEYDOWN&&w=='K'&&(GetKeyState(VK_CONTROL)&0x8000)){chords++;return 0;}
    if(msg==WM_DESTROY){PostQuitMessage(0);return 0;}
    return DefWindowProcW(hwnd,msg,w,l);
}
std::string Utf8(const std::wstring& text){int size=WideCharToMultiByte(CP_UTF8,0,text.data(),(int)text.size(),nullptr,0,nullptr,nullptr);std::string result(size,0);WideCharToMultiByte(CP_UTF8,0,text.data(),(int)text.size(),result.data(),size,nullptr,nullptr);return result;}
int main(){
    ready=CreateEventW(nullptr,TRUE,FALSE,nullptr);
    std::thread ui([]{
        WNDCLASSW cls{};cls.hInstance=GetModuleHandleW(nullptr);cls.lpszClassName=L"BetterWinControlNativeSmokeFixture";cls.lpfnWndProc=WindowProc;cls.hbrBackground=(HBRUSH)(COLOR_WINDOW+1);RegisterClassW(&cls);
        root=CreateWindowExW(0,cls.lpszClassName,L"BetterWinControl owned x86 smoke fixture",WS_OVERLAPPEDWINDOW,40,70,500,280,nullptr,nullptr,cls.hInstance,nullptr);
        edit=CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"seed",WS_CHILD|WS_VISIBLE|ES_AUTOHSCROLL,20,130,410,35,root,nullptr,cls.hInstance,nullptr);
        ShowWindow(root,SW_SHOWNOACTIVATE);UpdateWindow(root);SetEvent(ready);
        MSG msg{};while(GetMessageW(&msg,nullptr,0,0)>0){TranslateMessage(&msg);DispatchMessageW(&msg);}
    });
    WaitForSingleObject(ready,3000);CloseHandle(ready);
    std::cout<<json({{"pid",GetCurrentProcessId()},{"hwnd",(uint64_t)(uintptr_t)root},{"editHwnd",(uint64_t)(uintptr_t)edit},{"pointerBits",sizeof(void*)*8}}).dump()<<std::endl;
    std::string command;while(std::getline(std::cin,command)){
        if(command=="quit")break;
        if(command=="state"){
            wchar_t buffer[4096]{};SendMessageW(edit,WM_GETTEXT,4096,(LPARAM)buffer);
            std::cout<<json({{"clicks",clicks.load()},{"chords",chords.load()},{"text",Utf8(buffer)}}).dump()<<std::endl;
        }
    }
    PostMessageW(root,WM_CLOSE,0,0);ui.join();return 0;
}
