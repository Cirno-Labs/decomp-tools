/* The tracer runtime (trace.h). */

#include "trace.h"

static HANDLE s_out = INVALID_HANDLE_VALUE;
static BYTE s_buf[1 << 20];
static DWORD s_used;
static char s_info[1024];
static int s_infoLen;

static DWORD(WINAPI *s_realTimeGetTime)(void);
static DWORD s_speed;
static DWORD s_clockBase;
static LONG s_fakeTime = 0x10000;

static void (*s_crashed)(DWORD code, DWORD address);

DWORD TraceNextNumber(const char **s)
{
    DWORD n = 0;
    DWORD base = 10;
    const char *p = *s;
    char c;

    if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X'))
    {
        base = 16;
        p += 2;
    }
    for (;; p++)
    {
        c = *p;
        if (c >= '0' && c <= '9')
        {
            n = n * base + c - '0';
        }
        else if (base == 16 && c >= 'a' && c <= 'f')
        {
            n = n * 16 + c - 'a' + 10;
        }
        else if (base == 16 && c >= 'A' && c <= 'F')
        {
            n = n * 16 + c - 'A' + 10;
        }
        else
        {
            break;
        }
    }
    if (*p == ',')
    {
        p++;
    }
    *s = p;
    return n;
}

DWORD TraceEnvNumber(const char *name, DWORD otherwise)
{
    char buf[32];
    const char *p = buf;

    if (GetEnvironmentVariableA(name, buf, sizeof(buf)) == 0)
    {
        return otherwise;
    }
    return TraceNextNumber(&p);
}

BOOL TraceEnvString(const char *name, char *buf, DWORD size)
{
    DWORD len = GetEnvironmentVariableA(name, buf, size);

    return len != 0 && len < size;
}

BOOL TraceReadAddrs(const char **names, DWORD *addrs, int count)
{
    static char text[4096];
    const char *s = text;
    DWORD seen[64];
    int len;
    int i;

    if (count > 64 || !TraceEnvString("TRACE_ADDR", text, sizeof(text)))
    {
        return FALSE;
    }
    for (i = 0; i < count; i++)
    {
        seen[i] = 0;
    }
    while (*s != '\0')
    {
        for (len = 0; s[len] != '=' && s[len] != '\0'; len++)
        {
        }
        for (i = 0; i < count; i++)
        {
            if (lstrlenA(names[i]) == len &&
                CompareStringA(LOCALE_INVARIANT, 0, s, len, names[i], len) == CSTR_EQUAL)
            {
                break;
            }
        }
        if (i == count || s[len] != '=' || seen[i])
        {
            return FALSE;
        }
        s += len + 1;
        addrs[i] = TraceNextNumber(&s);
        seen[i] = 1;
    }
    for (i = 0; i < count; i++)
    {
        if (!seen[i])
        {
            return FALSE;
        }
    }
    return TRUE;
}

BOOL TraceWriteCode(void *at, const void *bytes, DWORD size)
{
    DWORD protect;

    if (!VirtualProtect(at, size, PAGE_EXECUTE_READWRITE, &protect))
    {
        return FALSE;
    }
    CopyMemory(at, bytes, size);
    VirtualProtect(at, size, protect, &protect);
    FlushInstructionCache(GetCurrentProcess(), at, size);
    return TRUE;
}

int TracePatchImmediates(DWORD start, DWORD size, DWORD from, DWORD to)
{
    BYTE *p;
    int count = 0;

    for (p = (BYTE *)start; p + 4 <= (BYTE *)(start + size); p++)
    {
        if (*(DWORD *)p == from && TraceWriteCode(p, &to, 4))
        {
            count++;
        }
    }
    return count;
}

int TracePatchCalls(DWORD start, DWORD size, DWORD from, DWORD to)
{
    BYTE *p;
    DWORD rel;
    int count = 0;

    for (p = (BYTE *)start; p + 5 <= (BYTE *)(start + size); p++)
    {
        if ((p[0] == 0xe8 || p[0] == 0xe9) && (DWORD)p + 5 + *(DWORD *)(p + 1) == from)
        {
            rel = to - ((DWORD)p + 5);
            if (TraceWriteCode(p + 1, &rel, 4))
            {
                count++;
            }
        }
    }
    return count;
}

