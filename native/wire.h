#pragma once
#include <windows.h>
#include <cstdint>

namespace bwc {
constexpr uint32_t Magic = 0x42574332;
constexpr uint32_t Version = 1;
constexpr size_t MaxText = 2048;
enum class Op : uint32_t { Capabilities=1, Move, Button, Wheel, Key, Text, Release, Detach, State, Ping, DoubleClick };
enum class Error : uint32_t { None, InvalidTarget, InvalidCommand, OutOfScope, Unsupported, Timeout, Revoked, Internal, Busy };
#pragma pack(push, 8)
struct Config {
    uint32_t magic=Magic, version=Version;
    uint64_t hwnd=0;
    uint32_t targetPid=0, targetThread=0, hostPid=0;
    uint64_t token=0;
    wchar_t pipeName[160]{};
    wchar_t revokeEventName[160]{};
};
struct Command {
    uint32_t magic=Magic, version=Version;
    uint64_t sequence=0;
    Op op=Op::State;
    int32_t x=0, y=0, delta=0;
    uint32_t button=0, vk=0, down=0, textLength=0, hasPoint=0;
    wchar_t text[MaxText]{};
};
struct Reply {
    uint32_t magic=Magic, version=Version;
    uint64_t sequence=0;
    Error error=Error::None;
    uint32_t delivered=0, hooksRemoved=0, revoked=0;
    int32_t x=0,y=0;
    uint64_t focus=0,capture=0;
    uint8_t keys[256]{};
    uint32_t buttons=0;
    char message[256]{};
};
#pragma pack(pop)
}