void *TracePatchImport(const char *dll, const char *name, void *to)
{
    BYTE *base = (BYTE *)GetModuleHandleA(NULL);
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    IMAGE_DATA_DIRECTORY *dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    IMAGE_IMPORT_DESCRIPTOR *desc;
    IMAGE_THUNK_DATA *names;
    IMAGE_THUNK_DATA *funcs;
    void *old;

    for (desc = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir->VirtualAddress); desc->Name != 0; desc++)
    {
        if (lstrcmpiA((char *)(base + desc->Name), dll) != 0)
        {
            continue;
        }
        names = (IMAGE_THUNK_DATA *)(base + (desc->OriginalFirstThunk ? desc->OriginalFirstThunk : desc->FirstThunk));
        funcs = (IMAGE_THUNK_DATA *)(base + desc->FirstThunk);
        for (; names->u1.AddressOfData != 0; names++, funcs++)
        {
            if (names->u1.Ordinal & IMAGE_ORDINAL_FLAG32)
            {
                continue;
            }
            if (lstrcmpA((char *)((IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData))->Name, name) == 0)
            {
                old = (void *)funcs->u1.Function;
                TraceWriteCode(&funcs->u1.Function, &to, 4);
                return old;
            }
        }
    }
    return NULL;
}

BOOL TraceOpen(const char *path)
{
    s_out = CreateFileA(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, 0, NULL);
    return s_out != INVALID_HANDLE_VALUE;
}

void TraceInfo(const char *fmt, ...)
{
    va_list args;

    if (s_infoLen > (int)sizeof(s_info) - 256)
    {
        return;
    }
    va_start(args, fmt);
    s_infoLen += wvsprintfA(s_info + s_infoLen, fmt, args);
    va_end(args);
}

void TraceWriteHeader(const char *names, DWORD count)
{
    DWORD header[4];
    DWORD written;

    header[0] = *(DWORD *)"TRCE";
    header[1] = 1;
    header[2] = count;
    header[3] = count * 4;
    s_info[s_infoLen++] = '\n';
    WriteFile(s_out, header, sizeof(header), &written, NULL);
    WriteFile(s_out, names, lstrlenA(names), &written, NULL);
    WriteFile(s_out, s_info, s_infoLen, &written, NULL);
}

void TraceFlush(void)
{
    DWORD written;

    if (s_out != INVALID_HANDLE_VALUE && s_used != 0)
    {
        WriteFile(s_out, s_buf, s_used, &written, NULL);
    }
    s_used = 0;
}

void TraceWrite(const void *record, DWORD size)
{
    if (s_used + size > sizeof(s_buf))
    {
        TraceFlush();
    }
    CopyMemory(s_buf + s_used, record, size);
    s_used += size;
}

void TraceExit(UINT code)
{
    TraceFlush();
    if (s_out != INVALID_HANDLE_VALUE)
    {
        CloseHandle(s_out);
        s_out = INVALID_HANDLE_VALUE;
    }
    ExitProcess(code);
}

static DWORD WINAPI FakeTimeGetTime(void)
{
    if (s_speed == 0)
    {
        return InterlockedExchangeAdd(&s_fakeTime, 17) + 17;
    }
    return 0x10000 + (s_realTimeGetTime() - s_clockBase) * s_speed;
}

BOOL TraceInstallClock(void)
{
    s_speed = TraceEnvNumber("TRACE_SPEED", 0);
    *(void **)&s_realTimeGetTime = TracePatchImport("winmm.dll", "timeGetTime", (void *)FakeTimeGetTime);
    TraceInfo(" timeGetTime=%d speed=%d", s_realTimeGetTime != NULL, s_speed);
    if (s_realTimeGetTime == NULL)
    {
        return FALSE;
    }
    s_clockBase = s_realTimeGetTime();
    return TRUE;
}

static LONG WINAPI OnCrash(EXCEPTION_POINTERS *info)
{
    if (s_crashed != NULL)
    {
        s_crashed(info->ExceptionRecord->ExceptionCode, (DWORD)info->ExceptionRecord->ExceptionAddress);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

void TraceOnCrash(void (*crashed)(DWORD code, DWORD address))
{
    s_crashed = crashed;
    SetUnhandledExceptionFilter(OnCrash);
}
